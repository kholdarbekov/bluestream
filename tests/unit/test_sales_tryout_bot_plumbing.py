"""The try-out conversation's plumbing: buttons, stages, error map, copy, flow key.

The sibling of `tests/unit/test_sales_visit_bot_plumbing.py`, and it exists for
the same three reasons a static guard elsewhere cannot cover this:

  * the quantity prefix must be LITERAL. `_materialize_literal` in
    tests/unit/test_staff_bot_routing_regressions.py rewrites every `{...}` to
    `1`, so an interpolated prefix would materialise as `staff_sales_t_1_1` --
    a literal no registered pattern accepts.
  * the seed script CANNOT import `staff_bot` (it runs inside the business_app
    container, which has no `staff_bot/` tree), so the copy is checked through
    its own `_curated_value` rather than pasted here.
  * `staff.sales.tryout.*` is drawn as literals, which the /health extractor
    does see -- but nothing checks the PLACEHOLDER sets, and a stray `{...}`
    reaches the agent as a literal brace.
"""

import asyncio
import importlib.util
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.handlers.base import BaseHandler
from staff_bot.handlers.sales.tryout import tryout_eligible
from staff_bot.keyboards.sales import (
    TRYOUT_QTY_CHOICES,
    TRYOUT_STAGES,
    SalesKeyboards,
)
from staff_bot.utils.flow_state import PENDING_FLOW_USER_DATA_KEYS

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
LANGUAGES = ("en", "uz", "ru")

# The patterns Task 7 registers in staff_bot/bot.py, verbatim from the plan's
# shared interfaces, plus the two this conversation BORROWS: the receipt and
# the no-phone screen both land on `staff_sales_outlet_<id>` (the hub's group-0
# handler, reachable because both paths END the conversation first), and every
# screen carries the `staff_back_to_main` cancel the fallbacks answer.
# Shapes are restricted to `^literal$`, `^prefix_\d+$` and `^prefix_\d+_\d+$`
# because tests/staff_bot/test_staff_wiring_contract.py::_sample_matching
# cannot read anything richer.
TRYOUT_PATTERNS = (
    r"^staff_sales_tryout_\d+$",
    r"^staff_sales_t_p_\d+$",
    r"^staff_sales_t_qty_\d+_\d+$",
    r"^staff_sales_t_done$",
    r"^staff_sales_t_skip$",
    r"^staff_sales_t_back$",
    r"^staff_sales_t_confirm$",
    r"^staff_sales_outlet_\d+$",
    r"^staff_back_to_main$",
)

# Literals this conversation DRAWS but does not own: the outlet card's own
# buttons, registered in group 0 by phase 2a and Task 6. The receipt is that
# card (Task 5's 201 carries it, so the agent lands on a screen that already
# says `trial`), and the conversation has ENDED by the time any of them can be
# tapped — which is exactly why they may be borrowed rather than re-registered.
# They are checked separately from TRYOUT_PATTERNS so that "every pattern this
# task registers is really drawn" keeps meaning what it says.
BORROWED_CARD_PATTERNS = (
    r"^staff_sales_activate_\d+$",
    r"^staff_sales_visit_start_\d+$",
    r"^staff_sales_visit_resume$",
    r"^staff_sales_hub$",
)

# `tryout_products` rows: `{product_id, product_name, quantity}`. A quantity of
# 0 is "not chosen", which is what *Continue* gates on.
TRYOUT_ROWS = [
    {"product_id": 11, "product_name": "Pure Water 19L", "quantity": 2},
    {"product_id": 12, "product_name": "Pure Water 10L", "quantity": 0},
]

TRYOUT_SUFFIXES = (
    "tryout.button", "tryout.choose_products", "tryout.quantity_prompt",
    "tryout.done", "tryout.notes_prompt", "tryout.confirm_title",
    "tryout.confirm", "tryout.created", "tryout.handoff_queued",
    "tryout.no_products", "tryout.no_phone", "tryout.open_outlet",
    "error.tryout_items",
)

