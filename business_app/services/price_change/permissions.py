"""Who views, manages and cancels price changes (spec 2026-11-16 §5.2): one rule, read everywhere.

Every `/admin/price-changes` route guards with `validate_admin_action` over one of these tuples, the row actions
(`schedule.create_action` / `row_actions`) ask `has_admin_permission` over the same tuples, and
`AuthService.get_user_permissions` publishes `can_view_price_changes`, `can_manage_price_changes` and
`can_cancel_announced_price_changes` from them, so a button can never promise what its route refuses.

No imports beyond typing: `auth_service` imports this module.
"""

from typing import Tuple

# ADMIN, MANAGER and OPERATOR read.
PRICE_CHANGE_VIEW_PERMISSIONS: Tuple[str, ...] = ("view_products", "manage_products")
# ADMIN and MANAGER schedule, edit and cancel before the announcement.
PRICE_CHANGE_MANAGE_PERMISSIONS: Tuple[str, ...] = ("manage_products",)
# Held by no role: only the ADMIN wildcard "all" satisfies it.
PRICE_CHANGE_CANCEL_ANNOUNCED_PERMISSIONS: Tuple[str, ...] = ("cancel_announced_price_change",)
