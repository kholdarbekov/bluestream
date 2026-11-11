"""After the receiver counts a transfer (spec 2026-11-09 T4, T5 and the §4.2 re-trigger).

An exact count that leaves the sender's truck empty closes his trip on its own, with nothing returned to the warehouse
and his position left where it is; the sender is told what was counted and whether his trip closed. A different count
posts one alert to the admins' Slack alerts channel, like every other application alert (owner decision 2026-11-10),
and tells the sender both counts. A count that puts full bottles on the receiver's truck offers the pool to auto-assign
again.

Driven through the HTTP routes the staff bot and the admin page call, with the camelCase bodies they send. Only the
outbound calls are faked (signature-enforcing spies): Celery publishing, and the Slack post (`send_slack_alert`, or the
HTTP call under it). The sender's message is rendered by the running bot's own renderer, fed the seeded copy. The
failure tests break a step after the commit the way a database error does: a flush that fails, which leaves the
request's session refusing every later use until it is rolled back.
"""

import importlib.util
import inspect
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests
from flask_jwt_extended import create_access_token
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from business_app.models.bottle import DriverBottleSession, DriverBottleTransfer, DriverSessionMembership
from business_app.models.translation import Translation
from business_app.services import bottle_notifications
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.staff_service import StaffService
from business_app.tasks import delivery_monitoring_tasks, staff_tasks
from business_app.utils import slack_alerts
from shared.enums import DeliveryStatus, DriverBottleSessionStatus
from staff_bot.i18n import i18n
from staff_bot.webhook_server import _render_bottle_event
from tests.integration.test_bottle_transfer_split_api import _confirm, _pair, _send, _trip
from tests.unit.test_bottle_transfer_and_join_requests import _driver
from tests.unit.test_sales_agent_tasks import _spy
from tests.unit.test_staff_error_endpoints import _assigned_delivery, _order_with_bottles

pytestmark = pytest.mark.integration

RESOLVE = "/api/v1/admin/bottles/transfers/{}/resolve"
# Where the admin page lives (config ADMIN_UI_BASE_URL); the alert links its Bottle Tracking page.
ADMIN_SITE = "https://admin.aqua-element.uz"
# RFC 2606 `.invalid`: a well-formed webhook that can never resolve, for the test that drives the real Slack helper.
UNREACHABLE_WEBHOOK = "https://hooks.slack-must-not-be-reached.invalid/services/T000/B000/dispute-test"
TASHKENT = ZoneInfo("Asia/Tashkent")
# What the staff bot reads from the confirm route's answer: the transfer it counted, and whether an admin was alerted.
RESPONSE_KEYS = (
    "id",
    "status",
    "sender_name",
    "receiver_name",
    "receiver_session_id",
    "declared_full",
    "declared_empty",
    "confirmed_full",
    "confirmed_empty",
    "admins_alerted",
)
SENDER_COPY = (
    "staff.bottles.notify.transfer_confirmed",
    "staff.bottles.notify.trip_auto_closed",
    "staff.bottles.notify.transfer_disputed",
    "staff.bottles.notify.transfer_disputed_no_admin",
)

_seed_spec = importlib.util.spec_from_file_location(
    "seed_staff_translations_auto_close",
    Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py",
)
_SEED = importlib.util.module_from_spec(_seed_spec)
_seed_spec.loader.exec_module(_SEED)


@pytest.fixture
def pushes(monkeypatch):
    return _spy(monkeypatch, staff_tasks.push_bottle_event, "delay")


@pytest.fixture
def anchor_spy(monkeypatch):
    """Every warehouse anchor of a driver's stored position (`StaffService` owns the write)."""
    return _spy(monkeypatch, StaffService, "anchor_driver_at_warehouse")


@pytest.fixture
def reoffers(monkeypatch):
    return _spy(monkeypatch, delivery_monitoring_tasks.reoffer_pool_deliveries_task, "delay")


@pytest.fixture
def bot_copy(monkeypatch):
    """The running bot's English for the sender's message, as scripts/seed_staff_translations.py ships it."""
    rows = {key: _SEED._curated_value(key, "en") for key in SENDER_COPY}
    assert all(rows.values()), rows
    monkeypatch.setitem(i18n.translations, "en", {**i18n.translations.get("en", {}), **rows})


