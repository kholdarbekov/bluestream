"""Static regressions for backend translation seed coverage."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SEED_SCRIPT = ROOT / "scripts" / "seed_backend_translations.py"


def test_seed_script_includes_loyalty_admin_navigation_and_page_keys():
    text = SEED_SCRIPT.read_text(encoding="utf-8")

    assert "'ui.nav.loyalty_members': {" in text
    assert "'ui.nav.loyalty_programs': {" in text
    assert "'ui.nav.loyalty_rewards': {" in text
    assert "'ui.loyalty.export_members': {" in text
    assert "'ui.loyalty.reward_details': {" in text
    assert "'ui.loyalty.search_rewards': {" in text
    assert "'ui.loyalty.tier_create_success': {" in text


def test_seed_script_includes_loyalty_analytics_keys():
    text = SEED_SCRIPT.read_text(encoding="utf-8")

    assert "'ui.analytics.loyalty': {" in text
    assert "'ui.analytics.loyalty_points_trend': {" in text
    assert "'ui.analytics.total_loyalty_members': {" in text
    assert "'ui.analytics.points_in_circulation': {" in text
    assert "'ui.analytics.top_rewards': {" in text


def test_seed_script_includes_orders_and_products_fiscalization_ui_catalogs():
    text = SEED_SCRIPT.read_text(encoding="utf-8")

    assert "ADMIN_UI_ORDER_TRANSLATIONS = {" in text
    assert "ADMIN_UI_PRODUCT_TRANSLATIONS = {" in text
    assert "'ui.orders.fiscalization': _ui_tr(" in text
    assert "'ui.orders.record_personal_card_payment': _ui_tr(" in text
    assert "'ui.orders.retry_fiscalization': _ui_tr(" in text
    assert "'ui.products.fiscal_profile': _ui_tr(" in text
    assert "'ui.products.marking_codes': _ui_tr(" in text
    assert "'ui.products.marking_code_import_issues': _ui_tr(" in text
    assert "'ui.products.marking_code_status_available': _ui_tr(" in text
    assert "'ui.products.marking_code_status_reserved': _ui_tr(" in text
    assert "'ui.products.marking_code_status_used': _ui_tr(" in text
    assert "'ui.products.marking_code_status_archived': _ui_tr(" in text


def test_seed_script_includes_marking_code_utilisation_filter_keys():
    text = SEED_SCRIPT.read_text(encoding="utf-8")

    assert "'ui.products.marking_code_status_available_unutilised': _ui_tr(" in text
    assert "'ui.products.marking_code_status_available_pre_utilised': _ui_tr(" in text


def test_seed_script_includes_order_money_breakdown_keys():
    """Orders.js passes an English fallback, so a missing row degrades silently
    to English for a ru/uz operator instead of failing loudly. Pin the rows."""
    text = SEED_SCRIPT.read_text(encoding="utf-8")

    assert "'ui.orders.money_breakdown': _ui_tr(" in text
    assert "'ui.orders.subtotal': _ui_tr(" in text
    assert "'ui.orders.subscription_discount': _ui_tr(" in text
    assert "'ui.orders.loyalty_discount': _ui_tr(" in text
    assert "'ui.orders.tier_discount': _ui_tr(" in text
    assert "'ui.orders.delivery_fee': _ui_tr(" in text


def test_seed_script_includes_the_tier_discount_condition_key():
    """GET /loyalty/tiers renders this key. get_translation returns the KEY
    itself when the row is missing, so an unseeded deploy publishes the literal
    string 'api.loyalty.tier_discount_condition' to every /my-loyalty visitor."""
    text = SEED_SCRIPT.read_text(encoding="utf-8")

    assert "'api.loyalty.tier_discount_condition': {" in text


def test_seed_script_includes_the_sales_stock_check_product_keys():
    """The product switch, its table tag and the help text under the switch are
    the flag's only human-readable surface. An unseeded key renders as the raw
    dotted string in all three languages, so the admin sees
    `ui.products.in_sales_stock_check` next to a toggle that decides what the
    sales agent counts — and, for the help text, loses the only statement that
    membership of the agent's list is `flag AND active`."""
    text = SEED_SCRIPT.read_text(encoding="utf-8")

    assert "'ui.products.in_sales_stock_check': _ui_tr(" in text
    assert "'ui.products.sales_stock_check_tag': _ui_tr(" in text
    assert "'ui.products.sales_stock_check_help': _ui_tr(" in text


def test_seed_script_includes_the_sales_nav_group_keys():
    """The `/sales` group's own labels (D24).

    `ui.nav.outlets` and `ui.nav.sales_agents` moved under a parent that has no seed of its own,
    and the group gained a third child. An unseeded key renders as the literal `ui.nav.sales` in
    every non-English sidebar: i18next only falls back to the English default the call site
    passed, which is exactly the language an admin working in uz/ru does not want."""
    text = SEED_SCRIPT.read_text(encoding="utf-8")

    assert "'ui.nav.sales': {" in text
    assert "'ui.nav.visits': {" in text


def test_every_delivery_status_has_an_admin_ui_label():
    """The Delivery page renders `ui.delivery.status_<value>` for every status: tag,
    filter option and update dropdown. An unseeded row shows the English fallback to a
    uz/ru admin, which is what `rescheduled` would have done."""
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
    from shared.enums import DeliveryStatus

    missing = [
        status.value for status in DeliveryStatus if f"ui.delivery.status_{status.value}" not in BACKEND_TRANSLATIONS
    ]
    assert missing == []
    assert BACKEND_TRANSLATIONS["ui.delivery.status_rescheduled"] == {
        "en": "Rescheduled",
        "uz": "Ko'chirildi",
        "ru": "Перенесено",
    }


