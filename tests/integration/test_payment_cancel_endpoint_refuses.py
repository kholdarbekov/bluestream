"""B2 — `POST /api/v1/payments/<id>/cancel` must REFUSE, never 500, never cancel.

The owner's rule, verbatim (2026-08-24):

    "the payment that is done via click/card is non-returnable. We don't return
     the payment. The reason is we can't undo fiscalization once we submit it.
     Cancelling payment only makes chaos in the payment vs fiscalization. So our
     final business logic is we never ever cancel card / click paid payments. We
     can cancel the order itself, and in that case the payment will settle as
     prepaid customer balance."

and the standing policy (docs/click_payment_policy_rework_plan.md, Phase 4D):
"A payment's life ends where the ORDER resolves — cash at the door, payment of
the link, or order cancellation."

So this endpoint must NOT grow a `PaymentService.cancel_payment`. That would be a
FOURTH way to end a payment independently of its order, which is exactly what B1
spent three rounds collapsing into one expression (`order_is_resolved`). It must
refuse cleanly and say something TRUE about this payment and this order.

Fix round 1 reshaped the refusal into REASON + " " + ADVICE:

  * reason — one true statement about THIS payment. The fiscal-receipt sentence
    is gated on `COMPLETED and rail in {CLICK, CARD}`, because telling a cash
    customer that "a card or Click payment is never refunded" is simply false.
  * advice — what the customer can actually DO, which is a property of the
    ORDER. Never advises cancelling an order that `OrderService.cancel_order`
    would refuse with ConflictError.

Because the test DB carries no translation rows, `get_translation` returns the
key — which makes the response message a space-joined list of the keys the
handler chose. `_english()` maps those back through the canonical seeder, so the
assertions below run against the real customer-facing English sentence.
"""

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.order import Order
from business_app.models.payment import Payment
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus

CANCEL_URL = "/api/v1/payments/{payment_id}/cancel"

# One machine-readable fact for the whole route: payments are never cancellable
# here. The actionable branch is carried by the typed `order_cancellable` flag,
# not by a second code that would be a lie whenever the order is uncancellable.
CODE = "PAYMENT_NOT_CANCELLABLE"

R_PENDING = "api.payments.error.lifecycle.reason_pending"
R_FISCALIZED = "api.payments.error.lifecycle.reason_online_fiscalized"
R_SETTLED = "api.payments.error.lifecycle.reason_already_settled"
R_IN_PROGRESS = "api.payments.error.lifecycle.reason_in_progress"
R_ENDED = "api.payments.error.lifecycle.reason_already_ended"
A_CANCEL_ORDER = "api.payments.error.lifecycle.advice_cancel_order"
A_DELIVERED = "api.payments.error.lifecycle.advice_order_delivered"
A_ALREADY_CANCELLED = "api.payments.error.lifecycle.advice_order_already_cancelled"
A_NOT_CANCELLABLE = "api.payments.error.lifecycle.advice_order_not_cancellable"


def _english(message: str) -> str:
    """Resolve the returned key list into the real English copy.

    Doubles as proof that the handler only ever emits keys the canonical seeder
    actually seeds — a KeyError here is the raw-key-string bug (I-2) caught at
    test time.
    """
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
        payment_id=f"PAY_B2_{status.value}_{user.id}",
        provider_data={"click": {"click_paydoc_id": "20240101000009"}},
        # ARCH-006 `ck_payments_cash_completed_requires_collector`: a COMPLETED
        # cash payment must name its collector, so the door-collected case
        # cannot even be constructed without one.
        collected_by=(
            user.id if method == PaymentMethod.CASH and status == PaymentStatus.COMPLETED else None
        ),
    )
    db.session.add(payment)
    db.session.commit()
    return payment


def _post(client, app, user, payment_id):
    response = client.post(CANCEL_URL.format(payment_id=payment_id), headers=_headers(app, user))
    return response, response.get_json()


