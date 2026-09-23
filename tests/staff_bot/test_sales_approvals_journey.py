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

from tests.staff_bot.ptb_harness import (
    DEFAULT_DRIVER_TELEGRAM_ID,
    FakeStaffDatabase,
    build_staff_harness,
    staff_backend_failure,
)
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
    # D25: the attach door. `error.stage_invalid` is already seeded -- it is
    # the sentence `review` shows when the request has gone, and the one the
    # backend's own refusal maps to -- but it was never in this table, so the
    # harness served a humanised fallback for it.
    "staff.sales.approvals.attach", "staff.sales.approvals.attached",
    "staff.sales.approvals.candidate", "staff.sales.error.stage_invalid",
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


async def test_a_request_whose_phone_is_a_known_account_attaches_instead_of_approving(monkeypatch):
    """D25 rule 3: the phone already belongs to a customer account.

    A plain approve on it is a 409 (`SALES_APPROVAL_PHONE_TAKEN`), so the
    backend publishes the candidate on the activation-request row, the review
    card names WHO it is, and the keyboard draws Attach INSTEAD of Approve. The
    operator is never handed a button whose only outcome is a refusal -- the
    same rule the outlet card follows for Start visit vs Resume visit -- and
    the bot resolves no phone of its own to decide it.
    """
    harness, ops = await _operator(monkeypatch)
    # The activation-requests row: `serialize_outlet` plus `account_candidate`.
    pending = [_outlet(id=5, stage="activation_requested",
                       account_candidate={"user_id": 41, "name": "Bahor Savdo MChJ",
                                          "outlet_count": 2})]
    harness.backend.route("GET", REQUESTS, lambda c: {"items": pending})
    harness.backend.route("POST", f"{OUTLETS}/5/approve",
                          lambda c: {"outlet": _outlet(id=5, stage="active")})

    await harness.send(ops.tap("staff_sales_approvals"))
    await harness.send(ops.tap("staff_sales_review_5"))

    review = harness.telegram.last_shown()
    # The rendered line and button are asserted WHOLE and literally (R31/R44):
    # an expectation composed from the seed would double its glyph along with
    # a seed row that grew one, and still pass.
    assert "🔗 Phone belongs to Bahor Savdo MChJ (outlets: 2)" in review.text.split("\n")
    assert "🔗 Attach to existing account" in review.button_labels()
    assert _curated("staff.sales.approvals.approve") not in " ".join(review.button_labels())
    assert "staff_sales_attach_5" in review.callback_data()
    assert "staff_sales_approve_5" not in review.callback_data()

    await harness.send(ops.tap("staff_sales_attach_5"))

    assert [c.data for c in _calls(harness, "POST", f"{OUTLETS}/5/approve")] == [{"attach": True}]
    attached = harness.telegram.last_shown()
    assert attached.text.split("\n")[0] == (
        "✅ Outlet attached to the existing account as a branch. The agent has been notified."
    )
    assert _curated("staff.sales.approvals.approved") not in attached.text
    # Still working a queue: the way back to it is on the result screen.
    assert _curated("staff.sales.approvals.back") in " ".join(attached.button_labels())


async def test_the_account_name_on_the_review_card_is_html_escaped(monkeypatch):
    """The candidate's name is whatever the customer typed as their company, and the review
    card is sent with parse_mode=HTML: one raw '<' and Telegram refuses the whole card, so the
    operator gets no review screen -- and no Attach button -- at all."""
    harness, ops = await _operator(monkeypatch)
    harness.backend.route("GET", REQUESTS, lambda c: {"items": [
        _outlet(id=5, stage="activation_requested",
                account_candidate={"user_id": 41, "name": "Bahor <Savdo> & Co", "outlet_count": 2}),
    ]})

    await harness.send(ops.tap("staff_sales_approvals"))
    await harness.send(ops.tap("staff_sales_review_5"))

    review = harness.telegram.last_shown()
    assert "🔗 Phone belongs to Bahor &lt;Savdo&gt; &amp; Co (outlets: 2)" in review.text.split("\n")
    assert "<Savdo>" not in review.text


async def test_an_attach_that_lost_its_race_is_an_alert_not_a_crash(monkeypatch):
    """Somebody approved the request between the queue being drawn and the
    button being tapped.

    The attach door answers the way every other sales write does -- the
    backend's own error code, translated, as an alert -- and the operator keeps
    the screen they are standing on instead of a spinner.
    """
    harness, ops = await _operator(monkeypatch)
    harness.backend.route("GET", REQUESTS, lambda c: {"items": [
        _outlet(id=5, stage="activation_requested",
                account_candidate={"user_id": 41, "name": "Bahor Savdo MChJ", "outlet_count": 2}),
    ]})
    harness.backend.route("POST", f"{OUTLETS}/5/approve", lambda c: staff_backend_failure(
        "stage invalid", 409, "SALES_OUTLET_STAGE_INVALID"))

    await harness.send(ops.tap("staff_sales_approvals"))
    await harness.send(ops.tap("staff_sales_review_5"))
    await harness.send(ops.tap("staff_sales_attach_5"))

    assert f"❌ {_curated('staff.sales.error.stage_invalid')}" in _alerts(harness)
    assert _curated("staff.sales.approvals.attached") not in harness.telegram.last_shown().text


async def test_empty_queue_and_non_operator_denied(monkeypatch):
    harness, ops = await _operator(monkeypatch)
    harness.backend.route("GET", REQUESTS, lambda c: {"items": []})
    await harness.send(ops.tap("staff_sales_approvals"))
    assert _curated("staff.sales.approvals.empty") in harness.telegram.last_shown().text

    from tests.staff_bot.test_sales_hub_journey import _agent

    agent_harness, agent_ops, _labels = await _agent(monkeypatch)
    await agent_harness.send(agent_ops.tap("staff_sales_approvals"))
    assert _curated("staff.unauthorized") in _alerts(agent_harness)

    # The Attach door is its own callback, so it carries its own operator guard: a stale or
    # forwarded button tapped by an agent is refused HERE, before anything is posted. (The
    # guard lint accepts `require_auth` alone, so only this tap notices a dropped
    # `@require_operator`.)
    agent_harness.telegram.reset()
    await agent_harness.send(agent_ops.tap("staff_sales_attach_5"))
    assert _curated("staff.unauthorized") in _alerts(agent_harness)
    assert not _calls(agent_harness, "POST", f"{OUTLETS}/5/approve")


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
