"""Transfers carry full bottles and empties as two numbers (spec 2026-11-09 T1, T2, T3, T5 and §4.3).

Driven through the HTTP routes the staff bot and the admin Bottle Tracking page call, with real JWTs and the camelCase
bodies the new clients send. The old send body's `quantity` (full bottles, snake_case too) stays accepted for the
restart window (spec "Old data in flight"); the old single full count on Confirm does not, since every line is now
counted. Only the Celery publish is faked, with the signature-enforcing `_spy`; the running bot's renderer and error
resolver are the real ones, fed the real seeded copy.
"""

import importlib.util
from pathlib import Path

import pytest

from business_app.models.bottle import DriverBottleSession, DriverBottleTransfer
from business_app.tasks import staff_tasks
from shared.enums import DeliveryStatus, DriverBottleSessionStatus
from shared.staff_constants import BOTTLE_RETURN_COLUMN_CEILING
from staff_bot.handlers.base import BaseHandler
from staff_bot.i18n import i18n
from tests.unit.test_bottle_transfer_and_join_requests import _driver, _headers
from tests.unit.test_sales_agent_tasks import _spy
from tests.unit.test_staff_error_endpoints import _assigned_delivery, _order_with_bottles

pytestmark = pytest.mark.integration

TRANSFERS = "/api/v1/staff/bottles/transfers"
CONFIRM = TRANSFERS + "/{}/confirm"
PENDING = TRANSFERS + "/pending"
CURRENT_SESSION = "/api/v1/staff/bottles/session/current"
CLOSE_SESSION = "/api/v1/staff/bottles/session/close"
ADMIN_TRANSFERS = "/api/v1/admin/bottles/transfers"
RESOLVE = ADMIN_TRANSFERS + "/{}/resolve"

# The six numbers every transfer row publishes, and its status.
SPLIT_KEYS = (
    "declared_full",
    "declared_empty",
    "declared_total",
    "confirmed_full",
    "confirmed_empty",
    "confirmed_total",
    "status",
)

_seed_spec = importlib.util.spec_from_file_location(
    "seed_staff_translations_transfer_split",
    Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py",
)
_SEED = importlib.util.module_from_spec(_seed_spec)
_seed_spec.loader.exec_module(_SEED)


@pytest.fixture
def pushes(monkeypatch):
    return _spy(monkeypatch, staff_tasks.push_bottle_event, "delay")


@pytest.fixture
def seeded_copy(monkeypatch):
    """The running bot's English, as scripts/seed_staff_translations.py ships it."""

    def install(*keys):
        rows = {key: _SEED._curated_value(key, "en") for key in keys}
        assert all(rows.values()), rows
        monkeypatch.setitem(i18n.translations, "en", {**i18n.translations.get("en", {}), **rows})

    return install


def _trip(db, driver, *, loaded, collected=0, delivered=0):
    """The driver's OPEN session: `loaded` full at the depot, `delivered` handed to customers, `collected` empties."""
    session = DriverBottleSession(
        driver_user_id=driver.id,
        bottles_loaded=loaded,
        bottles_delivered=delivered,
        bottles_collected_from_customers=collected,
        status=DriverBottleSessionStatus.OPEN,
    )
    db.session.add(session)
    db.session.commit()
    return session


def _pair(db, phone_base):
    """Ali sends, Vali receives."""
    return _driver(db, f"+99890125{phone_base}1", "Ali"), _driver(db, f"+99890125{phone_base}2", "Vali")


def _send(client, app, sender, body):
    sent = client.post(TRANSFERS, headers=_headers(app, sender.id), json=body)
    assert sent.status_code == 201, sent.get_json()
    return sent.get_json()["data"]


def _confirm(client, app, transfer_id, receiver, body):
    return client.post(CONFIRM.format(transfer_id), headers=_headers(app, receiver.id), json=body)


def _split(row):
    return {key: row[key] for key in SPLIT_KEYS}


def _moved_out(session):
    return session.bottles_transferred_out, session.empties_transferred_out


