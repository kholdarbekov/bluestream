"""Spec §4.2: one tap optimizes. The 412 fallback asks for a location exactly
once and arms the flag that makes the following pin finish the job."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.handlers.delivery import active_delivery as active_mod
from staff_bot.handlers.delivery.active_delivery import ActiveDeliveryHandler


class _ApiClient:
    """Async-context-manager stub matching `async with api_client as client`."""

    def __init__(self, optimize_response):
        self.client = MagicMock()
        self.client.optimize_route = AsyncMock(return_value=optimize_response)

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *args):
        return False


def _context():
    ctx = MagicMock()
    ctx.user_data = {
        "authenticated": True,
        "staff_roles": ["delivery_driver"],
        "language": "en",
    }
    return ctx


def _callback_update():
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.effective_user = MagicMock(id=555)
    return update


def _handler(monkeypatch, optimize_response):
    handler = ActiveDeliveryHandler()
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(handler, "show_active_deliveries", AsyncMock())
    monkeypatch.setattr(active_mod, "api_client", _ApiClient(optimize_response))
    return handler


@pytest.mark.unit
def test_fresh_position_optimizes_in_one_tap(monkeypatch):
    """No location keyboard appears at all when the stored fix is usable."""
    response = MagicMock(success=True, status_code=200, data={"route_locked": False})
    handler = _handler(monkeypatch, response)
    update, ctx = _callback_update(), _context()

    asyncio.run(handler.optimize_routes(update, ctx))

    handler.show_active_deliveries.assert_awaited()
    update.callback_query.message.reply_text.assert_not_awaited()
    assert "pending_optimize_after_location" not in ctx.user_data


@pytest.mark.unit
def test_stale_position_asks_once_and_arms_the_flag(monkeypatch):
    response = MagicMock(
        success=False, status_code=412, error_code="LOCATION_REQUIRED", error="LOCATION_REQUIRED"
    )
    handler = _handler(monkeypatch, response)
    update, ctx = _callback_update(), _context()

    asyncio.run(handler.optimize_routes(update, ctx))

    update.callback_query.message.reply_text.assert_awaited_once()
    assert ctx.user_data["pending_optimize_after_location"] is True


@pytest.mark.unit
def test_route_locked_alerts_and_still_rerenders(monkeypatch):
    response = MagicMock(success=True, status_code=200, data={"route_locked": True})
    handler = _handler(monkeypatch, response)
    update, ctx = _callback_update(), _context()

    asyncio.run(handler.optimize_routes(update, ctx))

    alert_calls = [c for c in update.callback_query.answer.await_args_list
                   if c.kwargs.get("show_alert")]
    assert alert_calls, "dispatch-locked routes must say so, not look like a dead button"
    handler.show_active_deliveries.assert_awaited()


@pytest.mark.unit
def test_run_optimize_and_render_is_reusable_without_a_callback(monkeypatch):
    """Task 6's location handler calls this after a pin, where there is no
    callback_query to answer — it must not assume one."""
    response = MagicMock(success=True, status_code=200, data={"route_locked": False})
    handler = _handler(monkeypatch, response)
    update, ctx = _callback_update(), _context()
    update.callback_query = None
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

    ran = asyncio.run(handler.run_optimize_and_render(update, ctx, "en", "tok"))

    assert ran is True
    handler.show_active_deliveries.assert_awaited()


@pytest.mark.unit
def test_stale_position_with_no_callback_prompts_message_and_arms_flag(monkeypatch):
    """Fix round 1 (Minor): the 412 branch is exercised above only through the
    callback path. Task 6's location handler can hit a fresh 412 too, and that
    branch has its own `query is None` guard (`target = query.message if
    query is not None else update.message`) that test 4 never exercises
    because its response is a 200 success. Pin it directly."""
    response = MagicMock(
        success=False, status_code=412, error_code="LOCATION_REQUIRED", error="LOCATION_REQUIRED"
    )
    handler = _handler(monkeypatch, response)
    update, ctx = _callback_update(), _context()
    update.callback_query = None
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

    ran = asyncio.run(handler.run_optimize_and_render(update, ctx, "en", "tok"))

    assert ran is False
    update.message.reply_text.assert_awaited_once()
    assert ctx.user_data["pending_optimize_after_location"] is True


@pytest.mark.unit
def test_unauthenticated_caller_returns_none_not_false(monkeypatch):
    """Fix round 1 (Important): `@require_auth`'s early-return
    (`staff_bot/permissions.py:73`) is a bare `return`, i.e. `None` — distinct
    from the `False` this method returns itself when it ran but the
    optimization didn't happen for a business reason (412 / API failure). A
    caller doing `is False` rather than plain truthiness must be able to tell
    "rejected by the guard" from "ran, declined". Task 6's location path is
    where this is genuinely reachable: that update didn't come from a button
    authorised a moment ago."""
    response = MagicMock(success=True, status_code=200, data={"route_locked": False})
    handler = _handler(monkeypatch, response)
    update, ctx = _callback_update(), _context()
    ctx.user_data["authenticated"] = False

    result = asyncio.run(handler.run_optimize_and_render(update, ctx, "en", "tok"))

    assert result is None
    active_mod.api_client.client.optimize_route.assert_not_awaited()


@pytest.mark.unit
def test_unauthorized_role_returns_none_not_false(monkeypatch):
    """Fix round 1 (Important): same three-valued contract, via
    `@require_delivery_driver`'s bare-return rejection
    (`staff_bot/permissions.py:115`) instead of the auth guard."""
    response = MagicMock(success=True, status_code=200, data={"route_locked": False})
    handler = _handler(monkeypatch, response)
    update, ctx = _callback_update(), _context()
    ctx.user_data["staff_roles"] = []

    result = asyncio.run(handler.run_optimize_and_render(update, ctx, "en", "tok"))

    assert result is None
    active_mod.api_client.client.optimize_route.assert_not_awaited()


# --- the pin that follows a 412 ------------------------------------------

from staff_bot.handlers.delivery import location as location_mod
from staff_bot.handlers.delivery.location import LocationHandler
from staff_bot.keyboards.menu import MenuKeyboards


class _LocationApiClient:
    def __init__(self, response):
        self.client = MagicMock()
        self.client.update_driver_location = AsyncMock(return_value=response)

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *args):
        return False


def _pin_update(accuracy=None, live=False):
    update = MagicMock()
    location = MagicMock(latitude=41.31, longitude=69.28, horizontal_accuracy=accuracy)
    message = MagicMock()
    message.location = location
    message.reply_text = AsyncMock()
    if live:
        update.message = None
        update.edited_message = message
    else:
        update.message = message
        update.edited_message = None
    update.effective_user = MagicMock(id=555)
    return update


def _location_handler(monkeypatch, response):
    handler = LocationHandler()
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(location_mod, "api_client", _LocationApiClient(response))
    return handler


@pytest.mark.unit
def test_pin_forwards_horizontal_accuracy(monkeypatch):
    handler = _location_handler(monkeypatch, MagicMock(success=True))
    update, ctx = _pin_update(accuracy=18.0), _context()

    asyncio.run(handler.handle_location_update(update, ctx))

    call = location_mod.api_client.client.update_driver_location.await_args
    assert call.args[1] == 41.31 and call.args[2] == 69.28
    assert call.kwargs.get("horizontal_accuracy") == 18.0


@pytest.mark.unit
def test_armed_pin_optimizes_and_restores_the_menu(monkeypatch):
    """The driver asked to optimize and was asked for a location instead. The
    pin must finish the job — and restore the main menu, because the card is
    edited in place and an edited message cannot carry a reply keyboard."""
    handler = _location_handler(monkeypatch, MagicMock(success=True))
    optimize = AsyncMock(return_value=True)
    monkeypatch.setattr(location_mod, "_run_optimize_after_location", optimize)
    ctx = _context()
    ctx.user_data["pending_optimize_after_location"] = True

    update = _pin_update(accuracy=12.0)
    asyncio.run(handler.handle_location_update(update, ctx))

    optimize.assert_awaited()
    assert "pending_optimize_after_location" not in ctx.user_data
    markups = [c.kwargs.get("reply_markup") for c in update.message.reply_text.await_args_list]
    expected = MenuKeyboards.main_menu("en", ["delivery_driver"])
    assert any(getattr(m, "keyboard", None) == expected.keyboard for m in markups), (
        "the driver must not be left holding the collapsed location keyboard"
    )


@pytest.mark.unit
def test_unarmed_pin_keeps_the_quiet_two_message_ack(monkeypatch):
    handler = _location_handler(monkeypatch, MagicMock(success=True))
    optimize = AsyncMock()
    monkeypatch.setattr(location_mod, "_run_optimize_after_location", optimize)
    update, ctx = _pin_update(), _context()

    asyncio.run(handler.handle_location_update(update, ctx))

    optimize.assert_not_awaited()
    assert len(update.message.reply_text.await_args_list) == 2


@pytest.mark.unit
def test_live_location_never_triggers_a_synchronous_optimize(monkeypatch):
    """Chaining an OSRM matrix solve onto every GPS tick would hammer the
    self-hosted engine."""
    handler = _location_handler(monkeypatch, MagicMock(success=True))
    optimize = AsyncMock()
    monkeypatch.setattr(location_mod, "_run_optimize_after_location", optimize)
    ctx = _context()
    ctx.user_data["pending_optimize_after_location"] = True

    asyncio.run(handler.handle_location_update(_pin_update(live=True), ctx))

    optimize.assert_not_awaited()


@pytest.mark.unit
def test_coarse_fix_keeps_the_flag_armed_and_does_not_optimize(monkeypatch):
    """Spec §4.2: the driver steps outdoors and retries in one tap, still
    getting the optimize they originally asked for. That promise only holds
    if the reply actually hands them a working location button — so this
    checks the real `reply_markup`, not just that *some* reply was sent."""
    refusal = MagicMock(success=False, status_code=400, error_code="LOCATION_TOO_COARSE")
    handler = _location_handler(monkeypatch, refusal)
    optimize = AsyncMock()
    monkeypatch.setattr(location_mod, "_run_optimize_after_location", optimize)
    ctx = _context()
    ctx.user_data["pending_optimize_after_location"] = True

    update = _pin_update(accuracy=900.0)
    asyncio.run(handler.handle_location_update(update, ctx))

    optimize.assert_not_awaited()
    assert ctx.user_data["pending_optimize_after_location"] is True
    update.message.reply_text.assert_awaited_once()
    markup = update.message.reply_text.await_args.kwargs.get("reply_markup")
    assert markup is not None, "the driver must get a keyboard back, not a bare message"
    first_button = markup.keyboard[0][0]
    assert first_button.request_location is True, (
        "a one-tap retry needs a REAL location-request button, not just any reply"
    )


@pytest.mark.unit
def test_noncoarse_failure_clears_the_flag_and_restores_the_menu(monkeypatch):
    """Spec §4.2: the flag clears on success, on a non-coarse failure, and via
    clear_pending_flows. A 500/timeout must not leave it armed -- an unrelated
    future pin would otherwise silently fire an optimize -- and must not
    strand the driver on the collapsed location panel with no reply_markup at
    all."""
    refusal = MagicMock(success=False, status_code=500, error_code="INTERNAL_ERROR")
    handler = _location_handler(monkeypatch, refusal)
    optimize = AsyncMock()
    monkeypatch.setattr(location_mod, "_run_optimize_after_location", optimize)
    ctx = _context()
    ctx.user_data["pending_optimize_after_location"] = True

    update = _pin_update(accuracy=12.0)
    asyncio.run(handler.handle_location_update(update, ctx))

    optimize.assert_not_awaited()
    assert "pending_optimize_after_location" not in ctx.user_data, (
        "a non-coarse failure must clear the armed flag -- otherwise an "
        "unrelated future pin silently fires an optimize"
    )
    update.message.reply_text.assert_awaited_once()
    markup = update.message.reply_text.await_args.kwargs.get("reply_markup")
    expected = MenuKeyboards.main_menu("en", ["delivery_driver"])
    assert markup is not None and markup.keyboard == expected.keyboard, (
        "the driver must get their main menu back, not be left holding the "
        "collapsed location panel"
    )


# --- the real seam: _run_optimize_after_location is NOT mocked ------------


class _ActiveDeliveryApiClient:
    """Stub for the API client used INSIDE active_delivery.py -- the only
    thing mocked in the test below. Everything above it (the late import of
    ActiveDeliveryHandler, its instantiation, and the (update, context,
    language, token) argument order into run_optimize_and_render) runs for
    real."""

    def __init__(self, optimize_response):
        self.client = MagicMock()
        self.client.optimize_route = AsyncMock(return_value=optimize_response)

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *args):
        return False


@pytest.mark.unit
def test_armed_pin_really_calls_the_late_imported_optimizer(monkeypatch):
    """Every armed-pin test above monkeypatches `_run_optimize_after_location`
    itself, so its late import of ActiveDeliveryHandler, the instantiation,
    and the (update, context, language, token) argument order are never
    actually executed. A wrong arg order or a genuinely circular late import
    would leave the whole suite green with the headline feature dead in
    production. Drive the real function; stub only the API client
    underneath it."""
    handler = _location_handler(monkeypatch, MagicMock(success=True))

    # A non-412 failure keeps this test out of the heavy (Redis/bot-backed)
    # route-card render pipeline while still proving the call landed for
    # real: optimize_route is only awaited if the late import resolved and
    # the handler was instantiated and called correctly.
    optimize_response = MagicMock(
        success=False, status_code=500, error_code="INTERNAL_ERROR", error="boom"
    )
    fake_active_client = _ActiveDeliveryApiClient(optimize_response)
    monkeypatch.setattr(active_mod, "api_client", fake_active_client)

    ctx = _context()
    ctx.user_data["pending_optimize_after_location"] = True

    update = _pin_update(accuracy=12.0)
    update.callback_query = None  # the post-location path never has one
    asyncio.run(handler.handle_location_update(update, ctx))

    fake_active_client.client.optimize_route.assert_awaited_once_with("tok")


# --- live_location_ack_sent bookkeeping ----------------------------------
#
# The brief called out a leak: without clearing this flag on a one-shot
# share, the first edit of the driver's NEXT live-location stream would be
# silently un-ACKed (spec §4.4). Pin both halves so a future edit can't trade
# one behaviour off against the other — clearing too eagerly (ACK spam on a
# live stream) is just as wrong as never clearing (a silently un-ACKed
# stream).


@pytest.mark.unit
def test_one_shot_share_clears_stale_live_ack_flag(monkeypatch):
    """After an earlier live-location stream, `live_location_ack_sent` is
    left True in user_data. A one-shot share afterwards must clear it — this
    is the exact leak the brief called out, and nothing else in this suite
    inspects the flag directly."""
    handler = _location_handler(monkeypatch, MagicMock(success=True))
    monkeypatch.setattr(location_mod, "_run_optimize_after_location", AsyncMock())
    update, ctx = _pin_update(), _context()
    ctx.user_data["live_location_ack_sent"] = True

    asyncio.run(handler.handle_location_update(update, ctx))

    assert "live_location_ack_sent" not in ctx.user_data, (
        "a one-shot share must clear the live-stream ACK-suppression flag so "
        "the driver's next live stream gets ACKed again"
    )


@pytest.mark.unit
def test_live_edit_sets_ack_flag_then_suppresses_the_next_edit(monkeypatch):
    """The suppression the clear above must not break: the first edit of a
    live-location stream sets the flag and ACKs; the second edit of the same
    stream must neither re-ACK nor re-optimize."""
    handler = _location_handler(monkeypatch, MagicMock(success=True))
    optimize = AsyncMock()
    monkeypatch.setattr(location_mod, "_run_optimize_after_location", optimize)
    ctx = _context()

    first_update = _pin_update(live=True)
    asyncio.run(handler.handle_location_update(first_update, ctx))

    assert ctx.user_data.get("live_location_ack_sent") is True
    assert len(first_update.edited_message.reply_text.await_args_list) == 2

    second_update = _pin_update(live=True)
    asyncio.run(handler.handle_location_update(second_update, ctx))

    second_update.edited_message.reply_text.assert_not_awaited()
    optimize.assert_not_awaited()
    assert ctx.user_data.get("live_location_ack_sent") is True


# --- the reveal step and the nag are gone ---------------------------------

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_share_location_prompt_callback_is_gone_everywhere():
    """The reveal step was a button whose only job was to show an identical
    button. Nothing may render or register it any more."""
    offenders = []
    for path in (REPO_ROOT / "staff_bot").rglob("*.py"):
        if "staff_share_location_prompt" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"stale share-location-prompt references: {offenders}"


@pytest.mark.unit
def test_accepting_a_pool_order_does_not_swap_the_drivers_keyboard():
    """The nag fired on every accept and replaced the whole main menu."""
    source = (REPO_ROOT / "staff_bot" / "handlers" / "delivery" / "orders_pool.py").read_text(
        encoding="utf-8"
    )
    assert "CommonKeyboards.location_request" not in source
    assert "share_location_after_accept" not in source


# --- copy ------------------------------------------------------------------


@pytest.mark.unit
def test_new_keys_have_curated_copy_in_all_three_languages():
    """An uncurated staff.* key gets a humanised guess seeded in en/uz/ru while
    /health stays green — so the guard belongs here, not in production."""
    import importlib.util

    seed_path = REPO_ROOT / "scripts" / "seed_staff_translations.py"
    spec = importlib.util.spec_from_file_location("seed_staff_translations", seed_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for key in ("staff.delivery.location_too_coarse",):
        entry = module.STAFF_TRANSLATIONS.get(key)
        assert entry, f"{key} needs curated copy before deploy"
        for lang in ("en", "uz", "ru"):
            assert entry.get(lang), f"{key} is missing {lang}"
