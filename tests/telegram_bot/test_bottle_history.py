"""Bot: My-bottles restructure + paginated ledger history view.

Rendering reworked per live-testing feedback:
- dd.mm.yyyy dates everywhere
- no running-balance ``(📊 N)`` fragment
- one line per order (delivery + return_on_delivery grouped by order_id)
- non-order rows: ``{label} ({date}): {qty}``
- single 📜 title (emoji comes from the seeded string, not a hardcoded prefix)

The balances endpoint serves the place-aware OVERVIEW dict
(``{is_linked, balances: [...]}``) instead of a bare list, with one row per
distinct PLACE — the address group when grouped, else the address. A row's
number is ``place_balance``; there is deliberately NO ``balance``, no
``place_union_balance`` and no server-side ``cluster_total_balance`` (a shared
place's pool has no per-member slice, so summing it per member would count the
same bottles once per coworker — the bot sums ``place_balance`` across the
already-deduped rows itself). ``place_members`` rows carry NAMES ONLY.

The history endpoint serves the PLACE ledger for grouped addresses (404 instead
of an empty 200 for a foreign address) — see
``tests/unit/test_customer_bot_bottles_place.py`` for the pure-formatter tests.

Every balance fixture below is built by ``overview_balance_row`` /
``overview_payload`` in ``tests/telegram_bot/helpers.py`` — the ONE fabrication
point for this payload, shared with the pure-formatter module and pinned against
the real service by
``test_fabricated_overview_matches_the_real_customer_overview``. A literal row
written inline here is invisible to that guard, which is precisely how this file
stayed green while the live screen showed 0 for every customer.
"""

from unittest.mock import AsyncMock

import pytest

from api_client import APIResponse
from handlers import bottles as bottles_module
from tests.telegram_bot.helpers import (
    DummyCallbackQuery,
    DummyUpdate,
    make_context,
    overview_balance_row as _balance_row,
    overview_payload,
    overview_place_member,
)


# All eight BottleLedgerEventType values (shared/enums.py:230-239).
ALL_EVENT_TYPES = [
    "delivery",
    "return_on_delivery",
    "standalone_collection",
    "admin_adjustment",
    "fine_issued",
    "fine_reversed",
    "fine_paid",
    "initial_balance",
]

# U+FF0B FULLWIDTH PLUS / U+2212 MINUS SIGN — the signed-adjustment glyphs.
PLUS = "＋"
MINUS = "−"


class _FakeBottleClient:
    """async-context-manager fake exposing the two customer bottle endpoints."""

    def __init__(self, *, balances=None, ledger=None):
        self._balances = balances
        self._ledger = ledger

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_my_bottle_balances(self, _token):
        return self._balances

    async def get_my_bottle_ledger(self, _token, _address_id, page=1, per_page=10):
        return self._ledger


