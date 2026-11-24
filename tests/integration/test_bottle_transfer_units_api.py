"""Transfers carry the one-way unit lines (spec 2026-11-18 §6.7 "Sending" and "Counting").

The sender types full bottles, empties, then each 10 L line (`units`, one `{product_id, quantity}` per product), or
taps Send all, which sends every line's transferable. Each line is capped at what it holds on board
(BOTTLE_TRANSFER_EXCEEDS_INVENTORY, `kind = "product"`), a total of 0 over every line is refused, and the sender is
debited at once. The transfer keeps one unit line for every line on the sender's trip, declared 0 when nothing of it
was sent, and publishes every line (`lines`: full, empty, then each product) to the receiver's inbox and push.

The receiver taps Confirm (every line as declared) or enters other numbers: both 18.9 L counts and one count per line
the transfer carries, never fewer or more (BOTTLE_TRANSFER_COUNT_INCOMPLETE). Every line matching confirms it; any line
differing disputes it, and the admins' Slack alert prints every line. The landing trip gets each line's count, and a
line it lacks is made on it. Only the transfer's lines decide what is counted: a product ticked after the send asks
nothing.

Driven through the routes the staff bot and the admin pages call, with the bodies they send. Only the outbound calls
are recorded: the Celery publish (`enqueued`) and the Slack post (`slack`); the bot's error resolver is the real one,
fed the real seeded copy.
"""

from datetime import datetime

import pytest

from business_app.models.bottle import (
    DriverBottleSession,
    DriverBottleSessionUnit,
    DriverBottleTransfer,
    DriverBottleTransferUnit,
    DriverSessionMembership,
)
from business_app.models.product import Product
from shared.enums import (
    DriverBottleSessionStatus,
    DriverBottleTransferStatus,
    DriverSessionMembershipStatus,
)
from staff_bot.handlers.base import BaseHandler
from tests.integration.test_admin_order_edit import _attach_returnable_address, _seed_paid_cash_delivered_order
from tests.integration.test_bottle_transfer_split_api import _bot_body, seeded_copy  # noqa: F401 -- a fixture
from tests.integration.test_redispatch_reschedules_to_today import enqueued  # noqa: F401 -- a fixture
from tests.integration.test_transfer_auto_close_and_alerts import TASHKENT, _dispute_alert, _posted_alert
from tests.integration.trip_unit_builders import (  # noqa: F401  (fixtures)
    big,
    bind,
    owner,
    pushes,
    receiver,
    sender,
    slack,
    small,
    staff_headers,
    ten_litre,
    trip,
)

pytestmark = pytest.mark.integration

TRANSFERS = "/api/v1/staff/bottles/transfers"
PENDING = TRANSFERS + "/pending"
CONFIRM = TRANSFERS + "/{}/confirm"
CLOSE = "/api/v1/staff/bottles/session/close"
CURRENT = "/api/v1/staff/bottles/session/current"
ADMIN_PRODUCT = "/api/v1/admin/products/{}"
ORDER_EDIT = "/api/v1/admin/orders/{}/edit"


def _send(client, giver, taker, *, full=0, empty=0, units=None):
    """The staff bot's send body: both numbers, then one `{product_id, quantity}` per 10 L line."""
    body = {"receiver_driver_id": taker.id, "full_quantity": full, "empty_quantity": empty}
    if units is not None:
        body["units"] = [{"product_id": product_id, "quantity": quantity} for product_id, quantity in units.items()]
    return client.post(TRANSFERS, json=body, headers=staff_headers(giver))


def _send_all(client, giver, taker):
    """The staff bot's Send all."""
    return client.post(TRANSFERS, json={"receiver_driver_id": taker.id, "send_all": True}, headers=staff_headers(giver))


def _line_of(db, session_id, product_id):
    db.session.expire_all()
    return DriverBottleSessionUnit.query.filter_by(session_id=session_id, product_id=product_id).one()


def _moved_out(db, session_id):
    db.session.expire_all()
    session = db.session.get(DriverBottleSession, session_id)
    return session.bottles_transferred_out, session.empties_transferred_out


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #


def test_a_send_with_ten_litre_is_capped_by_its_line(client, db, sender, receiver, small, enqueued):  # noqa: F811
    s = trip(db, sender, units={small.id: 6})

    refused = _send(client, sender, receiver, units={small.id: 7})

    assert refused.status_code == 400, refused.get_data(as_text=True)
    body = refused.get_json()
    assert (body["error_code"], body["details"]) == (
        "BOTTLE_TRANSFER_EXCEEDS_INVENTORY",
        {
            "kind": "product",
            "product_id": small.id,
            "product_name": "10 Litr suv",
            "requested": 7,
            "available": 6,
            "validation_errors": [],
        },
    )
    assert _line_of(db, s.id, small.id).transferred_out == 0
    assert DriverBottleTransfer.query.count() == 0
    assert pushes(enqueued) == []


def test_the_bot_words_a_capped_ten_litre_line_by_its_product(
    client, db, sender, receiver, small, seeded_copy  # noqa: F811
):
    """The product refusal also carries `requested` and `available`, so its own sentence must be tried before the
    generic one."""
    seeded_copy(
        "staff.error.api.transfer_exceeds_unit_detail",
        "staff.error.api.transfer_exceeds_full_detail",
        "staff.error.api.transfer_exceeds_empty_detail",
        "staff.error.api.transfer_exceeds_inventory_detail",
        "staff.error.api.transfer_exceeds_inventory",
    )
    trip(db, sender, units={small.id: 6})

    refused = _send(client, sender, receiver, units={small.id: 7}).get_json()

    sentence = BaseHandler.__new__(BaseHandler)._resolve_api_error_message(
        "en", refused.get("message"), 400, refused["error_code"], details=refused["details"]
    )
    assert sentence == "10 Litr suv: you have only 6 on your trip."


