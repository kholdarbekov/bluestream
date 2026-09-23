"""Postgres-only guarantees of migrations c3d9e5f1a7b2 (phase 1), d4e7f9a2b6c1
(phase 2a), f1a2b3c4d5e6 (the delivered-history index), b7c8d9e0f1a2 (phase 2b:
visit photos, the try-out's outlet link), d8e9f0a1b2c3 (phase 3: the agent
day-plan snapshot) and e0f1a2b3c4d5 (branch outlets) — SQLite cannot see them.

The SQLite suite builds its schema from the models with ``db.create_all()``, so
every CHECK / partial-unique that only the migration writes is invisible there.
These tests run the real migrations against a throwaway Postgres database.
"""
import re
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from business_app.models.order import Order, OrderStatusHistory
from business_app.models.product import Product, ProductCategory
from business_app.models.sales import Outlet
from business_app.models.sales_visits import (
    CONFIRMATION_STATUSES,
    NO_ORDER_REASONS,
    PHOTO_KINDS,
    RATE_SOURCES,
    VISIT_OUTCOMES,
    VISIT_STATUSES,
    VISIT_STEPS,
    OrderConfirmationRequest,
    SalesAgentDayPlan,
    Visit,
    VisitPhoto,
    VisitStockCheck,
)
from business_app.models.tryout import ProductTryout, TrialContact
from business_app.models.user import User, UserAddress
from business_app.utils.state_validators import STAFF_ORDER_SOURCES

pytestmark = pytest.mark.integration


def test_user_role_enum_accepts_sales_agent(pg_db):
    values = set(pg_db.session.execute(text("SELECT unnest(enum_range(NULL::user_role))::text")).scalars())
    assert "sales_agent" in values


def test_phase1_tables_exist(pg_db):
    names = set(
        pg_db.session.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        ).scalars()
    )
    assert {"sales_agent_profiles", "outlets", "outlet_contacts", "outlet_stage_history"} <= names


def _rollback_on_named_integrity(pg_db, row, constraint_name):
    """Assert that committing ``row`` violates exactly ``constraint_name``.

    Every constraint test in this file goes through here. A bare IntegrityError
    is weak evidence: a probe row can violate two constraints at once, Postgres
    reports only the first, and the constraint you meant to test is never
    evaluated — the assertion then passes against a schema that does not contain
    it. That is not hypothetical; it bit twice in this very file (the
    rate_source CHECK hidden behind uq_visit_stock_checks_visit_product, and the
    visit FK probes hidden behind uq_visits_one_open_per_agent). Naming the
    constraint also pins the NAMES themselves, which downgrade() drops by name,
    so a typo would otherwise surface only on rollback day.
    """
    pg_db.session.add(row)
    with pytest.raises(IntegrityError) as exc_info:
        pg_db.session.commit()
    # psycopg2 reports the violated constraint by name in the error's diagnostics -- an exact
    # answer, where a substring match against the message is satisfied by any name this one
    # extends (`ck_visits_status` inside `ck_visits_status_v2`).
    assert exc_info.value.orig.diag.constraint_name == constraint_name, str(exc_info.value)
    pg_db.session.rollback()


def _constraint_def(pg_db, table, constraint_name):
    """``pg_get_constraintdef`` for a named constraint ON ``table``, or None if absent.

    `conname` is unique per table, not per database: filtering on the name alone would answer
    for a same-named constraint on any other relation.
    """
    return pg_db.session.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = :name AND conrelid = to_regclass(:table)::oid"
        ),
        {"name": constraint_name, "table": table},
    ).scalar()


def _index_def(pg_db, index_name):
    """``pg_indexes.indexdef`` for a named index, or None if absent."""
    return pg_db.session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = :name"),
        {"name": index_name},
    ).scalar()


def _quoted_literals(constraint_def):
    """The 'single-quoted' values inside a constraint's ARRAY[...] literal.

    Postgres renders the IN-list as
    ``ARRAY['phone'::character varying, 'admin'::character varying, ...]``.
    """
    array_body = re.search(r"ARRAY\[(.*?)\]", constraint_def, re.S)
    assert array_body, f"no ARRAY[...] literal in: {constraint_def}"
    return set(re.findall(r"'([^']*)'", array_body.group(1)))


def test_outlets_reject_unknown_stage_and_half_pins(pg_db):
    _rollback_on_named_integrity(
        pg_db, Outlet(name="A", outlet_type="grocery_store", stage="bogus"), "ck_outlets_stage"
    )
    _rollback_on_named_integrity(
        pg_db,
        Outlet(name="B", outlet_type="grocery_store", latitude=41.3, longitude=None),
        "ck_outlets_coords_pair",
    )
    # "kiosk" is not in OUTLET_TYPES.
    _rollback_on_named_integrity(pg_db, Outlet(name="C", outlet_type="kiosk"), "ck_outlets_outlet_type")


# --------------------------------------------------------------------------- #
# Phase 2a — visits, stock checks, confirmation requests, agent order source
# --------------------------------------------------------------------------- #


