"""A sales agent signs in, opens My outlets, lists, opens a card and requests activation.

Driven through the real ``Application`` (``tests/staff_bot/ptb_harness.py``), so
what is asserted is the WIRING as well as the rendering: the reply-keyboard
label the agent sees, the router that has to recognise it, the callbacks the
inline keyboards emit, and the backend calls each one makes. A hub that renders
perfectly but is registered behind the LOCATION handler, or a list button whose
callback_data no pattern matches, fails here and nowhere else.

Copy comes from ``scripts/seed_staff_translations.py`` via ``_curated_value`` —
the same resolver ``seed_translations()`` uses — so a future edit to the seed
cannot leave this file asserting strings production no longer ships.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from tests.staff_bot.ptb_harness import (
    DEFAULT_DRIVER_TELEGRAM_ID,
    FakeStaffDatabase,
    build_staff_harness,
    staff_backend_failure,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "seed_staff_translations", ROOT / "scripts" / "seed_staff_translations.py"
)
_SEED = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_SEED)

LOGIN = "/api/v1/staff/auth/login"
OUTLETS = "/api/v1/staff/sales/outlets"
KEYS = (
    "staff.menu.my_outlets", "staff.menu.new_outlet",
    "staff.menu.profile", "staff.menu.settings", "staff.menu.help", "staff.menu.title",
    "staff.sales.hub.title", "staff.sales.hub.hint", "staff.sales.hub.prospects", "staff.sales.hub.all",
    "staff.sales.list.title_prospects", "staff.sales.list.title_all", "staff.sales.list.empty",
    "staff.sales.list.next", "staff.sales.list.prev",
    "staff.sales.card.stage", "staff.sales.card.class", "staff.sales.card.contact", "staff.sales.card.address",
    "staff.sales.card.receivable", "staff.sales.card.bottles", "staff.sales.card.notes", "staff.sales.card.last_orders",
    "staff.sales.card.request_activation", "staff.sales.card.navigate", "staff.sales.card.back_to_hub",
    "staff.sales.card.activation_requested",
    "staff.sales.stage.prospect", "staff.sales.stage.activation_requested", "staff.sales.stage.active",
    "staff.sales.type.grocery_store", "staff.sales.type.workplace", "staff.sales.type.individual",
    "staff.role.sales_agent", "staff.back", "staff.session_expired", "staff.unauthorized", "staff.error_occurred",
    "staff.error.api.forbidden", "staff.error.api.validation", "staff.error.api.unexpected",
    "staff.sales.error.phone_required", "staff.sales.error.duplicate",
    "staff.error.api.service_unavailable",
    # The money and status SSOTs the card renders through, and the only help
    # string in the bot that carries HTML markup.
    "staff.currency.uzs", "staff.order.status.delivered", "staff.help.sales_agent",
    # The "New outlet" walk-in (tests/staff_bot/test_sales_new_outlet_journey.py
    # imports this table): every step's copy, plus the shared confirm/cancel
    # words and the operator refusals it reuses rather than reseeding.
    "staff.sales.new.choose_type", "staff.sales.new.enter_name",
    "staff.sales.new.enter_contact_name", "staff.sales.new.enter_contact_phone",
    "staff.sales.new.share_pin", "staff.sales.new.share_pin_hint", "staff.sales.new.share_pin_button",
    "staff.sales.new.pin_received", "staff.sales.new.choose_class", "staff.sales.new.enter_notes",
    "staff.sales.new.skip", "staff.sales.new.summary_title", "staff.sales.new.confirm_hint",
    "staff.sales.new.duplicates_title", "staff.sales.new.link_existing",
    "staff.sales.new.open_existing", "staff.sales.new.create_anyway", "staff.sales.new.created",
    "staff.confirm", "staff.cancel", "staff.cancelled", "staff.flow_timed_out",
    "staff.operator.invalid_phone", "staff.operator.outside_delivery_area",
    "staff.operator.address_not_found",
    # Phase 2a, Task 10: the due list and the visit block on the card. Every
    # key rendered has to be here — the harness serves translations from this
    # table and falls back to `humanise_key`, which never returns falsy, so a
    # key left out of KEYS renders a plausible English word instead of failing.
    "staff.sales.hub.due", "staff.sales.list.title_due", "staff.sales.list.overdue_suffix",
    "staff.sales.card.last_visit", "staff.sales.card.next_due", "staff.sales.card.overdue",
    "staff.sales.card.rate", "staff.sales.card.suggested",
    "staff.sales.card.start_visit", "staff.sales.card.resume_visit",
    "staff.sales.stage.at_risk", "staff.sales.visit.outcome.order_placed",
    # Phase 2a — the visit loop's first half
    # (tests/staff_bot/test_sales_visit_journey.py imports this table): the
    # 409 offer, check-in, the shelf screens, and the order screen the stock
    # check lands on. The four `outcome.*` rows are here because the RESUME
    # path can land on the close screen today, even though Task 12 owns it.
    "staff.sales.visit.started", "staff.sales.visit.resume_or_abandon",
    "staff.sales.visit.resume", "staff.sales.visit.abandon", "staff.sales.visit.abandoned",
    "staff.sales.visit.checkin_prompt", "staff.sales.visit.checkin_button",
    "staff.sales.visit.checkin_skip", "staff.sales.visit.checkin_ok",
    "staff.sales.visit.checkin_far", "staff.sales.visit.checkin_skipped",
    "staff.sales.visit.stock_title", "staff.sales.visit.stock_hint",
    "staff.sales.visit.stock_row", "staff.sales.visit.stock_qty_prompt",
    "staff.sales.visit.stock_empties_prompt",
    "staff.sales.visit.sold_out", "staff.sales.visit.low",
    "staff.sales.visit.stock_done", "staff.sales.visit.stock_back",
    # The shelf screen's two dead-end shapes (an empty catalogue, a failed
    # fetch) and the retry that gets out of the second one.
    "staff.sales.visit.stock_no_products", "staff.sales.visit.stock_retry",
    "staff.sales.visit.order_title", "staff.sales.visit.order_suggested_line",
    "staff.sales.visit.order_last_line", "staff.sales.visit.order_none_line",
    "staff.sales.visit.order_suggested", "staff.sales.visit.order_edit",
    "staff.sales.visit.order_last", "staff.sales.visit.no_order",
    "staff.sales.visit.close_outcome_prompt",
    "staff.sales.visit.outcome.no_order", "staff.sales.visit.outcome.closed",
    "staff.sales.visit.outcome.owner_absent", "staff.sales.visit.outcome.refused",
    "staff.error.api.not_found",
    # The visit loop's second half
    # (tests/staff_bot/test_sales_visit_orders_journey.py): the order options,
    # the money rails, the delivery day, the confirm screen and the close.
    # The four visit FAMILIES are listed member by member on purpose — the
    # table is built from literals, and a family loop here would hide exactly
    # the gap `_add_dynamic_family_keys` exists to catch.
    "staff.sales.visit.order_suggested", "staff.sales.visit.order_edit",
    "staff.sales.visit.order_last", "staff.sales.visit.no_order",
    "staff.sales.visit.no_order_reason_prompt", "staff.sales.visit.order_edit_title",
    "staff.sales.visit.order_done", "staff.sales.visit.payment_prompt",
    "staff.sales.visit.day_prompt", "staff.sales.visit.day_tomorrow",
    "staff.sales.visit.day_today", "staff.sales.visit.day_pick",
    "staff.sales.visit.day_pick_prompt", "staff.sales.visit.day_invalid",
    "staff.sales.visit.notes_prompt", "staff.sales.visit.confirm_title",
    "staff.sales.visit.confirm", "staff.sales.visit.back",
    "staff.sales.visit.order_created", "staff.sales.visit.order_pending_confirmation",
    "staff.sales.visit.order_confirmed", "staff.sales.visit.order_auto_confirmed",
    "staff.sales.visit.close_notes_prompt", "staff.sales.visit.next_visit_prompt",
    "staff.sales.visit.closed",
    "staff.sales.visit.outcome.no_order", "staff.sales.visit.outcome.closed",
    "staff.sales.visit.outcome.owner_absent", "staff.sales.visit.outcome.refused",
    "staff.sales.visit.reason.sufficient_stock", "staff.sales.visit.reason.cash_issue",
    "staff.sales.visit.reason.price", "staff.sales.visit.reason.competitor",
    "staff.sales.visit.reason.other",
    "staff.sales.visit.pay.cash", "staff.sales.visit.pay.business_account",
    "staff.sales.visit.next.3", "staff.sales.visit.next.7", "staff.sales.visit.next.14",
    "staff.sales.visit.next.30", "staff.sales.visit.next.none",
    # `card.resume_visit` is already in the tuple from Task 10 (the card
    # draws it); `order_maybe_landed` is new here — the button on the
    # "it may have landed" screen reuses the card's label.
    "staff.sales.visit.order_maybe_landed",
    "staff.sales.error.outlet_not_active", "staff.sales.error.order_exists",
    "staff.sales.error.outcome_required",
    # The four visit refusals this table used to omit (finding C17): they are
    # mapped in `handlers/base.py` and seeded, but a key missing HERE renders
    # `humanise_key` — a plausible English sentence — so a journey asserting
    # the mapped copy fails for a reason that has nothing to do with the bot.
    "staff.sales.error.visit_open", "staff.sales.error.visit_not_open",
    "staff.sales.error.visit_step", "staff.sales.error.stock_qty",
    # The fix wave: the per-line order floor, the min-quantity refusal, and
    # the visit's own timeout copy (the shared one says "nothing was saved",
    # which is false for a visit the server is still holding open).
    "staff.sales.visit.order_min", "staff.sales.error.order_min_qty",
    "staff.sales.visit.timeout",
    # The backlog fixes (Task 7 renders them, Task 6 seeds them): the
    # rail-less outlet's own line, the labelled delivery window and the two
    # new coded refusals.
    "staff.sales.visit.no_rails", "staff.sales.visit.window_line",
    "staff.sales.error.order_qty", "staff.sales.error.stock_product",
    # Phase 2b, Task 6: the Nearby conversation
    # (tests/staff_bot/test_sales_nearby_journey.py builds its table from this
    # tuple). A key left out renders `humanise_key` — a plausible English word
    # — instead of failing, so every one the screens draw is listed here.
    "staff.sales.hub.nearby", "staff.sales.nearby.title",
    "staff.sales.nearby.pin_prompt", "staff.sales.nearby.pin_button",
    "staff.sales.nearby.found", "staff.sales.nearby.distance",
    "staff.sales.nearby.again",
    # Not new — `SALES_NEARBY_PIN_REQUIRED` maps onto the key the outlet and
    # activation pin refusals already use — but absent from this table it
    # rendered as `humanise_key` ("Pin required"), which is close enough to
    # the real copy to read as a pass while the mapping was broken.
    "staff.sales.error.pin_required",
    # Phase 2b, Task 7: the try-out conversation
    # (tests/staff_bot/test_sales_tryout_journey.py imports this table). The
    # three REUSED keys (`new.skip`, `visit.back`, `error.phone_required`) are
    # already above; only the new family and its one error row go here.
    "staff.sales.tryout.button", "staff.sales.tryout.choose_products",
    "staff.sales.tryout.quantity_prompt", "staff.sales.tryout.done",
    "staff.sales.tryout.notes_prompt", "staff.sales.tryout.confirm_title",
    "staff.sales.tryout.confirm", "staff.sales.tryout.created",
    "staff.sales.tryout.handoff_queued", "staff.sales.tryout.no_products",
    "staff.sales.tryout.no_phone", "staff.sales.tryout.open_outlet",
    "staff.sales.error.tryout_items",
    # Phase 2b, Task 8: Photo at every step. The harness serves
    # translations from this table and falls back to `humanise_key`, which
    # never returns falsy — a key left out here renders a plausible English
    # word instead of failing.
    "staff.sales.visit.photo_kind_prompt", "staff.sales.visit.photo_kind.storefront",
    "staff.sales.visit.photo_kind.shelf", "staff.sales.visit.photo_kind.other",
    "staff.sales.visit.photo_saved", "staff.sales.visit.photo_duplicate",
    "staff.sales.visit.photo_failed", "staff.sales.visit.photo_forwarded",
    "staff.sales.error.photo_invalid",
    # Phase 3, Task 7: the agent's own KPI card (tests/staff_bot/
    # test_sales_stats_journey.py builds its table from this tuple). Written
    # out member by member rather than looped: the harness falls back to
    # `humanise_key`, which never returns falsy, so a key left out renders a
    # plausible English word and the journey passes against copy production
    # does not ship.
    "staff.sales.stats.button", "staff.sales.stats.title", "staff.sales.stats.na",
    "staff.sales.stats.period_today", "staff.sales.stats.period_week",
    "staff.sales.stats.period_month",
    "staff.sales.stats.section_visits", "staff.sales.stats.section_outlets",
    "staff.sales.stats.section_orders", "staff.sales.stats.section_discipline",
    "staff.sales.stats.metric_planned_visits", "staff.sales.stats.metric_completed_visits",
    "staff.sales.stats.metric_plan_vs_fact_pct", "staff.sales.stats.metric_unplanned_visits",
    "staff.sales.stats.metric_visits_per_day", "staff.sales.stats.metric_strike_rate_pct",
    "staff.sales.stats.metric_assigned_outlets", "staff.sales.stats.metric_active_outlets",
    "staff.sales.stats.metric_active_share_pct",
    "staff.sales.stats.metric_new_outlets_registered",
    "staff.sales.stats.metric_new_outlets_activated", "staff.sales.stats.metric_orders_placed",
    "staff.sales.stats.metric_orders_delivered_paid",
    "staff.sales.stats.metric_bottles_delivered_paid",
    "staff.sales.stats.metric_revenue_delivered_paid",
    "staff.sales.stats.metric_agent_orders_cancelled",
    "staff.sales.stats.metric_suggested_vs_accepted_pct",
    "staff.sales.stats.metric_out_of_range_checkins",
    "staff.sales.stats.metric_skipped_checkins", "staff.sales.stats.metric_avg_visit_minutes",
    # D25 branch outlets: the account line and the two labels that say WHOSE
    # figure the card is printing. The tests assert their RENDERED lines whole
    # and literally (R31), never the template.
    "staff.sales.card.account", "staff.sales.card.receivable_account",
    "staff.sales.card.bottles_branch", "staff.sales.new_outlet.sibling",
)


def _curated(key, language="en"):
    value = _SEED._curated_value(key, language)
    assert value, f"{key} has no curated {language} value"
    return value


def _rendered(key, language="en", **values):
    """What an interpolating key really renders to, through production's renderer.

    `_curated` returns the TEMPLATE; six keys in this module's copy carry a
    placeholder (controller ruling 19), and asserting the template against a
    rendered screen would either fail or — worse — pass vacuously when the
    placeholder is dropped. `shared.i18n_rendering.render_translation` is the
    one `staff_bot/i18n.py::get` calls, so this is the same string the agent
    sees, including its fallback behaviour for copy whose placeholders do not
    match what the call site passes.
    """
    from shared.i18n_rendering import render_translation

    return render_translation(key, _curated(key, language), kwargs=values)


def _table():
    return {(lang, key): _curated(key, lang) for key in KEYS for lang in ("en", "uz", "ru")}


def _outlet(**over):
    """`serialize_outlet` — what POST /request-activation answers with.

    Deliberately carries NO `open_receivable` / `bottle_balance` /
    `last_orders`: the backend's POST really does answer with this shape, and
    the card must therefore print no money line at all rather than "Owes: 0".
    """
    base = {
        "id": 5, "name": "Bahor market", "outlet_type": "grocery_store", "stage": "prospect", "class": "B",
        "latitude": 41.3111, "longitude": 69.2797, "address_text": "Chilonzor 5", "district": "chilanzar",
        "notes": "Corner shop", "user_id": 41,
        "contacts": [{"id": 1, "name": "Olim aka", "phone": "+998901112266", "role": "owner",
                      "is_primary": True, "presence_window": None}],
    }
    base.update(over)
    return base


def _agent_row(**over):
    """`serialize_outlet_agent_row` — one row of the agent's own list scopes.

    `serialize_outlet` plus the three fields Task 6 adds: the backend's OWN
    lateness arithmetic (`overdue_days`) and whether this agent already has a
    visit open at this outlet. The bot never subtracts dates to decide either.
    """
    base = _outlet(overdue_days=None, has_open_visit=False, open_visit_id=None)
    base.update(over)
    return base


def _card(**over):
    """`OutletService.card` — `serialize_outlet` plus the money AND the visit
    intelligence only the GET carries: the last completed visit, the rule
    figures (`rate_per_day`, `suggested_now`), the due date with the backend's
    own `overdue_days`, and this agent's open visit for THIS outlet."""
    base = _outlet(
        open_receivable=250000.0,
        bottle_balance=3.0,
        last_orders=[{"order_number": "AE-101", "status": "delivered", "total_amount": 120000.0}],
        last_visit={"id": 71, "outcome": "order_placed", "notes": "Owner wants a fridge poster",
                    "ended_at": "2026-08-30T11:20:00+00:00"},
        next_visit_due_at="2026-09-06T04:00:00+00:00",
        overdue_days=2,
        rate_per_day=1.9,
        suggested_now=20,
        open_visit_id=None,
        # D25: what `OutletService.card` publishes about the ACCOUNT. The
        # default is a one-outlet account -- `is_branch` False, `branch_count`
        # counting this outlet only -- so every card test written before branch
        # outlets keeps the plain labels, and only a test that flips `is_branch`
        # gets the branch line. The bot gates on `is_branch`, never on the count.
        account_name="Bahor Savdo",
        branch_count=1,
        is_branch=False,
        open_receivable_scope="account",
        # R25's fifth published field. The agent's card renderer never reads it (only the
        # operator's review row does), but the fixture mirrors the real payload: the default
        # is an account this outlet already belongs to, so there is nothing left to attach.
        account_candidate=None,
    )
    base.update(over)
    return base


