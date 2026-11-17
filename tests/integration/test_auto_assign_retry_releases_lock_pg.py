"""REAL POSTGRES: auto-assign never waits on its own row lock when it retries (Task Y2, dev-drive defect D2).

`auto_assign_delivery_task` locks the delivery row (`with_for_update`) and then retries: in 15 minutes when no driver
is free, after the default delay when anything raised. Dev runs Celery with `task_always_eager`
(business_app/config/development.py), so a worker that took the task off the broker publishes its retry eagerly:
`self.retry()` runs the task again at once, inline, through `ContextTask`, in a new app context, so on a new session and
a new Postgres connection, and that run asks for the same row lock. While the first run's transaction still held it,
the retry waited on a transaction that was waiting on the retry: the dev worker hung until the 600 s hard time limit
killed it (delivery 110 of the 2026-11-16 drive). In production the retry goes to the broker and the lock went only at
the worker's teardown. The rule: the task ends its own transaction before it schedules a retry, on both retry paths.

The task runs here as that dev worker runs it: the business Celery app `make_celery` builds for this Postgres app (its
`ContextTask` pushes a new app context per run; the testing config is eager, as dev's is), the task traced the way a
worker traces a message off the broker (`build_tracer(..., eager=False)`), and the real `apply_async`, which the suite
otherwise mocks. `.apply()` alone does not show the hang: an eager run's retry is re-applied only after the run has
returned and its app context has rolled back. Every wait is bounded: the worker gets 30 s on a daemon thread, and the
teardown terminates any backend still stuck, so a regression fails here instead of wedging the suite.

The sibling tasks of the module that retry on any error hold row locks too, taken by their callees, not by a
`with_for_update` of their own: the delivery confirmation claims the order row (`OrderService._claim_status_transition`,
an UPDATE) and runs the delivered branch before its commit; the delay report writes the delivery
row, which the next query flushes. Each of them ends its transaction before it asks for the retry too, and is driven
here the same way.
"""

import threading
import uuid
from datetime import UTC, datetime

import pytest
from celery import states
from celery.app.defaults import DEFAULTS as CELERY_DEFAULTS
from celery.app.task import Task
from celery.app.trace import build_tracer
from celery.signals import task_postrun
from sqlalchemy import text

import business_app.tasks.delivery_tasks as delivery_tasks
from business_app.config.development import DevelopmentConfig
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.services.notification_service import NotificationService
from business_app.services.order_service import OrderService
from business_app.tasks.delivery_tasks import (
    auto_assign_delivery_task,
    handle_delivery_exception_task,
    process_delivery_confirmation_task,
)
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import DeliveryStatus, OrderStatus
from tests.integration.eta_plan_builders import celery_app_module_fixture  # noqa: F401 -- the fixture
from tests.integration.eta_plan_builders import bind, make_driver, open_session, order_at_km
from tests.integration.test_customer_cancel_races_pg import _lock_waiters

pytestmark = pytest.mark.integration

ALL_DAY = ("00:00", "23:59")  # on shift whatever the hour: unavailability is the only reason nobody takes it
WORKER_WAIT_SECONDS = 30
EXHAUSTED = {"success": False, "error": "no_available_drivers_max_retries"}

# Celery's own `apply_async`, read at import: the autouse `block_external_side_effects` (tests/conftest.py) replaces it
# with a no-op for every test, and the eager inline retry is exactly what this file needs to run.
_REAL_APPLY_ASYNC = Task.apply_async


class _DevWorker:
    """The dev worker's copy of one delivery task: the business app's task, traced as a worker traces a message."""

    def __init__(self, task):
        self.task = task
        self.runs = []  # (state, retval) of every run of the task, in the order the runs ended
        self.threads = []

    def record_run(self, sender=None, task_id=None, task=None, args=None, kwargs=None, retval=None, state=None, **_):
        """A `task_postrun` receiver (Celery sends exactly these names, plus `signal`)."""
        self.runs.append((state, retval))

    def take(self, *args):
        """The worker takes the task off the broker with the positional `args` its publisher sends and gets 30 s.
        Answers the thread and what it produced: the worker's trace of its own run (`trace`) or what escaped it
        (`error`)."""
        tracer = build_tracer(self.task.name, self.task, app=self.task.app, eager=False, propagate=False)
        task_id = str(uuid.uuid4())
        outcome = {}

        def work():
            try:
                outcome["trace"] = tracer(task_id, list(args), {}, {"id": task_id, "retries": 0})
            except BaseException as exc:  # noqa: BLE001 -- named by the assertion that reads it
                outcome["error"] = exc

        thread = threading.Thread(target=work, name=f"dev-worker-{self.task.name}", daemon=True)
        self.threads.append(thread)
        thread.start()
        thread.join(timeout=WORKER_WAIT_SECONDS)
        return thread, outcome


