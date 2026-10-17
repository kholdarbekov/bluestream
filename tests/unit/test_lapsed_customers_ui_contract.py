"""Every reorder-reminder string the admin UI renders is seeded trilingual, as the page says it.

`ui.*` keys resolve from the DB (category `ui`, the common namespace). An unseeded key renders its
English default in every language, so an admin working in uz or ru silently reads English. The
scan reads the static keys out of the sources; the published vocabularies give the dynamic ones
(`ui.reorder_reminders.via.<via>` and friends).

The same module pins the Users page's side of the admin settings contract against the real
backend: the field names the switches send, the published setting the page reads, and the refusal
shape its error handling reads.

The Analytics overview cards and the inactive-customers columns (Task 24b) are held to the real
summary and the real inactive-customers endpoint the same way, and their strings are seeded
trilingual like the page's.

The Lapsed customers page (Task 23a) is held to the real worklist the same way: the row keys of its
Vitest fixture are the service's own `ROW_KEYS`; every field the page reads off a row, a nested
record, the summary or the metrics is one a real HTTP response publishes; and the requests it sends
(every tab, every flag chip, every outcome, the turn-off) are accepted in the shape it sends them.
"""

import pathlib
import re
from collections import defaultdict
from datetime import date, datetime, timezone

import pytest

from business_app.models.reorder_reminder import (
    CHANNELS,
    ESCALATION_FLAGS,
    EVENT_KINDS,
    OUTCOMES,
    SETTING_ACTOR_KINDS,
    SETTING_VIAS,
    SUSPEND_REASONS,
)
from business_app.services.reorder_reminders.worklist import ROW_KEYS, TABS
from business_app.utils.state_validators import ACTIVE_ORDER_STATUSES
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from tests.integration.reorder_reminder_builders import frozen_fixture  # noqa: F401 - the `frozen` fixture
from tests.unit.test_place_group_translation_seeds import _JS_PAIR, _JS_STRING, _unquote_js

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ADMIN_UI = REPO_ROOT / "admin_ui" / "src"
LAPSED_PAGE = ADMIN_UI / "pages" / "LapsedCustomers.js"
LAYOUT = ADMIN_UI / "components" / "layout" / "AdminLayout.js"
ANALYTICS_PAGE = ADMIN_UI / "pages" / "Analytics.js"
SCANNED_SOURCES = (
    ADMIN_UI / "pages" / "Users.js",
    ADMIN_UI / "utils" / "reorderReminderSetting.js",
    LAPSED_PAGE,
    LAYOUT,
    ANALYTICS_PAGE,
)
# The two Analytics keys this feature adds (the inactive-customers columns, which now measure the last
# DELIVERY). Analytics.js renders ~100 other `ui.analytics.*` keys that are not this feature's.
ANALYTICS_OWNED = ("ui.analytics.last_delivery", "ui.analytics.days_since_last_delivery")
OWNED_PREFIXES = (
    "ui.users.reorder_reminders",
    "ui.reorder_reminders.",
    "ui.lapsed.",
    "ui.nav.lapsed_customers",
    *ANALYTICS_OWNED,
)
PAGE_TEST = ADMIN_UI / "__tests__" / "pages" / "LapsedCustomers.test.js"
ANALYTICS_CARDS_TEST = ADMIN_UI / "__tests__" / "pages" / "Analytics.lapsedCards.test.js"
SETTINGS_KEYS = (
    "fallback_days", "ratio", "min_deliveries", "max_per_episode", "gap_days", "snooze_days",
    "escalate_after_days", "fresh_days", "live_order_grace_days", "local_time",
)
LANGUAGES = ("en", "uz", "ru")
# A key literal the sources hand to i18next, single- or double-quoted: `t('ui.x', ...)` or a `labelKey: 'ui.x'`
# property of an options table later passed to `t`. A template literal (`ui.x.${kind}`) is dynamic, not static.
_STATIC_KEY = re.compile(r"""(?:\bt\(\s*|\b\w*Key\s*:\s*)(['"])(ui\.[\w.]+)\1""")
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
# Seeded values that legitimately read like the English text, per language. Anything else equal to
# English (uz) or without Cyrillic (ru) is copy that was never translated.
SAME_AS_ENGLISH = {
    "uz": {
        # Uzbek writes "admin panel" as the loanword too.
        "ui.reorder_reminders.via.admin_ui",
        # Brand names, written the same in every language (seeded by Task 23a).
        "ui.lapsed.channel.telegram",
        "ui.lapsed.channel.email",
    },
    "ru": {
        # Brand names: "Telegram" and "Email" stay in Latin script in Russian admin copy.
        "ui.lapsed.channel.telegram",
        "ui.lapsed.channel.email",
    },
}


def _owned_keys_in(text):
    return {key for _, key in _STATIC_KEY.findall(text) if key.startswith(OWNED_PREFIXES)}


def _static_keys():
    keys = set()
    for source in SCANNED_SOURCES:
        keys |= _owned_keys_in(source.read_text(encoding="utf-8"))
    return keys


def _vocabulary_keys():
    keys = {f"ui.reorder_reminders.via.{via}" for via in SETTING_VIAS}
    keys |= {f"ui.reorder_reminders.source.{kind}" for kind in SETTING_ACTOR_KINDS}
    keys.add("ui.reorder_reminders.source.default")
    keys |= {f"ui.lapsed.flag.{flag}" for flag in ESCALATION_FLAGS}
    keys |= {f"ui.lapsed.outcome.{outcome}" for outcome in OUTCOMES}
    keys |= {f"ui.lapsed.suspend.{reason}" for reason in SUSPEND_REASONS}
    keys |= {f"ui.lapsed.event.{kind}" for kind in EVENT_KINDS}
    keys |= {f"ui.lapsed.channel.{channel}" for channel in CHANNELS}
    keys |= {f"ui.lapsed.tab.{tab}" for tab in TABS}
    keys |= {f"ui.lapsed.settings.{key}" for key in SETTINGS_KEYS}
    # A stuck order is always one of the live statuses (`ACTIVE_ORDER_STATUSES`); the page labels it
    # with its own rows, because the Orders page's `ui.orders.status_*` rows are not seeded by the repo.
    keys |= {f"ui.lapsed.order_status.{status.value}" for status in ACTIVE_ORDER_STATUSES}
    # Seeded with this page and rendered by Tasks 23b (the nav item) and 24b (the Analytics link);
    # both are scanned from those sources now, and stay listed here so a dropped call site cannot
    # also drop the seed from the checks.
    keys |= {"ui.nav.lapsed_customers", "ui.lapsed.open_list"}
    keys |= set(ANALYTICS_OWNED)
    return keys


