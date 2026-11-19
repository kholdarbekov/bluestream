"""The one self-decision rule: a non-admin may not decide about their own money.

Sales pay (D-Q11) and driver expense claims both call it, each with its own error code, so the
two cannot drift. The role is read from the users row (`User.is_admin`), never from
`g.current_user_role`, whose type differs between decorators.
"""

from typing import Any, Dict, Optional

from business_app.models.user import User
from business_app.utils.exceptions import ForbiddenError


def is_self(subject_user_id: int, actor_user_id: Optional[int]) -> bool:
    """The one spelling of "a decision about the actor's own money". The write guard, its check-mode
    twin and the statement tag (§4.13) all call it. A system write has no actor and is never self."""
    return actor_user_id is not None and int(subject_user_id) == int(actor_user_id)


def self_decision_refused(subject_user_id: int, actor: User) -> bool:
    """The guard in check mode: True when `actor` may NOT decide about `subject_user_id`, i.e. a
    non-admin deciding about themselves. `check_self_decision` raises exactly when this is True, and
    the admin pages publish `can_decide` from it, so the button and the refusal cannot disagree.
    """
    return is_self(subject_user_id, actor.id) and not actor.is_admin


def check_self_decision(
    subject_user_id: int,
    actor: User,
    *,
    error_code: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
) -> bool:
    """Refuse a non-admin deciding about themselves with the caller's `error_code`.

    An admin may decide about themselves: True comes back, and the caller audits it. Any other
    case returns False.
    """
    if self_decision_refused(subject_user_id, actor):
        raise ForbiddenError(message, error_code=error_code, details=details)
    return is_self(subject_user_id, actor.id)
