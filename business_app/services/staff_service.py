"""
Staff service for the Water Business Platform.
Handles staff bot operations: authentication, delivery management,
operator actions, and staff analytics.
"""

import json
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List, Tuple
from flask import current_app
import redis
from sqlalchemy import and_, or_, func
from sqlalchemy.orm import joinedload
from decimal import Decimal

from business_app.models.user import User, UserAddress
from business_app.models.order import Order, OrderItem
from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryStatusHistory
from business_app.models.staff import StaffActivityLog
from business_app.services.cod_collect_ceiling import (
    collectible_cod_total,
    place_widening_applies,
    resolve_collect_scope,
)
from business_app.utils.exceptions import ValidationError, NotFoundError, ForbiddenError, ConflictError
from business_app.utils.geo_validation import ensure_within_delivery_zone
from business_app.utils.payment_projection import (
    is_ledger_receivable,
    open_receivable_amount,
)
from business_app.utils.state_validators import (
    assert_order_address_for_status,
    assert_order_creator_for_source,
    assert_unassigned_for_pool_status,
)
from shared.enums import UserRole, UserStatus, OrderStatus, DeliveryStatus, PaymentMethod, UserType
from shared.staff_constants import (
    STAFF_BOT_ROLES,
    STAFF_ACTIONS,
    DELIVERY_STATUS_TRANSITIONS,
    DELIVERY_TO_ORDER_STATUS_SYNC,
    FAILED_DELIVERY_REASONS,
)
from shared.redis_keyspace import RedisKeyspace
from business_app import db


