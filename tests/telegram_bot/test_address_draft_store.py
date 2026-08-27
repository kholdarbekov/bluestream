"""Task 4: `BotUserRepository.arm_awaiting_input` / `disarm` preserve the draft.

`update_user_state` (`telegram_bot/database.py`) is a whole-document replace —
`UPDATE users SET bot_state = $1`. Every flow arms by writing a fresh document
(e.g. `handlers/support.py`'s "Report an issue" prompt), discarding whatever
was there. Once an unfinished address draft lives in `bot_state`, that means
tapping any unrelated menu item destroys it AT ARM TIME, before any disarm or
parking logic ever runs. Preservation has to be a property of the write
itself — these two tests prove `arm_awaiting_input` and `disarm` are that
write, backed by the shared `_PRESERVED_KEYS` constant.

Fixtures: `repo`, `database`, and `user_id` are LOCAL to this file (ruling: no
shared conftest fixture, no new DB abstraction). They reuse
`tests/telegram_bot/ptb_harness.py`'s existing `FakeDatabase` — the same fake
already wired into `BotUserRepository.get_user_state` /
`BotUserRepository.update_user_state` for every other bot test in this
directory — rather than standing up a second fake or touching real Postgres.
The `database` fixture is also exposed directly so a test can assert on
`FakeDatabase.executed` — e.g. that a not-mine `disarm` issues no write at all.
"""

import pytest

from database import BotUserRepository
from ptb_harness import DEFAULT_USER_ID, FakeDatabase

pytestmark = pytest.mark.anyio


@pytest.fixture
def database() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def repo(database: FakeDatabase) -> BotUserRepository:
    return BotUserRepository(database)


@pytest.fixture
def user_id() -> int:
    return DEFAULT_USER_ID


async def test_arming_another_flow_preserves_an_open_address_draft(repo, user_id):
    """R3's headline case. `update_user_state` replaces the whole document, so
    arming "Report an issue" over an open draft destroyed it at ARM time —
    before any parking or disarm logic could run."""
    await repo.update_user_state(user_id, {'address_draft': {'step': 'apartment'}})

    await repo.arm_awaiting_input(user_id, 'support_message', support_order_id=1042)

    state = await repo.get_user_state(user_id)
    assert state['awaiting_input'] == 'support_message'
    assert state['support_order_id'] == 1042
    assert state['address_draft'] == {'step': 'apartment'}, (
        "arming another flow must not destroy the customer's unfinished address"
    )


async def test_arming_a_new_flow_drops_the_previous_flows_companions(repo, user_id):
    """Pins the OTHER half of `arm_awaiting_input`'s contract: only
    `_PRESERVED_KEYS` survive an arm, nothing else does.

    A merge-shaped implementation — `{**state, 'awaiting_input': ..., **companions}`
    instead of `{**preserved, 'awaiting_input': ..., **companions}` — would
    still pass the headline preservation test above, because that test's
    pre-state holds only `address_draft`. It is a real bug: arming general
    support after an order-scoped "Report an issue" would carry the stale
    `support_order_id` forward, and `handlers/support.py` would prefix the
    customer's new message with the OLD order number.
    """
    await repo.update_user_state(user_id, {
        'awaiting_input': 'support_message',
        'support_order_id': 1042,
        'address_draft': {'step': 'apartment'},
    })

    await repo.arm_awaiting_input(user_id, 'edit_profile_name')

    state = await repo.get_user_state(user_id)
    assert state['awaiting_input'] == 'edit_profile_name'
    assert 'support_order_id' not in state, (
        "the PREVIOUS flow's companions must not carry forward into the new one"
    )
    assert state['address_draft'] == {'step': 'apartment'}


async def test_disarming_preserves_the_draft_but_drops_the_flow_keys(repo, user_id):
    await repo.update_user_state(user_id, {
        'awaiting_input': 'support_message',
        'support_order_id': 1042,
        'address_draft': {'step': 'apartment'},
    })

    assert await repo.disarm(user_id, 'support_message') is True

    state = await repo.get_user_state(user_id)
    assert 'awaiting_input' not in state
    assert 'support_order_id' not in state, "the disarmed flow's companions go with it"
    assert state['address_draft'] == {'step': 'apartment'}


async def test_disarming_a_flow_you_do_not_own_changes_nothing(repo, database, user_id):
    await repo.update_user_state(user_id, {
        'awaiting_input': 'edit_profile_name',
        'address_draft': {'step': 'apartment'},
    })
    writes_before = len(database.executed)

    assert await repo.disarm(user_id, 'support_message') is False

    state = await repo.get_user_state(user_id)
    assert state['awaiting_input'] == 'edit_profile_name'
    assert state['address_draft'] == {'step': 'apartment'}, (
        "the draft must survive a disarm the customer doesn't own too"
    )
    assert len(database.executed) == writes_before, (
        "a disarm the customer doesn't own must issue NO write at all — "
        "not even one that would bump last_bot_interaction on a row the "
        "screen has no business touching"
    )


# ---------------------------------------------------------------------------
# Task 6 (P2): the draft accessors themselves — `get_address_draft`,
# `save_address_draft`, `clear_address_draft`. The tests above cover
# `arm_awaiting_input` / `disarm` preserving a draft that already exists;
# these cover the accessors that create, read, and remove it.
# ---------------------------------------------------------------------------


