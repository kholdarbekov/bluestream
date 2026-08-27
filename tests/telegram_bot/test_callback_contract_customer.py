"""Structural contracts over the customer bot's WHOLE callback wiring.

The sibling of ``tests/staff_bot/test_staff_wiring_contract.py``, aimed at the
other bot and at the other failure mode. The staff bot dies at reply-keyboard
LABELS; the customer bot dies at ``callback_data``: a button whose data no
registered pattern accepts renders perfectly, spins forever when tapped, and
never appears in a log line that says anything is wrong.

Every other module under ``tests/telegram_bot/`` walks ONE journey. This one
walks none. It builds the real :class:`telegram.ext.Application` out of
``WaterBusinessBot._setup_handlers()`` and asks four questions about the shape
of the whole thing:

1. can any button be STOLEN by an earlier-registered handler in the same
   dispatch scope? (:func:`test_no_callback_data_is_stolen_by_an_earlier_handler`)
2. does every conversation that can time out SAY so? (the ratchet)
3. is every ``callback_data`` ``keyboards.py`` can emit CLAIMED by something?
4. do the handler groups run in the order the comments in ``bot.py`` claim?

SCOPE IS THE WHOLE TRICK IN (1)
-------------------------------
Handlers shadow each other only when PTB offers them the same update: the
top-level handlers inside ONE group do, and so do the handlers listed under ONE
conversation state (or one conversation's entry_points, or its fallbacks). A
``cancel_address_creation`` fallback inside ``address_conversation`` never
competes with ``cancel_subscription_creation`` inside ``subscription_creation``,
because a customer is only ever inside one of them. Flatten those together and
every shared Cancel button becomes a false alarm, and the test gets deleted.

Collisions are also checked with CONCRETE STRINGS, never by comparing literal
prefixes. ``^cancel_order$`` only LOOKS like it shadows ``^cancel_order_\\d+``
until you notice the ``$``.

A sampler that cannot read a pattern SKIPS it, which would let (1) go blind one
handler at a time without ever turning red — so
``test_every_registered_pattern_is_readable_by_the_collision_check`` asserts the
skipped set is empty, and a second test proves the sampler really does expand
each branch instead of returning something vacuous.

KEYBOARD BUILDERS THIS FILE DELIBERATELY DOES NOT CALL
------------------------------------------------------
Check (3) is only as good as the set of builders it can invoke, so the gap is
written down here rather than left invisible:

* ``KeyboardBuilder.build_inline_keyboard`` / ``build_reply_keyboard`` and
  ``i18n_button`` — plumbing. They emit whatever their caller passes; there is
  no callback_data of their own to harvest.
* ``ProfileKeyboards.phone_request`` and ``ProfileKeyboards.location_request``
  — REPLY keyboards. They carry no callback_data at all (their taps arrive as
  text, and the address conversation matches them by localized regex; that
  wiring is covered by ``test_address_location_patterns.py``).
* ``ProductKeyboards._build_quantity_presets`` — returns integers, not buttons.
  (What it may return at all is decided by ``ProductHandlers._purchase_bounds``
  and driven end to end in ``test_cart_and_quantity_journeys.py``.)
* The ``quick_suggestions=`` argument of ``ProductKeyboards.product_categories``
  and ``product_list``. The rows come from
  ``QuickOrderHandlers.build_quick_suggestions``, an async method that needs an
  authenticated backend client and a real order history; hand-writing the dicts
  would only prove that a string this file typed matches a pattern this file
  read. The two callbacks it can emit are instead checked against the real
  dispatcher in
  ``test_the_quick_order_suggestion_buttons_are_wired_to_their_handlers``.

Everything else in ``telegram_bot/keyboards.py`` that can emit callback_data is
called below, with every branch its arguments select.
"""

from __future__ import annotations

import re

import pytest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
)

# Bot modules resolve by bare name; tests/telegram_bot/conftest.py ranks
# telegram_bot/ first on sys.path.
from config import config
from handlers.orders import _cancel_confirmation_callback
from handlers.subscriptions import (
    CONFIRM_SUBSCRIPTION,
    ITEM_SELECT_QUANTITY,
    SELECT_FREQUENCY,
)
from keyboards import (
    product_page_callback,
    LanguageKeyboards,
    MenuKeyboards,
    OrderKeyboards,
    PaymentKeyboards,
    ProductKeyboards,
    ProfileKeyboards,
    SubscriptionKeyboards,
)

from tests.telegram_bot.ptb_harness import build_bot_harness
# The debounce window is 2 REAL seconds. Ageing the guard's own lock table is
# what the wall clock would do, without putting two seconds into the suite —
# reused rather than re-typed so there is one expression of the trick.
from tests.telegram_bot.test_cart_and_quantity_journeys import expire_dedup_window

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
async def bot(monkeypatch):
    return await build_bot_harness(monkeypatch)


@pytest.fixture
def user(bot):
    return bot.updates()


# ---------------------------------------------------------------------------
# Walking the registration tree
# ---------------------------------------------------------------------------


def _callback_handlers(application):
    """Yield ``(scope, handler)`` for every CallbackQueryHandler, in dispatch
    order, tagged with the scope it actually competes in.

    See this module's docstring: a scope is one top-level group, or one
    conversation STATE, or one conversation's entry_points, or its fallbacks.
    Two handlers in different scopes are never offered the same update.
    """
    for group in sorted(application.handlers):
        for handler in application.handlers[group]:
            if isinstance(handler, CallbackQueryHandler):
                yield ("group", group), handler
            elif isinstance(handler, ConversationHandler):
                for state, state_handlers in handler.states.items():
                    for inner in state_handlers:
                        if isinstance(inner, CallbackQueryHandler):
                            yield ("state", handler.name, str(state)), inner
                for kind, members in (
                    ("entry", handler.entry_points),
                    ("fallback", handler.fallbacks),
                ):
                    for inner in members:
                        if isinstance(inner, CallbackQueryHandler):
                            yield (kind, handler.name), inner


def _conversations(application):
    for group in sorted(application.handlers):
        for handler in application.handlers[group]:
            if isinstance(handler, ConversationHandler):
                yield group, handler


def _compiled(handler) -> re.Pattern:
    return (
        handler.pattern
        if isinstance(handler.pattern, re.Pattern)
        else re.compile(handler.pattern)
    )


_SIMPLE_GROUP = re.compile(r"\(([^()]*)\)(\?)?")


