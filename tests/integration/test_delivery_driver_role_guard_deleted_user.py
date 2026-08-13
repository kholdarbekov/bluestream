"""Fix 2 (final fix wave, route-ux-phase1): every driver-only handler in
``business_app/api/delivery.py`` used to dereference ``user.role`` BEFORE
checking ``if not user`` — e.g. (pre-fix, around line 637):

    user = User.query.get(current_user_id)
    role_value = user.role.value if hasattr(user.role, "value") else user.role
    if not user or role_value != UserRole.DELIVERY_DRIVER.value:
        return jsonify({"error": ...}), 403

If ``user`` is ``None``, ``user.role`` raises ``AttributeError`` INSIDE the
``try`` block, which the handler's own broad ``except Exception`` swallows
into a generic 500 — instead of the 403 the missing/wrong-role guard is
supposed to produce.

Reachability note: a plain "JWT for an id that was never created / already
deleted" does NOT reach this code. This app registers a global
`user_lookup_loader` (`business_app/__init__.py`) that flask-jwt-extended's
`verify_jwt_in_request()` invokes unconditionally, BEFORE the view function
runs — and that loader 401s first ("User Not Found") when its own
`User.query.get(...)` finds nothing. Verified empirically against both the
pre-fix and post-fix code: both returned 401 for a nonexistent-user JWT,
never 500 or 403 — that reproduction can't tell the two apart.

The line this test actually exercises is the handler's OWN, independent
`User.query.get(current_user_id)` call a few lines further down (see
`grep -n "User\\.query" business_app/api/delivery.py` — exactly one call per
handler, always after the JWT layer has already let the request through).
To reach it deterministically without fighting the exact call count of
every JWT-adjacent lookup on the request path (the global loader, plus this
app's language-detection `before_request` hook, which also resolves the
user), this test monkeypatches the `User` name as *delivery.py itself* sees
it — leaving the real `business_app.models.user.User` (and therefore the
JWT layer, which re-imports it fresh) untouched and fully able to find the
real, committed driver row — so only the route handler's own lookup is
made to come up empty, exactly as it would if the row were deleted in the
narrow window between the JWT layer's check and the handler's re-query.
"""

import pytest
from flask_jwt_extended import create_access_token

import business_app.api.delivery as delivery_module
from business_app.models.user import User
from shared.enums import UserRole, UserType


class _AlwaysEmptyQuery:
    def get(self, _id):
        return None


class _UserRowGoneByHandlersOwnQuery:
    """Stands in for the `User` name as `business_app/api/delivery.py` sees
    it: `.query.get(...)` always misses. The real `business_app.models.user.
    User` is untouched, so everything upstream of the view function (the
    global JWT `user_lookup_loader`, the language-detection `before_request`
    hook) still resolves the real, committed user and lets the request
    through — only the handler's own re-query comes up empty."""

    query = _AlwaysEmptyQuery()


def _driver_and_token(app, db):
    user = User(
        email="race-driver@example.com",
        phone="+998900000199",
        password_hash="x",
        first_name="Race",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    return user, {"Authorization": f"Bearer {token}"}


@pytest.mark.integration
@pytest.mark.delivery
class TestDriverRoleGuardRaceWithGlobalJwtUserLookup:
    @pytest.mark.parametrize(
        "method,path,json_body",
        [
            ("get", "/api/v1/delivery/driver/assignments", None),
            ("post", "/api/v1/delivery/driver/update-location", {"lat": 41.3, "lng": 69.2}),
            ("post", "/api/v1/delivery/driver/start-delivery/1", None),
            ("post", "/api/v1/delivery/driver/arrive/1", None),
            ("post", "/api/v1/delivery/driver/complete/1", {"notes": "n/a"}),
            ("post", "/api/v1/delivery/driver/report-issue/1", {"issue_type": "delay"}),
            ("post", "/api/v1/delivery/driver/route-optimization", None),
            ("post", "/api/v1/delivery/upload-photo", None),
        ],
        ids=[
            "get_driver_assignments",
            "update_driver_location",
            "start_delivery",
            "mark_arrived",
            "complete_delivery",
            "report_delivery_issue",
            "request_route_optimization",
            "upload_delivery_photo",
        ],
    )
    def test_user_row_gone_by_the_handlers_own_lookup_returns_403_not_500(
        self, client, app, db, monkeypatch, method, path, json_body
    ):
        _user, headers = _driver_and_token(app, db)
        monkeypatch.setattr(delivery_module, "User", _UserRowGoneByHandlersOwnQuery)

        response = getattr(client, method)(path, headers=headers, json=json_body)

        assert response.status_code == 403
        assert response.get_json()["error"]
