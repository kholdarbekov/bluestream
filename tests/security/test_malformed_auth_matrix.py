"""Security matrices for malformed authorization handling."""

import pytest

from business_app import db as _db
from tests.integration.api_surface_cases import API_SURFACE_CASES


PROTECTED_PREFIXES = (
    "/api/v1/admin",
    "/api/v1/orders",
    "/api/v1/addresses",
    "/api/v1/cart",
    "/api/v1/payments",
    "/api/v1/subscriptions",
    "/api/v1/loyalty",
    "/api/v1/notifications",
    "/api/v1/session",
    "/api/v1/staff",
    "/api/v1/analytics",
)

PROTECTED_ROUTE_CASES = [
    (method, path, endpoint)
    for method, path, endpoint in API_SURFACE_CASES
    if path.startswith(PROTECTED_PREFIXES)
]

MALFORMED_AUTH_HEADERS = [
    pytest.param("Bearer invalid.token.value", id="invalid-bearer-jwt-shape"),
    pytest.param("Bearer not-a-jwt", id="invalid-bearer-segments"),
    pytest.param("Basic Zm9vOmJhcg==", id="wrong-auth-scheme"),
]

STRICT_AUTH_ROUTE_CASES = [
    ("GET", "/api/v1/orders/"),
    ("GET", "/api/v1/addresses/"),
    ("GET", "/api/v1/cart/"),
    ("GET", "/api/v1/payments/"),
    ("GET", "/api/v1/subscriptions/"),
    ("GET", "/api/v1/notifications/"),
    ("GET", "/api/v1/auth/profile"),
    ("GET", "/api/v1/auth/permissions"),
    ("GET", "/api/v1/auth/sessions"),
    ("POST", "/api/v1/auth/logout"),
    ("GET", "/api/v1/session/sessions"),
    ("GET", "/api/v1/loyalty/account"),
    ("GET", "/api/v1/staff/delivery/active"),
    ("GET", "/api/v1/analytics/dashboard"),
    ("GET", "/api/v1/admin/dashboard"),
]

STRICT_MALFORMED_BEARER_HEADERS = [
    pytest.param("Bearer invalid.token.value", id="strict-invalid-bearer"),
    pytest.param("Bearer short", id="strict-short-bearer"),
]

STRICT_UNAUTHORIZED_STATUSES = {401, 403, 422}


def _call_case(client, method: str, path: str, headers: dict):
    if method == "GET":
        return client.get(path, headers=headers)
    if method == "POST":
        return client.post(path, json={}, headers=headers)
    if method == "PUT":
        return client.put(path, json={}, headers=headers)
    if method == "PATCH":
        return client.patch(path, json={}, headers=headers)
    if method == "DELETE":
        return client.delete(path, headers=headers)
    raise AssertionError(f"Unsupported method: {method}")


@pytest.fixture(scope="module")
def matrix_db(app):
    """Create schema once for malformed auth matrix checks."""
    with app.app_context():
        _db.create_all()
        yield _db
        _db.session.remove()
        _db.drop_all()


@pytest.mark.security
@pytest.mark.integration
@pytest.mark.parametrize("method,path,endpoint", PROTECTED_ROUTE_CASES)
@pytest.mark.parametrize("auth_header", MALFORMED_AUTH_HEADERS)
def test_protected_routes_handle_malformed_auth_without_500(app, matrix_db, method, path, endpoint, auth_header):
    """
    Broad guardrail: malformed Authorization values must not cause 500s.
    """
    client = app.test_client(use_cookies=False)
    headers = {"Content-Type": "application/json", "Authorization": auth_header}
    response = _call_case(client, method, path, headers=headers)

    body_text = response.get_data(as_text=True).lower()
    assert (
        response.status_code < 500
    ), f"{method} {path} ({endpoint}) returned {response.status_code} for auth={auth_header}: {body_text[:400]}"
    assert "traceback" not in body_text
    assert "stack trace" not in body_text


@pytest.mark.security
@pytest.mark.integration
@pytest.mark.parametrize("method,path", STRICT_AUTH_ROUTE_CASES)
@pytest.mark.parametrize("auth_header", STRICT_MALFORMED_BEARER_HEADERS)
def test_critical_protected_routes_reject_malformed_bearer_tokens(app, matrix_db, method, path, auth_header):
    """
    Strict check: key protected routes must reject malformed bearer tokens.
    """
    client = app.test_client(use_cookies=False)
    headers = {"Content-Type": "application/json", "Authorization": auth_header}
    response = _call_case(client, method, path, headers=headers)

    assert (
        response.status_code in STRICT_UNAUTHORIZED_STATUSES
    ), f"{method} {path} returned {response.status_code} for malformed auth header"
