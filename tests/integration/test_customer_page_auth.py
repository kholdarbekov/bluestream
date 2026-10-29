"""Customer pages after arriving from outside (spec 2026-10-27 §4.6, §17 Q4; step 4 Task 9).

Production's JWT cookies are `SameSite=Strict`, so a browser arriving from another site (a Click return,
an email link, a link opened from Telegram) sends none of them on its first request. Every customer page
then answers with one same-site hop (a tiny page that reloads the same URL, a navigation that does send
the cookies); an expired access cookie with a live refresh cookie is renewed quietly through the one
refresh rule (`TokenService.refresh_access_token`, with its revocation cut-off); and only a customer with
no usable cookie after that is sent to `/login?next=`, with `next` checked by the one rule
(`shared.safe_paths.safe_next_path`). API routes keep their JSON 401.

The test client sends every cookie it holds whatever its SameSite, so a cross-site arrival is a fresh
client (Strict sends no cookie cross-site) carrying the browser's `Sec-Fetch-Site`. Every browser signs in
the way login.js does: it opens /login, then posts `{"identifier", "password"}` to `/api/v1/auth/login`.
"""

import json
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup
from flask_jwt_extended import create_access_token, create_refresh_token, decode_token, get_csrf_token

from business_app.services.session_revocation_service import SessionRevocationService
from business_app.services.staff_service import StaffService
from business_app.services.token_service import TokenService
from business_app.utils.page_auth import HOP_MARKER, _this_page
from business_app.utils.user_types import STAFF_ROLE_VALUES
from shared.staff_constants import STAFF_BOT_ROLES
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from shared.enums import UserRole, UserStatus, UserType
from tests.integration.reorder_reminder_builders import seed_translations
from tests.integration.test_session_revocation import next_second

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
ROUTES_SOURCE = (REPO / "business_app" / "frontend" / "routes.py").read_text(encoding="utf-8")
WEB_JS = REPO / "business_app" / "static" / "js"

LOGIN = "/api/v1/auth/login"
REFRESH_COOKIE = "/api/v1/auth/refresh-cookie"
REFRESH_BODY = "/api/v1/auth/refresh-token"  # the body-token renewal (the admin UI calls it; the bot, as /refresh)
PROFILE = "/api/v1/auth/profile"
PASSWORD = "TestPassword123!"  # sample_user's (tests/conftest.py)

SAME = {"Sec-Fetch-Site": "same-origin"}
CROSS = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}
JWT_COOKIES = ("access_token_cookie", "refresh_token_cookie", "csrf_access_token", "csrf_refresh_token")


@pytest.fixture(autouse=True)
def production_cookie_setting(app, db, monkeypatch):
    """§4.6: dev and tests use Lax; this path must run with production's Strict, or it passes here and
    fails there. (`db`: every page, the hop included, renders through the context processors that read it.)"""
    monkeypatch.setitem(app.config, "JWT_COOKIE_SAMESITE", "Strict")


@pytest.fixture
def seeded_copy(app, db):
    """Seeds keys exactly as the seeder ships them, then compiles the pages afresh: Jinja writes a plain
    `{{ 'key'|t }}` into a template when it first compiles it, and `app` lives for the whole session
    (tests/integration/test_loyalty_guide_consecutive_render.py). Cleared again on the way out, so no later test
    reads this test's rows (tests/integration/test_web_payment_return_pages.py renders the same pages)."""

    def seed(*keys):
        seed_translations(db, keys)
        app.jinja_env.cache.clear()

    yield seed
    app.jinja_env.cache.clear()


def _set_cookies(resp):
    """The cookies a response sets, by name: the whole Set-Cookie header of each."""
    return {header.split("=", 1)[0]: header for header in resp.headers.getlist("Set-Cookie")}


def _cookie_value(resp, name):
    return _set_cookies(resp)[name].split(";", 1)[0].split("=", 1)[1]


def _jwt_set_cookies(resp):
    """The login cookies a response hands the browser (the Flask `session` cookie is not one)."""
    return sorted(name for name in _set_cookies(resp) if name in JWT_COOKIES)


def _sign_in(app, user):
    """A browser on the login page, then login.js's request. Answers the browser and the login response."""
    browser = app.test_client()
    assert browser.get("/login", headers=SAME).status_code == 200
    resp = browser.post(LOGIN, json={"identifier": user.email, "password": PASSWORD}, headers={"X-Platform": "web"})
    assert resp.status_code == 200, resp.get_json()
    return browser, resp


def _expire_access_cookie(app, browser, user, claims=None):
    """The access cookie has run out (30 minutes in production); the refresh cookie still lives."""
    with app.app_context():
        expired = create_access_token(
            identity=str(user.id), expires_delta=timedelta(seconds=-60), additional_claims=claims
        )
    browser.set_cookie("access_token_cookie", expired)


def _claims(app, token):
    with app.app_context():
        return decode_token(token)


def _login_next(resp):
    """Where a redirect to the login page sends the customer back to after signing in."""
    assert resp.status_code == 302, resp.get_data(as_text=True)[:300]
    location = urlsplit(resp.headers["Location"])
    assert location.path == "/login", resp.headers["Location"]
    return parse_qs(location.query)["next"]


