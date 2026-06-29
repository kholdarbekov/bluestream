"""Inactive Delivery Persons must lose staff-bot + staff-API access while
keeping customer-bot access. Gate keys off DeliveryPerson.is_active, never
User.status."""
import importlib.util
from pathlib import Path

import pytest
from flask_jwt_extended import create_access_token, create_refresh_token

from business_app.services.staff_service import StaffService
from business_app.models.delivery import DeliveryPerson
from business_app.utils.exceptions import ForbiddenError
from shared.enums import UserStatus


def _make_delivery_person(db, user, *, is_active: bool) -> DeliveryPerson:
    dp = DeliveryPerson(
        user_id=user.id,
        full_name="Test Driver",
        phone="+998900000001",
        is_active=is_active,
    )
    db.session.add(dp)
    db.session.commit()
    return dp


@pytest.mark.unit
class TestAssertDeliveryPersonActive:
    def test_raises_for_inactive_delivery_person(self, db, delivery_driver):
        _make_delivery_person(db, delivery_driver, is_active=False)

        with pytest.raises(ForbiddenError) as exc_info:
            StaffService.assert_delivery_person_active(delivery_driver)

        assert exc_info.value.error_code == "STAFF_ACCOUNT_DEACTIVATED"

    def test_noop_for_active_delivery_person(self, db, delivery_driver):
        _make_delivery_person(db, delivery_driver, is_active=True)

        assert StaffService.assert_delivery_person_active(delivery_driver) is None

    def test_noop_for_staff_without_delivery_person(self, db, delivery_driver):
        # Operators have a staff role but no DeliveryPerson row.
        assert StaffService.assert_delivery_person_active(delivery_driver) is None

    def test_does_not_touch_user_status(self, db, delivery_driver):
        # Customer-bot access must remain intact: the gate never mutates status.
        _make_delivery_person(db, delivery_driver, is_active=False)

        with pytest.raises(ForbiddenError):
            StaffService.assert_delivery_person_active(delivery_driver)

        assert delivery_driver.status == UserStatus.ACTIVE


@pytest.mark.unit
class TestAuthenticateAndLinkStaffDeactivation:
    def test_login_rejected_for_inactive_delivery_person(self, db, delivery_driver):
        delivery_driver.telegram_id = "990000001"
        db.session.commit()
        _make_delivery_person(db, delivery_driver, is_active=False)

        with pytest.raises(ForbiddenError) as exc_info:
            StaffService.authenticate_and_link_staff("990000001")

        assert exc_info.value.error_code == "STAFF_ACCOUNT_DEACTIVATED"

    def test_login_succeeds_for_active_delivery_person(self, app, db, delivery_driver):
        delivery_driver.telegram_id = "990000002"
        db.session.commit()
        _make_delivery_person(db, delivery_driver, is_active=True)

        with app.test_request_context():
            result = StaffService.authenticate_and_link_staff("990000002")

        assert result["access_token"]
        assert "delivery_driver" in result["user"]["staff_roles"]


STAFF_ENDPOINT = "/api/v1/staff/customers/with-open-cod"


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.mark.unit
class TestRequireStaffRolesDeactivation:
    def test_inactive_driver_is_blocked_on_staff_api(self, app, client, db, delivery_driver):
        _make_delivery_person(db, delivery_driver, is_active=False)

        response = client.get(STAFF_ENDPOINT, headers=_auth_headers(app, delivery_driver.id))

        assert response.status_code == 403
        assert "STAFF_ACCOUNT_DEACTIVATED" in response.get_data(as_text=True)

    def test_active_driver_is_allowed_on_staff_api(self, app, client, db, delivery_driver):
        _make_delivery_person(db, delivery_driver, is_active=True)

        response = client.get(STAFF_ENDPOINT, headers=_auth_headers(app, delivery_driver.id))

        assert response.status_code == 200


def _load_staff_translations():
    path = Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py"
    spec = importlib.util.spec_from_file_location("seed_staff_translations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.STAFF_TRANSLATIONS


@pytest.mark.unit
class TestDeactivatedTranslationsSeeded:
    @pytest.mark.parametrize(
        "key",
        ["staff.account_deactivated", "staff.error.api.account_deactivated"],
    )
    def test_key_present_in_all_languages(self, key):
        translations = _load_staff_translations()
        assert key in translations
        for lang in ("en", "uz", "ru"):
            assert translations[key].get(lang)


STAFF_REFRESH_ENDPOINT = "/api/v1/staff/auth/refresh"


@pytest.mark.unit
class TestStaffRefreshDeactivation:
    def test_inactive_driver_refresh_is_blocked(self, app, client, db, delivery_driver):
        _make_delivery_person(db, delivery_driver, is_active=False)
        with app.app_context():
            refresh = create_refresh_token(identity=str(delivery_driver.id))

        response = client.post(
            STAFF_REFRESH_ENDPOINT,
            headers={"Authorization": f"Bearer {refresh}", "Content-Type": "application/json"},
        )

        assert response.status_code == 403
        assert "STAFF_ACCOUNT_DEACTIVATED" in response.get_data(as_text=True)

    def test_active_driver_refresh_not_blocked_by_gate(self, app, client, db, delivery_driver):
        # The gate must not fire for an active driver. Whatever else the refresh
        # flow does (e.g. session validation) is out of scope — assert ONLY that
        # our deactivation gate did not block it.
        _make_delivery_person(db, delivery_driver, is_active=True)
        with app.app_context():
            refresh = create_refresh_token(identity=str(delivery_driver.id))

        response = client.post(
            STAFF_REFRESH_ENDPOINT,
            headers={"Authorization": f"Bearer {refresh}", "Content-Type": "application/json"},
        )

        assert "STAFF_ACCOUNT_DEACTIVATED" not in response.get_data(as_text=True)


@pytest.mark.unit
class TestAssertDeliveryPersonActiveByUserId:
    def test_raises_for_inactive_delivery_person(self, db, delivery_driver):
        _make_delivery_person(db, delivery_driver, is_active=False)

        with pytest.raises(ForbiddenError) as exc_info:
            StaffService.assert_delivery_person_active_by_user_id(delivery_driver.id)

        assert exc_info.value.error_code == "STAFF_ACCOUNT_DEACTIVATED"

    def test_noop_for_active_delivery_person(self, db, delivery_driver):
        _make_delivery_person(db, delivery_driver, is_active=True)

        assert StaffService.assert_delivery_person_active_by_user_id(delivery_driver.id) is None

    def test_noop_for_unknown_user_id(self, db):
        assert StaffService.assert_delivery_person_active_by_user_id(99999999) is None
