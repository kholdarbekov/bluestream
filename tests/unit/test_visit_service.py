"""VisitService: the agent's in-store loop as the staff bot drives it.

Every rule the bot renders — `planned`, `distance_m`, `in_radius`, `suggested_qty`,
and the resume step — is asserted here as a STORED field, because the bot re-derives
none of them (D15). Ownership is never asked twice: a driver or another agent is
refused by `OutletService.get_for_agent`, the same code that guards every other
outlet route, so this file asserts the code it publishes rather than a second rule.
"""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from business_app.models.order import Order
from business_app.models.sales import Outlet, OutletContact
from business_app.models.sales_visits import Visit, VisitPhoto, VisitStockCheck
from business_app.serializers.sales_serializers import (
    AgentConfirmationPayload,
    CheckinPayload,
    CloseVisitPayload,
    StockCheckItemPayload,
    StockCheckPayload,
    serialize_outlet,
    serialize_outlet_agent_row,
    serialize_order_brief,
    serialize_order_placed,
    serialize_stock_check,
    serialize_stock_check_product,
    serialize_visit,
    serialize_visit_photo,
)
from business_app.services.sales.replenishment_service import ReplenishmentService
from business_app.services.sales.visit_service import VisitService
from business_app.utils.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import OrderStatus

# The outlet pin, inside TASHKENT_POLYGON (same coordinate as tests/unit/test_outlet_dedupe.py).
PIN = (41.3111, 69.2797)
# A pure NORTH offset is a clean metre ruler: 1 degree of latitude is ~111.32 km, so
# +0.00027 deg is ~30 m (inside the 250 m geofence) and +0.00629 deg is ~700 m (outside it).
NEAR_30M = (PIN[0] + 0.00027, PIN[1])
FAR_700M = (PIN[0] + 0.00629, PIN[1])

VISIT_KEYS = {
    "id", "outlet_id", "agent_user_id", "status", "planned", "photo_requested", "current_step", "started_at",
    "checkin_at", "checkin_latitude", "checkin_longitude", "checkin_accuracy_m", "distance_m", "in_radius",
    "checkin_skipped", "ended_at", "outcome", "no_order_reason", "dm_present", "notes", "next_visit_at", "order",
    "stock_checks", "previous_stock",
}
STOCK_CHECK_KEYS = {
    "product_id", "product_name", "on_hand_qty", "empties_qty", "is_sold_out", "is_low",
    "suggested_qty", "accepted_qty", "rate_per_day", "rate_source",
}
STOCK_CHECK_PRODUCT_KEYS = {"id", "name", "is_returnable_bottle", "min_order_quantity"}
VISIT_PHOTO_KEYS = {
    "id", "visit_id", "kind", "sha256", "telegram_file_unique_id",
    "received_at", "duplicate_of_photo_id", "is_duplicate",
}


@pytest.fixture
def outlet(db, sales_agent_user):
    """An active class-A grocery outlet assigned to the conftest sales agent, pinned at PIN."""
    row = Outlet(
        name="Bahor do'koni",
        outlet_type="grocery_store",
        stage="active",
        outlet_class="A",
        latitude=PIN[0],
        longitude=PIN[1],
        district="chilanzar",
        assigned_agent_user_id=sales_agent_user.id,
        onboarded_by_user_id=sales_agent_user.id,
    )
    row.contacts.append(OutletContact(name="Olim aka", phone="+998901112277", role="owner", is_primary=True))
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def stock_product(db, sample_product):
    """`sample_product` flagged onto the agent's stock-check list (D22)."""
    sample_product.in_sales_stock_check = True
    db.session.commit()
    return sample_product


def _visit_at_stock(agent_user_id, outlet_id):
    """Open a visit and clear the check-in step the way a skipped check-in does."""
    visit = VisitService.start(agent_user_id, outlet_id)
    return VisitService.checkin(visit, latitude=None, longitude=None, horizontal_accuracy=None, skipped=True)


def _visit_with_an_order(db, outlet, agent, customer, *, order_number):
    """A visit that has ALREADY bought the store water — the subject of ruling 74.

    One builder, because "a visit that carries an order" is what `close`, `abandon` and the
    hourly sweep are each asserted against: three hand-rolled copies of the same row would let
    a rule be proved against a shape the other two never see.
    """
    visit = _visit_at_stock(agent.id, outlet.id)
    db.session.add(
        Order(
            user_id=customer.id,
            order_number=order_number,
            status=OrderStatus.CONFIRMED,
            subtotal=Decimal("120000.00"),
            total_amount=Decimal("120000.00"),
            visit_id=visit.id,
        )
    )
    db.session.commit()
    return visit


