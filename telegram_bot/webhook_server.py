"""
Internal Webhook Server for Bot Management
Receives webhooks from backend for cache invalidation and other operations
"""
import hmac
import hashlib
import logging
import os
from datetime import datetime, timezone
from aiohttp import web

from config import config
from database import db_manager
from i18n import i18n

logger = logging.getLogger(__name__)


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

    # Get webhook secret from config (falls back to JWT secret for backward compatibility)
    webhook_secret = config.security.webhook_secret or config.security.jwt_secret_key
    if not webhook_secret:
        logger.error("Webhook secret not configured")
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

    # Check Redis connectivity (via token manager)
    try:
        from token_manager import token_manager
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
        # Deduplication: track recently processed order_ids with timestamps
        self._processed_orders: dict = {}
        self._dedup_ttl = 300  # 5 minutes

    def set_application(self, application):
        """Set Telegram Application instance"""
        self.bot_app = application

    async def setup(self):
        """Setup webhook server routes"""
        self.app = web.Application(client_max_size=1024 * 1024)  # 1MB payload limit

        # Add routes
        self.app.router.add_post('/internal/reload-translations', reload_translations_handler)
        self.app.router.add_post('/internal/payment-success', self.payment_success_handler)
        self.app.router.add_get('/internal/stats', stats_handler)
        self.app.router.add_get('/health', health_handler)

        logger.info(f"Webhook server configured on {self.host}:{self.port}")

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
                
            # Deduplication: prevent duplicate notifications for the same order
            now = datetime.now(timezone.utc)
            # Clean stale entries
            self._processed_orders = {
                k: v for k, v in self._processed_orders.items()
                if (now - v).total_seconds() < self._dedup_ttl
            }
            if order_id in self._processed_orders:
                logger.info(f"Duplicate payment webhook for order {order_id}, skipping")
                return web.json_response({
                    'success': True,
                    'message': 'Already processed (deduplicated)'
                })
            self._processed_orders[order_id] = now

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

            # Try to edit the existing payment message, fall back to sending new
            message_edited = False
            try:
                from token_manager import token_manager
                if token_manager and token_manager.redis:
                    redis_key = f"bot:payment_msg:{order_id}"
                    stored_message_id = await token_manager.redis.get(redis_key)
                    if stored_message_id:
                        from keyboards import PaymentKeyboards
                        keyboard = PaymentKeyboards.payment_success(order_id, language)
                        await self.bot_app.bot.edit_message_text(
                            chat_id=telegram_id,
                            message_id=int(stored_message_id),
                            text=message_text,
                            reply_markup=keyboard
                        )
                        message_edited = True
                        await token_manager.redis.delete(redis_key)
                        logger.info(f"Edited payment message {stored_message_id} for order {order_id}")
            except Exception as edit_err:
                logger.warning(f"Failed to edit payment message, falling back to send_message: {edit_err}")

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
