"""
Internal Webhook Server for Staff Bot
Receives webhooks from backend for staff notifications (new orders, assignments, etc.)
"""
import asyncio
import hmac
import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional, Tuple
from aiohttp import web
import redis.asyncio as redis

from staff_bot.config import config
from staff_bot.database import db_manager
from staff_bot.i18n import i18n
from staff_bot.utils import flow_state
from staff_bot.utils.formatters import escape_html
from shared.redis_failure import report_redis_failure
from shared.redis_keyspace import RedisKeyspace

logger = logging.getLogger(__name__)

try:  # telegram is always present in the bot runtime; guard keeps imports safe.
    from telegram.error import Forbidden as _TelegramForbidden
except Exception:  # pragma: no cover - defensive
    _TelegramForbidden = ()

# Substrings of benign "this recipient can't receive messages" Telegram errors.
# These are operational facts (the user blocked the bot / deactivated their
# account), not server faults — they belong at WARNING, not ERROR. A single
# blocked driver otherwise emits an ERROR on every broadcast.
_UNREACHABLE_RECIPIENT_MARKERS = (
    "bot was blocked by the user",
    "user is deactivated",
    "chat not found",
    "bots can't send messages to bots",
    "have no rights to send",
    "peer_id_invalid",
)


def _is_recipient_unreachable(exc: Exception) -> bool:
    """True when ``exc`` means a specific recipient simply can't be messaged.

    Such failures are expected at the edges (drivers block the bot, delete their
    account, etc.) and must not be logged at ERROR or they drown the error feed.
    """
    if _TelegramForbidden and isinstance(exc, _TelegramForbidden):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _UNREACHABLE_RECIPIENT_MARKERS)


def _log_notify_failure(description: str, exc: Exception) -> None:
    """Log a per-recipient notification failure at the right severity.

    WARNING for benign unreachable recipients; ERROR (the genuine-fault level)
    otherwise.
    """
    if _is_recipient_unreachable(exc):
        logger.warning("%s (recipient unreachable): %s", description, exc)
    else:
        logger.error("%s: %s", description, exc)


class _TokenBucket:
    """Per-endpoint token bucket rate limiter — dep-free, single-process.

    The webhook endpoints are only meant to receive traffic from our own
    backend, but the URL is reachable from inside the cluster's network and
    a leaked secret would let an attacker flood any single endpoint. This
    bucket lets the legitimate backend traffic through (well under any sane
    rate) while clamping the worst case to `rate_per_sec` sustained with a
    `burst` headroom for the natural batching the backend already does.
    """

    __slots__ = ("rate", "capacity", "_tokens", "_last", "_lock")

    def __init__(self, rate_per_sec: float, burst: int):
        self.rate = float(rate_per_sec)
        self.capacity = float(burst)
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def try_acquire(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


async def _parse_json_body(request) -> Tuple[Optional[dict], Optional[web.Response]]:
    """Parse the request body as JSON.

    Returns (data, None) on success, or (None, error_response) on failure.
    The previous code let `await request.json()` propagate to the catch-all
    `except Exception` block which always returned 500 — masking client
    errors as server errors and making it harder to spot a misbehaving
    caller in the logs. 400 is the right status for malformed input.
    """
    try:
        return await request.json(), None
    except Exception as e:
        logger.warning(f"Webhook JSON parse failed for {request.path}: {e}")
        return None, web.json_response(
            {'success': False, 'message': 'Invalid JSON body'}, status=400
        )


async def verify_webhook_signature(request):
    """Verify webhook signature for security"""
    signature = request.headers.get('X-Bot-Webhook-Signature')
    if not signature:
        return False

    # Use only the dedicated webhook secret. JWT_SECRET_KEY belongs to a
    # different trust domain (auth tokens); falling back to it here would
    # let a JWT-secret leak forge webhooks. Mismatch with backend signer
    # surfaces as a 401 with a clear log line — failing closed by design.
    webhook_secret = config.security.webhook_secret
    if not webhook_secret:
        logger.error("WEBHOOK_SECRET not configured")
        return False

    body = await request.read()
    expected_signature = hmac.new(
        webhook_secret.encode('utf-8'),
        body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(signature, expected_signature)


async def reload_translations_handler(request):
    """Handle translation reload webhook - POST /internal/reload-translations"""
    try:
        if not await verify_webhook_signature(request):
            return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)

        # The translation-reload bucket is the tightest in the set (1/s, burst
        # of 5) — translations are seeded by an admin action, so anything
        # faster than that is either a runaway script or a flood.
        server = request.app.get('staff_webhook_server')
        if server is not None:
            limited = await server._check_rate_limit(request)
            if limited:
                return limited

        await i18n.reload_translations()
        return web.json_response({
            'success': True,
            'message': 'Staff translations reloaded',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'translation_count': sum(len(keys) for keys in i18n.translations.values())
        })
    except Exception as e:
        logger.error(f"Error reloading translations: {e}", exc_info=True)
        return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)