def _island(resp):
    assert resp.status_code == 200
    return json.loads(BeautifulSoup(resp.get_data(as_text=True), "html.parser").select_one("script#page-data").string)


def _hop_target(resp):
    """Where the hop page sends the browser, checked every way it can go there. Its one script is a file that
    reads the target from a data attribute (the page must also work where inline script is blocked); a plain
    meta refresh continues a browser whose script never runs; a `<noscript>` refresh and link serve a browser
    with script turned off. Answers the target."""
    assert (resp.status_code, resp.mimetype) == (200, "text/html")
    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    scripts = soup.find_all("script")
    assert [urlsplit(script.get("src", "")).path for script in scripts] == ["/static/js/pages/same-site-hop.js"]
    target = soup.html["data-hop-target"]
    refreshes = soup.select('meta[http-equiv="refresh"]')
    plain = [meta["content"] for meta in refreshes if meta.find_parent("noscript") is None]
    without_script = [meta["content"] for meta in refreshes if meta.find_parent("noscript") is not None]
    assert (plain, without_script) == ([f"1; url={target}"], [f"0; url={target}"])
    assert soup.select_one("noscript a")["href"] == target
    return target


# --------------------------------------------------------------------------- the cookie setting


def test_this_module_runs_with_the_production_cookie_setting(app, sample_user):
    _browser, login = _sign_in(app, sample_user)
    named = _set_cookies(login)
    assert all("SameSite=Strict" in named[n] for n in ("access_token_cookie", "refresh_token_cookie"))


# --------------------------------------------------------------------------- the same-site hop


@pytest.mark.parametrize("headers", [CROSS, {}], ids=["cross-site", "no-fetch-metadata"])
def test_a_cross_site_arrival_gets_one_hop_to_the_same_url(app, headers):
    resp = app.test_client().get("/my-subscriptions?sub=7", headers=headers)
    body = resp.get_data(as_text=True)

    assert _hop_target(resp) == "/my-subscriptions?sub=7&_hop=1"
    assert '<meta name="referrer" content="no-referrer">' in body and 'content="noindex,nofollow"' in body
    assert "googletagmanager" not in body and "no-store" in resp.headers["Cache-Control"]
    assert _jwt_set_cookies(resp) == []
    target = BeautifulSoup(body, "html.parser").select_one("noscript a")["href"]
    assert parse_qs(urlsplit(target).query) == {"sub": ["7"], HOP_MARKER: ["1"]}


def test_the_hop_offers_a_continue_link_in_the_page_language(app, seeded_copy):
    """A browser without script follows the meta refresh, or the link."""
    seeded_copy("web.page_auth.continue")

    resp = app.test_client().get("/my-orders?lang=ru", headers=CROSS)

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    link = soup.select_one("noscript a")
    assert (link.get_text(strip=True), link["href"]) == ("Продолжить", "/my-orders?lang=ru&_hop=1")
    assert soup.select_one('noscript meta[http-equiv="refresh"]')["content"] == "0; url=/my-orders?lang=ru&_hop=1"
    assert _hop_target(resp) == "/my-orders?lang=ru&_hop=1"


def test_the_hop_page_works_without_inline_script(app):
    """Its one script is a file that reads the target from a data attribute, never a value written into script.
    A browser that runs no script at all (blocked, or inline script refused) still continues within a second
    through the plain meta refresh; the script, which keeps the #fragment, wins inside that second."""
    resp = app.test_client().get("/my-orders?page=2", headers=CROSS)

    assert _hop_target(resp) == "/my-orders?page=2&_hop=1"
    assert "location.replace" not in resp.get_data(as_text=True)


def test_the_hop_reload_carries_the_cookies_and_renders(app, sample_user):
    browser, _login = _sign_in(app, sample_user)

    resp = browser.get("/my-subscriptions?sub=7&_hop=1", headers=SAME)

    assert resp.status_code == 200, resp.headers.get("Location")
    assert 'class="subs"' in resp.get_data(as_text=True)


def test_the_hop_never_loops(app):
    resp = app.test_client().get("/my-subscriptions?sub=7&_hop=1", headers=CROSS)

    assert _login_next(resp) == ["/my-subscriptions?sub=7"]


@pytest.mark.parametrize("site", ["same-origin", "same-site", "none"])
def test_a_signed_out_visit_from_our_own_site_goes_straight_to_login(app, site):
    resp = app.test_client().get("/my-orders?page=2", headers={"Sec-Fetch-Site": site})

    assert _login_next(resp) == ["/my-orders?page=2"]
    assert _jwt_set_cookies(resp) == []


def test_an_address_too_long_to_hand_back_returns_to_the_page_itself(app):
    resp = app.test_client().get("/my-orders?page=2&note=" + "a" * 2100, headers=SAME)

    assert _login_next(resp) == ["/my-orders"]


@pytest.mark.parametrize(
    "url, page",
    [
        ("/my-orders?page=2&note=" + "a" * 2100, "/my-orders"),  # the query makes it too long: the bare path
        ("/my-orders/" + "a" * 2100, "/"),  # the path alone is too long: the home page
        ("/my%20orders", "/"),  # a path the one rule refuses (whitespace): the home page
    ],
    ids=["long-query", "long-path", "refused-path"],
)
def test_this_page_is_always_a_path_the_one_rule_accepts(app, url, page):
    """Every hop target and every login `next` is built from `_this_page`, so it never answers a path
    `safe_next_path` refused."""
    with app.test_request_context(url):
        assert _this_page() == page