def _assert_seeded_as_the_delivery_page_writes_it(expected):
    """Each key is seeded trilingually, rendered by `Delivery.js`, and falls back there to
    the seeded English. Two different English sentences would be two copies of one
    message, and an unseeded key shows that English to a uz/ru admin."""
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

    page = (ROOT / "admin_ui" / "src" / "pages" / "Delivery.js").read_text(encoding="utf-8")
    for key, row in expected.items():
        assert BACKEND_TRANSLATIONS[key] == row, key
        assert f"'{key}'" in page, f"{key} is not rendered by admin_ui/src/pages/Delivery.js"
        assert row["en"] in page, f"{key}: Delivery.js falls back to different English"


def test_the_delivery_page_reschedule_and_return_copy_is_seeded_as_the_page_writes_it():
    """F13's Reschedule row action, and F6's confirmed return with its internal reason, as the
    Delivery page renders them."""
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

    expected = {
        "ui.delivery.reschedule": {"en": "Reschedule", "uz": "Ko'chirish", "ru": "Перенести"},
        "ui.delivery.return_confirm_title": {
            "en": "Mark this delivery as returned?",
            "uz": "Yetkazib berish qaytarilgan deb belgilansinmi?",
            "ru": "Отметить доставку как возвращённую?",
        },
        "ui.delivery.return_confirm_message": {
            "en": "Order {{order}} will be closed as returned and its driver released.",
            "uz": "{{order}} buyurtmasi qaytarilgan sifatida yopiladi va haydovchisi bo'shatiladi.",
            "ru": "Заказ {{order}} будет закрыт как возвращённый, а водитель освобождён.",
        },
        "ui.delivery.return_confirm_ok": {
            "en": "Mark as returned",
            "uz": "Qaytarilgan deb belgilash",
            "ru": "Отметить как возвращённый",
        },
        "ui.delivery.return_reason_label": {
            "en": "Reason (internal, not shown to the customer)",
            "uz": "Sabab (ichki, mijozga ko'rsatilmaydi)",
            "ru": "Причина (внутренняя, клиенту не показывается)",
        },
        "ui.delivery.return_reason_required": {
            "en": "Reason is required",
            "uz": "Sabab kerak",
            "ru": "Причина обязательна",
        },
    }

    _assert_seeded_as_the_delivery_page_writes_it(expected)
    # The page fills {{order}}. A translation that drops the slot would not say which order closes.
    assert all("{{order}}" in text for text in BACKEND_TRANSLATIONS["ui.delivery.return_confirm_message"].values())


@pytest.mark.parametrize(
    "delivery_key, orders_key",
    [
        ("ui.delivery.reschedule", "ui.orders.reschedule"),
        ("ui.delivery.return_confirm_ok", "ui.orders.mark_returned"),
        ("ui.delivery.return_reason_label", "ui.orders.reason_internal_label"),
        ("ui.delivery.return_reason_required", "ui.orders.reason_required"),
    ],
)
def test_the_delivery_page_names_each_shared_action_as_the_orders_page_does(delivery_key, orders_key):
    """The Delivery page's Reschedule, its return button and the internal-reason field are the Orders
    page's own actions and field (F13, F6). Spec §7 keeps the page's copy under `ui.delivery.*`, so
    each of these rows has an Orders twin, and a twin reworded on one side only would make one
    action read two ways. Reword both or neither."""
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

    assert BACKEND_TRANSLATIONS[delivery_key] == BACKEND_TRANSLATIONS[orders_key]


def test_the_retired_redispatch_copy_is_neither_seeded_nor_rendered():
    """F13 deleted the Delivery page's today-only Re-dispatch. Its copy went with it, so a stale
    "returns the failed delivery to the pool" sentence can never reach an admin again."""
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

    page = (ROOT / "admin_ui" / "src" / "pages" / "Delivery.js").read_text(encoding="utf-8")
    assert sorted(key for key in BACKEND_TRANSLATIONS if key.startswith("ui.delivery.redispatch")) == []
    assert "ui.delivery.redispatch" not in page


def test_the_delivery_page_notes_action_is_seeded_as_the_page_writes_it():
    """A row with no status move (failed, delivered, cancelled, returned, held) still opens
    the update form, for its notes. With no status to pick, the action is labelled as a
    notes edit rather than "Update Status"."""
    _assert_seeded_as_the_delivery_page_writes_it(
        {
            "ui.delivery.edit_notes": {
                "en": "Edit notes",
                "uz": "Izohni tahrirlash",
                "ru": "Изменить заметки",
            },
        }
    )


def test_the_dispatch_map_says_the_orders_tag_sentence_from_the_same_row():
    """F8: the popup on a "needs a new date" pin says why, in the Orders tag's own sentence and
    from its own row, `ui.orders.delivery_failed_needs_new_date`. A `ui.dispatch.*` twin would
    be a second row to check and correct, and the tag and the popup would drift apart. The map
    looks keys up in its `delivery:` namespace, and a `ui` row reaches that lookup through
    i18n.js `fallbackNS: ['common']`.

    The Orders page's guard (tests/unit/test_order_awaiting_new_date_ui_translations.py) reads
    a fallback only where a quote sits directly before `ui.`, so it cannot see this namespaced
    call. The map's fallback must be the seeded English byte for byte, or an unseeded and a
    seeded database show two different sentences."""
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

    english = BACKEND_TRANSLATIONS["ui.orders.delivery_failed_needs_new_date"]["en"]
    source = (ROOT / "admin_ui" / "src" / "components" / "OperationsMap.jsx").read_text(encoding="utf-8")
    assert f"t('delivery:ui.orders.delivery_failed_needs_new_date', '{english}')" in source
