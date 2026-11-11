# scripts/seed_shared_place_ui_translations.py
"""Seed the admin-UI copy of the Users page's "Driver reports" tab (shared-place cases).

category='ui' with DOTTED `ui.users.shared_place_reports.*` keys, the
scripts/seed_place_group_ui_translations.py mechanism: AdminUiTranslationService
resolves `ui.users.*` into the `users` i18next namespace via
LEGACY_NAMESPACE_PREFIXES, which requires category == 'ui'.

This script owns ONLY `ui.users.shared_place_reports.*`. The tab's other copy is
already seeded elsewhere: `ui.common.confirm` / `ui.common.cancel` /
`ui.common.loading` (scripts/seed_backend_translations.py) and the reused
`ui.users.place_groups.*` and `ui.users.linked_accounts.*` keys (their own
seeds). `translations` is unique on (key, language) and a later seed re-assigns
the row's category, so one key has one owning script.

Every call site passes an inline English fallback, so the tab renders in
English BEFORE this seed lands; the `en` values below reproduce those fallbacks
byte for byte, and tests/unit/test_shared_place_ui_translation_seed.py fails on
any drift, on a seeded key nobody reads, on a rendered key nobody seeds, and on
a `{{placeholder}}` that differs between languages.

Run inside the business_app container (scripts/ is NOT volume-mounted, so pipe
the file in over stdin; piped stdin still executes as __main__):
    docker compose exec -T business_app python - < scripts/seed_shared_place_ui_translations.py
Then restart business_app so the translations API serves them:
    docker compose restart business_app
"""

from business_app import create_app
from business_app.models.translation import Translation

CATEGORY = "ui"

