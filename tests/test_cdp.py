"""Тест wb_cdp: настоящий Chromium с портом отладки + поддельный «WB» на localhost.

Проверяется, что скрипт подключается по CDP, листает страницы выдачи, читает
ответы самого сайта и отбирает нужную модель и память. Поведение настоящего WB
этим не проверяется.
"""

import json
import os
import socket
import subprocess
import threading
import time
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from wb_cdp import ResponseCollector, page_url, run_cycle

sync_api = pytest.importorskip("playwright.sync_api")

PAGE = """<!doctype html><html><body><div id="list"></div><script>
const p = new URLSearchParams(location.search).get('page') || 1;
fetch('/__internal/search/exactmatch/ru/common/v18/search?query=x&sort=priceup&page=' + p)
  .then(r => r.json()).then(d => { document.getElementById('list').textContent = d.products.length; });
</script></body></html>"""


def prod(pid, name, rub, seller="Магазин"):
    return {"id": pid, "name": name, "supplier": seller,
            "sizes": [{"price": {"basic": rub * 130, "product": rub * 100}}]}


PAGES = {
    1: [prod(1, "Apple iPhone 16 Pro 256 ГБ Desert Titanium", 99000, "Дешёвый"),
        prod(2, "Apple iPhone 16 Pro 128 ГБ", 95000),
        prod(3, "Чехол iPhone 16 Pro 256", 900)],
    2: [prod(4, "Смартфон iPhone 16 Pro 256GB", 101000), prod(5, "iPhone 16 Pro Max 256 ГБ", 120000)],
}
PRICE = {"p1": 99000}


class FakeWB(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        url = urlsplit(self.path)
        if url.path.startswith("/__internal/"):
            n = int(parse_qs(url.query)["page"][0])
            products = [dict(x) for x in PAGES.get(n, [])]
            if n == 1:
                products[0] = prod(1, "Apple iPhone 16 Pro 256 ГБ Desert Titanium", PRICE["p1"], "Дешёвый")
            body, ctype = json.dumps({"products": products}).encode(), "application/json"
        else:
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_page_url():
    assert page_url("https://x/s.aspx?search=a&page=4&sort=priceup", 1) == "https://x/s.aspx?search=a&sort=priceup"
    assert page_url("https://x/s.aspx?search=a", 3) == "https://x/s.aspx?search=a&page=3"


def test_cdp_end_to_end(tmp_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeWB)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    with sync_api.sync_playwright() as pw:
        exe = pw.chromium.executable_path
        port = free_port()
        # «ваш Chrome»: обычный процесс браузера с портом отладки, запущенный не Playwright
        chrome = subprocess.Popen([exe, "--headless=new", f"--remote-debugging-port={port}",
                                   f"--user-data-dir={tmp_path / 'profile'}", "--no-first-run",
                                   *(["--no-sandbox"] if os.geteuid() == 0 else []),  # root в CI
                                   "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                try:
                    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                    break
                except Exception:
                    time.sleep(0.2)
            else:
                pytest.skip("Chromium с портом отладки не запустился")
            context = browser.contexts[0]
            collector = ResponseCollector()
            context.on("response", collector)
            page = context.new_page()
            sent = []
            args = Namespace(url=base + "/catalog/0/search.aspx?search=x&sort=priceup", max_pages=5,
                             delay_min=0.05, delay_max=0.1, dump_raw=tmp_path / "raw", min_price=20000,
                             model="iPhone 16 Pro", memory="256 ГБ", db=tmp_path / "db.sqlite",
                             min_drop_pct=0.0, min_drop_rub=1.0, export=None)
            res = run_cycle(args, page, collector, sent.append)
            assert res["items"] == 2 and res["first_run"]           # 1 и 4: Pro 256; 128, Max и чехол — мимо
            assert "База создана" in sent[-1] and "99 000 ₽" in sent[-1]
            assert len(list((tmp_path / "raw").glob("cdp_*.json"))) == 3       # 2 страницы + пустая 3-я

            PRICE["p1"] = 98500                                      # подешевел на 500 ₽ — «любое снижение»
            res = run_cycle(args, page, collector, sent.append)
            assert res["drops"] == 1 and res["segments"] == 1
            assert "98 500 ₽" in sent[-1] and "Дешёвый" in sent[-1]
            page.close()
        finally:
            chrome.terminate()
            chrome.wait(10)
            srv.shutdown()
