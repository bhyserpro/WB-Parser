"""Тесты импорта токена из «Copy as cURL (bash)»."""

import pytest

from wb_token import TokenRejected, TokenStore, parse_curl

CURL = r"""curl 'https://www.wildberries.ru/__internal/search/exactmatch/ru/common/v18/search?dest=-123&query=iphone+16&page=1&uclusters=2' \
  -H 'accept: */*' \
  -H 'accept-language: ru-RU,ru;q=0.9' \
  -H 'accept-encoding: gzip, deflate, br, zstd' \
  -H 'x-spa-version: 13.1.2' \
  -b '_wbauid=123; x_wbaas_token=1.1000.abc==; routeb=1758' \
  -H 'user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/141.0.0.0 Safari/537.36'"""


def test_parse_curl_cookie_flag():
    req = parse_curl(CURL)
    assert req["cookies"]["x_wbaas_token"] == "1.1000.abc==" and len(req["cookies"]) == 3
    assert "Chrome/141" in req["headers"]["user-agent"]
    assert req["params"]["dest"] == "-123" and req["url"].startswith("https://www.wildberries.ru/")


def test_parse_curl_cookie_header():
    req = parse_curl("curl 'https://x' -H 'Cookie: x_wbaas_token=t; a=b' --compressed")
    assert req["cookies"] == {"x_wbaas_token": "t", "a": "b"} and req["headers"] == {}


def test_parse_curl_rejects_cmd_format():
    with pytest.raises(ValueError):
        parse_curl('curl ^"https://www.wildberries.ru/^" -H ^"cookie: x=1^"')


def test_import_saves_and_reloads(tmp_path):
    store = TokenStore(tmp_path, use_browser=False)
    store.import_curl(CURL)
    again = TokenStore(tmp_path, use_browser=False)
    assert again.has_token and again.user_agent.startswith("Mozilla")
    # заголовки браузера переносятся, кроме тех, что выставляет requests
    assert again.headers["x-spa-version"] == "13.1.2" and "accept-encoding" not in again.headers
    assert again.base_params == {"dest": "-123", "uclusters": "2"}
    import requests
    session = requests.Session()
    again.apply(session)
    assert session.headers["x-spa-version"] == "13.1.2" and session.cookies["x_wbaas_token"] == "1.1000.abc=="


def test_import_without_token_fails(tmp_path):
    with pytest.raises(ValueError):
        TokenStore(tmp_path).import_curl("curl 'https://x' -b 'a=b'")


def test_no_browser_mode_asks_for_import(tmp_path):
    with pytest.raises(TokenRejected, match="import"):
        TokenStore(tmp_path, use_browser=False).refresh()
