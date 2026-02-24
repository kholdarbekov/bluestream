"""Regression matrix for boundary ID values on ID-based API routes."""

import re

import pytest

from business_app import db as _db
from tests.integration.api_surface_cases import API_SURFACE_CASES


ID_ROUTE_CASES = [(method, path, endpoint) for method, path, endpoint in API_SURFACE_CASES if "/1" in path]

BOUNDARY_ID_VALUES = [
    pytest.param("0", id="id-zero"),
    pytest.param("-1", id="id-negative-one"),
    pytest.param("999999999", id="id-large-positive"),
]


def _replace_id_segments(path: str, id_value: str) -> str:
    """Replace all '/1' path placeholders with a boundary value."""
    return re.sub(r"/1(?=/|$)", f"/{id_value}", path)


def _call_case(client, method: str, path: str):
    headers = {"Content-Type": "application/json"}
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
    """Create schema once for path-boundary regression matrix checks."""
    with app.app_context():
        _db.create_all()
        yield _db
        _db.session.remove()
        _db.drop_all()


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.parametrize("method,path,endpoint", ID_ROUTE_CASES)
@pytest.mark.parametrize("id_value", BOUNDARY_ID_VALUES)
def test_id_routes_handle_boundary_values_without_500(app, matrix_db, method, path, endpoint, id_value):
    """
    Ensure ID boundary values do not trigger server errors on ID-based routes.
    """
    client = app.test_client(use_cookies=False)
    mutated_path = _replace_id_segments(path, id_value)

    response = _call_case(client, method, mutated_path)

    body_text = response.get_data(as_text=True).lower()
    assert (
        response.status_code < 500
    ), f"{method} {mutated_path} ({endpoint}) returned {response.status_code}: {body_text[:400]}"
    assert "traceback" not in body_text
    assert "stack trace" not in body_text
