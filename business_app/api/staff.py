"""
Staff API endpoints for the Water Business Platform.
Handles staff authentication, delivery operations, and operator actions.
"""

from flask import Blueprint, request, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity

from business_app.services.staff_service import StaffService
from business_app.utils.service_factory import get_corporate_contract_service
from business_app.utils.address_helpers import get_address_label, get_address_line
from business_app.utils.decorators import require_staff_roles, verify_webhook_signature
from business_app.utils.error_handlers import handle_api_exception
from business_app.utils.api_responses import success_response
from business_app.utils.exceptions import ValidationError

staff_bp = Blueprint("staff", __name__)

# --- Staff Authentication ---


@staff_bp.route("/auth/login", methods=["POST"])
@verify_webhook_signature(secret_config_key="WEBHOOK_SECRET")
@handle_api_exception
def staff_login():
    """Staff login: pre-bound telegram_id or one-time invite-token binding."""
    data = request.get_json()
    if not data:
        raise ValidationError("Request body is required", error_code="STAFF_REQUEST_BODY_REQUIRED")

    telegram_id = data.get("telegram_id")
    invite_token = data.get("invite_token")

    if not telegram_id:
        raise ValidationError("telegram_id is required", error_code="STAFF_TELEGRAM_ID_REQUIRED")

    result = StaffService.authenticate_and_link_staff(
        telegram_id=str(telegram_id),
        invite_token=invite_token,
    )
    return success_response(result, status_code=200)


@staff_bp.route("/auth/refresh", methods=["POST"])
@handle_api_exception
@jwt_required(refresh=True)
def staff_refresh_token():
    """Refresh JWT access token"""
    auth_header = request.headers.get("Authorization", "")
    refresh_token = None
    if auth_header.startswith("Bearer "):
        refresh_token = auth_header[7:].strip()

    if not refresh_token:
        refresh_cookie_name = current_app.config.get("JWT_REFRESH_COOKIE_NAME", "refresh_token_cookie")
        refresh_token = request.cookies.get(refresh_cookie_name)

    if not refresh_token:
        raise ValidationError("Refresh token is required", error_code="STAFF_REFRESH_TOKEN_REQUIRED")

    # Block delivery persons an admin has deactivated before minting a fresh token.
    StaffService.assert_delivery_person_active_by_user_id(get_jwt_identity())

    from business_app.services.token_service import TokenService

    token_service = TokenService()
    refreshed = token_service.refresh_access_token(refresh_token)

    return success_response(
        {
            "access_token": refreshed["access_token"],
            "expires_in": refreshed.get("expires_in", 3600),
        }
    )


# --- Delivery Operations ---


