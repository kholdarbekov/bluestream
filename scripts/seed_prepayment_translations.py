#!/usr/bin/env python3
"""Seed translation keys for the customer COD prepayments feature.

Covers two surfaces:
- Admin UI strings (``ui.*``) for the new Customer Prepayments page and the
  prepayment card inside the Users detail modal.
- Telegram bot strings (``telegram.orders.*``, ``telegram.cart.*``) that
  replace the hardcoded English fragments rendered around COD prepayment.

Run inside the business_app container:

    docker compose exec business_app python scripts/seed_prepayment_translations.py
"""

import sys

sys.path.insert(0, '/app')

from business_app import create_app, db  # noqa: E402
from business_app.models.translation import Translation  # noqa: E402


# Translation key structure: {category}.{subcategory}.{identifier}
PREPAYMENT_TRANSLATIONS = {
    # ------------------------------------------------------------------
    # Admin UI — navigation
    # ------------------------------------------------------------------
    'ui.nav.prepayments': {
        'en': 'Customer Prepayments',
        'uz': 'Mijoz oldindan to\'lovlari',
        'ru': 'Предоплаты клиентов',
    },

    # ------------------------------------------------------------------
    # Admin UI — Prepayments page (ui.prepayments.*)
    # ------------------------------------------------------------------
    'ui.prepayments.title': {
        'en': 'Customer Prepayments',
        'uz': 'Mijoz oldindan to\'lovlari',
        'ru': 'Предоплаты клиентов',
    },
    'ui.prepayments.balance': {
        'en': 'Prepayment balance',
        'uz': 'Oldindan to\'lov qoldig\'i',
        'ru': 'Остаток предоплаты',
    },
    'ui.prepayments.total_customers': {
        'en': 'Customers with balance',
        'uz': 'Qoldig\'i bo\'lgan mijozlar',
        'ru': 'Клиенты с остатком',
    },
    'ui.prepayments.total_balance': {
        'en': 'Total prepayment balance',
        'uz': 'Umumiy oldindan to\'lov qoldig\'i',
        'ru': 'Общий остаток предоплат',
    },
    'ui.prepayments.lifetime_collected': {
        'en': 'Lifetime collected',
        'uz': 'Umumiy yig\'ilgan',
        'ru': 'Всего собрано',
    },
    'ui.prepayments.lifetime_applied': {
        'en': 'Lifetime applied',
        'uz': 'Umumiy hisobga olingan',
        'ru': 'Всего зачтено',
    },
    'ui.prepayments.events_table': {
        'en': 'Cash collection events',
        'uz': 'Naqd pul yig\'ish hodisalari',
        'ru': 'События сбора наличных',
    },
    'ui.prepayments.customer': {
        'en': 'Customer',
        'uz': 'Mijoz',
        'ru': 'Клиент',
    },
    'ui.prepayments.role': {
        'en': 'Role',
        'uz': 'Rol',
        'ru': 'Роль',
    },
    'ui.prepayments.last_collection_at': {
        'en': 'Last collection',
        'uz': 'Oxirgi yig\'ish',
        'ru': 'Последний сбор',
    },
    'ui.prepayments.view_ledger': {
        'en': 'View ledger',
        'uz': 'Tafsilotlarni ko\'rish',
        'ru': 'Открыть журнал',
    },
    'ui.prepayments.customer_ledger': {
        'en': 'Customer ledger',
        'uz': 'Mijoz tafsilotlari',
        'ru': 'Журнал клиента',
    },
    'ui.prepayments.occurred_at': {
        'en': 'When',
        'uz': 'Vaqt',
        'ru': 'Когда',
    },
    'ui.prepayments.source': {
        'en': 'Source',
        'uz': 'Manba',
        'ru': 'Источник',
    },
    'ui.prepayments.collected_amount': {
        'en': 'Collected',
        'uz': 'Yig\'ilgan',
        'ru': 'Собрано',
    },
    'ui.prepayments.unapplied_amount': {
        'en': 'Unapplied',
        'uz': 'Hisobga olinmagan',
        'ru': 'Не зачтено',
    },
    'ui.prepayments.origin_order': {
        'en': 'Origin order',
        'uz': 'Asl buyurtma',
        'ru': 'Исходный заказ',
    },
    'ui.prepayments.notes': {
        'en': 'Notes',
        'uz': 'Izohlar',
        'ru': 'Примечания',
    },
    'ui.prepayments.allocations': {
        'en': 'Allocations',
        'uz': 'Taqsimotlar',
        'ru': 'Распределения',
    },
    'ui.prepayments.allocation_mode': {
        'en': 'Mode',
        'uz': 'Rejim',
        'ru': 'Режим',
    },
    'ui.prepayments.allocated_at': {
        'en': 'Allocated at',
        'uz': 'Taqsimlangan vaqt',
        'ru': 'Распределено',
    },
    'ui.prepayments.allocated_amount': {
        'en': 'Amount',
        'uz': 'Miqdor',
        'ru': 'Сумма',
    },
    'ui.prepayments.order_number': {
        'en': 'Order',
        'uz': 'Buyurtma',
        'ru': 'Заказ',
    },
    'ui.prepayments.reversed': {
        'en': 'Reversed',
        'uz': 'Bekor qilingan',
        'ru': 'Отменено',
    },
    'ui.prepayments.voided': {
        'en': 'Voided',
        'uz': 'Bekor qilingan',
        'ru': 'Аннулировано',
    },
    'ui.prepayments.include_voided': {
        'en': 'Include voided',
        'uz': 'Bekor qilinganlarni ko\'rsatish',
        'ru': 'Показывать аннулированные',
    },
    'ui.prepayments.include_fully_applied': {
        'en': 'Include fully applied',
        'uz': 'To\'liq hisobga olinganlarni ko\'rsatish',
        'ru': 'Показывать полностью зачтённые',
    },
    'ui.prepayments.no_customers': {
        'en': 'No customers carry an open prepayment balance.',
        'uz': 'Hech bir mijozda ochiq oldindan to\'lov qoldig\'i yo\'q.',
        'ru': 'Ни у одного клиента нет открытого остатка предоплаты.',
    },
    'ui.prepayments.no_events': {
        'en': 'No cash collection events match these filters.',
        'uz': 'Ushbu filtrlarga mos naqd pul yig\'ish hodisalari yo\'q.',
        'ru': 'Нет событий сбора наличных по этим фильтрам.',
    },
    'ui.prepayments.no_allocations': {
        'en': 'No allocations yet — this event sits as available prepayment.',
        'uz': 'Hali taqsimotlar yo\'q — bu mablag\' mavjud oldindan to\'lov sifatida qoldi.',
        'ru': 'Распределений ещё нет — сумма доступна как предоплата.',
    },
    'ui.prepayments.no_history': {
        'en': 'No prepayment activity for this customer.',
        'uz': 'Bu mijoz uchun oldindan to\'lov harakatlari yo\'q.',
        'ru': 'У этого клиента нет операций по предоплате.',
    },
    'ui.prepayments.search_placeholder': {
        'en': 'Search by name or phone',
        'uz': 'Ism yoki telefon bo\'yicha qidirish',
        'ru': 'Поиск по имени или телефону',
    },
    'ui.prepayments.list_load_error': {
        'en': 'Failed to load customers with prepayment balance',
        'uz': 'Oldindan to\'lov qoldig\'iga ega mijozlarni yuklashda xatolik',
        'ru': 'Не удалось загрузить клиентов с остатком предоплаты',
    },
    'ui.prepayments.history_load_error': {
        'en': 'Failed to load prepayment history',
        'uz': 'Oldindan to\'lov tarixini yuklashda xatolik',
        'ru': 'Не удалось загрузить историю предоплат',
    },

    # ------------------------------------------------------------------
    # Admin UI — Users modal additions (ui.users.*)
    # ------------------------------------------------------------------
    'ui.users.prepayment_balance': {
        'en': 'Prepayment balance',
        'uz': 'Oldindan to\'lov qoldig\'i',
        'ru': 'Остаток предоплаты',
    },
    'ui.users.prepayment_history': {
        'en': 'Prepayment history',
        'uz': 'Oldindan to\'lov tarixi',
        'ru': 'История предоплат',
    },
    'ui.users.view_full_ledger': {
        'en': 'View full ledger',
        'uz': 'To\'liq tafsilotlarni ko\'rish',
        'ru': 'Открыть полный журнал',
    },
    'ui.users.no_prepayment_history': {
        'en': 'No prepayment activity for this customer.',
        'uz': 'Bu mijoz uchun oldindan to\'lov harakatlari yo\'q.',
        'ru': 'У этого клиента нет операций по предоплате.',
    },

    # ------------------------------------------------------------------
    # Telegram bot — order confirmation & post-order (telegram.orders.*)
    # ------------------------------------------------------------------
    'telegram.orders.cod_restricted_has_debts': {
        'en': 'Cash on delivery is unavailable because you already have {active_debt_count} outstanding COD debts. Please choose a card payment method.',
        'uz': 'Sizda {active_debt_count} ta to\'lanmagan naqd buyurtma bo\'lgani uchun yetkazib berishda naqd to\'lash mavjud emas. Iltimos, karta to\'lovini tanlang.',
        'ru': 'Оплата наличными при доставке недоступна: у вас {active_debt_count} непогашенных задолженностей. Пожалуйста, выберите оплату картой.',
    },
    'telegram.orders.cod_restricted_unavailable': {
        'en': 'Cash on delivery is temporarily unavailable. Please choose a card payment method.',
        'uz': 'Yetkazib berishda naqd to\'lash vaqtincha mavjud emas. Iltimos, karta to\'lovini tanlang.',
        'ru': 'Оплата наличными при доставке временно недоступна. Пожалуйста, выберите оплату картой.',
    },
    'telegram.orders.cod_prepayment_applied': {
        'en': '\n🔁 COD prepaid used: {potential_applied} UZS. Pay on delivery: {payable_after} UZS.',
        'uz': '\n🔁 Oldindan to\'langan summadan ishlatildi: {potential_applied} so\'m. Yetkazib berishda to\'lanadi: {payable_after} so\'m.',
        'ru': '\n🔁 Использован предоплаченный остаток: {potential_applied} сум. К оплате при доставке: {payable_after} сум.',
    },
    'telegram.orders.cod_prepaid_balance': {
        'en': '💳 COD prepaid balance: {available_balance} UZS',
        'uz': '💳 Oldindan to\'langan qoldiq: {available_balance} so\'m',
        'ru': '💳 Предоплаченный остаток: {available_balance} сум',
    },
    'telegram.orders.cod_prepaid_auto_applied': {
        'en': '🔁 Auto-applied on this COD order: {potential_applied} UZS',
        'uz': '🔁 Ushbu buyurtmaga avtomatik qo\'llaniladi: {potential_applied} so\'m',
        'ru': '🔁 Автоматически зачтено по этому заказу: {potential_applied} сум',
    },
    'telegram.orders.cod_estimated_payable': {
        'en': '🧾 Estimated COD payable after prepaid: {payable_after} UZS',
        'uz': '🧾 Predoplata hisobga olingach to\'lanadi: {payable_after} so\'m',
        'ru': '🧾 К оплате при доставке после зачёта: {payable_after} сум',
    },

    # ------------------------------------------------------------------
    # Telegram bot — cart view (telegram.cart.*)
    # ------------------------------------------------------------------
    'telegram.cart.cod_prepaid_balance': {
        'en': '💳 COD prepaid balance: {available_balance} UZS',
        'uz': '💳 Oldindan to\'langan qoldiq: {available_balance} so\'m',
        'ru': '💳 Предоплаченный остаток: {available_balance} сум',
    },
    'telegram.cart.cod_prepaid_auto_applied_next': {
        'en': '🔁 Auto-applied on next COD order: {potential_applied} UZS',
        'uz': '🔁 Keyingi buyurtmaga avtomatik qo\'llaniladi: {potential_applied} so\'m',
        'ru': '🔁 Будет зачтено в следующий заказ: {potential_applied} сум',
    },
    'telegram.cart.cod_estimated_payable': {
        'en': '🧾 Estimated COD payable after prepaid: {payable_after} UZS',
        'uz': '🧾 Predoplata hisobga olingach to\'lanadi: {payable_after} so\'m',
        'ru': '🧾 К оплате при доставке после зачёта: {payable_after} сум',
    },
}


