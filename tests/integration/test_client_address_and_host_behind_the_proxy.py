"""Behind nginx the app has one client address and one host: the values ProxyFix hands Flask (Task Gfix, 2026-10-29).

nginx (nginx/nginx.conf) resolves the customer behind the Cloudflare Tunnel into `$remote_addr`, appends it as the
last X-Forwarded-For entry, sends `X-Forwarded-Proto: https` and the Host it served. Production wraps the app in
`ProductionConfig.trust_the_front_proxy`, so `request.remote_addr` is the customer and `request.host` is that Host.
What a client typed passes through nginx untouched: an `X-Forwarded-Host` of its choosing (it rewrote every href of
/.well-known/api-catalog on prod) and the leading X-Forwarded-For entries. So no code reads either header itself.
Every request below is shaped as nginx forwards it, through the production wrapping.
"""

import inspect
import io
import logging
import os
import re

import pytest
from flask_jwt_extended import create_access_token, decode_token
from PIL import Image

from business_app.config.production import ProductionConfig
from business_app.middleware.auth_middleware import audit_log_action
from business_app.utils.rate_limiting import get_ip_address
from tests.integration.reorder_reminder_builders import captured_logs

pytestmark = pytest.mark.integration

CUSTOMER = "203.0.113.7"  # nginx's $remote_addr, from CF-Connecting-IP
TYPED_BY_THE_CLIENT = "198.51.100.9"
AS_NGINX_FORWARDS = {
    "Host": "aqua-element.uz",
    "X-Forwarded-Proto": "https",
    "X-Forwarded-For": f"{TYPED_BY_THE_CLIENT}, {CUSTOMER}",
    "X-Real-IP": CUSTOMER,
    "X-Forwarded-Host": "xfh-probe.invalid",
}
PASSWORD = "TestPassword123!"  # conftest's sample_user


@pytest.fixture
def behind_production_proxy(app):
    unwrapped = app.wsgi_app
    ProductionConfig.trust_the_front_proxy(app)
    yield
    app.wsgi_app = unwrapped


def test_production_wraps_the_app_in_that_proxy_fix():
    assert "cls.trust_the_front_proxy(app)" in inspect.getsource(ProductionConfig.init_app)


def test_the_api_catalog_links_the_real_host(app, behind_production_proxy):
    resp = app.test_client().get("/.well-known/api-catalog", headers=AS_NGINX_FORWARDS)

    assert resp.status_code == 200
    [context] = resp.get_json(force=True)["linkset"]
    hrefs = [context["anchor"]] + [link["href"] for rel, links in context.items() if rel != "anchor" for link in links]
    assert hrefs and all(href.startswith("https://aqua-element.uz/") for href in hrefs), hrefs


def test_url_for_external_builds_on_the_real_host(app, db, behind_production_proxy):
    """Every storefront page's hreflang links are `url_for(request.endpoint, _external=True)` (base.html)."""
    resp = app.test_client().get("/contact", headers=AS_NGINX_FORWARDS)

    page = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert dict(re.findall(r'hreflang="(uz|en|ru)" href="([^"]*)"', page)) == {
        "uz": "https://aqua-element.uz/contact",
        "en": "https://aqua-element.uz/contact?lang=en",
        "ru": "https://aqua-element.uz/contact?lang=ru",
    }
    assert "xfh-probe.invalid" not in page  # the canonical, the JSON-LD and the logo included


def test_an_uploaded_file_is_linked_on_the_real_host(app, db, admin_user, behind_production_proxy):
    token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})
    png = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(png, "PNG")
    png.seek(0)

    resp = app.test_client().post(
        "/api/v1/admin/upload/file",
        data={"file": (png, "probe.png"), "folder": "images"},
        content_type="multipart/form-data",
        headers={**AS_NGINX_FORWARDS, "Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 201, resp.get_json()
    data = resp.get_json()["data"]
    assert data["url"] == f"https://aqua-element.uz/uploads/{data['file_path']}"
    os.remove(os.path.join(app.config["UPLOAD_FOLDER"], data["file_path"]))


def test_a_login_and_its_refresh_record_the_customer_not_what_the_client_typed(
    app, db, sample_user, behind_production_proxy
):
    login = app.test_client().post(
        "/api/v1/auth/login",
        json={"identifier": sample_user.email, "password": PASSWORD},
        headers=AS_NGINX_FORWARDS,
    )
    assert login.status_code == 200, login.get_json()
    tokens = login.get_json()["data"]["tokens"]

    refreshed = app.test_client().post(
        "/api/v1/auth/refresh-token", json={"refresh_token": tokens["refresh_token"]}, headers=AS_NGINX_FORWARDS
    )
    assert refreshed.status_code == 200, refreshed.get_json()
    sessions = app.test_client().get(
        "/api/v1/auth/sessions", headers={**AS_NGINX_FORWARDS, "Authorization": f"Bearer {tokens['access_token']}"}
    )

    with app.app_context():
        claims = decode_token(tokens["access_token"])
        assert claims["ip"] == CUSTOMER
        assert decode_token(refreshed.get_json()["data"]["access_token"])["ip"] == CUSTOMER
    assert sessions.status_code == 200, sessions.get_json()
    this_login = claims["session_id"].split("_")[-1]  # the Redis key's suffix, which the list names it by
    listed = sessions.get_json()["data"]["sessions"]
    assert [s["ip_address"] for s in listed if s["session_id"] == this_login] == [CUSTOMER]


def test_the_audit_decorator_and_the_ip_key_read_the_resolved_address(app, caplog):
    """Neither has a caller today (audit_log_action decorates nothing; get_ip_address keys only the unused
    RateLimitDecorators), so each is driven directly with the address ProxyFix resolved and a typed header."""
    with app.test_request_context(
        headers={"X-Forwarded-For": TYPED_BY_THE_CLIENT}, environ_base={"REMOTE_ADDR": CUSTOMER}
    ):
        with captured_logs(caplog, logging.getLogger("business_app.middleware.auth_middleware"), logging.INFO):
            audit_log_action("probe")(lambda: "ok")()
        key = get_ip_address()

    [audit] = [record.getMessage() for record in caplog.records if record.getMessage().startswith("Audit log: ")]
    assert f"'ip_address': '{CUSTOMER}'" in audit and TYPED_BY_THE_CLIENT not in audit
    assert key == CUSTOMER