# --------------------------------------------------------------------------- the quiet refresh


def test_an_expired_access_cookie_with_a_live_refresh_cookie_renews_quietly(app, sample_user):
    browser, _login = _sign_in(app, sample_user)
    _expire_access_cookie(app, browser, sample_user)

    first = browser.get("/my-orders", headers=SAME)

    assert (first.status_code, first.headers["Location"]) == (302, "/my-orders?_hop=1")
    claims = _claims(app, _cookie_value(first, "access_token_cookie"))
    assert (claims["sub"], claims["role"]) == (str(sample_user.id), "customer")
    assert browser.get(first.headers["Location"], headers=SAME).status_code == 200


def test_a_cross_site_return_with_an_expired_access_cookie_hops_then_renews(app, sample_user):
    """The Click return: no cookie on the cross-site arrival, then the reload finds the access cookie expired."""
    browser, _login = _sign_in(app, sample_user)
    _expire_access_cookie(app, browser, sample_user)

    hop = app.test_client().get("/my-subscriptions?sub=7", headers=CROSS)  # Strict sends no cookie cross-site
    assert _hop_target(hop) == "/my-subscriptions?sub=7&_hop=1"

    back = browser.get("/my-subscriptions?sub=7&_hop=1", headers=SAME)  # the reload: expired access + live refresh
    assert (back.status_code, back.headers["Location"]) == (302, "/my-subscriptions?sub=7&_hop=2")
    landed = browser.get(back.headers["Location"], headers=SAME)
    assert landed.status_code == 200 and 'class="subs"' in landed.get_data(as_text=True)


def test_a_second_marker_never_refreshes_again(app, sample_user):
    browser, _login = _sign_in(app, sample_user)
    _expire_access_cookie(app, browser, sample_user)

    resp = browser.get("/my-orders?_hop=2", headers=SAME)

    assert _login_next(resp) == ["/my-orders"]
    assert "access_token_cookie" not in _set_cookies(resp)


@pytest.mark.parametrize("marker", ["3", "x", ""])
def test_a_marker_the_page_never_wrote_counts_as_spent(app, sample_user, marker):
    browser, _login = _sign_in(app, sample_user)
    _expire_access_cookie(app, browser, sample_user)

    resp = browser.get(f"/my-orders?_hop={marker}", headers=SAME)

    assert _login_next(resp) == ["/my-orders"]
    assert "access_token_cookie" not in _set_cookies(resp)


def test_a_revoked_refresh_cookie_is_not_renewed(app, db, sample_user):
    browser, _login = _sign_in(app, sample_user)
    next_second()
    SessionRevocationService.revoke_all(sample_user.id, reason="password_reset")
    db.session.commit()

    resp = browser.get("/my-orders", headers=SAME)

    assert _login_next(resp) == ["/my-orders"]
    cleared = _set_cookies(resp)
    for name in ("access_token_cookie", "refresh_token_cookie"):
        assert cleared[name].startswith(f"{name}=;"), cleared[name]
    assert browser.get_cookie("access_token_cookie") is None and browser.get_cookie("refresh_token_cookie") is None


# --------------------------------------------------------------------------- every customer page

CUSTOMER_PAGES = [
    "/my-account",
    "/my-orders",
    "/my-subscriptions",
    "/my-loyalty",
    "/profile-settings",
    "/account-security",
    "/addresses",
    "/order-tracking?order_id=1",
    "/order-confirmation?order_id=1",
    "/checkout",
    "/order/1",
]
PAGE_ENDPOINTS = [
    "checkout",
    "order_confirmation",
    "payment_success",
    "payment_cancel",
    "order_tracking",
    "my_subscriptions",
    "my_account",
    "my_loyalty",
    "my_orders",
    "order_detail",
    "profile_settings",
    "account_security",
    "addresses",
]


@pytest.mark.parametrize("url", CUSTOMER_PAGES + ["/payment/success?order_id=1", "/payment/cancel?order_id=1"])
def test_no_customer_page_ever_answers_a_json_401(app, url):
    resp = app.test_client().get(url, headers=SAME)
    assert resp.status_code != 401 and resp.mimetype != "application/json"


@pytest.mark.parametrize("url", CUSTOMER_PAGES)
def test_every_customer_page_sends_a_signed_out_visitor_to_login_and_back(app, url):
    resp = app.test_client().get(url, headers=SAME)
    assert _login_next(resp) == [url]


def test_a_payment_return_page_knows_the_signed_in_customer(app, sample_user, sample_order):
    browser, _login = _sign_in(app, sample_user)

    resp = browser.get(f"/payment/cancel?order_id={sample_order.id}", headers=SAME)

    assert resp.status_code == 200, resp.headers.get("Location")
    assert sample_order.order_number in resp.get_data(as_text=True)


