"""The shared-place question and note, driven through the staff bot against the REAL backend.

A first-order customer at a fresh pinned address hands back 3 empties 14 m from Akmal's place,
which holds 8. The driver's bot runs through the real staff dispatcher, and its own
`_make_request` (signing included) is answered by the real Flask app, so the delivery PUT really
opens the case, the question really comes from the GET, and the pick and the note really land
on the case row. Only Telegram, the bot's own SQL and its token Redis are faked.
"""

import pytest

from business_app.models.shared_place_case import SharedPlaceCase
from staff_bot import api_client as staff_api_module

from tests.bot_dispatcher_harness import FlaskBackend
from tests.integration.shared_place_builders import (
    BASE_LAT,
    BASE_LNG,
    customer,
    driver,
    empties_at,
    pinned_address,
    stop_for,
)
from tests.staff_bot.ptb_harness import DEFAULT_DRIVER_TELEGRAM_ID
from tests.staff_bot.test_staff_bot_signs_in_again_after_revocation import _token_manager
from tests.staff_bot.test_staff_delivery_journey_dispatcher import (
    FakeDeliveryDesk,  # noqa: F401  (build_driver attaches it)
    _isolated_route_card_state,  # noqa: F401  (autouse fixture: fresh route-card locks per test)
    build_driver,
    capture_errors,
    menu_label,
    sign_in,
)
from tests.staff_bot.test_staff_shared_place_question import BOTTLE_COPY, _seed_keys

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

M14 = 0.000126  # ~14 m of latitude
NOTE = "Akmal's coworker, same office"


async def _driver_bot_on_the_real_backend(app, monkeypatch):
    """The driver's staff bot, its own request funnel answered by the real Flask app."""
    bot = await build_driver(monkeypatch, extra_keys=BOTTLE_COPY, translations=_seed_keys())
    tokens = _token_manager()
    bot.application.bot_data["token_manager"] = tokens
    client = staff_api_module.api_client
    monkeypatch.setattr(client, "token_manager", tokens)
    monkeypatch.delattr(client, "_make_request")  # the bot's own funnel, not the harness double
    backend = FlaskBackend(app)
    monkeypatch.setattr(client, "_client", backend)
    return bot, backend


async def test_driver_journey_against_real_routes(app, db, monkeypatch):
    akmal = customer(db, "Akmal")
    empties_at(pinned_address(db, akmal, BASE_LAT + M14, BASE_LNG), 8)
    jasur = customer(db, "Jasur")
    address = pinned_address(db, jasur, BASE_LAT, BASE_LNG)
    botir = driver(db, "Botir")
    botir.telegram_id = str(DEFAULT_DRIVER_TELEGRAM_ID)
    botir.preferred_language = "en"
    db.session.commit()
    _order, delivery = stop_for(jasur, address, botir, quantity=1)

    bot, backend = await _driver_bot_on_the_real_backend(app, monkeypatch)
    errors = capture_errors(bot)
    tapper, labels = await sign_in(bot)
    await bot.send(tapper.text(menu_label(labels, "staff.menu.active_deliveries")))
    await bot.send(tapper.tap(f"staff_view_active_{delivery.id}"))
    await bot.send(tapper.tap(f"staff_status_{delivery.id}_delivered"))
    await bot.send(tapper.tap(f"staff_execute_status_{delivery.id}_delivered"))
    await bot.send(tapper.tap(f"staff_bottles_custom_{delivery.id}"))
    await bot.send(tapper.text("3"))

    [case] = SharedPlaceCase.query.all()
    question = bot.telegram.last_shown()
    assert "Is Jasur the same as one of these?" in question.text
    assert question.button_labels()[0] == "Akmal · 8 empties · 14 m"
    assert question.callback_data() == [
        f"staff_sp_{case.id}_p0", f"staff_sp_{case.id}_none", f"staff_sp_{case.id}_skip",
    ]

    await bot.send(tapper.tap(f"staff_sp_{case.id}_p0"))
    assert bot.telegram.last_shown().text == "✅ Akmal\nAnything for the admin? Type it, or Skip."

    await bot.send(tapper.text(NOTE))

    assert bot.telegram.last_shown().text == "Thanks — sent to the admin."
    db.session.expire_all()
    case = db.session.get(SharedPlaceCase, case.id)
    assert (case.status, case.driver_answer, case.picked_index, case.driver_note) == (
        "awaiting_admin", "picked", 0, NOTE,
    )
    assert [step[:3] for step in backend.seen if "shared-place" in step[1]] == [
        ("GET", f"/api/v1/staff/delivery/{delivery.id}/shared-place-question", 200),
        ("PUT", f"/api/v1/staff/shared-place-cases/{case.id}/answer", 200),
        ("PUT", f"/api/v1/staff/shared-place-cases/{case.id}/note", 200),
    ]
    assert not errors, errors