def _expand_groups(body: str) -> list[str]:
    """Every branch of the ``(a|b|c)`` and ``(x)?`` groups in ``body``.

    The real registration table uses both shapes and nothing harder:
    ``^payment_(cash|card|payme|click|uzcard|humo|business_account)$``,
    ``^(pause|resume|cancel)_sub_``, ``^add_new_address(_checkout)?$``,
    ``^loyalty_history(_page_\\d+)?$``,
    ``^toggle_delivery_telegram_status_(on|off)$``.

    The EMPTY branch of a ``(...)?`` group is a real callback in its own right —
    ``add_new_address`` and ``add_new_address_checkout`` are different buttons on
    different screens, and only one of them can be shadowed at a time — so it is
    produced too. Anything more complicated is left carrying its metacharacters
    and dropped by the caller.
    """
    match = _SIMPLE_GROUP.search(body)
    if match is None:
        return [body]

    head, tail = body[: match.start()], body[match.end():]
    options = match.group(1).split("|")
    if match.group(2):
        options.append("")

    return [head + option + rest for option in options for rest in _expand_groups(tail)]


def _samples_matching(pattern_source: str) -> list[str]:
    """Concrete callback_data strings ``pattern_source`` accepts.

    PTB dispatches to the FIRST registered handler whose pattern matches a real
    string, so a real collision needs a real string. Comparing literal prefixes
    instead would report ``^cancel_order$`` as shadowing ``^cancel_order_\\d+``,
    which it does not.

    Returns ``[]`` for any pattern using a construct this cannot sample, so an
    unsupported pattern is SKIPPED rather than guessed at — a missed collision
    is a gap, a fabricated one gets the whole test deleted. That skipping is
    itself a silent hole, which is why
    :func:`test_every_registered_pattern_is_readable_by_the_collision_check`
    fails when the set of skipped patterns grows.
    """
    body = pattern_source
    if body.startswith("^"):
        body = body[1:]
    if body.endswith("$"):
        body = body[:-1]

    body = body.replace(r"\d+", "7").replace(r"\d*", "7").replace(r"\w+", "x")

    # An escaped paren or pipe would be misread as a group boundary below, and
    # a sample built from that misreading is a fabricated one.
    if re.search(r"\\[()|]", body):
        return []

    samples = []
    for candidate in _expand_groups(body):
        candidate = re.sub(r"\\(.)", r"\1", candidate)
        if re.search(r"[\[\](){}*+?|]", candidate):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_:.\-]+", candidate):
            continue
        samples.append(candidate)
    return samples


# ---------------------------------------------------------------------------
# (1) Nothing steals anybody else's button
# ---------------------------------------------------------------------------

# Duplicate patterns registered in ONE scope, where the second handler would
# therefore be dead code. EMPTY, and it must stay empty: recording one here
# buys a green bar by declaring a dead handler acceptable.
#
# It used to hold `sub_qty_` in item_management/ITEM_SELECT_QUANTITY, where the
# state listed `add_item_confirm` and then `update_item_confirm` behind the
# SAME pattern. `item_management` is the ADD-an-item flow (its only entry point
# is `^add_item_`, and its quantity step is reached from
# `add_item_select_quantity`, which sets `adding_product_id`);
# `update_item_confirm` belongs to the separate `update_item` conversation and
# needs `editing_item_id`, which the add flow never sets. The second
# registration was removed rather than given a pattern of its own — see
# test_the_add_and_update_item_flows_each_own_their_quantity_step below.
#
# The list may SHRINK, never grow.
_KNOWN_SHADOWED_CALLBACKS: set[tuple[str, str, str]] = set()


async def test_no_callback_data_is_stolen_by_an_earlier_handler(bot):
    """Two handlers whose patterns both accept the same callback_data: the one
    registered first wins, and the second button silently does the wrong thing —
    or, when it is the LAST handler for a screen, nothing at all.

    Checked with concrete strings against the real registration order, which is
    what PTB does at dispatch time.
    """
    registered = []
    for order, (scope, handler) in enumerate(_callback_handlers(bot.application)):
        if handler.pattern is None:
            continue
        compiled = _compiled(handler)
        registered.append(
            (scope, order, compiled, compiled.pattern, handler.callback.__qualname__)
        )

    thefts = set()
    for scope, order, compiled, source, name in registered:
        for sample in _samples_matching(source):
            if not compiled.match(sample):
                continue
            for other_scope, other_order, other_compiled, _other_source, _other_name in registered:
                if other_scope != scope or other_order >= order:
                    continue
                if other_compiled.match(sample):
                    thefts.add((sample, str(scope), name))
                    break

    new = thefts - _KNOWN_SHADOWED_CALLBACKS
    assert not new, (
        "callback data claimed by an earlier handler in the same scope:\n  "
        + "\n  ".join(sorted(f"{data!r} in {scope} never reaches {name}" for data, scope, name in new))
        + "\nRe-order the registrations (see the `^checkout` comment in "
        "telegram_bot/bot.py) rather than widening _KNOWN_SHADOWED_CALLBACKS."
    )

    healed = _KNOWN_SHADOWED_CALLBACKS - thefts
    assert not healed, (
        f"these shadowed handlers are now reachable: {sorted(healed)}. "
        "Strike them off _KNOWN_SHADOWED_CALLBACKS so the ratchet holds the "
        "new ground."
    )


# Registered patterns `_samples_matching` cannot build a concrete string for.
# Every entry is a handler the collision check above is BLIND to, so the set
# must stay empty; it exists as a named place for the failure rather than as an
# allowance. The list may never grow.
_PATTERNS_THE_COLLISION_CHECK_CANNOT_READ: set[str] = set()


async def test_every_registered_pattern_is_readable_by_the_collision_check(bot):
    """Without this, the collision check degrades silently instead of failing.

    `_samples_matching` skips any pattern whose shape it cannot turn into a real
    callback_data — deliberately, because a fabricated sample would report
    collisions that do not exist and get the whole check deleted. But a skipped
    pattern is an UNCHECKED handler, and skipping is invisible: add
    `^(a|b)_thing_(x|y)?$` tomorrow and the collision test still passes, having
    quietly stopped looking at it.

    So this asserts the sampler can read the whole table. If it fails, teach
    `_expand_groups` the new shape; adding the pattern to the allowlist buys a
    green bar by making the other test weaker.
    """
    unreadable = {
        _compiled(handler).pattern
        for _scope, handler in _callback_handlers(bot.application)
        if handler.pattern is not None and not _samples_matching(_compiled(handler).pattern)
    }

    new = unreadable - _PATTERNS_THE_COLLISION_CHECK_CANNOT_READ
    assert not new, (
        "these registered patterns are invisible to "
        "test_no_callback_data_is_stolen_by_an_earlier_handler:\n  "
        + "\n  ".join(sorted(new))
        + "\nTeach _expand_groups the shape rather than widening "
        "_PATTERNS_THE_COLLISION_CHECK_CANNOT_READ."
    )

    healed = _PATTERNS_THE_COLLISION_CHECK_CANNOT_READ - unreadable
    assert not healed, (
        f"the sampler can now read {sorted(healed)}. Strike them off so the "
        "ratchet holds the new ground."
    )


