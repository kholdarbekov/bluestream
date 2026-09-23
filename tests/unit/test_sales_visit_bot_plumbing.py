"""The staff bot's visit-loop plumbing: buttons, error map, translation families, flow key.

This file is the SPECIFICATION Tasks 11 and 12 wire against, which is why the
callback patterns below are written out rather than scraped from `bot.py`: a
scrape would agree happily with a `bot.py` that registered nothing at all.
Until Task 12 registers the last of them,
`tests/unit/test_staff_bot_routing_regressions.py` is RED by design (plan
ruling 16) -- the literals exist and no pattern matches them yet. THIS test is
what keeps the two halves honest in the meantime.

Three of the checks here exist because a static guard elsewhere cannot see
what they see:

  * the grid prefixes must be literal. `_materialize_literal` in the routing
    guard rewrites every `{...}` to `1`, so a shared helper emitting
    `f"staff_sales_v_{prefix}_{product_id}_{n}"` would materialise as
    `staff_sales_v_1_1_1` -- a literal that would demand a
    `^staff_sales_v_\\d+_\\d+_\\d+$` handler which must never exist.
  * the seed script CANNOT import `staff_bot` (it runs inside the business_app
    container, which has no `staff_bot/` tree -- see `_add_curated_keys`'s
    docstring), so its family tuples are a hand-written copy of the keyboard
    constants. The copy is checked here instead of being wished away.
  * `staff.sales.visit.*` keys are drawn with f-strings, so the literal
    extractor `/health` relies on cannot see them at all.
"""

import importlib.util
import re
from pathlib import Path

import pytest

from staff_bot.handlers.base import BaseHandler
from staff_bot.i18n import i18n
from staff_bot.keyboards.sales import (
    CLOSE_OUTCOMES,
    EMPTIES_CHOICES,
    NEXT_VISIT_CHOICES,
    NO_ORDER_REASONS,
    ORDER_QTY_CHOICES,
    PAY_METHODS,
    PHOTO_KINDS,
    STOCK_QTY_CHOICES,
    VISITABLE_STAGES,
    VISIT_OUTCOMES,
    SalesKeyboards,
)
from staff_bot.utils.flow_state import PENDING_FLOW_USER_DATA_KEYS
from shared.staff_constants import SALES_EVENTS

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
LANGUAGES = ("en", "uz", "ru")

# The patterns Tasks 11 and 12 register in staff_bot/bot.py, verbatim from the
# plan's shared-interfaces contract. Shapes are restricted to `^literal$`,
# `^prefix_\d+$` and `^prefix_\w+$` because tests/staff_bot/
# test_staff_wiring_contract.py::_sample_matching cannot read anything richer.
REGISTERED_PATTERNS = (
    r"^staff_sales_v_skipcheckin$",
    r"^staff_sales_v_abandon$",
    r"^staff_sales_v_resume$",
    r"^staff_sales_v_stock_\d+$",
    r"^staff_sales_v_qty_\d+_\d+$",
    r"^staff_sales_v_empt_\d+_\d+$",
    r"^staff_sales_v_soldout_\d+$",
    r"^staff_sales_v_low_\d+$",
    r"^staff_sales_v_stockback$",
    r"^staff_sales_v_stockdone$",
    r"^staff_sales_v_stockretry$",
    r"^staff_sales_v_ordersugg$",
    r"^staff_sales_v_orderedit$",
    r"^staff_sales_v_orderlast$",
    r"^staff_sales_v_noorder$",
    r"^staff_sales_v_noreason_\w+$",
    r"^staff_sales_v_oqty_\d+$",
    r"^staff_sales_v_oqtyset_\d+_\d+$",
    r"^staff_sales_v_orderdone$",
    r"^staff_sales_v_pay_\w+$",
    r"^staff_sales_v_day_\w+$",
    r"^staff_sales_v_dayback$",
    r"^staff_sales_v_skip_\w+$",
    r"^staff_sales_v_orderconfirm$",
    r"^staff_sales_v_orderback$",
    r"^staff_sales_v_outcome_\w+$",
    r"^staff_sales_v_next_\w+$",
    # The kind picker. Registered in EVERY visit state, because a photo can
    # arrive on any step and the picker has to be answerable from the step
    # the agent is standing on.
    r"^staff_sales_v_photo_\w+$",
    # The card's entry point (Task 10 draws it too, Task 11 registers it).
    # `visit_resume` below draws it from INSIDE this module, for the
    # transport-ambiguous order POST of controller ruling 30.
    r"^staff_sales_visit_resume$",
)

# Controller ruling 32: the row crossing the stock-overview seam is a
# `serialize_stock_check` row merged with the two product flags. The keyboard
# reads six of the twelve keys; the rest ride along so one shape serves the
# overview, the quantity screen and the order screen.
STOCK_ROWS = [
    {"product_id": 11, "product_name": "Pure Water 19L", "on_hand_qty": 3, "empties_qty": 6,
     "is_sold_out": False, "is_low": True, "suggested_qty": 20, "accepted_qty": None,
     "rate_per_day": 1.857, "rate_source": "stock_checks",
     "is_returnable_bottle": True, "min_order_quantity": 2},
    {"product_id": 12, "product_name": "Pure Water 10L", "on_hand_qty": None, "empties_qty": None,
     "is_sold_out": True, "is_low": False, "suggested_qty": None, "accepted_qty": None,
     "rate_per_day": None, "rate_source": "none",
     "is_returnable_bottle": False, "min_order_quantity": 1},
]
ORDER_LINES = [
    {"product_id": 11, "product_name": "Pure Water 19L", "quantity": 8},
    {"product_id": 12, "product_name": "Pure Water 10L", "quantity": 2},
]
# `GET /outlets/<id>/payment-methods` verbatim (ruling 32). The Click row is
# what the keyboard has to DROP: the staff visit path offers cash and the
# business account only (D20).
PAYMENT_ROWS = [
    {"method": "cash", "name": "Cash on Delivery", "description": "Pay when it arrives"},
    {"method": "business_account", "name": "Business account", "description": "Bill the contract"},
    {"method": "click", "name": "Click", "description": "Pay by link"},
]

VISIT_ERROR_CODE_KEYS = {
    "SALES_VISIT_ALREADY_OPEN": "staff.sales.error.visit_open",
    "SALES_VISIT_NOT_OPEN": "staff.sales.error.visit_not_open",
    "SALES_VISIT_NOT_FOUND": "staff.error.api.not_found",
    "SALES_VISIT_NOT_OWNED": "staff.error.api.forbidden",
    "SALES_VISIT_STEP_INVALID": "staff.sales.error.visit_step",
    "SALES_STOCK_QTY_INVALID": "staff.sales.error.stock_qty",
    "SALES_STOCK_PRODUCT_INVALID": "staff.sales.error.stock_product",
    "SALES_OUTLET_NOT_ACTIVE": "staff.sales.error.outlet_not_active",
    "SALES_VISIT_ORDER_EXISTS": "staff.sales.error.order_exists",
    "SALES_ORDER_MIN_QTY": "staff.sales.error.order_min_qty",
    "SALES_ORDER_QTY_INVALID": "staff.sales.error.order_qty",
    # L47(4): the schedule branch's own code. No new copy -- the day screen
    # already owns the sentence for an unusable date, and Task 7 re-shows that
    # screen instead of leaving the agent on the confirm card with a generic
    # 400.
    "SALES_DELIVERY_DATE_INVALID": "staff.sales.visit.day_invalid",
    "SALES_PAYMENT_METHOD_INVALID": "staff.error.api.validation",
    "SALES_VISIT_OUTCOME_REQUIRED": "staff.sales.error.outcome_required",
    "SALES_VISIT_OUTCOME_INVALID": "staff.error.api.validation",
    # D17: a non-image or an oversize file. The bot refuses a FORWARD before
    # it downloads anything; everything else is the backend's call, because
    # the size cap is the storage service's, not a number the bot knows.
    "SALES_PHOTO_INVALID": "staff.sales.error.photo_invalid",
}

