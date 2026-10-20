"""Each app logs out only itself (spec 2026-10-19 R10, owner decision 2026-10-20).

There is no "log out of all (other) devices" anywhere: the storefront's menu "Log out" ends this
browser's login only, and the customer bot has no Logout. The routes, the storefront script and
the bot call that served the old feature are gone.
"""

import pathlib

import pytest

from tests.integration.test_session_revocation import OLD_PASSWORD, sign_in

pytestmark = pytest.mark.integration

ROOT = pathlib.Path(__file__).resolve().parents[2]
# 405, not 404, where DELETE /auth/sessions/<session_id> owns the path shape: no handler for POST either way
RETIRED_ROUTES = [
    ("/api/v1/auth/logout-all", 404),
    ("/api/v1/auth/sessions/revoke-all", 405),
    ("/api/v1/session/sessions/revoke-all", 404),
]


@pytest.mark.parametrize("route,expected_status", RETIRED_ROUTES)
def test_the_log_out_of_all_devices_routes_have_no_handler(app, db, sample_user, route, expected_status):
    tokens = sign_in(app, sample_user.email, OLD_PASSWORD)

    resp = app.test_client().post(route, headers={"Authorization": f"Bearer {tokens['access_token']}"})

    assert resp.status_code == expected_status
    assert {rule.endpoint for rule in app.url_map.iter_rules()}.isdisjoint(
        {"auth.logout_all", "session_management.revoke_all_sessions"}
    )


def test_the_storefront_has_no_log_out_of_all_devices_code():
    offenders = [
        str(path.relative_to(ROOT))
        for folder in ("business_app/static/js", "business_app/templates")
        for path in (ROOT / folder).rglob("*")
        if path.is_file() and path.suffix in {".js", ".html"}
        for text in [path.read_text(encoding="utf-8")]
        if "logout-all" in text or "logoutAll" in text
    ]
    assert offenders == []


def test_the_menu_log_out_still_logs_out_this_browser_only(app, db, sample_user):
    this_device = sign_in(app, sample_user.email, OLD_PASSWORD)
    other_device = sign_in(app, sample_user.email, OLD_PASSWORD)

    resp = app.test_client().post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {this_device['access_token']}"})

    assert resp.status_code == 200, resp.get_json()
    profile = "/api/v1/auth/profile"
    assert app.test_client().get(profile, headers={"Authorization": f"Bearer {this_device['access_token']}"}).status_code == 401
    assert app.test_client().get(profile, headers={"Authorization": f"Bearer {other_device['access_token']}"}).status_code == 200