async def test_the_collision_check_samples_the_branches_it_claims_to(bot):
    """Evidence that the previous test is not passing on an empty table.

    A `_samples_matching` that returned one blank string for everything would
    satisfy "readable" while proving nothing. These are the five real
    alternation patterns in `bot.py`, each of which must yield the SEPARATE
    buttons a customer can actually tap — including `add_new_address`, which is
    the Profile screen's button, and `add_new_address_checkout`, which is the
    different one rendered mid-checkout.
    """
    assert set(_samples_matching("^add_new_address(_checkout)?$")) == {
        "add_new_address",
        "add_new_address_checkout",
    }
    assert set(_samples_matching("^toggle_delivery_telegram_status_(on|off)$")) == {
        "toggle_delivery_telegram_status_on",
        "toggle_delivery_telegram_status_off",
    }
    assert set(_samples_matching("^(pause|resume|cancel)_sub_")) == {
        "pause_sub_",
        "resume_sub_",
        "cancel_sub_",
    }
    assert set(_samples_matching(r"^loyalty_history(_page_\d+)?$")) == {
        "loyalty_history",
        "loyalty_history_page_7",
    }
    assert set(
        _samples_matching("^payment_(cash|card|payme|click|uzcard|humo|business_account)$")
    ) == {
        "payment_cash",
        "payment_card",
        "payment_payme",
        "payment_click",
        "payment_uzcard",
        "payment_humo",
        "payment_business_account",
    }

    # And a shape it genuinely cannot read still yields nothing, rather than a
    # plausible-looking string that would invent a collision.
    assert _samples_matching(r"^weird_[a-z]{2,3}_(x(y|z))?$") == []


# ---------------------------------------------------------------------------
# (2) A conversation that can expire must say so
# ---------------------------------------------------------------------------

# Customer conversations that time out with NO ConversationHandler.TIMEOUT
# handler. EMPTY, and it must stay empty: such a flow ends in silence — PTB
# looks for handlers under the TIMEOUT key, finds none, drops the conversation,
# and the customer is left on a prompt whose buttons are dead while the flow's
# keys survive in user_data for the next flow to trip over.
#
# `address_conversation` was the first one fixed — it lost 20 of 33 pinned
# addresses in production. `item_management`, `phone_verification`,
# `registration`, `subscription_creation` and `update_item` followed; the
# registration one mattered most, because 300s is well inside normal Uzbek SMS
# latency, so a customer waiting for their code was dropped mid-signup.
#
# This list may SHRINK, never grow.
_CONVERSATIONS_THAT_EXPIRE_IN_SILENCE: set[str] = set()


async def test_no_new_customer_conversation_expires_in_silence(bot):
    """Ratchet, not a red bar: the known-silent set may shrink, never grow.

    A conversation with `conversation_timeout` and no TIMEOUT state is a flow
    that abandons the customer without a word — the exact shape of the address
    bug. Adding a new one must fail here rather than in production.
    """
    silent = {
        handler.name
        for _group, handler in _conversations(bot.application)
        if handler.conversation_timeout
        and ConversationHandler.TIMEOUT not in handler.states
    }

    new = silent - _CONVERSATIONS_THAT_EXPIRE_IN_SILENCE
    assert not new, (
        f"new customer conversations that time out with no TIMEOUT handler: {sorted(new)}. "
        "They end without telling the customer anything and leave their flow "
        "keys in user_data. Register a TIMEOUT state (address_conversation in "
        "telegram_bot/bot.py is the worked example) rather than adding them here."
    )

    fixed = _CONVERSATIONS_THAT_EXPIRE_IN_SILENCE - silent
    assert not fixed, (
        f"these conversations now handle their timeout: {sorted(fixed)}. "
        "Remove them from _CONVERSATIONS_THAT_EXPIRE_IN_SILENCE so the ratchet "
        "keeps the new ground."
    )


async def test_the_address_conversation_answers_its_own_timeout(bot):
    """The flow that lost 20 of 33 addresses must keep its TIMEOUT state.

    `conversation_timeout` is not self-announcing: without handlers under the
    TIMEOUT key PTB ends the flow silently. Both an idle MESSAGE and an idle
    CALLBACK must be covered, because the synthetic timeout update carries
    whatever the last real one was — a customer who abandoned the flow on the
    Skip screen produces a callback-shaped timeout, and a MessageHandler-only
    registration would miss exactly that customer.
    """
    address = bot.conversation("address_conversation")

    assert address.conversation_timeout, "the flow is supposed to expire at all"
    timeout_handlers = address.states.get(ConversationHandler.TIMEOUT)
    assert timeout_handlers, (
        "address_conversation has no ConversationHandler.TIMEOUT state: it would "
        "expire in silence and strand address_flow_origin / temp_address_data "
        "in user_data"
    )

    kinds = {type(handler) for handler in timeout_handlers}
    assert MessageHandler in kinds, "an idle TEXT step would time out unhandled"
    assert CallbackQueryHandler in kinds, (
        "an idle SKIP-button step produces a callback-shaped timeout update and "
        "would time out unhandled"
    )


async def test_the_address_flow_gives_the_customer_a_full_day(bot):
    """The window is a product decision, so it is pinned rather than inferred.

    It was 600s until 2026-08-26. Manual entry is seven prompts long and
    customers put the phone down mid-way, so the same-afternoon expiry returned
    them to dead buttons.

    Pinned because the cost of the long window is invisible from bot.py: while
    this flow is open, `_consumes` stops every typed message at the step that
    asked for it, so nothing the customer types reaches the group-0 support
    catch-all. Shortening or lengthening it again should be a decision someone
    makes on purpose, not a number someone tunes in passing.

    Note this is NOT `utils.AWAITING_LOCATION_STALE_MINUTES`, which bounds a
    pin arriving with no conversation open (the zero-address checkout prompt)
    and is deliberately shorter — see test_address_location_entry.py.
    """
    address = bot.conversation("address_conversation")

    assert address.conversation_timeout == 86400, (
        f"address_conversation expires after {address.conversation_timeout}s, "
        "not the 24h the flow is meant to give a customer who walks away "
        "mid-form. Change the number in telegram_bot/bot.py and here together, "
        "and re-read what a longer window costs: the support-inbox catch-all "
        "sees nothing this customer types until the flow ends."
    )


async def test_every_expiring_conversation_covers_BOTH_shapes_of_timeout_update(bot):
    """A TIMEOUT state that registers only one handler kind is half a fix.

    The synthetic timeout update carries whatever the customer's LAST real one
    was, so a flow with both typed steps and inline steps needs a MessageHandler
    AND a CallbackQueryHandler. Registration is the clearest case: the language
    step is inline and the phone/OTP steps are typed, so a MessageHandler-only
    registration would still drop every customer who walked away at the
    language screen — silently, exactly as before.
    """
    for _group, handler in _conversations(bot.application):
        if not handler.conversation_timeout:
            continue
        timeout_handlers = handler.states.get(ConversationHandler.TIMEOUT) or []
        kinds = {type(inner) for inner in timeout_handlers}
        assert MessageHandler in kinds, (
            f"{handler.name}: a customer parked on a TYPED step expires unhandled"
        )
        assert CallbackQueryHandler in kinds, (
            f"{handler.name}: a customer parked on an INLINE step expires unhandled"
        )


