"""Admin Lapsed-customers worklist (spec §9.1): the rows, the summary and the mandatory outcome.

Admins and managers only (`manager_or_higher_required`); the admin UI reads the same rule as the
`can_manage_lapsed_customers` permission. No model access here: the worklist service computes
every published field.
"""

from flask import Blueprint, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from business_app.serializers.reorder_reminder_serializers import (
    ResolveEpisodePayload,
    WorklistQuery,
    serialize_resolved_episode,
)
from business_app.serializers.sales_pay_serializers import to_wire
from business_app.services.reorder_reminders.actions import ReorderReminderActions
from business_app.services.reorder_reminders.worklist import ReorderReminderWorklist
from business_app.utils.api_responses import pagination_meta, success_response
from business_app.utils.constants import DEFAULT_PAGE_SIZE
from business_app.utils.decorators import manager_or_higher_required
from business_app.utils.error_handlers import handle_api_exception
from business_app.utils.request_helpers import validated_json_payload

admin_reorder_reminders_bp = Blueprint("admin_reorder_reminders", __name__)


@admin_reorder_reminders_bp.route("/reorder-reminders/episodes", methods=["GET"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def list_reorder_reminder_episodes():
    # The ints are pre-parsed so `?page=abc` lands on the default (the order-approvals precedent).
    # A cleared flag chip or search box sends an empty value, which means "no filter".
    params = WorklistQuery.model_validate(
        {
            **request.args.to_dict(),
            "flag": request.args.get("flag") or None,
            "search": (request.args.get("search") or "").strip() or None,
            "page": request.args.get("page", 1, type=int),
            "per_page": request.args.get("per_page", DEFAULT_PAGE_SIZE, type=int),
        }
    )
    rows, total = ReorderReminderWorklist.list_rows(
        tab=params.tab, flag=params.flag, search=params.search, page=params.page, per_page=params.per_page
    )
    # `meta` rides INSIDE `data` (the visits and order-approvals precedent).
    return success_response(
        data=to_wire({"items": rows, "meta": pagination_meta(page=params.page, per_page=params.per_page, total=total)})
    )


@admin_reorder_reminders_bp.route("/reorder-reminders/summary", methods=["GET"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def get_reorder_reminder_summary():
    # The badge, the page header and the Analytics cards read this one payload (D17).
    return success_response(data=to_wire(ReorderReminderWorklist.summary()))


@admin_reorder_reminders_bp.route("/reorder-reminders/episodes/<int:episode_id>/resolve", methods=["POST"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def resolve_reorder_reminder_episode(episode_id: int):
    payload = validated_json_payload(ResolveEpisodePayload)
    if not isinstance(payload, dict):
        return payload
    # The actions service is the one writer of an outcome, and of its do_not_contact side effect.
    episode = ReorderReminderActions.resolve(
        episode_id,
        outcome=payload["outcome"],
        note=payload.get("note"),
        actor_user_id=int(get_jwt_identity()),
    )
    return success_response(data={"episode": serialize_resolved_episode(episode)})
