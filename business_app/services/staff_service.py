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
from business_app.utils.exceptions import ValidationError, NotFoundError, ForbiddenError, ConflictError
from business_app.utils.geo_validation import ensure_within_delivery_zone
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
    def get_active_delivery_statuses() -> Tuple[DeliveryStatus, ...]:
        """Return statuses counted as active driver workload."""
        return StaffService.ACTIVE_DELIVERY_STATUSES

    @staticmethod
    def get_cod_collection_projection(order: Optional[Order]) -> Dict[str, float]:
        """Compute COD cash collection projection for driver-facing workflows."""
        total_amount = Decimal(str(getattr(order, "total_amount", 0) or 0))
        if not order:
            return {
                "cod_reserved_prepayment_amount": 0.0,
                "expected_cash_to_collect": float(total_amount),
            }

        payment_method = order.payment_method.value if hasattr(order.payment_method, "value") else order.payment_method
        if payment_method != PaymentMethod.CASH.value:
            return {
                "cod_reserved_prepayment_amount": 0.0,
                "expected_cash_to_collect": float(total_amount),
            }

        payment = getattr(order, "payment", None)
        raw_outstanding_amount = getattr(payment, "outstanding_amount", None)
        if raw_outstanding_amount is None:
            outstanding_amount = total_amount
        else:
            outstanding_amount = Decimal(str(raw_outstanding_amount))
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

        if is_cash_order or settle_electronic_as_cash:
            from business_app.services.cash_collection_service import CashCollectionService

            cash_collection_service = CashCollectionService()
            if is_cash_order:
                # Pre-count only for true CASH orders; we use it for the debt-limit
                # breach notification below (which must not fire for just-converted orders).
                pre_cod_debt_count = cash_collection_service.get_active_cod_debt_count(delivery.order.user_id)

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
        if new_status in ("picked_up", "in_transit", "arrived", "delivered", "failed"):
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
            if new_status == "delivered" and (is_cash_order or settle_electronic_as_cash) and cash_collection_service:
                # For unsuccessful electronic orders, convert to CASH first so the
                # existing delivery_completion collection can post against a CASH payment.
                if settle_electronic_as_cash:
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
                    post_cod_debt_count = cash_collection_service.get_active_cod_debt_count(delivery.order.user_id)
                    cod_debt_limit_breached = (
                        pre_cod_debt_count is not None and pre_cod_debt_count < 2 and post_cod_debt_count >= 2
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

        # Re-optimize the driver's remaining stops from the new origin.
        # ARRIVED and DELIVERED both happen at the delivery destination, so
        # the optimizer should pick up where the driver actually is now.
        if new_status in ("arrived", "delivered") and delivery.delivery_person_id:
            try:
                from business_app.tasks.delivery_tasks import optimize_driver_route_task

                trigger = "arrival" if new_status == "arrived" else "delivery"
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

            from business_app.services.notification_service import NotificationService
            from business_app.utils.constants import NotificationChannel, NotificationType

            template = SimpleNamespace(
                subject="Cash on delivery is restricted",
                content=(
                    "You have 2 outstanding cash on delivery debts. "
                    "Cash on delivery is now unavailable for new orders. "
                    "Please use card payment methods until your outstanding COD debts are settled."
                ),
                get_translated=lambda field_name, _language: (
                    "Cash on delivery is restricted"
                    if field_name == "subject"
                    else (
                        "You have 2 outstanding cash on delivery debts. "
                        "Cash on delivery is now unavailable for new orders. "
                        "Please use card payment methods until your outstanding COD debts are settled."
                    )
                ),
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
    def update_delivery_location(delivery_id: int, lat: float, lng: float) -> Delivery:
        """
        Update delivery and delivery person's current location.

        Args:
            delivery_id: ID of the delivery
            lat: Latitude
            lng: Longitude

        Returns:
            Updated Delivery object

        Raises:
            NotFoundError: If delivery not found
            ValidationError: If coordinates are invalid
        """
        if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            raise ValidationError("Invalid coordinates", error_code="STAFF_INVALID_COORDINATES")

        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            raise NotFoundError("Delivery not found", error_code="STAFF_DELIVERY_NOT_FOUND")

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

        # Process order items
        from business_app.models.product import Product
        from business_app.services.corporate_contract_service import CorporateContractService

        corporate_service = CorporateContractService()
        order_items = []
        subtotal = Decimal("0")

        for item in items_data:
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
                    "contract_id": resolution["contract"].id if resolution["contract"] else None,
                    "contract_product_price_id": (
                        resolution["contract_price_row"].id if resolution["contract_price_row"] else None
                    ),
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "total_price": item_total,
                }
            )

        # Calculate delivery fee
        delivery_fee = Decimal(str(order_data.get("delivery_fee", 0)))
        total_amount = subtotal + delivery_fee

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

            CashCollectionService().validate_customer_can_use_cod(client_id)
        if payment_method == PaymentMethod.BUSINESS_ACCOUNT:
            corporate_service.validate_business_account_order(
                user=client,
                order_items=order_items,
            )

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
    def get_client_payment_methods(user_id: int) -> Dict[str, Any]:
        """Return debt-aware payment methods for an operator-created customer order."""
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
                }
            )

        cod_context = CashCollectionService().get_cod_restriction_context(user_id)
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
        """Search users and attach COD debt summary for collection workflows."""
        users = StaffService.search_cod_collection_users(query_text, search_type)

        from business_app.services.cash_collection_service import CashCollectionService

        cash_collection_service = CashCollectionService()
        items: List[Dict[str, Any]] = []
        for user in users:
            statement = cash_collection_service.get_customer_cod_statement(user.id)
            if only_with_open_cod and statement["active_cod_debt_count"] <= 0:
                continue

            items.append(
                {
                    "id": user.id,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "phone": user.phone,
                    "address_count": len(user.addresses) if hasattr(user, "addresses") and user.addresses else 0,
                    "order_count": len(user.orders) if hasattr(user, "orders") and user.orders else 0,
                    "active_cod_debt_count": statement["active_cod_debt_count"],
                    "total_outstanding_amount": statement["total_outstanding_amount"],
                    "cod_restricted": statement["cod_restricted"],
                }
            )

        return items

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