async def test_a_timeout_handler_is_never_wrapped_in_the_dispatch_stopper(bot):
    """`_consumes` raises ApplicationHandlerStop, and PTB dispatches TIMEOUT
    handlers itself — it warns that the exception has no effect there.

    Cheap to get wrong (every neighbouring registration in bot.py is wrapped)
    and invisible when it is: the flow still ends, only a warning is logged.
    """
    for _group, handler in _conversations(bot.application):
        for inner in handler.states.get(ConversationHandler.TIMEOUT) or []:
            assert inner.callback.__name__ != "_stop_after_this_conversation", (
                f"{handler.name}'s TIMEOUT handler is wrapped in _consumes"
            )


# ---------------------------------------------------------------------------
# (3) Every button keyboards.py can render is claimed by something
# ---------------------------------------------------------------------------


def _emitted(source: str, markup) -> list[tuple[str, str]]:
    """``(where it came from, callback_data)`` for one rendered keyboard."""
    return [
        (source, button.callback_data)
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]


# Realistic domain rows. Shapes copied from what the backend actually returns —
# `full_address` is sliced to 30 chars by the builder, `created_at` to 10, and
# `get_product_display_price` reads `pricing.current_price` first.
_PRODUCTS = [
    {"id": 7, "name": "Aqua Element 19L", "pricing": {"current_price": 25000}},
    {"id": 8, "name": "Aqua Element 10L", "current_price": 16000},
]
_CATEGORIES = [{"id": 1, "name": "Suv"}, {"id": 2, "name": "Kulerlar"}]
_ADDRESSES = [
    {
        "id": 91,
        "title": "Uy",
        "full_address": "15, Chilonzor dahasi, Toshkent shahri",
        "is_default": True,
    },
    {
        "id": 92,
        "title": "Ish",
        "full_address": "1, Amir Temur ko'chasi, Toshkent",
        "is_default": False,
    },
]
_PAYMENT_METHODS = [
    {"type": "cash", "name": "Naqd"},
    {"type": "card", "name": "Karta"},
    {"type": "click", "name": "Click"},
    {"type": "payme", "name": "Payme"},
    {"type": "business_account", "name": "Hisob raqam"},
]
# `SubscriptionKeyboards.payment_methods` runs the raw backend payload through
# the real `payment_methods.build_payment_method_buttons`, so this is the
# BACKEND shape (`method` / `is_active`), not the button shape above.
_AVAILABLE_METHODS = [
    {"method": "cash", "is_active": True},
    {"method": "click", "is_active": True},
    {"method": "business_account", "is_active": True},
]