def test_serialize_visit_publishes_the_full_key_set(db, outlet, sales_agent_user, sample_user, sample_product):
    visit = Visit(
        outlet_id=outlet.id,
        agent_user_id=sales_agent_user.id,
        status="completed",
        planned=True,
        current_step="close",
        started_at=datetime(2026, 9, 8, 6, 0, tzinfo=UTC),
        checkin_at=datetime(2026, 9, 8, 6, 5, tzinfo=UTC),
        checkin_latitude=NEAR_30M[0],
        checkin_longitude=NEAR_30M[1],
        checkin_accuracy_m=12.5,
        distance_m=30.1,
        in_radius=True,
        checkin_skipped=False,
        ended_at=datetime(2026, 9, 8, 6, 20, tzinfo=UTC),
        outcome="order_placed",
        no_order_reason=None,
        dm_present=True,
        notes="Owner wants a cooler",
        next_visit_at=date(2026, 9, 15),
    )
    db.session.add(visit)
    db.session.flush()
    db.session.add(
        VisitStockCheck(
            visit_id=visit.id,
            product_id=sample_product.id,
            on_hand_qty=3,
            empties_qty=6,
            is_sold_out=False,
            is_low=True,
            suggested_qty=20,
            accepted_qty=18,
            rate_per_day=Decimal("1.857"),
            rate_source="stock_checks",
        )
    )
    db.session.add(
        Order(
            user_id=sample_user.id,
            order_number="SA_000123_26",
            status=OrderStatus.CONFIRMED,
            subtotal=Decimal("270000.00"),
            total_amount=Decimal("270000.00"),
            visit_id=visit.id,
        )
    )
    db.session.commit()

    data = serialize_visit(visit)

    assert set(data) == VISIT_KEYS
    assert data["planned"] is True
    assert data["current_step"] == "close"
    assert data["started_at"] == "2026-09-08T06:00:00+00:00"
    assert data["next_visit_at"] == "2026-09-15"
    assert data["in_radius"] is True and data["distance_m"] == 30.1
    assert data["order"] == {
        "id": visit.order.id,
        "order_number": "SA_000123_26",
        "status": "confirmed",
        "total_amount": 270000.0,
    }
    # One shaper: Task 5a's SALES_VISIT_ORDER_EXISTS details and Task 6's order reply are
    # this same dict, so a 409 can never arrive without the order number the bot prints.
    assert data["order"] == serialize_order_brief(visit.order)
    assert serialize_order_brief(None) is None
    assert data["stock_checks"] == [
        {
            "product_id": sample_product.id,
            "product_name": "Pure Water 19L",
            "on_hand_qty": 3,
            "empties_qty": 6,
            "is_sold_out": False,
            "is_low": True,
            "suggested_qty": 20,
            "accepted_qty": 18,
            "rate_per_day": 1.857,
            "rate_source": "stock_checks",
        }
    ]
    assert set(data["stock_checks"][0]) == STOCK_CHECK_KEYS
    # Ruling 29: the key is always published; it is EMPTY unless the caller supplies the
    # previous visit's rows (only the two screen-opening routes do — Task 6).
    assert data["previous_stock"] == []


def test_serialize_visit_without_an_order_or_stock_checks(db, outlet, sales_agent_user):
    visit = Visit(
        outlet_id=outlet.id,
        agent_user_id=sales_agent_user.id,
        status="in_progress",
        planned=False,
        current_step="checkin",
        started_at=datetime(2026, 9, 8, 6, 0, tzinfo=UTC),
    )
    db.session.add(visit)
    db.session.commit()

    data = serialize_visit(visit)

    assert set(data) == VISIT_KEYS
    assert data["order"] is None
    assert data["stock_checks"] == []
    assert data["previous_stock"] == []
    assert data["checkin_skipped"] is False
    assert data["distance_m"] is None and data["in_radius"] is None


def test_serialize_outlet_agent_row_adds_overdue_and_open_visit(db, outlet):
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    outlet.next_visit_due_at = now - timedelta(days=3, hours=2)
    db.session.commit()

    row = serialize_outlet_agent_row(outlet, open_visit_id=77, now=now)

    assert set(row) == set(serialize_outlet(outlet)) | {
        "overdue_days", "has_open_visit", "open_visit_id", "distance_m",
    }
    assert row["overdue_days"] == 3
    assert row["has_open_visit"] is True
    assert row["open_visit_id"] == 77


def test_serialize_outlet_agent_row_is_none_overdue_when_nothing_is_due(db, outlet):
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    outlet.next_visit_due_at = None
    db.session.commit()

    row = serialize_outlet_agent_row(outlet, open_visit_id=None, now=now)

    assert row["overdue_days"] is None
    assert row["has_open_visit"] is False
    assert row["open_visit_id"] is None


def test_serialize_outlet_agent_row_never_reports_negative_overdue_days(db, outlet):
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    outlet.next_visit_due_at = now + timedelta(days=4)
    db.session.commit()

    assert serialize_outlet_agent_row(outlet, open_visit_id=None, now=now)["overdue_days"] == 0


def test_serialize_stock_check_product_publishes_what_the_shelf_screen_needs(db, stock_product):
    """The bot's shelf screen needs exactly four things per SKU, and `is_returnable_bottle`
    is the SSOT property (tracks AND per-unit > 0) — the bot never sees the pair."""
    stock_product.tracks_returnable_bottles = True
    stock_product.returnable_bottles_per_unit = Decimal("1.00")
    stock_product.min_order_quantity = 2
    db.session.commit()

    data = serialize_stock_check_product(stock_product)

    assert set(data) == STOCK_CHECK_PRODUCT_KEYS
    assert data == {
        "id": stock_product.id,
        "name": "Pure Water 19L",
        "is_returnable_bottle": True,
        "min_order_quantity": 2,
    }


def test_checkin_payload_defaults_to_a_pinned_checkin():
    payload = CheckinPayload(latitude=NEAR_30M[0], longitude=NEAR_30M[1], horizontal_accuracy=12.5)

    assert payload.skipped is False
    assert payload.latitude == NEAR_30M[0]
    assert payload.horizontal_accuracy == 12.5
    # The Skip button sends no coordinates at all.
    assert CheckinPayload(skipped=True).latitude is None


def test_stock_check_payload_accepts_an_empty_count_and_refuses_a_negative_qty():
    payload = StockCheckPayload(items=[{"product_id": 5, "on_hand_qty": 7, "empties_qty": 2, "is_low": True}])

    assert payload.items == [
        StockCheckItemPayload(product_id=5, on_hand_qty=7, empties_qty=2, is_sold_out=False, is_low=True)
    ]
    # An empty list is "there is nothing on the count list here", which is the state of every
    # install until an admin flags a product -- not a malformed request.
    assert StockCheckPayload(items=[]).items == []
    with pytest.raises(ValueError):
        StockCheckPayload(items=[{"product_id": 5, "on_hand_qty": -1}])


