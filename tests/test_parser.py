"""Тесты разбора, фильтра и дедупликации на синтетических ответах API."""

from wb_parser import (
    dedupe_items, detect_memory, detect_model, filter_items, parse_items, summarize,
)

TS = "2026-10-18T12:00:00"


def product(pid, name, product=None, basic=None, **extra):
    """Карточка в формате выдачи: цены в копейках внутри sizes[].price."""
    sizes = [{"name": "", "origName": "0", "price": {"basic": basic, "product": product}}] if product else [{"name": ""}]
    return {"id": pid, "name": name, "brand": "Apple", "supplier": "Продавец", "supplierId": 1,
            "reviewRating": 4.8, "nmFeedbacks": 120, "colors": [{"name": "черный"}], "sizes": sizes, **extra}


def test_detect_model():
    assert detect_model("Смартфон Apple iPhone 16 128 ГБ") == "iPhone 16"
    assert detect_model("iPhone 16 Plus 256GB") == "iPhone 16 Plus"
    assert detect_model("Apple iPhone 16 Pro 1TB") == "iPhone 16 Pro"
    assert detect_model("iPhone 16 Pro Max 512 ГБ") == "iPhone 16 Pro Max"
    assert detect_model("iPhone 16ProMax 256") == "iPhone 16 Pro Max"
    assert detect_model("iPhone 16e 128 ГБ") == "iPhone 16e"
    assert detect_model("iPhone 15 Pro") is None
    assert detect_model("iPhone 160") is None


def test_detect_memory_ignores_ram():
    assert detect_memory("iPhone 16 8/256 ГБ") == "256 ГБ"
    assert detect_memory("iPhone 16 Pro 1 TB") == "1 ТБ"
    assert detect_memory("iPhone 16", "128GB") == "128 ГБ"
    assert detect_memory("iPhone 16 8 ГБ") is None


def test_parse_prices_in_rubles_and_discount():
    items = parse_items({"products": [product(1, "iPhone 16 128 ГБ", 6999000, 9999000)]}, TS)
    it = items[0]
    assert (it.price, it.price_basic, it.discount_pct) == (69990.0, 99990.0, 30.0)
    assert it.memory == "128 ГБ" and it.color == "черный" and it.feedbacks == 120
    assert it.url == "https://www.wildberries.ru/catalog/1/detail.aspx"


def test_parse_old_format_and_nested_data():
    data = {"data": {"products": [{"id": 2, "name": "iPhone 16 Pro 256 ГБ", "salePriceU": 9000000, "priceU": 11000000}]}}
    it = parse_items(data, TS)[0]
    assert (it.price, it.price_basic) == (90000.0, 110000.0)


def test_filter_drops_accessories_cheap_and_out_of_stock():
    data = {"products": [
        product(1, "Смартфон iPhone 16 Pro Max 256 ГБ", 11000000, 13000000),
        product(2, "Чехол для iPhone 16 Pro Max", 50000, 90000),
        product(3, "Защитное стекло iPhone 16", 30000),
        product(4, "iPhone 16 Pro", 150000),             # дёшево — не смартфон
        product(5, "iPhone 16 Plus 128 ГБ"),             # нет в наличии
        product(6, "iPhone 16e 128 ГБ", 5500000),        # не та модель
        product(7, "iPhone 15 128 ГБ", 5000000),
    ]}
    items = parse_items(data, TS)
    assert [it.id for it in filter_items(items)] == [1]
    assert len(filter_items(items, include_accessories=True)) == 7


def test_dedupe_by_id():
    items = parse_items({"products": [product(1, "iPhone 16 128 ГБ", 7000000)] * 3}, TS)
    assert len(dedupe_items(items)) == 1


def test_summary():
    items = parse_items({"products": [
        product(1, "iPhone 16 128 ГБ", 7000000), product(2, "iPhone 16 Pro 256 ГБ", 10000000),
        product(3, "iPhone 16 Pro 256 ГБ", 9000000),
    ]}, TS)
    text = summarize(items)
    assert "Всего товаров: 3" in text and "медиана 90 000 ₽" in text
    assert "iPhone 16 Pro 256 ГБ: 90 000 ₽" in text


def test_refurbished_filtered_out():
    data = {"products": [
        product(1, "Смартфон iPhone 16 Pro 256 ГБ", 9900000),
        product(2, "iPhone 16 Pro 256 ГБ восстановленный", 7000000),
        product(3, "Apple iPhone 16 Pro 256GB Refurbished", 7100000),
        product(4, "iPhone 16 Pro 256 ГБ (как новый)", 7200000),
        product(5, "iPhone 16 Pro 256 ГБ б/у", 6000000),
        product(6, "iPhone 16 Pro 256 ГБ уценка", 8000000),
        product(7, "iPhone 16 Pro 256 ГБ, Desert Titanium, eSIM", 9800000, supplierRating=4.9),
    ]}
    items = parse_items(data, TS)
    assert [it.id for it in filter_items(items)] == [1, 7]
    assert len(filter_items(items, include_refurbished=True)) == 7
    assert items[6].seller_rating == 4.9 and items[1].refurbished
