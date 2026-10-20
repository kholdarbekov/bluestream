"""One session-revocation rule (spec 2026-10-19 R9 and R8e; owner decisions R9b and R9c, 2026-10-20).

A password reset, the phone-link merge that moves a website password onto the phone
holder, and a change of the account's roles end every existing login of the account at once,
through `revoke_all`. It sets the account's cut-off in Redis (`RedisKeyspace.tokens_valid_after`,
tier SECURITY): every token issued before it is revoked. `is_revoked` is the one check. The JWT
blocklist loader asks it on every signed-in request, so an access token is refused on its very next
request, and every refresh that decodes its token by hand asks it too.

Every token the app mints carries its issue time in milliseconds (`ISSUED_AT_MS_CLAIM`, added by
`issue_time_claims`), and the cut-off is kept in milliseconds, so a login made before the cut-off is
refused even in the cut-off's own second, and one made after it is an ordinary login. A token minted
in the cut-off's own millisecond stays valid but is never extended (`may_extend`): the app's
`after_request` re-mints a signed-in access cookie near its expiry, which on production's 30-minute
tokens is every request, so extending it would keep a login from before the cut-off alive for good.
A token minted before the millisecond claim existed has only JWT `iat`, in whole seconds, and keeps
that rule: refused when issued in an earlier second, valid but never extended in the cut-off's own.

Redis down: the per-request check fails open and logs an error (availability first; the owner can
object). Revoking fails loudly instead, so the action that asked for it reports the failure.
"""

import json
import logging
from datetime import timedelta
from typing import Any, Dict, FrozenSet, Optional, Tuple

import redis
from flask import current_app

from business_app import redis_client
from business_app.models.user import User, UserSession
from business_app.services.token_service import TokenService
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from business_app.utils.timezone_utils import get_utc_now
from shared.redis_keyspace import RedisKeyspace

logger = logging.getLogger(__name__)

# Why an account's logins were ended: the audit row's reason.
REVOKED_BY_PASSWORD_RESET = "password_reset"
REVOKED_BY_ACCOUNT_MERGE = "account_merge"
REVOKED_BY_ROLE_CHANGE = "role_changed"

# A token's issue time in milliseconds. JWT `iat` has whole seconds, too coarse for the cut-off: a
# login made in the same second as a password reset could not be told from one made after it.
ISSUED_AT_MS_CLAIM = "iat_ms"


def _now_ms() -> int:
    """Now, in whole milliseconds: the unit of the cut-off and of every token's issue time."""
    return int(get_utc_now().timestamp() * 1000)


def issue_time_claims() -> Dict[str, int]:
    """The claim every token the app mints carries, its issue time in milliseconds.

    Registered once as the JWT manager's `additional_claims_loader` (business_app/__init__.py), so
    every `create_access_token` and `create_refresh_token` adds it: a login's pair and the staff
    login (`TokenService.generate_tokens`), `TokenService.refresh_access_token`,
    `/auth/refresh-cookie` and the `after_request` re-mint. A claim a caller passes itself wins
    over it, and none passes this one.
    """
    return {ISSUED_AT_MS_CLAIM: _now_ms()}


def _cutoff_ttl_seconds() -> int:
    """As long as a refresh token lives: any token older than that has expired by itself."""
    lifetime = current_app.config.get("JWT_REFRESH_TOKEN_EXPIRES", timedelta(days=30))
    return int(lifetime.total_seconds()) if isinstance(lifetime, timedelta) else int(lifetime)