async def _agent(monkeypatch, language="en"):
    row = {"id": 55, "telegram_id": str(DEFAULT_DRIVER_TELEGRAM_ID), "first_name": "Sardor", "last_name": "Agent",
           "phone": "+998901234577", "preferred_language": language, "role": "sales_agent", "status": "active",
           "staff_roles": json.dumps(["sales_agent"]), "staff_bot_state": "{}"}
    harness = await build_staff_harness(monkeypatch, translations=_table(), database=FakeStaffDatabase(staff_user=row))
    harness.backend.route("POST", LOGIN, lambda _c: {
        "access_token": "staff-access-token", "refresh_token": "staff-refresh-token", "expires_in": 3600,
        "user": {"id": 55, "first_name": "Sardor", "last_name": "Agent", "phone": "+998901234577",
                 "preferred_language": language, "staff_roles": ["sales_agent"], "delivery_person_id": None,
                 "sales_agent_profile_id": 3},
    })
    ops = harness.updates()
    await harness.send(ops.command("start"))
    labels = harness.telegram.shown[-1].button_labels()
    harness.telegram.reset()
    harness.backend.calls.clear()
    return harness, ops, labels


def _label(labels, key):
    value = _curated(key)
    hits = [label for label in labels if label.strip().endswith(value)]
    assert len(hits) == 1, (value, labels)
    return hits[0]


