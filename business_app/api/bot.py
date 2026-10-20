"""
Bot Management API
Endpoints for managing Telegram bot operations
"""

import logging
from flask import Blueprint, request, jsonify
from pydantic import ValidationError as PydanticValidationError

from business_app.config import get_config
from business_app.serializers.bot_serializers import (
    TelegramReachabilityReport,
    WebLinkConfirmRequest,
    WebLinkPreviewRequest,
)
from business_app.services.telegram_reachability_service import (
    REACHABILITY_REPORT_INVALID,
    TelegramReachabilityService,
)
from business_app.utils.api_responses import success_response, validation_error_response
from business_app.utils.decorators import require_auth, require_admin, verify_webhook_signature
from business_app.utils.decorators import handle_exceptions, rate_limit_by_telegram_id
from business_app.utils.exceptions import ValidationError

logger = logging.getLogger(__name__)

bot_bp = Blueprint("bot", __name__)

config = get_config()


@bot_bp.route("/reload-translations", methods=["POST"])
@verify_webhook_signature()
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
            return jsonify({"success": False, "message": "Bot webhook URL not configured"}), 500

        # Prepare webhook payload
        payload = {
            "action": "reload_translations",
            "timestamp": request.json.get("timestamp") if request.json else None,
        }

        # Send async request to bot
        async def send_reload_request():
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{bot_webhook_url}/internal/reload-translations",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    return await response.json(), response.status

        # Run async request
        try:
            response_data, status_code = asyncio.run(send_reload_request())

            if status_code == 200:
                logger.info("Successfully triggered bot translation reload")
                return (
                    jsonify(
                        {"success": True, "message": "Translation reload triggered", "bot_response": response_data}
                    ),
                    200,
                )
            else:
                logger.error(f"Bot reload failed with status {status_code}: {response_data}")
                return (
                    jsonify({"success": False, "message": "Bot reload request failed", "details": response_data}),
                    500,
                )

        except asyncio.TimeoutError:
            logger.error("Bot reload request timed out")
            return jsonify({"success": False, "message": "Bot reload request timed out"}), 504

        except Exception as e:
            logger.error(f"Error sending reload request to bot: {e}", exc_info=True)
            return jsonify({"success": False, "message": f"Failed to communicate with bot: {str(e)}"}), 500

    except Exception as e:
        logger.error(f"Error in reload_translations webhook: {e}", exc_info=True)
        return jsonify({"success": False, "message": "Internal server error", "error": str(e)}), 500


@bot_bp.route("/health", methods=["GET"])
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
            return (
                jsonify({"success": False, "message": "Bot webhook URL not configured", "status": "unconfigured"}),
                200,
            )

        async def check_bot_health():
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{bot_webhook_url}/health", timeout=aiohttp.ClientTimeout(total=5)) as response:
                    return await response.json(), response.status

        try:
            response_data, status_code = asyncio.run(check_bot_health())

            return (
                jsonify(
                    {
                        "success": status_code == 200,
                        "message": "Bot health check complete",
                        "bot_status": response_data,
                        "status": "healthy" if status_code == 200 else "unhealthy",
                    }
                ),
                200,
            )

        except Exception as e:
            return (
                jsonify({"success": False, "message": f"Bot health check failed: {str(e)}", "status": "unreachable"}),
                200,
            )

    except Exception as e:
        logger.error(f"Error in bot health check: {e}", exc_info=True)
        return jsonify({"success": False, "message": "Health check error", "error": str(e)}), 500


@bot_bp.route("/stats", methods=["GET"])
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
            return jsonify({"success": False, "message": "Bot webhook URL not configured"}), 500

        async def get_stats():
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{bot_webhook_url}/internal/stats", timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    return await response.json(), response.status

        try:
            response_data, status_code = asyncio.run(get_stats())

            if status_code == 200:
                return jsonify({"success": True, "data": response_data}), 200
            else:
                return (
                    jsonify({"success": False, "message": "Failed to get bot stats", "details": response_data}),
                    status_code,
                )

        except Exception as e:
            logger.error(f"Error getting bot stats: {e}", exc_info=True)
            return jsonify({"success": False, "message": f"Failed to get bot stats: {str(e)}"}), 500

    except Exception as e:
        logger.error(f"Error in get_bot_stats: {e}", exc_info=True)
        return jsonify({"success": False, "message": "Internal server error"}), 500


