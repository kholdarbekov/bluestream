"""Guards for the Phase-2c place-group translation seeds.

These tests validate the *seed scripts as data* — key sets, categories, language
coverage and placeholder integrity — never the database. Nothing here opens an
app context or writes a row: every script keeps its side effect behind an
``if __name__ == "__main__":`` guard, which this module also asserts, because
``spec.loader.exec_module`` below would otherwise fire a real seed against the
dev database from a unit test.

Why this matters more than a normal "is it seeded" check: when a key is missing
neither bot shows the key. ``telegram_bot/i18n.py:80-92`` and
``staff_bot/i18n.py:112-118`` humanise the last key segment ("Member line") and
then silently drop every interpolation kwarg, so a linked customer sees a label
with no names and no numbers. ``staff_bot``'s ``/health`` is stricter still: it
reports the service unhealthy when any literal ``staff.*`` key used in
``staff_bot/**.py`` is absent (``staff_bot/webhook_server.py:170-195``). So each
call site must have a key, and each key must have a call site.
"""

import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LANGUAGES = {"en", "uz", "ru"}


def _load(name):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(name):
    return (REPO_ROOT / "scripts" / f"{name}.py").read_text(encoding="utf-8")


def _placeholders(value):
    """Single-brace ``str.format`` placeholder names used by both bots."""
    return set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", value))


