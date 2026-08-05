# scripts/seed_place_group_ui_translations.py
"""Seed the Phase-2c place-group / linked-account ADMIN-UI keys.

category='ui' with DOTTED `ui.*` keys — the same mechanism as
scripts/seed_customer_link_translations.py. AdminUiTranslationService resolves
`ui.users.*` / `ui.orders.*` into the `users` / `orders` i18next namespaces via
LEGACY_NAMESPACE_PREFIXES, which requires category == 'ui'; `ui.common.*` and
`ui.prepayments.*` land in the shared `common` namespace, which every other
namespace falls back to (`fallbackNS: ['common']` in admin_ui/src/i18n.js).

Every call site passes an inline English fallback, so the admin UI renders
correctly in English BEFORE this seed lands — the seed is what gives uz/ru
users the same page. The `en` values below therefore reproduce those inline
fallbacks byte for byte; tests/unit/test_place_group_translation_seeds.py
parses the JSX and fails on any drift.

NOT seeded here: `ui.common.cancel` already exists
(scripts/seed_backend_translations.py) — `ui.common.ok` is the only new
`ui.common.*` key. Two further admin-UI strings from this plan do NOT use the
`ui.*` convention and live in their own scripts:
  * `scope` / `attribution`            -> scripts/seed_ui_staff_translations.py
                                          (category 'ui_staff', BARE keys)
  * `fine_place_union_balance_label`   -> scripts/seed_ui_bottle_tracking_linked_accounts.py
                                          (category 'ui_bottle_tracking', BARE keys)

Run inside the business_app container (scripts/ is NOT volume-mounted, so pipe
the file in over stdin — piped stdin still executes as __main__):
    docker compose exec -T business_app python - < scripts/seed_place_group_ui_translations.py
Then restart business_app so the translations API serves them:
    docker compose restart business_app
"""

from business_app import create_app
from business_app.models.translation import Translation

CATEGORY = "ui"