def test_a_ten_litre_only_send_debits_the_line(client, db, sender, receiver, small, enqueued):  # noqa: F811
    """Aziz holds only 10 L: he can send it."""
    s = trip(db, sender, units={small.id: 6})

    sent = _send(client, sender, receiver, units={small.id: 6})

    assert sent.status_code == 201, sent.get_data(as_text=True)
    data = sent.get_json()["data"]
    assert data["units"] == [
        {"product_id": small.id, "product_name": "10 Litr suv", "declared": 6, "confirmed": None, "resolved": None}
    ]
    assert (data["declared_full"], data["declared_empty"], data["status"]) == (0, 0, "pending")
    line = _line_of(db, s.id, small.id)
    assert (line.transferred_out, line.on_board, line.transferable) == (6, 0, 0)
    assert _moved_out(db, s.id) == (0, 0)


def test_a_send_writes_a_zero_line_for_each_counted_product_not_sent(
    client, db, sender, receiver, small, enqueued  # noqa: F811
):
    """Aziz sends 18.9 L only; his trip counts 10 L, so the transfer still carries its line, declared 0."""
    s = trip(db, sender, bottles=10, units={small.id: 3}, collected=4)

    sent = _send(client, sender, receiver, full=10, empty=4)

    assert sent.status_code == 201, sent.get_data(as_text=True)
    data = sent.get_json()["data"]
    assert data["units"] == [
        {"product_id": small.id, "product_name": "10 Litr suv", "declared": 0, "confirmed": None, "resolved": None}
    ]
    assert data["lines"] == [
        {"kind": "full", "product_id": None, "product_name": None, "declared": 10, "confirmed": None, "resolved": None},
        {"kind": "empty", "product_id": None, "product_name": None, "declared": 4, "confirmed": None, "resolved": None},
        {
            "kind": "product",
            "product_id": small.id,
            "product_name": "10 Litr suv",
            "declared": 0,
            "confirmed": None,
            "resolved": None,
        },
    ]
    assert _line_of(db, s.id, small.id).transferred_out == 0
    assert _moved_out(db, s.id) == (10, 4)


def test_send_all_includes_every_line(client, db, sender, receiver, small, enqueued):  # noqa: F811
    s = trip(db, sender, bottles=5, units={small.id: 6}, collected=2)

    sent = _send_all(client, sender, receiver)

    assert sent.status_code == 201, sent.get_data(as_text=True)
    data = sent.get_json()["data"]
    assert data["lines"] == [
        {"kind": "full", "product_id": None, "product_name": None, "declared": 5, "confirmed": None, "resolved": None},
        {"kind": "empty", "product_id": None, "product_name": None, "declared": 2, "confirmed": None, "resolved": None},
        {
            "kind": "product",
            "product_id": small.id,
            "product_name": "10 Litr suv",
            "declared": 6,
            "confirmed": None,
            "resolved": None,
        },
    ]
    assert _line_of(db, s.id, small.id).transferred_out == 6
    assert _moved_out(db, s.id) == (5, 2)


def test_send_all_takes_a_ten_litre_line_alone(client, db, sender, receiver, small, enqueued):  # noqa: F811
    """No 18.9 L on board: Send all still sends the 10 L."""
    s = trip(db, sender, units={small.id: 4})

    sent = _send_all(client, sender, receiver)

    assert sent.status_code == 201, sent.get_data(as_text=True)
    assert [line["declared"] for line in sent.get_json()["data"]["lines"]] == [0, 0, 4]
    assert _line_of(db, s.id, small.id).transferred_out == 4


@pytest.mark.parametrize("send_all", [False, True], ids=["chose-zero-on-every-line", "send-all-on-an-empty-line"])
def test_nothing_to_send_sums_every_line(client, db, sender, receiver, small, enqueued, send_all):  # noqa: F811
    """Aziz has only a 10 L line. He chooses 0 on every line; or he taps Send all once all 3 are delivered."""
    s = trip(db, sender, units={small.id: 3})
    if send_all:
        _line_of(db, s.id, small.id).delivered = 3
        db.session.commit()
        refused = _send_all(client, sender, receiver)
    else:
        refused = _send(client, sender, receiver, units={small.id: 0})

    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "BOTTLE_TRANSFER_NOTHING_TO_SEND"
    assert _line_of(db, s.id, small.id).transferred_out == 0
    assert DriverBottleTransfer.query.count() == 0
    assert pushes(enqueued) == []


def test_a_product_the_trip_does_not_count_is_refused(client, db, sender, receiver, small, enqueued):  # noqa: F811
    """Aziz's trip opened before 10 L was counted: it has no line, so he cannot send any."""
    trip(db, sender, bottles=5)

    refused = _send(client, sender, receiver, full=2, units={small.id: 1})

    assert refused.status_code == 400, refused.get_data(as_text=True)
    body = refused.get_json()
    assert (body["error_code"], body["details"]) == (
        "TRIP_UNIT_NOT_COUNTED",
        {"product_id": small.id, "validation_errors": []},
    )
    assert DriverBottleTransfer.query.count() == 0
    assert pushes(enqueued) == []