@pytest.fixture
def slack(app, monkeypatch):
    """Every post to the admins' Slack alerts channel, through its one door (`slack_alerts.send_slack_alert`); Slack
    accepts each. The admin site is pinned, so the alert's link reads the same wherever the suite runs."""
    monkeypatch.setitem(app.config, "ADMIN_UI_BASE_URL", ADMIN_SITE)
    return _spy(monkeypatch, slack_alerts, "send_slack_alert", result=True)


def _sender_message(pushes, sender):
    """The last push, which must be the sender's; rendered by the running bot."""
    (chat, event, payload), kwargs = pushes[-1]
    assert (chat, kwargs) == (int(sender.telegram_id), {})
    return event, payload


def _break_the_session(db):
    """A write that fails at flush (a NOT NULL column left empty). SQLAlchemy then refuses every later use of the
    session, a lazy load included, until it is rolled back: what a database error leaves behind."""
    db.session.add(Translation(key=None, language="en", value="never stored"))
    db.session.flush()


def _role_headers(app, user):
    """The admin page's bearer: the user's id and the role claim the panel's routes read."""
    with app.app_context():
        token = create_access_token(identity=str(user.id), additional_claims={"role": user.role.value})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# --------------------------------------------------------------------------- #
# Auto-close (T4)
# --------------------------------------------------------------------------- #


def test_send_all_exact_confirm_auto_closes_sender(app, client, db, anchor_spy, pushes, reoffers, bot_copy, slack):
    sender, receiver = _pair(db, "51")
    sender_trip = _trip(db, sender, loaded=5, collected=2)
    receiver_trip = _trip(db, receiver, loaded=4)
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "sendAll": True})

    confirmed = _confirm(client, app, sent["id"], receiver, {"confirmAsDeclared": True})

    assert confirmed.status_code == 200, confirmed.get_json()
    # A confirmed count alerts nobody (not a word to Slack), and the route says so.
    assert (confirmed.get_json()["data"]["status"], confirmed.get_json()["data"]["admins_alerted"]) == (
        "confirmed",
        False,
    )
    assert slack == []
    transfer = db.session.get(DriverBottleTransfer, sent["id"])
    db.session.refresh(sender_trip)
    assert (
        sender_trip.status,
        sender_trip.bottles_returned_to_warehouse,
        sender_trip.discrepancy,
        sender_trip.closed_by_user_id,
        sender_trip.notes,
    ) == (
        DriverBottleSessionStatus.CLOSED,
        0,
        0,
        sender.id,
        f"Closed automatically after transfer {transfer.transfer_ref}: nothing left on the truck.",
    )
    assert sender_trip.closed_at is not None
    # The trip closed mid-route: the driver is wherever he handed the bottles over, not at the depot.
    assert anchor_spy == []
    db.session.refresh(receiver_trip)
    assert (receiver_trip.status, receiver_trip.bottles_transferred_in, receiver_trip.empties_transferred_in) == (
        DriverBottleSessionStatus.OPEN,
        5,
        2,
    )

    event, payload = _sender_message(pushes, sender)
    assert (event, payload) == (
        "transfer_confirmed",
        {
            "id": sent["id"],
            "receiver_name": "Vali Driver",
            "confirmed_full": 5,
            "confirmed_empty": 2,
            "auto_closed": True,
        },
    )
    text, keyboard = _render_bottle_event(event, payload, "en")
    assert text == "Vali Driver confirmed 5 full + 2 empty.\nYour trip was closed: nothing is left on your truck."
    assert keyboard is None
    assert reoffers == [((), {})]


def _second_receiver(db):
    return _driver(db, "+998901255203", "Gani")


# The driver's stops not yet delivered (T4): each one keeps his trip open.
UNDELIVERED = {
    "undelivered_assigned": DeliveryStatus.ASSIGNED,
    "undelivered_picked_up": DeliveryStatus.PICKED_UP,
    "undelivered_in_transit": DeliveryStatus.IN_TRANSIT,
    "undelivered_arrived": DeliveryStatus.ARRIVED,
}


