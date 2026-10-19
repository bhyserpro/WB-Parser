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
const API = '/__internal/search/exactmatch/ru/common/v18/search?appType=1&dest=777&query=iphone+16&page=';
let page = +(new URLSearchParams(location.search).get('page') || 1), loading = false, end = false;
const rub = (k) => (k / 100).toLocaleString('ru-RU').replace(/\\s/g, '\\u00a0') + '\\u00a0₽';
let worker;
if (MODE === 'worker') {   // запросы из Web Worker перехват страницы не видит
  const code = "onmessage = (e) => fetch(e.data).then(r => r.json()).then(d => postMessage(d))";
  worker = new Worker(URL.createObjectURL(new Blob([code])));
}
function get(p) {
  if (!worker) return fetch(API + p).then(r => r.ok ? r.json() : {products: []});
  return new Promise((res) => { worker.onmessage = (e) => res(e.data); worker.postMessage(location.origin + API + p); });
}
function render(d, p) {
  const box = document.createElement('div');
  box.style.minHeight = '3000px';
  for (const x of d.products) {
    const pr = x.sizes[0].price;
    box.insertAdjacentHTML('beforeend',
      `<article class="product-card" data-nm-id="${x.id}"><a href="/catalog/${x.id}/detail.aspx" aria-label="${x.name}">` +
      `<span class="product-card__brand">Apple</span><span class="product-card__name">/ ${x.name}</span>` +
      `<ins class="price__lower-price">${rub(pr.product)}</ins><del>${rub(pr.basic)}</del>` +
      `<span class="address-rate-mini">4,8</span><span class="product-card__count">1 234 оценки</span></a></article>`);
  }
  document.getElementById('list').appendChild(box);
}
function load(p) {
  loading = true;
  return get(p).then(d => { if (!d.products.length) end = true; render(d, p); loading = false; });
}
load(page);
if (MODE !== 'pages') {
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
    assert "ответов API 3\niPhone 3, снижений 0" in badge
    assert FakeWB.calls == [1, 2, 3, 4]   # скрипт сам ничего не запрашивал — только сайт
    check_ingest(posted, sent)


def test_page_navigation_fallback(tmp_path):
    badge, posted, sent = run_script(tmp_path, "pages")
    assert "ответов API 3\niPhone 3" in badge
    assert FakeWB.calls == [1, 2, 3, 4]
    check_ingest(posted, sent)


def test_site_requests_rejected(tmp_path):
    badge, posted, sent = run_script(tmp_path, "scroll", api_status=403, wait_for="ошибка")
    # сайт получил 403 и ничего не показал — после повторов ошибка
    assert "WB отклоняет запросы самого сайта: HTTP 403" in badge
    assert posted == [] and sent == []    # первая ошибка — без уведомления, повтор позже


def test_dom_fallback_when_api_not_visible(tmp_path):
    badge, posted, sent = run_script(tmp_path, "worker")
    assert "с карточек 6 (без продавца)\niPhone 3" in badge
    url, body = posted[0]
    assert [p["page"] for p in body["pages"]] == ["dom"]
    first = body["pages"][0]["products"][0]
    assert first == {"id": 11, "name": "Apple iPhone 16 Pro 256 ГБ", "brand": "Apple", "reviewRating": 4.8,
                     "feedbacks": 1234, "sizes": [{"price": {"product": 9100000, "basic": 12000000}}],
                     "source": "dom"}
    assert "База создана" in sent[0] and "91 000 ₽" in sent[0]