def test_close_visit_payload_parses_the_next_visit_date():
    payload = CloseVisitPayload(outcome="no_order", no_order_reason="sufficient_stock", next_visit_at="2026-09-15")

    assert payload.next_visit_at == date(2026, 9, 15)
    assert payload.notes is None
    assert payload.dm_present is None


def test_agent_confirmation_payload_carries_the_action_and_an_optional_reason():
    """The customer bot's Confirm/Decline body. The model shapes it; the VALUE of `action`
    is refused by `AgentOrderConfirmationService.respond` with SALES_CONFIRMATION_ACTION_INVALID
    (Task 5b), so the rule lives in one place and the route stays thin."""
    assert AgentConfirmationPayload(action="confirm").reason is None

    payload = AgentConfirmationPayload(action="decline", reason="Bugun pul yo'q")

    assert (payload.action, payload.reason) == ("decline", "Bugun pul yo'q")


def test_start_opens_a_visit_at_the_checkin_step_and_flags_a_due_outlet(db, outlet, sales_agent_user):
    outlet.next_visit_due_at = datetime.now(UTC) - timedelta(days=1)
    db.session.commit()

    visit = VisitService.start(sales_agent_user.id, outlet.id)

    assert visit.id is not None
    assert visit.outlet_id == outlet.id
    assert visit.agent_user_id == sales_agent_user.id
    assert visit.status == "in_progress"
    assert visit.current_step == "checkin"
    assert visit.planned is True
    assert visit.started_at is not None
    assert visit.ended_at is None
    assert VisitService.current(sales_agent_user.id).id == visit.id


def test_start_counts_a_visit_due_later_today_as_planned(db, outlet, sales_agent_user):
    """C14: the boundary `planned` is decided on is the end of the LOCAL day, not `now`.

    D7 routinely produces same-day-later due instants (`predicted_stockout_at - lead`), and every
    existing case sits a whole day either side of the line -- so a regression to
    `<= datetime.now(UTC)`, or a `local_day_end_utc` that lost its `+1 day`, would pass them all
    while quietly marking an outlet the agent reached FROM the due list as an unplanned drop-in.
    """
    due_later_today = ReplenishmentService.local_day_end_utc() - timedelta(minutes=1)
    assert due_later_today > datetime.now(UTC), "the boundary case must lie in the future"
    outlet.next_visit_due_at = due_later_today
    db.session.commit()

    assert VisitService.start(sales_agent_user.id, outlet.id).planned is True


def test_start_marks_an_unplanned_visit_and_abandon_frees_the_agent(db, outlet, sales_agent_user):
    """An agent may visit anything at any time (D7); `planned` records which it was."""
    outlet.next_visit_due_at = datetime.now(UTC) + timedelta(days=10)
    db.session.commit()

    visit = VisitService.start(sales_agent_user.id, outlet.id)
    assert visit.planned is False

    VisitService.abandon(visit, sales_agent_user.id)
    outlet.next_visit_due_at = None
    db.session.commit()

    again = VisitService.start(sales_agent_user.id, outlet.id)
    assert again.planned is False
    assert again.id != visit.id


def test_a_second_start_returns_the_open_visit_in_the_conflict(db, outlet, sales_agent_user):
    """The bot renders Resume/Abandon straight from these details — no extra round trip."""
    first = VisitService.start(sales_agent_user.id, outlet.id)

    with pytest.raises(ConflictError) as exc:
        VisitService.start(sales_agent_user.id, outlet.id)

    assert exc.value.error_code == "SALES_VISIT_ALREADY_OPEN"
    assert exc.value.details == {"visit_id": first.id, "outlet_id": outlet.id}
    assert Visit.query.count() == 1


def test_start_refuses_an_outlet_that_is_not_the_actors(db, outlet, delivery_driver):
    with pytest.raises(ForbiddenError) as exc:
        VisitService.start(delivery_driver.id, outlet.id)

    assert exc.value.error_code == "SALES_OUTLET_NOT_ASSIGNED"
    assert Visit.query.count() == 0


def test_start_refuses_another_agents_outlet(db, outlet):
    from tests.unit.test_sales_agent_role import make_sales_agent_user

    other_agent = make_sales_agent_user(db, phone="+998901234579")

    with pytest.raises(ForbiddenError) as exc:
        VisitService.start(other_agent.id, outlet.id)

    assert exc.value.error_code == "SALES_OUTLET_NOT_ASSIGNED"
    assert Visit.query.count() == 0


def test_current_is_none_until_a_visit_is_open(db, sales_agent_user):
    assert VisitService.current(sales_agent_user.id) is None


def test_get_owned_refuses_a_missing_a_foreign_and_a_closed_visit(db, outlet, sales_agent_user, delivery_driver):
    visit = VisitService.start(sales_agent_user.id, outlet.id)

    with pytest.raises(NotFoundError) as missing:
        VisitService.get_owned(visit.id + 1000, sales_agent_user.id)
    assert missing.value.error_code == "SALES_VISIT_NOT_FOUND"

    with pytest.raises(ForbiddenError) as foreign:
        VisitService.get_owned(visit.id, delivery_driver.id)
    assert foreign.value.error_code == "SALES_VISIT_NOT_OWNED"

    assert VisitService.get_owned(visit.id, sales_agent_user.id).id == visit.id

    VisitService.abandon(visit, sales_agent_user.id)

    with pytest.raises(ConflictError) as closed:
        VisitService.get_owned(visit.id, sales_agent_user.id)
    assert closed.value.error_code == "SALES_VISIT_NOT_OPEN"
    assert VisitService.get_owned(visit.id, sales_agent_user.id, must_be_open=False).status == "abandoned"