@pytest.mark.integration
@pytest.mark.payment
class TestCancelPaymentEndpointRefuses:
    def test_pending_click_payment_is_refused_and_points_at_order_cancellation(
        self, app, client, db, sample_order, sample_user
    ):
        """(a) A PENDING Click payment on a live order. This case used to 500."""
        payment = _seed_payment(db, sample_order, sample_user, PaymentStatus.PENDING)

        response, body = _post(client, app, sample_user, payment.id)

        assert response.status_code == 400, f"expected a clean refusal, got {response.status_code}: {body}"
        assert body["success"] is False
        assert body["message"] == f"{R_PENDING} {A_CANCEL_ORDER}"
        assert body["data"]["error_code"] == CODE
        assert body["data"]["order_cancellable"] is True
        assert body["data"]["order_id"] == sample_order.id
        assert "cancel the order" in _english(body["message"])

        db.session.refresh(payment)
        assert payment.status == PaymentStatus.PENDING, "the refusal must not touch the payment"

    def test_completed_click_payment_is_refused_with_the_fiscal_receipt_reason(
        self, app, client, db, sample_order, sample_user
    ):
        """(b) A COMPLETED Click payment — fiscalized, so never reversible here."""
        sample_order.status = OrderStatus.CONFIRMED
        db.session.commit()
        payment = _seed_payment(db, sample_order, sample_user, PaymentStatus.COMPLETED)

        response, body = _post(client, app, sample_user, payment.id)

        assert response.status_code == 400, f"expected a clean refusal, got {response.status_code}: {body}"
        assert body["message"] == f"{R_FISCALIZED} {A_CANCEL_ORDER}"
        assert body["data"]["error_code"] == CODE
        assert body["message"] != f"{R_PENDING} {A_CANCEL_ORDER}", "the two refusals must not collapse"

        english = _english(body["message"])
        assert "fiscal receipt" in english
        assert "cancel the order" in english

        db.session.refresh(payment)
        assert payment.status == PaymentStatus.COMPLETED, "a paid payment must never be cancelled"

    def test_another_users_payment_id_is_404_not_a_refusal(
        self, app, client, db, sample_order, sample_user, second_sample_user
    ):
        """(c) Ownership preserved: a customer must not be able to probe ids.

        A refusal would confirm the id exists. Only the 404 keeps other people's
        payment ids unenumerable.
        """
        foreign_payment = _seed_payment(db, sample_order, second_sample_user, PaymentStatus.PENDING)

        response, body = _post(client, app, sample_user, foreign_payment.id)

        assert response.status_code == 404
        assert body["message"] == "api.payments.error.payment_not_found"
        assert "data" not in body, "a 404 must leak nothing about the payment, not even its order"

    def test_nonexistent_payment_id_is_404(self, app, client, db, sample_user):
        """(d) Byte-identical to (c) — the two must be indistinguishable."""
        response, body = _post(client, app, sample_user, 99999999)

        assert response.status_code == 404
        assert body["message"] == "api.payments.error.payment_not_found"
        assert "data" not in body