def _keyboard_callbacks(lang: str = "uz") -> list[tuple[str, str]]:
    """Every ``callback_data`` ``telegram_bot/keyboards.py`` can emit.

    Each builder is called once per branch its arguments select, because a
    button that only exists on one branch (Cancel on an unpaid order, Change
    address for a customer with several) is exactly the kind that gets wired
    late and forgotten. Builders this cannot call are listed in the module
    docstring.
    """
    out: list[tuple[str, str]] = []

    def add(source, markup):
        out.extend(_emitted(source, markup))

    # -- menus ---------------------------------------------------------------
    add("MenuKeyboards.main_menu(loyalty)", MenuKeyboards.main_menu(lang, show_loyalty=True))
    add("MenuKeyboards.main_menu(no loyalty)", MenuKeyboards.main_menu(lang, show_loyalty=False))
    add("MenuKeyboards.back_button", MenuKeyboards.back_button(lang))
    add("MenuKeyboards.cancel_button", MenuKeyboards.cancel_button(lang))
    # The ONLY production caller (handlers/orders.py::cancel_order) passes these
    # two explicitly; the builder's own defaults are never rendered. The strings
    # come from production's own helper rather than being typed here, because
    # they carry the order id — a literal copy would keep asserting that a
    # callback shape production no longer emits is wired.
    add(
        "MenuKeyboards.yes_no_buttons",
        MenuKeyboards.yes_no_buttons(
            lang,
            yes_callback=_cancel_confirmation_callback(42, "yes"),
            no_callback=_cancel_confirmation_callback(42, "no"),
        ),
    )

    # -- language ------------------------------------------------------------
    add("LanguageKeyboards.select_language", LanguageKeyboards.select_language())
    add("LanguageKeyboards.language_selection", LanguageKeyboards.language_selection(lang))

    # -- products ------------------------------------------------------------
    add("ProductKeyboards.product_categories", ProductKeyboards.product_categories(_CATEGORIES, lang))
    add(
        "ProductKeyboards.product_list(paginated)",
        ProductKeyboards.product_list(
            _PRODUCTS, page=2, total_pages=3, language=lang, category_id=1
        ),
    )
    # The single-category screen renders the SAME row with a different Back
    # target riding on it, so both shapes of the paging callback are harvested.
    add(
        "ProductKeyboards.product_list(paginated, single category)",
        ProductKeyboards.product_list(
            _PRODUCTS, page=2, total_pages=3, language=lang,
            single_category=True, category_id=1,
        ),
    )
    add(
        "ProductKeyboards.product_list(single category)",
        ProductKeyboards.product_list(
            _PRODUCTS, page=1, total_pages=1, language=lang, single_category=True
        ),
    )
    add(
        "ProductKeyboards.product_details(in category)",
        ProductKeyboards.product_details(7, category_id=1, language=lang),
    )
    add(
        "ProductKeyboards.product_details(no category)",
        ProductKeyboards.product_details(7, category_id=None, language=lang),
    )
    add(
        "ProductKeyboards.product_list_for_subscription",
        ProductKeyboards.product_list_for_subscription(_PRODUCTS, lang),
    )
    add(
        "ProductKeyboards.quantity_selector",
        ProductKeyboards.quantity_selector(
            7, current_quantity=3, language=lang, min_order_qty=2, max_quantity=40
        ),
    )

    # -- cart / checkout -----------------------------------------------------
    add("OrderKeyboards.cart_actions(empty)", OrderKeyboards.cart_actions(lang, cart_is_empty=True))
    add(
        "OrderKeyboards.cart_actions(ready)",
        OrderKeyboards.cart_actions(lang, cart_is_empty=False, meets_minimum=True),
    )
    add(
        "OrderKeyboards.cart_actions(below minimum)",
        OrderKeyboards.cart_actions(lang, cart_is_empty=False, meets_minimum=False),
    )
    edit_items = [{"product": {"id": 7, "name": "Aqua Element 19L"}, "quantity": 2}]
    add(
        "OrderKeyboards.cart_actions(edit from cart)",
        OrderKeyboards.cart_actions(
            lang, cart_is_empty=False, edit_mode=True, cart_items=edit_items
        ),
    )
    add(
        "OrderKeyboards.cart_actions(edit from order confirm)",
        OrderKeyboards.cart_actions(
            lang,
            cart_is_empty=False,
            edit_mode=True,
            cart_items=edit_items,
            edit_return="order_confirm",
        ),
    )
    add("OrderKeyboards.delivery_addresses", OrderKeyboards.delivery_addresses(_ADDRESSES, lang))
    add(
        "OrderKeyboards.single_address_confirm(only address)",
        OrderKeyboards.single_address_confirm(_ADDRESSES[0], lang),
    )
    add(
        "OrderKeyboards.single_address_confirm(quick order)",
        OrderKeyboards.single_address_confirm(
            _ADDRESSES[0], lang, back_callback="menu_products", show_change=True
        ),
    )
    add("OrderKeyboards.payment_methods", OrderKeyboards.payment_methods(_PAYMENT_METHODS, lang))
    for meets_minimum in (True, False):
        for has_reward in (True, False):
            for show_reward in (True, False):
                add(
                    f"OrderKeyboards.order_confirmation(min={meets_minimum},"
                    f"reward={has_reward},show={show_reward})",
                    OrderKeyboards.order_confirmation(
                        lang,
                        meets_minimum=meets_minimum,
                        has_reward=has_reward,
                        show_reward=show_reward,
                    ),
                )
    add(
        "OrderKeyboards.checkout_reward_picker",
        OrderKeyboards.checkout_reward_picker(
            [{"id": 4, "name": "Bepul 19L", "points_cost": 300}], lang
        ),
    )
    add(
        "OrderKeyboards.order_list",
        OrderKeyboards.order_list(
            [
                {
                    "id": 51,
                    "order_number": "BS-51",
                    "status": "delivered",
                    "created_at": "2026-08-19T10:00:00",
                }
            ],
            lang,
        ),
    )
    for status in ("pending", "confirmed", "preparing", "out_for_delivery", "delivered", "cancelled"):
        add(
            f"OrderKeyboards.order_details({status})",
            OrderKeyboards.order_details(51, status, lang),
        )
    add("OrderKeyboards.order_tracking", OrderKeyboards.order_tracking(51, lang))
    add("OrderKeyboards.asl_belgisi_error", OrderKeyboards.asl_belgisi_error(lang))

    # -- subscriptions -------------------------------------------------------
    add("SubscriptionKeyboards.subscription_frequency", SubscriptionKeyboards.subscription_frequency(lang))
    add(
        "SubscriptionKeyboards.subscription_list",
        SubscriptionKeyboards.subscription_list(
            [{"id": 12, "status": "active", "name": "Haftalik", "delivery_frequency": "weekly"}],
            lang,
        ),
    )
    for status in ("active", "paused", "cancelled"):
        add(
            f"SubscriptionKeyboards.subscription_actions({status})",
            SubscriptionKeyboards.subscription_actions(12, status, lang),
        )
    add(
        "SubscriptionKeyboards.subscription_creation_options",
        SubscriptionKeyboards.subscription_creation_options(lang),
    )
    add("SubscriptionKeyboards.quantity_selector", SubscriptionKeyboards.quantity_selector(lang))
    add(
        "SubscriptionKeyboards.payment_methods",
        SubscriptionKeyboards.payment_methods(_AVAILABLE_METHODS, lang),
    )
    add(
        "SubscriptionKeyboards.item_management_menu",
        SubscriptionKeyboards.item_management_menu(
            12, [{"id": 33, "product": {"name": "Aqua Element 19L"}, "quantity": 2}], lang
        ),
    )
    add(
        "SubscriptionKeyboards.edit_subscription_menu",
        SubscriptionKeyboards.edit_subscription_menu(12, lang),
    )

    # -- profile / addresses -------------------------------------------------
    for verified in (True, False):
        add(
            f"ProfileKeyboards.profile_menu(verified={verified})",
            ProfileKeyboards.profile_menu(lang, phone_verified=verified),
        )
    add("ProfileKeyboards.profile_edit_menu", ProfileKeyboards.profile_edit_menu(lang))
    for enabled in (True, False):
        add(
            f"ProfileKeyboards.notification_settings(enabled={enabled})",
            ProfileKeyboards.notification_settings(
                lang, delivery_telegram_status_updates_enabled=enabled
            ),
        )
    add(
        "ProfileKeyboards.addresses_management",
        ProfileKeyboards.addresses_management(_ADDRESSES, lang),
    )
    add("ProfileKeyboards.empty_addresses", ProfileKeyboards.empty_addresses(lang))
    add("ProfileKeyboards.region_selection", ProfileKeyboards.region_selection(lang))
    add(
        "ProfileKeyboards.district_selection",
        ProfileKeyboards.district_selection(
            [{"key": "chilonzor", "name": "Chilonzor"}, {"key": "yunusobod", "name": "Yunusobod"}],
            lang,
        ),
    )
    # The exact field names production passes (handlers/profile.py::_ADDRESS_STEPS),
    # plus 'street' whose Skip button the street step deliberately does not render
    # but whose handler is registered as a safety net.
    for field in ("street", "building", "apartment", "floor"):
        add(
            f"ProfileKeyboards.optional_field_keyboard({field})",
            ProfileKeyboards.optional_field_keyboard(field, lang),
        )
    for show_edit in (True, False):
        add(
            f"ProfileKeyboards.geocode_confirmation(show_edit={show_edit})",
            ProfileKeyboards.geocode_confirmation(lang, show_edit=show_edit),
        )
    add("ProfileKeyboards.address_title_suggestions", ProfileKeyboards.address_title_suggestions(lang))
    for is_default in (True, False):
        add(
            f"ProfileKeyboards.address_view_actions(default={is_default})",
            ProfileKeyboards.address_view_actions(91, is_default, lang),
        )
    add(
        "ProfileKeyboards.delivery_instructions_keyboard",
        ProfileKeyboards.delivery_instructions_keyboard(lang),
    )

    # -- payments ------------------------------------------------------------
    add("PaymentKeyboards.payment_success", PaymentKeyboards.payment_success(51, lang))
    add("PaymentKeyboards.payment_failed", PaymentKeyboards.payment_failed(51, lang))
    add("PaymentKeyboards.payment_link", PaymentKeyboards.payment_link("https://pay.example/1", lang))

    return out


