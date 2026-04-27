"""
Internal Webhook Server for Staff Bot
Receives webhooks from backend for staff notifications (new orders, assignments, etc.)
"""
import hmac
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Optional
from aiohttp import web
import redis.asyncio as redis

from staff_bot.config import config
from staff_bot.database import db_manager
from staff_bot.i18n import i18n
from shared.redis_failure import report_redis_failure
from shared.redis_keyspace import RedisKeyspace

logger = logging.getLogger(__name__)


async def verify_webhook_signature(request):
    """Verify webhook signature for security"""
    signature = request.headers.get('X-Bot-Webhook-Signature')
    if not signature:
        return False

    webhook_secret = config.security.webhook_secret or config.security.jwt_secret_key
    if not webhook_secret:
        logger.error("Webhook secret not configured")
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

    def set_application(self, application):
        """Set Telegram Application instance"""
        self.bot_app = application

    async def setup(self):
        """Setup webhook server routes"""
        self.app = web.Application(client_max_size=1024 * 1024)

        self.app.router.add_post('/internal/new-order', self.new_order_handler)
        self.app.router.add_post('/internal/order-assigned', self.order_assigned_handler)
        self.app.router.add_post('/internal/order-reassigned', self.order_reassigned_handler)
        self.app.router.add_post('/internal/order-cancelled', self.order_cancelled_handler)
        self.app.router.add_post('/internal/reload-translations', reload_translations_handler)
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
        """
        Notify delivery persons of a new order available for pickup.
        POST /internal/new-order
        """
        try:
            if not await verify_webhook_signature(request):
                return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)

            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data = await request.json()
            order_id = data.get('order_id')
            event_id = data.get('event_id')

            if await self._is_duplicate_event(event_id, f"new_order:{order_id}"):
                return web.json_response({'success': True, 'message': 'Already processed'})

            # Get delivery persons who should be notified
            telegram_ids = data.get('delivery_person_telegram_ids', [])
            order_info = data.get('order_info', {})

            sent_count = 0
            for tid in telegram_ids:
                try:
                    language = await i18n.get_user_language(int(tid))
                    message = self._format_new_order_message(order_info, language)
                    await self.bot_app.bot.send_message(chat_id=tid, text=message, parse_mode='HTML')
                    sent_count += 1
                except Exception as e:
                    logger.error(f"Failed to notify delivery person {tid}: {e}")

            return web.json_response({
                'success': True,
                'message': f'Notified {sent_count}/{len(telegram_ids)} delivery persons'
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
            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data = await request.json()
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
            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data = await request.json()
            old_telegram_id = data.get('old_telegram_id')
            new_telegram_id = data.get('new_telegram_id')
            order_info = data.get('order_info', {})
            event_id = data.get('event_id')

            if await self._is_duplicate_event(
                event_id,
                f"order_reassigned:{old_telegram_id}:{new_telegram_id}:{order_info.get('order_number', '')}"
            ):
                return web.json_response({'success': True, 'message': 'Already processed'})

            if old_telegram_id:
                try:
                    lang = await i18n.get_user_language(int(old_telegram_id))
                    msg = i18n.get('staff.notification.order_reassigned_from', lang,
                                   number=order_info.get('order_number', ''))
                    await self.bot_app.bot.send_message(chat_id=old_telegram_id, text=msg)
                except Exception as e:
                    logger.error(f"Failed to notify old delivery person {old_telegram_id}: {e}")

            if new_telegram_id:
                try:
                    lang = await i18n.get_user_language(int(new_telegram_id))
                    msg = i18n.get('staff.notification.order_assigned', lang,
                                   number=order_info.get('order_number', ''))
                    await self.bot_app.bot.send_message(chat_id=new_telegram_id, text=msg)
                except Exception as e:
                    logger.error(f"Failed to notify new delivery person {new_telegram_id}: {e}")

            return web.json_response({'success': True, 'message': 'Reassignment notifications sent'})
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
            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data = await request.json()
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

    def _format_new_order_message(self, order_info: dict, language: str) -> str:
        """Format new order notification message"""
        number = order_info.get('order_number') or i18n.get('staff.common.not_available', language)
        district = order_info.get('district', '')
        time_slot = order_info.get('time_slot', '')
        amount = order_info.get('total_amount', 0)
        payment = order_info.get('payment_method', '')
        payment_label = i18n.get(f'staff.delivery.payment.{payment}', language) if payment else ''
        item_count = order_info.get('item_count', 0)
        amount_text = format(amount, ',.0f')
        if payment_label:
            amount_text = f"{amount_text} {i18n.get('staff.currency.uzs', language)} ({payment_label})"
        else:
            amount_text = f"{amount_text} {i18n.get('staff.currency.uzs', language)}"

        return (
            f"\U0001f195 {i18n.get('staff.notification.new_order', language)}\n\n"
            f"\U0001f4e6 #{number}\n"
            f"\U0001f4cd {district}\n"
            f"\U0001f550 {time_slot}\n"
            f"\U0001f4b0 {amount_text}\n"
            f"\U0001f4dd {item_count} {i18n.get('staff.items', language)}"
        )

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