def _moved_in(session):
    return session.bottles_transferred_in, session.empties_transferred_in


def _screen(client, app, driver):
    """The session screen's numbers, as the bot reads them."""
    data = client.get(CURRENT_SESSION, headers=_headers(app, driver.id)).get_json()["data"]
    return data["free_full_bottles"], data["transferable_full"], data["transferable_empties"]


# --------------------------------------------------------------------------- #
# Sending (T1, T2, T3)
# --------------------------------------------------------------------------- #


def test_choose_amounts_moves_each_component(app, client, db, pushes):
    sender, receiver = _pair(db, "01")
    trip = _trip(db, sender, loaded=5, collected=2)

    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "fullQuantity": 5, "emptyQuantity": 2})

    assert _split(sent) == {
        "declared_full": 5,
        "declared_empty": 2,
        "declared_total": 7,
        "confirmed_full": None,
        "confirmed_empty": None,
        "confirmed_total": None,
        "status": "pending",
    }
    db.session.refresh(trip)
    assert _moved_out(trip) == (5, 2)
    assert _screen(client, app, sender) == (0, 0, 0)
    assert pushes == [
        (
            (
                int(receiver.telegram_id),
                "transfer_received",
                {
                    "id": sent["id"],
                    "declared_quantity": 5,
                    "declared_empty_quantity": 2,
                    "sender_name": "Ali Driver",
                    "lines": [
                        {
                            "kind": "full",
                            "product_id": None,
                            "product_name": None,
                            "declared": 5,
                            "confirmed": None,
                            "resolved": None,
                        },
                        {
                            "kind": "empty",
                            "product_id": None,
                            "product_name": None,
                            "declared": 2,
                            "confirmed": None,
                            "resolved": None,
                        },
                    ],
                },
            ),
            {},
        )
    ]


@pytest.mark.parametrize(
    "body, details",
    [
        ({"fullQuantity": 6, "emptyQuantity": 2}, {"kind": "full", "requested": 6, "available": 5}),
        ({"fullQuantity": 5, "emptyQuantity": 3}, {"kind": "empty", "requested": 3, "available": 2}),
    ],
    ids=["full-over", "empties-over"],
)
def test_each_component_capped_separately(app, client, db, pushes, body, details):
    sender, receiver = _pair(db, "02")
    trip = _trip(db, sender, loaded=5, collected=2)

    refused = client.post(TRANSFERS, headers=_headers(app, sender.id), json={"receiverDriverId": receiver.id, **body})

    assert refused.status_code == 400, refused.get_json()
    assert refused.get_json()["error_code"] == "BOTTLE_TRANSFER_EXCEEDS_INVENTORY"
    assert refused.get_json()["details"] == {**details, "validation_errors": []}
    db.session.refresh(trip)
    assert _moved_out(trip) == (0, 0)
    assert DriverBottleTransfer.query.count() == 0
    assert pushes == []


@pytest.mark.parametrize(
    "body, sentence",
    [
        ({"fullQuantity": 6}, "You have only 5 full bottles."),
        ({"fullQuantity": 5, "emptyQuantity": 3}, "You have only 2 empty bottles."),
    ],
    ids=["full-over", "empties-over"],
)
def test_the_bot_words_a_capped_component_by_its_kind(app, client, db, seeded_copy, body, sentence):
    """The staff bot picks its sentence from the refusal's `kind` and names that number's cap (spec 2026-11-09 T2)."""
    seeded_copy(
        "staff.error.api.transfer_exceeds_full_detail",
        "staff.error.api.transfer_exceeds_empty_detail",
        "staff.error.api.transfer_exceeds_inventory_detail",
        "staff.error.api.transfer_exceeds_inventory",
    )
    sender, receiver = _pair(db, "03")
    _trip(db, sender, loaded=5, collected=2)

    refused = client.post(
        TRANSFERS, headers=_headers(app, sender.id), json={"receiverDriverId": receiver.id, **body}
    ).get_json()

    sentence_shown = BaseHandler.__new__(BaseHandler)._resolve_api_error_message(
        "en", refused.get("message"), 400, refused["error_code"], details=refused["details"]
    )
    assert sentence_shown == sentence


