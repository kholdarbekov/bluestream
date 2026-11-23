"""The duplicate-address guard's rules (spec 2026-11-18 §6.1, §6.3, §6.5): the pair predicate, the candidates, the
pin-changed test, the four pin rules, their batched answer, and the 409.

Addresses are inserted through the ORM and orders seeded the way production leaves them (`_seed`). Every point is
a real offset around one base pin (`address_guard_geometry`), and every radius is read from `app.config`, so these
tests never restate a rule: they ask the real predicate and the real pin rules.
"""

import logging
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from business_app import db
from business_app.models.bottle import BottleBalance
from business_app.models.order import Order
from business_app.models.user import UserAddress
from business_app.services.address_guard_service import (
    as_point,
    assert_not_duplicate,
    assert_pin_movable,
    is_placed_pin,
    is_possible_duplicate_pair,
    pin_changed,
    pin_move_refusal,
    pin_move_refusals,
    possible_duplicates,
    take_confirm_flag,
    take_point_source,
    within_duplicate_reach,
)
from business_app.utils.exceptions import ConflictError, ValidationError
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from shared.constants import TASHKENT_COORDINATES, TASHKENT_DISTRICTS
from shared.enums import DeliveryStatus as S
from shared.enums import OrderStatus
from tests.integration.address_guard_geometry import BASE, at
from tests.integration.reorder_reminder_builders import captured_logs
from tests.integration.sales_pay_builders import make_outlet
from tests.integration.test_admin_order_reschedule import _seed, rostered_driver  # noqa: F401  fixture
from tests.unit._scope_money_helpers import make_place_group

pytestmark = pytest.mark.integration

GUARD_LOGGER = logging.getLogger(assert_not_duplicate.__module__)
CITY_CENTRE = (TASHKENT_COORDINATES["latitude"], TASHKENT_COORDINATES["longitude"])
CHILANZAR = TASHKENT_DISTRICTS["chilanzar"]["center"]
YUNUSABAD = TASHKENT_DISTRICTS["yunusabad"]["center"]
NO_POINT = (None, None)


def _saved(user, point, **columns) -> UserAddress:
    """One saved address of `user` at `point` ((lat, lng), or None for no pin), inserted as the write paths do."""
    latitude, longitude = point if point is not None else NO_POINT
    address = UserAddress(
        user_id=user.id,
        full_address="Chilonzor 12-kv, 5-uy",
        city="Tashkent",
        latitude=latitude,
        longitude=longitude,
        **columns,
    )
    db.session.add(address)
    db.session.commit()
    return address


def _held_by_an_outlet(address, user):
    outlet = make_outlet(db, onboarded_by=None, created_at=datetime.now(UTC), user=user)
    outlet.address_id = address.id
    db.session.commit()


@pytest.fixture
def world(app, db, sample_user, second_sample_user, rostered_driver, local_noon):  # noqa: F811
    """The customer, a coworker, the rostered driver, local today and the move cap, for the pin-rule set-ups."""
    return SimpleNamespace(
        me=sample_user,
        other=second_sample_user,
        driver=rostered_driver,
        today=local_noon.date(),
        cap=app.config["ADDRESS_MOVE_MAX_M"],
    )


# ---------------------------------------------------------------------------
# The pair predicate and the candidates
# ---------------------------------------------------------------------------


def test_a_pair_inside_the_configured_radius_is_a_duplicate_and_one_outside_is_not(app):
    radius = app.config["ADDRESS_DUPLICATE_RADIUS_M"]
    base = as_point(*BASE)

    assert is_possible_duplicate_pair(base, as_point(*at(radius - 2))) is True
    assert is_possible_duplicate_pair(base, as_point(*at(east_m=radius - 2))) is True
    assert is_possible_duplicate_pair(base, as_point(*at(radius + 2))) is False


@pytest.mark.parametrize(
    "north_m,east_m",
    [(1, 0), (0, 1), (-1, 0), (0, -1), (0.7, 0.7), (-0.7, -0.7)],
    ids=["north", "east", "south", "west", "north-east", "south-west"],
)
def test_the_reach_pre_filter_never_drops_a_pair_the_rule_accepts(app, north_m, east_m):
    """`within_duplicate_reach` is the cheap box the suggestions read asks before the rule: every pair the rule
    accepts, out to the configured radius in any direction, is inside it; a pair far beyond the radius is not."""
    radius = app.config["ADDRESS_DUPLICATE_RADIUS_M"]
    base = as_point(*BASE)
    edge = as_point(*at(north_m * (radius - 2), east_m * (radius - 2)))

    assert is_possible_duplicate_pair(base, edge) is True
    assert within_duplicate_reach(base, edge) is True
    assert within_duplicate_reach(base, as_point(*at(north_m * radius * 3, east_m * radius * 3))) is False


