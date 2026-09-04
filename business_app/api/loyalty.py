"""
Loyalty Program API endpoints for the Water Business Platform
"""

from flask import Blueprint, request, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity

from business_app.utils.service_factory import get_loyalty_service
from business_app.utils.helpers import get_current_language
from business_app.serializers.loyalty_serializers import (
    serialize_loyalty_reward,
    serialize_loyalty_transaction,
    serialize_loyalty_program,
)
from business_app.utils.decorators import validate_json, cache_response, require_loyalty_eligible
from business_app.utils.constants import LoyaltyActionType
from business_app.utils.api_responses import (
    success_response,
    error_response,
    paginated_response,
    created_response,
    not_found_response,
    internal_error_response,
)
from business_app.utils.exceptions import ValidationError, NotFoundError
from business_app.utils.translations import get_translation

loyalty_bp = Blueprint("loyalty", __name__)


def _validation_error_key_for_points_history(message: str) -> str:
    lower = message.lower()
    if "transaction type" in lower:
        return "api.loyalty.error.invalid_transaction_type"
    if "start date" in lower:
        return "api.loyalty.error.invalid_start_date_format"
    if "end date" in lower:
        return "api.loyalty.error.invalid_end_date_format"
    return "api.loyalty.error.validation_failed"


def _validation_error_key_for_gift(message: str) -> str:
    lower = message.lower()
    if "positive" in lower:
        return "api.loyalty.error.points_amount_must_be_positive"
    if "insufficient points" in lower:
        return "api.loyalty.error.insufficient_points"
    if "self" in lower:
        return "api.loyalty.error.cannot_gift_to_self"
    return "api.loyalty.error.validation_failed"


@loyalty_bp.route("/tiers", methods=["GET"])
@cache_response(3600)
def get_membership_tiers():
    """Get all membership tier configurations."""
    try:
        tiers = get_loyalty_service().get_tiers()
        return success_response(
            data={
                "tiers": tiers,
                "tier_count": len(tiers),
                # Each tier's `discount_percentage` is a COD-rail benefit. This
                # route is unauthenticated and 3600 s cached, and /my-loyalty
                # renders the rate straight from it — publishing the number
                # without the condition promises a discount checkout refuses on
                # Click/Payme/card. cache_response keys on the language, so the
                # sentence is translated per request, not frozen in English.
                "tier_discount_condition": get_translation("api.loyalty.tier_discount_condition"),
            }
        )
    except Exception as exc:
        current_app.logger.error(f"Get membership tiers error: {exc}")
        return internal_error_response(get_translation("api.loyalty.error.get_membership_tiers_failed"))


@loyalty_bp.route("/points", methods=["GET"])
@jwt_required()
@require_loyalty_eligible
def get_loyalty_points():
    """Get user's loyalty points balance."""
    try:
        current_user_id = get_jwt_identity()
        payload = get_loyalty_service().get_points_summary_for_user(current_user_id)
        return success_response(data=payload)
    except Exception as exc:
        current_app.logger.error(f"Get loyalty points error: {exc}")
        return internal_error_response(get_translation("api.loyalty.error.get_points_failed"))


@loyalty_bp.route("/account", methods=["GET"])
@jwt_required()
@require_loyalty_eligible
def get_loyalty_account():
    """Get complete loyalty account data for frontend dashboard."""
    try:
        current_user_id = get_jwt_identity()
        payload = get_loyalty_service().get_account_dashboard_for_user(current_user_id)
        return success_response(data=payload)
    except Exception as exc:
        current_app.logger.error(f"Get loyalty account error: {exc}")
        return internal_error_response(get_translation("api.loyalty.error.get_account_failed"))


@loyalty_bp.route("/history", methods=["GET"])
@jwt_required()
@require_loyalty_eligible
def get_loyalty_points_history():
    """Get loyalty points transaction history."""
    try:
        current_user_id = get_jwt_identity()
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 20)), 100)

        payload = get_loyalty_service().get_loyalty_history_for_user(
            current_user_id,
            page=page,
            per_page=per_page,
        )
        return paginated_response(
            items=[serialize_loyalty_transaction(item) for item in payload["items"]],
            page=payload["page"],
            per_page=payload["per_page"],
            total=payload["total"],
        )
    except Exception as exc:
        current_app.logger.error(f"Get loyalty history error: {exc}")
        return internal_error_response(get_translation("api.loyalty.error.get_history_failed"))


@loyalty_bp.route("/profile", methods=["GET"])
@jwt_required()
@require_loyalty_eligible
def get_loyalty_profile():
    """Get user's loyalty profile."""
    try:
        current_user_id = get_jwt_identity()
        payload = get_loyalty_service().get_profile_for_user(current_user_id)
        return success_response(
            data={
                "loyalty_profile": payload["loyalty_profile"],
                "active_program": (
                    serialize_loyalty_program(payload["active_program"]) if payload["active_program"] else None
                ),
                "recent_transactions": [serialize_loyalty_transaction(txn) for txn in payload["recent_transactions"]],
            }
        )
    except NotFoundError:
        return not_found_response(get_translation("user_not_found"))
    except Exception as exc:
        current_app.logger.error(f"Get loyalty profile error: {exc}")
        return internal_error_response(get_translation("api.loyalty.error.get_profile_failed"))


