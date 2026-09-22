"""Characterization tests for `POST /api/v1/admin/staff/operators`.

`StaffService.create_operator` had NO test of its own: the 175 tests matching
`-k operator` all build operator Users by hand and never call it. That made the
extraction of its find-or-create body into
`StaffService._create_or_attach_staff_user` (shared with create_sales_agent /
update_sales_agent) an unguarded refactor.

These pin the behaviours that moved into the helper -- phone/email uniqueness,
full-name splitting, status validation, the new-user defaults, and the
"existing users keep users.role" rule -- through the endpoint the admin UI
actually posts to, so a future edit to the helper for the sales-agent side
cannot silently change what operator creation does.
"""

from flask_jwt_extended import create_access_token

from business_app.models.user import User
from shared.enums import UserRole, UserStatus, UserType


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _post(client, app, admin_user, payload):
    return client.post(
        "/api/v1/admin/staff/operators",
        json=payload,
        headers=_auth_headers(app, admin_user.id),
    )


def test_create_operator_builds_a_new_staff_user_with_defaults(client, app, db, admin_user):
    response = _post(
        client,
        app,
        admin_user,
        {"full_name": "Nodir Karimov", "phone": "+998901234580", "email": "nodir@example.com"},
    )

    assert response.status_code == 201, response.get_json()
    user = User.query.filter_by(phone="+998901234580").one()
    assert user.first_name == "Nodir" and user.last_name == "Karimov"
    assert user.email == "nodir@example.com"
    assert user.role == UserRole.OPERATOR
    assert user.user_type == UserType.STAFF
    assert user.status == UserStatus.ACTIVE
    assert user.staff_roles == ["operator"]
    assert user.preferred_language == "uz"
    assert user.registration_source == "admin"


def test_create_operator_on_an_existing_user_keeps_their_role_column(client, app, db, admin_user, sample_user):
    original_role = sample_user.role
    assert original_role != UserRole.OPERATOR

    response = _post(client, app, admin_user, {"user_id": sample_user.id})

    assert response.status_code == 201, response.get_json()
    fresh = User.query.get(sample_user.id)
    assert fresh.role == original_role  # only brand-new users get users.role stamped
    assert fresh.staff_roles == ["operator"]
    assert fresh.user_type == UserType.STAFF


def test_create_operator_honours_an_explicit_status(client, app, db, admin_user):
    response = _post(client, app, admin_user, {"phone": "+998901234581", "status": "inactive"})

    assert response.status_code == 201, response.get_json()
    assert User.query.filter_by(phone="+998901234581").one().status == UserStatus.INACTIVE


def test_create_operator_rejects_an_unknown_status(client, app, db, admin_user):
    """400, and no operator role is granted.

    Pre-existing quirk held still here rather than fixed: the new User is
    already flushed by the time the status is validated, so the row is visible
    in the session until request teardown rolls it back. It is never committed
    and never gets `staff_roles`, which is what this asserts. The same shape
    applies to `create_sales_agent` and its district validation.
    """
    response = _post(client, app, admin_user, {"phone": "+998901234582", "status": "retired"})

    assert response.status_code == 400
    assert "Invalid status value" in response.get_json()["message"]
    leftover = User.query.filter_by(phone="+998901234582").first()
    assert leftover is None or not leftover.staff_roles


def test_create_operator_rejects_a_phone_owned_by_another_user(client, app, db, admin_user, sample_user):
    response = _post(client, app, admin_user, {"user_id": sample_user.id, "phone": admin_user.phone})

    assert response.status_code == 400
    assert "Phone is already used by another user" in response.get_json()["message"]
    assert User.query.get(sample_user.id).phone != admin_user.phone


def test_create_operator_rejects_an_email_owned_by_another_user(client, app, db, admin_user):
    response = _post(client, app, admin_user, {"phone": "+998901234583", "email": admin_user.email})

    assert response.status_code == 400
    assert "Email is already used by another user" in response.get_json()["message"]


def test_create_operator_requires_a_phone_when_no_user_id_is_given(client, app, db, admin_user):
    response = _post(client, app, admin_user, {"full_name": "No Contact"})

    assert response.status_code == 400
    assert "phone is required when user_id is not provided" in response.get_json()["message"]


def test_create_operator_on_an_existing_phone_keeps_that_users_email(client, app, db, admin_user):
    """The second door onto `_create_or_attach_staff_user`, pinned like the sales-agent one.

    Operators.js used to send `email: null` for an untouched input, and this route passes
    `request.get_json()` through raw, so the null reached the helper and blanked the email of
    whoever already owned the typed phone. That is how an admin lost access to the panel:
    `login_user` resolves the account by email before it ever checks the password.
    """
    response = _post(client, app, admin_user, {"phone": admin_user.phone, "email": None})

    assert response.status_code == 201, response.get_data(as_text=True)
    db.session.expire_all()
    assert User.query.get(admin_user.id).email == "admin@example.com"