def _agent_and_outlet(pg_db, *, phone, outlet_name):
    """One sales agent (users row) plus one outlet, both committed."""
    agent = User(phone=phone, password_hash="not-a-real-hash", first_name="Sardor", last_name="Agent")
    pg_db.session.add(agent)
    outlet = Outlet(name=outlet_name, outlet_type="grocery_store", stage="active")
    pg_db.session.add(outlet)
    pg_db.session.commit()
    return agent, outlet


def _product(pg_db, *, name, sku):
    category = ProductCategory(name="Water")
    pg_db.session.add(category)
    pg_db.session.flush()
    product = Product(
        name=name,
        sku=sku,
        category_id=category.id,
        size="19L",
        base_price=Decimal("15000.00"),
    )
    pg_db.session.add(product)
    pg_db.session.commit()
    return product


def _order(pg_db, *, user_id, **kwargs):
    return Order(
        user_id=user_id,
        subtotal=Decimal("15000.00"),
        total_amount=Decimal("15000.00"),
        **kwargs,
    )


def test_phase2a_tables_exist(pg_db):
    names = set(
        pg_db.session.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        ).scalars()
    )
    assert {"visits", "visit_stock_checks", "order_confirmation_requests"} <= names


def test_products_carry_the_stock_check_flag_defaulting_to_false(pg_db):
    product = _product(pg_db, name="Pure Water 19L", sku="PG-SKU-FLAG")
    assert product.in_sales_stock_check is False


def test_visits_reject_unknown_status_step_outcome_reason_and_half_pins(pg_db):
    agent, outlet = _agent_and_outlet(pg_db, phone="+998900000001", outlet_name="Corner Shop")
    agent_id, outlet_id, now = agent.id, outlet.id, datetime.now(UTC)

    def _visit(**kwargs):
        fields = {"outlet_id": outlet_id, "agent_user_id": agent_id, "started_at": now}
        fields.update(kwargs)
        return Visit(**fields)

    _rollback_on_named_integrity(pg_db, _visit(status="paused"), "ck_visits_status")
    _rollback_on_named_integrity(pg_db, _visit(current_step="payment"), "ck_visits_current_step")
    _rollback_on_named_integrity(pg_db, _visit(outcome="maybe"), "ck_visits_outcome")
    _rollback_on_named_integrity(pg_db, _visit(no_order_reason="forgot"), "ck_visits_no_order_reason")
    _rollback_on_named_integrity(
        pg_db, _visit(checkin_latitude=41.31, checkin_longitude=None), "ck_visits_checkin_coords_pair"
    )


def test_only_one_open_visit_per_agent(pg_db):
    agent, outlet = _agent_and_outlet(pg_db, phone="+998900000002", outlet_name="Second Shop")
    agent_id, outlet_id, now = agent.id, outlet.id, datetime.now(UTC)

    pg_db.session.add(Visit(outlet_id=outlet_id, agent_user_id=agent_id, started_at=now, status="in_progress"))
    pg_db.session.commit()

    _rollback_on_named_integrity(
        pg_db,
        Visit(outlet_id=outlet_id, agent_user_id=agent_id, started_at=now, status="in_progress"),
        "uq_visits_one_open_per_agent",
    )

    # A closed visit never blocks the next one — the index is partial on purpose.
    pg_db.session.add(Visit(outlet_id=outlet_id, agent_user_id=agent_id, started_at=now, status="completed"))
    pg_db.session.add(Visit(outlet_id=outlet_id, agent_user_id=agent_id, started_at=now, status="abandoned"))
    pg_db.session.commit()
    assert Visit.query.filter_by(agent_user_id=agent_id).count() == 3


def test_stock_checks_are_one_row_per_visit_product_with_a_known_rate_source(pg_db):
    agent, outlet = _agent_and_outlet(pg_db, phone="+998900000003", outlet_name="Third Shop")
    product = _product(pg_db, name="Pure Water 19L", sku="PG-SKU-STOCK")
    visit = Visit(outlet_id=outlet.id, agent_user_id=agent.id, started_at=datetime.now(UTC))
    pg_db.session.add(visit)
    pg_db.session.commit()
    visit_id, product_id = visit.id, product.id

    pg_db.session.add(
        VisitStockCheck(
            visit_id=visit_id,
            product_id=product_id,
            on_hand_qty=3,
            rate_per_day=Decimal("1.857"),
            rate_source="stock_checks",
        )
    )
    pg_db.session.commit()

    # (a) the pair is unique.
    _rollback_on_named_integrity(
        pg_db,
        VisitStockCheck(visit_id=visit_id, product_id=product_id, on_hand_qty=4),
        "uq_visit_stock_checks_visit_product",
    )

    # (b) the rate_source CHECK. This row MUST carry a different product: reusing
    # (visit_id, product_id) makes the unique index above fire first and the CHECK
    # is never evaluated, so the test would pass with no CHECK in the schema at all.
    assert _constraint_def(pg_db, "visit_stock_checks", "ck_visit_stock_checks_rate_source") is not None
    other_product = _product(pg_db, name="Pure Water 10L", sku="PG-SKU-STOCK-2")
    _rollback_on_named_integrity(
        pg_db,
        VisitStockCheck(visit_id=visit_id, product_id=other_product.id, on_hand_qty=4, rate_source="guesswork"),
        "ck_visit_stock_checks_rate_source",
    )

    # ...and the same isolated row is accepted once rate_source is legal, proving
    # (b) failed on the CHECK and not on something incidental to the second product.
    pg_db.session.add(
        VisitStockCheck(visit_id=visit_id, product_id=other_product.id, on_hand_qty=4, rate_source="none")
    )
    pg_db.session.commit()


