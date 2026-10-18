"""Получение и хранение антибот-токена Wildberries (cookie ``x_wbaas_token``).

Токен выдаёт JS-проверка браузера на сайте, поэтому он получается в
настоящем Chromium через Playwright. Профиль браузера постоянный: cookie
переживают перезапуски, а если WB покажет капчу, её достаточно решить
руками один раз в открывшемся окне.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

import requests

log = logging.getLogger("wb_token")

SITE = "https://www.wildberries.ru"
TOKEN_COOKIE = "x_wbaas_token"


class TokenRejected(Exception):
    """WB не принял токен (401/403/498) — нужно получить новый."""


class TokenStore:
    """Кэш cookie и User-Agent браузера, прошедшего проверку WB."""

    def __init__(self, profile_dir: Path, headless: bool = False, timeout: float = 180) -> None:
        self.profile_dir = Path(profile_dir)
        self.cache_path = self.profile_dir / "token.json"
        self.headless = headless
        self.timeout = timeout
        self.cookies: list[dict] = []
        self.user_agent = ""
        self._load()

    def _load(self) -> None:
        """Читает сохранённый токен, если он есть."""
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.cookies, self.user_agent = data.get("cookies", []), data.get("user_agent", "")

    def _save(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps({
            "cookies": self.cookies,
            "user_agent": self.user_agent,
            "obtained_at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    @property
    def has_token(self) -> bool:
        """Есть ли в кэше cookie антибота."""
        return any(c.get("name") == TOKEN_COOKIE for c in self.cookies)

    def apply(self, session: requests.Session, query: str = "iPhone 16") -> None:
        """Кладёт cookie и User-Agent в сессию; при пустом кэше получает токен."""
        if not self.has_token:
            self.refresh(query)
        for c in self.cookies:
            session.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
        session.headers["User-Agent"] = self.user_agent

    def refresh(self, query: str = "iPhone 16") -> None:
        """Открывает страницу поиска в Chromium и ждёт, пока сайт сам загрузит выдачу.

        Успешный ответ ``/__internal/search/`` означает, что проверка пройдена
        и cookie годятся для API.
        """
        from playwright.sync_api import TimeoutError as PwTimeout, sync_playwright

        url = f"{SITE}/catalog/0/search.aspx?search={quote_plus(query)}"
        log.info("Открываю браузер для получения токена: %s", url)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                str(self.profile_dir / "chromium"),
                headless=self.headless,
                locale="ru-RU",
                viewport={"width": 1366, "height": 900},
            )
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
                        f"За {self.timeout:.0f} с сайт не отдал выдачу: проверка WB не пройдена. "
                        "Если в окне капча — решите её и запустите снова. "
                        "С IP дата-центров/зарубежных VPN WB обычно не пускает."
                    ) from exc
                self.user_agent = page.evaluate("navigator.userAgent")
                self.cookies = ctx.cookies(SITE)
            finally:
                ctx.close()
        if not self.has_token:
            raise TokenRejected("Выдача загрузилась, но cookie x_wbaas_token не найдена")
        self._save()
        log.info("Токен получен и сохранён в %s", self.cache_path)