async def health_handler(_request):
    """Health check - GET /health"""
    checks = {}
    overall_healthy = True

    try:
        translation_count = sum(len(keys) for keys in i18n.translations.values())
        if translation_count > 0:
            missing = i18n.get_missing_translation_keys()
            if not missing:
                checks['translations'] = {'status': 'ok', 'total_count': translation_count}
            else:
                missing_summary = {
                    lang: {
                        'missing_count': len(keys),
                        'sample': keys[:5],
                    }
                    for lang, keys in missing.items()
                }
                checks['translations'] = {
                    'status': 'error',
                    'total_count': translation_count,
                    'missing_by_language': missing_summary,
                }
                overall_healthy = False
        else:
            checks['translations'] = {
                'status': 'error',
                'error': 'No translations loaded',
                'total_count': 0
            }
            overall_healthy = False
    except Exception as e:
        checks['translations'] = {'status': 'error', 'error': str(e)}
        overall_healthy = False

    try:
        if db_manager.pool:
            async with db_manager.pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            checks['database'] = {
                'status': 'ok',
                'pool_size': db_manager.pool.get_size(),
                'pool_free': db_manager.pool.get_idle_size()
            }
        else:
            checks['database'] = {'status': 'not_connected'}
            overall_healthy = False
    except Exception as e:
        checks['database'] = {'status': 'error', 'error': str(e)}
        overall_healthy = False

    status_code = 200 if overall_healthy else 503
    return web.json_response({
        'status': 'healthy' if overall_healthy else 'degraded',
        'service': 'staff_bot',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'checks': checks
    }, status=status_code)


async def handle_route_updated_payload(server, data: dict) -> bool:
    """Apply one parsed `/internal/route-updated` payload.

    Returns True iff this call caused a new observable action -- a card
    was actually refreshed (create or edit), or a sounded push was
    attempted (Telegram-side failure there is swallowed below, matching
    existing best-effort semantics -- "attempted" still counts). Returns
    False when the call was a genuine no-op: the event was already
    deduped, or `update_card_for_driver` itself determined nothing should
    change (no cached token, borrowed card, API failure). The caller
    (`route_updated_handler`) does not currently branch on this, but a
    meaningful value beats an unconditional True that can never be wrong
    (fix round 1, M4) -- callers and tests can tell "something happened"
    from "this call was inert".

    Callers must have already validated `data['telegram_id']` is present
    and int-coercible -- see `route_updated_handler`. This function assumes
    it.

    THE GATE IS PLAN 1'S: `data["sound"]` is the backend's already-computed
    verdict on whether this update is worth interrupting the driver for.
    This function reads that field and MUST NOT re-derive it from
    `head_changed` / `set_changed` / `sequence_changed` / `driver_initiated`
    -- re-deciding materiality here would put the same rule in two places
    (CLAUDE.md SSOT). See `docs/*route-ux*` Plan 1 for the gate itself.

    sound=False (the common case, and the whole point of the user's original
    complaint -- "your address is updated" on every arrival is noise): NO
    chat message, ever. The driver's route card quietly becomes correct via
    `route_card.update_card_for_driver` (Task 5), which edits the existing
    card in place -- `editMessageText` produces no notification -- or, on
    first contact for that driver, sends + pins exactly one silent card
    (`disable_notification=True` on both calls; see `route_card._create_card`).
    Checked BEFORE dedup so a silent event never consumes the dedup slot of
    a later sounded one -- the bug shape `test_silent_then_sounded_is_not_deduped_away`
    guards: a constant-key dedup fallback swallowing a genuine sounded push.
    Trade-off accepted for that ordering (fix round 1, M3): a silent event
    is never deduped at all, so a backend retry of the SAME silent event
    costs one extra `GET /delivery/active` round trip (idempotent -- the
    card content-signature check skips the actual Telegram edit when
    nothing changed). Cheaper than the alternative: `_is_duplicate_event`'s
    fallback key is a *constant* per driver, so checking it before the
    sound gate would let one silent push swallow every later push --
    including genuine sounded ones -- for the full 24h dedup TTL during a
    Redis outage.

    sound=True or missing (an older backend that predates the `sound` field
    keeps today's behaviour, fail-open toward sounding): Task 9's restyled,
    capped `route_card.send_head_change_alert` -- the ONLY sounded message
    this plan sends. It refreshes the card alongside the alert (the alert is
    a pointer, not a data carrier -- Plan 3's Behaviour note), throttled by
    `ROUTE_ALERT_MIN_INTERVAL_SECONDS`: outside the window it pings, inside
    it a newest-supersedes silent alert replaces the previous one. Dedup is
    still checked first here (unlike the silent branch above) -- a sounded
    push is exactly the kind of driver-visible event a backend retry must
    not double-send.
    """
    from staff_bot.handlers.delivery import route_card

    telegram_id = int(data['telegram_id'])
    driver_id = data.get('driver_id')
    event_id = data.get('event_id')

    if not data.get('sound', True):
        return await route_card.update_card_for_driver(server.bot_app, telegram_id)

    if await server._is_duplicate_event(event_id, f"route_updated:{telegram_id}:{driver_id}"):
        return False

    await route_card.send_head_change_alert(server.bot_app, telegram_id=telegram_id)
    return True