def test_confirmation_requests_reject_unknown_status_and_duplicate_request_ids(pg_db):
    agent, outlet = _agent_and_outlet(pg_db, phone="+998900000004", outlet_name="Fourth Shop")
    customer = User(phone="+998900000104", password_hash="not-a-real-hash", first_name="Store")
    pg_db.session.add(customer)
    pg_db.session.commit()

    order = _order(pg_db, user_id=customer.id, order_source="sales_agent", created_by_staff_id=agent.id)
    pg_db.session.add(order)
    pg_db.session.commit()
    order_id, outlet_id = order.id, outlet.id
    now = datetime.now(UTC)

    pg_db.session.add(
        OrderConfirmationRequest(
            order_id=order_id,
            outlet_id=outlet_id,
            status="pending",
            requested_at=now,
            expires_at=now,
            request_id="abc123def4567890",
        )
    )
    pg_db.session.commit()

    _rollback_on_named_integrity(
        pg_db,
        OrderConfirmationRequest(
            order_id=order_id,
            outlet_id=outlet_id,
            status="maybe",
            requested_at=now,
            expires_at=now,
            request_id="0000000000000001",
        ),
        "ck_order_confirmation_requests_status",
    )
    _rollback_on_named_integrity(
        pg_db,
        OrderConfirmationRequest(
            order_id=order_id,
            outlet_id=outlet_id,
            status="pending",
            requested_at=now,
            expires_at=now,
            request_id="abc123def4567890",
        ),
        "uq_order_confirmation_requests_request_id",
    )


def test_agent_order_requires_a_creator_and_is_numbered_sa(pg_db):
    """The widened ck_orders_staff_creator_for_staff_source, against the real DB."""
    agent, _outlet = _agent_and_outlet(pg_db, phone="+998900000005", outlet_name="Fifth Shop")
    customer = User(phone="+998900000105", password_hash="not-a-real-hash", first_name="Store")
    pg_db.session.add(customer)
    pg_db.session.commit()
    agent_id, customer_id = agent.id, customer.id

    _rollback_on_named_integrity(
        pg_db,
        _order(pg_db, user_id=customer_id, order_source="sales_agent", created_by_staff_id=None),
        "ck_orders_staff_creator_for_staff_source",
    )

    order = _order(pg_db, user_id=customer_id, order_source="sales_agent", created_by_staff_id=agent_id)
    pg_db.session.add(order)
    pg_db.session.commit()
    assert order.order_number.startswith("SA_")

    # The pre-existing halves of the same CHECK still hold.
    _rollback_on_named_integrity(
        pg_db,
        _order(pg_db, user_id=customer_id, order_source="phone", created_by_staff_id=None),
        "ck_orders_staff_creator_for_staff_source",
    )
    self_service = _order(pg_db, user_id=customer_id, order_source="telegram", created_by_staff_id=None)
    pg_db.session.add(self_service)
    pg_db.session.commit()
    assert self_service.order_number.startswith("TG_")


def test_at_most_one_order_per_visit(pg_db):
    agent, outlet = _agent_and_outlet(pg_db, phone="+998900000006", outlet_name="Sixth Shop")
    customer = User(phone="+998900000106", password_hash="not-a-real-hash", first_name="Store")
    pg_db.session.add(customer)
    visit = Visit(outlet_id=outlet.id, agent_user_id=agent.id, started_at=datetime.now(UTC))
    pg_db.session.add(visit)
    pg_db.session.commit()
    agent_id, customer_id, visit_id = agent.id, customer.id, visit.id

    first = _order(
        pg_db, user_id=customer_id, order_source="sales_agent", created_by_staff_id=agent_id, visit_id=visit_id
    )
    pg_db.session.add(first)
    pg_db.session.commit()

    _rollback_on_named_integrity(
        pg_db,
        _order(
            pg_db, user_id=customer_id, order_source="sales_agent", created_by_staff_id=agent_id, visit_id=visit_id
        ),
        "uq_orders_visit_id",
    )

    # NULL visit_id is not covered by the partial index — self-service orders coexist.
    pg_db.session.add(_order(pg_db, user_id=customer_id, order_source="telegram"))
    pg_db.session.add(_order(pg_db, user_id=customer_id, order_source="telegram"))
    pg_db.session.commit()
    assert Order.query.filter(Order.visit_id.is_(None)).count() == 2


