"""What step 4 retired stays retired (spec 2026-10-27 §14).

The customer's Pay and Cancel are the published `customer_actions` (§4.2, §5.1), so the old per-field answers
(`can_customer_cancel`, `customer_cancel_via`, `is_payable`, `payable_payment_link`) leave the customer payloads, and
no client reads them. The admin Orders drawer still opens the stored link only while it is payable
(`payable_payment_link`, B3), so the admin payloads keep it. The routes nothing calls any more are gone:
`POST /payments/create` (Pay is `retry-payment`), the activity log, and the per-item subscription writes (the
website and the bot save the whole list with `PUT /subscriptions/<id>/items`).
"""

import re
from decimal import Decimal
from pathlib import Path

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.order import Order
from business_app.models.payment import Payment
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus

ROOT = Path(__file__).resolve().parents[2]
RETIRED = ("can_customer_cancel", "customer_cancel_via", "is_payable", "payable_payment_link")
CLIENTS = ["telegram_bot/**/*.py", "business_app/static/js/**/*.js", "business_app/templates/frontend/**/*.html"]
CLICK_LINK = "https://my.click.uz/services/pay?id=1&t=RETIRED"


def _word(name: str) -> re.Pattern:
    """`name` as a whole identifier, so `order_is_payable_online` is not read as `is_payable`."""
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")


def _client_files():
    for pattern in CLIENTS:
        yield from sorted(ROOT.glob(pattern))


def test_no_client_reads_a_retired_field():
    """A source scan, docstrings and comments included: a comment naming a retired field is how the next reader
    puts it back."""
    found = []
    for path in _client_files():
        text = path.read_text(encoding="utf-8")
        found += [f"{path.relative_to(ROOT)}: {name}" for name in RETIRED if _word(name).search(text)]
    assert found == []


