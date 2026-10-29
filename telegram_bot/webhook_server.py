"""
Internal Webhook Server for Bot Management
Receives webhooks from backend for cache invalidation and other operations
"""
import hmac
import hashlib
import html
import ipaddress
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from aiohttp import web
from telegram.error import Forbidden

from config import config
from database import db_manager
from i18n import i18n
from shared import reorder_reminder_copy
from shared.i18n_rendering import SUPPORTED_LANGUAGES
from shared.redis_failure import report_redis_failure
from shared.redis_keyspace import REORDER_REMINDER_CLAIM_TTL_SECONDS, RedisKeyspace

logger = logging.getLogger(__name__)

# The sales-agent order proposal. ONE token behind the URL path, the dedup
# endpoint both layers are CLAIMED under, and the endpoint the send-failure
# branch RELEASES them under. A claim and a release spelled out separately is a
# release that silently no-ops — and a proposal the store never sees.
_AGENT_ORDER_ENDPOINT = 'agent-order-proposed'

# The delivered summary. Same rule as the constant above: the route path, the
# layer-1 dedup claim and the layer-1 release must be ONE token, or a release
# spelled separately from its claim silently no-ops and the summary is lost.
# (Layer 2's 'delivery-completed-order' keeps its own name — see
# `_release_order_dedup`'s default.)
_DELIVERY_ENDPOINT = 'delivery-completed'

# The reorder reminder (spec 2026-10-09 §7.3). Same one-token rule as the two above: the
# route path, both dedup layers' claims and both releases.
_REORDER_REMINDER_ENDPOINT = 'reorder-reminder'

# The customer notices (plan 2026-10-24 Task 3; spec 2026-10-23 §9). Same one-token rule: the route path,
# the claim and its release.
_CUSTOMER_NOTICE_ENDPOINT = 'customer-notice'
_NOTICE_ORDER_CREATED = 'subscription_order_created'
_NOTICE_DELIVERY_SKIPPED = 'subscription_delivery_skipped'
_NOTICE_PAID_ORDER_KEPT = 'subscription_paid_order_kept'
_NOTICE_PAYMENT_EXPIRED = 'order_payment_expired'
_NOTICE_SCHEDULE_CHANGED = 'subscription_schedule_changed'
_CUSTOMER_NOTICE_KINDS = frozenset(
    {
        _NOTICE_ORDER_CREATED,
        _NOTICE_DELIVERY_SKIPPED,
        _NOTICE_PAID_ORDER_KEPT,
        _NOTICE_PAYMENT_EXPIRED,
        _NOTICE_SCHEDULE_CHANGED,
    }
)
# A paid order that stays: one copy per action that kept it (spec 2026-10-23 §2.7).
_PAID_ORDER_KEPT_KEYS = {
    'skip': 'telegram.customer_notice.paid_order_kept_skip',
    'pause': 'telegram.customer_notice.paid_order_kept_pause',
    'cancel': 'telegram.customer_notice.paid_order_kept_cancel',
}
# What a slot write changed in a subscription's schedule (spec 2026-10-27 §6.5): one line per change, in the order
# the backend lists them (business_app/services/subscription_notices.py::SCHEDULE_CHANGES).
_SCHEDULE_CHANGE_KEYS = {
    'time_cleared': 'telegram.customer_notice.schedule_change.time_cleared',
    'time_changed': 'telegram.customer_notice.schedule_change.time_changed',
    'day_moved': 'telegram.customer_notice.schedule_change.day_moved',
    'date_moved': 'telegram.customer_notice.schedule_change.date_moved',
    'days_added': 'telegram.customer_notice.schedule_change.days_added',
}

# Answered with HTTP 410 when Telegram refuses the send because this account blocked the bot
# (or was deleted). The backend send task reads the STATUS (`trigger_bot_webhook` drops
# non-200 bodies, keeping only their `message`), marks the account blocked and does not retry
# (spec §7.2 c).
_BOT_BLOCKED_CODE = 'BOT_BLOCKED'


# BOT-007: IP allow-list for /internal/* webhook endpoints.
# Default allows only RFC 1918 private ranges (Docker internal networks) + loopback.
# Override with WEBHOOK_ALLOWED_NETWORKS env var (comma-separated CIDRs) to lock
# down to a specific subnet, e.g. the Docker compose network's assigned range.
_DEFAULT_ALLOWED_NETWORKS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8,::1/128"


def _parse_allowed_networks() -> list:
    """Parse WEBHOOK_ALLOWED_NETWORKS env var into a list of ip_network objects.

    Invalid CIDRs are logged and skipped rather than failing the server — we'd
    rather serve a narrower allow-list than refuse to start.
    """
    raw = os.environ.get("WEBHOOK_ALLOWED_NETWORKS", _DEFAULT_ALLOWED_NETWORKS)
    networks = []
    for cidr in (c.strip() for c in raw.split(",") if c.strip()):
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError as exc:
            logger.error("WEBHOOK_ALLOWED_NETWORKS: skipping invalid CIDR %r: %s", cidr, exc)
    if not networks:
        logger.error(
            "WEBHOOK_ALLOWED_NETWORKS resolved to an empty list — "
            "all /internal/* requests will be rejected. Fix the env var."
        )
    return networks


_ALLOWED_NETWORKS = _parse_allowed_networks()


def _request_client_ip(request) -> str:
    """Return the peer IP for allow-list evaluation.

    For the internal bot webhook server there's no reverse proxy in front of
    us (backend talks to us directly on the Docker network), so `request.remote`
    is the authoritative source. Explicitly ignoring `X-Forwarded-For` here is
    intentional — trusting it would let a compromised backend spoof its IP.
    """
    return request.remote or ""


def _is_allowed_ip(client_ip: str) -> bool:
    """True if client_ip falls inside any configured allow-list network."""
    if not client_ip or not _ALLOWED_NETWORKS:
        return False
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(addr in net for net in _ALLOWED_NETWORKS)


def _html_escaped(value) -> str:
    """Escape a backend-supplied string for ``parse_mode='HTML'``.

    Agent names and product names are free text an operator typed. Unescaped, a
    single '<' makes Telegram reject the entire send with "Can't parse
    entities" — the customer sees nothing at all, deterministically, for that
    order.
    """
    return html.escape(str(value if value is not None else ''))