def test_send_all_sends_everything_including_promised(app, client, db, sample_user, sample_product):
    """T3: Send all empties the truck, the 3 full bottles promised to his own On-the-way stop included."""
    sender, receiver = _pair(db, "04")
    trip = _trip(db, sender, loaded=5, collected=2)
    _assigned_delivery(
        db, _order_with_bottles(db, sample_user, sample_product, quantity=3), sender, status=DeliveryStatus.IN_TRANSIT
    )
    assert _screen(client, app, sender) == (2, 5, 2)

    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "sendAll": True})

    assert (sent["declared_full"], sent["declared_empty"]) == (5, 2)
    db.session.refresh(trip)
    assert _moved_out(trip) == (5, 2)


def test_send_all_clamps_legacy_negative_full(app, client, db):
    """A legacy tally reads -4 full (4 delivered off a 0 load): Send all sends no full bottles, only the 4 empties."""
    sender, receiver = _pair(db, "05")
    trip = _trip(db, sender, loaded=0, delivered=4, collected=4)

    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "sendAll": True})

    assert (sent["declared_full"], sent["declared_empty"]) == (0, 4)
    db.session.refresh(trip)
    assert _moved_out(trip) == (0, 4)


@pytest.mark.parametrize(
    "counts, body",
    [
        ({"loaded": 5, "collected": 2}, {"fullQuantity": 0, "emptyQuantity": 0}),
        ({"loaded": 3, "delivered": 3}, {"sendAll": True}),
    ],
    ids=["chose-zero-and-zero", "send-all-on-an-empty-truck"],
)
def test_nothing_to_send_refused(app, client, db, pushes, counts, body):
    sender, receiver = _pair(db, "06")
    trip = _trip(db, sender, **counts)

    refused = client.post(TRANSFERS, headers=_headers(app, sender.id), json={"receiverDriverId": receiver.id, **body})

    assert refused.status_code == 400, refused.get_json()
    assert refused.get_json()["error_code"] == "BOTTLE_TRANSFER_NOTHING_TO_SEND"
    db.session.refresh(trip)
    assert _moved_out(trip) == (0, 0)
    assert DriverBottleTransfer.query.count() == 0
    assert pushes == []


@pytest.mark.parametrize(
    "body",
    [
        {"quantity": 3, "fullQuantity": 3},
        {"sendAll": True, "fullQuantity": 2},
        {"sendAll": True, "emptyQuantity": 1},
        {"fullQuantity": -1, "emptyQuantity": 2},
    ],
    ids=["legacy-and-new", "send-all-with-full", "send-all-with-empties", "negative"],
)
def test_a_send_that_names_two_ways_or_a_negative_is_refused(app, client, db, pushes, body):
    sender, receiver = _pair(db, "07")
    trip = _trip(db, sender, loaded=5, collected=2)

    refused = client.post(TRANSFERS, headers=_headers(app, sender.id), json={"receiverDriverId": receiver.id, **body})

    assert refused.status_code == 400, refused.get_json()
    db.session.refresh(trip)
    assert _moved_out(trip) == (0, 0)
    assert DriverBottleTransfer.query.count() == 0
    assert pushes == []


# A count the bottle columns (4-byte integers) cannot hold: a phone number typed into a quantity box. Every count field
# carries the storage bound (`BOTTLE_RETURN_COLUMN_CEILING`), so it is a 400 naming the field, never a DataError 500.
OVER_THE_CEILING = BOTTLE_RETURN_COLUMN_CEILING + 1
TOO_BIG = f"Input should be less than or equal to {BOTTLE_RETURN_COLUMN_CEILING}"


