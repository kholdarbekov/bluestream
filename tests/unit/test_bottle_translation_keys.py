"""Enum-coverage + completeness guard for the bottle delivery-summary /
support / ledger-history telegram translation keys (spec §3.7).

The eight ``telegram.bottles.event.*`` labels are keyed on
``BottleLedgerEventType`` values; a new enum member without a seeded label
would degrade to a capitalized raw segment in the bot's ledger-history view.
This test fails loudly if the seed set ever drifts from the enum, and if any
spec §3.7 key is dropped or left partially translated.
"""
import pytest

from shared.enums import BottleLedgerEventType
from scripts.seed_bottle_ledger_translations import KEYS

EVENT_PREFIX = "telegram.bottles.event."

# The full spec §3.7 key list — the seed must cover exactly these, no more, no less.
SPEC_KEYS = {
    "telegram.delivery_summary.title",
    "telegram.delivery_summary.bottles_delivered",
    "telegram.delivery_summary.bottles_collected",
    "telegram.delivery_summary.balance",
    "telegram.delivery_summary.report_button",
    "telegram.support.describe_issue_prompt",
    "telegram.support.cancel_button",
    "telegram.support.ack",
    "telegram.support.send_failed",
    "telegram.support.cancelled",
    "telegram.bottles.history_button",
    "telegram.bottles.history_title",
    "telegram.bottles.history_empty",
    "telegram.bottles.event.delivery",
    "telegram.bottles.event.return_on_delivery",
    "telegram.bottles.event.standalone_collection",
    "telegram.bottles.event.admin_adjustment",
    "telegram.bottles.event.fine_issued",
    "telegram.bottles.event.fine_reversed",
    "telegram.bottles.event.fine_paid",
    "telegram.bottles.event.initial_balance",
}

# Keys whose values are formatted with str.format kwargs (i18n.py:88-90).
PARAM_KEYS = {
    "telegram.delivery_summary.title": "{order_number}",
    "telegram.delivery_summary.bottles_delivered": "{count}",
    "telegram.delivery_summary.bottles_collected": "{count}",
    "telegram.delivery_summary.balance": "{count}",
    "telegram.support.describe_issue_prompt": "{order_number}",
}


@pytest.mark.unit
def test_seed_covers_every_bottle_ledger_event_type_exactly():
    seeded_event_keys = {k for k in KEYS if k.startswith(EVENT_PREFIX)}
    expected_event_keys = {
        f"{EVENT_PREFIX}{event.value}" for event in BottleLedgerEventType
    }
    assert seeded_event_keys == expected_event_keys


@pytest.mark.unit
def test_seed_key_set_matches_spec_exactly():
    assert set(KEYS) == SPEC_KEYS


@pytest.mark.unit
def test_every_key_is_trilingual_and_nonempty():
    for key, langs in KEYS.items():
        assert set(langs) == {"en", "uz", "ru"}, key
        for language, value in langs.items():
            assert isinstance(value, str) and value.strip(), f"{key}/{language}"


@pytest.mark.unit
def test_parameterized_keys_preserve_placeholder_in_every_language():
    for key, placeholder in PARAM_KEYS.items():
        for language, value in KEYS[key].items():
            assert placeholder in value, f"{key}/{language} missing {placeholder}"
