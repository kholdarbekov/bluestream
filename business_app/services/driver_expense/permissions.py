"""Who reviews driver expense claims (spec 2026-11-17 §6.8): one rule, read everywhere.

Every `/admin/driver-expenses` route guards with `validate_admin_action(list(DRIVER_EXPENSE_REVIEW_PERMISSIONS))`,
and `AuthService.get_user_permissions` publishes `can_review_driver_expenses` from `has_admin_permission` over the same
tuple, so the admin UI's page and badge can never disagree with the routes. MANAGER holds the permission; ADMIN holds
it through the "all" wildcard; OPERATOR does not.

No imports beyond typing: `auth_service` imports this module.
"""

from typing import Tuple

DRIVER_EXPENSE_REVIEW_PERMISSIONS: Tuple[str, ...] = ("review_driver_expenses",)