def _patch_common(monkeypatch, labels=None):
    monkeypatch.setattr(bottles_module, "user_middleware", AsyncMock(return_value={"id": 1001}))
    monkeypatch.setattr(bottles_module, "get_auth_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(bottles_module.i18n, "get_user_language", AsyncMock(return_value="en"))
    # Echo the key verbatim so tests assert exact i18n keys / event labels, unless
    # `labels` maps a key to a human string (used to assert human-facing formatting).
    # Keys called WITH kwargs also echo the interpolated values: the real
    # `i18n.get` returns a humanised last key segment on a miss and then silently
    # DROPS every kwarg (telegram_bot/i18n.py:80-93), which would hide member
    # names and balances from these assertions in an unseeded test process.
    mapping = labels or {}

    def _get(key, language=None, *args, **kwargs):
        if key in mapping:
            template = mapping[key]
            if not args and not kwargs:
                return template
            try:
                return template.format(*args, **kwargs)
            except (KeyError, IndexError, ValueError):
                return template
        return " ".join([key] + [str(v) for v in kwargs.values()])

    monkeypatch.setattr(bottles_module.i18n, "get", _get)


def _callback_datas(markup):
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


def _last_edit_kwargs(query):
    return query.edit_message_text.call_args.kwargs


def _ledger_response(items, *, total, page, per_page=10):
    return APIResponse(
        success=True,
        data={"data": {"items": items, "total": total, "page": page, "per_page": per_page}},
    )


async def _run_history(monkeypatch, ledger, *, data="bottle_history_5_1", labels=None):
    _patch_common(monkeypatch, labels=labels)
    monkeypatch.setattr(bottles_module, "api_client", _FakeBottleClient(ledger=ledger))
    handler = bottles_module.BottleBalanceHandler()
    update = DummyUpdate()
    update.callback_query = DummyCallbackQuery(data=data)
    await handler.show_bottle_history(update, make_context())
    return update.callback_query


def _balances_response(rows, *, is_linked=False):
    """The overview envelope, wrapped in the API envelope the bot unwraps.

    ``_balance_row`` / ``overview_payload`` live in ``tests/telegram_bot/helpers``
    so this module and ``tests/unit/test_customer_bot_bottles_place.py`` share ONE
    fabrication point, which
    ``test_fabricated_overview_matches_the_real_customer_overview`` pins against
    the real ``get_customer_bottle_overview`` payload. Do not inline a literal
    row here — the guard cannot see it.
    """
    return APIResponse(success=True, data={"data": overview_payload(rows, is_linked=is_linked)})


async def _run_balances(monkeypatch, balances, *, labels=None):
    _patch_common(monkeypatch, labels=labels)
    monkeypatch.setattr(bottles_module, "api_client", _FakeBottleClient(balances=balances))
    handler = bottles_module.BottleBalanceHandler()
    update = DummyUpdate()
    update.callback_query = DummyCallbackQuery(data="my_bottles")
    await handler.show_bottle_balance(update, make_context())
    return update.callback_query


# --------------------------------------------------------------------------- #
# Balance screen — solo customer (unlinked + ungrouped baseline)
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.anyio
async def test_zero_balance_address_listed_with_history_button(monkeypatch):
    query = await _run_balances(monkeypatch, _balances_response([
        _balance_row(5, "Home", 0),
        _balance_row(6, "Work", 3),
    ]))

    kwargs = _last_edit_kwargs(query)
    datas = _callback_datas(kwargs["reply_markup"])
    assert "bottle_history_5_1" in datas
    assert "bottle_history_6_1" in datas
    assert "Home" in kwargs["text"]
    assert "Work" in kwargs["text"]


@pytest.mark.unit
@pytest.mark.anyio
async def test_solo_customer_screen_has_no_place_or_cluster_lines(monkeypatch):
    """Global regression baseline: an unlinked + ungrouped customer sees exactly
    what they saw before linking — one plain line per address, nothing else."""
    query = await _run_balances(monkeypatch, _balances_response([
        _balance_row(5, "Home", 4),
    ]))
    kwargs = _last_edit_kwargs(query)
    text = kwargs["text"]

    assert "• Home: <b>4</b>" in text
    for key in ("place_total", "member_line", "cluster_total", "linked_account_line"):
        assert f"telegram.bottles.{key}" not in text
    # Exactly one History button (own address) + Back.
    assert _callback_datas(kwargs["reply_markup"]) == ["bottle_history_5_1", "back_to_main"]
    assert "Alice Member" not in text


@pytest.mark.unit
@pytest.mark.anyio
async def test_empty_bottles_shows_empty_state(monkeypatch):
    query = await _run_balances(monkeypatch, _balances_response([]))

    kwargs = _last_edit_kwargs(query)
    assert "telegram.bottles.no_balance" in kwargs["text"]
    assert _callback_datas(kwargs["reply_markup"]) == ["back_to_main"]


@pytest.mark.unit
@pytest.mark.anyio
async def test_non_dict_balances_payload_falls_back_to_empty_state(monkeypatch):
    """Defensive: a stale bot talking to a pre-Task-5 backend (bare list) must
    show the empty state, not crash the whole My-bottles screen."""
    query = await _run_balances(
        monkeypatch,
        APIResponse(success=True, data={"data": [{"address_id": 5, "balance": 2}]}),
    )
    kwargs = _last_edit_kwargs(query)
    assert "telegram.bottles.no_balance" in kwargs["text"]
    assert _callback_datas(kwargs["reply_markup"]) == ["back_to_main"]


@pytest.mark.unit
@pytest.mark.anyio
async def test_balance_screen_html_escapes_address_title(monkeypatch):
    # The balance screen is sent with parse_mode='HTML'; an unescaped title
    # containing '<' or '&' would make Telegram reject the whole message.
    query = await _run_balances(monkeypatch, _balances_response([
        _balance_row(5, "Home <3 & Co", 2),
    ]))

    text = _last_edit_kwargs(query)["text"]
    assert "Home &lt;3 &amp; Co" in text
    # The raw, unescaped title must NOT leak into the HTML-parsed message body.
    assert "Home <3 & Co" not in text


# --------------------------------------------------------------------------- #
# Balance screen — grouped / linked customer
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.anyio
async def test_grouped_linked_customer_sees_place_total_named_members_and_cluster(monkeypatch):
    """The full linked screen: the grouped place's total + its named member list,
    the sibling account's place listed and labelled, and a cluster total.

    Every assertion is line-exact rather than substring, because the pre-re-key
    renderings ("• Office: <b>2</b>", "member_line Alice Member 2",
    "cluster_total 0") are substrings of, or otherwise confusable with, the
    correct output — a loose `in` here is what let this file stay green while the
    live screen showed 0 for every customer."""
    query = await _run_balances(
        monkeypatch,
        _balances_response(
            [
                _balance_row(
                    5, "Office", 5,
                    is_grouped=True, place_group_id=7,
                    place_members=[
                        overview_place_member("Alice Member", is_own=True),
                        overview_place_member("Bob Coworker"),
                    ],
                ),
                _balance_row(
                    9, "Home", 4,
                    is_own=False, owner_user_id=12, owner_name="AliceTwo Member",
                ),
            ],
            is_linked=True,
        ),
    )

    kwargs = _last_edit_kwargs(query)
    text = kwargs["text"]
    lines = text.splitlines()

    # D6: a grouped place prints its number ONCE, on the place_total line — the
    # row above it carries the label alone.
    assert "• Office" in lines
    assert not any(line.startswith("• Office:") for line in lines)
    assert [line.strip() for line in lines if "telegram.bottles.place_total" in line] == [
        "telegram.bottles.place_total 5"
    ]

    # Member breakdown is NAMES ONLY — no per-member number exists any more.
    assert [line.strip() for line in lines if "telegram.bottles.member_line" in line] == [
        "telegram.bottles.member_line Alice Member",
        "telegram.bottles.member_line Bob Coworker",
    ]

    # The sibling account's place is listed, attributed to its owner, and — being
    # ungrouped — keeps its own number on the row line.
    assert (
        "• telegram.bottles.linked_account_line Home AliceTwo Member: <b>4</b>" in lines
    )

    # Cluster footer = client-side sum of place_balance across the deduped rows.
    assert [line for line in lines if "telegram.bottles.cluster_total" in line] == [
        "telegram.bottles.cluster_total 9"
    ]

    # Both places are browsable; the sibling's button is disambiguated.
    datas = _callback_datas(kwargs["reply_markup"])
    assert datas == ["bottle_history_5_1", "bottle_history_9_1", "back_to_main"]
    labels = [
        btn.text for row in kwargs["reply_markup"].inline_keyboard for btn in row
    ]
    assert any("AliceTwo Member" in label for label in labels)


@pytest.mark.unit
@pytest.mark.anyio
async def test_grouped_screen_escapes_member_and_owner_names(monkeypatch):
    query = await _run_balances(
        monkeypatch,
        _balances_response(
            [_balance_row(
                5, "Office", 1,
                is_own=False, owner_name="Bob <b>",
                is_grouped=True, place_group_id=7,
                place_members=[overview_place_member("Eve <i>")],
            )],
            is_linked=True,
        ),
    )
    text = _last_edit_kwargs(query)["text"]
    assert "Bob &lt;b&gt;" in text and "Bob <b>" not in text
    assert "Eve &lt;i&gt;" in text and "Eve <i>" not in text


# --------------------------------------------------------------------------- #
# Order-grouped ledger lines
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.anyio
async def test_order_group_single_line_matches_exact_example(monkeypatch):
    # User's live-testing example, byte-for-byte:
    #   delivery + return_on_delivery for one order collapse to ONE line,
    #   Collected (return) fragment first, Delivered second, dd.mm.yyyy date.
    items = [
        {"event_type": "delivery", "quantity": 2, "balance_after": 5,
         "order_id": 77, "order_number": "TG_000077_26",
         "occurred_at": "2026-07-12T10:00:00+00:00"},
        {"event_type": "return_on_delivery", "quantity": -2, "balance_after": 3,
         "order_id": 77, "order_number": "TG_000077_26",
         "occurred_at": "2026-07-12T10:00:00+00:00"},
    ]
    query = await _run_history(
        monkeypatch, _ledger_response(items, total=2, page=1),
        labels={
            "telegram.bottles.event.return_on_delivery": "Olindi",
            "telegram.bottles.event.delivery": "Yetkazildi",
        },
    )
    text = _last_edit_kwargs(query)["text"]
    order_lines = [l for l in text.splitlines() if l.startswith("#TG_000077_26")]
    assert len(order_lines) == 1, order_lines
    assert order_lines[0] == "#TG_000077_26 (12.07.2026): Olindi: 2, Yetkazildi: 2"


@pytest.mark.unit
@pytest.mark.anyio
async def test_delivery_only_order_renders_single_fragment(monkeypatch):
    items = [
        {"event_type": "delivery", "quantity": 4, "balance_after": 5,
         "order_id": 42, "order_number": "88",
         "occurred_at": "2026-07-05T10:00:00+00:00"},
    ]
    query = await _run_history(monkeypatch, _ledger_response(items, total=1, page=1))
    lines = _last_edit_kwargs(query)["text"].splitlines()
    order_lines = [l for l in lines if l.startswith("#88")]
    assert len(order_lines) == 1
    # delivery label key echoed by _patch_common; no return fragment present.
    assert order_lines[0] == "#88 (05.07.2026): telegram.bottles.event.delivery: 4"
    assert "return_on_delivery" not in order_lines[0]


@pytest.mark.unit
@pytest.mark.anyio
async def test_return_only_order_renders_single_fragment(monkeypatch):
    items = [
        {"event_type": "return_on_delivery", "quantity": -3, "balance_after": 2,
         "order_id": 91, "order_number": "99",
         "occurred_at": "2026-07-05T10:00:00+00:00"},
    ]
    query = await _run_history(monkeypatch, _ledger_response(items, total=1, page=1))
    order_lines = [l for l in _last_edit_kwargs(query)["text"].splitlines() if l.startswith("#99")]
    assert len(order_lines) == 1
    # abs() of the negative return quantity, no sign glyph.
    assert order_lines[0] == "#99 (05.07.2026): telegram.bottles.event.return_on_delivery: 3"
    assert MINUS not in order_lines[0]
    assert PLUS not in order_lines[0]


@pytest.mark.unit
@pytest.mark.anyio
async def test_order_group_collapses_non_adjacent_rows(monkeypatch):
    # Same order_id split by an unrelated row in between → still ONE order line,
    # positioned at the group's first occurrence (the delivery row here).
    items = [
        {"event_type": "delivery", "quantity": 2, "balance_after": 5,
         "order_id": 77, "order_number": "70", "occurred_at": "2026-07-12T10:00:00+00:00"},
        {"event_type": "standalone_collection", "quantity": 1, "balance_after": 6,
         "order_id": None, "order_number": None, "occurred_at": "2026-07-11T10:00:00+00:00"},
        {"event_type": "return_on_delivery", "quantity": -2, "balance_after": 3,
         "order_id": 77, "order_number": "70", "occurred_at": "2026-07-12T10:00:00+00:00"},
    ]
    query = await _run_history(monkeypatch, _ledger_response(items, total=3, page=1))
    lines = [l for l in _last_edit_kwargs(query)["text"].splitlines() if l.startswith("#70")]
    assert len(lines) == 1
    assert lines[0] == (
        "#70 (12.07.2026): "
        "telegram.bottles.event.return_on_delivery: 2, "
        "telegram.bottles.event.delivery: 2"
    )


@pytest.mark.unit
@pytest.mark.anyio
async def test_history_line_html_escapes_order_number(monkeypatch):
    # order_number is user-influenced free text rendered into an HTML message;
    # angle brackets must be escaped so Telegram doesn't reject the message.
    items = [
        {"event_type": "delivery", "quantity": 4, "balance_after": 5,
         "order_id": 7, "order_number": "<x>", "occurred_at": "2026-07-05T10:00:00+00:00"},
    ]
    query = await _run_history(monkeypatch, _ledger_response(items, total=1, page=1))
    line = next(
        l for l in _last_edit_kwargs(query)["text"].splitlines() if l.startswith("#")
    )
    assert "#&lt;x&gt;" in line
    assert "#<x>" not in line


# --------------------------------------------------------------------------- #
# Non-order ledger lines
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.anyio
async def test_standalone_and_initial_render_unsigned(monkeypatch):
    items = [
        {"event_type": "standalone_collection", "quantity": 3, "balance_after": 8,
         "order_id": None, "order_number": None, "occurred_at": "2026-05-07T10:00:00+00:00"},
        {"event_type": "initial_balance", "quantity": 5, "balance_after": 5,
         "order_id": None, "order_number": None, "occurred_at": "2026-04-13T10:00:00+00:00"},
    ]
    query = await _run_history(monkeypatch, _ledger_response(items, total=2, page=1))
    lines = _last_edit_kwargs(query)["text"].splitlines()
    standalone = next(l for l in lines if "standalone_collection" in l)
    initial = next(l for l in lines if "initial_balance" in l)
    assert standalone == "telegram.bottles.event.standalone_collection (07.05.2026): 3"
    assert initial == "telegram.bottles.event.initial_balance (13.04.2026): 5"
    assert PLUS not in standalone and MINUS not in standalone
    assert PLUS not in initial and MINUS not in initial


@pytest.mark.unit
@pytest.mark.anyio
async def test_admin_adjustment_renders_signed_minus(monkeypatch):
    items = [
        {"event_type": "admin_adjustment", "quantity": -2, "balance_after": 3,
         "order_id": None, "order_number": None, "occurred_at": "2026-07-12T10:00:00+00:00"},
    ]
    query = await _run_history(monkeypatch, _ledger_response(items, total=1, page=1))
    line = next(l for l in _last_edit_kwargs(query)["text"].splitlines() if "admin_adjustment" in l)
    assert line == f"telegram.bottles.event.admin_adjustment (12.07.2026): {MINUS}2"


@pytest.mark.unit
@pytest.mark.anyio
async def test_admin_adjustment_renders_signed_plus(monkeypatch):
    items = [
        {"event_type": "admin_adjustment", "quantity": 2, "balance_after": 7,
         "order_id": None, "order_number": None, "occurred_at": "2026-07-12T10:00:00+00:00"},
    ]
    query = await _run_history(monkeypatch, _ledger_response(items, total=1, page=1))
    line = next(l for l in _last_edit_kwargs(query)["text"].splitlines() if "admin_adjustment" in l)
    assert line == f"telegram.bottles.event.admin_adjustment (12.07.2026): {PLUS}2"


@pytest.mark.unit
@pytest.mark.anyio
async def test_fine_rows_render_label_and_date_only(monkeypatch):
    items = [
        {"event_type": "fine_issued", "quantity": 0, "balance_after": 5,
         "order_id": None, "order_number": None, "occurred_at": "2026-07-12T10:00:00+00:00"},
        {"event_type": "fine_reversed", "quantity": 0, "balance_after": 5,
         "order_id": None, "order_number": None, "occurred_at": "2026-07-12T10:00:00+00:00"},
    ]
    query = await _run_history(monkeypatch, _ledger_response(items, total=2, page=1))
    lines = _last_edit_kwargs(query)["text"].splitlines()
    issued = next(l for l in lines if "fine_issued" in l)
    reversed_ = next(l for l in lines if "fine_reversed" in l)
    assert issued == "telegram.bottles.event.fine_issued (12.07.2026)"
    assert reversed_ == "telegram.bottles.event.fine_reversed (12.07.2026)"
    # No quantity / no ':' separator on 0-qty fine rows.
    assert ": " not in issued.split(")", 1)[-1]
    assert PLUS not in issued and MINUS not in issued


@pytest.mark.unit
@pytest.mark.anyio
async def test_delivery_without_order_id_falls_back_to_unsigned(monkeypatch):
    # Defensive: an event that should carry order_id but doesn't renders like a
    # standalone non-order row (label + date + unsigned qty), NOT a #order line.
    items = [
        {"event_type": "delivery", "quantity": 4, "balance_after": 5,
         "order_id": None, "order_number": "1234", "occurred_at": "2026-07-05T10:00:00+00:00"},
    ]
    query = await _run_history(monkeypatch, _ledger_response(items, total=1, page=1))
    lines = _last_edit_kwargs(query)["text"].splitlines()
    assert not any(l.startswith("#") for l in lines)
    delivery = next(l for l in lines if "telegram.bottles.event.delivery" in l)
    assert delivery == "telegram.bottles.event.delivery (05.07.2026): 4"
    assert PLUS not in delivery and MINUS not in delivery


# --------------------------------------------------------------------------- #
# Format-wide invariants
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.anyio
async def test_no_balance_fragment_and_year_present(monkeypatch):
    items = [
        {"event_type": "delivery", "quantity": 2, "balance_after": 999, "order_id": 77,
         "order_number": "70", "occurred_at": "2026-07-12T10:00:00+00:00"},
        {"event_type": "return_on_delivery", "quantity": -2, "balance_after": 999, "order_id": 77,
         "order_number": "70", "occurred_at": "2026-07-12T10:00:00+00:00"},
        {"event_type": "standalone_collection", "quantity": 3, "balance_after": 999,
         "order_id": None, "order_number": None, "occurred_at": "2026-05-07T10:00:00+00:00"},
    ]
    query = await _run_history(monkeypatch, _ledger_response(items, total=3, page=1))
    text = _last_edit_kwargs(query)["text"]
    # Running-balance chart glyph and the balance_after value are both gone.
    assert "📊" not in text
    assert "999" not in text
    # dd.mm.yyyy: the year is present on every rendered date.
    assert "12.07.2026" in text
    assert "07.05.2026" in text


@pytest.mark.unit
@pytest.mark.anyio
async def test_history_renders_all_eight_event_labels(monkeypatch):
    items = []
    for i, et in enumerate(ALL_EVENT_TYPES):
        qty = 0 if et in ("fine_issued", "fine_reversed") else (i + 1)
        items.append({
            "event_type": et,
            "quantity": qty,
            "balance_after": 5,
            "order_id": 10 + i,
            "order_number": str(1000 + i),
            "occurred_at": "2026-07-05T10:00:00+00:00",
        })
    query = await _run_history(monkeypatch, _ledger_response(items, total=len(items), page=1))
    text = _last_edit_kwargs(query)["text"]
    # Every one of the eight event types renders its own localized label key.
    for et in ALL_EVENT_TYPES:
        assert f"telegram.bottles.event.{et}" in text


# --------------------------------------------------------------------------- #
# Title: single 📜 (emoji from the seeded string, no hardcoded prefix)
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.anyio
async def test_history_title_has_single_scroll_emoji_list(monkeypatch):
    items = [
        {"event_type": "delivery", "quantity": 2, "balance_after": 5, "order_id": 77,
         "order_number": "70", "occurred_at": "2026-07-12T10:00:00+00:00"},
    ]
    query = await _run_history(
        monkeypatch, _ledger_response(items, total=1, page=1),
        labels={"telegram.bottles.history_title": "📜 Bottle history"},
    )
    text = _last_edit_kwargs(query)["text"]
    assert "📜 📜" not in text
    assert text.count("📜") == 1
    assert text.startswith("<b>📜 Bottle history</b>")


@pytest.mark.unit
@pytest.mark.anyio
async def test_history_title_has_single_scroll_emoji_empty(monkeypatch):
    query = await _run_history(
        monkeypatch, _ledger_response([], total=0, page=1),
        labels={
            "telegram.bottles.history_title": "📜 Bottle history",
            "telegram.bottles.history_empty": "Nothing here yet.",
        },
    )
    text = _last_edit_kwargs(query)["text"]
    assert "📜 📜" not in text
    assert text.count("📜") == 1
    assert text.startswith("<b>📜 Bottle history</b>")


# --------------------------------------------------------------------------- #
# Pagination / empty state (unchanged behavior)
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.anyio
@pytest.mark.parametrize(
    "page,total,expect_prev,expect_next",
    [
        (1, 25, False, True),   # first page: prev hidden, next shown
        (2, 25, True, True),    # middle page: both shown
        (3, 25, True, False),   # last page: prev shown, next hidden (30 >= 25)
    ],
)
async def test_history_pagination_buttons(monkeypatch, page, total, expect_prev, expect_next):
    items = [{"event_type": "delivery", "quantity": 1, "balance_after": 1, "order_id": 1,
              "order_number": "1", "occurred_at": "2026-07-05T10:00:00+00:00"}]
    query = await _run_history(
        monkeypatch,
        _ledger_response(items, total=total, page=page, per_page=10),
        data=f"bottle_history_5_{page}",
    )
    datas = _callback_datas(_last_edit_kwargs(query)["reply_markup"])
    assert (f"bottle_history_5_{page - 1}" in datas) is expect_prev
    assert (f"bottle_history_5_{page + 1}" in datas) is expect_next
    # Back always routes to the balances screen.
    assert "my_bottles" in datas


@pytest.mark.unit
@pytest.mark.anyio
async def test_history_empty_state(monkeypatch):
    query = await _run_history(monkeypatch, _ledger_response([], total=0, page=1))
    kwargs = _last_edit_kwargs(query)
    assert "telegram.bottles.history_empty" in kwargs["text"]
    # No pagination when empty — Back only.
    assert _callback_datas(kwargs["reply_markup"]) == ["my_bottles"]


# --------------------------------------------------------------------------- #
# Place ledger: member attribution + the new 404 contract
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.anyio
async def test_history_attributes_other_members_rows_only(monkeypatch):
    """A grouped address's page mixes members: foreign rows carry the acting
    member's name, the viewer's own rows stay unprefixed."""
    items = [
        {"event_type": "standalone_collection", "quantity": 2, "order_id": None,
         "order_number": None, "occurred_at": "2026-07-20T10:00:00+00:00",
         "member_name": "Bob Coworker", "is_own": False},
        {"event_type": "standalone_collection", "quantity": 1, "order_id": None,
         "order_number": None, "occurred_at": "2026-07-19T10:00:00+00:00",
         "member_name": "Alice Member", "is_own": True},
    ]
    query = await _run_history(monkeypatch, _ledger_response(items, total=2, page=1))
    lines = _last_edit_kwargs(query)["text"].splitlines()
    foreign = next(line for line in lines if line.endswith("(20.07.2026): 2"))
    own = next(line for line in lines if line.endswith("(19.07.2026): 1"))
    assert foreign.startswith("Bob Coworker — ")
    assert "Alice Member" not in own


@pytest.mark.unit
@pytest.mark.anyio
async def test_history_404_for_a_no_longer_visible_address_is_handled(monkeypatch):
    """The endpoint now 404s (instead of an empty 200) when the caller may not
    see the address — e.g. a stale callback kept after an unlink/ungroup. The
    screen must degrade to the load-error toast, never raise."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        bottles_module,
        "api_client",
        _FakeBottleClient(
            ledger=APIResponse(success=False, error="Not found", status_code=404)
        ),
    )
    handler = bottles_module.BottleBalanceHandler()
    update = DummyUpdate()
    update.callback_query = DummyCallbackQuery(data="bottle_history_5_1")

    await handler.show_bottle_history(update, make_context())

    # No message is rendered from a failed response...
    update.callback_query.edit_message_text.assert_not_called()
    # ...the user gets the localized load-error instead.
    answers = [c.args[0] for c in update.callback_query.answer.call_args_list if c.args]
    assert any("telegram.bottles.load_error" in a for a in answers)
