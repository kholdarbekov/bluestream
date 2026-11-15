"""asset_url: the versioned static URL every base.html stylesheet and script goes through (website redesign 2026-11-13)."""
import os

from business_app.utils.template_helpers import asset_url


def test_asset_url_versions_by_file_mtime(app):
    with app.test_request_context("/"):
        expected = int(os.stat(os.path.join(app.static_folder, "css/style.css")).st_mtime)
        assert asset_url("css/style.css") == f"/static/css/style.css?v={expected}"


def test_asset_url_changes_when_the_file_changes(app, monkeypatch):
    from business_app.utils import template_helpers

    real_stat = os.stat
    with app.test_request_context("/"):
        before = asset_url("css/style.css")
        shifted = os.stat_result(tuple(real_stat(os.path.join(app.static_folder, "css/style.css")))[:8] + (1_900_000_000, 1_900_000_000))
        monkeypatch.setattr(template_helpers.os, "stat", lambda p: shifted)
        after = asset_url("css/style.css")
        assert after == "/static/css/style.css?v=1900000000"
        assert after != before


def test_asset_url_missing_file_falls_back_to_plain_static_url(app):
    with app.test_request_context("/"):
        assert asset_url("css/does-not-exist.css") == "/static/css/does-not-exist.css"


def test_asset_url_is_a_template_global(app):
    with app.test_request_context("/"):
        rendered = app.jinja_env.from_string("{{ asset_url('css/style.css') }}").render()
        assert rendered.startswith("/static/css/style.css?v=")