@pytest.mark.integration
@pytest.mark.payment
class TestRefusalCopyIsRailAndStatusAccurate:
    """I-1: the message must be true for THIS payment on THIS order."""

    def test_completed_cash_payment_on_a_delivered_order(
        self, app, client, db, sample_order, sample_user
    ):
        """The headline bad case: COD collected, order DELIVERED, payment COMPLETED.

        The old single message told this customer that "a card or Click payment
        that has gone through is never cancelled or refunded" (wrong rail) and
        then told them to cancel the order — which `OrderService.cancel_order`
        refuses with ConflictError. Advice that cannot be followed.
        """
        sample_order.status = OrderStatus.DELIVERED
        sample_order.payment_method = PaymentMethod.CASH
        sample_order.is_paid = True
        db.session.commit()
        payment = _seed_payment(db, sample_order, sample_user, PaymentStatus.COMPLETED, PaymentMethod.CASH)

        response, body = _post(client, app, sample_user, payment.id)
        english = _english(body["message"])

        assert response.status_code == 400
        assert body["message"] == f"{R_SETTLED} {A_DELIVERED}"
        assert body["data"]["order_cancellable"] is False

        # WRONG-RAIL words only. "refund" left this list with B4a: the reason
        # family is now shared verb-neutral copy served to BOTH this route and
        # POST /payments/refund, so "cannot be cancelled or refunded" is a true,
        # rail-agnostic sentence for a settled cash payment. What must never
        # appear is a claim about a rail this customer did not use — and the
        # exact-message assertion above already pins that the FISCALIZED reason
        # was not chosen.
        for wrong_rail_word in ("card", "click", "fiscal"):
            assert wrong_rail_word not in english, (
                f"a CASH payment must never be told about {wrong_rail_word!r}: {english}"
            )
        assert "cancel the order" not in english, (
            "a DELIVERED order cannot be cancelled; this advice would dead-end in a 400"
        )
        assert "delivered" in english, "say something true instead"

    def test_pending_payment_on_a_delivered_order_is_not_told_to_cancel_it_either(
        self, app, client, db, sample_order, sample_user
    ):
        """Case B (delivered + unpaid) reaches the PENDING branch, not just the
        settled one — so the advice has to be gated on the ORDER, not the status."""
        sample_order.status = OrderStatus.DELIVERED
        sample_order.is_paid = False
        db.session.commit()
        payment = _seed_payment(db, sample_order, sample_user, PaymentStatus.PENDING)

        response, body = _post(client, app, sample_user, payment.id)

        assert response.status_code == 400
        assert body["message"] == f"{R_PENDING} {A_DELIVERED}"
        assert body["data"]["order_cancellable"] is False
        assert "cancel the order" not in _english(body["message"])

    def test_payment_on_an_already_cancelled_order_says_where_the_money_is(
        self, app, client, db, sample_order, sample_user
    ):
        sample_order.status = OrderStatus.CANCELLED
        db.session.commit()
        payment = _seed_payment(db, sample_order, sample_user, PaymentStatus.COMPLETED)

        response, body = _post(client, app, sample_user, payment.id)
        english = _english(body["message"])

        assert body["message"] == f"{R_FISCALIZED} {A_ALREADY_CANCELLED}"
        assert body["data"]["order_cancellable"] is False
        assert "cancel the order" not in english
        assert "prepaid balance" in english, "the customer must be told the money is not lost"

    @pytest.mark.parametrize(
        "payment_status,expected_reason",
        [
            (PaymentStatus.FAILED, R_ENDED),
            (PaymentStatus.CANCELLED, R_ENDED),
            (PaymentStatus.REFUNDED, R_ENDED),
            (PaymentStatus.PARTIALLY_REFUNDED, R_ENDED),
            (PaymentStatus.PROCESSING, R_IN_PROGRESS),
            (PaymentStatus.PARTIALLY_PAID, R_IN_PROGRESS),
        ],
    )
    def test_non_completed_statuses_get_status_accurate_copy(
        self, app, client, db, sample_order, sample_user, payment_status, expected_reason
    ):
        """No money paragraph for a payment that never took money, and a
        PROCESSING payment has not "gone through"."""
        sample_order.status = OrderStatus.CONFIRMED
        db.session.commit()
        payment = _seed_payment(db, sample_order, sample_user, payment_status)

        response, body = _post(client, app, sample_user, payment.id)
        english = _english(body["message"])

        assert response.status_code == 400
        assert body["message"] == f"{expected_reason} {A_CANCEL_ORDER}"
        assert "fiscal receipt" not in english, "only a COMPLETED card/Click payment has one filed"
        assert "gone through" not in english

    def test_only_a_completed_online_payment_gets_the_fiscal_sentence(
        self, app, client, db, sample_order, sample_user
    ):
        """The gate is COMPLETED *and* an online rail — both halves load-bearing."""
        sample_order.status = OrderStatus.CONFIRMED
        db.session.commit()
        payment = _seed_payment(db, sample_order, sample_user, PaymentStatus.COMPLETED, PaymentMethod.CARD)

        _response, body = _post(client, app, sample_user, payment.id)

        assert body["message"].startswith(R_FISCALIZED)
        assert "fiscal receipt" in _english(body["message"])

    def test_an_orderless_payment_omits_order_id_rather_than_sending_null(
        self, app, client, db, sample_user
    ):
        """M1: `Payment.order_id` is nullable and `exclude_none` does not reach
        inside the plain `data` dict. Latent today, but a null `order_id` next to
        "cancel the order instead" would be advice with no order to act on."""
        payment = _seed_payment(db, None, sample_user, PaymentStatus.PENDING)

        response, body = _post(client, app, sample_user, payment.id)

        assert response.status_code == 400
        assert "order_id" not in body["data"], f"null order_id must be omitted: {body['data']}"
        assert body["data"]["order_cancellable"] is False
        assert body["message"] == R_PENDING, "no order means no advice about an order"


