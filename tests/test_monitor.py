"""Тесты анализа снижения цен на временной базе SQLite."""

from datetime import datetime, timedelta

from monitor import (
    find_price_drops, find_segment_drops, format_report, open_db, previous_run,
    split_message, store_run,
)
from wb_parser import Item


def item(pid, price, model="iPhone 16 Pro", memory="256 ГБ"):
    return Item(pid, f"{model} {memory}", "Apple", model, memory, "черный", price, None, None,
                "Продавец", 1, 4.9, 10, f"https://www.wildberries.ru/catalog/{pid}/detail.aspx", "")


def run(conn, items, when):
    rid = store_run(conn, "iPhone 16", items, when.isoformat(timespec="seconds"))
    return rid, previous_run(conn, "iPhone 16", rid)


def test_price_drop_flow(tmp_path):
    conn = open_db(tmp_path / "p.sqlite")
    t0 = datetime(2026, 10, 18, 10)
    rid, prev = run(conn, [item(1, 100000), item(2, 90000), item(3, 95000, memory="512 ГБ")], t0)
    assert prev is None
    first = format_report("iPhone 16", t0, [], [], [], first_run=True)
    assert "База создана" in first

    t1 = t0 + timedelta(hours=1)
    now_items = [item(1, 95000), item(2, 89500), item(3, 95000, memory="512 ГБ"), item(4, 85000)]
    rid, prev = run(conn, now_items, t1)
    drops = find_price_drops(conn, now_items, rid, 3.0, 1000, t1)
    # товар 1: −5% и −5000 ₽ → да; товар 2: −500 ₽ → нет; товар 4 новый → нет истории
    assert [d.item.id for d in drops] == [1]
    assert drops[0].all_time_low and round(drops[0].drop_pct, 1) == 5.0
    segs = find_segment_drops(conn, now_items, prev, 1000)
    # минимум в «Pro 256» был 90 000, стал 85 000 (новый товар 4)
    assert [(s.memory, s.old_min, s.new_min, s.item.id) for s in segs] == [("256 ГБ", 90000, 85000, 4)]
    text = format_report("iPhone 16", t1, now_items, drops, segs, first_run=False)
    assert "Снижение цен" in text and "−5.0%" in text and "85 000 ₽" in text

    t2 = t1 + timedelta(hours=1)
    rid, prev = run(conn, now_items, t2)
    assert find_price_drops(conn, now_items, rid, 3.0, 1000, t2) == []
    assert format_report("iPhone 16", t2, now_items, [], [], first_run=False) is None


def test_split_message():
    text = "\n".join(f"строка {i}" for i in range(1000))
    parts = split_message(text, 500)
    assert all(len(p) <= 500 for p in parts) and "\n".join(parts) == text
