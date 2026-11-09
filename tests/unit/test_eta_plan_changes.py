"""The §4.9 rule, counted in shadow (honest-ETA Phase 2, Task 9): when a moved promise is worth a message to the
customer, and how many would go out under the limits.

`move_message_reason(baseline, new)` compares a promise with the last one the customer would have been told: "later"
when the window end moved 30 minutes or more later, "earlier" when it moved 60 or more earlier, "day" when the promised
day changed. `messages_that_would_send(rows)` walks one delivery's engine rows, oldest first: the baseline is its first
windowed row, then each row that would have sent a message. At most one message an hour and three an order, none from
22:00 to 08:00 local; a change the limits hold is sent once they allow it, if it still holds against the baseline.

The rows are `DeliveryPromise` objects as the recorder writes them; nothing is stored. Every instant is handed in on the
Tashkent wall clock (`at`), so no test depends on the time of day. `TODAY` is Wednesday 2026-11-04.
"""

from datetime import date, datetime, time, timedelta

import pytest

from business_app.models.delivery_promise import MODEL_ENGINE_S1, DeliveryPromise
from business_app.services.eta_plan.changes import ChangeMessage, messages_that_would_send, move_message_reason
from business_app.utils.delivery_window import window_edge_utc
from shared.constants import ETA_STAGE_WAITING

pytestmark = pytest.mark.unit

TODAY = date(2026, 11, 4)
TOMORROW = TODAY + timedelta(days=1)


def at(hour: int, minute: int = 0, *, day: date = TODAY) -> datetime:
    """`hour:minute` on the Tashkent wall clock on the local date `day`, as aware UTC."""
    return window_edge_utc(day, time(hour, minute))


def promise(computed_at: datetime, ends: datetime, *, promised: date = TODAY, day_kind: str = "today"):
    """An engine row computed at `computed_at` whose window ends at `ends`, promising the local day `promised`. Its
    window is two hours wide; only its end and its day matter to the rule."""
    return DeliveryPromise(
        computed_at=computed_at,
        stage=ETA_STAGE_WAITING,
        model_version=MODEL_ENGINE_S1,
        shown=False,
        expected_at=ends - timedelta(minutes=60),
        window_start=ends - timedelta(minutes=120),
        window_end=ends,
        day_kind=day_kind,
        promised_date=promised,
    )


def unconfirmed(computed_at: datetime):
    """An engine row no car could take: no day, no time, no window."""
    return DeliveryPromise(
        computed_at=computed_at,
        stage=ETA_STAGE_WAITING,
        model_version=MODEL_ENGINE_S1,
        shown=False,
        day_kind="unconfirmed",
    )


def test_later_by_thirty_minutes_sends_one_message():
    baseline = promise(at(10), at(13))

    assert move_message_reason(baseline, promise(at(11), at(13, 29))) is None
    assert move_message_reason(baseline, promise(at(11), at(13, 30))) == "later"
    assert messages_that_would_send([baseline, promise(at(11), at(13, 30))]) == [ChangeMessage(at(11), "later")]


def test_earlier_by_an_hour_sends_one_message():
    baseline = promise(at(10), at(15))

    assert move_message_reason(baseline, promise(at(11), at(14, 1))) is None
    assert move_message_reason(baseline, promise(at(11), at(14))) == "earlier"
    assert messages_that_would_send([baseline, promise(at(11), at(14))]) == [ChangeMessage(at(11), "earlier")]


def test_small_moves_that_add_up_send_one_message():
    """+20 then +20: the first move is too small to tell, so the baseline stays, and the second is 40 minutes later
    than what the customer was told."""
    rows = [promise(at(10), at(13)), promise(at(11), at(13, 20)), promise(at(12), at(13, 40))]

    assert messages_that_would_send(rows) == [ChangeMessage(at(12), "later")]


def test_a_second_change_within_an_hour_sends_none():
    """A message at 10:30 moves the baseline to 13:30. At 10:45 the end moves on to 14:00, 30 minutes later again, but
    within the hour: held. At 11:15 it is back at 13:30, so when the hour is up there is nothing to tell."""
    rows = [
        promise(at(10), at(13)),
        promise(at(10, 30), at(13, 30)),
        promise(at(10, 45), at(14)),
        promise(at(11, 15), at(13, 30)),
    ]

    assert messages_that_would_send(rows) == [ChangeMessage(at(10, 30), "later")]


