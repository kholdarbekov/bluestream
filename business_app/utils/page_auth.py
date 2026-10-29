"""Customer page auth (spec 2026-10-27 §4.6, §17 Q4): the one handler for every customer page route.

Production's JWT cookies are `SameSite=Strict` (config/production.py), so a browser arriving from another
site (a Click return, an email link, a link opened from Telegram) sends none of them on its first request,
and the access cookie lives 30 minutes. A page route therefore never answers the API's JSON 401:
- a cross-site arrival with no cookie gets one same-site hop, a tiny page that reloads the same URL; that
  navigation is same-site, so the browser now sends the cookies;
- an expired access cookie with a live refresh cookie is renewed through the one refresh rule
  (`TokenService.refresh_access_token`, which applies the session-revocation cut-off) under its storefront
  rule (a customer's login only), then the page reloads with the new cookie. The refresh cookie must still
  have its readable CSRF half, which Log out always deletes (`_renewed_access_token`);
- only a customer with no usable cookie after that is sent to `/login?next=<this page>`.
The `_hop` marker counts the redirects this module made, so a page hops once and refreshes once at most and
never loops. API routes keep `@jwt_required()` and their JSON 401.
"""

from functools import partial, wraps
from typing import Literal, Optional
from urllib.parse import urlencode

from flask import Response, current_app, g, make_response, redirect, render_template, request, url_for
from flask_jwt_extended import get_jwt_identity, set_access_cookies, unset_jwt_cookies, verify_jwt_in_request
from flask_jwt_extended.exceptions import JWTExtendedException
from jwt.exceptions import PyJWTError

from business_app.services.token_service import NotACustomerLogin, TokenService
from shared.safe_paths import safe_next_path

HOP_MARKER = "_hop"
# The marker's values: one redirect made (the hop, or the refresh), or both. Anything else counts as spent.
_MARKER_COUNTS = {"1": 1, "2": 2}
_SPENT = 2
_PAGE_METHODS = ("GET", "HEAD")
# A top-level navigation that may have arrived without our cookies: from another site, or from a browser
# that sends no fetch metadata.
_MAY_HAVE_DROPPED_COOKIES = (None, "cross-site")


def page_user_id() -> Optional[int]:
    """The signed-in customer's id on a `customer_page_required` page, or None when nobody is signed in
    (a `signed_out="render"` page)."""
    return g.get("page_user_id")


def render_same_site_hop(target: str) -> Response:
    """The tiny public page that reloads `target` as a same-site navigation. `target` must already have
    passed `safe_next_path`. It is never cached, sends no Referer, and is kept out of search engines."""
    response = make_response(render_template("frontend/same_site_hop.html", target=target))
    response.headers["Cache-Control"] = "no-store"
    return response


def _marker_count() -> int:
    raw = request.args.get(HOP_MARKER)
    if raw is None:
        return 0
    return _MARKER_COUNTS.get(raw, _SPENT)


def _this_page() -> str:
    """This page's path and query without the marker; the bare path when that is not a safe next path, and the
    home page when the bare path is not one either. Every answer has passed `safe_next_path`."""
    args = [(key, value) for key, value in request.args.items(multi=True) if key != HOP_MARKER]
    page = f"{request.path}?{urlencode(args)}" if args else request.path
    return safe_next_path(page) or safe_next_path(request.path) or "/"


def _with_marker(page: str, count: int) -> str:
    return f"{page}{'&' if '?' in page else '?'}{HOP_MARKER}={count}"


def _signed_in() -> bool:
    try:
        verify_jwt_in_request()
    except (JWTExtendedException, PyJWTError):
        return False
    g.page_user_id = int(get_jwt_identity())
    return True


def _renewed_access_token(refresh_cookie: str) -> Optional[str]:
    """A new access token from the refresh cookie through the one refresh rule, under its storefront rule, or
    None when it refuses (revoked, expired, an inactive account, an unknown session, not a refresh token).

    The browser must still hold the refresh cookie's readable half, `csrf_refresh_token`, with the value the
    token carries. base.js's Log out deletes that half in every case, and has done so since 2025-12, even when
    its server call failed and the httpOnly refresh cookie survived. A refresh cookie without its readable half
    is therefore a login its customer ended, and renewing it would hand the next person at that browser the
    previous customer's cabinet. `/auth/refresh-cookie` asks for the same value in X-CSRF-TOKEN.

    A staff login raises `NotACustomerLogin`: the admin UI signs in through the same `/auth/login`, on the same
    cookie domain, and the storefront neither renews that login nor ends it."""
    csrf_value = request.cookies.get(current_app.config["JWT_REFRESH_CSRF_COOKIE_NAME"])
    if not csrf_value:
        return None
    try:
        return TokenService().refresh_access_token(refresh_cookie, csrf_value=csrf_value, storefront=True)[
            "access_token"
        ]
    except NotACustomerLogin:
        raise
    except Exception:  # noqa: BLE001 - any other refusal of the one refresh rule (it logs why) means signed out
        return None


def _guarded(view, signed_out, *args, **kwargs):
    if request.method not in _PAGE_METHODS:
        verify_jwt_in_request()  # as @jwt_required(): the JWT error handlers answer
        g.page_user_id = int(get_jwt_identity())
        return view(*args, **kwargs)

    if _signed_in():
        return view(*args, **kwargs)

    count = _marker_count()
    page = _this_page()
    access_cookie = request.cookies.get(current_app.config["JWT_ACCESS_COOKIE_NAME"])
    refresh_cookie = request.cookies.get(current_app.config["JWT_REFRESH_COOKIE_NAME"])
    clear_cookies = False

    if refresh_cookie and count < _SPENT:
        try:
            access_token = _renewed_access_token(refresh_cookie)
        except NotACustomerLogin:
            pass  # signed out here, and the staff login's cookies are left as they are
        else:
            if access_token:
                response = redirect(_with_marker(page, count + 1))
                set_access_cookies(response, access_token)
                return response
            clear_cookies = True
    elif (
        not access_cookie
        and not refresh_cookie
        and count == 0
        and request.headers.get("Sec-Fetch-Site") in _MAY_HAVE_DROPPED_COOKIES
    ):
        return render_same_site_hop(_with_marker(page, 1))

    if signed_out == "render":
        g.page_user_id = None
        response = make_response(view(*args, **kwargs))
    else:
        response = redirect(url_for("frontend.login", next=page))
    if clear_cookies:
        unset_jwt_cookies(response)
    return response


def customer_page_required(view=None, *, signed_out: Literal["login", "render"] = "login"):
    """The one handler for a customer page route (bare `@customer_page_required`, or called).

    `signed_out="login"` sends a signed-out visitor to the login page and back; `"render"` runs the view
    with `page_user_id()` None (the payment return pages, which work without a web session)."""
    if signed_out not in ("login", "render"):
        raise ValueError(f"signed_out must be 'login' or 'render', not {signed_out!r}")
    if view is None:
        return partial(customer_page_required, signed_out=signed_out)

    @wraps(view)
    def wrapper(*args, **kwargs):
        return _guarded(view, signed_out, *args, **kwargs)

    wrapper.customer_page = True
    return wrapper
