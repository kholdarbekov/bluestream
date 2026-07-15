from decimal import Decimal

from business_app import db
from business_app.models.user import User, UserAddress
from business_app.models.order import Order
from business_app.models.bottle import BottleBalance
from business_app.models.payment import Payment
from business_app.services.customer_map_service import CustomerMapService
from shared.enums import UserRole, UserType, EntitySubtype, OrderStatus, PaymentMethod
from business_app.utils.password_security import hash_password


def _customer(email, phone, user_type=UserType.INDIVIDUAL, entity_subtype=None):
    u = User(email=email, phone=phone, password_hash=hash_password("Passw0rd123!"),
             first_name="Map", last_name="Cust", user_type=user_type, entity_subtype=entity_subtype,
             role=UserRole.CUSTOMER, status="active", is_verified=True)
    db.session.add(u); db.session.commit()
    return u


def _addr(user_id, lat, lng, is_default=True):
    a = UserAddress(user_id=user_id, full_address="Chilonzor 1", city="Tashkent",
                    latitude=lat, longitude=lng, is_default=is_default)
    db.session.add(a); db.session.commit()
    return a


def _order(user_id, status):
    o = Order(user_id=user_id, status=status, total_amount=Decimal("50000"))
    db.session.add(o); db.session.commit()
    return o


def _cash_payment(user_id, order_id, outstanding_amount):
    p = Payment(user_id=user_id, order_id=order_id, amount=outstanding_amount,
                payment_method=PaymentMethod.CASH, outstanding_amount=outstanding_amount)
    db.session.add(p); db.session.commit()
    return p


def test_only_customers_with_non_cancelled_orders_appear(app, db):
    with app.app_context():
        ordered = _customer("ordered@ex.com", "+998900000001")
        _addr(ordered.id, 41.31, 69.28)
        _order(ordered.id, OrderStatus.DELIVERED)

        cancelled_only = _customer("cancel@ex.com", "+998900000002")
        _addr(cancelled_only.id, 41.32, 69.29)
        _order(cancelled_only.id, OrderStatus.CANCELLED)

        never = _customer("never@ex.com", "+998900000003")
        _addr(never.id, 41.33, 69.30)

        pins = CustomerMapService.get_customer_map_pins()
        user_ids = {p["user_id"] for p in pins}
        assert ordered.id in user_ids
        assert cancelled_only.id not in user_ids   # only order is cancelled
        assert never.id not in user_ids            # never ordered


def test_one_pin_per_geocoded_address_with_index(app, db):
    with app.app_context():
        u = _customer("multi@ex.com", "+998900000004")
        _addr(u.id, 41.31, 69.28, is_default=True)
        _addr(u.id, 41.34, 69.31, is_default=False)
        _order(u.id, OrderStatus.CONFIRMED)

        pins = [p for p in CustomerMapService.get_customer_map_pins() if p["user_id"] == u.id]
        assert len(pins) == 2
        assert {p["address_count"] for p in pins} == {2}
        assert sorted(p["address_index"] for p in pins) == [1, 2]


def test_per_address_bottle_balance(app, db):
    with app.app_context():
        u = _customer("bottles@ex.com", "+998900000005")
        a = _addr(u.id, 41.31, 69.28)
        _order(u.id, OrderStatus.DELIVERED)
        db.session.add(BottleBalance(user_id=u.id, address_id=a.id, balance=Decimal("4")))
        db.session.commit()

        pin = next(p for p in CustomerMapService.get_customer_map_pins() if p["user_id"] == u.id)
        assert Decimal(str(pin["bottle_balance"])) == Decimal("4")
        assert pin["last_order_date"] is not None


def test_cod_debt_and_restriction(app, db):
    with app.app_context():
        # Individual customer: 2 delivered orders, each with an open CASH debt.
        u = _customer("cod@ex.com", "+998900000006")
        _addr(u.id, 41.31, 69.28)
        o1 = _order(u.id, OrderStatus.DELIVERED)
        o2 = _order(u.id, OrderStatus.DELIVERED)
        _cash_payment(u.id, o1.id, Decimal("20000.00"))
        _cash_payment(u.id, o2.id, Decimal("15000.00"))

        pin = next(p for p in CustomerMapService.get_customer_map_pins() if p["user_id"] == u.id)
        assert Decimal(str(pin["outstanding_debt"])) == Decimal("35000.00")
        assert pin["active_cod_debt_count"] == 2
        assert pin["cod_restricted"] is True  # 2 >= CashCollectionService.COD_ACTIVE_DEBT_LIMIT

        # Grocery-store entity customer: same debt shape, but exempt from COD restriction.
        g = _customer("grocery@ex.com", "+998900000007",
                      user_type=UserType.ENTITY, entity_subtype=EntitySubtype.GROCERY_STORE)
        _addr(g.id, 41.35, 69.32)
        go1 = _order(g.id, OrderStatus.DELIVERED)
        go2 = _order(g.id, OrderStatus.DELIVERED)
        _cash_payment(g.id, go1.id, Decimal("20000.00"))
        _cash_payment(g.id, go2.id, Decimal("15000.00"))

        gpin = next(p for p in CustomerMapService.get_customer_map_pins() if p["user_id"] == g.id)
        assert Decimal(str(gpin["outstanding_debt"])) > 0
        assert gpin["active_cod_debt_count"] == 2
        assert gpin["cod_restricted"] is False  # grocery stores are exempt