# Ruling 19: every other suffix is a BARE LABEL and the handler composes the
# product name and the quantity around it.
INTERPOLATING = {"tryout.created": {"tryout_number"}}

TRYOUT_ERROR_CODE_KEYS = {
    # The try-out upserts its trial contact BY PHONE, so an outlet with no
    # contact phone cannot have one. The sentence already exists -- the
    # activation path refuses for the same reason -- so no new row.
    "SALES_TRYOUT_PHONE_REQUIRED": "staff.sales.error.phone_required",
    "SALES_TRYOUT_ITEMS_INVALID": "staff.sales.error.tryout_items",
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


def _every_tryout_keyboard():
    """Every keyboard the try-out conversation can draw, both branches of each."""
    return [
        ("products", SalesKeyboards.tryout_products("en", TRYOUT_ROWS)),
        ("products_nothing_chosen", SalesKeyboards.tryout_products(
            "en", [{"product_id": 11, "product_name": "Pure Water 19L", "quantity": 0}])),
        ("quantity", SalesKeyboards.tryout_quantity("en", 11, 2)),
        ("quantity_unset", SalesKeyboards.tryout_quantity("en", 12, 0)),
        ("notes", SalesKeyboards.tryout_notes("en")),
        ("confirm", SalesKeyboards.tryout_confirm("en")),
        ("outlet_link", SalesKeyboards.tryout_outlet_link("en", 5)),
        ("card", SalesKeyboards.outlet_card(
            "en", {"id": 5, "stage": "prospect", "outlet_type": "grocery_store"})),
        # The RECEIPT: Task 5's 201 carries the moved card, so the last screen
        # of this conversation is an outlet card at `trial` — drawn here, so a
        # button on it can never be one nothing routes.
        ("receipt_card", SalesKeyboards.outlet_card(
            "en", {"id": 5, "stage": "trial", "outlet_type": "grocery_store"})),
    ]


class TestEveryTryoutButtonIsRoutable:
    def test_every_callback_matches_a_pattern_task_7_registers(self):
        unmatched = []
        for name, markup in _every_tryout_keyboard():
            for data in _callbacks(markup):
                if not any(re.match(pattern, data)
                           for pattern in TRYOUT_PATTERNS + BORROWED_CARD_PATTERNS):
                    unmatched.append(f"{name}: {data}")
        assert not unmatched, (
            "these try-out buttons have no registered handler shape, so a tap on them "
            f"would fall through to the catch-all router: {unmatched}"
        )

    def test_every_registered_pattern_is_actually_drawn(self):
        """A pattern nothing emits is dead wiring that hides a rename."""
        drawn = [data for _, markup in _every_tryout_keyboard() for data in _callbacks(markup)]
        unused = [
            pattern for pattern in TRYOUT_PATTERNS
            if not any(re.match(pattern, data) for data in drawn)
        ]
        assert not unused, f"no keyboard draws a button for: {unused}"

    def test_the_quantity_prefix_is_literal_not_interpolated(self):
        """The routing guard rewrites `{...}` to `1`; a variable prefix goes blind."""
        source = (ROOT / "staff_bot" / "keyboards" / "sales.py").read_text(encoding="utf-8")
        assert 'staff_sales_t_qty_{product_id}_{step}' in source
        assert 'staff_sales_t_p_{product_id}' in source
        assert 'staff_sales_t_{' not in source


class TestTheCardOffersATryoutWhereItMeansSomething:
    def test_only_the_pipeline_stages_get_the_button(self):
        """One definition of "a try-out is the next move", used by the CARD only.

        `active` is deliberately absent: a shop that already orders is not a
        prospect to convert, and `lost` has been written off. The backend
        states the rule again (prospect|activation_requested -> trial, an
        already-trial outlet keeps its stage) and that duplication is the same
        one VISITABLE_STAGES documents: this tuple decides what is OFFERED and
        the backend is what refuses.
        """
        from business_app.models.sales import OUTLET_STAGES

        offered = set()
        # The BACKEND's tuple, not a hand-copy: a stage added there must either
        # be offered a try-out on purpose or refused on purpose, and a local
        # copy would quietly stop asking the question about it.
        for stage in OUTLET_STAGES:
            card = SalesKeyboards.outlet_card(
                "en", {"id": 5, "stage": stage, "outlet_type": "grocery_store"})
            if "staff_sales_tryout_5" in _callbacks(card):
                offered.add(stage)
        assert offered == set(TRYOUT_STAGES)
        assert "active" not in offered and "lost" not in offered


class TestTheQuantityGrid:
    def test_the_grid_marks_the_current_value_and_offers_every_step(self):
        markup = SalesKeyboards.tryout_quantity("en", 11, 2)
        assert [d for d in _callbacks(markup) if d.startswith("staff_sales_t_qty_")] == [
            f"staff_sales_t_qty_11_{step}" for step in TRYOUT_QTY_CHOICES
        ]
        assert "• 2" in _labels(markup)

    def test_zero_is_on_the_grid_because_it_is_how_a_line_is_removed(self):
        assert TRYOUT_QTY_CHOICES[0] == 0

    def test_continue_appears_only_once_something_is_chosen(self):
        chosen = SalesKeyboards.tryout_products("en", TRYOUT_ROWS)
        empty = SalesKeyboards.tryout_products(
            "en", [{"product_id": 11, "product_name": "Pure Water 19L", "quantity": 0}])
        assert "staff_sales_t_done" in _callbacks(chosen)
        assert "staff_sales_t_done" not in _callbacks(empty)

    def test_a_chosen_line_prints_its_quantity_and_an_unchosen_one_prints_nothing(self):
        labels = _labels(SalesKeyboards.tryout_products("en", TRYOUT_ROWS))
        assert "Pure Water 19L · 2" in labels
        assert "Pure Water 10L" in labels


class TestTheTryoutErrorCodesAreTranslated:
    def test_every_code_maps_to_the_planned_key(self):
        for code, key in TRYOUT_ERROR_CODE_KEYS.items():
            assert BaseHandler.API_ERROR_CODE_KEY_MAP.get(code) == key, (
                f"{code} must map to {key} or the agent reads a raw backend code"
            )

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_every_mapped_key_is_seeded(self, language):
        module = _load_seed_script()
        unseeded = sorted(
            {key for key in TRYOUT_ERROR_CODE_KEYS.values()
             if not module._curated_value(key, language)}
        )
        assert not unseeded, f"try-out error copy is missing in {language}: {unseeded}"


class TestTheTryoutCopyIsSeededAndBare:
    @pytest.mark.parametrize("language", LANGUAGES)
    def test_every_tryout_suffix_is_seeded(self, language):
        module = _load_seed_script()
        missing = sorted(
            suffix for suffix in TRYOUT_SUFFIXES
            if not module._curated_value(f"staff.sales.{suffix}", language)
        )
        assert not missing, f"unseeded staff.sales.* copy in {language}: {missing}"

    def test_only_the_documented_suffixes_interpolate(self):
        module = _load_seed_script()
        wrong = {}
        for suffix in TRYOUT_SUFFIXES:
            expected = INTERPOLATING.get(suffix, set())
            for language in LANGUAGES:
                value = module.SALES_TEXT_TRANSLATIONS[suffix][language]
                found = set(re.findall(r"\{(\w+)\}", value))
                if found != expected:
                    wrong[f"{suffix}[{language}]"] = sorted(found)
        assert not wrong, f"placeholder sets do not match the plan: {wrong}"


class TestTheTryoutFlowKeyIsCleared:
    def test_sales_tryout_is_a_documented_pending_flow(self):
        """WRITE-side rule: the conversation stamps `user_data['sales_tryout']`,
        so a menu tap must drop it -- otherwise the agent's next text lands in
        a basket they already walked out of."""
        assert "sales_tryout" in PENDING_FLOW_USER_DATA_KEYS


class TestTheEligibilityPredicateReadsThePublishedFields:
    def test_both_flags_live_under_their_serializer_section(self):
        """`serialize_product` nests them: `flags.is_active` and
        `inventory.is_tryout_eligible`. Read at the top level they are always
        absent, and a `.get(key, True)` default then passes EVERY product."""
        lendable = {"id": 1, "flags": {"is_active": True},
                    "inventory": {"is_tryout_eligible": True}}
        assert tryout_eligible(lendable) is True
        assert tryout_eligible({**lendable, "flags": {"is_active": False}}) is False
        assert tryout_eligible(
            {**lendable, "inventory": {"is_tryout_eligible": False}}) is False
        # A row that carries the keys only at the TOP level is not eligible-by-
        # accident: the predicate never looks there.
        assert tryout_eligible({"id": 1, "is_tryout_eligible": False}) is True

    def test_the_drivers_tryout_screen_asks_the_same_question(self):
        """One bot-side expression of "which SKU may be lent", not two.

        `TryoutHandler._fetch_tryout_products` (the DRIVER's flow) reads
        `is_active` / `is_tryout_eligible` at the TOP level, where
        `serialize_product` never puts them: both reads miss, the `.get(...,
        True)` default wins, and that filter passes the entire catalogue --
        drivers are offered SKUs `TryoutService._validate_and_build_items` then
        refuses. Step 7c points it at this predicate, and this is the assertion
        that keeps it pointed there.
        """
        from staff_bot.handlers import tryouts as driver_tryouts

        source = (ROOT / "staff_bot" / "handlers" / "tryouts.py").read_text(encoding="utf-8")
        assert "tryout_eligible" in source
        assert "is_tryout_eligible', True" not in source
        assert 'is_tryout_eligible", True' not in source
        assert driver_tryouts is not None  # imported for the same reason the source is read

    def test_the_driver_is_really_offered_only_the_lendable_sku(self):
        """The BEHAVIOURAL gate on the same repoint, because the grep above is not one.

        Neither driver characterization suite reaches
        `TryoutHandler._fetch_tryout_products` (both drive `TryoutKeyboards`
        directly), so a source check is the whole of what stood behind the
        change -- and a source check passes for any body that merely mentions
        the name.

        This drives the method with the shape `serialize_product` really
        publishes: both flags NESTED. Against the old top-level filter
        (`product.get('is_active') is not False and
        product.get('is_tryout_eligible', True)`) both reads miss, the `True`
        default wins, and BOTH rows come back -- so this assertion is red on
        the code that shipped before the repoint, which is exactly what makes
        it a pin rather than a restatement.
        """
        from staff_bot.handlers import tryouts as driver_tryouts

        catalogue = {"items": [
            {"id": 11, "name": "Pure Water 19L",
             "flags": {"is_active": True}, "inventory": {"is_tryout_eligible": True}},
            {"id": 13, "name": "Ice Tea 1L",
             "flags": {"is_active": True}, "inventory": {"is_tryout_eligible": False}},
        ]}

        class _Client:
            """`async with api_client as client` -> an object with `get_products`."""

            def __init__(self):
                self.client = MagicMock()
                self.client.get_products = AsyncMock(
                    return_value=MagicMock(success=True, data=catalogue)
                )

            async def __aenter__(self):
                return self.client

            async def __aexit__(self, *args):
                return False

        handler = driver_tryouts.TryoutHandler()
        original = driver_tryouts.api_client
        driver_tryouts.api_client = _Client()
        try:
            _response, eligible = asyncio.run(handler._fetch_tryout_products("tok"))
        finally:
            driver_tryouts.api_client = original

        assert [product["id"] for product in eligible] == [11], (
            "the ineligible SKU reached the driver's screen, so the filter is "
            "reading the flags where serialize_product does not put them"
        )