def _prospect_card_response():
    """What GET /outlets/<id> really answers for an unlinked prospect: the money keys are PRESENT
    and NULL. `OutletService.card` decides applicability server-side, so the bot has one field to
    read instead of re-deriving the rule from `user_id`.

    The visit fields are NULL for the same reason and by the same rule: a
    prospect has never been visited, has no consumption history to rate, and
    cannot be ordered for — so there is nothing to print, not a zero."""
    return {"outlet": _card(user_id=None, open_receivable=None, bottle_balance=None, last_orders=[],
                            last_visit=None, next_visit_due_at=None, overdue_days=None,
                            rate_per_day=None, suggested_now=None, open_visit_id=None)}


def _calls(harness, method, endpoint):
    return [c for c in harness.backend.calls if c.method == method and c.endpoint == endpoint]


def _alerts(harness):
    return [c.params.get("text", "") for c in harness.telegram.of("answerCallbackQuery") if c.params.get("text")]


async def test_menu_hub_list_card_and_activation(monkeypatch):
    harness, ops, labels = await _agent(monkeypatch)
    assert _curated("staff.menu.my_outlets") in " ".join(labels)
    assert not any(_curated("staff.menu.new_orders", "en") in label for label in labels)

    harness.backend.route("GET", OUTLETS, lambda c: {"items": [_outlet()]})
    harness.backend.route("GET", f"{OUTLETS}/5", lambda c: {"outlet": _card()})
    harness.backend.route("POST", f"{OUTLETS}/5/request-activation",
                          lambda c: {"outlet": _outlet(stage="activation_requested")})

    await harness.send(ops.text(_label(labels, "staff.menu.my_outlets")))
    hub = harness.telegram.last_shown()
    assert _curated("staff.sales.hub.title") in hub.text
    assert _curated("staff.sales.hub.prospects") in " ".join(hub.button_labels())

    await harness.send(ops.tap("staff_sales_list_prospects"))
    listing = harness.telegram.last_shown()
    assert "Bahor market" in " ".join(listing.button_labels())
    assert _calls(harness, "GET", OUTLETS)[-1].params == {"scope": "prospects", "page": 1, "per_page": 20}

    await harness.send(ops.tap("staff_sales_outlet_5"))
    card = harness.telegram.last_shown()
    assert "Bahor market" in card.text and _curated("staff.sales.stage.prospect") in card.text
    assert "+998901112266" in card.text and "Chilonzor 5" in card.text
    # Money through the currency SSOT (never a hardcoded "UZS") and the order
    # status through its translation family (never the raw backend enum).
    assert f"250,000 {_curated('staff.currency.uzs')}" in card.text
    assert f"120,000 {_curated('staff.currency.uzs')}" in card.text
    assert f"({_curated('staff.order.status.delivered')})" in card.text
    assert "(delivered)" not in card.text
    assert _curated("staff.sales.card.request_activation") in " ".join(card.button_labels())

    await harness.send(ops.tap("staff_sales_activate_5"))
    assert [c.data for c in _calls(harness, "POST", f"{OUTLETS}/5/request-activation")] == [{}]
    after = harness.telegram.last_shown()
    assert _curated("staff.sales.stage.activation_requested") in after.text
    # Re-rendered from the POST's `serialize_outlet`, which has a `user_id` but
    # no money fields: the receivable line must be ABSENT, not "Owes: 0".
    assert _curated("staff.sales.card.receivable") not in after.text
    assert _curated("staff.sales.card.bottles") not in after.text
    assert _curated("staff.sales.card.request_activation") not in " ".join(after.button_labels())


