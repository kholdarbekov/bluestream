"""Integration matrix tests for noisy query input handling across GET routes."""

import pytest

from business_app import db as _db
from tests.integration.api_surface_cases import API_SURFACE_CASES


GET_ROUTE_CASES = [(path, endpoint) for method, path, endpoint in API_SURFACE_CASES if method == "GET"]

NOISY_QUERY_CASES = [
    pytest.param(
        {
            "q": "<script>alert(1)</script>",
            "search": "'; DROP TABLE users; --",
            "page": "-1",
            "per_page": "10000",
        },
        id="xss-sql-like-query",
    ),
    pytest.param(
        {
            "q": "\x00\x01\x7f",
            "sort_by": "__proto__",
            "order": "DESC",
            "include": "../../../etc/passwd",
        },
        id="control-chars-and-path-traversal",
    ),
    pytest.param(
        {
            "search": "A" * 2048,
            "page": "999999999",
            "per_page": "999999999",
        },
        id="oversized-pagination-values",
    ),
]


@pytest.fixture(scope="module")
def matrix_db(app):
    """Create schema once for high-volume API query matrix checks."""
    with app.app_context():
        _db.create_all()
        yield _db
        _db.session.remove()
        _db.drop_all()


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.parametrize("path,endpoint", GET_ROUTE_CASES)
@pytest.mark.parametrize("query", NOISY_QUERY_CASES)
def test_get_routes_handle_noisy_queries_without_500(app, matrix_db, path, endpoint, query):
    """
    Ensure malformed, oversized, and hostile query params do not trigger 500s.
    """
    client = app.test_client(use_cookies=False)
    response = client.get(path, query_string=query, headers={"Accept": "application/json"})

    body_text = response.get_data(as_text=True).lower()
    assert (
        response.status_code < 500
    ), f"GET {path} ({endpoint}) returned {response.status_code} for query={query}: {body_text[:400]}"
    assert "traceback" not in body_text
    assert "stack trace" not in body_text