@pytest.mark.parametrize(
    "blocker",
    [
        "disputed",
        *UNDELIVERED,
        "active_codriver",
        "second_pending_transfer",
        "leftover_full",
        "leftover_empty",
    ],
)
def test_no_auto_close_when(app, client, db, anchor_spy, pushes, sample_user, sample_product, blocker):
    """Each setup breaks exactly one T4 condition; everything else would close the trip."""
    sender, receiver = _pair(db, "52")
    sender_trip = _trip(db, sender, loaded=5, collected=2)
    _trip(db, receiver, loaded=4)
    send = {"receiverDriverId": receiver.id, "sendAll": True}
    count = {"confirmAsDeclared": True}
    confirmed_pair = (5, 2)
    if blocker == "disputed":
        count, confirmed_pair = {"confirmedFull": 5, "confirmedEmpty": 1}, (5, 1)
    elif blocker in UNDELIVERED:
        # Send all sends every bottle on the truck, promised ones included (T3), so 5 + 2 still leave it; the stop is
        # still his to deliver.
        order = _order_with_bottles(db, sample_user, sample_product, quantity=3)
        _assigned_delivery(db, order, sender, status=UNDELIVERED[blocker])
    elif blocker == "active_codriver":
        db.session.add(
            DriverSessionMembership(
                session_id=sender_trip.id, session_owner_id=sender.id, member_driver_id=_second_receiver(db).id
            )
        )
        db.session.commit()
    elif blocker == "second_pending_transfer":
        _send(client, app, sender, {"receiverDriverId": _second_receiver(db).id, "fullQuantity": 2})
        send, confirmed_pair = {"receiverDriverId": receiver.id, "fullQuantity": 3, "emptyQuantity": 2}, (3, 2)
    elif blocker == "leftover_full":
        send, confirmed_pair = {"receiverDriverId": receiver.id, "fullQuantity": 4, "emptyQuantity": 2}, (4, 2)
    elif blocker == "leftover_empty":
        send, confirmed_pair = {"receiverDriverId": receiver.id, "fullQuantity": 5, "emptyQuantity": 1}, (5, 1)
    sent = _send(client, app, sender, send)

    answered = _confirm(client, app, sent["id"], receiver, count)

    assert answered.status_code == 200, answered.get_json()
    db.session.refresh(sender_trip)
    assert (sender_trip.status, sender_trip.closed_at, sender_trip.discrepancy) == (
        DriverBottleSessionStatus.OPEN,
        None,
        None,
    )
    assert anchor_spy == []
    event, payload = _sender_message(pushes, sender)
    if blocker == "disputed":
        # Tests never reach Slack (TestingConfig has no webhook), so nobody was alerted and the message must not say
        # one will check.
        assert (event, payload) == (
            "transfer_disputed",
            {
                "id": sent["id"],
                "receiver_name": "Vali Driver",
                "declared_full": 5,
                "declared_empty": 2,
                "confirmed_full": 5,
                "confirmed_empty": 1,
                "admins_alerted": False,
            },
        )
    else:
        assert (event, payload) == (
            "transfer_confirmed",
            {
                "id": sent["id"],
                "receiver_name": "Vali Driver",
                "confirmed_full": confirmed_pair[0],
                "confirmed_empty": confirmed_pair[1],
                "auto_closed": False,
            },
        )


def test_the_conditions_are_read_when_the_close_runs_not_when_the_count_was_written(
    app, client, db, pushes, sample_user, sample_product, monkeypatch
):
    """An admin assigns the sender an order between the receiver's committed count and the auto-close: the trip is his
    to deliver from, so it stays open."""
    sender, receiver = _pair(db, "53")
    sender_trip = _trip(db, sender, loaded=5, collected=2)
    _trip(db, receiver, loaded=4)
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "sendAll": True})
    real_auto_close = BottleTrackingService._auto_close_sender_if_emptied

    def an_admin_assigns_first(self, transfer):
        assert db.session.get(DriverBottleTransfer, transfer.id).status.value == "confirmed"
        _assigned_delivery(db, _order_with_bottles(db, sample_user, sample_product, quantity=3), sender)
        return real_auto_close(self, transfer)

    monkeypatch.setattr(BottleTrackingService, "_auto_close_sender_if_emptied", an_admin_assigns_first)

    confirmed = _confirm(client, app, sent["id"], receiver, {"confirmAsDeclared": True})

    assert confirmed.status_code == 200, confirmed.get_json()
    db.session.refresh(sender_trip)
    assert sender_trip.status == DriverBottleSessionStatus.OPEN
    assert _sender_message(pushes, sender)[1]["auto_closed"] is False


