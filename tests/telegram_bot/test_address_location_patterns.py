"""Spec §6: Cancel and Enter-manually must match the SEEDED copy, not an
English word that only appears in the English string.

`telegram_bot/bot.py:503` once matched `.*(cancel|❌ cancel).*` while the DB
holds `❌ Bekor qilish` / `❌ Отмена` — so Cancel was dead in uz and ru, and the
tap escaped ADDRESS_LOCATION into the group-0 catch-all, where it was silently
filed as a support ticket with no reply.

The regex builder that fixed that (`_label_pattern`) has since been deleted:
it resolved the copy ONCE, at handler-build time, so an admin retitling a label
produced a button that rendered with the new text and matched nothing. The rule
now lives in `_resolve_tapped_label`, asked per update by `MenuTapFilter`.
These are its unit-level guarantees; the journey-level ones live in
`test_language_and_i18n_journeys.py`.
"""

from pathlib import Path

import pytest

# NOTE: the plan brief's exact test source names the class `TelegramBot`, but
# telegram_bot/bot.py's actual class is `WaterBusinessBot` — there is no
# `TelegramBot` symbol anywhere in telegram_bot/. Importing the real name here;
# see the Task 11 report for the discrepancy note.
import bot as bot_module
from bot import MenuTapFilter, _resolve_tapped_label

pytestmark = pytest.mark.unit

CANCEL = "telegram.cancel"
LABELS = {"uz": "❌ Bekor qilish", "ru": "❌ Отмена", "en": "❌ Cancel"}


@pytest.fixture
def seeded_cancel_copy(monkeypatch):
    monkeypatch.setattr("i18n.i18n.supported_languages", ["uz", "ru", "en"])
    monkeypatch.setattr(
        "i18n.i18n.get",
        lambda key, language=None, *a, **kw: LABELS.get(language, LABELS["en"]),
    )


def test_cancel_matches_every_language(seeded_cancel_copy):
    for language, label in LABELS.items():
        assert _resolve_tapped_label(label, [CANCEL]) == CANCEL, (
            f"{label!r} ({language}) must resolve to the Cancel button"
        )


def test_cancel_is_whole_string_not_substring(seeded_cancel_copy):
    """The substring patterns this replaces would swallow an ordinary sentence."""
    assert _resolve_tapped_label(
        "I would like to cancel my order please", [CANCEL]
    ) is None
    assert _resolve_tapped_label(
        "Я не хочу ❌ Отмена заказа, просто вопрос", [CANCEL]
    ) is None


def test_a_word_typed_in_front_of_the_label_is_not_a_tap(seeded_cancel_copy):
    """The optional `(?:\\S+\\s+)?` prefix `_label_pattern` carried is gone.

    "any single leading token" is the shape the staff bot deleted in wave 3,
    where a five-character first word satisfied the escape FILTER while the
    matcher resolved nothing and the conversation died with zero output. Here
    it meant a customer typing a note that happened to end in the Cancel copy
    had their address flow cancelled for them. Only the EMOJI decoration a
    keyboard row may carry is strippable; a word a person typed is not.
    """
    for typed in ("Sardor ❌ Bekor qilish", "Пожалуйста ❌ Отмена", "please ❌ Cancel"):
        assert _resolve_tapped_label(typed, [CANCEL]) is None, (
            f"{typed!r} is typed text, not a button tap"
        )


def test_the_emoji_decoration_may_differ_on_either_side(seeded_cancel_copy):
    """The keyboard on the phone was rendered from the row as it read THEN.

    Unlike the staff bot — where the keyboard adds the emoji and the row is
    bare — the emoji here is part of the seeded copy, so an admin adding or
    removing one leaves a live keyboard whose decoration no longer matches the
    row. The label still routes; only the decoration is allowed to differ.
    """
    assert _resolve_tapped_label("Bekor qilish", [CANCEL]) == CANCEL
    assert _resolve_tapped_label("🚫 Bekor qilish", [CANCEL]) == CANCEL
    assert _resolve_tapped_label("  ❌ Bekor qilish  ", [CANCEL]) == CANCEL


def test_a_different_emoji_alone_is_not_a_tap(monkeypatch):
    """A pure-emoji label strips to nothing, and nothing must not equal nothing."""
    monkeypatch.setattr("i18n.i18n.supported_languages", ["uz", "ru", "en"])
    monkeypatch.setattr("i18n.i18n.get", lambda key, language=None, *a, **kw: "❌")

    assert _resolve_tapped_label("❌", [CANCEL]) == CANCEL
    assert _resolve_tapped_label("🚫", [CANCEL]) is None


def test_nothing_matches_when_copy_is_missing(monkeypatch):
    """An empty label must match NOTHING — a naive empty alternation would have
    matched every message ever sent."""
    monkeypatch.setattr("i18n.i18n.supported_languages", ["uz", "ru", "en"])
    monkeypatch.setattr("i18n.i18n.get", lambda key, language=None, *a, **kw: "")

    assert _resolve_tapped_label("", [CANCEL]) is None
    assert _resolve_tapped_label("anything at all", [CANCEL]) is None
    assert _resolve_tapped_label(None, [CANCEL]) is None


def test_the_filter_asks_the_matcher_and_reports_which_key(seeded_cancel_copy):
    """One rule, one implementation: `MenuTapFilter` IS `_resolve_tapped_label`.

    The staff bot shipped a filter and a matcher that disagreed; this pins that
    the customer bot has only the one decider, and that a multi-key filter
    answers "which button" rather than merely "some button".
    """
    other = "telegram.address.enter_manually_button"
    tap_filter = MenuTapFilter(other, CANCEL)

    assert tap_filter.filter(_message(LABELS["ru"])) is True
    assert tap_filter.filter(_message("просто вопрос")) is False
    # Both keys render the same copy under this fixture, so key ORDER decides.
    assert _resolve_tapped_label(LABELS["ru"], [other, CANCEL]) == other
    assert _resolve_tapped_label(LABELS["ru"], [CANCEL, other]) == CANCEL


def test_the_address_flow_escapes_are_wired_to_the_dispatch_time_filter():
    """The ratchet that stops the frozen wiring coming back.

    `_label_pattern` resolved these three labels ONCE, into a `filters.Regex`,
    at handler-build time. Retitling one in the admin UI then rendered new copy
    (the keyboard reads `i18n` per render) on a button nothing would answer
    until the bot was restarted, while the retired copy went on claiming typed
    text. Behaviour is covered by test_language_and_i18n_journeys.py
    ::test_copy_reseeded_after_startup_matches_the_moment_it_renders; this
    pins the wiring itself, because a single `filters.Regex` reintroduced on
    any one of these rows is invisible until a customer is stuck in the
    address flow.
    """
    text = (Path(bot_module.__file__)).read_text(encoding="utf-8")

    required_fragments = [
        "MenuTapFilter(\n                        'telegram.address.enter_manually_button'\n                    )",
        "MenuTapFilter('telegram.cancel')",
        "'telegram.address.enter_manually_button',\n                            "
        "'telegram.address.reenter_manually_button',",
    ]
    missing = [fragment for fragment in required_fragments if fragment not in text]
    assert not missing, f"Address-flow escape wiring is not the tap filter: {missing}"
    assert "_label_pattern" not in text, (
        "the build-time label regex is back — reply-keyboard labels must be "
        "resolved at dispatch time, by the one decider"
    )


class _message:  # noqa: N801 - a stand-in for telegram.Message, text only
    def __init__(self, text):
        self.text = text
