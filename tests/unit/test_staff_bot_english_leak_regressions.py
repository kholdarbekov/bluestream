"""Every driver-facing staff_bot string must be translatable — the leaks drivers reported.

WHY THIS FILE EXISTS AND WHY THE EXISTING GUARDS DID NOT CATCH ANY OF IT
------------------------------------------------------------------------
Drivers reported that "some buttons and texts are English only". Every existing
translation guard was green at the time, because each of them re-derives the
required-key set from the SAME hardcoded tuple the bug lives in:

    staff_bot/i18n.py::_add_dynamic_family_keys      -> six delivery statuses
    scripts/seed_staff_translations.py::_add_dynamic_keys -> the same six

`/health` asks the first one, and
`tests/unit/test_staff_translation_catalog_complete.py` asks both. So when
`keyboards/delivery.py` started rendering a seventh status
(`DeliveryStatus.CANCELLED`, which `DELIVERY_STATUS_TRANSITIONS` lists as a
successor of EVERY active status), nothing noticed: the renderer asked for
`staff.delivery.status.cancelled`, the key set never claimed to need it, and
`Translation.get` silently fell through to its humanise branch and printed the
English word "Cancelled" on every active-delivery card in every language.

The three classes of leak pinned here are therefore the three the shared-tuple
design could not see:

1. DYNAMIC KEY FAMILIES narrower than the enum that feeds them
   (`test_delivery_status_family_covers_every_status_the_bot_can_render`).
2. Keys addressed in the WRONG NAMESPACE. `staff_bot/i18n.py::load_translations`
   only ever loads `category='staff_bot' OR key LIKE 'staff.%'`, so a call site
   asking for `common.back` cannot resolve in ANY language — and because
   `_extract_literal_staff_keys` greps for `staff\\.` only, the key is invisible
   to `/health` as well (`test_no_call_site_asks_for_a_key_staff_bot_cannot_load`).
3. A hardcoded `'en'` argument where the deployment default is `uz`
   (`test_no_call_site_hardcodes_english_as_the_language`).

Conventions follow tests/integration/test_place_i18n_render_e2e.py: production
code is called, never re-implemented, and expectations are derived from the
seed catalogs so a copy edit updates them but a LOST row still fails.
"""

import ast
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STAFF_ROOT = ROOT / "staff_bot"
SEED_SCRIPT = ROOT / "scripts" / "seed_staff_translations.py"

LANGUAGES = ("en", "uz", "ru")