@pytest.mark.parametrize("case", ["send-all-with-units", "a-product-twice"])
def test_a_send_body_with_units_two_ways_or_twice_is_refused(
    client, db, sender, receiver, small, enqueued, case  # noqa: F811
):
    s = trip(db, sender, units={small.id: 6})
    one = {"product_id": small.id, "quantity": 1}
    body, error = {
        "send-all-with-units": (
            {"send_all": True, "units": [one]},
            "sendAll sends everything; give no amounts with it",
        ),
        "a-product-twice": (
            {"units": [one, {**one, "quantity": 2}]},
            f"units: Product {small.id} is listed twice; give each product once.",
        ),
    }[case]

    refused = client.post(TRANSFERS, json={"receiver_driver_id": receiver.id, **body}, headers=staff_headers(sender))

    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert refused.get_json()["errors"] == [error]
    assert _line_of(db, s.id, small.id).transferred_out == 0
    assert DriverBottleTransfer.query.count() == 0
    assert pushes(enqueued) == []


# --------------------------------------------------------------------------- #
# What the receiver reads
# --------------------------------------------------------------------------- #


def test_the_pending_list_and_the_push_carry_every_line(client, db, sender, receiver, small, enqueued):  # noqa: F811
    trip(db, sender, bottles=10, units={small.id: 6}, collected=4)
    sent = _send(client, sender, receiver, full=10, empty=4, units={small.id: 6})
    assert sent.status_code == 201, sent.get_data(as_text=True)
    transfer_id = sent.get_json()["data"]["id"]

    pending = client.get(PENDING, headers=staff_headers(receiver))

    assert pending.status_code == 200, pending.get_data(as_text=True)
    [row] = pending.get_json()["data"]
    every_line = [
        {"kind": "full", "product_id": None, "product_name": None, "declared": 10, "confirmed": None, "resolved": None},
        {"kind": "empty", "product_id": None, "product_name": None, "declared": 4, "confirmed": None, "resolved": None},
        {
            "kind": "product",
            "product_id": small.id,
            "product_name": "10 Litr suv",
            "declared": 6,
            "confirmed": None,
            "resolved": None,
        },
    ]
    assert (row["id"], row["lines"]) == (transfer_id, every_line)
    assert pushes(enqueued) == [
        (
            int(receiver.telegram_id),
            "transfer_received",
            {
                "id": transfer_id,
                "declared_quantity": 10,
                "declared_empty_quantity": 4,
                "sender_name": "Aziz",
                "lines": every_line,
            },
        )
    ]


# --------------------------------------------------------------------------- #
# Counting
# --------------------------------------------------------------------------- #


def _sent(client, giver, taker, **amounts):
    """A send that must succeed; its transfer's id."""
    sent = _send(client, giver, taker, **amounts)
    assert sent.status_code == 201, sent.get_data(as_text=True)
    return sent.get_json()["data"]["id"]


def _count(client, taker, transfer_id, body):
    """The receiver's count: Confirm (`confirmAsDeclared`), or the numbers he typed with one count per 10 L line."""
    return client.post(CONFIRM.format(transfer_id), json=body, headers=staff_headers(taker))


def _lines_of(db, session_id):
    """The trip's lines as `(product_id, loaded, transferred_in)`, read fresh."""
    db.session.expire_all()
    lines = DriverBottleSessionUnit.query.filter_by(session_id=session_id).order_by(DriverBottleSessionUnit.product_id)
    return [(line.product_id, line.loaded, line.transferred_in) for line in lines]


def _status_of(db, transfer_id):
    db.session.expire_all()
    return db.session.get(DriverBottleTransfer, transfer_id).status


def _sender_pushes(published, giver):
    """The pushes to the sender, as `(event, payload)`."""
    return [(event, payload) for chat, event, payload in pushes(published) if chat == int(giver.telegram_id)]


def test_one_tap_confirm_credits_every_line(client, db, sender, receiver, small, enqueued):  # noqa: F811
    """Aziz hands Bobur 10 full + 4 empty + 6 x 10 L; Bobur taps Confirm."""
    trip(db, sender, bottles=10, units={small.id: 6}, collected=4)
    landing = trip(db, receiver, bottles=4, units={small.id: 2})
    transfer_id = _sent(client, sender, receiver, full=10, empty=4, units={small.id: 6})

    counted = _count(client, receiver, transfer_id, {"confirmAsDeclared": True})

    assert counted.status_code == 200, counted.get_data(as_text=True)
    data = counted.get_json()["data"]
    every_line = [
        {"kind": "full", "product_id": None, "product_name": None, "declared": 10, "confirmed": 10, "resolved": None},
        {"kind": "empty", "product_id": None, "product_name": None, "declared": 4, "confirmed": 4, "resolved": None},
        {
            "kind": "product",
            "product_id": small.id,
            "product_name": "10 Litr suv",
            "declared": 6,
            "confirmed": 6,
            "resolved": None,
        },
    ]
    assert (data["status"], data["receiver_session_id"], data["admins_alerted"], data["lines"]) == (
        "confirmed",
        landing.id,
        False,
        every_line,
    )
    assert data["units"] == [
        {"product_id": small.id, "product_name": "10 Litr suv", "declared": 6, "confirmed": 6, "resolved": None}
    ]
    stored = db.session.get(DriverBottleSession, landing.id)
    assert (stored.bottles_transferred_in, stored.empties_transferred_in) == (10, 4)
    assert _lines_of(db, landing.id) == [(small.id, 2, 6)]
    # Nothing is left on Aziz's truck, so his trip closed; his message carries every line.
    assert _sender_pushes(enqueued, sender) == [
        (
            "transfer_confirmed",
            {
                "id": transfer_id,
                "receiver_name": "Bobur",
                "confirmed_full": 10,
                "confirmed_empty": 4,
                "auto_closed": True,
                "lines": every_line,
            },
        )
    ]


