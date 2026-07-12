"""
Internal Webhook Server for Bot Management
Receives webhooks from backend for cache invalidation and other operations
"""
import hmac
import hashlib
import ipaddress
import logging
import os
from datetime import datetime, timezone
from aiohttp import web

from config import config
from database import db_manager
from i18n import i18n
from shared.redis_failure import report_redis_failure
from shared.redis_keyspace import RedisKeyspace

logger = logging.getLogger(__name__)


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

    async def _is_duplicate_webhook(self, endpoint: str, request_id: str) -> bool:
        """BOT-008: Redis-backed webhook dedup.

        Uses ``SET NX EX`` to claim the request_id atomically. Returns True if
        the key already existed (duplicate), False if we just claimed it.
        TTL = 24h matches the upper bound on backend retry storms (PAY-002).

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
                    key, "1", nx=True, ex=self._dedup_ttl_seconds
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

    async def _release_order_dedup(self, order_id) -> None:
        """Release the order-id delivery-dedup marker claimed by
        ``_is_duplicate_webhook('delivery-completed-order', 'order:{order_id}')``.

        The order-id layer is load-bearing: a backend Celery retry mints a FRESH
        X-Request-ID, so only the order-id SET-NX can collapse it. But that also
        means a claimed order-id key permanently absorbs the retry — if the
        Telegram send fails AFTER the claim, the handler 500s, the backend
        releases its own key and re-POSTs, and this layer answers "already
        processed", silently dropping the summary. Releasing the key on send
        failure lets the retry through. Best-effort in both stores
        ``_is_duplicate_webhook`` writes (Redis marker + in-memory fallback set).
        """
        endpoint = 'delivery-completed-order'
        request_id = f"order:{order_id}"

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
        self.app.router.add_post('/internal/delivery-completed', self.delivery_completed_handler)
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

            if stored_message_id:
                try:
                    from keyboards import PaymentKeyboards
                    keyboard = PaymentKeyboards.payment_success(order_id, language)
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
                    text=message_text
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
            "balance": "5"
        }

        Sends the customer a delivery summary with a "Report an issue" inline
        button. Numbers arrive pre-formatted as strings and are rendered verbatim
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
            if request_id and await self._is_duplicate_webhook('delivery-completed', request_id):
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
            from keyboards import KeyboardBuilder
            keyboard = KeyboardBuilder.build_inline_keyboard([[
                {
                    'text': i18n.get('telegram.delivery_summary.report_button', language),
                    'callback_data': f'report_issue_{order_id}',
                }
            ]])

            try:
                await self.bot_app.bot.send_message(
                    chat_id=telegram_id,
                    text=message_text,
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception:
                # Send failed AFTER the order-id dedup key was claimed. Release
                # it so the backend's Celery retry (fresh X-Request-ID → only the
                # order-id layer could catch it) is NOT absorbed and the summary
                # permanently lost. Re-raise into the 500 branch below so the
                # backend still sees the failure and retries.
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