def _i18next_placeholders(value):
    """Double-brace i18next interpolation names used by the admin UI."""
    return set(re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", value))


SEED_MODULES = (
    "seed_place_group_telegram_translations",
    "seed_place_group_staff_translations",
    "seed_place_group_ui_translations",
)


# --------------------------------------------------------------------------
# The three new scripts: category, key coverage, language coverage
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_telegram_seed_covers_bot_keys():
    mod = _load("seed_place_group_telegram_translations")
    assert mod.CATEGORY == "telegram"
    expected = {
        "telegram.bottles.place_total",
        "telegram.bottles.member_line",
        "telegram.bottles.cluster_total",
        "telegram.bottles.linked_account_line",
        "telegram.orders.cod_restricted_place",
        "telegram.payments.cluster_debt_total",
        "telegram.payments.place_debt_total",
        "telegram.payments.place_order_line",
    }
    assert expected.issubset(set(mod.KEYS))
    for key, langs in mod.KEYS.items():
        assert set(langs) == LANGUAGES, key


@pytest.mark.unit
def test_staff_seed_covers_staff_keys():
    mod = _load("seed_place_group_staff_translations")
    assert mod.CATEGORY == "staff_bot"
    expected = {
        "staff.delivery.place_cod_total",
        "staff.delivery.cluster_debt_total",
        "staff.delivery.cluster_members",
        "staff.delivery.collectible_now",
        "staff.delivery.fine_place_union_hint",
    }
    assert expected.issubset(set(mod.KEYS))
    for key in mod.KEYS:
        assert key.startswith("staff."), key


@pytest.mark.unit
def test_ui_seed_covers_admin_keys():
    mod = _load("seed_place_group_ui_translations")
    assert mod.CATEGORY == "ui"
    expected = {
        "ui.users.place_groups.title",
        "ui.users.place_groups.suggestions_title",
        "ui.users.place_groups.group_action",
        "ui.users.place_groups.dismiss_action",
        "ui.users.place_groups.reason_placeholder",
        "ui.users.cluster_outstanding",
        "ui.orders.scope_place",
        "ui.orders.scope_cluster",
        "ui.orders.cash_edit_scope_place",
        "ui.orders.cash_edit_scope_cluster",
    }
    assert expected.issubset(set(mod.KEYS))


@pytest.mark.unit
@pytest.mark.parametrize("name", SEED_MODULES)
def test_every_value_is_trilingual_and_non_empty(name):
    mod = _load(name)
    for key, langs in mod.KEYS.items():
        assert set(langs) == LANGUAGES, f"{name}:{key} is not trilingual"
        for lang, value in langs.items():
            assert isinstance(value, str) and value.strip(), f"{name}:{key}:{lang} empty"


@pytest.mark.unit
@pytest.mark.parametrize("name", SEED_MODULES)
def test_seed_scripts_keep_run_behind_a_main_guard(name):
    """Importing a seed must never write to the database.

    ``_load`` above execs the module top-to-bottom; without the guard the import
    in this very test would call ``create_app()`` and
    ``Translation.bulk_create_or_update`` against the dev DB.
    """
    src = _source(name)
    assert 'if __name__ == "__main__":' in src, name
    body = src.split('if __name__ == "__main__":')[0]
    assert not re.search(r"^\s*run\(\)", body, re.M), f"{name} calls run() at import time"


# --------------------------------------------------------------------------
# Call-site parity: every key seeded is used, every key used is seeded
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_customer_bot_place_keys_match_call_sites():
    """The 8 shipped customer-bot keys are exactly the ones the handlers read.

    ``telegram.payments.*`` are included since Task 16 (customer wallet surface)
    landed: ``_build_cod_summary_lines`` in ``telegram_bot/handlers/orders.py``
    reads all three.
    """
    mod = _load("seed_place_group_telegram_translations")
    sources = "".join(
        (REPO_ROOT / "telegram_bot" / "handlers" / f).read_text(encoding="utf-8")
        for f in ("bottles.py", "orders.py")
    )
    used = set(
        re.findall(r"['\"](telegram\.(?:bottles|orders|payments)\.[a-z_.]+)['\"]", sources)
    )

    shipped = set(mod.KEYS)
    assert shipped <= used, f"seeded but never read: {sorted(shipped - used)}"
    assert shipped == {
        "telegram.bottles.place_total",
        "telegram.bottles.member_line",
        "telegram.bottles.cluster_total",
        "telegram.bottles.linked_account_line",
        "telegram.orders.cod_restricted_place",
        "telegram.payments.cluster_debt_total",
        "telegram.payments.place_debt_total",
        "telegram.payments.place_order_line",
    }


@pytest.mark.unit
def test_staff_bot_keys_all_have_call_sites():
    """Every seeded staff key is a literal ``i18n.get`` argument in staff_bot.

    Mirrors ``StaffI18n._extract_literal_staff_keys`` — the same scan that drives
    ``/health``. A key seeded here that no handler reads would be dead weight; a
    handler key missing here takes the bot to 503.
    """
    mod = _load("seed_place_group_staff_translations")
    pattern = re.compile(r"""i18n\.get\(\s*(['"])(staff\.[^'"]+)\1\s*[,)]""")
    used = set()
    for path in (REPO_ROOT / "staff_bot").rglob("*.py"):
        for _, key in pattern.findall(path.read_text(encoding="utf-8")):
            used.add(key)

    assert set(mod.KEYS) <= used, f"seeded but never read: {sorted(set(mod.KEYS) - used)}"
    # A7 deleted the place-statement screen, so `place_statement_title` and
    # `place_members` left this seed with it — nothing under staff_bot/ reads
    # them, and the `<= used` assertion above is what would have caught keeping
    # them.
    assert set(mod.KEYS) == {
        "staff.delivery.place_cod_total",
        "staff.delivery.cluster_debt_total",
        "staff.delivery.cluster_members",
        "staff.delivery.account_cod_debts",
        # The fifth-instance fix: the COD statement screen's headline is now the
        # figure `_collect_offer` will offer, not the raw per-account total.
        "staff.delivery.collectible_now",
        "staff.delivery.fine_place_union_hint",
        "staff.operator.cod_restricted_place",
    }


@pytest.mark.unit
def test_staff_keys_are_not_already_covered_by_the_main_staff_seeder():
    """No overlap with ``scripts/seed_staff_translations.py``.

    That generator upserts a humanised fallback for any key it knows about, so a
    key living in both places would be a race: whichever seed runs last wins.
    """
    mod = _load("seed_place_group_staff_translations")
    main_src = _source("seed_staff_translations")
    for key in mod.KEYS:
        suffix = key.split(".", 2)[2]
        assert f'"{suffix}"' not in main_src, f"{key} already curated in seed_staff_translations"


# --------------------------------------------------------------------------
# Admin UI: English values must match the JSX fallbacks byte for byte
# --------------------------------------------------------------------------

_JS_STRING = r"""(?:'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")"""
_JS_PAIR = re.compile(
    r"""(['"])(ui\.[A-Za-z0-9_.]+)\1\s*,\s*({s}(?:\s*\+\s*{s})*)""".format(s=_JS_STRING),
    re.S,
)


def _unquote_js(literal):
    return re.sub(
        r"\\(.)",
        lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)),
        literal[1:-1],
    )