def _order_id_or_none(value) -> Optional[int]:
    """The positive whole order id `value` stands for, or None.

    Feeds the Order-again button's ``reorder_<id>`` callback (reorder reminder and delivery
    summary), which the bot only claims as
    ``^reorder_\\d+$``. Anything else would ship a button that does nothing: ``9001.0``, ``"abc"``,
    ``-5``, ``True`` (``int(True)`` is 1, someone else's order). A whole float or a numeric
    string is coerced (``9001.0`` -> 9001); everything else is None, which opens the catalogue on a
    reminder and drops Order again from a delivery summary.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        order_id = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return order_id if order_id > 0 else None


def _customer_notice_problem(data: dict) -> Optional[str]:
    """Why a customer-notice payload cannot be rendered, or None. Asked before the claim is taken, so a
    corrected retry still gets through. The language is the backend's one answer; the bot never picks its own."""
    if not data.get('notice_id') or not data.get('telegram_id'):
        return 'Missing required fields: notice_id, telegram_id'
    if data.get('kind') not in _CUSTOMER_NOTICE_KINDS:
        return f"Unknown kind: {data.get('kind')!r}"
    if data.get('language') not in SUPPORTED_LANGUAGES:
        return f'language must be one of {", ".join(SUPPORTED_LANGUAGES)}'
    if data['kind'] == _NOTICE_PAID_ORDER_KEPT and data.get('trigger') not in _PAID_ORDER_KEPT_KEYS:
        return f"Unknown trigger: {data.get('trigger')!r}"
    if data['kind'] == _NOTICE_SCHEDULE_CHANGED:
        changes = data.get('changes')
        if not isinstance(changes, list) or not changes or not all(
            isinstance(code, str) and code in _SCHEDULE_CHANGE_KEYS for code in changes
        ):
            return f"Unknown changes: {changes!r}"
        added = data.get('added_weekdays')
        if not isinstance(added, list) or not all(_is_weekday(day) for day in added):
            return f"added_weekdays must be a list of weekdays 0-6, not {added!r}"
    return None


def _is_weekday(value) -> bool:
    """0 (Monday) to 6 (Sunday), as the backend sends a weekday; a bool is not one."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 6


def _notice_weekday(language: str, value) -> str:
    """A backend weekday (0 = Monday) by name; a dash when absent."""
    return i18n.get(f'telegram.subscription.weekday_{value}', language) if _is_weekday(value) else '—'


def _schedule_change_line(language: str, code: str, data: dict) -> str:
    """One change of a schedule notice, with the values its copy names (windows HTML-escaped)."""
    old_window = _html_escaped(data.get('old_window')) or '—'
    old_day = _notice_weekday(language, data.get('old_weekday'))
    if code == 'time_cleared':
        values = {'old_window': old_window}
    elif code == 'time_changed':
        values = {'new_window': _html_escaped(data.get('new_window')) or '—', 'old_window': old_window}
    elif code == 'day_moved':
        values = {'old_day': old_day, 'new_day': _notice_weekday(language, data.get('new_weekday'))}
    elif code == 'date_moved':
        values = {'old_day': old_day}
    else:  # days_added
        values = {'days': ', '.join(_notice_weekday(language, day) for day in data['added_weekdays']) or '—'}
    return i18n.get(_SCHEDULE_CHANGE_KEYS[code], language, **values)


def _notice_deadline(language: str, value) -> str:
    """A backend deadline (an ISO instant) by the bot's own date-and-time rule; a dash when absent."""
    from utils import format_datetime

    try:
        return format_datetime(datetime.fromisoformat(str(value)), language) if value else '—'
    except ValueError:
        return '—'


def _notice_payment_line(language: str, data: dict) -> str:
    """How the order is paid, from the backend's `payment` answer (spec 2026-10-23 §4.4)."""
    from utils import format_price

    payment = data.get('payment')
    if payment == 'cash':
        due = float(data.get('amount_due') or 0)
        if due > 0:
            line = i18n.get('telegram.customer_notice.pay_cash', language, amount=format_price(due))
        else:
            line = i18n.get('telegram.customer_notice.pay_cash_covered', language)
        prepaid = float(data.get('prepaid_applied') or 0)
        if prepaid > 0:
            prepaid_line = i18n.get('telegram.customer_notice.pay_cash_prepaid', language, amount=format_price(prepaid))
            line = f'{prepaid_line}\n{line}'
        return line
    if payment == 'company_account':
        return i18n.get('telegram.customer_notice.pay_company_account', language)
    if payment == 'online_unpaid':
        return i18n.get(
            'telegram.customer_notice.pay_online', language, deadline=_notice_deadline(language, data.get('deadline'))
        )
    if payment == 'online_paid':
        return i18n.get('telegram.customer_notice.pay_online_paid', language)
    return ''


def _customer_notice_text(language: str, data: dict) -> str:
    """The notice's message, in the backend's `language`. Every backend-supplied string is HTML-escaped (the
    message is sent as HTML); dates and money are formatted by the bot's own rules (`utils`)."""
    from utils import format_date, format_price

    kind = data['kind']
    if kind == _NOTICE_ORDER_CREATED:
        when = format_date(data.get('delivery_date')) or '—'
        has_window = bool(data.get('window_start') and data.get('window_end'))
        # `time_kind` (window / any_time / to_be_confirmed) is the backend's answer; a notice queued before it
        # existed has none, and the old rule applies: a window, or "to be confirmed".
        time_kind = data.get('time_kind') or ('window' if has_window else 'to_be_confirmed')
        if time_kind == 'window' and has_window:
            delivery = i18n.get(
                'telegram.customer_notice.delivery_window', language, date=when,
                start=_html_escaped(data['window_start']), end=_html_escaped(data['window_end']),
            )
        elif time_kind == 'any_time':
            delivery = i18n.get('telegram.customer_notice.delivery_any_time', language, date=when)
        else:
            delivery = i18n.get('telegram.customer_notice.delivery_tbc', language, date=when)
        items = '\n'.join(
            i18n.get(
                'telegram.customer_notice.item', language,
                name=_html_escaped(item.get('name')), qty=_html_escaped(item.get('qty')),
            )
            for item in (data.get('items') or [])
        )
        not_included = '\n'.join(
            i18n.get(
                'telegram.customer_notice.not_included', language,
                name=_html_escaped(name),
            )
            for name in (data.get('not_included') or [])
        )
        if not_included:
            items = f"{items}\n{not_included}" if items else not_included
        return i18n.get(
            'telegram.customer_notice.order_created', language,
            order_number=_html_escaped(data.get('order_number')),
            delivery=delivery,
            items=items or '—',
            total=format_price(float(data.get('total') or 0)),
            payment=_notice_payment_line(language, data),
        )
    if kind == _NOTICE_DELIVERY_SKIPPED:
        dates = ', '.join(filter(None, (format_date(day) for day in (data.get('skipped_dates') or []))))
        return i18n.get(
            'telegram.customer_notice.delivery_skipped', language,
            subscription_name=_html_escaped(data.get('subscription_name')) or '—',
            dates=dates or '—',
            reason=_html_escaped(data.get('reason')) or '—',
            next_date=format_date(data.get('next_delivery_date')) or '—',
        )
    if kind == _NOTICE_PAID_ORDER_KEPT:
        trigger = data['trigger']
        values = {
            'order_number': _html_escaped(data.get('order_number')),
            'date': format_date(data.get('delivery_date')) or '—',
        }
        if trigger == 'skip':
            values['next_date'] = format_date(data.get('next_delivery_date')) or '—'
        return i18n.get(_PAID_ORDER_KEPT_KEYS[trigger], language, **values)
    if kind == _NOTICE_SCHEDULE_CHANGED:
        lines = [_schedule_change_line(language, code, data) for code in data['changes']]
        next_date = format_date(data.get('next_delivery_date'))
        if next_date:  # a paused subscription has no next delivery
            lines.append(i18n.get('telegram.customer_notice.schedule_changed_next', language, date=next_date))
        return i18n.get(
            'telegram.customer_notice.schedule_changed', language,
            subscription_name=_html_escaped(data.get('subscription_name')) or '—',
            lines='\n\n'.join(lines),
        )
    return i18n.get(
        'telegram.customer_notice.payment_expired', language,
        order_number=_html_escaped(data.get('order_number')),
        deadline=_notice_deadline(language, data.get('deadline')),
    )


