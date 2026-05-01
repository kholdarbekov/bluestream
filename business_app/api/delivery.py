"""
Delivery API endpoints
This file should be placed in business_app/api/delivery.py
"""

from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import func
from datetime import datetime, UTC, timedelta, date

from business_app import db
from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.user import User
from business_app.utils.service_factory import get_delivery_service, get_maps_service, get_file_storage_service
from business_app.utils.error_handlers import handle_api_exception
from business_app.utils.api_responses import success_response
from business_app.utils.translations import get_translation
from business_app.utils.exceptions import ValidationError, NotFoundError
from business_app.utils.validation_helpers import validate_list_request_params, FilterValidator
from business_app.utils.query_optimization import get_deliveries_optimized, PaginationOptimizer
from business_app.serializers.delivery_serializers import (
    serialize_delivery,
    serialize_delivery_list,
)
from business_app.utils.decorators import validate_json, rate_limit
from shared.enums import DeliveryStatus, UserRole
from business_app.tasks.delivery_tasks import (
    track_delivery_location_task,
    calculate_delivery_eta_task,
    handle_delivery_exception_task,
    process_delivery_confirmation_task,
)

delivery_bp = Blueprint("delivery", __name__)


@delivery_bp.route("/track/<tracking_number>", methods=["GET"])
@handle_api_exception
def track_delivery_public(tracking_number):
    """Public endpoint to track delivery by tracking number"""
    if not tracking_number or not tracking_number.strip():
        raise ValidationError(get_translation("api.delivery.error.tracking_number_required"))

    delivery = Delivery.query.filter_by(tracking_number=tracking_number.strip()).first()

    if not delivery:
        raise NotFoundError(
            get_translation("api.delivery.error.not_found"), details={"tracking_number": tracking_number}
        )

    # Public tracking info (limited details)
    tracking_info = {
        "tracking_number": delivery.tracking_number,
        "status": delivery.status.value,
        "estimated_delivery_time": (
            delivery.estimated_delivery_time.isoformat() if delivery.estimated_delivery_time else None
        ),
        "actual_delivery_time": delivery.actual_delivery_time.isoformat() if delivery.actual_delivery_time else None,
        "delivery_attempts": delivery.delivery_attempts,
        "order_number": delivery.order.order_number,
        "timeline": get_delivery_service().get_delivery_timeline(delivery.id),
    }

    # Add driver info if delivery is in progress
    if delivery.status in [DeliveryStatus.ASSIGNED, DeliveryStatus.IN_TRANSIT, DeliveryStatus.ARRIVED]:
        if delivery.delivery_person:
            tracking_info["driver"] = {
                "name": delivery.delivery_person.first_name,
                "phone": delivery.delivery_person.phone,
            }

    return success_response(
        data={"delivery": tracking_info}, message=get_translation("api.delivery.tracking_retrieved")
    )


@delivery_bp.route("/my-deliveries", methods=["GET"])
@jwt_required()
@handle_api_exception
def get_my_deliveries():
    """Get current user's deliveries"""
    # Validate request parameters using centralized validation
    params = validate_list_request_params(
        default_per_page=20,
        max_per_page=50,
        allow_status_filter=True,
        status_enum=DeliveryStatus,
        allow_date_filter=True,
        allow_future_dates=True,
    )

    # Build query through orders
    query = Delivery.query.join(Order).filter(Order.user_id == params["user_id"])

    # Apply filters using centralized filter builders
    query = FilterValidator.build_status_filter_query(query, Delivery.status, params.get("status"))

    query = FilterValidator.build_date_filter_query(
        query, Delivery.scheduled_date, params.get("start_date"), params.get("end_date")
    )

    # Order by scheduled date (newest first)
    query = query.order_by(Delivery.scheduled_date.desc())

    # Apply eager loading for deliveries
    query = get_deliveries_optimized(query)

    # Paginate with optimized query
    pagination = PaginationOptimizer.optimize_paginated_query(
        query, params["page"], params["per_page"], eager_load_strategy="delivery_with_order"
    )

    # Serialize deliveries using the proper serializer
    serialized_deliveries = serialize_delivery_list(pagination.items, user_view=True)

    # Build standardized pagination response
    response_data = {
        "items": serialized_deliveries,
        "pagination": {
            "page": pagination.page,
            "per_page": pagination.per_page,
            "total": pagination.total,
            "pages": pagination.pages,
        },
    }

    return success_response(
        data={"deliveries": response_data["items"], "pagination": response_data["pagination"]},
        message=get_translation("api.delivery.list_retrieved"),
    )