async def test_backend_error_surfaces_as_alert_not_crash(monkeypatch):
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("GET", f"{OUTLETS}/5", lambda c: {"outlet": _outlet(contacts=[])})
    harness.backend.route("POST", f"{OUTLETS}/5/request-activation",
                          lambda c: staff_backend_failure("phone required", 400, "SALES_ACTIVATION_PHONE_REQUIRED"))
    await harness.send(ops.tap("staff_sales_outlet_5"))
    await harness.send(ops.tap("staff_sales_activate_5"))
    # `BaseHandler._handle_api_error` prefixes the resolved copy with "❌ " —
    # asserted whole rather than by substring so this stays a payload check.
    assert f"❌ {_curated('staff.sales.error.phone_required')}" in _alerts(harness)


async def test_a_malformed_list_callback_is_answered_not_raised(monkeypatch):
    r"""The registered pattern is `^staff_sales_list_\w+$`, so the scope and page
    are validated in the handler instead. A tap that is neither must be
    ANSWERED — an exception raised while parsing happens outside the handler's
    try block, which leaves the agent's button spinning — and it must never
    reach the backend with a scope nobody asked for.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("GET", OUTLETS, lambda c: {"items": []})
    await harness.send(ops.tap("staff_sales_list_bogus"))
    await harness.send(ops.tap("staff_sales_list_all_x"))
    assert not _calls(harness, "GET", OUTLETS)
    assert len(harness.telegram.of("answerCallbackQuery")) == 2


async def test_a_prospect_card_shows_no_money_lines(monkeypatch):
    """A prospect opened from the "Prospects" list — the hub's primary path.

    `OutletService.card` sets `open_receivable` to 0.00 and `bottle_balance` to
    0.0 for EVERY outlet, account or not, so a presence-only gate printed
    "Owes: 0" and "Bottles at outlet: 0" on a store that has never ordered and
    has no address row yet. Those figures are not applicable, not zero.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("GET", f"{OUTLETS}/5", lambda c: _prospect_card_response())
    await harness.send(ops.tap("staff_sales_outlet_5"))
    card = harness.telegram.last_shown()
    assert "Bahor market" in card.text and _curated("staff.sales.card.stage") in card.text
    assert _curated("staff.sales.card.receivable") not in card.text
    assert _curated("staff.sales.card.bottles") not in card.text