# {key: {language: value}} — `en` MUST match the inline t(key, 'fallback')
# string in the component exactly.
KEYS = {
    # ---- shared ------------------------------------------------------------
    "ui.common.ok": {"en": "OK", "uz": "OK", "ru": "OK"},

    # ---- PlaceGroupPanel: headings and figures ------------------------------
    "ui.users.place_groups.title": {
        "en": "Place groups (same physical place)",
        "uz": "Manzil guruhlari (bir xil jismoniy joy)",
        "ru": "Группы адресов (один и тот же адрес)",
    },
    "ui.users.place_groups.unnamed": {
        "en": "Place",
        "uz": "Manzil",
        "ru": "Адрес",
    },
    "ui.users.place_groups.no_groups": {
        "en": "No place groups for this customer yet",
        "uz": "Bu mijoz uchun hali manzil guruhi yo'q",
        "ru": "У этого клиента пока нет групп адресов",
    },
    "ui.users.place_groups.no_members": {
        "en": "No addresses in this place group",
        "uz": "Bu manzil guruhida manzillar yo'q",
        "ru": "В этой группе адресов нет адресов",
    },
    # One pool, one wording. The Bottle Tracking drawer renders the SAME figure
    # through the bare `place_balance_label`
    # (scripts/seed_ui_bottle_tracking_linked_accounts.py); when the two screens
    # said manzil/адрес here and joy/место there, a uz/ru admin cross-referencing
    # them had no way to tell it is one pool — the exact conflation the place
    # re-key exists to remove. `joy`/`место` is the place vocabulary.
    "ui.users.place_groups.union_balance": {
        "en": "Bottles at this place",
        "uz": "Ushbu joydagi idishlar",
        "ru": "Тара в этом месте",
    },
    "ui.users.place_groups.place_cod_total": {
        "en": "Place COD debt",
        "uz": "Manzil bo'yicha naqd qarz",
        "ru": "Долг наличными по адресу",
    },
    "ui.users.place_groups.place_cod_count": {
        "en": "Unpaid COD orders",
        "uz": "To'lanmagan naqd buyurtmalar",
        "ru": "Неоплаченные заказы наличными",
    },
    "ui.users.place_groups.audit_title": {
        "en": "Place group history",
        "uz": "Manzil guruhi tarixi",
        "ru": "История группы адресов",
    },
    "ui.users.place_groups.no_events": {
        "en": "No changes recorded yet",
        "uz": "Hali o'zgarishlar qayd etilmagan",
        "ru": "Изменений пока не зафиксировано",
    },

    # ---- PlaceGroupPanel: actions ------------------------------------------
    "ui.users.place_groups.create_action": {
        "en": "New place group",
        "uz": "Yangi manzil guruhi",
        "ru": "Новая группа адресов",
    },
    "ui.users.place_groups.add_action": {
        "en": "Add address",
        "uz": "Manzil qo'shish",
        "ru": "Добавить адрес",
    },
    "ui.users.place_groups.remove": {
        "en": "Remove",
        "uz": "O'chirish",
        "ru": "Удалить",
    },
    "ui.users.place_groups.group_action": {
        "en": "Group as same place",
        "uz": "Bir xil manzil deb guruhlash",
        "ru": "Сгруппировать как один адрес",
    },
    "ui.users.place_groups.dismiss_action": {
        "en": "Not the same place",
        "uz": "Boshqa manzil",
        "ru": "Другой адрес",
    },

    # ---- PlaceGroupPanel: suggestions --------------------------------------
    "ui.users.place_groups.suggestions_title": {
        "en": "Possible same place",
        "uz": "Ehtimoliy bir xil manzil",
        "ru": "Возможно один и тот же адрес",
    },
    # Opt-in trigger: the co-location engine clusters the whole ungrouped
    # estate per call and cannot be narrowed (a bounding box would truncate a
    # transitive component and void dismissals — plan E19), so the drawer must
    # not bill it on every open. See PlaceGroupPanel.jsx.
    "ui.users.place_groups.find_suggestions": {
        "en": "Find possible same-place matches",
        "uz": "Ehtimoliy bir xil manzillarni topish",
        "ru": "Найти возможные совпадения адресов",
    },
    "ui.users.place_groups.no_suggestions": {
        "en": "No suggestions",
        "uz": "Takliflar yo'q",
        "ru": "Нет предложений",
    },
    "ui.users.place_groups.distinct_customers": {
        "en": "customers",
        "uz": "mijozlar",
        "ru": "клиентов",
    },

    # ---- PlaceGroupPanel: confirmation modals ------------------------------
    "ui.users.place_groups.create_title": {
        "en": "Group these addresses as one place?",
        "uz": "Ushbu manzillarni bitta joy deb guruhlaysizmi?",
        "ru": "Сгруппировать эти адреса как один адрес?",
    },
    "ui.users.place_groups.add_title": {
        "en": "Add an address to this place group?",
        "uz": "Ushbu manzil guruhiga manzil qo'shasizmi?",
        "ru": "Добавить адрес в эту группу адресов?",
    },
    "ui.users.place_groups.remove_title": {
        "en": "Remove this address from the place group?",
        "uz": "Ushbu manzilni manzil guruhidan chiqarasizmi?",
        "ru": "Удалить этот адрес из группы адресов?",
    },
    "ui.users.place_groups.dismiss_title": {
        "en": "Not the same place?",
        "uz": "Boshqa manzilmi?",
        "ru": "Это разные адреса?",
    },
    "ui.users.place_groups.search_placeholder": {
        "en": "Search addresses by phone, name or address",
        "uz": "Manzillarni telefon, ism yoki manzil bo'yicha qidiring",
        "ru": "Поиск адресов по телефону, имени или адресу",
    },
    "ui.users.place_groups.label_placeholder": {
        "en": "Label (e.g. Acme office)",
        "uz": "Nom (masalan, Acme ofisi)",
        "ru": "Метка (например, офис Acme)",
    },
    "ui.users.place_groups.reason_placeholder": {
        "en": "Reason (required)",
        "uz": "Sabab (majburiy)",
        "ru": "Причина (обязательно)",
    },

    # ---- PlaceGroupPanel: the remove dialog's split (spec §7.1) -------------
    # The backend has emitted `suggested_bottles_leaving` per member since the
    # remove endpoint learned `bottlesLeaving`; until the panel read it, every
    # removal defaulted to "all the bottles stay with the place".
    "ui.users.place_groups.bottles_leaving_label": {
        "en": "Bottles leaving with this address",
        "uz": "Ushbu manzil bilan ketadigan idishlar",
        "ru": "Тара, уходящая вместе с этим адресом",
    },
    "ui.users.place_groups.bottles_leaving_hint": {
        "en": (
            "Pre-filled from this address's own entries at this place, capped at the place "
            "total. The rest stays with the place."
        ),
        "uz": (
            "Ushbu manzilning shu joydagi o'z yozuvlari asosida to'ldirilgan va manzildagi "
            "jami bilan cheklangan. Qolgani manzilda qoladi."
        ),
        "ru": (
            "Заполнено по собственным записям этого адреса в этом месте и ограничено общим "
            "количеством по адресу. Остальное остаётся по адресу."
        ),
    },

    # ---- PlaceGroupPanel: the merge review (spec §7.4) ----------------------
    "ui.users.place_groups.merge_review_action": {
        "en": "Review bottle history",
        "uz": "Idishlar tarixini ko'rib chiqish",
        "ru": "Просмотреть историю тары",
    },
    "ui.users.place_groups.merge_review_title": {
        "en": "Review the merged bottle history",
        "uz": "Birlashtirilgan idishlar tarixini ko'rib chiqing",
        "ru": "Просмотрите объединённую историю тары",
    },
    "ui.users.place_groups.merge_computed_balance": {
        "en": "Combined balance",
        "uz": "Umumiy qoldiq",
        "ru": "Суммарный остаток",
    },
    "ui.users.place_groups.merge_excluded_total": {
        "en": "Excluded",
        "uz": "Chiqarib tashlangan",
        "ru": "Исключено",
    },
    "ui.users.place_groups.merge_resulting_balance": {
        "en": "Resulting balance",
        "uz": "Yakuniy qoldiq",
        "ru": "Итоговый остаток",
    },
    # What the place will actually HOLD once the join commits, which is NOT the
    # ledger-derived resulting balance on a place whose stored figure its history
    # never explained. Both are shown, because the override is measured against
    # this one.
    "ui.users.place_groups.merge_projected_balance": {
        "en": "Place will hold",
        "uz": "Manzilda qoladi",
        "ru": "Останется по адресу",
    },
    "ui.users.place_groups.merge_drift": {
        "en": "Unexplained drift",
        "uz": "Tushuntirilmagan farq",
        "ru": "Необъяснённое расхождение",
    },
    # CONDITIONAL, because the repair is conditional: with no exclusion and no
    # override `_apply_merge_review` returns before writing anything, so the
    # drift survives the join. An unconditional "joining writes the difference
    # into the ledger" is false on the path an admin is most likely to take.
    "ui.users.place_groups.merge_drift_hint": {
        "en": (
            "These places hold more (or fewer) bottles than their history explains, which is "
            "why the place will hold the figure above rather than the combined history total. "
            "Excluding an entry or setting the resulting balance writes that difference into "
            "the ledger so both figures agree; joining without a change leaves it in place."
        ),
        "uz": (
            "Bu manzillarda tarixi tushuntirganidan ko'proq (yoki kamroq) idish bor — shuning "
            "uchun manzilda umumiy tarix jami emas, yuqoridagi raqam qoladi. Yozuvni chiqarib "
            "tashlash yoki yakuniy qoldiqni belgilash bu farqni tarixga yozadi va ikkala raqam "
            "mos keladi; o'zgarishsiz birlashtirish esa farqni joyida qoldiradi."
        ),
        "ru": (
            "В этих местах тары больше (или меньше), чем объясняет их история, — поэтому по "
            "адресу останется цифра выше, а не суммарный итог истории. Исключение записи или "
            "указание итогового остатка записывает эту разницу в историю, и обе цифры "
            "совпадают; объединение без изменений оставляет разницу как есть."
        ),
    },
    "ui.users.place_groups.merge_exclude": {
        "en": "Exclude",
        "uz": "Chiqarib tashlash",
        "ru": "Исключить",
    },
    "ui.users.place_groups.merge_override_label": {
        "en": "Set the resulting balance instead",
        "uz": "Buning o'rniga yakuniy qoldiqni belgilang",
        "ru": "Вместо этого задайте итоговый остаток",
    },
    "ui.users.place_groups.merge_preview_failed": {
        "en": "Could not load the merged history",
        "uz": "Birlashtirilgan tarixni yuklab bo'lmadi",
        "ru": "Не удалось загрузить объединённую историю",
    },
    "ui.users.place_groups.merge_empty": {
        "en": "No bottle history to merge",
        "uz": "Birlashtiriladigan idishlar tarixi yo'q",
        "ru": "Нет истории тары для объединения",
    },

    # ---- PlaceGroupPanel: outcomes -----------------------------------------
    "ui.users.place_groups.create_success": {
        "en": "Place group created",
        "uz": "Manzil guruhi yaratildi",
        "ru": "Группа адресов создана",
    },
    "ui.users.place_groups.add_success": {
        "en": "Address added to place group",
        "uz": "Manzil guruhga qo'shildi",
        "ru": "Адрес добавлен в группу адресов",
    },
    "ui.users.place_groups.remove_success": {
        "en": "Address removed from place group",
        "uz": "Manzil guruhdan chiqarildi",
        "ru": "Адрес удалён из группы адресов",
    },
    "ui.users.place_groups.dismiss_success": {
        "en": "Suggestion dismissed",
        "uz": "Taklif rad etildi",
        "ru": "Предложение отклонено",
    },
    "ui.users.place_groups.action_failed": {
        "en": "Action failed",
        "uz": "Amal bajarilmadi",
        "ru": "Не удалось выполнить действие",
    },
    "ui.users.place_groups.search_failed": {
        "en": "Address search failed",
        "uz": "Manzil qidiruvi muvaffaqiyatsiz",
        "ru": "Ошибка поиска адресов",
    },

    # ---- PlaceGroupPanel: backend fence codes ------------------------------
    # These replace the literal "Validation failed" envelope message with the
    # actionable reason the admin can act on.
    "ui.users.place_groups.error_grocery_member": {
        "en": "Grocery-store accounts cannot be part of a place group.",
        "uz": "Do'kon hisoblari manzil guruhiga kira olmaydi.",
        "ru": "Аккаунты магазинов не могут входить в группу адресов.",
    },
    "ui.users.place_groups.error_entity_member": {
        "en": "Business (entity) accounts cannot be part of a place group.",
        "uz": "Yuridik shaxs hisoblari manzil guruhiga kira olmaydi.",
        "ru": "Аккаунты юридических лиц не могут входить в группу адресов.",
    },
    "ui.users.place_groups.error_already_grouped": {
        "en": "That address is already in another place group. Remove it from that group first.",
        "uz": "Bu manzil allaqachon boshqa manzil guruhida. Avval uni o'sha guruhdan chiqaring.",
        "ru": "Этот адрес уже входит в другую группу адресов. Сначала удалите его из той группы.",
    },
    "ui.users.place_groups.error_group_not_found": {
        "en": "This place group no longer exists. Refresh and try again.",
        "uz": "Bu manzil guruhi endi mavjud emas. Sahifani yangilab qayta urinib ko'ring.",
        "ru": "Эта группа адресов больше не существует. Обновите страницу и попробуйте снова.",
    },
    "ui.users.place_groups.error_address_not_found": {
        "en": "One of the selected addresses no longer exists.",
        "uz": "Tanlangan manzillardan biri endi mavjud emas.",
        "ru": "Одного из выбранных адресов больше не существует.",
    },
    # Spec §7.1: an impossible split is REJECTED, never clamped, so this line is
    # the only thing that tells the admin the number they typed was thrown away.
    "ui.users.place_groups.error_place_split_invalid": {
        "en": "Bottles leaving must be between 0 and the place total.",
        "uz": "Ketadigan idishlar soni 0 bilan manzildagi jami orasida bo'lishi kerak.",
        "ru": "Количество уходящей тары должно быть от 0 до общего количества по адресу.",
    },
    "ui.users.place_groups.error_merge_preview_stale": {
        "en": (
            "The bottle history changed while you were reviewing it. Reload the preview and "
            "try again."
        ),
        "uz": (
            "Siz ko'rib chiqayotganingizda idishlar tarixi o'zgardi. Ko'rinishni qayta yuklab, "
            "yana urinib ko'ring."
        ),
        "ru": (
            "История тары изменилась, пока вы её просматривали. Обновите предпросмотр и "
            "попробуйте снова."
        ),
    },
    "ui.users.place_groups.error_merge_exclusion": {
        "en": "One of the excluded entries is not part of this merge.",
        "uz": "Chiqarib tashlangan yozuvlardan biri ushbu birlashtirishga tegishli emas.",
        "ru": "Одна из исключённых записей не относится к этому объединению.",
    },
    "ui.users.place_groups.error_merge_reason": {
        "en": "A reason is required to exclude entries or override the balance.",
        "uz": "Yozuvlarni chiqarib tashlash yoki qoldiqni o'zgartirish uchun sabab talab qilinadi.",
        "ru": "Чтобы исключить записи или изменить остаток, требуется причина.",
    },
    # PLACE_GROUP_MIN_ADDRESSES is reachable in one call (a DUPLICATE address id
    # passes the route's len(address_ids) >= 2 guard and trips the service's
    # len(set(address_ids)) >= 2 guard); PLACE_GROUP_REASON_REQUIRED is masked
    # only by every route's own blank-reason guard. Without these two rows a
    # uz/ru admin reads the raw English service sentence.
    "ui.users.place_groups.error_min_addresses": {
        "en": "A place group needs at least two different addresses.",
        "uz": "Joy guruhi uchun kamida ikkita har xil manzil kerak.",
        "ru": "Для группы адресов нужны минимум два разных адреса.",
    },
    "ui.users.place_groups.error_reason_required": {
        "en": "A reason is required for this change.",
        "uz": "Ushbu o'zgarish uchun sabab talab qilinadi.",
        "ru": "Для этого изменения требуется причина.",
    },

    # ---- PlaceGroupPanel: audit-history event types -------------------------
    # CustomerLinkEvent.event_type values, rendered in the place-group history
    # list. Dotted below the `event.` segment so they cannot collide with the
    # panel's flat `ui.users.place_groups.<name>` vocabulary.
    "ui.users.place_groups.event.create_place_group": {
        "en": "Place group created",
        "uz": "Joy guruhi yaratildi",
        "ru": "Группа адресов создана",
    },
    "ui.users.place_groups.event.add_to_place_group": {
        "en": "Address added to the place group",
        "uz": "Manzil joy guruhiga qo'shildi",
        "ru": "Адрес добавлен в группу адресов",
    },
    "ui.users.place_groups.event.remove_from_place_group": {
        "en": "Address removed from the place group",
        "uz": "Manzil joy guruhidan chiqarildi",
        "ru": "Адрес удалён из группы адресов",
    },
    "ui.users.place_groups.event.dismiss_place_suggestion": {
        "en": "Same-place suggestion dismissed",
        "uz": "Bir xil joy taklifi rad etildi",
        "ru": "Предложение об одном месте отклонено",
    },
    "ui.users.place_groups.event.link": {
        "en": "Accounts linked",
        "uz": "Hisoblar bog'landi",
        "ru": "Аккаунты связаны",
    },
    "ui.users.place_groups.event.unlink": {
        "en": "Accounts unlinked",
        "uz": "Hisoblar ajratildi",
        "ru": "Аккаунты отвязаны",
    },
    "ui.users.place_groups.event.dismiss": {
        "en": "Marked as different customers",
        "uz": "Har xil mijoz sifatida belgilandi",
        "ru": "Отмечены как разные клиенты",
    },

    # ---- Orders: cash-collection scope tags (utils/cashScopeDisplay.js) ----
    "ui.orders.scope_place": {
        "en": "Place collection",
        "uz": "Manzil bo'yicha yig'im",
        "ru": "Сбор по адресу",
    },
    "ui.orders.scope_cluster": {
        "en": "Linked-accounts collection",
        "uz": "Bog'langan hisoblar bo'yicha yig'im",
        "ru": "Сбор по связанным аккаунтам",
    },
    "ui.orders.timeline_scope": {
        "en": "Scope",
        "uz": "Qamrov",
        "ru": "Область",
    },

    # ---- Orders: collected-cash edit modal ---------------------------------
    "ui.orders.cash_edit_scope_place": {
        "en": "Settles this place's oldest unpaid order first",
        "uz": "Avval ushbu manzildagi eng eski to'lanmagan buyurtmani yopadi",
        "ru": "Сначала погашает самый старый неоплаченный заказ по этому адресу",
    },
    "ui.orders.cash_edit_scope_cluster": {
        "en": "Settles the customer's (and linked accounts') oldest unpaid order first",
        "uz": "Avval mijozning (va bog'langan hisoblarning) eng eski to'lanmagan buyurtmasini yopadi",
        "ru": "Сначала погашает самый старый неоплаченный заказ клиента (и связанных аккаунтов)",
    },
    "ui.orders.cash_warning_no_delivery_timestamp": {
        "en": "No delivery timestamp on this order — the correction window is treated as unlimited",
        "uz": "Bu buyurtmada yetkazib berish vaqti yo'q — tuzatish oynasi cheklanmagan deb hisoblanadi",
        "ru": "У этого заказа нет времени доставки — окно корректировки считается неограниченным",
    },
    "ui.orders.cash_warning_below_total": {
        "en": "The order will not be fully paid — loyalty may need manual review",
        "uz": "Buyurtma to'liq to'lanmaydi — sodiqlik dasturini qo'lda tekshirish kerak bo'lishi mumkin",
        "ru": "Заказ не будет оплачен полностью — программу лояльности может потребоваться проверить вручную",
    },
    "ui.orders.cash_warning_settled_elsewhere": {
        "en": (
            "This order is already paid from another source (card transfer or prepaid credit), "
            "so nothing applies to it and the whole amount becomes customer credit"
        ),
        "uz": (
            "Bu buyurtma boshqa manbadan (karta o'tkazmasi yoki oldindan to'lov krediti) to'langan, "
            "shuning uchun unga hech narsa qo'llanilmaydi va butun summa mijoz krediti bo'ladi"
        ),
        "ru": (
            "Этот заказ уже оплачен из другого источника (перевод на карту или предоплаченный кредит), "
            "поэтому к нему ничего не применяется и вся сумма становится кредитом клиента"
        ),
    },
    "ui.orders.cash_warning_spill": {
        "en": (
            "Extra cash settles the scope's oldest unpaid order first — that can be a linked "
            "account's or a coworker's debt, so the per-order figures are approximate"
        ),
        "uz": (
            "Ortiqcha naqd pul avval qamrovdagi eng eski to'lanmagan buyurtmani yopadi — bu "
            "bog'langan hisobning yoki hamkasbning qarzi bo'lishi mumkin, shuning uchun buyurtma "
            "bo'yicha raqamlar taxminiy"
        ),
        "ru": (
            "Излишек наличных сначала погашает самый старый неоплаченный заказ в этой области — "
            "это может быть долг связанного аккаунта или коллеги, поэтому суммы по заказам "
            "приблизительны"
        ),
    },
    "ui.orders.cash_warning_surplus": {
        "en": "Surplus becomes the customer's prepaid credit (shared across linked accounts)",
        "uz": "Ortiqcha summa mijozning oldindan to'lov krediti bo'ladi (bog'langan hisoblar uchun umumiy)",
        "ru": "Излишек становится предоплаченным кредитом клиента (общим для связанных аккаунтов)",
    },
    "ui.orders.cash_warning_cap": {
        "en": (
            "This correction puts the customer (or their workplace) back at the COD debt cap — "
            "they will not be able to order cash-on-delivery until it is paid down"
        ),
        "uz": (
            "Bu tuzatish mijozni (yoki uning ish joyini) naqd qarz chegarasiga qaytaradi — qarz "
            "kamaytirilmaguncha u yetkazib berishda naqd to'lash bilan buyurtma bera olmaydi"
        ),
        "ru": (
            "Эта корректировка возвращает клиента (или его рабочий адрес) к лимиту долга наличными — "
            "заказать с оплатой наличными будет нельзя, пока долг не уменьшат"
        ),
    },

    # ---- Users: COD statement card cluster/place context -------------------
    "ui.users.cluster_outstanding": {
        "en": "Across linked accounts",
        "uz": "Bog'langan hisoblar bo'yicha",
        "ru": "По связанным аккаунтам",
    },
    "ui.users.places_heading": {
        "en": "Shared places",
        "uz": "Umumiy manzillar",
        "ru": "Общие адреса",
    },
    "ui.users.place_unnamed": {
        "en": "Place",
        "uz": "Manzil",
        "ru": "Адрес",
    },
    "ui.users.place_active_cod_debts": {
        "en": "active COD debts",
        "uz": "faol naqd qarzlar",
        "ru": "активных долгов наличными",
    },

    # ---- Prepayments: cluster-wide balance tag -----------------------------
    "ui.prepayments.linked_accounts": {
        "en": "linked accounts",
        "uz": "bog'langan hisoblar",
        "ru": "связанные аккаунты",
    },

    # ---- GroupedAddressesPanel: the estate-wide "Grouped Addresses" tab -----
    # The third tab on the Users page (plan E, A1.3). `PlaceGroupPanel` above
    # answers "what does THIS customer share?" from inside the details drawer;
    # these keys belong to the other door — every group that exists plus the
    # co-located candidates found across the whole estate. The ACTIONS on that
    # tab deliberately reuse the `place_groups.*` keys above (one confirm flow,
    # one vocabulary), so only the tab's own chrome is new here.
    "ui.users.grouped_addresses.tab": {
        "en": "Grouped Addresses",
        "uz": "Guruhlangan manzillar",
        "ru": "Сгруппированные адреса",
    },
    "ui.users.grouped_addresses.groups_title": {
        "en": "Existing place groups",
        "uz": "Mavjud manzil guruhlari",
        "ru": "Существующие группы адресов",
    },
    "ui.users.grouped_addresses.suggestions_title": {
        "en": "Suggested candidates",
        "uz": "Taklif qilingan nomzodlar",
        "ru": "Предлагаемые кандидаты",
    },
    "ui.users.grouped_addresses.search_placeholder": {
        "en": "Search by place label",
        "uz": "Guruh nomi bo'yicha qidirish",
        "ru": "Поиск по названию группы",
    },
    "ui.users.grouped_addresses.no_groups": {
        "en": "No place groups yet",
        "uz": "Hali manzil guruhlari yo'q",
        "ru": "Групп адресов пока нет",
    },
    "ui.users.grouped_addresses.no_suggestions": {
        "en": "No suggested candidates",
        "uz": "Taklif qilingan nomzodlar yo'q",
        "ru": "Предлагаемых кандидатов нет",
    },
    # Spec §2.1: auto-grouping fails dangerously in seven distinct ways, so the
    # tab states in words what its missing "accept all" button states by absence.
    "ui.users.grouped_addresses.no_auto_group_hint": {
        "en": (
            "Suggestions are never grouped automatically. "
            "Each one needs an admin confirmation with a reason."
        ),
        "uz": (
            "Takliflar hech qachon avtomatik guruhlanmaydi. "
            "Har biri uchun admin sababni ko'rsatib tasdiqlashi kerak."
        ),
        "ru": (
            "Предложения никогда не группируются автоматически. "
            "Каждое требует подтверждения администратора с указанием причины."
        ),
    },
    "ui.users.grouped_addresses.col_place": {
        "en": "Place",
        "uz": "Manzil",
        "ru": "Адрес",
    },
    # Distinct address OWNERS, not addresses — the same definition
    # `get_place_cod_statement` uses. `col_addresses` is the separate figure.
    "ui.users.grouped_addresses.col_members": {
        "en": "Customers at this place",
        "uz": "Ushbu manzildagi mijozlar",
        "ru": "Клиенты по этому адресу",
    },
    "ui.users.grouped_addresses.col_addresses": {
        "en": "Addresses",
        "uz": "Manzillar",
        "ru": "Адреса",
    },
    "ui.users.grouped_addresses.col_cod_total": {
        "en": "Open COD debt",
        "uz": "Ochiq naqd qarz",
        "ru": "Открытый долг наличными",
    },
    "ui.users.grouped_addresses.col_cod_count": {
        "en": "Unpaid COD orders",
        "uz": "To'lanmagan naqd buyurtmalar",
        "ru": "Неоплаченные заказы наличными",
    },
    "ui.users.grouped_addresses.col_candidate": {
        "en": "Possible same place",
        "uz": "Ehtimoliy bir xil manzil",
        "ru": "Возможно один и тот же адрес",
    },
    "ui.users.grouped_addresses.col_distinct_customers": {
        "en": "Distinct customers",
        "uz": "Alohida mijozlar",
        "ru": "Уникальные клиенты",
    },
    "ui.users.grouped_addresses.col_actions": {
        "en": "Actions",
        "uz": "Amallar",
        "ru": "Действия",
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
