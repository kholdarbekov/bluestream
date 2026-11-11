"""Who reviews and resolves shared-place cases (spec 2026-11-10 §4.7): one rule, read everywhere.

Every `/admin/shared-place-cases` route guards with `validate_admin_action(list(SHARED_PLACE_REVIEW_PERMISSIONS))`,
and `AuthService.get_user_permissions` publishes `can_review_shared_places` from `has_admin_permission` over the same
tuple, so the admin UI's tab and badge can never disagree with the routes. Today only ADMIN holds `manage_users`.

No imports beyond typing: `auth_service` imports this module.
"""

from typing import Tuple

SHARED_PLACE_REVIEW_PERMISSIONS: Tuple[str, ...] = ("manage_users",)