def test_a_payment_return_with_a_refused_refresh_cookie_renders_signed_out_and_clears_it(
    app, db, seeded_copy, sample_user, sample_order
):
    seeded_copy("web.payment_return.neutral_cancel")
    browser, _login = _sign_in(app, sample_user)
    next_second()
    SessionRevocationService.revoke_all(sample_user.id, reason="password_reset")
    db.session.commit()

    resp = browser.get(f"/payment/cancel?order_id={sample_order.id}", headers=SAME)

    assert (resp.status_code, resp.mimetype) == (200, "text/html"), resp.headers.get("Location")
    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    page = soup.select_one(".cancelled-content").get_text(" ", strip=True)
    assert soup.html["lang"] == "uz"  # the language a request with no preference renders
    assert BACKEND_TRANSLATIONS["web.payment_return.neutral_cancel"]["uz"] in page, page
    assert soup.select_one("#paymentActions") is None
    assert sample_order.order_number not in resp.get_data(as_text=True)
    assert browser.get_cookie("access_token_cookie") is None and browser.get_cookie("refresh_token_cookie") is None


def test_every_page_that_reads_the_session_uses_the_page_decorator(app):
    assert "@jwt_required" not in ROUTES_SOURCE
    assert [
        name for name in PAGE_ENDPOINTS if not getattr(app.view_functions[f"frontend.{name}"], "customer_page", False)
    ] == []


# --------------------------------------------------------------------------- the login page's `next`

BAD_NEXT = [
    "//evil.example/x",
    "https://evil.example/x",
    "/\\evil.example",
    "javascript:alert(1)",
    "/my-orders\r\nSet-Cookie: x=1",
    "/a?u=https://evil.example",
    "",
]


@pytest.mark.parametrize("bad", BAD_NEXT)
def test_login_drops_a_next_outside_the_allowlist(app, sample_user, bad):
    browser, _login = _sign_in(app, sample_user)
    signed_in = browser.get("/login", query_string={"next": bad}, headers=SAME)
    assert (signed_in.status_code, signed_in.headers["Location"]) == (302, "/my-account")

    island = _island(app.test_client().get("/login", query_string={"next": bad}, headers=SAME))
    assert island["after_login_url"] == "/my-account"


def test_login_keeps_an_allowed_next(app, sample_user):
    browser, _login = _sign_in(app, sample_user)
    signed_in = browser.get("/login", query_string={"next": "/my-subscriptions?sub=7"}, headers=SAME)
    assert (signed_in.status_code, signed_in.headers["Location"]) == (302, "/my-subscriptions?sub=7")

    island = _island(app.test_client().get("/login", query_string={"next": "/my-subscriptions?sub=7"}, headers=SAME))
    assert island["after_login_url"] == "/my-subscriptions?sub=7"


@pytest.mark.parametrize("bad", ["//evil.example/x", "https://evil.example/x"])
def test_register_drops_a_next_outside_the_allowlist(app, sample_user, bad):
    browser, _login = _sign_in(app, sample_user)
    signed_in = browser.get("/register", query_string={"next": bad}, headers=SAME)
    assert (signed_in.status_code, signed_in.headers["Location"]) == (302, "/my-account")


# --------------------------------------------------------------------------- API routes and the refresh route


def test_api_routes_keep_their_json_401(app, sample_user):
    resp = app.test_client().get(PROFILE, headers=SAME)
    assert (resp.status_code, resp.get_json()["error"]) == (401, "Authorization Required")

    browser, _login = _sign_in(app, sample_user)
    _expire_access_cookie(app, browser, sample_user)
    expired = browser.get(PROFILE, headers=SAME)
    assert (expired.status_code, expired.get_json()["error"]) == (401, "Token Expired")
    assert _jwt_set_cookies(expired) == []


def test_the_refresh_cookie_route_renews_through_the_one_refresh_rule(app, db, sample_user):
    """base.js's renewal: the httpOnly refresh cookie and its readable CSRF value in X-CSRF-TOKEN."""
    browser, _login = _sign_in(app, sample_user)
    csrf = browser.get_cookie("csrf_refresh_token").value

    renewed = browser.post(REFRESH_COOKIE, headers={"X-CSRF-TOKEN": csrf})

    assert renewed.status_code == 200, renewed.get_json()
    claims = _claims(app, _cookie_value(renewed, "access_token_cookie"))
    assert claims["sub"] == str(sample_user.id)
    assert claims["role"] == "customer" and claims["session_id"]

    sample_user.status = UserStatus.INACTIVE
    db.session.commit()
    refused = browser.post(REFRESH_COOKIE, headers={"X-CSRF-TOKEN": csrf})
    assert refused.status_code == 401, refused.get_json()
    assert "access_token_cookie" not in _set_cookies(refused)


@pytest.mark.parametrize("with_cookies", [False, True], ids=["header-only", "header-instead-of-the-csrf-value"])
def test_the_refresh_cookie_route_reads_the_refresh_token_from_the_cookie_only(app, sample_user, with_cookies):
    """A token in an Authorization header skips the cookie's CSRF check, so a refresh token sent there can never
    stand in for the cookie: alone it renews nothing, and beside the cookies it cannot replace the CSRF value."""
    browser, login = _sign_in(app, sample_user)
    refresh_token = _cookie_value(login, "refresh_token_cookie")
    client = browser if with_cookies else app.test_client()

    resp = client.post(REFRESH_COOKIE, headers={"Authorization": f"Bearer {refresh_token}"})

    assert resp.status_code == 401, resp.get_json()
    assert "access_token_cookie" not in _set_cookies(resp)