@loyalty_bp.route("/points/history", methods=["GET"])
@jwt_required()
@require_loyalty_eligible
def get_points_history():
    """Get user's points transaction history."""
    try:
        current_user_id = get_jwt_identity()
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 20)), 50)
        transaction_type = request.args.get("type")
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")

        payload = get_loyalty_service().get_filtered_points_history_for_user(
            user_id=current_user_id,
            page=page,
            per_page=per_page,
            transaction_type=transaction_type,
            start_date=start_date,
            end_date=end_date,
        )
        return paginated_response(
            items=[serialize_loyalty_transaction(item) for item in payload["items"]],
            page=payload["page"],
            per_page=payload["per_page"],
            total=payload["total"],
        )
    except ValidationError as exc:
        return error_response(get_translation(_validation_error_key_for_points_history(str(exc))))
    except Exception as exc:
        current_app.logger.error(f"Get points history error: {exc}")
        return internal_error_response(get_translation("api.loyalty.error.get_points_history_failed"))


@loyalty_bp.route("/rewards", methods=["GET"])
@jwt_required()
@require_loyalty_eligible
def get_available_rewards():
    """Get available loyalty rewards."""
    try:
        current_user_id = get_jwt_identity()
        category = request.args.get("category")
        min_points = request.args.get("min_points", type=int)
        max_points = request.args.get("max_points", type=int)

        payload = get_loyalty_service().get_rewards_for_user(
            user_id=current_user_id,
            category=category,
            min_points=min_points,
            max_points=max_points,
        )
        user_points = payload["user_points_balance"]

        rewards = []
        for reward in payload["rewards"]:
            rewards.append(
                {
                    **serialize_loyalty_reward(reward, None),
                    "can_redeem": payload["can_redeem_by_id"].get(reward.id, False),
                    "points_needed": max(0, reward.points_cost - user_points),
                }
            )

        return success_response(
            data={
                "rewards": rewards,
                "user_points_balance": user_points,
                "categories": payload["categories"],
            }
        )
    except Exception as exc:
        current_app.logger.error(f"Get available rewards error: {exc}")
        return internal_error_response(get_translation("api.loyalty.error.get_rewards_failed"))


@loyalty_bp.route("/rewards/<int:reward_id>", methods=["GET"])
@jwt_required()
@require_loyalty_eligible
def get_reward_details(reward_id):
    """Get single reward details by ID."""
    try:
        current_user_id = get_jwt_identity()
        language = get_current_language()
        payload = get_loyalty_service().get_reward_details_for_user(current_user_id, reward_id)

        reward = payload["reward"]
        return success_response(
            data={
                "id": reward.id,
                "name": reward.get_translated("name", language),
                "description": reward.get_translated("description", language),
                "reward_type": reward.reward_type,
                "points_cost": reward.points_cost,
                "points_required": reward.points_cost,
                "min_order_value": float(reward.min_order_value) if reward.min_order_value else None,
                "discount_type": reward.discount_type,
                "discount_value": float(reward.discount_value) if reward.discount_value else None,
                "image_url": reward.image_url,
                "terms_conditions": reward.terms_conditions,
                "valid_from": reward.valid_from.isoformat() if reward.valid_from else None,
                "valid_until": reward.valid_until.isoformat() if reward.valid_until else None,
                "can_redeem": payload["can_redeem"],
                "points_needed": payload["points_needed"],
                "user_points_balance": payload["user_points_balance"],
            }
        )
    except NotFoundError:
        return not_found_response(get_translation("api.loyalty.error.reward_not_found"))
    except Exception as exc:
        current_app.logger.error(f"Get reward details error: {exc}")
        return internal_error_response(get_translation("api.loyalty.error.get_reward_details_failed"))


@loyalty_bp.route("/rewards/history", methods=["GET"])
@jwt_required()
@require_loyalty_eligible
def get_redemption_history():
    """Get user's reward redemption history."""
    try:
        current_user_id = get_jwt_identity()
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 20)), 50)
        status = request.args.get("status")

        payload = get_loyalty_service().get_redemption_history_for_user(
            user_id=current_user_id,
            page=page,
            per_page=per_page,
            status=status,
        )
        return paginated_response(
            items=[serialize_loyalty_transaction(item) for item in payload["items"]],
            page=payload["page"],
            per_page=payload["per_page"],
            total=payload["total"],
        )
    except ValidationError:
        return error_response(get_translation("api.loyalty.error.invalid_status_value"))
    except Exception as exc:
        current_app.logger.error(f"Get redemption history error: {exc}")
        return internal_error_response(get_translation("api.loyalty.error.get_redemption_history_failed"))


@loyalty_bp.route("/programs", methods=["GET"])
@cache_response(3600)
def get_loyalty_programs():
    """Get available loyalty programs."""
    try:
        programs = get_loyalty_service().get_active_programs()
        return success_response(
            data={
                "programs": [serialize_loyalty_program(program) for program in programs],
            }
        )
    except Exception as exc:
        current_app.logger.error(f"Get loyalty programs error: {exc}")
        return internal_error_response(get_translation("api.loyalty.error.get_programs_failed"))