@pytest.mark.parametrize("case", ["a-line-left-out", "a-line-not-sent"])
def test_other_numbers_missing_a_line_are_refused(client, db, sender, receiver, small, enqueued, case):  # noqa: F811
    """Other numbers answer exactly the lines the transfer carries: one left out, or one it never carried, is refused
    and nothing is credited."""
    if case == "a-line-left-out":
        trip(db, sender, bottles=10, units={small.id: 6}, collected=4)
        transfer_id = _sent(client, sender, receiver, full=10, empty=4, units={small.id: 6})
        body = {"confirmedFull": 10, "confirmedEmpty": 4}
        expected = {"missing_product_ids": [small.id], "extra_product_ids": []}
    else:
        trip(db, sender, bottles=10, collected=4)
        transfer_id = _sent(client, sender, receiver, full=10, empty=4)
        body = {"confirmedFull": 10, "confirmedEmpty": 4, "units": [{"productId": small.id, "quantity": 1}]}
        expected = {"missing_product_ids": [], "extra_product_ids": [small.id]}
    received = pushes(enqueued)

    refused = _count(client, receiver, transfer_id, body)

    assert refused.status_code == 400, refused.get_data(as_text=True)
    answer = refused.get_json()
    assert (answer["error_code"], answer["details"]) == (
        "BOTTLE_TRANSFER_COUNT_INCOMPLETE",
        {**expected, "validation_errors": []},
    )
    assert _status_of(db, transfer_id) == DriverBottleTransferStatus.PENDING
    assert DriverBottleSession.query.filter_by(driver_user_id=receiver.id).count() == 0
    assert pushes(enqueued) == received


@pytest.mark.parametrize("body", [{"confirmedQuantity": 10}, {"confirmed_quantity": 10}], ids=["camel", "snake"])
def test_the_legacy_confirmed_quantity_body_is_refused(client, db, sender, receiver, enqueued, body):  # noqa: F811
    """The old bot's one full count is no longer a count: every line is answered, or Confirm is tapped."""
    trip(db, sender, bottles=10)
    transfer_id = _sent(client, sender, receiver, full=10)

    refused = _count(client, receiver, transfer_id, body)

    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert refused.get_json()["errors"] == ["send confirmAsDeclared, or confirmedFull and confirmedEmpty"]
    assert _status_of(db, transfer_id) == DriverBottleTransferStatus.PENDING
    assert DriverBottleSession.query.filter_by(driver_user_id=receiver.id).count() == 0


@pytest.mark.parametrize("case", ["confirm-and-units", "a-product-twice", "units-without-numbers"])
def test_a_count_body_with_units_two_ways_or_twice_is_refused(
    client, db, sender, receiver, small, enqueued, case  # noqa: F811
):
    trip(db, sender, bottles=10, units={small.id: 6}, collected=4)
    transfer_id = _sent(client, sender, receiver, full=10, empty=4, units={small.id: 6})
    one = {"productId": small.id, "quantity": 6}
    body, error = {
        "confirm-and-units": (
            {"confirmAsDeclared": True, "units": [one]},
            "send confirmAsDeclared, or confirmedFull and confirmedEmpty",
        ),
        "a-product-twice": (
            {"confirmedFull": 10, "confirmedEmpty": 4, "units": [one, {**one, "quantity": 1}]},
            f"units: Product {small.id} is listed twice; give each product once.",
        ),
        "units-without-numbers": ({"units": [one]}, "confirmedFull and confirmedEmpty go together"),
    }[case]

    refused = _count(client, receiver, transfer_id, body)

    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert refused.get_json()["errors"] == [error]
    assert _status_of(db, transfer_id) == DriverBottleTransferStatus.PENDING


def test_a_surplus_on_a_zero_line_is_disputed(client, db, enqueued, slack, sender, receiver, small):  # noqa: F811
    """Aziz sends 10 full + 4 empty; his trip counts 10 L, so the transfer carries its line, declared 0. Bobur counts
    the same 18.9 L and one 10 L more: only the 10 L line differs, and the admins read it."""
    trip(db, sender, bottles=10, units={small.id: 3}, collected=4)
    transfer_id = _sent(client, sender, receiver, full=10, empty=4)

    before = datetime.now(TASHKENT)
    counted = _count(
        client,
        receiver,
        transfer_id,
        {"confirmedFull": 10, "confirmedEmpty": 4, "units": [{"productId": small.id, "quantity": 1}]},
    )
    after = datetime.now(TASHKENT)

    assert counted.status_code == 200, counted.get_data(as_text=True)
    data = counted.get_json()["data"]
    every_line = [
        {"kind": "full", "product_id": None, "product_name": None, "declared": 10, "confirmed": 10, "resolved": None},
        {"kind": "empty", "product_id": None, "product_name": None, "declared": 4, "confirmed": 4, "resolved": None},
        {
            "kind": "product",
            "product_id": small.id,
            "product_name": "10 Litr suv",
            "declared": 0,
            "confirmed": 1,
            "resolved": None,
        },
    ]
    assert (data["status"], data["admins_alerted"], data["lines"]) == ("disputed", True, every_line)
    transfer = db.session.get(DriverBottleTransfer, transfer_id)
    text, stamp = _posted_alert(slack, before, after)
    assert text == _dispute_alert(
        transfer.transfer_ref,
        sender="Aziz",
        receiver="Bobur",
        declared="10 full + 4 empty + 0 × 10 Litr suv",
        counted="10 full + 4 empty + 1 × 10 Litr suv",
        difference="0 full, 0 empty, +1 × 10 Litr suv",
        stamp=stamp,
    )
    # Bobur had nothing open: the hand-off opened his trip, which holds what he counted until an admin resolves it.
    [landing] = DriverBottleSession.query.filter_by(driver_user_id=receiver.id).all()
    assert (landing.status, landing.bottles_transferred_in, landing.empties_transferred_in) == (
        DriverBottleSessionStatus.OPEN,
        10,
        4,
    )
    assert _lines_of(db, landing.id) == [(small.id, 0, 1)]
    assert _sender_pushes(enqueued, sender) == [
        (
            "transfer_disputed",
            {
                "id": transfer_id,
                "receiver_name": "Bobur",
                "declared_full": 10,
                "declared_empty": 4,
                "confirmed_full": 10,
                "confirmed_empty": 4,
                "admins_alerted": True,
                "lines": every_line,
            },
        )
    ]