def main():
    app = create_app()
    with app.app_context():
        added = updated = unchanged = 0

        for key, translations in PREPAYMENT_TRANSLATIONS.items():
            category = key.split('.')[0]  # 'ui' or 'telegram'
            for language in ('en', 'uz', 'ru'):
                value = translations[language]
                existing = Translation.query.filter_by(
                    key=key,
                    language=language,
                ).first()

                if existing:
                    if existing.value == value and existing.is_active:
                        unchanged += 1
                        continue
                    existing.value = value
                    existing.is_active = True
                    existing.category = category
                    updated += 1
                    print(f"  ↻ {key} [{language}]")
                else:
                    db.session.add(Translation(
                        key=key,
                        language=language,
                        value=value,
                        category=category,
                        description=f"COD prepayment feature ({category})",
                        is_active=True,
                    ))
                    added += 1
                    print(f"  + {key} [{language}]")

        db.session.commit()

        print()
        print("=" * 70)
        print("✓ PREPAYMENT TRANSLATION SEEDING COMPLETED")
        print("=" * 70)
        print(f"  added:     {added}")
        print(f"  updated:   {updated}")
        print(f"  unchanged: {unchanged}")
        print()
        print("Don't forget to clear the translation cache:")
        print("  docker compose exec redis redis-cli FLUSHDB")


if __name__ == '__main__':
    main()