def test_known_different_apartments_and_fallback_points_are_never_pairs(app):
    near = at(5)

    assert is_possible_duplicate_pair(as_point(*BASE, "5"), as_point(*near, "7")) is False
    assert is_possible_duplicate_pair(as_point(*BASE, " 5a "), as_point(*near, "5A")) is True
    assert is_possible_duplicate_pair(as_point(*BASE, "5"), as_point(*near)) is True  # one unknown apartment asks
    assert is_possible_duplicate_pair(as_point(*CHILANZAR), as_point(*CHILANZAR)) is False
    assert is_possible_duplicate_pair(as_point(*CITY_CENTRE), as_point(*at(5, origin=CITY_CENTRE))) is False


def test_candidates_are_the_customers_own_nearest_three(db, sample_user, second_sample_user):
    thirty, five, ten, twenty = (
        _saved(sample_user, point) for point in (at(30), at(east_m=5), at(-10), at(east_m=-20))
    )
    _saved(second_sample_user, at(1))

    found = possible_duplicates(sample_user.id, *BASE)

    assert [d for _, d in found] == [5, 10, 20]
    assert [a.id for a, _ in found] == [five.id, ten.id, twenty.id]
    assert possible_duplicates(sample_user.id, *map(str, BASE)) == found
    assert [a.id for a, _ in possible_duplicates(sample_user.id, *BASE, exclude_address_id=five.id)] == [
        ten.id,
        twenty.id,
        thirty.id,
    ]


def test_the_new_points_apartment_is_compared_with_each_saved_one(db, sample_user):
    flat_5 = _saved(sample_user, at(5), apartment_number="5")

    assert possible_duplicates(sample_user.id, *BASE, apartment_number="7") == []
    assert [a.id for a, _ in possible_duplicates(sample_user.id, *BASE, apartment_number=" 5 ")] == [flat_5.id]


def test_pinless_rows_are_never_candidates(db, sample_user):
    _saved(sample_user, None)
    _saved(sample_user, (BASE[0], None))

    assert possible_duplicates(sample_user.id, *BASE) == []

    _saved(sample_user, BASE)
    assert possible_duplicates(sample_user.id, *NO_POINT) == []


# ---------------------------------------------------------------------------
# Has the pin changed? Is it a pin the customer placed?
# ---------------------------------------------------------------------------


def test_pin_changed_reads_numeric_strings_and_half_pairs(db, user_address):
    assert pin_changed(user_address, {"latitude": "41.3111", "longitude": "69.2797"}) is False
    assert pin_changed(user_address, {"latitude": "41.3121", "longitude": "69.2797"}) is True
    assert pin_changed(user_address, {"latitude": 41.3111 + 0.0002}) is True  # half pair read against stored lng
    assert pin_changed(user_address, {"latitude": None, "longitude": None}) is False
    assert pin_changed(user_address, {"latitude": "north", "longitude": "69.2797"}) is False
    assert pin_changed(user_address, {"latitude": "nan", "longitude": "69.2797"}) is False
    assert pin_changed(user_address, {"title": "x"}) is False


@pytest.mark.parametrize(
    "latitude,longitude",
    [(200, BASE[1]), (-90.5, BASE[1]), (BASE[0], 180.5), (BASE[0], -400), ("1e3", BASE[1])],
    ids=["latitude-200", "latitude-below-south-pole", "longitude-past-180", "longitude-minus-400", "string-1e3"],
)
def test_an_out_of_range_coordinate_is_no_point(db, user_address, latitude, longitude):
    """A latitude outside [-90, 90] or a longitude outside [-180, 180] is not a place on Earth: like a missing or
    non-numeric value it changes no pin, pairs with nothing and is no placed pin (geopy would raise on it)."""
    assert as_point(latitude, longitude) is None
    assert pin_changed(user_address, {"latitude": latitude, "longitude": longitude}) is False
    assert possible_duplicates(user_address.user_id, latitude, longitude) == []
    assert is_placed_pin("map_pin", latitude, longitude) is False


def test_the_poles_and_the_antimeridian_are_points():
    assert as_point(90, 180) == as_point(90.0, 180.0) is not None
    assert as_point(-90, -180) is not None