# {key: {language: value}}; `en` MUST match the inline t(key, 'fallback') string exactly.
KEYS = {
    # ---- Users.js: the tab -------------------------------------------------------------------------------------
    "ui.users.shared_place_reports.tab": {
        "en": "Driver reports",
        "uz": "Haydovchi xabarlari",
        "ru": "Отчёты водителей",
    },
    # ---- SharedPlaceReportsPanel: the list ---------------------------------------------------------------------
    "ui.users.shared_place_reports.filter_open": {
        "en": "Open",
        "uz": "Ochiq",
        "ru": "Открытые",
    },
    "ui.users.shared_place_reports.col_case": {
        "en": "Case",
        "uz": "Ish",
        "ru": "Случай",
    },
    "ui.users.shared_place_reports.col_opened": {
        "en": "Opened",
        "uz": "Ochilgan",
        "ru": "Открыт",
    },
    "ui.users.shared_place_reports.col_order": {
        "en": "Order",
        "uz": "Buyurtma",
        "ru": "Заказ",
    },
    "ui.users.shared_place_reports.col_customer": {
        "en": "Customer",
        "uz": "Mijoz",
        "ru": "Клиент",
    },
    "ui.users.shared_place_reports.col_address": {
        "en": "Address",
        "uz": "Manzil",
        "ru": "Адрес",
    },
    "ui.users.shared_place_reports.col_returned": {
        "en": "Empties handed back",
        "uz": "Qaytarilgan bo'sh idishlar",
        "ru": "Сдано пустых",
    },
    "ui.users.shared_place_reports.col_driver": {
        "en": "Driver",
        "uz": "Haydovchi",
        "ru": "Водитель",
    },
    "ui.users.shared_place_reports.col_status": {
        "en": "Status",
        "uz": "Holat",
        "ru": "Статус",
    },
    "ui.users.shared_place_reports.status_awaiting_driver": {
        "en": "Waiting for the driver",
        "uz": "Haydovchi kutilmoqda",
        "ru": "Ждём водителя",
    },
    "ui.users.shared_place_reports.status_awaiting_admin": {
        "en": "Waiting for an admin",
        "uz": "Administrator kutilmoqda",
        "ru": "Ждёт администратора",
    },
    "ui.users.shared_place_reports.status_resolved": {
        "en": "Resolved",
        "uz": "Hal qilingan",
        "ru": "Решён",
    },
    "ui.users.shared_place_reports.status_closed": {
        "en": "Closed",
        "uz": "Yopilgan",
        "ru": "Закрыт",
    },
    "ui.users.shared_place_reports.empty": {
        "en": "No driver reports",
        "uz": "Haydovchi xabarlari yo'q",
        "ru": "Отчётов водителей нет",
    },
    "ui.users.shared_place_reports.case_title": {
        "en": "Case #{{id}}",
        "uz": "Ish #{{id}}",
        "ru": "Случай №{{id}}",
    },
    # ---- SharedPlaceCaseDetail: what happened ------------------------------------------------------------------
    "ui.users.shared_place_reports.load_failed": {
        "en": "Could not load this case.",
        "uz": "Bu ishni yuklab bo'lmadi.",
        "ru": "Не удалось загрузить случай.",
    },
    "ui.users.shared_place_reports.what_happened": {
        "en": "What happened",
        "uz": "Nima bo'ldi",
        "ru": "Что произошло",
    },
    "ui.users.shared_place_reports.order": {
        "en": "Order",
        "uz": "Buyurtma",
        "ru": "Заказ",
    },
    "ui.users.shared_place_reports.customer": {
        "en": "Customer",
        "uz": "Mijoz",
        "ru": "Клиент",
    },
    "ui.users.shared_place_reports.delivered": {
        "en": "Delivered",
        "uz": "Yetkazildi",
        "ru": "Доставлено",
    },
    "ui.users.shared_place_reports.delivered_by": {
        "en": "{{when}} by {{driver}}",
        "uz": "{{when}}, haydovchi {{driver}}",
        "ru": "{{when}}, водитель {{driver}}",
    },
    "ui.users.shared_place_reports.delivered_by_admin": {
        "en": "{{when}}, entered by an admin",
        "uz": "{{when}}, administrator kiritgan",
        "ru": "{{when}}, внесено администратором",
    },
    "ui.users.shared_place_reports.counts": {
        "en": "got {{delivered}} full, handed back {{returned}} empties; this address had {{before}} on record before",
        "uz": (
            "{{delivered}} ta to'la oldi, {{returned}} ta bo'sh idish qaytardi; "
            "bu manzilda avval {{before}} ta qayd etilgan edi"
        ),
        "ru": "получил полных: {{delivered}}, сдал пустых: {{returned}}; до этого за адресом числилось {{before}}",
    },
    # ---- what the driver said ------------------------------------------------------------------------------------
    "ui.users.shared_place_reports.driver_said": {
        "en": "What the driver said",
        "uz": "Haydovchi nima dedi",
        "ru": "Что сказал водитель",
    },
    "ui.users.shared_place_reports.driver_picked": {
        "en": "Picked: {{name}} · {{empties}} empties · {{metres}} m",
        "uz": "Tanladi: {{name}} · {{empties}} ta bo'sh idish · {{metres}} m",
        "ru": "Выбрал: {{name}} · пустых: {{empties}} · {{metres}} м",
    },
    "ui.users.shared_place_reports.driver_picked_short": {
        "en": "Picked a place",
        "uz": "Joyni tanladi",
        "ru": "Выбрал место",
    },
    "ui.users.shared_place_reports.driver_none": {
        "en": "None of these",
        "uz": "Hech biri emas",
        "ru": "Ни одно из них",
    },
    # The note-only screen's "none": no place was offered, so none was turned down (spec §4.4).
    "ui.users.shared_place_reports.driver_no_candidates": {
        "en": "No nearby places",
        "uz": "Yaqinda joy topilmadi",
        "ru": "Мест поблизости нет",
    },
    "ui.users.shared_place_reports.driver_skipped": {
        "en": "Skipped",
        "uz": "O'tkazib yubordi",
        "ru": "Пропустил",
    },
    "ui.users.shared_place_reports.driver_not_asked": {
        "en": "Entered by an admin",
        "uz": "Administrator kiritgan",
        "ru": "Внесено администратором",
    },
    "ui.users.shared_place_reports.driver_awaiting": {
        "en": "Waiting for the answer",
        "uz": "Javob kutilmoqda",
        "ru": "Ждём ответа",
    },
    "ui.users.shared_place_reports.driver_no_answer": {
        "en": "Didn't answer",
        "uz": "Javob bermadi",
        "ru": "Не ответил",
    },
    "ui.users.shared_place_reports.driver_note": {
        "en": "Driver's note",
        "uz": "Haydovchi izohi",
        "ru": "Заметка водителя",
    },
    # ---- where ---------------------------------------------------------------------------------------------------
    "ui.users.shared_place_reports.where": {
        "en": "Where",
        "uz": "Qayerda",
        "ru": "Где",
    },
    "ui.users.shared_place_reports.apartment": {
        "en": "Apartment {{number}}",
        "uz": "Xonadon {{number}}",
        "ru": "Квартира {{number}}",
    },
    "ui.users.shared_place_reports.offered": {
        "en": "Places offered to the driver",
        "uz": "Haydovchiga taklif qilingan joylar",
        "ru": "Места, предложенные водителю",
    },
    "ui.users.shared_place_reports.candidate": {
        "en": "{{name}} · {{empties}} empties · {{metres}} m",
        "uz": "{{name}} · {{empties}} ta bo'sh idish · {{metres}} m",
        "ru": "{{name}} · пустых: {{empties}} · {{metres}} м",
    },
    "ui.users.shared_place_reports.candidate_gone": {
        "en": "No longer exists",
        "uz": "Endi mavjud emas",
        "ru": "Больше не существует",
    },
    "ui.users.shared_place_reports.candidate_now_place": {
        "en": "Now the place '{{label}}' · {{empties}} empties",
        "uz": "Hozir '{{label}}' joyi · {{empties}} ta bo'sh idish",
        "ru": "Сейчас место «{{label}}» · пустых: {{empties}}",
    },
    "ui.users.shared_place_reports.candidate_now": {
        "en": "Now {{empties}} empties",
        "uz": "Hozir {{empties}} ta bo'sh idish",
        "ru": "Сейчас пустых: {{empties}}",
    },
    "ui.users.shared_place_reports.unnamed_place": {
        "en": "Unnamed place",
        "uz": "Nomsiz joy",
        "ru": "Место без названия",
    },
    "ui.users.shared_place_reports.empty_no_pin": {
        "en": "This address has no map pin, so no nearby places were looked up.",
        "uz": "Bu manzilda xaritadagi belgi yo'q, shuning uchun yaqin joylar qidirilmadi.",
        "ru": "У этого адреса нет точки на карте, поэтому места поблизости не искали.",
    },
    "ui.users.shared_place_reports.empty_none_within_radius": {
        "en": "No other customer keeps empties close to this address.",
        "uz": "Bu manzil yaqinida boshqa hech bir mijozda bo'sh idish yo'q.",
        "ru": "Рядом с этим адресом ни у одного другого клиента нет пустых бутылей.",
    },
    "ui.users.shared_place_reports.empty_none_eligible": {
        "en": "The places nearby belong to business, shop or staff accounts, which are never offered.",
        "uz": (
            "Yaqin atrofdagi joylar korxona, do'kon yoki xodim hisoblariga tegishli, "
            "ular hech qachon taklif qilinmaydi."
        ),
        "ru": "Места поблизости принадлежат компаниям, магазинам или сотрудникам, их никогда не предлагают.",
    },
    "ui.users.shared_place_reports.empty_none": {
        "en": "No places were offered.",
        "uz": "Hech qanday joy taklif qilinmadi.",
        "ru": "Места не предлагались.",
    },
    "ui.users.shared_place_reports.search": {
        "en": "Another place",
        "uz": "Boshqa joy",
        "ru": "Другое место",
    },
    "ui.users.shared_place_reports.search_placeholder": {
        "en": "Find an address by phone, name or address",
        "uz": "Manzilni telefon, ism yoki manzil bo'yicha toping",
        "ru": "Найдите адрес по телефону, имени или адресу",
    },
    "ui.users.shared_place_reports.search_none": {
        "en": "No addresses found",
        "uz": "Manzillar topilmadi",
        "ru": "Адреса не найдены",
    },
    "ui.users.shared_place_reports.hit_place": {
        "en": "The place '{{label}}'",
        "uz": "'{{label}}' joyi",
        "ru": "Место «{{label}}»",
    },
    # ---- live facts ----------------------------------------------------------------------------------------------
    "ui.users.shared_place_reports.facts": {
        "en": "Read now",
        "uz": "Hozirgi holat",
        "ru": "Сейчас",
    },
    "ui.users.shared_place_reports.fact_linked": {
        "en": "Already linked to {{names}}",
        "uz": "Allaqachon bog'langan: {{names}}",
        "ru": "Уже связан с: {{names}}",
    },
    "ui.users.shared_place_reports.fact_referred": {
        "en": "Referred by {{name}}",
        "uz": "Tavsiya qilgan: {{name}}",
        "ru": "Пригласил: {{name}}",
    },
    "ui.users.shared_place_reports.fact_grouped": {
        "en": "This address has since been grouped into '{{label}}'",
        "uz": "Bu manzil keyinchalik '{{label}}' joyiga guruhlangan",
        "ru": "Этот адрес позже объединён в место «{{label}}»",
    },
    "ui.users.shared_place_reports.tryout_outstanding": {
        "en": "{{n}} trial bottles still out",
        "uz": "{{n}} ta sinov idishi hali qaytarilmagan",
        "ru": "Пробных бутылей ещё не возвращено: {{n}}",
    },
    "ui.users.shared_place_reports.tryout_hint": {
        "en": (
            'Settle the trial bottles on the try-out, then choose "Customer\'s own bottles" (+{{n}}) '
            "so this address isn't short."
        ),
        "uz": (
            "Avval sinov idishlarini sinov sahifasida hisob-kitob qiling, so'ng bu manzil kam ko'rinmasligi uchun "
            '"Mijozning o\'z idishlari" (+{{n}}) ni tanlang.'
        ),
        "ru": (
            "Сначала закройте пробные бутыли на странице пробы, затем выберите «Собственные бутыли клиента» "
            "(+{{n}}), чтобы у этого адреса не было недостачи."
        ),
    },
    # ---- the decision --------------------------------------------------------------------------------------------
    "ui.users.shared_place_reports.decision": {
        "en": "Decision",
        "uz": "Qaror",
        "ru": "Решение",
    },
    "ui.users.shared_place_reports.choice_shared_place": {
        "en": "Shared place — different people, one place",
        "uz": "Umumiy joy — turli odamlar, bitta joy",
        "ru": "Общее место — разные люди, одно место",
    },
    "ui.users.shared_place_reports.choice_same_home": {
        "en": "Same person, same home",
        "uz": "Bir odam, bir uy",
        "ru": "Один человек, тот же дом",
    },
    "ui.users.shared_place_reports.choice_other_place": {
        "en": "Same person, a different place",
        "uz": "Bir odam, boshqa joy",
        "ru": "Один человек, другое место",
    },
    "ui.users.shared_place_reports.choice_own_bottles": {
        "en": "Customer's own bottles",
        "uz": "Mijozning o'z idishlari",
        "ru": "Собственные бутыли клиента",
    },
    "ui.users.shared_place_reports.choice_wrong_count": {
        "en": "Wrong count",
        "uz": "Noto'g'ri son",
        "ru": "Неверное количество",
    },
    "ui.users.shared_place_reports.choice_close": {
        "en": "Close, no action",
        "uz": "Yopish, hech narsa qilmasdan",
        "ru": "Закрыть без действий",
    },
    "ui.users.shared_place_reports.confirm_shared_place": {
        "en": "Confirm: shared place '{{label}}'",
        "uz": "Tasdiqlash: umumiy joy '{{label}}'",
        "ru": "Подтвердить: общее место «{{label}}»",
    },
    "ui.users.shared_place_reports.confirm_same_home": {
        "en": "Confirm: same person, same home",
        "uz": "Tasdiqlash: bir odam, bir uy",
        "ru": "Подтвердить: один человек, тот же дом",
    },
    "ui.users.shared_place_reports.needs_target": {
        "en": "Pick a place or another account above to use the greyed-out choices.",
        "uz": "Kulrang tanlovlardan foydalanish uchun yuqorida joy yoki boshqa hisobni tanlang.",
        "ru": "Чтобы воспользоваться серыми вариантами, выберите выше место или другой аккаунт.",
    },
    "ui.users.shared_place_reports.refusal_not_verified": {
        "en": "The grouping or link this choice needs is not in place now. Nothing was recorded.",
        "uz": "Bu tanlov uchun kerakli guruhlash yoki bog'lash hozir mavjud emas. Hech narsa saqlanmadi.",
        "ru": "Нужной для этого выбора группировки или связи сейчас нет. Ничего не сохранено.",
    },
    "ui.users.shared_place_reports.refusal_invalid": {
        "en": "This choice cannot be recorded as entered. Nothing was recorded; check the numbers and try again.",
        "uz": (
            "Bu tanlovni kiritilganidek saqlab bo'lmaydi. Hech narsa saqlanmadi; "
            "raqamlarni tekshirib, qayta urinib ko'ring."
        ),
        "ru": "Этот выбор нельзя сохранить в таком виде. Ничего не сохранено; проверьте числа и попробуйте снова.",
    },
    "ui.users.shared_place_reports.refusal_conflict": {
        "en": "Another outcome is already recorded for this case. Nothing was changed.",
        "uz": "Bu ish uchun boshqa natija allaqachon saqlangan. Hech narsa o'zgartirilmadi.",
        "ru": "Для этого случая уже сохранён другой итог. Ничего не изменено.",
    },
    "ui.users.shared_place_reports.resolved": {
        "en": "Case resolved",
        "uz": "Ish hal qilindi",
        "ru": "Случай решён",
    },
    "ui.users.shared_place_reports.outcome": {
        "en": "Outcome",
        "uz": "Natija",
        "ru": "Итог",
    },
    "ui.users.shared_place_reports.reason": {
        "en": "Reason",
        "uz": "Sabab",
        "ru": "Причина",
    },
    "ui.users.shared_place_reports.decided": {
        "en": "Decided",
        "uz": "Qaror qilingan",
        "ru": "Решено",
    },
    "ui.users.shared_place_reports.decided_by": {
        "en": "{{name}}, {{when}}",
        "uz": "{{name}}, {{when}}",
        "ru": "{{name}}, {{when}}",
    },
    # ---- SharedPlaceAdjustConfirm: the case-owned confirmation ---------------------------------------------------
    "ui.users.shared_place_reports.claim_hint": {
        "en": "This outcome was started earlier. Confirm it again to finish it.",
        "uz": "Bu natija avvalroq boshlangan. Uni yakunlash uchun yana tasdiqlang.",
        "ru": "Этот итог начат ранее. Подтвердите его ещё раз, чтобы завершить.",
    },
    "ui.users.shared_place_reports.own_bottles_here": {
        "en": "+{{n}} at this address",
        "uz": "Bu manzilga +{{n}}",
        "ru": "+{{n}} по этому адресу",
    },
    "ui.users.shared_place_reports.own_bottles_place": {
        "en": "+{{n}} at the place '{{label}}'",
        "uz": "'{{label}}' joyiga +{{n}}",
        "ru": "+{{n}} в месте «{{label}}»",
    },
    # E asks for the empties actually handed back (the resolve books `returned_qty - actual_returned`), never the
    # place's balance.
    "ui.users.shared_place_reports.wrong_count_question": {
        "en": "The driver typed {{typed}} empties handed back. How many were actually handed back?",
        "uz": "Haydovchi {{typed}} ta bo'sh idish qaytarilgan deb kiritgan. Haqiqatda nechtasi qaytarildi?",
        "ru": "Водитель ввёл, что сдано пустых: {{typed}}. Сколько сдано на самом деле?",
    },
    "ui.users.shared_place_reports.wrong_count_booked_here": {
        "en": "The difference is booked at this address.",
        "uz": "Farq shu manzilga yoziladi.",
        "ru": "Разница записывается на этот адрес.",
    },
    "ui.users.shared_place_reports.wrong_count_booked_place": {
        "en": "The difference is booked at the place '{{label}}'.",
        "uz": "Farq '{{label}}' joyiga yoziladi.",
        "ru": "Разница записывается на место «{{label}}».",
    },
    "ui.users.shared_place_reports.actual_returned_required": {
        "en": "Enter the number of empties actually handed back",
        "uz": "Haqiqatda qaytarilgan bo'sh idishlar sonini kiriting",
        "ru": "Укажите, сколько пустых бутылей сдано на самом деле",
    },
    "ui.users.shared_place_reports.actual_returned_min": {
        "en": "Enter 0 or more",
        "uz": "0 yoki undan ko'p son kiriting",
        "ru": "Укажите 0 или больше",
    },
    "ui.users.shared_place_reports.actual_returned_same": {
        "en": "That is the number the driver typed. Enter the number actually handed back.",
        "uz": "Bu haydovchi kiritgan son. Haqiqatda qaytarilgan sonni kiriting.",
        "ru": "Это число ввёл водитель. Укажите, сколько сдано на самом деле.",
    },
    "ui.users.shared_place_reports.close_text": {
        "en": "The case is closed and nothing else changes.",
        "uz": "Ish yopiladi va boshqa hech narsa o'zgarmaydi.",
        "ru": "Случай закрывается, больше ничего не меняется.",
    },
    "ui.users.shared_place_reports.grouped_text": {
        "en": "The grouping is already done; this records the decision.",
        "uz": "Guruhlash allaqachon bajarilgan; bu faqat qarorni qayd etadi.",
        "ru": "Группировка уже сделана; это только фиксирует решение.",
    },
    "ui.users.shared_place_reports.move_facts": {
        "en": "handed back {{returned}} today; this address now shows {{now}}; {{source}} shows {{k}}",
        "uz": "bugun {{returned}} ta qaytardi; bu manzilda hozir {{now}}; {{source}}da {{k}}",
        "ru": "сегодня сдал {{returned}}; у этого адреса сейчас {{now}}; у {{source}}: {{k}}",
    },
    "ui.users.shared_place_reports.move_facts_here": {
        "en": "handed back {{returned}} today; this address now shows {{now}}",
        "uz": "bugun {{returned}} ta qaytardi; bu manzilda hozir {{now}}",
        "ru": "сегодня сдал {{returned}}; у этого адреса сейчас {{now}}",
    },
    "ui.users.shared_place_reports.move_not_allowed": {
        "en": "This address is already in a place group, so no empties can move here.",
        "uz": "Bu manzil allaqachon joy guruhida, shuning uchun bu yerga bo'sh idish ko'chirib bo'lmaydi.",
        "ru": "Этот адрес уже в группе места, поэтому переносить сюда пустые нельзя.",
    },
    "ui.users.shared_place_reports.move_none": {
        "en": "No other place of the linked accounts holds empties, so nothing can move.",
        "uz": (
            "Bog'langan hisoblarning boshqa hech bir joyida bo'sh idish yo'q, "
            "shuning uchun hech narsa ko'chirilmaydi."
        ),
        "ru": "Ни в одном другом месте связанных аккаунтов нет пустых, поэтому переносить нечего.",
    },
    "ui.users.shared_place_reports.move_source": {
        "en": "Move empties from",
        "uz": "Bo'sh idishlar qayerdan ko'chiriladi",
        "ru": "Откуда перенести пустые",
    },
    "ui.users.shared_place_reports.move_source_option": {
        "en": "{{source}} · {{k}} empties",
        "uz": "{{source}} · {{k}} ta bo'sh idish",
        "ru": "{{source}} · пустых: {{k}}",
    },
    "ui.users.shared_place_reports.move_source_required": {
        "en": "Choose the place the empties move from",
        "uz": "Bo'sh idishlar ko'chiriladigan joyni tanlang",
        "ru": "Выберите место, откуда переносятся пустые",
    },
    "ui.users.shared_place_reports.move_count_range": {
        "en": "Enter a number from 1 to {{k}}",
        "uz": "1 dan {{k}} gacha son kiriting",
        "ru": "Введите число от 1 до {{k}}",
    },
    "ui.users.shared_place_reports.move_count_placeholder": {
        "en": "Empties to move (leave empty to link only)",
        "uz": "Ko'chiriladigan bo'sh idishlar (faqat bog'lash uchun bo'sh qoldiring)",
        "ru": "Сколько пустых перенести (оставьте пустым, чтобы только связать)",
    },
    "ui.users.shared_place_reports.place_number": {
        "en": "Place #{{id}}",
        "uz": "Joy #{{id}}",
        "ru": "Место №{{id}}",
    },
    "ui.users.shared_place_reports.reason_placeholder": {
        "en": "Reason (required)",
        "uz": "Sabab (majburiy)",
        "ru": "Причина (обязательно)",
    },
    "ui.users.shared_place_reports.reason_required": {
        "en": "Enter a reason",
        "uz": "Sababni kiriting",
        "ru": "Укажите причину",
    },
    # ---- PlaceGroupConfirmModal: the place note (its field opts in from the case screen) -------------------------
    "ui.users.shared_place_reports.place_note": {
        "en": "Place note (optional)",
        "uz": "Joy izohi (ixtiyoriy)",
        "ru": "Заметка о месте (необязательно)",
    },
    "ui.users.shared_place_reports.place_note_hint": {
        "en": "Visible to admins on this place's card; drivers and customers never see it.",
        "uz": "Faqat administratorlar shu joy kartasida ko'radi; haydovchilar va mijozlar hech qachon ko'rmaydi.",
        "ru": "Видна администраторам в карточке этого места; водители и клиенты её никогда не видят.",
    },
    # ---- PlaceGroupCard: the place note, read-only for every viewer of the card ----------------------------------
    "ui.users.shared_place_reports.place_note_label": {
        "en": "Note",
        "uz": "Izoh",
        "ru": "Заметка",
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