@pytest.mark.parametrize("field", ["fullQuantity", "emptyQuantity", "quantity"])
def test_a_send_count_the_columns_cannot_hold_is_refused_by_its_field(app, client, db, pushes, field):
    sender, receiver = _pair(db, "08")
    trip = _trip(db, sender, loaded=5, collected=2)

    refused = client.post(
        TRANSFERS, headers=_headers(app, sender.id), json={"receiverDriverId": receiver.id, field: OVER_THE_CEILING}
    )

    assert refused.status_code == 400, refused.get_json()
    assert refused.get_json()["errors"] == [f"{field}: {TOO_BIG}"]
    db.session.refresh(trip)
    assert _moved_out(trip) == (0, 0)
    assert DriverBottleTransfer.query.count() == 0
    assert pushes == []


# --------------------------------------------------------------------------- #
# Confirming (T5)
# --------------------------------------------------------------------------- #


def test_confirm_as_declared_credits_both_and_closes_clean(app, client, db):
    sender, receiver = _pair(db, "11")
    sender_trip = _trip(db, sender, loaded=7, collected=3)
    receiver_trip = _trip(db, receiver, loaded=4)
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "fullQuantity": 5, "emptyQuantity": 2})

    confirmed = _confirm(client, app, sent["id"], receiver, {"confirmAsDeclared": True})

    assert confirmed.status_code == 200, confirmed.get_json()
    assert _split(confirmed.get_json()["data"]) == {
        "declared_full": 5,
        "declared_empty": 2,
        "declared_total": 7,
        "confirmed_full": 5,
        "confirmed_empty": 2,
        "confirmed_total": 7,
        "status": "confirmed",
    }
    db.session.refresh(receiver_trip)
    assert _moved_in(receiver_trip) == (5, 2)
    assert (receiver_trip.full_bottles_on_board, receiver_trip.empties_on_board) == (9, 2)
    assert DriverBottleTransfer.query.get(sent["id"]).receiver_session_id == receiver_trip.id

    # Back at the warehouse each returns what his truck holds, full and empty alike, and both balance.
    sender_close = client.post(
        CLOSE_SESSION, headers=_headers(app, sender.id), json={"bottles_returned_to_warehouse": 3}
    )
    receiver_close = client.post(
        CLOSE_SESSION, headers=_headers(app, receiver.id), json={"bottles_returned_to_warehouse": 11}
    )
    assert sender_close.status_code == 200, sender_close.get_json()
    assert receiver_close.status_code == 200, receiver_close.get_json()
    assert (sender_close.get_json()["data"]["discrepancy"], receiver_close.get_json()["data"]["discrepancy"]) == (0, 0)
    db.session.refresh(sender_trip)
    assert sender_trip.status == DriverBottleSessionStatus.CLOSED


@pytest.mark.parametrize("counted", [(5, 1), (4, 2)], ids=["empties-differ", "full-differ"])
def test_different_count_disputes(app, client, db, counted):
    sender, receiver = _pair(db, "12")
    _trip(db, sender, loaded=7, collected=3)
    receiver_trip = _trip(db, receiver, loaded=4)
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "fullQuantity": 5, "emptyQuantity": 2})
    full, empty = counted

    disputed = _confirm(client, app, sent["id"], receiver, {"confirmedFull": full, "confirmedEmpty": empty})

    assert disputed.status_code == 200, disputed.get_json()
    assert _split(disputed.get_json()["data"]) == {
        "declared_full": 5,
        "declared_empty": 2,
        "declared_total": 7,
        "confirmed_full": full,
        "confirmed_empty": empty,
        "confirmed_total": full + empty,
        "status": "disputed",
    }
    # The receiver's truck holds what he counted until an admin decides.
    db.session.refresh(receiver_trip)
    assert _moved_in(receiver_trip) == counted


def test_an_exact_count_typed_by_hand_confirms(app, client, db):
    sender, receiver = _pair(db, "13")
    _trip(db, sender, loaded=7, collected=3)
    receiver_trip = _trip(db, receiver, loaded=4)
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "fullQuantity": 5, "emptyQuantity": 2})

    confirmed = _confirm(client, app, sent["id"], receiver, {"confirmedFull": 5, "confirmedEmpty": 2})

    assert confirmed.status_code == 200, confirmed.get_json()
    assert confirmed.get_json()["data"]["status"] == "confirmed"
    db.session.refresh(receiver_trip)
    assert _moved_in(receiver_trip) == (5, 2)


