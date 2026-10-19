"""Мониторинг цен через ваш собственный Chrome (Playwright по CDP).

Вы сами запускаете обычный Chrome с портом отладки и сами проходите проверку
WB — токен остаётся в этом браузере. Скрипт подключается к нему, открывает
свою вкладку с выдачей и читает ответы, которые получает сам сайт (на
сетевом уровне — со всеми полями, включая продавца). Ничего не подделывает:
не создаёт токены, не меняет отпечаток браузера.

Запуск Chrome (macOS)::

    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\
        --remote-debugging-port=9222 --user-data-dir="$HOME/wb-chrome"

Затем откройте в нём wildberries.ru, при необходимости пройдите проверку, и::

    python3 wb_cdp.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from monitor import analyze_and_report, load_env, make_notifier
from wb_parser import Item, dedupe_items, filter_items, fmt_rub, parse_items, setup_logging

log = logging.getLogger("wb_cdp")

# сортировка «сначала дешёвые» и цена от 50 000 ₽ (в копейках) — аксессуары не займут первые страницы
DEFAULT_URL = ("https://www.wildberries.ru/catalog/0/search.aspx"
               "?search=iPhone%2016%20Pro%20256&sort=priceup&priceU=5000000%3B40000000")
CHALLENGE_MARK = "/__wbaas/challenges/"


def page_url(url: str, page: int) -> str:
    """Подставляет номер страницы в адрес выдачи."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "page"]
    if page > 1:
        query.append(("page", str(page)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def extract_products(data: Any) -> list[dict] | None:
    """Список товаров из ответа API выдачи (поиск или каталог); None — не выдача."""
    if not isinstance(data, dict):
        return None
    products = data.get("products")
    if products is None and isinstance(data.get("data"), dict):
        products = data["data"].get("products")
    return products if isinstance(products, list) else None


class ResponseCollector:
    """Собирает ответы сайта с товарами со всех вкладок контекста."""

    def __init__(self) -> None:
        self.responses: list[Any] = []
        self.statuses: list[int] = []

    def __call__(self, response: Any) -> None:
        if "/__internal/" not in response.url:
            return
        if response.request.resource_type not in ("fetch", "xhr"):
            return
        self.statuses.append(response.status)
        if response.status == 200 and "json" in (response.headers.get("content-type") or ""):
            self.responses.append(response)

    def reset(self) -> None:
        self.responses.clear()
        self.statuses.clear()

    def products(self) -> list[dict]:
        """Товары из накопленных ответов (тело читается сразу, до следующего перехода)."""
        out: list[dict] = []
        for resp in self.responses:
            try:
                items = extract_products(resp.json())
            except Exception:  # тело уже недоступно или не JSON
                continue
            if items:
                out.extend(items)
        return out


def is_challenge(page: Any) -> bool:
    """Показывает ли вкладка проверку антибота."""
    try:
        return page.locator(f'script[src*="{CHALLENGE_MARK}"]').count() > 0
    except Exception:
        return False


def wait_challenge(page: Any, notify: Callable[[str], None], timeout_s: float) -> None:
    """Ждёт, пока вы пройдёте проверку WB в окне Chrome."""
    msg = "WB показывает проверку в окне Chrome — пройдите её, скрипт подождёт"
    log.warning(msg)
    notify(f"⚠️ {msg}.")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        page.wait_for_timeout(3000)
        if not is_challenge(page):
            log.info("Проверка пройдена")
            return
    raise RuntimeError(f"проверка WB не пройдена за {timeout_s / 60:.0f} мин")


def collect_pages(
    page: Any, collector: ResponseCollector, url: str, max_pages: int, notify: Callable[[str], None],
    wait_s: float = 30, scrolls: int = 0, delay: tuple[float, float] = (1.0, 3.0),
    challenge_wait_s: float = 600, dump_raw: Path | None = None,
) -> list[dict]:
    """Открывает страницы выдачи по очереди и возвращает товары из ответов сайта."""
    products: list[dict] = []
    for n in range(1, max_pages + 1):
        for attempt in range(2):
            collector.reset()
            page.goto(page_url(url, n), wait_until="domcontentloaded")
            deadline = time.time() + wait_s
            while time.time() < deadline and not collector.responses and not is_challenge(page):
                page.wait_for_timeout(200)
            if collector.responses:
                page.wait_for_timeout(700)  # сайт мог сделать ещё запросы следом
            if is_challenge(page) or (not collector.responses and 498 in collector.statuses):
                wait_challenge(page, notify, challenge_wait_s)
                continue  # после проверки — та же страница ещё раз
            break
        for _ in range(scrolls):  # прокрутка: сайт может догружать карточки частями
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(int(random.uniform(*delay) * 1000))
        found = collector.products()
        if dump_raw:
            dump_raw.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            (dump_raw / f"cdp_{stamp}_p{n:03d}.json").write_text(
                json.dumps(found, ensure_ascii=False, indent=1), encoding="utf-8")
        log.info("Страница %d: товаров в ответах сайта %d (HTTP: %s)",
                 n, len(found), ", ".join(map(str, sorted(set(collector.statuses)))) or "—")
        if not found:
            if n == 1 and not collector.statuses:
                raise RuntimeError("сайт не сделал ни одного запроса выдачи — проверьте адрес --url")
            break
        products.extend(found)
        if n < max_pages:
            page.wait_for_timeout(int(random.uniform(*delay) * 1000))
    return products


BLOCKED_TYPES = {"image", "media", "font"}


def block_heavy_resources(page: Any) -> None:
    """Не грузить в своей вкладке картинки, видео и шрифты — страницы открываются в разы быстрее."""
    page.route("**/*", lambda route: route.abort() if route.request.resource_type in BLOCKED_TYPES
               else route.continue_())


def select_target(
    items: list[Item], model: str | None, memory: str | None,
    min_rating: float = 0, min_feedbacks: int = 0, min_seller_rating: float = 0,
) -> list[Item]:
    """Оставляет нужную модель и память и (если заданы) хорошие карточки и продавцов."""
    return [
        it for it in items
        if (not model or it.model == model) and (not memory or it.memory == memory)
        and (not min_rating or (it.rating or 0) >= min_rating)
        and (not min_feedbacks or (it.feedbacks or 0) >= min_feedbacks)
        and (not min_seller_rating or (it.seller_rating or 0) >= min_seller_rating)
    ]


def cheapest_text(items: list[Item], top: int = 10) -> str:
    """Короткий список самых дешёвых предложений для консоли."""
    rows = sorted((it for it in items if it.price), key=lambda it: it.price)[:top]
    return "\n".join(
        f"  {fmt_rub(it.price):>10}  ★{it.rating or '—'} ({it.feedbacks or 0} отз.)  "
        f"{it.seller or '—'} ★{it.seller_rating or '—'}  {it.url}"
        for it in rows)


def run_cycle(args: argparse.Namespace, page: Any, collector: ResponseCollector,
              notify: Callable[[str], None]) -> dict:
    """Один проход: выдача → фильтр → история → уведомление о снижениях."""
    raw = collect_pages(page, collector, args.url, args.max_pages, notify, scrolls=args.scrolls,
                        delay=(args.delay_min, args.delay_max), dump_raw=args.dump_raw)
    now = datetime.now()
    items = dedupe_items(parse_items({"products": raw}, now.isoformat(timespec="seconds")))
    items = filter_items(items, min_price=args.min_price, include_refurbished=args.include_refurbished)
    items = select_target(items, args.model, args.memory,
                          args.min_rating, args.min_feedbacks, args.min_seller_rating)
    log.info("Предложений %s %s: %d. Самые дешёвые:\n%s",
             args.model, args.memory or "", len(items), cheapest_text(items))
    label = " ".join(p for p in (args.model, args.memory) if p)
    return analyze_and_report(items, label, args.db, args.min_drop_pct, args.min_drop_rub,
                              notify, now, args.export)


def build_arg_parser() -> argparse.ArgumentParser:
    """Аргументы командной строки."""
    ap = argparse.ArgumentParser(description="Мониторинг цен WB через ваш Chrome (Playwright по CDP)")
    ap.add_argument("--cdp", default="http://127.0.0.1:9222", help="адрес порта отладки Chrome")
    ap.add_argument("--url", default=DEFAULT_URL,
                    help="адрес выдачи (лучше свой: с фильтрами и сортировкой «сначала дешёвые»)")
    ap.add_argument("--model", default="iPhone 16 Pro", help="модель (как в сводке: iPhone 16 Pro)")
    ap.add_argument("--memory", default="256 ГБ", help="объём памяти (128 ГБ / 256 ГБ / 512 ГБ / 1 ТБ)")
    ap.add_argument("-p", "--max-pages", type=int, default=3, help="страниц выдачи за проход")
    ap.add_argument("--interval", type=float, default=15, help="период, мин (0 — один проход)")
    ap.add_argument("--delay-min", type=float, default=1.0)
    ap.add_argument("--delay-max", type=float, default=3.0)
    ap.add_argument("--min-price", type=float, default=20000)
    ap.add_argument("--include-refurbished", action="store_true",
                    help="не отсекать восстановленные / б/у / уценённые")
    ap.add_argument("--min-rating", type=float, default=0, help="мин. рейтинг карточки (0 — любой)")
    ap.add_argument("--min-feedbacks", type=int, default=0, help="мин. число отзывов карточки")
    ap.add_argument("--min-seller-rating", type=float, default=0, help="мин. рейтинг продавца")
    ap.add_argument("--scrolls", type=int, default=0, help="прокруток на странице (0 — не крутить)")
    ap.add_argument("--load-images", action="store_true", help="грузить картинки (медленнее)")
    ap.add_argument("--min-drop-pct", type=float, default=0.0, help="порог снижения, %% (0 — любое)")
    ap.add_argument("--min-drop-rub", type=float, default=1.0, help="порог снижения, ₽")
    ap.add_argument("--db", type=Path, default=Path("data/prices.sqlite"))
    ap.add_argument("--export", type=Path, help="сохранять XLSX/CSV каждого прохода")
    ap.add_argument("--dump-raw", type=Path, help="сохранять товары из ответов сайта в папку")
    ap.add_argument("--dry-run", action="store_true", help="печатать сообщения вместо Telegram")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    """Подключается к Chrome и повторяет проходы с периодом --interval."""
    load_env()
    args = build_arg_parser().parse_args(argv)
    setup_logging(args.verbose)
    notify = make_notifier(args.dry_run)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(args.cdp)
        except Exception as exc:
            log.error("Не удалось подключиться к Chrome по %s: %s\nЗапустите Chrome с "
                      "--remote-debugging-port=9222 --user-data-dir=... (см. README)", args.cdp, exc)
            return 2
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        collector = ResponseCollector()
        context.on("response", collector)
        page = context.new_page()  # своя вкладка — ваши вкладки скрипт не трогает
        if not args.load_images:
            block_heavy_resources(page)
        try:
            while True:
                try:
                    res = run_cycle(args, page, collector, notify)
                    log.info("Итог: предложений %d, снижений %d, новых минимумов %d",
                             res["items"], res["drops"], res["segments"])
                except Exception as exc:
                    log.exception("Проход не удался")
                    notify(f"⚠️ Мониторинг WB: {exc}")
                if args.interval <= 0:
                    return 0
                pause = args.interval * 60 * random.uniform(0.9, 1.1)
                log.info("Следующий проход через %.0f мин", pause / 60)
                page.wait_for_timeout(pause * 1000)
        except KeyboardInterrupt:
            log.info("Остановлено")
            return 0
        finally:
            try:
                page.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