class StaffService:
    """Service for staff bot operations"""

    ACTIVE_DELIVERY_STATUSES = (
        DeliveryStatus.ASSIGNED,
        DeliveryStatus.PICKED_UP,
        DeliveryStatus.IN_TRANSIT,
        DeliveryStatus.ARRIVED,
    )

    # Statuses an unassigned delivery may be in while sitting in the pool waiting
    # to be claimed. Only these can be accepted by a driver — accepting anything
    # else (in-progress, delivered, failed, cancelled, returned) is invalid and
    # must go through the admin/operator re-dispatch flow instead.
    CLAIMABLE_DELIVERY_STATUSES = (
        DeliveryStatus.SCHEDULED,
        DeliveryStatus.PENDING,
    )

    @staticmethod
    def _normalize_role_value(value: Any) -> Optional[str]:
        """Normalize Enum/string role values to canonical string."""
        if value is None:
            return None
        if hasattr(value, "value"):
            return value.value
        if isinstance(value, str):
            return value
        return str(value)

    @staticmethod
    def _normalize_staff_roles_input(raw_roles: Any) -> List[str]:
        """Normalize arbitrary role payloads into valid unique staff roles."""
        if raw_roles is None:
            return []
        if isinstance(raw_roles, str):
            try:
                raw_roles = json.loads(raw_roles)
            except (TypeError, ValueError):
                raw_roles = [raw_roles]
        if not isinstance(raw_roles, list):
            return []

        normalized: List[str] = []
        for role in raw_roles:
            if isinstance(role, str) and role in STAFF_BOT_ROLES and role not in normalized:
                normalized.append(role)
        return normalized

    @staticmethod
    def _set_name_from_full_name(user: User, full_name: Optional[str]) -> None:
        """Update first/last names from a full name string."""
        if not full_name or not isinstance(full_name, str):
            return
        parts = [p for p in full_name.strip().split() if p]
        if not parts:
            return
        user.first_name = parts[0]
        user.last_name = " ".join(parts[1:]) if len(parts) > 1 else None

    @staticmethod
    def _extract_staff_roles(user: User) -> List[str]:
        """Build normalized staff roles list for staff bot auth checks."""
        normalized = StaffService._normalize_staff_roles_input(user.staff_roles)
        role_value = StaffService._normalize_role_value(user.role)
        if role_value in STAFF_BOT_ROLES and role_value not in normalized:
            normalized.append(role_value)

        return normalized

    @staticmethod
    def assert_delivery_person_active(user: User) -> None:
        """Raise if the user is a delivery person an admin has deactivated.

        Keys off ``DeliveryPerson.is_active`` — the flag the admin "Delivery
        Persons" page toggles. No-op for staff with no delivery-person record
        (e.g. operators), so only drivers are gated by this control. Never reads
        or writes ``User.status``, so a deactivated driver keeps full customer-bot
        access.
        """
        delivery_person = DeliveryPerson.query.filter_by(user_id=user.id).first()
        if delivery_person is not None and not delivery_person.is_active:
            raise ForbiddenError(
                "Your delivery account has been deactivated",
                error_code="STAFF_ACCOUNT_DEACTIVATED",
            )

    @staticmethod
    def assert_delivery_person_active_by_user_id(user_id) -> None:
        """Resolve a user by id and assert their delivery-person account is active.

        For callers that only hold a JWT identity (e.g. the staff refresh
        endpoint) and should not reach into the model layer directly. No-op if
        the user does not exist (the caller's own auth handles that case).
        """
        user = User.query.get(user_id)
        if user is not None:
            StaffService.assert_delivery_person_active(user)

    @staticmethod
    def get_active_delivery_statuses() -> Tuple[DeliveryStatus, ...]:
        """Return statuses counted as active driver workload."""
        return StaffService.ACTIVE_DELIVERY_STATUSES

    @staticmethod
    def get_cod_collection_projection(order: Optional[Order]) -> Dict[str, float]:
        """Compute the cash-collection projection for driver-facing workflows.

        RAIL-AGNOSTIC since 2026-08-08 (plan 2026-08-08-open-receivable-ssot).

        This used to early-return the FULL order total for any non-CASH order.
        That was harmless only because every bot surface hid the value behind
        `payment_method == 'cash'`. Now that the bot shows it, returning the
        total would make a driver collect the whole order from a customer who
        had already paid most of it by card — strictly worse than the invisible
        receivable it replaces. `open_receivable_amount` is the single decision
        and there is no per-rail branch left.
        """
        total_amount = Decimal(str(getattr(order, "total_amount", 0) or 0))
        if not order:
            return {
                "cod_reserved_prepayment_amount": 0.0,
                "expected_cash_to_collect": float(total_amount),
            }

        payment = getattr(order, "payment", None)
        if payment is None:
            # No payment row yet — the whole order is due. Same fallback the
            # CASH arm has always used.
            outstanding_amount = total_amount
        else:
            outstanding_amount = open_receivable_amount(payment)
        provider_data = dict(getattr(payment, "provider_data", {}) or {})
        reserved_amount = Decimal(str(provider_data.get("cod_prepayment_reserved_amount") or 0))

        if reserved_amount < Decimal("0.00"):
            reserved_amount = Decimal("0.00")
        if reserved_amount > outstanding_amount:
            reserved_amount = outstanding_amount

        expected_cash_to_collect = max(Decimal("0.00"), outstanding_amount - reserved_amount)
        return {
            "cod_reserved_prepayment_amount": float(reserved_amount),
            "expected_cash_to_collect": float(expected_cash_to_collect),
        }

    @staticmethod
    def get_active_delivery_counts(delivery_person_ids: List[int]) -> Dict[int, int]:
        """Return live active-delivery counts keyed by delivery-person user ID."""
        person_ids = sorted({int(person_id) for person_id in delivery_person_ids if person_id})
        if not person_ids:
            return {}

        counts = {person_id: 0 for person_id in person_ids}
        rows = (
            db.session.query(
                Delivery.delivery_person_id,
                func.count(Delivery.id),
            )
            .filter(
                Delivery.delivery_person_id.in_(person_ids),
                Delivery.status.in_(StaffService.get_active_delivery_statuses()),
            )
            .group_by(Delivery.delivery_person_id)
            .all()
        )

        for delivery_person_id, active_count in rows:
            counts[int(delivery_person_id)] = int(active_count)

        return counts

    @staticmethod
    def get_active_delivery_count(delivery_person_id: int) -> int:
        """Return the live active-delivery count for one delivery person."""
        if not delivery_person_id:
            return 0
        return StaffService.get_active_delivery_counts([delivery_person_id]).get(int(delivery_person_id), 0)

    @staticmethod
    def sync_active_delivery_counters(delivery_person_ids: List[int]) -> None:
        """Best-effort sync for legacy cached workload counters."""
        person_ids = sorted({int(person_id) for person_id in delivery_person_ids if person_id})
        if not person_ids:
            return

        live_counts = StaffService.get_active_delivery_counts(person_ids)
        profiles = DeliveryPerson.query.filter(DeliveryPerson.user_id.in_(person_ids)).all()
        for profile in profiles:
            profile.current_active_deliveries = live_counts.get(profile.user_id, 0)

    @staticmethod
    def create_delivery_person(staff_data: Dict[str, Any], created_by: Optional[int] = None) -> DeliveryPerson:
        """
        Create delivery person profile (and user when needed) for admin panel.
        """
        from business_app.utils.helpers import format_phone_number
        from business_app.utils.password_security import hash_password
        import secrets

        staff_data = staff_data or {}
        user_id = staff_data.get("user_id")
        full_name = (staff_data.get("full_name") or "").strip()
        phone = staff_data.get("phone")

        if not user_id and (not full_name or not phone):
            raise ValidationError(
                "full_name and phone are required when user_id is not provided",
                error_code="STAFF_FULL_NAME_PHONE_REQUIRED",
            )

        user: Optional[User] = None
        creating_new_user = False

        if user_id:
            user = User.query.get(user_id)
            if not user:
                raise NotFoundError("User not found", error_code="STAFF_USER_NOT_FOUND")
        else:
            formatted_phone = format_phone_number(phone)
            if not formatted_phone:
                raise ValidationError("Invalid phone number format", error_code="STAFF_PHONE_INVALID")
            user = User.query.filter_by(phone=formatted_phone).first()
            if not user:
                creating_new_user = True
                user = User(
                    phone=formatted_phone,
                    email=(staff_data.get("email") or "").strip() or None,
                    password_hash=hash_password(secrets.token_urlsafe(32)),
                    user_type=UserType.STAFF,
                    role=UserRole.DELIVERY_DRIVER,
                    status=UserStatus.ACTIVE,
                    registration_source="admin",
                    preferred_language=staff_data.get("preferred_language", "uz") or "uz",
                )
                StaffService._set_name_from_full_name(user, full_name)
                db.session.add(user)
                db.session.flush()

        if DeliveryPerson.query.filter_by(user_id=user.id).first():
            raise ConflictError(
                "Delivery person profile already exists for this user", error_code="STAFF_DELIVERY_PERSON_EXISTS"
            )

        if phone and user.phone:
            formatted_phone = format_phone_number(phone)
            if not formatted_phone:
                raise ValidationError("Invalid phone number format", error_code="STAFF_PHONE_INVALID")
            if formatted_phone != user.phone:
                duplicate_phone = User.query.filter(
                    User.phone == formatted_phone,
                    User.id != user.id,
                ).first()
                if duplicate_phone:
                    raise ConflictError("Phone is already used by another user", error_code="STAFF_PHONE_EXISTS")
                user.phone = formatted_phone

        if not user.phone:
            if not phone:
                raise ValidationError("phone is required", error_code="STAFF_PHONE_REQUIRED")
            user.phone = format_phone_number(phone)

        if full_name:
            StaffService._set_name_from_full_name(user, full_name)

        if staff_data.get("email") is not None:
            email_value = (staff_data.get("email") or "").strip() or None
            if email_value:
                duplicate_email = User.query.filter(
                    User.email == email_value,
                    User.id != user.id,
                ).first()
                if duplicate_email:
                    raise ConflictError("Email is already used by another user", error_code="STAFF_EMAIL_EXISTS")
            user.email = email_value

        roles = StaffService._extract_staff_roles(user)
        if UserRole.DELIVERY_DRIVER.value not in roles:
            roles.append(UserRole.DELIVERY_DRIVER.value)

        provided_roles = StaffService._normalize_staff_roles_input(staff_data.get("staff_roles"))
        for role in provided_roles:
            if role not in roles:
                roles.append(role)

        user.staff_roles = roles
        user.user_type = UserType.STAFF
        if creating_new_user:
            user.role = UserRole.DELIVERY_DRIVER
            user.status = UserStatus.ACTIVE

        profile_full_name = full_name or user.full_name or user.phone

        delivery_person = DeliveryPerson(
            user_id=user.id,
            full_name=profile_full_name,
            phone=user.phone,
            email=user.email,
            employee_id=(staff_data.get("employee_id") or "").strip() or None,
            vehicle_type=(staff_data.get("vehicle_type") or "").strip() or None,
            vehicle_number=(staff_data.get("vehicle_number") or "").strip() or None,
            vehicle_capacity_kg=(
                float(staff_data.get("vehicle_capacity_kg") or 0)
                if staff_data.get("vehicle_capacity_kg") is not None
                else 0.0
            ),
            working_hours_start=(staff_data.get("working_hours_start") or "09:00"),
            working_hours_end=(staff_data.get("working_hours_end") or "18:00"),
            working_days=staff_data.get("working_days")
            or ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"],
            max_concurrent_deliveries=int(staff_data.get("max_concurrent_deliveries") or 3),
            is_active=bool(staff_data.get("is_active", True)),
            is_available=bool(staff_data.get("is_available", True)),
            notifications_muted=bool(staff_data.get("notifications_muted", False)),
            emergency_contact_name=(staff_data.get("emergency_contact_name") or "").strip() or None,
            emergency_contact_phone=(staff_data.get("emergency_contact_phone") or "").strip() or None,
        )

        db.session.add(delivery_person)
        db.session.commit()

        actor_id = created_by or user.id
        StaffService._log_activity(
            user_id=actor_id,
            action=STAFF_ACTIONS["USER_CREATED"],
            entity_type="user",
            entity_id=user.id,
            metadata_={"staff_role": UserRole.DELIVERY_DRIVER.value, "delivery_person_id": delivery_person.id},
        )

        return delivery_person

    @staticmethod
    def update_delivery_person(
        person_id: int, updates: Dict[str, Any], updated_by: Optional[int] = None
    ) -> DeliveryPerson:
        """
        Update delivery person profile and linked user fields for admin panel.
        """
        from business_app.utils.helpers import format_phone_number

        updates = updates or {}
        delivery_person = DeliveryPerson.query.options(joinedload(DeliveryPerson.user)).get(person_id)
        if not delivery_person:
            raise NotFoundError("Delivery person not found", error_code="STAFF_DELIVERY_PERSON_NOT_FOUND")

        user = delivery_person.user
        if not user:
            raise ValidationError(
                "Delivery person is missing linked user", error_code="STAFF_DELIVERY_PERSON_LINK_MISSING"
            )

        if "full_name" in updates and isinstance(updates.get("full_name"), str):
            full_name = updates.get("full_name", "").strip()
            if not full_name:
                raise ValidationError("full_name cannot be empty", error_code="STAFF_FULL_NAME_EMPTY")
            delivery_person.full_name = full_name
            StaffService._set_name_from_full_name(user, full_name)

        if "phone" in updates and updates.get("phone") is not None:
            formatted_phone = format_phone_number(updates.get("phone"))
            if not formatted_phone:
                raise ValidationError("Invalid phone number format", error_code="STAFF_PHONE_INVALID")
            duplicate_phone = User.query.filter(
                User.phone == formatted_phone,
                User.id != user.id,
            ).first()
            if duplicate_phone:
                raise ConflictError("Phone is already used by another user", error_code="STAFF_PHONE_EXISTS")
            user.phone = formatted_phone
            delivery_person.phone = formatted_phone

        if "email" in updates:
            email_value = (updates.get("email") or "").strip() or None
            if email_value:
                duplicate_email = User.query.filter(
                    User.email == email_value,
                    User.id != user.id,
                ).first()
                if duplicate_email:
                    raise ConflictError("Email is already used by another user", error_code="STAFF_EMAIL_EXISTS")
            user.email = email_value
            delivery_person.email = email_value

        if "employee_id" in updates:
            employee_id = (updates.get("employee_id") or "").strip() or None
            if employee_id:
                duplicate_emp = DeliveryPerson.query.filter(
                    DeliveryPerson.employee_id == employee_id,
                    DeliveryPerson.id != delivery_person.id,
                ).first()
                if duplicate_emp:
                    raise ConflictError("Employee ID already exists", error_code="STAFF_EMPLOYEE_ID_EXISTS")
            delivery_person.employee_id = employee_id

        if "vehicle_type" in updates:
            delivery_person.vehicle_type = (updates.get("vehicle_type") or "").strip() or None
        if "vehicle_number" in updates:
            delivery_person.vehicle_number = (updates.get("vehicle_number") or "").strip() or None
        if "vehicle_capacity_kg" in updates:
            delivery_person.vehicle_capacity_kg = float(updates.get("vehicle_capacity_kg") or 0)
        if "working_hours_start" in updates and updates.get("working_hours_start"):
            delivery_person.working_hours_start = str(updates.get("working_hours_start"))
        if "working_hours_end" in updates and updates.get("working_hours_end"):
            delivery_person.working_hours_end = str(updates.get("working_hours_end"))
        if "working_days" in updates and isinstance(updates.get("working_days"), list):
            delivery_person.working_days = updates.get("working_days")
        if "max_concurrent_deliveries" in updates and updates.get("max_concurrent_deliveries") is not None:
            delivery_person.max_concurrent_deliveries = int(updates.get("max_concurrent_deliveries"))
        if "is_active" in updates:
            delivery_person.is_active = bool(updates.get("is_active"))
        if "is_available" in updates:
            delivery_person.is_available = bool(updates.get("is_available"))
        if "notifications_muted" in updates:
            delivery_person.notifications_muted = bool(updates.get("notifications_muted"))
        if "emergency_contact_name" in updates:
            delivery_person.emergency_contact_name = (updates.get("emergency_contact_name") or "").strip() or None
        if "emergency_contact_phone" in updates:
            delivery_person.emergency_contact_phone = (updates.get("emergency_contact_phone") or "").strip() or None

        roles = StaffService._extract_staff_roles(user)
        if UserRole.DELIVERY_DRIVER.value not in roles:
            roles.append(UserRole.DELIVERY_DRIVER.value)
        user.staff_roles = roles
        user.user_type = UserType.STAFF

        db.session.commit()

        StaffService._log_activity(
            user_id=updated_by or user.id,
            action=STAFF_ACTIONS["DELIVERY_STATUS_UPDATED"],
            entity_type="user",
            entity_id=user.id,
            metadata_={"delivery_person_id": delivery_person.id, "operation": "admin_update_profile"},
        )

        return delivery_person

    @staticmethod
    def create_operator(staff_data: Dict[str, Any], created_by: Optional[int] = None) -> User:
        """
        Create operator account (or grant operator role to an existing user).
        """
        from business_app.utils.helpers import format_phone_number
        from business_app.utils.password_security import hash_password
        import secrets

        staff_data = staff_data or {}
        user_id = staff_data.get("user_id")
        phone = staff_data.get("phone")

        if not user_id and not phone:
            raise ValidationError("phone is required when user_id is not provided", error_code="STAFF_PHONE_REQUIRED")

        user: Optional[User] = None
        creating_new_user = False

        if user_id:
            user = User.query.get(user_id)
            if not user:
                raise NotFoundError("User not found", error_code="STAFF_USER_NOT_FOUND")
        else:
            formatted_phone = format_phone_number(phone)
            if not formatted_phone:
                raise ValidationError("Invalid phone number format", error_code="STAFF_PHONE_INVALID")
            user = User.query.filter_by(phone=formatted_phone).first()
            if not user:
                creating_new_user = True
                user = User(
                    phone=formatted_phone,
                    password_hash=hash_password(secrets.token_urlsafe(32)),
                    user_type=UserType.STAFF,
                    role=UserRole.OPERATOR,
                    status=UserStatus.ACTIVE,
                    registration_source="admin",
                    preferred_language=staff_data.get("preferred_language", "uz") or "uz",
                )
                db.session.add(user)
                db.session.flush()

        if "phone" in staff_data and staff_data.get("phone"):
            formatted_phone = format_phone_number(staff_data.get("phone"))
            if not formatted_phone:
                raise ValidationError("Invalid phone number format", error_code="STAFF_PHONE_INVALID")
            duplicate_phone = User.query.filter(
                User.phone == formatted_phone,
                User.id != user.id,
            ).first()
            if duplicate_phone:
                raise ConflictError("Phone is already used by another user", error_code="STAFF_PHONE_EXISTS")
            user.phone = formatted_phone

        if "first_name" in staff_data and staff_data.get("first_name") is not None:
            user.first_name = (staff_data.get("first_name") or "").strip() or None
        if "last_name" in staff_data and staff_data.get("last_name") is not None:
            user.last_name = (staff_data.get("last_name") or "").strip() or None
        if "full_name" in staff_data and staff_data.get("full_name"):
            StaffService._set_name_from_full_name(user, staff_data.get("full_name"))
        if "email" in staff_data:
            email_value = (staff_data.get("email") or "").strip() or None
            if email_value:
                duplicate_email = User.query.filter(
                    User.email == email_value,
                    User.id != user.id,
                ).first()
                if duplicate_email:
                    raise ConflictError("Email is already used by another user", error_code="STAFF_EMAIL_EXISTS")
            user.email = email_value
        if "status" in staff_data and staff_data.get("status"):
            status_value = str(staff_data.get("status")).lower()
            if status_value not in [UserStatus.ACTIVE.value, UserStatus.INACTIVE.value, UserStatus.BANNED.value]:
                raise ValidationError("Invalid status value", error_code="STAFF_INVALID_STATUS")
            user.status = UserStatus(status_value)
        elif creating_new_user:
            user.status = UserStatus.ACTIVE

        roles = StaffService._extract_staff_roles(user)
        if UserRole.OPERATOR.value not in roles:
            roles.append(UserRole.OPERATOR.value)

        provided_roles = StaffService._normalize_staff_roles_input(staff_data.get("staff_roles"))
        for role in provided_roles:
            if role not in roles:
                roles.append(role)

        user.staff_roles = roles
        user.user_type = UserType.STAFF
        if creating_new_user:
            user.role = UserRole.OPERATOR

        db.session.commit()

        StaffService._log_activity(
            user_id=created_by or user.id,
            action=STAFF_ACTIONS["USER_CREATED"],
            entity_type="user",
            entity_id=user.id,
            metadata_={"staff_role": UserRole.OPERATOR.value},
        )

        return user

    @staticmethod
    def update_operator(user_id: int, updates: Dict[str, Any], updated_by: Optional[int] = None) -> User:
        """
        Update operator profile and roles.
        """
        from business_app.utils.helpers import format_phone_number

        updates = updates or {}
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found", error_code="STAFF_USER_NOT_FOUND")

        roles = StaffService._extract_staff_roles(user)
        if UserRole.OPERATOR.value not in roles:
            raise ValidationError("User does not have operator role", error_code="STAFF_OPERATOR_ROLE_REQUIRED")

        if "first_name" in updates and updates.get("first_name") is not None:
            user.first_name = (updates.get("first_name") or "").strip() or None
        if "last_name" in updates and updates.get("last_name") is not None:
            user.last_name = (updates.get("last_name") or "").strip() or None
        if "full_name" in updates and updates.get("full_name"):
            StaffService._set_name_from_full_name(user, updates.get("full_name"))
        if "phone" in updates and updates.get("phone") is not None:
            formatted_phone = format_phone_number(updates.get("phone"))
            if not formatted_phone:
                raise ValidationError("Invalid phone number format", error_code="STAFF_PHONE_INVALID")
            duplicate_phone = User.query.filter(
                User.phone == formatted_phone,
                User.id != user.id,
            ).first()
            if duplicate_phone:
                raise ConflictError("Phone is already used by another user", error_code="STAFF_PHONE_EXISTS")
            user.phone = formatted_phone
        if "email" in updates:
            email_value = (updates.get("email") or "").strip() or None
            if email_value:
                duplicate_email = User.query.filter(
                    User.email == email_value,
                    User.id != user.id,
                ).first()
                if duplicate_email:
                    raise ConflictError("Email is already used by another user", error_code="STAFF_EMAIL_EXISTS")
            user.email = email_value
        if "status" in updates and updates.get("status"):
            status_value = str(updates.get("status")).lower()
            if status_value not in [UserStatus.ACTIVE.value, UserStatus.INACTIVE.value, UserStatus.BANNED.value]:
                raise ValidationError("Invalid status value", error_code="STAFF_INVALID_STATUS")
            user.status = UserStatus(status_value)

        provided_roles = updates.get("staff_roles")
        if provided_roles is not None:
            normalized_roles = StaffService._normalize_staff_roles_input(provided_roles)
            if UserRole.OPERATOR.value not in normalized_roles:
                normalized_roles.append(UserRole.OPERATOR.value)
            user.staff_roles = normalized_roles
        else:
            if UserRole.OPERATOR.value not in roles:
                roles.append(UserRole.OPERATOR.value)
            user.staff_roles = roles
        user.user_type = UserType.STAFF

        db.session.commit()

        StaffService._log_activity(
            user_id=updated_by or user.id,
            action=STAFF_ACTIONS["DELIVERY_STATUS_UPDATED"],
            entity_type="user",
            entity_id=user.id,
            metadata_={"operation": "admin_update_operator"},
        )

        return user

    @staticmethod
    def update_staff_roles(user_id: int, staff_roles: Any, updated_by: Optional[int] = None) -> User:
        """
        Update staff_roles for a user while keeping delivery profile consistency.
        """
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found", error_code="STAFF_USER_NOT_FOUND")

        normalized_roles = StaffService._normalize_staff_roles_input(staff_roles)
        if not normalized_roles:
            raise ValidationError("At least one staff role is required", error_code="STAFF_ROLE_REQUIRED")

        has_delivery_profile = DeliveryPerson.query.filter_by(user_id=user.id).first() is not None
        if has_delivery_profile and UserRole.DELIVERY_DRIVER.value not in normalized_roles:
            normalized_roles.append(UserRole.DELIVERY_DRIVER.value)

        user.staff_roles = normalized_roles
        user.user_type = UserType.STAFF
        db.session.commit()

        StaffService._log_activity(
            user_id=updated_by or user.id,
            action=STAFF_ACTIONS["DELIVERY_STATUS_UPDATED"],
            entity_type="user",
            entity_id=user.id,
            metadata_={"operation": "admin_update_roles", "staff_roles": normalized_roles},
        )

        return user

    @staticmethod
    def _consume_invite_payload(invite_token: str) -> Dict[str, Any]:
        """
        Validate and consume a one-time invite token from Redis.
        Token key format: staff_bot:invite:<token>
        """
        redis_url = current_app.config.get("REDIS_URL")
        if not redis_url:
            raise ValidationError(
                "Invite token flow is unavailable: REDIS_URL is not configured",
                error_code="STAFF_INVITE_REDIS_UNAVAILABLE",
            )

        redis_client = redis.from_url(redis_url, decode_responses=True)
        key = RedisKeyspace.staff_bot_invite(invite_token)

        try:
            if hasattr(redis_client, "getdel"):
                payload_json = redis_client.getdel(key)
            else:
                payload_json = redis_client.get(key)
                if payload_json:
                    redis_client.delete(key)
        except redis.RedisError as exc:
            raise ValidationError(f"Invite token store unavailable: {exc}", error_code="STAFF_INVITE_STORE_UNAVAILABLE")
        finally:
            try:
                redis_client.close()
            except Exception:
                pass

        if not payload_json:
            raise ForbiddenError(
                "Invite token is invalid, expired, or already used", error_code="STAFF_INVALID_INVITE_TOKEN"
            )

        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            raise ValidationError("Invite token payload is malformed", error_code="STAFF_INVITE_PAYLOAD_MALFORMED")

        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def authenticate_and_link_staff(telegram_id: str, invite_token: Optional[str] = None) -> Dict[str, Any]:
        """
        Authenticate a staff member by pre-bound Telegram ID or one-time invite token.
        Issues a JWT token for subsequent API calls.

        Args:
            telegram_id: Telegram user ID to link
            invite_token: Optional one-time invite token for first-time Telegram binding

        Returns:
            Dict with user info and access token

        Raises:
            ValidationError: If payload is invalid
            ForbiddenError: If Telegram account is not pre-approved and no valid invite token provided
            ConflictError: If telegram_id is linked to a different staff account
        """
        if not telegram_id:
            raise ValidationError("telegram_id is required", error_code="STAFF_TELEGRAM_ID_REQUIRED")

        telegram_id = str(telegram_id)
        auth_method = "prebound"

        # Path A: Telegram ID already pre-linked by admin.
        user = User.query.filter_by(telegram_id=telegram_id).first()

        # Path B: First-time binding using one-time invite token.
        if not user:
            if not invite_token:
                raise ForbiddenError(
                    "This Telegram account is not approved for staff bot access",
                    error_code="STAFF_TELEGRAM_NOT_APPROVED",
                )

            auth_method = "invite_token"
            payload = StaffService._consume_invite_payload(invite_token)
            invited_user_id = payload.get("user_id")

            if not invited_user_id:
                raise ValidationError(
                    "Invite token payload must include user_id", error_code="STAFF_INVITE_PAYLOAD_USER_ID_REQUIRED"
                )

            user = User.query.get(invited_user_id)
            if not user:
                raise NotFoundError("Staff account from invite token was not found", error_code="STAFF_USER_NOT_FOUND")

            existing = User.query.filter(
                User.telegram_id == telegram_id,
                User.id != user.id,
            ).first()
            if existing:
                raise ConflictError(
                    "This Telegram account is already linked to another user",
                    error_code="STAFF_TELEGRAM_ALREADY_LINKED",
                )

            user.telegram_id = telegram_id

        staff_roles_list = StaffService._extract_staff_roles(user)
        if not staff_roles_list:
            raise ForbiddenError("User does not have a staff role", error_code="STAFF_NO_ROLE")

        # Block staff-bot access for delivery persons an admin has deactivated.
        StaffService.assert_delivery_person_active(user)

        # Keep normalized staff_roles persisted so staff bot can use it as role source.
        if user.staff_roles != staff_roles_list:
            user.staff_roles = staff_roles_list

        now = datetime.now(timezone.utc)
        user.last_login = now

        # Generate JWT token
        from business_app.services.token_service import TokenService

        token_service = TokenService()
        tokens = token_service.generate_tokens(user, additional_claims={"staff_roles": staff_roles_list})
        db.session.commit()

        # Log the authentication
        StaffService._log_activity(
            user_id=user.id,
            action=STAFF_ACTIONS["STAFF_LOGIN"],
            entity_type="user",
            entity_id=user.id,
            metadata_={"telegram_id": telegram_id, "auth_method": auth_method},
        )

        current_app.logger.info(
            "Staff user %s authenticated via %s flow (telegram_id=%s)",
            user.id,
            auth_method,
            telegram_id,
        )

        role_value = user.role.value if hasattr(user.role, "value") else user.role
        delivery_person = DeliveryPerson.query.filter_by(user_id=user.id).first()

        return {
            "user": {
                "id": user.id,
                "phone": user.phone,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "full_name": user.full_name,
                "role": role_value,
                "staff_roles": staff_roles_list,
                "preferred_language": user.preferred_language,
                "delivery_person_id": delivery_person.id if delivery_person else None,
            },
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token"),
            "expires_in": tokens.get("expires_in", 3600),
        }

    @staticmethod
    def get_delivery_pool(filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Get deliveries available for pickup in staff order pool.

        Default behavior returns unassigned deliveries whose orders are in
        confirmed/preparing statuses.

        Optional `order_id` filter can fetch a single pool item by order id.
        This mode may include assigned deliveries so stale order cards can still
        show current assignee/status to operators.

        Args:
            filters: Optional dict with 'page', 'per_page', 'order_id', 'delivery_id',
                'include_assigned' keys

        Returns:
            Dict with Delivery items and pagination info
        """
        filters = filters or {}
        page = filters.get("page", 1)
        per_page = filters.get("per_page", 20)
        order_id = filters.get("order_id")
        delivery_id = filters.get("delivery_id")
        include_assigned = bool(filters.get("include_assigned", False))

        query = (
            Delivery.query.join(Order, Delivery.order_id == Order.id)
            .filter(
                Order.status.in_([OrderStatus.CONFIRMED, OrderStatus.PREPARING]),
            )
            .options(
                joinedload(Delivery.order).joinedload(Order.order_items).joinedload(OrderItem.product),
                joinedload(Delivery.order).joinedload(Order.delivery_address),
                joinedload(Delivery.order).joinedload(Order.user),
                joinedload(Delivery.order).joinedload(Order.payment),
                joinedload(Delivery.delivery_person),
            )
        )

        if delivery_id is not None:
            query = query.filter(Delivery.id == delivery_id)
            if not include_assigned:
                query = query.filter(Delivery.delivery_person_id.is_(None))
        elif order_id is not None:
            query = query.filter(Order.id == order_id)
            if not include_assigned:
                query = query.filter(Delivery.delivery_person_id.is_(None))
        else:
            query = query.filter(
                Delivery.delivery_person_id.is_(None),
                Delivery.status.in_([DeliveryStatus.SCHEDULED, DeliveryStatus.PENDING]),
            )

        query = query.order_by(Order.created_at.asc())

        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        return {
            "items": pagination.items,
            "pagination": {
                "page": pagination.page,
                "per_page": pagination.per_page,
                "total": pagination.total,
                "pages": pagination.pages,
            },
        }

    @staticmethod
    def accept_order(delivery_id: int, delivery_person_id: int) -> Delivery:
        """
        Accept a delivery assignment using row-level locking to prevent race conditions.

        Args:
            delivery_id: ID of the delivery to accept
            delivery_person_id: ID of the delivery person accepting

        Returns:
            Updated Delivery object

        Raises:
            NotFoundError: If delivery not found
            ValidationError: If delivery already assigned or person at max capacity
        """
        from business_app.services.delivery_assignment_service import DeliveryAssignmentService
        from shared.enums import AssignmentSource

        result = DeliveryAssignmentService.assign_driver(
            delivery_id,
            driver_user_id=delivery_person_id,
            actor_id=delivery_person_id,
            source=AssignmentSource.BOT_SELF_ACCEPT,
            note="Accepted via staff bot",
            require_session=True,
        )
        delivery = result.delivery
        history_id = result.history_id

        # Post-commit side-effects specific to bot self-accept.
        if result.changed and history_id is not None:
            try:
                from business_app.tasks.notification_tasks import send_delivery_update_task

                send_delivery_update_task.delay(history_id)
            except Exception as notify_exc:
                current_app.logger.warning(
                    "Failed to enqueue assigned-delivery notification for delivery %s: %s",
                    delivery.id,
                    notify_exc,
                )
            StaffService._log_activity(
                user_id=delivery_person_id,
                action=STAFF_ACTIONS["DELIVERY_ACCEPTED"],
                entity_type="delivery",
                entity_id=delivery.id,
                metadata_={"order_id": delivery.order_id},
            )
        current_app.logger.info(f"Delivery {delivery_id} accepted by delivery person {delivery_person_id}")
        return delivery

    @staticmethod
    def return_delivery_to_pool(
        delivery_id: int,
        actor_id: int,
        *,
        reason: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Delivery:
        """Return a delivery to the unassigned pool so it can be (re-)claimed.

        This is the single supported way to move a delivery *back* toward the
        pool and the foundation of the failed-delivery re-dispatch flow. It:

        - clears the driver assignment (``delivery_person_id`` → None),
        - resets the delivery status to SCHEDULED,
        - restores the parent order to a pool-eligible status (CONFIRMED) when
          it has moved past it (e.g. OUT_FOR_DELIVERY/RETURNED), so the delivery
          actually surfaces in the pool (which requires the order to be in
          CONFIRMED/PREPARING),
        - clears the order's bottle-session binding (so re-accept doesn't conflict),
        - records a DeliveryStatusHistory row attributed to ``actor_id``,
        - resyncs the previous driver's active-delivery counters.

        It enforces the invariant that a pool-status delivery never retains a
        driver (``assert_unassigned_for_pool_status``).

        Raises:
            NotFoundError: If delivery not found.
        """
        delivery = Delivery.query.with_for_update().get(delivery_id)
        if not delivery:
            raise NotFoundError("Delivery not found", error_code="STAFF_DELIVERY_NOT_FOUND")

        now = datetime.now(timezone.utc)
        old_status = delivery.status
        old_driver_id = delivery.delivery_person_id

        delivery.delivery_person_id = None
        delivery.status = DeliveryStatus.SCHEDULED
        # Clear the stale failure reason so the next driver doesn't inherit it;
        # delivery_attempts is intentionally preserved as the running counter.
        delivery.failed_delivery_reason = None
        delivery.updated_at = now

        # Enforce the pool invariant on the row we just produced.
        assert_unassigned_for_pool_status(delivery, DeliveryStatus.SCHEDULED)

        # Make sure the order is pool-eligible. The pool only lists deliveries
        # whose order is CONFIRMED/PREPARING; a failed/out-for-delivery order
        # would keep the returned delivery hidden.
        if delivery.order and delivery.order.status not in (OrderStatus.CONFIRMED, OrderStatus.PREPARING):
            delivery.order.status = OrderStatus.CONFIRMED
            delivery.order.updated_at = now

        history = DeliveryStatusHistory(
            delivery_id=delivery.id,
            old_status=old_status,
            new_status=DeliveryStatus.SCHEDULED,
            changed_by=actor_id,
            changed_at=now,
            notes=notes or "Returned to delivery pool for re-dispatch",
            reason=reason,
        )
        db.session.add(history)

        # The delivery no longer has a driver, so its order must not keep a stale
        # bottle-session binding — otherwise the next driver to accept it hits a
        # cross-session ConflictError in bind_order_to_session. The order rebinds
        # when it is re-accepted/re-assigned.
        if delivery.order:
            from business_app.services.bottle_tracking_service import BottleTrackingService

            BottleTrackingService().unbind_order(delivery.order.id)

        # The previous driver lost a delivery — refresh their cached workload.
        if old_driver_id:
            StaffService.sync_active_delivery_counters([old_driver_id])

        db.session.commit()

        current_app.logger.info(
            f"Delivery {delivery_id} returned to pool by user {actor_id} "
            f"(was {old_status.value if old_status else None}, driver {old_driver_id})"
        )

        return delivery

    @staticmethod
    def redispatch_failed_delivery(
        delivery_id: int,
        actor_id: int,
        *,
        reason: Optional[str] = None,
    ) -> Delivery:
        """Re-dispatch a FAILED delivery by returning it to the pool.

        Thin wrapper over ``return_delivery_to_pool`` that enforces the source
        status. Shared entry point for the admin re-dispatch endpoint and the
        operator staff-bot flow so the "only failed deliveries are
        re-dispatchable" rule lives in one place.

        Raises:
            NotFoundError: If delivery not found.
            ValidationError: If the delivery is not in FAILED status.
        """
        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            raise NotFoundError("Delivery not found", error_code="STAFF_DELIVERY_NOT_FOUND")
        if delivery.status != DeliveryStatus.FAILED:
            raise ValidationError(
                f"Only failed deliveries can be re-dispatched (current status: {delivery.status.value})",
                error_code="STAFF_DELIVERY_NOT_REDISPATCHABLE",
            )
        return StaffService.return_delivery_to_pool(
            delivery_id,
            actor_id,
            reason=reason,
            notes="Re-dispatched from failed status",
        )

    @staticmethod
    def get_failed_deliveries(limit: int = 25) -> List[Delivery]:
        """List recent FAILED deliveries available for operator re-dispatch,
        newest first."""
        return (
            Delivery.query.filter(Delivery.status == DeliveryStatus.FAILED)
            .options(
                joinedload(Delivery.order).joinedload(Order.order_items).joinedload(OrderItem.product),
                joinedload(Delivery.order).joinedload(Order.delivery_address),
                joinedload(Delivery.order).joinedload(Order.user),
            )
            .order_by(Delivery.updated_at.desc())
            .limit(limit)
            .all()
        )

    @staticmethod
    def update_delivery_status(
        delivery_id: int, new_status: str, staff_user_id: int, metadata: Optional[Dict[str, Any]] = None
    ) -> Delivery:
        """
        Update delivery status with transition validation and order status sync.

        Args:
            delivery_id: ID of the delivery
            new_status: New status string (e.g. 'picked_up', 'in_transit', 'delivered', 'failed')
            staff_user_id: ID of the staff user making the update
            metadata: Optional metadata (e.g. fail_reason, cash_collected, notes)

        Returns:
            Updated Delivery object

        Raises:
            NotFoundError: If delivery not found
            ValidationError: If status transition is invalid
        """
        metadata = metadata or {}
        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            raise NotFoundError("Delivery not found", error_code="STAFF_DELIVERY_NOT_FOUND")

        cash_collection_service = None
        pre_cod_debt_count = None
        is_cash_order = bool(delivery.order and delivery.order.payment_method == PaymentMethod.CASH)

        # Detect unsettled electronic orders: driver may collect cash at the door
        # even when the customer's online payment never completed.
        # _OFFLINE_SETTLEABLE_STATUSES is the canonical set; reuse it (DRY).
        from business_app.services.cash_collection_service import CashCollectionService as _CCS

        _electronic_methods = {PaymentMethod.CLICK, PaymentMethod.PAYME, PaymentMethod.CARD}
        order = delivery.order
        order_payment = order.payment if order else None
        is_unsettled_electronic = bool(
            order
            and order.payment_method in _electronic_methods
            and order_payment is not None
            and order_payment.status in _CCS._OFFLINE_SETTLEABLE_STATUSES
        )
        _raw_cash = metadata.get("cash_collected") if metadata else None
        try:
            driver_collected_cash = Decimal(str(_raw_cash)) > Decimal("0.00") if _raw_cash is not None else False
        except Exception:
            # Non-numeric / unparseable value → treat as "no cash" (safe coercion)
            driver_collected_cash = False
        settle_electronic_as_cash = new_status == "delivered" and is_unsettled_electronic and driver_collected_cash

        # THIRD SETTLEMENT SHAPE (plan 2026-08-08-open-receivable-ssot). A
        # successfully-settled electronic order whose total was edited upward at
        # the door owes the delta in cash. It is neither `is_cash_order` (the
        # rail is CLICK) nor `settle_electronic_as_cash` (the payment is
        # PARTIALLY_PAID, which is not in _OFFLINE_SETTLEABLE_STATUSES), so
        # before this arm existed the driver's cash was silently discarded and
        # the endpoint still returned 200 OK — prod order 961.
        #
        # No conversion happens here: the money is allocated onto the ELECTRONIC
        # payment so the card-paid portion keeps its fiscal record.
        # 🔴 THE RAIL CHECK IS LOAD-BEARING, not defensive tidiness.
        # `_validate_collection_context` permits a DELIVERY_COMPLETION against a
        # non-CASH order ONLY for `_electronic_methods`. Without the same
        # restriction here, a BUSINESS_ACCOUNT (contract-billed) or
        # LOYALTY_POINTS order edited upward at the door would enter this arm,
        # hit that validator, raise, and the driver could not mark the delivery
        # delivered AT ALL — a hard field blocker, not a money bug. Contract
        # billing settles against the corporate contract, never as door cash.
        settle_receivable_in_place = bool(
            new_status == "delivered"
            and driver_collected_cash
            and not is_cash_order
            and not settle_electronic_as_cash
            and order is not None
            and order.payment_method in _electronic_methods
            # `is_ledger_receivable`, not `has_open_receivable`: this must be the
            # SAME predicate `_validate_collection_context` applies downstream,
            # or this arm fires and post_collection then refuses — which would
            # block the driver from completing the delivery at all.
            and is_ledger_receivable(order_payment)
        )

        if is_cash_order or settle_electronic_as_cash or settle_receivable_in_place:
            from business_app.services.cash_collection_service import CashCollectionService

            cash_collection_service = CashCollectionService()
            if is_cash_order:
                # Pre-count only for true CASH orders; we use it for the debt-limit
                # breach notification below (which must not fire for just-converted orders).
                # Cluster-wide (spec 5.5) so the warning fires exactly when the cap
                # the engine enforces is actually crossed — one person's linked
                # phones share a single credit line.
                pre_cod_debt_count = cash_collection_service.get_cluster_active_cod_debt_count(delivery.order.user_id)

        old_status_value = delivery.status.value if hasattr(delivery.status, "value") else delivery.status

        # Validate transition against allowed transitions
        allowed = DELIVERY_STATUS_TRANSITIONS.get(old_status_value, [])
        if new_status not in allowed:
            raise ValidationError(
                f"Cannot transition from '{old_status_value}' to '{new_status}'. " f"Allowed transitions: {allowed}",
                error_code="STAFF_INVALID_STATUS_TRANSITION",
            )

        # Validate failed delivery reason
        if new_status == "failed":
            fail_reason = metadata.get("fail_reason")
            if fail_reason and fail_reason not in FAILED_DELIVERY_REASONS:
                raise ValidationError(
                    f"Invalid failure reason. Must be one of: {FAILED_DELIVERY_REASONS}",
                    error_code="STAFF_INVALID_FAIL_REASON",
                )

        # Map string to enum
        new_status_enum = DeliveryStatus(new_status)
        old_status_enum = delivery.status

        # Bottle-session continuity guard. For any post-assignment transition
        # on an order that carries returnable bottles, the driver's effective
        # session must still match the session the order was bound to at
        # accept time. Strict mode raises; legacy mode logs a warning.
        # "failed" is deliberately excluded: a failed attempt delivers no
        # bottles (nothing to tally), so it needs no session continuity — and
        # the failed branch below releases the binding outright. Running the
        # guard on "failed" would only re-bind the order moments before we
        # unbind it (and could even block marking-failed under strict mode).
        if new_status in ("picked_up", "in_transit", "arrived", "delivered"):
            from business_app.services.bottle_tracking_service import BottleTrackingService

            BottleTrackingService().assert_driver_can_progress_delivery(delivery)

        cod_debt_limit_breached = False

        try:
            # Update delivery status
            delivery.status = new_status_enum
            delivery.updated_at = datetime.now(timezone.utc)

            # Handle status-specific fields
            now = datetime.now(timezone.utc)
            if new_status == "picked_up":
                delivery.route_data = delivery.route_data or {}
                delivery.route_data["picked_up_at"] = now.isoformat()
            elif new_status == "in_transit":
                delivery.route_data = delivery.route_data or {}
                delivery.route_data["in_transit_at"] = now.isoformat()
            elif new_status == "arrived":
                delivery.route_data = delivery.route_data or {}
                delivery.route_data["arrived_at"] = now.isoformat()
            elif new_status == "delivered":
                delivery.delivered_at = now
                delivery.actual_delivery_time = now
                dp = DeliveryPerson.query.filter_by(user_id=delivery.delivery_person_id).first()
                if dp:
                    dp.total_deliveries = (dp.total_deliveries or 0) + 1
                    dp.successful_deliveries = (dp.successful_deliveries or 0) + 1
            elif new_status == "failed":
                delivery.failed_delivery_reason = metadata.get("fail_reason", "other")
                delivery.delivery_attempts = (delivery.delivery_attempts or 0) + 1
                dp = DeliveryPerson.query.filter_by(user_id=delivery.delivery_person_id).first()
                if dp:
                    dp.total_deliveries = (dp.total_deliveries or 0) + 1
                # A failed attempt ends this trip for the order: release its
                # bottle-session binding so it stops counting as an "open" order
                # and the driver can close their session (the prod session-72
                # lockup — BOTTLE_SESSION_HAS_OPEN_ORDERS — came from this gap).
                # The order stays FAILED for operator re-dispatch and re-binds
                # when re-accepted. Mirrors return_delivery_to_pool's unbind.
                if delivery.order_id:
                    from business_app.services.bottle_tracking_service import BottleTrackingService

                    BottleTrackingService().unbind_order(delivery.order_id)

            # Create status history
            history = DeliveryStatusHistory(
                delivery_id=delivery.id,
                old_status=old_status_enum,
                new_status=new_status_enum,
                changed_by=staff_user_id,
                changed_at=now,
                notes=metadata.get("notes", f"Status updated via staff bot to {new_status}"),
                reason=metadata.get("fail_reason"),
            )
            db.session.add(history)
            # On ARRIVED / DELIVERED, stamp the destination coords onto the
            # history row and (if the driver's live location is stale)
            # refresh DeliveryPerson.current_location_*. Same helper is
            # used by DeliveryService.mark_delivery_arrived; we call it via
            # an instance because this method is a @staticmethod.
            if new_status in ("arrived", "delivered"):
                from business_app.services.delivery_service import DeliveryService

                DeliveryService()._capture_arrival_position(delivery, history)
            db.session.flush()
            history_id = history.id

            # Take the id-ordered candidate batch for the whole settlement BEFORE
            # anything in this transaction writes a single payment row.
            #
            # BOTH settlement shapes need it, not just the conversion:
            #   * settle_electronic_as_cash — convert_electronic_order_to_cash
            #     returns a ROW-LOCKED payment;
            #   * plain CASH — update_order_status(..., DELIVERED) reaches
            #     consume_reserved_prepayment_for_payment and
            #     apply_customer_prepaid_credit_to_payment, which write
            #     payment.amount_collected and so take a ROW EXCLUSIVE lock on the
            #     target at the next autoflush.
            # Either way a lone row lock ahead of the batch is the inversion that
            # deadlocks against a concurrent post walking the batch in id order:
            # T1 holds P_target then requests {P_older, P_target}; T2 holds
            # P_older and blocks on P_target. Cluster-fungible credit makes this
            # likelier still — a SIBLING's over-collection now also triggers the
            # pre-batch write. The abort is worse than a 500: _allocate_to_payment
            # may already have enqueued send_payment_confirmation_task, which does
            # not roll back, so the customer is told a rolled-back payment was
            # confirmed. Holding the batch first makes every later single-row lock
            # a re-request of a row we already own.
            if (
                new_status == "delivered"
                and (is_cash_order or settle_electronic_as_cash or settle_receivable_in_place)
                and cash_collection_service
            ):
                cash_collection_service.lock_order_settlement_candidates(
                    delivery.order,
                    source="delivery_completion",
                )

            # Sync order status per DELIVERY_TO_ORDER_STATUS_SYNC
            order_status_str = DELIVERY_TO_ORDER_STATUS_SYNC.get(new_status)
            if order_status_str and delivery.order:
                if order_status_str == OrderStatus.DELIVERED.value:
                    current_order_status = (
                        delivery.order.status.value
                        if hasattr(delivery.order.status, "value")
                        else delivery.order.status
                    )
                    if current_order_status != OrderStatus.DELIVERED.value:
                        # Flush delivery updates first so OrderService can apply
                        # delivery-linked business logic (inventory, loyalty) safely.
                        db.session.flush()
                        from business_app.services.order_service import OrderService

                        OrderService().update_order_status(
                            order_id=delivery.order_id,
                            new_status=OrderStatus.DELIVERED,
                            updated_by=staff_user_id,
                            notes=metadata.get("notes", "Delivered via staff bot"),
                            bottles_returned=metadata.get("bottles_returned"),
                            commit=False,
                        )
                else:
                    delivery.order.status = OrderStatus(order_status_str)
                    delivery.order.updated_at = now

            if delivery.delivery_person_id:
                db.session.flush()
                StaffService.sync_active_delivery_counters([delivery.delivery_person_id])

            # Cash collection runs in the same transaction as the status update so
            # a downstream failure rolls the delivery state back rather than
            # leaving the system half-applied.
            if (
                new_status == "delivered"
                and (is_cash_order or settle_electronic_as_cash or settle_receivable_in_place)
                and cash_collection_service
            ):
                # For unsuccessful electronic orders, convert to CASH first so the
                # existing delivery_completion collection can post against a CASH payment.
                if settle_electronic_as_cash:
                    # The id-ordered batch is already held (see above), so the
                    # ROW-LOCKED payment this conversion returns is a re-request
                    # of a row we already own rather than a new lock order.
                    cash_collection_service.convert_electronic_order_to_cash(
                        delivery.order,
                        actor_user_id=staff_user_id,
                        reason="cash_collected_at_delivery",
                    )

                cash_amount = metadata.get("cash_collected")
                if cash_amount is None:
                    cash_amount = Decimal("0.00")
                collection_notes = metadata.get("notes")
                if Decimal(str(cash_amount)) <= Decimal("0.00") and not collection_notes:
                    collection_notes = "No cash collected at delivery"

                cash_collection_service.post_collection(
                    customer_id=delivery.order.user_id,
                    amount=cash_amount,
                    source="delivery_completion",
                    collector_user_id=staff_user_id,
                    recorded_by_user_id=staff_user_id,
                    order_id=delivery.order_id,
                    delivery_id=delivery.id,
                    notes=collection_notes,
                    proof_data={
                        "delivery_status_history_id": history_id,
                        "status_metadata": metadata,
                    },
                    occurred_at=delivery.delivered_at or now,
                    commit=False,
                )

                # Debt-limit breach notification only applies to true CASH orders
                # (pre_cod_debt_count was captured only for those).
                if is_cash_order:
                    post_cod_debt_count = cash_collection_service.get_cluster_active_cod_debt_count(
                        delivery.order.user_id
                    )
                    limit = cash_collection_service.COD_ACTIVE_DEBT_LIMIT
                    cod_debt_limit_breached = (
                        pre_cod_debt_count is not None and pre_cod_debt_count < limit and post_cod_debt_count >= limit
                    )

            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        if new_status == "delivered" and is_cash_order and cod_debt_limit_breached:
            StaffService._notify_customer_cod_debt_limit(delivery.order.user_id)

        # Notify customer about delivery status updates (post-commit so a
        # rolled-back transition does not fire a stale notification).
        try:
            from business_app.tasks.notification_tasks import send_delivery_update_task

            send_delivery_update_task.delay(history_id)
        except Exception as notify_exc:
            current_app.logger.warning(
                "Failed to enqueue delivery-status notification for delivery %s: %s",
                delivery.id,
                notify_exc,
            )

        # Re-optimize on the transitions that MOVE the anchor:
        #  - picked_up / in_transit: the driver just committed to a stop —
        #    the tail re-anchors on it (silent: driver-initiated, spec §5.2).
        #  - delivered: the next leg starts from the drop point (silent).
        #  - arrived stays deliberately trigger-free (Task 2, spec §5.3).
        if new_status in ("picked_up", "in_transit", "delivered") and delivery.delivery_person_id:
            try:
                from business_app.tasks.delivery_tasks import optimize_driver_route_task

                trigger = "delivery" if new_status == "delivered" else new_status
                optimize_driver_route_task.delay(delivery.delivery_person_id, trigger)
            except Exception as exc:  # noqa: BLE001 — non-critical
                current_app.logger.warning(
                    "post-%s route optimization enqueue failed for driver=%s: %s",
                    new_status,
                    delivery.delivery_person_id,
                    exc,
                )

        # Log activity
        StaffService._log_activity(
            user_id=staff_user_id,
            action=STAFF_ACTIONS["DELIVERY_STATUS_UPDATED"],
            entity_type="delivery",
            entity_id=delivery.id,
            metadata_={
                "old_status": old_status_value,
                "new_status": new_status,
                "order_id": delivery.order_id,
                **metadata,
            },
        )

        current_app.logger.info(
            f"Delivery {delivery_id} status updated: {old_status_value} -> {new_status} "
            f"by staff user {staff_user_id}"
        )

        return delivery

    @staticmethod
    def _notify_customer_cod_debt_limit(user_id: int) -> None:
        user = User.query.get(user_id)
        if not user or not getattr(user, "telegram_id", None):
            return

        try:
            from types import SimpleNamespace

            from business_app.services.cash_collection_service import CashCollectionService
            from business_app.services.notification_service import NotificationService
            from business_app.utils.constants import NotificationChannel, NotificationType

            # The cap is configuration, not copy — read it from the SSOT so the
            # message can never claim a threshold the engine does not enforce.
            # "or more" because the crossing count is cluster-wide and a single
            # delivery can take the cluster past the cap.
            limit = CashCollectionService.COD_ACTIVE_DEBT_LIMIT
            subject = "Cash on delivery is restricted"
            body = (
                f"You have {limit} or more outstanding cash on delivery debts. "
                "Cash on delivery is now unavailable for new orders. "
                "Please use card payment methods until your outstanding COD debts are settled."
            )
            template = SimpleNamespace(
                subject=subject,
                content=body,
                get_translated=lambda field_name, _language: (subject if field_name == "subject" else body),
            )
            NotificationService().send_notification(
                user_id,
                NotificationType.SYSTEM,
                channels=[NotificationChannel.TELEGRAM],
                template_data={},
                template_override=template,
            )
        except Exception as exc:
            current_app.logger.warning(
                "Failed to send COD debt-limit Telegram warning for user %s: %s",
                user_id,
                exc,
            )

    @staticmethod
    def update_driver_location(user_id: int, lat: float, lng: float) -> DeliveryPerson:
        """
        Update the driver's own current location (driver-level, not tied to a
        specific delivery). Use this when the driver shares a one-shot or
        live location for route optimization purposes — no in-progress
        delivery is required.

        Returns the updated DeliveryPerson.
        """
        if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            raise ValidationError("Invalid coordinates", error_code="STAFF_INVALID_COORDINATES")

        dp = DeliveryPerson.query.filter_by(user_id=user_id).first()
        if not dp:
            raise NotFoundError("Delivery person profile not found", error_code="STAFF_DELIVERY_PERSON_NOT_FOUND")

        dp.current_location_lat = lat
        dp.current_location_lng = lng
        dp.last_location_update = datetime.now(timezone.utc)

        db.session.commit()
        return dp

    @staticmethod
    def update_delivery_location(delivery_id: int, lat: float, lng: float, *, acting_driver_id: int) -> Delivery:
        """
        Update delivery and delivery person's current location.

        Only the ASSIGNED driver may write: the coordinates are mirrored onto
        the assigned driver's DeliveryPerson row, so accepting any caller let
        one driver poison another driver's start point and location freshness
        (route-UX plan 2026-08-11, Task 1). 404 — not 403 — so the endpoint
        is not an existence oracle for other drivers' delivery ids.
        """
        if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            raise ValidationError("Invalid coordinates", error_code="STAFF_INVALID_COORDINATES")

        delivery = Delivery.query.get(delivery_id)
        if not delivery or delivery.delivery_person_id != acting_driver_id:
            raise NotFoundError("Delivery not found or not assigned", error_code="STAFF_DELIVERY_NOT_FOUND")

        now = datetime.now(timezone.utc)
        delivery.current_location_lat = lat
        delivery.current_location_lng = lng
        delivery.last_location_update = now

        # Also update delivery person's location
        if delivery.delivery_person_id:
            dp = DeliveryPerson.query.filter_by(user_id=delivery.delivery_person_id).first()
            if dp:
                dp.current_location_lat = lat
                dp.current_location_lng = lng
                dp.last_location_update = now

        db.session.commit()

        return delivery

    @staticmethod
    def get_active_deliveries(delivery_person_id: int) -> List[Delivery]:
        """
        Get deliveries currently assigned to a delivery person with active statuses.

        Args:
            delivery_person_id: User ID of the delivery person

        Returns:
            List of active Delivery objects
        """
        deliveries = (
            Delivery.query.filter(
                Delivery.delivery_person_id == delivery_person_id,
                Delivery.status.in_(StaffService.get_active_delivery_statuses()),
            )
            .options(
                joinedload(Delivery.order).joinedload(Order.order_items).joinedload(OrderItem.product),
                joinedload(Delivery.order).joinedload(Order.delivery_address),
                joinedload(Delivery.order).joinedload(Order.user),
                joinedload(Delivery.order).joinedload(Order.payment),
            )
            .order_by(Delivery.created_at.asc())
            .all()
        )

        return deliveries

    @staticmethod
    def get_delivery_history(delivery_person_id: int, page: int = 1, per_page: int = 20) -> Dict[str, Any]:
        """
        Get paginated delivery history for a delivery person.

        Args:
            delivery_person_id: User ID of the delivery person
            page: Page number
            per_page: Items per page

        Returns:
            Dict with items and pagination info
        """
        completed_statuses = [DeliveryStatus.DELIVERED, DeliveryStatus.FAILED]

        query = (
            Delivery.query.filter(
                Delivery.delivery_person_id == delivery_person_id, Delivery.status.in_(completed_statuses)
            )
            .options(
                joinedload(Delivery.order).joinedload(Order.user),
                joinedload(Delivery.order).joinedload(Order.delivery_address),
                joinedload(Delivery.order).joinedload(Order.payment),
            )
            .order_by(Delivery.updated_at.desc())
        )

        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        return {
            "items": pagination.items,
            "pagination": {
                "page": pagination.page,
                "per_page": pagination.per_page,
                "total": pagination.total,
                "pages": pagination.pages,
            },
        }

    @staticmethod
    def get_delivery_stats(user_id: int, period: str = "month") -> Dict[str, Any]:
        """
        Get delivery performance stats for a staff member.

        Args:
            user_id: User ID of the delivery person
            period: 'day', 'week', or 'month'

        Returns:
            Dict with performance metrics
        """
        now = datetime.now(timezone.utc)
        if period == "day":
            start_date = now - timedelta(days=1)
        elif period == "week":
            start_date = now - timedelta(weeks=1)
        else:  # month
            start_date = now - timedelta(days=30)

        # Base query for the period
        base_query = Delivery.query.filter(Delivery.delivery_person_id == user_id, Delivery.updated_at >= start_date)

        total_deliveries = base_query.count()
        delivered_count = base_query.filter(Delivery.status == DeliveryStatus.DELIVERED).count()
        failed_count = base_query.filter(Delivery.status == DeliveryStatus.FAILED).count()

        # Total cash collected in period
        cash_sum = (
            db.session.query(func.coalesce(func.sum(Delivery.cash_collected), 0))
            .filter(
                Delivery.delivery_person_id == user_id,
                Delivery.updated_at >= start_date,
                Delivery.status == DeliveryStatus.DELIVERED,
            )
            .scalar()
        )

        # Average delivery time (assigned to delivered)
        avg_time_result = (
            db.session.query(func.avg(func.extract("epoch", Delivery.delivered_at - Delivery.created_at)))
            .filter(
                Delivery.delivery_person_id == user_id,
                Delivery.updated_at >= start_date,
                Delivery.status == DeliveryStatus.DELIVERED,
                Delivery.delivered_at.isnot(None),
            )
            .scalar()
        )

        avg_delivery_minutes = round(avg_time_result / 60, 1) if avg_time_result else None

        # Average rating
        avg_rating = (
            db.session.query(func.avg(Delivery.customer_rating))
            .filter(
                Delivery.delivery_person_id == user_id,
                Delivery.updated_at >= start_date,
                Delivery.customer_rating.isnot(None),
            )
            .scalar()
        )

        success_rate = round((delivered_count / total_deliveries) * 100, 1) if total_deliveries > 0 else 0.0

        return {
            "period": period,
            "total_deliveries": total_deliveries,
            "delivered": delivered_count,
            "failed": failed_count,
            "success_rate": success_rate,
            "total_cash_collected": float(cash_sum) if cash_sum else 0.0,
            "avg_delivery_time_minutes": avg_delivery_minutes,
            "avg_rating": round(float(avg_rating), 2) if avg_rating else None,
        }

    @staticmethod
    def mark_order_preparing(order_id: int, staff_user_id: int) -> Order:
        """
        Mark an order as 'preparing'.

        Args:
            order_id: ID of the order
            staff_user_id: ID of the staff member

        Returns:
            Updated Order object

        Raises:
            NotFoundError: If order not found
            ValidationError: If order is not in a valid state for this transition
        """
        order = Order.query.get(order_id)
        if not order:
            raise NotFoundError("Order not found", error_code="STAFF_ORDER_NOT_FOUND")

        status_value = order.status.value if hasattr(order.status, "value") else order.status
        if status_value != OrderStatus.CONFIRMED.value:
            raise ValidationError(
                f"Order must be in 'confirmed' status to mark as preparing. " f"Current status: {status_value}",
                error_code="STAFF_ORDER_STATUS_INVALID_FOR_PREPARING",
            )

        order.status = OrderStatus.PREPARING
        order.updated_at = datetime.now(timezone.utc)
        delivery = Delivery.query.filter_by(order_id=order.id).first()
        if delivery and delivery.delivery_person_id:
            db.session.flush()
            StaffService.sync_active_delivery_counters([delivery.delivery_person_id])

        db.session.commit()

        # Notify customer that order entered preparing stage.
        try:
            from business_app.tasks.notification_tasks import send_order_notification_task

            send_order_notification_task.delay(order.id, "status_changed_preparing")
        except Exception as notify_exc:
            current_app.logger.warning(
                "Failed to enqueue preparing notification for order %s: %s",
                order.id,
                notify_exc,
            )

        # Log activity
        StaffService._log_activity(
            user_id=staff_user_id,
            action=STAFF_ACTIONS["ORDER_PREPARING"],
            entity_type="order",
            entity_id=order.id,
            metadata_={"old_status": status_value, "new_status": "preparing"},
        )

        current_app.logger.info(f"Order {order_id} marked as preparing by staff user {staff_user_id}")

        return order

    @staticmethod
    def create_client_user(operator_id: int, user_data: Dict[str, Any]) -> User:
        """
        Create a new customer user (used by operator from staff bot).

        Args:
            operator_id: ID of the operator creating the user
            user_data: Dict with 'phone', 'first_name', optional 'last_name'

        Returns:
            Created User object

        Raises:
            ValidationError: If required fields missing or phone already exists
        """
        phone = user_data.get("phone")
        first_name = user_data.get("first_name")

        if not phone or not first_name:
            raise ValidationError("Phone and first_name are required", error_code="STAFF_PHONE_FIRST_NAME_REQUIRED")

        # Check if phone already registered
        from business_app.utils.helpers import format_phone_number

        formatted_phone = format_phone_number(phone)
        if not formatted_phone:
            raise ValidationError("Invalid phone number format", error_code="STAFF_PHONE_INVALID")

        existing = User.query.filter_by(phone=formatted_phone).first()
        if existing:
            raise ConflictError("A user with this phone number already exists", error_code="STAFF_PHONE_EXISTS")

        # Create user with a random secure password (cannot login via password)
        import secrets
        from business_app.utils.password_security import hash_password

        random_password = secrets.token_urlsafe(32)

        user = User(
            phone=formatted_phone,
            first_name=first_name.strip(),
            last_name=user_data.get("last_name", "").strip() if user_data.get("last_name") else None,
            email=None,
            password_hash=hash_password(random_password),
            user_type=UserType.INDIVIDUAL,
            role=UserRole.CUSTOMER.value,
            status=UserStatus.ACTIVE.value,
            is_verified=False,
            registration_source="staff_bot",
            preferred_language=user_data.get("preferred_language", "uz"),
        )

        db.session.add(user)
        db.session.commit()

        # Log activity
        StaffService._log_activity(
            user_id=operator_id,
            action=STAFF_ACTIONS["USER_CREATED"],
            entity_type="user",
            entity_id=user.id,
            metadata_={"phone": formatted_phone, "created_by_operator": operator_id},
        )

        current_app.logger.info(f"Client user {user.id} created by operator {operator_id}")

        return user

    @staticmethod
    def price_phone_order(client_id: int, order_data: Dict[str, Any]) -> Dict[str, Any]:
        """🔴 THE ONE PLACE A PHONE ORDER IS PRICED. Do not grow a second one.

        Prices for the **CLIENT**, never for the caller. That distinction is the
        whole reason this function exists as a callable rather than as a loop
        inlined in :meth:`create_phone_order`:

        ``GET /api/v1/products/`` resolves ``pricing.current_price`` for whoever
        holds the JWT (``business_app/api/products.py:100-111`` ->
        ``serialize_product(..., user=current_user)``), and on the operator's
        order screen that is the OPERATOR. For a client on a corporate contract
        the operator therefore read the GENERIC price down the phone while this
        loop charged the CONTRACT price — measured 45 000 shown against 27 000
        charged (``tests/integration/test_operator_order_price_parity.py``).
        The catalogue payload also carries the caller's VIP / loyalty-tier
        discount (``product_serializers.calculate_product_price``), which this
        loop does not apply at all — a discounted operator leaked their own
        rate into every quote.

        The estimate endpoint
        (``POST /staff/operator/users/<id>/order-estimate`` ->
        :meth:`estimate_phone_order`) and :meth:`create_phone_order` both enter
        HERE. The figure quoted and the figure charged are the same expression
        by construction; two expressions that agree today desynchronise on the
        next edit.

        Returns a dict of ``items`` (the exact rows ``OrderItem`` is built from),
        ``subtotal``, ``delivery_fee`` and ``total_amount``, all ``Decimal``.

        Raises:
            NotFoundError: a requested product does not exist.
            ValidationError: contract pricing for a line is ambiguous.
        """
        from business_app.models.product import Product
        from business_app.services.corporate_contract_service import CorporateContractService

        corporate_service = CorporateContractService()
        order_items: List[Dict[str, Any]] = []
        subtotal = Decimal("0")

        for item in order_data.get("items") or []:
            product_id = item.get("product_id")
            quantity = item.get("quantity", 1)

            product = Product.query.get(product_id)
            if not product:
                raise NotFoundError(
                    f"Product {product_id} not found",
                    error_code="STAFF_PRODUCT_NOT_FOUND",
                )

            fallback_price = Decimal(str(product.calculate_price(quantity=quantity)))
            resolution = corporate_service.resolve_contract_pricing_for_user_product(
                user_id=client_id,
                product_id=product_id,
                fallback_price=fallback_price,
            )
            unit_price = Decimal(str(resolution["unit_price"]))
            item_total = unit_price * Decimal(str(quantity))
            subtotal += item_total

            order_items.append(
                {
                    "product_id": product_id,
                    "product_name": product.name,
                    "contract_id": resolution["contract"].id if resolution["contract"] else None,
                    "contract_product_price_id": (
                        resolution["contract_price_row"].id if resolution["contract_price_row"] else None
                    ),
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "total_price": item_total,
                }
            )

        delivery_fee = Decimal(str(order_data.get("delivery_fee", 0)))

        return {
            "items": order_items,
            "subtotal": subtotal,
            "delivery_fee": delivery_fee,
            "total_amount": subtotal + delivery_fee,
        }

    @staticmethod
    def estimate_phone_order(client_id: int, order_data: Dict[str, Any]) -> Dict[str, Any]:
        """READ-ONLY client-scoped quote for a phone-order basket.

        The operator screen's money surface. It writes NOTHING — no order, no
        item, no reservation, no activity log — it only replays
        :meth:`price_phone_order`, the same call :meth:`create_phone_order`
        makes, so what the operator reads out and what the customer is charged
        cannot diverge.

        Deliberately NOT re-checked here: the delivery-address ownership guard
        and the COD cap. Those decide whether an order may be PLACED, not what
        it costs, and running them on a quote would make a read-only endpoint
        reject baskets the operator is still building.
        """
        client = User.query.get(client_id)
        if not client:
            raise NotFoundError("Client user not found", error_code="STAFF_CLIENT_NOT_FOUND")

        items_data = order_data.get("items") or []
        if not items_data:
            raise ValidationError("Order must contain at least one item", error_code="STAFF_ORDER_ITEMS_REQUIRED")

        pricing = StaffService.price_phone_order(client_id, order_data)

        return {
            "client_id": client_id,
            "currency": "UZS",
            "items": [
                {
                    "product_id": row["product_id"],
                    "product_name": row["product_name"],
                    "quantity": row["quantity"],
                    "unit_price": float(row["unit_price"]),
                    "total_price": float(row["total_price"]),
                    "is_contract_price": row["contract_id"] is not None,
                }
                for row in pricing["items"]
            ],
            "subtotal": float(pricing["subtotal"]),
            "delivery_fee": float(pricing["delivery_fee"]),
            "total_amount": float(pricing["total_amount"]),
        }

    @staticmethod
    def create_phone_order(operator_id: int, client_id: int, order_data: Dict[str, Any]) -> Order:
        """
        Create an order on behalf of a client (phone order by operator).

        Args:
            operator_id: ID of the operator creating the order
            client_id: ID of the client user
            order_data: Dict with order details (items, delivery_address_id, etc.)

        Returns:
            Created Order object

        Raises:
            NotFoundError: If client not found
            ValidationError: If order data is invalid
        """
        client = User.query.get(client_id)
        if not client:
            raise NotFoundError("Client user not found", error_code="STAFF_CLIENT_NOT_FOUND")

        items_data = order_data.get("items", [])
        if not items_data:
            raise ValidationError("Order must contain at least one item", error_code="STAFF_ORDER_ITEMS_REQUIRED")

        delivery_address_id = order_data.get("delivery_address_id")

        # Five of the six order-creation paths re-check that the delivery
        # address belongs to the order's user; this one did not. The FK
        # guarantees the row exists, not who owns it, and
        # `assert_order_address_for_status` checks PRESENCE only
        # (business_app/utils/state_validators.py:101-126). Unreachable from the
        # operator's own screen — which only lists this client's addresses — but
        # reachable by a direct API call or a script, and an order carrying
        # someone else's address silently breaks place-scoped COD collection
        # (plan Q5/E16). Same predicate as
        # OrderService.get_user_and_address_for_order (order_service.py:547-553).
        # MUST run before the COD cap call below: an unowned address must never
        # seed the place arm of validate_customer_can_use_cod.
        if delivery_address_id is not None:
            owned_address = UserAddress.query.filter_by(
                id=delivery_address_id,
                user_id=client_id,
            ).first()
            if owned_address is None:
                raise ValidationError(
                    "Delivery address does not belong to this client",
                    error_code="STAFF_INVALID_DELIVERY_ADDRESS",
                )

        # Process order items. The pricing lives in `price_phone_order` — the
        # SAME call the operator's order-estimate endpoint makes, so the figure
        # quoted on the phone and the figure charged here are one expression.
        # Do NOT re-inline this loop.
        from business_app.services.corporate_contract_service import CorporateContractService

        corporate_service = CorporateContractService()
        pricing = StaffService.price_phone_order(client_id, order_data)
        order_items = pricing["items"]
        subtotal = pricing["subtotal"]
        delivery_fee = pricing["delivery_fee"]
        total_amount = pricing["total_amount"]

        # Map payment method
        payment_method = None
        payment_method_str = order_data.get("payment_method")
        if payment_method_str:
            try:
                payment_method = PaymentMethod(payment_method_str)
            except ValueError:
                pass
        if payment_method == PaymentMethod.CASH:
            from business_app.services.cash_collection_service import CashCollectionService

            # Two-armed cap (spec 5.5): the client's cluster AND the destination
            # place group. `delivery_address_id` is resolved above from
            # order_data; None / ungrouped degrades to the person arm.
            CashCollectionService().validate_customer_can_use_cod(client_id, delivery_address_id=delivery_address_id)
        if payment_method == PaymentMethod.BUSINESS_ACCOUNT:
            corporate_service.validate_business_account_order(
                user=client,
                order_items=order_items,
            )

        # Default qualifying workplace phone orders to business-account settlement
        # when the operator did not specify a method. Explicit choices respected.
        if payment_method is None and not payment_method_str:
            if corporate_service.order_qualifies_for_business_account(
                user=client,
                order_items=order_items,
            ):
                payment_method = PaymentMethod.BUSINESS_ACCOUNT

        # ARCH-006: phone orders open as CONFIRMED, so the address + staff
        # creator invariants must hold before insert.
        assert_order_creator_for_source(
            order_source="phone",
            created_by_staff_id=operator_id,
        )

        # Create order
        order = Order(
            user_id=client_id,
            status=OrderStatus.CONFIRMED,
            subtotal=subtotal,
            delivery_fee=delivery_fee,
            total_amount=total_amount,
            delivery_address_id=delivery_address_id,
            payment_method=payment_method,
            delivery_notes=order_data.get("delivery_notes"),
            order_source="phone",
            created_by_staff_id=operator_id,
        )
        assert_order_address_for_status(order, OrderStatus.CONFIRMED)

        db.session.add(order)
        db.session.flush()

        # Add order items
        for item_data in order_items:
            order_item = OrderItem(
                order_id=order.id,
                product_id=item_data["product_id"],
                contract_id=item_data.get("contract_id"),
                contract_product_price_id=item_data.get("contract_product_price_id"),
                quantity=item_data["quantity"],
                unit_price=item_data["unit_price"],
                total_price=item_data["total_price"],
            )
            db.session.add(order_item)

        # Ensure order pool/accept flows always have a delivery record.
        delivery = Delivery(
            order_id=order.id,
            delivery_person_id=None,
            status=DeliveryStatus.SCHEDULED,
            scheduled_date=order.delivery_date or datetime.now(timezone.utc),
            scheduled_time_slot=order.delivery_time_slot or "09:00-12:00",
        )
        db.session.add(delivery)

        # Reserve corporate prepayment units on order creation.
        CorporateContractService().reserve_for_order(order.id, actor_user_id=operator_id)

        db.session.commit()

        from business_app.services.payment_service import PaymentService

        PaymentService().initialize_order_payment(order.id, actor_user_id=operator_id)
        db.session.refresh(order)

        # Log activity
        StaffService._log_activity(
            user_id=operator_id,
            action=STAFF_ACTIONS["ORDER_CREATED"],
            entity_type="order",
            entity_id=order.id,
            metadata_={
                "client_id": client_id,
                "order_source": "phone",
                "total_amount": float(total_amount),
            },
        )

        current_app.logger.info(f"Phone order {order.id} created by operator {operator_id} for client {client_id}")

        return order

    @staticmethod
    def get_recent_operator_orders(operator_id: int, limit: int = 20) -> List[Order]:
        """
        Get recent orders created by an operator.

        Args:
            operator_id: Staff operator user id
            limit: Max orders to return

        Returns:
            List of Order models ordered by newest first
        """
        return (
            Order.query.options(
                joinedload(Order.user),
            )
            .filter_by(created_by_staff_id=operator_id)
            .order_by(Order.created_at.desc())
            .limit(limit)
            .all()
        )

    @staticmethod
    def add_client_address(user_id: int, address_data: Dict[str, Any]) -> UserAddress:
        """
        Add an address for a customer user.

        Args:
            user_id: Target customer user id
            address_data: Address payload from staff bot/API

        Returns:
            Created UserAddress model
        """
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found", error_code="STAFF_USER_NOT_FOUND")

        # Enforce the delivery-zone SSOT before persisting any coordinate.
        ensure_within_delivery_zone(address_data.get("latitude"), address_data.get("longitude"))

        address = UserAddress(
            user_id=user_id,
            title=address_data.get("title", "Home"),
            full_address=address_data.get("full_address", ""),
            street_address=address_data.get("street_address", ""),
            city=address_data.get("city", "Tashkent"),
            district=address_data.get("district"),
            latitude=address_data.get("latitude"),
            longitude=address_data.get("longitude"),
            delivery_instructions=address_data.get("delivery_notes", address_data.get("delivery_instructions")),
        )

        db.session.add(address)
        db.session.commit()
        return address

    @staticmethod
    def get_client_addresses(user_id: int) -> List[UserAddress]:
        """
        Return all addresses for a customer user.

        Args:
            user_id: Target customer user id

        Returns:
            List of address models
        """
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found", error_code="STAFF_USER_NOT_FOUND")

        return UserAddress.query.filter_by(user_id=user_id).all()

    @staticmethod
    def get_client_payment_methods(user_id: int, delivery_address_id: Optional[int] = None) -> Dict[str, Any]:
        """Return debt-aware payment methods for an operator-created customer order.

        ``delivery_address_id`` is optional: supply the destination address and
        the COD cap's PLACE arm is evaluated too (spec 5.5), so the operator's
        menu matches what ``create_phone_order`` will actually accept. Omitted
        (or ungrouped) ⇒ person arm only, exactly as before.
        """
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found", error_code="STAFF_USER_NOT_FOUND")

        from business_app.services.cash_collection_service import CashCollectionService
        from business_app.services.corporate_contract_service import CorporateContractService

        entity_subtype_value = (
            user.entity_subtype.value
            if user.entity_subtype is not None and hasattr(user.entity_subtype, "value")
            else user.entity_subtype
        )

        # Entity users with no subtype assigned cannot place orders. Surface a
        # clear restriction flag so admin UI can show "assign subtype first".
        if user.is_entity_user and user.normalized_entity_subtype is None:
            return {
                "customer_id": user_id,
                "entity_subtype": None,
                "available_methods": [],
                "payment_restrictions": {
                    "cod_restricted": True,
                    "cod_restriction_reason": "entity_subtype_unassigned",
                    "requires_subtype_assignment": True,
                    "active_cod_debt_count": 0,
                    "available_prepayment_balance": 0.0,
                },
                "has_business_account": False,
            }

        methods = [
            {
                "method": PaymentMethod.CASH.value,
                "name": "Cash on Delivery",
                "description": "Pay with cash when the order is delivered",
            },
            {
                "method": PaymentMethod.CLICK.value,
                "name": "Click",
                "description": "Pay with Click wallet or linked card",
            },
            {
                "method": PaymentMethod.PAYME.value,
                "name": "Payme",
                "description": "Pay with Payme wallet or linked card",
            },
        ]

        # Business Account availability — single source of truth.
        corporate_balances = CorporateContractService().get_business_account_balances(user)
        if corporate_balances:
            methods.append(
                {
                    "method": PaymentMethod.BUSINESS_ACCOUNT.value,
                    "name": "Business Account",
                    "description": "Charge the active corporate prepayment balance",
                    "is_default": True,
                }
            )

        cod_context = CashCollectionService().get_cod_restriction_context(
            user_id, delivery_address_id=delivery_address_id
        )
        # Grocery stores are exempt from the COD restriction cap (already
        # enforced in get_cod_restriction_context).
        if cod_context["cod_restricted"]:
            methods = [method for method in methods if method["method"] != PaymentMethod.CASH.value]

        return {
            "customer_id": user_id,
            "entity_subtype": entity_subtype_value,
            "available_methods": methods,
            "payment_restrictions": cod_context,
            "has_business_account": bool(corporate_balances),
        }

    @staticmethod
    def search_users(query_text: str, search_type: str = "phone") -> List[User]:
        """
        Search users by phone or name.

        Args:
            query_text: Search query
            search_type: 'phone' or 'name'

        Returns:
            List of matching User objects (max 20 results)
        """
        if not query_text or len(query_text) < 2:
            raise ValidationError(
                "Search query must be at least 2 characters", error_code="STAFF_SEARCH_QUERY_TOO_SHORT"
            )

        if search_type == "phone":
            users = (
                User.query.filter(User.phone.ilike(f"%{query_text}%"), User.role == UserRole.CUSTOMER).limit(20).all()
            )
        elif search_type == "name":
            users = (
                User.query.filter(
                    or_(User.first_name.ilike(f"%{query_text}%"), User.last_name.ilike(f"%{query_text}%")),
                    User.role == UserRole.CUSTOMER,
                )
                .limit(20)
                .all()
            )
        else:
            raise ValidationError("search_type must be 'phone' or 'name'", error_code="STAFF_SEARCH_TYPE_INVALID")

        return users

    @staticmethod
    def search_cod_collection_users(query_text: str, search_type: str = "phone") -> List[User]:
        """Search COD collection targets by phone or name without role restrictions.

        Phone search uses ILIKE substring on the canonical phone column (typing
        ``9012`` matches ``+998901234567``). Name search expands the query into
        Latin↔Cyrillic variants via the ``transliterate`` package so a staff
        member typing ``Aziz`` matches a customer stored as ``Азиз`` and vice
        versa. ILIKE is case-insensitive in PostgreSQL.
        """
        normalized_query = (query_text or "").strip()
        if not normalized_query:
            raise ValidationError(
                "Search query must be at least 2 characters", error_code="STAFF_SEARCH_QUERY_TOO_SHORT"
            )
        # Short numeric queries (1–3 digits) are unambiguously user-ID lookups
        # for direct service / admin callers. The bot's `detect_search_type`
        # routes ≥4-digit numeric input to phone substring search via the
        # `search_type='phone'` branch below, so this short-circuit only fires
        # for callers passing tiny numeric strings — matching the behavior
        # asserted by `test_cod_collection_search_accepts_single_digit_user_id_query`.
        if normalized_query.isdigit() and len(normalized_query) < 4:
            return User.query.filter(User.id == int(normalized_query)).limit(20).all()
        if len(normalized_query) < 2:
            raise ValidationError(
                "Search query must be at least 2 characters", error_code="STAFF_SEARCH_QUERY_TOO_SHORT"
            )

        if search_type == "phone":
            users = (
                User.query.filter(
                    User.phone.ilike(f"%{normalized_query}%"),
                )
                .limit(20)
                .all()
            )
        elif search_type == "name":
            # Multi-word search: tokenize on whitespace and require each token to
            # appear in *either* first_name or last_name. Lets "Donald Trump" and
            # "Trump Donald" both find a customer named Donald Trump (and shorter
            # prefixes like "Don Tr" still hit via ILIKE substring match). We do
            # this for every Latin↔Cyrillic transliteration variant so a Cyrillic
            # name in DB cross-matches a Latin query and vice versa.
            variants = StaffService._expand_name_variants(normalized_query)
            variant_clauses = []
            for variant in variants:
                tokens = [t for t in variant.split() if t]
                if not tokens:
                    continue
                token_clauses = [
                    or_(
                        User.first_name.ilike(f"%{tok}%"),
                        User.last_name.ilike(f"%{tok}%"),
                    )
                    for tok in tokens
                ]
                variant_clauses.append(and_(*token_clauses))
            if not variant_clauses:
                return []
            users = User.query.filter(or_(*variant_clauses)).limit(20).all()
        else:
            raise ValidationError("search_type must be 'phone' or 'name'", error_code="STAFF_SEARCH_TYPE_INVALID")

        return users

    @staticmethod
    def _expand_name_variants(query: str) -> List[str]:
        """Return the original query plus its Latin↔Cyrillic transliterations.

        Best-effort: when ``transliterate`` cannot detect/convert a string the
        helper silently falls back to the inputs collected so far. The GOST
        7.79-2000 scheme used by the ``transliterate`` package emits
        apostrophes for the Russian soft/hard signs (e.g. ``Дональд`` →
        ``Donal'd``) — those punctuation marks are absent from real-world
        Latin spellings of names, so each variant is also folded into an
        apostrophe-stripped form to keep matching symmetric in practice.
        """
        from transliterate import translit  # local import — keep top-level lean
        from transliterate.exceptions import LanguageDetectionError

        variants = {query}
        try:
            variants.add(translit(query, "ru"))  # Latin → Cyrillic
        except (LanguageDetectionError, Exception):  # noqa: BLE001 — best-effort
            pass
        try:
            variants.add(translit(query, "ru", reversed=True))  # Cyrillic → Latin
        except (LanguageDetectionError, Exception):  # noqa: BLE001 — best-effort
            pass
        # Strip GOST-injected punctuation (apostrophe / prime / double-prime).
        cleaned = {v.replace("'", "").replace("ʹ", "").replace("ʺ", "") for v in variants}
        variants |= cleaned
        return list(variants)

    @staticmethod
    def search_customers_for_cod_collection(
        query_text: str,
        search_type: str = "phone",
        *,
        only_with_open_cod: bool = True,
    ) -> List[Dict[str, Any]]:
        """Search users and attach COD debt summary for collection workflows.

        🔴 THE ROW IS THE COLLECT SCOPE (A6/R-A, R-B). This search feeds the
        ADMIN cash-collection modal's customer dropdown
        (``api/admin.py:12243``) as well as the staff bot, so the same rule that
        governs the driver's debtor list governs it: the figure a row advertises
        must be the figure collecting from that row settles.

        Before this, every row was built from the RAW per-account statement and
        filtered on the person's OWN ``active_cod_debt_count`` — the exact
        per-person gate Plan E R1 removed everywhere else. Two measured
        consequences on the A6 rows:

        * Alice's row read ``(2 debts, 25 000)`` where the collection settles
          ``(3, 45 000)``;
        * a debt-free coworker standing at an indebted workplace returned **zero
          rows**, so the admin could not select the very person holding the
          office's cash.

        Both halves now come from :func:`resolve_collect_scope` — the one
        decision the admin modal itself posts with. Gate OFF ⇒ the raw engine
        figures, verbatim (plan C0).
        """
        users = StaffService.search_cod_collection_users(query_text, search_type)

        from business_app.services.cash_collection_service import CashCollectionService

        cash_collection_service = CashCollectionService()
        gate_on = bool(current_app.config.get("PLACE_COD_COLLECTION_ENABLED"))
        staff_service = StaffService()
        items: List[Dict[str, Any]] = []
        for user in users:
            if gate_on:
                statement = staff_service.get_customer_cod_statement_for_admin(user.id)
                scope = statement["collect_scope"]
                debt_count = scope["debt_count"]
                outstanding = scope["amount"]
            else:
                statement = cash_collection_service.get_customer_cod_statement(user.id)
                debt_count = statement["active_cod_debt_count"]
                outstanding = statement["total_outstanding_amount"]
            if only_with_open_cod and debt_count <= 0:
                continue

            items.append(
                {
                    "id": user.id,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "phone": user.phone,
                    "address_count": len(user.addresses) if hasattr(user, "addresses") and user.addresses else 0,
                    "order_count": len(user.orders) if hasattr(user, "orders") and user.orders else 0,
                    "active_cod_debt_count": debt_count,
                    "total_outstanding_amount": outstanding,
                    "cod_restricted": statement["cod_restricted"],
                }
            )

        return items

    def get_customer_cod_statement_for_staff(self, customer_id: int) -> Dict[str, Any]:
        """The engine's COD statement, plus THE collect ceiling for each place.

        🔴 A6/R-B — THE OTHER END OF THE ROW==CEILING SEAM. Do not inline this
        arithmetic into the staff bot.

        The driver's debtor row is composed by
        :meth:`paginate_cod_debtors_for_staff` as the person's own cluster debt
        UNION their place's debt. The staff bot cannot reproduce that union from
        the raw engine statement: ``places[].place_open_cod_debt_total`` is the
        place's WHOLE debt, and nothing in the payload says how much of it the
        cluster already owns — so the bot combined the two figures with a
        ``max``, and ``max(25k, 25k, 35k)`` is 35 000 where the row (and the
        settlement) is 45 000. This method publishes the union itself, computed
        by the SAME :func:`collectible_cod_total` the row calls, so the bot has
        a figure to READ instead of one to recompose.

        ``place_collect_ceiling_amount`` is what one standalone collection
        posted by this customer AT that place can settle: ring 1 (the place, any
        owner) ∪ ring 2 (their own cluster) —
        ``cash_collection_service.py:3503-3511``. Anything above it is a genuine
        surplus, which is what makes the driver's overpayment copy true.

        🔴 A PLACE IS ONLY PUBLISHED WHEN THE ENGINE WILL HONOUR IT. The ceiling
        above is the union a PLACE-scoped post settles, so it may only be
        published for a place the engine actually resolves to PLACE scope for
        this customer. It does not for a grocery account, whose contract-mirrored
        cash is forced to personal scope — and the union was published for it
        anyway, which is instance #4 of the show-vs-settle defect (see
        :func:`place_widening_applies`, which is the one call both display halves
        now gate on).

        Gate OFF ⇒ the engine's payload is returned untouched, so the rollback
        path is a true no-op (plan C0). The engine itself is never opened
        (plan C1): ``get_customer_cod_statement`` and ``get_place_cod_statement``
        are public readers and this is a composition over them.
        """
        from business_app.services.cash_collection_service import CashCollectionService
        from business_app.services.customer_link_service import CustomerLinkService

        cash_service = CashCollectionService()
        statement = cash_service.get_customer_cod_statement(customer_id)
        if not current_app.config.get("PLACE_COD_COLLECTION_ENABLED"):
            return statement

        places = statement.get("places") or []
        if not places:
            return statement

        cluster_ids = CustomerLinkService().get_cluster_user_ids(customer_id)
        # Both halves of the person's OWN debt, cluster-wide and DELIVERED-only —
        # the identical definition the debtor row is collapsed from
        # (cash_collection_service.py:1544-1561 vs :1935-1944 / :1995).
        cluster_total = float(statement.get("cluster_delivered_outstanding_amount") or 0)
        cluster_count = int(statement.get("active_cod_debt_count") or 0)
        for place in places:
            group_id = place.get("place_group_id")
            # 🔴 ASK THE ENGINE FIRST (:func:`place_widening_applies`). A grocery
            # account is FORCED to personal scope, so a post at this very address
            # settles only its own debt; publishing a place ceiling for it would
            # advertise a coworker's debt no lap can ever pay. Publishing NOTHING
            # is how this payload already spells "degrade": both consumers —
            # `CashCollectionHandler._scoped_ceiling` and
            # :func:`resolve_collect_scope` — treat an absent ceiling as
            # "cluster-scoped, no address", dropping the figure and the address
            # TOGETHER. So the fix needs no new key and no new branch anywhere.
            if not place_widening_applies(cash_service, customer_id, place.get("address_id")):
                continue
            place_items = cash_service.get_place_cod_statement(int(group_id))["items"] if group_id else []
            ceiling, ceiling_count = collectible_cod_total(
                cluster_total=cluster_total,
                cluster_debt_count=cluster_count,
                place_items=place_items,
                cluster_user_ids=cluster_ids,
            )
            place["place_collect_ceiling_amount"] = ceiling
            place["place_collect_ceiling_debt_count"] = ceiling_count
        return statement

    def get_customer_cod_statement_for_admin(self, customer_id: int) -> Dict[str, Any]:
        """The staff statement, plus the RESOLVED ``collect_scope`` the admin
        cash-collection modal must both DISPLAY and POST.

        🔴 THIRD INSTANCE OF ONE ROOT DEFECT. The admin modal decided its two
        halves in two places that never met: it posted
        ``places[0].address_id`` (``DeliveryReports.js:1310``) ⇒ PLACE scope,
        settling ring 1 ∪ ring 2, while displaying the RAW per-account
        ``total_outstanding_amount`` (``:1361``). The route behind it
        (``api/admin.py``) called the engine's ``get_customer_cod_statement``
        directly, so ``place_collect_ceiling_amount`` — composed only in
        :meth:`get_customer_cod_statement_for_staff` — never reached the admin
        at all. Measured on the A6 rows: shown **25 000**, posted address 2,
        true ceiling **45 000**; the admin collects the 25 000 they were shown,
        Alice still owes 10 000 and 10 000 of BOB's debt is paid. With a PENDING
        order present the displayed figure was 95 000 against the same 45 000.

        The staff bot solved this with ``_scoped_ceiling`` returning the posting
        scope and the offer from one call; the admin needs the same object, so
        it is resolved server-side and published as ONE dict. The UI reads it —
        it does not recompose either half.

        WHY THIS IS A SEPARATE METHOD FROM THE STAFF ONE. The staff payload is
        pinned: gate OFF must be a verbatim pass-through with **no new fields**
        (plan C0, ``test_gate_off_leaves_the_row_and_the_ceiling_un_widened``),
        and the driver's screen resolves its place from the address the driver
        TAPPED, which no server-side derivation can stand in for. The admin has
        no tap, so its scope is derived from "exactly one grouped place" — and
        it is published in BOTH gate states, because with the gate off the modal
        must still show the figure a cluster-scoped post settles rather than the
        PENDING-inclusive per-account headline.

        The engine is never opened (plan C1) — this is a composition over public
        readers, the technique task 4 and the P0 ceiling fix both used.
        """
        statement = self.get_customer_cod_statement_for_staff(customer_id)
        statement["collect_scope"] = resolve_collect_scope(statement)
        return statement

    def paginate_cod_debtors_for_staff(self, *, page: int = 1, per_page: int = 10) -> Dict[str, Any]:
        """The driver's COD debtor list: **USER ROWS ONLY**, each carrying the
        debt of the grouped places they belong to (plan E14 / owner rule 3).

        🔴 OWNER RULING A7 — NO PLACE ROW. Verbatim: *"in staff bot there won't
        be any 'office' row in debtors list. The debtors list only shows the
        users, and the office debt is included in each coworker's debt."* The
        engine's own paginator still emits a 🏢 row per indebted group
        (``paginate_users_with_open_cod_debts``) and is left byte-identical
        (plan C1); this staff-facing composition simply does not carry that
        family through, in EITHER gate state. Emitting it here while the bot has
        no handler for ``staff_cod_place_<id>`` would ship a dead button and
        inflate the pagination block with rows the driver can never see.

        Why the removal is capability-neutral: A7/R-F — the office's debt is
        collectible THROUGH A PERSON. Half 1 puts it on every existing
        coworker's row and half 2 synthesises a row for a coworker who owes
        nothing personally, so every doorway the 🏢 row offered has a person-row
        equivalent that leads to the SAME place-scoped settlement (ring 1 ∪
        ring 2). With the gate off the 🏢 row was navigation only — its buttons
        opened each member's own statement, which the list already lists.

        WHY THIS LIVES HERE AND NOT IN CashCollectionService: the allocation
        engine is frozen for Plan E (plan C1). Every input below is an existing
        PUBLIC reader, so the rule is expressible as a composition and the
        engine stays byte-identical. Do not "simplify" this by moving it into
        cash_collection_service.py.

        The rule: a debt at a shared workplace is EVERY coworker's (plan R3), so
        it must appear on every coworker's row. A member's OWN debt is already in
        their person row, so only items whose ``owner_user_id`` falls outside
        their cluster are added; otherwise the office order they placed
        themselves would be counted twice.

        Gate off => no widening and no synthesis: the engine's own person rows,
        in the engine's own order, so the rollback path is a true money no-op.

        TWO HALVES, and half 2 is the one that makes the rule real:
          1. widen the person rows that already exist;
          2. SYNTHESISE a row for a place member who owes nothing personally.
             Without (2) a driver who meets Bob, when only Alice owes, has no
             Bob row to tap and cannot collect the office's debt from him --
             which is the exact scenario R3 exists for.
        """
        from business_app.services.cash_collection_service import CashCollectionService

        cash_service = CashCollectionService()
        safe_page = max(1, int(page or 1))
        safe_per_page = max(1, min(int(per_page or 10), 100))

        # The same person-row set, in the same order, that the engine's own
        # paginator builds (cash_collection_service.py:1815) — one public reader,
        # so gate-on and gate-off can never disagree about who is on the list.
        person_rows = cash_service.list_users_with_open_cod_debts(limit=1000)
        if not current_app.config.get("PLACE_COD_COLLECTION_ENABLED"):
            return self._paginate_rows(person_rows, safe_page, safe_per_page)

        # NOT a list row any more (A7) — purely the set of indebted groups whose
        # debt the person rows must absorb. limit MUST match the engine's default
        # (cash_collection_service.py:1816) or the widened set would diverge from
        # the engine's own place set above 200 groups.
        place_rows = cash_service.get_place_cod_debtor_rows()

        # One statement fetch per indebted group, reused across every member.
        statements = {
            int(r["place_group_id"]): cash_service.get_place_cod_statement(int(r["place_group_id"])) for r in place_rows
        }
        if statements:
            groups_by_user = self._address_group_ids_by_user(list(statements.keys()))

            # ---- ONE definition of "person", shared by BOTH halves ---------
            # (invariant 3c) An earlier draft discovered half 1's groups through
            # row["member_user_ids"], which holds only the cluster members that
            # carry their OWN debt. A person whose only group-owning account is
            # the debt-free sibling therefore discovered no group in half 1 AND
            # was skipped as "already covered" in half 2, and the office's debt
            # vanished for them. Both halves now ask the same question.
            place_member_ids = {int(uid) for uid in groups_by_user}
            row_member_ids = {int(m) for r in person_rows for m in (r.get("member_user_ids") or [r["id"]])}
            all_ids = sorted(place_member_ids | row_member_ids)
            # Same ("c"/"u") key shape as the engine's own collapse
            # (cash_collection_service.py:1766-1768), so a person the engine
            # collapses into one row is one person here too. Plain FK select on
            # `users` -- C9 does not apply (no orders/payments join).
            canonical_by_user: Dict[int, Any] = {}
            if all_ids:
                canonical_by_user = {
                    int(r[0]): r[1]
                    for r in db.session.query(User.id, User.canonical_customer_id).filter(User.id.in_(all_ids)).all()
                }

            def _cluster_key(uid: int):
                canonical = canonical_by_user.get(int(uid))
                return ("c", int(canonical)) if canonical is not None else ("u", int(uid))

            place_members_by_key: Dict[Any, List[int]] = {}
            for uid in sorted(place_member_ids):
                place_members_by_key.setdefault(_cluster_key(uid), []).append(uid)

            # ---- the E7 seam: never advertise a total the collect flow refuses
            # A person row is a doorway to ONE screen, and that screen resolves
            # ONE place. `get_customer_cod_statement` puts every grouped address
            # of the cluster into `places` (cash_collection_service.py:1948-1952)
            # and `_resolve_scope_address_id` returns None whenever there is more
            # than one — since A7 removed the place screen, nothing can name a
            # place for the driver any more, so two places is always ambiguous.
            # With no address `_resolved_place` yields 0.0: the ceiling collapses
            # back to the person's own cluster debt, and a debt-free member gets
            # no Collect button at all. So for a cluster owning two or more
            # places we widen NOTHING and synthesise NOTHING. This is decision E7
            # ("ambiguity must not be guessed"), applied one screen earlier.
            #
            # ⚠️ A7 CONSEQUENCE, RECORDED RATHER THAN PAPERED OVER. This guard
            # used to be free: the 🏢 place row was still a route to that debt,
            # and tapping it set the place explicitly. That row is gone, so a
            # place ALL of whose members own a second grouped address is no
            # longer collectible from the staff bot at all — it must be settled
            # from the admin surface (`get_customer_cod_statement_for_admin`,
            # which resolves its own scope) or the second grouping removed.
            # Widening anyway would re-open the exact show-vs-settle split A6
            # and A7 exist to close, so the guard stays.
            owned_groups_by_key = self._owned_place_group_ids_by_cluster(canonical_by_user)

            def _place_is_unambiguous(key) -> bool:
                return len(owned_groups_by_key.get(key, ())) == 1

            # 🔴 ONE GATE, BOTH HALVES. `_place_items` is the only door either
            # half reaches a place's debt through, so gating it here is what
            # makes "never advertise a total the collect flow refuses" true by
            # construction rather than by two call sites remembering to ask.
            widening_gate: Dict[tuple, bool] = {}

            def _may_widen(customer_id: int, address_id: int) -> bool:
                """Memoised :func:`place_widening_applies` — the debtor list asks
                the same (person, place) question once per row and once per
                synthesised row, and each answer costs the engine a handful of
                SELECTs."""
                key = (int(customer_id), int(address_id))
                if key not in widening_gate:
                    widening_gate[key] = place_widening_applies(cash_service, customer_id, address_id)
                return widening_gate[key]

            def _place_items(customer_id: int, cluster: set) -> List[Dict[str, Any]]:
                """Every open-debt item at the indebted places this cluster
                belongs to, each group visited once — restricted to the places
                the ENGINE will actually settle from ``customer_id``.

                ``customer_id`` is the account the row is KEYED on, i.e. the one
                the collect flow posts as ``customer_id``, so the question asked
                here is byte-for-byte the one ``post_collection`` answers. It is
                not a property of the cluster: grocery-ness lives on the account
                (``User.is_grocery_store``), so a grocery member and an
                individual sibling of one person get different answers, and each
                row must ask for itself.
                """
                items: List[Dict[str, Any]] = []
                seen_groups = set()
                for member_id in sorted(cluster):
                    for group_id, address_id in (groups_by_user.get(member_id) or {}).items():
                        if group_id in seen_groups or group_id not in statements:
                            continue
                        seen_groups.add(group_id)
                        if not _may_widen(customer_id, address_id):
                            continue
                        items.extend(statements[group_id]["items"])
                return items

            def _foreign(customer_id: int, cluster: set) -> tuple:
                """(amount, count) of debt at this cluster's groups that is NOT
                already theirs — :func:`collectible_cod_total` with an empty own
                half. Excluding `owner_user_id in cluster` is the whole
                anti-double-count argument: a member's own office order is
                ALREADY in their person row (it is their Payment.user_id).

                Returns ``(0.0, 0)`` for an account the engine forces to personal
                scope, because `_place_items` hands it nothing — which is exactly
                what drops a grocery's synthesised row (half 2 skips a zero)."""
                return collectible_cod_total(
                    cluster_total=0.0,
                    cluster_debt_count=0,
                    place_items=_place_items(customer_id, cluster),
                    cluster_user_ids=cluster,
                )

            # ---- half 1: widen the rows that exist -------------------------
            for row in person_rows:
                if not _place_is_unambiguous(_cluster_key(row["id"])):
                    continue
                seed = {int(m) for m in (row.get("member_user_ids") or [row["id"]])}
                # The FULL cluster, not just the accounts carrying debt: a
                # debt-free linked sibling may be the ONLY account that OWNS the
                # office address, and without them this row discovers no group.
                cluster = set(seed)
                for uid in seed:
                    cluster.update(place_members_by_key.get(_cluster_key(uid), ()))
                # 🔴 A6/R-B: THE SAME FUNCTION the staff bot's collect ceiling
                # calls, via `get_customer_cod_statement_for_staff`. The row and
                # the ceiling are one calculation, not two that agree — two
                # agreeing expressions (a union here, a `max` there) is exactly
                # what shipped 45 000 on the row and refused it in the flow.
                row["total_outstanding_amount"], row["active_cod_debt_count"] = collectible_cod_total(
                    cluster_total=float(row["total_outstanding_amount"]),
                    cluster_debt_count=int(row["active_cod_debt_count"]),
                    place_items=_place_items(row["id"], cluster),
                    cluster_user_ids=cluster,
                )

            # ---- half 2: give the debt-free coworkers a row ----------------
            # Same `_cluster_key`, so a sibling half 1 has just widened through
            # is `covered` here and does NOT also get a row of her own.
            covered_keys = {_cluster_key(uid) for uid in row_member_ids}
            person_rows.extend(
                self._synthesise_debt_free_place_member_rows(
                    cash_service,
                    {
                        k: ids
                        for k, ids in place_members_by_key.items()
                        if k not in covered_keys and _place_is_unambiguous(k)
                    },
                    _foreign,
                )
            )

            # Widening AND synthesis move people up the list; re-sort ONCE,
            # after both, so the coworker holding the office's cash is not
            # stranded on page 3.
            person_rows.sort(key=lambda r: r["total_outstanding_amount"], reverse=True)

        return self._paginate_rows(person_rows, safe_page, safe_per_page)

    @staticmethod
    def _paginate_rows(rows: List[Dict[str, Any]], page: int, per_page: int) -> Dict[str, Any]:
        """In-memory page slice in the engine's own shape
        (``cash_collection_service.py:1818-1830``), shared by both gate states so
        ``total``/``pages`` can never describe a different list than ``items``.
        Inputs are already clamped by the caller."""
        total = len(rows)
        pages = (total + per_page - 1) // per_page
        start = (page - 1) * per_page
        return {
            "items": rows[start : start + per_page],
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": pages,
            },
        }

    @staticmethod
    def _address_group_ids_by_user(group_ids: List[int]) -> Dict[int, Dict[int, int]]:
        """``{user_id: {address_group_id: address_id}}`` for the given groups.

        The ADDRESS rides along with the group because the widening gate
        (:func:`place_widening_applies`) asks the engine what scope a post at a
        concrete address resolves to — ``resolve_allocation_scope`` takes a
        ``delivery_address_id``, not a group. Carrying it here keeps that one
        extra column on the query the caller already issues instead of adding a
        second round trip. Iterating a value still yields group ids, which is all
        the arithmetic below needs.

        A user may own several addresses in one group; the lowest id wins, so the
        gate asks the same question twice for the same world.

        `addresses` has a SINGLE FK to `users`, so this join is unambiguous —
        the multi-FK gotcha (plan C9) applies to `orders`/`payments`, not here.
        """
        rows = (
            db.session.query(UserAddress.user_id, UserAddress.address_group_id, UserAddress.id)
            .filter(UserAddress.address_group_id.in_(group_ids))
            .distinct()
            .all()
        )
        out: Dict[int, Dict[int, int]] = {}
        for user_id, group_id, address_id in rows:
            by_group = out.setdefault(int(user_id), {})
            existing = by_group.get(int(group_id))
            if existing is None or int(address_id) < existing:
                by_group[int(group_id)] = int(address_id)
        return out

    @staticmethod
    def _owned_place_group_ids_by_cluster(canonical_by_user: Dict[int, Any]) -> Dict[Any, set]:
        """``{cluster_key: {address_group_id, ...}}`` — every place a cluster
        OWNS an address in, indebted or not.

        This is deliberately a DIFFERENT question from
        :meth:`_address_group_ids_by_user`, which answers "which of the INDEBTED
        groups does this account own an address in?" and feeds the arithmetic.
        This one answers "how many places will the driver's next screen have to
        choose between?", and the screen counts them ALL: `statement["places"]`
        is built from every grouped address of the cluster with no debt filter
        (``cash_collection_service.py:1948-1952``). A single debt-free second
        group is therefore enough to make the place ambiguous, so counting only
        the indebted ones here would silently re-open the mismatch the caller's
        guard exists to close.

        THE CLUSTER IS EXPANDED TO ITS FULL MEMBERSHIP FIRST. `canonical_by_user`
        covers only the accounts already in play — the debtors and the members of
        indebted groups — but the statement is built over
        ``CustomerLinkService.get_cluster_user_ids`` (`:1934`), i.e. every account
        sharing the canonical customer. A linked sibling who owns an address in a
        second, debt-free group appears in neither input set and would be missed,
        yet she is exactly what makes her own person's screen ambiguous.

        `users` → `addresses` is a SINGLE FK, so this join is unambiguous; the
        multi-FK gotcha (plan C9) is about `orders`/`payments`.
        """
        seeds = sorted(int(uid) for uid in canonical_by_user)
        if not seeds:
            return {}
        conditions = [User.id.in_(seeds)]
        canonicals = sorted({int(c) for c in canonical_by_user.values() if c is not None})
        if canonicals:
            conditions.append(User.canonical_customer_id.in_(canonicals))

        rows = (
            db.session.query(User.id, User.canonical_customer_id, UserAddress.address_group_id)
            .join(UserAddress, UserAddress.user_id == User.id)
            .filter(UserAddress.address_group_id.isnot(None), or_(*conditions))
            .distinct()
            .all()
        )
        out: Dict[Any, set] = {}
        for user_id, canonical, group_id in rows:
            # Same ("c"/"u") shape as the caller's `_cluster_key` and the
            # engine's own collapse (cash_collection_service.py:1766-1768).
            key = ("c", int(canonical)) if canonical is not None else ("u", int(user_id))
            out.setdefault(key, set()).add(int(group_id))
        return out

    @staticmethod
    def _synthesise_debt_free_place_member_rows(cash_service, pending, foreign) -> List[Dict[str, Any]]:
        """Rows for coworkers who owe NOTHING of their own at an indebted place.

        Owner rule 3 is "the debt is ALL the coworkers' debt". A coworker with no
        personal debt never enters `_open_cod_debtors_query`
        (cash_collection_service.py:1552-1561 filters on their OWN
        Payment.outstanding_amount > 0), so widening alone leaves them off the
        list entirely -- and a driver standing in front of them cannot collect
        the office's debt. This is the row that makes them tappable.

        `pending` is ``{cluster_key: [user_id, ...]}`` -- the place members whose
        cluster carries NO existing person row, already collapsed with the SAME
        `_cluster_key` the caller widens half 1 with (invariant 3c). Two
        debt-free members of one person therefore produce ONE row, and a
        debt-free sibling of an existing debtor produces NONE because half 1 has
        already put that place's debt on the sibling's row. `foreign` is half 1's
        own ``(customer_id, cluster) -> (amount, count)`` reducer, PASSED IN
        rather than re-implemented here, so the two halves can never disagree
        about what a place owes — nor about whether the engine will settle it.

        Their statement is already correct once tapped: Task 3's widened
        can_collect and ceiling use place_open_cod_debt_total, and
        resolve_allocation_scope grants PLACE scope because they genuinely own
        an address in the group (cash_collection_service.py:621) — unless it
        refuses them one, which is why `foreign` asks it. An account forced to
        personal scope reduces to 0 here and gets no row: offering a doorway to a
        settlement the engine will not perform is the whole defect this list has
        now been fixed for four times.
        """
        if not pending:
            return []

        missing_ids = sorted({uid for ids in pending.values() for uid in ids})
        # Plain FK select on `users` — C9 does not apply (no orders/payments join).
        users = {u.id: u for u in User.query.filter(User.id.in_(missing_ids)).all()}
        flags = cash_service.get_cod_restricted_flags(missing_ids)

        synthesised: List[Dict[str, Any]] = []
        for cluster_ids in pending.values():
            identity = users.get(cluster_ids[0])
            if identity is None:
                continue
            # `cluster_ids[0]` is the account this row will be KEYED on, so it is
            # the account the collect flow posts and therefore the one `foreign`
            # must ask the engine about (invariant 3c: both halves ask the same
            # question of the same person).
            amount, count = foreign(int(identity.id), set(cluster_ids))
            # Normally non-zero (an indebted group has items and none is theirs),
            # but a zero-debt entry on a DEBTORS list would be noise — and it is
            # exactly zero for an account the engine forces to personal scope, so
            # a grocery who owes nothing personally is never offered a coworker's
            # debt it cannot settle.
            if count == 0 or amount <= 0:
                continue
            role_value = identity.role.value if hasattr(identity.role, "value") else identity.role
            type_value = identity.user_type.value if hasattr(identity.user_type, "value") else identity.user_type
            synthesised.append(
                {
                    # EXACTLY the shape _serialize_open_cod_debtor_row emits
                    # (cash_collection_service.py:1581-1594) plus the two collapse
                    # keys. `id` in particular is what makes the row RENDERABLE:
                    # DeliveryKeyboards.cod_debtor_list skips any row without one
                    # (staff_bot/keyboards/delivery.py:247) — a row missing it is
                    # silently invisible to the driver.
                    "id": int(identity.id),
                    "first_name": identity.first_name,
                    "last_name": identity.last_name,
                    "phone": identity.phone,
                    "role": role_value,
                    "user_type": type_value,
                    "active_cod_debt_count": count,
                    "total_outstanding_amount": float(amount),  # C6: float on the wire
                    "cod_restricted": bool(flags.get(int(identity.id), False)),
                    "row_type": "person",
                    "member_user_ids": sorted(int(i) for i in cluster_ids),
                    # ⚠️ SAME key, SAME type, DIFFERENT population -- deliberately.
                    # On an engine row this counts the cluster's DEBTOR accounts
                    # (cash_collection_service.py:1786-1788); this cluster HAS none
                    # (that is why the row is synthesised), so it counts its PLACE
                    # MEMBERS. Both are honest for the only consumer, the 👥xN
                    # marker (staff_bot/keyboards/delivery.py:253-259) = "one person,
                    # several phone accounts". Do NOT "make them match": 0 or 1 here
                    # drops the marker for a real two-account human. Invariant 3b.
                    "cluster_member_count": len(cluster_ids),
                }
            )
        return synthesised

    @staticmethod
    def get_staff_overview() -> Dict[str, Any]:
        """
        Get dashboard overview data for staff.

        Returns:
            Dict with overview metrics
        """
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Today's order counts
        orders_today = Order.query.filter(Order.created_at >= today_start).count()

        pending_orders = Order.query.filter(Order.status.in_([OrderStatus.PENDING, OrderStatus.CONFIRMED])).count()

        preparing_orders = Order.query.filter(Order.status == OrderStatus.PREPARING).count()

        # Delivery stats
        active_deliveries = Delivery.query.filter(
            Delivery.status.in_(
                [
                    DeliveryStatus.ASSIGNED,
                    DeliveryStatus.PICKED_UP,
                    DeliveryStatus.IN_TRANSIT,
                    DeliveryStatus.ARRIVED,
                ]
            )
        ).count()

        unassigned_deliveries = Delivery.query.filter(
            Delivery.delivery_person_id.is_(None),
            Delivery.status.in_([DeliveryStatus.SCHEDULED, DeliveryStatus.PENDING]),
        ).count()

        deliveries_today = Delivery.query.filter(
            Delivery.status == DeliveryStatus.DELIVERED, Delivery.delivered_at >= today_start
        ).count()

        # Active delivery persons
        active_drivers = (
            DeliveryPerson.query.join(User, DeliveryPerson.user_id == User.id)
            .filter(DeliveryPerson.is_active == True, User.status == UserStatus.ACTIVE)  # noqa: E712
            .count()
        )

        return {
            "orders_today": orders_today,
            "pending_orders": pending_orders,
            "preparing_orders": preparing_orders,
            "active_deliveries": active_deliveries,
            "unassigned_deliveries": unassigned_deliveries,
            "deliveries_completed_today": deliveries_today,
            "active_drivers": active_drivers,
        }

    @staticmethod
    def mute_notifications(user_id: int, muted: bool) -> bool:
        """
        Toggle notification muting for a delivery person.

        Args:
            user_id: User ID of the delivery person
            muted: True to mute, False to unmute

        Returns:
            True if updated successfully

        Raises:
            NotFoundError: If delivery person profile not found
        """
        dp = DeliveryPerson.query.filter_by(user_id=user_id).first()
        if not dp:
            raise NotFoundError("Delivery person profile not found", error_code="STAFF_DELIVERY_PERSON_NOT_FOUND")

        dp.notifications_muted = muted
        db.session.commit()

        current_app.logger.info(
            f"Notifications {'muted' if muted else 'unmuted'} for delivery person user_id={user_id}"
        )

        return True

    @staticmethod
    def _log_activity(
        user_id: int, action: str, entity_type: str = None, entity_id: int = None, metadata_: Dict[str, Any] = None
    ):
        """
        Log a staff activity to StaffActivityLog.

        Args:
            user_id: ID of the staff user
            action: Action type string
            entity_type: Type of entity ('order', 'delivery', 'user')
            entity_id: ID of the entity
            metadata_: Additional context data
        """
        try:
            log_entry = StaffActivityLog(
                user_id=user_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                metadata_=metadata_ or {},
            )
            db.session.add(log_entry)
            db.session.commit()
        except Exception:
            current_app.logger.exception("Failed to log staff activity")
            # Don't fail the main operation due to logging failure
            db.session.rollback()