# callback_data the customer bot RENDERS and no registered pattern accepts.
# Every entry here is a button that shows a spinner and then nothing.
#
# `page_1` / `page_3` are GONE because the pagination row is wired: the buttons
#   now carry the category they page within (`keyboards.product_page_callback`)
#   and `ProductHandlers.product_page_handler` re-renders the list from them —
#   see test_the_product_list_pagination_buttons_re_render_the_list below and
#   tests/telegram_bot/test_product_list_pagination.py for the journey.
#
# `timeslot_3` / `back_to_address` are GONE because the builder that rendered
#   them is gone: `OrderKeyboards.delivery_time_slots` had no caller in the
#   whole repository, and deleting it beat wiring a feature nobody asked for.
#
# `back_to_address_selection` is GONE because it is now wired: it is claimed
#   inside `subscription_creation`'s CONFIRM_SUBSCRIPTION state and takes the
#   customer back to the address list — see
#   test_the_subscription_payment_back_button_returns_to_the_address_step.
#
# This list may SHRINK, never grow.
_KNOWN_UNCLAIMED_CALLBACKS: set[str] = set()


async def test_every_keyboard_callback_is_claimed_by_a_registered_handler(bot):
    """A rendered button whose callback_data matches no pattern is a dead button.

    The customer taps it, Telegram shows the spinner until it times out, and
    nothing is logged as an error — the single quietest way a Telegram flow
    dies. Checked against every pattern registered ANYWHERE (top level,
    conversation entry points, states and fallbacks), because "claimed by
    something" is the weakest useful claim and the one a keyboard builder can
    be held to without knowing which state its caller renders it from.
    """
    patterns = [
        (_compiled(handler), _compiled(handler).pattern, str(scope))
        for scope, handler in _callback_handlers(bot.application)
        if handler.pattern is not None
    ]
    assert patterns, "no callback patterns registered at all — the bot is unwired"

    unclaimed = {}
    for source, data in _keyboard_callbacks():
        if any(compiled.match(data) for compiled, _src, _scope in patterns):
            continue
        unclaimed.setdefault(data, source)

    new = set(unclaimed) - _KNOWN_UNCLAIMED_CALLBACKS
    assert not new, (
        "keyboards.py renders callback_data no handler claims:\n  "
        + "\n  ".join(sorted(f"{data!r} from {unclaimed[data]}" for data in new))
        + "\nRegister a handler for it — do not add it to "
        "_KNOWN_UNCLAIMED_CALLBACKS."
    )

    healed = _KNOWN_UNCLAIMED_CALLBACKS - set(unclaimed)
    assert not healed, (
        f"these callbacks are now handled: {sorted(healed)}. Strike them off "
        "_KNOWN_UNCLAIMED_CALLBACKS so the ratchet holds the new ground."
    )


async def test_callback_data_is_identical_in_every_supported_language(bot):
    """A translated word inside callback_data would wire the button in one
    language and break it in the other two — and the broken ones are exactly the
    ones this project's own tests are least likely to drive.

    ``ProfileKeyboards.region_selection`` and ``address_title_suggestions``
    already carry per-language LABELS beside a shared callback; this is the
    contract that keeps that split where it belongs.
    """
    languages = list(config.localization.supported_languages)
    assert len(languages) > 1, "the bot is supposed to be multilingual"

    by_language = {
        language: {data for _source, data in _keyboard_callbacks(language)}
        for language in languages
    }

    baseline_language = languages[0]
    baseline = by_language[baseline_language]
    for language in languages[1:]:
        assert by_language[language] == baseline, (
            f"callback_data differs between {baseline_language!r} and {language!r}: "
            f"only in {baseline_language}: {sorted(baseline - by_language[language])}; "
            f"only in {language}: {sorted(by_language[language] - baseline)}"
        )


async def test_an_impatient_double_tap_never_reaches_the_conversation(bot, user):
    """Group numbers are a claim; this is the evidence.

    The customer taps "Add new address", nothing visibly happens for a second
    because the handler is still talking to the backend, and they tap again.
    The dedup guard at group -5 must swallow the second tap BEFORE the
    conversation at group -2 sees it — otherwise `allow_reentry=True` restarts
    the flow, the customer gets two prompts, and the second one is anchored to a
    message the first tap already replaced.

    The window is 2 real seconds, so a DELIBERATE re-tap is proven by ageing the
    lock table rather than by sleeping.
    """
    bot.telegram.reset()

    await bot.send(user.tap("add_new_address"))
    after_first = len(bot.telegram.shown)
    assert after_first, "the first tap showed the customer nothing"
    assert bot.conversation_state("address_conversation") is not None, (
        "the first tap did not enter address_conversation at all"
    )

    await bot.send(user.tap("add_new_address"))
    assert len(bot.telegram.shown) == after_first, (
        "the impatient second tap re-entered the conversation and pushed another "
        "prompt; the dedup middleware is no longer ahead of group -2"
    )

    # Same button, same customer, but seconds later — a real change of mind.
    expire_dedup_window()
    await bot.send(user.tap("add_new_address"))
    assert len(bot.telegram.shown) > after_first, (
        "a deliberate re-tap after the debounce window must still work; a guard "
        "that never releases is a dead button"
    )


async def test_the_product_list_pagination_buttons_re_render_the_list(bot, user):
    """Was a RATCHET: two rendered buttons that no pattern claimed.

    ``ProductKeyboards.product_list`` renders Previous/Next whenever the backend
    reports more than one page, and ``ProductHandlers`` feeds it the real
    ``meta.pages`` (six products per page), so every category with more than six
    products shipped them to every customer. Nothing matched ``^page_``: the tap
    reached no handler, no ``answerCallbackQuery`` was sent, and the spinner
    stopped only when Telegram gave up.

    WHAT THE FIX GUARANTEES, and what this pins: the row addresses itself —
    each button carries the CATEGORY as well as the page
    (``keyboards.product_page_callback``), because bot memory is empty after
    every deploy — a registered pattern claims it, and the tap is answered.

    The journey behind it (which page the backend is asked for, what the
    customer ends up looking at, and a card that outlived a restart) is driven
    in ``tests/telegram_bot/test_product_list_pagination.py``.
    """
    rendered = ProductKeyboards.product_list(
        _PRODUCTS, page=2, total_pages=3, language="uz", category_id=4
    )
    pagination = [
        button.callback_data
        for row in rendered.inline_keyboard
        for button in row
        if (button.callback_data or "").startswith("page_")
    ]
    assert pagination == [product_page_callback(4, 1), product_page_callback(4, 3)], (
        "the pagination row changed shape; re-derive this from the builder "
        "rather than editing the expectation"
    )

    for data in pagination:
        bot.telegram.reset()
        assert bot.handlers_matching(user.tap(data)), (
            f"{data!r} reaches no handler — the pagination buttons are dead "
            "again, and a dead button is silent"
        )

        await bot.send(user.tap(data))
        assert bot.telegram.of("answerCallbackQuery"), (
            f"{data!r} was never answered; the customer's spinner only stops "
            "when Telegram gives up"
        )