async def test_a_half_activated_outlet_shows_its_debt_but_not_a_bottle_count(monkeypatch):
    """`OutletService.approve` is resumable: it commits the customer account in one step and the
    address row in the next, so an outlet whose activation stopped in between has a wallet and no
    bottle ledger at all. The two figures are answered independently, and each line is printed
    only when its OWN field is applicable -- otherwise "Bottles at outlet: 0" claims an empty
    crate for a place that has no crate.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("GET", f"{OUTLETS}/5", lambda c: {
        "outlet": _card(user_id=41, open_receivable=0.0, bottle_balance=None, last_orders=[])
    })
    await harness.send(ops.tap("staff_sales_outlet_5"))
    card = harness.telegram.last_shown()
    assert _curated("staff.sales.card.receivable") in card.text
    assert _curated("staff.sales.card.bottles") not in card.text


async def test_a_branch_card_names_its_account_and_labels_the_money_account_wide(monkeypatch):
    """D25 rules 5-7: money is the ACCOUNT's, bottles are the BRANCH's.

    A chain's second outlet shows the same receivable as its sibling -- the
    figure is the account's wallet, not this shop's -- so the card has to say
    so, or the agent reads one branch's debt as the chain's and collects
    twice. The bottle count is already per address and is labelled as the
    branch's for the same reason. Branch mode is the backend's `is_branch`;
    the bot does no counting and no comparing of its own.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    # 4 branches against 3 bottles: every figure on this card is distinct, so a
    # renderer that printed one in the other's line would fail here.
    harness.backend.route("GET", f"{OUTLETS}/5", lambda c: {"outlet": _card(
        id=5, name="Bahor market, Chilonzor 5", account_name="Bahor Savdo MChJ",
        branch_count=4, is_branch=True,
    )})

    await harness.send(ops.tap("staff_sales_outlet_5"))

    card = harness.telegram.last_shown()
    lines = card.text.split("\n")
    # R31 pins the RENDERED lines, glyph included, so they are asserted whole
    # and literally. An expectation composed from the seed (`f"🏢 {_rendered(...)}"`)
    # would double its glyph along with a seed row that grew one and still
    # pass -- the doubled-glyph bug R31 exists to stop.
    assert "🏢 Bahor Savdo MChJ · 4 branches" in lines
    # Directly under the address: the address is what makes it a branch.
    assert lines[lines.index("📍 Address: Chilonzor 5") + 1] == "🏢 Bahor Savdo MChJ · 4 branches"
    # One label per figure: the account-wide number is never ALSO printed
    # under the plain label, which is what would make the qualifier decorative.
    assert [line for line in lines if line.startswith("💰")] == ["💰 Owes (account): 250,000 UZS"]
    assert [line for line in lines if line.startswith("🧴")] == ["🧴 Bottles at this branch: 3"]


