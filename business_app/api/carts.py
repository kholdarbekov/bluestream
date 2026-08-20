from flask import Blueprint, request
from flask_jwt_extended import jwt_required, get_jwt_identity

from business_app.utils.service_factory import (
    get_cart_service,
)
from business_app.utils.translations import get_translation
from business_app.utils.error_handlers import handle_api_exception
from business_app.utils.api_responses import (
    success_response,
    validation_error_response,
)

cart_bp = Blueprint("cart", __name__)


@cart_bp.route("/", methods=["GET"])
@jwt_required()
@handle_api_exception
def get_cart():
    """Get current user's cart"""
    user_id = get_jwt_identity()
    cart_service = get_cart_service()
    cart_data = cart_service.get_cart_details(user_id)
    return success_response(data={"cart": cart_data})


@cart_bp.route("/items", methods=["POST"])
@jwt_required()
@handle_api_exception
def add_cart_item():
    """Add item to cart"""
    user_id = get_jwt_identity()
    data = request.get_json()

    cart_service = get_cart_service()
    cart = cart_service.add_item_to_cart(user_id, data.get("product_id"), data.get("quantity", 1))

    return success_response(data={"cart": cart.to_dict()})


@cart_bp.route("/items/<int:product_id>", methods=["PUT"])
@jwt_required()
@handle_api_exception
def update_cart_item(product_id):
    """Update item quantity in cart"""
    user_id = get_jwt_identity()
    data = request.get_json()

    cart_service = get_cart_service()
    cart = cart_service.update_item_quantity(user_id, product_id, data.get("quantity", 1))

    return success_response(data={"cart": cart.to_dict()})


@cart_bp.route("/items/<int:product_id>", methods=["DELETE"])
@jwt_required()
@handle_api_exception
def remove_cart_item(product_id):
    """Remove item from cart"""
    user_id = get_jwt_identity()

    cart_service = get_cart_service()
    cart = cart_service.remove_item_from_cart(user_id, product_id)

    return success_response(data={"cart": cart.to_dict() if cart else None})


@cart_bp.route("/clear", methods=["POST"])
@jwt_required()
@handle_api_exception
def clear_cart():
    """Clear all items from cart"""
    user_id = get_jwt_identity()

    cart_service = get_cart_service()
    cart_service.clear_cart(user_id)

    return success_response(data={"message": get_translation("api.cart.cleared")})


@cart_bp.route("/sync", methods=["POST"])
@jwt_required()
@handle_api_exception
def sync_cart():
    """
    Sync localStorage cart to database
    Used when user logs in with items in localStorage
    """
    user_id = get_jwt_identity()
    data = request.get_json()

    local_cart_items = data.get("cart_items", [])

    if not isinstance(local_cart_items, list):
        return validation_error_response(errors={"cart_items": get_translation("api.cart.error.items_must_be_list")})

    cart_service = get_cart_service()
    cart = cart_service.sync_cart_from_local(user_id, local_cart_items)

    return success_response(
        data={"cart": cart.to_dict() if cart else None, "message": get_translation("api.cart.synchronized")}
    )


@cart_bp.route("/estimate", methods=["POST"])
@jwt_required()
@handle_api_exception
def get_cart_estimate():
    """
    Get cart price estimate with delivery fees and discounts
    """
    user_id = get_jwt_identity()
    data = request.get_json()

    cart_items = data.get("cart_items", [])
    delivery_address_id = data.get("delivery_address_id")
    delivery_date = data.get("delivery_date")
    loyalty_points_used = data.get("loyalty_points_used", 0)
    promo_code = data.get("promo_code")

    cart_service = get_cart_service()
    estimate = cart_service.calculate_cart_estimate(
        user_id=user_id,
        items=cart_items,
        delivery_address_id=delivery_address_id,
        delivery_date=delivery_date,
        loyalty_points_used=loyalty_points_used,
        promo_code=promo_code,
    )

    return success_response(data={"estimate": estimate})


@cart_bp.route("/validate", methods=["POST"])
@jwt_required()
@handle_api_exception
def validate_cart():
    """
    Validate cart before checkout
    Checks inventory, pricing, and minimum order requirements
    """
    user_id = get_jwt_identity()
    data = request.get_json()

    cart_items = data.get("cart_items", [])

    cart_service = get_cart_service()
    validation_result = cart_service.prepare_cart_for_checkout(user_id=user_id, items=cart_items)

    return success_response(
        data={
            "valid": validation_result.get("ready_for_checkout", False),
            "items": [
                {
                    "product_id": item["product_id"],
                    "product_name": item["product"].name,
                    "quantity": item["quantity"],
                    "unit_price": item["unit_price"],
                    "subtotal": item["subtotal"],
                }
                for item in validation_result.get("items", [])
            ],
            "subtotal": validation_result.get("subtotal", 0),
            "warnings": validation_result.get("warnings", []),
        }
    )