@delivery_bp.route("/<int:delivery_id>/live-tracking", methods=["GET"])
@jwt_required()
def get_live_tracking(delivery_id):
    """Get live tracking information for a delivery"""
    try:
        current_user_id = get_jwt_identity()

        # Verify user owns this delivery
        delivery = (
            Delivery.query.join(Order).filter(Delivery.id == delivery_id, Order.user_id == current_user_id).first()
        )

        if not delivery:
            return jsonify({"error": get_translation("api.delivery.error.not_found")}), 404

        # Check if delivery is trackable
        if delivery.status not in [DeliveryStatus.ASSIGNED, DeliveryStatus.IN_TRANSIT, DeliveryStatus.ARRIVED]:
            return jsonify({"error": get_translation("api.delivery.error.not_trackable")}), 400

        tracking_data = {
            "delivery_id": delivery.id,
            "tracking_number": delivery.tracking_number,
            "status": delivery.status.value,
            "current_location": (
                {
                    "lat": delivery.current_location_lat,
                    "lng": delivery.current_location_lng,
                    "last_update": delivery.last_location_update.isoformat() if delivery.last_location_update else None,
                }
                if delivery.current_location_lat and delivery.current_location_lng
                else None
            ),
            "estimated_delivery_time": (
                delivery.estimated_delivery_time.isoformat() if delivery.estimated_delivery_time else None
            ),
            "driver": (
                {"name": delivery.delivery_person.full_name, "phone": delivery.delivery_person.phone}
                if delivery.delivery_person
                else None
            ),
            "delivery_address": (
                {
                    "lat": delivery.order.delivery_address.latitude,
                    "lng": delivery.order.delivery_address.longitude,
                    "address": delivery.order.delivery_address.address_line1,
                }
                if delivery.order.delivery_address
                else None
            ),
        }

        # Calculate distance to destination if current location available
        if (
            delivery.current_location_lat
            and delivery.current_location_lng
            and delivery.order.delivery_address
            and delivery.order.delivery_address.latitude
        ):

            distance = get_maps_service().calculate_distance(
                delivery.current_location_lat,
                delivery.current_location_lng,
                delivery.order.delivery_address.latitude,
                delivery.order.delivery_address.longitude,
            )
            tracking_data["distance_to_destination_km"] = round(distance, 2)

        return jsonify({"tracking": tracking_data})

    except Exception as e:
        current_app.logger.error(f"Get live tracking error: {e}")
        return jsonify({"error": get_translation("api.delivery.error.get_live_tracking_failed")}), 500


@delivery_bp.route("/time-slots", methods=["GET"])
def get_time_slots():
    """Get available delivery time slots for a date"""
    try:
        target_date_str = request.args.get("date")

        if not target_date_str:
            return jsonify({"error": get_translation("api.delivery.error.date_required")}), 400

        try:
            target_date = datetime.fromisoformat(target_date_str).date()
        except ValueError:
            return jsonify({"error": get_translation("api.delivery.error.invalid_date_format")}), 400

        # Cannot book for past dates
        if target_date < date.today():
            return jsonify({"error": get_translation("api.delivery.error.cannot_book_past_dates")}), 400

        slots_data = get_delivery_service().get_time_slot_availability(target_date)

        return jsonify({"date": target_date_str, "time_slots": slots_data})

    except Exception as e:
        current_app.logger.error(f"Get time slots error: {e}")
        return jsonify({"error": get_translation("api.delivery.error.get_time_slots_failed")}), 500