def test_one_metre_is_the_pin_changed_tolerance(db, user_address):
    assert pin_changed(user_address, dict(zip(("latitude", "longitude"), at(0.9)))) is False
    assert pin_changed(user_address, dict(zip(("latitude", "longitude"), at(1.2)))) is True


def test_any_point_is_a_change_for_an_address_without_one(db, sample_user):
    pinless = _saved(sample_user, None)

    assert pin_changed(pinless, {"latitude": BASE[0], "longitude": BASE[1]}) is True
    assert pin_changed(pinless, {"latitude": BASE[0]}) is False  # half a pair over no stored half is no point


@pytest.mark.parametrize(
    "source,point,placed",
    [
        ("map_pin", at(5), True),
        ("gps", at(5), True),
        ("search", at(5), False),
        ("typed", at(5), False),
        (None, at(5), False),
        ("bogus", at(5), False),
        (["map_pin"], at(5), False),
        ("map_pin", CHILANZAR, False),
        ("gps", CITY_CENTRE, False),
        ("map_pin", NO_POINT, False),
    ],
)
def test_only_a_real_point_the_customer_placed_is_a_placed_pin(source, point, placed):
    assert is_placed_pin(source, *point) is placed


# ---------------------------------------------------------------------------
# The pin rules
# ---------------------------------------------------------------------------


def _picked_up(w):
    address = _saved(w.me, BASE)
    _seed(address, w.driver, S.PICKED_UP, w.today)
    return address, NO_POINT


def _accepted(w):
    address = _saved(w.me, BASE)
    _seed(address, w.driver, S.ASSIGNED, w.today)
    return address, NO_POINT


def _sent_out_by_hand(w):
    """An admin set the order out for delivery without a pickup: the driver has it (OrderService's predicate)."""
    address = _saved(w.me, BASE)
    _seed(address, w.driver, S.SCHEDULED, w.today, order_status=OrderStatus.OUT_FOR_DELIVERY)
    return address, NO_POINT


def _pinless_picked_up(w):
    """No stored pin under the driver: its first pin moves nothing, and is never too far."""
    address = _saved(w.me, None)
    _seed(address, w.driver, S.PICKED_UP, w.today)
    return address, at(2000)


def _shared_with_a_coworker(w):
    address = _saved(w.me, BASE)
    make_place_group(db, address, _saved(w.other, at(3)))
    return address, NO_POINT


def _grouped_with_an_own_address(w):
    address = _saved(w.me, BASE)
    make_place_group(db, address, _saved(w.me, at(3)))
    return address, NO_POINT


def _branch(w):
    address = _saved(w.me, BASE)
    _held_by_an_outlet(address, w.me)
    return address, NO_POINT


def _delivered_to(w, origin=BASE):
    address = _saved(w.me, origin)
    _seed(address, w.driver, S.DELIVERED, w.today, order_status=OrderStatus.DELIVERED)
    return address


def _past_the_cap(w):
    return _delivered_to(w), at(w.cap + 2)


def _inside_the_cap(w):
    return _delivered_to(w), at(w.cap - 2)


def _fallback_pin_far(w):
    return _delivered_to(w, YUNUSABAD), at(3000, origin=YUNUSABAD)


def _no_history_far(w):
    return _saved(w.me, BASE), at(2000)


def _bottles_here_past_the_cap(w):
    address = _saved(w.me, BASE)
    db.session.add(BottleBalance(address_id=address.id, balance=Decimal("2")))
    db.session.commit()
    return address, at(w.cap + 2)


def _bottles_at_its_place_past_the_cap(w):
    address = _saved(w.me, BASE)
    group = make_place_group(db, address, _saved(w.me, at(3)))
    db.session.add(BottleBalance(address_group_id=group.id, balance=Decimal("2")))
    db.session.commit()
    return address, at(w.cap + 2)


def _every_rule(w):
    address = _saved(w.me, BASE)
    _seed(address, w.driver, S.PICKED_UP, w.today)
    make_place_group(db, address, _saved(w.other, at(3)))
    _held_by_an_outlet(address, w.me)
    return address, at(w.cap + 2)


def _shared_branch_past_the_cap(w):
    address = _delivered_to(w)
    make_place_group(db, address, _saved(w.other, at(3)))
    _held_by_an_outlet(address, w.me)
    return address, at(w.cap + 2)


def _branch_past_the_cap(w):
    address = _delivered_to(w)
    _held_by_an_outlet(address, w.me)
    return address, at(w.cap + 2)