def test_an_access_token_in_the_refresh_cookie_slot_is_never_renewed(app, sample_user):
    """The one refresh rule renews refresh tokens only. An access token planted in the refresh cookie, with its
    readable CSRF half, is refused, and the leftover cookies are cleared as for any refused refresh cookie."""
    browser, login = _sign_in(app, sample_user)
    access_token = _cookie_value(login, "access_token_cookie")
    _expire_access_cookie(app, browser, sample_user)
    browser.set_cookie("refresh_token_cookie", access_token)
    browser.set_cookie("csrf_refresh_token", _claims(app, access_token)["csrf"])

    resp = browser.get("/my-account", headers=SAME)

    assert _login_next(resp) == ["/my-account"]
    assert browser.get_cookie("access_token_cookie") is None


def test_the_refresh_token_route_refuses_an_access_token(app, sample_user):
    """The body-token renewal (`AuthService.refresh_token`, which the admin UI calls and the customer bot reaches
    as `/auth/refresh`) refuses an access token in the same words as any other token it cannot renew, and still
    renews a refresh token."""
    _browser, login = _sign_in(app, sample_user)
    tokens = login.get_json()["data"]["tokens"]
    client = app.test_client()

    refused = client.post(REFRESH_BODY, json={"refresh_token": tokens["access_token"]})
    garbage = client.post(REFRESH_BODY, json={"refresh_token": "not.a.jwt"})

    assert (refused.status_code, refused.get_json()) == (401, garbage.get_json()), refused.get_json()
    assert garbage.status_code == 401
    assert client.post(REFRESH_BODY, json={"refresh_token": tokens["refresh_token"]}).status_code == 200


# --------------------------------------------------------------------------- staff logins (Task 9fix)
#
# The admin UI signs in through the same `POST /auth/login`, so its cookies sit on the storefront's cookie
# domain (`.aqua-element.uz` in production), and it never deletes the readable `csrf_refresh_token`. A lapsed
# staff login therefore keeps both halves. The storefront renews a customer's login only: renewing a staff one
# would mint an access token with the staff role, which the admin API accepts. Nor does the storefront end that
# login: its cookies are the admin subdomain's.

NON_CUSTOMER_ROLES = [role for role in UserRole if role is not UserRole.CUSTOMER]


def _lapsed_login(app, db, user, role):
    """A browser whose `role` login lapsed: signed in through `/auth/login`, as the admin UI and the storefront
    both do, then its access cookie (with that role claim) ran out. The refresh cookie and both readable CSRF
    halves remain."""
    if role is not UserRole.CUSTOMER:
        user.role, user.user_type = role, UserType.STAFF
        db.session.commit()
    browser, _login = _sign_in(app, user)
    _expire_access_cookie(app, browser, user, claims={"role": role.value})
    return browser


@pytest.mark.parametrize("role", NON_CUSTOMER_ROLES, ids=lambda role: role.value)
def test_a_lapsed_staff_login_is_never_renewed_by_a_cabinet_page(app, db, sample_user, role):
    browser = _lapsed_login(app, db, sample_user, role)
    held = _login_cookies(browser)

    resp = browser.get("/my-account", headers=SAME)

    assert _login_next(resp) == ["/my-account"]
    assert _jwt_set_cookies(resp) == []  # no access cookie minted, and the admin's login is not ended either
    assert _login_cookies(browser) == held


@pytest.mark.parametrize("role", list(UserRole), ids=lambda role: role.value)
def test_the_refresh_cookie_route_renews_only_a_customer(app, db, sample_user, role):
    """base.js's renewal, from the same browser state: a customer's login is renewed, a staff one is refused
    with the route's usual 401, and nothing is set or cleared."""
    browser = _lapsed_login(app, db, sample_user, role)
    held = _login_cookies(browser)

    resp = browser.post(REFRESH_COOKIE, headers={"X-CSRF-TOKEN": held["csrf_refresh_token"]})

    if role is UserRole.CUSTOMER:
        assert resp.status_code == 200, resp.get_json()
        assert _claims(app, _cookie_value(resp, "access_token_cookie"))["role"] == "customer"
    else:
        assert resp.status_code == 401, resp.get_json()
        assert _jwt_set_cookies(resp) == []
        assert _login_cookies(browser) == held


def test_a_staff_login_on_a_render_page_sees_the_neutral_cancel_page_and_keeps_its_cookies(
    app, db, seeded_copy, sample_user, sample_order
):
    """`/payment/cancel` renders signed out for anyone the storefront will not renew: the neutral page, no order
    number, nothing minted and nothing cleared (the admin's login stays)."""
    seeded_copy("web.payment_return.neutral_cancel")
    browser = _lapsed_login(app, db, sample_user, UserRole.ADMIN)
    held = _login_cookies(browser)

    resp = browser.get(f"/payment/cancel?order_id={sample_order.id}", headers=SAME)

    assert (resp.status_code, resp.mimetype) == (200, "text/html"), resp.headers.get("Location")
    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    assert soup.html["lang"] == "uz"
    assert BACKEND_TRANSLATIONS["web.payment_return.neutral_cancel"]["uz"] in soup.select_one(
        ".cancelled-content"
    ).get_text(" ", strip=True)
    assert sample_order.order_number not in resp.get_data(as_text=True)
    assert _jwt_set_cookies(resp) == []  # nothing minted, nothing cleared
    assert _login_cookies(browser) == held


