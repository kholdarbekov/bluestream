"""Seed the admin-UI `sales_agents` i18next namespace (category='ui_sales_agents').

The admin UI serves the `sales_agents` namespace from Translation rows with
category='ui_sales_agents' and BARE keys (`title`, not `sales_agents:title`) —
see AdminUiTranslationService._get_scoped_namespace_records, which maps an
unlisted namespace to `ui_<namespace>`. These rows are therefore NOT seeded by
scripts/seed_backend_translations.py (that script stores dotted `ui.*` keys in
the shared `ui` category).

English values here MUST equal the inline `t(key, 'default')` strings in
admin_ui/src/pages/SalesAgents.js, admin_ui/src/pages/Outlets.js and
admin_ui/src/components/sales/ exactly.
A mismatch is silent: the seeded text wins in every language, so an English
session shows one wording and the source another. The one carve-out is
`employment_*`, whose call site passes the raw enum value as its default
(``t(`sales_agents:employment_${v}`, v)`` → "employee"), so its English rows
are the intended labels "Employee"/"Contractor" and deliberately differ from
the default.

Two families are rendered through maps and cannot be found by grepping for a
literal default:
- `stage_*`  — `t(`sales_agents:stage_${s}`, STAGE_LABELS[s])` in Outlets.js.
  All seven stages of `OUTLET_STAGES` (business_app/models/sales.py) are here.
  The uz/ru wording is copied from `stage.*` in
  scripts/seed_staff_translations.py so the staff bot and the admin UI name the
  same pipeline the same way.
- `employment_*` — over `EMPLOYMENT_TYPES` (mirrors business_app/models/sales.py).
- `exceptions.type.*` — ``t(`sales_agents:exceptions.type.${value}`, value)`` in Visits.js,
  whose default is the raw feed type. Same carve-out as `employment_*`: the English rows
  here are the intended labels and deliberately differ from that default. All seven of
  `ExceptionFeedService.EXCEPTION_TYPES` are present.
- `outlets.payment_terms.*`, `contacts.role.*`, `photos.kind.*` — the same raw-value default,
  in admin_ui/src/components/sales/ (OutletEditModal, OutletContactsTab, VisitPhotoThumb), over
  `PAYMENT_TERMS`, `CONTACT_ROLES` (business_app/models/sales.py) and `PHOTO_KINDS`
  (business_app/models/sales_visits.py). Same carve-out: the English rows are the labels.
A missing key in either family degrades to the English default, not to a
visible raw key, so a partial seed fails silently.

ONE OWNER PER BARE KEY — the rule stated in
scripts/seed_ui_bottle_tracking_linked_accounts.py. `translations` is unique on
``(key, language)`` ONLY and `bulk_create_or_update` REASSIGNS `category`, so a
bare key seeded from two scripts is ONE row with two claimed owners: whichever
runs last wins and the loser's namespace bundle silently drops it. Five generic
keys the sales pages use are therefore NOT seeded here — they already have an
owner, and the pages still resolve them because i18n.js sets
`fallbackNS: ['common']` and the `common` bundle is the union of every `ui_*`
category (AdminUiTranslationService.get_translations):

    status         -> seeded by seven ui_* seeds already (identical values);
                      scripts/seed_ui_bottle_tracking_translations.py et al.
    notes          -> scripts/seed_ui_bottle_tracking_translations.py
                      (also scripts/seed_ui_tryouts_translations.py, identical)
    phone          -> scripts/seed_ui_tryouts_translations.py (trilingual there; the
                      Analytics *Agent performance* tab's phone column and CSV header read
                      it through the same `common` fallback -- re-seeding it here would only
                      move the row's category out of ui_tryouts)
    district       -> scripts/seed_ui_tryouts_translations.py
    tab_overview   -> scripts/seed_ui_tryouts_translations.py

The other half of the rule is that a key which means something PAGE-SPECIFIC
must not take a generic name. `name`, `search_placeholder`, `details`,
`address`, `active` and `inactive` were renamed in the two pages to
`contact_name`, `search_agents_placeholder`, `agent_details`, `outlet_address`,
`agent_active` and `agent_inactive` for exactly that reason: a sales agent's
`Ism`/`Имя` is not a product category's `Nomi`/`Название`, and four seeds
already write four different `search_placeholder` strings.
tests/unit/test_admin_ui_seed_key_ownership.py fails on any NEW conflict.

Run inside the business_app container (scripts/ is not mounted, so pipe it in):
    docker compose exec -T business_app python - < scripts/seed_ui_sales_translations.py
"""
from business_app import create_app
from business_app.models.translation import Translation

UI_SALES_CATEGORY = "ui_sales_agents"

