"""Customer actions on a reorder reminder (spec 2026-10-09 §6 "Customer actions", §7.3, §14).

The two buttons on a reminder in the customer bot, *Remind me later* and *Stop these reminders*.
Thin by contract (API_BOUNDARY_SCORE_BUDGET = 0): the rules (Later only on an open step-1 lapse with
the max rule; Stop as a customer-off that never overrides an admin-off, D20) live in
ReorderReminderActions. `@handle_api_exception` turns its ValidationError into a 400 whose
`error_code` sits at the TOP level of the body, which is where telegram_bot/handlers/reorder_reminders.py
reads REORDER_REMINDER_NOT_ACTIVE.
"""

from flask import Blueprint, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from pydantic import ValidationError as PydanticValidationError

from business_app.serializers.reorder_reminder_serializers import StopReorderRemindersPayload
from business_app.services.reorder_reminders.actions import ReorderReminderActions
from business_app.utils.api_responses import success_response, validation_error_response
from business_app.utils.decorators import rate_limit
from business_app.utils.error_handlers import handle_api_exception

reorder_reminders_bp = Blueprint("reorder_reminders", __name__)

# The only client of POST /stop is the bot's "Stop these reminders" button, so the route names
# itself (spec §8 table). A request body can never claim another route.
STOP_VIA = "telegram_button"

# Remind-me-later taps per customer per hour. A lapse sends at most two reminders, so a few taps is
# every real use; the budget only stops a runaway client. It is per LOGIN (`per="user"`), never per
# IP: the bot calls from one address for every customer. Stop has NO budget: an opt-out must always
# work (spec §7.3: Stop never refuses; §14 sets no limit). A repeat tap changes no setting (D20), but
# it is not free: every tap still writes a `stop` event and a `setting_changed` / `already_off` event.
MAX_LATER_TAPS_PER_HOUR = 20


@reorder_reminders_bp.route("/<int:episode_id>/later", methods=["POST"])
@handle_api_exception
@jwt_required()
@rate_limit(max_requests=MAX_LATER_TAPS_PER_HOUR, window_seconds=3600, per="user")
def snooze_reorder_reminder(episode_id):
    """Remind me later: push reminder 2 out by REORDER_REMINDER_SNOOZE_DAYS (D13). The bot sends no body."""
    snooze_days = ReorderReminderActions.snooze(int(get_jwt_identity()), episode_id)
    return success_response(data={"snooze_days": snooze_days})


@reorder_reminders_bp.route("/stop", methods=["POST"])
@handle_api_exception
@jwt_required()
def stop_reorder_reminders():
    """Stop these reminders: customer-off for the caller (D5); a no-op over an admin-off (D20)."""
    body = request.get_json(silent=True)
    if body is None and not request.get_data():
        body = {}  # no body at all is fine; a body that is present must be the expected object
    try:
        payload = StopReorderRemindersPayload.model_validate(body)
    except PydanticValidationError as exc:
        return validation_error_response(exc.errors())

    effective = ReorderReminderActions.stop(int(get_jwt_identity()), via=STOP_VIA, episode_id=payload.episode_id)
    # The one published shape (master amendment 15). No `reason`: the admin's note is staff-only.
    return success_response(data={"reorder_reminders": effective.to_dict(include_reason=False)})