# The table locks a transaction takes beside its row locks: ROW SHARE for SELECT ... FOR UPDATE, ROW EXCLUSIVE for an
# UPDATE / INSERT / DELETE. A plain read takes ACCESS SHARE only, which locks no row.
_ROW_LOCKING_MODES = ("RowShareLock", "RowExclusiveLock")
# The rows the delivery tasks here lock: the delivery, its order, and the crew's driver rows the bottle guard locks.
_DELIVERY_TABLES = ("deliveries", "orders", "delivery_persons")


def _idle_row_lockers(pg_db, tables, modes=_ROW_LOCKING_MODES):
    """Backends of this database sitting `idle in transaction` with a row-locking lock (`modes`) taken on one of
    `tables`. A fresh connection per read: `pg_stat_activity` is a snapshot per transaction."""
    with pg_db.engine.connect() as connection:
        holders = connection.execute(
            text(
                "SELECT DISTINCT a.pid FROM pg_locks l "
                "JOIN pg_class c ON c.oid = l.relation "
                "JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE c.relname = ANY(:tables) AND l.mode = ANY(:modes) "
                "AND a.datname = current_database() AND a.state = 'idle in transaction'"
            ),
            {"tables": list(tables), "modes": list(modes)},
        )
        return holders.scalars().all()


def _idle_holders_of_a_deliveries_lock(pg_db):
    """Idle transactions holding a `with_for_update` row lock on `deliveries` (ROW SHARE on the table)."""
    return _idle_row_lockers(pg_db, ("deliveries",), ("RowShareLock",))


def _release_the_stuck(pg_db, thread):
    """Terminate whatever keeps the worker waiting (its own idle transaction, its retry's lock wait) until the thread
    ends. Bounded: about a minute at most."""
    for _ in range(20):
        if not thread.is_alive():
            return
        with pg_db.engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                    "AND (state = 'idle in transaction' OR wait_event_type = 'Lock')"
                )
            )
            connection.commit()
        thread.join(timeout=3)


@pytest.fixture
def dev_worker_for(pg_app, pg_db, celery_app_module, monkeypatch):
    """Answers the dev worker of a delivery task (`dev_worker_for(task)`), on the business Celery app built for this
    Postgres app."""
    assert _REAL_APPLY_ASYNC.__module__ == "celery.app.task", "read after the suite mocked it"
    # `make_celery` rebinds the module's `_flask_app`; the test puts it back.
    monkeypatch.setattr(celery_app_module, "_flask_app", celery_app_module._flask_app)
    business_celery = celery_app_module.make_celery(pg_app)
    # Dev's eager settings, not the testing config's: dev leaves `task_eager_propagates` at Celery's default (off), so
    # an inline run's own Retry is handled where it is raised; the testing config turns it on.
    for key in ("task_always_eager", "task_eager_propagates"):
        business_celery.conf[key] = DevelopmentConfig.CELERY.get(key, CELERY_DEFAULTS[key])
    assert business_celery.conf.task_always_eager, "dev's eager config is the premise"
    workers = []

    def worker_for(task):
        bound = business_celery.tasks[task.name]
        # This task's own class only: every other task keeps the suite's no-op publish.
        monkeypatch.setattr(type(bound), "apply_async", _REAL_APPLY_ASYNC)
        worker = _DevWorker(bound)
        task_postrun.connect(worker.record_run, sender=bound, weak=False)
        workers.append(worker)
        return worker

    yield worker_for
    for worker in workers:
        task_postrun.disconnect(worker.record_run, sender=worker.task)
        for thread in worker.threads:
            _release_the_stuck(pg_db, thread)
    business_celery.close()


@pytest.fixture
def dev_worker(dev_worker_for):
    return dev_worker_for(auto_assign_delivery_task)


def _pool_delivery_nobody_can_take(pg_db) -> int:
    """A SCHEDULED pool delivery while the only driver is unavailable. Committed: the worker must find the row free."""
    make_driver(pg_db, available=False, roster=ALL_DAY)
    order = order_at_km(pg_db, 1.0, created_at=datetime.now(UTC))
    delivery_id = order.delivery.id
    pg_db.session.commit()
    return delivery_id


def _assert_the_worker_finished(pg_db, thread, outcome):
    assert not thread.is_alive(), (
        f"the dev worker still runs after {WORKER_WAIT_SECONDS} s: {_lock_waiters(pg_db)} connection(s) wait on a "
        f"lock, idle transaction(s) {_idle_row_lockers(pg_db, _DELIVERY_TABLES)} hold a row lock on "
        f"{', '.join(_DELIVERY_TABLES)}"
    )
    assert "error" not in outcome, outcome