async def test_saving_a_draft_leaves_awaiting_input_alone(repo, user_id):
    """P2 dual-writes the draft but must NOT arm the flow: armed text would
    reach `bot.py::_handle_contextual_input`'s unknown-state branch, which
    disarms and replies 'invalid input' to a customer mid-conversation.
    Arming `address_flow` is P3a's job, not this one's."""
    await repo.save_address_draft(user_id, step='apartment', data={'title': 'Home'}, address_id=123)

    state = await repo.get_user_state(user_id)
    assert 'awaiting_input' not in state, "P2 does not arm; P3a does"
    assert state['address_draft'] == {
        'step': 'apartment', 'data': {'title': 'Home'},
        'address_id': 123, 'origin': None, 'parked': False,
    }


async def test_saving_a_draft_preserves_an_armed_flows_companions(repo, user_id):
    """The mirror image of `test_arming_another_flow_preserves_an_open_address_draft`
    above: `save_address_draft` is a read-modify-write too, so starting the
    address flow while a concern report is armed must not erase IT."""
    await repo.update_user_state(user_id, {
        'awaiting_input': 'support_message',
        'support_order_id': 1042,
    })

    await repo.save_address_draft(user_id, step='title', data={}, address_id=None)

    state = await repo.get_user_state(user_id)
    assert state['awaiting_input'] == 'support_message'
    assert state['support_order_id'] == 1042, (
        "an unrelated flow's armed companions must survive a draft save"
    )
    assert state['address_draft'] == {
        'step': 'title', 'data': {}, 'address_id': None, 'origin': None, 'parked': False,
    }


async def test_get_address_draft_returns_none_when_absent(repo, user_id):
    assert await repo.get_address_draft(user_id) is None


async def test_get_address_draft_returns_the_saved_shape(repo, user_id):
    await repo.save_address_draft(
        user_id, step='street', data={'district': 'yunusabad'},
        address_id=900, origin='checkout', parked=True,
    )

    draft = await repo.get_address_draft(user_id)

    assert draft == {
        'step': 'street', 'data': {'district': 'yunusabad'},
        'address_id': 900, 'origin': 'checkout', 'parked': True,
    }


async def test_clear_address_draft_removes_only_that_key(repo, user_id):
    await repo.update_user_state(user_id, {
        'awaiting_input': 'support_message',
        'support_order_id': 1042,
        'address_draft': {'step': 'apartment'},
    })

    await repo.clear_address_draft(user_id)

    state = await repo.get_user_state(user_id)
    assert 'address_draft' not in state
    assert state['awaiting_input'] == 'support_message'
    assert state['support_order_id'] == 1042, (
        "clearing the draft must not disturb an unrelated armed flow"
    )


async def test_clearing_an_absent_draft_issues_no_write(repo, database, user_id):
    """M3 (coordinator review, Task 6): must agree with `disarm`'s own promise
    two methods above — "issues NO write at all — not even one that would
    bump `last_bot_interaction`." `address_flow_timeout` calls this on a
    SYNTHETIC update PTB generates; an unconditional write would stamp
    `last_bot_interaction` on every timeout, including one where the customer
    sent nothing after arming the flow, feeding `session_cleanup_service`'s
    180-day sweep and the admin UI with an activity timestamp nobody
    generated."""
    await repo.update_user_state(user_id, {'awaiting_input': 'support_message'})
    writes_before = len(database.executed)

    await repo.clear_address_draft(user_id)

    state = await repo.get_user_state(user_id)
    assert state == {'awaiting_input': 'support_message'}
    assert len(database.executed) == writes_before, (
        "clearing an absent draft must issue no write at all — not even one "
        "that would bump last_bot_interaction on a row with nothing to clear"
    )


# ---------------------------------------------------------------------------
# M1 (final whole-branch review): the no-write guard above only helps when NO
# draft exists. Task 6's dual-write means one exists from the moment
# `add_address` runs, so by the time ANY teardown fires there is almost
# always a real draft to remove and the guard above never engages. The write
# that removes it must still be able to say "not customer activity" when the
# caller is `address_flow_timeout` (a SYNTHETIC PTB update).
# ---------------------------------------------------------------------------


async def test_clearing_a_real_draft_with_touch_activity_false_skips_the_timestamp(
    repo, database, user_id
):
    """`address_flow_timeout` calls this. Left unconditional, every timeout —
    even one where the customer sent nothing after arming the flow 24h
    earlier (`conversation_timeout`) — would stamp `last_bot_interaction =
    now()`, feeding `session_cleanup_service`'s 180-day sweep and the admin
    UI's "Last Bot Interaction" column with an activity timestamp nobody
    generated."""
    await repo.update_user_state(user_id, {'address_draft': {'step': 'apartment'}})
    writes_before = len(database.executed)

    await repo.clear_address_draft(user_id, touch_activity=False)

    assert len(database.executed) == writes_before + 1, "the draft must still be removed"
    assert 'last_bot_interaction' not in database.executed[-1]
    state = await repo.get_user_state(user_id)
    assert 'address_draft' not in state


async def test_clearing_a_real_draft_by_default_still_stamps_the_timestamp(
    repo, database, user_id
):
    """The default is unchanged: cancel, cancel-from-text and a completed
    save are all real customer actions, and only the synthetic timeout path
    (tested above) opts out of stamping activity."""
    await repo.update_user_state(user_id, {'address_draft': {'step': 'apartment'}})
    writes_before = len(database.executed)

    await repo.clear_address_draft(user_id)

    assert len(database.executed) == writes_before + 1
    assert 'last_bot_interaction' in database.executed[-1]