def test_an_empties_only_hand_off_to_a_driver_with_nothing_open_opens_his_session(app, client, db):
    """The receiver's credit lands by the one rule; a total of 3 empties is something to land, so a session opens."""
    sender, receiver = _pair(db, "14")
    _trip(db, sender, loaded=2, collected=3)
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "emptyQuantity": 3})

    confirmed = _confirm(client, app, sent["id"], receiver, {"confirmAsDeclared": True})

    assert confirmed.status_code == 200, confirmed.get_json()
    [opened] = DriverBottleSession.query.filter_by(
        driver_user_id=receiver.id, status=DriverBottleSessionStatus.OPEN
    ).all()
    assert (opened.bottles_loaded, *_moved_in(opened)) == (0, 0, 3)
    assert (opened.full_bottles_on_board, opened.empties_on_board) == (0, 3)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"confirmedFull": 5},
        {"confirmedEmpty": 2},
        {"confirmAsDeclared": True, "confirmedFull": 5, "confirmedEmpty": 2},
        {"confirmedQuantity": 5},
        {"confirmedFull": -1, "confirmedEmpty": 2},
    ],
    ids=[
        "nothing",
        "full-only",
        "empties-only",
        "declared-and-numbers",
        "legacy",
        "negative",
    ],
)
def test_a_confirm_that_does_not_give_exactly_one_count_is_refused_and_credits_nothing(app, client, db, body):
    sender, receiver = _pair(db, "15")
    _trip(db, sender, loaded=7, collected=3)
    receiver_trip = _trip(db, receiver, loaded=4)
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "fullQuantity": 5, "emptyQuantity": 2})

    refused = _confirm(client, app, sent["id"], receiver, body)

    assert refused.status_code == 400, refused.get_json()
    assert DriverBottleTransfer.query.get(sent["id"]).status.value == "pending"
    db.session.refresh(receiver_trip)
    assert _moved_in(receiver_trip) == (0, 0)


@pytest.mark.parametrize(
    "body, field",
    [
        ({"confirmedFull": OVER_THE_CEILING, "confirmedEmpty": 2}, "confirmedFull"),
        ({"confirmedFull": 5, "confirmedEmpty": OVER_THE_CEILING}, "confirmedEmpty"),
    ],
    ids=["full", "empty"],
)
def test_a_count_the_columns_cannot_hold_is_refused_by_its_field_and_credits_nothing(app, client, db, body, field):
    sender, receiver = _pair(db, "16")
    _trip(db, sender, loaded=7, collected=3)
    receiver_trip = _trip(db, receiver, loaded=4)
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "fullQuantity": 5, "emptyQuantity": 2})

    refused = _confirm(client, app, sent["id"], receiver, body)

    assert refused.status_code == 400, refused.get_json()
    assert refused.get_json()["errors"] == [f"{field}: {TOO_BIG}"]
    assert DriverBottleTransfer.query.get(sent["id"]).status.value == "pending"
    db.session.refresh(receiver_trip)
    assert _moved_in(receiver_trip) == (0, 0)


