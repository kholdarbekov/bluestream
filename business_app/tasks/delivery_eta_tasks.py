"""Celery entry points of the capacity-aware ETA plan (honest-ETA Phase 2).

Registered in tasks/celery_app.py `include`, and routed to the `delivery` queue with the other delivery tasks. The beat
runs `recompute_eta_plan` every ten minutes, at :03, :13 ... :53, as the backstop of the commit-driven requests
(`utils/eta_recompute_dispatch.request_recompute`), which publish it through `_publish_recompute`; and `fit_eta_model`
every Monday at 05:30, the weekly fit the engine reads its parameters from.

The engine and the calibration are imported inside the tasks, so this module stays light: the request side imports it to
publish.
"""

from celery import shared_task
from flask import current_app

from business_app.utils.timezone_utils import get_utc_now
from shared.business_config import ETA_RECOMPUTE_LOCK_SECONDS

# Ruling P6. A run is killed at the run lock's own seconds, so a run that dies frees the lock as it expires; the soft
# limit comes first, so a slow run stops and releases the lock itself. Read from business_config at import: a decorator
# runs before the app's config exists.
_SOFT_LIMIT_MARGIN_SECONDS = 20


@shared_task(
    time_limit=ETA_RECOMPUTE_LOCK_SECONDS,
    soft_time_limit=ETA_RECOMPUTE_LOCK_SECONDS - _SOFT_LIMIT_MARGIN_SECONDS,
)
def recompute_eta_plan() -> dict:
    """One recompute of the plan (`EtaEngine.recompute`), returning its summary."""
    from business_app.services.eta_plan.engine import EtaEngine

    return EtaEngine().recompute()


@shared_task
def fit_eta_model() -> dict:
    """The weekly fit (`EtaCalibration.fit`) as of now, returning its summary: the new row's `fit_id`, `data_to` and
    `kept_previous`. `{"skipped": reason}` when it did nothing: "disabled" (the kill switch ETA_SHADOW_ENGINE_ENABLED)
    or "already_fitted" (the window of plan days has its fit)."""
    if not current_app.config["ETA_SHADOW_ENGINE_ENABLED"]:
        return {"skipped": "disabled"}
    from business_app.services.eta_plan.calibration import EtaCalibration

    fit = EtaCalibration().fit(get_utc_now())
    if fit is None:
        return {"skipped": "already_fitted"}
    return {"fit_id": fit.id, "data_to": fit.data_to.isoformat(), "kept_previous": fit.kept_previous}