def _extract_ui_fallbacks():
    """``t('ui.x', 'fallback')`` / ``['ui.x', 'a' + 'b']`` pairs from the admin UI."""
    found = {}
    for rel in (
        "src/components/PlaceGroupPanel.jsx",
        # A1.3 split the panel: the fence-code / audit-label maps went to
        # `placeGroupCopy.js` and the whole confirm flow (picker, label, split,
        # merge review, mandatory reason) to `PlaceGroupConfirmModal.jsx`, so
        # ONE confirm flow serves both the per-customer panel and the
        # estate-wide `GroupedAddressesPanel.jsx`. Same call sites, new files.
        "src/components/placeGroupCopy.js",
        "src/components/PlaceGroupConfirmModal.jsx",
        "src/components/GroupedAddressesPanel.jsx",
        "src/utils/cashScopeDisplay.js",
        "src/pages/Orders.js",
        "src/pages/Users.js",
        "src/pages/Prepayments.js",
        # Listed LAST on purpose: ``found.setdefault`` gives the first file that
        # declares a key precedence, and ``ui.users.entity_subtype_*`` is also
        # read here — Users.js stays the canonical fallback for those.
        "src/components/CustomerMap.js",
    ):
        text = (REPO_ROOT / "admin_ui" / rel).read_text(encoding="utf-8")
        for _, key, expr in _JS_PAIR.findall(text):
            found.setdefault(
                key,
                "".join(_unquote_js(lit) for lit in re.findall(_JS_STRING, expr, re.S)),
            )
    return found


@pytest.mark.unit
def test_ui_seed_english_matches_the_jsx_fallbacks_exactly():
    """A mismatch means uz/ru users read different copy than en users.

    Every admin-UI call passes an inline English fallback, so English renders
    correctly before seeding; the seeded ``en`` row must therefore reproduce it
    verbatim or the page changes wording the moment the seed lands.
    """
    mod = _load("seed_place_group_ui_translations")
    fallbacks = _extract_ui_fallbacks()
    missing = sorted(k for k in mod.KEYS if k not in fallbacks)
    assert not missing, f"seeded keys with no admin-UI call site: {missing}"
    for key, langs in mod.KEYS.items():
        assert langs["en"] == fallbacks[key], key


@pytest.mark.unit
def test_ui_seed_covers_every_place_group_key_rendered_by_the_panel():
    mod = _load("seed_place_group_ui_translations")
    # The UNION of the panel and the two modules A1.3 extracted from it, plus
    # the estate-wide tab that reuses them. The count below is unchanged by that
    # split precisely because it is a set over the same literals: a key that
    # merely moved file is still rendered, while a key that lost its last reader
    # still drops out.
    panel = "\n".join(
        (REPO_ROOT / "admin_ui" / rel).read_text(encoding="utf-8")
        for rel in (
            "src/components/PlaceGroupPanel.jsx",
            "src/components/placeGroupCopy.js",
            "src/components/PlaceGroupConfirmModal.jsx",
            "src/components/GroupedAddressesPanel.jsx",
        )
    )
    used = set(re.findall(r"['\"](ui\.users\.place_groups\.[a-z_]+)['\"]", panel))
    assert used, "extractor found no place_groups keys — the panel moved?"
    assert used <= set(mod.KEYS), f"unseeded: {sorted(used - set(mod.KEYS))}"
    # 53 = the 35 the panel shipped with, plus the 18 the place LIFECYCLE needed:
    #   * 2 for the remove dialog's split (spec §7.1) — label + hint. The
    #     backend had emitted `suggested_bottles_leaving` per member since the
    #     remove endpoint learned `bottlesLeaving`, and the panel read neither,
    #     so every removal defaulted to "all the bottles stay with the place".
    #   * 12 for the merge review (spec §7.4) — the entry point, the dialog, its
    #     five figures, the exclusion checkbox, the override, the empty state and
    #     the load failure. THREE of those five figures are the drift trio
    #     (`merge_projected_balance`, `merge_drift`, `merge_drift_hint`): on a
    #     place whose stored figure its history never explained, the ledger-derived
    #     `resulting_balance` is NOT what the place ends up holding, and the
    #     override is measured against the projection rather than against it.
    #   * 4 for the fence codes the two new surfaces can hit
    #     (PLACE_SPLIT_INVALID, MERGE_PREVIEW_STALE, MERGE_EXCLUSION_NOT_ELIGIBLE,
    #     MERGE_REASON_REQUIRED) — the envelope `message` is always the generic
    #     "Validation failed", so without these the admin is told nothing.
    # (35, not the original 36, since `ui.users.place_groups.member_balance` went
    # away with the per-member "bottles: N" clause — a place's pool has no
    # per-coworker slice, so that line could only ever render `bottles: undefined`.)
    #   * plus 2 for the fence codes that reached a uz/ru admin as raw English:
    #     PLACE_GROUP_MIN_ADDRESSES (reachable with a duplicate address id) and
    #     PLACE_GROUP_REASON_REQUIRED (masked only by the routes' own guard).
    # The seven `ui.users.place_groups.event.*` audit labels are NOT counted
    # here: this extractor's `[a-z_]+` tail stops at the `event.` dot, and the
    # dotted form is deliberate so the audit vocabulary cannot collide with the
    # panel's flat one. They are covered by the JSX-fallback test above, which
    # matches dotted keys.
    #   * plus 1 (56) for `find_suggestions`, the opt-in trigger that replaced
    #     the fire-on-drawer-open suggestions query: the co-location engine
    #     clusters the whole ungrouped estate per call and deliberately refuses
    #     to narrow the pool (a bounding box truncates a transitive component
    #     and voids dismissals — plan E19), so the pass is now requested rather
    #     than billed on every user-drawer open.
    assert len(used) == 56


