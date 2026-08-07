"""Admin API for the dispatch map.

Thin HTTP layer: parsing, permissions, status-code mapping. All behaviour lives
in DispatchService (read) and RouteEditService (write).
"""

import hashlib
import json
import logging
from datetime import date

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from pydantic import ValidationError as PydanticValidationError

from business_app import redis_client
from business_app.serializers.dispatch_serializers import (
    AssignStopRequest,
    SetStopOrderRequest,
    UnassignStopRequest,
)
from business_app.services.dispatch_service import DispatchService
from business_app.services.maps_service import MapsService
from business_app.services.route_edit_service import RouteEditService, RouteStaleError
from business_app.services.route_optimization_service import RouteOptimizationService
from business_app.utils.api_responses import success_response, validation_error_response
from business_app.utils.decorators import validate_admin_action
from business_app.utils.error_handlers import handle_api_exception

logger = logging.getLogger(__name__)

admin_dispatch_bp = Blueprint("admin_dispatch", __name__)

# Geometry is keyed by the SEQUENCE, so panning, the 30s poll and re-selecting a
# driver are all free; only a real route change re-fetches. Invalidation is
# implicit — a new sequence is a new key.
GEOMETRY_CACHE_TTL_SECONDS = 900


def _validated(schema_cls):
    try:
        return schema_cls(**(request.get_json(silent=True) or {}))
    except PydanticValidationError as exc:
        return validation_error_response(exc.errors())


@admin_dispatch_bp.route("/dispatch/snapshot", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["view_delivery", "manage_delivery"])
def dispatch_snapshot():
    """Everything the dispatch map draws for one day."""
    raw = request.args.get("date")
    if raw:
        try:
            target = date.fromisoformat(raw)
        except ValueError:
            return validation_error_response("date must be YYYY-MM-DD")
    else:
        target = DispatchService.today()

    return success_response(data=DispatchService.get_snapshot(target))


@admin_dispatch_bp.route("/dispatch/routes/<int:driver_id>/geometry", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["view_delivery", "manage_delivery"])
def dispatch_route_geometry(driver_id):
    """Real road geometry for a driver's planned route."""
    route = RouteOptimizationService().current_route(driver_id)
    if route is None or not route.optimized_order:
        return success_response(data={"driver_id": driver_id, "geometry": None, "approximate": False, "cached": False})

    # Resolved via DispatchService (service-layer-first), not a raw model
    # query here: geometry is polled per selected driver, and rebuilding
    # every order/driver/route for one polyline is a lot of query for
    # nothing, so this resolves exactly the stops on ONE route.
    points = DispatchService.route_stop_coordinates(route)
    if not points:
        return success_response(data={"driver_id": driver_id, "geometry": None, "approximate": False, "cached": False})

    start = (route.start_location_lat, route.start_location_lng)
    digest = hashlib.sha1(json.dumps([start] + points).encode("utf-8")).hexdigest()  # noqa: S324
    cache_key = f"dispatch:route_geom:{driver_id}:{digest}"

    try:
        cached = redis_client.get(cache_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("geometry cache read failed driver=%s: %s", driver_id, exc)
        cached = None
    if cached:
        payload = json.loads(cached)
        payload["cached"] = True
        return success_response(data=payload)

    try:
        result = MapsService().get_route(
            start[0], start[1], points[-1][0], points[-1][1], waypoints=points[:-1] or None
        )
        # `MapsService.get_route()` already normalises every provider's
        # geometry into `[[lat, lng], ...] | None` (Google/OSRM's encoded
        # polylines decoded, Yandex's nested legs/steps/polyline.points
        # extracted) — this handler just relays it, deliberately with no
        # provider-shape knowledge of its own (boundary-coupling budget for
        # this file is 0, enforced by test_structure_boundary_regressions.py).
        geometry = result.get("geometry")
        payload = {
            "driver_id": driver_id,
            "geometry": geometry,
            "distance_km": result.get("distance_km"),
            "duration_minutes": result.get("duration_minutes"),
            # A provider call can succeed (no exception) yet still carry no
            # usable path — MapsService already reports that as `geometry:
            # None`, so mirror it here instead of hard-coding False. Getting
            # this wrong is exactly how the dashed fallback used to render
            # correctly (OperationsMap.jsx checks geometry length, not this
            # flag) while the API silently lied about it being real.
            "approximate": geometry is None,
            "cached": False,
        }
        try:
            redis_client.setex(cache_key, GEOMETRY_CACHE_TTL_SECONDS, json.dumps(payload))
        except Exception as exc:  # noqa: BLE001
            logger.warning("geometry cache write failed driver=%s: %s", driver_id, exc)
        return success_response(data=payload)
    except Exception as exc:  # noqa: BLE001
        # Degrade, never blank: the UI draws straight dashed legs and badges them
        # as approximate rather than showing an empty map or a fake road path.
        logger.warning("route geometry unavailable driver=%s: %s", driver_id, exc)
        return success_response(data={"driver_id": driver_id, "geometry": None, "approximate": True, "cached": False})


@admin_dispatch_bp.route("/dispatch/routes/<int:driver_id>/stops", methods=["PUT"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["manage_delivery"])
def dispatch_set_stops(driver_id):
    """Persist an admin-authored stop sequence."""
    payload = _validated(SetStopOrderRequest)
    if not isinstance(payload, SetStopOrderRequest):
        return payload

    try:
        route = RouteEditService.set_stop_order(
            driver_id=driver_id,
            ordered_delivery_ids=payload.ordered_delivery_ids,
            pinned=payload.pinned,
            actor_id=int(get_jwt_identity()),
            expected_delivery_ids=payload.expected_delivery_ids,
        )
    except RouteStaleError as exc:
        # 409, not 400: the request was well-formed, the world moved.
        return (
            jsonify(
                {
                    "success": False,
                    "message": str(exc),
                    "error_code": "DISPATCH_ROUTE_STALE",
                    "data": {"current_delivery_ids": exc.current_delivery_ids},
                }
            ),
            409,
        )

    return success_response(data={"route": route.to_dict()})


@admin_dispatch_bp.route("/dispatch/routes/<int:driver_id>/reoptimize", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["manage_delivery"])
def dispatch_reoptimize(driver_id):
    """Drop the manual lock and re-solve from scratch."""
    route = RouteEditService.reoptimize(driver_id=driver_id, actor_id=int(get_jwt_identity()))
    return success_response(data={"route": route.to_dict() if route else None})


@admin_dispatch_bp.route("/dispatch/stops/<int:delivery_id>/assign", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["manage_delivery"])
def dispatch_assign_stop(delivery_id):
    """Move a stop onto a driver (from another driver or from the pool)."""
    payload = _validated(AssignStopRequest)
    if not isinstance(payload, AssignStopRequest):
        return payload

    delivery = RouteEditService.move_stop(
        delivery_id=delivery_id,
        to_driver_id=payload.driver_id,
        actor_id=int(get_jwt_identity()),
        position=payload.position,
    )
    return success_response(data={"delivery_id": delivery.id})


@admin_dispatch_bp.route("/dispatch/stops/<int:delivery_id>/unassign", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["manage_delivery"])
def dispatch_unassign_stop(delivery_id):
    """Return a stop to the unassigned pool."""
    payload = _validated(UnassignStopRequest)
    if not isinstance(payload, UnassignStopRequest):
        return payload

    delivery = RouteEditService.return_stop_to_pool(
        delivery_id=delivery_id,
        actor_id=int(get_jwt_identity()),
        reason=payload.reason,
    )
    return success_response(data={"delivery_id": delivery.id})
