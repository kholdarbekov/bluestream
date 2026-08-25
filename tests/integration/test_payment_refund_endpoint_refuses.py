"""B4a — ``POST /api/v1/payments/refund`` must REFUSE, and say something true.

The owner's rule (2026-08-24): a card/Click payment is never returned, because
the fiscal receipt filed for it cannot be undone. Cancel the ORDER instead; the
money settles as prepaid customer balance.

WHY THE ROUTE IS KEPT RATHER THAN DELETED, when the ADMIN refund route beside it
is deleted outright: this is a public ``jwt_required`` surface with no
first-party caller but possible in-flight or third-party clients, and it appears
in ``tests/contract/snapshots/api_routes.json``. For them a translated refusal
that names the real action beats a 404. The admin surface has only first-party
callers, admin_ui provably never called it, and a refusing admin route would be
exactly the escape hatch the owner said to remove.

IT SHARES B2's VOCABULARY RATHER THAN CLONING IT. Both routes answer the same
two questions — one true statement about the PAYMENT, one about what the customer
can DO with the ORDER — so they use one verb-neutral
``api.payments.error.lifecycle.*`` family. A second ``refund.*`` family would put
two copies of the prepaid-balance promise into three languages, which is the
exact drift the project rule forbids.

Because the test DB carries no translation rows, ``get_translation`` returns the
key, so the response message is a space-joined list of the keys the handler
chose. ``_english()`` maps them back through the canonical seeder — which doubles
as proof that the handler only emits keys the seeder actually seeds.
"""

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.payment import Payment
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus

REFUND_URL = "/api/v1/payments/refund"

# A client that asked for a REFUND must not be answered about cancellability.
CODE = "PAYMENT_NOT_REFUNDABLE"

R_PENDING = "api.payments.error.lifecycle.reason_pending"
R_FISCALIZED = "api.payments.error.lifecycle.reason_online_fiscalized"
R_SETTLED = "api.payments.error.lifecycle.reason_already_settled"
R_ENDED = "api.payments.error.lifecycle.reason_already_ended"
A_CANCEL_ORDER = "api.payments.error.lifecycle.advice_cancel_order"
A_DELIVERED = "api.payments.error.lifecycle.advice_order_delivered"


def _english(message: str) -> str:
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

    return " ".join(BACKEND_TRANSLATIONS[key]["en"] for key in message.split(" ")).lower()


def _headers(app, user):
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _seed_payment(db, order, user, status, method=PaymentMethod.CLICK):
    payment = Payment(
        order_id=order.id if order is not None else None,
        user_id=user.id,
        payment_method=method,
        amount=order.total_amount if order is not None else 18000,
        currency="UZS",
        status=status,
        payment_id=f"PAY_B4_{status.value}_{user.id}",
        provider_data={"click": {"click_paydoc_id": "20240101000009"}},
        collected_by=(user.id if method == PaymentMethod.CASH and status == PaymentStatus.COMPLETED else None),
    )
    db.session.add(payment)
    db.session.commit()
    return payment


def _post(client, app, user, payment_id, **extra):
    body = {"payment_id": payment_id}
    body.update(extra)
    response = client.post(REFUND_URL, json=body, headers=_headers(app, user))
    return response, response.get_json()