def _assert_nothing_was_written_and_nothing_is_held(pg_db, delivery_id):
    assert _idle_holders_of_a_deliveries_lock(pg_db) == []
    delivery = pg_db.session.get(Delivery, delivery_id)
    assert (delivery.status, delivery.delivery_person_id) == (DeliveryStatus.SCHEDULED, None)


def test_a_no_driver_retry_releases_the_row_lock(dev_worker, pg_db):
    delivery_id = _pool_delivery_nobody_can_take(pg_db)

    thread, outcome = dev_worker.take(delivery_id)

    _assert_the_worker_finished(pg_db, thread, outcome)
    # The worker's run asked for its retry as production does: in 15 minutes, with no exception.
    worker_info = outcome["trace"].info
    assert worker_info.state == states.RETRY
    assert (worker_info.retval.when, worker_info.retval.exc) == (900, None)
    # Dev ran the retries inline until they ran out: the first run and its three retries, the last one giving up.
    assert len(dev_worker.runs) == 4
    assert [retval for state, retval in dev_worker.runs if state == states.SUCCESS] == [EXHAUSTED]
    _assert_nothing_was_written_and_nothing_is_held(pg_db, delivery_id)


class _LookupFailed(Exception):
    pass


class _DriverLookupFailsOnce:
    """`DeliveryPerson` as the task module sees it, except that the first driver lookup raises. The rest is the real
    model."""

    def __init__(self):
        self.lookups = 0

    def __getattr__(self, name):
        return getattr(DeliveryPerson, name)

    @property
    def query(self):
        self.lookups += 1
        if self.lookups == 1:
            raise _LookupFailed("the driver lookup failed")
        return DeliveryPerson.query


def test_an_exception_retry_releases_the_lock_too(dev_worker, pg_db, monkeypatch):
    delivery_id = _pool_delivery_nobody_can_take(pg_db)
    lookup = _DriverLookupFailsOnce()
    monkeypatch.setattr(delivery_tasks, "DeliveryPerson", lookup)

    thread, outcome = dev_worker.take(delivery_id)

    _assert_the_worker_finished(pg_db, thread, outcome)
    # The worker's run retried on the error as production does: after the default delay, carrying the error.
    worker_info = outcome["trace"].info
    assert worker_info.state == states.RETRY
    assert worker_info.retval.when == auto_assign_delivery_task.default_retry_delay == 300
    assert isinstance(worker_info.retval.exc, _LookupFailed)
    # The retry looked again, found nobody, and retried until the retries ran out.
    assert lookup.lookups == len(dev_worker.runs) == 4
    assert [retval for state, retval in dev_worker.runs if state == states.SUCCESS] == [EXHAUSTED]
    _assert_nothing_was_written_and_nothing_is_held(pg_db, delivery_id)


# ---------------------------------------------------------------------------------------------------------------------
# The sibling tasks that retry on any error. Their callees take the row locks.

AT_THE_DOOR_KM = 1.0


def _out_for_delivery(pg_db, delivery_status):
    """An order out for delivery with its driver, its delivery at `delivery_status`. The driver loaded bottles (an open
    bottle session) and the order rides on it, as the bot's accept leaves it. Committed: the worker must find the rows free. Answers (order id, delivery id)."""
    now = datetime.now(UTC)
    driver = make_driver(pg_db, roster=ALL_DAY)
    order = order_at_km(pg_db, AT_THE_DOOR_KM, created_at=now, driver=driver, status=OrderStatus.OUT_FOR_DELIVERY)
    bind(pg_db, open_session(pg_db, driver, now), order)
    order.delivery.status = delivery_status
    ids = (order.id, order.delivery.id)
    pg_db.session.commit()
    return ids


def _fresh(pg_db, model, row_id):
    """The committed row: the test's session forgets what it read before the worker ran."""
    pg_db.session.expire_all()
    return pg_db.session.get(model, row_id)


class _BottleWriteFailed(Exception):
    """Stands for an IntegrityError of the bottle ledger: not a ValidationError, so the confirmation retries."""


def _bottle_write_fails_once(monkeypatch):
    """`OrderService._record_delivery_bottles`, the delivered branch's first write, which runs after the claim UPDATE
    and before the commit, except that its first call raises. The rest is the real method. Answers the order of every
    call."""
    real = OrderService._record_delivery_bottles
    calls = []

    def record_delivery_bottles(self, order, bottles_returned, updated_by):
        calls.append(order.id)
        if len(calls) == 1:
            raise _BottleWriteFailed("the bottle write failed")
        return real(self, order, bottles_returned, updated_by)

    monkeypatch.setattr(OrderService, "_record_delivery_bottles", record_delivery_bottles)
    return calls


