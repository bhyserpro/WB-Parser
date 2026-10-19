"""Сквозной тест userscript: Chromium + поддельный «WB» на localhost + настоящий receiver.

GM_* подменяются заглушками, GM_xmlhttpRequest пересылает запрос в receiver
через Python. Проверяется логика скрипта, а не поведение настоящего WB.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from receiver import Receiver, make_handler

sync_api = pytest.importorskip("playwright.sync_api")
SCRIPT = Path(__file__).resolve().parent.parent / "wb-monitor.user.js"

PAGE = """<!doctype html><html><body><h1>fake wb</h1>
<script>fetch('/__internal/search/exactmatch/ru/common/v18/search?appType=1&dest=777&query=iphone+16&page=1');</script>
</body></html>"""


def products(page):
    if page > 3:
        return []
    return [
        {"id": page * 10 + 1, "name": f"Apple iPhone 16 Pro {256 if page < 3 else 512} ГБ", "supplier": "S",
         "extraHeavyField": "x" * 1000,
         "sizes": [{"name": "", "origName": "0", "stocks": [1, 2, 3],
                    "price": {"basic": 12000000, "product": 9000000 + page * 100000}}]},
        {"id": page * 10 + 2, "name": "Чехол для iPhone 16 Pro", "supplier": "S",
         "sizes": [{"price": {"basic": 90000, "product": 50000}}]},
    ]


class FakeWB(BaseHTTPRequestHandler):
    calls: list = []
    api_status = 200

    def do_GET(self):  # noqa: N802
        url = urlsplit(self.path)
        if url.path.startswith("/__internal/search/"):
            q = parse_qs(url.query)
            FakeWB.calls.append(q)
            body = json.dumps({"products": products(int(q["page"][0]))}).encode()
            ctype = "application/json"
            status = FakeWB.api_status if len(FakeWB.calls) > 1 else 200
        else:
            body, ctype, status = PAGE.encode(), "text/html; charset=utf-8", 200
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


GM_STUBS = """
window.WB_MONITOR_CONFIG = {delayMinMs: 10, delayMaxMs: 20, maxPages: 10, receiver: 'RECEIVER'};
const store = {secret: 'tok'};
window.GM_getValue = (k, d) => (k in store ? store[k] : d);
window.GM_setValue = (k, v) => { store[k] = v; };
window.__reloads = 0;
window.GM_xmlhttpRequest = (o) => {
  window.pyPost(o.url, o.headers['X-Auth-Token'], o.data)
    .then((r) => o.onload({status: r.status, responseText: r.text}))
    .catch(() => o.onerror());
};
"""


def run_script(tmp_path, api_status=200):
    """Прогоняет userscript в Chromium; возвращает (текст плашки, posted, sent)."""
    sent = []
    rec = Receiver("tok", sent.append, tmp_path / "db.sqlite")
    rsrv, rbase = serve(make_handler(rec))
    wsrv, wbase = serve(FakeWB)
    FakeWB.calls, FakeWB.api_status = [], api_status
    posted = []
    try:
        with sync_api.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception as exc:  # браузер не установлен
                pytest.skip(f"Chromium недоступен: {exc}")
            page = browser.new_page()

            def py_post(url, token, data):
                posted.append((url, json.loads(data)))
                r = requests.post(url, data=data.encode(), timeout=30,
                                  headers={"Content-Type": "application/json", "X-Auth-Token": token})
                return {"status": r.status_code, "text": r.text}

            page.expose_function("pyPost", py_post)
            page.add_init_script(GM_STUBS.replace("RECEIVER", rbase))
            page.goto(wbase + "/catalog/0/search.aspx?search=iPhone%2016#wb-monitor")
            page.add_script_tag(content=SCRIPT.read_text(encoding="utf-8"))
            page.wait_for_function("() => document.body.innerText.includes('следующий запуск')", timeout=30000)
            badge = page.inner_text("body")
            browser.close()
    finally:
        rsrv.shutdown()
        wsrv.shutdown()
    return badge, posted, sent


def test_userscript_end_to_end(tmp_path):
    badge, posted, sent = run_script(tmp_path)
    assert "страниц 3, iPhone 3, снижений 0" in badge
    # шаблон URL взят из запроса самой страницы (dest=777), query/page — свои
    assert [c["page"][0] for c in FakeWB.calls if "dest" in c][-4:] == ["1", "2", "3", "4"]
    assert all(c["dest"] == ["777"] and c["query"] == ["iPhone 16"] for c in FakeWB.calls[1:])
    url, body = posted[0]
    assert url.endswith("/ingest") and len(body["pages"]) == 3
    prod = body["pages"][0]["products"][0]
    assert "extraHeavyField" not in prod and "stocks" not in prod["sizes"][0]  # лишнее обрезано
    assert "База создана" in sent[0]


def test_userscript_token_rejected(tmp_path):
    badge, posted, sent = run_script(tmp_path, api_status=498)
    assert "ошибка: WB отклонил запрос: HTTP 498" in badge
    assert posted == [] and sent == []  # первая такая ошибка — только перезагрузка через минуту