@web.middleware
async def ip_allowlist_middleware(request, handler):
    """Reject /internal/* requests from IPs outside the allow-list.

    Signature verification still runs inside each handler — this is a
    defense-in-depth layer: if the webhook secret ever leaks, attackers
    still need to come from inside the Docker network.
    """
    if request.path.startswith("/internal/"):
        client_ip = _request_client_ip(request)
        if not _is_allowed_ip(client_ip):
            logger.warning(
                "Rejected /internal request from disallowed IP %s (path=%s)",
                client_ip or "<unknown>",
                request.path,
            )
            return web.json_response(
                {"success": False, "message": "Forbidden"}, status=403
            )
    return await handler(request)


async def verify_webhook_signature(request):
    """
    Verify webhook signature for security

    Args:
        request: aiohttp request object

    Returns:
        bool: True if signature is valid
    """
    signature = request.headers.get('X-Bot-Webhook-Signature')
    if not signature:
        return False

    # Use the dedicated bot-webhook secret. No cross-domain fallback to
    # JWT_SECRET_KEY: a leaked auth-token secret must not also be able to
    # forge internal webhooks. Startup validation in config._validate_config
    # ensures BOT_WEBHOOK_SECRET is set, so this guard is defence-in-depth.
    webhook_secret = config.security.webhook_secret
    if not webhook_secret:
        logger.error("BOT_WEBHOOK_SECRET not configured")
        return False

    # Calculate expected signature
    body = await request.read()
    expected_signature = hmac.new(
        webhook_secret.encode('utf-8'),
        body,
        hashlib.sha256
    ).hexdigest()

    # Constant-time comparison
    return hmac.compare_digest(signature, expected_signature)


async def reload_translations_handler(request):
    """
    Handle translation reload webhook from backend

    POST /internal/reload-translations
    """
    try:
        # Verify signature
        if not await verify_webhook_signature(request):
            logger.warning(f"Invalid webhook signature from {request.remote}")
            return web.json_response({
                'success': False,
                'message': 'Invalid signature'
            }, status=401)

        # Parse request data
        try:
            data = await request.json()
        except Exception:
            data = {}

        logger.info(f"Received translation reload request: {data}")

        # Reload translations
        await i18n.reload_translations()

        logger.info("Translation reload completed successfully")

        return web.json_response({
            'success': True,
            'message': 'Translations reloaded successfully',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'translation_count': sum(len(keys) for keys in i18n.translations.values())
        })

    except Exception as e:
        logger.error(f"Error reloading translations: {e}", exc_info=True)
        return web.json_response({
            'success': False,
            'message': 'Internal server error'
        }, status=500)


async def health_handler(_request):
    """
    Health check endpoint

    GET /health
    """
    checks = {}
    overall_healthy = True

    # Check translation status
    try:
        translation_count = sum(len(keys) for keys in i18n.translations.values())
        missing_keys_count = sum(len(keys) for keys in i18n.missing_keys.values())
        checks['translations'] = {
            'status': 'ok',
            'total_count': translation_count,
            'languages': list(i18n.translations.keys()),
            'missing_keys_count': missing_keys_count
        }
    except Exception as e:
        checks['translations'] = {'status': 'error', 'error': str(e)}
        overall_healthy = False

    # Check database connectivity
    try:
        if db_manager.pool:
            async with db_manager.pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            pool_size = db_manager.pool.get_size()
            pool_free = db_manager.pool.get_idle_size()
            checks['database'] = {
                'status': 'ok',
                'pool_size': pool_size,
                'pool_free': pool_free
            }
        else:
            checks['database'] = {'status': 'not_connected'}
            overall_healthy = False
    except Exception as e:
        checks['database'] = {'status': 'error', 'error': str(e)}
        overall_healthy = False

    # Check Redis connectivity (via token manager). The TokenManager instance
    # lives on the running Telegram Application's bot_data (see bot.py:166),
    # not as a module-level singleton — `from token_manager import token_manager`
    # was importing a name that never existed, so /health perpetually reported
    # redis: not_connected.
    try:
        token_manager = (
            webhook_server.bot_app.bot_data.get('token_manager')
            if webhook_server.bot_app else None
        )
        if token_manager and token_manager.redis:
            await token_manager.redis.ping()
            checks['redis'] = {'status': 'ok'}
        else:
            checks['redis'] = {'status': 'not_connected'}
            overall_healthy = False
    except Exception as e:
        checks['redis'] = {'status': 'error', 'error': str(e)}
        overall_healthy = False

    status_code = 200 if overall_healthy else 503
    return web.json_response({
        'status': 'healthy' if overall_healthy else 'degraded',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'checks': checks
    }, status=status_code)


async def stats_handler(request):
    """
    Get bot statistics

    GET /internal/stats
    """
    try:
        # Verify signature (stats are admin-only)
        if not await verify_webhook_signature(request):
            logger.warning(f"Invalid webhook signature from {request.remote}")
            return web.json_response({
                'success': False,
                'message': 'Invalid signature'
            }, status=401)

        # Get translation completeness
        completeness = i18n.check_completeness()

        # Get missing keys
        missing_keys = i18n.get_missing_keys()

        return web.json_response({
            'success': True,
            'data': {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'translations': {
                    'completeness': completeness,
                    'missing_keys': missing_keys,
                    'total_count': sum(len(keys) for keys in i18n.translations.values())
                }
            }
        })

    except Exception as e:
        logger.error(f"Error getting stats: {e}", exc_info=True)
        return web.json_response({
            'success': False,
            'message': 'Internal server error'
        }, status=500)


