# scripts/seed_customer_link_translations.py
"""Seed trilingual strings for the multi-phone customer-linking admin UI
(ui.users.linked_accounts.* + ui.users.primary / no_linked_accounts).

category='ui' — same mechanism as scripts/seed_customer_map_translations.py:
AdminUiTranslationService resolves dotted 'ui.users.*' keys into the 'users'
i18next namespace via LEGACY_NAMESPACE_PREFIXES, which requires category == 'ui'.
(ui.common.cancel / ui.common.confirm already exist and are NOT re-seeded here.)

Run inside the business_app container (scripts/ is not mounted, so pipe it in):
    docker compose exec -T business_app python - < scripts/seed_customer_link_translations.py
"""
from business_app import create_app
from business_app.models.translation import Translation

CATEGORY = "ui"

# key: (en, uz, ru)
KEYS = {
    "ui.users.linked_accounts":                        ("Linked accounts", "Bog'langan hisoblar", "Связанные аккаунты"),
    "ui.users.no_linked_accounts":                     ("Not linked to any other account", "Boshqa hisobga bog'lanmagan", "Не связан с другими аккаунтами"),
    "ui.users.primary":                                ("primary", "asosiy", "основной"),
    "ui.users.linked_accounts.unlink":                 ("Unlink", "Ajratish", "Отвязать"),
    "ui.users.linked_accounts.unlink_success":         ("Account unlinked", "Hisob ajratildi", "Аккаунт отвязан"),
    "ui.users.linked_accounts.link":                   ("Link", "Bog'lash", "Связать"),
    "ui.users.linked_accounts.link_title":             ("Link this account?", "Ushbu hisobni bog'laysizmi?", "Связать этот аккаунт?"),
    "ui.users.linked_accounts.link_success":           ("Accounts linked", "Hisoblar bog'landi", "Аккаунты связаны"),
    "ui.users.linked_accounts.suggestions_title":      ("Possible same customer", "Ehtimoliy bir xil mijoz", "Возможно тот же клиент"),
    "ui.users.linked_accounts.no_suggestions":         ("No suggestions", "Takliflar yo'q", "Нет предложений"),
    "ui.users.linked_accounts.not_same_person":        ("Not the same person", "Boshqa shaxs", "Другой человек"),
    "ui.users.linked_accounts.dismiss_title":          ("Mark as a different person?", "Boshqa shaxs deb belgilaysizmi?", "Отметить как другого человека?"),
    "ui.users.linked_accounts.dismiss_success":        ("Marked as different customers", "Har xil mijoz sifatida belgilandi", "Отмечены как разные клиенты"),
    "ui.users.linked_accounts.manual_title":           ("Link another account", "Boshqa hisobni bog'lash", "Связать другой аккаунт"),
    "ui.users.linked_accounts.find":                   ("Find", "Qidirish", "Найти"),
    "ui.users.linked_accounts.search_placeholder":     ("Search by phone or name", "Telefon yoki ism bo'yicha qidiring", "Поиск по телефону или имени"),
    "ui.users.linked_accounts.search_failed":          ("Search failed", "Qidiruv muvaffaqiyatsiz", "Ошибка поиска"),
    # NOTE: eight address-GROUPING keys (same_place_title, same_place_hint,
    # no_addresses, mark_same_place, group_title, group_label_placeholder,
    # group_success, grouped_tag) were dropped in Phase 2c. Grouping is the
    # WHERE axis and moved out of LinkedAccountsPanel (the WHO axis) into
    # components/PlaceGroupPanel.jsx, whose keys are seeded by
    # scripts/seed_place_group_ui_translations.py as ui.users.place_groups.*.
    # Rows already written by an earlier run of this script are harmless
    # orphans; no component reads them.
    "ui.users.linked_accounts.reason_placeholder":     ("Reason (required)", "Sabab (majburiy)", "Причина (обязательно)"),
    "ui.users.linked_accounts.action_failed":          ("Action failed", "Amal bajarilmadi", "Не удалось выполнить действие"),
}


def run():
    app = create_app()
    with app.app_context():
        data = {"en": {}, "uz": {}, "ru": {}}
        for key, (en, uz, ru) in KEYS.items():
            data["en"][key] = en
            data["uz"][key] = uz
            data["ru"][key] = ru
        Translation.bulk_create_or_update(data, category=CATEGORY)
        print(f"Seeded {len(KEYS)} keys x3 langs into category '{CATEGORY}'.")


if __name__ == "__main__":
    run()
