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
    # Task 30A: a NEW, separate key rather than a retrofit of the one above.
    # `cod_restricted_has_debts` also serves as the degrade target when the
    # PLACE arm's key is unseeded (telegram_bot/handlers/orders.py) — for
    # that path the customer's own count is the only safe thing to state,
    # since their own balance is not necessarily over the floor. This key is
    # used ONLY for a genuine PERSON-arm restriction, where the balance named
    # is the customer's own money and really is over {threshold} — the
    # actionable fact a bare count can't give them (they can't "reduce" a
    # count, only pay down money). Degrades to `cod_restricted_has_debts` on
    # an unseeded environment, same guard as the place key.
    'telegram.orders.cod_restricted_person': {
        'en': (
            'Cash on delivery is unavailable: your outstanding balance is {net_debt_total} so\'m, '
            'over the {threshold} so\'m limit. Please pay down your balance or choose a card '
            'payment method.'
        ),
        'uz': (
            'Yetkazib berishda naqd to\'lash mavjud emas: balansingizdagi qarz {net_debt_total} '
            'so\'m bo\'lib, bu {threshold} so\'mlik chegaradan yuqori. Iltimos, qarzingizni '
            'kamaytiring yoki karta to\'lovini tanlang.'
        ),
        'ru': (
            'Оплата наличными при доставке недоступна: ваш непогашенный остаток составляет '
            '{net_debt_total} сум, что превышает лимит в {threshold} сум. Пожалуйста, погасите '
            'часть долга или выберите оплату картой.'
        ),
    },
    # Task X2: the subject line of the Telegram warning sent when a delivery tips
    # the customer over the COD debt limit (StaffService._notify_customer_cod_debt_limit).
    # Its body is `telegram.orders.cod_restricted_person` above: one sentence for one fact.
    'telegram.orders.cod_restricted_subject': {
        'en': 'Cash on delivery is paused',
        'uz': 'Yetkazib berishda naqd to\'lash vaqtincha to\'xtatildi',  # NATIVE-READ
        'ru': 'Оплата наличными при доставке приостановлена',  # NATIVE-READ
    },
    'telegram.orders.cod_restricted_unavailable': {
        'en': 'Cash on delivery is temporarily unavailable. Please choose a card payment method.',
        'uz': 'Yetkazib berishda naqd to\'lash vaqtincha mavjud emas. Iltimos, karta to\'lovini tanlang.',
        'ru': 'Оплата наличными при доставке временно недоступна. Пожалуйста, выберите оплату картой.',
    },
    'telegram.orders.cod_prepayment_applied': {
        'en': '\n🔁 COD prepaid used: {potential_applied} so\'m. Pay on delivery: {payable_after} so\'m.',
        'uz': '\n🔁 Oldindan to\'langan summadan ishlatildi: {potential_applied} so\'m. Yetkazib berishda to\'lanadi: {payable_after} so\'m.',
        'ru': '\n🔁 Использован предоплаченный остаток: {potential_applied} сум. К оплате при доставке: {payable_after} сум.',
    },
    # MOVED to scripts/seed_backend_translations.py by B4b (one key, one home):
    # 'telegram.orders.cod_prepaid_balance' and the whole 'telegram.cart.*'
    # block below it. B4b reworded them so they stop promising an auto-apply
    # that a card/Click checkout will not honour, and leaving a stale copy here
    # would mean the next run of this script silently reverted that wording.
    'telegram.orders.cod_prepaid_auto_applied': {
        'en': '🔁 Auto-applied on this COD order: {potential_applied} so\'m',
        'uz': '🔁 Ushbu buyurtmaga avtomatik qo\'llaniladi: {potential_applied} so\'m',
        'ru': '🔁 Автоматически зачтено по этому заказу: {potential_applied} сум',
    },
    'telegram.orders.cod_estimated_payable': {
        'en': '🧾 Estimated COD payable after prepaid: {payable_after} so\'m',
        'uz': '🧾 Predoplata hisobga olingach to\'lanadi: {payable_after} so\'m',
        'ru': '🧾 К оплате при доставке после зачёта: {payable_after} сум',
    },

    # ------------------------------------------------------------------
    # Admin UI — returning a customer's prepayment (Return prepayment, Undo,
    # blocking-reason copy). JSX fallbacks equal the 'en' values byte for byte.
    # ------------------------------------------------------------------
    'ui.prepayments.return_button': {
        'en': 'Return prepayment',
        'uz': "Oldindan to'lovni qaytarish",
        'ru': 'Вернуть предоплату',
    },
    'ui.prepayments.mode_amount': {
        'en': 'Amount to return',
        'uz': 'Qaytariladigan summa',
        'ru': 'Сумма к возврату',
    },
    'ui.prepayments.mode_target': {
        'en': 'Balance after return',
        'uz': 'Qaytarishdan keyingi qoldiq',
        'ru': 'Остаток после возврата',
    },
    'ui.prepayments.paid_by_office': {
        'en': 'Office',
        'uz': 'Ofis',
        'ru': 'Офис',
    },
    'ui.prepayments.paid_by_driver_current': {
        'en': "Driver, from his current trip's cash",
        'uz': 'Haydovchi, joriy reysdagi naqd pulidan',
        'ru': 'Водитель, из наличных текущего рейса',
    },
    'ui.prepayments.paid_by_driver_settled': {
        'en': 'Driver, already settled',
        'uz': 'Haydovchi, allaqachon hisob-kitob qilingan',
        'ru': 'Водитель, уже рассчитан',
    },
    'ui.prepayments.returned': {
        'en': 'Returned',
        'uz': 'Qaytarilgan',
        'ru': 'Возвращено',
    },
    'ui.prepayments.undone': {
        'en': 'Undone',
        'uz': 'Bekor qilingan',
        'ru': 'Отменено',
    },
    'ui.prepayments.undo': {
        'en': 'Undo',
        'uz': 'Bekor qilish',
        'ru': 'Отменить',
    },
    'ui.prepayments.undo_title': {
        'en': 'Undo this return',
        'uz': 'Ushbu qaytarishni bekor qilish',
        'ru': 'Отменить этот возврат',
    },
    'ui.prepayments.undo_success': {
        'en': 'Return undone',
        'uz': 'Qaytarish bekor qilindi',
        'ru': 'Возврат отменён',
    },
    'ui.prepayments.lifetime_returned': {
        'en': 'Lifetime returned',
        'uz': 'Jami qaytarilgan',
        'ru': 'Всего возвращено',
    },
    'ui.prepayments.returns_table': {
        'en': 'Returned to the customer',
        'uz': 'Mijozga qaytarilgan',
        'ru': 'Возвращено клиенту',
    },
    'ui.prepayments.card_click_share': {
        'en': 'Of this, from card/Click',
        'uz': 'Shundan karta/Click orqali tushgan',
        'ru': 'Из них поступило картой/Click',
    },
    'ui.prepayments.release_by_cancel': {
        'en': 'Cancel order {{order_number}} to release it',
        'uz': "Bo'shatish uchun {{order_number}} buyurtmani bekor qiling",
        'ru': 'Чтобы освободить, отмените заказ {{order_number}}',
    },
    'ui.prepayments.correction_blocked_by_return': {
        'en': 'A prepayment return drew on this collection. Undo the return first.',
        'uz': "Oldindan to'lovni qaytarish ushbu yig'imdan foydalangan. Avval qaytarishni bekor qiling.",
        'ru': 'Возврат предоплаты использовал эту инкассацию. Сначала отмените возврат.',
    },
    'ui.prepayments.return_amount': {
        'en': 'Amount returned',
        'uz': 'Qaytarilgan summa',
        'ru': 'Возвращённая сумма',
    },
    'ui.prepayments.return_paid_by': {
        'en': 'Paid by',
        'uz': "Kim to'lagan",
        'ru': 'Кто выплатил',
    },
    'ui.prepayments.return_driver': {
        'en': 'Driver',
        'uz': 'Haydovchi',
        'ru': 'Водитель',
    },
    'ui.prepayments.return_driver_placeholder': {
        'en': 'Select a driver',
        'uz': 'Haydovchini tanlang',
        'ru': 'Выберите водителя',
    },
    'ui.prepayments.return_driver_required': {
        'en': 'Pick the driver',
        'uz': 'Haydovchini tanlang',
        'ru': 'Выберите водителя',
    },
    'ui.prepayments.return_paid_at': {
        'en': 'Paid at (optional)',
        'uz': "To'langan vaqt (ixtiyoriy)",
        'ru': 'Время выплаты (необязательно)',
    },
    'ui.prepayments.return_reason': {
        'en': 'Reason',
        'uz': 'Sabab',
        'ru': 'Причина',
    },
    'ui.prepayments.return_reason_required': {
        'en': 'Reason (min 5 chars) is required',
        'uz': 'Sabab kiritilishi shart (kamida 5 belgi)',
        'ru': 'Укажите причину (минимум 5 символов)',
    },
    'ui.prepayments.return_preview': {
        'en': 'Preview',
        'uz': "Ko'rib chiqish",
        'ru': 'Предпросмотр',
    },
    'ui.prepayments.return_free_balance': {
        'en': 'Free balance',
        'uz': "Bo'sh qoldiq",
        'ru': 'Свободный остаток',
    },
    'ui.prepayments.return_pending_orders': {
        'en': 'Pending orders',
        'uz': 'Kutilayotgan buyurtmalar',
        'ru': 'Ожидающие заказы',
    },
    'ui.prepayments.return_live_subscriptions': {
        'en': 'Live subscriptions may rely on this credit',
        'uz': 'Faol obunalar shu kreditga tayanishi mumkin',
        'ru': 'Активные подписки могут опираться на этот кредит',
    },
    'ui.prepayments.return_place_debts': {
        'en': "Open debts at this customer's places",
        'uz': 'Mijozning joylaridagi ochiq qarzlar',
        'ru': 'Открытые долги по местам клиента',
    },
    'ui.prepayments.return_session_effect': {
        'en': "The driver's trip cash",
        'uz': 'Haydovchining reysdagi naqd puli',
        'ru': 'Наличные водителя на рейсе',
    },
    'ui.prepayments.return_contract_effect': {
        'en': 'Contract accounts',
        'uz': 'Shartnoma hisoblari',
        'ru': 'Счета договоров',
    },
    'ui.prepayments.order_status_pending': {
        'en': 'Pending',
        'uz': 'Kutilmoqda',
        'ru': 'Ожидает',
    },
    'ui.prepayments.order_status_confirmed': {
        'en': 'Confirmed',
        'uz': 'Tasdiqlangan',
        'ru': 'Подтверждён',
    },
    'ui.prepayments.order_status_preparing': {
        'en': 'Preparing',
        'uz': 'Tayyorlanmoqda',
        'ru': 'Готовится',
    },
    'ui.prepayments.order_status_out_for_delivery': {
        'en': 'Out for delivery',
        'uz': 'Yetkazib berilmoqda',
        'ru': 'В пути',
    },
    'ui.prepayments.sub_status_active': {
        'en': 'Active',
        'uz': 'Faol',
        'ru': 'Активна',
    },
    'ui.prepayments.sub_status_paused': {
        'en': 'Paused',
        'uz': "To'xtatilgan",
        'ru': 'Приостановлена',
    },
    'ui.prepayments.return_amount_locked_hint': {
        'en': 'There is no free prepayment right now. Preview shows what is held.',
        'uz': "Hozir bo'sh oldindan to'lov yo'q. Ko'rib chiqish nimalar band ekanini ko'rsatadi.",
        'ru': 'Свободной предоплаты сейчас нет. Предпросмотр покажет, что удержано.',
    },
    'ui.prepayments.return_success': {
        'en': 'Prepayment returned',
        'uz': "Oldindan to'lov qaytarildi",
        'ru': 'Предоплата возвращена',
    },
    'ui.prepayments.block_no_free_balance': {
        'en': 'There is no free prepayment to return. Credit held on pending orders is released by cancelling them.',
        'uz': "Qaytariladigan bo'sh oldindan to'lov yo'q. Kutilayotgan buyurtmalarda band qilingan kredit ularni bekor qilish orqali bo'shatiladi.",
        'ru': 'Свободной предоплаты для возврата нет. Кредит, удерживаемый ожидающими заказами, освобождается их отменой.',
    },
    'ui.prepayments.block_amount_exceeds_free_balance': {
        'en': 'That is more than the free prepayment balance.',
        'uz': "Bu bo'sh oldindan to'lov qoldig'idan ko'p.",
        'ru': 'Это больше свободного остатка предоплаты.',
    },
    'ui.prepayments.block_target_not_below_free_balance': {
        'en': 'The balance after the return must be lower than the free balance now.',
        'uz': "Qaytarishdan keyingi qoldiq hozirgi bo'sh qoldiqdan past bo'lishi kerak.",
        'ru': 'Остаток после возврата должен быть ниже текущего свободного остатка.',
    },
    'ui.prepayments.block_driver_not_eligible': {
        'en': 'That person is not an active driver.',
        'uz': 'Bu shaxs faol haydovchi emas.',
        'ru': 'Этот человек не является активным водителем.',
    },
    'ui.prepayments.block_no_active_session': {
        'en': 'This driver has no current trip to take the cash from.',
        'uz': "Bu haydovchida naqd pul olinadigan joriy reys yo'q.",
        'ru': 'У этого водителя нет текущего рейса, из наличных которого можно выплатить.',
    },
    'ui.prepayments.block_amount_exceeds_driver_cash': {
        'en': "The driver's current trip does not hold that much cash.",
        'uz': "Haydovchining joriy reysida bunchalik naqd pul yo'q.",
        'ru': 'В текущем рейсе водителя нет столько наличных.',
    },
    'ui.prepayments.block_paid_before_current_trip': {
        'en': "The paid-at time is before the driver's current trip started. Use Driver, already settled instead.",
        'uz': 'To\'lov vaqti haydovchining joriy reysi boshlanishidan oldin. Buning o\'rniga "Haydovchi, allaqachon hisob-kitob qilingan" ni tanlang.',
        'ru': 'Время выплаты раньше начала текущего рейса водителя. Выберите «Водитель, уже рассчитан».',
    },
    'ui.prepayments.block_ambiguous_contract_mirror': {
        'en': 'This credit sits on more than one contract, so it cannot be returned automatically.',
        'uz': "Bu kredit bir nechta shartnomada turibdi, shuning uchun uni avtomatik qaytarib bo'lmaydi.",
        'ru': 'Этот кредит лежит на нескольких договорах, поэтому автоматически вернуть его нельзя.',
    },
    'ui.prepayments.block_units_contract_mirror': {
        'en': 'This credit funds prepaid contract bottles, so it cannot be returned as cash.',
        'uz': "Bu kredit shartnomadagi oldindan to'langan idishlarni ta'minlaydi, shuning uchun uni naqd pul sifatida qaytarib bo'lmaydi.",
        'ru': 'Этот кредит обеспечивает предоплаченные бутыли по договору, поэтому вернуть его наличными нельзя.',
    },
    'ui.prepayments.block_contract_account_missing': {
        'en': 'The contract account for this credit is missing, so it cannot be returned.',
        'uz': "Bu kredit uchun shartnoma hisobi topilmadi, shuning uchun uni qaytarib bo'lmaydi.",
        'ru': 'Счёт договора для этого кредита не найден, поэтому вернуть его нельзя.',
    },
    'ui.prepayments.undo_block_already_undone': {
        'en': 'This return has already been undone.',
        'uz': 'Bu qaytarish allaqachon bekor qilingan.',
        'ru': 'Этот возврат уже отменён.',
    },
    'ui.prepayments.undo_block_funding_event_voided': {
        'en': 'A collection this return drew on was voided, so it cannot be undone.',
        'uz': "Bu qaytarish foydalangan yig'im bekor qilingan, shuning uchun qaytarishni bekor qilib bo'lmaydi.",
        'ru': 'Инкассация, из которой был сделан этот возврат, аннулирована, поэтому отменить возврат нельзя.',
    },
    'ui.prepayments.undo_block_session_checked': {
        'en': "The driver's trip is closed, so this return can no longer be undone.",
        'uz': "Haydovchining reysi yopilgan, shuning uchun bu qaytarishni endi bekor qilib bo'lmaydi.",
        'ru': 'Рейс водителя закрыт, поэтому этот возврат больше нельзя отменить.',
    },

    'ui.prepayments.return_success_amount': {
        'en': 'Returned {{amount}} to the customer',
        'uz': '{{amount}} mijozga qaytarildi',
        'ru': 'Клиенту возвращено {{amount}}',
    },
    'ui.prepayments.return_amount_required': {
        'en': 'Enter an amount above 0',
        'uz': '0 dan katta summani kiriting',
        'ru': 'Введите сумму больше 0',
    },
    'ui.prepayments.return_drivers_failed': {
        'en': 'Could not load the drivers. Close this window and try again.',
        'uz': "Haydovchilarni yuklab bo'lmadi. Oynani yopib, qayta urinib ko'ring.",
        'ru': 'Не удалось загрузить водителей. Закройте окно и попробуйте снова.',
    },
    'ui.prepayments.return_paid_at_future': {
        'en': "Paid at can't be in the future",
        'uz': "To'lov vaqti kelajakda bo'lishi mumkin emas",
        'ru': 'Время выплаты не может быть в будущем',
    },
    'ui.prepayments.return_session_closes_warning': {
        'en': "This return closes the driver's trip; it can't be undone afterwards.",
        'uz': "Bu qaytarish haydovchining reysini yopadi; keyin uni bekor qilib bo'lmaydi.",
        'ru': 'Этот возврат закроет рейс водителя; потом его нельзя будет отменить.',
    },
    'ui.prepayments.error_amount': {
        'en': 'Enter the amount to return, or the balance to leave.',
        'uz': 'Qaytariladigan summani yoki qoldiriladigan qoldiqni kiriting.',
        'ru': 'Введите сумму к возврату или остаток, который нужно оставить.',
    },
    'ui.prepayments.error_paid_at': {
        'en': 'Paid at must be a past date and time.',
        'uz': "To'lov vaqti o'tgan sana va vaqt bo'lishi kerak.",
        'ru': 'Время выплаты должно быть датой и временем в прошлом.',
    },
    'ui.prepayments.error_reason': {
        'en': 'The reason is required: at least 5 characters, and not too long.',
        'uz': 'Sabab majburiy: kamida 5 ta belgi, va juda uzun emas.',
        'ru': 'Причина обязательна: не меньше 5 символов и не слишком длинная.',
    },
    'ui.prepayments.error_payer': {
        'en': 'Check who paid: the office names no driver, and a driver payment needs one.',
        'uz': "Kim to'laganini tekshiring: ofis haydovchini ko'rsatmaydi, haydovchi to'lovi esa haydovchini talab qiladi.",
        'ru': 'Проверьте, кто платил: для офиса водитель не указывается, а для выплаты водителем он обязателен.',
    },
    'ui.prepayments.error_key_reused': {
        'en': 'This return was already recorded with different values. Close this window and start a new return.',
        'uz': 'Bu qaytarish boshqa qiymatlar bilan allaqachon yozilgan. Oynani yopib, yangi qaytarishni boshlang.',
        'ru': 'Этот возврат уже записан с другими значениями. Закройте окно и начните новый возврат.',
    },
    'ui.prepayments.error_request': {
        'en': 'The request was not understood. Reload the page and try again.',
        'uz': "So'rov tushunilmadi. Sahifani yangilab, qayta urinib ko'ring.",
        'ru': 'Запрос не распознан. Обновите страницу и попробуйте снова.',
    },

    # The 'telegram.cart.*' prepayment block lived here until B4b moved it to
    # scripts/seed_backend_translations.py — see the note above.
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