def test_the_scan_sees_the_pages_keys():
    assert {"ui.users.reorder_reminders_setting", "ui.reorder_reminders.setting_off"} <= _static_keys()


@pytest.mark.parametrize("key", sorted(_static_keys() | _vocabulary_keys()))
def test_every_reorder_reminder_ui_key_is_seeded_trilingual_with_one_placeholder_set(key):
    assert key in BACKEND_TRANSLATIONS, f"{key} is rendered by the admin UI but never seeded"
    values = BACKEND_TRANSLATIONS[key]
    for language in LANGUAGES:
        assert values.get(language), f"{key} has no {language} value"
    placeholder_sets = {language: frozenset(_PLACEHOLDER.findall(values[language])) for language in LANGUAGES}
    assert len(set(placeholder_sets.values())) == 1, placeholder_sets


# `{labelKey: 'ui.x', fallback: 'English'}`: the options-table spelling of `t('ui.x', 'English')`.
_KEY_PROPERTY_PAIR = re.compile(
    r"""\b\w*Key\s*:\s*(['"])(ui\.[\w.]+)\1\s*,\s*fallback\s*:\s*({s}(?:\s*\+\s*{s})*)""".format(s=_JS_STRING),
    re.S,
)


def _fallbacks_in(text):
    """key -> every plain string-literal fallback `text` passes for it (a set, so drift shows)."""
    found = defaultdict(set)
    for _, key, expr in [*_JS_PAIR.findall(text), *_KEY_PROPERTY_PAIR.findall(text)]:
        found[key].add("".join(_unquote_js(literal) for literal in re.findall(_JS_STRING, expr, re.S)))
    return found


def _call_site_fallbacks():
    found = defaultdict(set)
    for source in SCANNED_SOURCES:
        for key, fallbacks in _fallbacks_in(source.read_text(encoding="utf-8")).items():
            found[key] |= fallbacks
    return found


def _assert_fallback_is_seeded_english(key, fallbacks, seeded_english):
    assert fallbacks, (
        f"{key}: no plain string-literal fallback was found at its call site (a variable, a template "
        "literal, a computed expression or no fallback at all). Pass the English text as a literal so "
        "the seed can be checked against it."
    )
    assert fallbacks == {seeded_english}, (
        f"{key}: the call sites pass {sorted(fallbacks)!r} but the seeded en is {seeded_english!r}"
    )


@pytest.mark.parametrize("key", sorted(_static_keys()))
def test_the_seeded_english_is_the_call_sites_fallback_byte_for_byte(key):
    """Before the seed runs the page shows its fallback; after, the DB row. They must be one text."""
    seeded = BACKEND_TRANSLATIONS.get(key)
    assert seeded, f"{key} is rendered by the admin UI but never seeded"
    _assert_fallback_is_seeded_english(key, _call_site_fallbacks().get(key), seeded["en"])


def test_the_scan_reads_label_key_properties_and_double_quoted_literals():
    source = """
        t("ui.reorder_reminders.double", "Double " + 'quoted')
        const OPTION = { labelKey: 'ui.users.reorder_reminders_prop', fallback: "From a property" };
        const OTHER = { titleKey: "ui.users.reorder_reminders_title", fallback: 'Title' };
        t(`ui.reorder_reminders.dynamic.${kind}`, kind)
        t('ui.users.reorder_reminders_bare')
        t('ui.users.cod_restricted', 'Not ours')
    """
    assert _owned_keys_in(source) == {
        "ui.reorder_reminders.double",
        "ui.users.reorder_reminders_prop",
        "ui.users.reorder_reminders_title",
        "ui.users.reorder_reminders_bare",
    }
    fallbacks = _fallbacks_in(source)
    assert fallbacks["ui.reorder_reminders.double"] == {"Double quoted"}
    assert fallbacks["ui.users.reorder_reminders_prop"] == {"From a property"}
    assert fallbacks["ui.users.reorder_reminders_title"] == {"Title"}
    assert "ui.users.reorder_reminders_bare" not in fallbacks


def test_a_key_without_a_plain_literal_fallback_fails_with_a_clear_message():
    with pytest.raises(AssertionError, match="no plain string-literal fallback was found at its call site"):
        _assert_fallback_is_seeded_english("ui.users.reorder_reminders_x", None, "Seeded")
    with pytest.raises(AssertionError, match=r"call sites pass \['Other'\] but the seeded en is 'Seeded'"):
        _assert_fallback_is_seeded_english("ui.users.reorder_reminders_x", {"Other"}, "Seeded")
    _assert_fallback_is_seeded_english("ui.users.reorder_reminders_x", {"Seeded"}, "Seeded")


# --------------------------------------------------------------------------- #
# Resolution through the real AdminUiTranslationService (what i18next is served)
# --------------------------------------------------------------------------- #
def _page_namespace_chain(page=None):
    """The namespaces i18next consults for a page's `t`: the one it opens, then `fallbackNS`."""
    page = page or ADMIN_UI / "pages" / "Users.js"
    # `useTranslation('users')` or `useTranslation(['analytics', 'sales_agents'])`: react-i18next's `t`
    # is bound to the FIRST namespace only (the others are loaded, not consulted).
    opened = re.search(r"useTranslation\(\s*\[?\s*'(\w+)'", page.read_text(encoding="utf-8"))
    fallback = re.search(r"fallbackNS:\s*\[([^\]]*)\]", (ADMIN_UI / "i18n.js").read_text(encoding="utf-8"))
    assert opened and fallback, f"{page.name}'s namespace or i18n.js's fallbackNS moved: re-derive the chain"
    return [opened.group(1), *re.findall(r"'(\w+)'", fallback.group(1))]


@pytest.fixture
def seeded_reminder_ui_keys(app, db):
    """The owned keys written by the seed script's own category and value rules."""
    from business_app.models.translation import Translation
    from scripts.seed_backend_translations import _category_for, _resolve_seed_value

    keys = sorted(_static_keys() | _vocabulary_keys())
    with app.app_context():
        for key in keys:
            assert key in BACKEND_TRANSLATIONS, f"{key} is rendered by the admin UI but never seeded"
            for language in LANGUAGES:
                value, _ = _resolve_seed_value(BACKEND_TRANSLATIONS[key], language)
                Translation.bulk_create_or_update({language: {key: value}}, category=_category_for(key))
    return keys


def _resolve(language, key, chain):
    from business_app.services.admin_ui_translation_service import AdminUiTranslationService

    for namespace in chain:
        bundle = AdminUiTranslationService.get_translations(language, namespace)
        if key in bundle:
            return bundle[key]
    return None


