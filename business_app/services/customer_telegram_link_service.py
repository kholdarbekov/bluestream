"""A web-only customer connects our Telegram bot in one tap (spec 2026-10-19 D2–D4, D11, D13, D16).

`telegram_link_state` is the one answer to "does this customer have Telegram, and may they get a
link". The /my-account page, the issue route and both bot routes read it, so the card a customer
sees and the redeem the bot sends cannot disagree.

The token lives only in Redis (tier SECURITY): issued at the tap for ten minutes, replaced by the
customer's next tap, previewed without being consumed, and redeemed once by an atomic GETDEL.
Redis down means no link and no redeem (fail closed); nothing is kept in memory.
"""

import json
import logging
import secrets
from typing import Any, Dict, Optional

import redis
from flask import current_app
from sqlalchemy.exc import IntegrityError

from business_app import db
from business_app.models.user import User
from business_app.services.auth_service import mask_account_email, mask_person_name, may_sign_in
from business_app.services.telegram_reachability_service import TelegramReachabilityService
from business_app.services.token_service import TokenService
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from business_app.utils.exceptions import ConflictError, ExternalServiceError, ValidationError
from business_app.utils.timezone_utils import get_utc_now
from business_app.utils.transactions import atomic_transaction
from business_app.utils.translations import get_translation
from shared.constants import WEB_LINK_START_PREFIX
from shared.redis_keyspace import RedisKeyspace
from shared.user_names import greeting_name
from shared.validators import mask_phone_number

logger = logging.getLogger(__name__)

# How long a link works (R3).
LINK_TTL_SECONDS = 600

# `telegram_link_state` (D13). Only `available` gets the card, a link and a redeem.
TELEGRAM_LINK_AVAILABLE = "available"
TELEGRAM_LINK_CONNECTED = "connected"
TELEGRAM_LINK_CONNECTED_BLOCKED = "connected_blocked"
TELEGRAM_LINK_UNAVAILABLE = "unavailable"

# Confirm's `phone_status`: the customer bot asks for a code, a number or nothing on these (D12).
PHONE_VERIFIED = "verified"
PHONE_UNVERIFIED = "unverified"
PHONE_MISSING = "missing"