@pytest.mark.unit
def test_ui_seed_does_not_reseed_ui_common_cancel():
    """``ui.common.cancel`` predates this plan (scripts/seed_backend_translations.py)."""
    mod = _load("seed_place_group_ui_translations")
    assert "ui.common.cancel" not in mod.KEYS
    assert "ui.common.ok" in mod.KEYS


@pytest.mark.unit
def test_ui_seed_carries_no_stray_interpolation():
    """None of these 2c admin strings interpolate — a ``{{x}}`` would be a typo."""
    mod = _load("seed_place_group_ui_translations")
    for key, langs in mod.KEYS.items():
        for lang, value in langs.items():
            assert not _i18next_placeholders(value), f"{key}:{lang}"


# --------------------------------------------------------------------------
# Placeholder integrity (a dropped placeholder = a number the customer never sees)
# --------------------------------------------------------------------------

EXPECTED_PLACEHOLDERS = {
    "telegram.bottles.place_total": {"total"},
    # NAME ONLY — `place_members` rows carry no balance any more, so the handler
    # passes `name=` alone. A `{balance}` here would come back to the customer as
    # the raw unformatted template (telegram_bot/i18n.py:88-93 swallows the
    # KeyError from `translation.format(**kwargs)`).
    "telegram.bottles.member_line": {"name"},
    "telegram.bottles.cluster_total": {"total"},
    "telegram.bottles.linked_account_line": {"address", "owner"},
    "telegram.orders.cod_restricted_place": {"place_active_cod_debt_count"},
    "telegram.payments.cluster_debt_total": {"total"},
    "telegram.payments.place_debt_total": {"label", "total"},
    "telegram.payments.place_order_line": {"order_number", "member_name", "amount"},
    "staff.delivery.place_cod_total": set(),
    "staff.delivery.cluster_debt_total": set(),
    "staff.delivery.cluster_members": set(),
    "staff.delivery.account_cod_debts": set(),
    # Label only — `_format_statement` appends the amount, so a `{...}` here
    # would never be substituted and would reach the driver literally.
    "staff.delivery.collectible_now": set(),
    "staff.delivery.fine_place_union_hint": {"union"},
    "staff.operator.cod_restricted_place": {"place_active_cod_debt_count"},
}


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    ["seed_place_group_telegram_translations", "seed_place_group_staff_translations"],
)
def test_bot_placeholders_are_identical_in_every_language(name):
    mod = _load(name)
    for key, langs in mod.KEYS.items():
        expected = EXPECTED_PLACEHOLDERS[key]
        for lang, value in langs.items():
            assert _placeholders(value) == expected, f"{key}:{lang}"


@pytest.mark.unit
def test_place_cod_copy_never_names_a_coworker():
    """Spec §7: only a COUNT crosses the privacy boundary at a shared workplace."""
    telegram = _load("seed_place_group_telegram_translations")
    staff = _load("seed_place_group_staff_translations")
    for mod in (telegram, staff):
        for key in ("telegram.orders.cod_restricted_place", "staff.operator.cod_restricted_place"):
            langs = mod.KEYS.get(key)
            if not langs:
                continue
            for lang, value in langs.items():
                assert _placeholders(value) == {"place_active_cod_debt_count"}, f"{key}:{lang}"


# --------------------------------------------------------------------------
# Sibling namespaces that do NOT use the ui.* convention
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_ui_staff_seed_covers_the_delivery_reports_scope_columns():
    """``DeliveryReports.js`` reads the ``staff:`` namespace → category ``ui_staff``,
    BARE keys. Seeding these as ``ui.*`` would leave both column headers English."""
    mod = _load("seed_ui_staff_translations")
    assert mod.UI_STAFF_CATEGORY == "ui_staff"
    for lang in LANGUAGES:
        block = mod.UI_STAFF_TRANSLATIONS[lang]
        assert block["scope"], lang
        assert block["attribution"], lang
    assert mod.UI_STAFF_TRANSLATIONS["en"]["scope"] == "Scope"
    assert mod.UI_STAFF_TRANSLATIONS["en"]["attribution"] == "Paid by → settles"

    page = (REPO_ROOT / "admin_ui" / "src" / "pages" / "DeliveryReports.js").read_text(
        encoding="utf-8"
    )
    assert "t('staff:scope'" in page
    assert "t('staff:attribution'" in page


