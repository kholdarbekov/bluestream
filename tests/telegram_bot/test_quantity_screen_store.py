"""`BotUserRepository`'s quantity-screen window: which screen a typed number answers.

The window is a ROUTING HINT that lives beside whatever prompt the customer has
armed, not in the single ``awaiting_input`` slot. So:

* recording it must not disarm "Report an issue" (or any other prompt);
* arming or disarming another prompt must not wipe it;
* closing it touches nothing else.

Same local fixtures as ``test_address_draft_store.py``: the harness's
``FakeDatabase`` behind the real repository, so the real read-modify-write SQL
path runs.
"""

import json

import pytest

from database import BotUserRepository
from ptb_harness import DEFAULT_USER_ID, FakeDatabase

pytestmark = pytest.mark.anyio

SCREEN = {'product_id': 5, 'message_id': 321, 'shown_at': '2026-09-23T09:00:00+00:00'}


@pytest.fixture
def database() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def repo(database: FakeDatabase) -> BotUserRepository:
    return BotUserRepository(database)


@pytest.fixture
def user_id() -> int:
    return DEFAULT_USER_ID


async def remember(repo, user_id):
    await repo.remember_quantity_screen(
        user_id, product_id=5, message_id=321, shown_at=SCREEN['shown_at'],
    )


async def test_opening_the_screen_leaves_an_armed_issue_report_armed(repo, user_id):
    await repo.arm_awaiting_input(user_id, 'support_message', support_order_id=1042)

    await remember(repo, user_id)

    state = await repo.get_user_state(user_id)
    assert state['awaiting_input'] == 'support_message'
    assert state['support_order_id'] == 1042
    assert state['quantity_screen'] == SCREEN


async def test_arming_and_disarming_another_prompt_keep_the_window(repo, user_id):
    await remember(repo, user_id)

    await repo.arm_awaiting_input(user_id, 'edit_profile_name')
    assert (await repo.get_user_state(user_id))['quantity_screen'] == SCREEN

    await repo.disarm(user_id, 'edit_profile_name')
    assert await repo.get_user_state(user_id) == {'quantity_screen': SCREEN}


async def test_closing_the_window_touches_nothing_else(repo, user_id):
    await repo.arm_awaiting_input(user_id, 'support_message', support_order_id=1042)
    await remember(repo, user_id)

    closed = await repo.forget_quantity_screen(user_id)

    assert closed is True
    assert await repo.get_user_state(user_id) == {
        'awaiting_input': 'support_message', 'support_order_id': 1042,
    }


async def test_closing_a_window_that_is_not_open_writes_nothing(repo, user_id, database):
    writes_before = len(database.executed)

    assert await repo.forget_quantity_screen(user_id) is False
    assert len(database.executed) == writes_before
    assert json.loads(database.user['bot_state']) == {}