def test_every_visit_check_constraint_lists_exactly_the_python_tuple(pg_db):
    """Seven CHECKs, seven Python tuples, one rule each (C01/#31/#44).

    `models/sales_visits.py` builds its CHECK expressions from these tuples and migrations
    d4e7f9a2b6c1 / b7c8d9e0f1a2 write the same literals independently -- only
    `STAFF_ORDER_SOURCES` was ever pinned against the DB. A value added to the Python tuple
    alone silently loses its DB-level backstop; one added in a migration alone is refused by
    the service layer. Nothing else in the repo notices either direction, so the same
    technique is applied to all seven here.
    """
    pins = {
        "ck_visits_status": ("visits", VISIT_STATUSES),
        "ck_visits_current_step": ("visits", VISIT_STEPS),
        "ck_visits_outcome": ("visits", VISIT_OUTCOMES),
        "ck_visits_no_order_reason": ("visits", NO_ORDER_REASONS),
        "ck_visit_stock_checks_rate_source": ("visit_stock_checks", RATE_SOURCES),
        "ck_order_confirmation_requests_status": ("order_confirmation_requests", CONFIRMATION_STATUSES),
        "ck_visit_photos_kind": ("visit_photos", PHOTO_KINDS),
    }
    for name, (table, values) in pins.items():
        constraint_def = _constraint_def(pg_db, table, name)
        assert constraint_def is not None, f"{name} is missing from the migrated schema"
        assert _quoted_literals(constraint_def) == set(values), f"{name}: {constraint_def}"


def test_staff_creator_check_lists_exactly_the_python_staff_order_sources(pg_db):
    """The DB CHECK and STAFF_ORDER_SOURCES are two expressions of ONE rule.

    Nothing re-derives one from the other at runtime, so a fourth staff source
    added in Python alone would silently lose its DB-level backstop (and a source
    added in a migration alone would lose the service-layer guard). This test is
    the only thing that notices.
    """
    constraint_def = _constraint_def(pg_db, "orders", "ck_orders_staff_creator_for_staff_source")
    assert constraint_def is not None, "the staff-creator CHECK is missing from the migrated schema"
    assert _quoted_literals(constraint_def) == set(STAFF_ORDER_SOURCES)


def test_stock_check_flag_defaults_to_false_in_the_DDL_not_just_the_orm(pg_db):
    """A raw INSERT that never mentions the column must still land false.

    ``Column(default=False)`` is applied by SQLAlchemy at INSERT time, so an ORM
    round trip proves nothing about the migration. Only the server_default makes
    the backfill on the existing production rows — and any write that does not go
    through the ORM — come out false.

    The INSERT names only the columns with no server default of their own; every other NOT
    NULL column on `products` -- `in_sales_stock_check` included -- is left to the DDL, which
    is exactly the condition being tested.
    """
    category = ProductCategory(name="Water")
    pg_db.session.add(category)
    pg_db.session.commit()

    pg_db.session.execute(
        text(
            "INSERT INTO products (name, sku, category_id, size, base_price, created_at, updated_at) "
            "VALUES (:name, :sku, :category_id, CAST(:size AS product_size_enum), :base_price, NOW(), NOW())"
        ),
        {
            "name": "Raw Insert 19L",
            "sku": "PG-SKU-RAW-DDL",
            "category_id": category.id,
            "size": "19L",
            "base_price": Decimal("15000.00"),
        },
    )
    pg_db.session.commit()

    stored = pg_db.session.execute(
        text("SELECT in_sales_stock_check FROM products WHERE sku = :sku"),
        {"sku": "PG-SKU-RAW-DDL"},
    ).scalar()
    assert stored is False


def test_every_named_foreign_key_is_enforced(pg_db):
    """All seven FKs the migration names, provoked once each with a bogus id.

    SQLite never sees these (db.create_all() builds the test schema and SQLite
    does not enforce FKs in this suite at all), so this file is the ONLY place
    they can be proven — and the only place their names are checked, which
    downgrade() depends on.
    """
    agent, outlet = _agent_and_outlet(pg_db, phone="+998900000007", outlet_name="Seventh Shop")
    product = _product(pg_db, name="Pure Water 19L", sku="PG-SKU-FK")
    visit = Visit(outlet_id=outlet.id, agent_user_id=agent.id, started_at=datetime.now(UTC))
    pg_db.session.add(visit)
    pg_db.session.commit()
    agent_id, outlet_id, product_id, visit_id = agent.id, outlet.id, product.id, visit.id

    order = _order(pg_db, user_id=agent_id, order_source="sales_agent", created_by_staff_id=agent_id)
    pg_db.session.add(order)
    pg_db.session.commit()
    order_id = order.id

    missing = 10**8  # an id no row in this throwaway database can hold
    now = datetime.now(UTC)

    # status="completed" keeps these off uq_visits_one_open_per_agent: the setup
    # visit above is this agent's open one, so an in_progress probe would trip the
    # partial unique first and the FK would never be evaluated.
    _rollback_on_named_integrity(
        pg_db,
        Visit(outlet_id=missing, agent_user_id=agent_id, started_at=now, status="completed"),
        "fk_visits_outlet_id",
    )
    _rollback_on_named_integrity(
        pg_db,
        Visit(outlet_id=outlet_id, agent_user_id=missing, started_at=now, status="completed"),
        "fk_visits_agent_user_id",
    )
    _rollback_on_named_integrity(
        pg_db,
        VisitStockCheck(visit_id=missing, product_id=product_id, on_hand_qty=1),
        "fk_visit_stock_checks_visit_id",
    )
    _rollback_on_named_integrity(
        pg_db,
        VisitStockCheck(visit_id=visit_id, product_id=missing, on_hand_qty=1),
        "fk_visit_stock_checks_product_id",
    )
    _rollback_on_named_integrity(
        pg_db,
        OrderConfirmationRequest(
            order_id=missing,
            outlet_id=outlet_id,
            requested_at=now,
            expires_at=now,
            request_id="fk00000000000001",
        ),
        "fk_order_confirmation_requests_order_id",
    )
    _rollback_on_named_integrity(
        pg_db,
        OrderConfirmationRequest(
            order_id=order_id,
            outlet_id=missing,
            requested_at=now,
            expires_at=now,
            request_id="fk00000000000002",
        ),
        "fk_order_confirmation_requests_outlet_id",
    )
    _rollback_on_named_integrity(
        pg_db,
        _order(pg_db, user_id=agent_id, order_source="telegram", visit_id=missing),
        "fk_orders_visit_id",
    )


