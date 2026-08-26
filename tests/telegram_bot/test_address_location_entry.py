"""Spec §6: a customer with no saved address reaches checkout, shares a pin on
THAT message, and the address flow starts with the checkout origin intact.

Drives the real handlers, not the service layer: the break this guards against
is a contract one. A pin arriving before the conversation exists (zero-address
checkout arms the location keyboard without ever starting the
`ConversationHandler`) still has to land in `location_received`, not be
dropped or misrouted.

RULING 2026-08-25 (see `.superpowers/sdd/2026-08-25-support-inbox-rich-messages/
progress.md`, "SPEC DEFECT: 'a pin with no flow open' does not exist"): a
spontaneous pin the bot never asked for is now, BY DESIGN, filed as a support
message — that is the correct outcome, not a bug. Filing to support only
becomes wrong when the bot DID ask for the pin (via
`utils.arm_location_request`, called at every `location_request(...)` site,
including the zero-address-checkout prompt this file drives). Do not restore
the old assumption that any bare pin should always start address creation —
the address entry point (`bot.py::_route_address_location_entry`) decides
per-update whether an arming marker is present, not whether a conversation
happens to be "active"."""

from unittest.mock import AsyncMock, MagicMock

import pytest

# Imported at module (collection) level, not inside a test function, so that
# `i18n`, `keyboards` and `config` are cached in sys.modules as the BOT's
# versions (resolved via handlers/profile.py's own bare imports) before any
# fixture below tries to reach them by string path. `handlers.profile` is a
# workdir-relative bare import — see tests/telegram_bot/conftest.py, which
# puts telegram_bot/ first on sys.path and evicts any stale same-named
# modules. Deferring this import into a test body works when this file runs
# alongside others that already import handlers.profile first (e.g. the full
# suite), but running this file in isolation (as Step 3 of the task does)
# leaves "i18n" unimported when the echo_i18n fixture runs, and pytest's
# string-path monkeypatch.setattr then does a *fresh* `__import__("i18n")`
# that can resolve `from config import config` against the repo-root
# config.py instead of telegram_bot/config.py.
from handlers.profile import ProfileHandlers

from tests.telegram_bot.helpers import DummyLocation, DummyUpdate, make_context

pytestmark = [pytest.mark.unit, pytest.mark.anyio]


@pytest.fixture
def echo_i18n(monkeypatch):
    """i18n.get returns a humanised key tail and SILENTLY DROPS kwargs on a
    missing key, so an unstubbed render test passes against broken code. Echo
    the key plus every interpolated value instead."""
    def _get(key, language=None, *args, **kwargs):
        if kwargs:
            return f"{key}|" + "|".join(f"{k}={v}" for k, v in sorted(kwargs.items()))
        return key

    monkeypatch.setattr("i18n.i18n.get", _get)
    monkeypatch.setattr("i18n.i18n.get_user_language", AsyncMock(return_value="en"))
    return _get


def test_a_pin_can_start_the_address_conversation():
    """Structural: without a LOCATION entry point the armed keyboard's pin has
    nowhere to land. Reads the registration itself so a future refactor that
    drops the entry point fails here rather than in production.

    Scoped to the ADDRESS conversation specifically: `entry_points=[` first
    appears at the REGISTRATION conversation earlier in bot.py, and
    `filters.LOCATION` already appears inside the address conversation's
    `states` block regardless of this change — a loosely-scoped search would
    pass vacuously even if the entry point were missing.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "telegram_bot" / "bot.py"
    ).read_text(encoding="utf-8")

    after_address_handler = source.split("address_handler = ConversationHandler(", 1)[1]
    entry_block = after_address_handler.split("entry_points=[", 1)[1].split("]", 1)[0]

    assert "filters.LOCATION" in entry_block, (
        "the address conversation must accept a pin as an entry point — "
        "zero-address checkout arms the keyboard before the flow starts"
    )


async def test_pin_at_checkout_stores_coordinates_and_keeps_the_origin(echo_i18n, monkeypatch):
    """The pin must reach location_received, populate temp_address_data, and
    leave address_flow_origin alone so the save routes back into checkout."""
    handler = ProfileHandlers()

    # In-zone Tashkent coordinates; the zone check is the real SSOT function.
    update = DummyUpdate()
    update.message.location = DummyLocation(41.31, 69.28)

    ctx = make_context()
    ctx.user_data["address_flow_origin"] = "checkout"

    # Reverse geocoding is external I/O — the only thing mocked here.
    api = MagicMock()
    api.reverse_geocode = AsyncMock(
        return_value=MagicMock(success=True, data={"data": {"formatted_address": "Chilanzar 5"}})
    )

    class _Client:
        async def __aenter__(self):
            return api

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("handlers.profile.api_client", _Client())
    monkeypatch.setattr("handlers.profile.get_auth_token", AsyncMock(return_value="tok"))

    await handler.location_received(update, ctx)

    stored = ctx.user_data["temp_address_data"]
    assert stored["latitude"] == 41.31
    assert stored["longitude"] == 69.28
    assert stored["location_source"] == "shared"
    assert ctx.user_data["address_flow_origin"] == "checkout", (
        "losing the origin dumps the customer on the main menu with a full cart"
    )


async def test_out_of_zone_pin_reprompts_with_a_location_keyboard(echo_i18n, monkeypatch):
    """A pin outside TASHKENT_POLYGON must re-offer the location button rather
    than stranding the customer with a collapsed keyboard."""
    handler = ProfileHandlers()
    update = DummyUpdate()
    update.message.location = DummyLocation(55.75, 37.61)  # Moscow — out of zone
    ctx = make_context()

    monkeypatch.setattr(
        "keyboards.ProfileKeyboards.location_request",
        staticmethod(lambda _lang, **_kw: "loc-kbd"),
    )

    await handler.location_received(update, ctx)

    assert update.message.reply_text.await_args.kwargs["reply_markup"] == "loc-kbd"
    assert "temp_address_data" not in ctx.user_data or not ctx.user_data[
        "temp_address_data"
    ].get("latitude")