# Every `staff.sales.` suffix this task seeds, from the plan's shared
# interfaces plus ruling 19 (`list.overdue_suffix`, `visit.checkin_button`).
VISIT_SUFFIXES = (
    "hub.due", "list.title_due", "list.overdue_suffix",
    "card.next_due", "card.overdue", "card.rate", "card.suggested",
    "card.last_visit", "card.start_visit", "card.resume_visit",
    "visit.started", "visit.resume_or_abandon", "visit.checkin_prompt",
    "visit.checkin_button", "visit.checkin_skip", "visit.checkin_ok",
    "visit.checkin_far", "visit.checkin_skipped", "visit.stock_title",
    "visit.stock_hint", "visit.stock_row", "visit.stock_qty_prompt",
    "visit.stock_empties_prompt", "visit.sold_out", "visit.low",
    "visit.stock_done", "visit.stock_back", "visit.stock_no_products",
    "visit.stock_retry", "visit.order_title",
    "visit.order_suggested_line", "visit.order_last_line", "visit.order_none_line",
    "visit.order_suggested", "visit.order_edit", "visit.order_last",
    "visit.no_order", "visit.no_order_reason_prompt", "visit.order_edit_title",
    "visit.order_done", "visit.order_min", "visit.payment_prompt", "visit.no_rails",
    "visit.day_prompt", "visit.day_tomorrow", "visit.day_today", "visit.day_pick",
    "visit.day_pick_prompt", "visit.day_invalid", "visit.window_line",
    "visit.notes_prompt",
    "visit.confirm_title", "visit.confirm", "visit.back",
    "visit.order_created", "visit.order_pending_confirmation",
    "visit.order_confirmed", "visit.order_auto_confirmed",
    "visit.close_outcome_prompt", "visit.close_notes_prompt",
    "visit.next_visit_prompt", "visit.closed", "visit.abandoned",
    "visit.abandon", "visit.resume", "visit.order_maybe_landed",
    "visit.timeout",
    "visit.photo_kind_prompt", "visit.photo_saved", "visit.photo_duplicate",
    "visit.photo_failed", "visit.photo_forwarded",
    "notify.digest_due_today", "notify.digest_overdue", "notify.digest_unvisited",
    "notify.digest_open_visit", "notify.digest_days", "notify.digest_never",
    "notify.digest_more",
    "error.visit_open", "error.visit_not_open", "error.visit_step",
    "error.stock_qty", "error.stock_product", "error.outlet_not_active",
    "error.order_exists", "error.outcome_required", "error.order_min_qty",
    "error.order_qty", "error.photo_invalid",
)

# Only these interpolate (plan ruling 19); every other suffix is a BARE LABEL
# and the handlers compose the product name, the count and the quantity
# around it. That is the whole contract Tasks 10-12 render against — a
# placeholder added to any other row here breaks the guard below, and a
# handler that passes a kwarg for a bare label silently renders nothing extra.
INTERPOLATING = {
    "card.overdue": {"days"},
    "list.overdue_suffix": {"days"},
    "visit.checkin_ok": {"distance"},
    "visit.checkin_far": {"distance"},
    "visit.order_created": {"order_number"},
    "visit.closed": {"next_due"},
    # L106: the receipt's 🕘 line. Both ends interpolate, so a language that
    # writes the window the other way round can; the GLYPH stays the
    # handler's, exactly like `visit.checkin_ok`.
    "visit.window_line": {"start", "end"},
    # The digest's unvisited rows. `overdue` rows reuse
    # `list.overdue_suffix` -- one expression of "how late", drawn on the due
    # list and printed in the digest -- so only the AGE needs its own row.
    # `digest_never` is deliberately absent: "never visited" is a WORD, not a
    # number, which is the whole reason it is a separate key.
    "notify.digest_days": {"days"},
    # How many due rows the BACKEND cut (Task 3's `more_count`). The bot prints
    # the figure it was given and never counts anything itself.
    "notify.digest_more": {"count"},
}

# The three notify keys live in STAFF_TRANSLATIONS (all three carry HTML), so
# they are checked separately: ruling 19's carve-out is that `{reason}` belongs
# to the DECLINE only — a confirmation has no reason to print — and the digest
# header names a DAY and no outlet, because the lists under it are the outlets.
# This is the SSOT for the digest header's field set; Task 8 consumes it and
# does not restate it.
NOTIFY_PLACEHOLDERS = {
    "staff.sales.notify.agent_order_confirmed": {"outlet_name", "order_number"},
    "staff.sales.notify.agent_order_declined": {"outlet_name", "order_number", "reason"},
    "staff.sales.notify.morning_digest": {"date"},
}

# The two hub lists the hint has to account for, in the words each language
# uses for them. Lower-cased CONTAINMENT, not equality: this pins the hint's
# MEANING (L95 -- it explained Prospects and left the due list, which is the
# hub's first row and its whole "where do I go now" answer, unmentioned), so a
# future rewording stays free as long as it still says what the due list is.
# uz/ru carry THREE markers because `hub.due` names two halves there
# ("Bugungi va kechikkan" / "На сегодня и просроченные"): a hint that mentions
# only one half sends the agent looking for a button that is not written that
# way, so both halves are pinned, not either one.
HUB_HINT_MARKERS = {
    "en": ("due", "prospect"),
    "uz": ("bugungi", "kechikkan", "nomzod"),
    "ru": ("сегодня", "просроченн", "кандидат"),
}