# NOTE: ``test_bottle_tracking_seed_covers_the_place_union_fine_label`` used to
# live here. It pinned ``fine_place_union_balance_label`` — the fine modal's
# second, "place union" number — to its call site in ``BottleTracking.js``. That
# duality is gone: a place holds ONE pool, ``place_balance`` IS the union, so the
# call site was deleted and the guard has to go with it (the guard is
# bidirectional — a call site with no key and a key with no call site both fail).
# ``balance_label`` is now the single number the modal shows.


@pytest.mark.unit
def test_customer_map_seed_owns_the_shared_place_badge():
    """``ui.users.map.shared_place`` is seeded, trilingual, and matches the JSX.

    The map now shows the PLACE pool on every pin of a shared workplace, so three
    coworkers each read ``Bottles: 7`` for one 7-bottle place. The badge is the only
    thing stopping an admin from totalling those popups to 21 — an unseeded badge is
    therefore a real misreading, not cosmetic: ``admin_ui/src/i18n.js:72-77``
    ``parseMissingKeyHandler`` returns the raw KEY outside development, so the inline
    English ``defaultValue`` does **not** rescue a missing row; the admin would read
    the literal string ``ui.users.map.shared_place``.

    Category must be ``ui``: ``AdminUiTranslationService.LEGACY_NAMESPACE_PREFIXES``
    only routes dotted ``ui.users.*`` into the ``users`` i18next namespace that
    ``CustomerMap.js`` opens with ``useTranslation('users')``.
    """
    mod = _load("seed_customer_map_translations")
    key = "ui.users.map.shared_place"

    assert mod.CATEGORY == "ui"
    assert key in mod.KEYS, "the map badge has no seeded copy"

    en, uz, ru = mod.KEYS[key]
    for lang, value in (("en", en), ("uz", uz), ("ru", ru)):
        assert isinstance(value, str) and value.strip(), f"{key}:{lang} empty"
    # A missing/blank `en` row is the dangerous one: DEFAULT_LANGUAGE is `uz`, so an
    # English-canonical column with no `en` row silently renders Uzbek.
    assert len({en, uz, ru}) == 3, "the three languages must actually differ"

    fallbacks = _extract_ui_fallbacks()
    assert key in fallbacks, f"{key} has no t(...) call site in the scanned admin UI"
    assert en == fallbacks[key], "seeded English drifted from the CustomerMap.js fallback"


@pytest.mark.unit
def test_only_the_customer_map_seed_claims_the_ui_users_map_namespace():
    """One key, one owning script (the ``bulk_create_or_update`` category race).

    ``translations`` is unique on ``(key, language)`` ONLY and
    ``Translation.bulk_create_or_update`` REASSIGNS ``category``, so a key claimed by
    two seed scripts is a single row with two owners — whichever script runs last
    wins and the loser's namespace bundle silently drops it. Every ``ui.users.map.*``
    key belongs to ``scripts/seed_customer_map_translations.py`` and to nothing else.
    """
    owner = _load("seed_customer_map_translations")
    assert all(k.startswith("ui.users.map.") for k in owner.KEYS)

    for other in (
        "seed_place_group_ui_translations",
        "seed_ui_bottle_tracking_translations",
        "seed_ui_bottle_tracking_linked_accounts",
        "seed_customer_link_translations",
    ):
        src = _source(other)
        for key in owner.KEYS:
            assert key not in src, f"{key} is claimed by both seeds ({other})"
        # The BARE-key ui_bottle_tracking scripts must not claim `shared_place`
        # either: same row, different category, and the map would lose the badge.
        assert '"shared_place"' not in src, f"bare `shared_place` claimed by {other}"


