"""Shared builders for Phase-2b scope-aware money-engine tests (not collected)."""
from datetime import datetime, UTC
from decimal import Decimal

from business_app.models.customer_link import AddressGroup, CanonicalCustomer
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.user import User, UserAddress
from business_app.utils.password_security import hash_password
from shared.enums import (
    EntitySubtype,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
    UserType,
)

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def make_user(db, *, exempt=False, grocery=False):
    n = _next()
    u = User(
        email=f"u2b{n}@example.com",
        phone=f"+9989{n:08d}",
        password_hash=hash_password("TestPassword123!"),
        first_name=f"F{n}",
        last_name=f"L{n}",
        user_type=UserType.ENTITY if grocery else UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
        cod_debt_check_exempt=exempt,
        created_at=datetime.now(UTC),
    )
    if grocery:
        u.entity_subtype = EntitySubtype.GROCERY_STORE
    db.session.add(u)
    db.session.commit()
    return u


def make_address(db, user):
    a = UserAddress(
        user_id=user.id,
        full_address=f"Addr {_next()}",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
    )
    db.session.add(a)
    db.session.commit()
    return a


def make_place_group(db, *addresses, label="office"):
    # canonical_customer_id is nullable after migration f7c3b9e1d5a2 (Plan 2a).
    g = AddressGroup(label=label)
    db.session.add(g)
    db.session.flush()
    for a in addresses:
        a.address_group_id = g.id
    db.session.commit()
    return g


def link_users(db, users):
    canonical = CanonicalCustomer(primary_user_id=users[0].id)
    db.session.add(canonical)
    db.session.commit()
    for u in users:
        u.canonical_customer_id = canonical.id
    db.session.commit()
    return canonical


def delivered_cod_order(
    db,
    user,
    *,
    address=None,
    total=Decimal("15000.00"),
    outstanding=None,
    status=OrderStatus.DELIVERED,
    created_at=None,
    payment_method=PaymentMethod.CASH,
    payment_status=PaymentStatus.PENDING,
):
    """Delivered order + payment. Defaults to COD, so existing callers are unchanged.

    `payment_method` / `payment_status` exist so the electronic-receivable shape
    can be expressed: a CLICK payment, PARTIALLY_PAID, amount > amount_collected
    (plan 2026-08-08-open-receivable-ssot). Both are threaded onto the order AND
    the payment row, because the receivable queries join the two.
    """
    n = _next()
    ts = created_at or datetime.now(UTC)
    order = Order(
        user_id=user.id,
        order_number=f"ORD-2B-{n}",
        status=status,
        subtotal=total,
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=total,
        payment_method=payment_method,
        delivery_address_id=address.id if address is not None else None,
        created_at=ts,
    )
    db.session.add(order)
    db.session.flush()
    out = outstanding if outstanding is not None else total
    payment = Payment(
        order_id=order.id,
        user_id=user.id,
        payment_method=payment_method,
        amount=total,
        currency="UZS",
        status=payment_status,
        payment_id=f"pay-2b-{n}",
        amount_collected=total - out,
        outstanding_amount=out,
        created_at=ts,
        # ck_payments_cash_completed_requires_collector: only the CASH rail needs
        # a collector to reach COMPLETED; non-cash rows are exempt by the
        # constraint's first disjunct.
        collected_by=(
            user.id
            if payment_method == PaymentMethod.CASH and payment_status == PaymentStatus.COMPLETED
            else None
        ),
    )
    db.session.add(payment)
    db.session.commit()
    return order, payment
