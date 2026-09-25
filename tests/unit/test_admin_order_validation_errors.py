"""The admin create-order modal shows the backend's own sentence, not raw detail fields.

``ValidationError.errors`` prefers ``details`` over the message when no
``validation_errors`` are set. Refusals such as ``ORDER_MIN_AMOUNT`` now carry
``details`` for the staff bot, so answering the admin with ``e.errors`` turned
"Minimum order amount is 20000" into ``["min_amount: 20000.0"]`` -- the admin UI
reads ``payload.errors`` first (``admin_ui/src/utils/apiError.js``).

Driven through the real route the operator's create-order modal posts to, with a
role-claim admin token.
"""
from flask_jwt_extended import create_access_token

from business_app.models.order import Order


def _admin_headers(app, admin_user):
    with app.app_context():
        token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def test_admin_order_below_minimum_shows_the_backend_sentence(app, client, admin_user, sample_product, user_address):
    resp = client.post(
        "/api/v1/admin/orders",
        headers=_admin_headers(app, admin_user),
        json={
            "user_id": user_address.user_id,
            "delivery_address_id": user_address.id,
            "items": [{"product_id": sample_product.id, "quantity": 1}],
            "payment_method": "cash",
        },
    )

    assert resp.status_code == 400, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["errors"][0].startswith("Minimum order amount is"), body
    assert body["data"]["error_code"] == "ORDER_MIN_AMOUNT"
    with app.app_context():
        assert Order.query.filter_by(user_id=user_address.user_id).count() == 0