def test_the_delivered_history_window_is_indexed_by_status_and_instant(pg_db):
    """M04: migration f1a2b3c4d5e6's index exists, under its own name, on the pair
    `ReplenishmentService.delivered_qty` filters.

    `orders` has no `delivered_at` column, so every D7 rate window, every stock-out
    projection and `last_delivered_qty` read `order_status_history` by
    `(new_status, changed_at)`; only `order_id` was indexed. SQLite builds its schema
    from the models and never runs the migration, so this is the only place the
    SHIPPED DDL is seen — and naming the index is what `downgrade()` drops by name.
    The model mirror is asserted against the same definition so the two expressions
    of one index cannot drift.
    """
    definition = _index_def(pg_db, "idx_order_status_history_new_status_changed_at")
    assert definition is not None, "migration f1a2b3c4d5e6 did not create the index"
    assert "ON public.order_status_history" in definition, definition
    assert re.search(r"\(new_status, changed_at\)", definition), definition

    declared = {
        index.name: [column.name for column in index.columns]
        for index in OrderStatusHistory.__table__.indexes
    }
    assert declared["idx_order_status_history_new_status_changed_at"] == ["new_status", "changed_at"]


def test_the_constraint_helpers_answer_for_one_name_on_one_table(pg_db):
    """Both helpers are the evidence every other test in this file produces, so both are
    pinned here rather than trusted.

    (a) `_rollback_on_named_integrity` matched the name as an UNANCHORED SUBSTRING of the
    psycopg2 message. That is airtight only while no constraint name is a prefix of another:
    the day `ck_visits_status_v2` exists beside `ck_visits_status`, a probe that violates the
    new one satisfies an assertion naming the old one, and the test passes against a schema
    that no longer contains what it claims to prove. The name now comes from the error's own
    diagnostics.

    (b) `_constraint_def` filtered `pg_constraint` on `conname` alone. Constraint names are
    unique per TABLE, not per database, so `ck_orders_staff_creator_for_staff_source` on a
    future `orders_archive` would have answered for the one on `orders` -- and the CHECK-vs-
    Python-tuple pins below would then be pinned against the wrong table's expression.
    """
    # (a) a name that is a strict prefix of the violated constraint is NOT a match.
    with pytest.raises(AssertionError):
        _rollback_on_named_integrity(
            pg_db, Outlet(name="Prefix probe", outlet_type="grocery_store", stage="bogus"), "ck_outlets_stag"
        )
    # The helper aborts before its own rollback when the name disagrees, so the failed
    # transaction is cleaned up here instead.
    pg_db.session.rollback()

    # ...and the real name still is.
    _rollback_on_named_integrity(
        pg_db, Outlet(name="Exact probe", outlet_type="grocery_store", stage="bogus"), "ck_outlets_stage"
    )

    # (b) right name, wrong table: not found.
    assert _constraint_def(pg_db, "visits", "ck_orders_staff_creator_for_staff_source") is None
    assert _constraint_def(pg_db, "orders", "ck_orders_staff_creator_for_staff_source") is not None


def test_every_named_index_of_the_phase2a_migration_is_live_on_its_columns(pg_db):
    """The nine plain indexes d4e7f9a2b6c1 creates, by NAME and by definition.

    Every named CHECK, unique and foreign key in this file is asserted by name because
    `downgrade()` drops them by name (see `_rollback_on_named_integrity`). The nine
    `op.create_index` calls were the gap: a typo, a dropped line or a rename reaches
    production as a sequential scan on `order_confirmation_requests.expires_at` (the expiry
    sweep's only predicate) and a rollback that dies on `index "..." does not exist`. The two
    PARTIAL uniques (`uq_visits_one_open_per_agent`, `uq_orders_visit_id`) are deliberately
    absent here -- they are proven behaviourally above, which is stronger.
    """
    expected = {
        "ix_visits_outlet_id": "CREATE INDEX ix_visits_outlet_id ON public.visits USING btree (outlet_id)",
        "ix_visits_agent_user_id": (
            "CREATE INDEX ix_visits_agent_user_id ON public.visits USING btree (agent_user_id)"
        ),
        "ix_visits_started_at": "CREATE INDEX ix_visits_started_at ON public.visits USING btree (started_at)",
        "ix_visit_stock_checks_visit_id": (
            "CREATE INDEX ix_visit_stock_checks_visit_id ON public.visit_stock_checks USING btree (visit_id)"
        ),
        "ix_visit_stock_checks_product_id": (
            "CREATE INDEX ix_visit_stock_checks_product_id ON public.visit_stock_checks USING btree (product_id)"
        ),
        "ix_order_confirmation_requests_order_id": (
            "CREATE INDEX ix_order_confirmation_requests_order_id ON public.order_confirmation_requests "
            "USING btree (order_id)"
        ),
        "ix_order_confirmation_requests_status": (
            "CREATE INDEX ix_order_confirmation_requests_status ON public.order_confirmation_requests "
            "USING btree (status)"
        ),
        "ix_order_confirmation_requests_expires_at": (
            "CREATE INDEX ix_order_confirmation_requests_expires_at ON public.order_confirmation_requests "
            "USING btree (expires_at)"
        ),
        "ix_products_in_sales_stock_check": (
            "CREATE INDEX ix_products_in_sales_stock_check ON public.products USING btree (in_sales_stock_check)"
        ),
    }

    assert {name: _index_def(pg_db, name) for name in expected} == expected
    # Negative control: the helper answers None for an index that is not there, so the dict
    # comparison above really is the thing that would fail if one were dropped.
    assert _index_def(pg_db, "ix_visits_no_such_index") is None