def _load_seed_script():
    spec = importlib.util.spec_from_file_location("seed_staff_translations_leaks", SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _python_sources():
    return sorted(STAFF_ROOT.rglob("*.py"))


class TestDynamicKeyFamiliesCoverTheirEnum:
    """A family must cover every value the renderer can be handed, not a subset."""

    def test_delivery_status_family_covers_every_status_the_bot_can_render(self):
        """`staff.delivery.status.*` must span DeliveryStatus, not a hand-written six.

        `keyboards/delivery.py:95` builds a button label for EVERY successor in
        `DELIVERY_STATUS_TRANSITIONS`, and `utils/formatters.py:358` falls
        through to `staff.delivery.status.{status}` for any status outside its
        own map. Both are fed straight from the enum, so the catalog has to be.
        """
        from shared.enums import DeliveryStatus

        seed = _load_seed_script()
        missing = sorted(
            status.value
            for status in DeliveryStatus
            if status.value not in seed.DELIVERY_STATUS_TRANSLATIONS
        )
        assert missing == [], (
            f"DELIVERY_STATUS_TRANSLATIONS is missing {missing}. Every one of these renders "
            f"as humanised English on a driver's screen."
        )

    def test_transition_buttons_resolve_for_every_active_delivery(self):
        """The exact labels `DeliveryKeyboards.delivery_actions` puts on screen."""
        from shared.enums import DeliveryStatus
        from shared.staff_constants import DELIVERY_STATUS_TRANSITIONS

        seed = _load_seed_script()
        active = ("assigned", "picked_up", "in_transit", "arrived")

        unresolvable = set()
        for current in active:
            for nxt in DELIVERY_STATUS_TRANSITIONS.get(current, []):
                if nxt not in seed.DELIVERY_STATUS_TRANSLATIONS:
                    unresolvable.add((current, nxt))

        assert unresolvable == set(), (
            f"These transition buttons render untranslated English: {sorted(unresolvable)}"
        )

    @pytest.mark.parametrize(
        "family_attr,enum_path",
        [
            ("DELIVERY_STATUS_TRANSLATIONS", "shared.enums:DeliveryStatus"),
            ("ORDER_STATUS_TRANSLATIONS", "shared.enums:OrderStatus"),
            ("PAYMENT_TRANSLATIONS", "shared.enums:PaymentMethod"),
        ],
    )
    def test_family_is_not_narrower_than_its_enum(self, family_attr, enum_path):
        """Generalises the defect: no family may lag the enum that drives it."""
        import importlib

        module_name, enum_name = enum_path.split(":")
        enum_cls = getattr(importlib.import_module(module_name), enum_name)

        seed = _load_seed_script()
        catalog = getattr(seed, family_attr)
        missing = sorted(v.value for v in enum_cls if v.value not in catalog)
        assert missing == [], f"{family_attr} is missing {missing}"

    def test_health_check_requires_the_full_delivery_status_family(self):
        """`/health` must be able to SEE the gap, not just be fixed behind it.

        `_add_dynamic_family_keys` is what `/health` and the catalog test both
        ask. While it hardcodes its own six-status tuple, a seventh rendered
        status is undetectable no matter how good the seed catalog is.
        """
        from shared.enums import DeliveryStatus
        from staff_bot.i18n import Translation

        keys = set()
        Translation._add_dynamic_family_keys(keys)

        missing = sorted(
            f"staff.delivery.status.{s.value}"
            for s in DeliveryStatus
            if f"staff.delivery.status.{s.value}" not in keys
        )
        assert missing == [], (
            f"/health cannot detect these keys as required: {missing}. The required-key "
            f"set must be derived from DeliveryStatus, not a hand-written tuple."
        )


class TestNamespaceCorrectness:
    """staff_bot can only ever load `staff.*` — asking for anything else is a guaranteed leak."""

    # Mirrors staff_bot/i18n.py::load_translations, which filters on
    # `category='staff_bot' OR key LIKE 'staff.%'`.
    LOADABLE_PREFIX = "staff."

    I18N_GET = re.compile(r"""i18n\.get\(\s*(['"])([a-z][\w]*(?:\.[\w]+)+)\1""")

    def test_no_call_site_asks_for_a_key_staff_bot_cannot_load(self):
        """`common.back` / `common.cancel` resolve in NO language, in any deployment."""
        offenders = []
        for path in _python_sources():
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                for _, key in self.I18N_GET.findall(line):
                    if not key.startswith(self.LOADABLE_PREFIX):
                        offenders.append(f"{path.relative_to(ROOT)}:{lineno} -> {key!r}")

        assert offenders == [], (
            "These call sites request keys outside the `staff.` namespace that "
            "staff_bot/i18n.py::load_translations can never load, so they render the "
            "humanised English key tail in every language:\n  " + "\n  ".join(offenders)
        )


class TestLanguageArgumentIsNeverHardcodedEnglish:
    """DEFAULT_LANGUAGE is `uz`; a literal 'en' argument pins the screen to English."""

    def test_no_call_site_hardcodes_english_as_the_language(self):
        """`i18n.get('some.key', 'en')` ignores both the user AND the deployment default."""
        offenders = []
        for path in _python_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (
                    isinstance(func, ast.Attribute)
                    and func.attr == "get"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "i18n"
                ):
                    continue
                if len(node.args) < 2:
                    continue
                lang = node.args[1]
                if isinstance(lang, ast.Constant) and lang.value == "en":
                    key = node.args[0].value if isinstance(node.args[0], ast.Constant) else "<dynamic>"
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} -> {key}")

        assert offenders == [], (
            "These call sites hardcode 'en' instead of the user's language or "
            "config.localization.default_language (which is 'uz' in production):\n  "
            + "\n  ".join(offenders)
        )

    def test_default_language_fallbacks_use_config_not_a_literal(self):
        """`context.user_data.get('language', 'en')` is the same defect in dict form."""
        pattern = re.compile(r"""\.get\(\s*['"]language['"]\s*,\s*['"]en['"]\s*\)""")
        offenders = []
        for path in _python_sources():
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}")

        assert offenders == [], (
            "These fall back to English rather than config.localization.default_language:\n  "
            + "\n  ".join(offenders)
        )