def test_a_ten_litre_only_count_to_a_receiver_with_nothing_open_opens_a_trip_with_it(
    client, db, sender, receiver, small, enqueued  # noqa: F811
):
    """Aziz hands Bobur 6 x 10 L and nothing else; Bobur, with no trip, types 0, 0 and 6."""
    trip(db, sender, units={small.id: 6})
    transfer_id = _sent(client, sender, receiver, units={small.id: 6})

    counted = _count(
        client,
        receiver,
        transfer_id,
        {"confirmedFull": 0, "confirmedEmpty": 0, "units": [{"productId": small.id, "quantity": 6}]},
    )

    assert counted.status_code == 200, counted.get_data(as_text=True)
    data = counted.get_json()["data"]
    [opened] = DriverBottleSession.query.filter_by(driver_user_id=receiver.id).all()
    assert (data["status"], data["receiver_session_id"]) == ("confirmed", opened.id)
    assert (opened.status, opened.bottles_loaded, opened.bottles_transferred_in, opened.empties_transferred_in) == (
        DriverBottleSessionStatus.OPEN,
        0,
        0,
        0,
    )
    assert _lines_of(db, opened.id) == [(small.id, 0, 6)]


def test_a_count_onto_a_reopened_trip_creates_the_line(
    client, db, admin_claim_headers, sender, receiver, big, small, sample_user, enqueued  # noqa: F811
):
    """Bobur's last trip closed before 10 L was counted, so it has no 10 L line. An office edit of an order he delivered
    on it reopens it; then he counts Aziz's 6 x 10 L onto it. The line is made on the reopened trip, and the trip he
    reads next lists it."""
    address = _attach_returnable_address(db, sample_user, big)
    order = _seed_paid_cash_delivered_order(sample_user, big, quantity=4)
    order.delivery_address_id = address.id
    db.session.commit()
    reopened = trip(db, receiver, bottles=10, status=DriverBottleSessionStatus.CLOSED)
    reopened.bottles_returned_to_warehouse, reopened.discrepancy = 6, 0
    db.session.commit()
    bind(db, reopened, order, receiver)
    edited = client.post(
        ORDER_EDIT.format(order.id),
        json={
            "items": [{"orderItemId": order.order_items[0].id, "productId": big.id, "quantity": 2}],
            "reason": "customer kept only 2 of 4 bottles after delivery",
        },
        headers=admin_claim_headers,
    )
    assert edited.status_code == 200, edited.get_data(as_text=True)
    assert db.session.get(DriverBottleSession, reopened.id).status == DriverBottleSessionStatus.OPEN
    trip(db, sender, units={small.id: 6})
    transfer_id = _sent(client, sender, receiver, units={small.id: 6})

    counted = _count(client, receiver, transfer_id, {"confirmAsDeclared": True})

    assert counted.status_code == 200, counted.get_data(as_text=True)
    assert (counted.get_json()["data"]["status"], counted.get_json()["data"]["receiver_session_id"]) == (
        "confirmed",
        reopened.id,
    )
    assert _lines_of(db, reopened.id) == [(small.id, 0, 6)]
    current = client.get(CURRENT, headers=staff_headers(receiver))
    assert current.status_code == 200, current.get_data(as_text=True)
    trip_read = current.get_json()["data"]
    assert trip_read["id"] == reopened.id
    assert [
        (line["product_id"], line["loaded"], line["transferred_in"], line["on_board"], line["transferable"])
        for line in trip_read["units"]
    ] == [(small.id, 0, 6, 6, 6)]


def test_a_ten_litre_only_trip_auto_closes_with_zero_on_every_line(
    client, db, sender, receiver, small, enqueued  # noqa: F811
):
    """Aziz carries only 10 L and hands all 6 to Bobur, who taps Confirm: nothing is left on Aziz's truck, so his trip
    closes on its own, 0 returned on every line and no discrepancy."""
    s = trip(db, sender, units={small.id: 6})
    trip(db, receiver, bottles=4)
    transfer_id = _sent(client, sender, receiver, units={small.id: 6})

    counted = _count(client, receiver, transfer_id, {"confirmAsDeclared": True})

    assert counted.status_code == 200, counted.get_data(as_text=True)
    db.session.expire_all()
    closed = db.session.get(DriverBottleSession, s.id)
    assert (closed.status, closed.bottles_returned_to_warehouse, closed.discrepancy) == (
        DriverBottleSessionStatus.CLOSED,
        0,
        0,
    )
    assert [(line.returned, line.discrepancy) for line in closed.unit_lines] == [(0, 0)]
    assert _sender_pushes(enqueued, sender) == [
        (
            "transfer_confirmed",
            {
                "id": transfer_id,
                "receiver_name": "Bobur",
                "confirmed_full": 0,
                "confirmed_empty": 0,
                "auto_closed": True,
                "lines": [
                    {
                        "kind": "full",
                        "product_id": None,
                        "product_name": None,
                        "declared": 0,
                        "confirmed": 0,
                        "resolved": None,
                    },
                    {
                        "kind": "empty",
                        "product_id": None,
                        "product_name": None,
                        "declared": 0,
                        "confirmed": 0,
                        "resolved": None,
                    },
                    {
                        "kind": "product",
                        "product_id": small.id,
                        "product_name": "10 Litr suv",
                        "declared": 6,
                        "confirmed": 6,
                        "resolved": None,
                    },
                ],
            },
        )
    ]