@pytest.mark.parametrize(
    "build,expected",
    [
        pytest.param(_picked_up, "WITH_DRIVER", id="picked-up order"),
        pytest.param(_sent_out_by_hand, "WITH_DRIVER", id="order set out for delivery by hand"),
        pytest.param(_accepted, None, id="stop only accepted"),
        pytest.param(_pinless_picked_up, None, id="first pin under a picked-up order"),
        pytest.param(_shared_with_a_coworker, "SHARED_PLACE", id="place shared with another customer"),
        pytest.param(_grouped_with_an_own_address, None, id="place of own addresses only"),
        pytest.param(_branch, "OUTLET", id="outlet-held"),
        pytest.param(_past_the_cap, "TOO_FAR", id="order history, real pin, past the cap"),
        pytest.param(_inside_the_cap, None, id="order history, real pin, inside the cap"),
        pytest.param(_bottles_here_past_the_cap, "TOO_FAR", id="bottles at the address, past the cap"),
        pytest.param(_bottles_at_its_place_past_the_cap, "TOO_FAR", id="bottles at its place, past the cap"),
        pytest.param(_fallback_pin_far, None, id="district-centre pin with history, 3 km"),
        pytest.param(_no_history_far, None, id="no history, 2 km"),
        pytest.param(_every_rule, "WITH_DRIVER", id="with the driver comes first"),
        pytest.param(_shared_branch_past_the_cap, "SHARED_PLACE", id="shared place before outlet and cap"),
        pytest.param(_branch_past_the_cap, "OUTLET", id="outlet before the cap"),
    ],
)
def test_each_pin_rule_answers_its_reason(world, build, expected):
    address, point = build(world)

    assert pin_move_refusal(address, *point) == expected


# reason -> (a set-up refused for it, its error code, its message key)
REFUSALS = {
    "WITH_DRIVER": (_picked_up, "ADDRESS_PIN_WITH_DRIVER", "api.addresses.error.pin_with_driver"),
    "SHARED_PLACE": (_shared_with_a_coworker, "ADDRESS_PIN_SHARED_PLACE", "api.addresses.error.pin_shared_place"),
    "OUTLET": (_branch, "ADDRESS_PIN_OUTLET", "api.addresses.error.pin_outlet"),
    "TOO_FAR": (_past_the_cap, "ADDRESS_MOVE_TOO_FAR", "api.addresses.error.move_too_far"),
}


@pytest.mark.parametrize("reason", list(REFUSALS))
def test_a_refused_pin_change_raises_its_code_and_logs_it(world, caplog, reason):
    build, code, key = REFUSALS[reason]
    address, point = build(world)

    with captured_logs(caplog, GUARD_LOGGER, logging.INFO), pytest.raises(ValidationError) as refused:
        assert_pin_movable(address, *point, actor_id=world.me.id)

    order_number = (
        {"order_number": Order.query.filter_by(delivery_address_id=address.id).one().order_number}
        if reason == "WITH_DRIVER"
        else {}
    )
    assert type(refused.value) is ValidationError  # ExceptionMapper maps by exact type: a subclass is a 500
    assert (refused.value.error_code, refused.value.details, refused.value.message) == (
        code,
        {"reason": reason, **order_number},
        BACKEND_TRANSLATIONS[key]["en"],
    )
    assert f"[ADDRESS_PIN] refused user={world.me.id} address={address.id} reason={reason}" in caplog.messages


@pytest.mark.parametrize("build", [_accepted, _inside_the_cap, _grouped_with_an_own_address])
def test_a_movable_pin_change_raises_nothing(world, build):
    address, point = build(world)

    assert assert_pin_movable(address, *point, actor_id=world.me.id) is None


def test_the_batched_answer_is_the_single_answer_for_every_address(world):
    w = world
    free = _saved(w.me, BASE)
    delivered = _delivered_to(w, at(200))
    with_driver = _saved(w.me, at(400))
    _seed(with_driver, w.driver, S.PICKED_UP, w.today)
    pinless_with_driver = _saved(w.me, None)
    _seed(pinless_with_driver, w.driver, S.SCHEDULED, w.today, order_status=OrderStatus.OUT_FOR_DELIVERY)
    open_order = _saved(w.me, at(600))
    _seed(open_order, w.driver, S.SCHEDULED, w.today)
    shared = _saved(w.me, at(800))
    make_place_group(db, shared, _saved(w.other, at(803)))
    own_place = [_saved(w.me, at(1000)), _saved(w.me, at(1003))]
    make_place_group(db, *own_place)
    branch = _saved(w.me, at(1200))
    _held_by_an_outlet(branch, w.me)
    addresses = UserAddress.query.filter_by(user_id=w.me.id).all()

    batched = pin_move_refusals(w.me.id)

    assert batched == {a.id: pin_move_refusal(a) for a in addresses}
    assert batched == {
        free.id: None,
        delivered.id: None,
        with_driver.id: "WITH_DRIVER",
        pinless_with_driver.id: None,
        open_order.id: None,
        shared.id: "SHARED_PLACE",
        own_place[0].id: None,
        own_place[1].id: None,
        branch.id: "OUTLET",
    }


