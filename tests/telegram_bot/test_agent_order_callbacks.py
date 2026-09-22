"""The store's answer to an agent-placed order, driven through the real bot.

The proposal is pushed by `telegram_bot/webhook_server.py` and answered by two
inline buttons. That is two files that have to agree — the `callback_data` the
webhook renders and the patterns `bot.py` registers — plus one HTTP body the
backend has to receive exactly right, because `{"action": "confirm"}` and
`{"action": "decline"}` are the difference between an order that ships and an
order that is cancelled.

Every test goes in through `Application.process_update` on the real application
built by the real `_setup_handlers()`, for the reason
`test_support_and_text_routing.py` spells out: a handler called directly proves
the handler works, never that it is CALLED.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

# Module-level, before anything below touches them, so `i18n`, `keyboards` and
# `config` resolve as the BOT's versions. See tests/telegram_bot/conftest.py.
import bot as bot_module  # noqa: F401

# This import has a session-wide side effect, not a file-local one:
# `scripts/seed_backend_translations.py` inserts `/app` at `sys.path[0]` at
# import time, and the two bot suites co-run in one pytest session. It is safe
# here only because `tests/telegram_bot/conftest.py`'s `pytest_collectstart`
# re-asserts `telegram_bot` on `sys.path` (and the staff conftest APPENDs
# rather than `insert(0)`) — see `project_bot_test_suite_syspath_collision`. If
# a later change removes that re-assertion, this import is what breaks the
# other suite.
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

from tests.telegram_bot.ptb_harness import backend_failure, build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


ORDER_ID = 9001
CONFIRMATION_ENDPOINT = f"/api/v1/orders/{ORDER_ID}/agent-confirmation"

# The journey runs in Uzbek: `i18n.get_user_language` reads FakeDatabase's
# customer row, whose `preferred_language` is 'uz'
# (tests/telegram_bot/ptb_harness.py).
LANGUAGE = "uz"


def _seeded(key: str) -> str:
    """The copy production really ships for `key`, straight from the seeder.

    The staff-bot journeys resolve through `scripts/seed_staff_translations.py
    ::_curated_value` for this exact reason (tests/staff_bot/test_sales_hub_journey.py
    :154) — "so a future edit to the seed cannot leave this file asserting
    strings production no longer ships". A frozen literal is a second copy of
    the copy, and the one that drifts silently.
    """
    row = BACKEND_TRANSLATIONS.get(key)
    assert row, f"{key} is rendered by the agent-order handler but never seeded"
    value = row.get(LANGUAGE)
    assert value, f"{key} has no {LANGUAGE} copy"
    return value


# Real seeded copy, resolved from scripts/seed_backend_translations.py rather
# than frozen here: "the customer was told something" is not the same claim as
# "the customer was told the right thing", and a literal copied out of the
# seeder stops being either the moment the seeder is edited.
TRANSLATIONS = {
    key: _seeded(key)
    for key in (
        "telegram.agent_order.confirmed",
        "telegram.agent_order.declined",
        "telegram.agent_order.not_pending",
        "telegram.error.auth_failed",
    )
}


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    harness.backend.route(
        "POST",
        CONFIRMATION_ENDPOINT,
        lambda _c: {
            "data": {
                "order": {"id": ORDER_ID, "order_number": "SA_000123_26", "status": "confirmed"},
                "confirmation": {"status": "confirmed"},
            }
        },
    )
    return harness


@pytest.fixture
def user(bot, request):
    """An update factory for a customer id unique to THIS test, so nothing this
    file does can leak across tests through a per-user cache or lock."""
    digest = hashlib.sha256(request.node.nodeid.encode("utf-8")).hexdigest()[:8]
    telegram_id = 780_000_000 + int(digest, 16) % 1_000_000
    return bot.updates(user_id=telegram_id, chat_id=telegram_id)


def confirmation_calls(bot):
    """Every call that reached the agent-confirmation route, in order."""
    return [
        call
        for call in bot.backend.calls
        if call.method == "POST" and call.endpoint == CONFIRMATION_ENDPOINT
    ]


def toasts(bot):
    return [call.params.get("text") for call in bot.telegram.of("answerCallbackQuery")]


async def test_confirm_posts_exactly_the_confirm_action_with_the_bearer_token(bot, user):
    await bot.send(user.tap(f"agent_order_confirm_{ORDER_ID}"))

    (call,) = confirmation_calls(bot)
    assert call.data == {"action": "confirm"}, (
        "the body decides whether the order ships; 'reason' must not ride along "
        "unasked and 'action' must be the literal the backend validates"
    )
    assert call.user_token == "test-access-token", (
        "the confirmation is an authenticated customer action — an unauthed "
        "POST would 401 and the store would be told nothing"
    )


async def test_confirm_tells_the_customer_it_worked(bot, user):
    await bot.send(user.tap(f"agent_order_confirm_{ORDER_ID}"))

    assert toasts(bot) == [_seeded("telegram.agent_order.confirmed")]


async def test_decline_posts_exactly_the_decline_action(bot, user):
    await bot.send(user.tap(f"agent_order_decline_{ORDER_ID}"))

    (call,) = confirmation_calls(bot)
    assert call.data == {"action": "decline"}
    assert call.user_token == "test-access-token"


async def test_decline_tells_the_customer_it_worked(bot, user):
    await bot.send(user.tap(f"agent_order_decline_{ORDER_ID}"))

    assert toasts(bot) == [_seeded("telegram.agent_order.declined")]


async def test_a_second_tap_on_an_already_answered_proposal_says_so(bot, user):
    """A second tap is ordinary customer behaviour — the first answer may have
    been given elsewhere, or on a message whose edit Telegram refused, so the
    buttons can still be on screen. The backend answers 409
    SALES_CONFIRMATION_NOT_PENDING; the customer must read that in their own
    language, not the backend's English `message`.
    """
    bot.backend.route(
        "POST",
        CONFIRMATION_ENDPOINT,
        lambda _c: backend_failure(
            "Confirmation request is not pending",
            status_code=409,
            # The REAL body: `@handle_api_exception` puts `error_code` at the top level
            # (business_app/utils/error_handlers.py:76-77, :321), and `_make_request`
            # hands the whole body back as `APIResponse.data`. Task 5b's
            # `test_answering_twice_is_a_409_with_its_code` asserts the same shape from
            # the other side, so the two halves are pinned against one payload.
            data={"error_code": "SALES_CONFIRMATION_NOT_PENDING"},
        ),
    )

    await bot.send(user.tap(f"agent_order_confirm_{ORDER_ID}"))

    assert toasts(bot) == [_seeded("telegram.agent_order.not_pending")]


async def test_an_unmapped_backend_failure_surfaces_its_message(bot, user):
    """Anything without a mapped error code falls through to the shared
    `_handle_api_error` shim, so the customer still sees why it failed."""
    bot.backend.route(
        "POST",
        CONFIRMATION_ENDPOINT,
        lambda _c: backend_failure("Order not found", status_code=404),
    )

    await bot.send(user.tap(f"agent_order_confirm_{ORDER_ID}"))

    assert toasts(bot) == ["❌ Order not found"]


async def test_a_refused_ack_on_the_error_path_does_not_add_a_second_toast(bot, user):
    """Telegram discards callback queries after ~60 s and then refuses
    `answerCallbackQuery` — routinely, because every redeploy ends with Telegram
    redelivering the taps that piled up while the bot was down. `BaseHandler._ack`
    exists so that refusal never escapes into a handler's `except Exception`
    (handlers/base.py:29-73), and this method uses it on the auth, success and
    not-pending paths. The backend-error path did not: a refused ack there was
    re-raised, logged as a handler failure, and answered AGAIN — a second doomed
    call carrying the generic "something went wrong" copy, which describes
    neither what happened nor what the store should do.
    """
    bot.telegram.fail(
        "answerCallbackQuery",
        "Bad Request: query is too old and response timeout expired or query id is invalid",
    )
    bot.backend.route(
        "POST",
        CONFIRMATION_ENDPOINT,
        lambda _c: backend_failure("Order not found", status_code=404),
    )

    await bot.send(user.tap(f"agent_order_confirm_{ORDER_ID}"))

    assert toasts(bot) == ["❌ Order not found"]


async def test_an_expired_login_leaves_the_proposal_answerable(bot, user, monkeypatch):
    """A toast, not a screen — including on the failure paths.

    The shared `_handle_auth_error` shim EDITS the message it was tapped from,
    which here is the proposal itself: the text is replaced and the inline
    keyboard goes with it, so the one customer who could still answer after
    re-authenticating no longer has the buttons to do it with, and the order
    rides the confirmation TTL into an auto-confirm. Tell them, leave the
    proposal standing.
    """
    async def _no_cached_token(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        bot.application.bot_data["token_manager"], "get_valid_token", _no_cached_token
    )
    bot.backend.route(
        "POST",
        "/api/v1/auth/telegram-login",
        lambda _c: backend_failure("Unauthorized", status_code=401),
    )

    await bot.send(user.tap(f"agent_order_confirm_{ORDER_ID}"))

    assert toasts(bot) == [_seeded("telegram.error.auth_failed")]
    assert bot.telegram.of("editMessageText", "editMessageReplyMarkup") == [], (
        "the proposal message and its buttons must survive an auth failure — "
        "editing them away is what makes the order unanswerable"
    )
    assert confirmation_calls(bot) == []


async def test_the_buttons_the_webhook_renders_are_claimed_by_a_handler(bot, user):
    """`telegram_bot/webhook_server.py` renders the callback_data and `bot.py`
    registers the patterns. Those are two files that have to agree, and nothing
    else checks that they do — a rename on either side leaves the store looking
    at a proposal whose buttons spin forever, while the order quietly rides the
    confirmation TTL into an auto-confirm.
    """
    webhook_source = (
        Path(__file__).resolve().parents[2] / "telegram_bot" / "webhook_server.py"
    ).read_text(encoding="utf-8")

    assert "'callback_data': f'agent_order_confirm_{order_id}'" in webhook_source, (
        "the proposal no longer renders agent_order_confirm_<order_id>; "
        "update the pattern assertions below with it"
    )
    assert "'callback_data': f'agent_order_decline_{order_id}'" in webhook_source, (
        "the proposal no longer renders agent_order_decline_<order_id>; "
        "update the pattern assertions below with it"
    )

    assert bot.handlers_matching(user.tap(f"agent_order_confirm_{ORDER_ID}"))
    assert bot.handlers_matching(user.tap(f"agent_order_decline_{ORDER_ID}"))
    # An id-less tap must match nothing — it would crash the int() parse inside
    # the handler on every malformed callback.
    assert not bot.handlers_matching(user.tap("agent_order_confirm_"))
    assert not bot.handlers_matching(user.tap("agent_order_decline_"))


async def test_confirm_and_decline_are_not_the_same_handler(bot, user):
    """A copy-paste that wires both buttons to `confirm` renders perfectly and
    turns every Decline into a confirmed order."""
    confirm_handlers = {h for _, h in bot.handlers_matching(user.tap(f"agent_order_confirm_{ORDER_ID}"))}
    decline_handlers = {h for _, h in bot.handlers_matching(user.tap(f"agent_order_decline_{ORDER_ID}"))}

    assert confirm_handlers and decline_handlers
    assert confirm_handlers.isdisjoint(decline_handlers)


@pytest.mark.parametrize(
    "action, key",
    [
        ("confirm", "telegram.agent_order.confirmed"),
        ("decline", "telegram.agent_order.declined"),
    ],
)
async def test_an_answered_proposal_loses_its_buttons_and_shows_the_answer(
    bot, user, action, key
):
    """The toast is gone in seconds and Telegram never redraws a message on its
    own, so an answered proposal otherwise keeps reading "Please confirm or
    decline." under two live buttons — for an order the store already answered.
    The only way to find out was to tap again and read a refusal.

    The proposal's own text stays (it is the record of what was agreed); the
    keyboard goes — Telegram removes the inline keyboard of a message edited
    without one — and the answer is appended, so confirm and decline are
    distinguishable on the screen and not only in a toast nobody can re-read.
    """
    await bot.send(user.tap(f"agent_order_{action}_{ORDER_ID}"))

    (edit,) = bot.telegram.of("editMessageText")
    assert edit.params["text"] == f"previous bot message\n\n{_seeded(key)}"
    assert "reply_markup" not in edit.params, (
        "editing without a reply_markup is what retires the buttons; sending one "
        "back would leave the proposal answerable twice"
    )


async def test_a_second_tap_retires_the_proposal_the_backend_says_is_answered(bot, user):
    """The 409 branch settles the message too, and only an assertion says so.

    `_settle` is called from two places and the success branch is the only one
    with an `editMessageText` assertion, so dropping the call here — or
    reordering the `_ack`/`_settle` pair so the edit is skipped — stayed green
    while the store's proposal kept two live buttons over an order it had
    already answered: the very defect the settle was added for, on the branch a
    shopkeeper reaches MOST often.
    """
    bot.backend.route(
        "POST",
        CONFIRMATION_ENDPOINT,
        lambda _c: backend_failure(
            "Confirmation request is not pending",
            status_code=409,
            data={"error_code": "SALES_CONFIRMATION_NOT_PENDING"},
        ),
    )

    await bot.send(user.tap(f"agent_order_confirm_{ORDER_ID}"))

    (edit,) = bot.telegram.of("editMessageText")
    assert edit.params["text"].endswith(_seeded("telegram.agent_order.not_pending"))
    assert "reply_markup" not in edit.params, (
        "editing without a reply_markup is what retires the buttons; a proposal "
        "the backend calls answered must not stay answerable"
    )


async def test_a_refused_edit_never_costs_the_store_its_answer(bot, user):
    """`_settle`'s tolerance, exercised rather than asserted in a docstring.

    Telegram refuses `editMessageText` routinely — past the 48h window, on a
    message the customer deleted, "message is not modified" for an answer that
    raced another — and by the time it runs the POST is already committed on the
    backend. If the refusal escaped, it would land in `_respond`'s blanket
    `except` and the customer would be told their confirmation failed over an
    order that really is confirmed, plus a second doomed answerCallbackQuery.
    """
    bot.telegram.fail("editMessageText", "Bad Request: message can't be edited")

    await bot.send(user.tap(f"agent_order_confirm_{ORDER_ID}"))

    assert toasts(bot) == [_seeded("telegram.agent_order.confirmed")]
    assert len(bot.telegram.of("answerCallbackQuery")) == 1, (
        "the error path would answer the query a second time with generic copy"
    )


async def test_an_unmapped_backend_failure_leaves_the_proposal_answerable(bot, user):
    """The counterpart: nothing was answered, so nothing may be retired. A 5xx
    or a 404 means the store still has to answer — strip the buttons here and
    the order rides the confirmation TTL into an auto-confirm instead.
    """
    bot.backend.route(
        "POST",
        CONFIRMATION_ENDPOINT,
        lambda _c: backend_failure("Order not found", status_code=404),
    )

    await bot.send(user.tap(f"agent_order_confirm_{ORDER_ID}"))

    assert bot.telegram.of("editMessageText", "editMessageReplyMarkup") == []
