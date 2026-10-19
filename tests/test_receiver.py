"""Тесты HTTP-приёмника выдачи от userscript."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from receiver import Receiver, make_handler


def phone(pid, name, rub):
    return {"id": pid, "name": name, "supplier": "Магазин",
            "sizes": [{"price": {"basic": rub * 120, "product": rub * 100}}]}


@pytest.fixture
def server(tmp_path):
    sent = []
    rec = Receiver("s3cret", sent.append, tmp_path / "db.sqlite")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(rec))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", rec, sent
    srv.shutdown()


def post(url, body, token="s3cret"):
    req = urllib.request.Request(url, json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Auth-Token": token})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_ingest_two_runs_reports_drop(server):
    base, rec, sent = server
    pages = [{"products": [phone(1, "iPhone 16 Pro 256 ГБ", 100000), phone(2, "Чехол iPhone 16 Pro", 900)]},
             {"products": [phone(1, "iPhone 16 Pro 256 ГБ", 100000), phone(3, "iPhone 16 128 ГБ", 70000)]}]
    status, res = post(base + "/ingest", {"query": "iPhone 16", "pages": pages})
    assert status == 200 and res["items"] == 2 and res["raw_items"] == 4 and res["first_run"]
    assert "База создана" in sent[-1]

    pages[0]["products"][0] = phone(1, "iPhone 16 Pro 256 ГБ", 92000)
    pages[1]["products"][0] = phone(1, "iPhone 16 Pro 256 ГБ", 92000)
    status, res = post(base + "/ingest", {"query": "iPhone 16", "pages": pages})
    assert status == 200 and res["drops"] == 1 and res["segments"] == 1
    assert "Снижение цен" in sent[-1] and "92 000 ₽" in sent[-1]
    assert rec.last_ingest is not None


def test_dump_raw(tmp_path):
    rec = Receiver("s", lambda t: None, tmp_path / "db.sqlite", dump_raw=tmp_path / "raw")
    rec.ingest({"pages": [{"products": [phone(1, "iPhone 16 128 ГБ", 70000)]}]})
    assert len(list((tmp_path / "raw").glob("ingest_*.json"))) == 1


def test_auth_and_validation(server):
    base, _, sent = server
    assert post(base + "/ingest", {"pages": []}, token="wrong")[0] == 401
    assert post(base + "/ingest", {"pages": "x"})[0] == 400
    assert post(base + "/nope", {})[0] == 404
    assert sent == []


def test_error_forwarding_is_deduplicated(server):
    base, _, sent = server
    assert post(base + "/error", {"message": "капча"})[1]["notified"] is True
    assert post(base + "/error", {"message": "капча"})[1]["notified"] is False
    assert len(sent) == 1 and "капча" in sent[0]


def test_silence_watchdog(tmp_path):
    sent = []
    rec = Receiver("s", sent.append, tmp_path / "db.sqlite")
    assert rec.check_silence(60) is False          # данных ещё не было — молчим
    rec.last_ingest = 0
    assert rec.check_silence(60) is True and "нет уже" in sent[0]
    assert rec.check_silence(60) is False          # второй раз не повторяем