def test_the_batched_answer_reads_each_fact_once_for_any_number_of_addresses(world, count_queries):
    w = world

    def queries():
        with count_queries() as counter:
            pin_move_refusals(w.me.id)
        return counter.count

    first = _saved(w.me, BASE)
    _seed(first, w.driver, S.SCHEDULED, w.today)
    make_place_group(db, first, _saved(w.me, at(3)))
    _held_by_an_outlet(first, w.me)
    few = queries()
    for metres in (300, 600, 900):
        more = _saved(w.me, at(metres))
        _seed(more, w.driver, S.SCHEDULED, w.today)
        make_place_group(db, more, _saved(w.other, at(metres + 3)))

    assert queries() == few


# ---------------------------------------------------------------------------
# The 409 and the confirm flag
# ---------------------------------------------------------------------------


def test_the_409_names_the_candidates_and_offers_move_only_for_a_movable_nearest(db, sample_user):
    uy = _saved(sample_user, BASE, title="Uy")
    near = at(14)

    with pytest.raises(ConflictError) as refused:
        assert_not_duplicate(sample_user.id, *near, confirmed=False, offer_move=True)

    assert type(refused.value) is ConflictError
    assert refused.value.error_code == "ADDRESS_POSSIBLE_DUPLICATE"
    assert refused.value.message == BACKEND_TRANSLATIONS["api.addresses.error.possible_duplicate"]["en"]
    assert refused.value.details == {"move_address_id": uy.id, "candidates": [{**uy.to_dict(), "distance_m": 14}]}

    with pytest.raises(ConflictError) as without_move:
        assert_not_duplicate(sample_user.id, *near, confirmed=False)
    assert without_move.value.details["move_address_id"] is None

    ish = _saved(sample_user, at(40), title="Ish")
    _held_by_an_outlet(uy, sample_user)
    with pytest.raises(ConflictError) as nearest_held:
        assert_not_duplicate(sample_user.id, *near, confirmed=False, offer_move=True)
    assert nearest_held.value.details == {
        "move_address_id": None,
        "candidates": [{**uy.to_dict(), "distance_m": 14}, {**ish.to_dict(), "distance_m": 26}],
    }


def test_only_true_confirms_and_logs_the_ids(caplog, db, sample_user):
    uy = _saved(sample_user, BASE, title="Uy")
    ish = _saved(sample_user, at(20), title="Ish")
    for not_true in ("true", 1, False, None):
        with pytest.raises(ConflictError):
            assert_not_duplicate(sample_user.id, *at(14), confirmed=not_true)

    with captured_logs(caplog, GUARD_LOGGER, logging.INFO):
        assert assert_not_duplicate(sample_user.id, *at(14), confirmed=True) is None
        assert assert_not_duplicate(sample_user.id, *at(500), confirmed=False) is None  # nobody near: no question

    assert [m for m in caplog.messages if m.startswith("[ADDRESS_DUP]")] == [
        f"[ADDRESS_DUP] confirmed user={sample_user.id} near=[{ish.id},{uy.id}]"
    ]


def test_take_confirm_flag_accepts_only_json_true():
    assert [take_confirm_flag({"confirm_duplicate": v}) for v in (True, "true", "false", 1, None)] == [
        True,
        False,
        False,
        False,
        False,
    ]
    body = {"title": "Uy", "confirm_duplicate": True}
    assert take_confirm_flag(body) is True and body == {"title": "Uy"}
    assert take_confirm_flag({}) is False


def test_take_point_source_pops_it_and_keeps_only_a_string():
    body = {"title": "Uy", "point_source": "map_pin"}
    assert take_point_source(body) == "map_pin" and body == {"title": "Uy"}
    assert take_point_source({"point_source": ["gps"]}) is None
    assert take_point_source({}) is None
