"""An operator reviews activation requests from the profile hub and approves / rejects them.

Driven through the real ``Application`` for the same reason
``test_sales_hub_journey.py`` is: the operator's route into this screen is a
button on a keyboard someone else draws (the profile hub) and a push
notification the backend mints (``staff_sales_approvals``, Task 10), so what
has to hold is the WIRING — the button exists for an operator and not for an
agent, the callback reaches a registered handler, and the handler sends the
backend exactly the payload the approver routes expect.

Copy comes from ``scripts/seed_staff_translations.py`` via ``_curated_value``,
the same resolver ``seed_translations()`` uses, so an edit to the seed cannot
leave this file asserting strings production no longer ships.
"""

import json

import pytest

from tests.staff_bot.ptb_harness import DEFAULT_DRIVER_TELEGRAM_ID, FakeStaffDatabase, build_staff_harness
from tests.staff_bot.test_sales_hub_journey import (
    LOGIN,
    OUTLETS,
    _alerts,
    _calls,
    _curated,
    _outlet,
    _table,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

REQUESTS = "/api/v1/staff/sales/activation-requests"
EXTRA_KEYS = (
    "staff.sales.approvals.title", "staff.sales.approvals.empty", "staff.sales.approvals.button",
    "staff.sales.approvals.approve", "staff.sales.approvals.reject", "staff.sales.approvals.approved",
    "staff.sales.approvals.rejected",
    "staff.sales.approvals.reason.duplicate", "staff.sales.approvals.reason.incomplete",
    "staff.sales.approvals.reason.not_customer", "staff.sales.approvals.reason.other",
    "staff.sales.approvals.back", "staff.profile.title", "staff.profile.name", "staff.profile.phone",
    "staff.profile.roles", "staff.profile.language",
)


def _full_table():
    table = _table()
    for key in EXTRA_KEYS:
        for lang in ("en", "uz", "ru"):
            table[(lang, key)] = _curated(key, lang)
    return table


async def _operator(monkeypatch):
    row = {"id": 56, "telegram_id": str(DEFAULT_DRIVER_TELEGRAM_ID), "first_name": "Dilnoza",
           "last_name": "Operator", "phone": "+998901234571", "preferred_language": "en",
           "role": "operator", "status": "active", "staff_roles": json.dumps(["operator"]),
           "staff_bot_state": "{}"}
    harness = await build_staff_harness(monkeypatch, translations=_full_table(),
                                        database=FakeStaffDatabase(staff_user=row))
    harness.backend.route("POST", LOGIN, lambda _c: {
        "access_token": "a", "refresh_token": "r", "expires_in": 3600,
        "user": {"id": 56, "first_name": "Dilnoza", "last_name": "Operator", "phone": "+998901234571",
                 "preferred_language": "en", "staff_roles": ["operator"], "delivery_person_id": None},
    })
    ops = harness.updates()
    await harness.send(ops.command("start"))
    harness.telegram.reset()
    harness.backend.calls.clear()
    return harness, ops


async def test_operator_lists_reviews_approves_and_rejects(monkeypatch):
    harness, ops = await _operator(monkeypatch)
    pending = [_outlet(id=5, stage="activation_requested"), _outlet(id=6, name="Navruz", stage="activation_requested")]
    harness.backend.route("GET", REQUESTS, lambda c: {"items": pending})
    harness.backend.route("POST", f"{OUTLETS}/5/approve", lambda c: {"outlet": _outlet(id=5, stage="active")})
    harness.backend.route("POST", f"{OUTLETS}/6/reject",
                          lambda c: {"outlet": _outlet(id=6, name="Navruz", stage="prospect")})

    await harness.send(ops.tap("staff_profile"))
    assert _curated("staff.sales.approvals.button") in " ".join(harness.telegram.last_shown().button_labels())

    await harness.send(ops.tap("staff_sales_approvals"))
    listing = harness.telegram.last_shown()
    assert _curated("staff.sales.approvals.title") in listing.text
    assert "Bahor market" in " ".join(listing.button_labels()) and "Navruz" in " ".join(listing.button_labels())

    await harness.send(ops.tap("staff_sales_review_5"))
    review = harness.telegram.last_shown()
    assert "Bahor market" in review.text and "+998901112266" in review.text
    assert _curated("staff.sales.approvals.approve") in " ".join(review.button_labels())

    await harness.send(ops.tap("staff_sales_approve_5"))
    assert [c.data for c in _calls(harness, "POST", f"{OUTLETS}/5/approve")] == [{}]
    approved = harness.telegram.last_shown()
    assert _curated("staff.sales.approvals.approved") in approved.text
    # An operator works a QUEUE: the way back to it has to be on the result
    # screen, or every decision costs a round trip through the main menu.
    assert _curated("staff.sales.approvals.back") in " ".join(approved.button_labels())

    await harness.send(ops.tap("staff_sales_review_6"))
    await harness.send(ops.tap("staff_sales_reject_6_duplicate"))
    assert [c.data for c in _calls(harness, "POST", f"{OUTLETS}/6/reject")] == [{"reason": "Duplicate outlet"}]
    rejected = harness.telegram.last_shown()
    assert _curated("staff.sales.approvals.rejected") in rejected.text
    assert _curated("staff.sales.approvals.back") in " ".join(rejected.button_labels())

    # And that way back is live, not just drawn.
    await harness.send(ops.tap("staff_sales_approvals"))
    assert _curated("staff.sales.approvals.title") in harness.telegram.last_shown().text


async def test_empty_queue_and_non_operator_denied(monkeypatch):
    harness, ops = await _operator(monkeypatch)
    harness.backend.route("GET", REQUESTS, lambda c: {"items": []})
    await harness.send(ops.tap("staff_sales_approvals"))
    assert _curated("staff.sales.approvals.empty") in harness.telegram.last_shown().text

    from tests.staff_bot.test_sales_hub_journey import _agent

    agent_harness, agent_ops, _labels = await _agent(monkeypatch)
    await agent_harness.send(agent_ops.tap("staff_sales_approvals"))
    assert _curated("staff.unauthorized") in _alerts(agent_harness)


async def test_a_reason_the_keyboard_never_drew_is_answered_not_sent(monkeypatch):
    r"""The registered pattern is `^staff_sales_reject_\d+_\w+$`, not the
    four-way alternation the keyboard draws.

    It cannot be narrower: an alternation is a shape
    `test_every_registered_pattern_is_readable_by_the_collision_check` cannot
    sample, so registering one would silently stop the theft check from looking
    at this button at all. The REASON is therefore re-validated here, the same
    way `SalesHubHandler.show_list` re-checks its scope and
    `NewOutletHandler._stay` re-checks its step — a value the map has no entry
    for must be ANSWERED (so the operator's button stops spinning) and must
    never reach the backend, where `reason` is stored verbatim and shown in the
    admin UI.
    """
    harness, ops = await _operator(monkeypatch)
    harness.backend.route("GET", REQUESTS, lambda c: {"items": [_outlet(id=6, stage="activation_requested")]})
    harness.backend.route("POST", f"{OUTLETS}/6/reject", lambda c: {"outlet": _outlet(id=6)})

    await harness.send(ops.tap("staff_sales_review_6"))
    harness.telegram.reset()
    await harness.send(ops.tap("staff_sales_reject_6_whatever"))

    assert not _calls(harness, "POST", f"{OUTLETS}/6/reject")
    assert len(harness.telegram.of("answerCallbackQuery")) == 1