@delivery_bp.route("/calculate-fee", methods=["POST"])
@jwt_required()
@handle_api_exception
def calculate_delivery_fee():
    """
    Calculate delivery fee based on address and order total.
    Used by checkout page to get delivery fee when user selects an address.
    """
    from business_app.models.user import UserAddress

    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    address_id = data.get("address_id")
    order_total = data.get("order_total", 0)

    if not address_id:
        raise ValidationError(get_translation("api.delivery.error.address_id_required"))

    # Get the address
    address = UserAddress.query.filter_by(id=address_id, user_id=current_user_id).first()

    if not address:
        raise NotFoundError(get_translation("api.delivery.error.address_not_found"))

    # Use the delivery service to calculate fee
    delivery_service = get_delivery_service()

    # If address has coordinates, use them for calculation
    if address.latitude and address.longitude:
        delivery_fee = delivery_service.calculate_delivery_fee(
            latitude=address.latitude, longitude=address.longitude, order_total=int(order_total)
        )
    else:
        # Fallback: use default delivery fee logic from service
        delivery_fee = delivery_service.calculate_delivery_fee(latitude=0, longitude=0, order_total=int(order_total))

    return success_response(
        data={"delivery_fee": delivery_fee, "address_id": address_id, "order_total": order_total},
        message=get_translation("api.delivery.fee_calculated"),
    )


