"""Canonical user-type helpers."""

from typing import Any, Iterable, Optional

from shared.enums import UserRole, UserType
from shared.enums import EntitySubtype

LEGACY_ENTITY_BUSINESS_TYPES = {
    "business",
    "corporation",
    "small_business",
    "non_profit",
    "government",
}

STAFF_ROLE_VALUES = {
    UserRole.ADMIN.value,
    UserRole.MANAGER.value,
    UserRole.OPERATOR.value,
    UserRole.DELIVERY_DRIVER.value,
}

VALID_USER_TYPE_VALUES = (
    UserType.INDIVIDUAL.value,
    UserType.ENTITY.value,
    UserType.STAFF.value,
)


def _normalize_enum_or_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "value"):
        return str(value.value).strip().lower()
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized or None
    return str(value).strip().lower() or None


def _normalize_staff_roles(staff_roles: Any) -> list[str]:
    if not staff_roles:
        return []
    if isinstance(staff_roles, str):
        return [staff_roles.strip().lower()] if staff_roles.strip() else []
    if isinstance(staff_roles, Iterable):
        normalized = []
        for role in staff_roles:
            normalized_role = _normalize_enum_or_string(role)
            if normalized_role and normalized_role not in normalized:
                normalized.append(normalized_role)
        return normalized
    return []


def normalize_user_type(
    value: Any,
    *,
    role: Any = None,
    staff_roles: Any = None,
    legacy_business_type: Any = None,
) -> str:
    """Resolve canonical user type from stored value plus legacy signals."""
    normalized_value = _normalize_enum_or_string(value)
    normalized_role = _normalize_enum_or_string(role)
    normalized_staff_roles = _normalize_staff_roles(staff_roles)
    normalized_legacy_business_type = _normalize_enum_or_string(legacy_business_type)

    if normalized_value in VALID_USER_TYPE_VALUES:
        return normalized_value
    if normalized_role in STAFF_ROLE_VALUES or any(role in STAFF_ROLE_VALUES for role in normalized_staff_roles):
        return UserType.STAFF.value
    if normalized_legacy_business_type in LEGACY_ENTITY_BUSINESS_TYPES:
        return UserType.ENTITY.value
    return UserType.INDIVIDUAL.value


def is_entity_user_type(
    value: Any,
    *,
    role: Any = None,
    staff_roles: Any = None,
    legacy_business_type: Any = None,
) -> bool:
    return (
        normalize_user_type(
            value,
            role=role,
            staff_roles=staff_roles,
            legacy_business_type=legacy_business_type,
        )
        == UserType.ENTITY.value
    )


def is_staff_user_type(
    value: Any,
    *,
    role: Any = None,
    staff_roles: Any = None,
    legacy_business_type: Any = None,
) -> bool:
    return (
        normalize_user_type(
            value,
            role=role,
            staff_roles=staff_roles,
            legacy_business_type=legacy_business_type,
        )
        == UserType.STAFF.value
    )


def infer_non_staff_user_type(value: Any) -> str:
    """Normalize admin/customer-facing input without allowing staff promotion."""
    normalized = _normalize_enum_or_string(value)
    if normalized in LEGACY_ENTITY_BUSINESS_TYPES or normalized == UserType.ENTITY.value:
        return UserType.ENTITY.value
    return UserType.INDIVIDUAL.value


VALID_ENTITY_SUBTYPE_VALUES = (
    EntitySubtype.WORKPLACE.value,
    EntitySubtype.GROCERY_STORE.value,
)


def normalize_entity_subtype(value: Any) -> Optional[str]:
    """Coerce input into a canonical entity_subtype string or None."""
    normalized = _normalize_enum_or_string(value)
    if normalized in VALID_ENTITY_SUBTYPE_VALUES:
        return normalized
    return None


def is_grocery_store_subtype(value: Any) -> bool:
    return normalize_entity_subtype(value) == EntitySubtype.GROCERY_STORE.value


def is_workplace_subtype(value: Any) -> bool:
    return normalize_entity_subtype(value) == EntitySubtype.WORKPLACE.value
