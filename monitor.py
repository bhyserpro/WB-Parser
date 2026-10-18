"""Мониторинг снижения цен на iPhone 16 с уведомлениями в Telegram.

Каждый запуск: собирает выдачу, сохраняет цены в SQLite, сравнивает с
историей и шлёт в бота только снижения цен:

* товар подешевел относительно прошлого наблюдения (порог в % и ₽);
* цена товара — минимум за всю историю наблюдений;
* подешевел самый дешёвый вариант в сегменте «модель + память».

Запуск раз в час: ``python monitor.py --interval 3600`` или ``--once`` из cron.
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import random
import sqlite3
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import requests

from wb_parser import (
    DEFAULT_DEST, Item, _memory_order, _model_order, collect, filter_items,
    fmt_rub, save_results, setup_logging,
)
from wb_token import TokenStore

log = logging.getLogger("monitor")

TG_LIMIT = 4000  # у Telegram предел 4096 символов на сообщение

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    query TEXT NOT NULL,
    items INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    name TEXT, model TEXT, memory TEXT, color TEXT, seller TEXT, url TEXT,
    first_seen TEXT, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS observations (
    run_id INTEGER NOT NULL REFERENCES runs(run_id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    price REAL NOT NULL,
    price_basic REAL,
    PRIMARY KEY (run_id, product_id)
);
CREATE INDEX IF NOT EXISTS obs_product ON observations(product_id, run_id);
"""


@dataclass
class PriceDrop:
    """Снижение цены конкретного товара."""

    item: Item
    old_price: float
    drop_rub: float
    drop_pct: float
    all_time_low: bool
    median_7d: float | None


@dataclass
class SegmentDrop:
    """Снижение минимальной цены в сегменте «модель + память»."""

    model: str
    memory: str
    old_min: float
    new_min: float
    item: Item


# ----------------------------------------------------------------- база