async def test_a_single_outlet_account_keeps_the_plain_labels(monkeypatch):
    """`is_branch: false` is not a branch: one shop, one account, and exactly
    the card that has always shipped.

    The fixture's default -- `is_branch` False, `branch_count` 1 -- and the
    account name never leaks onto a card that has no second branch to
    disambiguate from. The shapes where the flag and the count could be
    confused (a count of two with the flag off, a payload with no `is_branch`
    key at all) are pinned by
    `test_the_branch_labels_follow_the_published_fields_not_a_count`.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("GET", f"{OUTLETS}/5", lambda c: {"outlet": _card()})

    await harness.send(ops.tap("staff_sales_outlet_5"))

    card = harness.telegram.last_shown()
    lines = card.text.split("\n")
    assert [line for line in lines if line.startswith("💰")] == ["💰 Owes: 250,000 UZS"]
    assert [line for line in lines if line.startswith("🧴")] == ["🧴 Bottles at outlet: 3"]
    assert not [line for line in lines if line.startswith("🏢")]
    assert "Bahor Savdo" not in card.text


@pytest.mark.parametrize("over, drop, money_line, bottles_line, account_line", [
    # The flag is off and the count says two. The count is only the number the
    # account line prints; reading it as the predicate is the re-derivation R25
    # forbids.
    pytest.param(
        {"account_name": "Bahor Savdo MChJ", "branch_count": 2, "is_branch": False}, (),
        "💰 Owes: 250,000 UZS", "🧴 Bottles at outlet: 3", None,
        id="is_branch_false_beats_a_count_of_two",
    ),
    # A branch whose receivable the backend does NOT call account-wide: the
    # money label is the conjunction R10 rules, so it stays plain while the
    # bottle label (branch mode alone) and the account line still move.
    pytest.param(
        {"account_name": "Bahor Savdo MChJ", "branch_count": 4, "is_branch": True,
         "open_receivable_scope": "address"}, (),
        "💰 Owes: 250,000 UZS", "🧴 Bottles at this branch: 3", "🏢 Bahor Savdo MChJ · 4 branches",
        id="a_branch_whose_money_is_not_account_wide",
    ),
    # The account name is what a customer typed as their company, and the card
    # is sent with parse_mode=HTML: one raw '<' makes Telegram refuse the whole
    # card, so the agent standing in the shop sees nothing at all.
    pytest.param(
        {"account_name": "Bahor <Savdo> & Co", "branch_count": 4, "is_branch": True}, (),
        "💰 Owes (account): 250,000 UZS", "🧴 Bottles at this branch: 3",
        "🏢 Bahor &lt;Savdo&gt; &amp; Co · 4 branches",
        id="the_account_name_is_html_escaped",
    ),
    # No `is_branch` key at all -- the plain `serialize_outlet` shape the
    # operator's review card renders -- even with every account field and both
    # figures present: absent is not a branch.
    pytest.param(
        {"account_name": "Bahor Savdo MChJ", "branch_count": 4}, ("is_branch",),
        "💰 Owes: 250,000 UZS", "🧴 Bottles at outlet: 3", None,
        id="no_is_branch_key_at_all",
    ),
])
async def test_the_branch_labels_follow_the_published_fields_not_a_count(
    monkeypatch, over, drop, money_line, bottles_line, account_line,
):
    """Branch mode is `is_branch`, and the account-wide money label is
    `is_branch AND open_receivable_scope == 'account'` (R10/R25) -- the
    backend's published answers, read and never re-derived from
    `branch_count` or `account_name`.
    """
    outlet = _card(**over)
    for key in drop:
        del outlet[key]
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("GET", f"{OUTLETS}/5", lambda c: {"outlet": outlet})

    await harness.send(ops.tap("staff_sales_outlet_5"))

    lines = harness.telegram.last_shown().text.split("\n")
    assert [line for line in lines if line.startswith("💰")] == [money_line]
    assert [line for line in lines if line.startswith("🧴")] == [bottles_line]
    assert [line for line in lines if line.startswith("🏢")] == ([account_line] if account_line else [])


async def test_the_card_speaks_the_agents_language_for_money_and_status(monkeypatch):
    """The English card cannot tell a hardcoded "UZS" from the currency SSOT —
    `staff.currency.uzs` IS "UZS" in en. In Russian it is "сум", so this is the
    run that proves `format_currency` (and the order-status family) is what
    renders the numbers a sales agent reads.
    """
    harness, ops, _labels = await _agent(monkeypatch, language="ru")
    harness.backend.route("GET", f"{OUTLETS}/5", lambda c: {"outlet": _card()})
    await harness.send(ops.tap("staff_sales_outlet_5"))
    card = harness.telegram.last_shown()
    assert f"250,000 {_curated('staff.currency.uzs', 'ru')}" in card.text
    assert f"({_curated('staff.order.status.delivered', 'ru')})" in card.text
    assert "UZS" not in card.text


async def test_help_for_a_sales_agent_renders_its_markup(monkeypatch):
    """`staff.help.sales_agent` is the only help row carrying <b> tags.

    `/help` builds its text by hand in `StaffBot._help_handler` rather than
    through `HelpHandler.show_help`, so the two have to agree about parse_mode.
    Without it the agent reads the tags instead of bold text.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    await harness.send(ops.command("help"))
    shown = harness.telegram.last_shown()
    assert shown.params.get("parse_mode") == "HTML"
    assert _curated("staff.help.sales_agent") in shown.text