def test_the_page_opens_users_and_falls_back_to_common():
    assert _page_namespace_chain() == ["users", "common"]


def test_the_lapsed_page_opens_the_common_namespace():
    """`ui.lapsed.*` is category `ui` with no `LEGACY_NAMESPACE_PREFIXES` entry, so it is served by
    `common` only (every `ui` row is). The page must open that namespace, or its keys would miss."""
    from business_app.services.admin_ui_translation_service import AdminUiTranslationService

    assert _page_namespace_chain(LAPSED_PAGE) == ["common", "common"]
    claimed = [
        prefix
        for prefixes in AdminUiTranslationService.LEGACY_NAMESPACE_PREFIXES.values()
        for prefix in prefixes
        if "ui.lapsed.".startswith(prefix) or prefix.startswith("ui.lapsed.")
    ]
    assert claimed == [], f"another namespace claims ui.lapsed.*: {claimed}"


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_reminder_key_resolves_for_the_page_in_every_language(app, seeded_reminder_ui_keys, language):
    with app.app_context():
        for key in seeded_reminder_ui_keys:
            resolved = _resolve(language, key, _page_namespace_chain())
            assert resolved == BACKEND_TRANSLATIONS[key][language], f"{key} does not resolve in {language}"


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_key_the_lapsed_page_renders_resolves_for_it_in_every_language(app, seeded_reminder_ui_keys, language):
    chain = _page_namespace_chain(LAPSED_PAGE)
    rendered = [key for key in seeded_reminder_ui_keys if key.startswith(("ui.lapsed.", "ui.reorder_reminders."))]
    assert len(rendered) > 100, "the Lapsed page's key set looks truncated"
    with app.app_context():
        for key in rendered:
            assert _resolve(language, key, chain) == BACKEND_TRANSLATIONS[key][language], (language, key)


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_nav_item_resolves_for_the_layout_in_every_language(app, seeded_reminder_ui_keys, language):
    """The layout opens `navigation`, which claims the `ui.nav.` prefix, so the nav label is served there."""
    chain = _page_namespace_chain(LAYOUT)
    assert chain == ["navigation", "common"], chain
    expected = BACKEND_TRANSLATIONS["ui.nav.lapsed_customers"][language]
    with app.app_context():
        assert _resolve(language, "ui.nav.lapsed_customers", chain) == expected


def test_the_analytics_page_opens_analytics_then_common():
    """`useTranslation(['analytics', 'sales_agents'])` binds `t` to `analytics` (the second name is only
    loaded); `fallbackNS` adds `common`, which serves every shared `ui` row, `ui.lapsed.*` included."""
    assert _page_namespace_chain(ANALYTICS_PAGE) == ["analytics", "common"]


def test_the_scan_sees_the_analytics_cards_and_columns():
    rendered = _owned_keys_in(ANALYTICS_PAGE.read_text(encoding="utf-8"))
    assert rendered == {"ui.lapsed.tab.reminding", "ui.lapsed.tab.follow_up", "ui.lapsed.open_list", *ANALYTICS_OWNED}


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_key_the_analytics_page_renders_for_this_feature_resolves_for_it_in_every_language(
    app, seeded_reminder_ui_keys, language
):
    from business_app.services.admin_ui_translation_service import AdminUiTranslationService

    chain = _page_namespace_chain(ANALYTICS_PAGE)
    rendered = sorted(_owned_keys_in(ANALYTICS_PAGE.read_text(encoding="utf-8")))
    assert len(rendered) == 5
    with app.app_context():
        for key in rendered:
            assert _resolve(language, key, chain) == BACKEND_TRANSLATIONS[key][language], (language, key)
        # The two `ui.analytics.*` columns are claimed by the `analytics` namespace itself, not by the fallback.
        analytics_bundle = AdminUiTranslationService.get_translations(language, "analytics")
        for key in ANALYTICS_OWNED:
            assert analytics_bundle[key] == BACKEND_TRANSLATIONS[key][language], (language, key)


def test_uz_and_ru_admins_never_read_the_english_copy_on_the_analytics_page(app, seeded_reminder_ui_keys):
    chain = _page_namespace_chain(ANALYTICS_PAGE)
    rendered = sorted(_owned_keys_in(ANALYTICS_PAGE.read_text(encoding="utf-8")))
    leaks = []
    with app.app_context():
        for key in rendered:
            english = _resolve("en", key, chain)
            for language in ("uz", "ru"):
                if _reads_untranslated(language, key, _resolve(language, key, chain), english):
                    leaks.append((language, key))
    assert not leaks, f"English copy served to uz/ru admins on Analytics: {leaks}"


def _reads_untranslated(language, key, value, english):
    """True when `value` is English copy served to a uz/ru admin, unless the key is allow-listed."""
    if key in SAME_AS_ENGLISH[language]:
        return False
    if language == "ru":
        return not re.search("[А-Яа-яЁё]", value)
    return value == english


def test_uz_and_ru_admins_never_read_the_english_copy(app, seeded_reminder_ui_keys):
    chain = _page_namespace_chain()
    leaks = []
    with app.app_context():
        for key in seeded_reminder_ui_keys:
            english = _resolve("en", key, chain)
            for language in ("uz", "ru"):
                if _reads_untranslated(language, key, _resolve(language, key, chain), english):
                    leaks.append((language, key))
    assert not leaks, f"English copy served to uz/ru admins: {leaks}"


def test_brand_names_may_read_the_same_in_every_language_but_other_copy_may_not():
    for language in ("uz", "ru"):
        assert not _reads_untranslated(language, "ui.lapsed.channel.telegram", "Telegram", "Telegram")
        assert not _reads_untranslated(language, "ui.lapsed.channel.email", "Email", "Email")
        assert _reads_untranslated(language, "ui.lapsed.tab.follow_up", "Follow up", "Follow up")
    # The Uzbek loanword is allowed; the Russian "admin panel" is not (and is Cyrillic in the seed).
    assert not _reads_untranslated("uz", "ui.reorder_reminders.via.admin_ui", "admin panel", "admin panel")
    assert _reads_untranslated("ru", "ui.reorder_reminders.via.admin_ui", "admin panel", "admin panel")
    assert not _reads_untranslated("ru", "ui.reorder_reminders.via.admin_ui", "админ-панель", "admin panel")


# --------------------------------------------------------------------------- #
# Users page <-> admin settings API, against the real backend
# --------------------------------------------------------------------------- #
def _users_source():
    return (ADMIN_UI / "pages" / "Users.js").read_text(encoding="utf-8")


def _users_constant(name):
    match = re.search(rf"^const {name} = '([^']+)';", _users_source(), re.M)
    assert match, f"Users.js no longer declares {name}"
    return match.group(1)