@pytest.mark.integration
@pytest.mark.payment
class TestOrderCancellabilityAgreesWithTheRealCancelPath:
    """The advice must agree with what `cancel_order` ACTUALLY does.

    `_order_cancel_advice_key` asks `shared/status_transitions.py` — the same
    SSOT `OrderService.update_order_status` asks — rather than re-enumerating a
    set of statuses. This drives the REAL service for every OrderStatus and
    fails the moment the answers diverge.

    It earned its keep immediately: an enumerated {DELIVERED, CANCELLED} mirror
    of `cancel_order`'s own explicit guard (order_service.py:1054) passed that
    guard for RETURNED and then died deeper with "Cannot change status from
    returned to cancelled" — so a RETURNED order would still have been told to
    "cancel the order instead" and sent into a 400.
    """

    @pytest.mark.parametrize("order_status", list(OrderStatus))
    def test_advice_agrees_with_order_service_for_every_status(
        self, app, db, sample_user, order_status
    ):
        from business_app.api.payments import _order_cancel_advice_key, A_CANCEL_ORDER_KEY
        from business_app.services.order_service import OrderService
        from business_app.utils.exceptions import ConflictError, ValidationError

        order = Order(
            user_id=sample_user.id,
            order_number=f"ORD-MIRROR-{order_status.value}",
            status=order_status,
            subtotal=15000,
            delivery_fee=3000,
            discount_amount=0,
            loyalty_discount=0,
            total_amount=18000,
        )
        db.session.add(order)
        db.session.commit()

        we_say_cancellable = _order_cancel_advice_key(order) == A_CANCEL_ORDER_KEY

        try:
            OrderService().cancel_order(order.id, sample_user.id, process_payment_refund=False)
            service_allows = True
        except (ConflictError, ValidationError):
            # BOTH count as a refusal. `cancel_order`'s own guard raises
            # ConflictError; the transition table underneath raises
            # ValidationError. A customer sent into either gets a 400.
            service_allows = False

        assert we_say_cancellable is service_allows, (
            f"{order_status.value}: the refusal advises cancellable={we_say_cancellable} "
            f"but the real cancel path allows={service_allows}"
        )

    def test_a_returned_order_is_told_something_true_not_to_cancel_it(
        self, app, client, db, sample_order, sample_user
    ):
        """The status the enumerated mirror got wrong, pinned end to end."""
        sample_order.status = OrderStatus.RETURNED
        db.session.commit()
        payment = _seed_payment(db, sample_order, sample_user, PaymentStatus.PENDING)

        _response, body = _post(client, app, sample_user, payment.id)

        assert body["message"] == f"{R_PENDING} {A_NOT_CANCELLABLE}"
        assert body["data"]["order_cancellable"] is False
        assert "cancel the order" not in _english(body["message"])


