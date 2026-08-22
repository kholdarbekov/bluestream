"""Every conversation-timeout message the bot can render must be real copy.

``BotApplication._flow_timeout`` is the factory behind the ``TIMEOUT`` handler of
five ConversationHandlers (registration, phone verification, subscription
creation, item management, update item). It renders its message with

    text = i18n.get(message_key, language)

— NO values. Two consequences, both of which this file pins:

* an UNSEEDED key renders as ``humanised_missing_key``: "Flow timed out",
  in English, to a customer who reads Uzbek. That is what a customer dropped
  mid-registration would have been shown;
* a SEEDED key that carries a ``{placeholder}`` is worse than unseeded. Because
  the call site passes nothing, ``shared.i18n_rendering.render_translation``
  treats it as broken copy and degrades it to the same humanised key — so the
  copy would look right in the admin UI and render as "Flow timed out" in
  Telegram.

The key list is read out of ``telegram_bot/bot.py`` by AST rather than pasted,
so adding a sixth timed-out flow with no copy fails HERE instead of in
production.
"""

from __future__ import annotations

import ast
from pathlib import Path
from string import Formatter

import pytest

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

REPO_ROOT = Path(__file__).resolve().parents[2]
BOT_MODULE = REPO_ROOT / "telegram_bot" / "bot.py"

LANGUAGES = ("en", "uz", "ru")


def _flow_timeout_message_keys() -> list[str]:
    """The first argument of every ``_flow_timeout(...)`` call in bot.py."""
    tree = ast.parse(BOT_MODULE.read_text(encoding="utf-8"))
    keys: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "_flow_timeout":
            continue
        first = node.args[0]
        assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
            "a _flow_timeout() call passes a computed message key — this test "
            "can no longer see what copy the customer will be shown"
        )
        keys.append(first.value)
    return keys


TIMEOUT_KEYS = sorted(set(_flow_timeout_message_keys()))


def test_the_five_timed_out_flows_are_all_covered():
    """A sanity floor: the factory is used, and the keys are the expected ones.

    Three distinct keys across five flows — the three subscription-shaped flows
    (creation, add item, update item) legitimately share one message.
    """
    assert len(_flow_timeout_message_keys()) == 5
    assert TIMEOUT_KEYS == [
        "telegram.phone.verification_flow_timed_out",
        "telegram.registration.flow_timed_out",
        "telegram.subscription.flow_timed_out",
    ]


@pytest.mark.parametrize("key", TIMEOUT_KEYS)
@pytest.mark.parametrize("language", LANGUAGES)
def test_timeout_copy_is_seeded_in_every_language(key, language):
    row = BACKEND_TRANSLATIONS.get(key)
    assert row is not None, (
        f"{key} is rendered by a live TIMEOUT handler but is not seeded — the "
        "customer is shown the humanised English key"
    )
    value = row.get(language)
    assert value and value.strip(), f"{key} has no {language} copy"


@pytest.mark.parametrize("key", TIMEOUT_KEYS)
@pytest.mark.parametrize("language", LANGUAGES)
def test_timeout_copy_carries_no_placeholder(key, language):
    """The call site passes no values, so a placeholder can never be filled."""
    value = BACKEND_TRANSLATIONS[key][language]
    fields = [name for _, name, _, _ in Formatter().parse(value) if name is not None]
    assert not fields, (
        f"{key} [{language}] carries {fields}; _flow_timeout renders it with no "
        "values, so render_translation would degrade the whole message to "
        "'Flow timed out'"
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_a_dropped_registration_invites_the_customer_to_start_again(language):
    """Registration is the sensitive one and gets a stricter check.

    300s is well inside normal Uzbek SMS latency, so the customer this message
    reaches was waiting for a code that arrived late. It has to read as an
    invitation to retry, not as a rejection — /start is the only way back in,
    and the flow has no menu to offer them.
    """
    value = BACKEND_TRANSLATIONS["telegram.registration.flow_timed_out"][language]

    assert "/start" in value, (
        "a half-registered customer has no menu and no buttons left on screen; "
        "if the copy does not name /start they have no way to resume"
    )
