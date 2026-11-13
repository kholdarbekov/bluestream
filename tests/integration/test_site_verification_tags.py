"""Search-console verification tags render the bare token, unchanged. Driven through the real /privacy route."""

import pytest
from bs4 import BeautifulSoup

TAGS = [("GOOGLE_SITE_VERIFICATION", "google-site-verification"),
        ("YANDEX_SITE_VERIFICATION", "yandex-verification"),
        ("BING_SITE_VERIFICATION", "msvalidate.01")]


def _page(app):
    response = app.test_client().get("/privacy")
    assert response.status_code == 200
    return BeautifulSoup(response.get_data(as_text=True), "html.parser")


@pytest.mark.parametrize("config_key, meta_name", TAGS)
def test_a_tag_renders_the_configured_token_unchanged(app, db, monkeypatch, config_key, meta_name):
    monkeypatch.setitem(app.config, config_key, "abc123DEF_-")
    [tag] = _page(app).find_all("meta", attrs={"name": meta_name})
    assert tag["content"] == "abc123DEF_-"


def test_an_empty_value_renders_no_tag(app, db, monkeypatch):
    for config_key, _meta_name in TAGS:
        monkeypatch.setitem(app.config, config_key, "")
    soup = _page(app)
    assert [soup.find("meta", attrs={"name": meta_name}) for _config_key, meta_name in TAGS] == [None, None, None]
