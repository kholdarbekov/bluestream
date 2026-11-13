from types import SimpleNamespace

import pytest
import requests

from business_app.services.public_urls_service import indexnow_urls
from business_app.utils import indexnow
from business_app.utils.indexnow import ACCEPTED_STATUSES, get_key, indexnow_host, submit_urls
from scripts.indexnow_submit import submit_all
from tests.unit.test_indexnow_key_file import _key_files

pytestmark = pytest.mark.unit

URLS = ["https://aqua-element.uz/shop", "https://aqua-element.uz/shop?lang=ru"]


class _Posts(list):
    status = 202


@pytest.fixture
def posts(monkeypatch):
    recorded = _Posts()

    def post(url, data=None, json=None, **kwargs):
        recorded.append((url, data, json, kwargs))
        return SimpleNamespace(status_code=recorded.status)

    monkeypatch.setattr(indexnow.requests, "post", post)
    return recorded


def _on(app, monkeypatch):
    monkeypatch.setitem(app.config, "INDEXNOW_ENABLED", True)
    monkeypatch.setitem(app.config, "COMPANY_WEBSITE", "https://aqua-element.uz/")


def test_the_tests_config_has_the_switch_off(app):
    assert app.config["INDEXNOW_ENABLED"] is False


def test_get_key_is_the_key_files_stem(app):
    with app.app_context():
        assert get_key() == _key_files()[0].stem


def test_submit_posts_host_key_and_the_url_list(app, monkeypatch, posts):
    _on(app, monkeypatch)
    assert get_key() is not None
    assert submit_urls(URLS) == 202
    assert posts == [
        (
            "https://api.indexnow.org/indexnow",
            None,
            {"host": "aqua-element.uz", "key": get_key(), "urlList": URLS},
            {"timeout": 10},
        )
    ]


def test_the_host_is_the_company_website_host(app, monkeypatch):
    _on(app, monkeypatch)
    assert indexnow_host() == "aqua-element.uz"


@pytest.mark.parametrize("files", [{}, {"a" * 32: "a" * 32, "b" * 32: "b" * 32}, {"c" * 32: "something else"}])
def test_without_exactly_one_valid_key_file_nothing_is_sent(app, monkeypatch, posts, tmp_path, files):
    for stem, body in files.items():
        (tmp_path / f"{stem}.txt").write_text(body, encoding="utf-8")
    monkeypatch.setattr(app, "static_folder", str(tmp_path))
    _on(app, monkeypatch)
    assert get_key() is None and submit_urls(URLS) is None and posts == []


@pytest.mark.parametrize("status", [200, 202, 400, 403, 422, 429, 503])
def test_the_status_is_returned_as_is(app, monkeypatch, posts, status):
    _on(app, monkeypatch)
    assert get_key() is not None
    posts.status = status
    assert submit_urls(URLS) == status


def test_switched_off_sends_nothing(app, posts):
    assert submit_urls(URLS) is None and posts == []


def test_an_empty_list_sends_nothing(app, monkeypatch, posts):
    _on(app, monkeypatch)
    assert get_key() is not None
    assert submit_urls([]) is None and posts == []


def test_a_network_error_returns_none_and_never_raises(app, monkeypatch):
    _on(app, monkeypatch)
    assert get_key() is not None
    calls = []

    def post(url, data=None, json=None, **kwargs):
        calls.append(url)
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(indexnow.requests, "post", post)
    assert submit_urls(URLS) is None
    assert len(calls) == 1


def test_unpatched_it_cannot_reach_the_network(app, monkeypatch):
    _on(app, monkeypatch)
    assert get_key() is not None
    assert submit_urls(URLS) is None


def test_the_dry_run_prints_every_url_and_sends_nothing(app, db, monkeypatch, capsys, posts):
    monkeypatch.setitem(app.config, "COMPANY_WEBSITE", "https://aqua-element.uz")  # the switch stays off
    assert submit_all(dry_run=True) == 0
    urls = indexnow_urls()
    assert capsys.readouterr().out.splitlines() == [f"IndexNow: {len(urls)} URLs for host aqua-element.uz", *urls]
    assert posts == []


@pytest.mark.parametrize("status, code", [(200, 0), (202, 0), (403, 1), (422, 1)])
def test_the_command_prints_the_status_and_exits_by_it(app, db, monkeypatch, capsys, posts, status, code):
    _on(app, monkeypatch)
    assert get_key() is not None
    posts.status = status
    assert submit_all(dry_run=False) == code
    assert f"HTTP {status}" in capsys.readouterr().out
    assert posts[0][2]["urlList"] == indexnow_urls()
    assert status in ACCEPTED_STATUSES or code == 1


def test_switched_off_the_command_says_how_to_switch_it_on(app, db, capsys, posts):
    assert submit_all(dry_run=False) == 2
    assert "INDEXNOW_ENABLED=true" in capsys.readouterr().out
    assert posts == []


def test_the_command_says_when_nothing_was_sent(app, db, monkeypatch, capsys, posts):
    _on(app, monkeypatch)
    monkeypatch.setattr(indexnow, "get_key", lambda: None)
    assert submit_all(dry_run=False) == 1
    assert "not sent: no key file or a network error" in capsys.readouterr().out