def test_a_failed_auto_close_never_fails_the_receivers_count(app, client, db, pushes, monkeypatch):
    sender, receiver = _pair(db, "54")
    sender_trip = _trip(db, sender, loaded=5, collected=2)
    receiver_trip = _trip(db, receiver, loaded=4)
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "sendAll": True})
    signature = inspect.signature(BottleTrackingService.close_bottle_session)

    def close_breaks(*args, **kwargs):
        signature.bind(*args, **kwargs)
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(BottleTrackingService, "close_bottle_session", close_breaks)

    confirmed = _confirm(client, app, sent["id"], receiver, {"confirmAsDeclared": True})

    assert confirmed.status_code == 200, confirmed.get_json()
    assert confirmed.get_json()["data"]["status"] == "confirmed"
    db.session.refresh(receiver_trip)
    assert (receiver_trip.bottles_transferred_in, receiver_trip.empties_transferred_in) == (5, 2)
    db.session.refresh(sender_trip)
    assert sender_trip.status == DriverBottleSessionStatus.OPEN
    # Told only what happened: the count, not a close that did not happen.
    assert _sender_message(pushes, sender) == (
        "transfer_confirmed",
        {
            "id": sent["id"],
            "receiver_name": "Vali Driver",
            "confirmed_full": 5,
            "confirmed_empty": 2,
            "auto_closed": False,
        },
    )


def test_a_sender_message_that_breaks_never_fails_the_receivers_count(app, client, db, pushes, reoffers, monkeypatch):
    """The count has committed when the sender's message fails on a database error: the receiver still gets his 200
    and the transfer he counted (a 500 would send the bot back to a Confirm that answers "not pending"), and the step
    after it still offers the pool his new full bottles."""
    sender, receiver = _pair(db, "60")
    _trip(db, sender, loaded=7, collected=3)
    receiver_trip = _trip(db, receiver, loaded=4)
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "fullQuantity": 5, "emptyQuantity": 2})
    pushes.clear()
    signature = inspect.signature(bottle_notifications.notify_transfer_outcome_to_sender)

    def message_breaks(*args, **kwargs):
        signature.bind(*args, **kwargs)
        _break_the_session(db)

    monkeypatch.setattr(bottle_notifications, "notify_transfer_outcome_to_sender", message_breaks)

    confirmed = _confirm(client, app, sent["id"], receiver, {"confirmAsDeclared": True})

    assert confirmed.status_code == 200, confirmed.get_json()
    data = confirmed.get_json()["data"]
    assert {key: data[key] for key in RESPONSE_KEYS} == {
        "id": sent["id"],
        "status": "confirmed",
        "sender_name": "Ali Driver",
        "receiver_name": "Vali Driver",
        "receiver_session_id": receiver_trip.id,
        "declared_full": 5,
        "declared_empty": 2,
        "confirmed_full": 5,
        "confirmed_empty": 2,
        "admins_alerted": False,
    }
    db.session.refresh(receiver_trip)
    assert (receiver_trip.bottles_transferred_in, receiver_trip.empties_transferred_in) == (5, 2)
    assert pushes == []
    assert reoffers == [((), {})]


def test_a_last_read_the_database_refuses_still_answers_the_count_as_committed(
    app, client, db, pushes, reoffers
):
    """Every step after the count has run (the sender is told, the pool is offered his 5 new full bottles) when the
    database stops serving the transfer and its drivers. The route's last read of the transfer fails; the receiver still
    gets his 200 with the count he made, as read right after the commit (a 500 would send his bot back to a Confirm
    that answers "not pending")."""
    sender, receiver = _pair(db, "62")
    _trip(db, sender, loaded=7, collected=3)
    receiver_trip = _trip(db, receiver, loaded=4)
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "fullQuantity": 5, "emptyQuantity": 2})
    pushes.clear()

    def database_down_once_the_pool_is_offered(_conn, _cursor, statement, _parameters, _context, _executemany):
        if reoffers and ("driver_bottle_transfers" in statement or "FROM users" in statement):
            raise OperationalError(statement, None, Exception("server closed the connection unexpectedly"))

    event.listen(db.engine, "before_cursor_execute", database_down_once_the_pool_is_offered)
    try:
        confirmed = _confirm(client, app, sent["id"], receiver, {"confirmAsDeclared": True})
    finally:
        event.remove(db.engine, "before_cursor_execute", database_down_once_the_pool_is_offered)

    assert confirmed.status_code == 200, confirmed.get_json()
    data = confirmed.get_json()["data"]
    assert {key: data[key] for key in RESPONSE_KEYS} == {
        "id": sent["id"],
        "status": "confirmed",
        "sender_name": "Ali Driver",
        "receiver_name": "Vali Driver",
        "receiver_session_id": receiver_trip.id,
        "declared_full": 5,
        "declared_empty": 2,
        "confirmed_full": 5,
        "confirmed_empty": 2,
        "admins_alerted": False,
    }
    assert [event for (_chat, event, _payload), _kwargs in pushes] == ["transfer_confirmed"]
    assert reoffers == [((), {})]
    db.session.refresh(receiver_trip)
    assert (receiver_trip.bottles_transferred_in, receiver_trip.empties_transferred_in) == (5, 2)