# A customer-role user who also holds staff roles (`StaffService.update_staff_roles`) or is typed staff is staff:
# staff routes read their roles from the database, not from the token's `role` claim, so a revived customer-role
# access token would still open them.
STAFF_GRANTS = [
    pytest.param({"staff_roles": [role], "user_type": UserType.STAFF}, id=f"staff_roles-{role}") for role in sorted(STAFF_ROLE_VALUES)
] + [pytest.param({"user_type": UserType.STAFF}, id="user_type-staff")]
# (the gate's reader keeps only the staff-bot roles) `staff_roles` alone, user type left individual: `User.is_staff_user` says no, the staff gate says yes (users 66, 67 on dev)
STAFF_ROLES_ALONE = [
    pytest.param({"staff_roles": [role]}, id=f"staff_roles_only-{role}") for role in STAFF_BOT_ROLES
]


def _lapsed_customer_login(app, db, user, **grants):
    browser, _login = _sign_in(app, user)
    for field, value in grants.items():
        setattr(user, field, value)
    db.session.commit()
    _expire_access_cookie(app, browser, user, claims={"role": "customer"})
    return browser


@pytest.mark.parametrize("grants", STAFF_GRANTS + STAFF_ROLES_ALONE)
def test_a_customer_role_with_staff_roles_is_never_renewed_by_the_storefront(app, db, sample_user, grants):
    browser = _lapsed_customer_login(app, db, sample_user, **grants)
    assert (sample_user.is_staff_user or StaffService._extract_staff_roles(sample_user)) and sample_user.role == UserRole.CUSTOMER
    held = _login_cookies(browser)

    page = browser.get("/my-account", headers=SAME)
    assert _login_next(page) == ["/my-account"]
    assert _jwt_set_cookies(page) == []
    assert _login_cookies(browser) == held

    route = browser.post(REFRESH_COOKIE, headers={"X-CSRF-TOKEN": held["csrf_refresh_token"]})
    assert route.status_code == 401, route.get_json()
    assert _jwt_set_cookies(route) == []
    assert _login_cookies(browser) == held


def test_an_entity_customer_is_still_renewed_on_both_storefront_paths(app, db, sample_user):
    browser = _lapsed_customer_login(app, db, sample_user, user_type=UserType.ENTITY)

    page = browser.get("/my-account", headers=SAME)  # renewed: the access cookie is minted, then the reload
    assert (page.status_code, page.headers["Location"]) == (302, "/my-account?_hop=1")
    assert _claims(app, _cookie_value(page, "access_token_cookie"))["role"] == "customer"

    _expire_access_cookie(app, browser, sample_user, claims={"role": "customer"})
    route = browser.post(REFRESH_COOKIE, headers={"X-CSRF-TOKEN": _login_cookies(browser)["csrf_refresh_token"]})
    assert route.status_code == 200, route.get_json()
    assert _claims(app, _cookie_value(route, "access_token_cookie"))["role"] == "customer"


@pytest.mark.parametrize("role", [UserRole.CUSTOMER, UserRole.ADMIN], ids=lambda role: role.value)
def test_a_refresh_cookie_in_its_last_half_hour_is_never_re_minted_into_an_access_cookie(app, db, sample_user, role):
    """The `after_request` hook extends a token that expires within 30 minutes. On the refresh route the
    request's token is the refresh token: re-minting it would hand a staff login an access cookie on the very
    401 that refused it, and would overwrite a customer's renewed access cookie with one that has no role."""
    browser = _lapsed_login(app, db, sample_user, role)
    session_id = _claims(app, browser.get_cookie("refresh_token_cookie").value)["session_id"]
    with app.app_context():
        ending = create_refresh_token(
            identity=str(sample_user.id),
            expires_delta=timedelta(minutes=10),
            additional_claims={"user_id": sample_user.id, "session_id": session_id, "platform": "web"},
        )
        csrf = get_csrf_token(ending)
    browser.set_cookie("refresh_token_cookie", ending)
    browser.set_cookie("csrf_refresh_token", csrf)

    resp = browser.post(REFRESH_COOKIE, headers={"X-CSRF-TOKEN": csrf})

    handed = [header for header in resp.headers.getlist("Set-Cookie") if header.startswith("access_token_cookie=")]
    if role is UserRole.CUSTOMER:
        assert (resp.status_code, len(handed)) == (200, 1), (resp.get_json(), handed)
        assert _claims(app, handed[0].split(";", 1)[0].split("=", 1)[1])["role"] == "customer"
    else:
        assert (resp.status_code, handed) == (401, []), resp.get_json()


