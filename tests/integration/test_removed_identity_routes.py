"""Eight identity routes that no caller used were deleted; each must now be unreachable."""

import pytest

REMOVED_ROUTES = [
    ("POST", "/api/v1/auth/link-telegram"),
    ("POST", "/api/v1/auth/link-web-account"),
    ("POST", "/api/v1/auth/sync-profile"),
    ("POST", "/api/v1/auth/suggest-auto-link"),
    ("POST", "/api/v1/auth/generate-telegram-auth"),
    ("POST", "/api/v1/auth/generate-web-auth"),
    ("POST", "/api/v1/auth/verify-telegram-auth/K4M0LA77"),
    ("GET", "/api/v1/auth/verify-web-auth/sometoken"),
]


@pytest.mark.parametrize(("method", "path"), REMOVED_ROUTES)
def test_removed_identity_route_is_not_served(client, method, path):
    response = client.open(path, method=method, json={} if method == "POST" else None)

    # Exactly 404. A 405 would mean the path is served again under another
    # method, which is a re-added route, not a removed one.
    assert response.status_code == 404, (method, path, response.status_code)
