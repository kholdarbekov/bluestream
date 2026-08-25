"""Trilingual rendering of EVERY place-related translation key — end to end.

WHAT MAKES THIS FILE DIFFERENT FROM THE EXISTING TRANSLATION GUARDS
-------------------------------------------------------------------
``tests/unit/test_place_group_translation_seeds.py`` (783 lines) and
``tests/unit/test_staff_bot_over_returned.py`` are excellent, and they are
*static*: they read the seed SCRIPTS as Python data and the JSX as text. Nothing
in them opens an app context or writes a row. That leaves a whole class of
production failure invisible, because the thing that renders a place screen is
not a script — it is a DATABASE ROW served through a real code path:

* a key added to a component but not to a script,
* a script edited and not re-run (so the DB and the JSX diverge),
* a category reassigned by whichever ``ui_*`` seed ran last,
* a language dropped from one row while ``/health`` stays green,
* a value cached in Redis before the copy changed,
* a language CODE the renderer cannot resolve.

Every test here therefore runs the REAL owning seed entry points against the
test database and then renders through a REAL path:

    real seed ``run()`` -> real ``translations`` rows
                        -> real ``TranslationService`` / real bot ``i18n.get``
                        -> real HTTP route (``/api/v1/translations/...``,
                           the three address-delete fences, ``/my-balances``)

Where a bot catalog is needed, the REAL ``telegram_bot/i18n.py`` /
``staff_bot/i18n.py`` ``Translation`` class is instantiated and its
``translations`` dict is filled from the rows the real seeds just wrote. Only
the asyncpg LOADER is bypassed (it needs a live Postgres connection); the
lookup, the fallback chain, the humanise branch and the format-swallow — i.e.
everything this axis is about — are the shipped code.

CONVENTIONS
-----------
* Expected values are DERIVED from the seeded rows (``_row('key','ru')``), not
  hand-copied, so a copy edit updates the expectation and a LOST ROW still fails.
* Translation caching is disabled by an autouse stub (``_no_translation_cache``)
  in every test except the ones whose SUBJECT is the cache. Without it a value
  cached earlier in the same test would mask a deleted row and the test would
  pass against broken code.
* ``xfail(strict=True)`` marks a CONFIRMED production defect: the assertion is
  what correct behaviour would look like, so the day it is fixed the suite tells
  you.
* Bottle figures are moved only through real service write paths. No test here
  hand-builds a ``BottleBalance`` row.

DELIBERATELY NOT COVERED HERE (see the file's closing note and the task report):
the real ``i18next`` instance behaviours (``parseMissingKeyHandler``,
``deepFind`` truthiness, plural resolution). Those live in JS and cannot be
executed from pytest; this file covers their DATABASE-side preconditions, which
is the half that decides whether they can ever fire.
"""

import copy
import html
import importlib.util
import logging
import pathlib
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# telegram_bot modules use workdir-relative BARE imports (`from i18n import
# i18n`), so they are not importable as `telegram_bot.i18n`. Same bootstrap as
# tests/integration/test_customer_bot_place_full_e2e.py:59-61.
if str(REPO_ROOT / "telegram_bot") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "telegram_bot"))

import i18n as tg_i18n_module  # noqa: E402  (telegram_bot/i18n.py)
import handlers.bottles as tg_bottles  # noqa: E402
import handlers.orders as tg_orders  # noqa: E402

from staff_bot import i18n as staff_i18n_module  # noqa: E402
from staff_bot.handlers.delivery.bottle_collection import BottleCollectionHandler  # noqa: E402
from staff_bot.utils import formatters as staff_formatters  # noqa: E402

from business_app.models.translation import Translation  # noqa: E402
from business_app.models.user import User, UserAddress  # noqa: E402
from business_app.services.bottle_tracking_service import BottleTrackingService  # noqa: E402
from business_app.services.customer_link_service import CustomerLinkService  # noqa: E402
from business_app.utils import translations as translations_util  # noqa: E402
from business_app.utils.exceptions import ValidationError  # noqa: E402
from business_app.utils.password_security import hash_password  # noqa: E402
from shared.enums import (  # noqa: E402
    BottleLedgerEventType,
    UserRole,
    UserStatus,
    UserType,
)

LANGUAGES = ("en", "uz", "ru")
LAT, LNG = 41.3111, 69.2797
TRANSLATIONS_URL = "/api/v1/translations/{lang}/{ns}"


# --------------------------------------------------------------------------- #
# Seed-script loading + the REAL seed entry points
# --------------------------------------------------------------------------- #
_MODULE_CACHE = {}


def _load_seed(name):
    """Import ``scripts/<name>.py``. Every script keeps its side effect behind
    ``if __name__ == "__main__":``, so importing writes nothing."""
    if name not in _MODULE_CACHE:
        path = REPO_ROOT / "scripts" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"_seed_{name}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MODULE_CACHE[name] = module
    return _MODULE_CACHE[name]


def _run_seed(app, name, entry="run"):
    """Execute a seed script's REAL entry point against the TEST app.

    ``run()``/``main()`` call ``create_app()`` themselves — pointed at the dev
    database. Swapping that one symbol keeps the whole body (the data assembly,
    the category, ``bulk_create_or_update``) exactly as it ships while writing
    into the test session.
    """
    module = _load_seed(name)
    original = getattr(module, "create_app")
    module.create_app = lambda *a, **k: app
    try:
        getattr(module, entry)()
    finally:
        module.create_app = original


def _seed_backend_slice(app, key):
    """Seed ONE key out of the canonical ``BACKEND_TRANSLATIONS``.

    Uses the script's OWN ``_category_for`` so the category the key lands in is
    the shipped rule (first key segment), not a guess — the whole point of the
    ownership tests below. Running the full 2000-key seeder per test would cost
    more than it proves.
    """
    mod = _load_seed("seed_backend_translations")
    row = mod.BACKEND_TRANSLATIONS[key]
    Translation.bulk_create_or_update(
        {lang: {key: row[lang]} for lang in LANGUAGES},
        category=mod._category_for(key),
    )


def _seed_backend_place_slice(app):
    """The ``api.addresses.error.in_place_group`` slice of the backend seed."""
    _seed_backend_slice(app, "api.addresses.error.in_place_group")


# The scripts that OWN place copy, in the order a deploy runbook lists them.
PLACE_SEED_SCRIPTS = (
    ("seed_place_group_telegram_translations", "run"),
    ("seed_bottle_ledger_translations", "main"),
    ("seed_place_group_staff_translations", "run"),
    ("seed_staff_over_returned_translations", "run"),
    ("seed_place_group_ui_translations", "run"),
    ("seed_customer_map_translations", "run"),
    ("seed_ui_staff_translations", "main"),
    ("seed_ui_bottle_tracking_linked_accounts", "run"),
)


@pytest.fixture
def place_seeds(app, db):
    """Every script that owns a place key, run through its real entry point."""
    for name, entry in PLACE_SEED_SCRIPTS:
        _run_seed(app, name, entry)
    _seed_backend_place_slice(app)
    return True


@pytest.fixture
def place_seeds_full(app, place_seeds):
    """``place_seeds`` plus the big ``ui_bottle_tracking`` page seed.

    Separate because it writes ~150 extra keys and only the ``event_*`` /
    ``common``-union tests need them.
    """
    _run_seed(app, "seed_ui_bottle_tracking_translations", "main")
    return True


# --------------------------------------------------------------------------- #
# Row access helpers — expectations are DERIVED, never hand-copied
# --------------------------------------------------------------------------- #
def _row(key, language):
    t = Translation.query.filter_by(key=key, language=language, is_active=True).first()
    assert t is not None, f"no active row for {key}:{language}"
    return t.value


def _rows(key):
    return {lang: _row(key, lang) for lang in LANGUAGES}


def _drop(key, language=None):
    q = Translation.query.filter_by(key=key)
    if language:
        q = q.filter_by(language=language)
    for row in q.all():
        from business_app import db as _db

        _db.session.delete(row)
    from business_app import db as _db

    _db.session.commit()


def _placeholders(value):
    """``str.format`` single-brace placeholder names (both bots)."""
    return set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", value))