def test_previous_stock_rows_are_the_last_completed_visits_counts(db, outlet, sales_agent_user, stock_product):
    """Ruling 29 / spec *Visit* step 2 ("Pre-filled from the previous stock check").

    Which visit counts as "previous" is decided HERE, once: the outlet's most recent
    COMPLETED visit. An abandoned attempt is not a stock count, and the open visit is
    not its own history — get either wrong and the agent corrects numbers that were
    never on the shelf.
    """
    older = Visit(
        outlet_id=outlet.id,
        agent_user_id=sales_agent_user.id,
        status="completed",
        planned=True,
        current_step="close",
        started_at=datetime(2026, 9, 1, 6, 0, tzinfo=UTC),
    )
    abandoned = Visit(
        outlet_id=outlet.id,
        agent_user_id=sales_agent_user.id,
        status="abandoned",
        planned=False,
        current_step="stock",
        started_at=datetime(2026, 9, 5, 6, 0, tzinfo=UTC),
    )
    db.session.add_all([older, abandoned])
    db.session.flush()
    db.session.add(
        VisitStockCheck(visit_id=older.id, product_id=stock_product.id, on_hand_qty=7, empties_qty=4)
    )
    db.session.add(VisitStockCheck(visit_id=abandoned.id, product_id=stock_product.id, on_hand_qty=99))
    db.session.commit()

    visit = VisitService.start(sales_agent_user.id, outlet.id)
    rows = VisitService.previous_stock_rows(visit)

    assert [(row.product_id, row.on_hand_qty, row.empties_qty) for row in rows] == [(stock_product.id, 7, 4)]
    # The two screen-opening routes pass the rows into the serializer (Task 6); every other
    # response publishes the key as an empty list.
    assert serialize_visit(visit, previous_stock=rows)["previous_stock"] == [serialize_stock_check(rows[0])]
    assert serialize_visit(visit)["previous_stock"] == []


def test_checkin_30_m_from_the_pin_is_inside_the_geofence(db, app, outlet, sales_agent_user):
    visit = VisitService.start(sales_agent_user.id, outlet.id)

    updated = VisitService.checkin(
        visit, latitude=NEAR_30M[0], longitude=NEAR_30M[1], horizontal_accuracy=12.5, skipped=False
    )

    assert abs(updated.distance_m - 30) <= 5
    assert updated.in_radius is True
    assert updated.checkin_skipped is False
    assert updated.checkin_latitude == pytest.approx(NEAR_30M[0])
    assert updated.checkin_longitude == pytest.approx(NEAR_30M[1])
    assert updated.checkin_accuracy_m == 12.5
    assert updated.checkin_at is not None
    assert updated.current_step == "stock"
    assert app.config["SALES_GEOFENCE_RADIUS_M"] == 250


def test_checkin_700_m_away_is_recorded_and_never_blocks(db, outlet, sales_agent_user):
    """D16: the geofence is SOFT — an agent may stand across the road."""
    visit = VisitService.start(sales_agent_user.id, outlet.id)

    updated = VisitService.checkin(
        visit, latitude=FAR_700M[0], longitude=FAR_700M[1], horizontal_accuracy=None, skipped=False
    )

    assert 690 <= updated.distance_m <= 710
    assert updated.in_radius is False
    assert updated.checkin_accuracy_m is None
    assert updated.current_step == "stock"


def test_checkin_can_be_skipped_and_the_skip_is_flagged(db, outlet, sales_agent_user):
    visit = VisitService.start(sales_agent_user.id, outlet.id)

    updated = VisitService.checkin(visit, latitude=None, longitude=None, horizontal_accuracy=None, skipped=True)

    assert updated.checkin_skipped is True
    assert updated.checkin_latitude is None
    assert updated.checkin_longitude is None
    assert updated.checkin_accuracy_m is None
    assert updated.distance_m is None
    assert updated.in_radius is None
    assert updated.checkin_at is not None
    assert updated.current_step == "stock"


def test_checkin_against_an_outlet_without_a_pin_records_no_distance(db, outlet, sales_agent_user):
    outlet.latitude = None
    outlet.longitude = None
    db.session.commit()
    visit = VisitService.start(sales_agent_user.id, outlet.id)

    updated = VisitService.checkin(
        visit, latitude=NEAR_30M[0], longitude=NEAR_30M[1], horizontal_accuracy=None, skipped=False
    )

    assert updated.checkin_latitude == pytest.approx(NEAR_30M[0])
    assert updated.distance_m is None
    assert updated.in_radius is None
    assert updated.current_step == "stock"


def test_a_second_checkin_is_refused_by_the_step_guard(db, outlet, sales_agent_user):
    visit = VisitService.start(sales_agent_user.id, outlet.id)
    VisitService.checkin(visit, latitude=None, longitude=None, horizontal_accuracy=None, skipped=True)

    with pytest.raises(ValidationError) as exc:
        VisitService.checkin(
            visit, latitude=NEAR_30M[0], longitude=NEAR_30M[1], horizontal_accuracy=None, skipped=False
        )

    assert exc.value.error_code == "SALES_VISIT_STEP_INVALID"
    assert exc.value.details["current_step"] == "stock"
    assert Visit.query.get(visit.id).checkin_skipped is True