class StaffWebhookServer:
    """Internal webhook server for staff bot notifications"""

    def __init__(self, host='0.0.0.0', port=8081):
        self.host = host
        self.port = port
        self.app = None
        self.runner = None
        self.site = None
        self.bot_app = None
        self._processed_events: dict = {}
        self._dedup_ttl = int(os.environ.get('STAFF_WEBHOOK_DEDUP_TTL_SECONDS', '86400'))
        self._redis: Optional[redis.Redis] = None
        self._redis_connected = False
        # Per-endpoint rate limits. Sized to comfortably absorb the backend's
        # legitimate traffic — broadcast `new-order` happens once per pool
        # insertion (single-digit / minute peak), the others are per-driver
        # actions (low double-digit / minute peak). The limits below are 50×
        # the realistic peaks, so legitimate callers never see 429s; the
        # bucket exists purely to clamp a rogue / leaked-key flood.
        self._rate_limiters = {
            '/internal/new-order': _TokenBucket(rate_per_sec=20, burst=60),
            '/internal/order-assigned': _TokenBucket(rate_per_sec=20, burst=60),
            '/internal/order-reassigned': _TokenBucket(rate_per_sec=20, burst=60),
            '/internal/order-cancelled': _TokenBucket(rate_per_sec=20, burst=60),
            '/internal/order-unassigned': _TokenBucket(rate_per_sec=20, burst=60),
            '/internal/route-updated': _TokenBucket(rate_per_sec=50, burst=120),
            '/internal/pool-insertion-suggestion': _TokenBucket(rate_per_sec=50, burst=120),
            '/internal/reload-translations': _TokenBucket(rate_per_sec=1, burst=5),
        }

    async def _check_rate_limit(self, request) -> Optional[web.Response]:
        """Return a 429 response if the per-endpoint token bucket is empty,
        otherwise None. Called at the top of each handler after the signature
        check so we don't account unauthorized traffic against the legitimate
        rate budget."""
        limiter = self._rate_limiters.get(request.path)
        if limiter is None:
            return None
        if await limiter.try_acquire():
            return None
        logger.warning(f"Webhook rate-limited: {request.path}")
        return web.json_response(
            {'success': False, 'message': 'Rate limit exceeded'}, status=429
        )

    def set_application(self, application):
        """Set Telegram Application instance"""
        self.bot_app = application

    async def setup(self):
        """Setup webhook server routes"""
        self.app = web.Application(client_max_size=1024 * 1024)
        # Stash a back-reference so the module-level handlers
        # (reload_translations_handler, health_handler) can reach the
        # rate-limit table without a global.
        self.app['staff_webhook_server'] = self

        self.app.router.add_post('/internal/new-order', self.new_order_handler)
        self.app.router.add_post('/internal/order-assigned', self.order_assigned_handler)
        self.app.router.add_post('/internal/order-reassigned', self.order_reassigned_handler)
        self.app.router.add_post('/internal/order-cancelled', self.order_cancelled_handler)
        self.app.router.add_post('/internal/order-unassigned', self.order_unassigned_handler)
        self.app.router.add_post('/internal/reload-translations', reload_translations_handler)
        self.app.router.add_post('/internal/route-updated', self.route_updated_handler)
        self.app.router.add_post('/internal/pool-insertion-suggestion', self.pool_insertion_suggestion_handler)
        self.app.router.add_get('/health', health_handler)
        self.app.router.add_get('/internal/stats', self.stats_handler)

        await self._init_redis()

        logger.info(f"Staff webhook server configured on {self.host}:{self.port}")

    async def _init_redis(self):
        """Initialize Redis connection for webhook idempotency keys."""
        try:
            self._redis = redis.from_url(
                config.redis.url,
                encoding='utf-8',
                decode_responses=True,
            )
            await self._redis.ping()
            self._redis_connected = True
            logger.info("Webhook server Redis dedup enabled")
        except Exception as e:
            self._redis_connected = False
            self._redis = None
            # RED-005: TIER_RELIABILITY — cross-replica dedup requires Redis.
            # In-memory fallback only works for single-replica deployments, so
            # ops must know the moment this degrades.
            report_redis_failure(
                "staff_bot.webhook_server.init_redis", str(e), tier="reliability"
            )

    def _deduplicate(self, event_key: str) -> bool:
        """Return True if this event was already processed recently."""
        now = datetime.now(timezone.utc)
        self._processed_events = {
            k: v for k, v in self._processed_events.items()
            if (now - v).total_seconds() < self._dedup_ttl
        }
        if event_key in self._processed_events:
            return True
        self._processed_events[event_key] = now
        return False

    async def _is_duplicate_event(self, event_id: str, fallback_key: str) -> bool:
        """
        Return True if event was already processed.
        Uses Redis when event_id is provided; otherwise falls back to in-memory dedup.
        """
        if event_id and self._redis_connected and self._redis:
            key = RedisKeyspace.staff_bot_webhook_event(event_id)
            try:
                created = await self._redis.set(
                    key,
                    datetime.now(timezone.utc).isoformat(),
                    ex=self._dedup_ttl,
                    nx=True,
                )
                return not bool(created)
            except Exception as e:
                # RED-005: TIER_RELIABILITY — in-memory fallback loses dedup
                # across replicas. Alert so ops treats a sustained failure as
                # a priority fix, not a silent degradation.
                report_redis_failure(
                    "staff_bot.webhook_server.is_duplicate_event", str(e), tier="reliability"
                )

        return self._deduplicate(fallback_key)

    async def stats_handler(self, _request):
        """Internal stats endpoint - GET /internal/stats"""
        now = datetime.now(timezone.utc)
        # Cleanup stale in-memory keys before reporting.
        self._deduplicate("__stats_cleanup__")
        self._processed_events.pop("__stats_cleanup__", None)

        return web.json_response({
            'success': True,
            'service': 'staff_bot',
            'timestamp': now.isoformat(),
            'dedup': {
                'ttl_seconds': self._dedup_ttl,
                'redis_enabled': self._redis_connected,
                'in_memory_keys': len(self._processed_events),
            }
        })

    async def new_order_handler(self, request):
        """Broadcast a freshly-created pool order to every eligible driver
        with an inline Accept/Decline UX. First driver to Accept wins
        (server-side row lock returns 409 to the rest, which the bot
        gracefully renders as 'already taken').

        POST /internal/new-order
        """
        try:
            if not await verify_webhook_signature(request):
                return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)

            limited = await self._check_rate_limit(request)
            if limited:
                return limited

            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data, parse_error = await _parse_json_body(request)
            if parse_error:
                return parse_error
            order_id = data.get('order_id')
            event_id = data.get('event_id')

            if await self._is_duplicate_event(event_id, f"new_order:{order_id}"):
                return web.json_response({'success': True, 'message': 'Already processed'})

            telegram_ids = data.get('delivery_person_telegram_ids', [])
            order_info = data.get('order_info', {})
            delivery_id = order_info.get('delivery_id')

            # Without a delivery_id we can't render Accept buttons that wire
            # into the standard accept flow — log loudly so the missing
            # field gets fixed at the source rather than silently skipped.
            if not delivery_id:
                logger.error(
                    "new_order broadcast missing delivery_id (order=%s) — Accept/Decline UX disabled",
                    order_id,
                )

            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            sent_count = 0
            failed_ids = []
            for tid in telegram_ids:
                try:
                    language = await i18n.get_user_language(int(tid))
                    message = self._format_new_order_message(order_info, language)
                    keyboard = None
                    if delivery_id:
                        # Accept reuses the existing confirm-accept callback so
                        # the broadcast and pool browse share one downstream
                        # flow (auth, row lock, location prompt, re-opt).
                        keyboard = InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton(
                                    f"✅ {i18n.get('staff.delivery.accept', language)}",
                                    callback_data=f"staff_confirm_accept_{int(delivery_id)}",
                                ),
                                InlineKeyboardButton(
                                    f"❌ {i18n.get('staff.cancel', language)}",
                                    callback_data=f"staff_decline_suggestion_{int(delivery_id)}",
                                ),
                            ]
                        ])
                    await self.bot_app.bot.send_message(
                        chat_id=tid, text=message, parse_mode='HTML', reply_markup=keyboard,
                    )
                    sent_count += 1
                except Exception as e:
                    failed_ids.append(int(tid) if isinstance(tid, (int, str)) else tid)
                    _log_notify_failure(f"Failed to notify delivery person {tid}", e)

            # F-3: report partial success. The previous return shape was always
            # `success: True`, even when 0/N sends landed — the backend had no
            # signal to retry the failed recipients. Now success means "every
            # send succeeded"; partial_success surfaces the mixed case so the
            # backend can re-emit for the failed_ids without re-broadcasting
            # to the ones that already received the message.
            total = len(telegram_ids)
            full_success = sent_count == total
            return web.json_response({
                'success': full_success,
                'partial_success': not full_success and sent_count > 0,
                'sent_count': sent_count,
                'failed_count': total - sent_count,
                'failed_telegram_ids': failed_ids,
                'message': f'Notified {sent_count}/{total} delivery persons',
            })
        except Exception as e:
            logger.error(f"Error handling new order notification: {e}", exc_info=True)
            return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)

    async def order_assigned_handler(self, request):
        """
        Notify delivery person that an order was assigned to them by admin.
        POST /internal/order-assigned
        """
        try:
            if not await verify_webhook_signature(request):
                return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)
            limited = await self._check_rate_limit(request)
            if limited:
                return limited
            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data, parse_error = await _parse_json_body(request)
            if parse_error:
                return parse_error
            telegram_id = data.get('telegram_id')
            order_info = data.get('order_info', {})
            event_id = data.get('event_id')

            if await self._is_duplicate_event(event_id, f"order_assigned:{telegram_id}:{order_info.get('order_number', '')}"):
                return web.json_response({'success': True, 'message': 'Already processed'})

            if not telegram_id:
                return web.json_response({'success': False, 'message': 'Missing telegram_id'}, status=400)

            language = await i18n.get_user_language(int(telegram_id))
            message = i18n.get('staff.notification.order_assigned', language,
                               number=order_info.get('order_number', ''))
            await self.bot_app.bot.send_message(chat_id=telegram_id, text=message)

            return web.json_response({'success': True, 'message': 'Notification sent'})
        except Exception as e:
            logger.error(f"Error handling order assigned notification: {e}", exc_info=True)
            return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)

    async def order_reassigned_handler(self, request):
        """
        Notify both old and new delivery persons about reassignment.
        POST /internal/order-reassigned
        """
        try:
            if not await verify_webhook_signature(request):
                return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)
            limited = await self._check_rate_limit(request)
            if limited:
                return limited
            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data, parse_error = await _parse_json_body(request)
            if parse_error:
                return parse_error
            old_telegram_id = data.get('old_telegram_id')
            new_telegram_id = data.get('new_telegram_id')
            order_info = data.get('order_info', {})
            event_id = data.get('event_id')

            if await self._is_duplicate_event(
                event_id,
                f"order_reassigned:{old_telegram_id}:{new_telegram_id}:{order_info.get('order_number', '')}"
            ):
                return web.json_response({'success': True, 'message': 'Already processed'})

            # F-3: track each leg of the reassignment so the caller can tell
            # if neither, one, or both of the drivers were notified. Useful
            # because reassignment is the rare case where a half-success
            # (only the new driver got the ping) is still meaningful — they
            # at least know the order is theirs — but the backend may want to
            # retry for the old driver to clear their stale "you're assigned"
            # state.
            failed = []
            sent = 0

            if old_telegram_id:
                try:
                    lang = await i18n.get_user_language(int(old_telegram_id))
                    msg = i18n.get('staff.notification.order_reassigned_from', lang,
                                   number=order_info.get('order_number', ''))
                    await self.bot_app.bot.send_message(chat_id=old_telegram_id, text=msg)
                    sent += 1
                except Exception as e:
                    failed.append({'telegram_id': old_telegram_id, 'role': 'old'})
                    _log_notify_failure(f"Failed to notify old delivery person {old_telegram_id}", e)

            if new_telegram_id:
                try:
                    lang = await i18n.get_user_language(int(new_telegram_id))
                    msg = i18n.get('staff.notification.order_assigned', lang,
                                   number=order_info.get('order_number', ''))
                    await self.bot_app.bot.send_message(chat_id=new_telegram_id, text=msg)
                    sent += 1
                except Exception as e:
                    failed.append({'telegram_id': new_telegram_id, 'role': 'new'})
                    _log_notify_failure(f"Failed to notify new delivery person {new_telegram_id}", e)

            return web.json_response({
                'success': not failed,
                'partial_success': bool(failed) and sent > 0,
                'sent_count': sent,
                'failed_count': len(failed),
                'failed_recipients': failed,
                'message': 'Reassignment notifications processed',
            })
        except Exception as e:
            logger.error(f"Error handling order reassigned notification: {e}", exc_info=True)
            return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)

    async def order_cancelled_handler(self, request):
        """
        Notify assigned delivery person that order was cancelled.
        POST /internal/order-cancelled
        """
        try:
            if not await verify_webhook_signature(request):
                return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)
            limited = await self._check_rate_limit(request)
            if limited:
                return limited
            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data, parse_error = await _parse_json_body(request)
            if parse_error:
                return parse_error
            telegram_id = data.get('telegram_id')
            order_info = data.get('order_info', {})
            event_id = data.get('event_id')

            if await self._is_duplicate_event(event_id, f"order_cancelled:{telegram_id}:{order_info.get('order_number', '')}"):
                return web.json_response({'success': True, 'message': 'Already processed'})

            if not telegram_id:
                return web.json_response({'success': True, 'message': 'No delivery person to notify'})

            language = await i18n.get_user_language(int(telegram_id))
            message = i18n.get('staff.notification.order_cancelled', language,
                               number=order_info.get('order_number', ''))
            await self.bot_app.bot.send_message(chat_id=telegram_id, text=message)

            return web.json_response({'success': True, 'message': 'Cancellation notification sent'})
        except Exception as e:
            logger.error(f"Error handling order cancelled notification: {e}", exc_info=True)
            return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)

    async def order_unassigned_handler(self, request):
        """
        Tell a driver that dispatch removed an order from their route.
        POST /internal/order-unassigned

        Deliberately separate from /internal/order-cancelled: the order is not
        cancelled, it is back in the pool for another driver. Same copy would be
        a lie to the driver.
        """
        try:
            if not await verify_webhook_signature(request):
                return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)
            limited = await self._check_rate_limit(request)
            if limited:
                return limited
            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data, parse_error = await _parse_json_body(request)
            if parse_error:
                return parse_error
            telegram_id = data.get('telegram_id')
            order_info = data.get('order_info', {})
            event_id = data.get('event_id')

            if await self._is_duplicate_event(
                event_id, f"order_unassigned:{telegram_id}:{order_info.get('order_number', '')}"
            ):
                return web.json_response({'success': True, 'message': 'Already processed'})

            if not telegram_id:
                return web.json_response({'success': True, 'message': 'No delivery person to notify'})

            language = await i18n.get_user_language(int(telegram_id))
            message = i18n.get('staff.notification.order_unassigned', language,
                               number=order_info.get('order_number', ''))
            await self.bot_app.bot.send_message(chat_id=telegram_id, text=message)

            return web.json_response({'success': True, 'message': 'Unassignment notification sent'})
        except Exception as e:
            logger.error(f"Error handling order unassigned notification: {e}", exc_info=True)
            return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)

    async def route_updated_handler(self, request):
        """Notify driver that their optimized route changed.
        POST /internal/route-updated

        Best-effort. Signature/rate-limit/existence guards live here; the
        parsed payload is delegated to `handle_route_updated_payload` (a
        free function) so the sound-gate / dedup / card-refresh branching is
        unit-testable without an aiohttp request. See that function's
        docstring for the sounded vs. silent split.

        Every failure from THIS POINT ON (payload handling, or an
        unrecognized exception) still returns 200 (fix round 1, M5,
        confirmed deliberate): this is a fire-and-forget backend->bot ping
        with no caller that acts on failure, so a 5xx here would only
        trigger the backend's webhook retry policy -- re-attempting an
        already-lost cause and, worse, risking a second driver-visible send
        on retry for the sounded branch. The malformed-input checks below
        are the one exception: those ARE surfaced as 4xx, because a
        malformed payload is a backend bug worth making loud, not a
        recipient-side condition retries would fix.
        """
        try:
            if not await verify_webhook_signature(request):
                return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)
            limited = await self._check_rate_limit(request)
            if limited:
                return limited
            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data, parse_error = await _parse_json_body(request)
            if parse_error:
                return parse_error

            raw_telegram_id = data.get('telegram_id')
            if not raw_telegram_id:
                return web.json_response({'success': False, 'message': 'Missing telegram_id'}, status=400)
            try:
                int(raw_telegram_id)
            except (TypeError, ValueError):
                # fix round 1, M1: this used to reach `int(data['telegram_id'])`
                # deep inside `handle_route_updated_payload`, where the
                # best-effort try/except below silently turned a malformed
                # payload into a 200 -- hiding a genuine backend bug. Caught
                # here, before that swallow, as an explicit 4xx instead.
                return web.json_response(
                    {'success': False, 'message': 'Malformed telegram_id'}, status=400
                )

            try:
                await handle_route_updated_payload(self, data)
            except Exception as e:  # noqa: BLE001 -- best-effort ping; a
                # failure applying the update must not turn into a 500 the
                # backend would retry (and potentially double-notify on retry).
                # exc_info=True (fix round 2, item 5): this branch now covers
                # the sounded alert too, a much larger blast radius than the
                # silent card-only refresh it used to guard alone -- a bare
                # message with no traceback made a real regression here
                # invisible in the logs while still returning 200.
                logger.warning(
                    f"route_updated handling failed for {data.get('telegram_id')}: {e}",
                    exc_info=True,
                )

            return web.json_response({'success': True, 'message': 'Route-updated handled'})
        except Exception as e:
            logger.error(f"Error in route_updated_handler: {e}", exc_info=True)
            return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)

    async def pool_insertion_suggestion_handler(self, request):
        """Push an Accept/Decline suggestion for a freshly-pooled order that
        fits the driver's current route.
        POST /internal/pool-insertion-suggestion
        """
        try:
            if not await verify_webhook_signature(request):
                return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)
            limited = await self._check_rate_limit(request)
            if limited:
                return limited
            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data, parse_error = await _parse_json_body(request)
            if parse_error:
                return parse_error
            telegram_id = data.get('telegram_id')
            delivery_id = data.get('delivery_id')
            order_no = data.get('order_no', '')
            detour_km = data.get('detour_km', 0)
            detour_min = data.get('detour_minutes', 0)
            # Plan 1's diversion-offer fields (§7). Read defensively — an
            # older backend that hasn't shipped them yet simply omits them,
            # and `offers.build_offer` already treats that as the plain
            # shape (deploy-skew tolerance).
            gain_minutes = data.get('gain_minutes')
            committed_order_number = data.get('committed_order_number')
            event_id = data.get('event_id')

            if not telegram_id or not delivery_id:
                return web.json_response({'success': False, 'message': 'Missing telegram_id or delivery_id'}, status=400)

            if await self._is_duplicate_event(event_id, f"pool_insert:{telegram_id}:{delivery_id}"):
                return web.json_response({'success': True, 'message': 'Already processed'})

            # C-2: check whether the driver is mid-flow (cash collection,
            # COD collect, bottle collect, reconciliation, tryout pickup).
            # The text router would interpret an unrelated typed reply as
            # input for the active flow, and an Accept tap would orphan
            # the flow's pending_*_flow flag. Queue the suggestion instead;
            # the flow's clear/finalize path drains the queue and dispatches
            # the deferred message at the user's next idle moment.
            active_flow = await flow_state.get_active_flow(int(telegram_id))
            payload = {
                'delivery_id': int(delivery_id),
                'order_no': order_no,
                'detour_km': detour_km,
                'detour_minutes': detour_min,
                'gain_minutes': gain_minutes,
                'committed_order_number': committed_order_number,
            }
            if active_flow:
                queued = await flow_state.queue_pool_suggestion(int(telegram_id), payload)
                logger.info(
                    f"pool_insertion deferred for {telegram_id} (active_flow={active_flow}, "
                    f"queued={queued})"
                )
                return web.json_response({
                    'success': True,
                    'deferred': True,
                    'queued': queued,
                    'active_flow': active_flow,
                    'message': 'Driver is mid-flow; suggestion queued',
                })

            language = await i18n.get_user_language(int(telegram_id))

            # SSOT: staff_bot/utils/offers.py is the ONE place that decides
            # the offer's text + keyboard (plain pool-insertion vs. diversion),
            # shared with the deferred-drain path in flow_state.clear_and_drain.
            from staff_bot.utils import offers

            text, keyboard = offers.build_offer(payload, language)
            # Rules that must hold (Task 10 brief): disable_notification=True
            # on every non-urgent send — only an uncapped (sent live, not
            # deferred) diversion offer is time-critical enough to ping.
            try:
                await self.bot_app.bot.send_message(
                    chat_id=int(telegram_id),
                    text=text,
                    reply_markup=keyboard,
                    disable_notification=not offers.is_diversion_offer(payload),
                )
            except Exception as e:
                logger.warning(f"pool_insertion send failed for {telegram_id}: {e}")

            return web.json_response({'success': True, 'message': 'Suggestion sent'})
        except Exception as e:
            logger.error(f"Error in pool_insertion_suggestion_handler: {e}", exc_info=True)
            return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)

    def _format_new_order_message(self, order_info: dict, language: str) -> str:
        """Format new order notification message.

        Fields carrying customer-provided free text (name, address, product
        names) are HTML-escaped because the message is sent with
        parse_mode='HTML'.
        """
        number = escape_html(order_info.get('order_number') or i18n.get('staff.common.not_available', language))
        customer_name = escape_html(order_info.get('customer_name', ''))
        address = escape_html(order_info.get('address') or order_info.get('district', ''))
        time_slot = escape_html(order_info.get('time_slot', ''))
        amount = order_info.get('total_amount', 0)
        payment = order_info.get('payment_method', '')
        payment_label = i18n.get(f'staff.delivery.payment.{payment}', language) if payment else ''
        amount_text = format(amount, ',.0f')
        if payment_label:
            amount_text = f"{amount_text} {i18n.get('staff.currency.uzs', language)} ({payment_label})"
        else:
            amount_text = f"{amount_text} {i18n.get('staff.currency.uzs', language)}"

        lines = [
            f"🆕 {i18n.get('staff.notification.new_order', language)}",
            "",
            f"📦 #{number}",
        ]
        if customer_name:
            lines.append(f"👤 {customer_name}")
        if address:
            lines.append(f"📍 {address}")
        if time_slot:
            lines.append(f"🕐 {time_slot}")
        lines.append(f"💰 {amount_text}")

        items = order_info.get('items') or []
        if items:
            for item in items:
                name = escape_html(item.get('product_name') or i18n.get('staff.common.not_available', language))
                quantity = item.get('quantity', 0)
                lines.append(f"📝 {name} × {quantity}")
        else:
            # Fall back to the bare count if the payload predates item details.
            item_count = order_info.get('item_count', 0)
            lines.append(f"📝 {item_count} {i18n.get('staff.items', language)}")

        return '\n'.join(lines)

    async def start(self):
        """Start the webhook server"""
        try:
            if not self.app:
                await self.setup()
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()
            self.site = web.TCPSite(self.runner, self.host, self.port)
            await self.site.start()
            logger.info(f"Staff webhook server started on http://{self.host}:{self.port}")
        except Exception as e:
            logger.error(f"Failed to start staff webhook server: {e}", exc_info=True)
            raise

    async def stop(self):
        """Stop the webhook server"""
        try:
            if self.site:
                await self.site.stop()
            if self.runner:
                await self.runner.cleanup()
            if self._redis:
                await self._redis.close()
                self._redis = None
                self._redis_connected = False
            logger.info("Staff webhook server stopped")
        except Exception as e:
            logger.error(f"Error stopping staff webhook server: {e}", exc_info=True)


# Global webhook server instance
webhook_server = StaffWebhookServer(
    host='0.0.0.0',
    port=int(os.environ.get('STAFF_WEBHOOK_SERVER_PORT', '8081'))
)