@pytest.mark.unit
def test_bottle_tracking_seed_claims_no_bare_key_that_ui_tryouts_owns():
    """One bare key seeded from two ui_* scripts = one row with two owners.

    ``translations`` is unique on ``(key, language)`` ONLY and
    ``Translation.bulk_create_or_update`` REASSIGNS ``category``, so whichever
    script runs last wins the row and the loser's namespace bundle drops the key.
    ``phone`` was seeded into ``ui_bottle_tracking`` here and into ``ui_tryouts``
    by ``scripts/seed_ui_tryouts_translations.py`` with identical trilingual
    values; the duplicate was removed from this script (the tryouts one keeps it,
    and ``fallbackNS: ['common']`` — a union of every ui_* category — still
    resolves it for BottleTracking.js).
    """
    bottle = _load("seed_ui_bottle_tracking_linked_accounts")
    tryouts = _load("seed_ui_tryouts_translations")

    assert bottle.CATEGORY != tryouts.UI_TRYOUTS_CATEGORY
    assert "phone" in tryouts.UI_TRYOUTS_TRANSLATIONS["en"]

    overlap = set(bottle.KEYS) & set(tryouts.UI_TRYOUTS_TRANSLATIONS["en"])
    assert not overlap, f"bare keys claimed by two ui_* categories: {sorted(overlap)}"

    # ``BottleTracking.js`` no longer reads ``phone`` at all: its rows and its
    # detail drawer are PLACES, and a place has members, not a phone number. The
    # key stays seeded by ``seed_ui_tryouts_translations`` for its own page; the
    # point of this test is that only one ui_* category ever claims it.
    page = (REPO_ROOT / "admin_ui" / "src" / "pages" / "BottleTracking.js").read_text(
        encoding="utf-8"
    )
    assert "t('phone'" not in page, "a place has no phone — the key must not come back here"


# --------------------------------------------------------------------------
# The BARE ``ui_bottle_tracking`` namespace — previously unguarded entirely
# --------------------------------------------------------------------------

# ``t('key', { … defaultValue: '…' … })`` — the only shape BottleTracking.js uses.
_BARE_T_CALL = re.compile(r"""t\(\s*'([a-z0-9_]+)'\s*,\s*\{(.*?)\}\s*\)""", re.S)
_DEFAULT_VALUE = re.compile(r"""defaultValue:\s*'((?:[^'\\]|\\.)*)'""")

# ``eventTypeLabel`` builds these with a template literal
# (``BottleTracking.js:256``: ``t(`event_${val}`, …)``), so a literal-scan can
# never see them. They are keyed on ``BottleLedgerEventType`` and guarded on the
# enum side by tests/unit/test_bottle_translation_keys.py.
_DYNAMIC_BARE_PREFIXES = ("event_",)


def _bottle_tracking_readers():
    """{bare key: inline English defaultValue} read by ``BottleTracking.js``."""
    page = (REPO_ROOT / "admin_ui" / "src" / "pages" / "BottleTracking.js").read_text(
        encoding="utf-8"
    )
    found = {}
    for key, opts in _BARE_T_CALL.findall(page):
        match = _DEFAULT_VALUE.search(opts)
        found.setdefault(key, match.group(1).replace("\\'", "'") if match else None)
    return found


def _bare_keys_of(module):
    """The BARE key set of a ``ui_*`` seed module, whichever shape it uses.

    Two shapes exist in ``scripts/``: ``{lang: {key: value}}`` (the
    ``UI_*_TRANSLATIONS`` generators) and flat ``{key: (en, uz, ru)}`` (the
    hand-written ``KEYS`` scripts). Returns ``None`` for a module that carries
    neither, so a new script shape fails loudly in the discovery test rather than
    being silently skipped by the ownership guard.
    """
    for name, value in vars(module).items():
        if name.startswith("_") or not isinstance(value, dict) or not value:
            continue
        if set(value) <= LANGUAGES and all(isinstance(v, dict) for v in value.values()):
            return set(value.get("en") or next(iter(value.values())))
        if all(isinstance(k, str) and not k.startswith("ui.") for k in value) and all(
            isinstance(v, (tuple, list, dict)) for v in value.values()
        ):
            return set(value)
    return None


def _ui_scoped_seed_modules():
    """Every ``scripts/seed_ui_*.py`` that claims a scoped ``ui_*`` category.

    Globbed, not listed: ``Translation.bulk_create_or_update`` REASSIGNS
    ``category`` on a table unique on ``(key, language)`` alone, so the ownership
    hazard is between ANY two ``ui_*`` scripts — including one added tomorrow.
    A hard-coded pair would stop catching it the moment a third script appears.
    """
    found = {}
    for path in sorted((REPO_ROOT / "scripts").glob("seed_ui_*.py")):
        module = _load(path.stem)
        category = next(
            (
                v
                for n, v in vars(module).items()
                if n.endswith("CATEGORY") and isinstance(v, str) and v.startswith("ui_")
            ),
            None,
        )
        if category is None:
            continue
        found[path.stem] = (category, _bare_keys_of(module))
    return found


