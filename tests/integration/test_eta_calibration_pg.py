"""The weekly fit on the real database (honest-ETA Phase 2, Task 8): the fit's SQL and its row as Postgres runs them.

`tests/unit/test_eta_plan_calibration.py` runs the fit's rules on SQLite, which compares timestamps as text and has no
enum type. This file runs one fit on a fully migrated Postgres, where the delivered and picked-up moves are read through
the `delivery_status` enum, the window and `fitted_at` are timestamptz, the row's JSON reads back with its nulls and
floats, `current_params` finds the row, the driver's loads are read from `driver_bottle_sessions`, and the unique window
refuses a second row for the same data. The rows are the builders' on a fixed `NOW`; the fit is never run against the
development database.
"""

from datetime import date, time, timedelta

import pytest

from business_app.models.delivery_promise import MODEL_ENGINE_S1, DeliveryPromise
from business_app.models.eta_model_fit import EtaModelFit
from business_app.services.eta_plan import calibration
from business_app.services.eta_plan.calibration import EXCLUDED_REASONS, EtaCalibration
from business_app.services.eta_plan.params import current_params
from business_app.services.eta_plan.windows import bucket_key
from business_app.utils.delivery_window import window_edge_utc
from business_app.utils.local_windows import local_instant_at_minutes
from business_app.utils.timezone_utils import ensure_utc
from shared import business_config
from shared.constants import ETA_STAGE_WAITING
from shared.enums import DeliveryStatus
from tests.integration.eta_plan_builders import make_driver, open_session, order_at_km, tap

pytestmark = pytest.mark.integration

MONDAY = date(2026, 11, 2)
NOW = local_instant_at_minutes(MONDAY, 5 * 60 + 30)  # 05:30 in Tashkent, a Monday: when the beat runs
DAY = date(2026, 10, 20)  # a plan day inside the 28 the fit reads


def at(minutes: float):
    return local_instant_at_minutes(DAY, minutes)


def test_a_fit_runs_on_postgres_and_a_second_row_for_the_window_is_refused(pg_app, pg_db, monkeypatch):
    driver = make_driver(pg_db)
    # Four stops 2, 6, 2 and 6 km from the depot, all picked up at 09:00 and delivered at 10:00, 10:40, 11:20 and 12:00:
    # legs of 4 km and 40 minutes, but the driver loaded again at 11:40, so the last leg carries a reload and is left
    # out. One depot pair (2 km, 60 minutes), and an engine row that expected the first stop at 09:50.
    orders = [order_at_km(pg_db, km, created_at=at(480), driver=driver) for km in (2.0, 6.0, 2.0, 6.0)]
    for order in orders:
        tap(pg_db, order.delivery, DeliveryStatus.PICKED_UP, at(540))
    for order, minutes in zip(orders, (600, 640, 680, 720)):
        tap(pg_db, order.delivery, DeliveryStatus.DELIVERED, at(minutes))
    open_session(pg_db, driver, at(700), loaded=30)
    pg_db.session.add(
        DeliveryPromise(
            delivery_id=orders[0].delivery.id,
            computed_at=at(420),
            stage=ETA_STAGE_WAITING,
            model_version=MODEL_ENGINE_S1,
            shown=False,
            expected_at=at(590),
            stops_ahead=1,
            day_started=True,
        )
    )
    pg_db.session.commit()

    fit = EtaCalibration().fit(NOW)

    stored = EtaModelFit.query.one()
    assert stored.id == fit.id
    assert ensure_utc(stored.data_to) == window_edge_utc(MONDAY, time(5, 0))
    assert ensure_utc(stored.data_from) == window_edge_utc(MONDAY - timedelta(days=28), time(5, 0))
    assert ensure_utc(stored.fitted_at) == NOW
    # Too few of anything to fit a component, so every one keeps the seeds, and says so.
    assert stored.rows_used == {
        "cycle": 2,
        "depot": 1,
        "window": 1,
        "excluded": {**dict.fromkeys(EXCLUDED_REASONS, 0), "reload": 1},
    }
    assert stored.params["cycle_points"] == [list(point) for point in business_config.ETA_CYCLE_SEED_POINTS]
    assert stored.params["depot_line"] == list(business_config.ETA_DEPOT_LEG_SEED)
    assert stored.params["window_buckets"] == {bucket_key(1, True): {"lo": None, "hi": None, "n": 1}}
    assert set(stored.kept_previous) == {f"bin:{n}" for n in range(6)} | {"depot"}
    # The leg that ended at 11:20 is in the first time-of-day band; the one that ended at 10:40 is before it. Both ended
    # at an order of one bottle.
    assert set(stored.params["monitoring"]["by_hour_band"]) == {"11-13"}
    assert stored.params["monitoring"]["by_bottles_band"]["0-5"]["n"] == 2
    # The engine finds the row through its timestamptz comparison.
    assert current_params(NOW + timedelta(minutes=1)).source == f"fit:{fit.id}"

    # A run that made its check before this one committed is refused by the unique window, and the session survives.
    checks = []
    already_fitted = calibration._already_fitted

    def check_made_too_early(data_to):
        checks.append(data_to)
        return False if len(checks) == 1 else already_fitted(data_to)

    monkeypatch.setattr(calibration, "_already_fitted", check_made_too_early)
    assert EtaCalibration().fit(NOW + timedelta(hours=1)) is None
    assert len(checks) == 2
    assert EtaModelFit.query.count() == 1