@bot_bp.route("/telegram-reachability", methods=["POST"])
@verify_webhook_signature()
def report_telegram_reachability():
    """The customer bot reports that a customer blocked or reached it (spec 2026-10-09 §7.2 a, b).

    Signed like telegram-login: no customer JWT travels, so the HMAC over the exact body is the
    only proof the bot sent it. Body `{"telegram_id", "status": "blocked"|"reachable", "at":
    ISO-8601 with offset, "source": "my_chat_member"|"recovery"}`. Answers `{"changed": bool}`; an
    unknown telegram_id is `changed: false`, not an error, so the bot never retries it.
    """
    try:
        report = TelegramReachabilityReport(**(request.get_json(silent=True) or {}))
        changed = TelegramReachabilityService.record_from_bot(
            report.telegram_id, report.status, report.at, source=report.source
        )
    except (PydanticValidationError, ValidationError) as exc:
        return validation_error_response(str(exc), error_code=REACHABILITY_REPORT_INVALID)
    return success_response(data={"changed": changed})


@bot_bp.route("/web-link/preview", methods=["POST"])
@verify_webhook_signature()
@rate_limit_by_telegram_id(30, 3600)
@handle_exceptions
def preview_web_link():
    """
    Name the account a website link would connect, without using the link (spec 2026-10-19 D4)
    ---
    tags:
      - Bot
    parameters:
      - in: header
        name: X-Bot-Webhook-Signature
        type: string
        required: true
        description: HMAC-SHA256 of the exact body with BOT_WEBHOOK_SECRET
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - token
            - telegram_id
          properties:
            token:
              type: string
              maxLength: 64
              description: The text after `/start web_`
            telegram_id:
              type: integer
              example: 123456789
    responses:
      200:
        description: The confirm card's account and language; the link stays usable
        schema:
          type: object
          properties:
            success:
              type: boolean
              example: true
            data:
              type: object
              properties:
                account_label:
                  type: string
                  example: "K*** K*** · +998***4567"
                language:
                  type: string
                  example: ru
      400:
        description: WEB_LINK_EXPIRED (expired, used or replaced by a newer link), or a malformed body
      401:
        description: Missing or invalid bot signature
      409:
        description: WEB_LINK_NOT_AVAILABLE (the account can no longer be connected) or TELEGRAM_ALREADY_HAS_ACCOUNT
      503:
        description: TELEGRAM_LINK_STORE_UNAVAILABLE (the link store is down)
    """
    from business_app.services.customer_telegram_link_service import CustomerTelegramLinkService

    body = WebLinkPreviewRequest.model_validate(request.get_json(silent=True) or {})
    return success_response(data=CustomerTelegramLinkService.preview(body.token, telegram_id=body.telegram_id))


@bot_bp.route("/web-link/confirm", methods=["POST"])
@verify_webhook_signature()
@rate_limit_by_telegram_id(30, 3600)
@handle_exceptions
def confirm_web_link():
    """
    Connect the Telegram to the website account on the customer's Yes, and sign the bot in (spec 2026-10-19 D4)
    ---
    tags:
      - Bot
    parameters:
      - in: header
        name: X-Bot-Webhook-Signature
        type: string
        required: true
        description: HMAC-SHA256 of the exact body with BOT_WEBHOOK_SECRET
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - token
            - telegram_id
          properties:
            token:
              type: string
              maxLength: 64
            telegram_id:
              type: integer
              example: 123456789
            username:
              type: string
              example: kamola_k
            first_name:
              type: string
              example: Kamola
            last_name:
              type: string
              example: Karimova
    responses:
      200:
        description: Connected; the link is spent
        schema:
          type: object
          properties:
            success:
              type: boolean
              example: true
            data:
              type: object
              properties:
                tokens:
                  type: object
                  description: access_token, refresh_token, token_type, expires_in
                language:
                  type: string
                  example: ru
                greeting_name:
                  type: string
                  example: Kamola
                phone_status:
                  type: string
                  enum: [verified, unverified, missing]
                phone:
                  type: string
                  example: "+998901234567"
      400:
        description: WEB_LINK_EXPIRED (expired, used or replaced by a newer link), or a malformed body
      401:
        description: Missing or invalid bot signature
      409:
        description: WEB_LINK_NOT_AVAILABLE or TELEGRAM_ALREADY_HAS_ACCOUNT; the link is spent either way
      503:
        description: TELEGRAM_LINK_STORE_UNAVAILABLE (the link store is down)
    """
    from business_app.services.customer_telegram_link_service import CustomerTelegramLinkService

    body = WebLinkConfirmRequest.model_validate(request.get_json(silent=True) or {})
    return success_response(
        data=CustomerTelegramLinkService.confirm(
            body.token,
            telegram_id=body.telegram_id,
            username=body.username,
            first_name=body.first_name,
            last_name=body.last_name,
        )
    )