def _bottle_tracking_seeds():
    """{bare key: (en, uz, ru)} across BOTH owning scripts, plus the raw dicts."""
    main = _load("seed_ui_bottle_tracking_translations").UI_BOTTLE_TRACKING_TRANSLATIONS
    drawer = _load("seed_ui_bottle_tracking_linked_accounts").KEYS
    merged = {k: (main["en"][k], main["uz"][k], main["ru"][k]) for k in main["en"]}
    merged.update(drawer)
    return merged, main, drawer


@pytest.mark.unit
def test_every_bare_bottle_tracking_key_the_page_reads_is_seeded():
    """An unseeded BARE key renders the raw identifier to the admin.

    ``admin_ui/src/i18n.js:71-76`` ``parseMissingKeyHandler`` returns the KEY
    outside development, so the inline ``defaultValue`` does **not** rescue a
    missing row — the balances table would literally read ``places_with_balance``.
    This half of the guard is what the Phase-2d re-key needed and did not have:
    seven keys (``places_with_balance``, ``place``, ``shared_place_tag``,
    ``members``, ``place_ledger_title``, ``place_ledger_heading``,
    ``place_balance_label``) reached the branch with a call site and no owner.
    """
    readers = _bottle_tracking_readers()
    assert len(readers) > 100, "the extractor stopped matching — did the page move?"

    seeded, _, _ = _bottle_tracking_seeds()
    unseeded = sorted(k for k in readers if k not in seeded)
    assert not unseeded, f"read by BottleTracking.js but seeded nowhere: {unseeded}"


@pytest.mark.unit
def test_every_seeded_bare_bottle_tracking_key_has_a_reader():
    """The other direction: a seeded key nobody reads is silent i18n rot.

    It is not merely dead weight — it is how "the code says place, the DB still
    says customer" survives review, because the bundle keeps serving the stale
    string long after its call site is gone.
    """
    readers = _bottle_tracking_readers()
    seeded, _, _ = _bottle_tracking_seeds()
    orphans = sorted(
        k
        for k in seeded
        if k not in readers and not k.startswith(_DYNAMIC_BARE_PREFIXES)
    )
    assert not orphans, f"seeded in a ui_bottle_tracking script with no call site: {orphans}"


@pytest.mark.unit
def test_bare_bottle_tracking_english_matches_the_jsx_fallback_exactly():
    """A drifted ``en`` row silently re-words the page the moment the seed lands.

    ``admin_ui``'s own tests stub ``react-i18next`` and assert the inline
    ``defaultValue``, so they are blind to this by construction — a seed that
    says "Search customer..." while the JSX says "Search by any member (name or
    phone)…" stays green in vitest and ships the wrong copy.
    """
    readers = _bottle_tracking_readers()
    seeded, _, _ = _bottle_tracking_seeds()
    drift = {
        key: (seeded[key][0], fallback)
        for key, fallback in readers.items()
        if fallback is not None and key in seeded and seeded[key][0] != fallback
    }
    assert not drift, f"seeded en != JSX defaultValue: {drift}"


@pytest.mark.unit
def test_bare_bottle_tracking_keys_are_trilingual():
    """``DEFAULT_LANGUAGE`` is ``uz``, so an English-canonical column with no
    ``en`` row silently renders Uzbek to an English-speaking admin."""
    seeded, _, _ = _bottle_tracking_seeds()
    for key, values in seeded.items():
        assert len(values) == 3, key
        for lang, value in zip(("en", "uz", "ru"), values):
            assert isinstance(value, str) and value.strip(), f"{key}:{lang} empty"


@pytest.mark.unit
def test_every_ui_scoped_seed_script_exposes_a_readable_bare_key_set():
    """The ownership guard below is only as good as this discovery step.

    If a new ``seed_ui_*.py`` uses a dict shape ``_bare_keys_of`` cannot read, it
    would contribute an empty key set and be silently exempt from the H16 check.
    Fail here instead, loudly, naming the script.
    """
    modules = _ui_scoped_seed_modules()
    assert len(modules) >= 9, f"discovery found only {sorted(modules)} — did scripts/ move?"
    unreadable = sorted(name for name, (_, keys) in modules.items() if not keys)
    assert not unreadable, f"ui_* seed scripts with an unreadable key set: {unreadable}"