def _load_seed_script():
    spec = importlib.util.spec_from_file_location(
        "seed_staff_translations", ROOT / "scripts" / "seed_staff_translations.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _callbacks(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


def _labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def _every_visit_keyboard():
    """Every keyboard the visit conversation can draw, both branches of each."""
    return [
        ("checkin", SalesKeyboards.checkin("en")),
        ("resume_or_abandon", SalesKeyboards.resume_or_abandon("en")),
        ("visit_resume", SalesKeyboards.visit_resume("en")),
        ("stock_overview", SalesKeyboards.stock_overview("en", STOCK_ROWS, True)),
        ("stock_overview_nothing_counted", SalesKeyboards.stock_overview("en", STOCK_ROWS, False)),
        # The two dead-end shapes of review #01/#49/#23: nothing is flagged for
        # the stock check at all, and the catalogue could not be fetched.
        ("stock_overview_empty_catalogue", SalesKeyboards.stock_overview("en", [], False)),
        ("stock_overview_fetch_failed",
         SalesKeyboards.stock_overview("en", [], False, can_retry=True)),
        ("stock_quantity_returnable", SalesKeyboards.stock_quantity("en", 11, 3, True, 2, False, True)),
        ("stock_quantity_plain", SalesKeyboards.stock_quantity("en", 12, None, False, None, True, False)),
        ("order_options", SalesKeyboards.order_options("en", True, True)),
        ("order_options_bare", SalesKeyboards.order_options("en", False, False)),
        ("order_edit", SalesKeyboards.order_edit("en", ORDER_LINES)),
        ("order_quantity", SalesKeyboards.order_quantity("en", 11, 8)),
        ("no_order_reasons", SalesKeyboards.no_order_reasons("en")),
        ("payment_methods", SalesKeyboards.payment_methods("en", PAYMENT_ROWS)),
        ("delivery_day", SalesKeyboards.delivery_day("en")),
        ("visit_skip_notes", SalesKeyboards.visit_skip("en", "notes")),
        ("visit_skip_closenotes", SalesKeyboards.visit_skip("en", "closenotes")),
        ("order_confirm", SalesKeyboards.order_confirm("en")),
        ("close_outcome", SalesKeyboards.close_outcome("en")),
        ("next_visit", SalesKeyboards.next_visit("en")),
        ("photo_kind", SalesKeyboards.photo_kind("en")),
    ]


class TestEveryVisitButtonIsRoutable:
    def test_every_callback_matches_a_pattern_tasks_11_and_12_register(self):
        unmatched = []
        for name, markup in _every_visit_keyboard():
            for data in _callbacks(markup):
                if not any(re.match(pattern, data) for pattern in REGISTERED_PATTERNS):
                    unmatched.append(f"{name}: {data}")
        assert not unmatched, (
            "these visit buttons have no registered handler shape, so a tap on them "
            f"would fall through to the catch-all router: {unmatched}"
        )

    def test_every_registered_pattern_is_actually_drawn(self):
        """A pattern nothing emits is dead wiring that hides a rename."""
        drawn = [data for _, markup in _every_visit_keyboard() for data in _callbacks(markup)]
        unused = [
            pattern for pattern in REGISTERED_PATTERNS
            if not any(re.match(pattern, data) for data in drawn)
        ]
        assert not unused, f"no keyboard draws a button for: {unused}"

    def test_the_grid_prefixes_are_literal_not_interpolated(self):
        """The routing guard rewrites `{...}` to `1`; a variable prefix goes blind."""
        source = (ROOT / "staff_bot" / "keyboards" / "sales.py").read_text(encoding="utf-8")
        assert 'staff_sales_v_qty_{product_id}_{step}' in source
        assert 'staff_sales_v_empt_{product_id}_{step}' in source
        assert 'staff_sales_v_oqtyset_{product_id}_{step}' in source
        assert 'staff_sales_v_{' not in source

    def test_the_resume_button_has_exactly_one_construction(self):
        """L95: the card and the "it may have landed" screen draw ONE button.

        Two constructions of one button are two places for the label and the
        callback to drift, and the callback here is a conversation ENTRY
        point: a rename in one of them leaves a live-looking button that
        starts nothing.
        """
        source = (ROOT / "staff_bot" / "keyboards" / "sales.py").read_text(encoding="utf-8")
        assert source.count('callback_data="staff_sales_visit_resume"') == 1
        card = SalesKeyboards.outlet_card("en", {"id": 5, "stage": "active", "open_visit_id": 88})
        standalone = SalesKeyboards.visit_resume("en")
        assert _callbacks(card)[0] == _callbacks(standalone)[0] == "staff_sales_visit_resume"
        assert _labels(card)[0] == _labels(standalone)[0]


class TestTheQuantityScreens:
    def test_the_on_hand_grid_marks_the_current_value_and_offers_every_step(self):
        markup = SalesKeyboards.stock_quantity("en", 11, 3, True, 2, False, True)
        assert [d for d in _callbacks(markup) if d.startswith("staff_sales_v_qty_")] == [
            f"staff_sales_v_qty_11_{step}" for step in STOCK_QTY_CHOICES
        ]
        assert "• 3" in _labels(markup)
        assert "5" in _labels(markup)

    def test_an_uncounted_product_marks_nothing(self):
        """`on_hand=None` is "not counted yet", which is not a counted zero.

        Marking `0` would tell the agent the shelf was already checked and
        found empty — and `VisitHandler._stock_items` would then be entitled
        to POST a number nobody gave.
        """
        markup = SalesKeyboards.stock_quantity("en", 12, None, False, None, False, False)
        assert not [label for label in _labels(markup) if label.startswith("• ")]

    def test_a_returnable_product_gets_the_empties_grid_and_a_plain_one_does_not(self):
        returnable = SalesKeyboards.stock_quantity("en", 11, 3, True, 2, False, True)
        assert [d for d in _callbacks(returnable) if d.startswith("staff_sales_v_empt_")] == [
            f"staff_sales_v_empt_11_{step}" for step in EMPTIES_CHOICES
        ]
        assert "♻️ • 2" in _labels(returnable)
        plain = SalesKeyboards.stock_quantity("en", 12, 0, False, None, True, False)
        assert [d for d in _callbacks(plain) if d.startswith("staff_sales_v_empt_")] == []

    def test_low_is_drawn_only_once_the_product_has_a_count(self):
        """"Running low" is a claim about a number, so it needs one.

        `_stock_items` leaves every uncounted product out of the POST, so a
        Low flag set on one is silently dropped while the overview keeps
        decorating the button with ⚠️. Sold out has an obvious quantity (zero,
        which the handler now writes); Low has none, so the answer is not to
        offer the question until the shelf is counted.
        """
        uncounted = SalesKeyboards.stock_quantity("en", 12, None, False, None, False, False)
        assert "staff_sales_v_soldout_12" in _callbacks(uncounted)
        assert "staff_sales_v_low_12" not in _callbacks(uncounted)
        counted = SalesKeyboards.stock_quantity("en", 12, 0, False, None, False, False)
        assert "staff_sales_v_low_12" in _callbacks(counted)

    def test_the_flag_toggles_show_their_current_state(self):
        off = SalesKeyboards.stock_quantity("en", 11, 3, True, 2, False, False)
        on = SalesKeyboards.stock_quantity("en", 11, 3, True, 2, True, True)
        assert [label for label in _labels(off) if label.startswith("☐")] and \
            not [label for label in _labels(off) if label.startswith("☑")]
        assert [label for label in _labels(on) if label.startswith("☑")] and \
            not [label for label in _labels(on) if label.startswith("☐")]
        assert "staff_sales_v_soldout_11" in _callbacks(on)
        assert "staff_sales_v_low_11" in _callbacks(on)

    def test_the_order_quantity_grid_targets_one_line(self):
        markup = SalesKeyboards.order_quantity("en", 11, 8)
        assert _callbacks(markup) == [
            *(f"staff_sales_v_oqtyset_11_{step}" for step in ORDER_QTY_CHOICES),
            "staff_sales_v_abandon",
        ]
        assert "• 8" in _labels(markup)


class TestTheOrderAndCloseScreens:
    def test_order_options_hides_what_the_backend_did_not_supply(self):
        """...and always keeps the way out.

        Every screen inside the visit carries Abandon, because the visit is
        server-side state: an agent who cannot end it here has to walk back
        out through the hub to do it.
        """
        full = _callbacks(SalesKeyboards.order_options("en", True, True))
        assert full == [
            "staff_sales_v_ordersugg", "staff_sales_v_orderedit",
            "staff_sales_v_orderlast", "staff_sales_v_noorder",
            "staff_sales_v_abandon",
        ]
        bare = _callbacks(SalesKeyboards.order_options("en", False, False))
        assert bare == ["staff_sales_v_orderedit", "staff_sales_v_noorder", "staff_sales_v_abandon"]

    def test_the_stock_overview_hides_done_until_something_was_counted(self):
        counted = _callbacks(SalesKeyboards.stock_overview("en", STOCK_ROWS, True))
        assert counted == [
            "staff_sales_v_stock_11", "staff_sales_v_stock_12",
            "staff_sales_v_stockdone", "staff_sales_v_abandon",
        ]
        empty = _callbacks(SalesKeyboards.stock_overview("en", STOCK_ROWS, False))
        assert "staff_sales_v_stockdone" not in empty

    def test_the_stock_overview_labels_carry_the_backends_own_keys(self):
        """The seam of controller ruling 32, pinned on the producer's side.

        `product_name` / `on_hand_qty` are `serialize_stock_check`'s names. A
        consumer that renamed them to `name` / `on_hand` would draw a shelf of
        buttons reading " · " with no product and no count, and no journey
        assertion on callback data would notice.
        """
        labels = _labels(SalesKeyboards.stock_overview("en", STOCK_ROWS, True))
        assert labels[0].startswith("Pure Water 19L · 3")
        # An uncounted row shows the product and no number — never a zero,
        # which would read as "checked, and the shelf is empty".
        assert labels[1].startswith("Pure Water 10L")
        assert "· 0" not in labels[1]

    def test_the_order_edit_screen_lists_one_button_per_line(self):
        assert _callbacks(SalesKeyboards.order_edit("en", ORDER_LINES)) == [
            "staff_sales_v_oqty_11", "staff_sales_v_oqty_12", "staff_sales_v_orderdone",
            "staff_sales_v_orderback", "staff_sales_v_abandon",
        ]
        assert "Pure Water 19L × 8" in _labels(SalesKeyboards.order_edit("en", ORDER_LINES))

    def test_payment_methods_draws_only_what_the_backend_allowed(self):
        """Dicts in, two rails out (controller ruling 32).

        The GET's rows arrive verbatim, so an outlet offered Click gets a
        keyboard with no Click button — the staff visit path cannot create a
        payment link, and a button that leads nowhere is worse than none.
        """
        assert _callbacks(SalesKeyboards.payment_methods("en", PAYMENT_ROWS)) == [
            "staff_sales_v_pay_cash", "staff_sales_v_pay_business_account",
            "staff_sales_v_abandon",
        ]
        assert _callbacks(SalesKeyboards.payment_methods("en", [PAYMENT_ROWS[0]])) == [
            "staff_sales_v_pay_cash", "staff_sales_v_abandon",
        ]

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_payment_methods_labels_a_rail_from_the_seeded_family_not_the_backend(self, language):
        """The FAMILY is the label, and the backend's `name` is never preferred.

        `StaffService.get_client_payment_methods` hard-codes "Cash on Delivery"
        and "Business Account" as plain English literals, so preferring
        `entry['name']` shipped English to every uz/ru agent while /health
        stayed green (the keys exist and are seeded — the English-leak class
        this bot has hit before). It also made ONE flow say two different words
        for the same rail: the confirm card one screen later already renders
        `staff.sales.visit.pay.<method>`.

        Dropping the preference is safe precisely because the family is
        registered for exactly `PAY_METHODS` and the loop has already filtered
        to it — there is no rail here whose key could be missing.
        """
        labelled = _labels(SalesKeyboards.payment_methods(language, PAYMENT_ROWS))
        for method in PAY_METHODS:
            expected = i18n.get(f"staff.sales.visit.pay.{method}", language)
            assert any(expected in label for label in labelled), (
                f"the {method} button does not render staff.sales.visit.pay.{method} "
                f"in {language}"
            )
        # The row's own name is what must NOT survive: every row in
        # PAYMENT_ROWS carries one, and "Cash on Delivery" is the literal
        # `get_client_payment_methods` hard-codes.
        assert not any("Cash on Delivery" in label for label in labelled), (
            "the backend's hard-coded English name is on the button again"
        )
        # A row with no name at all renders identically -- there is no
        # fallback branch left to diverge.
        unnamed = _labels(SalesKeyboards.payment_methods(language, [{"method": "cash"}]))
        assert unnamed[0] == labelled[0]

    def test_close_outcome_never_offers_the_backend_stamped_outcome(self):
        """`order_placed` is stamped by VisitService when the visit carries an
        order; offering it as a button would let an agent claim an order that
        does not exist."""
        assert _callbacks(SalesKeyboards.close_outcome("en")) == [
            *(f"staff_sales_v_outcome_{outcome}" for outcome in CLOSE_OUTCOMES),
            "staff_sales_v_abandon",
        ]
        assert "staff_sales_v_outcome_order_placed" not in _callbacks(
            SalesKeyboards.close_outcome("en")
        )

    def test_next_visit_offers_the_five_cadence_choices(self):
        assert _callbacks(SalesKeyboards.next_visit("en")) == [
            *(f"staff_sales_v_next_{choice}" for choice in NEXT_VISIT_CHOICES),
            "staff_sales_v_abandon",
        ]

    def test_no_order_reasons_covers_the_backend_check_constraint(self):
        assert _callbacks(SalesKeyboards.no_order_reasons("en")) == [
            *(f"staff_sales_v_noreason_{reason}" for reason in NO_ORDER_REASONS),
            "staff_sales_v_abandon",
        ]


class TestTheVisitErrorCodesAreTranslated:
    def test_every_visit_error_code_maps_to_the_planned_key(self):
        for code, key in VISIT_ERROR_CODE_KEYS.items():
            assert BaseHandler.API_ERROR_CODE_KEY_MAP.get(code) == key, (
                f"{code} must map to {key} or the agent reads a raw backend code"
            )

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_every_mapped_key_is_seeded(self, language):
        module = _load_seed_script()
        unseeded = sorted(
            {key for key in VISIT_ERROR_CODE_KEYS.values()
             if not module._curated_value(key, language)}
        )
        assert not unseeded, f"visit error copy is missing in {language}: {unseeded}"


class TestTheErrorKeyFamilyIsRegisteredInBothRegistries:
    """The error map's VALUES are keys no extractor can see.

    `BaseHandler._resolve_api_error_message` (handlers/base.py:467-478) looks
    the key up at RUNTIME out of a dict value, so
    `Translation._extract_literal_staff_keys`'s `i18n.get('literal')` regex
    finds none of them. Only three of the fifteen `staff.sales.error.*` keys
    are ALSO written as literals somewhere in a handler
    (`error.stage_invalid`, `error.visit_not_open`, `error.outlet_not_active`)
    -- the other twelve are addressed this way alone. Until the family is
    registered, an unseeded one is invisible to `/health`
    (`staff_bot/webhook_server.py:180`) and surfaces as a humanised English
    key tail inside an alert, at the exact moment something already went
    wrong. This is the same defect class as
    `staff.delivery.status.cancelled`, which
    `tests/unit/test_staff_bot_english_leak_regressions.py::
    test_health_check_requires_the_full_delivery_status_family` pins for the
    delivery side.
    """

    def test_health_requires_every_key_the_error_map_can_ask_for(self):
        from staff_bot.i18n import Translation

        keys = set()
        Translation._add_dynamic_family_keys(keys)
        missing = sorted(set(BaseHandler.API_ERROR_CODE_KEY_MAP.values()) - keys)
        assert not missing, (
            "/health cannot detect these error-map targets as required, so an "
            f"unseeded one stays invisible until it reaches a staff phone: {missing}"
        )

    def test_the_seeded_error_rows_and_the_error_map_name_the_same_keys(self):
        """Equality, both directions.

        A mapped key with no row renders a key tail; a row nothing maps to is
        dead copy the agent can never be shown, and the next author reuses the
        wrong one. Derived from the two live sources -- the map and the seed
        catalog -- so neither side can be edited alone.
        """
        module = _load_seed_script()
        seeded = {
            f"staff.sales.{suffix}"
            for suffix in module.SALES_TEXT_TRANSLATIONS
            if suffix.startswith("error.")
        }
        mapped = {
            key
            for key in BaseHandler.API_ERROR_CODE_KEY_MAP.values()
            if key.startswith("staff.sales.error.")
        }
        assert seeded == mapped, (
            f"rows with no code: {sorted(seeded - mapped)}; "
            f"codes with no row: {sorted(mapped - seeded)}"
        )


# Suffixes the derived Russian gate below skips, named one by one so that a key
# added tomorrow is gated by default and only an explicit entry here can opt it
# out. `new.choose_class` prints the outlet-class LETTERS ("A", "C"), which are
# the values themselves and cannot be Cyrillicised without changing what the
# agent has to type back.
RU_LATIN_ALLOWED_SUFFIXES = ("new.choose_class",)


class TestTheVisitTranslationsAreSeededAndRegistered:
    @pytest.mark.parametrize("language", LANGUAGES)
    def test_every_visit_suffix_is_seeded(self, language):
        module = _load_seed_script()
        missing = sorted(
            suffix for suffix in VISIT_SUFFIXES
            if not module._curated_value(f"staff.sales.{suffix}", language)
        )
        assert not missing, f"unseeded staff.sales.* copy in {language}: {missing}"

    def test_only_the_documented_suffixes_interpolate(self):
        """A stray `{placeholder}` reaches the agent as a literal brace.

        This is also the guard on the OTHER direction, and that is the one
        that bites: `render_translation` drops a kwarg the template has no
        field for, silently. A handler that passed `name=` to a bare label
        would render a screen with no product name and no error anywhere —
        so plan ruling 19's list is pinned here, and every consumer composes
        its values around the label instead.
        """
        module = _load_seed_script()
        wrong = {}
        for suffix in VISIT_SUFFIXES:
            expected = INTERPOLATING.get(suffix, set())
            for language in LANGUAGES:
                value = module.SALES_TEXT_TRANSLATIONS[suffix][language]
                found = set(re.findall(r"\{(\w+)\}", value))
                if found != expected:
                    wrong[f"{suffix}[{language}]"] = sorted(found)
        assert not wrong, f"placeholder sets do not match the plan: {wrong}"

    def test_the_notify_copy_carries_a_reason_only_where_there_is_one(self):
        """Ruling 19's carve-out: a confirmation has no reason to print.

        The webhook passes `outlet_name`, `order_number` AND `reason` to both
        events, so the copy is what decides which of them the agent reads;
        per-key sets are compared because a `{reason}` on the confirmation row
        would print the word "None" on a happy path.
        """
        module = _load_seed_script()
        wrong = {}
        for key, expected in NOTIFY_PLACEHOLDERS.items():
            for language in LANGUAGES:
                value = module.STAFF_TRANSLATIONS[key][language]
                found = set(re.findall(r"\{(\w+)\}", value))
                if found != expected:
                    wrong[f"{key}[{language}]"] = sorted(found)
        assert not wrong, f"notify placeholder sets do not match ruling 19: {wrong}"

    def test_health_requires_every_visit_family_key(self):
        """The families are built with f-strings, so only these loops make a
        gap visible to `/health`."""
        from staff_bot.i18n import Translation

        keys = set()
        Translation._add_dynamic_family_keys(keys)
        expected = (
            {f"staff.sales.visit.outcome.{o}" for o in VISIT_OUTCOMES}
            | {f"staff.sales.visit.reason.{r}" for r in NO_ORDER_REASONS}
            | {f"staff.sales.visit.pay.{m}" for m in PAY_METHODS}
            | {f"staff.sales.visit.next.{c}" for c in NEXT_VISIT_CHOICES}
            | {"staff.sales.list.title_due"}
            | {"staff.sales.notify.agent_order_confirmed",
               "staff.sales.notify.agent_order_declined"}
            | {f"staff.sales.visit.photo_kind.{kind}" for kind in PHOTO_KINDS}
        )
        assert expected <= keys, f"not registered for /health: {sorted(expected - keys)}"

    def test_both_registries_derive_the_sales_event_family_from_one_constant(self):
        """M27: /health and the seeder read `SALES_EVENTS`, not a hand copy.

        `staff.sales.notify.<event>` is built with an f-string from the
        BACKEND's event name, so the literal scraper cannot see it at all —
        these two loops are the only thing that makes a missing row visible.
        Comparing both against the constant (rather than against each other)
        is what stops all three drifting together.
        """
        from staff_bot.i18n import Translation

        expected = {f"staff.sales.notify.{event}" for event in SALES_EVENTS}
        health = set()
        Translation._add_dynamic_family_keys(health)
        assert {key for key in health if key.startswith("staff.sales.notify.")} == expected
        seeded = set()
        _load_seed_script()._add_dynamic_keys(seeded)
        assert {key for key in seeded if key.startswith("staff.sales.notify.")} == expected

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_every_family_member_is_seeded(self, language):
        module = _load_seed_script()
        keys = (
            [f"staff.sales.visit.outcome.{o}" for o in VISIT_OUTCOMES]
            + [f"staff.sales.visit.reason.{r}" for r in NO_ORDER_REASONS]
            + [f"staff.sales.visit.pay.{m}" for m in PAY_METHODS]
            + [f"staff.sales.visit.next.{c}" for c in NEXT_VISIT_CHOICES]
            + [f"staff.sales.visit.photo_kind.{kind}" for kind in PHOTO_KINDS]
            + ["staff.sales.list.title_due",
               "staff.sales.notify.agent_order_confirmed",
               "staff.sales.notify.agent_order_declined"]
        )
        missing = sorted(k for k in keys if not module._curated_value(k, language))
        assert not missing, f"unseeded family member in {language}: {missing}"

    def test_the_seed_scripts_hand_written_tuples_match_the_keyboard_constants(self):
        """The seed script cannot import `staff_bot` (it runs where that tree
        does not exist), so its family tuples are a copy. This is the check
        that stops the copy drifting."""
        module = _load_seed_script()
        keys = set()
        module._add_dynamic_keys(keys)

        def tails(prefix):
            return {key[len(prefix):] for key in keys if key.startswith(prefix)}

        assert tails("staff.sales.visit.outcome.") == set(VISIT_OUTCOMES)
        assert tails("staff.sales.visit.reason.") == set(NO_ORDER_REASONS)
        assert tails("staff.sales.visit.pay.") == set(PAY_METHODS)
        assert tails("staff.sales.visit.next.") == set(NEXT_VISIT_CHOICES)
        assert tails("staff.sales.visit.photo_kind.") == set(PHOTO_KINDS)
        assert "staff.sales.list.title_due" in keys
        assert "staff.sales.notify.agent_order_confirmed" in keys
        assert "staff.sales.notify.agent_order_declined" in keys

    def test_the_new_copy_passes_the_seeds_own_russian_quality_gate(self):
        """The seed refuses Latin transliteration in ru — THIS test is the gate.

        `main()` does not run `_validate_russian_translations`: the call at
        scripts/seed_staff_translations.py is commented out, so nothing else in
        the repo enforces it. The rule itself still lives in the seed script and
        is called rather than restated, but the enforcement point is here — and
        the key set is DERIVED from the family so a key added tomorrow is gated
        the day it is added, instead of only the five this cycle happened to
        name.
        """
        module = _load_seed_script()
        module._validate_russian_translations({
            f"staff.sales.{suffix}"
            for suffix in module.SALES_TEXT_TRANSLATIONS
            if suffix not in RU_LATIN_ALLOWED_SUFFIXES
        })


class TestTheVisitableStagesAreOfferedNotEnforced:
    def test_a_prospect_is_never_offered_a_visit(self):
        """One definition of "worth walking into", used by the CARD only.

        The backend keeps its own copy (`OutletService.DUE_STAGES` for the due
        scope, `VisitService.place_order` → `SALES_OUTLET_NOT_ACTIVE` for the
        write). That duplication is deliberate and recorded: this tuple
        decides only what is OFFERED, and the backend is what refuses. A
        prospect has no customer account and no address row, so an order
        placed at one has nothing to be written against.
        """
        assert VISITABLE_STAGES == ('active', 'at_risk', 'dormant', 'trial')
        assert 'prospect' not in VISITABLE_STAGES
        assert 'lost' not in VISITABLE_STAGES


class TestTheBotsVocabulariesMatchTheBackendOriginals:
    """The four tuples in `keyboards/sales.py` are COPIES, and this is the pin.

    Each one exists on the backend first -- as a CHECK constraint's value list,
    as the guard that accepts an order, as the filter that builds the due
    scope -- and the bot restates it so a button can carry the value and an
    i18n tail can be built from it. A copy nobody compares is drift waiting to
    happen: a sixth outcome added to the model would ship a screen that cannot
    offer it, and a rail removed from `AGENT_PAYMENT_METHODS` would ship a
    button the write path refuses. Compared as tuples because both sides are
    declared as ordered tuples, so this catches a REORDER too -- the order is
    the order the buttons are drawn in.
    """

    def test_visit_outcomes_match_the_model(self):
        from business_app.models.sales_visits import VISIT_OUTCOMES as BACKEND_VISIT_OUTCOMES

        assert VISIT_OUTCOMES == BACKEND_VISIT_OUTCOMES

    def test_no_order_reasons_match_the_model(self):
        from business_app.models.sales_visits import NO_ORDER_REASONS as BACKEND_NO_ORDER_REASONS

        assert NO_ORDER_REASONS == BACKEND_NO_ORDER_REASONS

    def test_pay_methods_match_the_order_guard(self):
        from business_app.services.sales.visit_service import AGENT_PAYMENT_METHODS

        assert PAY_METHODS == AGENT_PAYMENT_METHODS

    def test_visitable_stages_match_the_due_scope_filter(self):
        from business_app.services.sales.outlet_service import DUE_STAGES

        assert VISITABLE_STAGES == DUE_STAGES


class TestTheHubHintExplainsEveryList:
    """`HubHandler.show_hub` renders the title and this hint and nothing else.

    `staff_bot/handlers/sales/hub.py:129-132` builds the whole screen from
    `staff.sales.hub.title` + `staff.sales.hub.hint`, above a keyboard whose
    FIRST row is the due list (`staff_bot/keyboards/sales.py:126-129`, "the
    due list leads because it IS the agent's work for the day"). A hint that
    describes only the second row is the entire on-screen explanation of a
    screen it does not explain.
    """

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_the_hint_names_the_due_list_as_well_as_prospects(self, language):
        module = _load_seed_script()
        hint = module._curated_value("staff.sales.hub.hint", language)
        assert hint, f"staff.sales.hub.hint is unseeded in {language}"
        missing = [m for m in HUB_HINT_MARKERS[language] if m not in hint.lower()]
        assert not missing, (
            f"the {language} hub hint never mentions {missing}, so the agent is "
            f"told what one of the three lists is and left to guess the rest: {hint!r}"
        )


class TestTheVisitFlowKeyIsCleared:
    def test_sales_visit_is_a_documented_pending_flow(self):
        """WRITE-side rule: the conversation stamps `user_data['sales_visit']`,
        so a menu tap must drop it -- otherwise the agent's next text lands in
        a visit they already walked out of."""
        assert "sales_visit" in PENDING_FLOW_USER_DATA_KEYS


class TestTheOrderAndOutletVocabulariesMatchTheBackendOriginals:
    """Three more COPIES the bot keeps of a backend vocabulary, now pinned.

    The four in `keyboards/sales.py` were already compared above. These three
    were not, and two of them sit inside the very functions that define
    `/health`'s required-key set — which is what makes a gap in them
    UNDETECTABLE rather than merely untranslated: `stage_label()` builds
    `staff.sales.stage.<stage>` by f-string, so a stage the hand-written loop
    has not heard of renders a humanised English key tail to an agent while
    /health stays green. That is exactly how `staff.delivery.status.cancelled`
    shipped.
    """

    def test_the_order_states_the_bot_renders_are_the_backends_own_tuple(self):
        """`confirmation.state` is D15's, and all three lines are the agent's
        only word on whether the store still has to answer."""
        from business_app.services.sales.agent_order_confirmation_service import CONFIRMATION_STATES
        from staff_bot.handlers.sales.visit import ORDER_STATES

        assert ORDER_STATES == CONFIRMATION_STATES

    def test_every_order_state_renders_a_line_and_nothing_else_does(self):
        from staff_bot.handlers.sales.visit import ORDER_STATES, VisitHandler

        for state in ORDER_STATES:
            assert VisitHandler._order_state_line(state, "en"), (
                f"the backend can answer with {state!r} and the receipt says nothing about it"
            )
        # The 409's `details.order` carries no state at all, and the receipt
        # prints no line for it rather than guessing one.
        assert VisitHandler._order_state_line("", "en") == ""
        assert VisitHandler._order_state_line("cancelled", "en") == ""

    def test_the_stage_and_type_families_cover_every_backend_value(self):
        """Both `/health` registries, against `business_app/models/sales.py`."""
        from business_app.models.sales import OUTLET_STAGES, OUTLET_TYPES
        from staff_bot.i18n import Translation

        module = _load_seed_script()
        for register in (Translation._add_dynamic_family_keys, module._add_dynamic_keys):
            keys = set()
            register(keys)

            def tails(prefix, keys=keys):
                return {key[len(prefix):] for key in keys if key.startswith(prefix)}

            assert tails("staff.sales.stage.") == set(OUTLET_STAGES), register
            assert tails("staff.sales.type.") == set(OUTLET_TYPES), register

    def test_the_new_outlet_handlers_own_type_tuple_matches_the_model(self):
        """The third copy: the type keyboard's allowlist, re-checked in the handler."""
        from business_app.models.sales import OUTLET_TYPES
        from staff_bot.handlers.sales.new_outlet import OUTLET_TYPES as BOT_OUTLET_TYPES

        assert BOT_OUTLET_TYPES == OUTLET_TYPES

    def test_the_stage_emoji_map_decorates_every_stage(self):
        """The emoji is never the label, but a stage it has not heard of is
        drawn with a bullet — a signal that the map and the model have drifted."""
        from business_app.models.sales import OUTLET_STAGES
        from staff_bot.keyboards.sales import STAGE_EMOJI

        assert set(STAGE_EMOJI) == set(OUTLET_STAGES)


# ---------------------------------------------------------------------------
# Phase 2b, Task 6: Nearby, and the card's Try-out button
# ---------------------------------------------------------------------------

# The patterns Task 6 registers for the Nearby conversation, verbatim from the
# plan's shared interfaces. Same shape restriction as REGISTERED_PATTERNS
# above -- `^literal$`, `^prefix_\d+$` and `^prefix_\w+$` are all
# tests/staff_bot/test_staff_wiring_contract.py::_sample_matching can sample,
# and a pattern it cannot sample is a handler nobody checks for theft.
NEARBY_PATTERNS = (
    r"^staff_sales_nearby$",
    r"^staff_sales_nb_\w+$",
    r"^staff_sales_outlet_\d+$",
    r"^staff_sales_hub$",
)

# Every `staff.sales.` suffix this task seeds.
NEARBY_SUFFIXES = (
    "hub.nearby", "nearby.title", "nearby.pin_prompt", "nearby.pin_button",
    "nearby.found", "nearby.distance", "nearby.again",
)

# Only the distance row interpolates -- ruling 19's rule applied to this
# screen. The glyphs and the outlet name are composed AROUND these strings by
# `nearby_list` and `NearbyHandler`, because `render_translation` drops a
# kwarg the template has no field for without a word.
NEARBY_INTERPOLATING = {"nearby.distance": {"distance"}}

# `serialize_outlet_agent_row` + Task 4's `distance_m`. The second row has
# none: an outlet whose pin the backend could not measure from keeps its row
# and loses the suffix, exactly as an outlet with no due date loses the
# overdue one.
NEARBY_ROWS = [
    # Whole metres, because that is the only shape `serialize_outlet_agent_row`
    # can emit (`round(distance_m)`): a fractional fixture describes a body the
    # backend cannot send and turns the bot's formatting into a second rounding
    # rule nothing else pins.
    {"id": 5, "name": "Bahor market", "stage": "active", "distance_m": 118,
     "overdue_days": 3, "has_open_visit": False, "open_visit_id": None},
    {"id": 6, "name": "Nur savdo", "stage": "prospect", "distance_m": None,
     "overdue_days": None, "has_open_visit": False, "open_visit_id": None},
]


class TestTheNearbyScreensAreRoutable:
    def test_the_hub_offers_nearby_directly_under_the_due_list(self):
        """Row order IS the screen's advice: the due list is the day's work,
        and Nearby is what an agent reaches for when they are already out."""
        assert _callbacks(SalesKeyboards.hub("en")) == [
            "staff_sales_list_due",
            "staff_sales_nearby",
            "staff_sales_list_prospects",
            "staff_sales_list_all",
            "staff_back_to_main",
        ]

    def test_every_nearby_button_matches_a_pattern_task_6_registers(self):
        drawn = (
            _callbacks(SalesKeyboards.nearby_list("en", NEARBY_ROWS))
            + _callbacks(SalesKeyboards.back_to_hub("en"))
        )
        unmatched = [
            data for data in drawn
            if not any(re.match(pattern, data) for pattern in NEARBY_PATTERNS)
        ]
        assert not unmatched, (
            "these Nearby buttons have no registered handler shape, so a tap on "
            f"them would fall through to the catch-all router: {unmatched}"
        )

    def test_every_nearby_pattern_is_actually_drawn(self):
        """A pattern nothing emits is dead wiring that hides a rename."""
        drawn = (
            _callbacks(SalesKeyboards.hub("en"))
            + _callbacks(SalesKeyboards.nearby_list("en", NEARBY_ROWS))
            + _callbacks(SalesKeyboards.back_to_hub("en"))
        )
        unused = [
            pattern for pattern in NEARBY_PATTERNS
            if not any(re.match(pattern, data) for data in drawn)
        ]
        assert not unused, f"no Nearby keyboard draws a button for: {unused}"

    def test_the_rows_are_drawn_in_the_order_they_arrived(self):
        """`OutletService.nearby` ranks them; this keyboard never sorts."""
        assert _callbacks(SalesKeyboards.nearby_list("en", NEARBY_ROWS))[:2] == [
            "staff_sales_outlet_5", "staff_sales_outlet_6",
        ]

    def test_a_row_carries_the_backends_distance_and_a_measureless_row_carries_none(self):
        labels = _labels(SalesKeyboards.nearby_list("en", NEARBY_ROWS))
        suffix = i18n.get("staff.sales.nearby.distance", "en", distance=118)
        assert labels[0] == f"✅ Bahor market · {suffix}"
        assert labels[1] == "🆕 Nur savdo"

    def test_a_long_name_never_pushes_the_distance_off_the_button(self):
        """The distance is WHY the row is in this list; the name gives way.

        The same rule the due list's overdue suffix needed, which is why both
        lists now compose their label through one `_outlet_button`.
        """
        long_name = "Bahor market va oziq-ovqat do'koni Chilonzor 5-mavze filiali"
        label = _labels(SalesKeyboards.nearby_list(
            "en", [{"id": 5, "name": long_name, "stage": "active", "distance_m": 2410.0}]
        ))[0]
        assert label.endswith(i18n.get("staff.sales.nearby.distance", "en", distance=2410))
        assert label.startswith("✅ Bahor market")
        assert len(label) <= 60


class TestTheNearbyTranslationsAreSeeded:
    @pytest.mark.parametrize("language", LANGUAGES)
    def test_every_nearby_suffix_is_seeded(self, language):
        module = _load_seed_script()
        missing = sorted(
            suffix for suffix in NEARBY_SUFFIXES
            if not module._curated_value(f"staff.sales.{suffix}", language)
        )
        assert not missing, f"unseeded staff.sales.* copy in {language}: {missing}"

    def test_only_the_distance_row_interpolates(self):
        module = _load_seed_script()
        wrong = {}
        for suffix in NEARBY_SUFFIXES:
            expected = NEARBY_INTERPOLATING.get(suffix, set())
            for language in LANGUAGES:
                value = module.SALES_TEXT_TRANSLATIONS[suffix][language]
                found = set(re.findall(r"\{(\w+)\}", value))
                if found != expected:
                    wrong[f"{suffix}[{language}]"] = sorted(found)
        assert not wrong, f"placeholder sets do not match the plan: {wrong}"

    def test_the_russian_distance_unit_is_cyrillic(self):
        """`м`, not `m`. The seed's own Latin gate covers the family
        (`test_the_new_copy_passes_the_seeds_own_russian_quality_gate`), and a
        one-letter unit is exactly the row that slips past a human reader."""
        module = _load_seed_script()
        assert module._curated_value("staff.sales.nearby.distance", "ru") == "{distance} м"


class TestTheNearbyFlowKeyIsCleared:
    def test_sales_nearby_is_a_documented_pending_flow(self):
        """WRITE-side rule: the conversation stamps `user_data['sales_nearby']`,
        so a menu tap must drop it -- otherwise the agent's next pin lands in a
        search they already walked out of."""
        assert "sales_nearby" in PENDING_FLOW_USER_DATA_KEYS


class TestThePhotoPickerIsNotAVisitStep:
    def test_the_picker_offers_the_three_kinds_and_no_way_out_of_the_visit(self):
        """No Abandon row, unlike every other visit keyboard.

        The picker answers a MESSAGE the agent sent; the step screen is still
        on the chat above it, Abandon included. A second Abandon here would
        be the one button on this keyboard that ends the visit, one tap away
        from "shelf", on a screen that is about a photograph.
        """
        markup = SalesKeyboards.photo_kind("en")
        assert _callbacks(markup) == [f"staff_sales_v_photo_{kind}" for kind in PHOTO_KINDS]
        assert "staff_sales_v_abandon" not in _callbacks(markup)

    def test_the_kinds_are_the_backends_own_check_constraint(self):
        """`ck_visit_photos_kind` accepts exactly these three (D17 data model).

        A fourth button here is a 400 the agent cannot act on; a missing one
        is a photo they cannot file. Compared against the MODEL's tuple, the
        constraint's own source, like every other vocabulary pin in this file
        -- a literal typed here would drift with the bot rather than catch it.
        """
        from business_app.models.sales_visits import PHOTO_KINDS as BACKEND_PHOTO_KINDS

        assert PHOTO_KINDS == BACKEND_PHOTO_KINDS


class TestLeavingAVisitAlwaysDropsItsDraft:
    """"The session expired" must never end the conversation with the draft still there.

    `user_data` outlives the conversation, so a draft left behind belongs to no live
    flow: `SalesHubHandler._refused_by_open_visit` goes on refusing Nearby and the
    try-out on its behalf, and every button still on the agent's screen points at a
    visit this phone can no longer post to. `VisitHandler._session_gone` is the one
    place that decides it (say it, pop the draft, END), and the four journeys in
    tests/staff_bot/test_sales_visit_journey.py drive four of the eleven call sites.

    This is the guard for the other seven -- and for the eighth site somebody adds
    next month. Read off the SOURCE with `ast`, importing nothing: a handler that
    calls `_handle_auth_error` itself instead of going through the helper is the
    exact shape of the defect, whether or not a journey ever walks to that screen.
    """

    # Two legitimate direct callers besides the helper itself:
    #   * `_load_products` answers `bool`, and its three callers all keep the agent
    #     on the shelf screen rather than ENDing -- no conversation to leave;
    #   * `_load_current` answers `Optional[Dict]` ("already told them why"), so it
    #     cannot return `_session_gone`'s END; it drops the draft on the same line
    #     instead. Calling the helper and discarding its verdict is the exact defect
    #     the photo path was just fixed for, so it does not do that.
    # Anything else is a path that ENDS the conversation, and it must go through
    # `_session_gone` so the draft goes with it.
    ALLOWED_DIRECT_CALLERS = {"_session_gone", "_load_products", "_load_current"}

    def test_only_the_session_helper_announces_an_expired_session(self):
        import ast

        source = (ROOT / "staff_bot" / "handlers" / "sales" / "visit.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        callers = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "_handle_auth_error"
                ):
                    callers.add(node.name)

        assert callers == self.ALLOWED_DIRECT_CALLERS, (
            "these visit functions call `_handle_auth_error` directly: "
            f"{sorted(callers - self.ALLOWED_DIRECT_CALLERS)}. Every path that ENDS the "
            "conversation must go through `_session_gone`, which also drops the draft -- "
            "otherwise the agent is left with a draft no live flow owns, and Nearby and "
            "the try-out are refused on behalf of a visit conversation that is over."
        )


# ---------------------------------------------------------------------------
# Phase 3, Task 7: "My stats" — the agent's own KPI card
# ---------------------------------------------------------------------------

# Every `staff.sales.` suffix the stats screen seeds, from the plan's shared
# interfaces: the hub button, the card title, the dash a null figure prints,
# the three period labels, the four section headings and one label per
# metric the service publishes.
STATS_SUFFIXES = (
    "stats.button", "stats.title", "stats.na",
    "stats.period_today", "stats.period_week", "stats.period_month",
    "stats.section_visits", "stats.section_outlets", "stats.section_orders",
    "stats.section_discipline",
    "stats.metric_planned_visits", "stats.metric_completed_visits",
    "stats.metric_plan_vs_fact_pct", "stats.metric_unplanned_visits",
    "stats.metric_visits_per_day", "stats.metric_strike_rate_pct",
    "stats.metric_assigned_outlets", "stats.metric_active_outlets",
    "stats.metric_active_share_pct", "stats.metric_new_outlets_registered",
    "stats.metric_new_outlets_activated", "stats.metric_orders_placed",
    "stats.metric_orders_delivered_paid", "stats.metric_bottles_delivered_paid",
    "stats.metric_revenue_delivered_paid", "stats.metric_agent_orders_cancelled",
    "stats.metric_suggested_vs_accepted_pct", "stats.metric_out_of_range_checkins",
    "stats.metric_skipped_checkins", "stats.metric_avg_visit_minutes",
)


class TestTheStatsCardMirrorsTheBackendsMetricContract:
    """The KPI card is a RENDERER: every figure on it is a field the backend
    published, and every label is a row the seed carries.

    Three things can only be caught here. The period tuple is a COPY of the
    backend's `STATS_PERIODS` (the bot cannot import it at runtime, and the
    route refuses a window outside it), the twenty metric labels are a copy
    of `METRIC_KEYS` in the order the spec lists them, and the period keys
    are built with an f-string — invisible to the literal extractor that
    feeds `/health`, so only the two family loops make a gap visible.
    """

    def test_the_period_switch_is_the_backends_own_tuple(self):
        from business_app.utils.local_windows import STATS_PERIODS as BACKEND_STATS_PERIODS
        from staff_bot.keyboards.sales import STATS_PERIODS

        assert STATS_PERIODS == BACKEND_STATS_PERIODS

    def test_the_switch_draws_one_button_per_window_and_ticks_the_current_one(self):
        """The current window keeps its button rather than being dropped: a row
        that changes shape under the agent's thumb moves the other two, and the
        next tap lands on a window they did not mean."""
        from staff_bot.keyboards.sales import STATS_PERIODS

        markup = SalesKeyboards.stats("en", "week")
        assert _callbacks(markup) == [
            f"staff_sales_stats_{period}" for period in STATS_PERIODS
        ] + ["staff_back_to_main"]
        ticked = [label for label in _labels(markup) if label.startswith("✅ ")]
        assert ticked == [_labels(markup)[STATS_PERIODS.index("week")]]

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_every_stats_suffix_is_seeded(self, language):
        module = _load_seed_script()
        missing = sorted(
            suffix for suffix in STATS_SUFFIXES
            if not module._curated_value(f"staff.sales.{suffix}", language)
        )
        assert not missing, f"unseeded staff.sales.stats.* copy in {language}: {missing}"

    def test_no_stats_row_interpolates(self):
        """Every row is a BARE label: the handler composes the figure, the "%"
        and the currency around it. `render_translation` drops a kwarg a
        template has no field for silently, so a placeholder added here would
        print a literal brace to an agent."""
        module = _load_seed_script()
        wrong = {}
        for suffix in STATS_SUFFIXES:
            for language in LANGUAGES:
                found = set(re.findall(r"\{(\w+)\}", module.SALES_TEXT_TRANSLATIONS[suffix][language]))
                if found:
                    wrong[f"{suffix}[{language}]"] = sorted(found)
        assert not wrong, f"the stats card composes its own figures; these rows carry placeholders: {wrong}"

    def test_both_registries_register_the_period_family(self):
        from staff_bot.i18n import Translation
        from staff_bot.keyboards.sales import STATS_PERIODS

        expected = {f"staff.sales.stats.period_{period}" for period in STATS_PERIODS}
        health = set()
        Translation._add_dynamic_family_keys(health)
        assert expected <= health, f"not registered for /health: {sorted(expected - health)}"
        seeded = set()
        _load_seed_script()._add_dynamic_keys(seeded)
        assert expected <= seeded, f"not registered in the seed: {sorted(expected - seeded)}"

    def test_health_requires_every_stats_key(self):
        """The two halves of the same required set: twenty-seven LITERAL keys the
        extractor finds in `staff_bot/**`, three period keys the family loop
        adds. A key in neither parks the container `unhealthy` for good."""
        from staff_bot.i18n import Translation

        required = Translation._extract_literal_staff_keys(ROOT / "staff_bot")
        Translation._add_dynamic_family_keys(required)
        expected = {f"staff.sales.{suffix}" for suffix in STATS_SUFFIXES}
        assert expected <= required, f"/health would not notice these missing: {sorted(expected - required)}"

    def test_every_metric_the_service_publishes_has_a_line_in_spec_order(self):
        """The card's twenty labels ARE `METRIC_KEYS`, read off the source.

        A metric the service publishes and the card omits is a figure the
        agent is never shown; a label the card draws for a key the service
        dropped renders the dash for ever. Order is asserted too, because
        the grouping is the screen's whole readability -- and because the
        regex reads COMMENTS as well as code, a metric key named in a
        comment in that file would fail this loudly rather than quietly.
        """
        from business_app.services.sales.agent_metrics_service import METRIC_KEYS

        source = (ROOT / "staff_bot" / "handlers" / "sales" / "stats.py").read_text(encoding="utf-8")
        assert re.findall(r"staff\.sales\.stats\.metric_(\w+)", source) == list(METRIC_KEYS)

    def test_the_cards_four_sections_are_the_services_own_grouping(self):
        """R30: which figures sit under which heading is the BACKEND's `METRIC_GROUPS`,
        read off this card's source in the order it draws them.

        The same four groups are drawn by the Analytics tab. Before `METRIC_GROUPS`
        they were two hand-made lists held together only by the twenty names, so a
        metric could quietly move group on one surface and not the other and every
        test stayed green. Parsed from the source for the same reason the test above
        is: the headings and the rows are literal keys, and their ORDER on the screen
        is the thing being pinned.
        """
        from business_app.services.sales.agent_metrics_service import METRIC_GROUPS

        source = (ROOT / "staff_bot" / "handlers" / "sales" / "stats.py").read_text(encoding="utf-8")
        drawn, heading = {}, None
        for section, metric in re.findall(
            r"staff\.sales\.stats\.(?:section_(\w+)|metric_(\w+))", source
        ):
            if section:
                heading = section
                drawn[heading] = []
            elif heading is not None:
                drawn[heading].append(metric)

        assert {name: tuple(keys) for name, keys in drawn.items()} == {
            name: tuple(keys) for name, keys in METRIC_GROUPS.items()
        }
        assert list(drawn) == list(METRIC_GROUPS)


class TestTheAttachRefusalIsTranslated:
    """D25 rule 3's new code, `SALES_ATTACH_NO_ACCOUNT`.

    The operator meets it by tapping Attach on an outlet whose phone matches nothing, i.e. at
    the exact moment something has already gone sideways -- the worst moment to read a raw
    backend code. And because `/health` derives its required-key set from the error map's
    VALUES, an unseeded target parks the staff_bot container `unhealthy` on the next restart.
    """

    def test_the_attach_refusal_maps_to_a_seeded_key(self):
        key = BaseHandler.API_ERROR_CODE_KEY_MAP.get("SALES_ATTACH_NO_ACCOUNT")
        assert key == "staff.sales.error.attach_no_account"
        module = _load_seed_script()
        unseeded = [language for language in LANGUAGES if not module._curated_value(key, language)]
        assert not unseeded, f"attach refusal copy is missing in {unseeded}"