def _i18next_placeholders(value):
    return set(re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", value))


def _literal_fragments(value, min_length=3):
    """The template's non-placeholder runs — what identifies it in a render.

    A leading-stem check (``value.split('{')[0]``) is empty for any template
    that STARTS with a placeholder, which silently turns an absence assertion
    into a no-op. Splitting on every placeholder keeps such templates testable.
    Runs shorter than ``min_length`` (``')'``, ``' · '``) are dropped: they are
    punctuation that legitimately occurs elsewhere on the same screen.
    """
    return [
        fragment
        for fragment in re.split(r"\{[^{}]*\}", value)
        if len(fragment.strip()) >= min_length
    ]


def _humanised(key):
    """What BOTH bots print when a key is missing in every language."""
    last = key.rsplit(".", 1)[-1] if "." in key else key
    return last.replace("_", " ").capitalize()


# --------------------------------------------------------------------------- #
# The key inventory, with the kwargs its REAL call site passes
# --------------------------------------------------------------------------- #
# telegram_bot: key -> (kwargs the handler passes, module:line of the call site)
TELEGRAM_PLACE_CALLS = {
    "telegram.bottles.place_total": {"total": "7"},
    "telegram.bottles.member_line": {"name": "Alice"},
    "telegram.bottles.cluster_total": {"total": "12"},
    "telegram.bottles.linked_account_line": {"address": "Office", "owner": "Bob"},
    "telegram.orders.cod_restricted_place": {"place_active_cod_debt_count": 3},
    "telegram.payments.cluster_debt_total": {"total": "15 000"},
    "telegram.payments.place_debt_total": {"label": "Office 7", "total": "35 000"},
    "telegram.payments.place_order_line": {
        "order_number": "ORD-1",
        "member_name": "Alice",
        "amount": "15 000",
    },
}

# staff_bot label-only keys: the CALLER appends the number, so `.format()` is
# never invoked and a stray `{x}` would reach the driver's screen literally.
STAFF_LABEL_ONLY_KEYS = (
    "staff.delivery.place_cod_total",
    "staff.delivery.cluster_debt_total",
    "staff.delivery.cluster_members",
    "staff.delivery.account_cod_debts",
    # The COD statement headline. An untranslated row here shows a Russian
    # driver an English word attached to the one figure they act on.
    "staff.delivery.collectible_now",
)

STAFF_PLACE_CALLS = {
    "staff.delivery.fine_place_union_hint": {"union": "7"},
    "staff.delivery.fine_place_over_returned_hint": {"union": "3"},
    "staff.delivery.place_over_returned": {"count": "3"},
    "staff.delivery.bottles_return_prompt_over_returned": {"count": "2"},
    "staff.delivery.bottle_collection_recorded_over_returned": {
        "quantity": 4,
        "remaining": "3",
    },
    "staff.operator.cod_restricted_place": {"place_active_cod_debt_count": 3},
}

ALL_STAFF_PLACE_KEYS = tuple(STAFF_LABEL_ONLY_KEYS) + tuple(STAFF_PLACE_CALLS)

# Pre-existing staff keys that place surfaces RE-USE. A place-specific test that
# ignores them would miss an English fragment inside an otherwise-Uzbek screen.
STAFF_REUSED_ON_PLACE_SURFACES = (
    # `staff.delivery.total_outstanding` used to head this list. It labelled the
    # RAW per-account, PENDING-inclusive engine figure on the COD statement
    # screen while "Collect full" priced itself through `_scoped_ceiling` — the
    # fifth show-vs-settle split. The headline is now
    # `staff.delivery.collectible_now` (in STAFF_LABEL_ONLY_KEYS above), so
    # nothing under staff_bot/ reads the old key and `_extract_literal_staff_keys`
    # can no longer see it: leaving it here fails
    # `test_the_staff_key_extractor_still_finds_every_place_key_on_disk`, which
    # is the check that would have caught a key deleted from its call site.
    "staff.delivery.no_cod_debt",
    "staff.order.unknown",
    "staff.common.not_available",
    "staff.delivery.bottles_return_prompt_no_balance",
    "staff.delivery.bottle_collection_recorded",
    "staff.delivery.cod_statement_title",
)

BARE_BOTTLE_TRACKING_PLACE_KEYS = (
    "members",
    "place_balance_label",
    "place_ledger_heading",
    "linked_accounts_alert_title",
    "linked_member_count_label",
    "grouped_tag",
)

UI_STAFF_PLACE_KEYS = ("scope", "attribution")

FENCE_KEY = "api.addresses.error.in_place_group"

# Codes the place surfaces can raise, and the admin-UI key that translates them.
MAPPED_FENCE_CODES = {
    "PLACE_GROUP_GROCERY_MEMBER": "ui.users.place_groups.error_grocery_member",
    "PLACE_GROUP_ENTITY_MEMBER": "ui.users.place_groups.error_entity_member",
    "PLACE_GROUP_ADDRESS_ALREADY_GROUPED": "ui.users.place_groups.error_already_grouped",
    "PLACE_GROUP_NOT_FOUND": "ui.users.place_groups.error_group_not_found",
    "CUSTOMER_LINK_ADDRESS_NOT_FOUND": "ui.users.place_groups.error_address_not_found",
    "PLACE_SPLIT_INVALID": "ui.users.place_groups.error_place_split_invalid",
    "MERGE_PREVIEW_STALE": "ui.users.place_groups.error_merge_preview_stale",
    "MERGE_EXCLUSION_NOT_ELIGIBLE": "ui.users.place_groups.error_merge_exclusion",
    "MERGE_REASON_REQUIRED": "ui.users.place_groups.error_merge_reason",
    "PLACE_GROUP_MIN_ADDRESSES": "ui.users.place_groups.error_min_addresses",
    "PLACE_GROUP_REASON_REQUIRED": "ui.users.place_groups.error_reason_required",
}

# CustomerLinkEvent.event_type values the place-group audit list renders.
PLACE_GROUP_EVENT_TYPES = (
    "create_place_group",
    "add_to_place_group",
    "remove_from_place_group",
    "dismiss_place_suggestion",
    "link",
    "unlink",
    "dismiss",
)

# The place vocabulary that was DELETED with its call sites. A row left behind
# is inherited verbatim by any future component that re-adds the key.
DEAD_PLACE_VOCABULARY = (
    "ui.users.place_groups.member_balance",
    "customers_with_balance",
    "fine_place_union_balance_label",
    "combined_at_place_label",
    "this_account_only_label",
    "combined_cluster_balance_label",
    "cluster_ledger_heading",
    "user_id_label",
    "address_ledger_title",
)


def _ui_place_keys():
    return set(_load_seed("seed_place_group_ui_translations").KEYS)


_JS_COMMENT = r"(?:\s*//[^\n]*\n)*\s*"
_PANEL_ERROR_ENTRY = re.compile(
    r"\[{c}'([A-Z_]+)'\s*,{c}\[{c}'(ui\.[A-Za-z0-9_.]+)'\s*,".format(c=_JS_COMMENT), re.S
)


def _place_group_error_map():
    """``PLACE_GROUP_ERROR_MESSAGES`` as ``{error_code: translation_key}``.

    Parsed, not substring-matched: the thing that breaks a uz/ru admin's screen
    is a code paired with the WRONG key, and two separate ``token in source``
    checks cannot see that.

    Reads ``placeGroupCopy.js``: the map was EXTRACTED out of
    ``PlaceGroupPanel.jsx`` (plan E task A1.3) so the per-customer panel and the
    estate-wide "Grouped Addresses" tab share one fence-code vocabulary instead
    of two. Same map, same assertion — only the file moved.
    """
    copy = (REPO_ROOT / "admin_ui" / "src" / "components" / "placeGroupCopy.js").read_text(
        encoding="utf-8"
    )
    body = copy.split("PLACE_GROUP_ERROR_MESSAGES", 1)[1].split("]);", 1)[0]
    return dict(_PANEL_ERROR_ENTRY.findall(body))


# --------------------------------------------------------------------------- #
# Cache control
# --------------------------------------------------------------------------- #
class _NoCache:
    """A Redis stand-in that never remembers anything.

    ``TranslationService`` caches every hit for up to 24h. Inside a single test
    that turns "delete the row and look again" into "read the cached copy", so a
    test written to prove a fallback would pass against a broken fallback.
    """

    def get(self, _key):
        return None

    def setex(self, *_a, **_k):
        return True

    def delete(self, *_a, **_k):
        return 1

    def keys(self, *_a, **_k):
        return []


class _DictRedis:
    """A real-enough Redis for the cache-invalidation tests."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        value = self.store.get(key)
        return value.encode("utf-8") if isinstance(value, str) else value

    def setex(self, key, _ttl, value):
        self.store[key] = value
        return True

    def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)
        return len(keys)

    def keys(self, pattern):
        prefix = pattern.rstrip("*")
        return [k for k in self.store if k.startswith(prefix)]


@pytest.fixture(autouse=True)
def _no_translation_cache(monkeypatch):
    monkeypatch.setattr(translations_util.translation_service, "redis_client", _NoCache())
    yield
    translations_util.translation_service.redis_client = None


# --------------------------------------------------------------------------- #
# Bot catalogs, loaded from the rows the real seeds just wrote
# --------------------------------------------------------------------------- #
def _catalog_from_db(*categories, key_prefixes=()):
    tables = {lang: {} for lang in LANGUAGES}
    rows = Translation.query.filter(
        Translation.is_active.is_(True),
        Translation.category.in_(categories),
    ).all()
    for row in rows:
        if key_prefixes and not row.key.startswith(key_prefixes):
            continue
        if row.language in tables:
            tables[row.language][row.key] = row.value
    return tables


def _tg_i18n(*, fallback="uz", drop=()):
    """The REAL ``telegram_bot/i18n.py`` ``Translation``, DB-backed."""
    instance = tg_i18n_module.Translation()
    instance.translations = _catalog_from_db("telegram")
    instance.fallback_language = fallback
    for key, lang in drop:
        for language in ([lang] if lang else LANGUAGES):
            instance.translations.get(language, {}).pop(key, None)
    return instance


def _staff_i18n(*, fallback="uz", drop=()):
    """The REAL ``staff_bot/i18n.py`` ``Translation``, DB-backed."""
    instance = staff_i18n_module.Translation()
    instance.translations = _catalog_from_db("staff_bot")
    instance.fallback_language = fallback
    for key, lang in drop:
        for language in ([lang] if lang else LANGUAGES):
            instance.translations.get(language, {}).pop(key, None)
    return instance


@pytest.fixture
def tg_i18n(place_seeds):
    return _tg_i18n()


@pytest.fixture
def staff_i18n(place_seeds):
    return _staff_i18n()


@pytest.fixture
def patched_tg_i18n(monkeypatch, tg_i18n):
    """Point the telegram handlers' module-global ``i18n`` at the DB catalog."""
    monkeypatch.setattr(tg_bottles, "i18n", tg_i18n)
    monkeypatch.setattr(tg_orders, "i18n", tg_i18n)
    return tg_i18n


@pytest.fixture
def patched_staff_i18n(monkeypatch, staff_i18n):
    monkeypatch.setattr(staff_formatters, "i18n", staff_i18n)
    import staff_bot.handlers.delivery.bottle_collection as bc_mod
    import staff_bot.handlers.operator.create_order as co_mod

    monkeypatch.setattr(bc_mod, "i18n", staff_i18n)
    monkeypatch.setattr(co_mod, "i18n", staff_i18n)
    return staff_i18n


# --------------------------------------------------------------------------- #
# World builders — bottles move only through real service write paths
# --------------------------------------------------------------------------- #
def _user(db, email, phone, *, language="uz", role=UserRole.CUSTOMER):
    user = User(
        email=email,
        phone=phone,
        password_hash=hash_password("TestPassword123!"),
        first_name="T",
        last_name=email.split("@")[0],
        user_type=UserType.INDIVIDUAL,
        role=role,
        status=UserStatus.ACTIVE,
        is_verified=True,
        preferred_language=language,
        created_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.commit()
    return user


def _addr(db, user, title="Office"):
    address = UserAddress(
        user_id=user.id,
        title=title,
        full_address="Office, Tashkent",
        city="Tashkent",
        latitude=LAT,
        longitude=LNG,
    )
    db.session.add(address)
    db.session.commit()
    return address


def _put_bottles(db, address, user, qty):
    BottleTrackingService().admin_adjust_balance(
        user_id=user.id,
        address_id=address.id,
        adjustment=Decimal(qty),
        actor_user_id=user.id,
        notes="seed for i18n render",
    )
    db.session.commit()


def _headers(app, user):
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _admin_headers(app, user):
    """``manager_or_higher_required`` reads the role off the JWT CLAIMS.

    Without the claim the admin delete route 403s before ever reaching the
    place-group fence, so a language assertion would be vacuous.
    """
    with app.app_context():
        token = create_access_token(
            identity=str(user.id), additional_claims={"role": UserRole.ADMIN.value}
        )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ===========================================================================
# A. The seed round-trip: every place key is a LIVE, trilingual, active row
# ===========================================================================
@pytest.mark.integration
@pytest.mark.parametrize("key", sorted(TELEGRAM_PLACE_CALLS))
def test_customer_bot_place_key_is_a_live_trilingual_telegram_row(place_seeds, key):
    rows = Translation.query.filter_by(key=key).all()
    assert {r.language for r in rows} == set(LANGUAGES), f"{key}: {[r.language for r in rows]}"
    for row in rows:
        assert row.is_active is True, f"{key}:{row.language} is_active={row.is_active}"
        assert row.value.strip(), f"{key}:{row.language} is blank"
        assert row.category == "telegram", f"{key}:{row.language} category={row.category}"


@pytest.mark.integration
@pytest.mark.parametrize("key", sorted(ALL_STAFF_PLACE_KEYS))
def test_staff_bot_place_key_is_a_live_trilingual_staff_bot_row(place_seeds, key):
    rows = Translation.query.filter_by(key=key).all()
    assert {r.language for r in rows} == set(LANGUAGES), f"{key}: {[r.language for r in rows]}"
    for row in rows:
        assert row.is_active is True
        assert row.value.strip(), f"{key}:{row.language} is blank"
        assert row.category == "staff_bot", f"{key}:{row.language} category={row.category}"


@pytest.mark.integration
def test_every_dotted_admin_place_key_is_a_live_trilingual_ui_row(place_seeds):
    """Category MUST be exactly ``ui``.

    ``AdminUiTranslationService`` routes a dotted ``ui.users.*`` key into the
    ``users`` namespace only when ``category == 'ui'`` — a plausible
    "improvement" to ``ui_users`` makes every one of these vanish from the
    bundle while every static test still passes.
    """
    keys = _ui_place_keys() | {"ui.users.map.shared_place"}
    for key in sorted(keys):
        rows = Translation.query.filter_by(key=key).all()
        assert {r.language for r in rows} == set(LANGUAGES), f"{key}: {[r.language for r in rows]}"
        for row in rows:
            assert row.is_active is True
            assert row.value.strip(), f"{key}:{row.language} is blank"
            assert row.category == "ui", f"{key}:{row.language} category={row.category}"


@pytest.mark.integration
def test_every_bare_place_key_lands_in_its_scoped_ui_category(place_seeds):
    """BARE keys resolve by CATEGORY alone — there is no prefix to fall back on."""
    for key in BARE_BOTTLE_TRACKING_PLACE_KEYS:
        rows = Translation.query.filter_by(key=key).all()
        assert {r.language for r in rows} == set(LANGUAGES), key
        for row in rows:
            assert row.category == "ui_bottle_tracking", f"{key}:{row.language} -> {row.category}"
            assert row.value.strip(), f"{key}:{row.language} is blank"
    for key in UI_STAFF_PLACE_KEYS:
        rows = Translation.query.filter_by(key=key).all()
        assert {r.language for r in rows} == set(LANGUAGES), key
        for row in rows:
            assert row.category == "ui_staff", f"{key}:{row.language} -> {row.category}"
            assert row.value.strip(), f"{key}:{row.language} is blank"


@pytest.mark.integration
def test_the_fence_key_lands_in_the_api_category_in_all_three_languages(place_seeds):
    rows = Translation.query.filter_by(key=FENCE_KEY).all()
    assert {r.language for r in rows} == set(LANGUAGES)
    assert {r.category for r in rows} == {"api"}


@pytest.mark.integration
def test_every_place_key_has_exactly_one_owning_category(place_seeds_full):
    """``translations`` is unique on ``(key, language)`` only, and
    ``bulk_create_or_update`` REASSIGNS ``category``.

    So one row can have two claimed owners: last seed wins and the loser's
    namespace bundle silently loses the key. This already happened once with
    ``phone``. Asserted against the DATABASE after every owning script has run,
    in the runbook's order — a script-only check cannot see the outcome.
    """
    tracked = (
        set(TELEGRAM_PLACE_CALLS)
        | set(ALL_STAFF_PLACE_KEYS)
        | _ui_place_keys()
        | {"ui.users.map.shared_place", FENCE_KEY}
        | set(BARE_BOTTLE_TRACKING_PLACE_KEYS)
        | set(UI_STAFF_PLACE_KEYS)
    )
    conflicts = {}
    for key in sorted(tracked):
        categories = {r.category for r in Translation.query.filter_by(key=key).all()}
        if len(categories) != 1:
            conflicts[key] = sorted(categories)
    assert not conflicts, f"keys whose rows disagree about their owner: {conflicts}"


@pytest.mark.integration
def test_running_every_place_seed_twice_is_idempotent(app, place_seeds):
    """Deploy runbooks re-run seeds; nothing enforces the order."""
    before = {
        (r.key, r.language): (r.value, r.category, r.is_active)
        for r in Translation.query.all()
    }
    for name, entry in PLACE_SEED_SCRIPTS:
        _run_seed(app, name, entry)
    _seed_backend_place_slice(app)
    after = {
        (r.key, r.language): (r.value, r.category, r.is_active)
        for r in Translation.query.all()
    }
    assert after == before


@pytest.mark.integration
def test_reversing_the_seed_order_does_not_re_home_a_place_key(app, db):
    """The reverse permutation, because no runbook enforces an order.

    ``seed_backend_translations._category_for`` derives the category from the
    key's first segment — a DIFFERENT rule from the scoped scripts. Any overlap
    would silently re-home a key depending on which seed ran last.
    """
    _seed_backend_place_slice(app)
    for name, entry in reversed(PLACE_SEED_SCRIPTS):
        _run_seed(app, name, entry)
    assert {r.category for r in Translation.query.filter_by(key=FENCE_KEY).all()} == {"api"}
    for key in sorted(_ui_place_keys()):
        assert {r.category for r in Translation.query.filter_by(key=key).all()} == {"ui"}, key
    for key in BARE_BOTTLE_TRACKING_PLACE_KEYS:
        assert {r.category for r in Translation.query.filter_by(key=key).all()} == {
            "ui_bottle_tracking"
        }, key


@pytest.mark.integration
def test_the_deleted_place_vocabulary_is_not_resurrected_by_any_seed(place_seeds_full):
    """Nine keys were deleted WITH their call sites.

    Every existing guard reads the SCRIPTS, so a re-added key would inherit
    stale people-keyed wording ("This account only", "Linked accounts detected")
    that the re-key exists to eliminate. Checked in the DB, which is where a
    resurrected row would actually live.
    """
    resurrected = [
        key for key in DEAD_PLACE_VOCABULARY if Translation.query.filter_by(key=key).count()
    ]
    assert not resurrected, f"dead place vocabulary re-seeded: {resurrected}"


# ===========================================================================
# B. Namespace routing through the REAL /translations endpoint
# ===========================================================================
@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_users_namespace_serves_every_dotted_place_key(client, place_seeds, lang):
    bundle = client.get(TRANSLATIONS_URL.format(lang=lang, ns="users")).get_json()
    expected = {k for k in _ui_place_keys() if k.startswith("ui.users.")}
    expected |= {"ui.users.map.shared_place"}
    missing = sorted(expected - set(bundle))
    assert not missing, f"users/{lang} bundle is missing {len(missing)} place keys: {missing[:8]}"
    for key in expected:
        assert bundle[key] == _row(key, lang)
        assert bundle[key].strip()


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_orders_namespace_serves_the_scope_tags_and_all_six_cash_warnings(
    client, place_seeds, lang
):
    bundle = client.get(TRANSLATIONS_URL.format(lang=lang, ns="orders")).get_json()
    expected = {k for k in _ui_place_keys() if k.startswith("ui.orders.")}
    assert len({k for k in expected if k.startswith("ui.orders.cash_warning_")}) == 6
    missing = sorted(expected - set(bundle))
    assert not missing, f"orders/{lang} is missing {missing}"
    for key in expected:
        assert bundle[key] == _row(key, lang)


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_common_namespace_carries_ok_and_the_prepayments_cluster_tag(client, place_seeds, lang):
    """``ui.prepayments.*`` has NO entry in ``LEGACY_NAMESPACE_PREFIXES``.

    It reaches Prepayments.js only because that page opens ``common``, which is
    the union of category ``ui`` plus every ``ui_*``. Anyone "tidying"
    Prepayments.js to ``useTranslation('prepayments')`` un-translates the page.
    """
    bundle = client.get(TRANSLATIONS_URL.format(lang=lang, ns="common")).get_json()
    assert bundle.get("ui.common.ok") == _row("ui.common.ok", lang)
    assert bundle.get("ui.prepayments.linked_accounts") == _row(
        "ui.prepayments.linked_accounts", lang
    )
    assert "ui.prepayments.linked_accounts" not in client.get(
        TRANSLATIONS_URL.format(lang=lang, ns="prepayments")
    ).get_json()


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_bottle_tracking_namespace_serves_every_bare_place_key(client, place_seeds, lang):
    bundle = client.get(TRANSLATIONS_URL.format(lang=lang, ns="bottle_tracking")).get_json()
    for key in BARE_BOTTLE_TRACKING_PLACE_KEYS:
        assert bundle.get(key) == _row(key, lang), f"bottle_tracking/{lang} lost {key}"


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_staff_namespace_serves_the_delivery_report_scope_columns(client, place_seeds, lang):
    """``scope`` / ``attribution`` are BARE and live in ``ui_staff``, unlike
    every other place key. Seeding them as ``ui.*`` (what every neighbour does)
    leaves both column headers English forever."""
    bundle = client.get(TRANSLATIONS_URL.format(lang=lang, ns="staff")).get_json()
    for key in UI_STAFF_PLACE_KEYS:
        assert bundle.get(key) == _row(key, lang), f"staff/{lang} lost {key}"


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_delivery_reports_resolves_the_scope_columns_through_common_too(
    client, place_seeds, lang
):
    """DeliveryReports.js opens ``useTranslation(['staff','common'])``.

    ``common`` is the union of every ``ui_*`` category, so both bundles must
    carry the columns; any narrowing of the union silently un-translates the
    delivery-report scope column for uz/ru admins.
    """
    common = client.get(TRANSLATIONS_URL.format(lang=lang, ns="common")).get_json()
    for key in UI_STAFF_PLACE_KEYS:
        assert common.get(key) == _row(key, lang), f"common/{lang} lost {key}"


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_multi_namespace_load_path_serves_identical_place_bundles(client, place_seeds, lang):
    """``allowMultiLoading: true`` means production ONLY uses the ``+`` branch."""
    names = ["common", "users", "orders", "bottle_tracking", "staff"]
    multi = client.get(
        TRANSLATIONS_URL.format(lang=lang, ns="+".join(names))
    ).get_json()
    assert set(multi) == {lang}, multi.keys()
    for ns in names:
        single = client.get(TRANSLATIONS_URL.format(lang=lang, ns=ns)).get_json()
        assert multi[lang][ns] == single, f"{lang}/{ns} differs between the two load paths"


@pytest.mark.integration
def test_an_unsupported_language_code_yields_an_empty_place_bundle(client, place_seeds):
    """A stored ``kk`` preference degrades to raw identifiers, not a crash."""
    assert client.get(TRANSLATIONS_URL.format(lang="kk", ns="users")).get_json() == {}
    multi = client.get(TRANSLATIONS_URL.format(lang="kk", ns="users+orders")).get_json()
    assert multi == {"kk": {"users": {}, "orders": {}}}


@pytest.mark.integration
def test_a_db_failure_loading_the_place_bundle_returns_200_with_no_error_signal(
    client, place_seeds, monkeypatch
):
    """Total copy loss that presents as a working page.

    ``api/translations.py`` swallows every exception to keep i18next alive.
    Combined with the admin UI's ``parseMissingKeyHandler`` returning the KEY,
    a transient DB blip renders the whole place panel as
    ``ui.users.place_groups.*`` strings — with HTTP 200 and a green health
    check. Pinned so an operator can recognise it; the only signal is a log line.
    """
    from business_app.services.admin_ui_translation_service import AdminUiTranslationService

    def _boom(*_a, **_k):
        raise RuntimeError("simulated DB blip")

    monkeypatch.setattr(AdminUiTranslationService, "get_translations", _boom)

    # ``caplog`` cannot see this record: ``business_app/utils/logging_config.py``
    # attaches its own JSON handler and sets propagate=False, so it never
    # reaches pytest's root capture. Attach a collector to the exact logger the
    # route uses instead — that asserts the real emission, not a side channel.
    #
    # ORDER INDEPENDENCE. ``business_app/migrations/env.py:14`` calls
    # ``logging.config.fileConfig(alembic.ini)`` and alembic.ini names only
    # root/sqlalchemy/alembic/flask_migrate, so ``disable_existing_loggers``
    # (True by default) sets ``disabled = True`` on EVERY other already-created
    # logger in the process — including this route's. Any test that runs
    # ``flask_migrate.upgrade()`` in-process (tests/integration/
    # test_migrations_roundtrip.py, tests/integration/test_place_concurrency_pg_e2e.py)
    # therefore silences this logger for the rest of the worker, which made this
    # test pass alone and fail in a full run. That is pytest-process pollution,
    # not production behaviour (prod runs migrations in a separate container),
    # so the logger is restored to its shipped state for the duration of the
    # capture and put back afterwards. The assertion below is unchanged.
    collected = []

    class _Collector(logging.Handler):
        def emit(self, record):
            collected.append(record)

    route_logger = logging.getLogger("business_app.api.translations")
    was_disabled, was_level = route_logger.disabled, route_logger.level
    handler = _Collector(level=logging.ERROR)
    route_logger.addHandler(handler)
    route_logger.disabled = False
    route_logger.setLevel(logging.ERROR)
    try:
        assert route_logger.isEnabledFor(logging.ERROR), (
            "pre-condition: the route logger must be able to emit, otherwise the "
            "assertion below would be vacuous"
        )
        single = client.get(TRANSLATIONS_URL.format(lang="ru", ns="users"))
        multi = client.get(TRANSLATIONS_URL.format(lang="ru", ns="users+orders"))
    finally:
        route_logger.removeHandler(handler)
        route_logger.disabled = was_disabled
        route_logger.setLevel(was_level)

    messages = [record.getMessage() for record in collected]
    assert single.status_code == 200 and single.get_json() == {}
    assert multi.status_code == 200 and multi.get_json() == {"ru": {}}
    assert "Error loading translations for ru/users: simulated DB blip" in messages, (
        f"the ONLY signal of total place-copy loss is a server log line: {messages}"
    )
    assert (
        "Error loading translations for ru/users+orders: simulated DB blip" in messages
    ), messages
    # Exactly one record per request, both at ERROR, both from the route's own
    # logger: a 200 with an empty body and NOTHING else to tell them apart.
    assert len(collected) == 2, messages
    assert {record.levelno for record in collected} == {logging.ERROR}, messages
    assert {record.name for record in collected} == {"business_app.api.translations"}


@pytest.mark.integration
def test_a_blank_place_value_survives_the_transport_and_reaches_i18next(client, place_seeds):
    """``translations.value`` is only NOT NULL — never non-blank.

    The admin translations page can write an empty string, the endpoint serves
    it verbatim, and i18next's ``deepFind`` truthiness test then renders the raw
    identifier. The DB-level non-empty assertion above is the ONLY defence, so
    this test pins that nothing downstream rescues a cleared field.
    """
    row = Translation.query.filter_by(key="place_balance_label", language="ru").one()
    row.value = ""
    from business_app import db as _db

    _db.session.commit()

    bundle = client.get(TRANSLATIONS_URL.format(lang="ru", ns="bottle_tracking")).get_json()
    assert bundle["place_balance_label"] == "", (
        "a blank value is served as-is; only a DB-side non-empty check can stop it"
    )


@pytest.mark.integration
def test_deactivating_a_place_row_removes_it_from_the_bundle_entirely(client, place_seeds):
    row = Translation.query.filter_by(
        key="ui.users.place_groups.merge_drift_hint", language="uz"
    ).one()
    row.is_active = False
    from business_app import db as _db

    _db.session.commit()

    uz = client.get(TRANSLATIONS_URL.format(lang="uz", ns="users")).get_json()
    ru = client.get(TRANSLATIONS_URL.format(lang="ru", ns="users")).get_json()
    assert "ui.users.place_groups.merge_drift_hint" not in uz
    assert "ui.users.place_groups.merge_drift_hint" in ru, "only the uz row was deactivated"


# ===========================================================================
# C. Backend get_translation semantics for the fence key
# ===========================================================================
@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_fence_key_resolves_to_its_own_language(place_seeds, lang):
    assert translations_util.get_translation(FENCE_KEY, lang) == _row(FENCE_KEY, lang)


@pytest.mark.integration
def test_a_missing_en_row_silently_renders_uzbek_not_english_and_not_the_key(place_seeds):
    """The fallback chain is ``['uz','en','ru']`` with the request removed.

    So ``uz`` is tried FIRST for an ENGLISH request: a missing ``en`` row never
    surfaces as a missing key — the customer just silently gets Uzbek. Nothing
    else in the suite proves this direction.
    """
    english = _row(FENCE_KEY, "en")
    uzbek = _row(FENCE_KEY, "uz")
    _drop(FENCE_KEY, "en")

    resolved = translations_util.get_translation(FENCE_KEY, "en")

    assert resolved == uzbek
    assert resolved != english
    assert resolved != FENCE_KEY


@pytest.mark.integration
def test_deactivating_the_en_row_has_the_same_silent_uzbek_outcome(place_seeds):
    """``is_active=False`` is what the admin translations page writes, not DELETE."""
    row = Translation.query.filter_by(key=FENCE_KEY, language="en").one()
    row.is_active = False
    from business_app import db as _db

    _db.session.commit()

    assert translations_util.get_translation(FENCE_KEY, "en") == _row(FENCE_KEY, "uz")


@pytest.mark.integration
def test_an_unknown_language_code_walks_the_uz_en_ru_chain(place_seeds):
    assert translations_util.get_translation(FENCE_KEY, "kk") == _row(FENCE_KEY, "uz")
    uz = _row(FENCE_KEY, "uz")
    _drop(FENCE_KEY, "uz")
    assert translations_util.get_translation(FENCE_KEY, "kk") == _row(FENCE_KEY, "en")
    assert translations_util.get_translation(FENCE_KEY, "kk") != uz


@pytest.mark.integration
def test_a_fully_unseeded_fence_key_returns_the_key_itself(db):
    """Which is exactly what the fence's ``if message == key`` guard needs."""
    assert translations_util.get_translation(FENCE_KEY, "ru") == FENCE_KEY


# ===========================================================================
# D. The delete fence over REAL HTTP, in the caller's language
# ===========================================================================
@pytest.fixture
def grouped_place(db, admin_user):
    """Two coworkers at one place, 7 bottles in the pool, via real write paths."""
    admin = admin_user
    alice = _user(db, "i18n-alice@example.com", "+998900007701")
    bob = _user(db, "i18n-bob@example.com", "+998900007702")
    addr_a = _addr(db, alice, title="Acme office")
    addr_b = _addr(db, bob, title="Acme office")
    _put_bottles(db, addr_a, alice, "4")
    _put_bottles(db, addr_b, bob, "3")
    CustomerLinkService().create_place_group(
        [addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="coworkers", label="Acme office"
    )
    db.session.refresh(addr_a)
    db.session.refresh(addr_b)
    assert addr_a.address_group_id is not None
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("7.00")
    return {"admin": admin, "alice": alice, "bob": bob, "addr_a": addr_a, "addr_b": addr_b}


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_bot_delete_path_returns_the_fence_message_in_the_customers_language(
    app, client, place_seeds, grouped_place, lang
):
    """The Telegram bot deletes via this route and renders ``message`` VERBATIM.

    ``assert_address_not_in_place_group`` calls ``get_translation(key)`` with NO
    language, so the copy depends entirely on the ``before_request`` hook
    resolving ``g.language`` from the JWT's user.
    """
    alice = grouped_place["alice"]
    alice.preferred_language = lang
    from business_app import db as _db

    _db.session.commit()

    response = client.delete(
        f"/api/v1/auth/addresses/{grouped_place['addr_a'].id}", headers=_headers(app, alice)
    )

    body = response.get_json()
    assert response.status_code == 400
    assert body["error_code"] == "PLACE_GROUP_ADDRESS_NOT_DELETABLE"
    assert body["message"] == _row(FENCE_KEY, lang)
    assert UserAddress.query.get(grouped_place["addr_a"].id) is not None


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_addresses_delete_path_hides_the_translated_message_inside_errors(
    app, client, place_seeds, grouped_place, lang
):
    """The SECOND fenced entry point uses a DIFFERENT envelope.

    ``validation_error_response(errors={'address': msg})`` puts the translated
    sentence in ``errors[0]`` behind an ``address: `` prefix and leaves
    ``message`` as the untranslatable literal "Validation failed". A client
    reading only ``message`` shows the customer nothing actionable, in every
    language.
    """
    alice = grouped_place["alice"]
    alice.preferred_language = lang
    from business_app import db as _db

    _db.session.commit()

    response = client.delete(
        f"/api/v1/addresses/{grouped_place['addr_a'].id}", headers=_headers(app, alice)
    )

    body = response.get_json()
    assert response.status_code == 400
    assert body["data"]["error_code"] == "PLACE_GROUP_ADDRESS_NOT_DELETABLE"
    assert body["errors"] == [f"address: {_row(FENCE_KEY, lang)}"]
    assert body["message"] == "Validation failed"


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_admin_delete_path_returns_400_with_the_admins_language(
    app, client, place_seeds, grouped_place, lang
):
    """A 500 would lose the translated message entirely.

    The ``except ValidationError`` arm must sit AHEAD of ``except Exception``.
    """
    admin = grouped_place["admin"]
    admin.preferred_language = lang
    from business_app import db as _db

    _db.session.commit()

    response = client.delete(
        f"/api/v1/admin/users/{grouped_place['alice'].id}/addresses/{grouped_place['addr_a'].id}",
        headers=_admin_headers(app, admin),
    )

    body = response.get_json()
    assert response.status_code == 400, body
    assert body["data"]["error_code"] == "PLACE_GROUP_ADDRESS_NOT_DELETABLE"
    assert body["errors"] == [_row(FENCE_KEY, lang)]


@pytest.mark.integration
def test_a_null_preferred_language_drops_the_customer_to_uzbek(
    app, client, place_seeds, grouped_place
):
    """The bot never passes its own language to this endpoint.

    So a customer whose ``preferred_language`` is NULL reads the Uzbek sentence
    no matter which language their bot session is in. Pinned because it is a
    silent mismatch between two surfaces the same person is looking at.
    """
    alice = grouped_place["alice"]
    alice.preferred_language = None
    from business_app import db as _db

    _db.session.commit()

    response = client.delete(
        f"/api/v1/auth/addresses/{grouped_place['addr_a'].id}", headers=_headers(app, alice)
    )

    assert response.get_json()["message"] == _row(FENCE_KEY, "uz")
    assert app.config["DEFAULT_LANGUAGE"] == "uz"


@pytest.mark.integration
def test_accept_language_rescues_a_null_preference_on_the_fence(
    app, client, place_seeds, grouped_place
):
    alice = grouped_place["alice"]
    alice.preferred_language = None
    from business_app import db as _db

    _db.session.commit()

    headers = _headers(app, alice)
    headers["Accept-Language"] = "ru-RU,ru;q=0.9"
    response = client.delete(
        f"/api/v1/auth/addresses/{grouped_place['addr_a'].id}", headers=headers
    )

    assert response.get_json()["message"] == _row(FENCE_KEY, "ru")


@pytest.mark.integration
def test_the_url_lang_parameter_outranks_the_stored_preference_on_the_fence(
    app, client, place_seeds, grouped_place
):
    alice = grouped_place["alice"]
    alice.preferred_language = "uz"
    from business_app import db as _db

    _db.session.commit()

    response = client.delete(
        f"/api/v1/auth/addresses/{grouped_place['addr_a'].id}?lang=en",
        headers=_headers(app, alice),
    )

    assert response.get_json()["message"] == _row(FENCE_KEY, "en")


@pytest.mark.integration
def test_an_unseeded_fence_falls_back_to_the_hardcoded_english_never_the_raw_key(
    app, client, db, grouped_place
):
    """The guard compares against the key LITERAL.

    If the key string in that comparison ever drifts from the key passed to
    ``get_translation`` (a copy-paste rename), an unseeded environment sends
    customers the literal ``api.addresses.error.in_place_group``.
    """
    assert Translation.query.filter_by(key=FENCE_KEY).count() == 0

    response = client.delete(
        f"/api/v1/auth/addresses/{grouped_place['addr_a'].id}",
        headers=_headers(app, grouped_place["alice"]),
    )

    message = response.get_json()["message"]
    assert message != FENCE_KEY
    assert message == (
        "Cannot delete an address that belongs to a place group — "
        "remove it from the place first"
    )


@pytest.mark.integration
def test_the_service_level_fence_uses_the_request_language_when_there_is_no_request(
    place_seeds, grouped_place
):
    """Called outside a request (a Celery task, a script) there is no ``g``.

    ``get_translation`` then falls to ``DEFAULT_LANGUAGE``. Pinned so the
    fence's message can never become a raw key in a background context.
    """
    with pytest.raises(ValidationError) as exc:
        CustomerLinkService.assert_address_not_in_place_group(grouped_place["addr_a"].id)

    assert exc.value.error_code == "PLACE_GROUP_ADDRESS_NOT_DELETABLE"
    assert exc.value.message == _row(FENCE_KEY, "uz")


# ===========================================================================
# E. The Redis translation cache — the deploy hazard
# ===========================================================================
@pytest.mark.integration
def test_reseeding_a_changed_place_key_drops_the_stale_cache_entry(
    app, place_seeds, monkeypatch
):
    """``bulk_create_or_update`` invalidates the cache entry it just made stale.

    WAS THE DEPLOY HAZARD: every place seed writes through this method, the
    ``api.*`` TTL is 3600s (``ui.*`` is 86400s), and the documented deploy step
    ``docker compose restart business_app`` does not clear Redis — so a key
    whose copy CHANGED kept serving the pre-seed value for up to a day. New keys
    were safe only because misses are not cached.

    Asserted on the CACHE as well as the render: an implementation that merely
    stopped caching would also make the render assertion pass while quietly
    costing a query per lookup.
    """
    fake = _DictRedis()
    monkeypatch.setattr(translations_util.translation_service, "redis_client", fake)

    original = translations_util.get_translation(FENCE_KEY, "ru")
    assert fake.store[f"translations:ru:{FENCE_KEY}"] == original

    Translation.bulk_create_or_update({"ru": {FENCE_KEY: "НОВЫЙ ТЕКСТ"}}, category="api")

    assert f"translations:ru:{FENCE_KEY}" not in fake.store, (
        "the seed must drop the entry it just made stale"
    )
    assert translations_util.get_translation(FENCE_KEY, "ru") == "НОВЫЙ ТЕКСТ"
    assert fake.store[f"translations:ru:{FENCE_KEY}"] == "НОВЫЙ ТЕКСТ", (
        "and the fresh value must be cached again"
    )


@pytest.mark.integration
def test_set_translation_does_clear_the_cache_so_the_gap_is_specific_to_the_bulk_path(
    app, place_seeds, monkeypatch
):
    """Control: the asymmetry is in ``bulk_create_or_update``, not in Redis."""
    fake = _DictRedis()
    monkeypatch.setattr(translations_util.translation_service, "redis_client", fake)
    translations_util.get_translation(FENCE_KEY, "ru")

    translations_util.set_translation(FENCE_KEY, "ru", "ЧЕРЕЗ SET", category="api")

    assert translations_util.get_translation(FENCE_KEY, "ru") == "ЧЕРЕЗ SET"


@pytest.mark.integration
def test_bulk_create_or_update_should_invalidate_the_translation_cache(
    app, place_seeds, monkeypatch
):
    """The bulk path now performs the same invalidation ``set_translation`` does.

    Also asserted for a language the write did NOT name: ``set_translation``
    clears all three, and a bulk write that cleared only the written language
    would leave the sibling entries to be served after a key's copy changed.
    """
    fake = _DictRedis()
    monkeypatch.setattr(translations_util.translation_service, "redis_client", fake)
    translations_util.get_translation(FENCE_KEY, "ru")
    translations_util.get_translation(FENCE_KEY, "uz")

    Translation.bulk_create_or_update({"ru": {FENCE_KEY: "НОВЫЙ ТЕКСТ"}}, category="api")

    assert translations_util.get_translation(FENCE_KEY, "ru") == "НОВЫЙ ТЕКСТ"
    assert f"translations:uz:{FENCE_KEY}" not in fake.store


# ===========================================================================
# F. Bot catalogs from the DB — the REAL i18n.get()
# ===========================================================================
@pytest.mark.integration
@pytest.mark.parametrize("key", sorted(TELEGRAM_PLACE_CALLS))
@pytest.mark.parametrize("lang", LANGUAGES)
def test_customer_bot_place_template_formats_with_its_real_call_site_kwargs(
    tg_i18n, key, lang
):
    """``telegram_bot/i18n.py:88-93`` SWALLOWS a format error and returns the
    RAW TEMPLATE.

    So a single-language typo (``{totals}`` in the Russian row) ships a literal
    ``{totals}`` to Russian customers — invisible to any test that renders one
    language or only compares placeholder-name sets.
    """
    kwargs = TELEGRAM_PLACE_CALLS[key]
    rendered = tg_i18n.get(key, lang, **kwargs)

    assert "{" not in rendered and "}" not in rendered, f"{key}:{lang} -> {rendered!r}"
    assert rendered != _humanised(key), f"{key}:{lang} fell into the humanise branch"
    for value in kwargs.values():
        assert str(value) in rendered, f"{key}:{lang} dropped {value!r}: {rendered!r}"


@pytest.mark.integration
@pytest.mark.parametrize("key", sorted(STAFF_PLACE_CALLS))
@pytest.mark.parametrize("lang", LANGUAGES)
def test_staff_bot_place_template_formats_with_its_real_call_site_kwargs(
    staff_i18n, key, lang
):
    kwargs = STAFF_PLACE_CALLS[key]
    rendered = staff_i18n.get(key, lang, **kwargs)

    assert "{" not in rendered and "}" not in rendered, f"{key}:{lang} -> {rendered!r}"
    assert rendered != _humanised(key), f"{key}:{lang} fell into the humanise branch"
    for value in kwargs.values():
        assert str(value) in rendered, f"{key}:{lang} dropped {value!r}: {rendered!r}"


@pytest.mark.integration
@pytest.mark.parametrize("key", sorted(STAFF_LABEL_ONLY_KEYS))
def test_a_label_only_staff_key_carries_no_placeholder_in_any_language(place_seeds, key):
    """These six are APPENDED to by the caller and never passed kwargs.

    ``.format()`` is therefore never invoked, which means a stray ``{x}`` is NOT
    caught by the format-error path — it reaches the driver's screen literally.
    """
    for lang in LANGUAGES:
        value = _row(key, lang)
        assert not _placeholders(value), f"{key}:{lang} interpolates but is never formatted"
        assert "{" not in value and "}" not in value, f"{key}:{lang} -> {value!r}"


@pytest.mark.integration
@pytest.mark.parametrize(
    "key", sorted(set(TELEGRAM_PLACE_CALLS) | set(STAFF_PLACE_CALLS))
)
def test_placeholder_sets_are_identical_across_all_three_languages(place_seeds, key):
    """Translators edit rows through the admin translations page, not the script.

    A Russian row that drops ``{total}`` renders the label with no number and a
    warning in a log nobody reads. Read from the DB, so an edit that never went
    back into the script is still caught.
    """
    expected = set((TELEGRAM_PLACE_CALLS | STAFF_PLACE_CALLS)[key])
    sets = {lang: _placeholders(_row(key, lang)) for lang in LANGUAGES}
    assert len(set(map(frozenset, sets.values()))) == 1, f"{key} placeholder drift: {sets}"
    assert sets["en"] == expected, f"{key}: seeded {sets['en']} vs call site {expected}"


@pytest.mark.integration
def test_the_admin_ui_place_copy_interpolates_nowhere_except_the_member_count(place_seeds):
    """Only ``linked_member_count_label`` uses ``{{count}}``; a ``{{x}}``
    anywhere else in the admin place vocabulary is a typo that renders raw."""
    for key in sorted(_ui_place_keys() | {"ui.users.map.shared_place"}):
        for lang in LANGUAGES:
            assert not _i18next_placeholders(_row(key, lang)), f"{key}:{lang}"
    for lang in LANGUAGES:
        assert _i18next_placeholders(_row("linked_member_count_label", lang)) == {"count"}, lang


@pytest.mark.integration
@pytest.mark.parametrize("key", sorted(TELEGRAM_PLACE_CALLS))
def test_a_missing_row_makes_the_customer_bot_render_the_fallback_not_the_key(place_seeds, key):
    """Both bots fall back BEFORE the humanise branch.

    HYPOTHETICAL FALLBACK. ``fallback='uz'`` is not what the bot ships (see
    ``test_the_shipped_customer_bot_fallback_...`` below, which pins the real
    one); this case exists because the fallback is the ONE knob that decides
    whether a partially-seeded key degrades silently or visibly, and the silent
    direction is the one no monitoring catches. The production-true instance of
    the same mechanism — an Uzbek customer served ENGLISH because the uz row is
    missing — is asserted immediately after.
    """
    kwargs = TELEGRAM_PLACE_CALLS[key]
    uz_expected = _row(key, "uz").format(**kwargs)
    en_expected = _row(key, "en").format(**kwargs)
    instance = _tg_i18n(fallback="uz", drop=((key, "en"),))

    rendered = instance.get(key, "en", **kwargs)

    assert rendered == uz_expected
    assert rendered != _humanised(key)
    if uz_expected != en_expected:
        assert rendered != en_expected

    # SHIPPED fallback ('en', hardcoded at telegram_bot/config.py:179 and never
    # read from the environment): an Uzbek customer whose uz row went missing
    # silently reads ENGLISH, and an English customer whose en row went missing
    # gets the humanised key instead — the two halves of the same knob.
    shipped = tg_i18n_module.config.localization.fallback_language
    assert shipped == "en", shipped
    uz_customer = _tg_i18n(fallback=shipped, drop=((key, "uz"),)).get(key, "uz", **kwargs)
    assert uz_customer == en_expected
    if uz_expected != en_expected:
        assert uz_customer != uz_expected
    en_customer = _tg_i18n(fallback=shipped, drop=((key, "en"),)).get(key, "en", **kwargs)
    assert en_customer == _humanised(key), en_customer


@pytest.mark.integration
def test_a_key_missing_in_every_language_humanises_and_drops_every_kwarg(place_seeds):
    """The failure mode the additive seed scripts exist to prevent."""
    instance = _tg_i18n(drop=(("telegram.bottles.place_total", None),))

    rendered = instance.get("telegram.bottles.place_total", "ru", total="7")

    assert rendered == "Place total"
    assert "7" not in rendered


@pytest.mark.integration
def test_the_staff_bot_normalises_locale_variants_on_every_place_key(staff_i18n):
    """``staff_bot/i18n.py:29-50`` maps ``en-GB``/``en_US``/``English`` -> en and
    an unknown code -> the default. All 12 place keys must survive it."""
    for key in ALL_STAFF_PLACE_KEYS:
        kwargs = STAFF_PLACE_CALLS.get(key, {})
        expected_en = _row(key, "en")
        for variant in ("en-GB", "en_US", "EN", "english"):
            assert staff_i18n.get(key, variant, **kwargs) == (
                expected_en.format(**kwargs) if kwargs else expected_en
            ), f"{key} broke on {variant}"
        # An unknown code must land on the bot's DEFAULT language (uz), not on
        # the fallback and not on the humanised branch. Asserting only "not
        # humanised" would pass for any language at all.
        default = staff_i18n_module.config.localization.default_language
        assert default == "uz", default
        unknown = staff_i18n.get(key, "kk", **kwargs)
        expected_default = _row(key, default)
        assert unknown == (
            expected_default.format(**kwargs) if kwargs else expected_default
        ), f"{key} did not fall to the default language for an unknown code"
        assert unknown != _humanised(key), f"{key} humanised for an unknown code"


@pytest.mark.integration
@pytest.mark.parametrize(
    "variant,fallback,resolved",
    [
        ("en-US", "uz", "en"),
        ("en_GB", "uz", "en"),
        ("ru-RU", "uz", "ru"),
        ("uz-UZ", "en", "uz"),
        ("ru_RU", "en", "ru"),
    ],
)
def test_the_customer_bot_resolves_a_locale_variant_to_its_own_language(
    place_seeds, variant, fallback, resolved
):
    """WAS THE DEFECT — MECHANISM. ``telegram_bot/i18n.py`` now normalises.

    A stored locale variant used to miss the requested-language lookup entirely
    and be rescued by the FALLBACK language, whatever that happened to be, with
    no log line to mark it: from ``get()``'s point of view the fallback worked.
    Both fallback configurations are exercised because the resolution must not
    depend on which language happens to be the fallback.
    """
    key = "telegram.bottles.place_total"
    instance = _tg_i18n(fallback=fallback)

    rendered = instance.get(key, variant, total="7")

    assert rendered == _row(key, resolved).format(total="7")
    if _row(key, resolved) != _row(key, fallback):
        assert rendered != _row(key, fallback).format(total="7"), "served the fallback"
    assert "7" in rendered


@pytest.mark.integration
def test_the_shipped_customer_bot_config_serves_each_locale_variant_its_own_language(
    place_seeds,
):
    """THE PRODUCTION IMPACT, under the config that actually ships.

    ``telegram_bot/config.py`` now reads ``FALLBACK_LANGUAGE`` from the
    environment (default ``en``), and the constructor already read
    ``DEFAULT_LANGUAGE``. Before the fix an Uzbek or Russian speaker whose
    stored code carried a region subtag read the whole place surface in
    ENGLISH; ``en-US`` landed on English only because the fallback happened to
    be English, which is exactly why the defect survived — so that one case is
    asserted here too, alongside the ones that used to be wrong.
    """
    shipped = tg_i18n_module.config.localization.fallback_language
    assert shipped == "en", shipped
    assert tg_i18n_module.config.localization.default_language == "uz"

    key = "telegram.bottles.place_total"
    instance = _tg_i18n(fallback=shipped)
    english = _row(key, "en").format(total="7")

    for variant, requested in (("ru-RU", "ru"), ("ru_RU", "ru"), ("uz-UZ", "uz"), ("UZ", "uz")):
        rendered = instance.get(key, variant, total="7")
        assert rendered == _row(key, requested).format(total="7"), variant
        assert rendered != english, f"{variant} still degrades to English: {rendered!r}"
    # The coincidentally-correct case must stay correct.
    assert instance.get(key, "en-US", total="7") == english
    # An unresolvable code still lands on the bot's DEFAULT language, not on the
    # humanised branch and not on the fallback.
    assert instance.get(key, "kk", total="7") == _row(key, "uz").format(total="7")
    # The plain codes still work.
    for plain in LANGUAGES:
        assert instance.get(key, plain, total="7") == _row(key, plain).format(total="7")


@pytest.mark.integration
def test_a_locale_variant_whose_language_and_fallback_are_both_missing_drops_every_number(
    place_seeds,
):
    """Worst case, and the reason this matters beyond a wrong language: with the
    resolved row AND the fallback row gone, the same input reaches the humanise
    branch and the customer reads "Place total" with no figure at all."""
    key = "telegram.bottles.place_total"
    instance = _tg_i18n(fallback="uz", drop=((key, "en"), (key, "uz")))

    rendered = instance.get(key, "en-US", total="7")

    assert rendered == "Place total"
    assert "7" not in rendered


@pytest.mark.integration
@pytest.mark.parametrize(
    "variant,fallback",
    [
        # The shipped fallback is 'en'; the 'uz' rows are the hypothetical
        # configuration and are kept because the fix must work under both.
        ("ru-RU", "en"),
        ("uz-UZ", "en"),
        ("ru_RU", "en"),
        ("en-US", "uz"),
        ("en_GB", "uz"),
    ],
)
def test_the_customer_bot_should_normalise_a_locale_variant_language(
    place_seeds, variant, fallback
):
    key = "telegram.bottles.place_total"
    expected = _row(key, variant[:2]).format(total="7")

    assert _tg_i18n(fallback=fallback).get(key, variant, total="7") == expected


@pytest.mark.integration
def test_a_locale_variant_preference_is_writable_through_a_real_http_route(
    app, client, place_seeds, grouped_place
):
    """Reachability half of the locale-variant defect, proven end to end.

    ``POST /api/v1/auth/sync-profile`` stores ``preferred_language`` with no
    validation, so a customer's own JWT is enough to put a region-subtagged code
    in the column both surfaces read — the reachability is UNCHANGED by the fix
    and is pinned here so it stays visible.

    The bot now resolves that stored value to Russian. The BACKEND still does
    not: ``LANGUAGES`` has no ``ru-RU`` entry and the request hook drops the
    customer to Uzbek, so the same person still reads two different languages on
    two screens. That remaining half is a separate defect in the backend's
    language resolution, deliberately left pinned rather than silently changed
    here.
    """
    alice = grouped_place["alice"]
    key = "telegram.bottles.place_total"

    response = client.post(
        "/api/v1/auth/sync-profile",
        headers=_headers(app, alice),
        json={"preferred_language": "ru-RU"},
    )
    assert response.status_code == 200, response.get_json()

    from business_app import db as _db

    _db.session.expire_all()
    assert User.query.get(alice.id).preferred_language == "ru-RU"
    assert "ru-RU" not in app.config["LANGUAGES"]

    fence = client.delete(
        f"/api/v1/auth/addresses/{grouped_place['addr_a'].id}", headers=_headers(app, alice)
    )
    assert fence.get_json()["message"] == _row(FENCE_KEY, "uz")
    assert fence.get_json()["message"] != _row(FENCE_KEY, "ru")

    shipped = tg_i18n_module.config.localization.fallback_language
    assert shipped == "en", shipped
    bot_render = _tg_i18n(fallback=shipped).get(key, "ru-RU", total="7")
    assert bot_render == _row(key, "ru").format(total="7")
    assert bot_render != _row(key, "en").format(total="7")
    # The remaining disagreement: the backend says Uzbek, the bot says Russian.
    assert _row(FENCE_KEY, "uz") != _row(FENCE_KEY, "ru")


@pytest.mark.integration
@pytest.mark.parametrize(
    "key",
    sorted(
        set(TELEGRAM_PLACE_CALLS)
        | set(ALL_STAFF_PLACE_KEYS)
        | set(BARE_BOTTLE_TRACKING_PLACE_KEYS)
    ),
)
def test_no_place_value_carries_a_bare_angle_bracket(place_seeds, key):
    """``/bottles``, the fine prompt and the place statement send HTML.

    The handlers escape only the INTERPOLATED fragments, so an unbalanced ``<``
    in a template makes Telegram reject the ENTIRE message and the customer sees
    nothing at all.
    """
    for lang in LANGUAGES:
        value = _row(key, lang)
        assert "<" not in value and ">" not in value, f"{key}:{lang} -> {value!r}"


@pytest.mark.integration
def test_no_place_value_carries_a_bare_ampersand_that_html_mode_would_reject(place_seeds):
    for key in sorted(set(TELEGRAM_PLACE_CALLS) | set(ALL_STAFF_PLACE_KEYS)):
        for lang in LANGUAGES:
            value = _row(key, lang)
            assert "&" not in value, f"{key}:{lang} -> {value!r}"


# ===========================================================================
# G. Real render helpers, driven from the real payload
# ===========================================================================
@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_grouped_place_bottles_screen_renders_its_own_copy_in_each_language(
    app, client, place_seeds, grouped_place, patched_tg_i18n, lang
):
    """Real write paths -> real HTTP route -> the real ``_build_balance_lines``.

    ``member_line`` is byte-identical in all three languages, so a lost row is
    undetectable by comparing renders — this test asserts the CATALOG (the
    ``place_total`` copy for THIS language) as well as the render.
    """
    response = client.get(
        "/api/v1/orders/bottles/my-balances", headers=_headers(app, grouped_place["alice"])
    )
    overview = response.get_json()["data"]
    assert response.status_code == 200

    body = "\n".join(tg_bottles._build_balance_lines(overview, lang))

    assert _row("telegram.bottles.place_total", lang).format(total="7") in body

    grouped_row = next(r for r in overview["balances"] if r.get("is_grouped"))
    members = grouped_row["place_members"]
    assert len(members) == 2, members
    for member in members:
        expected = _row("telegram.bottles.member_line", lang).format(
            name=html.escape(str(member["member_name"]))
        )
        assert expected in body, f"{lang}: member line missing for {member}"

    assert "{" not in body and "}" not in body, body
    assert "telegram.bottles." not in body
    assert _humanised("telegram.bottles.place_total") not in body


@pytest.mark.integration
def test_the_three_grouped_renders_differ_wherever_the_catalog_differs(
    app, client, place_seeds, grouped_place, patched_tg_i18n
):
    """A regression that always served one language would make these identical."""
    overview = client.get(
        "/api/v1/orders/bottles/my-balances", headers=_headers(app, grouped_place["alice"])
    ).get_json()["data"]

    bodies = {
        lang: "\n".join(tg_bottles._build_balance_lines(overview, lang)) for lang in LANGUAGES
    }

    assert len(set(bodies.values())) == 3, "two languages rendered byte-identical screens"
    for lang, body in bodies.items():
        assert _row("telegram.bottles.place_total", lang).format(total="7") in body


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_an_ungrouped_customer_gets_no_place_copy_in_any_language(
    app, client, db, place_seeds, patched_tg_i18n, lang
):
    """A regression that always emitted the place block would show every solo
    customer a "Bottles at this place (all members)" line — in three languages
    simultaneously."""
    solo = _user(db, "i18n-solo@example.com", "+998900007710")
    address = _addr(db, solo, title="Home")
    _put_bottles(db, address, solo, "5")

    overview = client.get(
        "/api/v1/orders/bottles/my-balances", headers=_headers(app, solo)
    ).get_json()["data"]
    body = "\n".join(tg_bottles._build_balance_lines(overview, lang))

    # The discriminator has to be the template's LITERAL text, not its leading
    # stem: ``telegram.bottles.linked_account_line`` is "{address} (account:
    # {owner})" — it BEGINS with a placeholder, so a leading-stem check is the
    # empty string and silently asserts nothing about the one key that names a
    # coworker. ``_literal_fragments`` reads every literal run instead, and the
    # per-key non-empty assertion below makes a future template that loses all
    # of them fail loudly rather than skip.
    for key in (
        "telegram.bottles.place_total",
        "telegram.bottles.cluster_total",
        "telegram.bottles.linked_account_line",
    ):
        fragments = _literal_fragments(_row(key, lang))
        assert fragments, f"{key}:{lang} has no literal text left to detect it by"
        for fragment in fragments:
            assert fragment not in body, (
                f"{key} leaked onto a solo customer's screen ({lang}): {fragment!r}"
            )
    # Positive control: the solo row still prints its own number, so the
    # assertions above cannot be passing on an empty screen.
    assert body.count("\n") >= 1 and "Home" in body
    assert "• Home: <b>5</b>" in body


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
@pytest.mark.parametrize("qty,shown", [("0", "0"), ("-3", "-3"), ("1.5", "1.5")])
def test_zero_negative_and_fractional_places_render_the_number_in_each_language(
    app, client, db, place_seeds, patched_tg_i18n, lang, qty, shown
):
    """The customer surface prints the SIGNED/normalised figure (decision D1);
    ``1.5`` must not round to ``2`` or truncate to ``1`` in any language."""
    admin = _user(db, f"z-adm-{qty}@example.com", f"+99890001{abs(int(float(qty)*10)):04d}")
    a = _user(db, f"z-a-{qty}@example.com", f"+99890002{abs(int(float(qty)*10)):04d}")
    b = _user(db, f"z-b-{qty}@example.com", f"+99890003{abs(int(float(qty)*10)):04d}")
    addr_a, addr_b = _addr(db, a), _addr(db, b)
    CustomerLinkService().create_place_group(
        [addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="office"
    )
    if Decimal(qty) != 0:
        _put_bottles(db, addr_a, a, qty)
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal(qty)

    overview = client.get(
        "/api/v1/orders/bottles/my-balances", headers=_headers(app, a)
    ).get_json()["data"]
    body = "\n".join(tg_bottles._build_balance_lines(overview, lang))

    assert _row("telegram.bottles.place_total", lang).format(total=shown) in body


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_cod_restriction_place_arm_renders_a_count_and_never_an_identity(
    patched_tg_i18n, lang
):
    """Spec §7 privacy boundary: only a COUNT may cross at a shared workplace.

    A translator adding ``{member_name}`` to one language would leak a
    coworker's identity — and because the kwarg is not passed, ``.format()``
    raises and the bot sends the RAW template containing the literal
    ``{member_name}``, which is a different but equally bad failure.
    """
    restrictions = {"restriction_scope": "place", "place_active_cod_debt_count": 3}

    notice = tg_orders.OrderHandlers._cod_restriction_notice(restrictions, lang)

    assert notice == _row("telegram.orders.cod_restricted_place", lang).format(
        place_active_cod_debt_count=3
    )
    assert "3" in notice
    assert _placeholders(_row("telegram.orders.cod_restricted_place", lang)) == {
        "place_active_cod_debt_count"
    }
    for forbidden in ("member_name", "owner_name", "phone", "order_number", "+9989"):
        assert forbidden not in notice


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
@pytest.mark.parametrize("payload", [{}, {"place_active_cod_debt_count": 0}])
def test_the_cod_restriction_place_arm_survives_a_missing_or_zero_count(
    patched_tg_i18n, lang, payload
):
    """The handler's ``or 0`` is the only thing preventing a KeyError ->
    RAW-template on a payload shape the backend legitimately produces for a
    legacy order with no delivery address."""
    notice = tg_orders.OrderHandlers._cod_restriction_notice(
        {"restriction_scope": "place", **payload}, lang
    )

    assert notice == _row("telegram.orders.cod_restricted_place", lang).format(
        place_active_cod_debt_count=0
    )
    assert "{" not in notice
    assert notice != _humanised("telegram.orders.cod_restricted_place")


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_operator_bots_cod_place_arm_matches_the_customers_privacy_boundary(
    patched_staff_i18n, lang
):
    from staff_bot.handlers.operator.create_order import CreateOrderHandler

    restrictions = {"restriction_scope": "place", "place_active_cod_debt_count": 3}
    notice = CreateOrderHandler._cod_restriction_notice(restrictions, lang)

    assert notice == _row("staff.operator.cod_restricted_place", lang).format(
        place_active_cod_debt_count=3
    )
    assert "3" in notice
    for forbidden in ("member_name", "owner_name", "phone", "order_number"):
        assert forbidden not in notice


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_customer_wallet_block_renders_the_cluster_and_place_lines_per_language(
    patched_tg_i18n, place_seeds, lang
):
    """``place_order_line`` is byte-identical in all three languages, so a lost
    row is render-invisible; the assertion is against the catalog too. The block
    is emitted with NO parse_mode, so HTML entities in one language would show."""
    summary = {
        "cluster_member_count": 2,
        "cluster_delivered_outstanding_amount": 35000,
        "available_prepayment_balance": 5000,
        "places": [
            {
                "place_group_id": 9,
                "label": "Acme office",
                "place_open_cod_debt_total": 35000,
                "items": [
                    {
                        "order_number": "ORD-1",
                        "member_name": "Alice",
                        "outstanding_amount": 15000,
                    },
                    {
                        "order_number": "ORD-2",
                        "member_name": "Bob",
                        "outstanding_amount": 20000,
                    },
                ],
            }
        ],
    }

    lines = tg_orders._build_cod_summary_lines(summary, lang)
    body = "\n".join(lines)

    # Assert the WHOLE formatted line, not its leading stem. Both place lines
    # start with an emoji, and matching on "🏢" alone would pass while the
    # Russian row was served to an English customer, while ``{label}`` was
    # dropped from one language, and while the money was formatted wrong —
    # exactly the failures this surface exists to catch. ``_money`` is
    # ``format_price`` = ``f"{v:,.0f}"``, so every figure below is exact.
    assert _row("telegram.payments.cluster_debt_total", lang).format(total="35,000") in lines
    assert (
        _row("telegram.payments.place_debt_total", lang).format(
            label="Acme office", total="35,000"
        )
        in lines
    )
    assert "   " + _row("telegram.payments.place_order_line", lang).format(
        order_number="ORD-1", member_name="Alice", amount="15,000"
    ) in lines
    assert "   " + _row("telegram.payments.place_order_line", lang).format(
        order_number="ORD-2", member_name="Bob", amount="20,000"
    ) in lines
    assert "{" not in body and "}" not in body, body
    assert "&#" not in body and "<" not in body, "the wallet block is sent with no parse_mode"
    assert "telegram.payments." not in body


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
@pytest.mark.parametrize("count", [None, 1])
def test_the_wallet_block_emits_nothing_for_a_solo_ungrouped_customer(
    patched_tg_i18n, lang, count
):
    """The gate is ``int(summary.get('cluster_member_count') or 1) > 1``.

    A payload with ``cluster_member_count: null`` must NOT emit a cluster line —
    otherwise every solo customer sees a linked-accounts total of 0, in three
    languages.
    """
    summary = {"cluster_member_count": count, "places": []}

    assert tg_orders._build_cod_summary_lines(summary, lang) == []


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_reused_prepaid_key_on_the_cluster_surface_is_trilingual_and_formats(
    app, monkeypatch, place_seeds, lang
):
    """``telegram.orders.cod_prepaid_balance`` and the cash-only note B4b made
    travel with it are owned by ``seed_backend_translations.py``, not by any
    place seed, yet they only appear on the wallet surface.

    B4b moved BOTH out of the one-off ``seed_prepayment_translations.py``: the
    balance label because its audience widened from linked customers to everyone
    holding credit, the note because it is new. The owner asserted here moved
    with them — a stale copy in two seeders means the next reseed silently
    reverts the wording.

    Two halves, both asserted: with only the place seeds run the lines are
    MISSING (a deploy that skips the canonical seed leaves the wallet block
    half-translated), and once that seed runs they render trilingually.
    """
    key = "telegram.orders.cod_prepaid_balance"
    note = "telegram.payments.prepaid_cash_only"
    summary = {
        "cluster_member_count": 2,
        "cluster_delivered_outstanding_amount": 0,
        "available_prepayment_balance": 5000,
        "places": [],
    }
    for unseeded in (key, note):
        assert Translation.query.filter_by(key=unseeded).count() == 0, (
            f"no place seed owns {unseeded} — if one now does, drop this test's first half"
        )
    monkeypatch.setattr(tg_orders, "i18n", _tg_i18n())
    half_translated = tg_orders._build_cod_summary_lines(summary, lang)
    assert any(_humanised(key) in line for line in half_translated), half_translated
    assert any(_humanised(note) in line for line in half_translated), half_translated

    _seed_backend_slice(app, key)
    _seed_backend_slice(app, note)
    monkeypatch.setattr(tg_orders, "i18n", _tg_i18n())

    lines = tg_orders._build_cod_summary_lines(summary, lang)

    # The exact rendered line, formatted the way the handler formats it
    # (``_money`` = ``format_price`` = ``f"{v:,.0f}"``). A leading-stem match
    # would pass on a Russian row served to an English customer, and on a row
    # that dropped ``{available_balance}`` altogether.
    assert _row(key, lang).format(available_balance="5,000") in lines, lines
    # The balance NEVER ships without the sentence saying where it can be spent.
    assert _row(note, lang) in lines, lines
    assert not any(_humanised(key) in line for line in lines), lines
    assert not any(_humanised(note) in line for line in lines), lines
    for line in lines:
        assert "{" not in line and "}" not in line, line


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_driver_order_card_place_line_renders_its_language(patched_staff_i18n, lang):
    payload = {
        "is_place_grouped": True,
        "place_outstanding_cod_total": 35000,
        "place_active_cod_debt_count": 3,
        "place_group_label": "Acme office",
    }

    lines = staff_formatters.format_place_cod_lines(payload, lang)

    assert len(lines) == 1
    assert _row("staff.delivery.place_cod_total", lang) in lines[0]
    assert "(3)" in lines[0]
    assert "Acme office" in lines[0]
    assert "{" not in lines[0]


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_an_ungrouped_delivery_payload_emits_no_place_line_in_any_language(
    patched_staff_i18n, lang
):
    assert staff_formatters.format_place_cod_lines({"place_outstanding_cod_total": 1}, lang) == []


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
@pytest.mark.parametrize("balance,magnitude", [(-3, "3"), (-0.5, "0.5"), (-21, "21")])
def test_the_over_returned_line_always_states_the_magnitude_never_a_minus_sign(
    patched_staff_i18n, lang, balance, magnitude
):
    """The copy supplies the DIRECTION in words, so a branch that passed the
    signed value would render "Over-returned by -3" in three languages."""
    line = BottleCollectionHandler._over_returned_line(lang, balance)

    assert line == _row("staff.delivery.place_over_returned", lang).format(count=magnitude)
    assert magnitude in line
    # A hyphen in "Over-returned" is fine; a MINUS SIGN in front of the figure
    # is the failure this helper exists to prevent.
    assert not re.search(r"[-−]\s*\d", line), line
    assert "{" not in line


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_at_door_return_prompt_distinguishes_over_returned_from_no_record(
    staff_i18n, lang
):
    """The zero-balance copy says "no empties are on record", which is factually
    WRONG for a negative place: there IS a record and it is negative. The two
    strings must differ in every language."""
    over = staff_i18n.get("staff.delivery.bottles_return_prompt_over_returned", lang, count="2")
    none = staff_i18n.get("staff.delivery.bottles_return_prompt_no_balance", lang)

    assert over != none, f"{lang}: the -2 place and the 0 place read the same"
    assert "2" in over
    assert "{" not in over and "{" not in none


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_collection_receipt_names_an_over_returned_remainder_per_language(
    staff_i18n, lang
):
    """``remaining`` is ``abs()`` on one arm and signed on the other; a
    copy/paste of the wrong arm yields "over-returned by -3"."""
    over = staff_i18n.get(
        "staff.delivery.bottle_collection_recorded_over_returned", lang, quantity=4, remaining="3"
    )
    positive = staff_i18n.get(
        "staff.delivery.bottle_collection_recorded", lang, quantity=4, remaining="1"
    )

    assert over != positive, f"{lang}: the two arms share copy"
    assert "4" in over and "3" in over
    assert "-3" not in over
    assert "{" not in over and "{" not in positive


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_fine_prompt_hint_switches_between_positive_zero_and_over_returned(
    staff_i18n, lang
):
    """Both templates deliberately reuse the kwarg name ``{union}`` so both
    branches call ``i18n.get`` identically. Renaming one for readability makes
    ``.format()`` raise, which the bot swallows into the RAW template — the
    driver then reads a literal ``{union}``."""
    positive = staff_i18n.get("staff.delivery.fine_place_union_hint", lang, union="7")
    negative = staff_i18n.get("staff.delivery.fine_place_over_returned_hint", lang, union="3")

    assert "7" in positive and "3" in negative
    assert positive != negative
    assert "{" not in positive and "{" not in negative
    assert _placeholders(_row("staff.delivery.fine_place_union_hint", lang)) == {"union"}
    assert _placeholders(_row("staff.delivery.fine_place_over_returned_hint", lang)) == {"union"}


@pytest.mark.integration
@pytest.mark.parametrize("lang", ("uz", "ru"))
def test_the_place_statement_reuses_only_fully_localised_keys(place_seeds, lang):
    """``_curated_value`` falls back to a curated entry's ``en`` value when the
    requested language is absent, which writes ENGLISH into the uz/ru rows.

    A row-count or non-empty check passes; only comparing the localised value to
    the English one catches an English fragment inside an otherwise-Uzbek place
    statement.
    """
    _run_seed_staff_generator()
    english_fragments = {
        key: _row(key, lang)
        for key in STAFF_REUSED_ON_PLACE_SURFACES
        if _row(key, lang) == _row(key, "en")
    }
    assert not english_fragments, (
        f"{lang} rows that are verbatim English on a place surface: {english_fragments}"
    )


def _run_seed_staff_generator():
    """Run ``seed_staff_translations`` the way the runbook does, in this context."""
    mod = _load_seed("seed_staff_translations")
    mod.seed_translations(mod.collect_keys(REPO_ROOT))


# ===========================================================================
# H. Ledger event labels — the five NEW ADMIN_ADJUSTMENT writers
# ===========================================================================
@pytest.mark.integration
@pytest.mark.parametrize("event", [e.value for e in BottleLedgerEventType])
def test_every_ledger_event_type_has_a_trilingual_customer_bot_label(place_seeds, event):
    """Plan C added FIVE new writers that all use ``ADMIN_ADJUSTMENT``.

    If a future writer ever needs a new enum value, ``telegram.bottles.event.
    <new>`` will not exist and the bot humanises it to English inside otherwise
    Uzbek history — silently, with no health check.
    """
    key = f"telegram.bottles.event.{event}"
    english = _row(key, "en")
    for lang in LANGUAGES:
        value = _row(key, lang)
        assert value.strip()
        if lang != "en":
            # A uz/ru row equal to the English one is what ``_humanize_key``
            # style guesswork produces; a curated row never is.
            assert value != english, f"{key}:{lang} is verbatim English"
            assert value != _humanised(key), f"{key}:{lang} is the humanised fallback"


@pytest.mark.integration
@pytest.mark.parametrize("event", [e.value for e in BottleLedgerEventType])
def test_every_ledger_event_type_has_a_trilingual_admin_drawer_label(place_seeds_full, event):
    """``eventTypeLabel`` builds ``t(`event_${val}`)`` with a template literal.

    ``_DYNAMIC_BARE_PREFIXES`` exempts ``event_`` from the orphan check, so a
    missing ``event_*`` row is invisible to both the seed guard and the vitest
    suite (which stubs ``t``). The DB is the only place it shows.
    """
    for lang in LANGUAGES:
        assert _row(f"event_{event}", lang).strip()


@pytest.mark.integration
def test_the_place_lifecycles_admin_adjustment_rows_render_a_translated_label(
    app, client, db, place_seeds, patched_tg_i18n, grouped_place
):
    """A real split writes ADMIN_ADJUSTMENT rows; the customer's history must
    show translated labels, never the raw ``admin_adjustment`` string."""
    from business_app.models.bottle import BottleLedger

    CustomerLinkService().remove_address_from_group(
        grouped_place["addr_a"].id,
        acting_admin_id=grouped_place["admin"].id,
        reason="moved out",
        bottles_leaving=2,
    )
    db.session.commit()
    assert BottleLedger.query.filter_by(event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT).count()
    # A split is the classic globally-conserving, locally-wrong operation: the
    # 7 bottles stay 7 whichever scope each half lands on, so a total-only
    # oracle is blind to it. Assert BOTH sides.
    db.session.refresh(grouped_place["addr_a"])
    db.session.refresh(grouped_place["addr_b"])
    addr_a, addr_b = grouped_place["addr_a"], grouped_place["addr_b"]
    # Removing one of two members dissolves the place onto the survivor
    # (spec 7.3), so BOTH addresses end up on their own scope.
    assert (addr_a.address_group_id, addr_b.address_group_id) == (None, None)
    stored_a = BottleTrackingService.get_place_balance(addr_a.id)
    stored_b = BottleTrackingService.get_place_balance(addr_b.id)
    own_a = sum(
        (e.quantity for e in BottleLedger.query.filter_by(address_id=addr_a.id).filter(
            BottleLedger.address_group_id.is_(None)
        )),
        Decimal("0.00"),
    )
    own_b = sum(
        (e.quantity for e in BottleLedger.query.filter_by(address_id=addr_b.id).filter(
            BottleLedger.address_group_id.is_(None)
        )),
        Decimal("0.00"),
    )
    global_sum = sum((e.quantity for e in BottleLedger.query.all()), Decimal("0.00"))
    # Both sides of the split, not just the total: 2 leave with the departing
    # address and 5 stay, and each address's STORED figure matches the ledger
    # attributed to its own scope. A defect that conserved the 7 globally while
    # attributing it to the wrong scope passes a total-only check and fails here.
    assert (stored_a, stored_b) == (Decimal("2.00"), Decimal("5.00"))
    assert (own_a, own_b) == (stored_a, stored_b)
    assert global_sum == Decimal("7.00")
    items = [
        {
            "event_type": entry.event_type.value,
            "quantity": float(entry.quantity),
            "occurred_at": entry.created_at.isoformat(),
            "order_id": entry.order_id,
            "balance_after": float(entry.balance_after),
        }
        for entry in BottleLedger.query.order_by(BottleLedger.id.desc()).all()
    ]

    for lang in LANGUAGES:
        body = "\n".join(tg_bottles._render_ledger_lines(copy.deepcopy(items), lang))
        assert "admin_adjustment" not in body, f"{lang} rendered the raw enum value"
        assert _row("telegram.bottles.event.admin_adjustment", lang) in body
        assert "{" not in body and "}" not in body


# ===========================================================================
# I. Seeded English must still match the JSX inline fallbacks — from the DB
# ===========================================================================
_JS_STRING = r"""(?:'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")"""
_JS_PAIR = re.compile(
    r"""(['"])(ui\.[A-Za-z0-9_.]+)\1\s*,\s*({s}(?:\s*\+\s*{s})*)""".format(s=_JS_STRING),
    re.S,
)
_BARE_T_CALL = re.compile(r"""t\(\s*'([a-z0-9_]+)'\s*,\s*\{(.*?)\}\s*\)""", re.S)
_DEFAULT_VALUE = re.compile(r"""defaultValue:\s*'((?:[^'\\]|\\.)*)'""")
_STAFF_NS_CALL = re.compile(
    r"""t\(\s*'staff:([a-z0-9_]+)'\s*,\s*({s}(?:\s*\+\s*{s})*)""".format(s=_JS_STRING), re.S
)


def _unquote_js(literal):
    return re.sub(
        r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), literal[1:-1]
    )


def _join_js(expr):
    return "".join(_unquote_js(lit) for lit in re.findall(_JS_STRING, expr, re.S))


def _dotted_ui_fallbacks():
    found = {}
    for rel in (
        "src/components/PlaceGroupPanel.jsx",
        # The three A1.3 modules: the confirm flow and the fence-code/audit copy
        # were EXTRACTED out of the panel, and the estate-wide tab is new. Their
        # call sites are the same call sites — they just live next door now.
        "src/components/placeGroupCopy.js",
        "src/components/PlaceGroupConfirmModal.jsx",
        "src/components/GroupedAddressesPanel.jsx",
        "src/utils/cashScopeDisplay.js",
        "src/pages/Orders.js",
        "src/pages/Users.js",
        "src/pages/Prepayments.js",
        "src/components/CustomerMap.js",
    ):
        text = (REPO_ROOT / "admin_ui" / rel).read_text(encoding="utf-8")
        for _, key, expr in _JS_PAIR.findall(text):
            found.setdefault(key, _join_js(expr))
    return found


@pytest.mark.integration
def test_seeded_english_still_matches_the_dotted_jsx_fallbacks(place_seeds):
    """The existing guard compares the SCRIPT to the JSX.

    If a script is edited and not re-run — or run against one environment only —
    the DATABASE and the JSX diverge and English-speaking admins read different
    copy on prod than the code review approved. Vitest cannot see it because it
    asserts the ``defaultValue``.
    """
    fallbacks = _dotted_ui_fallbacks()
    tracked = sorted(_ui_place_keys() | {"ui.users.map.shared_place"})
    # Coverage first. Without this the loop below is one broken regex away from
    # being VACUOUS: `_JS_PAIR` failing to match (a Prettier reformat, a switch
    # to backtick literals) yields an empty `fallbacks`, every key is skipped,
    # and the test goes green while asserting nothing. All 71 place keys carry
    # an inline English fallback today, so this is an exact statement, not a
    # floor.
    unread = sorted(set(tracked) - set(fallbacks))
    assert not unread, (
        f"{len(unread)} place keys have no inline English fallback the comparison "
        f"below can see — either the key lost its reader or the JSX scan broke: {unread}"
    )
    drift = {}
    for key in tracked:
        seeded = _row(key, "en")
        if seeded != fallbacks[key]:
            drift[key] = (seeded, fallbacks[key])
    assert not drift, f"seeded EN differs from the JSX fallback: {drift}"


@pytest.mark.integration
def test_seeded_english_still_matches_the_bare_bottle_tracking_default_values(place_seeds):
    page = (REPO_ROOT / "admin_ui" / "src" / "pages" / "BottleTracking.js").read_text(
        encoding="utf-8"
    )
    readers = {}
    for key, opts in _BARE_T_CALL.findall(page):
        match = _DEFAULT_VALUE.search(opts)
        if match:
            readers.setdefault(key, match.group(1).replace("\\'", "'"))

    drift = {}
    for key in BARE_BOTTLE_TRACKING_PLACE_KEYS:
        if key in readers and _row(key, "en") != readers[key]:
            drift[key] = (_row(key, "en"), readers[key])
    assert not drift, f"seeded EN differs from the defaultValue: {drift}"
    assert set(BARE_BOTTLE_TRACKING_PLACE_KEYS) <= set(readers), (
        f"place keys with no reader in BottleTracking.js: "
        f"{sorted(set(BARE_BOTTLE_TRACKING_PLACE_KEYS) - set(readers))}"
    )


@pytest.mark.integration
def test_seeded_english_still_matches_the_delivery_report_column_fallbacks(place_seeds):
    page = (REPO_ROOT / "admin_ui" / "src" / "pages" / "DeliveryReports.js").read_text(
        encoding="utf-8"
    )
    found = {key: _join_js(expr) for key, expr in _STAFF_NS_CALL.findall(page)}
    for key in UI_STAFF_PLACE_KEYS:
        assert key in found, f"DeliveryReports.js no longer reads staff:{key}"
        assert _row(key, "en") == found[key], key


# ===========================================================================
# J. Admin fence codes: every reachable code has translated copy
# ===========================================================================
@pytest.mark.integration
@pytest.mark.parametrize("code,key", sorted(MAPPED_FENCE_CODES.items()))
def test_every_mapped_place_fence_code_has_trilingual_admin_copy(place_seeds, code, key):
    """The envelope ``message`` is always the generic "Validation failed" and
    ``extractApiErrorMessage`` prefers ``errors[0]`` — the untranslated English
    service string. Only the code->key map rescues it."""
    # Two independent "is this token somewhere in the file" checks would pass
    # while the code was mapped to a DIFFERENT key — the exact regression that
    # shows a uz/ru admin the wrong sentence. Parse the Map entries instead so
    # the PAIRING is what is asserted.
    mapping = _place_group_error_map()
    assert code in mapping, f"{code} is no longer in PLACE_GROUP_ERROR_MESSAGES"
    assert mapping[code] == key, f"{code} maps to {mapping[code]!r}, not {key!r}"
    for lang in LANGUAGES:
        assert _row(key, lang).strip()


@pytest.mark.integration
def test_the_panel_maps_exactly_the_place_fence_codes_this_file_tracks(place_seeds):
    """A code added panel-side without a row here would never be language-tested."""
    assert _place_group_error_map() == MAPPED_FENCE_CODES


@pytest.mark.integration
def test_the_split_rejection_message_is_translated_but_loses_the_actual_cap(
    app, client, place_seeds, grouped_place
):
    """§7.1 REJECTS rather than clamps, so this message is the ONLY signal the
    admin's number was discarded.

    It is also the one place where the backend's English message embeds a number
    the translated copy does not — an admin who reads only the translated line
    loses the actual cap. Pinned in both directions.
    """
    response = client.delete(
        f"/api/v1/admin/place-groups/"
        f"{grouped_place['addr_a'].address_group_id}/addresses/{grouped_place['addr_a'].id}",
        headers=_headers(app, grouped_place["admin"]),
        json={"reason": "moved out", "bottlesLeaving": 9},
    )

    body = response.get_json()
    assert response.status_code == 400, body
    assert body["data"]["error_code"] == "PLACE_SPLIT_INVALID"
    assert "7.00" in body["errors"][0], "the backend's English message carries the cap"
    for lang in LANGUAGES:
        translated = _row("ui.users.place_groups.error_place_split_invalid", lang)
        assert "7" not in translated, (
            "the translated copy states the RULE without the actual cap — "
            "an admin reading only it never learns the place total"
        )


@pytest.mark.integration
@pytest.mark.parametrize("value", [-1, 9, "abc", float("nan"), float("inf")])
def test_every_invalid_split_input_class_produces_the_same_translated_code(
    app, client, place_seeds, grouped_place, value
):
    """Three distinct raise sites in ``_validated_bottles_leaving`` share one
    code; if one ever gained a different code the UI would fall back to raw
    English for that input class only — the rarest one, hence the last noticed.

    ``nan``/``inf`` reach the service because Python's JSON parser accepts both
    literals, and they are caught by the ``is_finite()`` guard BEFORE any
    ordering comparison — ``Decimal('NaN') < 0`` would raise
    ``decimal.InvalidOperation``, not return False, so the guard's position is
    load-bearing and the two rows below pin it.
    """
    import json as _json

    response = client.delete(
        f"/api/v1/admin/place-groups/"
        f"{grouped_place['addr_a'].address_group_id}/addresses/{grouped_place['addr_a'].id}",
        headers=_headers(app, grouped_place["admin"]),
        data=_json.dumps({"reason": "moved out", "bottlesLeaving": value}),
        content_type="application/json",
    )

    body = response.get_json()
    assert response.status_code == 400, (value, body)
    assert body["data"]["error_code"] == "PLACE_SPLIT_INVALID", (value, body)
    for lang in LANGUAGES:
        assert _row("ui.users.place_groups.error_place_split_invalid", lang).strip()


@pytest.mark.integration
def test_place_group_reason_required_is_masked_by_the_route_and_never_reaches_the_admin(
    app, client, place_seeds, grouped_place
):
    """``PLACE_GROUP_REASON_REQUIRED`` has NO translated admin copy.

    It is currently unreachable over HTTP because every route strips and rejects
    a blank reason FIRST — with a generic message and no ``error_code``. Pinned
    so that removing the route-level guard (an obvious "the service validates
    it anyway" cleanup) fails here instead of shipping raw English.
    """
    response = client.delete(
        f"/api/v1/admin/place-groups/"
        f"{grouped_place['addr_a'].address_group_id}/addresses/{grouped_place['addr_a'].id}",
        headers=_headers(app, grouped_place["admin"]),
        json={"reason": "   "},
    )

    body = response.get_json()
    assert response.status_code == 400
    assert body.get("data") is None, "the route's own guard emits no error_code"
    assert "PLACE_GROUP_REASON_REQUIRED" not in str(body)

    with pytest.raises(ValidationError) as exc:
        CustomerLinkService().remove_address_from_group(
            grouped_place["addr_a"].id, acting_admin_id=grouped_place["admin"].id, reason="  "
        )
    assert exc.value.error_code == "PLACE_GROUP_REASON_REQUIRED"
    # The code is now MAPPED and seeded, so removing the route guard degrades to
    # translated copy rather than to the raw English service sentence.
    assert _place_group_error_map()["PLACE_GROUP_REASON_REQUIRED"] == (
        "ui.users.place_groups.error_reason_required"
    )
    for lang in LANGUAGES:
        assert _row("ui.users.place_groups.error_reason_required", lang).strip()


@pytest.mark.integration
def test_place_group_min_addresses_reaches_the_admin_as_translated_copy(
    app, client, place_seeds, db, admin_user
):
    """Reachable in one call — and now with translated copy behind the code.

    The route guard is ``len(address_ids) < 2`` while the service guard is
    ``len(set(address_ids)) < 2``, so a DUPLICATE id passes the route and hits
    the service. The response carries ``PLACE_GROUP_MIN_ADDRESSES``; the raw
    English ``errors[0]`` (which ``extractApiErrorMessage`` prefers) is still
    what a uz/ru admin would have read, so the code->key mapping is the only
    thing rescuing it. Both halves asserted: the reachability AND the mapping.
    """
    victim = _user(db, "min-a@example.com", "+998900007721")
    address = _addr(db, victim)

    response = client.post(
        "/api/v1/admin/place-groups",
        headers=_headers(app, admin_user),
        json={"addressIds": [address.id, address.id], "reason": "same office"},
    )

    body = response.get_json()
    assert response.status_code == 400, body
    assert body["data"]["error_code"] == "PLACE_GROUP_MIN_ADDRESSES"
    assert body["errors"] == ["A place group needs at least two addresses"]

    assert _place_group_error_map()["PLACE_GROUP_MIN_ADDRESSES"] == (
        "ui.users.place_groups.error_min_addresses"
    )
    for lang in LANGUAGES:
        assert _row("ui.users.place_groups.error_min_addresses", lang).strip()


@pytest.mark.integration
def test_place_group_min_addresses_should_have_translated_admin_copy(place_seeds):
    # The fence-code map lives in placeGroupCopy.js since A1.3 extracted it.
    copy = (REPO_ROOT / "admin_ui" / "src" / "components" / "placeGroupCopy.js").read_text(
        encoding="utf-8"
    )
    assert "PLACE_GROUP_MIN_ADDRESSES" in copy
    keys = {
        r.key
        for r in Translation.query.filter(
            Translation.key.like("ui.users.place_groups.error_%")
        ).all()
    }
    assert any("min" in k for k in keys), keys


# ===========================================================================
# K. Surfaces the place lifecycle leaves untranslated (documented + reported)
# ===========================================================================
@pytest.mark.integration
def test_the_place_audit_history_renders_a_translated_event_type(place_seeds):
    """WAS THE DEFECT: ``PlaceGroupPanel.jsx`` rendered ``{event.event_type}``
    with no ``t()`` at all and no seed defined a key for any of these values, so
    a Russian admin reading the place history — their only record of who changed
    a place and why — saw English snake_case.

    The keys are LITERAL in the JSX (a ``PLACE_GROUP_EVENT_LABELS`` map), not
    built with a template literal, so the static seed guards in
    ``tests/unit/test_place_group_translation_seeds.py`` can still see them.

    Two files since A1.3: the audit list still RENDERS in the panel, while the
    label map it renders through was extracted to ``placeGroupCopy.js``.
    """
    panel = (REPO_ROOT / "admin_ui" / "src" / "components" / "PlaceGroupPanel.jsx").read_text(
        encoding="utf-8"
    )
    copy = (REPO_ROOT / "admin_ui" / "src" / "components" / "placeGroupCopy.js").read_text(
        encoding="utf-8"
    )
    assert "{event.event_type}" not in panel, "the audit line renders the raw identifier again"
    assert "placeGroupEventText(event.event_type, t)" in panel, (
        "the audit line no longer renders through the translated label map"
    )

    for event_type in PLACE_GROUP_EVENT_TYPES:
        key = f"ui.users.place_groups.event.{event_type}"
        assert f"'{key}'" in copy, f"{key} has no literal call site in the shared copy map"
        rows = {lang: _row(key, lang) for lang in LANGUAGES}
        for lang, value in rows.items():
            assert value.strip(), f"{key}:{lang} is blank"
            assert value != event_type, f"{key}:{lang} is still the raw identifier"
        assert len(set(rows.values())) == 3, f"{key} does not actually differ per language"


@pytest.mark.integration
def test_the_merge_review_list_renders_a_raw_ledger_event_type_that_IS_translated(
    place_seeds_full,
):
    """The SECOND raw render, and the worse one.

    ``PlaceGroupConfirmModal.jsx`` prints ``{entry.event_type}`` for a BOTTLE
    LEDGER entry inside the merge-review list (the list moved there with the
    rest of the confirm flow in A1.3; it printed the raw identifier in
    ``PlaceGroupPanel.jsx`` before that, and still does). Unlike the audit
    history above, trilingual copy for exactly those values already exists — the
    ``event_*`` rows the drawer uses — so this is not a missing translation, it
    is an existing one the component never calls. A Russian admin deciding which
    ledger entries to exclude from a merge reads ``admin_adjustment`` while the
    Bottle Tracking drawer two clicks away says «Ручная корректировка».
    """
    modal = (
        REPO_ROOT / "admin_ui" / "src" / "components" / "PlaceGroupConfirmModal.jsx"
    ).read_text(encoding="utf-8")
    assert "{entry.event_type}" in modal, "the raw render moved — re-check this pin"
    assert "eventTypeLabel" not in modal, "the modal gained a label helper — assert the render"

    for event in BottleLedgerEventType:
        translated = {lang: _row(f"event_{event.value}", lang) for lang in LANGUAGES}
        assert all(v.strip() for v in translated.values()), (event, translated)
        assert translated["ru"] != event.value, (
            f"event_{event.value} has translated copy the merge-review list does not use"
        )


@pytest.mark.integration
def test_place_group_audit_event_types_should_be_translatable(place_seeds):
    for event_type in (
        "create_place_group",
        "add_to_place_group",
        "remove_from_place_group",
        "dismiss_place_suggestion",
    ):
        for lang in LANGUAGES:
            assert _row(f"ui.users.place_groups.event.{event_type}", lang).strip()


@pytest.mark.integration
def test_the_place_lifecycle_ledger_notes_exist_in_english_only(place_seeds):
    """PINS THE DEFECT. Four hardcoded English sentences are the ONLY human
    explanation of an audited balance change, and ``BottleTracking.js`` renders
    them in a localised Notes column."""
    service_src = (
        REPO_ROOT / "business_app" / "services" / "customer_link_service.py"
    ).read_text(encoding="utf-8")
    notes = (
        "Bottles leaving with the address on place-group removal",
        "Place ledger aligned to the balance the place carries, during merge review",
        "Ledger entry excluded during place merge review",
        "Resulting bottle balance corrected during place merge review",
    )
    for note in notes:
        assert f'notes="{note}"' in service_src, f"note literal changed: {note!r}"
        assert Translation.query.filter(Translation.value == note).count() == 0

    page = (REPO_ROOT / "admin_ui" / "src" / "pages" / "BottleTracking.js").read_text(
        encoding="utf-8"
    )
    # ``"notes" in page`` was the old check and it cannot fail: the token occurs
    # 13 times in this file for waive dialogs, form items and unrelated tables.
    # Pin the actual LEDGER columns — a localised header over a column whose
    # CONTENT is the English service literal above, which is the whole point.
    ledger_note_columns = re.findall(
        r"title:\s*t\('notes',\s*\{\s*defaultValue:\s*'Notes'\s*\}\),\s*"
        r"dataIndex:\s*'notes',\s*key:\s*'notes'",
        page,
    )
    assert len(ledger_note_columns) >= 2, (
        "both place-ledger tables must still render a localised Notes column over "
        f"the untranslated service literals; found {len(ledger_note_columns)}"
    )


@pytest.mark.integration
def test_the_place_drawer_address_and_user_fallbacks_are_untranslated_literals(place_seeds):
    """``BottleTracking.js`` builds ``Address #12`` / ``User #7`` with template
    literals and no ``t()``. They are the FALLBACK path — i.e. exactly what a
    place assembled from title-less addresses shows a Russian admin."""
    page = (REPO_ROOT / "admin_ui" / "src" / "pages" / "BottleTracking.js").read_text(
        encoding="utf-8"
    )
    assert "`Address #${" in page
    assert "`User #${" in page
    assert Translation.query.filter(Translation.value.like("Address #%")).count() == 0
    assert Translation.query.filter(Translation.value.like("User #%")).count() == 0


# ===========================================================================
# L. Cross-surface vocabulary consistency
# ===========================================================================
@pytest.mark.integration
def test_the_place_vocabulary_pairs_that_share_english_are_identified(place_seeds):
    """Guards the SET of same-English pairs, so a new one has to be considered.

    ``ui.users.place_groups.union_balance`` and the bare ``place_balance_label``
    are both "Bottles at this place"; ``ui.users.place_groups.unnamed`` and
    ``ui.users.place_unnamed`` are both "Place".
    """
    assert _row("ui.users.place_groups.union_balance", "en") == _row(
        "place_balance_label", "en"
    ) == "Bottles at this place"
    assert _row("ui.users.place_groups.unnamed", "en") == _row("ui.users.place_unnamed", "en")


@pytest.mark.integration
def test_the_two_place_labels_that_share_english_use_the_place_vocabulary(place_seeds):
    """WAS THE DEFECT: the two labels said manzil/адрес on one screen and
    joy/место on the other.

    Beyond "they are equal" (the test below), pin the WORD: re-converging both
    onto the ADDRESS vocabulary would satisfy equality while re-introducing the
    address-vs-place conflation the re-key exists to remove.
    """
    for key in ("ui.users.place_groups.union_balance", "place_balance_label"):
        assert "joy" in _row(key, "uz").lower(), key
        assert "мест" in _row(key, "ru").lower(), key
        assert "manzil" not in _row(key, "uz").lower(), key
        assert "адрес" not in _row(key, "ru").lower(), key


@pytest.mark.integration
@pytest.mark.parametrize("lang", ("uz", "ru"))
def test_identical_english_place_copy_should_have_identical_uz_and_ru(place_seeds, lang):
    assert _row("ui.users.place_groups.union_balance", lang) == _row(
        "place_balance_label", lang
    )


@pytest.mark.integration
def test_the_shared_place_badge_names_one_shared_stock_in_every_language(place_seeds):
    """WAS THE DEFECT: the Uzbek said 'bitta hisob' — one ACCOUNT/balance.

    This badge is the single thing stopping an admin from summing three
    coworkers' map popups ('Bottles: 7' x3 = 21) on one shared place, so the
    per-account framing was the one thing it must not say. All three languages
    must name the PLACE and a single shared stock.
    """
    rows = _rows("ui.users.map.shared_place")
    assert "pool" in rows["en"]
    assert "пул" in rows["ru"]
    assert "мест" in rows["ru"].lower()
    assert "joy" in rows["uz"].lower()
    assert "yagona" in rows["uz"].lower(), rows["uz"]


@pytest.mark.integration
def test_the_shared_place_badge_should_not_say_account_in_uzbek(place_seeds):
    assert "hisob" not in _row("ui.users.map.shared_place", "uz")


@pytest.mark.integration
def test_the_driver_and_admin_names_for_the_place_cod_figure_agree_in_ru_but_not_uz(
    place_seeds,
):
    """PINS a cross-surface asymmetry worth reporting.

    A Russian-speaking admin and driver can reconcile the same number by name;
    an Uzbek-speaking pair cannot, because only the Uzbek wording diverges.
    """
    assert _row("staff.delivery.place_cod_total", "ru") == _row(
        "ui.users.place_groups.place_cod_total", "ru"
    )
    assert _row("staff.delivery.place_cod_total", "uz") != _row(
        "ui.users.place_groups.place_cod_total", "uz"
    )


@pytest.mark.integration
def test_the_remove_from_place_action_reads_as_delete_in_uz_and_ru(place_seeds):
    """PINS a copy hazard: the action removes an address FROM a group, while the
    system explicitly FORBIDS deleting a grouped address
    (``PLACE_GROUP_ADDRESS_NOT_DELETABLE``). In uz/ru the remove-from-group
    button and the delete-address action now read the same word, on the same
    customer page."""
    assert _row("ui.users.place_groups.remove", "en") == "Remove"
    assert _row("ui.users.place_groups.remove", "uz") == "O'chirish"
    assert _row("ui.users.place_groups.remove", "ru") == "Удалить"
    assert _row("ui.users.place_groups.remove_title", "uz").startswith("Ushbu manzilni")


# ===========================================================================
# M. The staff-seed generator interplay — /health green on broken copy
# ===========================================================================
@pytest.mark.integration
def test_the_staff_generator_alone_creates_placeholderless_rows_while_health_stays_green(db):
    """PINS THE HAZARD the additive-script design defends against.

    Run BEFORE the two curated place scripts, ``_extract_literal_keys`` picks up
    the 12 place ``staff.*`` keys, ``_curated_value`` returns None, and the
    CREATE arm writes ``_humanize_key`` output — dropping ``{union}``,
    ``{count}``, ``{quantity}``, ``{remaining}`` and
    ``{place_active_cod_debt_count}`` in all three languages while the row still
    EXISTS, so ``staff_bot``'s presence-only ``/health`` reports the service
    healthy on a catalog that shows the driver a fine prompt with no number.
    """
    _run_seed_staff_generator()

    damaged = {}
    for key, kwargs in STAFF_PLACE_CALLS.items():
        for lang in LANGUAGES:
            value = _row(key, lang)
            if not _placeholders(value):
                damaged.setdefault(key, {})[lang] = value
    assert set(damaged) == set(STAFF_PLACE_CALLS), (
        "expected EVERY interpolating place key to be humanised by the generator alone; "
        f"got {sorted(damaged)}"
    )
    assert (
        damaged["staff.delivery.fine_place_union_hint"]["en"] == "Delivery fine place union hint"
    ), "the humanised shape changed — re-read _humanize_key before trusting this pin"

    instance = _staff_i18n()
    assert instance.get_missing_translation_keys(list(LANGUAGES)) == {}, (
        "/health is presence-only, so it must be GREEN on this damaged catalog"
    )
    assert instance.get("staff.delivery.fine_place_union_hint", "ru", union="7") == _row(
        "staff.delivery.fine_place_union_hint", "ru"
    )
    assert "7" not in instance.get("staff.delivery.fine_place_union_hint", "ru", union="7")


@pytest.mark.integration
def test_the_curated_place_seeds_repair_the_generators_damage_in_all_three_languages(app, db):
    """The curated scripts use ``bulk_create_or_update`` (overwrites).

    If one were ever converted to an absent-only upsert "to be safe", the repair
    silently stops working and the humanised junk becomes permanent.
    """
    _run_seed_staff_generator()
    _run_seed(app, "seed_place_group_staff_translations", "run")
    _run_seed(app, "seed_staff_over_returned_translations", "run")

    for key, kwargs in STAFF_PLACE_CALLS.items():
        for lang in LANGUAGES:
            value = _row(key, lang)
            assert _placeholders(value) == set(kwargs), f"{key}:{lang} -> {value!r}"
            row = Translation.query.filter_by(key=key, language=lang).one()
            assert row.category == "staff_bot" and row.is_active is True
    for key in STAFF_LABEL_ONLY_KEYS:
        for lang in LANGUAGES:
            assert _row(key, lang) != _humanised(key), f"{key}:{lang} still humanised"


@pytest.mark.integration
def test_the_generator_run_after_the_curated_seeds_does_not_clobber_them(app, place_seeds):
    """``_curated_value is None -> skip`` is what protects the 12 curated rows."""
    before = {
        key: _rows(key)
        for key in ALL_STAFF_PLACE_KEYS
    }

    _run_seed_staff_generator()

    for key, rows in before.items():
        assert _rows(key) == rows, f"the generator overwrote {key}"


@pytest.mark.integration
def test_no_curated_place_key_is_claimed_by_the_generators_catalogs(place_seeds):
    """The existing textual guard searches for ``f'"{suffix}"'``, which does NOT
    match a FULL-key ``EXTRA_TRANSLATIONS`` entry — ``"staff.delivery.
    place_cod_total"`` does not contain the substring ``"place_cod_total"`` with
    its leading quote. Checked against the live dicts instead of the source text.
    """
    mod = _load_seed("seed_staff_translations")
    for key in ALL_STAFF_PLACE_KEYS:
        assert key not in mod.STAFF_TRANSLATIONS, key
        assert key not in mod.EXTRA_TRANSLATIONS, f"EXTRA_TRANSLATIONS claims {key}"
        for prefix, catalog in (
            ("staff.delivery.", mod.DELIVERY_TEXT_TRANSLATIONS),
            ("staff.operator.", mod.OPERATOR_TEXT_TRANSLATIONS),
        ):
            if key.startswith(prefix):
                suffix = key[len(prefix):]
                assert suffix not in catalog, f"{catalog} claims {key}"
        assert mod._curated_value(key, "ru") is None, f"the generator has an opinion on {key}"


@pytest.mark.integration
def test_staff_health_reports_the_missing_language_and_only_that_language(app, place_seeds):
    """``/health`` must go red while any place key is missing, and green once
    seeded — per language, so a single dropped Russian row is visible.

    The full documented staff deploy is run first (generator + the two curated
    place scripts, in that order), because a green baseline is what makes the
    single-row failure meaningful.
    """
    key = "staff.delivery.place_over_returned"
    _run_seed_staff_generator()
    _run_seed(app, "seed_place_group_staff_translations", "run")
    _run_seed(app, "seed_staff_over_returned_translations", "run")

    healthy = _staff_i18n()
    assert healthy.get_missing_translation_keys(list(LANGUAGES)) == {}

    broken = _staff_i18n(drop=((key, "ru"),))
    missing = broken.get_missing_translation_keys(list(LANGUAGES))
    assert set(missing) == {"ru"}, missing
    assert missing["ru"] == [key]


@pytest.mark.integration
def test_the_staff_key_extractor_still_finds_every_place_key_on_disk(place_seeds):
    """``_extract_literal_staff_keys`` only matches a LITERAL first argument.

    Any refactor that hoists a key into a constant or an f-string removes it
    from the required set and the bot goes green on a catalog that is short.
    """
    required = staff_i18n_module.Translation().get_required_staff_keys(force_refresh=True)
    missing = sorted(set(ALL_STAFF_PLACE_KEYS) - required)
    assert not missing, f"place keys the health check can no longer see: {missing}"
    reused_missing = sorted(set(STAFF_REUSED_ON_PLACE_SURFACES) - required)
    assert not reused_missing, f"reused place-surface keys dropped from /health: {reused_missing}"


# ===========================================================================
# N. Warning-code parity between the money engine and the admin UI
# ===========================================================================
@pytest.mark.integration
def test_every_cash_edit_warning_code_has_ui_copy_and_vice_versa(place_seeds):
    """A new warning added backend-side reaches a Russian admin as raw English;
    a renamed one silently stops translating and nothing fails."""
    service_src = (
        REPO_ROOT / "business_app" / "services" / "order_cash_edit_service.py"
    ).read_text(encoding="utf-8")
    backend_codes = set(
        re.findall(r'warnings\.append\(\s*\n?\s*(?:f?["\'])([a-z_]+)[ :\-]', service_src)
    )
    display_src = (REPO_ROOT / "admin_ui" / "src" / "utils" / "cashScopeDisplay.js").read_text(
        encoding="utf-8"
    )
    ui_codes = set(re.findall(r"\['([a-z_]+)',\s*\[", display_src))

    assert len(backend_codes) == 6, backend_codes
    assert backend_codes == ui_codes, (
        f"backend-only: {sorted(backend_codes - ui_codes)}; "
        f"ui-only: {sorted(ui_codes - backend_codes)}"
    )
    for key in sorted(k for k in _ui_place_keys() if k.startswith("ui.orders.cash_warning_")):
        for lang in LANGUAGES:
            assert _row(key, lang).strip()


@pytest.mark.integration
def test_both_backend_warning_separators_survive_the_ui_code_extraction(place_seeds):
    """Five backend warnings use ``" - "`` and one uses ``": "``.

    ``describeCashEditWarning`` splits on ``/[:\\s]/``, so both work today — but
    a separator change on the backend would break the lookup for exactly one
    warning and it would silently render English. Pinned on the backend side,
    which is the half a Python test can see.
    """
    service_src = (
        REPO_ROOT / "business_app" / "services" / "order_cash_edit_service.py"
    ).read_text(encoding="utf-8")
    dash = len(re.findall(r'warnings\.append\(\s*\n?\s*["\'][a-z_]+ - ', service_src))
    colon = len(re.findall(r'warnings\.append\(\s*\n?\s*["\'][a-z_]+: ', service_src))
    assert dash == 5 and colon == 1, (dash, colon)

    # Both halves, not just the backend's. The claim is that the UI's splitter
    # tolerates the two separators; asserting only the backend counts would
    # still pass if the splitter were narrowed to `split(' ')`, which silently
    # un-translates the one colon-separated warning.
    display_src = (REPO_ROOT / "admin_ui" / "src" / "utils" / "cashScopeDisplay.js").read_text(
        encoding="utf-8"
    )
    splitter = re.search(r"String\(warning\)\.split\((/[^/]+/)[^)]*\)", display_src)
    assert splitter, "describeCashEditWarning no longer splits the warning string"
    separators = splitter.group(1)
    assert ":" in separators and r"\s" in separators, separators
    # And the codes those separators have to yield are the ones the UI maps.
    ui_codes = set(re.findall(r"\['([a-z_]+)',\s*\[", display_src))
    for match in re.finditer(r'warnings\.append\(\s*\n?\s*(?:f?["\'])([a-z_]+)([ :\-])', service_src):
        code, separator = match.group(1), match.group(2)
        assert code in ui_codes, code
        assert separator in (" ", ":"), (code, separator)


# ===========================================================================
# O. The two real dev-DB place shapes render correctly in three languages
# ===========================================================================
@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_a_place_whose_history_matches_its_balance_suppresses_the_drift_hint_copy(
    app, client, place_seeds, grouped_place, lang
):
    """Mirrors dev group 9 (stored 7.00, ledger 6+5-4 = 7.00, drift 0).

    The hint is CONDITIONAL because the repair is: with no exclusion and no
    override ``_apply_merge_review`` returns before writing anything, so the
    drift survives the join. All three languages must state that conditional.
    """
    from business_app.models.bottle import BottleLedger

    # A GLOBAL ledger sum is blind to a scope-attribution defect: moving Bob's
    # 3 bottles onto the wrong scope key conserves the total AND leaves
    # get_place_balance at 7, so the equality below would still hold. Assert the
    # PAIR — the entries attributed to THIS place, and the absence of any entry
    # left behind on a bare address — alongside the global figure.
    group_id = grouped_place["addr_a"].address_group_id
    scoped = BottleLedger.query.filter_by(address_group_id=group_id).all()
    orphaned = BottleLedger.query.filter(BottleLedger.address_group_id.is_(None)).all()
    scoped_sum = sum((e.quantity for e in scoped), Decimal("0.00"))
    ledger_sum = sum(
        (e.quantity for e in BottleLedger.query.all()), Decimal("0.00")
    )
    assert len(scoped) == 2, [(e.address_id, str(e.quantity)) for e in scoped]
    assert not orphaned, (
        "the join must re-scope every entry onto the place; these stayed on a "
        f"bare address: {[(e.address_id, str(e.quantity)) for e in orphaned]}"
    )
    assert scoped_sum == Decimal("7.00")
    assert ledger_sum == Decimal("7.00")
    assert BottleTrackingService.get_place_balance(grouped_place["addr_a"].id) == scoped_sum
    assert BottleTrackingService.get_place_balance(grouped_place["addr_b"].id) == scoped_sum
    assert BottleTrackingService.get_place_balance(grouped_place["addr_a"].id) == ledger_sum

    hint = _row("ui.users.place_groups.merge_drift_hint", lang)
    assert hint.strip()
    for figure in (
        "merge_computed_balance",
        "merge_excluded_total",
        "merge_resulting_balance",
        "merge_projected_balance",
        "merge_drift",
    ):
        assert _row(f"ui.users.place_groups.{figure}", lang).strip()


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_drift_hint_copy_states_the_conditional_in_every_language(place_seeds, lang):
    """An UNCONDITIONAL translation in one language tells the admin the books
    were fixed when they were not — on the path they are most likely to take."""
    hint = _row("ui.users.place_groups.merge_drift_hint", lang)
    conditional_markers = {
        "en": "joining without a change leaves it in place",
        "uz": "o'zgarishsiz birlashtirish",
        "ru": "объединение без изменений",
    }
    assert conditional_markers[lang] in hint, f"{lang} drift hint lost its conditional: {hint!r}"
    assert len(hint.split()) > 20, "the hint must explain both figures, not just name one"


@pytest.mark.integration
def test_a_drifted_place_keeps_the_stored_figure_and_still_has_hint_copy(app, db, place_seeds):
    """Mirrors dev address 24 (stored 20.00, ZERO ledger rows, drift 20).

    Built the only honest way: adjust the balance, then delete the ledger rows
    the adjustment wrote, so the stored figure survives with no history to
    explain it — exactly the production shape. Asserted as a PAIR (stored vs
    ledger sum) so the test cannot pass on broken money.
    """
    from business_app.models.bottle import BottleLedger

    owner = _user(db, "drift-a@example.com", "+998900007730")
    address = _addr(db, owner, title="Drifted")
    _put_bottles(db, address, owner, "20")
    assert BottleTrackingService.get_place_balance(address.id) == Decimal("20.00")

    for entry in BottleLedger.query.all():
        db.session.delete(entry)
    db.session.commit()

    stored = BottleTrackingService.get_place_balance(address.id)
    ledger_sum = sum((e.quantity for e in BottleLedger.query.all()), Decimal("0.00"))
    assert (stored, ledger_sum) == (Decimal("20.00"), Decimal("0.00"))

    for lang in LANGUAGES:
        assert _row("ui.users.place_groups.merge_drift", lang).strip()
        assert _row("ui.users.place_groups.merge_projected_balance", lang).strip()
        assert _row("ui.users.place_groups.merge_drift_hint", lang).strip()


# ===========================================================================
# P. Copy stability while the numbers move
# ===========================================================================
@pytest.mark.integration
def test_a_place_surface_keeps_its_copy_while_the_balance_moves(
    app, client, db, place_seeds, patched_tg_i18n, grouped_place
):
    """Only the NUMBERS may change between renders.

    The over-returned branch swaps the KEY, not just the number, and the two
    keys have different placeholder sets — so if one is unseeded in one
    language, the flip is where it shows up.
    """
    alice, addr_a = grouped_place["alice"], grouped_place["addr_a"]
    headers = _headers(app, alice)

    def _body(lang):
        overview = client.get(
            "/api/v1/orders/bottles/my-balances", headers=headers
        ).get_json()["data"]
        return "\n".join(tg_bottles._build_balance_lines(overview, lang))

    stages = []
    stages.append({lang: _body(lang) for lang in LANGUAGES})
    BottleTrackingService().record_bottles_delivered(
        order_id=None, user_id=alice.id, address_id=addr_a.id, quantity=Decimal("3")
    )
    db.session.commit()
    stages.append({lang: _body(lang) for lang in LANGUAGES})
    BottleTrackingService().record_bottles_returned(
        user_id=alice.id, address_id=addr_a.id, quantity=Decimal("2")
    )
    db.session.commit()
    stages.append({lang: _body(lang) for lang in LANGUAGES})

    expected_totals = ["7", "10", "8"]
    for stage, total in zip(stages, expected_totals):
        for lang in LANGUAGES:
            assert _row("telegram.bottles.place_total", lang).format(total=total) in stage[lang]
            assert "{" not in stage[lang]
    # The place_total line's non-numeric skeleton is identical at every stage.
    for lang in LANGUAGES:
        skeletons = {
            re.sub(r"[\d.]+", "#", body) for body in (s[lang] for s in stages)
        }
        assert len(skeletons) == 1, f"{lang} copy changed as the balance moved: {skeletons}"


@pytest.mark.integration
def test_repeated_renders_of_an_unchanged_place_are_byte_identical_in_each_language(
    app, client, place_seeds, patched_tg_i18n, grouped_place
):
    """Idempotent AND non-destructive.

    Two deep copies in and out proves only that the helper is deterministic —
    which almost nothing can break. The failure that actually reaches a
    customer is the helper MUTATING the payload it was handed (popping
    ``place_members`` as it renders, normalising a quantity in place): the
    orders menu renders this same overview once per language on a language
    switch, so the second screen silently loses its member list. Feeding the
    SAME object twice is what catches that, and the snapshot comparison names
    it directly.
    """
    headers = _headers(app, grouped_place["alice"])
    overview = client.get("/api/v1/orders/bottles/my-balances", headers=headers).get_json()["data"]
    snapshot = copy.deepcopy(overview)

    for lang in LANGUAGES:
        first = tg_bottles._build_balance_lines(overview, lang)
        second = tg_bottles._build_balance_lines(overview, lang)
        assert first == second
        assert overview == snapshot, f"{lang}: the renderer mutated the payload it was given"
        # A render that produced nothing would satisfy `first == second`.
        assert any(
            _row("telegram.bottles.place_total", lang).format(total="7") in line
            for line in first
        ), first
