"""Broad integration smoke checks across all registered API endpoints."""

import pytest

from business_app import db as _db
from tests.integration.api_surface_cases import API_SURFACE_CASES


@pytest.fixture(scope="module")
def smoke_db(app):
    """Create schema once for the API surface smoke matrix."""
    with app.app_context():
        _db.create_all()
        yield _db
        _db.session.remove()
        _db.drop_all()


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.parametrize("method,path,endpoint", API_SURFACE_CASES)
def test_api_surface_routes_do_not_500(app, smoke_db, method, path, endpoint):
    """
    Route-level reliability check.

    For each registered /api/v1 route+method pair, send a minimal request and
    verify we do not return a server error.
    """
    headers = {"Content-Type": "application/json"}
    client = app.test_client(use_cookies=False)

    if method == "GET":
        response = client.get(path, headers=headers)
    elif method == "POST":
        response = client.post(path, json={}, headers=headers)
    elif method == "PUT":
        response = client.put(path, json={}, headers=headers)
    elif method == "PATCH":
        response = client.patch(path, json={}, headers=headers)
    elif method == "DELETE":
        response = client.delete(path, headers=headers)
    else:
        pytest.fail(f"Unsupported method in API surface matrix: {method}")

    assert (
        response.status_code < 500
    ), f"{method} {path} ({endpoint}) returned {response.status_code}: {response.get_data(as_text=True)[:500]}"