async def test_the_subscription_quantity_back_button_is_owned_by_the_subscription_flow(bot, user):
    """Was a RATCHET: a Back button claimed by the WRONG handler.

    ``SubscriptionKeyboards.quantity_selector`` (rendered by
    ``SubscriptionHandlers._show_quantity_selector``) emits
    ``back_to_product_selection``. No conversation state claimed it, so it fell
    through to the group-0 ``^back_to_product_`` handler —
    ``ProductHandlers.product_details`` — which does
    ``int(query.data.split('_')[3])`` and got ``int('selection')``. "Claimed by
    some handler" was therefore not the same as "wired".

    WHAT THE FIX GUARANTEES: the group-0 pattern says what it means
    (``^back_to_product_\\d+$``, a product id and nothing else), and the two
    subscription flows that render this keyboard claim the button themselves,
    inside the state the customer is parked in.

    Pins the routing, not the error text, so an improved error message does not
    turn this red for the wrong reason.
    """
    rendered = SubscriptionKeyboards.quantity_selector("uz")
    backs = [
        button.callback_data
        for row in rendered.inline_keyboard
        for button in row
        if button.callback_data == "back_to_product_selection"
    ]
    assert backs, "the subscription quantity keyboard lost its Back button"

    owners_by_scope = {
        scope: handler.callback.__qualname__
        for scope, handler in _callback_handlers(bot.application)
        if handler.pattern is not None
        and _compiled(handler).match("back_to_product_selection")
    }

    assert owners_by_scope == {
        ("state", "subscription_creation", str(SELECT_FREQUENCY)):
            "SubscriptionHandlers.add_more_items",
        ("state", "item_management", str(ITEM_SELECT_QUANTITY)):
            "SubscriptionHandlers.add_item_back_to_products",
    }, (
        f"routing changed: {owners_by_scope}. The product handler must no "
        "longer be reachable from this button, and each subscription state "
        "that renders the quantity keyboard must claim it."
    )


async def test_the_product_back_button_pattern_only_accepts_a_product_id(bot, user):
    """The root cause, asserted where it lives.

    ``^back_to_product_`` was a prefix claim over a namespace the product menu
    does not own. ``ProductHandlers.product_details`` reads segment 3 as an
    integer, so the pattern is only honest as ``^back_to_product_\\d+$`` — and
    once it says that, a same-prefix button from another feature can never be
    stolen again, with or without a handler of its own.
    """
    owners = {
        handler.callback.__qualname__
        for scope, handler in _callback_handlers(bot.application)
        if scope == ("group", 0)
        and handler.pattern is not None
        and _compiled(handler).match("back_to_product_7")
    }
    assert "ProductHandlers.product_details" in owners, (
        "the real Back-to-product button (a numeric product id) lost its handler"
    )

    thieves = {
        handler.callback.__qualname__
        for scope, handler in _callback_handlers(bot.application)
        if scope == ("group", 0)
        and handler.pattern is not None
        and _compiled(handler).match("back_to_product_selection")
    }
    assert not thieves, (
        f"a group-0 handler still claims a non-numeric back_to_product_*: {thieves}. "
        "It would run AFTER the conversation handler (PTB walks every group) and "
        "blow up on int('selection')."
    )


async def test_the_subscription_payment_back_button_returns_to_the_address_step(bot, user):
    """``SubscriptionKeyboards.payment_methods`` renders Back as
    ``back_to_address_selection`` and it used to land nowhere: a customer part
    way through subscription checkout tapped Back and stayed on the payment
    screen forever.

    It is claimed inside CONFIRM_SUBSCRIPTION — the state `select_payment`
    leaves the customer in — and nowhere else, because that is the only screen
    that renders it.
    """
    rendered = SubscriptionKeyboards.payment_methods(_AVAILABLE_METHODS, "uz")
    backs = [
        button.callback_data
        for row in rendered.inline_keyboard
        for button in row
        if button.callback_data == "back_to_address_selection"
    ]
    assert backs, "the subscription payment keyboard lost its Back button"

    owners_by_scope = {
        scope: handler.callback.__qualname__
        for scope, handler in _callback_handlers(bot.application)
        if handler.pattern is not None
        and _compiled(handler).match("back_to_address_selection")
    }
    assert owners_by_scope == {
        ("state", "subscription_creation", str(CONFIRM_SUBSCRIPTION)):
            "SubscriptionHandlers.back_to_address_selection",
    }, f"back_to_address_selection is routed to {owners_by_scope}"


async def test_the_add_and_update_item_flows_each_own_their_quantity_step(bot, user):
    """Both flows show the same quantity keyboard, so both must claim
    ``sub_qty_`` — but each in its OWN conversation, and exactly once.

    `item_management` used to list `add_item_confirm` and then
    `update_item_confirm` behind the identical pattern. PTB stops at the first
    match, so the second was dead — and it was also wrong for that flow:
    `item_management` is entered only through `^add_item_`, and
    `update_item_confirm` needs `editing_item_id`, which the add flow never
    sets. The update path has its own conversation, which registers it once.
    """
    def owners(conversation_name, state):
        handler = bot.conversation(conversation_name)
        return [
            inner.callback.__qualname__
            for inner in handler.states[state]
            if isinstance(inner, CallbackQueryHandler)
            and _compiled(inner).match("sub_qty_3")
        ]

    assert owners("item_management", ITEM_SELECT_QUANTITY) == [
        "SubscriptionHandlers.add_item_confirm"
    ]
    assert owners("update_item", ITEM_SELECT_QUANTITY) == [
        "SubscriptionHandlers.update_item_confirm"
    ]


async def test_the_quick_order_suggestion_buttons_are_wired_to_their_handlers(bot, user):
    """The two Quick Order rows are the only keyboard content this file cannot
    build (see the module docstring), so their wiring is checked directly.

    They sit at the TOP of the products menu, above every category — the most
    tapped real estate in the bot. If either loses its handler the customer's
    first tap after opening Products does nothing.
    """
    for data, expected in (
        ("quick_repeat_last", "QuickOrderHandlers.handle_repeat_last"),
        ("quick_usual", "QuickOrderHandlers.handle_usual"),
        ("reorder_51", "QuickOrderHandlers.handle_reorder_from_history"),
    ):
        matched = bot.handlers_matching(user.tap(data))
        owners = {handler.callback.__qualname__ for _group, handler in matched}
        assert expected in owners, (
            f"{data!r} is not routed to {expected}; it reaches {sorted(owners) or 'nothing'}"
        )


# ---------------------------------------------------------------------------
# (4) Group order
# ---------------------------------------------------------------------------