@pytest.mark.integration
@pytest.mark.payment
class TestRefundEndpointRefuses:
    def test_completed_click_payment_is_refused_with_the_fiscal_receipt_reason(
        self, app, client, db, sample_order, sample_user
    ):
        sample_order.status = OrderStatus.CONFIRMED
        db.session.commit()
        payment = _seed_payment(db, sample_order, sample_user, PaymentStatus.COMPLETED)

        response, body = _post(client, app, sample_user, payment.id)

        assert response.status_code == 400, f"expected a clean refusal, got {response.status_code}: {body}"
        assert body["success"] is False
        assert body["message"] == f"{R_FISCALIZED} {A_CANCEL_ORDER}"
        assert body["data"]["error_code"] == CODE
        assert body["data"]["order_cancellable"] is True
        assert body["data"]["order_id"] == sample_order.id

        english = _english(body["message"])
        assert "fiscal receipt" in english
        assert "cancel the order" in english
        assert "prepaid balance" in english

        db.session.refresh(payment)
        assert payment.status is PaymentStatus.COMPLETED, "the refusal must not touch the payment"

    def test_the_amount_and_reason_fields_are_ignored_not_honoured(
        self, app, client, db, sample_order, sample_user
    ):
        """The old route always passed ``payment.amount``, so there was never a
        partial-refund concept to preserve. A client sending one still gets the
        same refusal rather than a partial reversal."""
        sample_order.status = OrderStatus.CONFIRMED
        db.session.commit()
        payment = _seed_payment(db, sample_order, sample_user, PaymentStatus.COMPLETED)

        response, body = _post(client, app, sample_user, payment.id, amount=1, reason="please")

        assert response.status_code == 400
        assert body["data"]["error_code"] == CODE
        db.session.refresh(payment)
        assert payment.status is PaymentStatus.COMPLETED

    def test_a_delivered_order_is_not_told_to_cancel_it(self, app, client, db, sample_order, sample_user):
        """Advice is a property of the ORDER, so a refund refusal inherits B2's
        rule: never advise a cancellation ``cancel_order`` would refuse."""
        sample_order.status = OrderStatus.DELIVERED
        sample_order.payment_method = PaymentMethod.CASH
        sample_order.is_paid = True
        db.session.commit()
        payment = _seed_payment(db, sample_order, sample_user, PaymentStatus.COMPLETED, PaymentMethod.CASH)

        response, body = _post(client, app, sample_user, payment.id)

        assert response.status_code == 400
        assert body["message"] == f"{R_SETTLED} {A_DELIVERED}"
        assert body["data"]["order_cancellable"] is False

        english = _english(body["message"])
        for wrong_rail_word in ("card", "click", "fiscal"):
            assert wrong_rail_word not in english, (
                f"a CASH payment must never be told about {wrong_rail_word!r}: {english}"
            )
        assert "cancel the order" not in english

    def test_a_pending_payment_is_refused_too(self, app, client, db, sample_order, sample_user):
        payment = _seed_payment(db, sample_order, sample_user, PaymentStatus.PENDING)

        response, body = _post(client, app, sample_user, payment.id)

        assert response.status_code == 400
        assert body["message"] == f"{R_PENDING} {A_CANCEL_ORDER}"
        assert body["data"]["error_code"] == CODE

    def test_an_already_ended_payment_is_refused_too(self, app, client, db, sample_order, sample_user):
        payment = _seed_payment(db, sample_order, sample_user, PaymentStatus.FAILED)

        response, body = _post(client, app, sample_user, payment.id)

        assert response.status_code == 400
        assert body["message"].startswith(R_ENDED)

    def test_another_users_payment_id_is_404_not_a_refusal(
        self, app, client, db, sample_order, sample_user, second_sample_user
    ):
        """Ownership IS the lookup. A refusal would confirm the id exists."""
        foreign = _seed_payment(db, sample_order, second_sample_user, PaymentStatus.PENDING)

        response, body = _post(client, app, sample_user, foreign.id)

        assert response.status_code == 404
        assert body["message"] == "api.payments.error.payment_not_found"
        assert "data" not in body

    def test_nonexistent_payment_id_is_404(self, app, client, db, sample_user):
        response, body = _post(client, app, sample_user, 99999999)

        assert response.status_code == 404
        assert body["message"] == "api.payments.error.payment_not_found"
        assert "data" not in body


@pytest.mark.integration
@pytest.mark.payment
class TestTheRetiredTranslationKeysAreGone:
    """Zero readers after this change. Left in the seeder they would be seeded
    into three languages forever and read as live copy by the next maintainer."""

    @pytest.mark.parametrize(
        "key",
        [
            "api.payments.refund_requested",
            "api.payments.refund_reason_customer_request",
            "api.payments.error.only_completed_refundable",
            "api.payments.error.refund_failed",
        ],
    )
    def test_key_is_deleted_from_the_seeder(self, key):
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        assert key not in BACKEND_TRANSLATIONS

    @pytest.mark.parametrize(
        "key",
        [
            R_PENDING,
            R_FISCALIZED,
            R_SETTLED,
            "api.payments.error.lifecycle.reason_in_progress",
            R_ENDED,
            A_CANCEL_ORDER,
            A_DELIVERED,
            "api.payments.error.lifecycle.advice_order_already_cancelled",
            "api.payments.error.lifecycle.advice_order_not_cancellable",
        ],
    )
    def test_the_shared_lifecycle_family_is_seeded_in_all_three_languages(self, key):
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        assert key in BACKEND_TRANSLATIONS, "renamed from api.payments.error.cancel.*"
        assert set(BACKEND_TRANSLATIONS[key]) >= {"en", "uz", "ru"}

    @pytest.mark.parametrize(
        "key",
        [
            "api.payments.error.cancel.reason_pending",
            "api.payments.error.cancel.reason_online_fiscalized",
            "api.payments.error.cancel.advice_cancel_order",
        ],
    )
    def test_the_old_cancel_scoped_names_are_gone(self, key):
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        assert key not in BACKEND_TRANSLATIONS

    @pytest.mark.parametrize("key", [R_PENDING, R_SETTLED, "api.payments.error.lifecycle.reason_in_progress", R_ENDED])
    def test_the_shared_reasons_are_verb_neutral(self, key):
        """They are served to a cancel request AND a refund request, so a
        sentence that only mentions cancelling answers the wrong question half
        the time."""
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        english = BACKEND_TRANSLATIONS[key]["en"].lower()
        assert "refund" in english or "return" in english, english
