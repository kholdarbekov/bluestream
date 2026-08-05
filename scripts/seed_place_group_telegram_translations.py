# scripts/seed_place_group_telegram_translations.py
"""Seed the Phase-2c place-group / linked-account CUSTOMER-BOT keys.

category='telegram' — the same mechanism and category as
scripts/seed_bottle_ledger_translations.py; telegram_bot/i18n.py loads every
row with this category into its per-process cache at boot.

Why this seed is deploy-critical rather than cosmetic: a missing key does NOT
render the key. telegram_bot/i18n.py:80-92 humanises the last segment
("Member line", "Place total") and then feeds that literal to `.format()`,
which silently drops every kwarg — so a linked customer would see a label with
no name, no address and no number. It fails soft and looks broken.

Run inside the business_app container (scripts/ is NOT volume-mounted, so pipe
the file in over stdin — piped stdin still executes as __main__):
    docker compose exec -T business_app python - < scripts/seed_place_group_telegram_translations.py
Then restart the bot (the translation cache is per-process):
    docker compose restart telegram_bot
"""

from business_app import create_app
from business_app.models.translation import Translation

CATEGORY = "telegram"

# {key: {language: value}} — the per-key trilingual shape used by
# scripts/seed_bottle_ledger_translations.py. Double-quoted values throughout so
# Uzbek/English apostrophes need no escaping.
KEYS = {
    # ---- /bottles screen (telegram_bot/handlers/bottles.py) -----------------
    # Rendered with parse_mode='HTML'; the handler escapes every interpolated
    # fragment, so these templates must not add markup of their own.
    # A place has ONE pool of empties (no per-member slice), so this line is the
    # grouped row's headline number — the row above it deliberately prints no
    # number of its own (decision D6). Do not re-word it back into a qualifier.
    "telegram.bottles.place_total": {
        "en": "🏢 Bottles at this place (all members): {total}",
        "uz": "🏢 Ushbu joydagi idishlar (barcha a'zolar): {total}",
        "ru": "🏢 Бутыли в этом месте (все участники): {total}",
    },
    # NAME ONLY. The backend emits no per-member balance any more
    # (get_customer_bottle_overview -> place_members: [{member_name, is_own}]),
    # so the handler passes `name=` and nothing else. Re-adding a {balance}
    # placeholder here makes telegram_bot/i18n.py:88-93 swallow the KeyError and
    # send the customer the RAW TEMPLATE ("Alice Member: {balance}").
    "telegram.bottles.member_line": {
        "en": "👤 {name}",
        "uz": "👤 {name}",
        "ru": "👤 {name}",
    },
    # Client-computed sum of every distinct PLACE balance — not a per-account
    # sum: at a shared workplace the figure includes coworkers' empties.
    "telegram.bottles.cluster_total": {
        "en": "📦 Total across all your places (shared places include other members): {total}",
        "uz": (
            "📦 Barcha joylaringiz bo'yicha jami "
            "(umumiy joylarda boshqa a'zolar ham hisobga olinadi): {total}"
        ),
        "ru": (
            "📦 Всего по всем вашим местам "
            "(в общих местах учтены другие участники): {total}"
        ),
    },
    "telegram.bottles.linked_account_line": {
        "en": "{address} (account: {owner})",
        "uz": "{address} (hisob: {owner})",
        "ru": "{address} (аккаунт: {owner})",
    },
    # ---- COD cap, PLACE arm (telegram_bot/handlers/orders.py) ---------------
    # Spec §7: only a COUNT may cross the privacy boundary at a shared
    # workplace — never a coworker's name, phone or order number. The copy
    # blames the workplace (not this customer, whose own record may be clean)
    # and still steers to the action they CAN take, matching the register of
    # the sibling telegram.orders.cod_restricted_* strings already seeded.
    "telegram.orders.cod_restricted_place": {
        "en": (
            "Cash on delivery is unavailable because the workplace at this delivery address has "
            "{place_active_cod_debt_count} outstanding COD debts. Please choose a card payment method."
        ),
        "uz": (
            "Ushbu yetkazib berish manzilidagi ish joyida {place_active_cod_debt_count} ta "
            "to'lanmagan naqd qarz bo'lgani uchun yetkazib berishda naqd to'lash mavjud emas. "
            "Iltimos, karta to'lovini tanlang."
        ),
        "ru": (
            "Оплата наличными при доставке недоступна: по рабочему адресу доставки есть "
            "{place_active_cod_debt_count} непогашенных задолженностей. "
            "Пожалуйста, выберите оплату картой."
        ),
    },
    # ---- Customer wallet surface (spec §7) ----------------------------------
    # Consumed by `_build_cod_summary_lines` in telegram_bot/handlers/orders.py
    # (Task 16). Every 2c customer-bot key lives in this one script so a single
    # seed run covers the whole plan.
    "telegram.payments.cluster_debt_total": {
        "en": "💵 Unpaid across your linked accounts: {total}",
        "uz": "💵 Bog'langan hisoblaringiz bo'yicha to'lanmagan: {total}",
        "ru": "💵 Не оплачено по вашим связанным аккаунтам: {total}",
    },
    "telegram.payments.place_debt_total": {
        "en": "🏢 {label} — unpaid at this place: {total}",
        "uz": "🏢 {label} — ushbu manzilda to'lanmagan: {total}",
        "ru": "🏢 {label} — не оплачено по этому адресу: {total}",
    },
    "telegram.payments.place_order_line": {
        "en": "{order_number} · {member_name}: {amount}",
        "uz": "{order_number} · {member_name}: {amount}",
        "ru": "{order_number} · {member_name}: {amount}",
    },
}


def run():
    app = create_app()
    with app.app_context():
        data = {"en": {}, "uz": {}, "ru": {}}
        for key, langs in KEYS.items():
            for lang, value in langs.items():
                data[lang][key] = value
        Translation.bulk_create_or_update(data, category=CATEGORY)
        print(f"Seeded {len(KEYS)} keys x3 langs into category '{CATEGORY}'.")


if __name__ == "__main__":
    run()
