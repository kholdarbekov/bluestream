"""Opening a stop detail borrows the card message so a webhook-driven silent
edit skips it until the next full render un-borrows it (the skip/unborrow
mechanics themselves are Task 5's coverage, in test_route_card_render.py --
this file only proves view_active_delivery is the thing that flips the
flag). The current_delivery snapshot contract stays untouched -- pinned by
test_active_delivery_detail_card.py.

Fix round 1 (review): also covers the Redis-outage path. `mark_borrowed`
starts with a Redis `load`, so on an outage it no-ops and the borrow never
lands in Redis -- but Task 6's `session_hint` fallback in
`render_route_card` would then replay the still-"next" hint on the very
next render, reproducing the exact same stranding bug the healthy-Redis
fix closes. `TestBackPathDuringRedisOutage` drives that path end to end
with Redis unconfigured throughout.

Fixture note: the plan's sketch fixture referenced a `route_card_state._memory`
in-memory fallback. That fallback was removed (see route_card_state's module
docstring: it was a split-brain hazard) -- tests/staff_bot/test_route_card_state.py
and test_route_card_render.py both use a `_FakeRedis` double instead, so this
file follows the same, currently-real, pattern.

Auth-bypass note: `view_active_delivery` is decorated with `@require_auth` and
`@require_delivery_driver`, both built with `functools.wraps` (verified against
staff_bot/permissions.py). Rather than unwrap through `__wrapped__.__wrapped__`,
this file mirrors the working pattern already proven in
test_active_delivery_detail_card.py: call the handler method directly with a
plain `context.user_data` dict carrying `authenticated: True` and
`staff_roles: ["delivery_driver"]`, which satisfies both decorators for real."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.handlers.delivery import active_delivery as mod
from staff_bot.handlers.delivery import route_card
from staff_bot.handlers.delivery.active_delivery import ActiveDeliveryHandler
from staff_bot.utils import route_card_state


class _FakeRedis:
    """Just enough of redis.asyncio for set/get/delete with ex= (same shape
    as tests/staff_bot/test_route_card_state.py and test_route_card_render.py)."""

    def __init__(self):
        self.store = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


class _Api:
    def __init__(self, payload):
        self.client = MagicMock()
        self.client.get_active_deliveries = AsyncMock(
            return_value=MagicMock(success=True, data=payload)
        )

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *a):
        return False


_DELIVERY = {
    "delivery_id": 5, "order_number": "AD-1", "status": "assigned",
    "customer_name": "U", "customer_phone": "+998900000001",
    "district": "Chilanzar", "address": "Street 1", "items": [],
    "total_amount": 10000, "payment_method": "cash", "amount_collected": 0,
    "outstanding_amount": 10000, "expected_cash_to_collect": 10000,
    "cod_reserved_prepayment_amount": 0,
    "destination_latitude": 41.31, "destination_longitude": 69.27,
    "route_position": 0, "is_next": True,
    "eta_minutes_from_current_location": None, "distance_km_to_next": None,
}


@pytest.fixture(autouse=True)
def _reset_state():
    route_card_state.configure(_FakeRedis())
    route_card_state._locks.clear()
    yield
    route_card_state.configure(None)
    route_card_state._locks.clear()


def _seed_card(view="next"):
    asyncio.run(route_card_state.save(777, {
        "chat_id": 777, "message_id": 100,
        "card_date": route_card.local_date_str(),
        "view": view, "content_sig": "sig",
    }))


def _detail_update():
    update = MagicMock()
    update.effective_user.id = 777
    update.callback_query = MagicMock()
    update.callback_query.data = "staff_view_active_5"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message = MagicMock()
    update.callback_query.message.chat.id = 777
    update.callback_query.message.message_id = 100
    update.message = None
    return update


def _driver_context():
    ctx = MagicMock()
    ctx.user_data = {"authenticated": True, "staff_roles": ["delivery_driver"]}
    ctx.bot = MagicMock()
    return ctx


@pytest.mark.unit
class TestBorrowOnDetail:
    def _run_view(self, monkeypatch):
        handler = ActiveDeliveryHandler()
        monkeypatch.setattr(
            mod, "api_client",
            _Api({"items": [_DELIVERY], "location_status": "fresh", "route_summary": {}}),
        )
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
        ctx = _driver_context()
        update = _detail_update()
        asyncio.run(handler.view_active_delivery(update, ctx))
        return update, ctx

    def test_detail_marks_card_borrowed_and_keeps_snapshot(self, monkeypatch):
        _seed_card()
        update, ctx = self._run_view(monkeypatch)
        assert asyncio.run(route_card_state.load(777))["view"] == route_card_state.VIEW_BORROWED
        # Detail still edits the tapped message and still caches the snapshot.
        update.callback_query.edit_message_text.assert_awaited_once()
        assert ctx.user_data["current_delivery"]["delivery_id"] == 5
        assert ctx.user_data["current_delivery"]["customer_phone"] == "+998900000001"

    def test_detail_without_card_state_is_harmless(self, monkeypatch):
        update, ctx = self._run_view(monkeypatch)  # no card seeded
        assert asyncio.run(route_card_state.load(777)) is None
        update.callback_query.edit_message_text.assert_awaited_once()


@pytest.mark.unit
class TestBackPathEndToEnd:
    """The specific defect this task closes: `view_active_delivery` used to
    edit the card message into the stop detail WITHOUT marking it borrowed
    and WITHOUT invalidating `content_sig`. So when the driver tapped Back,
    the freshly-built card render (same payload, same default view) hashed
    identically to the stored signature, the no-op skip in
    `render_route_card` fired, nothing was sent -- and the driver was
    stranded looking at the stop detail forever.

    This test drives the REAL production call sequence end to end: create
    the card via `render_route_card` (as `show_active_deliveries` does),
    open a stop via the real `view_active_delivery` handler (as the
    `staff_view_active_{id}` callback does), then tap Back by calling
    `render_route_card` again with the identical payload (as
    `show_active_deliveries` -> `_render_card_from_update` does). Before the
    fix, the final assertion fails: no edit and no send happen, and the
    stored view is still "borrowed" instead of "next"."""

    def test_open_stop_then_back_restores_the_card(self, monkeypatch):
        # 1. Card is created for the first time -- this is the pinned
        # message the driver will later tap Back on.
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock(chat_id=777, message_id=100))
        bot.edit_message_text = AsyncMock()
        bot.delete_message = AsyncMock()
        bot.pin_chat_message = AsyncMock()

        payload = {"items": [dict(_DELIVERY)], "total": 1,
                   "location_status": "fresh", "route_summary": {}}
        asyncio.run(route_card.render_route_card(
            bot, telegram_id=777, chat_id=777, language="en", payload=payload,
        ))
        bot.send_message.assert_awaited_once()  # sanity: card created at message_id 100

        # 2. Driver taps the stop -> the real view_active_delivery handler
        # edits that SAME message into the detail view.
        handler = ActiveDeliveryHandler()
        monkeypatch.setattr(
            mod, "api_client",
            _Api({"items": [_DELIVERY], "location_status": "fresh", "route_summary": {}}),
        )
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
        update = _detail_update()
        ctx = _driver_context()
        asyncio.run(handler.view_active_delivery(update, ctx))
        update.callback_query.edit_message_text.assert_awaited_once()

        # 3. Driver taps <- Back -> exactly the call show_active_deliveries
        # issues: render_route_card with the SAME payload/view. This is the
        # precise moment the reported bug strands the driver.
        bot.edit_message_text.reset_mock()
        bot.send_message.reset_mock()
        asyncio.run(route_card.render_route_card(
            bot, telegram_id=777, chat_id=777, language="en", payload=payload,
        ))

        # The card MUST come back: either an in-place edit, or (if Telegram
        # ever reports the message gone) a fresh send. Before the fix,
        # NEITHER happens -- the no-op skip fires because the stored view
        # ("next") and content_sig still match the freshly-built render.
        assert bot.edit_message_text.await_count == 1 or bot.send_message.await_count == 1
        state = asyncio.run(route_card_state.load(777))
        assert state["view"] == route_card_state.VIEW_NEXT


@pytest.mark.unit
class TestBackPathDuringRedisOutage:
    """Fix round 1 (review): the exact same stranding bug survives a Redis
    outage. `route_card_state.mark_borrowed` opens with a Redis `load`,
    which returns None when Redis is down, so it no-ops -- the borrow never
    reaches Redis. But Task 6 built `session_hint` (a caller-owned
    `context.user_data` dict) precisely so a card degrades in a BOUNDED way
    during an outage: `render_route_card` falls back to it whenever real
    state is None. Without also mirroring the borrow into that hint, the
    hint keeps reporting view="next" the whole time the driver is in the
    detail view, so tapping Back replays a still-"next", still-identical
    render -> the no-op skip fires again -> the driver is stranded, exactly
    as before the Task 7 fix, just triggered by an outage instead of a
    stale content_sig.

    Freezes `format_local_time` (same technique as
    test_route_card_render.py::test_identical_content_same_view_skips_edit)
    so the "same content" premise this test is built on can never be
    accidentally falsified by two renders straddling a clock-minute
    boundary -- that would make the RED proof (pre-fix) flaky rather than
    reliably reproducing the bug."""

    def test_open_stop_then_back_restores_the_card_during_outage(self, monkeypatch):
        monkeypatch.setattr(route_card, "format_local_time", lambda dt=None, with_seconds=False: "11:42")
        import staff_bot.utils.formatters as fmt
        monkeypatch.setattr(fmt, "format_local_time", lambda dt=None, with_seconds=False: "11:42")

        route_card_state.configure(None)  # Redis outage for the whole test

        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock(chat_id=777, message_id=100))
        bot.edit_message_text = AsyncMock()
        bot.delete_message = AsyncMock()
        bot.pin_chat_message = AsyncMock()

        payload = {"items": [dict(_DELIVERY)], "total": 1,
                   "location_status": "fresh", "route_summary": {}}

        # 1. First-ever render during the outage: creates unpinned (Task 6)
        # and populates the session hint -- mirrors what
        # _render_card_from_update does with context.user_data['route_card_session'].
        hint = {}
        asyncio.run(route_card.render_route_card(
            bot, telegram_id=777, chat_id=777, language="en", payload=payload,
            session_hint=hint,
        ))
        bot.send_message.assert_awaited_once()
        bot.pin_chat_message.assert_not_called()  # outage mode: never pins
        assert hint["view"] == route_card_state.VIEW_NEXT
        assert hint["message_id"] == 100

        # 2. Driver taps the stop -> the real view_active_delivery handler,
        # with the SAME context.user_data (and therefore the SAME hint
        # dict) a real PTB session carries across updates for this user.
        handler = ActiveDeliveryHandler()
        monkeypatch.setattr(
            mod, "api_client",
            _Api({"items": [_DELIVERY], "location_status": "fresh", "route_summary": {}}),
        )
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
        update = _detail_update()
        ctx = _driver_context()
        ctx.user_data["route_card_session"] = hint
        asyncio.run(handler.view_active_delivery(update, ctx))
        update.callback_query.edit_message_text.assert_awaited_once()

        # Redis never received the borrow -- it's down -- confirming the
        # hint is the only place the borrow can live during this outage.
        assert asyncio.run(route_card_state.load(777)) is None

        # 3. Driver taps <- Back -> the exact render_route_card call
        # _render_card_from_update issues, same hint, Redis still down.
        bot.edit_message_text.reset_mock()
        bot.send_message.reset_mock()
        asyncio.run(route_card.render_route_card(
            bot, telegram_id=777, chat_id=777, language="en", payload=payload,
            session_hint=hint,
        ))

        # The card MUST come back. Before this fix round, the hint's view
        # was still "next" (mark_borrowed no-op'd on the Redis outage), so
        # the no-op skip fired and NEITHER an edit nor a send happened --
        # the driver stayed stranded in the detail view.
        assert bot.edit_message_text.await_count == 1 or bot.send_message.await_count == 1
        assert hint["view"] == route_card_state.VIEW_NEXT