# --------------------------------------------------------------------------- #
# Disputes (T5)
# --------------------------------------------------------------------------- #


def _dispute_alert(ref, *, sender, receiver, declared, counted, difference, stamp):
    """The Slack alert, line for line. `stamp` is the Time line's local time; the rest is fixed by the transfer."""
    return (
        f"⚠️ *Bottle transfer disputed* — {ref}\n"
        f"Sender: {sender} declared {declared}\n"
        f"Receiver: {receiver} counted {counted}\n"
        f"Difference (counted − declared): {difference}\n"
        f"Time: {stamp} | Env: testing\n"
        "Action: resolve it on the admin Bottle Tracking page, Bottle Transfers tab: "
        "https://admin.aqua-element.uz/delivery/bottles"
    )


def _posted_alert(slack, before, after):
    """The one post Slack got, and the local time its Time line reads: Tashkent's (+0500), taken during the count."""
    [((text,), kwargs)] = slack
    assert kwargs == {}
    [time_line] = [line for line in text.split("\n") if line.startswith("Time: ")]
    stamp = datetime.strptime(time_line, "Time: %Y-%m-%d %H:%M:%S %z | Env: testing")
    assert stamp.utcoffset() == timedelta(hours=5)
    assert before.replace(microsecond=0) <= stamp <= after
    return text, stamp.strftime("%Y-%m-%d %H:%M:%S %z")


def test_dispute_alerts_admins_and_tells_sender(app, client, db, pushes, slack, bot_copy, admin_claim_headers):
    sender, receiver = _pair(db, "55")
    sender_trip = _trip(db, sender, loaded=7, collected=3)
    _trip(db, receiver, loaded=4)
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "fullQuantity": 5, "emptyQuantity": 2})

    before = datetime.now(TASHKENT)
    disputed = _confirm(client, app, sent["id"], receiver, {"confirmedFull": 4, "confirmedEmpty": 1})
    after = datetime.now(TASHKENT)

    assert disputed.status_code == 200, disputed.get_json()
    assert (disputed.get_json()["data"]["status"], disputed.get_json()["data"]["admins_alerted"]) == (
        "disputed",
        True,
    )
    transfer = db.session.get(DriverBottleTransfer, sent["id"])
    # One post to the admins' Slack alerts channel, every fact on its own line.
    text, stamp = _posted_alert(slack, before, after)
    assert text == _dispute_alert(
        transfer.transfer_ref,
        sender="Ali Driver",
        receiver="Vali Driver",
        declared="5 full + 2 empty",
        counted="4 full + 1 empty",
        difference="-1 full, -1 empty",
        stamp=stamp,
    )

    event, payload = _sender_message(pushes, sender)
    assert (event, payload) == (
        "transfer_disputed",
        {
            "id": sent["id"],
            "receiver_name": "Vali Driver",
            "declared_full": 5,
            "declared_empty": 2,
            "confirmed_full": 4,
            "confirmed_empty": 1,
            "admins_alerted": True,
        },
    )
    text, keyboard = _render_bottle_event(event, payload, "en")
    assert text == "Vali Driver counted 4 full + 1 empty; you sent 5 full + 2 empty. An admin will check it."
    assert keyboard is None

    # The admin the alert reached settles it from the page it names.
    resolved = client.post(
        RESOLVE.format(sent["id"]),
        headers=admin_claim_headers,
        json={"resolvedFull": 5, "resolvedEmpty": 1, "resolutionNotes": "Counted again at the depot"},
    )
    assert resolved.status_code == 200, resolved.get_json()
    db.session.refresh(sender_trip)
    assert (sender_trip.bottles_transferred_out, sender_trip.empties_transferred_out) == (5, 1)