# --------------------------------------------------------------------------- #
# Phase 2b — visit photos, the try-out's outlet link
# --------------------------------------------------------------------------- #


def _open_visit(pg_db, *, phone, outlet_name):
    """One agent, one outlet, one visit — committed — plus the ids to use after."""
    agent, outlet = _agent_and_outlet(pg_db, phone=phone, outlet_name=outlet_name)
    visit = Visit(outlet_id=outlet.id, agent_user_id=agent.id, started_at=datetime.now(UTC))
    pg_db.session.add(visit)
    pg_db.session.commit()
    return agent.id, outlet.id, visit.id


def _photo(**kwargs):
    fields = {
        "kind": "storefront",
        "file_path": "sales_visits/2026/09/photo.jpg",
        "sha256": "a" * 64,
        "received_at": datetime.now(UTC),
    }
    fields.update(kwargs)
    return VisitPhoto(**fields)


def test_phase2b_table_and_column_exist(pg_db):
    tables = set(
        pg_db.session.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        ).scalars()
    )
    assert "visit_photos" in tables

    columns = set(
        pg_db.session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'product_tryouts'"
            )
        ).scalars()
    )
    assert "outlet_id" in columns


def test_visit_photos_accept_every_documented_kind_and_refuse_anything_else(pg_db):
    """D17's three kinds, against the migrated CHECK.

    The bot offers exactly these three buttons, so an unknown kind can only
    arrive from a hand-made API call — which is precisely the caller the CHECK
    exists for.
    """
    _agent_id, _outlet_id, visit_id = _open_visit(pg_db, phone="+998900000008", outlet_name="Eighth Shop")

    _rollback_on_named_integrity(pg_db, _photo(visit_id=visit_id, kind="selfie"), "ck_visit_photos_kind")

    for index, kind in enumerate(PHOTO_KINDS):
        pg_db.session.add(_photo(visit_id=visit_id, kind=kind, sha256=str(index) * 64))
    pg_db.session.commit()

    assert VisitPhoto.query.filter_by(visit_id=visit_id).count() == len(PHOTO_KINDS)


def test_a_repeated_digest_is_recorded_not_refused(pg_db):
    """`ix_visit_photos_sha256` is a lookup index, NOT a unique.

    The duplicate rule is per-agent and is decided in the service, which then
    STORES the second photo and points it at the first. A unique index here
    would turn "the agent sent the same storefront twice" into a 500 and lose
    the evidence the exception feed is supposed to show.
    """
    _agent_id, _outlet_id, visit_id = _open_visit(pg_db, phone="+998900000009", outlet_name="Ninth Shop")
    digest = "c" * 64

    first = _photo(visit_id=visit_id, sha256=digest, telegram_file_unique_id="AgADAQADbotUnique")
    pg_db.session.add(first)
    pg_db.session.commit()
    first_id = first.id

    second = _photo(visit_id=visit_id, kind="shelf", sha256=digest, duplicate_of_photo_id=first_id)
    pg_db.session.add(second)
    pg_db.session.commit()

    assert second.duplicate_of_photo_id == first_id
    assert VisitPhoto.query.filter_by(sha256=digest).count() == 2
    assert first.telegram_file_unique_id == "AgADAQADbotUnique"


def test_a_tryout_carries_its_outlet_and_publishes_it(pg_db):
    """The column AND the read surface, in one row.

    `TryoutService.serialize_tryout` spreads `ProductTryout.to_dict()`, so the
    outlet link reaches every try-out response through this one key — there is
    no second place to add it, and no place for it to be forgotten.
    """
    _agent_id, outlet_id, _visit_id = _open_visit(pg_db, phone="+998900000010", outlet_name="Tenth Shop")
    contact = TrialContact(first_name="Dilshod", phone="+998900000110")
    pg_db.session.add(contact)
    pg_db.session.commit()

    tryout = ProductTryout(trial_contact_id=contact.id, outlet_id=outlet_id, source="sales_agent")
    pg_db.session.add(tryout)
    pg_db.session.commit()

    assert tryout.outlet_id == outlet_id
    assert tryout.to_dict()["outlet_id"] == outlet_id