# The place-keyed vocabulary this plan introduced or repointed. These are the
# keys with no generic twin anywhere in the admin UI, so a second claimant is
# always a mistake rather than a benign duplicate label.
_PLACE_OWNED_BARE_KEYS = frozenset(
    {
        "places_with_balance",
        "place",
        "shared_place_tag",
        "place_ledger_title",
        "place_ledger_heading",
        "place_balance_label",
        "members",
        "member",
        "balance_label",
        "grouped_tag",
        "linked_accounts_alert_title",
        "linked_member_count_label",
        "search_customer_balance_placeholder",
        "customer_detail_title",
        "addresses_heading",
        "details_button",
    }
)


@pytest.mark.unit
def test_place_owned_bare_keys_have_exactly_one_owning_ui_script():
    """H16 for this plan's vocabulary, checked against EVERY ``ui_*`` script.

    ``translations`` is unique on ``(key, language)`` ONLY and
    ``Translation.bulk_create_or_update`` **REASSIGNS ``category``**, so a bare
    key claimed by two scripts is ONE row with two owners: whichever seed runs
    last wins and the loser's namespace bundle silently drops the key. ``phone``
    was exactly this — claimed by both ``ui_bottle_tracking`` and ``ui_tryouts``.
    Comparing against every discovered script (not a hard-coded pair) is what
    keeps this true when a third script is added tomorrow.

    Scoped to ``_PLACE_OWNED_BARE_KEYS`` on purpose. A blanket all-keys version
    of this assertion goes red on ~110 PRE-EXISTING cross-script duplicates that
    predate this plan (``actions``, ``status``, ``cancel``, ``page_title``, …);
    18 of them even carry CONFLICTING values, which is a real latent bug but one
    that spans five unrelated pages. Widening the key set is the right follow-up;
    silently deleting the assertion to accommodate them would not be.
    """
    modules = _ui_scoped_seed_modules()
    owners = {}
    for name, (category, keys) in modules.items():
        for key in _PLACE_OWNED_BARE_KEYS & keys:
            owners.setdefault(key, []).append(f"{name} ({category})")

    multi = {k: sorted(v) for k, v in owners.items() if len(v) > 1}
    assert not multi, f"place-owned bare keys claimed by more than one ui_* seed: {multi}"

    unclaimed = sorted(_PLACE_OWNED_BARE_KEYS - set(owners))
    assert not unclaimed, f"place-owned bare keys claimed by NO ui_* seed: {unclaimed}"


@pytest.mark.unit
def test_people_keyed_bottle_tracking_copy_is_gone_from_the_seeds():
    """The place re-key deleted these call sites; the DB copy must go with them.

    ``linked_accounts_alert_title``/``linked_member_count_label`` are the reverse
    case and are deliberately NOT in this list: the keys survive, the copy was
    repointed. Their trigger moved to ``placeDetailTarget.is_shared_place``, which
    is true for any shared place — so "Linked accounts detected" would have
    labelled two UNLINKED coworkers at one office as linked accounts.
    """
    seeded, _, _ = _bottle_tracking_seeds()
    dead = (
        "customers_with_balance",        # a balances row is a PLACE, not a person
        "fine_place_union_balance_label",  # one pool ⇒ balance_label IS the union
        "combined_at_place_label",
        "this_account_only_label",       # backend dropped total_balance
        "combined_cluster_balance_label",  # backend dropped cluster_total_balance
        "cluster_ledger_heading",
        "user_id_label",                 # the ledger is place-keyed, no user in scope
        "address_ledger_title",
    )
    for key in dead:
        assert key not in seeded, f"{key} has no call site and must not be seeded"

    alert_title, _, _ = seeded["linked_accounts_alert_title"]
    assert alert_title != "Linked accounts detected", (
        "the alert fires on any shared place, so it must not claim the accounts are linked"
    )


@pytest.mark.unit
def test_dead_grouping_keys_are_gone_from_the_phase1_customer_link_seed():
    """Grouping moved out of ``LinkedAccountsPanel`` into ``PlaceGroupPanel``
    (Task 13), so these eight Phase-1 keys no longer have a reader."""
    mod = _load("seed_customer_link_translations")
    dead = {
        "same_place_title",
        "same_place_hint",
        "no_addresses",
        "mark_same_place",
        "group_title",
        "group_label_placeholder",
        "group_success",
        "grouped_tag",
    }
    for suffix in dead:
        assert f"ui.users.linked_accounts.{suffix}" not in mod.KEYS, suffix

    panel = (REPO_ROOT / "admin_ui" / "src" / "components" / "LinkedAccountsPanel.jsx").read_text(
        encoding="utf-8"
    )
    for suffix in dead:
        assert f"linked_accounts.{suffix}" not in panel, suffix
