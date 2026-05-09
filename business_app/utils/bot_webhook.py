"""
Bot Webhook Helper
Utilities for triggering bot webhooks (translation reload, etc.)
"""

import logging
import hmac
import hashlib
import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import aiohttp
from flask import current_app, g

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
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


async def _trigger_bot_webhook_async(
    endpoint: str,
    payload: Dict[str, Any],
    *,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Async helper to trigger bot webhook

    Args:
        endpoint: Webhook endpoint path (e.g., '/internal/reload-translations')
        payload: JSON payload to send
        request_id: stable id for the bot to dedup retries against (BOT-008).
            Defaults to current Flask request id if available, else a new UUID.

    Returns:
        Response data dictionary
    """
    try:
        # Get configuration
        bot_webhook_url = current_app.config.get("BOT_WEBHOOK_URL")
        webhook_secret = current_app.config.get("BOT_WEBHOOK_SECRET")

        if not bot_webhook_url:
            logger.warning("BOT_WEBHOOK_URL not configured, skipping bot webhook")
            return {"success": False, "message": "Bot webhook URL not configured"}

        if not webhook_secret:
            logger.warning("BOT_WEBHOOK_SECRET not configured, skipping bot webhook")
            return {"success": False, "message": "Bot webhook secret not configured"}

        # Prepare request
        import json

        body = json.dumps(payload).encode("utf-8")
        signature = _get_webhook_signature(body, webhook_secret)

        url = f"{bot_webhook_url.rstrip('/')}{endpoint}"
        # BOT-008: stable X-Request-ID lets the bot dedup backend retries.
        # Reuse the per-request id from `g.request_id` (set by setup_request_handlers
        # in business_app/__init__.py) so logs trace cleanly across services.
        if request_id is None:
            request_id = getattr(g, "request_id", None) or str(uuid.uuid4())[:8]
        headers = {
            "Content-Type": "application/json",
            "X-Bot-Webhook-Signature": signature,
            "X-Request-ID": request_id,
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
                        "success": False,
                        "message": f'Bot webhook failed: {response_data.get("message", "Unknown error")}',
                        "status_code": response.status,
                    }

    except asyncio.TimeoutError:
        logger.error(f"Bot webhook timeout: {endpoint}")
        return {"success": False, "message": "Bot webhook timeout"}

    except Exception as e:
        logger.error(f"Error triggering bot webhook: {e}", exc_info=True)
        return {"success": False, "message": f"Bot webhook error: {str(e)}"}


def trigger_bot_webhook(
    endpoint: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Trigger a bot webhook endpoint (sync wrapper for async function)

    Args:
        endpoint: Webhook endpoint path (e.g., '/internal/reload-translations')
        payload: JSON payload to send (default: empty dict)
        request_id: BOT-008 dedup id. Pass an explicit id when calling from a
            Celery task that retries a logical operation — same id across
            retries collapses to one bot notification.

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
    if "timestamp" not in payload:
        payload["timestamp"] = datetime.now(timezone.utc).isoformat()

    # Capture g.request_id BEFORE crossing into the new event loop — Flask's
    # request context isn't available inside the asyncio.run_until_complete
    # call below.
    if request_id is None:
        try:
            request_id = getattr(g, "request_id", None)
        except RuntimeError:
            # No Flask request context (e.g., called from Celery worker)
            request_id = None

    try:
        # Run async function in event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(_trigger_bot_webhook_async(endpoint, payload, request_id=request_id))
        loop.close()
        return result

    except Exception as e:
        logger.error(f"Error in trigger_bot_webhook: {e}", exc_info=True)
        return {"success": False, "message": f"Failed to trigger bot webhook: {str(e)}"}


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
    return trigger_bot_webhook(
        "/internal/reload-translations", {"action": "reload_translations", "source": "admin_api"}
    )


def trigger_bot_health_check() -> Dict[str, Any]:
    """
    Check bot health status

    Returns:
        Response data dictionary with bot health information
    """
    logger.info("Checking bot health")
    return trigger_bot_webhook("/health", {})


# ---------------------------------------------------------------------------
# Staff bot webhook helpers
#
# The staff bot runs as a separate process at STAFF_BOT_WEBHOOK_URL (default
# http://staff_bot:8081). These helpers POST signed payloads using the
# `requests` library, so they are safe to call from Celery workers (no
# event-loop juggling). HMAC signing matches the scheme used by
# `business_app.tasks.staff_tasks._send_staff_webhook`.
# ---------------------------------------------------------------------------


def _send_staff_bot_webhook(endpoint: str, payload: Dict[str, Any], *, timeout: float = 10.0) -> bool:
    """POST a signed JSON payload to the staff bot's internal webhook server.

    Returns True on 2xx, False otherwise (no exception raised — callers may
    safely ignore the result; the webhook is best-effort).
    """
    import os
    import json
    import requests

    url_base = os.environ.get("STAFF_BOT_WEBHOOK_URL", "http://staff_bot:8081")
    # Dedicated staff_bot HMAC secret. No JWT_SECRET_KEY fallback: that secret
    # belongs to the auth-token domain and using it here would collapse two
    # trust boundaries — a leaked auth secret could then forge staff webhooks.
    secret = os.environ.get("WEBHOOK_SECRET", "")
    if not secret:
        logger.warning("Staff bot webhook secret (WEBHOOK_SECRET) missing — skipping %s", endpoint)
        return False

    body = json.dumps(payload).encode("utf-8")
    signature = _get_webhook_signature(body, secret)
    headers = {
        "Content-Type": "application/json",
        "X-Bot-Webhook-Signature": signature,
        "X-Request-ID": str(uuid.uuid4())[:8],
    }
    url = f"{url_base.rstrip('/')}{endpoint}"

    try:
        response = requests.post(url, data=body, headers=headers, timeout=timeout)
        if 200 <= response.status_code < 300:
            return True
        logger.warning("Staff bot webhook %s returned %d: %s", endpoint, response.status_code, response.text[:200])
        return False
    except requests.exceptions.ConnectionError:
        logger.warning("Staff bot not reachable at %s — %s skipped", url, endpoint)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.error("Staff bot webhook %s failed: %s", endpoint, exc)
        return False


def _resolve_driver_telegram_id(driver_id: int) -> Optional[int]:
    """Look up telegram_id for a driver user_id. Returns None if not linked."""
    try:
        from business_app.models.user import User

        user = User.query.filter_by(id=driver_id).first()
        if user is None or not user.telegram_id:
            return None
        return int(user.telegram_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to resolve telegram_id for driver_id=%s: %s", driver_id, exc)
        return None


def notify_route_updated(driver_id: int) -> bool:
    """Tell the staff bot to refresh the driver's open active-deliveries view."""
    telegram_id = _resolve_driver_telegram_id(driver_id)
    if telegram_id is None:
        logger.info("Skipping route-updated push: driver %s has no telegram_id", driver_id)
        return False
    return _send_staff_bot_webhook(
        "/internal/route-updated",
        {
            "driver_id": driver_id,
            "telegram_id": telegram_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


def notify_pool_insertion_suggestion(
    *,
    driver_id: int,
    delivery_id: int,
    order_no: str,
    detour_km: float,
    detour_minutes: float,
) -> bool:
    """Push a pool-order insertion suggestion to a specific driver's chat."""
    telegram_id = _resolve_driver_telegram_id(driver_id)
    if telegram_id is None:
        logger.info("Skipping pool-insertion push: driver %s has no telegram_id", driver_id)
        return False
    return _send_staff_bot_webhook(
        "/internal/pool-insertion-suggestion",
        {
            "driver_id": driver_id,
            "telegram_id": telegram_id,
            "delivery_id": delivery_id,
            "order_no": order_no,
            "detour_km": detour_km,
            "detour_minutes": detour_minutes,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