def test_every_named_foreign_key_of_the_phase2b_migration_is_enforced(pg_db):
    """The three FKs b7c8d9e0f1a2 names, provoked once each with a bogus id.

    SQLite enforces none of them in this suite, so this is the only place they
    are proven — and the only place their names are checked, which downgrade()
    depends on.
    """
    _agent_id, outlet_id, visit_id = _open_visit(pg_db, phone="+998900000011", outlet_name="Eleventh Shop")
    contact = TrialContact(first_name="Nodira", phone="+998900000111")
    pg_db.session.add(contact)
    pg_db.session.commit()
    contact_id = contact.id

    missing = 10**8  # an id no row in this throwaway database can hold

    _rollback_on_named_integrity(pg_db, _photo(visit_id=missing), "fk_visit_photos_visit_id")
    _rollback_on_named_integrity(
        pg_db,
        _photo(visit_id=visit_id, duplicate_of_photo_id=missing),
        "fk_visit_photos_duplicate_of_photo_id",
    )
    _rollback_on_named_integrity(
        pg_db,
        ProductTryout(trial_contact_id=contact_id, outlet_id=missing, source="sales_agent"),
        "fk_product_tryouts_outlet_id",
    )

    # ...and the same try-out lands once the outlet exists, proving the row was
    # refused by the FK and not by something incidental to it.
    pg_db.session.add(ProductTryout(trial_contact_id=contact_id, outlet_id=outlet_id, source="sales_agent"))
    pg_db.session.commit()
    assert ProductTryout.query.filter_by(outlet_id=outlet_id).count() == 1


def test_every_named_index_of_the_phase2b_migration_is_live_on_its_columns(pg_db):
    """The three plain indexes b7c8d9e0f1a2 creates, by NAME and by definition.

    `ix_visit_photos_sha256` is the one the per-agent duplicate lookup reads on
    every photo upload; `ix_product_tryouts_outlet_id` is what the nightly stage
    job's converted-try-out pass filters on. A typo in either reaches production
    as a sequential scan and a rollback that dies on `index "..." does not exist`.
    """
    expected = {
        "ix_visit_photos_visit_id": (
            "CREATE INDEX ix_visit_photos_visit_id ON public.visit_photos USING btree (visit_id)"
        ),
        "ix_visit_photos_sha256": (
            "CREATE INDEX ix_visit_photos_sha256 ON public.visit_photos USING btree (sha256)"
        ),
        "ix_product_tryouts_outlet_id": (
            "CREATE INDEX ix_product_tryouts_outlet_id ON public.product_tryouts USING btree (outlet_id)"
        ),
    }

    assert {name: _index_def(pg_db, name) for name in expected} == expected
    assert _index_def(pg_db, "ix_visit_photos_no_such_index") is None


# --------------------------------------------------------------------------- #
# Phase 3 — the nightly agent day-plan snapshot
# --------------------------------------------------------------------------- #


PLAN_DATE = date(2026, 9, 16)


def _day_plan(**kwargs):
    fields = {
        "plan_date": PLAN_DATE,
        "due_count": 0,
        "overdue_count": 0,
        "snapshot_at": datetime.now(UTC),
    }
    fields.update(kwargs)
    return SalesAgentDayPlan(**fields)


def test_phase3_table_exists(pg_db):
    tables = set(
        pg_db.session.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        ).scalars()
    )
    assert "sales_agent_day_plans" in tables


def test_one_plan_row_per_agent_per_day(pg_db):
    """The upsert's backstop, proven by provoking it.

    A retried 01:20 job that inserted instead of updating would double every
    plan-vs-fact denominator for that day, silently and permanently — the unique
    is what turns that bug into a loud IntegrityError.
    """
    agent, _outlet = _agent_and_outlet(pg_db, phone="+998900000012", outlet_name="Twelfth Shop")
    agent_id = agent.id

    pg_db.session.add(_day_plan(agent_user_id=agent_id, due_count=4, overdue_count=2))
    pg_db.session.commit()

    _rollback_on_named_integrity(
        pg_db,
        _day_plan(agent_user_id=agent_id, due_count=9, overdue_count=9),
        "uq_sales_agent_day_plans_agent_date",
    )

    # The NEXT day is a different plan and lands without complaint.
    pg_db.session.add(_day_plan(agent_user_id=agent_id, plan_date=date(2026, 9, 17)))
    pg_db.session.commit()
    assert SalesAgentDayPlan.query.filter_by(agent_user_id=agent_id).count() == 2

    constraint_def = _constraint_def(pg_db, "sales_agent_day_plans", "uq_sales_agent_day_plans_agent_date")
    assert constraint_def == "UNIQUE (agent_user_id, plan_date)"


def test_the_day_plan_foreign_key_is_enforced_by_name(pg_db):
    agent, _outlet = _agent_and_outlet(pg_db, phone="+998900000013", outlet_name="Thirteenth Shop")
    agent_id = agent.id
    missing = 10**8  # an id no row in this throwaway database can hold

    _rollback_on_named_integrity(
        pg_db, _day_plan(agent_user_id=missing), "fk_sales_agent_day_plans_agent_user_id"
    )

    # ...and the same row lands once the agent exists, proving the refusal was the
    # FK and not something incidental to it.
    pg_db.session.add(_day_plan(agent_user_id=agent_id))
    pg_db.session.commit()
    assert SalesAgentDayPlan.query.filter_by(agent_user_id=agent_id).count() == 1