class SessionRevocationService:
    @staticmethod
    def revoke_all(user_id: int, *, reason: str) -> int:
        """End every existing login of the account now; answers how many login rows it ended.

        The cut-off is written first and raises if Redis cannot take it. The login rows then end in
        the caller's transaction: the caller commits, because the merge runs this inside its own.
        """
        cutoff = {"before_ms": _now_ms()}
        try:
            redis_client.set(RedisKeyspace.tokens_valid_after(user_id), json.dumps(cutoff), ex=_cutoff_ttl_seconds())
        except redis.RedisError:
            logger.error("Could not end the sessions of user %s (%s): Redis refused the cut-off", user_id, reason)
            raise

        # Today's bookkeeping, so the session lists agree: the tracked sessions and the login rows end.
        TokenService().revoke_user_tokens(user_id)
        ended = UserSession.query.filter_by(user_id=user_id, is_active=True).update(
            {"is_active": False, "ended_at": get_utc_now()}
        )

        try:
            audit_logger.log_event(
                event_type=AuditEventType.LOGOUT,
                action="all_sessions_revoked",
                severity=AuditSeverity.HIGH,
                resource_type="user",
                resource_id=str(user_id),
                description="Every existing login of the account was ended",
                additional_data={"reason": reason},
            )
        except Exception:  # noqa: BLE001 - the logins are ended; a lost audit row must not undo it
            logger.exception("all_sessions_revoked audit failed for user %s", user_id)
        return ended

    @staticmethod
    def roles_of(user: User) -> Tuple[Optional[str], FrozenSet[str]]:
        """What a role change compares: the account's role and its staff roles."""
        from business_app.services.staff_service import StaffService  # lazy: staff_service imports us

        return StaffService._normalize_role_value(user.role), frozenset(StaffService._extract_staff_roles(user))

    @staticmethod
    def revoke_on_role_change(user: User, roles_before: Tuple[Optional[str], FrozenSet[str]]) -> None:
        """End every login of the account when its roles are no longer `roles_before` (`roles_of`,
        read before the change); an edit that leaves them as they were ends nothing.

        A token carries the role it was issued with, and the app re-mints it with the same claims,
        so an open session would keep a role an admin took away (owner decision R9c, 2026-10-20).
        Call it after the change and before the caller commits, like `revoke_all`: with Redis down
        it raises, and the change is not saved.
        """
        if SessionRevocationService.roles_of(user) != roles_before:
            SessionRevocationService.revoke_all(user.id, reason=REVOKED_BY_ROLE_CHANGE)

    @staticmethod
    def is_revoked(claims: Dict[str, Any]) -> bool:
        """Whether a decoded access or refresh token is revoked: its own logout blacklisted it, or it
        was issued before its account's cut-off."""
        return not _outlives_cutoff(claims, keeps_cutoff_tick=True)

    @staticmethod
    def may_extend(claims: Dict[str, Any]) -> bool:
        """Whether the app may re-mint this access token with a later expiry (`after_request`).

        Stricter than `is_revoked`: a token issued in the cut-off's own tick (its millisecond, or its
        second for a token without the millisecond claim) is accepted until it expires, but never
        extended. Re-minting it would carry a login from before the cut-off past it for good. Asked after the
        request ran, so a logout or a cut-off written during the request is seen.
        """
        return _outlives_cutoff(claims, keeps_cutoff_tick=False)


def _outlives_cutoff(claims: Dict[str, Any], *, keeps_cutoff_tick: bool) -> bool:
    """Whether a token survives its own blacklisting and its account's cut-off; the one rule behind
    `is_revoked` and `may_extend`, which differ only on a token issued in the cut-off's own tick."""
    jti = claims.get("jti")
    if jti and TokenService().is_token_blacklisted(jti):
        return False

    user_id = claims.get("sub")
    try:
        raw = redis_client.get(RedisKeyspace.tokens_valid_after(user_id))
    except redis.RedisError as exc:
        logger.error("session cut-off unreadable for user %s, the token is let through: %s", user_id, exc)
        return True
    if not raw:
        return True

    cutoff = json.loads(raw)
    issued, before = _issue_time_and_cutoff(claims, cutoff["before_ms"])
    if issued is None:
        return False
    return issued >= before if keeps_cutoff_tick else issued > before


def _issue_time_and_cutoff(claims: Dict[str, Any], cutoff_ms: int) -> Tuple[Optional[int], int]:
    """The token's issue time and the cut-off in one unit: milliseconds when the token carries its
    millisecond claim, whole seconds (JWT `iat`) for a token minted before that claim existed."""
    issued_ms = claims.get(ISSUED_AT_MS_CLAIM)
    if issued_ms is not None:
        return issued_ms, cutoff_ms
    return claims.get("iat"), cutoff_ms // 1000