# --------------------------------------------------------------------------- #
# The admin resolves a dispute (T5)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field", ["resolvedFull", "resolvedEmpty"])
def test_a_resolve_count_the_columns_cannot_hold_is_refused_by_its_field(app, client, db, admin_claim_headers, field):
    sender, receiver = _pair(db, "22")
    trips = (_trip(db, sender, loaded=8, collected=4), _trip(db, receiver, loaded=3))
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "fullQuantity": 6, "emptyQuantity": 3})
    counted = _confirm(client, app, sent["id"], receiver, {"confirmedFull": 4, "confirmedEmpty": 2})
    assert counted.get_json()["data"]["status"] == "disputed"
    body = {
        "resolvedFull": 5,
        "resolvedEmpty": 2,
        "resolutionNotes": "Counted again at the depot",
        field: OVER_THE_CEILING,
    }

    refused = client.post(RESOLVE.format(sent["id"]), headers=admin_claim_headers, json=body)

    assert refused.status_code == 400, refused.get_json()
    assert refused.get_json()["errors"] == [f"{field}: {TOO_BIG}"]
    assert DriverBottleTransfer.query.get(sent["id"]).status.value == "disputed"
    for trip in trips:
        db.session.refresh(trip)
    assert (_moved_out(trips[0]), _moved_in(trips[1])) == ((6, 3), (4, 2))


@pytest.mark.parametrize(
    "closer, returned, closed_discrepancy",
    [
        # Sender: 8 full - 6 sent = 2, 4 empties - 3 sent = 1; he really kept 3 full + 2 empties and returns them.
        ("sender", 5, -2),
        # Receiver: 3 full + 4 counted = 7, plus 2 counted empties; he really got 5 + 2 and returns 10.
        ("receiver", 10, -1),
    ],
)
def test_admin_resolve_both_numbers_recomputes_closed_session(
    app, client, db, admin_claim_headers, admin_user, closer, returned, closed_discrepancy
):
    sender, receiver = _pair(db, "21")
    trips = {"sender": _trip(db, sender, loaded=8, collected=4), "receiver": _trip(db, receiver, loaded=3)}
    drivers = {"sender": sender, "receiver": receiver}
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "fullQuantity": 6, "emptyQuantity": 3})
    counted = _confirm(client, app, sent["id"], receiver, {"confirmedFull": 4, "confirmedEmpty": 2})
    assert counted.get_json()["data"]["status"] == "disputed"
    closed = client.post(
        CLOSE_SESSION, headers=_headers(app, drivers[closer].id), json={"bottles_returned_to_warehouse": returned}
    )
    assert closed.get_json()["data"]["discrepancy"] == closed_discrepancy

    resolved = client.post(
        RESOLVE.format(sent["id"]),
        headers=admin_claim_headers,
        json={"resolvedFull": 5, "resolvedEmpty": 2, "resolutionNotes": "Counted again at the depot"},
    )

    assert resolved.status_code == 200, resolved.get_json()
    data = resolved.get_json()["data"]
    assert _split(data) == {
        "declared_full": 6,
        "declared_empty": 3,
        "declared_total": 9,
        "confirmed_full": 5,
        "confirmed_empty": 2,
        "confirmed_total": 7,
        "status": "resolved",
    }
    assert (data["resolved_by_user_id"], data["resolution_notes"]) == (admin_user.id, "Counted again at the depot")
    for trip in trips.values():
        db.session.refresh(trip)
    assert _moved_out(trips["sender"]) == (5, 2)
    assert _moved_in(trips["receiver"]) == (5, 2)
    # The closed trip's books now balance; the open one has no discrepancy until it closes.
    still_open = "receiver" if closer == "sender" else "sender"
    assert (trips[closer].status, trips[closer].discrepancy) == (DriverBottleSessionStatus.CLOSED, 0)
    assert (trips[still_open].status, trips[still_open].discrepancy) == (DriverBottleSessionStatus.OPEN, None)


# --------------------------------------------------------------------------- #
# The lists: admin Transfers tab and the receiver's inbox
# --------------------------------------------------------------------------- #


