"""Admin delivery-time accuracy report (honest-ETA Phase 2, Task 9; spec §5, §9): the go-live gate of Phase 3.

Admins and managers only (`manager_or_higher_required`); the admin UI shows the page by the same rule, the
`can_view_eta_accuracy` permission. Read-only, and no model access here: `EtaAccuracyReport` computes every figure.
"""

from flask import Blueprint, current_app, request
from flask_jwt_extended import jwt_required

from business_app.services.eta_plan.report import EtaAccuracyReport
from business_app.utils.api_responses import success_response
from business_app.utils.decorators import manager_or_higher_required
from business_app.utils.error_handlers import handle_api_exception
from business_app.utils.local_windows import parse_date_range
from business_app.utils.timezone_utils import get_utc_now

admin_eta_bp = Blueprint("admin_eta", __name__)


@admin_eta_bp.route("/eta/accuracy", methods=["GET"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def get_eta_accuracy():
    # One clock reading for the range and the report. Absent dates mean the last ETA_REPORT_DEFAULT_DAYS local days.
    now = get_utc_now()
    start_date, end_date = parse_date_range(
        request.args.get("start_date"),
        request.args.get("end_date"),
        now=now,
        default_days=current_app.config["ETA_REPORT_DEFAULT_DAYS"],
    )
    return success_response(data=EtaAccuracyReport.build(start_date=start_date, end_date=end_date, now=now))