class TestNoHardcodedUserFacingLiterals:
    """Literals that reach a Telegram message or button label."""

    # (path, literal) pairs that were confirmed to render on a driver's screen.
    FORBIDDEN = (
        ("handlers/delivery/bottle_session.py", "Driver"),
        ("handlers/delivery/bottle_session.py", "Driver #"),
        ("handlers/delivery/bottle_collection.py", "Unknown driver"),
        ("handlers/delivery/bottle_collection.py", "unknown time"),
        ("handlers/delivery/bottle_collection.py", "</b> bottles  [ref: "),
        ("keyboards/delivery.py", "Driver"),
    )

    @pytest.mark.parametrize("relative_path,literal", FORBIDDEN)
    def test_confirmed_leak_literal_is_gone(self, relative_path, literal):
        """Each of these printed English into a driver-facing string."""
        source = (STAFF_ROOT / relative_path).read_text(encoding="utf-8")
        # Only string literals count; a translation KEY may legitimately contain
        # the word (e.g. `staff.common.unknown_driver`).
        literals = [
            node.value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        assert literal not in literals, (
            f"{relative_path} still contains the hardcoded user-facing literal {literal!r}"
        )

    def test_tryout_label_is_display_translated_but_stored_raw(self):
        """`'Try-out'` may survive ONLY as the persisted address title.

        Two occurrences were driver-facing display (a card header and a state
        placeholder) and are now `staff.tryout.default_label`. The third is the
        `address.label` written to the backend when a driver creates a try-out.
        That one must NOT be translated: it is user DATA persisted once, and
        localizing it at write time would freeze the creating driver's language
        into the record — `addresses.title` already holds a mix of 'Home', 'Uy'
        and 'Ish' precisely because it is free text, not a label to render.
        """
        source = (STAFF_ROOT / "handlers/tryouts.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        stored_as_label = 0
        other = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "label"
                        and isinstance(value, ast.Constant)
                        and value.value == "Try-out"
                    ):
                        stored_as_label += 1

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "Try-out":
                other.append(node.lineno)

        assert stored_as_label == 1, "the persisted address label should still be written raw"
        assert len(other) == stored_as_label, (
            f"'Try-out' is still used outside the persisted address label, at lines "
            f"{other}. Display sites must use staff.tryout.default_label."
        )


class TestBackendSuppliedStatusesAreTranslated:
    """Raw enum values from the API must be mapped through a key family, not printed."""

    @pytest.mark.parametrize(
        "family_prefix,enum_path",
        [
            ("staff.delivery.cash_session_status.", "shared.enums:DriverCashSessionStatus"),
            ("staff.delivery.bottle_session_status.", "shared.enums:DriverBottleSessionStatus"),
        ],
    )
    def test_status_family_is_seeded_for_every_enum_value(self, family_prefix, enum_path):
        import importlib

        module_name, enum_name = enum_path.split(":")
        enum_cls = getattr(importlib.import_module(module_name), enum_name)

        seed = _load_seed_script()
        missing = []
        for value in (v.value for v in enum_cls):
            key = f"{family_prefix}{value}"
            for language in LANGUAGES:
                if not seed._curated_value(key, language):
                    missing.append(f"{key} [{language}]")

        assert missing == [], f"Untranslated backend statuses shown to drivers: {missing}"

    def test_reconciliation_risk_flags_are_seeded(self):
        """`_build_risk_flags` emits snake_case identifiers straight onto the cash screen."""
        seed = _load_seed_script()
        flags = (
            "cash_on_hand_escalation",
            "cash_on_hand_warning",
            "repeated_mismatch_pattern",
            "submission_overdue",
            "reconciliation_warning_due",
        )
        missing = [
            f"staff.delivery.risk_flag.{flag} [{language}]"
            for flag in flags
            for language in LANGUAGES
            if not seed._curated_value(f"staff.delivery.risk_flag.{flag}", language)
        ]
        assert missing == [], f"Risk flags render as raw English identifiers: {missing}"


class TestNewKeysAreGenuinelyTrilingual:
    """A key present in three languages with one English value is still an English leak."""

    def test_no_new_family_value_is_english_in_uz_or_ru(self):
        seed = _load_seed_script()
        latin = re.compile(r"[A-Za-z]")
        allowed = seed.RU_ALLOWED_LATIN_TOKENS

        offenders = []
        for family in (
            "DELIVERY_STATUS_TRANSLATIONS",
            "CASH_SESSION_STATUS_TRANSLATIONS",
            "BOTTLE_SESSION_STATUS_TRANSLATIONS",
            "RISK_FLAG_TRANSLATIONS",
        ):
            catalog = getattr(seed, family, None)
            if catalog is None:
                offenders.append(f"{family} does not exist")
                continue
            for key, values in catalog.items():
                for language in LANGUAGES:
                    if language not in values:
                        offenders.append(f"{family}[{key}] missing {language}")
                if values.get("ru"):
                    stripped = re.sub(r"\{[^{}]+\}", "", values["ru"])
                    for token in allowed:
                        stripped = re.sub(re.escape(token), "", stripped, flags=re.IGNORECASE)
                    if latin.search(stripped):
                        offenders.append(f"{family}[{key}] ru is not Cyrillic: {values['ru']!r}")

        assert offenders == [], "\n  ".join(offenders)