def test_a_transfer_sent_before_the_tick_and_counted_after_asks_no_ten_litre(
    client, db, admin_claim_headers, sender, receiver, sample_category, enqueued  # noqa: F811
):
    """Aziz sent 10 full + 4 empty while 10 L was not counted, then closed his trip. The admin ticks 10 L (no trip is
    open); Bobur then counts the two 18.9 L numbers. The transfer carries no 10 L line, so none is asked."""
    product = ten_litre(db, sample_category, counted=False)
    trip(db, sender, bottles=10, collected=4)
    transfer_id = _sent(client, sender, receiver, full=10, empty=4)
    closed = client.post(CLOSE, json={"bottles_returned_to_warehouse": 0}, headers=staff_headers(sender))
    assert closed.status_code == 200, closed.get_data(as_text=True)
    ticked = client.put(
        ADMIN_PRODUCT.format(product.id), json={"counted_on_bottle_trip": True}, headers=admin_claim_headers
    )
    assert ticked.status_code == 200, ticked.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Product, product.id).counted_on_bottle_trip is True

    counted = _count(client, receiver, transfer_id, {"confirmedFull": 10, "confirmedEmpty": 4})

    assert counted.status_code == 200, counted.get_data(as_text=True)
    data = counted.get_json()["data"]
    assert (data["status"], data["units"]) == ("confirmed", [])
    assert data["lines"] == [
        {"kind": "full", "product_id": None, "product_name": None, "declared": 10, "confirmed": 10, "resolved": None},
        {"kind": "empty", "product_id": None, "product_name": None, "declared": 4, "confirmed": 4, "resolved": None},
    ]
    # The trip the count opened reads the setting as it is now: a 10 L line at 0.
    [opened] = DriverBottleSession.query.filter_by(driver_user_id=receiver.id).all()
    assert (opened.bottles_transferred_in, opened.empties_transferred_in) == (10, 4)
    assert _lines_of(db, opened.id) == [(product.id, 0, 0)]


# --------------------------------------------------------------------------- #
# The office resolve
# --------------------------------------------------------------------------- #

RESOLVE = "/api/v1/admin/bottles/transfers/{}/resolve"


def _resolve(client, admin_claim_headers, transfer_id, **body):
    """The admin page's resolve body: both 18.9 L numbers, `resolvedUnits` (one line per product) and the notes."""
    payload = {"resolvedFull": 0, "resolvedEmpty": 0, "resolutionNotes": "Counted again at the depot", **body}
    return client.post(RESOLVE.format(transfer_id), json=payload, headers=admin_claim_headers)


def _disputed(client, db, giver, taker, small, *, declared, counted):  # noqa: F811
    """A transfer of `declared` x 10 L (and nothing else) that the receiver counted as `counted`, now DISPUTED."""
    transfer_id = _sent(client, giver, taker, units={small.id: declared})
    answered = _count(
        client,
        taker,
        transfer_id,
        {"confirmedFull": 0, "confirmedEmpty": 0, "units": [{"productId": small.id, "quantity": counted}]},
    )
    assert answered.status_code == 200, answer_text(answered)
    assert _status_of(db, transfer_id) == DriverBottleTransferStatus.DISPUTED
    return transfer_id


def answer_text(response):
    return response.get_data(as_text=True)


def test_the_resolve_rewrites_both_sides_and_recomputes_a_closed_trip(
    client, db, admin_claim_headers, sender, receiver, small, enqueued  # noqa: F811
):
    """Aziz sends 6 of his 10 x 10 L, Bobur (a trip open, 2 on board) counts 4, and Aziz closes with the 4 left. The
    office decides 5 moved: Aziz's line is out 5 (1 unaccounted for on his closed trip), Bobur's is in 5."""
    closed = trip(db, sender, units={small.id: 10})
    landing = trip(db, receiver, units={small.id: 2})
    transfer_id = _disputed(client, db, sender, receiver, small, declared=6, counted=4)
    closing = client.post(
        CLOSE,
        json={"bottles_returned_to_warehouse": 0, "units_returned": [{"productId": small.id, "quantity": 4}]},
        headers=staff_headers(sender),
    )
    assert closing.status_code == 200, answer_text(closing)
    assert _lines_of(db, landing.id) == [(small.id, 2, 4)]
    assert _line_of(db, closed.id, small.id).discrepancy == 0

    resolved = _resolve(client, admin_claim_headers, transfer_id, resolvedUnits=[{"productId": small.id, "quantity": 5}])

    assert resolved.status_code == 200, answer_text(resolved)
    assert resolved.get_json()["data"]["status"] == "resolved"
    out_line = _line_of(db, closed.id, small.id)
    assert (out_line.loaded, out_line.transferred_out, out_line.returned, out_line.discrepancy) == (10, 5, 4, 1)
    assert _lines_of(db, landing.id) == [(small.id, 2, 5)]
    assert db.session.get(DriverBottleSession, landing.id).status == DriverBottleSessionStatus.OPEN
    [transfer_line] = DriverBottleTransferUnit.query.filter_by(transfer_id=transfer_id).all()
    assert (transfer_line.declared, transfer_line.confirmed, transfer_line.resolved) == (6, 4, 5)