@loyalty_bp.route("/earn-points", methods=["POST"])
@jwt_required()
@require_loyalty_eligible
@validate_json(["action"])
def earn_points():
    """Manually award points for specific actions."""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        loyalty_service = get_loyalty_service()

        action = data.get("action")
        reference_id = data.get("reference_id")
        points_amount = data.get("points_amount")

        valid_actions = [
            "referral_signup",
            "social_share",
            "review_submitted",
            "birthday_bonus",
            "survey_completed",
            "app_install",
            "newsletter_signup",
        ]
        if action not in valid_actions:
            return error_response(get_translation("api.loyalty.error.invalid_action_type"))

        action_type_map = {
            "referral_signup": LoyaltyActionType.REFERRAL,
            "social_share": LoyaltyActionType.SOCIAL_SHARE,
            "review_submitted": LoyaltyActionType.REVIEW,
            "birthday_bonus": LoyaltyActionType.BIRTHDAY_BONUS,
            "survey_completed": LoyaltyActionType.WELCOME_BONUS,
            "app_install": LoyaltyActionType.WELCOME_BONUS,
            "newsletter_signup": LoyaltyActionType.WELCOME_BONUS,
        }

        transaction = loyalty_service.award_points(
            user_id=current_user_id,
            points=points_amount or loyalty_service.get_action_points(action),
            description=f"AquaCoins earned for {action.replace('_', ' ')}",
            action_type=action_type_map.get(action, LoyaltyActionType.WELCOME_BONUS),
            reference_id=reference_id,
        )

        return created_response(
            data={
                "transaction": serialize_loyalty_transaction(transaction),
            },
            message=get_translation("api.loyalty.points_awarded_successfully"),
        )
    except ValidationError as exc:
        current_app.logger.warning(f"Earn points validation error: {exc}")
        return error_response(get_translation("api.loyalty.error.validation_failed"))
    except Exception as exc:
        current_app.logger.error(f"Earn points error: {exc}")
        return internal_error_response(get_translation("api.loyalty.error.earn_points_failed"))


@loyalty_bp.route("/referral", methods=["GET"])
@jwt_required()
@require_loyalty_eligible
def get_referral_info():
    """Get user's referral code and statistics."""
    try:
        current_user_id = get_jwt_identity()
        payload = get_loyalty_service().get_referral_info_for_user(
            user_id=current_user_id,
        )
        return success_response(data=payload)
    except NotFoundError:
        return not_found_response(get_translation("user_not_found"))
    except Exception as exc:
        current_app.logger.error(f"Get referral info error: {exc}")
        return internal_error_response(get_translation("api.loyalty.error.get_referral_info_failed"))


@loyalty_bp.route("/statistics", methods=["GET"])
@jwt_required()
@require_loyalty_eligible
def get_loyalty_statistics():
    """Get user's loyalty statistics."""
    try:
        current_user_id = get_jwt_identity()
        period = request.args.get("period", "year")
        payload = get_loyalty_service().get_statistics_for_user(
            user_id=current_user_id,
            period=period,
        )
        return success_response(data=payload)
    except Exception as exc:
        current_app.logger.error(f"Get loyalty statistics error: {exc}")
        return internal_error_response(get_translation("api.loyalty.error.get_statistics_failed"))


@loyalty_bp.route("/tier-benefits", methods=["GET"])
@jwt_required()
@require_loyalty_eligible
def get_tier_benefits():
    """Get benefits for user's current tier and upgrade requirements."""
    try:
        current_user_id = get_jwt_identity()
        payload = get_loyalty_service().get_tier_benefits_for_user(current_user_id)
        return success_response(data=payload)
    except Exception as exc:
        current_app.logger.error(f"Get tier benefits error: {exc}")
        return internal_error_response(get_translation("api.loyalty.error.get_tier_benefits_failed"))


@loyalty_bp.route("/gift-points", methods=["POST"])
@jwt_required()
@require_loyalty_eligible
@validate_json(["recipient_phone", "points_amount"])
def gift_points():
    """Gift points to another user."""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        recipient_phone = data.get("recipient_phone")
        points_amount = data.get("points_amount")
        message = data.get("message", "")

        gift_transaction = get_loyalty_service().gift_points_by_phone(
            sender_id=current_user_id,
            recipient_phone=recipient_phone,
            points_amount=points_amount,
            message=message,
        )

        return created_response(
            data={
                "transaction": serialize_loyalty_transaction(gift_transaction),
            },
            message=get_translation("api.loyalty.points_gifted_successfully"),
        )
    except NotFoundError:
        return not_found_response(get_translation("api.loyalty.error.recipient_not_found"))
    except ValidationError as exc:
        current_app.logger.warning(f"Gift points validation error: {exc}")
        return error_response(get_translation(_validation_error_key_for_gift(str(exc))))
    except Exception as exc:
        current_app.logger.error(f"Gift points error: {exc}")
        return internal_error_response(get_translation("api.loyalty.error.gift_points_failed"))