def test_a_drivers_name_never_pings_the_channel_and_each_difference_is_its_own(app, client, db, pushes, slack):
    """A name is the driver's own text: Slack's control characters in it are escaped, so `<!channel>` reads as
    written instead of notifying everyone. More empties counted than sent, the full ones matching: each component's
    difference stands apart, signed, a match as 0."""
    sender = _driver(db, "+99890125631", "<!channel> & Ali")
    receiver = _driver(db, "+99890125632", "Vali")
    _trip(db, sender, loaded=7, collected=3)
    _trip(db, receiver, loaded=4)
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "fullQuantity": 3, "emptyQuantity": 2})

    before = datetime.now(TASHKENT)
    disputed = _confirm(client, app, sent["id"], receiver, {"confirmedFull": 3, "confirmedEmpty": 5})
    after = datetime.now(TASHKENT)

    assert disputed.status_code == 200, disputed.get_json()
    assert disputed.get_json()["data"]["admins_alerted"] is True
    text, stamp = _posted_alert(slack, before, after)
    assert text == _dispute_alert(
        db.session.get(DriverBottleTransfer, sent["id"]).transfer_ref,
        sender="&lt;!channel&gt; &amp; Ali Driver",
        receiver="Vali Driver",
        declared="3 full + 2 empty",
        counted="3 full + 5 empty",
        difference="0 full, +3 empty",
        stamp=stamp,
    )


@pytest.mark.parametrize("why", ["slack_not_configured", "slack_refuses_the_post", "the_slack_sender_raises"])
def test_a_dispute_slack_did_not_take_never_says_an_admin_will_check(
    app, client, db, pushes, reoffers, bot_copy, monkeypatch, why
):
    """Slack is the only place the dispute is announced: when it did not take the post, the receiver's answer and the
    sender's message both say nobody was told, and the count still stands (200, the pool offered his full bottles)."""
    posts = []
    if why == "slack_not_configured":
        # The real helper, with no webhook: it posts nothing and answers False.
        monkeypatch.setitem(app.config, "SLACK_ALERTS_WEBHOOK_URL", None)
    if why == "slack_refuses_the_post":
        # The real helper; the HTTP call under it answers 500.
        monkeypatch.setitem(app.config, "SLACK_ALERTS_WEBHOOK_URL", UNREACHABLE_WEBHOOK)
        refused = requests.Response()
        refused.status_code = 500
        posts = _spy(monkeypatch, slack_alerts.requests, "post", result=refused)
    # Every alert handed to Slack's door, passed on to the real helper; or, never by its contract but a step after the
    # commit must not cost the receiver his count, a door that raises.
    real_send, signature, asked = slack_alerts.send_slack_alert, inspect.signature(slack_alerts.send_slack_alert), []

    def slack_door(*args, **kwargs):
        asked.append(signature.bind(*args, **kwargs).arguments["text"])
        if why == "the_slack_sender_raises":
            raise RuntimeError("slack client bug")
        return real_send(*args, **kwargs)

    monkeypatch.setattr(slack_alerts, "send_slack_alert", slack_door)
    sender, receiver = _pair(db, "56")
    _trip(db, sender, loaded=7, collected=3)
    receiver_trip = _trip(db, receiver, loaded=4)
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "fullQuantity": 5, "emptyQuantity": 2})

    disputed = _confirm(client, app, sent["id"], receiver, {"confirmedFull": 4, "confirmedEmpty": 1})

    assert disputed.status_code == 200, disputed.get_json()
    data = disputed.get_json()["data"]
    # The receiver's reply reads the same fact as the sender's message: nobody was alerted.
    assert {key: data[key] for key in RESPONSE_KEYS} == {
        "id": sent["id"],
        "status": "disputed",
        "sender_name": "Ali Driver",
        "receiver_name": "Vali Driver",
        "receiver_session_id": receiver_trip.id,
        "declared_full": 5,
        "declared_empty": 2,
        "confirmed_full": 4,
        "confirmed_empty": 1,
        "admins_alerted": False,
    }
    # Slack was asked, once, with this dispute's alert.
    [alert] = asked
    transfer_ref = db.session.get(DriverBottleTransfer, sent["id"]).transfer_ref
    assert alert.startswith(f"⚠️ *Bottle transfer disputed* — {transfer_ref}\n"), alert
    if why == "slack_refuses_the_post":
        # The real helper posted it to the configured webhook, and Slack refused.
        assert posts == [((UNREACHABLE_WEBHOOK,), {"json": {"text": alert}, "timeout": 5})]
    event, payload = _sender_message(pushes, sender)
    assert (event, payload["admins_alerted"]) == ("transfer_disputed", False)
    text, _keyboard = _render_bottle_event(event, payload, "en")
    assert text == (
        "Vali Driver counted 4 full + 1 empty; you sent 5 full + 2 empty. Please tell an admin so they can settle it."
    )
    assert "admin will check" not in text
    assert reoffers == [((), {})]