async def test_middlewares_run_before_conversations_which_run_before_the_catch_all(bot):
    """The dispatch order the comments in bot.py promise, asserted.

    PTB runs at most one handler PER GROUP, in ascending group order. So:
    the debug logger and the callback-dedup guard must sit in groups strictly
    below every conversation (a duplicate that reached a conversation first
    would be processed twice), and every conversation must sit strictly below
    group 0 (the group-0 text catch-all files unmatched free text as a support
    ticket, so a conversation's own TEXT step landing after it would have the
    customer's answer silently turned into a support message).
    """
    groups = bot.application.handlers

    debug_logger_group = min(groups)
    middleware_groups = sorted(
        group
        for group, handlers in groups.items()
        for handler in handlers
        if isinstance(handler, TypeHandler)
    )
    assert len(middleware_groups) == 2, (
        f"expected exactly the two dispatcher middlewares, found groups "
        f"{middleware_groups}"
    )
    assert middleware_groups[0] == debug_logger_group == -10
    assert middleware_groups[1] == -5, "the callback-dedup guard moved out of group -5"

    conversation_groups = sorted({group for group, _ in _conversations(bot.application)})
    assert conversation_groups, "no conversations registered at all"
    assert min(conversation_groups) > middleware_groups[-1], (
        "a conversation now runs at or before the callback-dedup middleware, so "
        "a double-tap would be processed before it can be dropped"
    )
    assert max(conversation_groups) < 0, (
        "a conversation slipped into group 0 and now competes with the callback "
        "handlers and the text catch-all"
    )


async def test_the_free_text_catch_all_is_the_last_handler_in_group_zero(bot):
    """The group-0 text catch-all silently files free text as a support ticket.

    Anything registered AFTER it in group 0 is dead, because PTB stops at the
    first matching handler in a group. Anything that needs plain text and is
    registered BEFORE it still works — which is why the assertion is about
    position, not about existence.
    """
    group_zero = bot.application.handlers[0]

    catch_alls = [
        index
        for index, handler in enumerate(group_zero)
        if isinstance(handler, MessageHandler)
        and getattr(handler.callback, "__name__", "") == "_handle_text_message"
    ]
    assert len(catch_alls) == 1, (
        f"expected exactly one free-text catch-all in group 0, found {len(catch_alls)}"
    )

    trailing = group_zero[catch_alls[0] + 1:]
    # `_handle_attachment_message` is exempt for the same reason the voice
    # handler used to be: its filter (PHOTO/Document/VIDEO/VIDEO_NOTE/AUDIO/
    # VOICE — LOCATION/VENUE are routed entirely by the address conversation's
    # entry point instead) cannot match a text update, so sitting behind the
    # text catch-all costs it nothing.
    non_attachment = [
        handler
        for handler in trailing
        if getattr(handler.callback, "__name__", "") != "_handle_attachment_message"
    ]
    assert not non_attachment, (
        "handlers registered after the group-0 text catch-all can never run for "
        f"a text update: {[type(h).__name__ for h in non_attachment]}"
    )


async def test_both_dispatcher_middlewares_claim_every_update(bot, user):
    """The two middlewares were once registered in `initialize()` instead of
    `_setup_handlers()`, which meant anything building the app from
    `_setup_handlers()` alone — this harness included — ran with NO dedup guard
    and could not see a double-tap regression at all.

    `include_catch_alls=True` is the one place that flag is correct: here the
    middlewares ARE the subject.
    """
    for update in (user.tap("menu_profile"), user.text("salom"), user.command("menu")):
        claimed = bot.handlers_matching(update, include_catch_alls=True)
        type_handlers = [
            handler for _group, handler in claimed if isinstance(handler, TypeHandler)
        ]
        assert len(type_handlers) == 2, (
            f"{update} is seen by {len(type_handlers)} middleware(s); both the "
            "debug logger and the callback-dedup guard must see every update"
        )


async def test_the_slash_commands_reach_their_handlers(bot, user):
    """/start must enter registration; the other three must not be swallowed by
    it. Driven through real dispatch, including the `bot_command` entity a
    hand-rolled text update omits — without which CommandHandler never matches
    and the whole assertion is vacuous."""
    start = bot.handlers_matching(user.command("start"))
    assert any(
        isinstance(handler, ConversationHandler) and handler.name == "registration"
        for _group, handler in start
    ), "/start must enter the registration conversation"

    for command, expected in (
        ("menu", "main_menu_handler"),
        # `support_handlers` is a SimpleHandlers instance (handlers/__init__.py),
        # NOT the SupportHandlers class in handlers/support.py — worth pinning,
        # because the two have same-named methods and the wrong one is a plausible
        # future mis-wire.
        ("help", "SimpleHandlers.help_handler"),
        ("language", "LanguageHandler.language_menu"),
    ):
        matched = bot.handlers_matching(user.command(command))
        owners = {
            handler.callback.__qualname__
            for _group, handler in matched
            if isinstance(handler, CommandHandler)
        }
        assert expected in owners, (
            f"/{command} reaches {sorted(owners) or 'no CommandHandler'}, not {expected}"
        )


# ---------------------------------------------------------------------------
# What a stale or mistyped button does
# ---------------------------------------------------------------------------


async def test_a_button_from_a_deleted_feature_does_not_crash_the_dispatcher(bot, user):
    """Old messages stay tappable forever in Telegram, so callbacks for removed
    features keep arriving years later. They must degrade quietly.

    An error handler is attached here because the harness has none: production
    registers `error_handler` in `initialize()`, not in `_setup_handlers()`, so
    without this a raised exception would be swallowed by PTB and the assertion
    would pass on a bot that is actually crashing.
    """
    errors = []
    bot.application.add_error_handler(
        lambda update, context: errors.append(context.error) or None
    )

    await bot.send(user.tap("legacy_feature_removed_in_2024"))

    assert not errors, f"an unknown callback raised {errors!r}"
    assert not bot.telegram.shown, (
        "an unknown callback should not push a message at the customer"
    )


async def test_a_stale_button_carrying_a_corrupted_id_does_not_crash_the_dispatcher(bot, user):
    """The nastier half of the same problem: data that MATCHES a live pattern
    but cannot be parsed.

    ``^order_`` and ``^address_`` both end in an integer id that the handler
    parses with a bare ``int()``. A truncated or hand-edited callback reaches
    the handler and blows up inside it. That must stay contained in the
    handler's own error path — an exception escaping to the dispatcher takes the
    whole update down and leaves the spinner running forever.
    """
    errors = []
    bot.application.add_error_handler(
        lambda update, context: errors.append((context.error, update)) or None
    )

    for data in ("order_notanumber", "address_"):
        matched = bot.handlers_matching(user.tap(data))
        assert matched, f"{data!r} no longer reaches any handler at all"
        await bot.send(user.tap(data))

    assert not errors, (
        "an exception escaped a handler and reached the dispatcher: "
        f"{[repr(error) for error, _update in errors]}"
    )


async def test_every_button_on_the_real_main_menu_is_answered_by_a_handler(bot, user):
    """The screen every customer sees first, checked as it is actually rendered
    rather than as the builder is called: the bot decides at runtime whether the
    loyalty button belongs, so the rendered keyboard is the only honest source."""
    await bot.send(user.tap("back_to_main"))

    shown = bot.telegram.last_shown()
    data = shown.callback_data()
    assert data, "the main menu rendered with no buttons at all"

    for callback in data:
        assert bot.handlers_matching(user.tap(callback)), (
            f"the main-menu button {callback!r} lands nowhere"
        )
