# scripts/seed_customer_map_translations.py
"""Seed trilingual strings for the admin customer map (ui.users.map.*).

category='ui' matches the dominant/active category for existing ui.users.*
keys (122 distinct keys, most recently updated of the three categories found
in the DB: 'ui', 'ui_users', 'ui.users'). AdminUiTranslationService resolves
dotted 'ui.users.*' keys into the 'users' i18next namespace via its
LEGACY_NAMESPACE_PREFIXES mechanism, which requires category == 'ui'.

Run inside the business_app container (scripts/ is not mounted, so pipe it in):
    docker compose exec -T business_app python - < scripts/seed_customer_map_translations.py
"""
from business_app import create_app, db
from business_app.models.translation import Translation

CATEGORY = "ui"

KEYS = {
    "ui.users.map.tab_list":        ("List", "Ro'yxat", "Список"),
    "ui.users.map.tab_map":         ("Map", "Xarita", "Карта"),
    "ui.users.map.view_pins":       ("Pins", "Nuqtalar", "Точки"),
    "ui.users.map.view_heat":       ("Heatmap", "Issiqlik xaritasi", "Тепловая карта"),
    "ui.users.map.heat_overlay":    ("Heat overlay", "Issiqlik qatlami", "Тепловой слой"),
    "ui.users.map.fresh_within":    ("Fresh ≤", "Yangi ≤", "Свежий ≤"),
    "ui.users.map.idle_after":      ("Idle ≥", "Nofaol ≥", "Неактивный ≥"),
    "ui.users.map.days":            ("days", "kun", "дней"),
    "ui.users.map.idle_min":        ("Idle ≥ (filter)", "Nofaol ≥ (filtr)", "Неактивен ≥ (фильтр)"),
    "ui.users.map.filter_bottles":  ("Has bottles", "Idishlari bor", "Есть тара"),
    "ui.users.map.filter_debt":     ("Has debt", "Qarzi bor", "Есть долг"),
    "ui.users.map.type_all":        ("All", "Hammasi", "Все"),
    "ui.users.map.type_individual": ("Individual", "Jismoniy shaxs", "Физлицо"),
    "ui.users.map.type_entity":     ("Entity", "Tashkilot", "Организация"),
    "ui.users.map.legend_recent":   ("Recent", "Yaqinda", "Недавно"),
    "ui.users.map.legend_idle":     ("Idle", "Nofaol", "Неактивен"),
    "ui.users.map.showing":         ("Showing", "Ko'rsatilmoqda", "Показано"),
    "ui.users.map.empty":           ("No customers to display", "Mijozlar yo'q", "Нет клиентов"),
    "ui.users.map.last_order":      ("Last order", "Oxirgi buyurtma", "Последний заказ"),
    "ui.users.map.days_ago":        ("days ago", "kun oldin", "дней назад"),
    "ui.users.map.bottles":         ("Bottles", "Idishlar", "Тара"),
    "ui.users.map.address":         ("address", "manzil", "адрес"),
    "ui.users.map.debt":            ("Debt", "Qarz", "Долг"),
    "ui.users.map.cod_restricted":  ("COD restricted", "Naqd cheklangan", "Наличные ограничены"),
    "ui.users.map.view_profile":    ("View full profile", "To'liq profil", "Полный профиль"),
    "ui.users.map.orders":          ("orders", "buyurtma", "заказов"),
    "ui.users.map.load_profile_failed": ("Could not load customer profile", "Profil yuklanmadi", "Не удалось загрузить профиль"),
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
        print(f"Seeded {len(KEYS)} keys x3 langs into '{CATEGORY}'.")

if __name__ == "__main__":
    run()
