"""Crash-window semantics of the Click webhook route (two-phase claim + classification).

Task 2 of the Click pipeline hardening. Asserts that:

- A transient failure (exception before the first durable commit) rolls the
  payment back, releases the idempotency claim, caches nothing, and answers
  503 with a ``Retry-After`` header so the gateway retries.
- A terminal success is cached and replayed from cache without reprocessing.
- An in-flight duplicate (claim present, no cached response yet) answers 503,
  never a fake success — the retry after the claim expires reprocesses.
- A failure AFTER the first durable commit still answers 503, and the retry
  hits the idempotent already-paid protocol ack (``error == -4``) now cached.

Fixtures ``matrix_app`` / ``matrix_client`` / ``no_fiscalization`` /
``order_with_address`` / ``sample_address`` live in
``tests/integration/conftest.py`` (shared with ``test_payment_matrix.py``).
"""

from business_app import db
from business_app.models.order import Order
from business_app.models.payment import Payment, PaymentTransaction
from shared.enums import OrderStatus, PaymentStatus

from tests.integration.fake_gateways import (
    TEST_CLICK_SHOP_SECRET_KEY,
    make_click_webhook_form,
)
from tests.integration.test_payment_matrix import _seed_click_payment  # reuse existing helper


WEBHOOK_URL = "/api/v1/payments/webhook/click"


def _guard_redis():
    """Return the exact Redis client the webhook route's idempotency guard uses.

    The route builds the guard from ``get_payment_service().redis_client``, which
    is bound to ``current_app.config['REDIS_URL']``. Under ``pytest -n auto`` that
    DB is remapped per worker, so the module-global ``business_app.redis_client``
    (fixed to the env DB) would point at a DIFFERENT DB than the route on any
    non-gw0 worker. Resolving through the payment service keeps the test and the
    route on the same DB regardless of worker.
    """
    from business_app.utils.service_factory import get_payment_service

    return get_payment_service().redis_client


def _claim_key(click_trans_id: str, action: str = "1") -> str:
    return f"bs:webhook:dedup:click:{click_trans_id}:{action}"


def _post_complete(client, order, payment, click_trans_id="900001", error=0):
    form = make_click_webhook_form(
        action="1",
        click_trans_id=click_trans_id,
        merchant_trans_id=order.order_number,
        amount=str(int(order.total_amount)),
        secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        merchant_prepare_id=str(payment.id),
        error=error,
    )
    return client.post(WEBHOOK_URL, data=form, content_type="application/x-www-form-urlencoded")


class TestClickCrashWindows:
    def test_transient_failure_returns_503_and_releases_claim(
        self, matrix_client, matrix_app, db, order_with_address, no_fiscalization, monkeypatch
    ):
        order = order_with_address
        payment = _seed_click_payment(db, order)

        from business_app.services.order_service import OrderService

        def boom(*args, **kwargs):
            raise RuntimeError("simulated DB failure before first commit")

        monkeypatch.setattr(OrderService, "update_order_status", boom)
        resp = _post_complete(matrix_client, order, payment, click_trans_id="900001")
        assert resp.status_code == 503
        assert resp.headers.get("Retry-After") is not None

        db.session.expire_all()
        payment = Payment.query.get(payment.id)
        assert payment.status == PaymentStatus.PENDING  # fully rolled back
        # Claim released + nothing cached: the retry must reprocess.
        guard_redis = _guard_redis()
        assert guard_redis.get(_claim_key("900001")) is None
        assert guard_redis.get(_claim_key("900001") + ":response") is None

        # Gateway retry (monkeypatch removed) completes cleanly.
        monkeypatch.undo()
        resp2 = _post_complete(matrix_client, order, payment, click_trans_id="900001")
        assert resp2.status_code == 200
        assert resp2.get_json()["error"] == 0
        db.session.expire_all()
        assert Payment.query.get(payment.id).status == PaymentStatus.COMPLETED
        assert Order.query.get(order.id).status == OrderStatus.CONFIRMED

    def test_terminal_success_cached_and_replayed_without_reprocessing(
        self, matrix_client, matrix_app, db, order_with_address, no_fiscalization
    ):
        order = order_with_address
        payment = _seed_click_payment(db, order)
        resp = _post_complete(matrix_client, order, payment, click_trans_id="900002")
        assert resp.status_code == 200
        body_first = resp.get_json()
        assert body_first["error"] == 0
        txn_count = PaymentTransaction.query.filter_by(payment_id=payment.id).count()

        resp2 = _post_complete(matrix_client, order, payment, click_trans_id="900002")
        assert resp2.status_code == 200
        assert resp2.get_json() == body_first  # replayed from cache
        assert PaymentTransaction.query.filter_by(payment_id=payment.id).count() == txn_count

    def test_inflight_duplicate_returns_503_not_fake_success(
        self, matrix_client, matrix_app, db, order_with_address, no_fiscalization
    ):
        order = order_with_address
        payment = _seed_click_payment(db, order)
        # Simulate a crashed first attempt: claim exists, no cached response.
        guard_redis = _guard_redis()
        guard_redis.set(_claim_key("900003"), "1", ex=90)

        resp = _post_complete(matrix_client, order, payment, click_trans_id="900003")
        assert resp.status_code == 503
        assert resp.get_json()["error"] == -1
        db.session.expire_all()
        assert Payment.query.get(payment.id).status == PaymentStatus.PENDING

        # Claim expiry (simulated by deletion) -> retry reprocesses fully.
        guard_redis.delete(_claim_key("900003"))
        resp2 = _post_complete(matrix_client, order, payment, click_trans_id="900003")
        assert resp2.status_code == 200
        assert resp2.get_json()["error"] == 0
        db.session.expire_all()
        assert Payment.query.get(payment.id).status == PaymentStatus.COMPLETED

    def test_post_commit_failure_returns_503_then_retry_hits_already_paid(
        self, matrix_client, matrix_app, db, order_with_address, no_fiscalization, monkeypatch
    ):
        order = order_with_address
        payment = _seed_click_payment(db, order)

        from business_app.services.payment_service import PaymentService

        def boom(self, payment_id):
            raise RuntimeError("simulated failure after first durable commit")

        monkeypatch.setattr(PaymentService, "queue_click_fiscalization", boom)
        resp = _post_complete(matrix_client, order, payment, click_trans_id="900004")
        assert resp.status_code == 503
        db.session.expire_all()
        # First durable commit already persisted the money state.
        assert Payment.query.get(payment.id).status == PaymentStatus.COMPLETED

        monkeypatch.undo()
        resp2 = _post_complete(matrix_client, order, payment, click_trans_id="900004")
        assert resp2.status_code == 200
        assert resp2.get_json()["error"] == -4  # protocol-correct idempotent ack, now cached