def open_db(path: Path) -> sqlite3.Connection:
    """Открывает базу истории цен и создаёт таблицы."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def store_run(conn: sqlite3.Connection, query: str, items: list[Item], ts: str) -> int:
    """Сохраняет снимок цен и возвращает run_id."""
    with conn:
        run_id = conn.execute(
            "INSERT INTO runs (ts, query, items) VALUES (?, ?, ?)", (ts, query, len(items))
        ).lastrowid
        for it in items:
            conn.execute(
                """INSERT INTO products (id, name, model, memory, color, seller, url, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, model=excluded.model,
                     memory=excluded.memory, color=excluded.color, seller=excluded.seller,
                     url=excluded.url, last_seen=excluded.last_seen""",
                (it.id, it.name, it.model, it.memory, it.color, it.seller, it.url, ts, ts),
            )
            conn.execute(
                "INSERT OR REPLACE INTO observations (run_id, product_id, price, price_basic) VALUES (?, ?, ?, ?)",
                (run_id, it.id, it.price, it.price_basic),
            )
    return run_id


def previous_run(conn: sqlite3.Connection, query: str, run_id: int) -> int | None:
    """run_id предыдущего запуска с тем же запросом."""
    row = conn.execute(
        "SELECT MAX(run_id) FROM runs WHERE query = ? AND run_id < ?", (query, run_id)
    ).fetchone()
    return row[0] if row and row[0] is not None else None


# --------------------------------------------------------------- анализ


def find_price_drops(
    conn: sqlite3.Connection, items: list[Item], run_id: int,
    min_drop_pct: float, min_drop_rub: float, now: datetime,
) -> list[PriceDrop]:
    """Товары, подешевевшие относительно своего прошлого наблюдения."""
    since = (now - timedelta(days=7)).isoformat(timespec="seconds")
    drops: list[PriceDrop] = []
    for it in items:
        if it.price is None:
            continue
        history = conn.execute(
            """SELECT o.price, r.ts FROM observations o JOIN runs r USING (run_id)
               WHERE o.product_id = ? AND o.run_id < ? ORDER BY o.run_id DESC""",
            (it.id, run_id),
        ).fetchall()
        if not history:
            continue
        old = history[0][0]
        drop_rub = old - it.price
        drop_pct = drop_rub / old * 100 if old else 0
        if drop_rub < min_drop_rub or drop_pct < min_drop_pct:
            continue
        week = [p for p, ts in history if ts >= since]
        drops.append(PriceDrop(
            item=it, old_price=old, drop_rub=drop_rub, drop_pct=drop_pct,
            all_time_low=it.price < min(p for p, _ in history),
            median_7d=statistics.median(week) if week else None,
        ))
    return sorted(drops, key=lambda d: d.drop_pct, reverse=True)


def segment_minimums(items: list[Item]) -> dict[tuple[str, str], Item]:
    """Самый дешёвый товар в каждом сегменте «модель + память»."""
    best: dict[tuple[str, str], Item] = {}
    for it in items:
        if it.price is None or not it.model or not it.memory:
            continue
        key = (it.model, it.memory)
        if key not in best or it.price < best[key].price:
            best[key] = it
    return best


def find_segment_drops(
    conn: sqlite3.Connection, items: list[Item], prev_run_id: int | None, min_drop_rub: float,
) -> list[SegmentDrop]:
    """Сегменты, где минимальная цена стала ниже, чем в прошлый запуск."""
    if prev_run_id is None:
        return []
    prev = {
        (model, memory): price
        for model, memory, price in conn.execute(
            """SELECT p.model, p.memory, MIN(o.price) FROM observations o
               JOIN products p ON p.id = o.product_id
               WHERE o.run_id = ? AND p.model IS NOT NULL AND p.memory IS NOT NULL
               GROUP BY p.model, p.memory""",
            (prev_run_id,),
        )
    }
    drops = []
    for key, it in segment_minimums(items).items():
        old = prev.get(key)
        if old is not None and old - it.price >= min_drop_rub:
            drops.append(SegmentDrop(key[0], key[1], old, it.price, it))
    return sorted(drops, key=lambda d: (_model_order(d.model), _memory_order(d.memory)))


# -------------------------------------------------------------- сообщения


def _link(it: Item, text: str) -> str:
    return f'<a href="{html.escape(it.url)}">{html.escape(text)}</a>'


def _title(it: Item) -> str:
    parts = [it.model or it.name[:40], it.memory, it.color]
    return " ".join(p for p in parts if p)


def format_report(
    query: str, now: datetime, items: list[Item],
    drops: list[PriceDrop], segments: list[SegmentDrop], first_run: bool, top: int = 25,
) -> str | None:
    """Готовит HTML-текст для Telegram; None — если сообщать нечего."""
    stamp = now.strftime("%d.%m %H:%M")
    if first_run:
        lines = [f"<b>База создана</b> ({html.escape(query)}, {stamp}): товаров {len(items)}.",
                 "Минимальные цены сейчас:"]
        for (model, memory), it in sorted(segment_minimums(items).items(),
                                          key=lambda kv: (_model_order(kv[0][0]), _memory_order(kv[0][1]))):
            lines.append(f"• {_link(it, f'{model} {memory}')} — {fmt_rub(it.price)}")
        lines.append("Дальше буду присылать только снижения цен.")
        return "\n".join(lines)
    if not drops and not segments:
        return None
    lines = [f"📉 <b>Снижение цен</b> — {html.escape(query)}, {stamp}"]
    if segments:
        lines.append("\n<b>Новые минимумы по сегментам:</b>")
        for s in segments:
            lines.append(
                f"• {_link(s.item, f'{s.model} {s.memory}')}: {fmt_rub(s.new_min)} "
                f"(было {fmt_rub(s.old_min)}, −{fmt_rub(s.old_min - s.new_min)}) · "
                f"{html.escape(s.item.seller)}"
            )
    if drops:
        lines.append(f"\n<b>Подешевели товары ({len(drops)}):</b>")
        for d in drops[:top]:
            notes = []
            if d.all_time_low:
                notes.append("🔥 минимум за всё время")
            if d.median_7d:
                notes.append(f"медиана 7 дн. {fmt_rub(d.median_7d)}")
            lines.append(
                f"• {_link(d.item, _title(d.item))} — <b>{fmt_rub(d.item.price)}</b> "
                f"(было {fmt_rub(d.old_price)}, −{d.drop_pct:.1f}%) · {html.escape(d.item.seller)}"
                + (f"\n   {' · '.join(notes)}" if notes else "")
            )
        if len(drops) > top:
            lines.append(f"…и ещё {len(drops) - top}")
    return "\n".join(lines)


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Режет длинный текст по строкам на куски не длиннее limit."""
    chunks, current = [], ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def send_telegram(token: str, chat_id: str, text: str) -> None:
    """Отправляет сообщение через Telegram Bot API (длинное — частями)."""
    for chunk in split_message(text):
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=30,
        )
        if not resp.ok:
            raise RuntimeError(f"Telegram: HTTP {resp.status_code} {resp.text[:200]}")