@staff_bp.route("/delivery/pool", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver", "operator")
def get_order_pool():
    """Get unassigned orders available for pickup"""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    order_id = request.args.get("order_id", type=int)
    delivery_id = request.args.get("delivery_id", type=int)
    include_assigned = request.args.get("include_assigned", "false").lower() == "true"

    pool = StaffService.get_delivery_pool(
        {
            "page": page,
            "per_page": per_page,
            "order_id": order_id,
            "delivery_id": delivery_id,
            "include_assigned": include_assigned,
        }
    )

    items = []
    for delivery in pool.get("items", []):
        order = delivery.order
        address = order.delivery_address if order else None
        assignee = delivery.delivery_person if delivery else None
        cod_projection = StaffService.get_cod_collection_projection(order)

        order_items = []
        if order and order.order_items:
            for oi in order.order_items:
                order_items.append(
                    {
                        "product_name": oi.product.name if oi.product else "",
                        "quantity": oi.quantity,
                        "unit_price": float(oi.unit_price) if oi.unit_price else 0,
                        "total_price": float(oi.total_price) if oi.total_price else 0,
                    }
                )

        order_status = (
            order.status.value if order and hasattr(order.status, "value") else (order.status if order else None)
        )
        delivery_status = (
            delivery.status.value
            if delivery and hasattr(delivery.status, "value")
            else (delivery.status if delivery else None)
        )

        items.append(
            {
                "delivery_id": delivery.id,
                "order_id": order.id if order else None,
                "order_number": order.order_number if order else None,
                "status": order_status,
                "delivery_status": delivery_status,
                "customer_name": (
                    f"{order.user.first_name} {order.user.last_name or ''}".strip() if order and order.user else ""
                ),
                "customer_phone": order.user.phone if order and order.user else "",
                "district": address.district if address else "",
                "address": get_address_line(address),
                "total_amount": float(order.total_amount) if order and order.total_amount else 0,
                "payment_method": order.payment_method.value if order and order.payment_method else "cash",
                "payment_status": (
                    order.payment.status.value
                    if order and getattr(order, "payment", None) and hasattr(order.payment.status, "value")
                    else None
                ),
                "amount_collected": (
                    float(order.payment.amount_collected or 0) if order and getattr(order, "payment", None) else 0
                ),
                "outstanding_amount": (
                    float(order.payment.outstanding_amount or 0) if order and getattr(order, "payment", None) else 0
                ),
                "cod_reserved_prepayment_amount": cod_projection["cod_reserved_prepayment_amount"],
                "expected_cash_to_collect": cod_projection["expected_cash_to_collect"],
                "item_count": len(order.order_items) if order and order.order_items else 0,
                "items": order_items,
                "delivery_notes": order.delivery_notes or "",
                "delivery_instructions": address.delivery_instructions or "" if address else "",
                # Structured door details the customer bot collects. Without
                # these the driver only ever sees the street line and has to
                # phone the customer for the flat/floor.
                "apartment_number": address.apartment_number or "" if address else "",
                "floor_number": address.floor_number or "" if address else "",
                "time_slot": order.delivery_time_slot if order else "",
                "created_at": order.created_at.isoformat() if order and order.created_at else None,
                "delivery_person_id": delivery.delivery_person_id,
                "delivery_person_name": assignee.full_name if assignee else "",
            }
        )

    return success_response(
        {
            "items": items,
            "pagination": pool.get(
                "pagination",
                {
                    "page": page,
                    "per_page": per_page,
                    "total": len(items),
                    "pages": 1,
                },
            ),
        }
    )


@staff_bp.route("/delivery/accept/<int:delivery_id>", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def accept_order(delivery_id):
    """Accept/pick an order from the pool (with row locking)"""
    from business_app.services.route_optimization_service import RouteOptimizationService

    current_user_id = get_jwt_identity()
    delivery = StaffService.accept_order(delivery_id, current_user_id)

    # Surface location_status so the bot can decide whether to prompt the
    # driver to share their live location right after accepting. Without a
    # known driver location the optimizer falls back to the depot / city
    # center, which produces a misleading "Next stop" suggestion.
    location_status = RouteOptimizationService().location_status(int(current_user_id))

    return success_response(
        {
            "delivery_id": delivery.id,
            "status": delivery.status.value if hasattr(delivery.status, "value") else delivery.status,
            "message": "Order accepted successfully",
            "location_status": location_status,
        }
    )


@staff_bp.route("/delivery/<int:delivery_id>/status", methods=["PUT"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def update_delivery_status(delivery_id):
    """Update delivery status with validation"""
    current_user_id = get_jwt_identity()
    data = request.get_json()

    if not data or "status" not in data:
        raise ValidationError("status field is required", error_code="STAFF_STATUS_REQUIRED")

    metadata = data.get("metadata", {})
    delivery = StaffService.update_delivery_status(delivery_id, data["status"], current_user_id, metadata)

    return success_response(
        {
            "delivery_id": delivery.id,
            "status": delivery.status.value if hasattr(delivery.status, "value") else delivery.status,
            "message": "Status updated successfully",
        }
    )


@staff_bp.route("/delivery/me/location", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def update_my_location():
    """Update the driver's own current location (no delivery_id required).
    Enqueues a debounced route re-optimization and returns the active-
    deliveries payload (same shape as GET /delivery/active) sorted by the
    last persisted route.
    """
    current_user_id = int(get_jwt_identity())
    data = request.get_json() or {}
    try:
        lat = float(data.get("latitude"))
        lng = float(data.get("longitude"))
    except (TypeError, ValueError):
        raise ValidationError(
            "latitude and longitude must be numeric",
            error_code="STAFF_INVALID_COORDINATES",
        )

    # Optional: Telegram only sends horizontal_accuracy on clients that
    # measure it. Absent stays absent — the service treats None as "unknown",
    # never as "coarse".
    raw_accuracy = data.get("horizontal_accuracy")
    if raw_accuracy is None:
        accuracy_m = None
    else:
        try:
            accuracy_m = float(raw_accuracy)
        except (TypeError, ValueError):
            raise ValidationError(
                "horizontal_accuracy must be numeric",
                error_code="STAFF_INVALID_COORDINATES",
            )

    StaffService.update_driver_location(current_user_id, lat, lng, accuracy_m)

    # Re-optimize OFF the request thread (plan §4.5). The task debounces
    # per-driver inside the service; the response below simply reflects the
    # last persisted sequence and the bot re-renders when the silent
    # route-updated webhook lands.
    try:
        from business_app.tasks.delivery_tasks import optimize_driver_route_task

        optimize_driver_route_task.delay(current_user_id, "location_update")
    except Exception as exc:  # noqa: BLE001 — non-critical
        current_app.logger.warning(
            "location-update optimize enqueue failed for driver=%s: %s",
            current_user_id,
            exc,
        )

    # Reuse the active-deliveries response shape so the bot can edit-in-place.
    return get_active_deliveries()


@staff_bp.route("/delivery/<int:delivery_id>/location", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def update_location(delivery_id):
    """Update delivery person's live location"""
    data = request.get_json()
    if not data:
        raise ValidationError("Request body is required", error_code="STAFF_REQUEST_BODY_REQUIRED")

    lat = data.get("latitude")
    lng = data.get("longitude")

    if lat is None or lng is None:
        raise ValidationError("latitude and longitude are required", error_code="STAFF_COORDINATES_REQUIRED")

    current_user_id = int(get_jwt_identity())
    StaffService.update_delivery_location(delivery_id, lat, lng, acting_driver_id=current_user_id)

    return success_response({"message": "Location updated"})


def _customer_bottle_balance(order) -> float:
    """Empties the customer currently holds at this delivery's PHYSICAL place (0 if none).

    One place, one pool: the address group when the delivery address is grouped,
    else the address itself. A customer ordering the same home from a second
    phone therefore still surfaces the true total of empties the driver should
    collect. Negative/over-credited places read as 0.
    """
    if not order or not order.delivery_address_id:
        return 0.0
    from business_app.services.bottle_tracking_service import BottleTrackingService

    place_balance = BottleTrackingService.get_place_balance(order.delivery_address_id)
    return max(0.0, float(place_balance or 0))


def _place_bottle_balance_signed(order) -> float:
    """The place's SIGNED balance — negative means over-returned.

    `_customer_bottle_balance` clamps to 0 on purpose: the "All N returned"
    anchor must never offer a negative quantity. This is an additional field so
    the driver can be TOLD the place is over-returned, not a change to that one.
    """
    if not order or not order.delivery_address_id:
        return 0.0
    from business_app.services.bottle_tracking_service import BottleTrackingService

    return float(BottleTrackingService.get_place_balance(order.delivery_address_id) or 0)


def _place_cod_context(order) -> dict:
    """Place-group COD context for a delivery card (spec 8).

    Thin adapter over ``CashCollectionService.get_place_cod_context`` — the
    lookup lives in the service layer so this module stays free of model
    imports and direct ORM access (API boundary budget). Zeroed for ungrouped
    addresses so ungrouped customers' payloads are byte-identical to today
    plus constant-false fields.
    """
    from business_app.services.cash_collection_service import CashCollectionService

    address_id = getattr(order, "delivery_address_id", None) if order else None
    return CashCollectionService().get_place_cod_context(address_id)


@staff_bp.route("/delivery/active", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def get_active_deliveries():
    """Get my active deliveries"""
    current_user_id = get_jwt_identity()
    deliveries = StaffService.get_active_deliveries(current_user_id)

    items = []
    for delivery in deliveries:
        order = delivery.order
        address = order.delivery_address if order else None
        cod_projection = StaffService.get_cod_collection_projection(order)

        item_list = []
        if order and order.order_items:
            for oi in order.order_items:
                item_list.append(
                    {
                        "product_name": oi.product.name if oi.product else "",
                        "quantity": oi.quantity,
                        "unit_price": float(oi.unit_price) if oi.unit_price else 0,
                    }
                )

        items.append(
            {
                "delivery_id": delivery.id,
                "order_id": order.id if order else None,
                "customer_id": order.user_id if order else None,
                "order_number": order.order_number if order else None,
                "status": delivery.status.value if hasattr(delivery.status, "value") else delivery.status,
                "customer_name": (
                    f"{order.user.first_name} {order.user.last_name or ''}".strip() if order and order.user else ""
                ),
                "customer_phone": order.user.phone if order and order.user else "",
                "address": get_address_line(address),
                "district": address.district if address else "",
                "total_amount": float(order.total_amount) if order and order.total_amount else 0,
                "payment_method": order.payment_method.value if order and order.payment_method else "cash",
                "payment_status": (
                    order.payment.status.value
                    if order and getattr(order, "payment", None) and hasattr(order.payment.status, "value")
                    else None
                ),
                "amount_collected": (
                    float(order.payment.amount_collected or 0) if order and getattr(order, "payment", None) else 0
                ),
                "outstanding_amount": (
                    float(order.payment.outstanding_amount or 0) if order and getattr(order, "payment", None) else 0
                ),
                "cod_reserved_prepayment_amount": cod_projection["cod_reserved_prepayment_amount"],
                "expected_cash_to_collect": cod_projection["expected_cash_to_collect"],
                "items": item_list,
                "delivery_notes": order.delivery_notes or "",
                "delivery_instructions": address.delivery_instructions or "" if address else "",
                # Structured door details the customer bot collects. Without
                # these the driver only ever sees the street line and has to
                # phone the customer for the flat/floor.
                "apartment_number": address.apartment_number or "" if address else "",
                "floor_number": address.floor_number or "" if address else "",
                # Destination coordinates (order address)
                "destination_latitude": address.latitude if address else None,
                "destination_longitude": address.longitude if address else None,
                # Driver's last known delivery coordinates (origin candidate)
                "current_location_lat": delivery.current_location_lat,
                "current_location_lng": delivery.current_location_lng,
                # Returnable bottle info
                "expected_returnable_bottles": (
                    sum(
                        float(oi.product.returnable_bottles_per_unit or 0) * (oi.quantity or 0)
                        for oi in (order.order_items or [])
                        if oi.product and oi.product.tracks_returnable_bottles
                    )
                    if order
                    else 0
                ),
                # Empties the customer currently holds at this address (return anchor).
                "customer_bottle_balance": _customer_bottle_balance(order),
                # The place's SIGNED balance — additional to the clamped anchor above,
                # so the driver can be told when the place is over-returned.
                "place_bottle_balance_signed": _place_bottle_balance_signed(order),
                # Place-group COD context (spec 8) — zeros when ungrouped.
                **_place_cod_context(order),
            }
        )

    # Apply route optimization ordering + next-stop annotations.
    from business_app.services.route_optimization_service import RouteOptimizationService

    route_svc = RouteOptimizationService()
    items = route_svc.annotate_active_items(current_user_id, items)

    return success_response(
        {
            "items": items,
            "total": len(items),
            "location_status": route_svc.location_status(int(current_user_id)),
            "route_summary": route_svc.build_route_summary(int(current_user_id), len(items)),
        }
    )


@staff_bp.route("/delivery/failed", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("operator")
def get_failed_deliveries():
    """Operator: list recent FAILED deliveries available for re-dispatch."""
    deliveries = StaffService.get_failed_deliveries()

    items = []
    for delivery in deliveries:
        order = delivery.order
        address = order.delivery_address if order else None
        items.append(
            {
                "delivery_id": delivery.id,
                "order_id": order.id if order else None,
                "order_number": order.order_number if order else None,
                "status": delivery.status.value if hasattr(delivery.status, "value") else delivery.status,
                "customer_name": (
                    f"{order.user.first_name} {order.user.last_name or ''}".strip() if order and order.user else ""
                ),
                "customer_phone": order.user.phone if order and order.user else "",
                "address": get_address_line(address),
                "total_amount": float(order.total_amount) if order and order.total_amount else 0,
                "failed_delivery_reason": delivery.failed_delivery_reason,
                "delivery_attempts": delivery.delivery_attempts or 0,
            }
        )

    return success_response({"items": items, "total": len(items)})


@staff_bp.route("/delivery/redispatch/<int:delivery_id>", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("operator")
def redispatch_failed_delivery(delivery_id):
    """Operator: re-dispatch a FAILED delivery back to the unassigned pool."""
    actor_id = int(get_jwt_identity())
    payload = request.get_json(silent=True) or {}
    reason = (payload.get("reason") or "").strip() or None
    delivery = StaffService.redispatch_failed_delivery(delivery_id, actor_id, reason=reason)
    return success_response(
        {
            "delivery_id": delivery.id,
            "status": delivery.status.value if hasattr(delivery.status, "value") else delivery.status,
            "message": "Delivery re-dispatched to pool",
        }
    )


@staff_bp.route("/delivery/optimize-route", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def manual_optimize_route():
    """Manually re-run route optimization (driver tapped 'Optimize routes').

    Runs synchronously (small N, fits in <2s) and returns the freshly sorted
    active-deliveries payload so the bot can edit its message in place.

    Refuses with 412 + LOCATION_REQUIRED when the driver's stored position is
    missing OR stale — without a start point that reflects where the driver
    actually is, any sequence we produce is a guess. Staleness uses the single
    location-freshness rule (RouteOptimizationService.location_status); do not
    introduce a second threshold here. The bot is expected to surface the
    share-location prompt rather than silently proceeding.
    """
    from flask import jsonify
    from business_app.services.route_optimization_service import RouteOptimizationService

    current_user_id = int(get_jwt_identity())
    service = RouteOptimizationService()

    if service.location_status(current_user_id) in ("missing", "stale"):
        return (
            jsonify(
                {
                    "success": False,
                    "error": "LOCATION_REQUIRED",
                    "error_code": "LOCATION_REQUIRED",
                    "message": "Driver location is required for route optimization",
                }
            ),
            412,
        )

    route = service.optimize_for_driver(current_user_id, trigger="manual")

    # Reuse /delivery/active's response shape for a consistent UX, plus one
    # flag: when dispatch has locked the route the optimiser deliberately did
    # nothing, and the bot must say so rather than render an unchanged list
    # that looks like a failed tap.
    response, status_code = get_active_deliveries()
    payload = response.get_json()
    payload.setdefault("data", {})["route_locked"] = bool(route is not None and route.manual_override)
    return jsonify(payload), status_code


@staff_bp.route("/delivery/history", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def get_delivery_history():
    """Get my delivery history"""
    current_user_id = get_jwt_identity()
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)

    result = StaffService.get_delivery_history(current_user_id, page, per_page)

    items = []
    for delivery in result["items"]:
        order = delivery.order
        address = order.delivery_address if order else None
        cod_projection = StaffService.get_cod_collection_projection(order)
        items.append(
            {
                "delivery_id": delivery.id,
                "order_number": order.order_number if order else None,
                "status": delivery.status.value if hasattr(delivery.status, "value") else delivery.status,
                "customer_name": (
                    f"{order.user.first_name} {order.user.last_name or ''}".strip() if order and order.user else ""
                ),
                "total_amount": float(order.total_amount) if order and order.total_amount else 0,
                "district": address.district if address else "",
                "delivered_at": delivery.delivered_at.isoformat() if delivery.delivered_at else None,
                "updated_at": delivery.updated_at.isoformat() if delivery.updated_at else None,
                "cash_collected": float(delivery.cash_collected) if delivery.cash_collected else None,
                "payment_status": (
                    order.payment.status.value
                    if order and getattr(order, "payment", None) and hasattr(order.payment.status, "value")
                    else None
                ),
                "amount_collected": (
                    float(order.payment.amount_collected or 0) if order and getattr(order, "payment", None) else 0
                ),
                "outstanding_amount": (
                    float(order.payment.outstanding_amount or 0) if order and getattr(order, "payment", None) else 0
                ),
                "cod_reserved_prepayment_amount": cod_projection["cod_reserved_prepayment_amount"],
                "expected_cash_to_collect": cod_projection["expected_cash_to_collect"],
            }
        )

    return success_response(
        {
            "items": items,
            "pagination": result["pagination"],
        }
    )


@staff_bp.route("/delivery/stats", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def get_delivery_stats():
    """Get my delivery performance stats"""
    current_user_id = get_jwt_identity()
    period = request.args.get("period", "month")

    stats = StaffService.get_delivery_stats(current_user_id, period)
    return success_response(stats)


@staff_bp.route("/cash-collections", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def record_cash_collection():
    """Record a standalone COD cash collection."""
    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    customer_id = data.get("customer_id")
    amount = data.get("amount")
    if customer_id is None:
        raise ValidationError("customer_id is required", error_code="STAFF_CUSTOMER_ID_REQUIRED")
    if amount is None:
        raise ValidationError("amount is required", error_code="STAFF_COLLECTION_AMOUNT_REQUIRED")

    from business_app.services.cash_collection_service import CashCollectionService
    from business_app.services.driver_reconciliation_service import DriverReconciliationService

    source = data.get("source")
    if not source:
        source = "next_delivery" if data.get("delivery_id") else "standalone_meeting"

    event = CashCollectionService().post_collection(
        customer_id=customer_id,
        amount=amount,
        source=source,
        collector_user_id=current_user_id,
        recorded_by_user_id=current_user_id,
        order_id=data.get("order_id"),
        delivery_id=data.get("delivery_id"),
        # Seeds PLACE scope for order-less standalone collections (spec 8):
        # without it a driver collecting at a grouped address can never settle
        # a coworker's debt. Order/delivery context still overrides it.
        #
        # GATED, exactly as api/admin.py:12199 is. The gate is the rollback
        # switch for the whole place feature (plan C0): with it off, PLACE scope
        # must be unreachable, and forwarding a client-supplied address made it
        # reachable from a direct API call or any future client — a scope input
        # no gate and no published ceiling authorises. The shipped staff bot
        # already sends None here when the gate is off (`_scoped_ceiling`), so
        # this closes the non-bot paths rather than changing bot behaviour.
        delivery_address_id=(
            data.get("delivery_address_id") if current_app.config.get("PLACE_COD_COLLECTION_ENABLED") else None
        ),
        notes=data.get("notes"),
        proof_data=data.get("proof_data") or {},
        occurred_at=data.get("occurred_at"),
        manual_allocations=data.get("manual_allocations"),
        allocation_mode=data.get("allocation_mode", "auto"),
        idempotency_key=data.get("idempotency_key"),
    )

    session_payload = None
    if event.driver_cash_session_id:
        session_payload = DriverReconciliationService().get_session_detail(event.driver_cash_session_id)

    return success_response(
        {
            "cash_collection_event": event.to_dict(),
            "driver_cash_session": session_payload,
        },
        status_code=201,
    )


@staff_bp.route("/reconciliation/session", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def get_reconciliation_session():
    """Get the driver's active cash custody reconciliation session."""
    current_user_id = get_jwt_identity()

    from business_app.services.driver_reconciliation_service import DriverReconciliationService

    session = DriverReconciliationService().get_open_session_for_driver(current_user_id)
    payload = DriverReconciliationService().get_session_detail(session.id)
    return success_response(payload)


@staff_bp.route("/reconciliation/session/submit", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def submit_reconciliation_session():
    """Submit the active driver reconciliation and open the next empty session."""
    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    from business_app.services.driver_reconciliation_service import DriverReconciliationService

    reconciliation_service = DriverReconciliationService()
    session = reconciliation_service.submit_session(
        driver_user_id=current_user_id,
        declared_cash=data.get("declared_cash"),
        notes=data.get("notes"),
        submitted_by_user_id=current_user_id,
    )
    payload = reconciliation_service.get_session_detail(session.id)
    next_session = getattr(session, "_next_active_session", None)
    payload["next_active_session"] = (
        reconciliation_service.get_session_detail(next_session.id) if next_session else None
    )
    return success_response(payload)


@staff_bp.route("/customers/<int:customer_id>/cod-statement", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver", "operator")
def get_staff_customer_cod_statement(customer_id):
    """Get COD receivable statement for a customer in staff workflows.

    Served through StaffService, not the engine directly, because the driver's
    collect ceiling must be the SAME figure their debtor row advertises (owner
    ruling A6/R-B) and that union is composed outside the frozen engine. Gate
    off, this is a verbatim pass-through of ``get_customer_cod_statement``.
    """
    statement = StaffService().get_customer_cod_statement_for_staff(customer_id)
    return success_response(statement)


@staff_bp.route("/place-groups/<int:group_id>/cod-statement", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver", "operator")
def get_staff_place_cod_statement(group_id):
    """Unified COD statement for a place group (any member's open debts)."""
    from business_app.services.cash_collection_service import CashCollectionService

    statement = CashCollectionService().get_place_cod_statement(group_id)
    return success_response(statement)


@staff_bp.route("/customers/search", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver", "operator")
def search_customers_for_cod_collection():
    """Search customers for COD collection workflows."""
    query = request.args.get("q", "")
    search_type = request.args.get("type", "phone")
    only_with_open_cod = request.args.get("only_with_open_cod", "true").lower() != "false"

    items = StaffService.search_customers_for_cod_collection(
        query,
        search_type,
        only_with_open_cod=only_with_open_cod,
    )
    return success_response({"items": items, "total": len(items)})


@staff_bp.route("/customers/with-open-cod", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver", "operator")
def list_customers_with_open_cod():
    """List customers with outstanding COD debt for staff collection flows."""
    page = request.args.get("page", 1, type=int) or 1
    per_page = request.args.get("per_page", 10, type=int) or 10

    # Plan E R3: person rows carry their grouped place's whole debt. The
    # composition lives in StaffService because the allocation engine is frozen
    # (plan C1); with the gate off it delegates verbatim to the engine.
    from business_app.services.staff_service import StaffService

    result = StaffService().paginate_cod_debtors_for_staff(page=page, per_page=per_page)
    return success_response(result)


# --- Operator Operations ---


@staff_bp.route("/operator/users", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("operator")
def create_client_user():
    """Create a new client user (operator)"""
    current_user_id = get_jwt_identity()
    data = request.get_json()

    if not data:
        raise ValidationError("Request body is required", error_code="STAFF_REQUEST_BODY_REQUIRED")

    user = StaffService.create_client_user(current_user_id, data)

    return success_response(
        {
            "id": user.id,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "phone": user.phone,
        },
        status_code=201,
    )


@staff_bp.route("/operator/users/search", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("operator")
def search_clients():
    """Search for client users"""
    query = request.args.get("q", "")
    search_type = request.args.get("type", "phone")

    users = StaffService.search_users(query, search_type)

    items = []
    for user in users:
        items.append(
            {
                "id": user.id,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "phone": user.phone,
                "address_count": len(user.addresses) if hasattr(user, "addresses") and user.addresses else 0,
                "order_count": len(user.orders) if hasattr(user, "orders") and user.orders else 0,
            }
        )

    return success_response({"items": items, "total": len(items)})


@staff_bp.route("/operator/orders", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("operator")
def create_order_for_client():
    """Create order on behalf of client (phone order)"""
    current_user_id = get_jwt_identity()
    data = request.get_json()

    if not data:
        raise ValidationError("Request body is required", error_code="STAFF_REQUEST_BODY_REQUIRED")

    client_id = data.get("client_id")
    if not client_id:
        raise ValidationError("client_id is required", error_code="STAFF_CLIENT_ID_REQUIRED")

    order = StaffService.create_phone_order(current_user_id, client_id, data)

    return success_response(
        {
            "id": order.id,
            "order_number": order.order_number,
            "status": order.status.value if hasattr(order.status, "value") else order.status,
            "total_amount": float(order.total_amount) if order.total_amount else 0,
        },
        status_code=201,
    )


@staff_bp.route("/operator/orders/recent", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("operator")
def get_recent_operator_orders():
    """Get recent orders created by this operator"""
    current_user_id = get_jwt_identity()
    orders = StaffService.get_recent_operator_orders(current_user_id, limit=20)

    items = []
    for order in orders:
        items.append(
            {
                "id": order.id,
                "order_number": order.order_number,
                "status": order.status.value if hasattr(order.status, "value") else order.status,
                "total_amount": float(order.total_amount) if order.total_amount else 0,
                "customer_name": f"{order.user.first_name} {order.user.last_name or ''}".strip() if order.user else "",
                "created_at": order.created_at.isoformat() if order.created_at else None,
            }
        )

    return success_response({"items": items, "total": len(items)})


@staff_bp.route("/operator/users/<int:user_id>/addresses", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("operator")
def add_client_address(user_id):
    """Add address for a client"""
    data = request.get_json()
    if not data:
        raise ValidationError("Request body is required", error_code="STAFF_REQUEST_BODY_REQUIRED")
    address = StaffService.add_client_address(user_id, data)

    return success_response(
        {
            "id": address.id,
            "label": get_address_label(address),
            "full_address": get_address_line(address),
        },
        status_code=201,
    )


@staff_bp.route("/operator/users/<int:user_id>/addresses", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("operator")
def get_client_addresses(user_id):
    """Get addresses for a client"""
    addresses = StaffService.get_client_addresses(user_id)

    items = []
    for addr in addresses:
        display_address = get_address_line(addr)
        items.append(
            {
                "id": addr.id,
                "label": get_address_label(addr),
                "full_address": display_address,
                "city": addr.city,
                "district": addr.district,
                "latitude": addr.latitude,
                "longitude": addr.longitude,
            }
        )

    return success_response({"items": items, "total": len(items)})


@staff_bp.route("/operator/users/<int:user_id>/order-estimate", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("operator")
def get_client_order_estimate(user_id):
    """Client-scoped price quote for a phone-order basket. READ-ONLY.

    POST rather than GET because the basket is a structured body, not a query
    string — this endpoint creates NOTHING (no order, no item, no reservation,
    no activity log); it only replays `StaffService.price_phone_order`, the same
    call `create_phone_order` makes.

    It exists because `GET /api/v1/products/` prices for the CALLER
    (`business_app/api/products.py:100-111`), and on the operator's screen the
    caller is the OPERATOR. A corporate-contract client was quoted the generic
    price and charged the contract one (measured 45 000 vs 27 000). Never render
    operator-scoped catalogue money on a screen that quotes a client.
    """
    data = request.get_json(silent=True)
    if not data:
        raise ValidationError("Request body is required", error_code="STAFF_REQUEST_BODY_REQUIRED")

    return success_response(StaffService.estimate_phone_order(user_id, data))


@staff_bp.route("/operator/users/<int:user_id>/payment-methods", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("operator")
def get_client_payment_methods(user_id):
    """Get debt-aware payment methods for an operator-created client order."""
    # Optional: when the operator has already picked the destination address,
    # the COD cap's PLACE arm is evaluated too (spec 5.5).
    delivery_address_id = request.args.get("delivery_address_id", type=int)
    return success_response(StaffService.get_client_payment_methods(user_id, delivery_address_id=delivery_address_id))


@staff_bp.route("/operator/users/<int:user_id>/corporate-balance", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("operator")
def get_client_corporate_balance(user_id):
    """Get active corporate contract balances for a client user."""
    service = get_corporate_contract_service()
    contract_balances = service.get_active_contract_balances_for_user(user_id)

    if not contract_balances:
        return success_response(
            {
                "user_id": user_id,
                "has_active_contracts": False,
                "contracts": [],
            }
        )

    return success_response(
        {
            "user_id": user_id,
            "has_active_contracts": True,
            "contracts": contract_balances,
        }
    )


# --- Shared Staff Operations ---


@staff_bp.route("/orders/<int:order_id>/preparing", methods=["PUT"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver", "operator")
def mark_order_preparing(order_id):
    """Mark order as preparing"""
    current_user_id = get_jwt_identity()
    order = StaffService.mark_order_preparing(order_id, current_user_id)

    return success_response(
        {
            "id": order.id,
            "order_number": order.order_number,
            "status": order.status.value if hasattr(order.status, "value") else order.status,
            "message": "Order marked as preparing",
        }
    )


# --- Bottle Tracking ---


@staff_bp.route("/bottles/customer/<int:user_id>/summary", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver", "operator")
def get_customer_bottle_summary(user_id):
    """Get customer's bottle summary (balances across addresses)."""
    from business_app.services.bottle_tracking_service import BottleTrackingService

    service = BottleTrackingService()
    summary = service.get_customer_summary(user_id)
    return success_response(summary)


@staff_bp.route("/bottles/customer/<int:user_id>/addresses", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver", "operator")
def get_customer_bottle_addresses(user_id):
    """Get the customer's DISTINCT places, each with the place's bottle balance.

    Thin adapter over ``BottleTrackingService.get_customer_place_rows`` — the
    place-to-owned-address mapping needs model access, which this module's
    boundary budget forbids (see tests/unit/test_structure_boundary_regressions).
    """
    from business_app.services.bottle_tracking_service import BottleTrackingService

    return success_response(BottleTrackingService.get_customer_place_rows(user_id))


@staff_bp.route("/bottles/collection", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver", "operator")
def record_bottle_collection():
    """Record standalone bottle collection by driver."""
    from business_app.services.bottle_tracking_service import BottleTrackingService

    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    customer_id = data.get("customer_id")
    address_id = data.get("address_id")
    quantity = data.get("quantity")
    notes = data.get("notes")
    # The driver's PER-INTENT retry token. Forwarded RAW: it is validated and
    # namespaced in the service (`compose_client_idempotency_key`), which is
    # where this module's boundary budget requires all validation to live.
    idempotency_key = data.get("idempotency_key")

    # NOTE: this is still TRUTHINESS, not presence — deliberately, and unlike
    # the sibling fine route below. A `quantity` of 0 therefore reports
    # "are required" rather than the service's "must be positive", which is the
    # same wrong-reason shape the fine route was fixed for. It is left alone
    # because `test_collection_quantity_zero_negative_and_string_are_handled_at
    # _the_boundary` (test_staff_bot_place_full_e2e.py) asserts the "required"
    # wording for a zero as intended behaviour; changing it needs that pin
    # re-pointed, which is an owner decision.
    if not customer_id or not address_id or not quantity:
        raise ValidationError("customer_id, address_id, and quantity are required")

    service = BottleTrackingService()
    entry = service.record_standalone_collection(
        user_id=customer_id,
        address_id=address_id,
        quantity=quantity,
        actor_user_id=current_user_id,
        notes=notes,
        idempotency_key=idempotency_key,
    )

    # The PLACE's remaining empties — the same number the driver was offered to
    # collect against. Reading the (user, address) pair here is what printed a
    # negative remainder after a driver collected a whole shared place's empties.
    balance = service.get_place_balance_row(address_id)
    return success_response(
        {
            "ledger_entry_id": entry.id,
            "quantity_collected": float(abs(entry.quantity)),
            "remaining_balance": float(balance.balance) if balance else 0,
        }
    )


@staff_bp.route("/bottles/fine", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver", "operator")
def create_bottle_fine_staff():
    """Driver/operator creates a manual fine for missing bottles."""
    from business_app.services.bottle_tracking_service import BottleTrackingService

    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    customer_id = data.get("customer_id")
    address_id = data.get("address_id")
    quantity = data.get("quantity")
    fine_amount = data.get("fine_amount")
    notes = data.get("notes")
    # See the sibling collection route: forwarded RAW, validated in the service.
    idempotency_key = data.get("idempotency_key")

    # PRESENCE, not truthiness. `not all([...])` made a quantity of 0 or a
    # fine_amount of 0 read as MISSING, so the driver was told the four fields
    # "are required" instead of the service's "must be positive" — and a client
    # retrying with a nonzero placeholder issues a REAL money-denominated fine.
    # The admin route's `BottleFineCreateRequest` never had this bug, so the two
    # entry points rejected the same input for different reasons.
    if any(value is None for value in (customer_id, address_id, quantity, fine_amount)):
        raise ValidationError("customer_id, address_id, quantity, and fine_amount are required")

    service = BottleTrackingService()
    fine = service.issue_fine(
        user_id=customer_id,
        address_id=address_id,
        quantity=quantity,
        fine_amount=fine_amount,
        actor_user_id=current_user_id,
        notes=notes,
        idempotency_key=idempotency_key,
    )
    return success_response(fine.to_dict())


@staff_bp.route("/bottles/load", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def record_bottles_loaded():
    """[DEPRECATED] Shim — delegates to session open endpoint for backward compatibility."""
    from business_app.serializers.bottle_serializers import (
        DriverBottleSessionOpenRequest,
        serialize_bottle_session,
    )
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from pydantic import ValidationError as PydanticValidationError
    from business_app.utils.api_responses import validation_error_response

    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    try:
        payload = DriverBottleSessionOpenRequest(**data)
    except PydanticValidationError as exc:
        return validation_error_response(exc.errors())

    service = BottleTrackingService()
    session = service.open_bottle_session(
        current_user_id,
        payload.bottles_loaded,
        actor_user_id=current_user_id,
        notes=payload.notes,
    )
    return success_response(serialize_bottle_session(session))


@staff_bp.route("/bottles/return-to-warehouse", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def record_bottles_returned_to_warehouse():
    """[DEPRECATED] Shim — delegates to session close endpoint for backward compatibility."""
    from business_app.serializers.bottle_serializers import (
        DriverBottleSessionCloseRequest,
        serialize_bottle_session,
    )
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from pydantic import ValidationError as PydanticValidationError
    from business_app.utils.api_responses import validation_error_response

    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    try:
        payload = DriverBottleSessionCloseRequest(**data)
    except PydanticValidationError as exc:
        return validation_error_response(exc.errors())

    service = BottleTrackingService()
    session = service.close_bottle_session(
        current_user_id,
        payload.bottles_returned_to_warehouse,
        actor_user_id=current_user_id,
        notes=payload.notes,
    )
    return success_response(serialize_bottle_session(session))


@staff_bp.route("/bottles/my-accountability", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def get_my_bottle_accountability():
    """Get driver's current open session or most recent closed session."""
    from business_app.serializers.bottle_serializers import serialize_bottle_session
    from business_app.services.bottle_tracking_service import BottleTrackingService

    current_user_id = get_jwt_identity()
    service = BottleTrackingService()

    # Return open session first; fall back to most recent closed one
    open_session = service.get_open_session(current_user_id)
    if open_session:
        return success_response(serialize_bottle_session(open_session))

    result = service.get_driver_sessions(current_user_id, page=1, per_page=1)
    items = result.get("items", [])
    return success_response(serialize_bottle_session(items[0]) if items else {})


# --- Session endpoints ---


@staff_bp.route("/bottles/session/open", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def open_bottle_session():
    """Driver opens a new trip session by loading bottles from the warehouse."""
    from business_app.serializers.bottle_serializers import (
        DriverBottleSessionOpenRequest,
        serialize_bottle_session,
    )
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from pydantic import ValidationError as PydanticValidationError
    from business_app.utils.api_responses import validation_error_response

    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    try:
        payload = DriverBottleSessionOpenRequest(**data)
    except PydanticValidationError as exc:
        return validation_error_response(exc.errors())

    service = BottleTrackingService()
    session = service.open_bottle_session(
        current_user_id,
        payload.bottles_loaded,
        actor_user_id=current_user_id,
        notes=payload.notes,
    )
    return success_response(serialize_bottle_session(session), status_code=201)


@staff_bp.route("/bottles/session/current", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def get_current_bottle_session():
    """Get driver's current open session, or null if none."""
    from business_app.serializers.bottle_serializers import serialize_bottle_session
    from business_app.services.bottle_tracking_service import BottleTrackingService

    current_user_id = get_jwt_identity()
    service = BottleTrackingService()
    session = service.get_open_session(current_user_id)
    return success_response(serialize_bottle_session(session) if session else None)


@staff_bp.route("/bottles/session/close", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def close_bottle_session():
    """Driver closes the active session by returning bottles to the warehouse."""
    from business_app.serializers.bottle_serializers import (
        DriverBottleSessionCloseRequest,
        serialize_bottle_session,
    )
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from pydantic import ValidationError as PydanticValidationError
    from business_app.utils.api_responses import validation_error_response

    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    try:
        payload = DriverBottleSessionCloseRequest(**data)
    except PydanticValidationError as exc:
        return validation_error_response(exc.errors())

    service = BottleTrackingService()
    session = service.close_bottle_session(
        current_user_id,
        payload.bottles_returned_to_warehouse,
        actor_user_id=current_user_id,
        notes=payload.notes,
    )
    return success_response(serialize_bottle_session(session))


@staff_bp.route("/bottles/sessions", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def list_my_bottle_sessions():
    """Get paginated session history for the calling driver."""
    from business_app.serializers.bottle_serializers import serialize_bottle_session
    from business_app.services.bottle_tracking_service import BottleTrackingService

    current_user_id = get_jwt_identity()
    service = BottleTrackingService()
    result = service.get_driver_sessions(
        current_user_id,
        page=request.args.get("page", 1, type=int),
        per_page=request.args.get("per_page", 20, type=int),
        status=request.args.get("status"),
    )
    return success_response(
        {
            "items": [serialize_bottle_session(s) for s in result["items"]],
            "total": result["total"],
            "page": result["page"],
            "per_page": result["per_page"],
            "pages": result["pages"],
        }
    )


# --- Co-driver session membership endpoints ---


@staff_bp.route("/bottles/sessions/joinable", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def list_joinable_bottle_sessions():
    """Return all open bottle sessions the calling driver can join as a co-driver."""
    from business_app.serializers.bottle_serializers import serialize_joinable_session
    from business_app.services.bottle_tracking_service import BottleTrackingService

    current_user_id = get_jwt_identity()
    service = BottleTrackingService()
    sessions = service.get_joinable_sessions(excluding_driver_id=current_user_id)
    return success_response([serialize_joinable_session(s) for s in sessions])


@staff_bp.route("/bottles/session/join", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def join_bottle_session():
    """Driver joins another driver's open session as a co-driver."""
    from business_app.serializers.bottle_serializers import (
        JoinSessionRequest,
        serialize_session_membership,
    )
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from pydantic import ValidationError as PydanticValidationError
    from business_app.utils.api_responses import validation_error_response

    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    try:
        payload = JoinSessionRequest(**data)
    except PydanticValidationError as exc:
        return validation_error_response(exc.errors())

    service = BottleTrackingService()
    membership = service.join_session(current_user_id, payload.session_id)
    return success_response(serialize_session_membership(membership), status_code=201)


@staff_bp.route("/bottles/session/leave", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def leave_bottle_session():
    """Driver leaves their current co-driver session membership."""
    from business_app.serializers.bottle_serializers import serialize_session_membership
    from business_app.services.bottle_tracking_service import BottleTrackingService

    current_user_id = get_jwt_identity()
    service = BottleTrackingService()
    membership = service.leave_session(current_user_id)
    return success_response(serialize_session_membership(membership))


@staff_bp.route("/bottles/session/membership", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def get_current_session_membership():
    """Return current co-driver membership info, or 404 if not a member of any session."""
    from business_app.serializers.bottle_serializers import serialize_membership_session_info
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from business_app.utils.exceptions import NotFoundError

    current_user_id = get_jwt_identity()
    service = BottleTrackingService()
    membership = service.get_active_membership(current_user_id)
    if not membership:
        raise NotFoundError(
            "No active co-driver session membership",
            error_code="BOTTLE_SESSION_MEMBERSHIP_NOT_FOUND",
        )
    session = service.get_open_session(membership.session_owner_id)
    if not session:
        raise NotFoundError(
            "The session you joined is no longer open",
            error_code="BOTTLE_SESSION_NOT_OPEN",
        )
    return success_response(serialize_membership_session_info(membership, session))


@staff_bp.route("/bottles/session/invite", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def invite_driver_to_session():
    """Session owner invites another driver to join their open session.

    The owner must have an open session. The invited driver must not already
    have their own open session or an active membership in another session.
    """
    from business_app.serializers.bottle_serializers import serialize_session_membership
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from business_app.utils.exceptions import ValidationError, ConflictError

    current_user_id = get_jwt_identity()
    data = request.get_json() or {}
    member_driver_id = data.get("member_driver_id")
    if not member_driver_id:
        raise ValidationError("member_driver_id is required", error_code="INVITE_MEMBER_REQUIRED")

    service = BottleTrackingService()
    owner_session = service.get_open_session(current_user_id)
    if not owner_session:
        raise ConflictError(
            "You must have an open bottle session to invite co-drivers",
            error_code="BOTTLE_SESSION_NOT_FOUND",
        )
    # Reuse join_session from the member's perspective but initiated by owner
    membership = service.join_session(int(member_driver_id), owner_session.id)
    return success_response(serialize_session_membership(membership), status_code=201)


@staff_bp.route("/bottles/sessions/available-drivers", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def list_drivers_available_to_invite():
    """Return delivery drivers who can be invited to the caller's session.

    A driver is eligible if they have no own open session and no active membership.
    """
    from business_app.services.bottle_tracking_service import BottleTrackingService

    current_user_id = get_jwt_identity()
    service = BottleTrackingService()
    return success_response(service.list_eligible_co_drivers(current_user_id))


# --- Transfer endpoints ---


@staff_bp.route("/bottles/transfers/pending", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def get_pending_bottle_transfers():
    """Get transfers pending confirmation by the calling driver (receiver inbox)."""
    from business_app.serializers.bottle_serializers import serialize_bottle_transfer
    from business_app.services.bottle_tracking_service import BottleTrackingService

    current_user_id = get_jwt_identity()
    service = BottleTrackingService()
    transfers = service.get_pending_transfers_for_driver(current_user_id)
    return success_response([serialize_bottle_transfer(t) for t in transfers])


@staff_bp.route("/bottles/transfers", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def initiate_bottle_transfer():
    """Driver initiates a mid-route bottle transfer to another driver."""
    from business_app.serializers.bottle_serializers import (
        DriverBottleTransferCreateRequest,
        serialize_bottle_transfer,
    )
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from pydantic import ValidationError as PydanticValidationError
    from business_app.utils.api_responses import validation_error_response

    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    try:
        payload = DriverBottleTransferCreateRequest(**data)
    except PydanticValidationError as exc:
        return validation_error_response(exc.errors())

    service = BottleTrackingService()
    transfer = service.initiate_bottle_transfer(
        sender_driver_id=current_user_id,
        receiver_driver_id=payload.receiver_driver_id,
        declared_quantity=payload.quantity,
        notes=payload.notes,
    )
    return success_response(serialize_bottle_transfer(transfer), status_code=201)


@staff_bp.route("/bottles/transfers/<int:transfer_id>/confirm", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def confirm_bottle_transfer(transfer_id: int):
    """Receiver confirms (or disputes) a pending bottle transfer."""
    from business_app.serializers.bottle_serializers import (
        DriverBottleTransferConfirmRequest,
        serialize_bottle_transfer,
    )
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from pydantic import ValidationError as PydanticValidationError
    from business_app.utils.api_responses import validation_error_response

    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    try:
        payload = DriverBottleTransferConfirmRequest(**data)
    except PydanticValidationError as exc:
        return validation_error_response(exc.errors())

    service = BottleTrackingService()
    transfer = service.confirm_bottle_transfer(
        transfer_id=transfer_id,
        receiver_driver_id=current_user_id,
        confirmed_quantity=payload.confirmed_quantity,
        notes=payload.notes,
    )
    return success_response(serialize_bottle_transfer(transfer))
