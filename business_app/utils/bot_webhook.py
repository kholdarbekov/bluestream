"""
Bot Webhook Helper
Utilities for triggering bot webhooks (translation reload, etc.)
"""
import logging
import hmac
import hashlib
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import aiohttp
from flask import current_app

logger = logging.getLogger(__name__)


def _get_webhook_signature(body: bytes, secret: str) -> str:
    """
    Generate webhook signature for authentication

    Args:
        body: Request body bytes
        secret: Webhook secret key

    Returns:
        HMAC-SHA256 signature hex string
    """
    return hmac.new(
        secret.encode('utf-8'),
        body,
        hashlib.sha256
    ).hexdigest()


async def _trigger_bot_webhook_async(endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Async helper to trigger bot webhook

    Args:
        endpoint: Webhook endpoint path (e.g., '/internal/reload-translations')
        payload: JSON payload to send

    Returns:
        Response data dictionary
    """
    try:
        # Get configuration
        bot_webhook_url = current_app.config.get('BOT_WEBHOOK_URL')
        webhook_secret = current_app.config.get('BOT_WEBHOOK_SECRET')

        if not bot_webhook_url:
            logger.warning("BOT_WEBHOOK_URL not configured, skipping bot webhook")
            return {'success': False, 'message': 'Bot webhook URL not configured'}

        if not webhook_secret:
            logger.warning("BOT_WEBHOOK_SECRET not configured, skipping bot webhook")
            return {'success': False, 'message': 'Bot webhook secret not configured'}

        # Prepare request
        import json
        body = json.dumps(payload).encode('utf-8')
        signature = _get_webhook_signature(body, webhook_secret)

        url = f"{bot_webhook_url.rstrip('/')}{endpoint}"
        headers = {
            'Content-Type': 'application/json',
            'X-Bot-Webhook-Signature': signature
        }

        # Send request with timeout
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=body, headers=headers, timeout=timeout) as response:
                response_data = await response.json()

                if response.status == 200:
                    logger.info(f"Bot webhook triggered successfully: {endpoint}")
                    return response_data
                else:
                    logger.error(f"Bot webhook failed with status {response.status}: {response_data}")
                    return {
                        'success': False,
                        'message': f'Bot webhook failed: {response_data.get("message", "Unknown error")}',
                        'status_code': response.status
                    }

    except asyncio.TimeoutError:
        logger.error(f"Bot webhook timeout: {endpoint}")
        return {'success': False, 'message': 'Bot webhook timeout'}

    except Exception as e:
        logger.error(f"Error triggering bot webhook: {e}", exc_info=True)
        return {'success': False, 'message': f'Bot webhook error: {str(e)}'}


def trigger_bot_webhook(endpoint: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Trigger a bot webhook endpoint (sync wrapper for async function)

    Args:
        endpoint: Webhook endpoint path (e.g., '/internal/reload-translations')
        payload: JSON payload to send (default: empty dict)

    Returns:
        Response data dictionary

    Example:
        result = trigger_bot_webhook('/internal/reload-translations', {
            'timestamp': datetime.now(timezone.utc).isoformat()
        })
    """
    if payload is None:
        payload = {}

    # Add timestamp if not present
    if 'timestamp' not in payload:
        payload['timestamp'] = datetime.now(timezone.utc).isoformat()

    try:
        # Run async function in event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(_trigger_bot_webhook_async(endpoint, payload))
        loop.close()
        return result

    except Exception as e:
        logger.error(f"Error in trigger_bot_webhook: {e}", exc_info=True)
        return {'success': False, 'message': f'Failed to trigger bot webhook: {str(e)}'}


def trigger_translation_reload() -> Dict[str, Any]:
    """
    Trigger bot to reload translations from database

    Returns:
        Response data dictionary with success status

    Example:
        result = trigger_translation_reload()
        if result.get('success'):
            print("Bot translations reloaded")
    """
    logger.info("Triggering bot translation reload")
    return trigger_bot_webhook('/internal/reload-translations', {
        'action': 'reload_translations',
        'source': 'admin_api'
    })


def trigger_bot_health_check() -> Dict[str, Any]:
    """
    Check bot health status

    Returns:
        Response data dictionary with bot health information
    """
    logger.info("Checking bot health")
    return trigger_bot_webhook('/health', {})
