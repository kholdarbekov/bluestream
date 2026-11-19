"""Payment rows integration tests start from."""

from business_app.models.payment import Payment
from shared.enums import PaymentMethod, PaymentStatus


def seed_click_payment(
    db,
    order,
    payment_id_str: str = 'PAY_CLICK_TEST',
    *,
    click_paydoc_id: str = '20240101000001',
) -> Payment:
    """Seed a PENDING Click Payment row.

    ``click_paydoc_id`` is required by ``_resolve_click_payment_id`` for the
    refund/status/reconcile paths. Default seeds a deterministic value so
    refund/reconcile cells don't have to set up provider_data themselves.
    """
    payment = Payment(
        order_id=order.id,
        user_id=order.user_id,
        payment_method=PaymentMethod.CLICK,
        amount=order.total_amount,
        currency='UZS',
        status=PaymentStatus.PENDING,
        payment_id=payment_id_str,
        provider_data={'click': {'click_paydoc_id': str(click_paydoc_id)}},
    )
    db.session.add(payment)
    db.session.commit()
    return payment