async def test_driver_cannot_open_the_sales_hub(monkeypatch):
    row = FakeStaffDatabase().staff_user
    harness = await build_staff_harness(monkeypatch, translations=_table(), database=FakeStaffDatabase(staff_user=row))
    harness.backend.route("POST", LOGIN, lambda _c: {
        "access_token": "a", "refresh_token": "r", "expires_in": 3600,
        "user": {"id": 55, "staff_roles": ["delivery_driver"], "delivery_person_id": 7, "preferred_language": "en"},
    })
    ops = harness.updates()
    await harness.send(ops.command("start"))
    harness.telegram.reset()
    await harness.send(ops.tap("staff_sales_hub"))
    assert _curated("staff.unauthorized") in _alerts(harness)


# ---------------------------------------------------------------------------
# Phase 2a, Task 10: the due list and the visit block on the outlet card.
# ---------------------------------------------------------------------------


async def test_the_due_list_leads_the_hub_and_says_how_late_each_call_is(monkeypatch):
    """The agent's actual job queue, and the first thing the hub offers.

    `scope=due` is the backend's filter AND its ordering
    (`OutletService.list_for_agent`), and `overdue_days` is the backend's own
    arithmetic (`serialize_outlet_agent_row`). The bot asks for the scope and
    prints the number; it never decides which outlets are due, nor how late.
    A call that is due today is `overdue_days == 0`, not "0 days late", so it
    carries no suffix at all.
    """
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("GET", OUTLETS, lambda c: {"items": [
        _agent_row(id=5, name="Bahor market", stage="active", overdue_days=3),
        _agent_row(id=6, name="Nur savdo", stage="at_risk", overdue_days=0),
    ]})

    await harness.send(ops.text(_label(labels, "staff.menu.my_outlets")))
    hub = harness.telegram.last_shown()
    assert hub.callback_data()[0] == "staff_sales_list_due"
    assert hub.button_labels()[0].strip().endswith(_curated("staff.sales.hub.due"))

    await harness.send(ops.tap("staff_sales_list_due"))
    assert _calls(harness, "GET", OUTLETS)[-1].params == {"scope": "due", "page": 1, "per_page": 20}
    listing = harness.telegram.last_shown()
    assert _curated("staff.sales.list.title_due") in listing.text
    late, on_time = listing.button_labels()[0], listing.button_labels()[1]
    assert "Bahor market" in late
    assert late.endswith(_rendered("staff.sales.list.overdue_suffix", days=3))
    assert "Nur savdo" in on_time
    assert _rendered("staff.sales.list.overdue_suffix", days=0) not in on_time
    assert listing.callback_data()[:2] == ["staff_sales_outlet_5", "staff_sales_outlet_6"]


async def test_a_long_name_never_pushes_the_overdue_suffix_off_the_button(monkeypatch):
    """The lateness is WHY the row is in this list; the name is what gives way.

    The label was composed and then sliced, so a shop with a long name kept
    every character of its name and lost "+9d" — the only thing on the button
    that says this call has slipped.
    """
    harness, ops, labels = await _agent(monkeypatch)
    long_name = "Bahor market va oziq-ovqat do'koni Chilonzor 5-mavze filiali"
    harness.backend.route("GET", OUTLETS, lambda c: {"items": [
        _agent_row(id=5, name=long_name, stage="active", overdue_days=9),
    ]})

    await harness.send(ops.text(_label(labels, "staff.menu.my_outlets")))
    await harness.send(ops.tap("staff_sales_list_due"))

    label = harness.telegram.last_shown().button_labels()[0]
    assert label.endswith(_rendered("staff.sales.list.overdue_suffix", days=9))
    assert label.startswith("\u2705 Bahor market")
    assert len(label) <= 60