class WebhookServer:
    """Internal webhook server for bot management"""

    def __init__(self, host='0.0.0.0', port=8080):
        self.host = host
        self.port = port
        self.app = None
        self.runner = None
        self.site = None
        self.bot_app = None
        # BOT-008: dedup is now Redis-backed (see _is_duplicate_webhook). The
        # in-memory dict + 5-min TTL was insufficient — backend retries past
        # 5 min re-sent notifications, and multi-replica deployments had no
        # cross-replica visibility. Kept these attrs only as a last-resort
        # in-memory fallback when Redis is unreachable; per RED-005 a Sentry
        # alert fires on Redis failure so the degradation is observable.
        self._processed_orders: dict = {}
        self._dedup_ttl_seconds = 24 * 60 * 60  # 24h Redis TTL

    def set_application(self, application):
        """Set Telegram Application instance"""
        self.bot_app = application

    async def _is_duplicate_webhook(self, endpoint: str, request_id: str, ttl_seconds: Optional[int] = None) -> bool:
        """BOT-008: Redis-backed webhook dedup.

        Uses ``SET NX EX`` to claim the request_id atomically. Returns True if
        the key already existed (duplicate), False if we just claimed it.
        TTL = 24h (``_dedup_ttl_seconds``) matches the upper bound on backend
        retry storms (PAY-002); a route whose sender can legitimately re-push
        later than that passes its own ``ttl_seconds``. ``ttl_seconds`` applies to the Redis
        claim only: the in-memory fallback below keeps its fixed 300 s window whatever the
        caller asks for.

        Falls back to the in-memory ``_processed_orders`` dict only when Redis
        is unreachable. The fallback is single-replica only — multi-replica
        deploys MUST have Redis up. RED-005's ``report_redis_failure`` makes
        the degradation observable in Sentry.
        """
        if not request_id:
            # No stable id to dedup on — let it through and rely on downstream
            # idempotency (e.g. payment-message Redis lookup overwrites cleanly).
            return False

        # Redis path. Resolve the TokenManager from the running Application's
        # bot_data; the prior `from token_manager import token_manager` was a
        # broken import that silently routed every dedup check to the in-memory
        # fallback, defeating multi-replica protection.
        try:
            token_manager = (
                self.bot_app.bot_data.get('token_manager')
                if self.bot_app else None
            )
            if token_manager and token_manager.redis:
                key = RedisKeyspace.bot_webhook_dedup(endpoint, request_id)
                # SET NX returns True if key was set (we claimed it), None if it existed.
                claimed = await token_manager.redis.set(
                    key,
                    "1",
                    nx=True,
                    ex=self._dedup_ttl_seconds if ttl_seconds is None else ttl_seconds,
                )
                return not claimed
        except Exception as exc:
            report_redis_failure(
                "webhook_server.dedup_check", str(exc), tier="reliability"
            )
            # Fall through to in-memory fallback below

        # In-memory fallback (single-replica only, narrower TTL to match the
        # in-memory bound). Periodically prune so the dict doesn't grow forever.
        now = datetime.now(timezone.utc)
        fallback_ttl = 300  # 5 min — old behaviour, only as a degraded fallback
        self._processed_orders = {
            k: v for k, v in self._processed_orders.items()
            if (now - v).total_seconds() < fallback_ttl
        }
        composite = f"{endpoint}:{request_id}"
        if composite in self._processed_orders:
            return True
        self._processed_orders[composite] = now
        return False

    async def _release_webhook_dedup(self, endpoint: str, request_id: str) -> None:
        """Release a dedup claim taken by ``_is_duplicate_webhook(endpoint, request_id)``.

        A claimed key permanently absorbs the sender's retry: if the Telegram send
        fails AFTER the claim, the handler 500s, the backend releases its own key
        and re-POSTs, and this layer answers "already processed" — silently dropping
        the message. Releasing on send failure lets the retry through. Best-effort in
        BOTH stores ``_is_duplicate_webhook`` writes (the Redis marker and the
        in-memory fallback set), because either may be the live one.

        Takes the raw ``request_id`` rather than an order id: the delivered summary
        dedups on ``order:<id>``, but nothing says the next caller will. One release
        rule, one implementation.
        """
        # In-memory fallback store (single-replica degraded path).
        self._processed_orders.pop(f"{endpoint}:{request_id}", None)

        # Redis marker (the primary store when Redis is up).
        try:
            token_manager = (
                self.bot_app.bot_data.get('token_manager')
                if self.bot_app else None
            )
            if token_manager and token_manager.redis:
                await token_manager.redis.delete(
                    RedisKeyspace.bot_webhook_dedup(endpoint, request_id)
                )
        except Exception as exc:
            report_redis_failure(
                "webhook_server.dedup_release", str(exc), tier="reliability"
            )

    async def _release_order_dedup(self, order_id, endpoint: str = 'delivery-completed-order') -> None:
        """The order-id flavour: release the claim taken as ``order:{order_id}``.

        Kept as the name `delivery_completed_handler` already calls, now a one-line
        delegate — the order-id KEY SHAPE lives here, the release MECHANICS live in
        `_release_webhook_dedup`.
        """
        await self._release_webhook_dedup(endpoint, f"order:{order_id}")

    async def setup(self):
        """Setup webhook server routes"""
        # BOT-007: ip_allowlist_middleware gates /internal/* on source IP.
        # /health stays open so Docker healthchecks don't need to spoof IPs.
        self.app = web.Application(
            client_max_size=1024 * 1024,  # 1MB payload limit
            middlewares=[ip_allowlist_middleware],
        )

        # Add routes
        self.app.router.add_post('/internal/reload-translations', reload_translations_handler)
        self.app.router.add_post('/internal/payment-success', self.payment_success_handler)
        self.app.router.add_post(f'/internal/{_DELIVERY_ENDPOINT}', self.delivery_completed_handler)
        self.app.router.add_post(f'/internal/{_AGENT_ORDER_ENDPOINT}',
                                 self.agent_order_proposed_handler)
        self.app.router.add_post(f'/internal/{_REORDER_REMINDER_ENDPOINT}', self.reorder_reminder_handler)
        self.app.router.add_post(f'/internal/{_CUSTOMER_NOTICE_ENDPOINT}', self.customer_notice_handler)
        self.app.router.add_get('/internal/stats', stats_handler)
        self.app.router.add_get('/health', health_handler)

        logger.info(f"Webhook server configured on {self.host}:{self.port}")
        logger.info(
            "IP allow-list active: %s",
            ", ".join(str(n) for n in _ALLOWED_NETWORKS) or "(empty — all requests blocked)",
        )

    async def payment_success_handler(self, request):
        """
        Handle payment success webhook from backend

        POST /internal/payment-success
        {
            "user_id": 12345,
            "order_id": 67890,
            "amount": 15000,
            "currency": "UZS"
        }
        """
        try:
            # Verify signature
            if not await verify_webhook_signature(request):
                logger.warning(f"Invalid webhook signature from {request.remote}")
                return web.json_response({
                    'success': False,
                    'message': 'Invalid signature'
                }, status=401)

            if not self.bot_app:
                logger.error("Bot application not initialized in webhook server")
                return web.json_response({
                    'success': False,
                    'message': 'Bot not initialized'
                }, status=503)

            # Parse request data
            try:
                data = await request.json()
            except Exception:
                return web.json_response({
                    'success': False,
                    'message': 'Invalid JSON'
                }, status=400)

            user_id = data.get('user_id')
            telegram_id = data.get('telegram_id')
            order_id = data.get('order_id')
            order_number = data.get('order_number')
            amount = data.get('amount')
            currency = data.get('currency', 'UZS')

            if not user_id or not order_id:
                return web.json_response({
                    'success': False,
                    'message': 'Missing required fields: user_id, order_id'
                }, status=400)

            if not telegram_id:
                logger.warning(f"Skipping notification for user {user_id}: No telegram_id provided")
                return web.json_response({
                    'success': True,
                    'message': 'Skipped: No telegram_id'
                })

            # BOT-008: dedup on caller-supplied X-Request-ID when present, fall
            # back to order_id so the existing payload still gets dedup coverage
            # while backends roll out the header. The request_id path keys on
            # the actual webhook attempt, so a backend that retries the same
            # request_id N times only fires one notification — even if a second
            # genuine notification for the same order arrives later (e.g.
            # refund-then-success), it gets through under a different request_id.
            request_id = request.headers.get('X-Request-ID') or f"order:{order_id}"
            if await self._is_duplicate_webhook('payment-success', request_id):
                logger.info(
                    "Duplicate payment webhook (endpoint=payment-success request_id=%s order=%s), skipping",
                    request_id, order_id,
                )
                return web.json_response({
                    'success': True,
                    'message': 'Already processed (deduplicated)'
                })

            logger.info(f"Sending payment success notification to telegram_id {telegram_id} (user {user_id}) for order {order_number}")

            # Get user language
            language = await i18n.get_user_language(telegram_id)

            # Construct message with localized formatting
            message_text = i18n.get(
                'telegram.payment.success_message',
                language,
                order_number=order_number or str(order_id),
                amount=f"{amount:,.0f}",
                currency=currency
            )

            # Try to edit the existing payment message, fall back to sending new.
            # RED-005: the Redis lookup is TIER_CACHE (speedup only — fresh
            # message is sent either way), but the Telegram edit is a business
            # operation. Separate them so Redis failures become observable
            # without masking Telegram-API edge cases (stale message id, etc).
            import redis as redis_lib  # sync module, for exception types
            message_edited = False
            stored_message_id = None

            # Resolve TokenManager via bot_data — `from token_manager import
            # token_manager` was a broken import (no module-level singleton),
            # so before this fix payment_success_handler raised ImportError
            # *before* hitting the Telegram edit/send path, and the customer
            # silently received nothing.
            token_manager = (
                self.bot_app.bot_data.get('token_manager')
                if self.bot_app else None
            )
            if token_manager and token_manager.redis:
                redis_key = RedisKeyspace.bot_payment_message(order_id)
                try:
                    stored_message_id = await token_manager.redis.get(redis_key)
                except redis_lib.RedisError as redis_err:
                    report_redis_failure(
                        "webhook_server.payment_message_lookup", str(redis_err), tier="cache"
                    )
                    stored_message_id = None

            from keyboards import PaymentKeyboards
            keyboard = PaymentKeyboards.payment_success(order_id, language)

            if stored_message_id:
                try:
                    await self.bot_app.bot.edit_message_text(
                        chat_id=telegram_id,
                        message_id=int(stored_message_id),
                        text=message_text,
                        reply_markup=keyboard,
                    )
                    message_edited = True
                    logger.info(f"Edited payment message {stored_message_id} for order {order_id}")
                    # Best-effort cleanup; a delete failure here just leaves a
                    # stale key that will expire naturally.
                    try:
                        await token_manager.redis.delete(redis_key)
                    except redis_lib.RedisError as del_err:
                        report_redis_failure(
                            "webhook_server.payment_message_cleanup",
                            str(del_err),
                            tier="cache",
                        )
                except Exception as edit_err:
                    logger.warning(
                        f"Failed to edit payment message, falling back to send_message: {edit_err}"
                    )

            if not message_edited:
                await self.bot_app.bot.send_message(
                    chat_id=telegram_id,
                    text=message_text,
                    reply_markup=keyboard,
                )

            return web.json_response({
                'success': True,
                'message': 'Notification sent'
            })

        except Exception as e:
            logger.error(f"Error processing payment success: {e}", exc_info=True)
            return web.json_response({
                'success': False,
                'message': 'Internal server error'
            }, status=500)

    async def delivery_completed_handler(self, request):
        """
        Handle delivery-completed webhook from backend.

        POST /internal/delivery-completed
        {
            "order_id": 67890,
            "order_number": "1234",
            "telegram_id": 12345,
            "bottles_delivered": "4",   # normalized Decimal strings, pre-formatted
            "bottles_collected": "3",   # by the backend (format_bottle_quantity)
            "balance": "5",
            "reorder_order_id": 67890   # or null
        }

        Sends the customer a delivery summary with two inline buttons: "Order again"
        (``reorder_<reorder_order_id>``, only when that is not null) above "Report an issue".
        Numbers arrive pre-formatted as strings and are rendered verbatim
        — the bot performs no numeric formatting of its own.
        """
        try:
            # Verify signature
            if not await verify_webhook_signature(request):
                logger.warning(f"Invalid webhook signature from {request.remote}")
                return web.json_response({
                    'success': False,
                    'message': 'Invalid signature'
                }, status=401)

            if not self.bot_app:
                logger.error("Bot application not initialized in webhook server")
                return web.json_response({
                    'success': False,
                    'message': 'Bot not initialized'
                }, status=503)

            # Parse request data
            try:
                data = await request.json()
            except Exception:
                return web.json_response({
                    'success': False,
                    'message': 'Invalid JSON'
                }, status=400)

            order_id = data.get('order_id')
            order_number = data.get('order_number')
            telegram_id = data.get('telegram_id')
            bottles_delivered = str(data.get('bottles_delivered', '0'))
            bottles_collected = str(data.get('bottles_collected', '0'))
            balance = str(data.get('balance', '0'))
            reorder_order_id = _order_id_or_none(data.get('reorder_order_id'))

            if not order_id or not telegram_id:
                return web.json_response({
                    'success': False,
                    'message': 'Missing required fields: order_id, telegram_id'
                }, status=400)

            # Two-layer dedup. Layer 1 (X-Request-ID) collapses an exact HTTP
            # replay of ONE Celery attempt. Layer 2 (order_id) is load-bearing:
            # trigger_bot_webhook mints a FRESH X-Request-ID on every Celery
            # retry (no g.request_id outside a Flask request), so request-id
            # dedup alone can't collapse backend retries of the same delivery.
            # Both reuse the shared Redis SET-NX helper (24h TTL, in-memory
            # fallback + Sentry-observable Redis-failure reporting).
            request_id = request.headers.get('X-Request-ID')
            if request_id and await self._is_duplicate_webhook(_DELIVERY_ENDPOINT, request_id):
                logger.info(
                    "Duplicate delivery webhook (request_id=%s order=%s), skipping",
                    request_id, order_id,
                )
                return web.json_response({
                    'success': True,
                    'message': 'Already processed (deduplicated)'
                })
            if await self._is_duplicate_webhook('delivery-completed-order', f"order:{order_id}"):
                logger.info("Duplicate delivery webhook for order %s, skipping", order_id)
                return web.json_response({
                    'success': True,
                    'message': 'Already processed (deduplicated)'
                })

            # BOTH keys are now CLAIMED, so from here on every failure path must
            # release them — not just the send. Mirrors
            # `agent_order_proposed_handler` below: the language read is a
            # Postgres round-trip and the rendering runs on backend-supplied
            # strings, so a 500 raised anywhere in this block with the keys still
            # held costs the summary the retry that would have delivered it.
            try:
                logger.info(
                    f"Sending delivery summary to telegram_id {telegram_id} "
                    f"for order {order_number or order_id}"
                )

                # Build the localized message (HTML). Title always; the bottle block
                # (incl. the balance line) is omitted for a genuinely non-bottle order
                # — the backend readiness guard guarantees zero/zero means "no ledger
                # row will ever exist", not "ledger not committed yet".
                language = await i18n.get_user_language(telegram_id)

                lines = [
                    i18n.get(
                        'telegram.delivery_summary.title', language,
                        order_number=order_number or str(order_id),
                    )
                ]
                if not (bottles_delivered == '0' and bottles_collected == '0'):
                    lines.append('')
                    lines.append(i18n.get(
                        'telegram.delivery_summary.bottles_delivered', language,
                        count=bottles_delivered,
                    ))
                    lines.append(i18n.get(
                        'telegram.delivery_summary.bottles_collected', language,
                        count=bottles_collected,
                    ))
                    lines.append(i18n.get(
                        'telegram.delivery_summary.balance', language,
                        count=balance,
                    ))
                message_text = '\n'.join(lines)

                # Lazy import mirrors payment_success_handler's `from keyboards import
                # PaymentKeyboards` — keeps keyboards out of module import time.
                from keyboards import DeliverySummaryKeyboards
                keyboard = DeliverySummaryKeyboards.summary(language, order_id, reorder_order_id)

                await self.bot_app.bot.send_message(
                    chat_id=telegram_id,
                    text=message_text,
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception:
                # Release BOTH layers. The order-id key is the load-bearing one
                # here — trigger_bot_webhook mints a FRESH X-Request-ID per Celery
                # attempt, so only layer 2 can catch the backend's retry — but a
                # request-id key left claimed still absorbs an exact HTTP replay of
                # THIS attempt, the one delivery nobody else re-sends. Re-raise into
                # the 500 branch below so the backend still sees the failure.
                if request_id:
                    await self._release_webhook_dedup(_DELIVERY_ENDPOINT, request_id)
                await self._release_order_dedup(order_id)
                raise

            return web.json_response({
                'success': True,
                'message': 'Notification sent'
            })

        except Exception as e:
            logger.error(f"Error processing delivery completed: {e}", exc_info=True)
            return web.json_response({
                'success': False,
                'message': 'Internal server error'
            }, status=500)

    async def agent_order_proposed_handler(self, request):
        """
        Handle agent-order-proposed webhook from backend.

        POST /internal/agent-order-proposed
        {
            "telegram_id": 12345,
            "order_id": 9001,
            "order_number": "SA_000123_26",
            "request_id": "a1b2c3d4e5f60718",
            "agent_name": "Dilshod",
            "outlet_name": "Bahor market, Yunusobod",   # which branch of the chain
            "delivery_address": "Yunusobod 12",         # the order's own delivery address
            "items": [{"name": "Pure Water 19L", "qty": 4}],
            "total": 60000.0,            # raw float; this bot formats it
            "delivery_date": "2026-09-10" # ISO date or null; this bot formats it
        }

        Shows the store an order a sales agent placed on their behalf, with
        Confirm / Decline buttons. Structure mirrors
        ``delivery_completed_handler``: HMAC verify, bot-app guard, JSON parse,
        required-field check, two-layer dedup, localized build, send.
        """
        try:
            # Verify signature
            if not await verify_webhook_signature(request):
                logger.warning(f"Invalid webhook signature from {request.remote}")
                return web.json_response({
                    'success': False,
                    'message': 'Invalid signature'
                }, status=401)

            if not self.bot_app:
                logger.error("Bot application not initialized in webhook server")
                return web.json_response({
                    'success': False,
                    'message': 'Bot not initialized'
                }, status=503)

            # Parse request data
            try:
                data = await request.json()
            except Exception:
                return web.json_response({
                    'success': False,
                    'message': 'Invalid JSON'
                }, status=400)

            order_id = data.get('order_id')
            telegram_id = data.get('telegram_id')
            order_number = data.get('order_number')
            agent_name = data.get('agent_name')
            outlet_name = data.get('outlet_name')
            delivery_address = data.get('delivery_address')
            items = data.get('items') or []
            total = data.get('total')
            delivery_date = data.get('delivery_date')

            if not order_id or not telegram_id:
                return web.json_response({
                    'success': False,
                    'message': 'Missing required fields: order_id, telegram_id'
                }, status=400)

            # Two-layer dedup — but NOT with delivery_completed_handler's
            # division of labour. This route's X-Request-ID is STABLE across
            # Celery retries: push_agent_order_confirmation passes the
            # confirmation row's own 16-hex `request_id` column to
            # trigger_bot_webhook, where "same id across retries collapses to
            # one bot notification" is the documented contract. (The delivered
            # summary passes NO request id, so every attempt of that one gets a
            # fresh one.) So here layer 1 collapses both an exact HTTP replay
            # AND the backend's retries; layer 2 (order_id) still collapses a
            # re-push carrying a different id — a second confirmation row for
            # the same order. Both layers live under ONE endpoint name — the key
            # VALUES ('order:9001' vs a 16-hex request id) can never collide —
            # so the failure branch below has a single endpoint to release.
            #
            # The header is the primary carrier, but the SAME id is in the body
            # (push_agent_order_confirmation puts the confirmation row's
            # `request_id` in both), and the body is the half that survives a
            # hop that strips unknown headers. Falling back to it keeps layer 1
            # — the layer that collapses this route's stable-id Celery retries —
            # working when the header does not arrive.
            request_id = request.headers.get('X-Request-ID') or data.get('request_id')
            if request_id and await self._is_duplicate_webhook(_AGENT_ORDER_ENDPOINT, request_id):
                logger.info(
                    "Duplicate agent-order webhook (request_id=%s order=%s), skipping",
                    request_id, order_id,
                )
                return web.json_response({
                    'success': True,
                    'message': 'Already processed (deduplicated)'
                })
            if await self._is_duplicate_webhook(_AGENT_ORDER_ENDPOINT, f"order:{order_id}"):
                logger.info("Duplicate agent-order webhook for order %s, skipping", order_id)
                return web.json_response({
                    'success': True,
                    'message': 'Already processed (deduplicated)'
                })

            # BOTH keys are now CLAIMED, so from here on every failure path
            # must release them — not just the send. The language read is a
            # Postgres round-trip and the rendering runs on backend-supplied
            # data; a 500 raised anywhere in this block with the keys still held
            # loses the proposal for good, because the backend's Celery retry
            # carries the SAME stable request id and layer 1 would answer it
            # "already processed".
            try:
                logger.info(
                    f"Sending agent-order proposal to telegram_id {telegram_id} "
                    f"for order {order_number or order_id}"
                )

                language = await i18n.get_user_language(telegram_id)

                # Lazy import mirrors the `from keyboards import KeyboardBuilder`
                # below — display helpers stay out of module import time.
                from utils import format_date, format_price

                item_lines = [
                    i18n.get(
                        'telegram.agent_order.item', language,
                        name=_html_escaped(item.get('name')),
                        qty=_html_escaped(item.get('qty') or 0),
                    )
                    for item in items
                ]

                # `delivery_date`, `outlet_name` and `delivery_address` are all nullable on
                # the backend side, and shared.i18n_rendering degrades a template it cannot
                # fill to the humanised key (raw English debug text). A dash is a value;
                # None is not. (`_html_escaped(None)` is '', which is why the `or` is here
                # and not inside the helper.)
                message_text = i18n.get(
                    'telegram.agent_order.proposed', language,
                    agent_name=_html_escaped(agent_name),
                    order_number=_html_escaped(order_number or order_id),
                    outlet_name=_html_escaped(outlet_name) or '—',
                    address=_html_escaped(delivery_address) or '—',
                    items='\n'.join(item_lines) or '—',
                    total=format_price(float(total or 0)),
                    delivery_date=format_date(delivery_date) or '—',
                )

                from keyboards import KeyboardBuilder
                keyboard = KeyboardBuilder.build_inline_keyboard([[
                    {
                        'text': i18n.get('telegram.agent_order.confirm_button', language),
                        'callback_data': f'agent_order_confirm_{order_id}',
                    },
                    {
                        'text': i18n.get('telegram.agent_order.decline_button', language),
                        'callback_data': f'agent_order_decline_{order_id}',
                    },
                ]])

                await self.bot_app.bot.send_message(
                    chat_id=telegram_id,
                    text=message_text,
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception:
                # Failed AFTER BOTH dedup keys were claimed. Release BOTH, or
                # the backend's Celery retry is absorbed and the proposal is
                # lost for good — an unanswered proposal rides the confirmation
                # TTL into an auto-confirm the store never agreed to. The
                # request-id key matters most here, precisely because this
                # route's X-Request-ID is the confirmation row's own stable id:
                # the retry arrives under the very key just claimed, so
                # releasing only the order key would leave layer 1 swallowing
                # every attempt.
                if request_id:
                    await self._release_webhook_dedup(_AGENT_ORDER_ENDPOINT, request_id)
                await self._release_order_dedup(order_id, endpoint=_AGENT_ORDER_ENDPOINT)
                raise

            return web.json_response({
                'success': True,
                'message': 'Notification sent'
            })

        except Exception as e:
            logger.error(f"Error processing agent order proposed: {e}", exc_info=True)
            return web.json_response({
                'success': False,
                'message': 'Internal server error'
            }, status=500)

    async def reorder_reminder_handler(self, request):
        """
        Handle reorder-reminder webhook from backend (spec 2026-10-09 §7.3).

        POST /internal/reorder-reminder
        {
            "telegram_id": "810000001",
            "episode_id": 77,
            "step": 1,                     # 1 or 2
            "silent": false,               # step >= 2 -> disable_notification
            "show_later": true,            # R1 of a two-step ladder only
            "name": "Alice",               # shared.user_names.greeting_name, or null
            "days": 12,                    # whole local days since the supply delivery
            "gap": 7,                      # usual gap in days, or null on the preset
            "snooze_days": 14,
            "reorder_order_id": 9001,      # the recipient's own reorder candidate, or null: never a
                                           # subscription delivery, never a reward-only order
                                           # (OrderService.reorder_candidate)
            "items": [{"qty": 2, "names": {"uz": "Toza suv 19 l", "ru": "Чистая вода 19 л",
                                           "en": "Pure Water 19L"}}],
            "language": "ru",              # shared.i18n_rendering.reminder_language, resolved
            "timestamp": "2026-09-13T05:00:00+00:00"   # added by trigger_bot_webhook; unused
        }

        `language` is THE reminder language, decided once by the backend
        (`shared.i18n_rendering.reminder_language` over the recipient's stored value; final review
        B1/E1), the one the email is written in and the worklist names. It is the only language
        that reaches the copy, the plurals, the item names and the buttons: this route never reads
        the customer's stored language, so the bot cannot answer the question a second way. Each
        item names its product in all three languages (master amendment 13) and the composer
        prints the one in `language`, so a line and its sentence never disagree. A payload whose
        `language` is not one of the seeded codes is refused (400) before any claim is taken.

        Built by business_app/services/reorder_reminders/messaging.py::telegram_payload and
        sent by its `_send_telegram` with X-Request-ID `reorder-reminder:<episode>:<step>`,
        stable across the Celery retries and the daily run's re-publish. Structure mirrors
        `agent_order_proposed_handler`. Three differences: the copy is assembled by
        `shared.reorder_reminder_copy` (the assembly the email uses too), both dedup claims are
        held `REORDER_REMINDER_CLAIM_TTL_SECONDS`, and a send Telegram refuses because the
        customer blocked the bot answers 410 BOT_BLOCKED, with both claims released.
        """
        try:
            if not await verify_webhook_signature(request):
                logger.warning(f"Invalid webhook signature from {request.remote}")
                return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)

            if not self.bot_app:
                logger.error("Bot application not initialized in webhook server")
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            try:
                data = await request.json()
            except Exception:
                return web.json_response({'success': False, 'message': 'Invalid JSON'}, status=400)

            telegram_id = data.get('telegram_id')
            episode_id = data.get('episode_id')
            step = data.get('step')
            language = data.get('language')
            if not telegram_id or not episode_id or not step:
                return web.json_response(
                    {'success': False, 'message': 'Missing required fields: telegram_id, episode_id, step'}, status=400
                )
            if language not in SUPPORTED_LANGUAGES:
                return web.json_response(
                    {'success': False, 'message': f'language must be one of {", ".join(SUPPORTED_LANGUAGES)}'},
                    status=400,
                )

            # Layer 1, the stable X-Request-ID, collapses an HTTP replay, the backend's Celery
            # retries and the run's re-publish of a queued send. Layer 2, (episode, step),
            # collapses a second push of the same reminder under another id. One endpoint
            # name, so the failure branches release both.
            request_id = request.headers.get('X-Request-ID')
            episode_key = f"episode:{episode_id}:step:{step}"
            if request_id and await self._is_duplicate_webhook(
                _REORDER_REMINDER_ENDPOINT, request_id, REORDER_REMINDER_CLAIM_TTL_SECONDS
            ):
                logger.info("Duplicate reorder-reminder webhook (request_id=%s), skipping", request_id)
                return web.json_response({'success': True, 'message': 'Already processed (deduplicated)'})
            if await self._is_duplicate_webhook(
                _REORDER_REMINDER_ENDPOINT, episode_key, REORDER_REMINDER_CLAIM_TTL_SECONDS
            ):
                logger.info("Duplicate reorder-reminder webhook for %s, skipping", episode_key)
                return web.json_response({'success': True, 'message': 'Already processed (deduplicated)'})

            async def release_claims():
                if request_id:
                    await self._release_webhook_dedup(_REORDER_REMINDER_ENDPOINT, request_id)
                await self._release_webhook_dedup(_REORDER_REMINDER_ENDPOINT, episode_key)

            # BOTH keys are claimed, so every failure from here on releases them. The copy and
            # the keyboard are built from backend data; a held key would answer the Celery retry
            # "already processed" and lose the reminder for good.
            try:
                from keyboards import ReorderReminderKeyboards

                name = data.get('name')
                gap = data.get('gap')
                # Names are free text an operator typed and the message is sent as HTML, so
                # they are escaped BEFORE the composer sees them. `qty` is passed as sent: an
                # int cannot carry markup, and the composer refuses any line whose quantity is
                # not a positive int (`reorder_reminder_copy.printable`).
                items = [
                    {
                        'qty': item.get('qty'),
                        'names': {code: _html_escaped(value) for code, value in (item.get('names') or {}).items()},
                    }
                    for item in (data.get('items') or [])
                ]
                text = reorder_reminder_copy.telegram_text(
                    i18n.renderer(language),
                    language,
                    step=int(step),
                    name=_html_escaped(name) if name else None,
                    days=int(data.get('days') or 0),
                    gap=int(gap) if gap is not None else None,
                    items=items,
                )
                keyboard = ReorderReminderKeyboards.reminder(
                    language,
                    episode_id=int(episode_id),
                    reorder_order_id=_order_id_or_none(data.get('reorder_order_id')),
                    show_later=bool(data.get('show_later')),
                    snooze_days=int(data.get('snooze_days') or 0),
                )
                await self.bot_app.bot.send_message(
                    chat_id=telegram_id,
                    text=text,
                    parse_mode='HTML',
                    reply_markup=keyboard,
                    disable_notification=bool(data.get('silent')),
                )
            except Forbidden as exc:
                await release_claims()
                logger.info("Reorder reminder %s not delivered, bot blocked: %s", episode_key, exc)
                return web.json_response(
                    {'success': False, 'error_code': _BOT_BLOCKED_CODE, 'message': 'Bot was blocked by the user'},
                    status=410,
                )
            except Exception:
                await release_claims()
                raise

            return web.json_response({'success': True, 'message': 'Notification sent'})

        except Exception as e:
            logger.error(f"Error processing reorder reminder: {e}", exc_info=True)
            return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)

    async def customer_notice_handler(self, request):
        """
        Handle a customer notice from the backend (plan 2026-10-24 Task 3; spec 2026-10-23 §4.4, §4.5, §9).

        POST /internal/customer-notice
        {
            "notice_id": "subscription-log:41",   # stable across the backend's retries: one message per id
            "kind": "subscription_order_created", # | subscription_delivery_skipped | subscription_paid_order_kept
                                                  # | order_payment_expired | subscription_schedule_changed
            "telegram_id": "810000001",
            "language": "ru",                     # resolved by the backend; never read from the customer here
            ... the kind's facts (business_app/services/customer_notices.py documents each set) ...
            "timestamp": "..."                    # added by trigger_bot_webhook; unused
        }

        Built and sent by business_app/services/customer_notices.py from Celery tasks published after the
        commit that caused the notice. Structure mirrors `reorder_reminder_handler`: HMAC verify, bot-app
        guard, JSON parse, the payload checked BEFORE the claim (a broken one is 400 and claims nothing), one
        Redis claim on `notice_id`, render, send. Every failure after the claim releases it; a send Telegram
        refuses because the customer blocked the bot answers 410 BOT_BLOCKED (the backend marks the account
        and emails instead). Buttons: Pay now for an unpaid Click order (`retry_payment`'s callback) and View
        order, from `keyboards.CustomerNoticeKeyboards`.
        """
        try:
            if not await verify_webhook_signature(request):
                logger.warning(f"Invalid webhook signature from {request.remote}")
                return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)

            if not self.bot_app:
                logger.error("Bot application not initialized in webhook server")
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            try:
                data = await request.json()
            except Exception:
                return web.json_response({'success': False, 'message': 'Invalid JSON'}, status=400)

            problem = _customer_notice_problem(data)
            if problem:
                return web.json_response({'success': False, 'message': problem}, status=400)

            notice_id = str(data['notice_id'])
            if await self._is_duplicate_webhook(_CUSTOMER_NOTICE_ENDPOINT, notice_id):
                logger.info("Duplicate customer notice %s, skipping", notice_id)
                return web.json_response({'success': True, 'message': 'Already processed (deduplicated)'})

            # The claim is held from here on: every failure releases it, or the backend's retry (same
            # notice_id) would be answered "already processed" and the notice lost for good.
            try:
                from keyboards import CustomerNoticeKeyboards

                language = data['language']
                keyboard = CustomerNoticeKeyboards.notice(
                    language,
                    order_id=_order_id_or_none(data.get('order_id')),
                    pay_now=data['kind'] == _NOTICE_ORDER_CREATED and data.get('payment') == 'online_unpaid',
                )
                await self.bot_app.bot.send_message(
                    chat_id=data['telegram_id'],
                    text=_customer_notice_text(language, data),
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Forbidden as exc:
                await self._release_webhook_dedup(_CUSTOMER_NOTICE_ENDPOINT, notice_id)
                logger.info("Customer notice %s not delivered, bot blocked: %s", notice_id, exc)
                return web.json_response(
                    {'success': False, 'error_code': _BOT_BLOCKED_CODE, 'message': 'Bot was blocked by the user'},
                    status=410,
                )
            except Exception:
                await self._release_webhook_dedup(_CUSTOMER_NOTICE_ENDPOINT, notice_id)
                raise

            return web.json_response({'success': True, 'message': 'Notification sent'})

        except Exception as e:
            logger.error(f"Error processing customer notice: {e}", exc_info=True)
            return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)

    async def start(self):
        """Start the webhook server"""
        try:
            if not self.app:
                await self.setup()

            self.runner = web.AppRunner(self.app)
            await self.runner.setup()

            self.site = web.TCPSite(self.runner, self.host, self.port)
            await self.site.start()

            logger.info(f"Webhook server started on http://{self.host}:{self.port}")
            logger.info("Available endpoints:")
            logger.info(f"  POST http://{self.host}:{self.port}/internal/reload-translations")
            logger.info(f"  POST http://{self.host}:{self.port}/internal/payment-success")
            logger.info(f"  POST http://{self.host}:{self.port}/internal/delivery-completed")
            logger.info(f"  POST http://{self.host}:{self.port}/internal/{_AGENT_ORDER_ENDPOINT}")
            logger.info(f"  POST http://{self.host}:{self.port}/internal/{_REORDER_REMINDER_ENDPOINT}")
            logger.info(f"  POST http://{self.host}:{self.port}/internal/{_CUSTOMER_NOTICE_ENDPOINT}")
            logger.info(f"  GET  http://{self.host}:{self.port}/internal/stats")
            logger.info(f"  GET  http://{self.host}:{self.port}/health")

        except Exception as e:
            logger.error(f"Failed to start webhook server: {e}", exc_info=True)
            raise

    async def stop(self):
        """Stop the webhook server"""
        try:
            if self.site:
                await self.site.stop()

            if self.runner:
                await self.runner.cleanup()

            logger.info("Webhook server stopped")

        except Exception as e:
            logger.error(f"Error stopping webhook server: {e}", exc_info=True)


# Global webhook server instance
webhook_server = WebhookServer(
    host='0.0.0.0',
    port=int(os.environ.get('WEBHOOK_SERVER_PORT', '8080'))
)