SESSION_DETAIL = "/api/v1/admin/bottles/sessions/{}"


def test_after_a_resolve_every_line_of_the_drawer_publishes_the_resolved_figure_beside_the_count(
    client, db, admin_claim_headers, sender, receiver, small, enqueued  # noqa: F811
):
    """Aziz sends 10 full + 6 x 10 L; Bobur counts 10 full + 4 x 10 L, so it is DISPUTED; the office resolves 10 / 6,
    and Bobur's 10 L line is credited 6. The session drawer (the admin session-detail route) shows each line of the
    transfer: what was sent, what was counted and what the office resolved, so its "Received" reads 6 for 10 L, as
    the trip's line does, and not the count of 4 beside the full line's resolved 10."""
    trip(db, sender, bottles=10, units={small.id: 10})
    landing = trip(db, receiver, units={small.id: 2})
    transfer_id = _sent(client, sender, receiver, full=10, units={small.id: 6})
    counted = _count(
        client,
        receiver,
        transfer_id,
        {"confirmedFull": 10, "confirmedEmpty": 0, "units": [{"productId": small.id, "quantity": 4}]},
    )
    assert counted.status_code == 200, answer_text(counted)
    assert _status_of(db, transfer_id) == DriverBottleTransferStatus.DISPUTED
    resolved = _resolve(
        client,
        admin_claim_headers,
        transfer_id,
        resolvedFull=10,
        resolvedUnits=[{"productId": small.id, "quantity": 6}],
    )
    assert resolved.status_code == 200, answer_text(resolved)

    drawer = client.get(SESSION_DETAIL.format(landing.id), headers=admin_claim_headers)

    assert drawer.status_code == 200, answer_text(drawer)
    session = drawer.get_json()["data"]
    [transfer] = session["transfers_in"]
    assert transfer["lines"] == [
        {"kind": "full", "product_id": None, "product_name": None, "declared": 10, "confirmed": 10, "resolved": 10},
        {"kind": "empty", "product_id": None, "product_name": None, "declared": 0, "confirmed": 0, "resolved": 0},
        {
            "kind": "product",
            "product_id": small.id,
            "product_name": "10 Litr suv",
            "declared": 6,
            "confirmed": 4,
            "resolved": 6,
        },
    ]
    assert [(line["product_id"], line["transferred_in"]) for line in session["units"]] == [(small.id, 6)]


def test_a_ten_litre_only_resolve_lands_on_the_receivers_trip(
    client, db, admin_claim_headers, sender, receiver, small, enqueued  # noqa: F811
):
    """Bobur had no trip and counted 0 of Aziz's 6 x 10 L: nothing was credited and no trip opened. The office decides
    3 moved: a trip is opened for Bobur with the 10 L line at 3, and Aziz's line is out 3."""
    sent_from = trip(db, sender, units={small.id: 6})
    transfer_id = _disputed(client, db, sender, receiver, small, declared=6, counted=0)
    assert DriverBottleSession.query.filter_by(driver_user_id=receiver.id).count() == 0

    resolved = _resolve(client, admin_claim_headers, transfer_id, resolvedUnits=[{"productId": small.id, "quantity": 3}])

    assert resolved.status_code == 200, answer_text(resolved)
    [opened] = DriverBottleSession.query.filter_by(driver_user_id=receiver.id).all()
    assert (opened.status, opened.bottles_loaded, opened.bottles_transferred_in) == (
        DriverBottleSessionStatus.OPEN,
        0,
        0,
    )
    assert resolved.get_json()["data"]["receiver_session_id"] == opened.id
    assert _lines_of(db, opened.id) == [(small.id, 0, 3)]
    assert _line_of(db, sent_from.id, small.id).transferred_out == 3


def test_a_resolve_missing_a_line_is_refused(
    client, db, admin_claim_headers, sender, receiver, small, enqueued  # noqa: F811
):
    """The resolve answers every line of the transfer: one left out is refused and nothing is written."""
    sent_from = trip(db, sender, units={small.id: 6})
    transfer_id = _disputed(client, db, sender, receiver, small, declared=6, counted=4)

    refused = _resolve(client, admin_claim_headers, transfer_id)

    assert refused.status_code == 400, answer_text(refused)
    answer = refused.get_json()
    assert (answer["error_code"], answer["details"]) == (
        "BOTTLE_TRANSFER_COUNT_INCOMPLETE",
        {"missing_product_ids": [small.id], "extra_product_ids": [], "validation_errors": []},
    )
    assert _status_of(db, transfer_id) == DriverBottleTransferStatus.DISPUTED
    assert _line_of(db, sent_from.id, small.id).transferred_out == 6


JOIN_REQUEST = "/api/v1/staff/bottles/session/join-request"
APPROVE_JOIN = JOIN_REQUEST + "/approve"


