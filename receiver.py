"""Локальный приёмник выдачи WB от userscript в браузере.

Userscript (``wb-monitor.user.js``) раз в час собирает страницы выдачи
прямо в вашем Firefox — там, где проверку WB проходит сам браузер, — и
отправляет сырые JSON сюда, на ``127.0.0.1``. Приёмник разбирает их,
фильтрует, сохраняет историю цен и шлёт в Telegram снижения.

Запуск::

    python receiver.py
"""

from __future__ import annotations

import argparse
import hmac
import html
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from monitor import analyze_and_report, load_env, make_notifier
from wb_parser import dedupe_items, filter_items, parse_items, setup_logging, summarize

log = logging.getLogger("receiver")

MAX_BODY = 50 * 1024 * 1024  # 50 МБ: 100 страниц выдачи с запасом


class Receiver:
    """Обработка входящих данных: анализ, уведомления, сторож молчания."""

    def __init__(
        self, secret: str, notify: Callable[[str], None], db: Path,
        min_price: float = 20000, min_drop_pct: float = 3.0, min_drop_rub: float = 1000,
        export: Path | None = None, include_accessories: bool = False,
        dump_raw: Path | None = None,
    ) -> None:
        self.secret = secret
        self.notify = notify
        self.db = db
        self.min_price = min_price
        self.min_drop_pct = min_drop_pct
        self.min_drop_rub = min_drop_rub
        self.export = export
        self.include_accessories = include_accessories
        self.dump_raw = dump_raw
        self.lock = threading.Lock()
        self.last_ingest: float | None = None
        self.last_errors: dict[str, float] = {}
        self.silence_reported = False

    def authorized(self, header: str | None) -> bool:
        """Сверяет общий секрет из заголовка X-Auth-Token."""
        return bool(header) and hmac.compare_digest(header, self.secret)

    def ingest(self, payload: dict[str, Any]) -> dict:
        """Принимает {query, pages: [JSON страниц выдачи]} и запускает анализ."""
        query = str(payload.get("query") or "iPhone 16")
        pages = payload.get("pages")
        if not isinstance(pages, list):
            raise ValueError("ожидается поле pages — список ответов API")
        now = datetime.now()
        collected_at = now.isoformat(timespec="seconds")
        if self.dump_raw:
            self.dump_raw.mkdir(parents=True, exist_ok=True)
            path = self.dump_raw / f"ingest_{now:%Y%m%d_%H%M%S}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            log.info("Сырые данные сохранены: %s", path)
        items = []
        for page in pages:
            if isinstance(page, dict):
                items.extend(parse_items(page, collected_at))
        raw_count = len(items)
        items = filter_items(dedupe_items(items), self.include_accessories, self.min_price)
        log.info("Получено страниц %d, товаров %d, после фильтра %d", len(pages), raw_count, len(items))
        with self.lock:
            result = analyze_and_report(items, query, self.db, self.min_drop_pct,
                                        self.min_drop_rub, self.notify, now, self.export)
            self.last_ingest = time.time()
            self.silence_reported = False
        log.info("Сводка:\n%s", summarize(items))
        return {"ok": True, "pages": len(pages), "raw_items": raw_count, **result}

    def report_error(self, message: str, repeat_after: float = 3 * 3600) -> bool:
        """Пересылает ошибку из браузера в Telegram (одинаковые — не чаще раза в 3 ч)."""
        message = message.strip()[:500] or "неизвестная ошибка"
        now = time.time()
        if now - self.last_errors.get(message, 0) < repeat_after:
            return False
        self.last_errors[message] = now
        self.notify(f"⚠️ Мониторинг WB (браузер): {html.escape(message)}")
        return True

    def check_silence(self, max_silence: float) -> bool:
        """Сообщает один раз, если данные давно не приходят (закрыт браузер, сон Mac)."""
        if self.last_ingest is None or self.silence_reported:
            return False
        if time.time() - self.last_ingest < max_silence:
            return False
        self.silence_reported = True
        hours = (time.time() - self.last_ingest) / 3600
        self.notify(
            f"⚠️ Мониторинг WB: данных из браузера нет уже {hours:.1f} ч. "
            "Проверьте, что Firefox открыт с вкладкой мониторинга и компьютер не спит."
        )
        return True