# --------------------------------------------------------------------------- "sign in again" (Task 9fix)
#
# base.js renews and repeats a request only on a 401 the backend marks `reauth: true`: the JWT layer's answer
# that the login itself lapsed. Any other 401 (a wrong password) goes back to the page that asked.

REAUTH_ANSWERS = {
    "missing": ("Authorization Required", "Request does not contain an access token."),
    "expired": ("Token Expired", "The token has expired."),
    "invalid": ("Invalid Token", "The token is invalid."),
    "revoked": ("Token Revoked", "The token has been revoked."),
    "unknown_user": ("User Not Found", "Token identity is no longer valid."),
}


@pytest.mark.parametrize("case", list(REAUTH_ANSWERS))
def test_every_jwt_layer_401_asks_the_client_to_sign_in_again(app, sample_user, case):
    with app.app_context():
        live = create_access_token(identity=str(sample_user.id))
        tokens = {
            "missing": None,
            "expired": create_access_token(identity=str(sample_user.id), expires_delta=timedelta(seconds=-60)),
            "invalid": "not.a.jwt",
            "revoked": live,
            "unknown_user": create_access_token(identity="987654321"),
        }
        if case == "revoked":
            TokenService().blacklist_token(decode_token(live)["jti"])
    headers = {"Authorization": f"Bearer {tokens[case]}"} if tokens[case] else {}

    resp = app.test_client().get(PROFILE, headers=headers)

    error, message = REAUTH_ANSWERS[case]
    assert (resp.status_code, resp.get_json()) == (401, {"error": error, "message": message, "reauth": True})


def test_a_wrong_password_is_a_401_that_never_asks_to_sign_in_again(app, sample_user):
    """login.js shows this answer's message; base.js hands it back unchanged because it carries no `reauth`."""
    resp = app.test_client().post(
        LOGIN, json={"identifier": sample_user.email, "password": "Not-the-password-1"}, headers={"X-Platform": "web"}
    )

    body = resp.get_json()
    assert (resp.status_code, body["success"], "reauth" in body) == (401, False, False), body
    assert body["message"]


# --------------------------------------------------------------------------- one renewal path (Task 9fix)


def test_no_storefront_script_renews_through_the_body_token_route():
    """The storefront renews one way: base.js `apiRequest` through `/auth/refresh-cookie`, under the one
    storefront rule. The old copies (base.js `refreshToken()`, cabinet-base.js) are gone."""
    offenders = sorted(
        str(path.relative_to(WEB_JS))
        for path in WEB_JS.rglob("*.js")
        if "/auth/refresh-token" in path.read_text(encoding="utf-8", errors="ignore")
    )
    assert offenders == []
    assert not (WEB_JS / "pages" / "cabinet-base.js").exists()
    cabinet_base = (REPO / "business_app" / "templates" / "frontend" / "cabinet_base.html").read_text(encoding="utf-8")
    assert "cabinet-base" not in cabinet_base and "refresh_with_cookie" not in cabinet_base


# --------------------------------------------------------------------------- Log out (fix round 1)
#
# A signed-out arrival must never get the previous customer's cabinet back through the quiet refresh. Before
# this round, a Log out after the 30-minute access cookie had expired got a 401 from `/auth/logout`, base.js
# swallowed it, and the httpOnly refresh cookie survived for the quiet refresh to bring back. Now
# `/auth/logout` proves the login with the refresh cookie too and always clears the cookies, a call that did
# not succeed sends the browser to the sign-out page `/logout`, and a refresh cookie whose readable CSRF half
# Log out deleted is never renewed.

LOGOUT = "/api/v1/auth/logout"
SIGN_OUT_PAGE = "/logout"
READABLE_HALVES = ("csrf_access_token", "csrf_refresh_token")


def _base_js_logout(browser):
    """base.js's Log out call: no body, the refresh cookie's readable CSRF value in X-CSRF-TOKEN."""
    return browser.post(
        LOGOUT, headers={"X-Platform": "web", "X-CSRF-TOKEN": browser.get_cookie("csrf_refresh_token").value}
    )


def _script_deletes_the_readable_halves(browser):
    """What base.js's Log out does to the cookies script can reach, whatever its call answered."""
    for name in READABLE_HALVES:
        browser.delete_cookie(name)


def _cleared(resp):
    """The login cookies a response clears (an empty value), by name."""
    return sorted(name for name, header in _set_cookies(resp).items() if name in JWT_COOKIES and "=;" in header)


def _a_browser_holding(app, login_cookies):
    """Another browser (the next person at a shared computer, say) holding these cookie values."""
    browser = app.test_client()
    for name, value in login_cookies.items():
        browser.set_cookie(name, value)
    return browser


def _login_cookies(browser):
    return {name: browser.get_cookie(name).value for name in JWT_COOKIES if browser.get_cookie(name)}


def test_logging_out_after_the_access_cookie_expired_ends_the_login(app, sample_user):
    browser, _login = _sign_in(app, sample_user)
    _expire_access_cookie(app, browser, sample_user)
    held = _login_cookies(browser)

    resp = _base_js_logout(browser)

    assert resp.status_code == 200, resp.get_json()
    assert _cleared(resp) == sorted(JWT_COOKIES)
    assert _login_next(browser.get("/my-account", headers=SAME)) == ["/my-account"]
    # The login itself is over, not only this browser's copy: the same cookies elsewhere are refused.
    assert _login_next(_a_browser_holding(app, held).get("/my-account", headers=SAME)) == ["/my-account"]
    renewal = _a_browser_holding(app, held).post(REFRESH_COOKIE, headers={"X-CSRF-TOKEN": held["csrf_refresh_token"]})
    assert renewal.status_code == 401


