"""Тесты импорта токена из «Copy as cURL (bash)»."""

import pytest

from wb_token import TokenRejected, TokenStore, parse_curl

CURL = r"""curl 'https://www.wildberries.ru/__internal/search/exactmatch/ru/common/v18/search?query=iphone+16' \
  -H 'accept: */*' \
  -H 'accept-language: ru-RU,ru;q=0.9' \
  -b '_wbauid=123; x_wbaas_token=1.1000.abc==; routeb=1758' \
  -H 'user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/141.0.0.0 Safari/537.36'"""


def test_parse_curl_cookie_flag():
    cookies, ua = parse_curl(CURL)
    assert cookies["x_wbaas_token"] == "1.1000.abc==" and len(cookies) == 3
    assert ua.startswith("Mozilla/5.0") and "Chrome/141" in ua


def test_parse_curl_cookie_header():
    cookies, _ = parse_curl("curl 'https://x' -H 'Cookie: x_wbaas_token=t; a=b'")
    assert cookies == {"x_wbaas_token": "t", "a": "b"}


def test_parse_curl_rejects_cmd_format():
    with pytest.raises(ValueError):
        parse_curl('curl ^"https://www.wildberries.ru/^" -H ^"cookie: x=1^"')


def test_import_saves_and_reloads(tmp_path):
    store = TokenStore(tmp_path, use_browser=False)
    store.import_curl(CURL)
    again = TokenStore(tmp_path, use_browser=False)
    assert again.has_token and again.user_agent.startswith("Mozilla")


def test_import_without_token_fails(tmp_path):
    with pytest.raises(ValueError):
        TokenStore(tmp_path).import_curl("curl 'https://x' -b 'a=b'")


def test_no_browser_mode_asks_for_import(tmp_path):
    with pytest.raises(TokenRejected, match="import"):
        TokenStore(tmp_path, use_browser=False).refresh()