def test_admin_transfers_list_has_names_and_split(app, client, db, admin_claim_headers):
    sender, receiver = _pair(db, "31")
    _trip(db, sender, loaded=9, collected=4)
    waiting = _send(client, app, sender, {"receiverDriverId": receiver.id, "fullQuantity": 5, "emptyQuantity": 2})
    counted = _send(client, app, sender, {"receiverDriverId": receiver.id, "fullQuantity": 3, "emptyQuantity": 1})
    assert _confirm(client, app, counted["id"], receiver, {"confirmedFull": 3, "confirmedEmpty": 1}).status_code == 200

    listed = client.get(ADMIN_TRANSFERS, headers=admin_claim_headers)

    assert listed.status_code == 200, listed.get_json()
    rows = {row["id"]: row for row in listed.get_json()["data"]["items"]}
    names = {"sender_name": "Ali Driver", "receiver_name": "Vali Driver"}
    assert {tid: {**_split(row), **{k: row[k] for k in names}} for tid, row in rows.items()} == {
        waiting["id"]: {
            **names,
            "declared_full": 5,
            "declared_empty": 2,
            "declared_total": 7,
            "confirmed_full": None,
            "confirmed_empty": None,
            "confirmed_total": None,
            "status": "pending",
        },
        counted["id"]: {
            **names,
            "declared_full": 3,
            "declared_empty": 1,
            "declared_total": 4,
            "confirmed_full": 3,
            "confirmed_empty": 1,
            "confirmed_total": 4,
            "status": "confirmed",
        },
    }


def test_the_receivers_inbox_rows_carry_both_numbers(app, client, db):
    sender, receiver = _pair(db, "32")
    _trip(db, sender, loaded=9, collected=4)
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "fullQuantity": 5, "emptyQuantity": 2})

    inbox = client.get(PENDING, headers=_headers(app, receiver.id))

    assert inbox.status_code == 200, inbox.get_json()
    [row] = inbox.get_json()["data"]
    assert (row["id"], row["sender_name"], row["receiver_name"]) == (sent["id"], "Ali Driver", "Vali Driver")
    assert (row["declared_full"], row["declared_empty"], row["declared_total"]) == (5, 2, 7)


# --------------------------------------------------------------------------- #
# Old data in flight: the running staff_bot's bodies and its push renderer
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        {"receiver_driver_id": None, "quantity": 3},  # the running StaffAPIClient.initiate_bottle_transfer body
        {"receiverDriverId": None, "quantity": 3},
    ],
    ids=["running-bot-body", "camel-case"],
)
def test_legacy_quantity_field_still_accepted(app, client, db, body):
    sender, receiver = _pair(db, "41")
    trip = _trip(db, sender, loaded=7, collected=3)
    receiver_trip = _trip(db, receiver, loaded=4)
    key = "receiver_driver_id" if "receiver_driver_id" in body else "receiverDriverId"

    sent = _send(client, app, sender, {**body, key: receiver.id})

    assert (sent["declared_full"], sent["declared_empty"]) == (3, 0)
    db.session.refresh(trip)
    assert _moved_out(trip) == (3, 0)
    # Counted the way the bot sends other numbers (snake_case): the 3 full it showed and no empties, so it confirms.
    confirmed = _confirm(client, app, sent["id"], receiver, {"confirmed_full": 3, "confirmed_empty": 0})
    assert confirmed.status_code == 200, confirmed.get_json()
    assert (confirmed.get_json()["data"]["status"], confirmed.get_json()["data"]["confirmed_empty"]) == (
        "confirmed",
        0,
    )
    db.session.refresh(receiver_trip)
    assert _moved_in(receiver_trip) == (3, 0)


