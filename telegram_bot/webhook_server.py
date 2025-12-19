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
from i18n import i18n

logger = logging.getLogger(__name__)


def verify_webhook_signature(request):
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

    # Get webhook secret from config
    webhook_secret = config.security.jwt_secret_key  # Use same secret as JWT
    if not webhook_secret:
        logger.error("Webhook secret not configured")
        return False

    # Calculate expected signature
    body = request._read_bytes
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
        if not verify_webhook_signature(request):
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
            'message': f'Failed to reload translations: {str(e)}'
        }, status=500)


async def health_handler(_request):
    """
    Health check endpoint

    GET /health
    """
    try:
        # Check translation status
        translation_count = sum(len(keys) for keys in i18n.translations.values())

        # Check missing keys
        missing_keys_count = sum(len(keys) for keys in i18n.missing_keys.values())

        return web.json_response({
            'status': 'healthy',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'translations': {
                'total_count': translation_count,
                'languages': list(i18n.translations.keys()),
                'missing_keys_count': missing_keys_count
            }
        })

    except Exception as e:
        logger.error(f"Health check failed: {e}", exc_info=True)
        return web.json_response({
            'status': 'unhealthy',
            'error': str(e)
        }, status=500)


async def stats_handler(request):
    """
    Get bot statistics

    GET /internal/stats
    """
    try:
        # Verify signature (stats are admin-only)
        if not verify_webhook_signature(request):
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
            'message': f'Failed to get stats: {str(e)}'
        }, status=500)


class WebhookServer:
    """Internal webhook server for bot management"""

    def __init__(self, host='0.0.0.0', port=8080):
        self.host = host
        self.port = port
        self.app = None
        self.runner = None
        self.site = None

    async def setup(self):
        """Setup webhook server routes"""
        self.app = web.Application()

        # Add routes
        self.app.router.add_post('/internal/reload-translations', reload_translations_handler)
        self.app.router.add_get('/internal/stats', stats_handler)
        self.app.router.add_get('/health', health_handler)

        logger.info(f"Webhook server configured on {self.host}:{self.port}")

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
