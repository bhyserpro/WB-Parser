"""Получение и хранение антибот-токена Wildberries (cookie ``x_wbaas_token``).

Два способа получить токен:

1. **Импорт из вашего браузера** (надёжнее всего). Откройте поиск на
   wildberries.ru в обычном браузере, DevTools → Network → запрос ``search``
   → Copy → Copy as cURL (bash), сохраните в файл и выполните::

       python wb_token.py import --curl curl.txt

2. **Автоматически через Playwright**: скрипт сам откроет окно браузера.
   WB может распознать браузер под управлением автоматизации и не пустить —
   тогда используйте способ 1 или ``--browser-channel chrome`` (установленный
   Google Chrome вместо встроенного Chromium).
"""

from __future__ import annotations

import argparse
import json
import logging
import shlex
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, quote_plus, urlsplit

import requests

log = logging.getLogger("wb_token")

SITE = "https://www.wildberries.ru"
TOKEN_COOKIE = "x_wbaas_token"
# заголовки из cURL, которые не переносим в сессию: их выставляет сам requests
SKIP_HEADERS = {"cookie", "host", "content-length", "accept-encoding", "connection"}
IMPORT_HINT = (
    "Возьмите токен из своего браузера: DevTools → Network → запрос search → "
    "Copy as cURL (bash) → сохраните в curl.txt → python wb_token.py import --curl curl.txt"
)


class TokenRejected(Exception):
    """WB не принял токен (401/403/498) — нужно получить новый."""


