"""One owner per bare admin-UI key: two seeds must never write it differently.

`translations` is unique on ``(key, language)`` ONLY. `category` is a plain
attribute that `Translation.bulk_create_or_update` REASSIGNS on every upsert, so
a BARE key seeded from two `scripts/seed_ui_*.py` scripts is ONE database row
with two claimed owners: whichever seed runs last wins, the loser's namespace
bundle silently drops the key, and the loser's PAGE silently renders the
winner's wording. Nothing errors — an operator just sees the wrong noun.

That is not hypothetical. Seeding the sales module moved twelve live rows out of
`ui_bottle_tracking`, and a scan then found six sales keys whose meaning simply
differs per page (`name` is a person on the Sales Agents page and a product
category elsewhere; four seeds already write four different `search_placeholder`
strings). The fix was to rename the page-specific ones — `contact_name`,
`search_agents_placeholder`, `agent_details`, `outlet_address`, `agent_active`,
`agent_inactive` — and to stop re-seeding the five generic keys that already
have an owner. This test is what stops the next module from repeating it.

Identical values under one key are NOT flagged: they cost a redundant write, not
a wrong string. Only a genuine disagreement fails.

The rule this enforces is stated in prose at
`scripts/seed_ui_bottle_tracking_linked_accounts.py`. Pages keep resolving a key
they do not seed because `admin_ui/src/i18n.js` sets `fallbackNS: ['common']`
and `AdminUiTranslationService.get_translations(lang, "common")` unions EVERY
`ui_*` category.
"""

import importlib.util
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEED_GLOB = "seed_ui_*.py"
LANGUAGES = ("en", "uz", "ru")

# Conflicts that predate this guard. Each is a generic key several page-specific
# seeds already write with different wording, so whichever seed ran last on a
# given database decides what every other page shows. They are recorded here so
# the suite is green today and any NEW conflict is red; none of them belongs to
# scripts/seed_ui_sales_translations.py (test_sales_seed_owns_no_contested_key
# pins that). Fixing one means renaming it in its page + seed, exactly as the
# sales module did, and deleting the line below.
PRE_EXISTING_CONFLICTS = {
    "active",  # notifications, product_categories, time_slots, translations_page, tryouts
    "address",  # bottle_tracking, subscriptions ("Delivery address"), tryouts
    "create_failed",  # product_categories, subscriptions, time_slots
    "created",  # product_categories, subscriptions, time_slots
    "delete_confirm",  # time_slots, translations_page
    "delete_failed",  # product_categories, time_slots
    "inactive",  # notifications, product_categories, time_slots, translations_page
    "name_placeholder",  # product_categories, time_slots
    "name_required",  # product_categories, time_slots
    "page_title",  # bottle_tracking, time_slots, translations_page, tryouts
    "quantity",  # bottle_tracking, subscriptions ("Qty"), tryouts
    "search_placeholder",  # product_categories, subscriptions, translations_page, tryouts
    "showing_range",  # product_categories, translations_page
    "toast_created",  # translations_page, tryouts
    "toast_export_failed",  # notifications, translations_page
    "toast_updated",  # translations_page, tryouts
    "update_failed",  # product_categories, subscriptions, time_slots
    "updated",  # subscriptions, time_slots
}

SALES_SEED = "seed_ui_sales_translations.py"


def _load(path):
    """Import a seed module WITHOUT running it: `main()` is `__main__`-guarded."""
    spec = importlib.util.spec_from_file_location(f"_seed_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _catalogs(module):
    """Yield {lang: {bare key: value}} for every translation table in a seed.

    Two shapes are in use and both are read from the module's real objects
    rather than re-parsed, so a seed cannot drift away from this check:
      * language-major -- {"en": {...}, "uz": {...}, "ru": {...}}
      * key-major      -- {key: (en, uz, ru)}  (bottle-tracking linked accounts)
    """
    for value in vars(module).values():
        if not isinstance(value, dict) or not value:
            continue
        if set(LANGUAGES) <= set(value) and all(isinstance(v, dict) for v in value.values()):
            yield {lang: value[lang] for lang in LANGUAGES}
        elif all(isinstance(v, tuple) and len(v) == len(LANGUAGES) for v in value.values()):
            yield {lang: {k: v[i] for k, v in value.items()} for i, lang in enumerate(LANGUAGES)}


def _seed_values():
    """(bare key, language) -> {seed filename: value} across every ui_* seed."""
    written = defaultdict(dict)
    seeds = sorted((ROOT / "scripts").glob(SEED_GLOB))
    assert seeds, f"no {SEED_GLOB} found under scripts/"
    for path in seeds:
        for catalog in _catalogs(_load(path)):
            for lang, pairs in catalog.items():
                for key, value in pairs.items():
                    written[(key, lang)][path.name] = value
    return written


def _conflicts():
    """Bare keys two seeds write with DIFFERENT text, as {key: {lang: {file: value}}}."""
    found = defaultdict(dict)
    for (key, lang), by_file in _seed_values().items():
        if len(set(by_file.values())) > 1:
            found[key][lang] = by_file
    return found


def test_no_two_ui_seeds_write_the_same_bare_key_differently():
    """A new bare-key collision must fail here, not silently on a page in prod."""
    conflicts = _conflicts()
    unexpected = {k: v for k, v in conflicts.items() if k not in PRE_EXISTING_CONFLICTS}

    lines = []
    for key in sorted(unexpected):
        lines.append(f"  {key!r} is written differently by:")
        for lang in LANGUAGES:
            for filename, value in sorted(unexpected[key].get(lang, {}).items()):
                lines.append(f"      [{lang}] {filename}: {value!r}")
    assert not unexpected, (
        "These bare keys are seeded with conflicting values. `translations` is unique on "
        "(key, language), so only one of them survives and the other page shows the wrong "
        "text.\n" + "\n".join(lines) + "\n"
        "Fix: rename the page-specific key (e.g. `name` -> `contact_name`) in the page AND "
        "its seed, or drop the duplicate and let the existing owner keep it."
    )


def test_pre_existing_conflict_allowlist_is_not_stale():
    """An allowlisted key that no longer conflicts must leave the allowlist."""
    still_conflicting = set(_conflicts())
    stale = sorted(PRE_EXISTING_CONFLICTS - still_conflicting)
    assert not stale, (
        f"These keys no longer conflict and must be removed from PRE_EXISTING_CONFLICTS: {stale}. "
        "Leaving them there would hide a future regression on the same key."
    )


def test_sales_seed_owns_no_contested_key():
    """The sales module must stay clean — the allowlist may never cover it."""
    offenders = sorted(
        {
            key
            for key, langs in _conflicts().items()
            for by_file in langs.values()
            if SALES_SEED in by_file
        }
    )
    assert not offenders, (
        f"{SALES_SEED} writes contested bare keys {offenders}. Page-specific strings belong "
        "under a page-specific key (`contact_name`, `search_agents_placeholder`, "
        "`agent_details`, `outlet_address`, `agent_active`, `agent_inactive`); generic ones "
        "belong to whichever seed already owns them."
    )