def test_a_delivery_confirmation_retry_releases_the_order_lock(dev_worker_for, pg_db, monkeypatch):
    order_id, delivery_id = _out_for_delivery(pg_db, DeliveryStatus.ARRIVED)
    bottle_writes = _bottle_write_fails_once(monkeypatch)
    # The confirmation `POST /api/v1/delivery/driver/complete/<delivery_id>` publishes.
    confirmation = {"photos": [], "signature": None, "notes": "Handed to the customer", "customer_present": True}
    worker = dev_worker_for(process_delivery_confirmation_task)

    thread, outcome = worker.take(delivery_id, confirmation)

    _assert_the_worker_finished(pg_db, thread, outcome)
    # The worker's run retried on the error as production does: after the default delay, carrying the error.
    worker_info = outcome["trace"].info
    assert worker_info.state == states.RETRY
    assert worker_info.retval.when == process_delivery_confirmation_task.default_retry_delay == 180
    assert isinstance(worker_info.retval.exc, _BottleWriteFailed)
    # The retry claimed the order again, booked the bottles and delivered it; it ended first, inside the worker's run.
    assert bottle_writes == [order_id, order_id]
    assert [state for state, _ in worker.runs] == [states.SUCCESS, states.RETRY]
    delivery = _fresh(pg_db, Delivery, delivery_id)
    assert worker.runs[0][1] == {
        "success": True,
        "delivery_id": delivery_id,
        "completed_at": delivery.delivered_at.isoformat(),
    }
    assert (delivery.status, delivery.delivery_notes) == (DeliveryStatus.DELIVERED, "Handed to the customer")
    assert _fresh(pg_db, Order, order_id).status == OrderStatus.DELIVERED
    assert _idle_row_lockers(pg_db, _DELIVERY_TABLES) == []


class _NoticeFailed(Exception):
    pass


def _customer_notice_fails_once(monkeypatch):
    """`NotificationService.send_notification` (the customer's notice, external I/O) with its real signature: records
    every send, and the first one fails."""
    sent = []

    def send_notification(
        self,
        user_id,
        notification_type,
        channels=None,
        template_data=None,
        priority="normal",
        template_override=None,
        campaign_id=None,
        email_headers=None,
        language=None,
    ):
        sent.append((user_id, notification_type, template_data))
        if len(sent) == 1:
            raise _NoticeFailed("the customer notice failed")
        return {}

    monkeypatch.setattr(NotificationService, "send_notification", send_notification)
    return sent


def test_a_delay_report_retry_releases_the_delivery_lock(dev_worker_for, pg_db, monkeypatch):
    order_id, delivery_id = _out_for_delivery(pg_db, DeliveryStatus.IN_TRANSIT)
    notices = _customer_notice_fails_once(monkeypatch)
    new_eta = datetime(2026, 11, 17, 14, 30, tzinfo=UTC)
    # The report `POST /api/v1/delivery/driver/report-issue/<delivery_id>` publishes: its issue type and details.
    details = {"new_eta": new_eta.isoformat(), "reason": "Traffic on the ring road"}
    worker = dev_worker_for(handle_delivery_exception_task)

    thread, outcome = worker.take(delivery_id, "delay", details)

    _assert_the_worker_finished(pg_db, thread, outcome)
    # The worker's run retried on the failed notice as production does: after the default delay, carrying the error.
    worker_info = outcome["trace"].info
    assert worker_info.state == states.RETRY
    assert worker_info.retval.when == handle_delivery_exception_task.default_retry_delay == 180
    assert isinstance(worker_info.retval.exc, _NoticeFailed)
    # The retry wrote the new time, told the customer and ended first, inside the worker's run.
    assert [state for state, _ in worker.runs] == [states.SUCCESS, states.RETRY]
    assert worker.runs[0][1] == {"success": True, "exception_type": "delay", "delivery_id": delivery_id}
    delivery = _fresh(pg_db, Delivery, delivery_id)
    order = _fresh(pg_db, Order, order_id)
    notice = (
        order.user_id,
        "delivery_delayed",
        {
            "order_number": order.order_number,
            "new_eta": "14:30",
            "delay_reason": "Traffic on the ring road",
            "tracking_number": delivery.tracking_number,
        },
    )
    assert notices == [notice, notice]
    assert ensure_utc(delivery.estimated_delivery_time) == new_eta
    assert _idle_row_lockers(pg_db, _DELIVERY_TABLES) == []