def test_a_resolve_that_would_open_a_co_drivers_own_trip_is_refused_and_writes_nothing(
    client, db, admin_claim_headers, sender, receiver, owner, small, enqueued  # noqa: F811
):
    """Aziz hands Bobur 3 full + 6 x 10 L. Bobur, with nothing open, counts 0 on every line: nothing is credited and the
    transfer is disputed. Bobur then asks to join Olim's trip and Olim taps Approve. Resolving 3 full + 5 x 10 L would
    open Bobur a trip of his own while he co-drives Olim's, two places to be: refused with Olim's trip named, and
    nothing is written. Resolving 0 on every line opens no trip, so it goes through."""
    sent_from = trip(db, sender, bottles=10, units={small.id: 6})
    olims = trip(db, owner, bottles=40, units={small.id: 4})
    transfer_id = _sent(client, sender, receiver, full=3, units={small.id: 6})
    counted = _count(
        client,
        receiver,
        transfer_id,
        {"confirmedFull": 0, "confirmedEmpty": 0, "units": [{"productId": small.id, "quantity": 0}]},
    )
    assert counted.status_code == 200, counted.get_data(as_text=True)
    assert _status_of(db, transfer_id) == DriverBottleTransferStatus.DISPUTED
    asked = client.post(JOIN_REQUEST, json={"session_id": olims.id}, headers=staff_headers(receiver))
    assert asked.status_code == 200, asked.get_data(as_text=True)
    approved = client.post(
        APPROVE_JOIN, json={"session_id": olims.id, "requester_id": receiver.id}, headers=staff_headers(owner)
    )
    assert approved.status_code == 200, approved.get_data(as_text=True)

    refused = _resolve(
        client,
        admin_claim_headers,
        transfer_id,
        resolvedFull=3,
        resolvedUnits=[{"productId": small.id, "quantity": 5}],
    )

    assert refused.status_code == 409, refused.get_data(as_text=True)
    answer = refused.get_json()
    assert (answer["error_code"], answer["details"]) == (
        "BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE",
        {"session_id": olims.id, "owner_user_id": owner.id, "owner_name": "Olim"},
    )
    db.session.expire_all()
    refused_transfer = db.session.get(DriverBottleTransfer, transfer_id)
    assert (refused_transfer.status, refused_transfer.receiver_session_id, refused_transfer.resolved_at) == (
        DriverBottleTransferStatus.DISPUTED,
        None,
        None,
    )
    [transfer_line] = DriverBottleTransferUnit.query.filter_by(transfer_id=transfer_id).all()
    assert (transfer_line.declared, transfer_line.confirmed, transfer_line.resolved) == (6, 0, None)
    assert _moved_out(db, sent_from.id) == (3, 0)
    sent_line = _line_of(db, sent_from.id, small.id)
    assert (sent_line.loaded, sent_line.transferred_out) == (6, 6)
    assert DriverBottleSession.query.filter_by(driver_user_id=receiver.id).all() == []
    assert db.session.get(DriverBottleSession, olims.id).bottles_transferred_in == 0
    assert _lines_of(db, olims.id) == [(small.id, 4, 0)]
    [membership] = DriverSessionMembership.query.filter_by(
        member_driver_id=receiver.id, status=DriverSessionMembershipStatus.ACTIVE
    ).all()
    assert membership.session_id == olims.id

    nothing_moved = _resolve(
        client, admin_claim_headers, transfer_id, resolvedUnits=[{"productId": small.id, "quantity": 0}]
    )

    assert nothing_moved.status_code == 200, nothing_moved.get_data(as_text=True)
    data = nothing_moved.get_json()["data"]
    assert (data["status"], data["receiver_session_id"]) == ("resolved", None)
    assert DriverBottleSession.query.filter_by(driver_user_id=receiver.id).all() == []
    assert _moved_out(db, sent_from.id) == (0, 0)
    assert _line_of(db, sent_from.id, small.id).transferred_out == 0
    assert _lines_of(db, olims.id) == [(small.id, 4, 0)]


def test_the_staff_bots_own_unit_send_body_is_what_the_endpoint_takes(
    client, db, sender, receiver, small, enqueued, monkeypatch  # noqa: F811
):
    """The body comes from the bot's client itself, so a field renamed on either side fails here."""
    trip(db, sender, units={small.id: 6})

    endpoint, body = _bot_body(
        monkeypatch, "initiate_bottle_transfer", receiver.id,
        full_quantity=0, empty_quantity=0, units=[{"product_id": small.id, "quantity": 2}],
    )
    sent = client.post(endpoint, headers=staff_headers(sender), json=body)

    assert sent.status_code == 201, sent.get_data(as_text=True)
    db.session.expire_all()
    [line] = DriverBottleTransferUnit.query.filter_by(transfer_id=sent.get_json()["data"]["id"]).all()
    assert (line.product_id, line.declared) == (small.id, 2)


def test_the_staff_bots_own_count_body_with_a_ten_litre_line_is_what_the_endpoint_takes(
    client, db, sender, receiver, small, enqueued, monkeypatch  # noqa: F811
):
    """Bobur's "Enter other numbers" from the bot's own client: both numbers and one count per 10 L line. The
    endpoint takes it as a full count and credits every line, the 10 L one on his own trip."""
    trip(db, sender, bottles=10, units={small.id: 6}, collected=4)
    landing = trip(db, receiver, bottles=4, units={small.id: 2})
    transfer_id = _sent(client, sender, receiver, full=10, empty=4, units={small.id: 6})

    endpoint, body = _bot_body(
        monkeypatch, "confirm_bottle_transfer", transfer_id,
        confirmed_full=10, confirmed_empty=4, units=[{"product_id": small.id, "quantity": 6}],
    )
    counted = client.post(endpoint, headers=staff_headers(receiver), json=body)

    assert body == {"confirmed_full": 10, "confirmed_empty": 4, "units": [{"product_id": small.id, "quantity": 6}]}
    assert counted.status_code == 200, counted.get_data(as_text=True)
    assert (counted.get_json()["data"]["status"], counted.get_json()["data"]["receiver_session_id"]) == (
        "confirmed",
        landing.id,
    )
    assert _lines_of(db, landing.id) == [(small.id, 2, 6)]
    stored = db.session.get(DriverBottleSession, landing.id)
    assert (stored.bottles_transferred_in, stored.empties_transferred_in) == (10, 4)
