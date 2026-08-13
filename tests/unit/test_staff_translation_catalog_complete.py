"""Every `staff.*` key the staff bot asks for must exist in the seed catalog.

This is not a style check — it is a production-availability check.

`staff_bot/webhook_server.py::health_handler` fails the WHOLE service on ANY
missing required key (`overall_healthy = False`, HTTP 503). Because Docker's
healthcheck polls that endpoint every 60s, a single unseeded string parks the
container at `unhealthy` indefinitely. Worse, the same endpoint also verifies
database connectivity — so while it is stuck red for a cosmetic reason, a real
DB outage produces no change in signal at all.

That is exactly what happened: `staff.tryout.outside_delivery_area` is
referenced in handlers and seeded only under category `api` (which staff_bot
never loads), so production ran `unhealthy` for months with nobody able to
tell the difference between "missing a label" and "database is gone".

Both halves of this test call PRODUCTION code rather than re-deriving it
(CLAUDE.md: never let a test re-implement production logic):

  * required keys  -> `Translation._extract_literal_staff_keys` +
                      `_add_dynamic_family_keys`, i.e. the very functions
                      `/health` uses to decide it is degraded.
  * seeded keys    -> the seed script's own `_curated_value`, the single
                      resolver that understands `STAFF_TRANSLATIONS`, the
                      bare-and-prefixed `DELIVERY_TEXT_TRANSLATIONS` /
                      `OPERATOR_TEXT_TRANSLATIONS` catalogs, AND the dynamic
                      families generated at seed time.

Modelling either side by hand gets it wrong, and drafts of this very check
proved it twice: a regex over the file reported 90 "missing" keys that were
all false positives (it did not know about the prefixed sub-catalogs), and a
second attempt using `_add_curated_keys` alone falsely accused every
`staff.delivery.payment.*` dynamic-family key.
"""

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SEED_SCRIPT = ROOT / "scripts" / "seed_staff_translations.py"
STAFF_ROOT = ROOT / "staff_bot"

LANGUAGES = ("en", "uz", "ru")


def _load_seed_script():
    spec = importlib.util.spec_from_file_location("seed_staff_translations", SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _required_keys():
    """The exact set `/health` checks against."""
    from staff_bot.i18n import Translation

    keys = Translation._extract_literal_staff_keys(STAFF_ROOT)
    Translation._add_dynamic_family_keys(keys)
    return keys


def _satellite_keys():
    """Keys owned by the OTHER `staff_bot`-category seed scripts.

    `seed_staff_translations.py` is not the whole picture: place/cluster/COD
    strings live in `seed_place_group_staff_translations.py` and the
    over-returned strings in `seed_staff_over_returned_translations.py`, both
    seeding the same `staff_bot` category. The split is deliberate and
    enforced — `tests/integration/test_place_i18n_render_e2e.py` fails if the
    main script claims a place key, because the generator would clobber the
    curated place translations.

    Ignoring these scripts is not a small inaccuracy: it makes 10 perfectly
    well-seeded keys look missing. This test was written that way first, and
    "fixing" the phantom gap broke five place-contract tests.
    """
    keys = set()
    for name in ("seed_place_group_staff_translations", "seed_staff_over_returned_translations"):
        path = ROOT / "scripts" / f"{name}.py"
        if not path.exists():
            continue
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        keys |= set(getattr(module, "KEYS", {}))
    return keys


def _unresolvable(module, keys, language="en"):
    """Keys NO staff_bot seed script can produce a value for, in `language`.

    The main script's own `_curated_value` is the resolver that understands
    `STAFF_TRANSLATIONS`, the bare-and-prefixed `DELIVERY_TEXT_TRANSLATIONS` /
    `OPERATOR_TEXT_TRANSLATIONS` catalogs, AND the dynamic families
    (`staff.delivery.payment.*`, `staff.role.*`, ...) that are generated
    rather than written out. Satellite-script ownership is unioned on top.
    """
    satellites = _satellite_keys()
    return sorted(
        k for k in keys
        if module._curated_value(k, language) is None and k not in satellites
    )


@pytest.mark.unit
class TestStaffTranslationCatalogIsComplete:
    @pytest.mark.parametrize("language", LANGUAGES)
    def test_every_required_key_is_seeded(self, language):
        """A missing key here means staff_bot boots permanently unhealthy."""
        module = _load_seed_script()
        missing = _unresolvable(module, _required_keys(), language)
        assert not missing, (
            f"{len(missing)} staff translation key(s) are referenced by staff_bot but "
            f"absent from scripts/seed_staff_translations.py. Each one makes "
            f"GET /health return 503 forever (webhook_server.py health_handler), "
            f"which also hides the database check living in that same endpoint. "
            f"Missing: {missing}"
        )

    def test_the_specific_keys_that_caused_the_outage_are_present(self):
        """Named regression guard for the 2026-08-13 investigation.

        Kept explicit as well as covered by the set-difference test above: if
        someone ever deletes these while refactoring, the failure message
        should say which incident it reopens rather than just printing a diff.
        """
        module = _load_seed_script()
        for key in ("staff.tryout.outside_delivery_area",):
            assert module._curated_value(key, "en") is not None, (
                f"{key} was unseeded and 503'd /health on 2026-08-13"
            )


@pytest.mark.unit
class TestPlaceholdersMatchAcrossLanguages:
    def test_no_key_has_a_placeholder_in_one_language_only(self):
        """A `{name}` present in en but missing in ru is a KeyError at render
        time for Russian-speaking staff ONLY — invisible to anyone testing in
        English, and it reaches a driver standing at a customer's door."""
        module = _load_seed_script()
        catalogs = [
            ("", module.STAFF_TRANSLATIONS),
            ("staff.delivery.", module.DELIVERY_TEXT_TRANSLATIONS),
            ("staff.operator.", module.OPERATOR_TEXT_TRANSLATIONS),
        ]

        mismatches = {}
        for prefix, catalog in catalogs:
            for suffix, values in catalog.items():
                if not isinstance(values, dict):
                    continue
                sets = {
                    lang: set(re.findall(r"\{(\w+)\}", values[lang]))
                    for lang in LANGUAGES
                    if isinstance(values.get(lang), str)
                }
                if len(sets) == len(LANGUAGES) and len(set(map(frozenset, sets.values()))) > 1:
                    mismatches[f"{prefix}{suffix}"] = sets

        assert not mismatches, f"placeholder sets differ across languages: {mismatches}"