def test_logging_out_while_the_access_cookie_lives_ends_the_login(app, sample_user):
    browser, _login = _sign_in(app, sample_user)
    held = _login_cookies(browser)

    resp = _base_js_logout(browser)

    assert resp.status_code == 200, resp.get_json()
    assert _cleared(resp) == sorted(JWT_COOKIES)
    assert _login_next(browser.get("/my-account", headers=SAME)) == ["/my-account"]
    assert _login_next(_a_browser_holding(app, held).get("/my-account", headers=SAME)) == ["/my-account"]


@pytest.mark.parametrize("answer", ["never_reached_the_server", "refused"])
def test_a_log_out_call_that_did_not_succeed_still_signs_the_browser_out(app, sample_user, answer):
    """Offline, a 5xx or nginx's auth rate limit (`never_reached_the_server`), or a 401 (`refused`): base.js
    deletes the readable halves and goes to the sign-out page, which clears the httpOnly cookies."""
    browser, _login = _sign_in(app, sample_user)
    _expire_access_cookie(app, browser, sample_user)
    if answer == "refused":
        refused = browser.post(LOGOUT, headers={"X-Platform": "web"})  # no CSRF value: it proves nothing
        assert refused.status_code == 401 and _cleared(refused) == sorted(JWT_COOKIES)
    _script_deletes_the_readable_halves(browser)

    page = browser.get(SIGN_OUT_PAGE, headers=SAME)

    assert (page.status_code, urlsplit(page.headers["Location"]).path) == (302, "/login")
    assert _cleared(page) == sorted(JWT_COOKIES)
    assert browser.get_cookie("access_token_cookie") is None and browser.get_cookie("refresh_token_cookie") is None
    assert _login_next(browser.get("/my-account", headers=SAME)) == ["/my-account"]


@pytest.mark.parametrize("arrival", ["from_our_site", "from_another_site"])
def test_a_browser_the_old_log_out_left_behind_is_not_signed_back_in(app, sample_user, arrival):
    """The pre-deploy residue: the old base.js's call failed and it deleted only the readable halves, so the
    expired access cookie and a live refresh cookie stayed. The next arrival gets the login page, and the
    leftover cookies are cleared."""
    browser, _login = _sign_in(app, sample_user)
    _expire_access_cookie(app, browser, sample_user)
    _script_deletes_the_readable_halves(browser)
    page = "/my-account"
    if arrival == "from_another_site":  # Strict sends no cookie cross-site: the hop, then the reload
        hop = app.test_client().get(page, headers=CROSS)
        assert _hop_target(hop) == "/my-account?_hop=1"
        page = "/my-account?_hop=1"

    resp = browser.get(page, headers=SAME)

    assert _login_next(resp) == ["/my-account"]
    assert browser.get_cookie("access_token_cookie") is None and browser.get_cookie("refresh_token_cookie") is None


def test_the_sign_out_page_needs_no_sign_in(app):
    resp = app.test_client().get(SIGN_OUT_PAGE, headers=SAME)

    assert (resp.status_code, urlsplit(resp.headers["Location"]).path) == (302, "/login")
    assert _cleared(resp) == sorted(JWT_COOKIES)


def test_the_sign_out_page_ends_a_live_login_and_hands_back_no_fresh_cookie(app, db, sample_user, monkeypatch):
    """With production's 30-minute access tokens the `after_request` hook re-mints a signed-in access cookie on
    every request. A browser with no session language reaches the hook signed in (the language lookup reads
    the access cookie), so the sign-out page must end the login or the hook would hand a fresh one back."""
    monkeypatch.setitem(app.config, "JWT_ACCESS_TOKEN_EXPIRES", timedelta(minutes=30))
    sample_user.preferred_language = app.config["DEFAULT_LANGUAGE"]  # no language redirect first
    db.session.commit()
    signed_in, _login = _sign_in(app, sample_user)
    held = _login_cookies(signed_in)
    browser = _a_browser_holding(app, held)  # the same login, without the Flask session cookie

    resp = browser.get(SIGN_OUT_PAGE, headers=SAME)

    assert (resp.status_code, urlsplit(resp.headers["Location"]).path) == (302, "/login")
    assert browser.get_cookie("access_token_cookie") is None
    assert (
        app.test_client().get(PROFILE, headers={"Authorization": f"Bearer {held['access_token_cookie']}"}).status_code
        == 401
    )
    assert _login_next(_a_browser_holding(app, held).get("/my-account", headers=SAME)) == ["/my-account"]


def test_logout_with_nothing_to_prove_answers_401_and_clears_the_cookies(app):
    resp = app.test_client().post(LOGOUT, headers={"X-Platform": "web"})

    assert resp.status_code == 401
    assert _cleared(resp) == sorted(JWT_COOKIES)
