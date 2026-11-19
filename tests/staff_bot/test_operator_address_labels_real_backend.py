"""The operator's address screens against the REAL backend (address-label contract, 2026-11-17).

`StaffService.add_client_address` writes `title` and the staff bot reads `title`, but the operator address endpoints
answered a hand-built dict that renamed it to `label`. So "Manage addresses" printed an empty bold name on every row
and the phone-order picker fell back to the street line: "Uy" and "Ofis" were lost, and two similar street lines were
one guess apart. The endpoints answer `UserAddress.to_dict()` now, `display_label` included.

The real staff bot runs through the real dispatcher and its own `_make_request` is answered by the real Flask app, so
the rows on screen are what `GET /api/v1/staff/operator/users/<id>/addresses` really sends. Only Telegram, the bot's
own SQL and its token Redis are faked.
"""

import pytest

from business_app.models.user import UserAddress
from staff_bot import api_client as staff_api_module

from tests.bot_dispatcher_harness import FlaskBackend
from tests.integration.test_operator_order_price_parity import operator  # noqa: F401  (fixture)
from tests.staff_bot.ptb_harness import DEFAULT_DRIVER_TELEGRAM_ID
from tests.staff_bot.test_staff_bot_signs_in_again_after_revocation import _token_manager
from tests.staff_bot.test_staff_operator_journey_dispatcher import (
    _curated,
    _translation_table,
    build_staff,
    capture_errors,
    menu_label,
    sign_in,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

UY_STREET = "Chilonzor 9-kvartal, 14-uy, Toshkent"
UNTITLED_STREET = "Amir Temur ko'chasi 108, Toshkent"
# The two screens' own copy, on top of the operator journey's table.
SCREEN_KEYS = ("staff.operator.addresses_title", "staff.operator.address")


@pytest.fixture
def addresses(db, sample_user):
    """One address the customer named and one saved without a title (the admin form allows it)."""
    rows = [
        UserAddress(user_id=sample_user.id, title="Uy", full_address=UY_STREET, city="Toshkent"),
        UserAddress(user_id=sample_user.id, title=None, full_address=UNTITLED_STREET, city="Toshkent"),
    ]
    db.session.add_all(rows)
    db.session.commit()
    return rows


async def _operator_bot(app, db, monkeypatch, operator_user):
    operator_user.telegram_id = str(DEFAULT_DRIVER_TELEGRAM_ID)
    operator_user.preferred_language = "en"
    db.session.commit()
    extra = {(language, key): _curated(key, language) for key in SCREEN_KEYS for language in ("en", "uz", "ru")}
    bot = await build_staff(monkeypatch, roles=["operator"], language="en", translations=_translation_table(extra))
    tokens = _token_manager()
    bot.application.bot_data["token_manager"] = tokens
    client = staff_api_module.api_client
    monkeypatch.setattr(client, "token_manager", tokens)
    monkeypatch.delattr(client, "_make_request")  # the bot's own funnel, not the harness double
    monkeypatch.setattr(client, "_client", FlaskBackend(app))
    return bot


async def _tap(bot, ops, callback_data):
    """Tap a button the bot itself drew on its last screen."""
    shown = bot.telegram.last_shown()
    assert callback_data in shown.callback_data(), (callback_data, shown.text)
    await bot.send(ops.tap(callback_data))


async def test_manage_addresses_names_each_row(app, db, sample_user, addresses, operator, monkeypatch):  # noqa: F811
    bot = await _operator_bot(app, db, monkeypatch, operator)
    errors = capture_errors(bot)
    ops, labels = await sign_in(bot)

    await bot.send(ops.text(menu_label(labels, "staff.menu.search_client")))
    await bot.send(ops.text(sample_user.phone))
    await _tap(bot, ops, f"staff_op_addresses_{sample_user.id}")

    lines = bot.telegram.last_shown().text.split("\n")
    assert lines[2:5] == ["📍 <b>Uy</b>", f"    {UY_STREET}", ""]
    assert lines[5:7] == [f"📍 <b>{_curated('staff.operator.address', 'en')}</b>", f"    {UNTITLED_STREET}"], (
        "an untitled address is named by the generic word, never by an empty <b></b>"
    )
    assert not errors, errors


async def test_the_phone_order_picker_names_each_address(app, db, sample_user, addresses, operator, monkeypatch):  # noqa: F811
    titled, untitled = addresses
    bot = await _operator_bot(app, db, monkeypatch, operator)
    errors = capture_errors(bot)
    ops, labels = await sign_in(bot)

    await bot.send(ops.text(menu_label(labels, "staff.menu.create_order")))
    await bot.send(ops.text(sample_user.phone))
    await _tap(bot, ops, f"staff_op_order_{sample_user.id}")

    picker = bot.telegram.last_shown()
    assert picker.text == _curated("staff.operator.select_address", "en")
    assert list(zip(picker.button_labels(), picker.callback_data()))[:2] == [
        ("📍 Uy", f"staff_op_addr_{titled.id}"),
        (f"📍 {UNTITLED_STREET}", f"staff_op_addr_{untitled.id}"),
    ]
    assert not errors, errors