def test_a_change_the_hour_held_goes_out_when_the_hour_is_up_if_it_still_holds():
    """The same 10:45 move, and no row after it: it still holds at 11:30, an hour after the last message. Before that
    hour is up nothing more has gone out."""
    rows = [promise(at(10), at(13)), promise(at(10, 30), at(13, 30)), promise(at(10, 45), at(14))]

    assert messages_that_would_send(rows) == [ChangeMessage(at(10, 30), "later"), ChangeMessage(at(11, 30), "later")]
    assert messages_that_would_send(rows, until=at(11, 29)) == [ChangeMessage(at(10, 30), "later")]


def test_quiet_hours_hold_a_change_until_eight():
    """Promised at 20:00 for tomorrow by 12:00. At 23:00 the plan says 13:00: held through the night and sent at 08:00,
    because nothing moved it back. Had it been back at 12:00 by 03:00, nothing would go out."""
    baseline = promise(at(20), at(12, day=TOMORROW), promised=TOMORROW, day_kind="tomorrow")
    moved = promise(at(23), at(13, day=TOMORROW), promised=TOMORROW, day_kind="tomorrow")
    moved_back = promise(at(3, day=TOMORROW), at(12, day=TOMORROW), promised=TOMORROW)

    assert messages_that_would_send([baseline, moved]) == [ChangeMessage(at(8, day=TOMORROW), "later")]
    assert messages_that_would_send([baseline, moved, moved_back]) == []


@pytest.mark.parametrize(
    "moved_at, sent_at",
    [
        pytest.param(at(21, 59), at(21, 59), id="21:59-goes-out"),
        pytest.param(at(22), at(8, day=TOMORROW), id="22:00-waits"),
        pytest.param(at(7, 59, day=TOMORROW), at(8, day=TOMORROW), id="07:59-waits"),
        pytest.param(at(8, day=TOMORROW), at(8, day=TOMORROW), id="08:00-goes-out"),
    ],
)
def test_the_quiet_hours_run_from_ten_to_eight(moved_at, sent_at):
    baseline = promise(at(20), at(12, day=TOMORROW), promised=TOMORROW, day_kind="tomorrow")
    moved = promise(moved_at, at(13, day=TOMORROW), promised=TOMORROW)

    assert messages_that_would_send([baseline, moved]) == [ChangeMessage(sent_at, "later")]


def test_never_more_than_three_messages_per_order():
    """Four moves of +30, an hour apart: the first three go out, the fourth never does."""
    ends = [at(12, 30), at(13), at(13, 30), at(14)]
    rows = [promise(at(9), at(12))] + [promise(at(10 + n), end) for n, end in enumerate(ends)]

    assert messages_that_would_send(rows) == [
        ChangeMessage(at(10), "later"),
        ChangeMessage(at(11), "later"),
        ChangeMessage(at(12), "later"),
    ]


def test_midnight_alone_sends_no_day_message():
    """Promised at 21:00 for tomorrow by 12:00; after midnight the same promise reads "today". Same day, same end."""
    rows = [
        promise(at(21), at(12, day=TOMORROW), promised=TOMORROW, day_kind="tomorrow"),
        promise(at(8, 30, day=TOMORROW), at(12, day=TOMORROW), promised=TOMORROW, day_kind="today"),
    ]

    assert messages_that_would_send(rows) == []


def test_a_thirty_minute_move_across_midnight_sends_no_day_message():
    """Expected at 23:00, by 23:45; then at 23:15, by 00:15 on the next calendar day. E stays on today's date, so the
    promised day is unchanged: a "later" message, not a "day" one."""
    baseline = promise(at(15), at(23, 45))
    moved = promise(at(16), at(0, 15, day=TOMORROW), promised=TODAY)

    assert move_message_reason(baseline, moved) == "later"
    assert messages_that_would_send([baseline, moved]) == [ChangeMessage(at(16), "later")]


def test_a_promised_date_change_sends_a_day_message():
    baseline = promise(at(10), at(18))
    moved = promise(at(11), at(12, day=TOMORROW), promised=TOMORROW, day_kind="tomorrow")

    assert move_message_reason(baseline, moved) == "day"
    assert messages_that_would_send([baseline, moved]) == [ChangeMessage(at(11), "day")]


def test_the_first_windowed_promise_is_the_baseline():
    """An unconfirmed first row told the customer no day; the window that follows it is the first promise, not a
    change to one."""
    rows = [unconfirmed(at(9)), promise(at(10), at(13)), promise(at(11), at(13, 10))]

    assert messages_that_would_send(rows) == []