def test_stock_check_stores_the_rows_and_moves_the_visit_to_the_order_step(
    db, outlet, sales_agent_user, stock_product
):
    visit = _visit_at_stock(sales_agent_user.id, outlet.id)

    rows = VisitService.stock_check(
        visit,
        [{"product_id": stock_product.id, "on_hand_qty": 7, "empties_qty": 2, "is_sold_out": False, "is_low": True}],
    )

    # A brand-new outlet has no history at all, so the rule publishes "none" rather than a guess.
    assert rows == [
        {
            "product_id": stock_product.id,
            "product_name": "Pure Water 19L",
            "on_hand_qty": 7,
            "empties_qty": 2,
            "is_sold_out": False,
            "is_low": True,
            "suggested_qty": None,
            "accepted_qty": None,
            "rate_per_day": None,
            "rate_source": "none",
            "last_order_qty": None,
        }
    ]
    assert visit.current_step == "order"
    stored = VisitStockCheck.query.filter_by(visit_id=visit.id).all()
    assert [(r.product_id, r.on_hand_qty, r.empties_qty, r.is_low, r.rate_source) for r in stored] == [
        (stock_product.id, 7, 2, True, "none")
    ]


def test_resubmitting_the_stock_check_overwrites_the_same_row(db, outlet, sales_agent_user, stock_product):
    """Step 2 is re-enterable from the order screen (Back), so a re-submit must not duplicate."""
    visit = _visit_at_stock(sales_agent_user.id, outlet.id)
    VisitService.stock_check(visit, [{"product_id": stock_product.id, "on_hand_qty": 7, "is_low": True}])

    rows = VisitService.stock_check(
        visit,
        [{"product_id": stock_product.id, "on_hand_qty": 0, "empties_qty": 11, "is_sold_out": True, "is_low": False}],
    )

    assert VisitStockCheck.query.filter_by(visit_id=visit.id).count() == 1
    assert rows[0]["on_hand_qty"] == 0
    assert rows[0]["empties_qty"] == 11
    assert rows[0]["is_sold_out"] is True
    assert rows[0]["is_low"] is False
    assert visit.current_step == "order"


def test_stock_check_refuses_a_product_that_is_not_on_the_agent_list(
    db, outlet, sales_agent_user, stock_product, sample_category
):
    """D22: the list is the admin's `in_sales_stock_check` flag, not "any product"."""
    from business_app.models.product import Product

    unflagged = Product(
        name="Sparkling 10L",
        category_id=sample_category.id,
        size="10L",
        volume=10.0,
        volume_unit="L",
        base_price=Decimal("9000.00"),
        stock_quantity=50,
        is_active=True,
    )
    db.session.add(unflagged)
    db.session.commit()
    visit = _visit_at_stock(sales_agent_user.id, outlet.id)

    with pytest.raises(ValidationError) as exc:
        VisitService.stock_check(
            visit,
            [
                {"product_id": stock_product.id, "on_hand_qty": 4},
                {"product_id": unflagged.id, "on_hand_qty": 3},
            ],
        )

    assert exc.value.error_code == "SALES_STOCK_PRODUCT_INVALID"
    assert exc.value.details["product_id"] == unflagged.id
    # Nothing from the same submission was written: the whole screen is one unit of work.
    assert VisitStockCheck.query.count() == 0
    assert Visit.query.get(visit.id).current_step == "stock"


def test_stock_check_refuses_a_quantity_above_the_configured_cap(db, app, outlet, sales_agent_user, stock_product):
    visit = _visit_at_stock(sales_agent_user.id, outlet.id)
    cap = app.config["SALES_STOCK_QTY_MAX"]

    with pytest.raises(ValidationError) as too_many:
        VisitService.stock_check(visit, [{"product_id": stock_product.id, "on_hand_qty": cap + 1}])
    assert too_many.value.error_code == "SALES_STOCK_QTY_INVALID"

    with pytest.raises(ValidationError) as negative:
        VisitService.stock_check(visit, [{"product_id": stock_product.id, "on_hand_qty": -1}])
    assert negative.value.error_code == "SALES_STOCK_QTY_INVALID"

    with pytest.raises(ValidationError) as empties:
        VisitService.stock_check(
            visit, [{"product_id": stock_product.id, "on_hand_qty": 1, "empties_qty": cap + 1}]
        )
    assert empties.value.error_code == "SALES_STOCK_QTY_INVALID"

    assert VisitStockCheck.query.count() == 0
    assert Visit.query.get(visit.id).current_step == "stock"


def test_stock_check_is_refused_before_the_checkin_step(db, outlet, sales_agent_user, stock_product):
    visit = VisitService.start(sales_agent_user.id, outlet.id)

    with pytest.raises(ValidationError) as exc:
        VisitService.stock_check(visit, [{"product_id": stock_product.id, "on_hand_qty": 1}])

    assert exc.value.error_code == "SALES_VISIT_STEP_INVALID"
    assert exc.value.details["current_step"] == "checkin"