class CustomerTelegramLinkService:
    @staticmethod
    def telegram_link_state(user: Optional[User]) -> str:
        """The one published Telegram state of an account (D13).

        A set telegram_id answers first: "connected" is the rule that routes delivery updates
        (`NotificationService._user_has_connected_telegram`), so staff with a Telegram are
        connected too. Without one, staff and accounts that may not sign in are unavailable.
        """
        from business_app.services.notification_service import NotificationService

        if user is None:
            return TELEGRAM_LINK_UNAVAILABLE
        if user.telegram_id:
            if NotificationService._user_has_connected_telegram(user):
                return TELEGRAM_LINK_CONNECTED
            return TELEGRAM_LINK_CONNECTED_BLOCKED
        if user.is_staff_user or not may_sign_in(user):
            return TELEGRAM_LINK_UNAVAILABLE
        return TELEGRAM_LINK_AVAILABLE

    @staticmethod
    def deep_link(token: str) -> str:
        """Our bot's `/start web_<token>` link, from the single bot-identity config (D2)."""
        return f"{current_app.config['COMPANY_TELEGRAM_BOT_URL']}?start={WEB_LINK_START_PREFIX}{token}"

    @staticmethod
    def account_label(user: User) -> str:
        """The confirm card's account: masked "name · phone", or "name · email" without a phone (D16)."""
        contact = mask_phone_number(user.phone) if user.phone else mask_account_email(user.email)
        return " · ".join(p for p in (mask_person_name(user.first_name, user.last_name), contact) if p)

    @staticmethod
    def _redis() -> redis.Redis:
        return redis.from_url(current_app.config["REDIS_URL"], decode_responses=True)

    @staticmethod
    def _store_unavailable() -> ExternalServiceError:
        return ExternalServiceError(
            get_translation("api.auth.telegram_link_store_unavailable"),
            service_name="redis",
            error_code="TELEGRAM_LINK_STORE_UNAVAILABLE",
        )

    @classmethod
    def issue_link(cls, user_id: int, *, language: str) -> Dict[str, Any]:
        """The /my-account tap: a one-time link, replacing the customer's previous one (D2, D11).

        `language` is the page's; the bot shows the confirm card and greets in it, and confirm
        saves it as the account's language.
        """
        if cls.telegram_link_state(db.session.get(User, user_id)) != TELEGRAM_LINK_AVAILABLE:
            raise ConflictError(
                get_translation("api.auth.telegram_link_not_available"), error_code="TELEGRAM_LINK_NOT_AVAILABLE"
            )
        token = secrets.token_urlsafe(24)
        if language not in current_app.config["LANGUAGES"]:
            language = current_app.config["DEFAULT_LANGUAGE"]
        payload = {"user_id": user_id, "issued_at": get_utc_now().isoformat(), "language": language}
        try:
            r = cls._redis()
            r.setex(RedisKeyspace.customer_telegram_link(token), LINK_TTL_SECONDS, json.dumps(payload))
            # Swapping the pointer atomically means two racing taps still leave one live link (RF1).
            previous = r.set(
                RedisKeyspace.customer_telegram_link_pointer(user_id), token, ex=LINK_TTL_SECONDS, get=True
            )
            if previous:
                r.delete(RedisKeyspace.customer_telegram_link(previous))
        except redis.RedisError as exc:
            raise cls._store_unavailable() from exc
        return {"telegram_link": cls.deep_link(token), "expires_in": LINK_TTL_SECONDS}

    @staticmethod
    def _payload(raw: Optional[str]) -> Dict:
        if not raw:
            raise ValidationError("This link has expired or was already used", error_code="WEB_LINK_EXPIRED")
        return json.loads(raw)

    @staticmethod
    def _already_has_account() -> ConflictError:
        return ConflictError("This Telegram already has an account", error_code="TELEGRAM_ALREADY_HAS_ACCOUNT")

    @staticmethod
    def _telegram_id_taken(telegram_id: int) -> bool:
        return db.session.query(User.id).filter(User.telegram_id == str(telegram_id)).first() is not None

    @classmethod
    def _redeemable(cls, user: Optional[User], telegram_id: int) -> None:
        """The account may still be connected, and this Telegram has no account of its own."""
        if cls.telegram_link_state(user) != TELEGRAM_LINK_AVAILABLE:
            raise ConflictError("This account can no longer be connected", error_code="WEB_LINK_NOT_AVAILABLE")
        if cls._telegram_id_taken(telegram_id):
            raise cls._already_has_account()

    @classmethod
    def preview(cls, token: str, *, telegram_id: int) -> Dict[str, Any]:
        """What the bot's confirm card names, without using the link (D15a order: token, account, Telegram)."""
        try:
            raw = cls._redis().get(RedisKeyspace.customer_telegram_link(token))
        except redis.RedisError as exc:
            raise cls._store_unavailable() from exc
        payload = cls._payload(raw)
        user = db.session.get(User, payload["user_id"])
        cls._redeemable(user, telegram_id)
        return {"account_label": cls.account_label(user), "language": payload["language"]}

    @classmethod
    def confirm(
        cls,
        token: str,
        *,
        telegram_id: int,
        username: Optional[str],
        first_name: Optional[str],
        last_name: Optional[str],
    ) -> Dict[str, Any]:
        """The customer's Yes: connect this Telegram to the account and sign the bot in (D4).

        The link is spent first, so a refusal below still kills it. It never touches
        `registration_source`, `email_verified_at`, `status`, loyalty or referrals (R7, RF5).
        """
        try:
            raw = cls._redis().getdel(RedisKeyspace.customer_telegram_link(token))
        except redis.RedisError as exc:
            raise cls._store_unavailable() from exc
        payload = cls._payload(raw)
        now = get_utc_now()
        try:
            with atomic_transaction():
                user = db.session.get(User, payload["user_id"], with_for_update=True, populate_existing=True)
                cls._redeemable(user, telegram_id)
                user.telegram_id = str(telegram_id)
                user.telegram_username = username
                user.last_bot_interaction = now
                # Telegram's names fill only what the account lacks; a web name the customer typed stays.
                user.first_name = user.first_name or first_name
                user.last_name = user.last_name or last_name
                user.preferred_language = payload["language"]  # D11: the page they tapped from
                TelegramReachabilityService.mark_reachable(user, source="link", at=now)
        except IntegrityError as exc:
            # Another account took this telegram_id between the check and the commit.
            raise cls._already_has_account() from exc

        try:
            audit_logger.log_event(
                event_type=AuditEventType.USER_UPDATED,
                action="telegram_linked",
                severity=AuditSeverity.HIGH,
                resource_type="user",
                resource_id=str(user.id),
                description="Telegram connected from the website link",
                new_values={"telegram_id": str(telegram_id)},
            )
        except Exception:  # noqa: BLE001 - the link is committed; a lost audit row must not undo it
            logger.exception("telegram_linked audit failed for user %s", user.id)

        tokens = TokenService().generate_tokens(user)
        db.session.commit()  # generate_tokens stamps last_login
        return {
            "tokens": tokens,
            "language": user.preferred_language,
            "greeting_name": greeting_name(
                first_name=user.first_name,
                full_name=user.full_name,
                company_name=user.company_name,
                is_entity=bool(user.is_entity_user),
            ),
            "phone_status": (
                PHONE_MISSING if not user.phone else (PHONE_VERIFIED if user.phone_verified else PHONE_UNVERIFIED)
            ),
            "phone": user.phone,
        }