@delivery_bp.route("/zones", methods=["GET"])
def get_delivery_zones():
    """Get delivery zones and coverage areas"""
    try:
        # Get delivery zones from service
        zones = get_delivery_service().get_delivery_zones()

        return jsonify(
            {
                "delivery_zones": zones,
                "coverage_info": {
                    "base_delivery_fee": 3000,  # Base fee in UZS
                    "free_delivery_threshold": 50000,  # Free delivery above this amount
                    "max_delivery_distance": 25,  # km
                    "emergency_delivery_available": True,
                    "emergency_delivery_fee": 10000,
                },
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get delivery zones error: {e}")
        return jsonify({"error": get_translation("api.delivery.error.get_zones_failed")}), 500


@delivery_bp.route("/estimate-delivery", methods=["POST"])
@validate_json(["delivery_address_lat", "delivery_address_lng"])
def estimate_delivery():
    """Estimate delivery time and fee for an address"""
    try:
        data = request.get_json()

        delivery_lat = data.get("delivery_address_lat")
        delivery_lng = data.get("delivery_address_lng")
        urgency = data.get("urgency", "normal")  # normal, urgent, emergency

        # Estimate delivery details
        estimate = get_delivery_service().estimate_delivery(
            delivery_lat=delivery_lat, delivery_lng=delivery_lng, urgency=urgency
        )

        return jsonify({"estimate": estimate, "estimated_at": datetime.now(UTC).isoformat()})

    except ValueError as e:
        current_app.logger.warning(f"Estimate delivery validation error: {e}")
        return jsonify({"error": get_translation("api.delivery.error.estimate_validation_failed")}), 400
    except Exception as e:
        current_app.logger.error(f"Estimate delivery error: {e}")
        return jsonify({"error": get_translation("api.delivery.error.estimate_failed")}), 500


# Driver-specific endpoints (require driver role)
@delivery_bp.route("/driver/assignments", methods=["GET"])
@jwt_required()
def get_driver_assignments():
    """Get current driver's delivery assignments"""
    try:
        current_user_id = get_jwt_identity()
        user = User.query.get(current_user_id)

        role_value = user.role.value if hasattr(user.role, "value") else user.role
        if not user or role_value != UserRole.DELIVERY_DRIVER.value:
            return jsonify({"error": get_translation("api.delivery.error.driver_role_required")}), 403

        # Get query parameters
        status = request.args.get("status", "active")  # active, completed, all
        date_filter = request.args.get("date", "today")  # today, tomorrow, week

        # Build query
        query = Delivery.query.filter_by(delivery_person_id=current_user_id)

        # Apply status filter
        if status == "active":
            query = query.filter(
                Delivery.status.in_([DeliveryStatus.ASSIGNED, DeliveryStatus.IN_TRANSIT, DeliveryStatus.ARRIVED])
            )
        elif status == "completed":
            query = query.filter(Delivery.status.in_([DeliveryStatus.DELIVERED, DeliveryStatus.FAILED]))

        # Apply date filter
        today = date.today()
        if date_filter == "today":
            query = query.filter(func.date(Delivery.scheduled_date) == today)
        elif date_filter == "tomorrow":
            tomorrow = today + timedelta(days=1)
            query = query.filter(func.date(Delivery.scheduled_date) == tomorrow)
        elif date_filter == "week":
            week_end = today + timedelta(days=7)
            query = query.filter(
                func.date(Delivery.scheduled_date) >= today, func.date(Delivery.scheduled_date) <= week_end
            )

        # Order by priority and scheduled time
        query = query.order_by(Delivery.order.is_urgent.desc(), Delivery.scheduled_date.asc())

        deliveries = query.all()

        # Serialize deliveries using the proper serializer
        deliveries_data = []
        for delivery in deliveries:
            delivery_data = serialize_delivery(delivery, include_sensitive=False, user_view=False)

            # Add route information if available
            if delivery.route_data and "sequence" in delivery.route_data:
                delivery_data["route_sequence"] = delivery.route_data["sequence"]

            deliveries_data.append(delivery_data)

        return jsonify(
            {
                "assignments": deliveries_data,
                "summary": {
                    "total_assignments": len(deliveries),
                    "urgent_assignments": len([d for d in deliveries if d.order.is_urgent]),
                    "estimated_completion_time": get_delivery_service().estimate_route_completion_time(deliveries),
                },
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get driver assignments error: {e}")
        return jsonify({"error": get_translation("api.delivery.error.get_assignments_failed")}), 500


@delivery_bp.route("/driver/update-location", methods=["POST"])
@jwt_required()
@validate_json(["lat", "lng"])
@rate_limit(30, 60)  # Allow frequent location updates (30 requests per 60 seconds)
def update_driver_location():
    """Update driver's current location"""
    try:
        current_user_id = get_jwt_identity()
        user = User.query.get(current_user_id)

        role_value = user.role.value if hasattr(user.role, "value") else user.role
        if not user or role_value != UserRole.DELIVERY_DRIVER.value:
            return jsonify({"error": get_translation("api.delivery.error.driver_role_required")}), 403

        data = request.get_json()
        lat = data.get("lat")
        lng = data.get("lng")

        # Validate coordinates
        if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            return jsonify({"error": get_translation("api.delivery.error.invalid_coordinates")}), 400

        # Get active deliveries for this driver
        active_deliveries = (
            Delivery.query.filter_by(delivery_person_id=current_user_id)
            .filter(Delivery.status.in_([DeliveryStatus.ASSIGNED, DeliveryStatus.IN_TRANSIT]))
            .all()
        )

        # Update location for all active deliveries
        for delivery in active_deliveries:
            track_delivery_location_task.delay(delivery.id, lat, lng)

            # Calculate new ETA
            calculate_delivery_eta_task.delay(delivery.id)

        return jsonify({"message": get_translation("api.delivery.location_updated_successfully")})

    except Exception as e:
        current_app.logger.error(f"Update driver location error: {e}")
        return jsonify({"error": get_translation("api.delivery.error.update_location_failed")}), 500


@delivery_bp.route("/driver/start-delivery/<int:delivery_id>", methods=["POST"])
@jwt_required()
def start_delivery(delivery_id):
    """Start a delivery (mark as picked up)"""
    try:
        current_user_id = get_jwt_identity()
        user = User.query.get(current_user_id)

        role_value = user.role.value if hasattr(user.role, "value") else user.role
        if not user or role_value != UserRole.DELIVERY_DRIVER.value:
            return jsonify({"error": get_translation("api.delivery.error.driver_role_required")}), 403

        delivery = Delivery.query.filter_by(id=delivery_id, delivery_person_id=current_user_id).first()

        if not delivery:
            return jsonify({"error": get_translation("api.delivery.error.not_found_or_not_assigned")}), 404

        if delivery.status != DeliveryStatus.ASSIGNED:
            return jsonify({"error": get_translation("api.delivery.error.cannot_start_at_stage")}), 400

        delivery = get_delivery_service().begin_delivery_in_transit(
            delivery_id,
            actor_user_id=current_user_id,
            required_driver_id=current_user_id,
            notes="Delivery started via driver API",
        )

        return jsonify(
            {
                "message": get_translation("api.delivery.started_successfully"),
                "delivery": serialize_delivery(delivery, user_view=False),
            }
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Start delivery error: {e}")
        return jsonify({"error": get_translation("api.delivery.error.start_failed")}), 500


@delivery_bp.route("/driver/arrive/<int:delivery_id>", methods=["POST"])
@jwt_required()
def mark_arrived(delivery_id):
    """Mark delivery as arrived at destination"""
    try:
        current_user_id = get_jwt_identity()
        user = User.query.get(current_user_id)

        role_value = user.role.value if hasattr(user.role, "value") else user.role
        if not user or role_value != UserRole.DELIVERY_DRIVER.value:
            return jsonify({"error": get_translation("api.delivery.error.driver_role_required")}), 403

        delivery = Delivery.query.filter_by(id=delivery_id, delivery_person_id=current_user_id).first()

        if not delivery:
            return jsonify({"error": get_translation("api.delivery.error.not_found_or_not_assigned")}), 404

        if delivery.status != DeliveryStatus.IN_TRANSIT:
            return jsonify({"error": get_translation("api.delivery.error.must_be_in_transit_to_arrive")}), 400

        delivery = get_delivery_service().mark_delivery_arrived(
            delivery_id,
            actor_user_id=current_user_id,
            required_driver_id=current_user_id,
            notes="Marked as arrived via driver API",
        )

        return jsonify(
            {
                "message": get_translation("api.delivery.arrived_marked_successfully"),
                "delivery": serialize_delivery(delivery, user_view=False),
            }
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Mark arrived error: {e}")
        return jsonify({"error": get_translation("api.delivery.error.mark_arrived_failed")}), 500


@delivery_bp.route("/driver/complete/<int:delivery_id>", methods=["POST"])
@jwt_required()
@validate_json()
def complete_delivery(delivery_id):
    """Complete a delivery with optional photos and signature"""
    try:
        current_user_id = get_jwt_identity()
        user = User.query.get(current_user_id)

        role_value = user.role.value if hasattr(user.role, "value") else user.role
        if not user or role_value != UserRole.DELIVERY_DRIVER.value:
            return jsonify({"error": get_translation("api.delivery.error.driver_role_required")}), 403

        delivery = Delivery.query.filter_by(id=delivery_id, delivery_person_id=current_user_id).first()

        if not delivery:
            return jsonify({"error": get_translation("api.delivery.error.not_found_or_not_assigned")}), 404

        if delivery.status != DeliveryStatus.ARRIVED:
            return jsonify({"error": get_translation("api.delivery.error.must_be_arrived_before_completion")}), 400

        data = request.get_json()

        # Process confirmation data
        confirmation_data = {
            "photos": data.get("photos", []),
            "signature": data.get("signature"),
            "notes": data.get("notes"),
            "customer_present": data.get("customer_present", True),
        }

        # Process delivery completion asynchronously
        process_delivery_confirmation_task.delay(delivery_id, confirmation_data)

        return jsonify({"message": get_translation("api.delivery.completion_processing"), "delivery_id": delivery_id})

    except Exception as e:
        current_app.logger.error(f"Complete delivery error: {e}")
        return jsonify({"error": get_translation("api.delivery.error.complete_failed")}), 500


@delivery_bp.route("/driver/report-issue/<int:delivery_id>", methods=["POST"])
@jwt_required()
@validate_json(["issue_type"])
def report_delivery_issue(delivery_id):
    """Report an issue with delivery"""
    try:
        current_user_id = get_jwt_identity()
        user = User.query.get(current_user_id)

        role_value = user.role.value if hasattr(user.role, "value") else user.role
        if not user or role_value != UserRole.DELIVERY_DRIVER.value:
            return jsonify({"error": get_translation("api.delivery.error.driver_role_required")}), 403

        delivery = Delivery.query.filter_by(id=delivery_id, delivery_person_id=current_user_id).first()

        if not delivery:
            return jsonify({"error": get_translation("api.delivery.error.not_found_or_not_assigned")}), 404

        data = request.get_json()
        issue_type = data.get("issue_type")  # delay, failed_attempt, vehicle_breakdown, customer_issue
        details = data.get("details", {})

        # Valid issue types
        valid_issues = ["delay", "failed_attempt", "vehicle_breakdown", "customer_issue", "address_issue"]
        if issue_type not in valid_issues:
            return jsonify({"error": get_translation("api.delivery.error.invalid_issue_type")}), 400

        # Handle delivery exception asynchronously
        handle_delivery_exception_task.delay(delivery_id, issue_type, details)

        return jsonify(
            {
                "message": get_translation("api.delivery.issue_reported_successfully"),
                "issue_type": issue_type,
                "delivery_id": delivery_id,
            }
        )

    except Exception as e:
        current_app.logger.error(f"Report delivery issue error: {e}")
        return jsonify({"error": get_translation("api.delivery.error.report_issue_failed")}), 500


@delivery_bp.route("/driver/route-optimization", methods=["POST"])
@jwt_required()
def request_route_optimization():
    """Request route optimization for driver's current assignments"""
    try:
        current_user_id = get_jwt_identity()
        user = User.query.get(current_user_id)

        role_value = user.role.value if hasattr(user.role, "value") else user.role
        if not user or role_value != UserRole.DELIVERY_DRIVER.value:
            return jsonify({"error": get_translation("api.delivery.error.driver_role_required")}), 403

        # Trigger route optimization
        from business_app.tasks.delivery_tasks import optimize_driver_route_task

        optimize_driver_route_task.delay(current_user_id)

        return jsonify({"message": get_translation("api.delivery.route_optimization_requested")})

    except Exception as e:
        current_app.logger.error(f"Request route optimization error: {e}")
        return jsonify({"error": get_translation("api.delivery.error.route_optimization_failed")}), 500


@delivery_bp.route("/upload-photo", methods=["POST"])
@jwt_required()
def upload_delivery_photo():
    """Upload delivery confirmation photo"""
    try:
        current_user_id = get_jwt_identity()
        user = User.query.get(current_user_id)

        role_value = user.role.value if hasattr(user.role, "value") else user.role
        if not user or role_value != UserRole.DELIVERY_DRIVER.value:
            return jsonify({"error": get_translation("api.delivery.error.driver_role_required")}), 403

        if "photo" not in request.files:
            return jsonify({"error": get_translation("api.delivery.error.no_photo_provided")}), 400

        file = request.files["photo"]
        if file.filename == "":
            return jsonify({"error": get_translation("api.delivery.error.no_file_selected")}), 400

        # Enhanced file validation and upload
        try:
            from business_app.utils.file_validation import validate_upload_file, FileValidationError

            # Validate with strict image-only policy
            validation_result = validate_upload_file(
                file=file, filename=file.filename, allowed_categories=["images"], expected_category="images"
            )

            # Additional check for delivery photos - only allow specific image types
            allowed_image_exts = {".jpg", ".jpeg", ".png"}
            file_ext = validation_result["validation_results"]["file_extension"]
            if file_ext not in allowed_image_exts:
                return (
                    jsonify({"error": get_translation("api.delivery.error.invalid_photo_type", file_ext=file_ext)}),
                    400,
                )

            # Check file size specifically for delivery photos (max 5MB)
            file_size = validation_result["validation_results"]["size"]
            max_delivery_photo_size = 5 * 1024 * 1024  # 5MB
            if file_size > max_delivery_photo_size:
                return (
                    jsonify(
                        {
                            "error": get_translation(
                                "api.delivery.error.photo_too_large", size_mb=f"{file_size / (1024*1024):.1f}"
                            )
                        }
                    ),
                    400,
                )

            # Upload using file storage service with validated data
            upload_result = get_file_storage_service().upload_image(
                file=file,
                filename=validation_result["safe_filename"],
                folder="delivery_photos",
                user_id=current_user_id,
                resize=True,
                max_width=1920,
                max_height=1080,
                quality=85,
            )

        except FileValidationError as e:
            current_app.logger.warning(f"File validation failed in delivery photo upload: {e}")
            return jsonify({"error": get_translation("api.delivery.error.file_validation_failed")}), 400
        except Exception as e:
            current_app.logger.error(f"File validation error in delivery photo upload: {e}")
            return jsonify({"error": get_translation("api.delivery.error.file_validation_failed")}), 400

        # The file storage service returns different format, adjust accordingly
        if upload_result:
            return jsonify(
                {
                    "message": get_translation("api.delivery.photo_uploaded_successfully"),
                    "photo_url": upload_result.get("url", ""),
                    "photo_path": upload_result.get("file_path", ""),
                    "file_info": {
                        "original_filename": upload_result.get("original_filename", ""),
                        "size": upload_result.get("size", 0),
                        "content_type": upload_result.get("content_type", ""),
                        "thumbnails": upload_result.get("thumbnails", {}),
                    },
                }
            )
        else:
            return jsonify({"error": get_translation("api.delivery.error.upload_no_result")}), 500

    except Exception as e:
        current_app.logger.error(f"Upload delivery photo error: {e}")
        return jsonify({"error": get_translation("api.delivery.error.upload_failed")}), 500