def load_env(path: Path = Path(".env")) -> None:
    """Подхватывает KEY=VALUE из .env, не перетирая уже заданные переменные."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ------------------------------------------------------------------ запуск


def run_once(args: argparse.Namespace, tokens: TokenStore, notify) -> None:
    """Один цикл: сбор → сохранение → анализ → уведомление."""
    now = datetime.now()
    items = collect(args.query, tokens, args.max_pages, (args.delay_min, args.delay_max), args.dest)
    items = filter_items(items, min_price=args.min_price)
    if not items:
        raise RuntimeError("После фильтра не осталось ни одного товара — проверьте выдачу (--dump-raw)")
    if args.export:
        save_results(items, args.export)
    conn = open_db(args.db)
    try:
        run_id = store_run(conn, args.query, items, now.isoformat(timespec="seconds"))
        prev_id = previous_run(conn, args.query, run_id)
        drops = find_price_drops(conn, items, run_id, args.min_drop_pct, args.min_drop_rub, now)
        segments = find_segment_drops(conn, items, prev_id, args.min_drop_rub)
    finally:
        conn.close()
    log.info("Товаров %d, снижений %d, новых минимумов по сегментам %d",
             len(items), len(drops), len(segments))
    text = format_report(args.query, now, items, drops, segments, first_run=prev_id is None)
    if text:
        notify(text)


def build_arg_parser() -> argparse.ArgumentParser:
    """Аргументы командной строки монитора."""
    ap = argparse.ArgumentParser(description="Мониторинг снижения цен на iPhone 16 (WB → Telegram)")
    ap.add_argument("-q", "--query", default="iPhone 16")
    ap.add_argument("-p", "--max-pages", type=int, default=100)
    ap.add_argument("--delay-min", type=float, default=1.0)
    ap.add_argument("--delay-max", type=float, default=3.0)
    ap.add_argument("--dest", default=DEFAULT_DEST)
    ap.add_argument("--min-price", type=float, default=20000)
    ap.add_argument("--min-drop-pct", type=float, default=3.0,
                    help="сообщать, если товар подешевел минимум на столько %%")
    ap.add_argument("--min-drop-rub", type=float, default=1000,
                    help="…и минимум на столько рублей")
    ap.add_argument("--db", type=Path, default=Path("data/prices.sqlite"))
    ap.add_argument("--export", type=Path, help="также сохранять XLSX/CSV каждого запуска")
    ap.add_argument("--profile-dir", type=Path, default=Path(".wb_profile"))
    ap.add_argument("--headless", action="store_true")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="один запуск (для cron/планировщика)")
    group.add_argument("--interval", type=int, default=3600, help="период повторения, с")
    ap.add_argument("--dry-run", action="store_true", help="печатать сообщения вместо отправки в Telegram")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    """Точка входа: разовый запуск или цикл с периодом --interval."""
    load_env()
    args = build_arg_parser().parse_args(argv)
    setup_logging(args.verbose)
    tg_token, tg_chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if args.dry_run or not (tg_token and tg_chat):
        if not args.dry_run:
            log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы — сообщения только в консоль")
        notify = lambda text: print("\n" + text + "\n")  # noqa: E731
    else:
        notify = lambda text: send_telegram(tg_token, tg_chat, text)  # noqa: E731
    tokens = TokenStore(args.profile_dir, headless=args.headless)

    while True:
        try:
            run_once(args, tokens, notify)
        except Exception as exc:  # цикл не должен падать из-за одного неудачного запуска
            log.exception("Запуск не удался")
            try:
                notify(f"⚠️ Мониторинг WB: запуск не удался — {html.escape(str(exc))[:500]}")
            except Exception:
                log.exception("Не удалось отправить уведомление об ошибке")
            if args.once:
                return 1
        if args.once:
            return 0
        pause = args.interval + random.uniform(-60, 60)
        log.info("Следующий запуск через %.0f мин", pause / 60)
        time.sleep(max(pause, 60))


if __name__ == "__main__":
    raise SystemExit(main())
