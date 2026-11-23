"""Staff-side address writes for a customer (admin UI), with the duplicate guard and the one pin-change path."""

from typing import Any, Dict

from business_app import db
from business_app.models.user import User, UserAddress
from business_app.services.address_guard_service import assert_not_duplicate
from business_app.services.address_pin_service import AddressPinService
from business_app.utils.exceptions import NotFoundError, ValidationError
from business_app.utils.geo_validation import ensure_within_delivery_zone


class AdminUserAddressService:
    @staticmethod
    def create(user_id: int, data: Dict[str, Any], *, actor_id: int, confirm_duplicate: bool) -> UserAddress:
        """Create an address for a customer. A point within the duplicate radius of one of their saved addresses is
        refused with a 409 unless `confirm_duplicate`; there is no Move offer on this path. The first address of a
        user becomes their default."""
        if not db.session.get(User, user_id):
            raise NotFoundError("User not found")

        full_address = (data.get("full_address") or "").strip()
        if not full_address:
            raise ValidationError("Full address is required")

        latitude, longitude = data.get("latitude"), data.get("longitude")
        ensure_within_delivery_zone(latitude, longitude)
        assert_not_duplicate(
            user_id,
            latitude,
            longitude,
            apartment_number=data.get("apartment_number"),
            confirmed=confirm_duplicate,
        )

        is_default = data.get("is_default", False)
        if is_default:
            UserAddress.query.filter_by(user_id=user_id, is_default=True).update({"is_default": False})
        if UserAddress.query.filter_by(user_id=user_id).count() == 0:
            is_default = True

        address = UserAddress(
            user_id=user_id,
            title=data.get("title"),
            full_address=full_address,
            street_address=data.get("street_address"),
            city=data.get("city", "Tashkent"),
            district=data.get("district"),
            postal_code=data.get("postal_code"),
            country=data.get("country", "Uzbekistan"),
            latitude=latitude,
            longitude=longitude,
            is_default=is_default,
            is_business=data.get("is_business", False),
            delivery_instructions=data.get("delivery_instructions"),
            landmark=data.get("landmark"),
            floor_number=data.get("floor_number"),
            apartment_number=data.get("apartment_number"),
        )
        db.session.add(address)
        db.session.commit()
        return address

    @staticmethod
    def update(
        user_id: int, address_id: int, data: Dict[str, Any], *, actor_id: int, confirm_duplicate: bool
    ) -> UserAddress:
        """Edit a customer's address. A change of the pin goes through the one pin-change path (zone, the duplicate
        question, the open orders redirected and the driver told; the customer-only pin rules do not apply to
        staff). Every other edit is the plain write, which never carries a point: a null one clears nothing."""
        if not db.session.get(User, user_id):
            raise NotFoundError("User not found")
        address = UserAddress.query.filter_by(id=address_id, user_id=user_id).first()
        if not address:
            raise NotFoundError("Address not found")

        AddressPinService.edit(
            address,
            data,
            actor=db.session.get(User, actor_id),
            customer_rules=False,
            confirm_duplicate=confirm_duplicate,
        )

        if data.get("is_default"):
            UserAddress.query.filter_by(user_id=user_id, is_default=True).update({"is_default": False})
            address.is_default = True

        db.session.commit()
        return address