class TestNoCancelPaymentMethodWasAdded:
    """The fix must NOT be to implement the missing method.

    `PaymentService.cancel_payment` would be a fourth, order-independent way to
    end a payment — the exact thing B1 collapsed into `order_is_resolved`.
    """

    def test_payment_service_has_no_cancel_payment_method(self):
        from business_app.services.payment_service import PaymentService

        assert not hasattr(PaymentService, "cancel_payment"), (
            "a payment's life must end where its ORDER resolves; see "
            "docs/click_payment_policy_rework_plan.md Phase 4D"
        )

    def test_the_api_handler_does_not_call_the_payment_service(self):
        import inspect

        from business_app.api import payments as payments_api

        source = inspect.getsource(payments_api.cancel_payment)
        assert "get_payment_service()" not in source, (
            "the refusal must be decided in the handler, not delegated to a "
            "payment-ending service method"
        )


class TestRefusalCopyIsSeededByTheCanonicalSeeder:
    """I-2: the keys must live where the audit and a fresh bootstrap can see them.

    `scripts/audit_translation_keys.py` reads seeded keys from five named files
    and maps the `api` prefix to `scripts/seed_backend_translations.py`. A key
    referenced from business_app but seeded only by a one-off script is reported
    as an unseeded reference, and any environment bootstrapped from the canonical
    seeder serves the RAW KEY STRING as the customer-facing message.

    There is no companion one-off script: `scripts/` is not mounted into the
    business_app container, so a script piped in over stdin cannot import the
    canonical dict — and restating the copy would be the duplication this whole
    workstream exists to remove. The canonical seeder is idempotent and is
    already the documented way to ship backend copy.
    """

    ALL_KEYS = (R_PENDING, R_FISCALIZED, R_SETTLED, R_IN_PROGRESS, R_ENDED,
                A_CANCEL_ORDER, A_DELIVERED, A_ALREADY_CANCELLED, A_NOT_CANCELLABLE)

    # `only_pending_cancellable` was actively FALSE — it promised that pending
    # payments are cancellable, and none are. The other two had zero readers once
    # the endpoint stopped attempting a cancel at all.
    RETIRED_KEYS = (
        "api.payments.cancelled",
        "api.payments.error.cancel_failed",
        "api.payments.error.only_pending_cancellable",
    )

    def test_every_key_the_handler_serves_is_in_backend_translations(self):
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        for key in self.ALL_KEYS:
            assert key in BACKEND_TRANSLATIONS, f"{key} is invisible to audit_translation_keys.py"
            assert set(BACKEND_TRANSLATIONS[key]) == {"en", "uz", "ru"}, key
            for language, text in BACKEND_TRANSLATIONS[key].items():
                assert text.strip(), f"{key}/{language} is empty"

    def test_the_retired_keys_are_gone_from_the_canonical_seeder(self):
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        for key in self.RETIRED_KEYS:
            assert key not in BACKEND_TRANSLATIONS, f"{key} is dead but still re-asserted on every reseed"

    # Both halves of the marking-code pool-short refusal. They set the precedent
    # this whole finding is about: seeded from a one-off script, therefore
    # reported as unseeded references by audit_translation_keys.py, which maps
    # BOTH the `api` and `telegram` prefixes to the canonical seeder.
    MARKING_CODE_KEYS = (
        "api.payments.marking_codes_unavailable",
        "telegram.payment.marking_codes_unavailable",
    )

    def test_the_marking_codes_keys_ship_with_the_canonical_seeder(self):
        """One key, one home — and the one-off script that owned them is gone.

        Its API key moved in fix round 1; its bot key moved in round 2, which was
        the last reason the file existed.
        """
        import importlib

        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        for key in self.MARKING_CODE_KEYS:
            assert key in BACKEND_TRANSLATIONS, key
            assert set(BACKEND_TRANSLATIONS[key]) == {"en", "uz", "ru"}, key

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("scripts.seed_marking_codes_pool_short_translations")

    def test_bot_keys_are_seeded_under_the_category_the_bot_actually_READS(self):
        """The assertion whose absence let a customer-facing key ship unreachable.

        `telegram.payment.marking_codes_unavailable` was seeded under
        `category="telegram_bot"`, but `telegram_bot/i18n.py::load_translations`
        selects ONLY `category = 'telegram'` (and the staff bot takes
        `'staff_bot' OR key LIKE 'staff.%'`), so the row was invisible to both.
        `telegram_bot/handlers/payments.py:372` then fell into i18n's missing-key
        path and rendered the humanised English "Marking codes unavailable" to
        every customer in every language — on the screen whose entire job is to
        explain that their order stays on Cash on Delivery. "The key exists" was
        true the whole time; it was never the question.

        The expected category is READ OUT OF THE BOT'S OWN QUERY rather than
        written here, so this cannot quietly stop matching if the bot's filter
        changes.
        """
        import re
        from pathlib import Path

        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for

        i18n_source = (Path(__file__).resolve().parents[2] / "telegram_bot" / "i18n.py").read_text()
        categories = re.findall(r"category\s*=\s*'([a-z_]+)'", i18n_source)
        assert categories, "could not find the bot's category filter in telegram_bot/i18n.py"
        bot_category = categories[0]

        # The key this finding is about, named explicitly so a regression points
        # straight at it rather than at "some telegram.* key".
        offender = "telegram.payment.marking_codes_unavailable"
        assert offender in BACKEND_TRANSLATIONS, (
            "seeding it anywhere else means picking the category by hand, which "
            "is how it ended up under 'telegram_bot' and unreachable"
        )
        assert _category_for(offender) == bot_category

        # And the invariant, for every bot-namespace key the canonical seeder owns.
        for key in BACKEND_TRANSLATIONS:
            if not key.startswith("telegram."):
                continue
            assert _category_for(key) == bot_category, (
                f"{key} would be seeded under {_category_for(key)!r}, which "
                f"telegram_bot/i18n.py never loads (it reads {bot_category!r})"
            )

    def test_the_bot_copy_survived_the_move_verbatim(self):
        """A move that silently rewrote the copy would be a different bug."""
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        copy = BACKEND_TRANSLATIONS["telegram.payment.marking_codes_unavailable"]
        for language in ("en", "uz", "ru"):
            assert "\n\n" in copy[language], f"{language} lost its paragraph break"
        assert "Cash on Delivery" in copy["en"]
        assert "Cash on Delivery" in copy["uz"]
        assert "наличными" in copy["ru"]

    def test_uzbek_copy_uses_straight_apostrophes_only(self):
        """This repo keeps copy ASCII-safe; a curly apostrophe elsewhere doubled
        the Eskiz SMS bill by pushing text out of GSM-7."""
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        for key in self.ALL_KEYS:
            assert "\u2019" not in BACKEND_TRANSLATIONS[key]["uz"], f"{key}/uz has a curly apostrophe"

    def test_only_the_advice_keys_talk_about_the_order(self):
        """Reason keys describe the PAYMENT; advice keys describe the ORDER. If a
        reason key started naming the order, the composition would contradict
        itself — e.g. a reason saying "cancel the order" glued to advice saying
        the order cannot be cancelled."""
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        for key in self.ALL_KEYS:
            if ".reason_" in key:
                assert "order" not in BACKEND_TRANSLATIONS[key]["en"].lower(), f"{key} is a reason, not advice"

    def test_only_the_fiscalized_reason_mentions_the_fiscal_receipt(self):
        """The sentence that must never reach a cash customer exists in exactly
        one key, so gating that key gates the claim."""
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        carriers = [k for k in self.ALL_KEYS if "fiscal" in BACKEND_TRANSLATIONS[k]["en"].lower()]
        assert carriers == [R_FISCALIZED], carriers