def test_the_day_plan_index_is_live_on_plan_date(pg_db):
    """The index plan-vs-fact's date range reads, by NAME and by definition: a typo
    reaches production as a sequential scan and a rollback that dies on
    `index "..." does not exist`."""
    expected = {
        "ix_sales_agent_day_plans_plan_date": (
            "CREATE INDEX ix_sales_agent_day_plans_plan_date ON public.sales_agent_day_plans "
            "USING btree (plan_date)"
        ),
    }

    assert {name: _index_def(pg_db, name) for name in expected} == expected
    assert _index_def(pg_db, "ix_sales_agent_day_plans_no_such_index") is None


# --------------------------------------------------------------------------- #
# D25 — branch outlets: one account, several outlets, one per address
# --------------------------------------------------------------------------- #


def _address(pg_db, *, user_id, full_address, is_default=False):
    """One `addresses` row, committed. No pin: the zone listener skips coordinate-less rows."""
    address = UserAddress(user_id=user_id, full_address=full_address, is_default=is_default)
    pg_db.session.add(address)
    pg_db.session.commit()
    return address


def test_one_account_may_hold_several_outlets_but_never_two_on_one_address(pg_db):
    """Both halves of migration e0f1a2b3c4d5, because only one of them changed.

    `uq_outlets_user_id` had to go: a chain is ONE customer account with several delivery
    addresses, and the second branch's INSERT died on it. `uq_outlets_address_id` had to
    stay: two outlets on one address would share that place's bottle scope and its
    per-branch order history, i.e. the branch would stop being a branch.
    """
    _agent, first = _agent_and_outlet(pg_db, phone="+998900000014", outlet_name="Chain, Chilonzor")
    customer = User(phone="+998900000114", password_hash="not-a-real-hash", first_name="Chain")
    pg_db.session.add(customer)
    pg_db.session.commit()
    home = _address(pg_db, user_id=customer.id, full_address="Chilonzor 5", is_default=True)
    branch = _address(pg_db, user_id=customer.id, full_address="Yunusobod 19")

    first.user_id = customer.id
    first.address_id = home.id
    pg_db.session.add(
        Outlet(
            name="Chain, Yunusobod",
            outlet_type="grocery_store",
            stage="active",
            user_id=customer.id,
            address_id=branch.id,
        )
    )
    pg_db.session.commit()
    assert Outlet.query.filter_by(user_id=customer.id).count() == 2

    _rollback_on_named_integrity(
        pg_db,
        Outlet(
            name="Chain, Yunusobod (again)",
            outlet_type="grocery_store",
            user_id=customer.id,
            address_id=branch.id,
        ),
        "uq_outlets_address_id",
    )
    assert _constraint_def(pg_db, "outlets", "uq_outlets_address_id") == "UNIQUE (address_id)"


def test_the_outlet_address_foreign_key_is_enforced_by_name(pg_db):
    """The branch IS its address, so a branch pointing at no address is not a branch.

    Pinned by NAME because `downgrade()` drops constraints by name and because every
    address-scoped read added by D25 (`order_scope`, the bottle place balance) trusts this
    column to point at a real row.
    """
    _agent, _outlet = _agent_and_outlet(pg_db, phone="+998900000015", outlet_name="Fifteenth Shop")
    customer = User(phone="+998900000115", password_hash="not-a-real-hash", first_name="Store")
    pg_db.session.add(customer)
    pg_db.session.commit()
    missing = 10**8  # an id no row in this throwaway database can hold

    _rollback_on_named_integrity(
        pg_db,
        Outlet(name="Ghost branch", outlet_type="grocery_store", user_id=customer.id, address_id=missing),
        "fk_outlets_address_id",
    )

    address = _address(pg_db, user_id=customer.id, full_address="Sergeli 3")
    pg_db.session.add(
        Outlet(name="Real branch", outlet_type="grocery_store", user_id=customer.id, address_id=address.id)
    )
    pg_db.session.commit()
    assert Outlet.query.filter_by(address_id=address.id).count() == 1


def test_the_account_index_is_live_and_the_account_unique_is_gone(pg_db):
    """The unique was also the only index on `outlets.user_id`.

    Dropping it without a replacement turns "the other branches of this account" — the
    card's branch count, the sibling dedupe, every account-scoped read — into a sequential
    scan. Postgres renders a unique's backing index in `pg_indexes` too, so `_index_def`
    answering None is the crisp "the unique is gone".
    """
    expected = {"ix_outlets_user_id": "CREATE INDEX ix_outlets_user_id ON public.outlets USING btree (user_id)"}

    assert {name: _index_def(pg_db, name) for name in expected} == expected
    assert _index_def(pg_db, "uq_outlets_user_id") is None
    assert _constraint_def(pg_db, "outlets", "uq_outlets_user_id") is None
    # Negative control: the helper really does answer None for something absent.
    assert _index_def(pg_db, "ix_outlets_no_such_index") is None