def test_the_resolve_route_lets_in_admins_and_managers_never_operators(
    app, client, db, pushes, reoffers, admin_user, manager_user, operator_user
):
    """The dispute alert names the admin page; its resolve route (TRANSFER_DISPUTE_RESOLVE_PERMISSIONS) lets the admin
    and the manager settle a dispute, and refuses the operator. Each resolves a dispute of his own."""
    staff = (admin_user, manager_user, operator_user)
    sender, receiver = _pair(db, "59")
    _trip(db, sender, loaded=9, collected=3)
    _trip(db, receiver, loaded=4)
    disputes = []
    for _user in staff:
        sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "fullQuantity": 2, "emptyQuantity": 1})
        disputed = _confirm(client, app, sent["id"], receiver, {"confirmedFull": 1, "confirmedEmpty": 1})
        assert disputed.get_json()["data"]["status"] == "disputed", disputed.get_json()
        disputes.append(sent["id"])

    answers = {}
    for user, transfer_id in zip(staff, disputes):
        resolved = client.post(
            RESOLVE.format(transfer_id),
            headers=_role_headers(app, user),
            json={"resolvedFull": 2, "resolvedEmpty": 1, "resolutionNotes": "Counted again at the depot"},
        )
        answers[user.id] = (resolved.status_code, resolved.get_json().get("message"))

    assert answers == {
        admin_user.id: (200, "Transfer dispute resolved successfully"),
        manager_user.id: (200, "Transfer dispute resolved successfully"),
        operator_user.id: (403, "Insufficient permissions for this action"),
    }
    db.session.expire_all()
    assert db.session.get(DriverBottleTransfer, disputes[2]).status.value == "disputed"


# --------------------------------------------------------------------------- #
# Re-offering the pool (§4.2 re-trigger, A3)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "send, count, reoffered",
    [
        ({"fullQuantity": 3, "emptyQuantity": 1}, {"confirmAsDeclared": True}, True),
        ({"emptyQuantity": 2}, {"confirmAsDeclared": True}, False),
        # Disputed, but the receiver's truck holds the 2 full bottles he counted until an admin decides.
        ({"fullQuantity": 3, "emptyQuantity": 1}, {"confirmedFull": 2, "confirmedEmpty": 1}, True),
    ],
    ids=["full-confirmed", "empties-only", "disputed-with-full"],
)
def test_full_confirm_reoffers_pool_empties_only_does_not(app, client, db, pushes, reoffers, send, count, reoffered):
    sender, receiver = _pair(db, "57")
    _trip(db, sender, loaded=7, collected=3)
    _trip(db, receiver, loaded=4)
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, **send})
    assert reoffers == []

    answered = _confirm(client, app, sent["id"], receiver, count)

    assert answered.status_code == 200, answered.get_json()
    assert reoffers == ([((), {})] if reoffered else [])


def test_a_refused_count_reoffers_nothing_and_tells_nobody(app, client, db, pushes, reoffers):
    sender, receiver = _pair(db, "58")
    _trip(db, sender, loaded=7, collected=3)
    sent = _send(client, app, sender, {"receiverDriverId": receiver.id, "fullQuantity": 5, "emptyQuantity": 2})
    pushes.clear()

    refused = _confirm(client, app, sent["id"], receiver, {"confirmedFull": 5})

    assert refused.status_code == 400, refused.get_json()
    assert (pushes, reoffers) == ([], [])
    assert DriverBottleSession.query.filter_by(driver_user_id=receiver.id).count() == 0