async def test_a_store_that_is_not_moving_prints_zero_rather_than_nothing(monkeypatch):
    """0.0 and 0 are the rule's ANSWER; None is the absence of one.

    `rate_per_day == 0.0` says "this store is not selling", which is the most
    actionable line on the card, and `suggested_now == 0` says "do not load
    the van for it". Gating either on truthiness would print nothing at all —
    the same screen a never-visited prospect gets.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("GET", f"{OUTLETS}/5", lambda c: {
        "outlet": _card(stage="at_risk", rate_per_day=0.0, suggested_now=0)})

    await harness.send(ops.tap("staff_sales_outlet_5"))

    card = harness.telegram.last_shown()
    assert f"{_curated('staff.sales.card.rate')}: 0.0" in card.text
    assert f"{_curated('staff.sales.card.suggested')}: 0" in card.text


async def test_an_active_outlet_offers_starting_a_visit_and_shows_why(monkeypatch):
    """The card is the agent's brief before they walk in: when they were last
    here and how it went, how late this call is, how fast the store sells, and
    what to propose. Every one of those is a backend field (D15) — the outcome
    goes through its translation family rather than shipping the raw enum,
    which is this project's recurring English-leak class.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("GET", f"{OUTLETS}/5", lambda c: {"outlet": _card(stage="active")})
    await harness.send(ops.tap("staff_sales_outlet_5"))
    card = harness.telegram.last_shown()

    assert "staff_sales_visit_start_5" in card.callback_data()
    assert "staff_sales_visit_resume" not in card.callback_data()
    assert _curated("staff.sales.card.start_visit") in " ".join(card.button_labels())

    assert f"{_curated('staff.sales.card.last_visit')}: 30.08.2026" in card.text
    assert _curated("staff.sales.visit.outcome.order_placed") in card.text
    assert "order_placed" not in card.text
    assert "Owner wants a fridge poster" in card.text
    assert f"{_curated('staff.sales.card.next_due')}: 06.09.2026" in card.text
    assert _rendered("staff.sales.card.overdue", days=2) in card.text
    assert f"{_curated('staff.sales.card.rate')}: 1.9" in card.text
    assert f"{_curated('staff.sales.card.suggested')}: 20" in card.text


async def test_an_open_visit_turns_the_card_button_into_resume(monkeypatch):
    """One open visit per agent (`uq_visits_one_open_per_agent`).

    Offering "Start" over an open visit posts straight into a 409 the agent
    can do nothing with, so the card offers the tap they actually want — and
    only that one. Which visit is open at THIS outlet is a backend field
    (`open_visit_id`), never a guess from a cached user_data flag.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("GET", f"{OUTLETS}/5",
                          lambda c: {"outlet": _card(stage="active", open_visit_id=88)})
    await harness.send(ops.tap("staff_sales_outlet_5"))
    card = harness.telegram.last_shown()
    assert "staff_sales_visit_resume" in card.callback_data()
    assert not [d for d in card.callback_data() if d.startswith("staff_sales_visit_start_")]
    assert _curated("staff.sales.card.resume_visit") in " ".join(card.button_labels())
    assert _curated("staff.sales.card.start_visit") not in " ".join(card.button_labels())


async def test_a_prospect_card_offers_no_visit_and_no_visit_lines(monkeypatch):
    """A prospect has no customer account and no address row, so the visit loop
    has nothing to write an order against — `VisitService.place_order` refuses
    it with `SALES_OUTLET_NOT_ACTIVE`. The card offers activation instead, and
    prints none of the visit lines: those figures are not applicable, not zero.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("GET", f"{OUTLETS}/5", lambda c: _prospect_card_response())
    await harness.send(ops.tap("staff_sales_outlet_5"))
    card = harness.telegram.last_shown()
    assert not [d for d in card.callback_data() if d.startswith("staff_sales_visit_")]
    assert "staff_sales_activate_5" in card.callback_data()
    assert _curated("staff.sales.card.last_visit") not in card.text
    assert _curated("staff.sales.card.next_due") not in card.text
    assert _curated("staff.sales.card.rate") not in card.text
    assert _curated("staff.sales.card.suggested") not in card.text


async def test_the_visit_lines_speak_the_agents_language(monkeypatch):
    """In English a missing translation row is invisible: `humanise_key` turns
    `staff.sales.card.rate` into "Rate", which reads exactly like copy. In
    Russian it does not, so this is the run that proves the seed really ships
    these keys and the outcome family — and that no raw backend enum reaches
    a Russian-speaking agent.
    """
    harness, ops, _labels = await _agent(monkeypatch, language="ru")
    harness.backend.route("GET", f"{OUTLETS}/5", lambda c: {"outlet": _card(stage="active")})
    await harness.send(ops.tap("staff_sales_outlet_5"))
    card = harness.telegram.last_shown()
    buttons = " ".join(card.button_labels())
    assert f"{_curated('staff.sales.card.rate', 'ru')}: 1.9" in card.text
    assert f"{_curated('staff.sales.card.suggested', 'ru')}: 20" in card.text
    assert _curated("staff.sales.visit.outcome.order_placed", "ru") in card.text
    assert _rendered("staff.sales.card.overdue", "ru", days=2) in card.text
    assert _curated("staff.sales.card.start_visit", "ru") in buttons
    assert "order_placed" not in card.text
    assert _curated("staff.sales.card.rate", "en") not in card.text
    assert _curated("staff.sales.card.suggested", "en") not in card.text
    assert _curated("staff.sales.card.start_visit", "en") not in buttons