def test_stock_check_stores_exactly_what_replenishment_service_published(
    db, monkeypatch, outlet, sales_agent_user, stock_product
):
    """The suggestion is the BACKEND's rule (D15). This pins the seam: the row carries what
    ReplenishmentService answered, and it is asked with THIS outlet's cadence and on-hand."""
    from business_app.services.sales import visit_service as module

    calls = {}

    class _Replenishment:
        @staticmethod
        def stock_check_products():
            return [stock_product]

        @staticmethod
        def cadence_days(outlet_arg):
            calls["cadence_outlet_id"] = outlet_arg.id
            return 7

        @staticmethod
        def rate_per_day(outlet_arg, product_arg, *, now, current_visit_id=None):
            calls["rate"] = (outlet_arg.id, product_arg.id, now.tzinfo is not None)
            calls["rate_visit_id"] = current_visit_id
            return Decimal("1.857"), "stock_checks"

        @staticmethod
        def last_delivered_qty(outlet_arg, product_arg):
            calls["last"] = (outlet_arg.id, product_arg.id)
            return 6

        @staticmethod
        def suggested_qty(product_arg, rate, on_hand, cadence, *, last_qty):
            calls["suggested"] = (product_arg.id, str(rate), on_hand, cadence, last_qty)
            return 20

        @staticmethod
        def local_day_end_utc(now=None):
            return datetime.now(UTC) + timedelta(hours=1)

        @staticmethod
        def recompute_outlet(outlet_arg, *, now=None):
            calls["recomputed"] = outlet_arg.id

    monkeypatch.setattr(module, "ReplenishmentService", _Replenishment)
    visit = _visit_at_stock(sales_agent_user.id, outlet.id)

    rows = VisitService.stock_check(visit, [{"product_id": stock_product.id, "on_hand_qty": 3}])

    assert calls["cadence_outlet_id"] == outlet.id
    assert calls["rate"] == (outlet.id, stock_product.id, True)
    # The rate is measured against the caller's OWN visit: this visit's rows have only just been
    # flushed and are not `completed` yet, and another agent's open visit at the same shop must
    # never be the half the measurement pairs with (C28).
    assert calls["rate_visit_id"] == visit.id
    assert calls["last"] == (outlet.id, stock_product.id)
    assert calls["suggested"] == (stock_product.id, "1.857", 3, 7, 6)
    assert rows[0]["suggested_qty"] == 20
    assert rows[0]["rate_per_day"] == 1.857
    assert rows[0]["rate_source"] == "stock_checks"
    assert rows[0]["last_order_qty"] == 6
    stored = VisitStockCheck.query.filter_by(visit_id=visit.id).one()
    assert stored.suggested_qty == 20
    assert stored.rate_source == "stock_checks"
    assert float(stored.rate_per_day) == 1.857


def test_close_with_an_order_auto_sets_the_outcome_and_reactivates_the_outlet(
    db, outlet, sales_agent_user, sample_user
):
    outlet.stage = "at_risk"
    db.session.commit()
    visit = _visit_with_an_order(db, outlet, sales_agent_user, sample_user, order_number="SA_000124_26")
    next_at = date(2026, 9, 15)

    closed = VisitService.close(
        visit,
        outcome=None,
        no_order_reason=None,
        notes="Owner wants a cooler",
        next_visit_at=next_at,
        dm_present=True,
    )

    assert closed.status == "completed"
    assert closed.outcome == "order_placed"
    assert closed.no_order_reason is None
    assert closed.notes == "Owner wants a cooler"
    assert closed.next_visit_at == next_at
    assert closed.dm_present is True
    assert closed.ended_at is not None
    assert outlet.stage == "active"
    assert outlet.last_visit_at is not None
    assert outlet.agent_next_visit_at == next_at
    # The due date is ReplenishmentService's rule, not this test's — asserted against the real
    # function so the two can never drift apart.
    assert ensure_utc(outlet.next_visit_due_at) == ReplenishmentService.next_visit_due_at(
        outlet, now=datetime.now(UTC)
    )
    assert VisitService.current(sales_agent_user.id) is None


def test_close_without_an_order_demands_an_outcome_and_a_reason(db, outlet, sales_agent_user):
    visit = _visit_at_stock(sales_agent_user.id, outlet.id)

    with pytest.raises(ValidationError) as missing:
        VisitService.close(visit, outcome=None, no_order_reason=None, notes=None, next_visit_at=None, dm_present=None)
    assert missing.value.error_code == "SALES_VISIT_OUTCOME_REQUIRED"

    with pytest.raises(ValidationError) as unknown:
        VisitService.close(
            visit, outcome="went_home", no_order_reason=None, notes=None, next_visit_at=None, dm_present=None
        )
    assert unknown.value.error_code == "SALES_VISIT_OUTCOME_INVALID"

    with pytest.raises(ValidationError) as no_reason:
        VisitService.close(
            visit, outcome="no_order", no_order_reason=None, notes=None, next_visit_at=None, dm_present=None
        )
    assert no_reason.value.error_code == "SALES_VISIT_OUTCOME_REQUIRED"

    with pytest.raises(ValidationError) as bad_reason:
        VisitService.close(
            visit, outcome="no_order", no_order_reason="mood", notes=None, next_visit_at=None, dm_present=None
        )
    assert bad_reason.value.error_code == "SALES_VISIT_OUTCOME_INVALID"

    assert Visit.query.get(visit.id).status == "in_progress"

    closed = VisitService.close(
        visit,
        outcome="no_order",
        no_order_reason="sufficient_stock",
        notes="  ",
        next_visit_at=None,
        dm_present=False,
    )

    assert closed.outcome == "no_order"
    assert closed.no_order_reason == "sufficient_stock"
    assert closed.notes is None
    assert closed.dm_present is False
    assert outlet.agent_next_visit_at is None


def test_close_drops_a_reason_that_does_not_belong_to_the_outcome(db, outlet, sales_agent_user):
    visit = _visit_at_stock(sales_agent_user.id, outlet.id)

    closed = VisitService.close(
        visit,
        outcome="owner_absent",
        no_order_reason="price",
        notes=None,
        next_visit_at=None,
        dm_present=None,
    )

    assert closed.outcome == "owner_absent"
    assert closed.no_order_reason is None


def test_close_without_an_order_leaves_an_at_risk_outlet_at_risk(db, outlet, sales_agent_user):
    """Only a real order rescues a stage — a visit alone is not evidence of a live customer."""
    outlet.stage = "at_risk"
    db.session.commit()
    visit = _visit_at_stock(sales_agent_user.id, outlet.id)

    VisitService.close(
        visit, outcome="refused", no_order_reason=None, notes=None, next_visit_at=None, dm_present=None
    )

    assert outlet.stage == "at_risk"


