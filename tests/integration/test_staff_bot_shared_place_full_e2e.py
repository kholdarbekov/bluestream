"""The shared-place question survives a lost acknowledgement of the Delivered PUT, end to end.

The driver types the returned count at the door; the Delivered PUT commits (and opens the case)
but its answer is lost, so the client sends it again and the terminal-status guard refuses the
replay with STAFF_INVALID_STATUS_TRANSITION. The bot treats that refusal as the recorded
completion it is (the replay arm of `_submit_delivery_completion`), and the question must still
reach the driver from there: the case only exists because the first PUT landed.

Driven through `_Bridge` (tests/integration/test_staff_bot_place_full_e2e.py), which answers the
bot's calls with the REAL Flask routes. The three shared-place calls are the REAL
`StaffAPIClient` methods, so their paths and bodies are production's own, sent through the bridge.
"""

import importlib.util

import pytest

from business_app import db as _db
from business_app.models.shared_place_case import SharedPlaceCase
from staff_bot.api_client import StaffAPIClient
from staff_bot.i18n import i18n

from tests.integration.shared_place_builders import (
    BASE_LAT,
    BASE_LNG,
    customer,
    driver,
    empties_at,
    pinned_address,
    stop_for,
)
from tests.integration.test_staff_bot_place_full_e2e import (  # noqa: F401  (_no_redis_flow_state is autouse)
    REPO_ROOT,
    _Bridge,
    _deliver_at_the_door_returning,
    _no_redis_flow_state,
    _open_delivery_card,
    _reach_bottle_prompt,
    _replies,
    _token,
    http,
)
from tests.unit.test_staff_bot_place_surfaces import _callbacks

pytestmark = pytest.mark.integration

M14 = 0.000126  # ~14 m of latitude


class _SharedPlaceBridge(_Bridge):
    """`_Bridge` plus the shared-place calls: the client's own methods, sent through the bridge."""

    get_shared_place_question = StaffAPIClient.get_shared_place_question
    answer_shared_place_case = StaffAPIClient.answer_shared_place_case
    note_shared_place_case = StaffAPIClient.note_shared_place_case

    async def _make_request(self, method, endpoint, token=None, data=None, **_kwargs):
        return self._request(method, endpoint, data)


@pytest.fixture
def shared_place_copy(monkeypatch):
    """The shared-place seed's real values, so the question renders the words a driver reads."""
    path = REPO_ROOT / "scripts" / "seed_staff_shared_place_translations.py"
    spec = importlib.util.spec_from_file_location("seed_staff_shared_place_translations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    catalog = {}
    for key, langs in module.KEYS.items():
        for lang, value in langs.items():
            catalog.setdefault(lang, {})[key] = value
    monkeypatch.setattr(i18n, "translations", catalog)


def test_replay_branch_still_reaches_the_question(app, db, http, monkeypatch, shared_place_copy):
    akmal = customer(db, "Akmal")
    empties_at(pinned_address(db, akmal, BASE_LAT + M14, BASE_LNG), 8)
    jasur = customer(db, "Jasur")
    address = pinned_address(db, jasur, BASE_LAT, BASE_LNG)
    botir = driver(db, "Botir")
    _order, delivery = stop_for(jasur, address, botir, quantity=1)

    bridge = _SharedPlaceBridge(http, _token(app, botir))
    context = _open_delivery_card(monkeypatch, bridge, delivery.id)
    _reach_bottle_prompt(monkeypatch, bridge, context, delivery.id)
    bridge.retry_suffixes = [f"/delivery/{delivery.id}/status"]  # the first answer is lost

    typed = _deliver_at_the_door_returning(monkeypatch, bridge, context, delivery.id, 3)

    statuses = [call for call in bridge.calls if call["path"].endswith(f"/delivery/{delivery.id}/status")]
    assert len(statuses) == 2 and statuses[0]["payload"] == statuses[1]["payload"]
    _db.session.expire_all()
    [case] = SharedPlaceCase.query.all()
    assert (case.status, case.asked_driver_id, case.returned_qty) == ("awaiting_driver", botir.id, 3)

    assert bridge.calls[-1] == {
        "method": "GET",
        "path": f"/api/v1/staff/delivery/{delivery.id}/shared-place-question",
        "payload": None,
    }
    [shown] = _replies(typed)
    assert "Is Jasur the same as one of these?" in shown
    markup = typed.message.reply_text.call_args.kwargs["reply_markup"]
    assert _callbacks(markup) == [f"staff_sp_{case.id}_p0", f"staff_sp_{case.id}_none", f"staff_sp_{case.id}_skip"]
    assert markup.inline_keyboard[0][0].text == "Akmal · 8 empties · 14 m"
    assert "pending_delivery_cash_flow" not in context.user_data
