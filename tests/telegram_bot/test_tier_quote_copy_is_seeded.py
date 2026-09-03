"""Every string the tier-discount quote block renders must be real copy.

Three ways this project has shipped English (or a raw key) to a paying customer
at checkout, all of which this file pins:

* an UNSEEDED key renders as `i18n.humanised_missing_key` — English debug text
  like "Estimate tier line", in all three languages;
* a key seeded WITHOUT the placeholder the call site passes still renders, but
  drops the number, so the block would show a discount line with no amount;
* a key that hardcodes the RATE. Production tier percentages differ from dev's
  and are admin-editable, so a literal "2%" in copy is a promise the database
  can silently break. The rate is always a parameter.

A fourth: a key that hardcodes a TIER NAME. Tier names are admin-editable too
(``LoyaltyTierConfig.name``), so a literal "Silver" is the same class of defect
as a literal "2%" — the tier is always the ``{tier_name}``/``{tier}`` parameter.

The web island is checked from both ends. First, that
`PAGE_DATA.i18n.<name>` — read by checkout.js — is actually defined by the
`render_page_data` dict in checkout.html (else it's `undefined` on the page).
Second — and this is the half a naive "is the property present" check misses —
that the literal `| t` key checkout.html uses for that property is *itself*
resolvable in BACKEND_TRANSLATIONS and gets the same guarantees the bot copy
gets: all three languages present, identical placeholders across languages, no
hardcoded rate, no hardcoded tier name. The island literal and the seed dict
key must match byte-for-byte — the `| t` filter looks the English text up
verbatim, so if a future edit reword one side without the other, the lookup
silently misses and uz/ru customers read raw English.
"""

from __future__ import annotations

import re
from pathlib import Path
from string import Formatter

import pytest

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKOUT_HTML = REPO_ROOT / "business_app" / "templates" / "frontend" / "checkout.html"

LANGUAGES = ("en", "uz", "ru")

# key -> the placeholders the bot's call site passes
BOT_KEYS = {
    "telegram.orders.estimate_discount_line": {"amount"},
    "telegram.orders.estimate_reward_line": {"amount"},
    "telegram.orders.estimate_tier_line": {"tier_name", "percentage", "amount"},
    # `icon` is a rail-chosen presentation constant (💰/💳/🏦), not the rail's
    # NAME — the name is stated on the line above and no longer repeated here.
    "telegram.orders.estimate_payable": {"icon", "amount"},
    "telegram.orders.estimate_neutral_total": {"amount"},
    # No longer rendered by the confirm screen (owner screenshot review moved
    # the motivator onto the picker's cash button) — the row is kept seeded
    # and is still checked here so it cannot silently rot into an untranslated
    # or rate-hardcoding row while it sits unused.
    "telegram.orders.estimate_cod_savings": {"amount"},
}

WEB_ISLAND_KEYS = ("discount", "reward_discount", "tier_discount_line", "cod_savings")

# Canonical default tier names that must never appear as literal text in this
# copy. Deliberately a fixed list, not a query against the dev DB: tier names
# are admin-editable (LoyaltyTierConfig.name), so a live DB read would bake
# whatever dev's config happens to contain into the test — the wrong kind of
# coupling for a guard that is also supposed to hold on prod, where the names
# and rates differ from dev's. This is the shipped-default set, taken from
# business_app/migrations/versions/c3f5a9d1e2b4_backfill_tier_name_translations.py
# (CANONICAL_TIER_NAMES), which is itself static for the same reason.
TIER_NAMES = (
    "Bronze", "Bronza", "Бронза",
    "Silver", "Kumush", "Серебро",
    "Gold", "Oltin", "Золото",
    "Platinum", "Platina", "Платина",
)


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(text) if name}


def _assert_no_rate(key: str, language: str, value: str) -> None:
    """The only source of a rate is LoyaltyTierConfig.discount_percentage."""
    assert not re.search(r"\d\s*%", value), (
        f"{key} [{language}] embeds a literal percentage: {value!r}. "
        "Production tier rates differ from dev's and are admin-editable."
    )


def _assert_no_tier_name(key: str, language: str, value: str) -> None:
    """The only source of a tier name is LoyaltyTierConfig.name."""
    lowered = value.lower()
    for tier_name in TIER_NAMES:
        assert tier_name.lower() not in lowered, (
            f"{key} [{language}] embeds a literal tier name {tier_name!r}: {value!r}. "
            "Tier names are admin-editable; the tier is always the "
            "{tier_name}/{tier} placeholder, never literal copy."
        )


@pytest.mark.parametrize("key", sorted(BOT_KEYS))
def test_every_quote_block_key_is_seeded_in_all_three_languages(key):
    assert key in BACKEND_TRANSLATIONS, (
        f"{key} is rendered by the checkout quote block but has no seed row; "
        "customers would read the humanised key as English debug text"
    )
    for language in LANGUAGES:
        value = BACKEND_TRANSLATIONS[key].get(language)
        assert value, f"{key} has no {language} copy"


@pytest.mark.parametrize("key", sorted(BOT_KEYS))
def test_every_quote_block_key_carries_exactly_the_values_it_is_passed(key):
    for language in LANGUAGES:
        assert _placeholders(BACKEND_TRANSLATIONS[key][language]) == BOT_KEYS[key], (
            f"{key} [{language}] does not carry the values the call site passes; "
            "shared.i18n_rendering degrades mismatched copy to the humanised key"
        )


@pytest.mark.parametrize("key", sorted(BOT_KEYS))
def test_no_quote_block_key_hardcodes_a_rate(key):
    for language in LANGUAGES:
        _assert_no_rate(key, language, BACKEND_TRANSLATIONS[key][language])