def test_abandon_ends_the_visit_and_frees_the_agent(db, outlet, sales_agent_user):
    visit = VisitService.start(sales_agent_user.id, outlet.id)

    abandoned = VisitService.abandon(visit, sales_agent_user.id)

    assert abandoned.status == "abandoned"
    assert abandoned.ended_at is not None
    assert VisitService.current(sales_agent_user.id) is None


def test_abandon_on_an_ordered_visit_closes_it_as_order_placed(db, outlet, sales_agent_user, sample_user):
    """Ruling 74: the agent's OWN Abandon tap obeys the rule the sweep obeys.

    Abandon is drawn on every screen inside a visit, the post-order ones included, so one tap
    could strand a visit that had just booked 8 bottles: no outcome for the KPIs that read that
    column, and no `close` — the only thing on the visit path that republishes the outlet's
    next-visit date after a productive call. The rule lives in `abandon` now, so the tap and the
    hourly sweep cannot answer it differently.
    """
    visit = _visit_with_an_order(db, outlet, sales_agent_user, sample_user, order_number="SA_000127_26")
    outlet.next_visit_due_at = None
    # The date agreed at the PREVIOUS close. Nobody is standing at the counter to offer a new
    # one, and it is a candidate in the D7 min(), so erasing it would silently move the shop's
    # next visit (ruling 54) — the same carry-forward the sweep makes.
    outlet.agent_next_visit_at = date(2026, 9, 20)
    db.session.commit()
    moment = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)

    result = VisitService.abandon(visit, sales_agent_user.id, at=moment)

    assert result.status == "completed"
    assert result.outcome == "order_placed"
    # `ensure_utc`, like the other stamped-moment assertions here: the commit expires the row and
    # SQLite reads the column back naive.
    assert ensure_utc(result.ended_at) == moment
    assert result.next_visit_at == date(2026, 9, 20)
    assert ensure_utc(outlet.last_visit_at) == moment
    assert outlet.agent_next_visit_at == date(2026, 9, 20)
    assert outlet.next_visit_due_at is not None
    assert VisitService.current(sales_agent_user.id) is None


def test_abandon_stale_closes_an_ordered_visit_instead_of_stranding_it(
    db, app, monkeypatch, outlet, sales_agent_user, sample_user
):
    """C05/#08: a stale visit that ALREADY bought the store water is closed, not abandoned.

    Abandoning it strands two things: the visit carries no outcome (every downstream KPI reads
    that column, and an order with no `order_placed` is invisible to them), and
    `VisitService.close` -- the only caller of `recompute_outlet` on the visit path -- never
    runs, so the outlet's next-visit date is never republished after a productive visit.
    """
    from business_app.services.sales import visit_service as module

    frozen = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)

    class _Clock:
        now_utc = frozen

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return _Clock.now_utc.astimezone(tz) if tz else _Clock.now_utc.replace(tzinfo=None)

    monkeypatch.setattr(module, "datetime", _FrozenDatetime)

    _Clock.now_utc = frozen - timedelta(hours=7)
    ordered = _visit_with_an_order(db, outlet, sales_agent_user, sample_user, order_number="SA_000125_26")
    outlet.next_visit_due_at = None
    # The date the agent and the shopkeeper agreed at the PREVIOUS close. The sweep has no new
    # date to offer, and it is a candidate in the D7 min() -- erasing it would silently move the
    # shop's next visit (ruling 54).
    outlet.agent_next_visit_at = date(2026, 9, 20)
    db.session.commit()

    _Clock.now_utc = frozen
    handled = VisitService.abandon_stale(now=frozen)

    assert handled == 1
    swept = Visit.query.get(ordered.id)
    assert swept.status == "completed"
    # An order IS the outcome — the same rule `close` applies to an agent's own close tap.
    assert swept.outcome == "order_placed" and swept.no_order_reason is None
    assert ensure_utc(swept.ended_at) == frozen
    reloaded = Outlet.query.get(outlet.id)
    assert reloaded.next_visit_due_at is not None
    assert reloaded.agent_next_visit_at == date(2026, 9, 20)
    assert VisitService.current(sales_agent_user.id) is None


def test_abandon_stale_sweeps_a_7_hour_old_visit_and_leaves_a_1_hour_old_one(
    db, app, monkeypatch, outlet, sales_agent_user
):
    """The window is 6 h (SALES_VISIT_AUTO_ABANDON_HOURS), so the sweep must split these two."""
    from business_app.services.sales import visit_service as module
    from tests.unit.test_sales_agent_role import make_sales_agent_user

    frozen = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)

    class _Clock:
        now_utc = frozen

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return _Clock.now_utc.astimezone(tz) if tz else _Clock.now_utc.replace(tzinfo=None)

    monkeypatch.setattr(module, "datetime", _FrozenDatetime)
    second_agent = make_sales_agent_user(db, phone="+998901234580")

    _Clock.now_utc = frozen - timedelta(hours=7)
    stale = VisitService.start(sales_agent_user.id, outlet.id)

    _Clock.now_utc = frozen - timedelta(hours=1)
    fresh = Visit(
        outlet_id=outlet.id,
        agent_user_id=second_agent.id,
        status="in_progress",
        planned=False,
        current_step="checkin",
        started_at=_Clock.now_utc,
    )
    db.session.add(fresh)
    db.session.commit()

    _Clock.now_utc = frozen
    swept = VisitService.abandon_stale(now=frozen)

    assert app.config["SALES_VISIT_AUTO_ABANDON_HOURS"] == 6
    assert swept == 1
    assert Visit.query.get(stale.id).status == "abandoned"
    assert ensure_utc(Visit.query.get(stale.id).ended_at) == frozen
    assert Visit.query.get(fresh.id).status == "in_progress"
    assert VisitService.current(second_agent.id).id == fresh.id


