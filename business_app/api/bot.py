"""
Bot Management API
Endpoints for managing Telegram bot operations
"""
import logging
import hmac
import hashlib
from flask import Blueprint, request, jsonify
from functools import wraps

from business_app.config import get_config
from business_app.utils.decorators import require_auth, require_admin

logger = logging.getLogger(__name__)

bot_bp = Blueprint('bot', __name__)

config = get_config()


def verify_webhook_signature(f):
    """Decorator to verify webhook signatures for security"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Get signature from header
        signature = request.headers.get('X-Bot-Webhook-Signature')
        if not signature:
            logger.warning("Webhook called without signature")
            return jsonify({
                'success': False,
                'message': 'Missing webhook signature'
            }), 401

        # Get webhook secret from config
        webhook_secret = config.BOT_WEBHOOK_SECRET
        if not webhook_secret:
            logger.error("BOT_WEBHOOK_SECRET not configured")
            return jsonify({
                'success': False,
                'message': 'Webhook not properly configured'
            }), 500

        # Calculate expected signature
        body = request.get_data()
        expected_signature = hmac.new(
            webhook_secret.encode('utf-8'),
            body,
            hashlib.sha256
        ).hexdigest()

        # Compare signatures (constant-time comparison)
        if not hmac.compare_digest(signature, expected_signature):
            logger.warning(f"Invalid webhook signature from {request.remote_addr}")
            return jsonify({
                'success': False,
                'message': 'Invalid signature'
            }), 401

        return f(*args, **kwargs)

    return decorated_function


@bot_bp.route('/reload-translations', methods=['POST'])
@verify_webhook_signature
def reload_translations():
    """
    Trigger bot to reload translations from database

    This endpoint is called by the backend when translations are updated
    to ensure the bot has the latest translations without restart.

    Security: Requires valid webhook signature

    Returns:
        JSON response with reload status
    """
    import asyncio
    import aiohttp

    try:
        # Get bot webhook URL from config
        bot_webhook_url = config.BOT_WEBHOOK_URL
        if not bot_webhook_url:
            logger.error("BOT_WEBHOOK_URL not configured")
            return jsonify({
                'success': False,
                'message': 'Bot webhook URL not configured'
            }), 500

        # Prepare webhook payload
        payload = {
            'action': 'reload_translations',
            'timestamp': request.json.get('timestamp') if request.json else None
        }

        # Send async request to bot
        async def send_reload_request():
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{bot_webhook_url}/internal/reload-translations",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    return await response.json(), response.status

        # Run async request
        try:
            response_data, status_code = asyncio.run(send_reload_request())

            if status_code == 200:
                logger.info("Successfully triggered bot translation reload")
                return jsonify({
                    'success': True,
                    'message': 'Translation reload triggered',
                    'bot_response': response_data
                }), 200
            else:
                logger.error(f"Bot reload failed with status {status_code}: {response_data}")
                return jsonify({
                    'success': False,
                    'message': 'Bot reload request failed',
                    'details': response_data
                }), 500

        except asyncio.TimeoutError:
            logger.error("Bot reload request timed out")
            return jsonify({
                'success': False,
                'message': 'Bot reload request timed out'
            }), 504

        except Exception as e:
            logger.error(f"Error sending reload request to bot: {e}", exc_info=True)
            return jsonify({
                'success': False,
                'message': f'Failed to communicate with bot: {str(e)}'
            }), 500

    except Exception as e:
        logger.error(f"Error in reload_translations webhook: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'message': 'Internal server error',
            'error': str(e)
        }), 500


@bot_bp.route('/health', methods=['GET'])
def bot_health():
    """
    Check bot health status

    Returns:
        JSON response with bot health information
    """
    import aiohttp
    import asyncio

    try:
        bot_webhook_url = config.BOT_WEBHOOK_URL
        if not bot_webhook_url:
            return jsonify({
                'success': False,
                'message': 'Bot webhook URL not configured',
                'status': 'unconfigured'
            }), 200

        async def check_bot_health():
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{bot_webhook_url}/health",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    return await response.json(), response.status

        try:
            response_data, status_code = asyncio.run(check_bot_health())

            return jsonify({
                'success': status_code == 200,
                'message': 'Bot health check complete',
                'bot_status': response_data,
                'status': 'healthy' if status_code == 200 else 'unhealthy'
            }), 200

        except Exception as e:
            return jsonify({
                'success': False,
                'message': f'Bot health check failed: {str(e)}',
                'status': 'unreachable'
            }), 200

    except Exception as e:
        logger.error(f"Error in bot health check: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'message': 'Health check error',
            'error': str(e)
        }), 500


@bot_bp.route('/stats', methods=['GET'])
@require_auth
@require_admin()
def get_bot_stats():
    """
    Get bot statistics (admin only)

    Returns:
        JSON response with bot statistics
    """
    import aiohttp
    import asyncio

    try:
        bot_webhook_url = config.BOT_WEBHOOK_URL
        if not bot_webhook_url:
            return jsonify({
                'success': False,
                'message': 'Bot webhook URL not configured'
            }), 500

        async def get_stats():
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{bot_webhook_url}/internal/stats",
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    return await response.json(), response.status

        try:
            response_data, status_code = asyncio.run(get_stats())

            if status_code == 200:
                return jsonify({
                    'success': True,
                    'data': response_data
                }), 200
            else:
                return jsonify({
                    'success': False,
                    'message': 'Failed to get bot stats',
                    'details': response_data
                }), status_code

        except Exception as e:
            logger.error(f"Error getting bot stats: {e}", exc_info=True)
            return jsonify({
                'success': False,
                'message': f'Failed to get bot stats: {str(e)}'
            }), 500

    except Exception as e:
        logger.error(f"Error in get_bot_stats: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'message': 'Internal server error'
        }), 500
