"""Сквозной тест userscript: Chromium + поддельный «WB» на localhost + настоящий receiver.

Поддельная страница сама запрашивает выдачу (как настоящий сайт): при загрузке —
страницу из ?page=N, при прокрутке — следующие (режим scroll). GM_* подменены
заглушками на localStorage, GM_xmlhttpRequest пересылает запрос в receiver
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

PAGE = """<!doctype html><html><body><h1>fake wb</h1><div id="list"></div>
<script>
const MODE = '%MODE%';
let page = +(new URLSearchParams(location.search).get('page') || 1), loading = false, end = false;
function load(p) {
  loading = true;
  return fetch('/__internal/search/exactmatch/ru/common/v18/search?appType=1&dest=777&query=iphone+16&page=' + p)
    .then(r => r.ok ? r.json() : {products: []})
    .then(d => {
      if (!d.products.length) end = true;
      const div = document.createElement('div');
      div.style.height = '3000px';
      div.textContent = 'page ' + p;
      document.getElementById('list').appendChild(div);
      loading = false;
    });
}
load(page);
if (MODE === 'scroll') {
  window.addEventListener('scroll', () => {
    if (!loading && !end && innerHeight + scrollY >= document.body.scrollHeight - 100) load(++page);
  });
}
</script></body></html>"""


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
    mode = "scroll"
    api_status = 200

    def do_GET(self):  # noqa: N802
        url = urlsplit(self.path)
        status = 200
        if url.path.startswith("/__internal/search/"):
            q = parse_qs(url.query)
            FakeWB.calls.append(int(q["page"][0]))
            status = FakeWB.api_status
            body = json.dumps({"products": products(int(q["page"][0]))}).encode()
            ctype = "application/json"
        else:
            body, ctype = PAGE.replace("%MODE%", FakeWB.mode).encode(), "text/html; charset=utf-8"
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
window.WB_MONITOR_CONFIG = {delayMinMs: 50, delayMaxMs: 100, scrollStallMs: 1500, waitResponseMs: 5000,
                            reloadDelayMs: [50, 100], maxPages: 10, receiver: 'RECEIVER'};
const KEY = 'gm:';
if (!localStorage.getItem(KEY + 'secret')) localStorage.setItem(KEY + 'secret', JSON.stringify('tok'));
window.GM_getValue = (k, d) => { const v = localStorage.getItem(KEY + k); return v === null ? d : JSON.parse(v); };
window.GM_setValue = (k, v) => localStorage.setItem(KEY + k, JSON.stringify(v));
window.GM_xmlhttpRequest = (o) => {
  window.pyPost(o.url, o.headers['X-Auth-Token'], o.data)
    .then((r) => o.onload({status: r.status, responseText: r.text}))
    .catch(() => o.onerror());
};
"""


def run_script(tmp_path, mode="scroll", api_status=200, wait_for="следующий запуск"):
    """Прогоняет userscript; возвращает (текст плашки, отправленное в receiver, сообщения бота)."""
    sent, posted = [], []
    rec = Receiver("tok", sent.append, tmp_path / "db.sqlite")
    rsrv, rbase = serve(make_handler(rec))
    FakeWB.calls, FakeWB.mode, FakeWB.api_status = [], mode, api_status
    wsrv, wbase = serve(FakeWB)
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
            # как у менеджера userscript: заглушки и скрипт — до скриптов страницы, на каждой загрузке
            page.add_init_script(GM_STUBS.replace("RECEIVER", rbase))
            page.add_init_script(SCRIPT.read_text(encoding="utf-8"))
            page.goto(wbase + "/catalog/0/search.aspx?search=iPhone%2016#wb-monitor")
            page.wait_for_function(
                f"() => document.body && document.body.innerText.includes({json.dumps(wait_for)})", timeout=60000)
            badge = page.inner_text("body")
            browser.close()
    finally:
        rsrv.shutdown()
        wsrv.shutdown()
    return badge, posted, sent


def check_ingest(posted, sent):
    assert len(posted) == 1
    url, body = posted[0]
    assert url.endswith("/ingest") and [p["page"] for p in body["pages"]] == [1, 2, 3]
    prod = body["pages"][0]["products"][0]
    assert "extraHeavyField" not in prod and "stocks" not in prod["sizes"][0]  # лишнее обрезано
    assert "База создана" in sent[0]


def test_infinite_scroll(tmp_path):
    badge, posted, sent = run_script(tmp_path, "scroll")
    assert "страниц 3, iPhone 3, снижений 0" in badge
    assert FakeWB.calls == [1, 2, 3, 4]   # скрипт сам ничего не запрашивал — только сайт
    check_ingest(posted, sent)


def test_page_navigation_fallback(tmp_path):
    badge, posted, sent = run_script(tmp_path, "pages")
    assert "страниц 3, iPhone 3" in badge
    assert FakeWB.calls == [1, 2, 3, 4]
    check_ingest(posted, sent)


def test_site_requests_rejected(tmp_path):
    badge, posted, sent = run_script(tmp_path, "scroll", api_status=403, wait_for="ошибка")
    assert "WB отклоняет запросы самого сайта: HTTP 403" in badge
    assert posted == [] and sent == []    # первая ошибка — без уведомления, повтор позже