def test_abandon_stale_stamps_the_sweep_moment_it_was_given(db, outlet, sales_agent_user, sample_user):
    """`abandon_stale(now=X)` SELECTS by X, so it must STAMP with X — on both legs.

    The caller owns the sweep's instant (`sales.abandon_stale_visits` passes the beat's),
    but `abandon` and `close` each read the module clock — so a visit selected as "6 h
    stale at 16:25" was ended at whatever time the write happened to land, and `ended_at`
    stopped being evidence of the window that chose the row. The ORDERED leg matters more,
    not less: `close` stamps `outlet.last_visit_at` and feeds `recompute_outlet(now=...)`
    from the same read, so the outlet's next-visit date was computed off a third instant.
    No frozen datetime here on purpose: the other sweep tests patch the module, which is
    what made the two clocks look like one.
    """
    from tests.unit.test_sales_agent_role import make_sales_agent_user

    moment = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0)
    visit = Visit(
        outlet_id=outlet.id,
        agent_user_id=sales_agent_user.id,
        status="in_progress",
        planned=False,
        current_step="checkin",
        started_at=moment - timedelta(hours=7),
    )
    # The other leg: one open visit per agent, so the ordered one belongs to a second.
    second_agent = make_sales_agent_user(db, phone="+998901234581")
    ordered = Visit(
        outlet_id=outlet.id,
        agent_user_id=second_agent.id,
        status="in_progress",
        planned=False,
        current_step="order",
        started_at=moment - timedelta(hours=7),
    )
    db.session.add_all([visit, ordered])
    db.session.commit()
    db.session.add(
        Order(
            user_id=sample_user.id,
            order_number="SA_000126_26",
            status=OrderStatus.CONFIRMED,
            subtotal=Decimal("120000.00"),
            total_amount=Decimal("120000.00"),
            visit_id=ordered.id,
        )
    )
    outlet.agent_next_visit_at = date(2026, 9, 20)
    db.session.commit()

    swept = VisitService.abandon_stale(now=moment)

    assert swept == 2
    abandoned = Visit.query.get(visit.id)
    assert abandoned.status == "abandoned"
    assert ensure_utc(abandoned.ended_at) == moment
    closed = Visit.query.get(ordered.id)
    assert (closed.status, closed.outcome) == ("completed", "order_placed")
    assert ensure_utc(closed.ended_at) == moment
    # `close` stamps the outlet from the same read, so the whole sweep is one instant.
    assert ensure_utc(Outlet.query.get(outlet.id).last_visit_at) == moment


def test_serialize_order_placed_answers_none_where_the_brief_does():
    """`serialize_order_placed` is built ON `serialize_order_brief` so the two cannot drift.

    The spread of it was the one place they did: the brief documents and returns None for
    "no order", and `{**None, ...}` is `TypeError: argument of type 'NoneType' is not a
    mapping`, not a serializer answer. Same contract on both sides — no order, no dict.
    """
    assert serialize_order_brief(None) is None
    assert serialize_order_placed(None) is None


def test_serialize_outlet_agent_row_rounds_the_distance_to_whole_metres(db, outlet):
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    measured = serialize_outlet_agent_row(outlet, open_visit_id=None, now=now, distance_m=54.7)
    unmeasured = serialize_outlet_agent_row(outlet, open_visit_id=None, now=now)

    # Whole metres, like `find_duplicates`' candidates: "54.7 m" is precision nobody has.
    assert measured["distance_m"] == 55
    # Present and null off the nearby list -- the key never disappears, so the bot's one row
    # renderer never has to know which scope drew it.
    assert "distance_m" in unmeasured and unmeasured["distance_m"] is None


def test_serialize_visit_photo_publishes_the_duplicate_flag(db, outlet, sales_agent_user):
    """`is_duplicate` is PUBLISHED, not left to the reader.

    "Has this agent already sent these exact bytes" is a rule the backend answered when it
    compared the hashes; a bot re-deriving it from `duplicate_of_photo_id` would be a second
    expression of it, in the client, one release behind.
    """
    visit = VisitService.start(sales_agent_user.id, outlet.id)
    received = datetime(2026, 9, 15, 6, 30, tzinfo=UTC)
    original = VisitPhoto(
        visit_id=visit.id, kind="storefront", telegram_file_id="AgAC-a",
        sha256="a" * 64, telegram_file_unique_id="AgADBAAD", received_at=received,
    )
    db.session.add(original)
    db.session.commit()
    copy = VisitPhoto(
        visit_id=visit.id, kind="shelf", telegram_file_id="AgAC-b",
        sha256="a" * 64, received_at=received, duplicate_of_photo_id=original.id,
    )
    db.session.add(copy)
    db.session.commit()

    first = serialize_visit_photo(original)
    second = serialize_visit_photo(copy)

    assert set(first) == VISIT_PHOTO_KEYS
    assert first["is_duplicate"] is False and first["duplicate_of_photo_id"] is None
    assert first["telegram_file_unique_id"] == "AgADBAAD"
    # `_iso`, not a raw isoformat: SQLite drops the tzinfo PostgreSQL keeps, and one field
    # with two wire formats is a parser the bot only ever exercises against the naive one.
    assert first["received_at"] == "2026-09-15T06:30:00+00:00"
    assert first["kind"] == "storefront" and "telegram_file_id" not in first
    assert second["is_duplicate"] is True and second["duplicate_of_photo_id"] == original.id
    assert second["kind"] == "shelf" and second["sha256"] == "a" * 64