def _click_order(db, user, *, order_number, status=OrderStatus.PENDING):
    order = Order(
        user_id=user.id,
        order_number=order_number,
        status=status,
        subtotal=Decimal("15000.00"),
        delivery_fee=Decimal("3000.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("18000.00"),
        payment_method=PaymentMethod.CLICK,
        is_paid=False,
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        Payment(
            order_id=order.id,
            user_id=user.id,
            payment_method=PaymentMethod.CLICK,
            amount=order.total_amount,
            currency="UZS",
            status=PaymentStatus.PENDING,
            payment_id=f"pay-{order_number}",
            payment_link=CLICK_LINK,
        )
    )
    db.session.commit()
    return order


def _headers(app, user, **claims):
    with app.app_context():
        token = create_access_token(identity=str(user.id), additional_claims=claims or None)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def test_customer_order_payloads_publish_customer_actions_only(client, app, db, sample_user):
    """The bot's and the website's two reads: the My orders list and the order detail."""
    order = _click_order(db, sample_user, order_number="RET-C-001")
    headers = _headers(app, sample_user)

    listed_resp = client.get("/api/v1/orders/", headers=headers)
    detail_resp = client.get(f"/api/v1/orders/{order.id}", headers=headers)
    assert (listed_resp.status_code, detail_resp.status_code) == (200, 200)
    listed = next(row for row in listed_resp.get_json()["data"]["orders"] if row["id"] == order.id)
    detail = detail_resp.get_json()["data"]["order"]

    for payload in (listed, detail):
        assert not set(RETIRED) & set(payload)
        assert not set(RETIRED) & set(payload.get("payment_info") or {})
        assert set(payload["customer_actions"]) == {"pay", "cancel", "confirm", "reorder"}
    # The payment row itself is still there; only its retired answers are gone.
    assert detail["payment_info"]["payment_link"] == CLICK_LINK


def test_the_retry_payment_answer_carries_no_retired_field(client, app, db, sample_user, monkeypatch):
    """`retry-payment` answers with the payment it settled on. Its `payment` is the customer serializer's too."""
    from business_app.services.payment_service import PaymentService

    order = _click_order(db, sample_user, order_number="RET-C-002")
    monkeypatch.setattr(
        PaymentService, "create_payment_link", lambda self, payment_id, **_: {"payment_url": CLICK_LINK}
    )
    shown = client.get(f"/api/v1/orders/{order.id}", headers=_headers(app, sample_user)).get_json()
    pay = shown["data"]["order"]["customer_actions"]["pay"]
    assert pay["allowed"] is True, pay

    resp = client.post(
        f"/api/v1/orders/{order.id}/retry-payment",
        headers=_headers(app, sample_user),
        json={"expected_amount": pay["expected_amount"], "channel": "web"},
    )

    assert resp.status_code == 200, resp.get_json()
    assert not set(RETIRED) & set(resp.get_json()["data"]["payment"])


def test_the_cancel_refusal_carries_no_retired_field(client, app, db, sample_user):
    """The 400 keeps its `error_code` and `reason_code`, plus the published `refusal_copy` (LDfix: one copy choice
    for the bot and the website); `can_customer_cancel` went with the field."""
    order = _click_order(db, sample_user, order_number="RET-C-003", status=OrderStatus.DELIVERED)
    shown = client.get(f"/api/v1/orders/{order.id}", headers=_headers(app, sample_user)).get_json()
    assert shown["data"]["order"]["customer_actions"]["cancel"]["allowed"] is False

    resp = client.post(
        f"/api/v1/orders/{order.id}/cancel", headers=_headers(app, sample_user), json={"expected_via": None}
    )

    assert resp.status_code == 400, resp.get_json()
    data = resp.get_json()["data"]
    assert "can_customer_cancel" not in data
    assert set(data) == {"error_code", "reason_code", "refusal_copy"}


def test_the_admin_drawer_still_gets_the_live_link(client, app, db, sample_user, admin_user):
    """B3: the admin Orders list row and detail payload keep `payable_payment_link` (role-claim token)."""
    order = _click_order(db, sample_user, order_number="RET-A-001")
    headers = _headers(app, admin_user, role="admin")

    listed = client.get("/api/v1/admin/orders", headers=headers)
    detail = client.get(f"/api/v1/admin/orders/{order.id}", headers=headers)

    assert (listed.status_code, detail.status_code) == (200, 200)
    row = next(r for r in listed.get_json()["data"]["items"] if r["order_number"] == "RET-A-001")
    for payload in (row, detail.get_json()["data"]["order"]):
        assert (payload["is_payable"], payload["payable_payment_link"]) == (True, CLICK_LINK)


def test_the_retired_routes_are_gone(app):
    rules = {(r.rule, m) for r in app.url_map.iter_rules() for m in r.methods}
    for gone in [
        ("/api/v1/payments/create", "POST"),
        ("/api/v1/subscriptions/<int:subscription_id>/logs", "GET"),
        ("/api/v1/subscriptions/<int:subscription_id>/items", "POST"),
        ("/api/v1/subscriptions/<int:subscription_id>/items/<int:item_id>", "PUT"),
        ("/api/v1/subscriptions/<int:subscription_id>/items/<int:item_id>", "DELETE"),
    ]:
        assert gone not in rules, gone
    # What stays: the website's and the bot's one item write, and the admin's per-item routes.
    assert ("/api/v1/subscriptions/<int:subscription_id>/items", "PUT") in rules
    assert ("/api/v1/admin/subscriptions/<int:subscription_id>/items/<int:item_id>", "DELETE") in rules


def test_the_bot_has_no_payment_create_or_activity_log_call():
    client_source = (ROOT / "telegram_bot" / "api_client.py").read_text(encoding="utf-8")
    for name in ("create_payment", "get_subscription_logs"):
        assert not _word(name).search(client_source), name
    for path in sorted((ROOT / "telegram_bot").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "/payments/create" not in text, path
        assert not re.search(r"/subscriptions/[^'\"]*/logs", text), path


def test_the_retired_service_methods_and_imports_are_gone():
    """No route reaches them any more: the per-item customer writes and the activity log page."""
    from business_app.services.subscription_service import SubscriptionService

    for name in (
        "add_subscription_item_for_user",
        "update_subscription_item_for_user",
        "remove_subscription_item_for_user",
        "get_subscription_logs_paginated_for_user",
    ):
        assert not hasattr(SubscriptionService, name), name
    payments_api = (ROOT / "business_app" / "api" / "payments.py").read_text(encoding="utf-8")
    for name in ("canonical_rail", "normalize_payment_method", "_SELF_SERVICE_PAYMENT_METHODS", "def create_payment("):
        assert name not in payments_api, name


def test_the_dead_click_card_flow_is_gone():
    """The card charge route went in Task 2c; what only it reached goes with it."""
    from business_app.config.base import BaseConfig
    from business_app.services.click_payment_provider_service import ClickPaymentProviderService
    from business_app.services.payment_service import PaymentService

    assert not hasattr(BaseConfig, "CLICK_SHOP_CALLBACK_URL")
    assert not hasattr(PaymentService, "_process_click_card_payment")
    assert not hasattr(PaymentService, "_save_or_update_verified_card")
    assert not hasattr(ClickPaymentProviderService, "process_card_payment")
    checkout = (ROOT / "business_app" / "templates" / "frontend" / "checkout.html").read_text(encoding="utf-8")
    for dead in ("verification-modal", "payment-processing-modal", "verification-code-input"):
        assert dead not in checkout, dead


@pytest.mark.parametrize("field", ["pre_utilization_at", "payment_ready_at"])
def test_the_checkout_answer_publishes_the_wait_in_seconds_only(field):
    """The bot and the website read `payment_ready_in_seconds`; the instants they were derived from stay inside."""
    orders_api = (ROOT / "business_app" / "api" / "orders.py").read_text(encoding="utf-8")
    assert f'response_data["{field}"]' not in orders_api