# ---------------------------------------------------------------------------
# Owner screenshot review (2026-08-27 bot UX rework): the tag marker and the
# icon-fronted payable line are pure COPY — `_build_estimate_block`'s call
# sites pass the same `percentage`/`icon` parameters either way, so nothing
# in the CODE proves the marker or the icon placement survived. These pin the
# literal shape of the seed rows themselves.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
def test_estimate_tier_line_carries_the_tag_marker_after_the_percent(language):
    """The cash confirm screen's tier line must carry the same 🏷 marker the
    picker's cash button carries, positioned right after the percent — not
    before it, and not on the amount half of the line."""
    value = BACKEND_TRANSLATIONS["telegram.orders.estimate_tier_line"][language]
    assert "🏷" in value, f"[{language}] {value!r} is missing the tag marker"
    assert value.index("{percentage}") < value.index("🏷") < value.index("{amount}"), (
        f"[{language}] {value!r} does not place the tag marker after the "
        "percent and before the amount"
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_estimate_payable_leads_with_the_icon_and_never_names_the_rail(language):
    """The payable line drops the `(method)` parenthetical — the rail is
    already named two lines above on the confirmation screen — and puts the
    icon at the FRONT of the line instead."""
    value = BACKEND_TRANSLATIONS["telegram.orders.estimate_payable"][language]
    assert value.startswith("{icon}"), (
        f"[{language}] {value!r} must lead with the icon, matching the "
        "owner's 'icon moves to the front of the line' instruction"
    )
    assert "{method}" not in value, (
        f"[{language}] {value!r} still names the rail; it is already stated "
        "on the 'Payment method' line above"
    )


@pytest.mark.parametrize("key", sorted(BOT_KEYS))
def test_no_quote_block_key_hardcodes_a_tier_name(key):
    for language in LANGUAGES:
        _assert_no_tier_name(key, language, BACKEND_TRANSLATIONS[key][language])


def _resolve_web_source_key(name: str) -> str:
    """Find the literal `| t` key checkout.html uses for island property `name`.

    Resolved by parsing checkout.html rather than a hardcoded mirror map, so a
    literal edited on one side (checkout.html or BACKEND_TRANSLATIONS) without
    the other is caught as a lookup miss below, not silently ignored.
    """
    island = CHECKOUT_HTML.read_text(encoding="utf-8")
    match = re.search(
        r"'" + re.escape(name) + r"':\s*'((?:[^'\\]|\\.)*)'\s*\|\s*t",
        island,
    )
    assert match, (
        f"checkout.js reads PAGE_DATA.i18n.{name}, which the render_page_data "
        "island in checkout.html does not define — it renders as `undefined`"
    )
    return match.group(1).replace("\\'", "'")


@pytest.mark.parametrize("name", WEB_ISLAND_KEYS)
def test_every_web_quote_string_is_in_the_render_page_data_island(name):
    """A DB row alone is not enough — checkout.js reads PAGE_DATA.i18n."""
    _resolve_web_source_key(name)


@pytest.mark.parametrize("name", WEB_ISLAND_KEYS)
def test_every_web_quote_source_key_is_seeded_in_all_three_languages(name):
    """The island literal must resolve to a real, fully-translated seed row.

    Catches drift: if checkout.html's `| t` literal and the BACKEND_TRANSLATIONS
    key stop matching exactly (e.g. whitespace/punctuation edited on one side),
    the lookup below misses and the `| t` filter falls back to raw English for
    uz/ru customers.
    """
    source_key = _resolve_web_source_key(name)
    assert source_key in BACKEND_TRANSLATIONS, (
        f"checkout.html's island entry '{name}' uses the `| t` key {source_key!r}, "
        "which has no BACKEND_TRANSLATIONS row — the island literal has drifted "
        "out of sync with the seed dict (or was never seeded); `| t` falls back "
        "to raw English for uz/ru customers"
    )
    for language in LANGUAGES:
        value = BACKEND_TRANSLATIONS[source_key].get(language)
        assert value, f"{source_key!r} (web island '{name}') has no {language} copy"


@pytest.mark.parametrize("name", WEB_ISLAND_KEYS)
def test_every_web_quote_source_key_carries_the_same_values_in_every_language(name):
    source_key = _resolve_web_source_key(name)
    row = BACKEND_TRANSLATIONS[source_key]
    placeholders_by_language = {language: _placeholders(row[language]) for language in LANGUAGES}
    reference_language, reference_placeholders = next(iter(placeholders_by_language.items()))
    for language, placeholders in placeholders_by_language.items():
        assert placeholders == reference_placeholders, (
            f"{source_key!r} (web island '{name}') [{language}] carries "
            f"{placeholders!r}, but [{reference_language}] carries "
            f"{reference_placeholders!r} — a dropped or renamed placeholder "
            "renders the line with a value missing"
        )


@pytest.mark.parametrize("name", WEB_ISLAND_KEYS)
def test_no_web_quote_source_key_hardcodes_a_rate(name):
    source_key = _resolve_web_source_key(name)
    for language in LANGUAGES:
        _assert_no_rate(
            f"{source_key!r} (web island {name!r})", language, BACKEND_TRANSLATIONS[source_key][language]
        )


@pytest.mark.parametrize("name", WEB_ISLAND_KEYS)
def test_no_web_quote_source_key_hardcodes_a_tier_name(name):
    source_key = _resolve_web_source_key(name)
    for language in LANGUAGES:
        _assert_no_tier_name(
            f"{source_key!r} (web island {name!r})", language, BACKEND_TRANSLATIONS[source_key][language]
        )
