"""Branch mode: one customer account, several outlets — one per address (D25).

The schema half is pinned in BOTH expressions on purpose. The SQLite suite builds its
schema from the models with `db.create_all()`, so a unique dropped only in the migration
still refuses a second branch here and `flask db migrate` would propose re-creating it;
the migrated DDL is pinned next door in
`tests/integration/test_sales_agent_pg_constraints.py`.
"""
from decimal import Decimal

import pytest
from sqlalchemy import UniqueConstraint

from business_app.models.order import Order
from business_app.models.sales import Outlet
from business_app.models.user import User, UserAddress
from business_app.services.sales.outlet_service import OutletService
from business_app.utils.password_security import hash_password
from shared.enums import EntitySubtype, OrderStatus, UserRole, UserType

pytestmark = pytest.mark.unit

PIN = (41.3111, 69.2797)      # inside TASHKENT_POLYGON
FAR = (41.3300, 69.3200)      # ~4 km away, still inside


def _chain(db, phone):
    user = User(
        phone=phone,
        password_hash=hash_password("Pw123456!"),
        first_name="Chain",
        last_name="Owner",
        user_type=UserType.ENTITY,
        role=UserRole.CUSTOMER,
        company_name="Bahor market",
        entity_subtype=EntitySubtype.GROCERY_STORE,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _address(db, user, full_address, *, pin=PIN, is_default=False):
    address = UserAddress(
        user_id=user.id, full_address=full_address, latitude=pin[0], longitude=pin[1], is_default=is_default
    )
    db.session.add(address)
    db.session.commit()
    return address


def _outlet(db, user, name, address):
    outlet = Outlet(
        name=name,
        outlet_type="grocery_store",
        stage="active",
        user_id=user.id,
        address_id=address.id if address is not None else None,
    )
    db.session.add(outlet)
    db.session.commit()
    return outlet


def _order(db, user, number, address):
    order = Order(
        user_id=user.id,
        order_number=number,
        status=OrderStatus.DELIVERED,
        subtotal=Decimal("15000.00"),
        total_amount=Decimal("15000.00"),
        delivery_address_id=address.id if address is not None else None,
    )
    db.session.add(order)
    db.session.commit()
    return order


def test_the_account_index_replaced_the_account_unique_on_the_model(db):
    """One account may hold several outlets; one address may not (D25 rule 1)."""
    declared = {index.name: [column.name for column in index.columns] for index in Outlet.__table__.indexes}
    uniques = {c.name for c in Outlet.__table__.constraints if isinstance(c, UniqueConstraint)}

    assert declared.get("ix_outlets_user_id") == ["user_id"]
    assert "uq_outlets_user_id" not in uniques
    assert "uq_outlets_address_id" in uniques


def test_a_single_outlet_account_keeps_its_whole_history(db):
    """Branch mode is OFF for one shop, and that is what protects legacy history.

    Orders placed before this feature carry no `delivery_address_id` (the column is
    nullable and the COD ceiling writes NULL ones), so address-scoping a single-shop
    account would zero its rate, its at-risk threshold and its suggested quantity.
    """
    user = _chain(db, "+998901234631")
    home = _address(db, user, "Chilonzor 5", is_default=True)
    outlet = _outlet(db, user, "Bahor market", home)
    pinned = _order(db, user, "SA_000301_26", home)
    legacy = _order(db, user, "SA_000302_26", None)
    other = _chain(db, "+998901234632")
    _outlet(db, other, "Navruz market", _address(db, other, "Sergeli 3", pin=FAR, is_default=True))

    assert OutletService.account_outlet_count(user.id) == 1
    assert OutletService.is_branch(outlet) is False
    assert {o.id for o in Order.query.filter(OutletService.order_scope(outlet)).all()} == {pinned.id, legacy.id}
    assert legacy.delivery_address_id is None


def test_a_second_outlet_scopes_each_branch_to_its_own_address(db):
    """D25 rule 5: in branch mode the history question is asked of the ADDRESS."""
    user = _chain(db, "+998901234633")
    home = _address(db, user, "Chilonzor 5", is_default=True)
    yunusobod = _address(db, user, "Yunusobod 19", pin=FAR)
    chilonzor_shop = _outlet(db, user, "Bahor market, Chilonzor 5", home)
    yunusobod_shop = _outlet(db, user, "Bahor market, Yunusobod 19", yunusobod)
    at_home = _order(db, user, "SA_000303_26", home)
    at_branch = _order(db, user, "SA_000304_26", yunusobod)
    _order(db, user, "SA_000305_26", None)  # legacy, belongs to neither branch

    assert OutletService.account_outlet_count(user.id) == 2
    assert (OutletService.is_branch(chilonzor_shop), OutletService.is_branch(yunusobod_shop)) == (True, True)
    assert {o.id for o in Order.query.filter(OutletService.order_scope(chilonzor_shop)).all()} == {at_home.id}
    assert {o.id for o in Order.query.filter(OutletService.order_scope(yunusobod_shop)).all()} == {at_branch.id}


def test_a_branch_with_no_address_is_not_scoped_to_one(db):
    """The guard that stops an address-less branch claiming the account's legacy orders.

    `outlets.address_id` is nullable, and `Order.delivery_address_id == None` renders
    `delivery_address_id IS NULL` — so without the `address_id is not None` conjunct this
    outlet would read every legacy address-less order on the account as its own history,
    the loudest possible version of the bug branch scoping exists to remove. It is not
    scoped at all instead: it falls back to the account clause until the office gives it
    an address.
    """
    user = _chain(db, "+998901234634")
    home = _address(db, user, "Chilonzor 5", is_default=True)
    addressed = _outlet(db, user, "Bahor market, Chilonzor 5", home)
    orphan = _outlet(db, user, "Bahor market (no address)", None)
    at_home = _order(db, user, "SA_000306_26", home)
    legacy = _order(db, user, "SA_000307_26", None)

    assert OutletService.is_branch(orphan) is True
    assert orphan.address_id is None
    assert {o.id for o in Order.query.filter(OutletService.order_scope(orphan)).all()} == {
        at_home.id,
        legacy.id,
    }
    assert {o.id for o in Order.query.filter(OutletService.order_scope(addressed)).all()} == {at_home.id}


def test_a_lost_sibling_does_not_keep_a_real_shop_in_branch_mode(db):
    """R45: an outlet marked lost is not one of the account's shops any more.

    *Mark lost* is the office's stated answer to a junk branch the import made out of a stale
    address (R4). If the count still saw that row, the one real shop would stay in branch mode:
    its history narrowed to one address, its card claiming a second branch, its money labelled
    account-wide against a sibling that does not trade.
    """
    user = _chain(db, "+998901234635")
    home = _address(db, user, "Chilonzor 5", is_default=True)
    stale = _address(db, user, "Yunusobod 19", pin=FAR)
    shop = _outlet(db, user, "Bahor market, Chilonzor 5", home)
    junk = _outlet(db, user, "Bahor market, Yunusobod 19", stale)
    junk.stage = "lost"
    db.session.commit()
    legacy = _order(db, user, "SA_000308_26", None)
    at_home = _order(db, user, "SA_000309_26", home)

    assert OutletService.account_outlet_count(user.id) == 1
    assert OutletService.is_branch(shop) is False
    # Out of branch mode, the shop reads the whole account again -- legacy rows included.
    assert {o.id for o in Order.query.filter(OutletService.order_scope(shop)).all()} == {legacy.id, at_home.id}


def test_an_outlet_with_no_account_is_never_a_branch(db):
    """A prospect has no customer and therefore no history — not a branch of anything."""
    outlet = Outlet(name="Prospect shop", outlet_type="grocery_store", stage="prospect")
    db.session.add(outlet)
    db.session.commit()

    assert OutletService.account_outlet_count(None) == 0
    assert OutletService.is_branch(outlet) is False
    assert Order.query.filter(OutletService.order_scope(outlet)).all() == []