UI_SALES_TRANSLATIONS = {
    "en": {
        # --- Sales agents page ---
        "title": "Sales agents",
        "add_sales_agent": "Add sales agent",
        "edit_sales_agent": "Edit sales agent",
        "total_agents": "Total agents",
        "active_agents": "Active agents",
        "search_agents_placeholder": "Search by name or phone",
        "agent_active": "Active",
        "agent_inactive": "Inactive",
        "contact_name": "Name",
        "email": "Email",
        "districts": "Districts",
        "outlets_assigned_active": "Outlets (assigned / active)",
        "weekly_target": "Weekly new-outlet target",
        "employment_type": "Employment",
        "telegram_linked": "Telegram linked",
        "agent_details": "Details",
        "invite": "Invite",
        "invite_link": "Invite link",
        "copy_link": "Copy link",
        "link_copied": "Link copied",
        "agent_created": "Sales agent created",
        "agent_updated": "Sales agent updated",
        "share_invite_description": (
            "Send this one-time link to the agent; it opens the staff bot and binds their Telegram account."
        ),
        # --- Outlets page ---
        "outlets_title": "Outlets",
        "outlet_name": "Outlet",
        "outlet_type": "Type",
        "stage": "Stage",
        "class": "Class",
        "agent": "Agent",
        "last_visit": "Last visit",
        "search_outlets": "Search by name or phone",
        "unvisited_days": "Unvisited ≥ days",
        "summary_is_estate_wide": "Totals across every outlet — the filters below do not narrow them.",
        "bulk_assign": "Bulk assign by district",
        "import_existing": "Import existing customers",
        "import_confirm": "Create an outlet for every grocery/workplace address that has none yet?",
        "imported": "Customers imported as outlets",
        "bulk_assigned": "Outlets assigned",
        "approve": "Approve",
        "reject": "Reject",
        "mark_lost": "Mark lost",
        "reason": "Reason",
        "outlet_approved": "Outlet activated",
        "outlet_rejected": "Request rejected",
        "outlet_assigned": "Agent assigned",
        "outlet_updated": "Outlet updated",
        "outlet_marked_lost": "Marked as lost",
        "tab_contacts": "Contacts",
        "tab_history": "History",
        "outlet_address": "Address",
        "open_receivable": "Owes",
        "bottle_balance": "Bottles at outlet",
        "dedupe_candidates": "Possible duplicates found at creation",
        # --- D25 branch outlets: the account line and the approve/attach modal ---
        "outlet_account_line": "Account: {{account}} · {{count}} branches",
        "open_receivable_account": "Owes (account)",
        "bottles_branch": "Bottles at this branch",
        "approve_modal_title": "Approve outlet",
        "attach_modal_title": "Attach to {{account}}",
        "attach_confirm": (
            "This outlet joins {{account}} as a branch — no new customer account is created."
        ),
        "contract_number_label": "Contract number",
        "role": "Role",
        "when": "When",
        "from": "From",
        "to": "To",
        # --- dynamic family: employment types (SalesAgents.js EMPLOYMENT_TYPES) ---
        "employment_employee": "Employee",
        "employment_contractor": "Contractor",
        # --- dynamic family: outlet stages (Outlets.js STAGE_LABELS) ---
        "stage_prospect": "Prospect",
        "stage_trial": "Trial",
        "stage_activation_requested": "Awaiting activation",
        "stage_active": "Active",
        "stage_at_risk": "At risk",
        "stage_dormant": "Dormant",
        "stage_lost": "Lost",
        # --- Sales agents page: today's field activity (R22) ---
        "agents.visits_today": "Visits today",
        "agents.orders_today": "Orders today",
        # --- Outlets page: the outlet's standing delivery window (C24) ---
        "outlets.delivery_window.label": "Delivery window",
        "outlets.delivery_window.start": "From",
        "outlets.delivery_window.end": "Until",
        "outlets.delivery_window.help": (
            "The default delivery window for every order placed at this outlet."
            " Clear both fields to remove it."
        ),
        "outlets.delivery_window.invalid": "Set both the start and the end, or clear both.",
        # --- Outlets drawer: the Edit form, the Contacts tab and the Photos tab (D27/D29/D30) ---
        "outlets.edit": "Edit",
        "outlets.edit_title": "Edit outlet",
        "outlets.fields.channel": "Channel",
        "outlets.fields.cadence_days_override": "Visit every (days)",
        "outlets.fields.cadence_help": "Leave empty to use the class default.",
        "outlets.fields.preferred_visit_window": "Best time to visit",
        "outlets.fields.payment_terms": "Payment terms",
        "outlets.fields.preferred_language": "Language",
        "outlets.fields.legal_form": "Legal form",
        "outlets.fields.tax_id": "Tax ID (INN)",
        "outlets.fields.competitor_note": "Competitor note",
        "outlets.fields.status_warning": "Warning for staff",
        "outlets.payment_terms.cash": "Cash",
        "outlets.payment_terms.business_account": "Business account",
        "contacts.add": "Add contact",
        "contacts.edit": "Edit",
        "contacts.edit_title": "Edit contact",
        "contacts.delete": "Delete",
        "contacts.delete_confirm": "Delete this contact?",
        "contacts.make_primary": "Make primary",
        "contacts.primary": "Primary",
        "contacts.presence_window": "When available",
        "contacts.saved": "Contact saved",
        "contacts.deleted": "Contact deleted",
        "contacts.role.owner": "Owner",
        "contacts.role.decision_maker": "Decision maker",
        "contacts.role.receiver": "Receives deliveries",
        "contacts.role.payer": "Pays",
        "photos.tab": "Photos",
        "photos.empty": "No photos yet",
        "photos.unavailable": "Photo unavailable",
        "photos.duplicate": "Duplicate",
        "photos.kind.storefront": "Storefront",
        "photos.kind.shelf": "Shelf",
        "photos.kind.other": "Other",
        # --- Visits page (admin *Visits* page + the Outlets drawer's Visits tab) ---
        "visits.title": "Visits",
        "visits.tab_visits": "Visits",
        "visits.tab_exceptions": "Exceptions",
        "visits.columns.planned": "Planned",
        "visits.columns.outcome": "Outcome",
        "visits.columns.checkin": "Check-in",
        "visits.columns.order": "Order",
        "visits.columns.detail": "Detail",
        "visits.columns.photos": "Photos",
        "visits.planned_yes": "Planned",
        "visits.planned_no": "Unplanned",
        "visits.checkin_skipped": "Skipped",
        "visits.checkin_unmeasured": "Not measured",
        "visits.filters.in_radius_true": "In radius",
        "visits.filters.in_radius_false": "Out of range",
        "visits.filters.photo": "Photo",
        "visits.filters.photo_missing": "No photo",
        "visits.filters.type": "Exception type",
        "visits.map_caption": "The map shows the check-ins on this page of the table only.",
        "visits.exceptions_caption": (
            "Unvisited outlets and duplicate open try-outs are"
            " as of now — the period does not narrow them."
        ),
        "visits.drawer_window": "The latest 20 visits in the last 90 days.",
        "visits.plan_vs_fact.title": "Plan vs fact",
        "visits.plan_vs_fact.day": "Day",
        "visits.plan_vs_fact.due": "Due",
        "visits.plan_vs_fact.completed": "Completed",
        "visits.plan_vs_fact.unplanned": "Unplanned",
        "visits.plan_vs_fact.strike_rate": "Strike rate",
        "visits.plan_vs_fact.no_plan": "No plan",
        # --- Visits page: the supervisor exceptions feed (labels for ExceptionFeedService's seven types) ---
        "exceptions.type.out_of_range_checkin": "Check-in outside the radius",
        "exceptions.type.skipped_checkin": "Skipped check-in",
        "exceptions.type.short_visit": "Short visit",
        "exceptions.type.declined_agent_order": "Declined agent order",
        "exceptions.type.duplicate_photo": "Duplicate photo",
        "exceptions.type.unvisited": "Unvisited outlet",
        "exceptions.type.duplicate_open_tryout": "Duplicate open try-out",
        # --- Analytics *Agent performance* tab: the twenty KPI column headers (AgentMetricsService.METRIC_KEYS) ---
        "metrics.planned_visits": "Planned visits",
        "metrics.completed_visits": "Completed visits",
        "metrics.plan_vs_fact_pct": "Plan vs fact %",
        "metrics.unplanned_visits": "Unplanned visits",
        "metrics.visits_per_day": "Visits / day",
        "metrics.strike_rate_pct": "Strike rate %",
        "metrics.assigned_outlets": "Assigned outlets",
        "metrics.active_outlets": "Active outlets",
        "metrics.active_share_pct": "Active share %",
        "metrics.new_outlets_registered": "New outlets registered",
        "metrics.new_outlets_activated": "New outlets activated",
        "metrics.orders_placed": "Orders placed",
        "metrics.orders_delivered_paid": "Orders delivered & paid",
        "metrics.bottles_delivered_paid": "Bottles delivered & paid",
        "metrics.revenue_delivered_paid": "Revenue delivered & paid",
        "metrics.agent_orders_cancelled": "Orders cancelled",
        "metrics.suggested_vs_accepted_pct": "Suggested vs accepted %",
        "metrics.out_of_range_checkins": "Out-of-range check-ins",
        "metrics.skipped_checkins": "Skipped check-ins",
        "metrics.avg_visit_minutes": "Avg visit minutes",
        "analytics.agent_performance.title": "Agent Performance",
        "analytics.agent_performance.group_visits": "Visits",
        "analytics.agent_performance.group_outlets": "Outlets",
        "analytics.agent_performance.group_orders": "Orders",
        "analytics.agent_performance.group_discipline": "Discipline",
        "analytics.agent_performance.as_of_note": (
            "Delivered-and-paid figures count the orders this agent placed in the period,"
            " as of now — a late delivery moves a past period."
        ),
        "analytics.agent_performance.plan_vs_fact_hint": "Measured only on days that had a plan",
    },
    "uz": {
        "title": "Savdo agentlari",
        "add_sales_agent": "Savdo agenti qo'shish",
        "edit_sales_agent": "Savdo agentini tahrirlash",
        "total_agents": "Jami agentlar",
        "active_agents": "Faol agentlar",
        "search_agents_placeholder": "Ism yoki telefon bo'yicha qidirish",
        "agent_active": "Faol",
        "agent_inactive": "Nofaol",
        "contact_name": "Ism",
        "email": "Email",
        "districts": "Tumanlar",
        "outlets_assigned_active": "Nuqtalar (biriktirilgan / faol)",
        "weekly_target": "Haftalik yangi nuqta rejasi",
        "employment_type": "Bandlik",
        "telegram_linked": "Telegram ulangan",
        "agent_details": "Tafsilotlar",
        "invite": "Taklif",
        "invite_link": "Taklif havolasi",
        "copy_link": "Havolani nusxalash",
        "link_copied": "Havola nusxalandi",
        "agent_created": "Savdo agenti yaratildi",
        "agent_updated": "Savdo agenti yangilandi",
        "share_invite_description": (
            "Bu bir martalik havolani agentga yuboring; u xodimlar botini ochadi va Telegram hisobini bog'laydi."
        ),
        "outlets_title": "Savdo nuqtalari",
        "outlet_name": "Nuqta",
        "outlet_type": "Turi",
        "stage": "Bosqich",
        "class": "Toifa",
        "agent": "Agent",
        "last_visit": "So'nggi tashrif",
        "search_outlets": "Nom yoki telefon bo'yicha qidirish",
        "unvisited_days": "Tashrif yo'q ≥ kun",
        "summary_is_estate_wide": "Barcha nuqtalar bo'yicha jami — quyidagi filtrlar bu raqamlarni o'zgartirmaydi.",
        "bulk_assign": "Tuman bo'yicha biriktirish",
        "import_existing": "Mavjud mijozlarni import qilish",
        "import_confirm": "Hali nuqtasi bo'lmagan har bir do'kon/ish joyi manzili uchun nuqta yaratilsinmi?",
        "imported": "Mijozlar nuqta sifatida import qilindi",
        "bulk_assigned": "Nuqtalar biriktirildi",
        "approve": "Tasdiqlash",
        "reject": "Rad etish",
        "mark_lost": "Yo'qotilgan deb belgilash",
        "reason": "Sabab",
        "outlet_approved": "Nuqta faollashtirildi",
        "outlet_rejected": "So'rov rad etildi",
        "outlet_assigned": "Agent biriktirildi",
        "outlet_updated": "Nuqta yangilandi",
        "outlet_marked_lost": "Yo'qotilgan deb belgilandi",
        "tab_contacts": "Kontaktlar",
        "tab_history": "Tarix",
        "outlet_address": "Manzil",
        "open_receivable": "Qarzdorlik",
        "bottle_balance": "Nuqtadagi idishlar",
        "dedupe_candidates": "Yaratishda topilgan ehtimoliy takrorlar",
        # --- D25 branch outlets ---
        "outlet_account_line": "Hisob: {{account}} · {{count}} ta filial",
        "open_receivable_account": "Qarzdorlik (hisob bo'yicha)",
        "bottles_branch": "Ushbu filialdagi idishlar",
        "approve_modal_title": "Nuqtani tasdiqlash",
        "attach_modal_title": "{{account}} hisobiga biriktirish",
        "attach_confirm": (
            "Bu nuqta {{account}} hisobiga filial sifatida qo'shiladi"
            " — yangi mijoz hisobi yaratilmaydi."
        ),
        "contract_number_label": "Shartnoma raqami",
        "role": "Rol",
        "when": "Qachon",
        "from": "Dan",
        "to": "Ga",
        "employment_employee": "Xodim",
        "employment_contractor": "Shartnoma asosida",
        "stage_prospect": "Nomzod",
        "stage_trial": "Sinov",
        "stage_activation_requested": "Faollashtirish kutilmoqda",
        "stage_active": "Faol",
        "stage_at_risk": "Xavf ostida",
        "stage_dormant": "Uxlab yotgan",
        "stage_lost": "Yo'qotilgan",
        "agents.visits_today": "Bugungi tashriflar",
        "agents.orders_today": "Bugungi buyurtmalar",
        "outlets.delivery_window.label": "Yetkazish oynasi",
        "outlets.delivery_window.start": "Dan",
        "outlets.delivery_window.end": "Gacha",
        "outlets.delivery_window.help": (
            "Bu nuqtaga rasmiylashtirilgan har bir buyurtma uchun standart yetkazish oynasi."
            " Olib tashlash uchun ikkala maydonni tozalang."
        ),
        "outlets.delivery_window.invalid": "Boshlanish va tugash vaqtini birga kiriting yoki ikkalasini tozalang.",
        # --- Outlets drawer: the Edit form, the Contacts tab and the Photos tab (D27/D29/D30) ---
        "outlets.edit": "Tahrirlash",
        "outlets.edit_title": "Nuqtani tahrirlash",
        "outlets.fields.channel": "Kanal",
        "outlets.fields.cadence_days_override": "Tashrif oralig'i (kun)",
        "outlets.fields.cadence_help": "Bo'sh qoldirilsa, toifa bo'yicha oraliq ishlatiladi.",
        "outlets.fields.preferred_visit_window": "Tashrif uchun qulay vaqt",
        "outlets.fields.payment_terms": "To'lov shartlari",
        "outlets.fields.preferred_language": "Til",
        "outlets.fields.legal_form": "Tashkiliy-huquqiy shakl",
        "outlets.fields.tax_id": "STIR (INN)",
        "outlets.fields.competitor_note": "Raqobatchi haqida izoh",
        "outlets.fields.status_warning": "Xodimlar uchun ogohlantirish",
        "outlets.payment_terms.cash": "Naqd",
        "outlets.payment_terms.business_account": "Korporativ hisob",
        "contacts.add": "Kontakt qo'shish",
        "contacts.edit": "Tahrirlash",
        "contacts.edit_title": "Kontaktni tahrirlash",
        "contacts.delete": "O'chirish",
        "contacts.delete_confirm": "Bu kontakt o'chirilsinmi?",
        "contacts.make_primary": "Asosiy qilish",
        "contacts.primary": "Asosiy",
        "contacts.presence_window": "Qachon bo'ladi",
        "contacts.saved": "Kontakt saqlandi",
        "contacts.deleted": "Kontakt o'chirildi",
        "contacts.role.owner": "Egasi",
        "contacts.role.decision_maker": "Qaror qabul qiluvchi",
        "contacts.role.receiver": "Yetkazmani qabul qiladi",
        "contacts.role.payer": "To'lovchi",
        "photos.tab": "Suratlar",
        "photos.empty": "Hali suratlar yo'q",
        "photos.unavailable": "Surat mavjud emas",
        "photos.duplicate": "Takroriy",
        "photos.kind.storefront": "Do'kon peshtoqi",
        "photos.kind.shelf": "Javon",
        "photos.kind.other": "Boshqa",
        # --- Visits page (admin *Visits* page + the Outlets drawer's Visits tab) ---
        "visits.title": "Tashriflar",
        "visits.tab_visits": "Tashriflar",
        "visits.tab_exceptions": "Chetlanishlar",
        "visits.columns.planned": "Rejalashtirilgan",
        "visits.columns.outcome": "Natija",
        "visits.columns.checkin": "Belgilanish",
        "visits.columns.order": "Buyurtma",
        "visits.columns.detail": "Tafsilot",
        "visits.columns.photos": "Suratlar",
        "visits.planned_yes": "Rejalashtirilgan",
        "visits.planned_no": "Rejadan tashqari",
        "visits.checkin_skipped": "O'tkazib yuborilgan",
        "visits.checkin_unmeasured": "O'lchanmagan",
        "visits.filters.in_radius_true": "Radius ichida",
        "visits.filters.in_radius_false": "Radius tashqarisida",
        "visits.filters.photo": "Surat",
        "visits.filters.photo_missing": "Suratsiz",
        "visits.filters.type": "Chetlanish turi",
        "visits.map_caption": "Xaritada jadvalning faqat shu sahifasidagi belgilanishlar ko'rsatiladi.",
        "visits.exceptions_caption": (
            "Tashrif buyurilmagan nuqtalar va takroriy ochiq sinovlar"
            " hozirgi holat bo'yicha — davr ularni cheklamaydi."
        ),
        "visits.drawer_window": "So'nggi 90 kundagi oxirgi 20 ta tashrif.",
        "visits.plan_vs_fact.title": "Reja va haqiqat",
        "visits.plan_vs_fact.day": "Kun",
        "visits.plan_vs_fact.due": "Rejada",
        "visits.plan_vs_fact.completed": "Bajarilgan",
        "visits.plan_vs_fact.unplanned": "Rejadan tashqari",
        "visits.plan_vs_fact.strike_rate": "Natijadorlik",
        "visits.plan_vs_fact.no_plan": "Reja yo'q",
        # --- Visits page: the supervisor exceptions feed (labels for ExceptionFeedService's seven types) ---
        "exceptions.type.out_of_range_checkin": "Radius tashqarisidagi belgilanish",
        "exceptions.type.skipped_checkin": "O'tkazib yuborilgan belgilanish",
        "exceptions.type.short_visit": "Qisqa tashrif",
        "exceptions.type.declined_agent_order": "Rad etilgan agent buyurtmasi",
        "exceptions.type.duplicate_photo": "Takroriy surat",
        "exceptions.type.unvisited": "Tashrif buyurilmagan nuqta",
        "exceptions.type.duplicate_open_tryout": "Takroriy ochiq sinov",
        # --- Analytics *Agent performance* tab: the twenty KPI column headers (AgentMetricsService.METRIC_KEYS) ---
        "metrics.planned_visits": "Rejalashtirilgan tashriflar",
        "metrics.completed_visits": "Yakunlangan tashriflar",
        "metrics.plan_vs_fact_pct": "Reja va fakt %",
        "metrics.unplanned_visits": "Rejadan tashqari tashriflar",
        "metrics.visits_per_day": "Kuniga tashrif",
        "metrics.strike_rate_pct": "Buyurtmali tashriflar %",
        "metrics.assigned_outlets": "Biriktirilgan nuqtalar",
        "metrics.active_outlets": "Faol nuqtalar",
        "metrics.active_share_pct": "Faollar ulushi %",
        "metrics.new_outlets_registered": "Yangi qo'shilgan nuqtalar",
        "metrics.new_outlets_activated": "Yangi faollashgan nuqtalar",
        "metrics.orders_placed": "Berilgan buyurtmalar",
        "metrics.orders_delivered_paid": "Buyurtmalar (yetkazilgan va to'langan)",
        "metrics.bottles_delivered_paid": "Idishlar (yetkazilgan va to'langan)",
        "metrics.revenue_delivered_paid": "Tushum (yetkazilgan va to'langan)",
        "metrics.agent_orders_cancelled": "Bekor qilingan buyurtmalar",
        "metrics.suggested_vs_accepted_pct": "Tavsiyadan qabul qilingan %",
        "metrics.out_of_range_checkins": "Radiusdan tashqari belgilanishlar",
        "metrics.skipped_checkins": "O'tkazib yuborilgan belgilanishlar",
        "metrics.avg_visit_minutes": "O'rtacha tashrif, daqiqa",
        "analytics.agent_performance.title": "Agent samaradorligi",
        "analytics.agent_performance.group_visits": "Tashriflar",
        "analytics.agent_performance.group_outlets": "Savdo nuqtalari",
        "analytics.agent_performance.group_orders": "Buyurtmalar",
        "analytics.agent_performance.group_discipline": "Intizom",
        "analytics.agent_performance.as_of_note": (
            "Yetkazilgan va to'langan ko'rsatkichlar agent shu davrda bergan buyurtmalarni"
            " hozirgi holat bo'yicha sanaydi — kechikkan yetkazish o'tgan davrni o'zgartiradi."
        ),
        "analytics.agent_performance.plan_vs_fact_hint": "Faqat reja bo'lgan kunlar bo'yicha o'lchanadi",
    },
    "ru": {
        "title": "Торговые агенты",
        "add_sales_agent": "Добавить агента",
        "edit_sales_agent": "Редактировать агента",
        "total_agents": "Всего агентов",
        "active_agents": "Активных агентов",
        "search_agents_placeholder": "Поиск по имени или телефону",
        "agent_active": "Активен",
        "agent_inactive": "Неактивен",
        "contact_name": "Имя",
        "email": "Email",
        "districts": "Районы",
        "outlets_assigned_active": "Точки (назначено / активных)",
        "weekly_target": "Недельный план новых точек",
        "employment_type": "Занятость",
        "telegram_linked": "Telegram привязан",
        "agent_details": "Детали",
        "invite": "Пригласить",
        "invite_link": "Ссылка-приглашение",
        "copy_link": "Скопировать ссылку",
        "link_copied": "Ссылка скопирована",
        "agent_created": "Агент создан",
        "agent_updated": "Агент обновлён",
        "share_invite_description": (
            "Отправьте эту одноразовую ссылку агенту: она откроет бот сотрудников и привяжет его Telegram."
        ),
        "outlets_title": "Торговые точки",
        "outlet_name": "Точка",
        "outlet_type": "Тип",
        "stage": "Этап",
        "class": "Класс",
        "agent": "Агент",
        "last_visit": "Последний визит",
        "search_outlets": "Поиск по названию или телефону",
        "unvisited_days": "Без визита ≥ дней",
        "summary_is_estate_wide": "Итоги по всем точкам — фильтры ниже их не сужают.",
        "bulk_assign": "Назначить по району",
        "import_existing": "Импортировать существующих клиентов",
        "import_confirm": "Создать точку для каждого адреса магазина/офиса, у которого её ещё нет?",
        "imported": "Клиенты импортированы как точки",
        "bulk_assigned": "Точки назначены",
        "approve": "Одобрить",
        "reject": "Отклонить",
        "mark_lost": "Отметить потерянной",
        "reason": "Причина",
        "outlet_approved": "Точка активирована",
        "outlet_rejected": "Заявка отклонена",
        "outlet_assigned": "Агент назначен",
        "outlet_updated": "Точка обновлена",
        "outlet_marked_lost": "Отмечена как потерянная",
        "tab_contacts": "Контакты",
        "tab_history": "История",
        "outlet_address": "Адрес",
        "open_receivable": "Долг",
        "bottle_balance": "Бутылей на точке",
        "dedupe_candidates": "Возможные дубликаты, найденные при создании",
        # --- D25 branch outlets ---
        "outlet_account_line": "Аккаунт: {{account}} · филиалов: {{count}}",
        "open_receivable_account": "Долг (по аккаунту)",
        "bottles_branch": "Бутылей в этом филиале",
        "approve_modal_title": "Одобрить точку",
        "attach_modal_title": "Привязать к аккаунту {{account}}",
        "attach_confirm": (
            "Эта точка станет филиалом аккаунта {{account}}"
            " — новый клиентский аккаунт не создаётся."
        ),
        "contract_number_label": "Номер договора",
        "role": "Роль",
        "when": "Когда",
        "from": "Из",
        "to": "В",
        "employment_employee": "Сотрудник",
        "employment_contractor": "По договору",
        "stage_prospect": "Кандидат",
        "stage_trial": "Пробный",
        "stage_activation_requested": "Ожидает активации",
        "stage_active": "Активна",
        "stage_at_risk": "Под риском",
        "stage_dormant": "Спящая",
        "stage_lost": "Потеряна",
        "agents.visits_today": "Визиты сегодня",
        "agents.orders_today": "Заказы сегодня",
        "outlets.delivery_window.label": "Окно доставки",
        "outlets.delivery_window.start": "С",
        "outlets.delivery_window.end": "До",
        "outlets.delivery_window.help": (
            "Окно доставки по умолчанию для каждого заказа, оформленного на этой точке."
            " Очистите оба поля, чтобы убрать его."
        ),
        "outlets.delivery_window.invalid": "Укажите и начало, и конец либо очистите оба поля.",
        # --- Outlets drawer: the Edit form, the Contacts tab and the Photos tab (D27/D29/D30) ---
        "outlets.edit": "Изменить",
        "outlets.edit_title": "Изменить точку",
        "outlets.fields.channel": "Канал",
        "outlets.fields.cadence_days_override": "Посещать каждые (дней)",
        "outlets.fields.cadence_help": "Оставьте пустым, чтобы использовать интервал по классу.",
        "outlets.fields.preferred_visit_window": "Удобное время визита",
        "outlets.fields.payment_terms": "Условия оплаты",
        "outlets.fields.preferred_language": "Язык",
        "outlets.fields.legal_form": "Организационно-правовая форма",
        "outlets.fields.tax_id": "ИНН",
        "outlets.fields.competitor_note": "Заметка о конкуренте",
        "outlets.fields.status_warning": "Предупреждение для сотрудников",
        "outlets.payment_terms.cash": "Наличные",
        "outlets.payment_terms.business_account": "Корпоративный счёт",
        "contacts.add": "Добавить контакт",
        "contacts.edit": "Изменить",
        "contacts.edit_title": "Изменить контакт",
        "contacts.delete": "Удалить",
        "contacts.delete_confirm": "Удалить этот контакт?",
        "contacts.make_primary": "Сделать основным",
        "contacts.primary": "Основной",
        "contacts.presence_window": "Когда на месте",
        "contacts.saved": "Контакт сохранён",
        "contacts.deleted": "Контакт удалён",
        "contacts.role.owner": "Владелец",
        "contacts.role.decision_maker": "Принимает решения",
        "contacts.role.receiver": "Принимает доставку",
        "contacts.role.payer": "Оплачивает",
        "photos.tab": "Фото",
        "photos.empty": "Фото пока нет",
        "photos.unavailable": "Фото недоступно",
        "photos.duplicate": "Повтор",
        "photos.kind.storefront": "Витрина",
        "photos.kind.shelf": "Полка",
        "photos.kind.other": "Другое",
        # --- Visits page (admin *Visits* page + the Outlets drawer's Visits tab) ---
        "visits.title": "Визиты",
        "visits.tab_visits": "Визиты",
        "visits.tab_exceptions": "Отклонения",
        "visits.columns.planned": "Плановый",
        "visits.columns.outcome": "Результат",
        "visits.columns.checkin": "Отметка",
        "visits.columns.order": "Заказ",
        "visits.columns.detail": "Детали",
        "visits.columns.photos": "Фото",
        "visits.planned_yes": "Плановый",
        "visits.planned_no": "Внеплановый",
        "visits.checkin_skipped": "Пропущена",
        "visits.checkin_unmeasured": "Не измерено",
        "visits.filters.in_radius_true": "В радиусе",
        "visits.filters.in_radius_false": "Вне радиуса",
        "visits.filters.photo": "Фото",
        "visits.filters.photo_missing": "Без фото",
        "visits.filters.type": "Тип отклонения",
        "visits.map_caption": "На карте показаны отметки только с этой страницы таблицы.",
        "visits.exceptions_caption": (
            "Точки без визитов и дубликаты открытых тест-периодов"
            " — по состоянию на сейчас; период их не сужает."
        ),
        "visits.drawer_window": "Последние 20 визитов за 90 дней.",
        "visits.plan_vs_fact.title": "План и факт",
        "visits.plan_vs_fact.day": "День",
        "visits.plan_vs_fact.due": "План",
        "visits.plan_vs_fact.completed": "Выполнено",
        "visits.plan_vs_fact.unplanned": "Внеплановые",
        "visits.plan_vs_fact.strike_rate": "Результативность",
        "visits.plan_vs_fact.no_plan": "Нет плана",
        # --- Visits page: the supervisor exceptions feed (labels for ExceptionFeedService's seven types) ---
        "exceptions.type.out_of_range_checkin": "Отметка вне радиуса",
        "exceptions.type.skipped_checkin": "Пропущенная отметка",
        "exceptions.type.short_visit": "Короткий визит",
        "exceptions.type.declined_agent_order": "Отклонённый заказ агента",
        "exceptions.type.duplicate_photo": "Дубликат фото",
        "exceptions.type.unvisited": "Точка без визитов",
        "exceptions.type.duplicate_open_tryout": "Дубликат открытого тест-периода",
        # --- Analytics *Agent performance* tab: the twenty KPI column headers (AgentMetricsService.METRIC_KEYS) ---
        "metrics.planned_visits": "Запланированные визиты",
        "metrics.completed_visits": "Завершённые визиты",
        "metrics.plan_vs_fact_pct": "План и факт %",
        "metrics.unplanned_visits": "Внеплановые визиты",
        "metrics.visits_per_day": "Визитов в день",
        "metrics.strike_rate_pct": "Визиты с заказом %",
        "metrics.assigned_outlets": "Закреплённые точки",
        "metrics.active_outlets": "Активные точки",
        "metrics.active_share_pct": "Доля активных %",
        "metrics.new_outlets_registered": "Новых точек добавлено",
        "metrics.new_outlets_activated": "Новых точек активировано",
        "metrics.orders_placed": "Оформлено заказов",
        "metrics.orders_delivered_paid": "Заказы (доставлено и оплачено)",
        "metrics.bottles_delivered_paid": "Бутыли (доставлено и оплачено)",
        "metrics.revenue_delivered_paid": "Выручка (доставлено и оплачено)",
        "metrics.agent_orders_cancelled": "Отменённые заказы",
        "metrics.suggested_vs_accepted_pct": "Принято из рекомендованного %",
        "metrics.out_of_range_checkins": "Отметки вне радиуса",
        "metrics.skipped_checkins": "Пропущенные отметки",
        "metrics.avg_visit_minutes": "Средний визит, минут",
        "analytics.agent_performance.title": "Эффективность агентов",
        "analytics.agent_performance.group_visits": "Визиты",
        "analytics.agent_performance.group_outlets": "Точки",
        "analytics.agent_performance.group_orders": "Заказы",
        "analytics.agent_performance.group_discipline": "Дисциплина",
        "analytics.agent_performance.as_of_note": (
            "Показатели «доставлено и оплачено» учитывают заказы, оформленные агентом"
            " в этом периоде, по состоянию на сейчас — поздняя доставка меняет прошлый период."
        ),
        "analytics.agent_performance.plan_vs_fact_hint": "Измеряется только по дням, на которые был план",
    },
}


def _assert_complete(translations: dict) -> None:
    en, uz, ru = (translations[lang] for lang in ("en", "uz", "ru"))
    assert set(en) == set(uz) == set(ru), "every key needs all three languages"


def seed_ui_sales_translations(user_id: int | None = None) -> None:
    """Upsert the ui_sales_agents admin-UI translations (idempotent)."""
    _assert_complete(UI_SALES_TRANSLATIONS)
    Translation.bulk_create_or_update(UI_SALES_TRANSLATIONS, category=UI_SALES_CATEGORY, user_id=user_id)


def main() -> None:
    app = create_app()
    with app.app_context():
        seed_ui_sales_translations()
        total = sum(len(v) for v in UI_SALES_TRANSLATIONS.values())
        print(f"Seeded {total} {UI_SALES_CATEGORY} translation rows.")


if __name__ == "__main__":
    main()
