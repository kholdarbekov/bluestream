"""The co-driver screens must name the driver (and count the bottles) they promise to.

Three screens in the co-driver session flow are built entirely around one
interpolated value:

* "Joined {name}'s session!"                 — WHOSE session did I just join?
* "Using {name}'s session — {qty} bottles"   — whose, and how many left?
* "{name} has been added as a co-driver"     — who did I just invite?

Each fetched its copy with ``i18n.get(key, language)`` and called ``.format()``
on the RESULT. That worked only while ``get()`` returned templates. Since the
rendering rule moved to ``shared.i18n_rendering.render_translation`` a template
the caller did not fill is treated as broken copy and degraded to the humanised
key, so ``get()`` returned "Joined session" / "Current membership" / "Codriver
invited", ``.format()`` found no field to fill, succeeded, and the driver read a
sentence with the only information in it removed. There is no exception and no
log line — which is why it needs a test.

THE REAL ``i18n.get`` IS USED ON PURPOSE. ``tests/staff_bot/ptb_harness.py``
installs a ``get`` stub that still hands back the raw template when called
without values, so a harness-driven journey renders these screens correctly
whether the handler is fixed or not. The copy is loaded from the seed script
(the same ``_curated_value`` ``seed_translations()`` calls) rather than pasted,
so a future edit to the seed cannot leave this file asserting copy that no
longer ships.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.handlers.delivery import bottle_session as mod
from staff_bot.handlers.delivery.bottle_session import BottleSessionMembershipHandler
from staff_bot.i18n import i18n

pytestmark = [pytest.mark.unit, pytest.mark.anyio]


_SEED_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_staff_translations", _SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SEED = _load_seed_module()

LANGUAGES = ("en", "uz", "ru")

KEYS = (
    "staff.bottles.joined_session",
    "staff.bottles.joined_session_info",
    "staff.bottles.current_membership",
    "staff.bottles.current_membership_title",
    "staff.bottles.leave_session",
    "staff.bottles.codriver_invited",
    "staff.common.unknown_driver",
    "staff.back",
)


def _seeded_table() -> dict:
    table = {}
    for language in LANGUAGES:
        rows = {}
        for key in KEYS:
            value = _SEED._curated_value(key, language)
            assert value, f"{key} is not seeded in {language} — the screen would render a placeholder"
            rows[key] = value
        table[language] = rows
    return table


# What the driver saw instead of the sentence: humanise_key() of each key.
HUMANISED_JOINED = "Joined session"
HUMANISED_MEMBERSHIP = "Current membership"
HUMANISED_INVITED = "Codriver invited"


class _FakeClient:
    """`async with api_client as client` over one canned response."""

    def __init__(self, **responses):
        for name, response in responses.items():
            setattr(self, name, AsyncMock(return_value=response))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _ok(data):
    return SimpleNamespace(success=True, data=data, error=None, status_code=200, error_code=None)


def _callback(data):
    query = MagicMock()
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.data = data
    update = MagicMock()
    update.callback_query = query
    update.message = None
    update.effective_user = MagicMock(id=4242)
    return update, query


def _context(language="uz"):
    context = MagicMock()
    context.user_data = {
        "language": language,
        "authenticated": True,
        "staff_roles": ["delivery_driver"],
    }
    context.bot_data = {}
    return context


@pytest.fixture
def real_copy(monkeypatch):
    monkeypatch.setattr(i18n, "translations", _seeded_table())


@pytest.fixture
def handler(monkeypatch):
    instance = BottleSessionMembershipHandler()
    monkeypatch.setattr(instance, "_get_auth_token", AsyncMock(return_value="staff-token"))
    return instance


def _rendered(query) -> str:
    call = query.edit_message_text.call_args
    return call.args[0] if call.args else call.kwargs["text"]


@pytest.mark.parametrize("language", LANGUAGES)
async def test_joining_a_session_names_whose_session_it_is(
    real_copy, handler, monkeypatch, language
):
    monkeypatch.setattr(
        mod, "api_client",
        _FakeClient(join_bottle_session=_ok({"owner_name": "Aziz Karimov"})),
    )
    update, query = _callback("bottles_join_execute_17")

    await handler.execute_join_session(update, _context(language))

    text = _rendered(query)
    assert "Aziz Karimov" in text, (
        "the driver is not told whose session they just joined — two trucks "
        "share one inventory and this name is the only thing distinguishing them"
    )
    assert HUMANISED_JOINED not in text
    assert "{" not in text and "}" not in text


@pytest.mark.parametrize("language", LANGUAGES)
async def test_membership_status_names_the_owner_and_counts_the_bottles(
    real_copy, handler, monkeypatch, language
):
    monkeypatch.setattr(
        mod, "api_client",
        _FakeClient(
            get_current_session_membership=_ok(
                {"owner_name": "Aziz Karimov", "current_inventory": 37}
            )
        ),
    )
    update, query = _callback("bottles_membership_status")

    await handler.show_membership_status(update, _context(language))

    text = _rendered(query)
    assert "Aziz Karimov" in text
    assert "37" in text, (
        "the bottle count is the number the driver is held accountable for; "
        "dropping it makes this screen worse than useless"
    )
    assert HUMANISED_MEMBERSHIP not in text
    assert "{" not in text and "}" not in text


@pytest.mark.parametrize("language", LANGUAGES)
async def test_inviting_a_codriver_names_who_was_invited(
    real_copy, handler, monkeypatch, language
):
    monkeypatch.setattr(
        mod, "api_client",
        _FakeClient(invite_driver_to_session=_ok({"member_name": "Bek Toshev"})),
    )
    update, query = _callback("bottles_invite_execute_9")

    await handler.execute_invite_driver(update, _context(language))

    text = _rendered(query)
    assert "Bek Toshev" in text, (
        "the owner is not told who they just handed their bottle inventory to"
    )
    assert HUMANISED_INVITED not in text
    assert "{" not in text and "}" not in text


@pytest.mark.parametrize("language", LANGUAGES)
async def test_the_invite_receipt_does_not_decorate_copy_that_already_decorates_itself(
    real_copy, handler, monkeypatch, language
):
    """``staff.bottles.codriver_invited`` ships its OWN ✅ and its own ``<b>``.

    Unlike its sibling ``staff.bottles.joined_session`` ("Joined {name}'s
    session!"), which is a bare sentence the handler is meant to decorate, this
    row is seeded as ``✅ <b>{name}</b> has been added…`` in all three
    languages. ``execute_invite_driver`` wrapped the rendered line in
    ``f"✅ <b>{...}</b>"`` anyway, so the owner read a doubled tick and
    ``parse_mode="HTML"`` got NESTED ``<b>`` tags around a line that only ever
    wanted one.

    Fixed on the handler side rather than in the seed: the copy is the half that
    is right, and changing it would need a reseed on every environment before
    the screen came back.
    """
    monkeypatch.setattr(
        mod, "api_client",
        _FakeClient(invite_driver_to_session=_ok({"member_name": "Bek Toshev"})),
    )
    update, query = _callback("bottles_invite_execute_9")

    await handler.execute_invite_driver(update, _context(language))

    text = _rendered(query)
    expected = _seeded_table()[language]["staff.bottles.codriver_invited"].replace(
        "{name}", "Bek Toshev"
    )
    assert text == expected, (
        "the invite receipt is no longer exactly the seeded line: the handler is "
        "decorating copy that already carries its own decoration"
    )
    assert text.count("✅") == 1, f"doubled tick: {text!r}"
    assert text.count("<b>") == 1 and text.count("</b>") == 1, (
        f"nested <b> tags under parse_mode=HTML: {text!r}"
    )