def _put_url(user):
    return f"/api/v1/admin/users/{user.id}/notification-settings"


# (field constant in Users.js, direction the switch can be flipped to, reason typed in the modal)
SWITCH_PAYLOADS = [
    pytest.param("DELIVERY_TELEGRAM_FIELD", False, "Asked to stop night pings", id="delivery-off"),
    pytest.param("DELIVERY_TELEGRAM_FIELD", True, "Wants delivery updates again", id="delivery-on"),
    pytest.param("REORDER_REMINDERS_FIELD", False, "Moved to Samarkand", id="reminders-off"),
    pytest.param("REORDER_REMINDERS_FIELD", True, "Back in Tashkent", id="reminders-on"),
]


@pytest.mark.parametrize("constant,enabled,reason", SWITCH_PAYLOADS)
def test_each_switch_payload_the_page_builds_is_accepted_and_published(
    client, db, manager_claim_headers, constant, enabled, reason
):
    """The page sends `{[field]: enabled, reason}`: the field names come from Users.js itself."""
    from tests.integration.reorder_reminder_builders import make_customer

    customer = make_customer(db, first_name="Dilnoza")
    field = _users_constant(constant)

    response = client.put(_put_url(customer), json={field: enabled, "reason": reason}, headers=manager_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    settings = response.get_json()["data"]["notification_settings"]
    if field == "reorder_reminders_enabled":
        assert (settings["reorder_reminders"]["enabled"], settings["reorder_reminders"]["reason"]) == (enabled, reason)
    else:
        assert settings[field] is enabled


def test_the_page_reads_only_fields_the_backend_publishes():
    """Every attribute the page and the shared label read off the reminder block is a published key."""
    from business_app.services.reorder_reminders.settings import DEFAULT_SETTING

    published = set(DEFAULT_SETTING.to_dict(include_reason=True))
    helper = (ADMIN_UI / "utils" / "reorderReminderSetting.js").read_text(encoding="utf-8")
    read_by_helper = set(re.findall(r"\bsetting\.(\w+)", helper))
    read_by_page = set(re.findall(r"reorder_reminders\??\.(\w+)", _users_source()))

    assert read_by_helper and read_by_page
    assert read_by_helper | read_by_page <= published


def test_the_pages_read_the_published_setting_state_and_keep_no_copy_of_its_rule():
    """Final review deferred 507 (ruling): the staff shape publishes `state`; the Users page offers
    *Exclude* on `state === customer_off` and the Lapsed page marks an off line from `state`. The JS
    names the backend's three states, and the old `isCustomerOff` copy of the rule is gone."""
    from business_app.services.reorder_reminders.settings import SETTING_STATES

    helper = (ADMIN_UI / "utils" / "reorderReminderSetting.js").read_text(encoding="utf-8")
    declared = re.search(r"export const REMINDER_SETTING_STATE = \{(.*?)\};", helper, re.DOTALL)
    assert declared, "reorderReminderSetting.js no longer declares REMINDER_SETTING_STATE"
    assert sorted(re.findall(r"'([a-z_]+)'", declared.group(1))) == sorted(SETTING_STATES)
    assert "isCustomerOff" not in helper and "isCustomerOff" not in _users_source()
    assert "reorder_reminders?.state === REMINDER_SETTING_STATE.CUSTOMER_OFF" in _users_source()
    assert "row.setting.state !== REMINDER_SETTING_STATE.ON" in LAPSED_PAGE.read_text(encoding="utf-8")


def test_a_reminder_change_without_a_reason_is_refused_in_the_shape_the_page_reads(
    client, db, manager_claim_headers
):
    """`errors[0]` is the localized text and `data.error_code` the code the page names."""
    from business_app.services.reorder_reminders.settings import REASON_REQUIRED
    from tests.integration.reorder_reminder_builders import make_customer

    customer = make_customer(db, first_name="Javlon")
    assert _users_constant("REASON_REQUIRED_CODE") == REASON_REQUIRED

    response = client.put(
        _put_url(customer),
        json={_users_constant("REORDER_REMINDERS_FIELD"): False, "reason": "   "},
        headers=manager_claim_headers,
    )

    body = response.get_json()
    assert response.status_code == 400
    assert body["data"]["error_code"] == REASON_REQUIRED
    assert isinstance(body["errors"], list) and body["errors"][0].strip()


def test_a_blank_reason_on_both_fields_is_refused_with_a_message_and_no_code(client, db, manager_claim_headers):
    """The delivery rule refuses first (amendment 20), so the page cannot rely on the code alone."""
    from tests.integration.reorder_reminder_builders import make_customer

    customer = make_customer(db, first_name="Madina")

    response = client.put(
        _put_url(customer),
        json={
            _users_constant("DELIVERY_TELEGRAM_FIELD"): False,
            _users_constant("REORDER_REMINDERS_FIELD"): False,
            "reason": "   ",
        },
        headers=manager_claim_headers,
    )

    body = response.get_json()
    assert response.status_code == 400
    assert "error_code" not in (body.get("data") or {})
    assert isinstance(body["errors"], list) and body["errors"][0].strip()


def test_the_staff_off_action_upgrades_a_customers_own_off_in_the_shape_the_page_redraws(
    client, db, manager_claim_headers
):
    """The action is offered for a published customer-off (the `state` the page compares, deferred
    507) and sends the reminder-off payload (spec §8)."""
    from business_app.services.reorder_reminders.settings import (
        ACTOR_CUSTOMER,
        STATE_ADMIN_OFF,
        STATE_CUSTOMER_OFF,
        ReorderReminderSettingsService,
    )
    from tests.integration.reorder_reminder_builders import make_customer

    assert "REMINDER_SETTING_STATE.CUSTOMER_OFF" in _users_source()
    customer = make_customer(db, first_name="Sardor")
    ReorderReminderSettingsService.set(customer.id, False, actor_kind=ACTOR_CUSTOMER, via="telegram_button")

    published = client.get(_put_url(customer), headers=manager_claim_headers).get_json()
    before = published["data"]["notification_settings"]["reorder_reminders"]
    assert (before["enabled"], before["changed_by_kind"], before["changed_via"], before["state"]) == (
        False,
        "customer",
        "telegram_button",
        STATE_CUSTOMER_OFF,
    )
    assert ReorderReminderSettingsService.effective_for([customer.id]).customer_off is True

    reason = "Asked us to leave him out"
    response = client.put(
        _put_url(customer),
        json={_users_constant("REORDER_REMINDERS_FIELD"): False, "reason": reason},
        headers=manager_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    after = response.get_json()["data"]["notification_settings"]["reorder_reminders"]
    assert (after["enabled"], after["changed_by_kind"], after["changed_via"], after["reason"], after["state"]) == (
        False,
        "admin",
        "admin_ui",
        reason,
        STATE_ADMIN_OFF,
    )
    assert ReorderReminderSettingsService.effective_for([customer.id]).admin_off is True


# --------------------------------------------------------------------------- #
# Lapsed customers page <-> the worklist API, against the real backend
# --------------------------------------------------------------------------- #
EPISODES = "/api/v1/admin/reorder-reminders/episodes"
SUMMARY = "/api/v1/admin/reorder-reminders/summary"
UTC = timezone.utc


def _declared_key_set(path, name):
    """Extract `const <name> = new Set([...])` from a JS source, line comments stripped."""
    source = path.read_text(encoding="utf-8")
    match = re.search(rf"const {re.escape(name)} = new Set\(\[(.*?)\]\)", source, re.DOTALL)
    assert match, f"{name} not found in {path.name}"
    body = re.sub(r"//[^\n]*", "", match.group(1))
    return set(re.findall(r"'([^']*)'", body))


def _declared_list(path, name):
    """Extract `const <name> = [...]` from a JS source: its quoted strings, in order."""
    source = path.read_text(encoding="utf-8")
    match = re.search(rf"const {re.escape(name)} = \[(.*?)\];", source, re.DOTALL)
    assert match, f"{name} not found in {path.name}"
    return re.findall(r"'([^']*)'", re.sub(r"//[^\n]*", "", match.group(1)))


def _page_reads(pattern):
    return set(re.findall(pattern, LAPSED_PAGE.read_text(encoding="utf-8")))


def test_the_worklist_row_fixture_matches_the_services_row_keys():
    assert _declared_key_set(PAGE_TEST, "WORKLIST_ROW_KEYS") == set(ROW_KEYS)
    # Task 20a's later key: the page shows "already reordered" from it.
    assert "pending_close" in ROW_KEYS


def test_the_summary_settings_keys_are_the_seeded_ones(app, db):
    from business_app.services.reorder_reminders.worklist import ReorderReminderWorklist

    with app.app_context():
        published = [setting["key"] for setting in ReorderReminderWorklist.summary()["settings"]]

    assert tuple(published) == SETTINGS_KEYS


def test_the_guarded_route_is_the_path_the_digest_links_to(app, db, manager_user):
    from business_app.services.auth_service import AuthService
    from business_app.services.reorder_reminders.digest import WORKLIST_PATH

    routes = (ADMIN_UI / "App.js").read_text(encoding="utf-8")
    declared = re.search(r'<Route\s+path="(/lapsed-customers)"(.{0,400})', routes, re.DOTALL)

    assert declared, "App.js declares no /lapsed-customers route"
    assert declared.group(1) == WORKLIST_PATH
    permission = re.search(r'<PermissionGuard permission="(\w+)"', declared.group(2))
    assert permission, "the /lapsed-customers route is not behind a PermissionGuard"
    with app.app_context():
        assert AuthService().get_user_permissions(manager_user.id)[permission.group(1)] is True
    assert re.search(r"hasPermission\('%s'\)" % permission.group(1), LAPSED_PAGE.read_text(encoding="utf-8"))


def test_the_lapsed_page_reads_only_row_summary_and_metric_fields_the_backend_publishes(app, db):
    from business_app.services.reorder_reminders.worklist import ReorderReminderWorklist

    with app.app_context():
        summary = ReorderReminderWorklist.summary()

    row_fields = _page_reads(r"\b(?:row|resolving|turningOff)\??\.(\w+)")
    assert row_fields and row_fields <= set(ROW_KEYS), sorted(row_fields - set(ROW_KEYS))
    summary_fields = _page_reads(r"\bsummary\??\.(\w+)")
    assert summary_fields and summary_fields <= set(summary), sorted(summary_fields - set(summary))
    metric_fields = _page_reads(r"\bmetrics\.(\w+)")
    assert metric_fields <= set(summary["metrics_30d"]), sorted(metric_fields - set(summary["metrics_30d"]))
    assert _page_reads(r"\bmetrics\.reminders_sent\.(\w+)") == set(CHANNELS)
    assert _page_reads(r"\bentry\.(\w+)") == {"key", "value"}
    assert all(set(setting) == {"key", "value"} for setting in summary["settings"])


def test_the_nested_fields_the_page_reads_are_published_on_real_rows(client, db, frozen, manager_user, manager_claim_headers):
    """A rich real row set over HTTP: phones, a shared place with a member, a stuck order, history,
    a customer's own Stop, and the dormant outlets behind an escalation."""
    from business_app.services.reorder_reminders.events import record_event_at
    from business_app.services.reorder_reminders.settings import ReorderReminderSettingsService
    from shared.enums import OrderStatus
    from tests.integration.reorder_reminder_builders import (
        delivered_order,
        link,
        live_order,
        make_customer,
        place,
        staff_episode,
    )

    alisher = make_customer(db, first_name="Alisher", phone="+998901110001", language="ru-RU")
    akmal = make_customer(db, first_name="Akmal", phone="+998937771122", blocked_at=datetime(2026, 10, 1, 8, 0, tzinfo=UTC))
    link(db, alisher, akmal)
    bobur = make_customer(db, first_name="Bobur", phone="+998901110003")
    office = place(db, alisher, akmal, bobur, label="Office 12")
    anchor = delivered_order(db, alisher, datetime(2026, 8, 1, 7, 0, tzinfo=UTC), address=office[alisher.id])
    delivered_order(db, bobur, datetime(2026, 7, 20, 7, 0, tzinfo=UTC), address=office[bobur.id])
    ReorderReminderSettingsService.set(
        alisher.id, False, actor_kind="customer", via="telegram_button", now=datetime(2026, 10, 10, 6, 0, tzinfo=UTC)
    )
    long_lapsed = staff_episode(
        db, alisher, anchor=anchor, status="escalated", escalation_flag="long_lapsed", due_on=date(2026, 9, 1)
    )
    record_event_at(datetime(2026, 10, 14, 5, 2, tzinfo=UTC), user_id=alisher.id, kind="escalated", episode=long_lapsed, detail="long_lapsed")
    record_event_at(datetime(2026, 10, 12, 5, 0, tzinfo=UTC), user_id=alisher.id, kind="reminder_sent", episode=long_lapsed, step=1, channel="telegram")

    dilshod = make_customer(db, first_name="Dilshod")
    staff_episode(
        db, dilshod, anchor=delivered_order(db, dilshod, datetime(2026, 8, 20, 7, 0, tzinfo=UTC)),
        status="escalated", escalation_flag="stuck_order", due_on=date(2026, 9, 20), usual_gap_days=14,
    )
    live_order(db, dilshod, datetime(2026, 9, 20, 7, 0, tzinfo=UTC), status=OrderStatus.CONFIRMED)

    shop = make_customer(db, first_name="Farrux")
    shop_row = staff_episode(
        db, shop, anchor=delivered_order(db, shop, datetime(2026, 8, 20, 7, 0, tzinfo=UTC)),
        status="escalated", escalation_flag="outlet_dormant", due_on=date(2026, 9, 20),
    )
    record_event_at(
        datetime(2026, 10, 14, 5, 0, tzinfo=UTC), user_id=shop.id, kind="escalated", episode=shop_row, detail="outlet_dormant",
        payload={"outlets": [{"outlet_id": 7, "name": "Kiosk", "dormant_since": "2026-09-01T06:00:00+00:00", "agent_user_id": manager_user.id}]},
    )
    db.session.commit()

    response = client.get(EPISODES, query_string={"tab": "follow_up", "page": 1, "per_page": 20}, headers=manager_claim_headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    rows = response.get_json()["data"]["items"]
    assert len(rows) == 3
    assert all(set(row) == set(ROW_KEYS) for row in rows)
    assert all(isinstance(row["pending_close"], bool) for row in rows)
    # The two values of the published rhythm source, and the one the page tests for.
    assert {row["gap_source"] for row in rows} == {"personal", "preset"}
    assert "row.gap_source === 'personal'" in LAPSED_PAGE.read_text(encoding="utf-8")

    records = {
        "phone": [phone for row in rows for phone in row["phones"]],
        "place": [shared for row in rows for shared in row["places"]],
        "member": [member for row in rows for shared in row["places"] for member in shared["members"]],
        "outlet": [outlet for row in rows for outlet in row["outlets"] or []],
        "event": [event for row in rows for event in row["history"]],
        "order": [order for row in rows for order in row["stuck_orders"]],
        "restriction": [row["contact_restriction"] for row in rows if row["contact_restriction"]],
        "recipient": [row["recipient"] for row in rows],
        "setting": [row["setting"] for row in rows],
    }
    for variable, published in records.items():
        assert published, f"the fixture published no {variable} record to check the page against"
        read = _page_reads(rf"\b{variable}\??\.(\w+)")
        assert read, f"the page reads nothing off {variable}: the pattern drifted"
        union = set().union(*(set(record) for record in published))
        assert read <= union, f"the page reads {sorted(read - union)} off {variable}, which the backend does not publish"
    # `row.recipient` / `turningOff.recipient` is the nested record the page names the customer from;
    # its language is the RESOLVED reminder language (Alisher stores "ru-RU"), never the raw value.
    assert any(record["reminder_language"] == "ru" for record in records["recipient"])


@pytest.mark.parametrize("tab", sorted(TABS))
def test_every_tab_and_every_flag_chip_the_page_offers_is_accepted(client, db, frozen, manager_claim_headers, tab):
    """The params `adminService.listReorderReminderEpisodes` sends: tab, page, per_page, then flag and
    search only when set. The tabs and the chips are the summary's published vocabularies."""
    summary = client.get(SUMMARY, headers=manager_claim_headers).get_json()["data"]
    assert tab in summary["tabs"]
    assert tuple(summary["flags"]) == ESCALATION_FLAGS

    plain = client.get(EPISODES, query_string={"tab": tab, "page": 1, "per_page": 20}, headers=manager_claim_headers)
    assert plain.status_code == 200, plain.get_data(as_text=True)
    for flag in summary["flags"]:
        chip = client.get(
            EPISODES, query_string={"tab": tab, "page": 1, "per_page": 20, "flag": flag}, headers=manager_claim_headers
        )
        assert chip.status_code == 200, (tab, flag, chip.get_data(as_text=True))
    searched = client.get(
        EPISODES, query_string={"tab": tab, "page": 1, "per_page": 20, "search": "+998901110020"}, headers=manager_claim_headers
    )
    assert searched.status_code == 200, searched.get_data(as_text=True)


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_every_outcome_the_modal_offers_is_accepted_as_the_page_posts_it(
    client, db, frozen, manager_claim_headers, outcome
):
    """The modal lists `summary.outcomes`; the body is `{outcome}` plus a trimmed `note` only when typed."""
    from business_app.services.reorder_reminders.settings import ReorderReminderSettingsService
    from tests.integration.reorder_reminder_builders import delivered_order, make_customer, staff_episode

    customer = make_customer(db, first_name="Outcome")
    episode = staff_episode(
        db, customer, anchor=delivered_order(db, customer, datetime(2026, 8, 1, 7, 0, tzinfo=UTC)),
        status="escalated", escalation_flag="no_response", due_on=date(2026, 9, 1),
    )
    summary = client.get(SUMMARY, headers=manager_claim_headers).get_json()["data"]
    assert outcome in summary["outcomes"]

    bare = client.post(f"{EPISODES}/{episode.id}/resolve", json={"outcome": outcome}, headers=manager_claim_headers)

    assert bare.status_code == 200, bare.get_data(as_text=True)
    published = bare.get_json()["data"]["episode"]
    assert (published["status"], published["outcome"], published["outcome_note"]) == ("resolved", outcome, None)
    if outcome == "do_not_contact":
        assert ReorderReminderSettingsService.effective_for([customer.id]).admin_off is True
    else:
        assert ReorderReminderSettingsService.effective_for([customer.id]).admin_off is False


def test_a_note_the_page_trims_is_stored_as_sent_and_a_padded_one_is_accepted(client, db, frozen, manager_claim_headers):
    from tests.integration.reorder_reminder_builders import delivered_order, make_customer, staff_episode

    def escalated(name):
        customer = make_customer(db, first_name=name)
        return staff_episode(
            db, customer, anchor=delivered_order(db, customer, datetime(2026, 8, 1, 7, 0, tzinfo=UTC)),
            status="escalated", escalation_flag="no_response", due_on=date(2026, 9, 1),
        )

    typed, padded = escalated("Typed"), escalated("Padded")

    ok = client.post(f"{EPISODES}/{typed.id}/resolve", json={"outcome": "away", "note": "Asked not to be called"}, headers=manager_claim_headers)
    # The textarea carries no maxLength, so a note of exactly 2000 characters plus padding can reach the server.
    long_padded = client.post(
        f"{EPISODES}/{padded.id}/resolve", json={"outcome": "other", "note": "  " + "x" * 2000 + "  "}, headers=manager_claim_headers
    )

    assert ok.get_json()["data"]["episode"]["outcome_note"] == "Asked not to be called"
    assert long_padded.status_code == 200, long_padded.get_data(as_text=True)
    assert long_padded.get_json()["data"]["episode"]["outcome_note"] == "x" * 2000


def test_the_refusal_the_page_explains_is_the_one_a_second_resolve_gets(client, db, frozen, manager_claim_headers):
    """The page's `handledErrorCodes` silence api.js's toast for exactly this code, which `apiErrorCode` reads off the body."""
    from business_app.services.reorder_reminders.actions import NOT_RESOLVABLE
    from tests.integration.reorder_reminder_builders import delivered_order, make_customer, staff_episode

    customer = make_customer(db, first_name="Twice")
    episode = staff_episode(
        db, customer, anchor=delivered_order(db, customer, datetime(2026, 8, 1, 7, 0, tzinfo=UTC)),
        status="escalated", escalation_flag="no_response", due_on=date(2026, 9, 1),
    )
    url = f"{EPISODES}/{episode.id}/resolve"

    first = client.post(url, json={"outcome": "away"}, headers=manager_claim_headers)
    second = client.post(url, json={"outcome": "moved"}, headers=manager_claim_headers)

    # The page names the code (the service only forwards what its caller names).
    assert _declared_list(LAPSED_PAGE, "RESOLVE_HANDLED_CODES") == [NOT_RESOLVABLE]
    assert "handledErrorCodes: RESOLVE_HANDLED_CODES" in LAPSED_PAGE.read_text(encoding="utf-8")
    assert "REORDER_REMINDER" not in (ADMIN_UI / "services" / "adminService.js").read_text(encoding="utf-8")
    assert first.status_code == 200
    body = second.get_json()
    assert (second.status_code, body["error_code"]) == (400, NOT_RESOLVABLE)


def test_turning_reminders_off_moves_a_reminding_row_to_paused_in_the_shape_the_page_refreshes(
    client, db, frozen, manager_claim_headers
):
    from tests.integration.reorder_reminder_builders import delivered_order, make_customer, staff_episode

    customer = make_customer(db, first_name="Feruza")
    episode = staff_episode(
        db, customer, anchor=delivered_order(db, customer, datetime(2026, 9, 1, 7, 0, tzinfo=UTC)),
        status="open", due_on=date(2026, 9, 23), step=1, reminders_sent=1, next_action_on=date(2026, 10, 20),
    )

    def tab(name):
        response = client.get(EPISODES, query_string={"tab": name, "page": 1, "per_page": 20}, headers=manager_claim_headers)
        return {row["episode_id"]: row for row in response.get_json()["data"]["items"]}

    before = tab("reminding")[episode.id]
    assert before["can_turn_off_reminders"] is True

    reason = "Customer moved to Samarkand"
    response = client.put(
        f"/api/v1/admin/users/{customer.id}/notification-settings",
        json={"reorder_reminders_enabled": False, "reason": reason},
        headers=manager_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert episode.id not in tab("reminding")
    after = tab("paused")[episode.id]
    assert (after["suspended_reason"], after["can_turn_off_reminders"], after["can_resolve"]) == ("admin_off", False, False)
    assert (after["setting"]["enabled"], after["setting"]["changed_by_kind"], after["setting"]["reason"]) == (False, "admin", reason)


def test_the_turn_off_payload_names_the_field_the_users_switch_sends():
    """One field name, two pages: the Lapsed page's turn-off writes what the Users switch writes."""
    page = LAPSED_PAGE.read_text(encoding="utf-8")
    assert re.search(rf"\b{_users_constant('REORDER_REMINDERS_FIELD')}: false,\s*reason: text", page)


def test_the_blocked_banner_is_keyed_on_the_value_the_backend_publishes():
    from business_app.services.reorder_reminders.eligibility import REASON_BLOCKED, REASON_STOPPED

    source = LAPSED_PAGE.read_text(encoding="utf-8")
    assert f"restriction.kind === '{REASON_BLOCKED}'" in source
    assert REASON_STOPPED != REASON_BLOCKED


def test_the_page_shows_the_reminder_language_the_backend_publishes_and_derives_none():
    """Final review B1/E1: the language a reminder is written in has ONE rule, in the backend
    (`shared.i18n_rendering.reminder_language`), published on the row as `recipient.reminder_language`.
    The page prints that field; the JS copy of the rule (utils/reminderLanguage.js) is gone."""
    page = LAPSED_PAGE.read_text(encoding="utf-8")

    assert "row.recipient?.reminder_language" in page
    assert not (ADMIN_UI / "utils" / "reminderLanguage.js").exists()
    assert not (ADMIN_UI / "__tests__" / "utils" / "reminderLanguage.test.js").exists()
    sources = [path.read_text(encoding="utf-8") for path in (ADMIN_UI).rglob("*.js")]
    assert not any("resolveReminderLanguage" in source or "DEFAULT_REMINDER_LANGUAGE" in source for source in sources)


# --------------------------------------------------------------------------- #
# Analytics overview <-> the summary and the inactive-customers endpoints (Task 24b)
# --------------------------------------------------------------------------- #
INACTIVE = "/api/v1/admin/analytics/inactive-customers"


def _flat_object_keys(path, name):
    """The keys of the flat `const <name> = { ... };` literal in a JS source (string values blanked first)."""
    source = path.read_text(encoding="utf-8")
    match = re.search(rf"const {re.escape(name)} = \{{(.*?)\}};", source, re.DOTALL)
    assert match, f"{name} not found in {path.name}"
    return set(re.findall(r"\b(\w+):", re.sub(r"'[^']*'", "''", match.group(1))))


def _block(source, start, end):
    """The text from the line starting `start` up to the line starting `end`."""
    begin = source.index(start)
    return source[begin : source.index(end, begin)]


def test_the_cards_read_exactly_the_counts_the_summary_endpoint_publishes(client, db, frozen, manager_claim_headers):
    """The Vitest fixture's `counts`, the keys Analytics.js reads and the real HTTP summary are one set,
    and the card shows the number the endpoint answers (D17: no figure of its own)."""
    from business_app.services.reorder_reminders.worklist import COUNTED_TABS
    from tests.integration.reorder_reminder_builders import delivered_order, make_customer, make_episode, staff_episode

    follow_up = make_customer(db, first_name="Follow")
    staff_episode(
        db, follow_up, anchor=delivered_order(db, follow_up, datetime(2026, 8, 1, 7, 0, tzinfo=UTC)),
        status="escalated", escalation_flag="no_response", due_on=date(2026, 9, 1),
    )
    for name in ("Remind", "Reminded"):
        customer = make_customer(db, first_name=name)
        make_episode(db, customer, anchor=delivered_order(db, customer, datetime(2026, 9, 20, 7, 0, tzinfo=UTC)))

    response = client.get(SUMMARY, headers=manager_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    counts = response.get_json()["data"]["counts"]
    assert counts == {"follow_up": 1, "reminding": 2}
    page = ANALYTICS_PAGE.read_text(encoding="utf-8")
    assert set(re.findall(r"\blapsedCounts\.(\w+)", page)) == set(counts) == set(COUNTED_TABS)
    fixture_counts = re.findall(r"summaryWith\(\{([^}]*)\}\)", ANALYTICS_CARDS_TEST.read_text(encoding="utf-8"))
    assert fixture_counts and all(set(re.findall(r"\b(\w+):", body)) == set(counts) for body in fixture_counts)


def test_the_cards_the_page_and_the_nav_badge_read_one_summary_entry_and_only_the_badge_polls():
    for source in (ANALYTICS_PAGE, LAPSED_PAGE, LAYOUT):
        text = source.read_text(encoding="utf-8")
        assert "queryKey: LAPSED_CUSTOMERS_SUMMARY_KEY" in text, source.name
        assert "adminService.getReorderReminderSummary()" in text, source.name
    assert "refetchInterval" not in ANALYTICS_PAGE.read_text(encoding="utf-8")
    assert "refetchInterval: 60_000" in LAYOUT.read_text(encoding="utf-8")


def test_the_cards_link_to_the_guarded_route_and_show_to_the_permission_that_guards_it():
    from business_app.services.reorder_reminders.digest import WORKLIST_PATH

    routes = (ADMIN_UI / "App.js").read_text(encoding="utf-8")
    declared = re.search(r'<Route\s+path="(/lapsed-customers)"(.{0,400})', routes, re.DOTALL)
    assert declared, "App.js declares no /lapsed-customers route"
    guard = re.search(r'<PermissionGuard permission="(\w+)"', declared.group(2))
    assert guard, "the /lapsed-customers route is not behind a PermissionGuard"

    page = ANALYTICS_PAGE.read_text(encoding="utf-8")
    assert re.findall(r'<Link to="([^"]+)"', page) == [WORKLIST_PATH, WORKLIST_PATH]
    assert declared.group(1) == WORKLIST_PATH
    assert re.findall(r"hasPermission\('(\w+)'\)", page) == [guard.group(1)]


@pytest.fixture
def inactive_customers(db, frozen):
    """One lapsed customer of every type the tab can filter to, delivered and never-delivered."""
    from shared.enums import EntitySubtype, UserType
    from tests.integration.reorder_reminder_builders import delivered_order, make_customer

    people = [
        make_customer(db, first_name="Dilshod"),
        make_customer(db, first_name="Madina", created_at=datetime(2026, 9, 1, 7, 0, tzinfo=UTC)),
        make_customer(
            db, first_name="Office", user_type=UserType.ENTITY, entity_subtype=EntitySubtype.WORKPLACE, company_name="Office LLC"
        ),
        make_customer(
            db, first_name="Shop", user_type=UserType.ENTITY, entity_subtype=EntitySubtype.GROCERY_STORE, company_name="Shop LLC"
        ),
    ]
    for person, delivered_on in ((people[0], 25), (people[2], 22), (people[3], 20)):
        delivered_order(db, person, datetime(2026, 8, delivered_on, 7, 0, tzinfo=UTC))
    return people


def _inactive_tab_options():
    """The choices the Inactive customers tab offers a human, read out of Analytics.js."""
    tab = _block(ANALYTICS_PAGE.read_text(encoding="utf-8"), "key: 'inactive',", "key: 'agent_performance',")
    types = re.findall(r"\{ value: '(\w+)', label: t\('ui\.analytics\.type_", tab)
    threshold = re.search(r"<InputNumber\s+min=\{(\d+)\}\s+max=\{(\d+)\}", tab)
    assert types and threshold, "the Inactive tab's controls moved: re-derive the options"
    return types, tuple(int(bound) for bound in threshold.groups())


def test_every_customer_type_and_never_ordered_choice_the_tab_offers_is_accepted_with_the_rows_it_reads(
    client, inactive_customers, manager_claim_headers
):
    published = _flat_object_keys(ANALYTICS_CARDS_TEST, "DELIVERED")
    assert published == _flat_object_keys(ANALYTICS_CARDS_TEST, "NEVER_DELIVERED")
    types, _ = _inactive_tab_options()
    assert set(types) == {"all", "individual", "workplace", "grocery"}

    for customer_type in types:
        for include_never in ("true", "false"):
            # The query `adminService.getInactiveCustomers` sends: days_since, customer_type, include_never_ordered, page, per_page.
            response = client.get(
                INACTIVE,
                query_string={
                    "days_since": 30, "customer_type": customer_type,
                    "include_never_ordered": include_never, "page": 1, "per_page": 50,
                },
                headers=manager_claim_headers,
            )
            assert response.status_code == 200, (customer_type, include_never, response.get_data(as_text=True))
            body = response.get_json()
            assert isinstance(body["meta"]["total"], int), body["meta"]
            assert body["data"]["items"], (customer_type, include_never)
            for item in body["data"]["items"]:
                assert set(item) == published, (customer_type, include_never)


def test_the_threshold_bounds_and_the_largest_page_the_tab_offers_are_accepted(client, inactive_customers, manager_claim_headers):
    _, (low, high) = _inactive_tab_options()
    assert (low, high) == (0, 3650)
    for query in ({"days_since": low}, {"days_since": high}, {"days_since": 30, "per_page": 100}):
        response = client.get(INACTIVE, query_string={"page": 1, "per_page": 50, **query}, headers=manager_claim_headers)
        assert response.status_code == 200, (query, response.get_data(as_text=True))


def test_the_inactive_columns_and_the_export_read_only_fields_the_endpoint_publishes():
    page = ANALYTICS_PAGE.read_text(encoding="utf-8")
    published = _flat_object_keys(ANALYTICS_CARDS_TEST, "DELIVERED")
    columns = _block(page, "const inactiveColumns = [", "const deliveryPerformanceColumns = [")
    export = _block(page, "if (activeTab === 'inactive') {", "Metric: 'Total Revenue'")

    read_by_columns = set(re.findall(r"dataIndex: '(\w+)'", columns)) | set(re.findall(r"\br\.(\w+)", columns))
    read_by_export = set(re.findall(r"\bc\.(\w+)", export))
    assert {"last_order_date", "days_since_last_order", "never_ordered"} <= read_by_columns
    assert read_by_columns <= published, sorted(read_by_columns - published)
    assert read_by_export and read_by_export <= published, sorted(read_by_export - published)