def make_handler(receiver: Receiver) -> type[BaseHTTPRequestHandler]:
    """HTTP-обработчик: POST /ingest, POST /error, GET /health."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "wb-receiver"

        def _reply(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._reply(200, {"ok": True, "last_ingest": receiver.last_ingest})
            else:
                self._reply(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path not in ("/ingest", "/error"):
                return self._reply(404, {"ok": False, "error": "not found"})
            if not receiver.authorized(self.headers.get("X-Auth-Token")):
                return self._reply(401, {"ok": False, "error": "неверный X-Auth-Token"})
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                return self._reply(413, {"ok": False, "error": "пустое или слишком большое тело"})
            try:
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("ожидается JSON-объект")
                if self.path == "/error":
                    sent = receiver.report_error(str(payload.get("message", "")))
                    return self._reply(200, {"ok": True, "notified": sent})
                return self._reply(200, receiver.ingest(payload))
            except ValueError as exc:
                return self._reply(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                log.exception("Ошибка обработки")
                return self._reply(500, {"ok": False, "error": str(exc)})

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("%s %s", self.address_string(), fmt % args)

    return Handler


def ensure_secret(env_path: Path = Path(".env")) -> str:
    """Берёт RECEIVER_SECRET из окружения или создаёт и дописывает в .env."""
    secret = os.getenv("RECEIVER_SECRET")
    if secret:
        return secret
    secret = secrets.token_urlsafe(24)
    with env_path.open("a", encoding="utf-8") as fh:
        fh.write(f"\nRECEIVER_SECRET={secret}\n")
    os.environ["RECEIVER_SECRET"] = secret
    log.info("Создан RECEIVER_SECRET и записан в %s", env_path)
    return secret


def watchdog(receiver: Receiver, max_silence: float, period: float = 600) -> None:
    """Фоновая проверка, что данные приходят."""
    while True:
        time.sleep(period)
        try:
            receiver.check_silence(max_silence)
        except Exception:
            log.exception("Сторож: не удалось отправить уведомление")


def build_arg_parser() -> argparse.ArgumentParser:
    """Аргументы командной строки приёмника."""
    ap = argparse.ArgumentParser(description="Приёмник выдачи WB от userscript → анализ → Telegram")
    ap.add_argument("--host", default="127.0.0.1", help="адрес (оставьте 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", type=Path, default=Path("data/prices.sqlite"))
    ap.add_argument("--min-price", type=float, default=20000)
    ap.add_argument("--min-drop-pct", type=float, default=3.0)
    ap.add_argument("--min-drop-rub", type=float, default=1000)
    ap.add_argument("--export", type=Path, help="сохранять XLSX/CSV каждого запуска")
    ap.add_argument("--include-accessories", action="store_true")
    ap.add_argument("--dump-raw", type=Path, help="сохранять каждую присланную выдачу в папку")
    ap.add_argument("--max-silence-hours", type=float, default=3.0,
                    help="предупредить, если данных нет дольше, ч (0 — не следить)")
    ap.add_argument("--dry-run", action="store_true", help="печатать сообщения вместо Telegram")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    """Запускает HTTP-приёмник."""
    load_env()
    args = build_arg_parser().parse_args(argv)
    setup_logging(args.verbose)
    receiver = Receiver(
        ensure_secret(), make_notifier(args.dry_run), args.db, args.min_price,
        args.min_drop_pct, args.min_drop_rub, args.export, args.include_accessories,
        args.dump_raw,
    )
    if args.max_silence_hours > 0:
        threading.Thread(target=watchdog, args=(receiver, args.max_silence_hours * 3600),
                         daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(receiver))
    log.info("Приёмник слушает http://%s:%d — секрет для userscript: %s",
             args.host, args.port, receiver.secret)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Остановлен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