def test_the_push_carries_both_numbers_and_the_bot_renders_both(app, client, db, pushes, seeded_copy):
    from staff_bot.webhook_server import _render_bottle_event

    seeded_copy(
        "staff.bottles.notify.transfer_received_lines",
        "staff.delivery.transfer_line_full",
        "staff.delivery.transfer_line_empty",
        "staff.delivery.transfer_line_product",
        "staff.delivery.transfer_confirm_lines_button",
        "staff.delivery.transfer_custom_count_button",
        "staff.delivery.transfer_reject_button",
        "staff.back",
    )
    sender, receiver = _pair(db, "43")
    _trip(db, sender, loaded=7, collected=3)
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "fullQuantity": 5, "emptyQuantity": 2})
    [((_chat, event, payload), _kwargs)] = pushes
    assert payload == {
        "id": sent["id"],
        "declared_quantity": 5,
        "declared_empty_quantity": 2,
        "sender_name": "Ali Driver",
        "lines": [
            {
                "kind": "full",
                "product_id": None,
                "product_name": None,
                "declared": 5,
                "confirmed": None,
                "resolved": None,
            },
            {
                "kind": "empty",
                "product_id": None,
                "product_name": None,
                "declared": 2,
                "confirmed": None,
                "resolved": None,
            },
        ],
    }

    text, keyboard = _render_bottle_event(event, payload, "en")

    assert text == "📥 Ali Driver sent you: 18.9 L full 5 · empty 2"
    buttons = [(button.text, button.callback_data) for row in keyboard.inline_keyboard for button in row]
    assert buttons[:3] == [
        ("✅ Confirm 18.9 L full 5 · empty 2", f"staff_transfer_confirm_{sent['id']}"),
        ("✏️ Enter other numbers", f"staff_transfer_custom_{sent['id']}"),
        ("❌ Reject", f"staff_transfer_reject_{sent['id']}"),
    ]


def _bot_body(monkeypatch, call, *args, **kwargs):
    """The (endpoint, body) the staff bot's own `StaffAPIClient` sends for one call, captured at its HTTP seam."""
    import asyncio

    from staff_bot.api_client import StaffAPIClient

    bot_client = StaffAPIClient()
    sent = []

    async def capture(method, endpoint, token=None, data=None, **_kwargs):
        sent.append((method, endpoint, data))

    monkeypatch.setattr(bot_client, "_make_request", capture)
    asyncio.run(getattr(bot_client, call)("staff-token", *args, **kwargs))
    [(method, endpoint, body)] = sent
    assert method == "POST"
    return endpoint, body


@pytest.mark.parametrize(
    "amounts, declared",
    [({"send_all": True}, (5, 2)), ({"full_quantity": 4, "empty_quantity": 1}, (4, 1))],
    ids=["send-all", "choose-amounts"],
)
def test_the_staff_bots_own_send_body_is_what_the_endpoint_takes(
    app, client, db, pushes, monkeypatch, amounts, declared
):
    """The bodies come from the bot's client itself, so a field renamed on either side fails here."""
    sender, receiver = _pair(db, "50")
    trip = _trip(db, sender, loaded=5, collected=2)

    endpoint, body = _bot_body(monkeypatch, "initiate_bottle_transfer", receiver.id, **amounts)
    sent = client.post(endpoint, headers=_headers(app, sender.id), json=body)

    assert sent.status_code == 201, sent.get_json()
    assert (sent.get_json()["data"]["declared_full"], sent.get_json()["data"]["declared_empty"]) == declared
    db.session.refresh(trip)
    assert _moved_out(trip) == declared


@pytest.mark.parametrize(
    "count, status, credited",
    [
        ({"confirm_as_declared": True}, "confirmed", (5, 2)),
        ({"confirmed_full": 5, "confirmed_empty": 1}, "disputed", (5, 1)),
    ],
    ids=["one-tap", "different-count"],
)
def test_the_staff_bots_own_confirm_body_is_what_the_endpoint_takes(
    app, client, db, pushes, monkeypatch, count, status, credited
):
    sender, receiver = _pair(db, "51")
    _trip(db, sender, loaded=5, collected=2)
    receiver_trip = _trip(db, receiver, loaded=4)
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "fullQuantity": 5, "emptyQuantity": 2})

    endpoint, body = _bot_body(monkeypatch, "confirm_bottle_transfer", sent["id"], **count)
    answered = client.post(endpoint, headers=_headers(app, receiver.id), json=body)

    assert answered.status_code == 200, answered.get_json()
    data = answered.get_json()["data"]
    assert (data["status"], data["confirmed_full"], data["confirmed_empty"]) == (status, *credited)
    assert isinstance(data["admins_alerted"], bool)
    db.session.refresh(receiver_trip)
    assert _moved_in(receiver_trip) == credited
