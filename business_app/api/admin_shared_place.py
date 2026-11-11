"""Admin review of shared-place cases (spec 2026-11-10 §4.5, §4.7; §9 binds): the Driver reports tab's list and case,
and the one call that resolves a case.

Admins only: every route is guarded by `SHARED_PLACE_REVIEW_PERMISSIONS` (`manage_users`), and the admin UI shows
the tab and badge by the same rule, the `can_review_shared_places` permission. No model access here:
`SharedPlaceReview` builds every published field and `SharedPlaceResolve` makes every write.
"""

from flask import Blueprint, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from pydantic import ValidationError as PydanticValidationError

from business_app.serializers.shared_place_serializers import SharedPlaceCaseListQuery, SharedPlaceResolveRequest
from business_app.services.shared_place.permissions import SHARED_PLACE_REVIEW_PERMISSIONS
from business_app.services.shared_place.resolve import SharedPlaceResolve
from business_app.services.shared_place.review import SharedPlaceReview
from business_app.utils.api_responses import success_response
from business_app.utils.constants import DEFAULT_PAGE_SIZE
from business_app.utils.decorators import validate_admin_action
from business_app.utils.error_handlers import handle_api_exception
from business_app.utils.exceptions import ValidationError

admin_shared_place_bp = Blueprint("admin_shared_place", __name__)


@admin_shared_place_bp.route("/shared-place-cases", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(list(SHARED_PLACE_REVIEW_PERMISSIONS))
def list_shared_place_cases():
    # The ints are pre-parsed so `?page=abc` lands on the default; an empty status means the default (open).
    params = SharedPlaceCaseListQuery.model_validate(
        {
            "status": request.args.get("status") or "open",
            "page": request.args.get("page", 1, type=int),
            "per_page": request.args.get("per_page", DEFAULT_PAGE_SIZE, type=int),
        }
    )
    return success_response(
        data=SharedPlaceReview.list_cases(status=params.status, page=params.page, per_page=params.per_page)
    )


@admin_shared_place_bp.route("/shared-place-cases/<int:case_id>", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(list(SHARED_PLACE_REVIEW_PERMISSIONS))
def get_shared_place_case(case_id: int):
    return success_response(data={"case": SharedPlaceReview.case_detail(case_id)})


@admin_shared_place_bp.route("/shared-place-cases/<int:case_id>/resolve", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(list(SHARED_PLACE_REVIEW_PERMISSIONS))
def resolve_shared_place_case(case_id: int):
    """Settle a case with one of the six choices. Repeating the stored outcome is a no-op that answers the case."""
    try:
        body = SharedPlaceResolveRequest.model_validate(request.get_json(silent=True))
    except PydanticValidationError as exc:
        raise ValidationError("Invalid shared-place resolution", error_code="SHARED_PLACE_RESOLVE_INVALID") from exc
    return success_response(
        data=SharedPlaceResolve.resolve(case_id, actor_user_id=int(get_jwt_identity()), payload=body.model_dump())
    )