class TokenStore:
    """Кэш cookie и User-Agent браузера, прошедшего проверку WB."""

    def __init__(
        self,
        profile_dir: Path,
        headless: bool = False,
        timeout: float = 180,
        use_browser: bool = True,
        browser_channel: str | None = None,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.cache_path = self.profile_dir / "token.json"
        self.headless = headless
        self.timeout = timeout
        self.use_browser = use_browser
        self.browser_channel = browser_channel
        self.cookies: list[dict] = []
        self.user_agent = ""
        self.headers: dict[str, str] = {}
        self.base_params: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        """Читает сохранённый токен, если он есть."""
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.cookies, self.user_agent = data.get("cookies", []), data.get("user_agent", "")
        self.headers, self.base_params = data.get("headers", {}), data.get("base_params", {})

    def save(self, source: str) -> None:
        """Записывает токен в кэш (файл локальный и в .gitignore)."""
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps({
            "cookies": self.cookies,
            "user_agent": self.user_agent,
            "headers": self.headers,
            "base_params": self.base_params,
            "source": source,
            "obtained_at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    @property
    def has_token(self) -> bool:
        """Есть ли в кэше cookie антибота."""
        return any(c.get("name") == TOKEN_COOKIE for c in self.cookies)

    def apply(self, session: requests.Session, query: str = "iPhone 16") -> None:
        """Кладёт cookie, заголовки и User-Agent в сессию; при пустом кэше получает токен."""
        if not self.has_token:
            self.refresh(query)
        session.headers.update(self.headers)
        for c in self.cookies:
            session.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
        if self.user_agent:
            session.headers["User-Agent"] = self.user_agent

    def refresh(self, query: str = "iPhone 16") -> None:
        """Получает новый токен через браузер (или просит импортировать вручную)."""
        if not self.use_browser:
            raise TokenRejected(f"Токена нет или он устарел. {IMPORT_HINT}")
        self._refresh_with_browser(query)

    def _refresh_with_browser(self, query: str) -> None:
        """Открывает страницу поиска и ждёт, пока сайт сам загрузит выдачу.

        Успешный ответ ``/__internal/search/`` означает, что проверка пройдена
        и cookie годятся для API.
        """
        from playwright.sync_api import Error as PwError, TimeoutError as PwTimeout, sync_playwright

        url = f"{SITE}/catalog/0/search.aspx?search={quote_plus(query)}"
        log.info("Открываю браузер для получения токена: %s", url)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            try:
                ctx = pw.chromium.launch_persistent_context(
                    str(self.profile_dir / f"browser-{self.browser_channel or 'chromium'}"),
                    channel=self.browser_channel,
                    headless=self.headless,
                    locale="ru-RU",
                    viewport={"width": 1366, "height": 900},
                )
            except PwError as exc:
                raise TokenRejected(f"Не удалось запустить браузер: {exc}. {IMPORT_HINT}") from exc
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                try:
                    with page.expect_response(
                        lambda r: "/__internal/search/" in r.url and r.status == 200,
                        timeout=self.timeout * 1000,
                    ):
                        page.goto(url, wait_until="commit")
                except PwTimeout as exc:
                    raise TokenRejected(
                        f"За {self.timeout:.0f} с сайт не отдал выдачу: WB не пропустил "
                        f"браузер под управлением скрипта. {IMPORT_HINT}"
                    ) from exc
                self.user_agent = page.evaluate("navigator.userAgent")
                self.cookies = ctx.cookies(SITE)
                self.headers, self.base_params = {}, {}
            finally:
                ctx.close()
        if not self.has_token:
            raise TokenRejected("Выдача загрузилась, но cookie x_wbaas_token не найдена")
        self.save("browser")
        log.info("Токен получен и сохранён в %s", self.cache_path)

    def import_curl(self, text: str) -> None:
        """Берёт URL, заголовки и cookie из команды «Copy as cURL (bash)».

        Повторяем запрос браузера целиком: те же заголовки и параметры URL
        (кроме query и page), чтобы WB видел тот же запрос, что и от сайта.
        """
        req = parse_curl(text)
        cookies = req["cookies"]
        if TOKEN_COOKIE not in cookies:
            raise ValueError(
                f"В команде нет cookie {TOKEN_COOKIE}. Скопируйте запрос search с "
                "wildberries.ru именно как «Copy as cURL (bash)» после загрузки выдачи."
            )
        headers = {k: v for k, v in req["headers"].items() if k.lower() not in SKIP_HEADERS}
        ua = next((v for k, v in headers.items() if k.lower() == "user-agent"), "")
        params = {k: v for k, v in req["params"].items() if k not in ("query", "page")}
        self.import_values(cookies, ua, headers, params)

    def import_values(
        self, cookies: dict[str, str], user_agent: str,
        headers: dict[str, str] | None = None, base_params: dict[str, str] | None = None,
    ) -> None:
        """Сохраняет cookie, User-Agent и (необязательно) заголовки и параметры запроса."""
        self.headers, self.base_params = headers or {}, base_params or {}
        self.cookies = [
            {"name": k, "value": v, "domain": "www.wildberries.ru", "path": "/"} for k, v in cookies.items()
        ]
        self.user_agent = user_agent
        self.save("import")


def parse_curl(text: str) -> dict:
    """Разбирает «Copy as cURL (bash)»: URL, параметры, заголовки, cookie."""
    text = text.strip()
    if text.startswith("curl ^") or '^"' in text:
        raise ValueError("Это формат cmd. В DevTools выберите «Copy as cURL (bash)».")
    args = shlex.split(text.replace("\\\r\n", " ").replace("\\\n", " "))
    url, headers, cookie_header = "", {}, ""
    i = 1
    while i < len(args):
        arg = args[i]
        value = args[i + 1] if i + 1 < len(args) else ""
        if arg in ("-H", "--header"):
            name, _, val = value.partition(":")
            if name.strip().lower() == "cookie":
                cookie_header = val.strip()
            else:
                headers[name.strip()] = val.strip()
            i += 2
        elif arg in ("-b", "--cookie"):
            cookie_header, i = value, i + 2
        elif arg in ("-A", "--user-agent"):
            headers["user-agent"], i = value, i + 2
        elif arg in ("-X", "--request", "--data", "--data-raw", "-d"):
            i += 2
        elif arg.startswith("http"):
            url, i = arg, i + 1
        else:
            i += 1  # --compressed и прочие флаги без значения
    cookies = dict(
        part.strip().split("=", 1) for part in cookie_header.split(";") if "=" in part
    )
    return {"url": url, "params": dict(parse_qsl(urlsplit(url).query)),
            "headers": headers, "cookies": cookies}


def main(argv: list[str] | None = None) -> int:
    """CLI: импорт токена из браузера или проверка, что он работает."""
    ap = argparse.ArgumentParser(description="Токен антибота Wildberries")
    ap.add_argument("--profile-dir", type=Path, default=Path(".wb_profile"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="взять токен из своего браузера")
    src = imp.add_mutually_exclusive_group(required=True)
    src.add_argument("--curl", type=Path, help="файл с «Copy as cURL (bash)» (или - для stdin)")
    src.add_argument("--token", help="значение cookie x_wbaas_token")
    imp.add_argument("--user-agent", default="",
                     help="User-Agent вашего браузера (с --token обязателен: токен к нему привязан)")
    sub.add_parser("check", help="проверить токен одним запросом к API")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store = TokenStore(args.profile_dir, use_browser=False)

    if args.cmd == "import":
        try:
            if args.curl:
                text = sys.stdin.read() if str(args.curl) == "-" else args.curl.read_text(encoding="utf-8")
                store.import_curl(text)
            else:
                if not args.user_agent:
                    ap.error("с --token укажите --user-agent (navigator.userAgent из консоли браузера)")
                store.import_values({TOKEN_COOKIE: args.token}, args.user_agent)
        except ValueError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 2
        print(f"Сохранено в {store.cache_path}: cookie {len(store.cookies)}, "
              f"User-Agent: {store.user_agent or '—'}")
        print("Файл содержит ваши cookie WB — никому его не передавайте.")

    # проверка после импорта и по команде check; при запуске как скрипта модуль
    # wb_token загружается второй раз (из wb_parser), отсюда два класса исключения
    from wb_parser import build_session, fetch_page
    from wb_token import TokenRejected as ParserTokenRejected
    try:
        data = fetch_page(build_session(store), "iPhone 16", 1, retries=1,
                          base_params=store.base_params)
    except (TokenRejected, ParserTokenRejected) as exc:
        print(f"Токен не принят: {exc}", file=sys.stderr)
        print("Проверьте, проходит ли тот же запрос из терминала: выполните команду из curl.txt "
              "в Git Bash (Windows) или Терминале (macOS/Linux). Пришлите, что ответит — "
              "первые строки и код ответа.", file=sys.stderr)
        return 1
    products = data.get("products") or (data.get("data") or {}).get("products") or []
    print(f"Токен работает: на первой странице {len(products)} товаров")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
