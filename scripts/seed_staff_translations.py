#!/usr/bin/env python3
"""
Seed staff bot translations into the database.

This script:
1. Collects literal i18n keys used in staff_bot code.
2. Adds dynamic key families used via f-strings.
3. Upserts translations with category='staff_bot' for en/uz/ru.
"""

import re
import sys
from pathlib import Path
from typing import Dict, Optional, Set

# Match existing project seeding scripts.
sys.path.insert(0, '/app')

from business_app import create_app, db  # noqa: E402
from business_app.models.translation import Translation  # noqa: E402
from shared.staff_constants import (  # noqa: E402
    FAILED_DELIVERY_REASONS,
    RECONCILIATION_RISK_FLAGS,
    SALES_EVENTS,
    STAFF_BOT_ROLES,
)
from shared.enums import (  # noqa: E402
    DeliveryStatus,
    DriverBottleSessionStatus,
    DriverCashSessionStatus,
    OrderStatus,
    PaymentMethod,
)


LANGUAGES = ("en", "uz", "ru")
RU_ALLOWED_LATIN_TOKENS = ("BlueStream", "Payme", "Click", "UZS", "API", "COD")


# Curated high-value strings.
STAFF_TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "staff.menu.title": {
        "en": "Staff Bot - Main Menu",
        "uz": "Xodimlar boti - Asosiy menyu",
        "ru": "Бот сотрудников Aqua Element - Главное меню",
    },
    "staff.menu.new_orders": {
        "en": "New Orders",
        "uz": "Yangi buyurtmalar",
        "ru": "Новые заказы",
    },
    "staff.menu.my_outlets": {
        "en": "My outlets",
        "uz": "Mening savdo nuqtalarim",
        "ru": "Мои точки",
    },
    "staff.menu.new_outlet": {
        "en": "New outlet",
        "uz": "Yangi savdo nuqtasi",
        "ru": "Новая точка",
    },
    "staff.menu.redispatch_failed": {
        "en": "Re-dispatch Failed",
        "uz": "Muvaffaqiyatsizni qayta yuborish",
        "ru": "Переотправить неуданые",
    },
    "staff.redispatch.title": {
        "en": "Re-dispatch Failed Delivery",
        "uz": "Muvaffaqiyatsiz yetkazishni qayta yuborish",
        "ru": "Переотправка неудачной доставки",
    },
    "staff.redispatch.pick": {
        "en": "Pick a failed delivery to return to the pool:",
        "uz": "Hovuzga qaytarish uchun muvaffaqiyatsiz yetkazishni tanlang:",
        "ru": "Выберите неудачную доставку для возврата в пул:",
    },
    "staff.redispatch.none": {
        "en": "There are no failed deliveries to re-dispatch.",
        "uz": "Qayta yuboriladigan muvaffaqiyatsiz yetkazishlar yo‘q.",
        "ru": "Нет неудачных доставок для переотправки.",
    },
    "staff.redispatch.button": {
        "en": "Re-dispatch to pool",
        "uz": "Hovuzga qayta yuborish",
        "ru": "Вернуть в пул",
    },
    "staff.redispatch.attempts": {
        "en": "Attempts",
        "uz": "Urinishlar",
        "ru": "Попытки",
    },
    "staff.redispatch.success": {
        "en": "Delivery re-dispatched to the pool. A driver can now re-claim it.",
        "uz": "Yetkazish hovuzga qayta yuborildi. Endi haydovchi uni qayta olishi mumkin.",
        "ru": "Доставка возвращена в пул. Теперь водитель может взять её снова.",
    },
    "staff.menu.new_orders_view": {
        "en": "New Orders (View)",
        "uz": "Yangi buyurtmalar (Korish)",
        "ru": "Новые заказы (просмотр)",
    },
    "staff.menu.active_deliveries": {
        "en": "Orders assigned to me",
        "uz": "Menga biriktirilgan buyurtmalar",
        "ru": "Заказы, назначенные мне",
    },
    "staff.menu.delivery_history": {
        "en": "Delivery History",
        "uz": "Yetkazish tarixi",
        "ru": "История доставок",
    },
    "staff.menu.tryout_tasks": {
        "en": "Try-out Tasks",
        "uz": "Sinov vazifalari",
        "ru": "Задачи по пробным выдачам",
    },
    "staff.menu.create_tryout_now": {
        "en": "Create Try-out Now",
        "uz": "Hozir sinov yaratish",
        "ru": "Создать пробную выдачу",
    },
    "staff.menu.active_tryouts": {
        "en": "Active Try-outs",
        "uz": "Faol sinovlar",
        "ru": "Активные пробные выдачи",
    },
    "staff.menu.my_stats": {
        "en": "My Stats",
        "uz": "Mening statistikam",
        "ru": "Моя статистика",
    },
    "staff.menu.cash_reconciliation": {
        "en": "Cash Reconciliation",
        "uz": "Naqd pul hisobini solishtirish",
        "ru": "Сверка наличных",
    },
    "staff.menu.create_client": {
        "en": "Create Client",
        "uz": "Mijoz yaratish",
        "ru": "Создать клиента",
    },
    "staff.menu.create_order": {
        "en": "Create Order",
        "uz": "Buyurtma yaratish",
        "ru": "Создать заказ",
    },
    "staff.menu.search_client": {
        "en": "Search Client",
        "uz": "Mijoz qidirish",
        "ru": "Поиск клиента",
    },
    "staff.menu.recent_orders": {
        "en": "Recent Orders",
        "uz": "Songgi buyurtmalar",
        "ru": "Последние заказы",
    },
    "staff.menu.profile": {
        "en": "Profile",
        "uz": "Profil",
        "ru": "Профиль",
    },
    "staff.menu.settings": {
        "en": "Settings",
        "uz": "Sozlamalar",
        "ru": "Настройки",
    },
    "staff.menu.help": {
        "en": "Help",
        "uz": "Yordam",
        "ru": "Помощь",
    },
    "staff.menu.tryouts": {
        "en": "Try-outs",
        "uz": "Sinovlar",
        "ru": "Пробные выдачи",
    },
    "staff.menu.cash": {
        "en": "Cash",
        "uz": "Naqd pul",
        "ru": "Наличные",
    },
    "staff.menu.collect_cod_debt": {
        "en": "Collect COD Debt",
        "uz": "Qarzni yigish",
        "ru": "Сбор долга",
    },
    "staff.tryouts.hub_title": {
        "en": "Try-outs",
        "uz": "Sinovlar",
        "ru": "Пробные выдачи",
    },
    "staff.tryouts.create": {
        "en": "Create Try-out",
        "uz": "Sinov yaratish",
        "ru": "Создать пробную выдачу",
    },
    "staff.cash.hub_title": {
        "en": "Cash",
        "uz": "Naqd pul",
        "ru": "Наличные",
    },
    "staff.profile.view_stats": {
        "en": "My Stats",
        "uz": "Mening statistikam",
        "ru": "Моя статистика",
    },
    "staff.profile.view_history": {
        "en": "Delivery History",
        "uz": "Yetkazish tarixi",
        "ru": "История доставок",
    },
    "staff.profile.view_recent_orders": {
        "en": "Recent Orders",
        "uz": "Songgi buyurtmalar",
        "ru": "Последние заказы",
    },
    "staff.operator.pool_title": {
        "en": "Order Pool (View Only)",
        "uz": "Buyurtmalar havzasi (Faqat korish)",
        "ru": "Список заказов (только просмотр)",
    },
    "staff.operator.assigned_to": {
        "en": "Assigned To",
        "uz": "Biriktirilgan",
        "ru": "Назначен",
    },
    "staff.operator.address": {
        "en": "Address",
        "uz": "Manzil",
        "ru": "Адрес",
    },
    "staff.operator.payment_cash": {
        "en": "Cash",
        "uz": "Naqd pul",
        "ru": "Наличные",
    },
    "staff.operator.payment_payme": {
        "en": "Payme",
        "uz": "Payme",
        "ru": "Payme",
    },
    "staff.operator.payment_click": {
        "en": "Click",
        "uz": "Click",
        "ru": "Click",
    },
    "staff.operator.payment_business_account": {
        "en": "Business Account",
        "uz": "Hisob raqami orqali",
        "ru": "Безналичный счёт",
    },
    "staff.operator.cod_restricted": {
        "en": "Cash on delivery is unavailable for this customer until earlier COD debts are settled.",
        "uz": "Avvalgi qarzlari yopilmaguncha bu mijoz uchun yetkazib berishda naqd tolov mavjud emas.",
        "ru": "Оплата наличными при доставке недоступна для этого клиента, пока не будут погашены прежние долги.",
    },
    "staff.operator.payment_unavailable": {
        "en": "This payment method is not available for the selected customer.",
        "uz": "Tanlangan mijoz uchun bu tolov usuli mavjud emas.",
        "ru": "Этот способ оплаты недоступен для выбранного клиента.",
    },
    "staff.back": {"en": "Back", "uz": "Orqaga", "ru": "Назад"},
    "staff.confirm": {"en": "Confirm", "uz": "Tasdiqlash", "ru": "Подтвердить"},
    "staff.cancel": {"en": "Cancel", "uz": "Bekor qilish", "ru": "Отмена"},
    "staff.cancelled": {"en": "Cancelled", "uz": "Bekor qilindi", "ru": "Отменено"},
    "staff.yes": {"en": "Yes", "uz": "Ha", "ru": "Да"},
    "staff.no": {"en": "No", "uz": "Yoq", "ru": "Нет"},
    "staff.page": {"en": "Page", "uz": "Sahifa", "ru": "Страница"},
    "staff.common.not_available": {
        "en": "N/A",
        "uz": "Mavjud emas",
        "ru": "Недоступно",
    },
    # Placeholders for records the API returned without a display name/time.
    # These used to be bare English literals (`or 'Driver'`, `or 'unknown time'`)
    # inside button labels and message bodies.
    "staff.common.unknown_driver": {
        "en": "Driver",
        "uz": "Haydovchi",
        "ru": "Водитель",
    },
    "staff.common.driver_number": {
        "en": "Driver #{driver_id}",
        "uz": "Haydovchi #{driver_id}",
        "ru": "Водитель №{driver_id}",
    },
    "staff.common.unknown_time": {
        "en": "unknown time",
        "uz": "vaqti nomalum",
        "ru": "время неизвестно",
    },
    "staff.delivery.pending_transfer_line": {
        "en": "• From <b>{sender}</b>: <b>{qty}</b> bottles  [ref: {ref}]",
        "uz": "• <b>{sender}</b>dan: <b>{qty}</b> ta idish  [ref: {ref}]",
        "ru": "• От <b>{sender}</b>: <b>{qty}</b> ед. тары  [ref: {ref}]",
    },
    "staff.tryout.default_label": {
        "en": "Try-out",
        "uz": "Sinov",
        "ru": "Пробная",
    },
    "staff.currency.uzs": {
        "en": "UZS",
        "uz": "som",
        "ru": "сум",
    },
    "staff.unit.minutes": {
        "en": "min",
        "uz": "daq",
        "ru": "мин",
    },
    "staff.error_occurred": {
        "en": "An error occurred. Please try again.",
        "uz": "Xatolik yuz berdi. Qayta urinib koring.",
        "ru": "Произошла ошибка. Попробуйте снова.",
    },
    "staff.session_expired": {
        "en": "Session expired. Please login again.",
        "uz": "Sessiya tugadi. Qayta kiring.",
        "ru": "Сессия истекла. Войдите снова.",
    },
    "staff.unauthorized": {
        "en": "You are not allowed to perform this action.",
        "uz": "Bu amal uchun sizda ruxsat yoq.",
        "ru": "У вас нет прав для этого действия.",
    },
    "staff.select_language": {
        "en": "Select language",
        "uz": "Tilni tanlang",
        "ru": "Выберите язык",
    },
    "staff.language_changed": {
        "en": "Language updated.",
        "uz": "Til yangilandi.",
        "ru": "Язык обновлен.",
    },
    "staff.welcome_intro": {
        "en": "Welcome to Aqua Element Staff Bot!\n\nPlease select your language:",
        "uz": "Aqua Element xodimlar botiga xush kelibsiz!\n\nIltimos, tilni tanlang:",
        "ru": "Добро пожаловать в бот сотрудников Aqua Element!\n\nПожалуйста, выберите язык:",
    },
    "staff.notification.new_order": {
        "en": "New order available!",
        "uz": "Yangi buyurtma mavjud!",
        "ru": "Доступен новый заказ!",
    },
    "staff.notification.order_assigned": {
        "en": "Order #{number} has been assigned to you.",
        "uz": "#{number} buyurtma sizga biriktirildi.",
        "ru": "Заказ #{number} назначен вам.",
    },
    "staff.notification.order_reassigned_from": {
        "en": "Order #{number} was reassigned.",
        "uz": "#{number} buyurtma qayta biriktirildi.",
        "ru": "Заказ #{number} был переназначен.",
    },
    "staff.notification.order_cancelled": {
        "en": "Order #{number} was cancelled.",
        "uz": "#{number} buyurtma bekor qilindi.",
        "ru": "Заказ #{number} отменен.",
    },
    # Sales module (Task 10): the agent's approval outcome, the operators'
    # "new outlet waiting" ping, and the two deep-link buttons. The
    # `staff.sales.notify.{event}` family is built by f-string in
    # webhook_server.sales_event_handler, so it is also registered in
    # _add_dynamic_keys below (and its twin in staff_bot/i18n.py): a literal
    # regex cannot see it, and /health would otherwise never notice a gap.
    "staff.sales.notify.outlet_approved": {
        "en": "✅ Outlet <b>{outlet_name}</b> has been activated. You can now place orders for it.",
        "uz": "✅ <b>{outlet_name}</b> savdo nuqtasi faollashtirildi. Endi unga buyurtma bera olasiz.",
        "ru": "✅ Торговая точка <b>{outlet_name}</b> активирована. Теперь можно оформлять для неё заказы.",
    },
    "staff.sales.notify.outlet_rejected": {
        "en": "❌ Activation of <b>{outlet_name}</b> was declined: {reason}",
        "uz": "❌ <b>{outlet_name}</b> faollashtirish rad etildi: {reason}",
        "ru": "❌ Активация <b>{outlet_name}</b> отклонена: {reason}",
    },
    "staff.sales.notify.activation_requested": {
        "en": "🆕 New outlet awaiting activation: <b>{outlet_name}</b>",
        "uz": "🆕 Faollashtirishni kutayotgan yangi savdo nuqtasi: <b>{outlet_name}</b>",
        "ru": "🆕 Новая торговая точка ждёт активации: <b>{outlet_name}</b>",
    },
    "staff.sales.notify.open_outlet": {
        "en": "Open outlet",
        "uz": "Savdo nuqtasini ochish",
        "ru": "Открыть точку",
    },
    "staff.sales.notify.open_requests": {
        "en": "Activation requests",
        "uz": "Faollashtirish so'rovlari",
        "ru": "Заявки на активацию",
    },
    # The store's answer to an agent order (phase 2a). Explicit rows rather
    # than SALES_TEXT_TRANSLATIONS suffixes for the same reason as the four
    # above: `_curated_value` consults STAFF_TRANSLATIONS first, and these
    # carry HTML the suffix catalog does not.
    #
    # Ruling 19's carve-out lives here: the webhook passes `outlet_name`,
    # `order_number` and `reason` to BOTH events, and the copy is what decides
    # which of them an agent reads. A confirmation has no reason, so it must
    # not carry `{reason}` -- it would print "None" on the happy path.
    "staff.sales.notify.agent_order_confirmed": {
        "en": "✅ <b>{outlet_name}</b> confirmed order {order_number}.",
        "uz": "✅ <b>{outlet_name}</b> {order_number} buyurtmasini tasdiqladi.",
        "ru": "✅ <b>{outlet_name}</b> подтвердила заказ {order_number}.",
    },
    # A separate SENTENCE for the reason, not a `: {reason}` clause. The
    # customer bot's Decline button posts no reason at all
    # (`respond_agent_order` omits the field), so `AgentOrderConfirmationService
    # .respond` pushes `reason: None` on EVERY real decline and the handler
    # renders `{reason}` as ''. A colon form would therefore end every single
    # one of these messages on a dangling ":" -- the empty case is the normal
    # case here, not the edge.
    "staff.sales.notify.agent_order_declined": {
        "en": "❌ <b>{outlet_name}</b> declined order {order_number}. {reason}",
        "uz": "❌ <b>{outlet_name}</b> {order_number} buyurtmasini rad etdi. {reason}",
        "ru": "❌ <b>{outlet_name}</b> отклонила заказ {order_number}. {reason}",
    },
    # The morning digest's header line (phase 2b), seeded in its FINAL copy so
    # this row has exactly one owner: Task 8 renders it and does not reseed it.
    # It carries `<b>` like its two order siblings, and exactly ONE replacement
    # field — `{date}`, the agent's own local day, already formatted by the bot.
    # No outlet name and no reason: the sections beneath it (due / overdue /
    # unvisited / open visit) are the bot's own keys, built from the backend's
    # structured payload.
    #
    # A second field here would be an English leak nothing else can see:
    # `render_translation` degrades a template with an unfilled field to the
    # humanised English key, in every language. For the same reason, Task 8's
    # dedicated digest branch must land before beat is ever restarted — the
    # generic `sales_event_handler` fills only `outlet_name`/`reason`/
    # `order_number` and would degrade this row too -- the deploy ordering
    # Task 9's runbook states.
    "staff.sales.notify.morning_digest": {
        "en": "📋 <b>Your day — {date}</b>",
        "uz": "📋 <b>Bugungi ishingiz — {date}</b>",
        "ru": "📋 <b>Ваш день — {date}</b>",
    },
    # Rendered by the BACKEND (NotificationService in-app subject), not the bot;
    # seeded here too because both seeders upsert the same `translations` table
    # by key and backend get_translation reads it regardless of category.
    "staff.notification.subject.outlet_activation_requested": {
        "en": "Outlet activation requested",
        "uz": "Savdo nuqtasini faollashtirish so'rovi",
        "ru": "Заявка на активацию точки",
    },
    "staff.notification.order_unassigned": {
        "uz": "➖ {number}-buyurtma dispetcher tomonidan marshrutingizdan olib tashlandi. U yana umumiy ro'yxatga qaytdi.",
        "ru": "➖ Заказ {number} снят с вашего маршрута диспетчером и возвращён в общий пул.",
        "en": "➖ Order {number} was removed from your route by dispatch and returned to the pool.",
    },
    "staff.delivery.next_stop": {
        "en": "Next stop",
        "uz": "Keyingi manzil",
        "ru": "Следующая остановка",
    },
    "staff.delivery.eta_minutes": {
        "en": "ETA: {minutes} min",
        "uz": "Borish vaqti: {minutes} daq",
        "ru": "Прибытие: {minutes} мин",
    },
    "staff.delivery.distance_km": {
        "en": "{km} km away",
        "uz": "{km} km uzoqlikda",
        "ru": "{km} км до точки",
    },
    "staff.delivery.optimize_routes_button": {
        "en": "Optimize routes",
        "uz": "Yo'nalishni qayta hisoblash",
        "ru": "Оптимизировать маршрут",
    },
    "staff.delivery.route_updated_toast": {
        "en": "Route updated",
        "uz": "Yo'nalish yangilandi",
        "ru": "Маршрут обновлён",
    },
    "staff.route.locked_by_dispatch": {
        "uz": "🗺 Marshrutingiz dispetcher tomonidan belgilangan, shuning uchun tartib o'zgarmadi.",
        "ru": "🗺 Ваш маршрут задан диспетчером, поэтому порядок не изменился.",
        "en": "🗺 Your route was set by dispatch, so the order is unchanged.",
    },
    "staff.delivery.share_location_prompt": {
        "en": "Tap the button below to share your current location — one tap is enough, you don't need live location. Share again whenever you accept a new order so the route stays accurate.",
        "uz": "Joriy joylashuvingizni yuborish uchun pastdagi tugmani bosing — bir marta bosish yetarli, jonli joylashuv shart emas. Yangi buyurtmani qabul qilganingizda yo'nalish aniq qolishi uchun qaytadan ulashing.",
        "ru": "Нажмите кнопку ниже, чтобы отправить текущую геопозицию — одного нажатия достаточно, живая геопозиция не нужна. Отправляйте снова после принятия каждого нового заказа, чтобы маршрут оставался актуальным.",
    },
    "staff.delivery.share_location_after_accept": {
        "en": "📍 Order accepted! Share your current location now so we can recalculate the optimal route from where you are.",
        "uz": "📍 Buyurtma qabul qilindi! Eng yaxshi yo'nalishni siz turgan joydan qayta hisoblashimiz uchun joriy joylashuvingizni yuboring.",
        "ru": "📍 Заказ принят! Отправьте текущую геопозицию, чтобы мы пересчитали оптимальный маршрут от вашего нынешнего местоположения.",
    },
    "staff.delivery.share_location_button": {
        "en": "Share location",
        "uz": "Joylashuvni yuborish",
        "ru": "Отправить геопозицию",
    },
    "staff.delivery.location_stale_notice": {
        "en": "Share your live location for better suggestions",
        "uz": "Yaxshiroq taklif uchun jonli joylashuvni yoqing",
        "ru": "Включите трансляцию геопозиции для точных подсказок",
    },
    "staff.delivery.location_too_coarse": {
        "en": "Weak GPS signal — your position is too imprecise to re-sort the route. Step outside and share again.",
        "uz": "GPS signali kuchsiz — joylashuvingiz marshrutni qayta tartiblash uchun juda noaniq. Tashqariga chiqib, qaytadan yuboring.",
        "ru": "Слабый сигнал GPS — ваша позиция слишком неточная для пересборки маршрута. Выйдите на улицу и отправьте снова.",
    },
    # Review fix (route-UX plan 2026-08-11, Task 13): this key is now used
    # ONLY for the §7 diversion offer (compute_insertion_cost's old "fits
    # your route, +detour" suggestion was removed). The old "+{km} km,
    # +{minutes} min" wording read as ADDED detour, but for a diversion
    # offer `minutes` is time SAVED by going here first (and `km` is always
    # 0) -- so it told the driver the opposite of the truth. Interim fix:
    # correct the sign, drop the meaningless km figure. Plan 3 restyles this
    # further using the payload's gain_minutes/committed_order_number.
    "staff.delivery.pool_insertion_offer": {
        "en": "Order #{order_no} is right on your way — going there first saves about {minutes} min. Accept?",
        "uz": "#{order_no} buyurtma yo'lingizda — avval o'sha yerga borsangiz, taxminan {minutes} daqiqa yutasiz. Qabul qilasizmi?",
        "ru": "Заказ #{order_no} совсем рядом — если поехать туда сначала, вы сэкономите около {minutes} мин. Принять?",
    },
    "staff.delivery.suggestion_declined": {
        "en": "Suggestion dismissed",
        "uz": "Taklif rad etildi",
        "ru": "Предложение отклонено",
    },
    # Route-UX Plan 3, Task 10: offer-builder SSOT buttons
    # (staff_bot/utils/offers.py). `staff.delivery.accept` was already
    # referenced by several call sites (webhook_server.py, keyboards/delivery.py,
    # flow_state.py) but never actually seeded here — it silently fell back to
    # the humanised key tail ("Accept"), which happened to read fine but was
    # one accidental key-rename away from breaking. Seeding it for real now
    # that offers.py is the single place that reads it.
    "staff.delivery.accept": {
        "en": "Accept",
        "uz": "Qabul qilish",
        "ru": "Принять",
    },
    "staff.delivery.suggestion_declined_button": {
        "en": "Not now",
        "uz": "Hozir emas",
        "ru": "Не сейчас",
    },
    # ---- Phase 3 route card (plan 2026-08-11-route-ux-phase3-route-card) ----
    "staff.route.suggested_next": {
        "en": "SUGGESTED NEXT",
        "uz": "TAVSIYA ETILGAN KEYINGISI",
        "ru": "РЕКОМЕНДУЕМАЯ СЛЕДУЮЩАЯ",
    },
    "staff.route.current_stop": {
        "en": "CURRENT STOP",
        "uz": "JORIY MANZIL",
        "ru": "ТЕКУЩАЯ ОСТАНОВКА",
    },
    "staff.route.card_header": {
        "en": "Stop {current} of {total}",
        "uz": "{total} tadan {current}-manzil",
        "ru": "Остановка {current} из {total}",
    },
    "staff.route.finish_by": {
        "en": "finish ~{time}",
        "uz": "tugash ~{time}",
        "ru": "финиш ~{time}",
    },
    "staff.route.updated_at": {
        "en": "updated {time}",
        "uz": "yangilandi {time}",
        "ru": "обновлено {time}",
    },
    "staff.route.all_stops_header": {
        "en": "All stops · {count} remaining",
        "uz": "Barcha manzillar · {count} ta qoldi",
        "ru": "Все остановки · осталось {count}",
    },
    "staff.route.all_stops_button": {
        "en": "All stops ({count})",
        "uz": "Barcha manzillar ({count})",
        "ru": "Все остановки ({count})",
    },
    "staff.route.start_this_stop": {
        "en": "Start this order",
        "uz": "Shu buyurtmani boshlash",
        "ru": "Начать этот заказ",
    },
    "staff.route.open_stop": {
        "en": "Open stop",
        "uz": "Manzilni ochish",
        "ru": "Открыть остановку",
    },
    "staff.route.navigate_all": {
        "en": "Navigate",
        "uz": "Navigatsiya",
        "ru": "Навигация",
    },
    # FINAL review, I1: the old copy asserted "your suggested next stop
    # changed" unconditionally, but `RouteEditService`'s dispatch paths
    # (business_app/services/route_edit_service.py:170-182) all default
    # `sound=True` regardless of `head_changed` -- so a tail-only reorder
    # (e.g. swapping stops 4 and 5) pinged the driver with a claim that was
    # simply false. The gate itself is Plan 1's and out of scope here; only
    # the copy changes, to a wording that is honest whether the head moved
    # or the route was reordered further down (and for the deferred 07:00
    # batch trigger, where the route is merely new for the day).
    "staff.route.head_changed_alert": {
        "en": "Your route was updated — your next stop may have changed.",
        "uz": "Yo'nalishingiz yangilandi — keyingi manzilingiz o'zgargan bo'lishi mumkin.",
        "ru": "Ваш маршрут обновлён — ваша следующая остановка могла измениться.",
    },
    "staff.route.open_route_card": {
        "en": "Open route card",
        "uz": "Marshrut kartasini ochish",
        "ru": "Открыть карту маршрута",
    },
    "staff.route.all_done": {
        "en": "All stops done for now — no active deliveries.",
        "uz": "Hozircha barcha manzillar yakunlandi — faol yetkazishlar yo'q.",
        "ru": "Все остановки выполнены — активных доставок нет.",
    },
    "staff.route.refresh": {
        "en": "Refresh",
        "uz": "Yangilash",
        "ru": "Обновить",
    },
    # Toast on the 🔄 button. Time-only on purpose: the toast is answered
    # BEFORE the backend fetch (callback ids expire in ~10-15s), so a stop
    # count is not known yet. A moving digit is also language-independent,
    # which matters for a trilingual fleet.
    "staff.route.refreshed_toast": {
        "en": "🔄 Updated {time}",
        "uz": "🔄 Yangilandi {time}",
        "ru": "🔄 Обновлено {time}",
    },
    "staff.route.diversion_offer": {
        "en": "📦 Order #{order_no} is close to you.\nGo here first instead of #{committed_no}? (saves ~{minutes} min)",
        "uz": "📦 #{order_no} buyurtma sizga yaqin.\n#{committed_no} o'rniga avval shu yerga borasizmi? (~{minutes} daqiqa tejaladi)",
        "ru": "📦 Заказ #{order_no} рядом с вами.\nПоехать сюда сначала вместо #{committed_no}? (сэкономит ~{minutes} мин)",
    },
    "staff.route.go_here_first": {
        "en": "Go here first",
        "uz": "Avval shu yerga",
        "ru": "Сначала сюда",
    },
    "staff.route.keep_current": {
        "en": "Keep current order",
        "uz": "Joriy tartibda davom etish",
        "ru": "Оставить текущий порядок",
    },
    "staff.delivery.location_required_notice": {
        "en": "Share your location to get the optimal delivery order",
        "uz": "Eng yaxshi yetkazib berish tartibini olish uchun joylashuvingizni yuboring",
        "ru": "Отправьте свою геопозицию, чтобы получить оптимальный порядок доставок",
    },
    "staff.delivery.share_location_first_toast": {
        "en": "Please share your location first — without it we can't compute the route",
        "uz": "Avval joylashuvingizni yuboring — usiz yo'nalishni hisoblay olmaymiz",
        "ru": "Сначала отправьте свою геопозицию — без неё мы не можем рассчитать маршрут",
    },
    "staff.delivery.location_received": {
        "en": "Location received",
        "uz": "Joylashuv qabul qilindi",
        "ru": "Геопозиция получена",
    },
    "staff.delivery.route_recalculated": {
        "en": "Route has been recalculated based on your current position.",
        "uz": "Yo'nalish joriy joylashuvingiz asosida qayta hisoblandi.",
        "ru": "Маршрут пересчитан на основе вашего текущего положения.",
    },
    "staff.delivery.tap_to_see_optimized": {
        "en": "Tap to see the optimized order:",
        "uz": "Optimallashtirilgan tartibni ko'rish uchun bosing:",
        "ru": "Нажмите, чтобы увидеть оптимизированный порядок:",
    },
    "staff.delivery.location_update_failed": {
        "en": "Couldn't save your location, please try again",
        "uz": "Joylashuvingizni saqlay olmadik, qayta urinib ko'ring",
        "ru": "Не удалось сохранить вашу геопозицию, попробуйте ещё раз",
    },
    "staff.delivery.cash_collected_label": {
        "en": "Collected",
        "uz": "Yigildi",
        "ru": "Собрано",
    },
    "staff.delivery.total_label": {
        "en": "Total",
        "uz": "Jami",
        "ru": "Итого",
    },
    "staff.delivery.no_cash_note": {
        "en": "No cash to collect",
        "uz": "Naqd talab qilinmaydi",
        "ru": "Наличные не требуются",
    },
    "staff.delivery.apartment_label": {
        "en": "Apt.",
        "uz": "Xonadon",
        "ru": "Кв.",
    },
    "staff.delivery.floor_label": {
        "en": "Floor",
        "uz": "Qavat",
        "ru": "Этаж",
    },
    "staff.delivery.cash_outstanding_label": {
        "en": "Outstanding",
        "uz": "Qoldiq qarz",
        "ru": "Остаток долга",
    },
    # Delivery-window rendering (scheduled-delivery-orders task 13). The call
    # site builds this key with an f-string
    # (f'staff.delivery.window.{kind}') — a hand-written dynamic key family
    # blinds the /health required-key check (see
    # Translation._add_dynamic_family_keys's docstring for the same failure
    # mode), so all four `kind` values MUST also be listed literally here.
    "staff.delivery.window.anytime": {
        "en": "Anytime today",
        "uz": "Bugun istalgan vaqtda",
        "ru": "Сегодня в любое время",
    },
    "staff.delivery.window.between": {
        "en": "Between {time}",
        "uz": "{time} oralig'ida",
        "ru": "В интервале {time}",
    },
    "staff.delivery.window.until": {
        "en": "Deliver before {time}",
        "uz": "{time} gacha yetkazing",
        "ru": "Доставить до {time}",
    },
    "staff.delivery.window.after": {
        "en": "Deliver after {time}",
        "uz": "{time} dan keyin yetkazing",
        "ru": "Доставить после {time}",
    },
    "staff.delivery.no_cash_collected": {
        "en": "No cash collected",
        "uz": "Naqd pul olinmadi",
        "ru": "Наличные не получены",
    },
    "staff.delivery.enter_no_cash_reason": {
        "en": "Enter why no cash was collected for this delivery.",
        "uz": "Bu yetkazib berishda nima uchun naqd pul olinmaganini kiriting.",
        "ru": "Укажите, почему наличные не были получены по этой доставке.",
    },
    "staff.delivery.enter_partial_cash_reason": {
        "en": "Enter a note explaining the partial cash collection.",
        "uz": "Qisman olingan naqd pul uchun izoh kiriting.",
        "ru": "Укажите примечание по частичному получению наличных.",
    },
    "staff.delivery.note_required": {
        "en": "A note is required for this cash exception.",
        "uz": "Bu naqd pul istisnosi uchun izoh majburiy.",
        "ru": "Для этого исключения по наличным требуется примечание.",
    },
    "staff.delivery.submit_reconciliation": {
        "en": "Submit Reconciliation",
        "uz": "Yarashtiruvni yuborish",
        "ru": "Отправить сверку",
    },
    "staff.delivery.handoff_expected_cash": {
        "en": "Handoff all expected cash",
        "uz": "Kutilgan naqdning hammasini topshirish",
        "ru": "Сдать всю ожидаемую наличность",
    },
    "staff.delivery.edit_reconciliation_cash": {
        "en": "Enter different amount",
        "uz": "Boshqa summani kiritish",
        "ru": "Ввести другую сумму",
    },
    "staff.delivery.enter_declared_cash": {
        "en": "Enter the counted cash amount only if it differs from expected cash.",
        "uz": "Faqat kutilgan naqd puldan farq qilsa, sanalgan summani kiriting.",
        "ru": "Введите пересчитанную сумму только если она отличается от ожидаемой.",
    },
    "staff.delivery.reconciliation_submitted": {
        "en": "Reconciliation submitted.",
        "uz": "Yarashtiruv yuborildi.",
        "ru": "Сверка отправлена.",
    },
    "staff.delivery.handoff_remaining_cash": {
        "en": "Submit remaining {amount}",
        "uz": "Qolgan {amount} ni topshirish",
        "ru": "Сдать оставшиеся {amount}",
    },
    "staff.delivery.remaining_to_submit": {
        "en": "Remaining to submit",
        "uz": "Topshiriladigan qolgan summa",
        "ru": "Осталось сдать",
    },
    "staff.delivery.reconciliation_partial_recorded": {
        "en": "Partial handoff recorded. The session stays open until the remaining cash is submitted.",
        "uz": "Qisman topshirish yozib olindi. Qolgan summa topshirilmaguncha sessiya ochiq qoladi.",
        "ru": "Частичная сдача записана. Сессия остаётся открытой, пока не сдадите остаток.",
    },
    "staff.delivery.expected_cash_label": {
        "en": "Expected cash",
        "uz": "Kutilgan naqd pul",
        "ru": "Ожидаемые наличные",
    },
    "staff.delivery.expected_cash_on_hand_label": {
        "en": "Expected cash on hand",
        "uz": "Qo'lda bo'lishi kerak bo'lgan naqd pul",
        "ru": "Ожидаемая сумма на руках",
    },
    "staff.delivery.declared_cash_label": {
        "en": "Declared cash",
        "uz": "Topshirilgan naqd pul",
        "ru": "Заявленные наличные",
    },
    "staff.delivery.cash_variance_label": {
        "en": "Variance",
        "uz": "Farq",
        "ru": "Расхождение",
    },
    "staff.delivery.session_age_days": {
        "en": "Session age: {days} day(s)",
        "uz": "Sessiya yoshi: {days} kun",
        "ru": "Возраст сессии: {days} дн.",
    },
    "staff.delivery.reconciliation_warning_due": {
        "en": "This cash session is 7+ days old. Please hand off cash when possible.",
        "uz": "Bu naqd sessiya 7 kundan oshdi. Imkon bo'lsa naqdni topshiring.",
        "ru": "Этой сессии наличных 7+ дней. По возможности сдайте наличные.",
    },
    "staff.command.start": {
        "en": "Start bot and authenticate",
        "uz": "Botni ishga tushirish va kirish",
        "ru": "Запустить бота и войти",
    },
    "staff.command.menu": {
        "en": "Show main menu",
        "uz": "Asosiy menyuni korsatish",
        "ru": "Показать главное меню",
    },
    "staff.command.help": {
        "en": "Show help",
        "uz": "Yordamni korsatish",
        "ru": "Показать помощь",
    },
    "staff.command.language": {
        "en": "Change language",
        "uz": "Tilni ozgartirish",
        "ru": "Сменить язык",
    },
    "staff.error.api.validation": {
        "en": "Please check the entered data and try again.",
        "uz": "Kiritilgan malumotlarni tekshirib, qayta urinib koring.",
        "ru": "Проверьте введенные данные и попробуйте снова.",
    },
    "staff.error.api.auth_failed": {
        "en": "Authentication failed. Please login again.",
        "uz": "Autentifikatsiya amalga oshmadi. Qayta kiring.",
        "ru": "Ошибка авторизации. Войдите снова.",
    },
    "staff.error.api.forbidden": {
        "en": "You do not have permission for this action.",
        "uz": "Bu amal uchun sizda ruxsat yoq.",
        "ru": "У вас нет прав для этого действия.",
    },
    "staff.error.api.account_deactivated": {
        "en": "Your delivery account has been deactivated. Please contact your administrator.",
        "uz": "Yetkazib berish hisobingiz faolsizlantirilgan. Iltimos, administrator bilan bog'laning.",
        "ru": "Ваша учётная запись курьера деактивирована. Пожалуйста, свяжитесь с администратором.",
    },
    "staff.error.api.not_found": {
        "en": "Requested data was not found.",
        "uz": "Sorangan malumot topilmadi.",
        "ru": "Запрошенные данные не найдены.",
    },
    "staff.error.api.conflict": {
        "en": "This action cannot be completed because of a conflict.",
        "uz": "Bu amalni bajarib bolmadi, tizimda ziddiyat bor.",
        "ru": "Не удалось выполнить действие из-за конфликта.",
    },
    # Rendered when the place-scope lock ladder times out (BOTTLE_SCOPE_LOCK_TIMEOUT).
    # Says WHY and says RETRY — a driver at the door needs to know the submission
    # was not recorded and that pressing again shortly will work.
    "staff.error.api.scope_busy": {
        "en": "This address is being updated by an administrator right now. Nothing was saved — please try again in a few seconds.",
        "uz": "Bu manzilni hozir administrator tahrirlayapti. Hech narsa saqlanmadi — bir necha soniyadan keyin qayta urinib koring.",
        "ru": "Этот адрес сейчас редактирует администратор. Ничего не сохранено — повторите попытку через несколько секунд.",
    },
    "staff.error.api.invalid_input": {
        "en": "Invalid input. Please correct it and try again.",
        "uz": "Noto'gri malumot kiritildi. Iltimos, tuzatib qayta urinib koring.",
        "ru": "Некорректный ввод. Исправьте и повторите.",
    },
    "staff.error.api.bottle_session_required": {
        "en": "You have no open bottle session. Open a new session to continue delivering this order.",
        "uz": "Sizda ochiq idish sessiyasi yoq. Bu buyurtmani davom ettirish uchun yangi sessiya oching.",
        "ru": "У вас нет открытой сессии по таре. Откройте новую сессию, чтобы продолжить доставку этого заказа.",
    },
    "staff.error.api.bottle_session_capacity_exceeded": {
        "en": "Your current bottle session does not have enough bottles for this order. Load more bottles, then try again.",
        "uz": "Joriy sessiyangizda bu buyurtma uchun yetarli idish yoq. Avval idish yuklang, keyin qayta urinib koring.",
        "ru": "В вашей текущей сессии недостаточно тары для этого заказа. Загрузите больше тары и повторите.",
    },
    # Referenced at staff_bot/handlers/tryouts.py:138 since the delivery-zone
    # SSOT check landed, but never seeded under the `staff_bot` CATEGORY. It
    # does exist in scripts/seed_tryout_translations.py — under category
    # `api`, which staff_bot's i18n never loads, so the bot could not see it.
    #
    # That single string held `/health` at 503: webhook_server.py:194 fails
    # the WHOLE service on any missing required key, so the container reported
    # docker-unhealthy indefinitely AND the database check living in that same
    # endpoint became unreadable — nobody could tell "missing a label" from
    # "database is gone".
    #
    # Verified 2026-08-13 to be the ONLY uncovered key of 434, counting the
    # union of all three staff_bot seed scripts (this one plus
    # seed_place_group_staff_translations.py and
    # seed_staff_over_returned_translations.py). Do NOT add place/cluster/
    # over-returned keys here: those are owned by the satellite scripts and
    # tests/integration/test_place_i18n_render_e2e.py enforces that boundary.
    "staff.tryout.outside_delivery_area": {
        "en": "That address is outside our delivery area. Please send a different location.",
        "uz": "Bu manzil yetkazib berish hududimizdan tashqarida. Iltimos, boshqa manzil yuboring.",
        "ru": "Этот адрес вне зоны доставки. Пожалуйста, отправьте другое местоположение.",
    },
    "staff.tryout.tasks_title": {
        "en": "Try-out Task Pool",
        "uz": "Sinov vazifalari",
        "ru": "Список задач по пробным выдачам",
    },
    "staff.tryout.no_tasks": {
        "en": "No try-out tasks are available right now.",
        "uz": "Hozircha sinov vazifalari yoq.",
        "ru": "Сейчас нет задач по пробным выдачам.",
    },
    "staff.tryout.active_title": {
        "en": "My Active Try-outs",
        "uz": "Mening faol sinovlarim",
        "ru": "Мои активные пробные выдачи",
    },
    "staff.tryout.no_active": {
        "en": "No active try-outs with outstanding bottles.",
        "uz": "Qaytarilishi kerak bo'lgan idishlari bor faol sinovlar yoq.",
        "ru": "Нет активных пробных выдач с невозвращенной тарой.",
    },
    "staff.tryout.task_type": {
        "en": "Task type",
        "uz": "Vazifa turi",
        "ru": "Тип задачи",
    },
    "staff.tryout.task_status": {
        "en": "Task status",
        "uz": "Vazifa holati",
        "ru": "Статус задачи",
    },
    "staff.tryout.outstanding": {
        "en": "Outstanding bottles",
        "uz": "Qaytishi kerak bolgan butilkalar",
        "ru": "Невозвращенная тара",
    },
    "staff.tryout.accept_task": {
        "en": "Accept Task",
        "uz": "Vazifani olish",
        "ru": "Принять задачу",
    },
    "staff.tryout.complete_handoff": {
        "en": "Complete Handoff",
        "uz": "Topshirishni yakunlash",
        "ru": "Завершить передачу",
    },
    "staff.tryout.record_pickup": {
        "en": "Record Pickup",
        "uz": "Qaytarishni kiritish",
        "ru": "Зафиксировать возврат",
    },
    "staff.tryout.view_tryout": {
        "en": "View Try-out",
        "uz": "Sinovni korish",
        "ru": "Открыть пробную выдачу",
    },
    "staff.tryout.open_tasks": {
        "en": "Open Tasks",
        "uz": "Ochiq vazifalar",
        "ru": "Открытые задачи",
    },
    "staff.tryout.task_accepted": {
        "en": "Try-out task accepted.",
        "uz": "Sinov vazifasi olindi.",
        "ru": "Задача по пробной выдаче принята.",
    },
    "staff.tryout.handoff_recorded": {
        "en": "Try-out handoff recorded.",
        "uz": "Sinov topshiruvi qayd etildi.",
        "ru": "Передача пробной выдачи зафиксирована.",
    },
    "staff.tryout.task_not_found": {
        "en": "Try-out task not found.",
        "uz": "Sinov vazifasi topilmadi.",
        "ru": "Задача по пробной выдаче не найдена.",
    },
    "staff.tryout.tryout_not_found": {
        "en": "Try-out not found.",
        "uz": "Sinov topilmadi.",
        "ru": "Пробная выдача не найдена.",
    },
    "staff.tryout.pickup_prompt": {
        "en": "Send returned bottle quantities one per line.",
        "uz": "Qaytgan butilkalarni har qatorda yuboring.",
        "ru": "Отправьте возвращенную тару по одной строке.",
    },
    "staff.tryout.pickup_invalid_format": {
        "en": "Invalid format. Use product_id:units on each line.",
        "uz": "Format notogri. Har qatorda product_id:units korinishida yuboring.",
        "ru": "Неверный формат. Используйте идентификатор товара и количество в каждой строке.",
    },
    "staff.tryout.pickup_recorded": {
        "en": "Bottle pickup recorded.",
        "uz": "Butilka qaytarilishi qayd etildi.",
        "ru": "Возврат тары зафиксирован.",
    },
    "staff.tryout.pickup_select_product": {
        "en": "Choose a product and then tap the returned quantity.",
        "uz": "Mahsulotni tanlang, keyin qaytgan miqdorni bosing.",
        "ru": "Выберите товар, затем нажмите количество возвращенной тары.",
    },
    "staff.tryout.pickup_selected": {
        "en": "selected: {selected}",
        "uz": "tanlandi: {selected}",
        "ru": "выбрано: {selected}",
    },
    "staff.tryout.pickup_not_selected": {
        "en": "not selected yet",
        "uz": "hali tanlanmagan",
        "ru": "пока не выбрано",
    },
    "staff.tryout.pickup_select_quantity": {
        "en": "Select how many bottles were returned for {product}.",
        "uz": "{product} uchun nechta butilka qaytganini tanlang.",
        "ru": "Выберите, сколько бутылей вернули по товару {product}.",
    },
    "staff.tryout.pickup_current_quantity": {
        "en": "Selected now: {quantity} of {outstanding}",
        "uz": "Hozir tanlangan: {quantity} / {outstanding}",
        "ru": "Сейчас выбрано: {quantity} из {outstanding}",
    },
    "staff.tryout.pickup_submit": {
        "en": "Record Selected Bottles",
        "uz": "Tanlangan butilkalarni qayd etish",
        "ru": "Зафиксировать выбранную тару",
    },
    "staff.tryout.pickup_clear_selection": {
        "en": "Clear Selection",
        "uz": "Tanlovni tozalash",
        "ru": "Очистить выбор",
    },
    "staff.tryout.pickup_fill_all": {
        "en": "Fill All Outstanding",
        "uz": "Barchasini to'ldirish",
        "ru": "Заполнить весь остаток",
    },
    "staff.tryout.pickup_clear_product": {
        "en": "Remove This Product",
        "uz": "Bu mahsulotni olib tashlash",
        "ru": "Убрать этот товар",
    },
    "staff.tryout.pickup_nothing_selected": {
        "en": "Select at least one returned quantity first.",
        "uz": "Avval kamida bitta qaytgan miqdorni tanlang.",
        "ru": "Сначала выберите хотя бы одно возвращенное количество.",
    },
    "staff.tryout.pickup_no_outstanding": {
        "en": "There are no outstanding bottles left for this try-out.",
        "uz": "Bu sinov bo'yicha qaytishi kerak bo'lgan butilkalar qolmagan.",
        "ru": "По этой пробной выдаче не осталось невозвращенной тары.",
    },
    "staff.tryout.pickup_use_buttons": {
        "en": "Use the buttons below to record bottle returns.",
        "uz": "Butilka qaytarilishini qayd etish uchun pastdagi tugmalardan foydalaning.",
        "ru": "Используйте кнопки ниже, чтобы зафиксировать возврат тары.",
    },
    "staff.tryout.enter_phone": {
        "en": "Enter the customer's phone number.",
        "uz": "Mijozning telefon raqamini kiriting.",
        "ru": "Введите номер телефона клиента.",
    },
    "staff.tryout.enter_name": {
        "en": "Enter the customer's first name.",
        "uz": "Mijozning ismini kiriting.",
        "ru": "Введите имя клиента.",
    },
    "staff.tryout.enter_address": {
        "en": "Enter the try-out delivery address.",
        "uz": "Sinov topshiriladigan manzilni kiriting.",
        "ru": "Введите адрес пробной выдачи.",
    },
    "staff.tryout.enter_address_or_location": {
        "en": "Enter the try-out delivery address or send your location.",
        "uz": "Sinov topshiriladigan manzilni kiriting yoki joylashuvingizni yuboring.",
        "ru": "Введите адрес пробной выдачи или отправьте геолокацию.",
    },
    "staff.tryout.send_location": {
        "en": "Send Location",
        "uz": "Joylashuvni yuborish",
        "ru": "Отправить геолокацию",
    },
    "staff.tryout.address_received": {
        "en": "Address saved. Now choose the try-out products.",
        "uz": "Manzil saqlandi. Endi sinov mahsulotlarini tanlang.",
        "ru": "Адрес сохранен. Теперь выберите товары для пробной выдачи.",
    },
    "staff.tryout.invalid_address": {
        "en": "Address is too short. Please enter a fuller address.",
        "uz": "Manzil juda qisqa. Iltimos, toliqroq manzil kiriting.",
        "ru": "Адрес слишком короткий. Введите полный адрес.",
    },
    "staff.tryout.location_received": {
        "en": "Location received: {address}",
        "uz": "Joylashuv qabul qilindi: {address}",
        "ru": "Геолокация получена: {address}",
    },
    "staff.tryout.location_geocode_failed": {
        "en": "Location received, but the address could not be resolved. Please type the address manually.",
        "uz": "Joylashuv qabul qilindi, lekin manzil aniqlanmadi. Iltimos, manzilni qo'lda kiriting.",
        "ru": "Геолокация получена, но адрес определить не удалось. Пожалуйста, введите адрес вручную.",
    },
    "staff.tryout.select_products": {
        "en": "Select try-out products.",
        "uz": "Sinov mahsulotlarini tanlang.",
        "ru": "Выберите товары для пробной выдачи.",
    },
    "staff.tryout.select_quantity": {
        "en": "Select quantity for {product}.",
        "uz": "{product} uchun miqdorni tanlang.",
        "ru": "Выберите количество для {product}.",
    },
    "staff.tryout.current_quantity": {
        "en": "Current quantity: {quantity}",
        "uz": "Hozirgi miqdor: {quantity}",
        "ru": "Текущее количество: {quantity}",
    },
    "staff.tryout.selected_products": {
        "en": "Selected products",
        "uz": "Tanlangan mahsulotlar",
        "ru": "Выбранные товары",
    },
    "staff.tryout.done_selecting": {
        "en": "Done Selecting",
        "uz": "Tanlashni yakunlash",
        "ru": "Завершить выбор",
    },
    "staff.tryout.add_more_products": {
        "en": "Add More Products",
        "uz": "Yana mahsulot qoshish",
        "ru": "Добавить еще товары",
    },
    "staff.tryout.no_products_selected": {
        "en": "Select at least one product first.",
        "uz": "Avval kamida bitta mahsulot tanlang.",
        "ru": "Сначала выберите хотя бы один товар.",
    },
    "staff.tryout.confirm_create_title": {
        "en": "Confirm Try-out",
        "uz": "Sinovni tasdiqlash",
        "ru": "Подтвердите пробную выдачу",
    },
    "staff.tryout.product_not_found": {
        "en": "Selected product was not found.",
        "uz": "Tanlangan mahsulot topilmadi.",
        "ru": "Выбранный товар не найден.",
    },
    "staff.tryout.remove_product": {
        "en": "Remove Product",
        "uz": "Mahsulotni olib tashlash",
        "ru": "Убрать товар",
    },
    "staff.tryout.created_success": {
        "en": "Try-out created successfully: {tryout_number}",
        "uz": "Sinov muvaffaqiyatli yaratildi: {tryout_number}",
        "ru": "Пробная выдача создана: {tryout_number}",
    },
    "staff.error.api.rate_limited": {
        "en": "Too many requests. Please wait and try again.",
        "uz": "Juda kop sorov yuborildi. Biroz kutib qayta urinib koring.",
        "ru": "Слишком много запросов. Подождите и попробуйте снова.",
    },
    "staff.error.api.service_unavailable": {
        "en": "Service is temporarily unavailable. Please try later.",
        "uz": "Xizmat vaqtincha mavjud emas. Keyinroq urinib koring.",
        "ru": "Сервис временно недоступен. Попробуйте позже.",
    },
    "staff.error.api.already_taken": {
        "en": "This order has already been taken by another courier.",
        "uz": "Bu buyurtma allaqachon boshqa kuryer tomonidan olingan.",
        "ru": "Этот заказ уже принят другим курьером.",
    },
    "staff.error.api.driver_cod_blocked": {
        "en": "You cannot accept new cash-on-delivery orders until your pending cash reconciliation is resolved. Please complete Cash Reconciliation first.",
        "uz": "Naqd pul yarashtiruvi hal qilinmaguncha siz yangi naqd tolovli buyurtmalarni qabul qila olmaysiz. Iltimos, avval Naqd pul yarashtiruvini bajaring.",
        "ru": "Вы не можете принимать новые заказы с оплатой наличными, пока не завершите сверку наличных. Пожалуйста, сначала выполните Сверку наличных.",
    },
    "staff.error.api.cod_debt_limit_reached": {
        "en": "This customer has reached the maximum number of unpaid cash-on-delivery debts and cannot take on more until earlier debts are settled.",
        "uz": "Bu mijoz to'lanmagan naqd to'lov qarzlarining maksimal soniga yetdi va eski qarzlar to'lanmagunicha yangi qarz olishi mumkin emas.",
        "ru": "Этот клиент достиг максимального количества непогашенных задолженностей по наложенному платежу — новые долги невозможны, пока не погашены прежние.",
    },
    "staff.error.api.invalid_invite": {
        "en": "Invite link is invalid or expired.",
        "uz": "Taklif havolasi yaroqsiz yoki muddati tugagan.",
        "ru": "Ссылка-приглашение неверна или просрочена.",
    },
    "staff.error.api.unexpected": {
        "en": "Unexpected server response. Please try again.",
        "uz": "Kutilmagan server javobi. Qayta urinib koring.",
        "ru": "Неожиданный ответ сервера. Попробуйте снова.",
    },
    # Rendered ONLY for a TRANSPORT_AMBIGUOUS bottle collection / fine failure —
    # the request may already have reached the backend and been committed, and
    # the response was what got lost. Redoing the flow by hand mints a NEW
    # intent token, which the server-side dedup fence cannot collapse, so the
    # driver must check before repeating. Deliberately does NOT say "try again":
    # the generic transport copy (`service_unavailable`, "please try later") is
    # the one instruction that turns a possible duplicate into a certain one.
    # Kept under 200 characters — BaseHandler._notify_user answers a callback
    # query with show_alert, and Telegram rejects longer alert text.
    "staff.error.api.maybe_recorded": {
        "en": "Connection lost after sending. This may ALREADY be recorded — check the customer's bottle statement before entering it again.",
        "uz": "Yuborilgandan keyin aloqa uzildi. Bu ALLAQACHON yozilgan bo'lishi mumkin — qayta kiritishdan oldin mijozning idish hisobini tekshiring.",
        "ru": "Связь пропала после отправки. Возможно, это УЖЕ записано — проверьте отчёт по таре клиента, прежде чем вводить снова.",
    },

    # --- Bottle tracking translations ---
    "staff.menu.bottle_collection": {
        "en": "Bottle Collection",
        "uz": "Idish yigish",
        "ru": "Сбор тары",
    },
    # The statement lists one line per distinct PLACE (the address group when one
    # exists, else the address), and its header total is the signed sum over
    # `cluster_scopes` — one row per place, so a shared workplace is counted ONCE
    # no matter how many members own an address there. "Total bottles" read as a
    # per-customer figure and invited the driver to add the body lines up
    # differently.
    "staff.delivery.bottle_statement_title": {
        "en": "Bottles by place",
        "uz": "Joylar bo'yicha idish hisoboti",
        "ru": "Тара по местам",
    },
    "staff.delivery.total_bottles": {
        "en": "Bottles across places",
        "uz": "Barcha joylar bo'yicha jami",
        "ru": "Всего по всем местам",
    },
    "staff.delivery.active_fines": {
        "en": "Active fines",
        "uz": "Faol jarimalar",
        "ru": "Активные штрафы",
    },
    # ZERO only. The over-returned (negative) case has its own copy
    # (staff.delivery.place_over_returned / …_over_returned_hint) — this string
    # used to cover zero, negative AND lookup misses, so a driver could not tell
    # "nothing out here" from "three too many came back".
    "staff.delivery.no_bottle_balance": {
        "en": "No bottles are currently out.",
        "uz": "Hozircha chiqarilgan idish yo'q.",
        "ru": "Сейчас тары на руках нет.",
    },
    "staff.delivery.bottle_collection_search_prompt": {
        "en": "Enter a customer name or phone number to find their bottle balance.",
        "uz": "Mijoz ismi yoki telefon raqamini kiriting.",
        "ru": "Введите имя клиента или номер телефона для поиска баланса тары.",
    },
    # The search is a plain customer lookup (`only_with_open_cod=False`) whose
    # results resolve to PLACES, so "customers with bottles" was wrong twice
    # over: it does not filter on bottles, and a match may be any member of a
    # shared place.
    "staff.delivery.no_customer_bottle_results": {
        "en": "No customers found for \"{query}\".",
        "uz": "\"{query}\" bo'yicha mijoz topilmadi.",
        "ru": "Клиенты по запросу «{query}» не найдены.",
    },
    "staff.delivery.view_bottle_balance": {
        "en": "View Bottle Balance",
        "uz": "Idish balansini korish",
        "ru": "Баланс тары",
    },
    "staff.delivery.bottle_address_selected": {
        "en": "Address selected. Choose an action:",
        "uz": "Manzil tanlandi. Amalni tanlang:",
        "ru": "Адрес выбран. Выберите действие:",
    },
    "staff.delivery.collect_bottles": {
        "en": "Collect Bottles",
        "uz": "Idish yigish",
        "ru": "Собрать тару",
    },
    "staff.delivery.issue_bottle_fine": {
        "en": "Issue Fine",
        "uz": "Jarima berish",
        "ru": "Выписать штраф",
    },
    "staff.delivery.enter_bottle_collection_qty": {
        "en": "Tap the number of bottles you collected:",
        "uz": "Yigilgan idishlar sonini tanlang:",
        "ru": "Выберите количество собранной тары:",
    },
    "staff.delivery.enter_bottle_collection_note": {
        "en": "Add a note for this collection, or tap Save without note:",
        "uz": "Izoh kiriting yoki «Izohsiz saqlash»ni bosing:",
        "ru": "Добавьте примечание или нажмите «Сохранить без примечания»:",
    },
    "staff.delivery.collect_all": {
        "en": "All",
        "uz": "Hammasi",
        "ru": "Все",
    },
    "staff.delivery.save_without_note": {
        "en": "Save without note",
        "uz": "Izohsiz saqlash",
        "ru": "Сохранить без примечания",
    },
    "staff.delivery.bottle_search_results_title": {
        "en": "Found {count} customer(s). Tap one to see the bottles at their places:",
        "uz": "{count} ta mijoz topildi. Joylaridagi idishlarni korish uchun bosing:",
        "ru": "Найдено клиентов: {count}. Нажмите, чтобы увидеть тару по их местам:",
    },
    "staff.delivery.invalid_bottle_count": {
        "en": "Please enter a valid positive number.",
        "uz": "Iltimos, musbat son kiriting.",
        "ru": "Пожалуйста, введите положительное число.",
    },
    # `{remaining}` is the PLACE's remainder, not this account's — at a shared
    # workplace it still counts a coworker's empties. Phrased to mirror its
    # over-returned sibling (staff.delivery.bottle_collection_recorded_over_returned),
    # which handles the negative arm.
    "staff.delivery.bottle_collection_recorded": {
        "en": "Collected {quantity} bottle(s). This place now holds {remaining}.",
        "uz": "{quantity} ta idish yigib olindi. Endi bu joyda {remaining} ta idish bor.",
        "ru": "Собрано {quantity} ед. тары. Теперь в этом месте {remaining}.",
    },
    "staff.delivery.enter_fine_bottle_qty": {
        "en": "How many bottles to fine for?",
        "uz": "Necha idish uchun jarima?",
        "ru": "За сколько единиц тары штраф?",
    },
    "staff.delivery.enter_fine_amount": {
        "en": "Enter the fine amount (in UZS):",
        "uz": "Jarima miqdorini kiriting (UZS):",
        "ru": "Введите сумму штрафа (в UZS):",
    },
    "staff.delivery.enter_fine_note": {
        "en": "Add a note for this fine:",
        "uz": "Jarima uchun izoh kiriting:",
        "ru": "Добавьте примечание для штрафа:",
    },
    "staff.delivery.invalid_amount": {
        "en": "Please enter a valid amount.",
        "uz": "Iltimos, togri miqdor kiriting.",
        "ru": "Пожалуйста, введите корректную сумму.",
    },
    "staff.delivery.bottle_fine_created": {
        "en": "Fine created: {quantity} bottle(s), amount {amount}.",
        "uz": "Jarima yaratildi: {quantity} ta idish, miqdori {amount}.",
        "ru": "Штраф создан: {quantity} ед. тары, сумма {amount}.",
    },
    # Bottle return during delivery.
    # NOTE: `staff.delivery.bottles_return*ed*_prompt` used to be curated here and
    # had ZERO readers — the handler calls `staff.delivery.bottles_return_prompt`
    # (no "ed"). It was a curated typo that only ever produced a dead row.
    "staff.delivery.bottles_all_returned": {
        "en": "All {count} bottles returned",
        "uz": "Barcha {count} ta idish qaytarildi",
        "ru": "Все {count} ед. тары возвращены",
    },
    "staff.delivery.bottles_enter_count": {
        "en": "Enter count",
        "uz": "Sonini kiriting",
        "ru": "Ввести количество",
    },
    "staff.delivery.bottles_none_returned": {
        "en": "No bottles returned",
        "uz": "Idish qaytarilmadi",
        "ru": "Тара не возвращена",
    },
    "staff.delivery.bottles_zero_returned": {
        "en": "0 bottles returned",
        "uz": "0 ta idish qaytarildi",
        "ru": "Возвращено 0 бутылок",
    },
    "staff.delivery.enter_bottle_count": {
        "en": "Enter the number of bottles returned:",
        "uz": "Qaytarilgan idishlar sonini kiriting:",
        "ru": "Введите количество возвращённой тары:",
    },
    # Warehouse accountability flows
    "staff.menu.log_bottles_loaded": {
        "en": "Log Bottles Loaded (18.9 L)",
        "uz": "Yuklangan (18,9 l) idishlarni kiritish",
        "ru": "Внести загруженные бутылки (18,9 л)",
    },
    "staff.menu.return_to_warehouse": {
        "en": "Return to Warehouse",
        "uz": "Omborga qaytarish",
        "ru": "Возврат на склад",
    },
    "staff.menu.my_bottle_accountability": {
        "en": "My Bottle Accountability (18.9 L)",
        "uz": "Mening (18,9 l) idish hisobotim",
        "ru": "Мой учёт тары (18,9 л)",
    },
    "staff.delivery.enter_bottles_loaded_qty": {
        "en": "Enter the number of bottles (18.9 L) you loaded from the warehouse:",
        "uz": "Ombordan nechta (18,9 l) idish yuklab olganingizni kiriting:",
        "ru": "Введите количество бутылок (18,9 л), загруженных со склада:",
    },
    "staff.delivery.bottles_loaded_recorded": {
        "en": "\u2705 Recorded: {quantity} (18.9 L) bottle(s) loaded from warehouse.",
        "uz": "\u2705 Qayd etildi: ombordan {quantity} ta (18,9 l) idish yuklandi.",
        "ru": "\u2705 Записано: {quantity} ед. тары (18,9 л) загружено со склада.",
    },
    "staff.delivery.enter_bottles_returned_qty": {
        "en": "Enter the number of bottles (18.9 L) you returned to the warehouse:",
        "uz": "Omborga nechta (18,9 l) idish qaytarganingizni kiriting:",
        "ru": "Введите количество бутылок (18,9 л), возвращённых на склад:",
    },
    "staff.delivery.bottles_returned_wh_recorded": {
        "en": "\u2705 Recorded: {quantity} (18.9 L) bottle(s) returned to warehouse.",
        "uz": "\u2705 Qayd etildi: {quantity} ta (18,9 l) idish omborga qaytarildi.",
        "ru": "\u2705 Записано: {quantity} ед. тары (18,9 л) возвращено на склад.",
    },
    "staff.delivery.bottle_accountability_no_data": {
        "en": "No (18.9 L) bottle accountability data yet.",
        "uz": "(18,9 l) Idish hisobi yo'q.",
        "ru": "Данных по учёту тары (18,9 л) нет.",
    },
    # Bottle session menu buttons
    "staff.menu.transfer_bottles_to_driver": {
        "en": "Transfer (18.9 L) bottles to other driver",
        "uz": "Boshqa haydovchiga (18,9 l) idish o'tkazish",
        "ru": "Передать (18,9 л) бутылки другому водителю",
    },
    "staff.menu.incoming_transfers": {
        "en": "Incoming transfers",
        "uz": "Kiruvchi o'tkazmalar",
        "ru": "Входящие передачи",
    },
    # Bottle session state messages
    "staff.delivery.bottle_session_already_open": {
        "en": "🚫 <b>Cannot start a new load.</b>\n\nYou already have an active session started at <b>{started}</b> with <b>{loaded}</b> (18.9 L) bottles loaded.\n\nReturn to the warehouse and close your current session first.",
        "uz": "🚫 <b>Yangi yuklash boshlash mumkin emas.</b>\n\nSizda allaqachon <b>{started}</b> da boshlangan va <b>{loaded}</b> ta (18,9 l) idish yuklangan faol sessiya mavjud.\n\nAvval omborga qayting va joriy sessiyangizni yoping.",
        "ru": "🚫 <b>Нельзя начать новую загрузку.</b>\n\nУ вас уже есть активная сессия, начатая в <b>{started}</b> с <b>{loaded}</b> (18,9 л) бутылками.\n\nВернитесь на склад и закройте текущую сессию.",
    },
    "staff.delivery.bottle_session_already_open_short": {
        "en": "🚫 You already have an open session. Close it before loading new (18.9 L) bottles.",
        "uz": "🚫 Sizda allaqachon ochiq sessiya bor. Yangi (18,9 l) idish yuklashdan oldin uni yoping.",
        "ru": "🚫 У вас уже есть открытая сессия. Закройте её перед загрузкой новых (18,9 л) бутылок.",
    },
    "staff.delivery.bottle_session_opened": {
        "en": "✅ <b>Session opened!</b>\n\n📦 Loaded: <b>{count}</b> (18.9 L) bottles\nSession ref: <code>{ref}</code>\n\nDeliver your orders and return to WH when done.",
        "uz": "✅ <b>Sessiya ochildi!</b>\n\n📦 Yuklandi: <b>{count}</b> ta (18,9 l) idish\nSessiya raqami: <code>{ref}</code>\n\nBuyurtmalaringizni yetkazing va tugagach omborga qayting.",
        "ru": "✅ <b>Сессия открыта!</b>\n\n📦 Загружено: <b>{count}</b> (18,9 л) бутылок\nНомер сессии: <code>{ref}</code>\n\nВыполняйте заказы и возвращайтесь на склад по завершении.",
    },
    "staff.delivery.no_active_bottle_session": {
        "en": "ℹ️ You have no active bottle session. Nothing to close.",
        "uz": "ℹ️ Sizda faol idish sessiyasi yo'q. Yopish uchun hech narsa yo'q.",
        "ru": "ℹ️ У вас нет активной сессии бутылок. Нечего закрывать.",
    },
    "staff.delivery.bottle_session_closed": {
        "en": "✅ <b>Session closed.</b>\n\n🏢 Returned to WH: <b>{count}</b>\n{disc_line}\nRef: <code>{ref}</code>",
        "uz": "✅ <b>Sessiya yopildi.</b>\n\n🏢 Omborga qaytarildi: <b>{count}</b>\n{disc_line}\nRaqam: <code>{ref}</code>",
        "ru": "✅ <b>Сессия закрыта.</b>\n\n🏢 Возвращено на склад: <b>{count}</b>\n{disc_line}\nНомер: <code>{ref}</code>",
    },
    "staff.delivery.discrepancy_zero": {
        "en": "✅ Discrepancy: <b>0</b>  🎯",
        "uz": "✅ Farq: <b>0</b>  🎯",
        "ru": "✅ Расхождение: <b>0</b>  🎯",
    },
    "staff.delivery.discrepancy_nonzero": {
        "en": "⚠️ Discrepancy: <b>{discrepancy}</b> (18.9 L) bottles unaccounted",
        "uz": "⚠️ Farq: <b>{discrepancy}</b> ta (18,9 l) idish hisoblanmagan",
        "ru": "⚠️ Расхождение: <b>{discrepancy}</b> (18,9 л) бутылок не учтено",
    },
    # Bottle transfer messages
    "staff.delivery.no_bottles_to_transfer": {
        "en": "🚫 You have no bottles available to transfer.",
        "uz": "🚫 O'tkazish uchun idishlaringiz yo'q.",
        "ru": "🚫 У вас нет бутылок для передачи.",
    },
    "staff.delivery.no_active_drivers": {
        "en": "No other active drivers found.",
        "uz": "Boshqa faol haydovchilar topilmadi.",
        "ru": "Других активных водителей не найдено.",
    },
    "staff.delivery.select_transfer_driver": {
        "en": "Select the driver to transfer bottles to.\n(You have <b>{available}</b> bottles available)",
        "uz": "Idish o'tkazish uchun haydovchini tanlang.\n(Sizda <b>{available}</b> ta idish mavjud)",
        "ru": "Выберите водителя для передачи бутылок.\n(У вас <b>{available}</b> бутылок доступно)",
    },
    "staff.delivery.enter_transfer_qty": {
        "en": "📦 How many bottles are you transferring?\n(You have <b>{available}</b> available)",
        "uz": "📦 Nechta idish o'tkazmoqdasiz?\n(Sizda <b>{available}</b> ta mavjud)",
        "ru": "📦 Сколько бутылок вы передаёте?\n(У вас <b>{available}</b> доступно)",
    },
    "staff.delivery.transfer_qty_exceeds_available": {
        "en": "⚠️ You only have {available} bottle(s) available. Enter a smaller number.",
        "uz": "⚠️ Sizda faqat {available} ta idish mavjud. Kichikroq raqam kiriting.",
        "ru": "⚠️ У вас всего {available} бутылок. Введите меньшее число.",
    },
    "staff.delivery.bottle_transfer_initiated": {
        "en": "✅ <b>Transfer initiated!</b>\n\n📦 Quantity: <b>{qty}</b> bottles\nThe receiving driver will get a notification to confirm.\nRef: <code>{ref}</code>",
        "uz": "✅ <b>O'tkazish boshlandi!</b>\n\n📦 Miqdor: <b>{qty}</b> ta idish\nQabul qiluvchi haydovchi tasdiqlash uchun bildirishnoma oladi.\nRaqam: <code>{ref}</code>",
        "ru": "✅ <b>Передача инициирована!</b>\n\n📦 Количество: <b>{qty}</b> бутылок\nПринимающий водитель получит уведомление для подтверждения.\nНомер: <code>{ref}</code>",
    },
    "staff.delivery.no_pending_transfers": {
        "en": "No pending transfers waiting for your confirmation.",
        "uz": "Tasdiqlashingizni kutayotgan o'tkazmalar yo'q.",
        "ru": "Нет ожидающих подтверждения передач.",
    },
    "staff.delivery.pending_transfers_title": {
        "en": "📥 <b>Pending Incoming Transfers:</b>",
        "uz": "📥 <b>Kutilayotgan kiruvchi o'tkazmalar:</b>",
        "ru": "📥 <b>Ожидающие входящие передачи:</b>",
    },
    "staff.delivery.enter_actual_received_qty": {
        "en": "✏️ How many bottles did you actually receive?\nEnter the count:",
        "uz": "✏️ Aslida nechta idish oldiniz?\nSonni kiriting:",
        "ru": "✏️ Сколько бутылок вы фактически получили?\nВведите количество:",
    },
    "staff.delivery.transfer_confirm_failed": {
        "en": "Failed to confirm transfer. Please try again.",
        "uz": "O'tkazmani tasdiqlash muvaffaqiyatsiz. Qayta urinib ko'ring.",
        "ru": "Не удалось подтвердить передачу. Попробуйте ещё раз.",
    },
    "staff.delivery.transfer_confirmed": {
        "en": "✅ <b>Transfer confirmed!</b>\n\n📥 <b>{qty}</b> bottles added to your session.",
        "uz": "✅ <b>O'tkazma tasdiqlandi!</b>\n\n📥 <b>{qty}</b> ta idish sessiyangizga qo'hildi.",
        "ru": "✅ <b>Передача подтверждена!</b>\n\n📥 <b>{qty}</b> бутылок добавлено в вашу сессию.",
    },
    "staff.delivery.transfer_disputed": {
        "en": "⚠️ <b>Bottle transfer to other driver request filed.</b>\n\nSender declared <b>{declared}</b>, you received <b>{qty}</b>.\nAdmin has been notified. Your session has been credited with <b>{qty}</b> pending resolution.",
        "uz": "⚠️ <b>Idish o'tkazma so'rovi kiritildi.</b>\n\nJo'natuvchi <b>{declared}</b> ta deb ko'rsatdi, siz <b>{qty}</b> ta oldingiz.\nAdmin xabardor qilindi. Sessiyangizga hal bo'lgunga qadar <b>{qty}</b> ta yozildi.",
        "ru": "⚠️ <b>Запрос на передачу бутылки другому водителю подан.</b>\n\nОтправитель указал <b>{declared}</b>, вы получили <b>{qty}</b>.\nАдмин уведомлён. В вашу сессию записано <b>{qty}</b> до разрешения.",
    },
    # Session display labels (used in _format_session)
    "staff.delivery.session_ref_label": {
        "en": "Session",
        "uz": "Sessiya",
        "ru": "Сессия",
    },
    "staff.delivery.session_started_label": {
        "en": "Started",
        "uz": "Boshlangan",
        "ru": "Начата",
    },
    "staff.delivery.bottles_loaded_label": {
        "en": "Loaded",
        "uz": "Yuklangan",
        "ru": "Загружено",
    },
    "staff.delivery.bottles_delivered_label": {
        "en": "Delivered",
        "uz": "Yetkazilgan",
        "ru": "Доставлено",
    },
    "staff.delivery.bottles_collected_label": {
        "en": "Collected",
        "uz": "Yig'ilgan",
        "ru": "Собрано",
    },
    "staff.delivery.bottles_transferred_out_label": {
        "en": "Transferred out",
        "uz": "Chiqib ketgan",
        "ru": "Передано",
    },
    "staff.delivery.bottles_transferred_in_label": {
        "en": "Transferred in",
        "uz": "Kirib kelgan",
        "ru": "Получено",
    },
    "staff.delivery.bottles_on_truck_label": {
        "en": "On truck now",
        "uz": "Hozir mashinada",
        "ru": "На машине сейчас",
    },
    "staff.delivery.bottles_returned_wh_label": {
        "en": "Returned to WH",
        "uz": "Omborga qaytarildi",
        "ru": "Возвращено на склад",
    },
    "staff.delivery.discrepancy_label": {
        "en": "Discrepancy",
        "uz": "Farq",
        "ru": "Расхождение",
    },

    # --- Co-driver session membership ---
    "staff.bottles.session_required_to_accept": {
        "en": "⚠️ A bottle session is required to accept this order.\nPlease start your own session or join a colleague's.",
        "uz": "⚠️ Buyurtmani qabul qilish uchun shisha sessiyasi kerak.\nO'z sessiyangizni boshlang yoki hamkasbingiznikiga qo'shiling.",
        "ru": "⚠️ Для принятия заказа требуется сессия бутылок.\nНачните свою сессию или присоединитесь к сессии коллеги.",
    },
    "staff.bottles.start_session": {
        "en": "▶️ Start My Session",
        "uz": "▶️ O'z sessiyamni boshlash",
        "ru": "▶️ Начать свою сессию",
    },
    "staff.bottles.join_session": {
        "en": "🤝 Join Colleague's Session",
        "uz": "🤝 Hamkasb sessiyasiga qo'shilish",
        "ru": "🤝 Присоединиться к сессии коллеги",
    },
    "staff.bottles.leave_session": {
        "en": "🚪 Leave Session",
        "uz": "🚪 Sessiyadan chiqish",
        "ru": "🚪 Покинуть сессию",
    },
    "staff.bottles.no_open_sessions": {
        "en": "ℹ️ No open sessions available to join right now.",
        "uz": "ℹ️ Hozirda qo'shilish uchun ochiq sessiyalar yo'q.",
        "ru": "ℹ️ Сейчас нет открытых сессий для присоединения.",
    },
    "staff.bottles.choose_session_to_join": {
        "en": "Choose a session to join:",
        "uz": "Qo'shilish uchun sessiyani tanlang:",
        "ru": "Выберите сессию для присоединения:",
    },
    "staff.bottles.join_session_confirm_title": {
        "en": "Join Session?",
        "uz": "Sessiyaga qo'shilasizmi?",
        "ru": "Присоединиться к сессии?",
    },
    "staff.bottles.session_owner": {
        "en": "Session owner",
        "uz": "Sessiya egasi",
        "ru": "Владелец сессии",
    },
    "staff.bottles.bottles_on_truck": {
        "en": "Bottles on truck",
        "uz": "Mashinadagi shishalar",
        "ru": "Бутылок в машине",
    },
    "staff.bottles.join_session_confirm_note": {
        "en": "While joined, your orders will be tracked against this session's inventory.",
        "uz": "Qo'shilgandan so'ng buyurtmalaringiz ushbu sessiya inventariga hisoblanadi.",
        "ru": "После присоединения ваши заказы будут учитываться в рамках этой сессии.",
    },
    "staff.bottles.confirm_join": {
        "en": "Confirm Join",
        "uz": "Qo'shilishni tasdiqlash",
        "ru": "Подтвердить присоединение",
    },
    "staff.bottles.joined_session": {
        "en": "Joined {name}'s session!",
        "uz": "{name} sessiyasiga qo'shildingiz!",
        "ru": "Вы присоединились к сессии {name}!",
    },
    "staff.bottles.joined_session_info": {
        "en": "You can now accept orders. Bottles will be deducted from the shared session.",
        "uz": "Endi buyurtmalarni qabul qilishingiz mumkin. Shishalar umumiy sessiyadan hisoblanadi.",
        "ru": "Теперь вы можете принимать заказы. Бутылки будут списываться из общей сессии.",
    },
    "staff.bottles.left_session": {
        "en": "✅ You have left the session.",
        "uz": "✅ Siz sessiyadan chiqdingiz.",
        "ru": "✅ Вы покинули сессию.",
    },
    "staff.bottles.current_membership_title": {
        "en": "Active Co-Driver Session",
        "uz": "Faol hamkor sessiyasi",
        "ru": "Активная совместная сессия",
    },
    "staff.bottles.current_membership": {
        "en": "Using {name}'s session — <b>{qty}</b> bottles available",
        "uz": "{name} sessiyasida — <b>{qty}</b> shisha mavjud",
        "ru": "Сессия {name} — доступно <b>{qty}</b> бутылок",
    },
    "staff.bottles.session_closed_membership_revoked": {
        "en": "ℹ️ The session you joined has been closed. Start or join a new session when needed.",
        "uz": "ℹ️ Siz qo'shilgan sessiya yopildi. Kerak bo'lganda yangi sessiya boshlang yoki qo'shiling.",
        "ru": "ℹ️ Сессия, к которой вы присоединились, закрыта. При необходимости начните или присоединитесь к новой сессии.",
    },
    "staff.bottles.no_active_membership": {
        "en": "ℹ️ You are not currently joined to any colleague's session.",
        "uz": "ℹ️ Hozirda hech qanday hamkasb sessiyasiga qo'shilmagansiz.",
        "ru": "ℹ️ Вы сейчас не присоединены ни к чьей сессии.",
    },
    "staff.bottles.session_not_found": {
        "en": "❌ Session not found. It may have been closed.",
        "uz": "❌ Sessiya topilmadi. U yopilgan bo'lishi mumkin.",
        "ru": "❌ Сессия не найдена. Возможно, она уже закрыта.",
    },
    "staff.bottles.membership_status_active": {
        "en": "Active",
        "uz": "Faol",
        "ru": "Активен",
    },
    "staff.bottles.membership_status_left": {
        "en": "Left",
        "uz": "Chiqib ketdi",
        "ru": "Покинул",
    },
    "staff.bottles.membership_status_revoked": {
        "en": "Revoked",
        "uz": "Bekor qilindi",
        "ru": "Отозван",
    },
    "staff.bottles.invite_codriver": {
        "en": "👥 Invite Co-driver",
        "uz": "👥 Hamkorni taklif qilish",
        "ru": "👥 Пригласить напарника",
    },
    "staff.bottles.no_drivers_to_invite": {
        "en": "ℹ️ No available drivers to invite. All drivers either have their own session or are already in a session.",
        "uz": "ℹ️ Taklif qilish uchun mavjud haydovchi yo'q. Barcha haydovchilar o'z sessiyasiga ega yoki allaqachon sessiyada.",
        "ru": "ℹ️ Нет доступных водителей для приглашения. Все водители либо имеют собственную сессию, либо уже состоят в сессии.",
    },
    "staff.bottles.choose_driver_to_invite": {
        "en": "👥 Choose a driver to invite to your session:",
        "uz": "👥 Sessiyangizga taklif qilish uchun haydovchini tanlang:",
        "ru": "👥 Выберите водителя для приглашения в вашу сессию:",
    },
    "staff.bottles.invite_codriver_confirm": {
        "en": "Invite this driver to join your session as a co-driver?",
        "uz": "Bu haydovchini sessiyangizga hamkor sifatida taklif qilasizmi?",
        "ru": "Пригласить этого водителя присоединиться к вашей сессии как напарника?",
    },
    "staff.bottles.invite_codriver_confirm_note": {
        "en": "ℹ️ They will be able to deliver orders and collect bottles under your session.",
        "uz": "ℹ️ Ular sizning sessiyangiz doirasida buyurtmalarni yetkazib berish va shishalarni yig'ish imkoniyatiga ega bo'ladi.",
        "ru": "ℹ️ Они смогут доставлять заказы и собирать бутылки в рамках вашей сессии.",
    },
    "staff.bottles.confirm_invite": {
        "en": "✅ Confirm Invite",
        "uz": "✅ Taklif qilishni tasdiqlash",
        "ru": "✅ Подтвердить приглашение",
    },
    "staff.bottles.codriver_invited": {
        "en": "✅ <b>{name}</b> has been added to your session as a co-driver.",
        "uz": "✅ <b>{name}</b> sessiyangizga hamkor sifatida qo'shildi.",
        "ru": "✅ <b>{name}</b> добавлен в вашу сессию как напарник.",
    },
    "staff.bottles.no_open_session_to_invite": {
        "en": "❌ You must have an open session to invite a co-driver.",
        "uz": "❌ Hamkorni taklif qilish uchun ochiq sessiyangiz bo'lishi kerak.",
        "ru": "❌ Для приглашения напарника необходимо иметь открытую сессию.",
    },
    "staff.delivery.cod_prepaid_reserved": {
        "en": "COD prepaid reserved",
        "uz": "Naqd to'lov uchun band qilingan oldindan to'lov",
        "ru": "Зарезервированная предоплата за наложенный платёж",
    },
    "staff.delivery.cash_to_collect_now": {
        "en": "Cash to collect now",
        "uz": "Hozir yig'iladigan naqd pul",
        "ru": "Сумма к получению сейчас",
    },
    "staff.delivery.cod_prepaid_deduction": {
        "en": "COD prepaid deduction",
        "uz": "Naqd to'lovdan oldindan to'lov ushlanmasi",
        "ru": "Вычет предоплаты из наложенного платежа",
    },
    "staff.delivery.no_cash_due_after_cod": {
        "en": "No cash due after COD prepaid deduction",
        "uz": "Oldindan to'lov ushlangandan keyin naqd pul talab qilinmaydi",
        "ru": "Наличные не требуются после вычета предоплаты",
    },
    "staff.delivery.transfer_confirm_button": {
        "en": "Confirm {qty} from {sender}",
        "uz": "{sender} dan {qty} ni tasdiqlash",
        "ru": "Подтвердить {qty} от {sender}",
    },
    "staff.delivery.transfer_custom_count_button": {
        "en": "Different count",
        "uz": "Boshqa miqdor",
        "ru": "Другое количество",
    },
    "staff.tryout.pickup_all_label": {
        "en": "All ({label})",
        "uz": "Hammasi ({label})",
        "ru": "Все ({label})",
    },
    "staff.delivery.active_cod_debts": {
        "en": "Active COD (Cash on Delivery) debts",
        "uz": "Faol naqd to'lov qarzlari",
        "ru": "Активные долги по наложенным платежам",
    },
    # `{balance}` is the PLACE's pool (`customer_bottle_balance`, clamped at 0),
    # so at a shared workplace it includes a coworker's empties and the driver may
    # legitimately be handed more than THIS customer ever received. "Customer
    # currently holds" made that read like an accusation; anchor it on the door.
    "staff.delivery.bottles_return_prompt": {
        "en": "How many bottles (18.9 L) did the customer return? At this address: {balance}",
        "uz": "Mijoz nechta idish (18,9 l) qaytardi? Ushbu manzilda: {balance} ta",
        "ru": "Сколько бутылок (18,9 л) вернул клиент? По этому адресу: {balance}",
    },
    # TRUE ZERO only — the over-returned arm has its own key
    # (staff.delivery.bottles_return_prompt_over_returned), because there the
    # record exists and is negative.
    "staff.delivery.bottles_return_prompt_no_balance": {
        "en": "How many bottles (18.9 L) did the customer return? No empties are on record at this address yet.",
        "uz": "Mijoz nechta idish (18,9 l) qaytardi? Ushbu manzilda hozircha qayd etilgan bo'sh idish yo'q.",
        "ru": "Сколько бутылок (18,9 л) вернул клиент? По этому адресу пока нет учтённой тары.",
    },
    "staff.delivery.cash_already_collected": {
        "en": "Cash already collected in full",
        "uz": "Naqd pul to'liq yig'ib olingan",
        "ru": "Наличные уже получены полностью",
    },
    "staff.delivery.cash_partially_collected": {
        "en": "Cash partially collected",
        "uz": "Naqd pul qisman yig'ib olingan",
        "ru": "Наличные получены частично",
    },
    "staff.delivery.cod_collection_amount_exceeds_outstanding": {
        "en": "Amount exceeds outstanding ({amount}). Please enter a smaller value.",
        "uz": "Summa qoldiqdan ({amount}) ortiq. Iltimos, kichikroq qiymat kiriting.",
        "ru": "Сумма превышает остаток ({amount}). Введите меньшее значение.",
    },
    "staff.delivery.cod_collection_amount_prompt": {
        "en": "Enter the amount you collected (UZS):",
        "uz": "Yig'ib olgan summani kiriting (UZS):",
        "ru": "Введите полученную сумму (UZS):",
    },
    "staff.delivery.cod_collection_note_prompt": {
        "en": "Add a note for this collection of {amount} (or send /skip to record without a note):",
        "uz": "Ushbu {amount} yig'imi uchun izoh qo'shing (izohsiz qayd qilish uchun /skip yuboring):",
        "ru": "Добавьте примечание для сбора {amount} (или отправьте /skip, чтобы записать без примечания):",
    },
    "staff.delivery.cod_collection_overpayment_confirm": {
        "en": "You entered {amount}, but the customer's outstanding COD debt is only {outstanding}. The surplus {overpayment} will be recorded as customer prepayment and auto-applied to future COD orders. Confirm?",
        "uz": "Siz {amount} kiritdingiz, ammo mijozning naqd to'lov qarzi atigi {outstanding}. Ortiqcha {overpayment} mijozning oldindan to'lovi sifatida qayd qilinadi va keyingi naqd to'lov buyurtmalariga avtomatik qo'llaniladi. Tasdiqlaysizmi?",
        "ru": "Вы ввели {amount}, но задолженность клиента по наложенному платежу — всего {outstanding}. Излишек {overpayment} будет записан как предоплата клиента и автоматически применён к будущим заказам с наложенным платежом. Подтверждаете?",
    },
    "staff.delivery.cod_collection_recorded": {
        "en": "Collection recorded successfully.",
        "uz": "Yig'im muvaffaqiyatli qayd qilindi.",
        "ru": "Сбор успешно записан.",
    },
    "staff.delivery.cod_debtors_hint": {
        "en": "Select a customer to view their statement:",
        "uz": "Hisobotni ko'rish uchun mijozni tanlang:",
        "ru": "Выберите клиента, чтобы открыть отчёт:",
    },
    "staff.delivery.cod_debtors_title": {
        "en": "Customers with outstanding COD (Cash on Delivery) debt",
        "uz": "To'lanmagan naqd to'lov qarzi bor mijozlar",
        "ru": "Клиенты с непогашенным долгом по наложенному платежу",
    },
    "staff.delivery.cod_statement_title": {
        "en": "COD (Cash on Delivery) statement",
        "uz": "Naqd to'lovlar hisoboti",
        "ru": "Отчёт по наложенным платежам",
    },
    "staff.delivery.collect_custom_cod": {
        "en": "Collect custom amount",
        "uz": "Boshqa summa yig'ish",
        "ru": "Получить другую сумму",
    },
    "staff.delivery.collect_full_cod": {
        "en": "Collect full COD (Cash on Delivery)",
        "uz": "To'liq naqd to'lov qarzini yig'ish",
        "ru": "Получить долг полностью",
    },
    "staff.delivery.collection_notes_required": {
        "en": "A note is required for this collection. Please send the note text.",
        "uz": "Ushbu yig'im uchun izoh talab qilinadi. Iltimos, izoh matnini yuboring.",
        "ru": "Для этого сбора требуется примечание. Отправьте текст примечания.",
    },
    "staff.delivery.invalid_cash_amount": {
        "en": "Invalid cash amount. Please enter a positive number.",
        "uz": "Noto'g'ri naqd summa. Iltimos, musbat son kiriting.",
        "ru": "Некорректная сумма. Введите положительное число.",
    },
    "staff.delivery.no_cod_debt": {
        "en": "No outstanding COD (Cash on Delivery) debt.",
        "uz": "To'lanmagan naqd to'lov qarzi yo'q.",
        "ru": "Нет непогашенного долга по наложенным платежам.",
    },
    "staff.delivery.no_cod_debtors": {
        "en": "No customers with outstanding COD (Cash on Delivery) debt right now.",
        "uz": "Hozircha to'lanmagan naqd to'lov qarzi bor mijozlar yo'q.",
        "ru": "Сейчас нет клиентов с непогашенным долгом по наложенному платежу.",
    },
    "staff.delivery.risk_flags": {
        "en": "Risk flags",
        "uz": "Xavf belgilari",
        "ru": "Признаки риска",
    },
    "staff.delivery.total_outstanding": {
        "en": "Total outstanding",
        "uz": "Jami qoldiq",
        "ru": "Общий остаток",
    },
    "staff.order.unknown": {
        "en": "Unknown order",
        "uz": "Noma'lum buyurtma",
        "ru": "Неизвестный заказ",
    },
    "staff.account_deactivated": {
        "en": "Your delivery account has been deactivated. Please contact your administrator.",
        "uz": "Yetkazib berish hisobingiz faolsizlantirilgan. Iltimos, administrator bilan bog'laning.",
        "ru": "Ваша учётная запись курьера деактивирована. Пожалуйста, свяжитесь с администратором.",
    },
}


ROLE_TRANSLATIONS = {
    "delivery_driver": {"en": "Delivery Driver", "uz": "Kuryer", "ru": "Курьер"},
    "operator": {"en": "Operator", "uz": "Operator", "ru": "Оператор"},
    "sales_agent": {"en": "Sales Agent", "uz": "Savdo agenti", "ru": "Торговый агент"},
}

# MUST cover every `shared.enums.DeliveryStatus` value, not just the ones a
# driver can move a delivery INTO. `keyboards/delivery.py:95` labels a button
# for every successor in `DELIVERY_STATUS_TRANSITIONS` — and CANCELLED is a
# successor of all four active statuses — while `formatters.py:358` falls
# through to `staff.delivery.status.{status}` for anything outside its own map.
# Both are fed straight from the enum, so a narrower catalog here prints the
# humanised English key tail on a driver's screen. `_add_dynamic_keys` derives
# from the enum for exactly this reason; keep the two in step.
DELIVERY_STATUS_TRANSLATIONS = {
    "scheduled": {"en": "Scheduled", "uz": "Rejalashtirilgan", "ru": "Запланирован"},
    "pending": {"en": "Pending", "uz": "Kutilmoqda", "ru": "В ожидании"},
    "assigned": {"en": "Assigned", "uz": "Biriktirilgan", "ru": "Назначен"},
    "picked_up": {"en": "Picked Up", "uz": "Olib ketildi", "ru": "Забран"},
    "in_transit": {"en": "In Transit", "uz": "Yolda", "ru": "В пути"},
    "arrived": {"en": "Arrived", "uz": "Yetib keldi", "ru": "Прибыл"},
    "delivered": {"en": "Delivered", "uz": "Yetkazildi", "ru": "Доставлен"},
    "failed": {"en": "Failed", "uz": "Muvaffaqiyatsiz", "ru": "Неудачно"},
    "cancelled": {"en": "Cancelled", "uz": "Bekor qilingan", "ru": "Отменен"},
    "returned": {"en": "Returned", "uz": "Qaytarilgan", "ru": "Возвращен"},
}

# Driver cash-reconciliation session status — `shared.enums.DriverCashSessionStatus`.
# `status_update.py::_format_session_summary` used to print the raw enum value
# ("open", "force_closed") onto the money screen in every language.
CASH_SESSION_STATUS_TRANSLATIONS = {
    "open": {"en": "Open", "uz": "Ochiq", "ru": "Открыта"},
    "partial": {"en": "Partially submitted", "uz": "Qisman topshirilgan", "ru": "Частично сдано"},
    "submitted": {"en": "Submitted", "uz": "Topshirilgan", "ru": "Сдано"},
    "verified": {"en": "Verified", "uz": "Tasdiqlangan", "ru": "Проверено"},
    "mismatch": {"en": "Mismatch", "uz": "Nomuvofiqlik", "ru": "Расхождение"},
    "overdue": {"en": "Overdue", "uz": "Muddati o'tgan", "ru": "Просрочено"},
    "resolved": {"en": "Resolved", "uz": "Hal qilingan", "ru": "Урегулировано"},
    "force_closed": {"en": "Force closed", "uz": "Majburan yopilgan", "ru": "Закрыта принудительно"},
}

# Bottle-accountability session status — `shared.enums.DriverBottleSessionStatus`.
# `bottle_collection.py::_format_session` used to render `[{status.upper()}]`.
BOTTLE_SESSION_STATUS_TRANSLATIONS = {
    "open": {"en": "Open", "uz": "Ochiq", "ru": "Открыта"},
    "closed": {"en": "Closed", "uz": "Yopiq", "ru": "Закрыта"},
    "force_closed": {"en": "Force closed", "uz": "Majburan yopilgan", "ru": "Закрыта принудительно"},
    "cancelled": {"en": "Cancelled", "uz": "Bekor qilingan", "ru": "Отменена"},
}

# Reconciliation risk flags — emitted as snake_case identifiers by
# `DriverReconciliationService._build_risk_flags` and previously joined straight
# into the driver's cash screen ("Признаки риска: cash_on_hand_warning").
RISK_FLAG_TRANSLATIONS = {
    "cash_on_hand_escalation": {
        "en": "Cash on hand above escalation limit",
        "uz": "Qo'ldagi naqd pul eskalatsiya chegarasidan yuqori",
        "ru": "Наличные на руках выше порога эскалации",
    },
    "cash_on_hand_warning": {
        "en": "Cash on hand above warning limit",
        "uz": "Qo'ldagi naqd pul ogohlantirish chegarasidan yuqori",
        "ru": "Наличные на руках выше порога предупреждения",
    },
    "repeated_mismatch_pattern": {
        "en": "Repeated mismatches recently",
        "uz": "So'nggi paytda takroriy nomuvofiqliklar",
        "ru": "Повторяющиеся расхождения за последнее время",
    },
    "submission_overdue": {
        "en": "Cash submission overdue",
        "uz": "Naqd pulni topshirish muddati o'tgan",
        "ru": "Сдача наличных просрочена",
    },
    "reconciliation_warning_due": {
        "en": "Reconciliation due soon",
        "uz": "Hisob-kitob muddati yaqinlashdi",
        "ru": "Скоро срок сверки",
    },
}

FAILED_REASON_TRANSLATIONS = {
    "customer_unavailable": {
        "en": "Customer unavailable",
        "uz": "Mijoz javob bermadi",
        "ru": "Клиент недоступен",
    },
    "customer_refused": {
        "en": "Customer refused",
        "uz": "Mijoz rad etdi",
        "ru": "Клиент отказался",
    },
    "wrong_address": {
        "en": "Wrong address",
        "uz": "Noto'gri manzil",
        "ru": "Неправильный адрес",
    },
    "product_damaged": {
        "en": "Product damaged",
        "uz": "Mahsulot shikastlangan",
        "ru": "Товар поврежден",
    },
    "other": {
        "en": "Other",
        "uz": "Boshqa",
        "ru": "Другое",
    },
}

PAYMENT_TRANSLATIONS = {
    "cash": {"en": "Cash", "uz": "Naqd", "ru": "Наличные"},
    "card": {"en": "Card", "uz": "Karta", "ru": "Карта"},
    "payme": {"en": "Payme", "uz": "Payme", "ru": "Payme"},
    "click": {"en": "Click", "uz": "Click", "ru": "Click"},
    "loyalty_points": {
        "en": "AquaCoins",
        "uz": "AquaCoins",
        "ru": "AquaCoins",
    },
    "business_account": {
        "en": "Business Account",
        "uz": "Biznes hisobi",
        "ru": "Бизнес-счет",
    },
}

ORDER_STATUS_TRANSLATIONS = {
    "pending": {"en": "Pending", "uz": "Kutilmoqda", "ru": "В ожидании"},
    "confirmed": {"en": "Confirmed", "uz": "Tasdiqlangan", "ru": "Подтвержден"},
    "preparing": {"en": "Preparing", "uz": "Tayyorlanmoqda", "ru": "Готовится"},
    "out_for_delivery": {"en": "Out for Delivery", "uz": "Yetkazishda", "ru": "На доставке"},
    "delivered": {"en": "Delivered", "uz": "Yetkazildi", "ru": "Доставлен"},
    "cancelled": {"en": "Cancelled", "uz": "Bekor qilingan", "ru": "Отменен"},
    "returned": {"en": "Returned", "uz": "Qaytarilgan", "ru": "Возвращен"},
}

EXTRA_TRANSLATIONS = {
    "staff.addresses": {"en": "Addresses", "uz": "Manzillar", "ru": "Адреса"},
    "staff.orders": {"en": "Orders", "uz": "Buyurtmalar", "ru": "Заказы"},
    "staff.items": {"en": "items", "uz": "mahsulot", "ru": "позиций"},
    "staff.auth_cancelled": {"en": "Authentication cancelled.", "uz": "Autentifikatsiya bekor qilindi.", "ru": "Авторизация отменена."},
    "staff.bottle_flow_cancelled": {
        "en": "Bottle operation cancelled. You are still signed in.",
        "uz": "Balon amali bekor qilindi. Siz tizimdasiz.",
        "ru": "Операция с баллонами отменена. Вы остаетесь в системе.",
    },
    "staff.flow_timed_out": {
        "en": "This step timed out, so I closed it. Nothing was saved — start again when you are ready.",
        "uz": "Bu bosqich vaqti tugadi va yopildi. Hech narsa saqlanmadi — tayyor bo'lganingizda qaytadan boshlang.",
        "ru": "Время шага истекло, он закрыт. Ничего не сохранено — начните заново, когда будете готовы.",
    },
    "staff.login_failed": {
        "en": "Login failed: {error}",
        "uz": "Kirish muvaffaqiyatsiz: {error}",
        "ru": "Вход не выполнен: {error}",
    },
    "staff.login_success": {
        "en": "Welcome, {name}! Role: {role}",
        "uz": "Xush kelibsiz, {name}! Rol: {role}",
        "ru": "Добро пожаловать, {name}! Роль: {role}",
    },
    "staff.not_staff": {
        "en": "Your account does not have staff bot access.",
        "uz": "Hisobingizda staff botga kirish huquqi yoq.",
        "ru": "У вашей учетной записи нет доступа к боту сотрудников.",
    },
    "staff.welcome_back": {
        "en": "Welcome back, {name}!",
        "uz": "Qaytganingiz bilan, xush kelibsiz, {name}!",
        "ru": "С возвращением, {name}!",
    },
    "staff.help.text": {
        "en": "Use the menu below to manage deliveries and operator tasks.",
        "uz": "Yetkazish va operator amallarini boshqarish uchun menyudan foydalaning.",
        "ru": "Используйте меню ниже для управления доставками и задачами оператора.",
    },
    "staff.help.delivery": {
        "en": "Delivery: take orders, update statuses, and share location when needed.",
        "uz": "Kuryer: buyurtmalarni qabul qiling, holatni yangilang va kerak bolganda lokatsiyani ulashing.",
        "ru": "Курьер: принимайте заказы, обновляйте статусы и при необходимости отправляйте геолокацию.",
    },
    "staff.help.operator": {
        "en": "Operator: create clients, manage addresses, and place phone orders.",
        "uz": "Operator: mijoz yarating, manzillarni boshqaring va telefon buyurtmalarini yarating.",
        "ru": "Оператор: создавайте клиентов, управляйте адресами и оформляйте заказы по телефону.",
    },
    "staff.help.sales_agent": {
        "en": "🏪 <b>Sales agent</b>\n• My outlets — your stores, prospects and their cards\n• New outlet — register a store you found in the field\n• Request activation once the contact phone and pin are in place; an operator or admin approves it",
        "uz": "🏪 <b>Savdo agenti</b>\n• Mening savdo nuqtalarim — do'konlaringiz, nomzodlar va ularning kartalari\n• Yangi savdo nuqtasi — dalada topgan do'koningizni ro'yxatga oling\n• Telefon va joylashuv kiritilgach faollashtirishni so'rang; operator yoki admin tasdiqlaydi",
        "ru": "🏪 <b>Торговый агент</b>\n• Мои точки — ваши магазины, кандидаты и их карточки\n• Новая точка — зарегистрируйте магазин, найденный на маршруте\n• Запросите активацию, когда указаны телефон и геометка; оператор или админ подтвердит",
    },
    "staff.profile.title": {"en": "Profile", "uz": "Profil", "ru": "Профиль"},
    "staff.profile.name": {"en": "Name", "uz": "Ism", "ru": "Имя"},
    "staff.profile.phone": {"en": "Phone", "uz": "Telefon", "ru": "Телефон"},
    "staff.profile.roles": {"en": "Roles", "uz": "Rollar", "ru": "Роли"},
    "staff.profile.language": {"en": "Language", "uz": "Til", "ru": "Язык"},
    "staff.stats.title": {"en": "My Stats", "uz": "Mening statistikam", "ru": "Моя статистика"},
    "staff.stats.total": {"en": "Total deliveries", "uz": "Jami yetkazishlar", "ru": "Всего доставок"},
    "staff.stats.completed": {"en": "Completed", "uz": "Bajarilgan", "ru": "Завершено"},
    "staff.stats.failed": {"en": "Failed", "uz": "Muvaffaqiyatsiz", "ru": "Неудачно"},
    "staff.stats.avg_time": {"en": "Average time", "uz": "Ortacha vaqt", "ru": "Среднее время"},
    "staff.stats.rating": {"en": "Rating", "uz": "Reyting", "ru": "Рейтинг"},
    "staff.stats.cash": {"en": "Cash collected", "uz": "Yigilgan naqd", "ru": "Собранные наличные"},
    "staff.stats.period.day": {"en": "Day", "uz": "Kun", "ru": "День"},
    "staff.stats.period.week": {"en": "Week", "uz": "Hafta", "ru": "Неделя"},
    "staff.stats.period.month": {"en": "Month", "uz": "Oy", "ru": "Месяц"},
    # ---------------------------------------------------------------- #
    # Backend-emitted staff Telegram notifications (B-1).               #
    # business_app composes these in the driver's preferred_language    #
    # via business_app.utils.translations.get_translation, then sends   #
    # them through NotificationService.send_staff_telegram_message.     #
    # ---------------------------------------------------------------- #
    "staff.notification.reconciliation_reminder_due": {
        "en": "🔔 Reminder: cash reconciliation for {date} is pending. Expected on-hand cash: {expected_cash} UZS.",
        "uz": "🔔 Eslatma: {date} sanasidagi naqd hisobotini topshirish kutilmoqda. Kutilayotgan naqd: {expected_cash} soʻm.",
        "ru": "🔔 Напоминание: сверка наличных за {date} ещё не сдана. Ожидаемый остаток: {expected_cash} сум.",
    },
    "staff.notification.reconciliation_reminder_overdue": {
        "en": "⚠️ Cash session warning: session started {date} is 7+ days old. Expected on-hand cash: {expected_cash} UZS.",
        "uz": "⚠️ Naqd sessiya ogohlantirishi: {date} boshlangan sessiya 7 kundan oshdi. Kutilayotgan naqd: {expected_cash} soʻm.",
        "ru": "⚠️ Предупреждение по наличным: сессии от {date} уже 7+ дней. Ожидаемый остаток: {expected_cash} сум.",
    },
    "staff.notification.manager_exception_summary": {
        "en": "There are {count} driver cash sessions with mismatch or 7+ day warning status requiring review.",
        "uz": "{count} ta haydovchi naqd sessiyasida farq yoki 7+ kunlik ogohlantirish bor, ko'rib chiqish kerak.",
        "ru": "Есть {count} сессий наличных курьеров с расхождением или предупреждением 7+ дней.",
    },
    "staff.notification.subject.driver_cash_reconciliation": {
        "en": "Driver cash reconciliation",
        "uz": "Haydovchi naqd hisoboti",
        "ru": "Сверка наличных курьера",
    },
    "staff.notification.subject.driver_cash_exceptions": {
        "en": "Driver cash exceptions",
        "uz": "Haydovchi naqd istisnolari",
        "ru": "Расхождения наличных курьеров",
    },
    "staff.notification.bottle_session_reopened": {
        "en": "🔓 Your bottle session #{session_id} was reopened by admin because order #{order_id} was edited after delivery. Please re-close the session when you're ready so admin can verify it.",
        "uz": "🔓 Sizning #{session_id} idishlar sessiyangiz admin tomonidan qayta ochildi (buyurtma #{order_id} yetkazib berilgandan keyin tahrirlandi). Iltimos, tayyor bo'lsangiz sessiyani qayta yoping.",
        "ru": "🔓 Ваша сессия по таре #{session_id} была переоткрыта администратором (заказ #{order_id} изменён после доставки). Пожалуйста, закройте сессию заново для проверки.",
    },
}

DELIVERY_TEXT_TRANSLATIONS = {
    "accept": {"en": "Accept", "uz": "Qabul qilish", "ru": "Принять"},
    "accepted_success": {"en": "Order accepted successfully.", "uz": "Buyurtma muvaffaqiyatli qabul qilindi.", "ru": "Заказ успешно принят."},
    "active_count": {"en": "{count} active deliveries", "uz": "{count} ta faol yetkazish", "ru": "{count} активных доставок"},
    "active_title": {"en": "Active Deliveries", "uz": "Faol yetkazishlar", "ru": "Активные доставки"},
    "already_taken": {"en": "This order is already taken.", "uz": "Bu buyurtma allaqachon olingan.", "ru": "Этот заказ уже принят."},
    "cash_collection": {"en": "Confirm collected cash: {amount}", "uz": "Qabul qilingan naqdni tasdiqlang: {amount}", "ru": "Подтвердите собранные наличные: {amount}"},
    "cash_recorded": {"en": "Cash recorded: {amount}", "uz": "Naqd qayd etildi: {amount}", "ru": "Наличные зафиксированы: {amount}"},
    "confirm_accept": {"en": "Do you want to accept this order?", "uz": "Bu buyurtmani qabul qilasizmi?", "ru": "Принять этот заказ?"},
    "confirm_cash": {"en": "Confirm cash {amount}", "uz": "{amount} naqdni tasdiqlash", "ru": "Подтвердить наличные {amount}"},
    "confirm_status": {"en": "Confirm status update to: {status}?", "uz": "Holatni quyidagiga ozgartirishni tasdiqlaysizmi: {status}?", "ru": "Подтвердить изменение статуса на: {status}?"},
    "current_status": {"en": "Current status", "uz": "Joriy holat", "ru": "Текущий статус"},
    "delivered_success": {"en": "Delivery marked as delivered.", "uz": "Yetkazish bajarildi deb belgilandi.", "ru": "Доставка отмечена как выполненная."},
    "edit_cash": {"en": "Edit cash amount", "uz": "Naqd summasini ozgartirish", "ru": "Изменить сумму наличных"},
    "enter_cash_amount": {"en": "Enter collected cash amount:", "uz": "Qabul qilingan naqd summasini kiriting:", "ru": "Введите сумму собранных наличных:"},
    "fail_reason_label": {"en": "Reason", "uz": "Sabab", "ru": "Причина"},
    "history_title": {"en": "Delivery History", "uz": "Yetkazish tarixi", "ru": "История доставок"},
    "invalid_amount": {"en": "Invalid amount. Please enter a valid number.", "uz": "Noto'gri summa. Iltimos, togri son kiriting.", "ru": "Некорректная сумма. Введите правильное число."},
    "items": {"en": "Items", "uz": "Mahsulotlar", "ru": "Позиции"},
    "manage": {"en": "Manage", "uz": "Boshqarish", "ru": "Управлять"},
    "mark_preparing": {"en": "Mark as Preparing", "uz": "Tayyorlanmoqda deb belgilash", "ru": "Отметить как готовится"},
    "marked_failed": {"en": "Delivery marked as failed.", "uz": "Yetkazish muvaffaqiyatsiz deb belgilandi.", "ru": "Доставка отмечена как неудачная."},
    "marked_preparing": {"en": "Order marked as preparing.", "uz": "Buyurtma tayyorlanmoqda deb belgilandi.", "ru": "Заказ отмечен как готовится."},
    "navigate": {"en": "Navigate", "uz": "Yonaltirish", "ru": "Навигация"},
    "navigate_text": {"en": "Open route in map", "uz": "Marshrutni xaritada ochish", "ru": "Открыть маршрут на карте"},
    "no_active": {"en": "No active deliveries.", "uz": "Faol yetkazishlar yoq.", "ru": "Нет активных доставок."},
    "no_address": {"en": "Address coordinates are not available.", "uz": "Manzil koordinatalari mavjud emas.", "ru": "Координаты адреса недоступны."},
    "no_history": {"en": "No delivery history yet.", "uz": "Hozircha yetkazish tarixi yoq.", "ru": "История доставок пока пуста."},
    "not_found": {"en": "Delivery not found.", "uz": "Yetkazish topilmadi.", "ru": "Доставка не найдена."},
    "open_maps": {"en": "Open Maps", "uz": "Xaritani ochish", "ru": "Открыть карты"},
    "order_not_found": {"en": "Order not found.", "uz": "Buyurtma topilmadi.", "ru": "Заказ не найден."},
    "pool_count": {"en": "{count} orders available", "uz": "{count} ta buyurtma mavjud", "ru": "Доступно заказов: {count}"},
    "pool_empty": {"en": "No available orders in the pool.", "uz": "Havzada mavjud buyurtmalar yoq.", "ru": "В списке нет доступных заказов."},
    "pool_title": {"en": "Order Pool", "uz": "Buyurtmalar havzasi", "ru": "Список заказов"},
    "select_fail_reason": {"en": "Select failure reason:", "uz": "Muvaffaqiyatsizlik sababini tanlang:", "ru": "Выберите причину неудачи:"},
    "share_location_prompt": {"en": "Please share your current location.", "uz": "Iltimos, joriy lokatsiyangizni yuboring.", "ru": "Пожалуйста, отправьте вашу текущую геолокацию."},
    "status_updated": {"en": "Status updated to: {status}", "uz": "Holat yangilandi: {status}", "ru": "Статус обновлен: {status}"},
    "view_details": {"en": "View details", "uz": "Batafsil korish", "ru": "Посмотреть детали"},
}

OPERATOR_TEXT_TRANSLATIONS = {
    "add_address": {"en": "Add Address", "uz": "Manzil qoshish", "ru": "Добавить адрес"},
    "add_more_or_done": {"en": "Add more products or finish selection.", "uz": "Yana mahsulot qoshing yoki yakunlang.", "ru": "Добавьте еще товары или завершите выбор."},
    "address_not_found": {"en": "That address could not be found on the map. Check the spelling or attach a pin with the button below.", "uz": "Bu manzil xaritada topilmadi. Imloni tekshiring yoki quyidagi tugma orqali joylashuvni yuboring.", "ru": "Этот адрес не найден на карте. Проверьте написание или отправьте геометку кнопкой ниже."},
    "address_saved": {"en": "Address saved successfully.", "uz": "Manzil muvaffaqiyatli saqlandi.", "ru": "Адрес успешно сохранен."},
    "addresses_title": {"en": "Client Addresses", "uz": "Mijoz manzillari", "ru": "Адреса клиента"},
    "cart": {"en": "Cart", "uz": "Savat", "ru": "Корзина"},
    "cart_empty": {"en": "Cart is empty.", "uz": "Savat bosh.", "ru": "Корзина пуста."},
    "confirm_address": {"en": "Confirm Address", "uz": "Manzilni tasdiqlash", "ru": "Подтвердить адрес"},
    "confirm_create_user": {"en": "Confirm Client Creation", "uz": "Mijoz yaratishni tasdiqlang", "ru": "Подтвердите создание клиента"},
    "confirm_order": {"en": "Confirm Order", "uz": "Buyurtmani tasdiqlash", "ru": "Подтвердить заказ"},
    "confirm_order_prompt": {"en": "Confirm order creation?", "uz": "Buyurtma yaratishni tasdiqlaysizmi?", "ru": "Подтвердить создание заказа?"},
    "create_order_for": {"en": "Create Order for Client", "uz": "Mijoz uchun buyurtma yaratish", "ru": "Создать заказ для клиента"},
    "create_user": {"en": "Create Client", "uz": "Mijoz yaratish", "ru": "Создать клиента"},
    "done_selecting": {"en": "Done selecting", "uz": "Tanlash yakunlandi", "ru": "Выбор завершен"},
    "enter_address_label": {"en": "Enter address label (e.g., Home, Office):", "uz": "Manzil nomini kiriting (masalan, Uy, Ofis):", "ru": "Введите название адреса (например, Дом, Офис):"},
    "enter_delivery_notes": {"en": "Enter delivery notes or type '-' to skip:", "uz": "Yetkazish izohini kiriting yoki '-' deb yozib otkazing:", "ru": "Введите примечание к доставке или '-' для пропуска:"},
    "enter_district": {"en": "Enter district or type '-' to skip:", "uz": "Tuman nomini kiriting yoki '-' deb yozib otkazing:", "ru": "Введите район или '-' для пропуска:"},
    "enter_first_name": {"en": "Enter first name:", "uz": "Ismni kiriting:", "ru": "Введите имя:"},
    "enter_full_address": {"en": "Enter full address, or attach a pin with the button below:", "uz": "Tolik manzilni kiriting yoki quyidagi tugma orqali joylashuvni yuboring:", "ru": "Введите полный адрес или отправьте геометку кнопкой ниже:"},
    "enter_last_name": {"en": "Enter last name or '-' to skip:", "uz": "Familiyani kiriting yoki '-' deb yozib otkazing:", "ru": "Введите фамилию или '-' для пропуска:"},
    "enter_notes": {"en": "Enter order notes or skip:", "uz": "Buyurtma izohini kiriting yoki otkazing:", "ru": "Введите примечание к заказу или пропустите:"},
    "enter_phone": {"en": "Enter phone number:", "uz": "Telefon raqamini kiriting:", "ru": "Введите телефон:"},
    "invalid_address": {"en": "Address is too short.", "uz": "Manzil juda qisqa.", "ru": "Адрес слишком короткий."},
    "invalid_label": {"en": "Invalid label. Try again.", "uz": "Noto'gri nom. Qayta urinib koring.", "ru": "Некорректная метка. Повторите."},
    "invalid_name": {"en": "Invalid name format.", "uz": "Ism formati noto'gri.", "ru": "Некорректный формат имени."},
    "invalid_phone": {"en": "Invalid phone number format.", "uz": "Telefon raqami formati noto'gri.", "ru": "Некорректный формат телефона."},
    "location_needs_address": {"en": "Pin saved, but the address could not be read from it. Type the full address now.", "uz": "Joylashuv saqlandi, lekin manzil aniqlanmadi. Endi tolik manzilni kiriting.", "ru": "Геометка сохранена, но адрес по ней не определен. Введите полный адрес."},
    "location_received": {"en": "Pin received: {address}", "uz": "Joylashuv qabul qilindi: {address}", "ru": "Геометка получена: {address}"},
    "manage_addresses": {"en": "Manage Addresses", "uz": "Manzillarni boshqarish", "ru": "Управлять адресами"},
    "no_addresses": {"en": "No addresses found for this client.", "uz": "Bu mijoz uchun manzillar topilmadi.", "ru": "У этого клиента нет адресов."},
    "no_items_selected": {"en": "No items selected yet.", "uz": "Hali mahsulot tanlanmagan.", "ru": "Товары еще не выбраны."},
    "no_products": {"en": "No products available.", "uz": "Mahsulotlar mavjud emas.", "ru": "Товары недоступны."},
    "no_recent_orders": {"en": "No recent orders yet.", "uz": "Hozircha songgi buyurtmalar yoq.", "ru": "Пока нет последних заказов."},
    "no_results": {"en": "No clients found for '{query}'.", "uz": "'{query}' boyicha mijoz topilmadi.", "ru": "По запросу '{query}' клиенты не найдены."},
    "order_created": {"en": "Order #{order_number} created successfully.", "uz": "#{order_number} buyurtma muvaffaqiyatli yaratildi.", "ru": "Заказ #{order_number} успешно создан."},
    "order_enter_phone": {"en": "Enter client phone number to create order:", "uz": "Buyurtma yaratish uchun mijoz telefonini kiriting:", "ru": "Введите телефон клиента для создания заказа:"},
    "outside_delivery_area": {"en": "That address is outside our delivery area. Enter an address inside Tashkent or attach a pin.", "uz": "Bu manzil yetkazib berish hududimizdan tashqarida. Toshkent ichidagi manzilni kiriting yoki joylashuvni yuboring.", "ru": "Этот адрес вне зоны доставки. Введите адрес в пределах Ташкента или отправьте геометку."},
    "recent_orders_title": {"en": "Recent Operator Orders", "uz": "Operatorning songgi buyurtmalari", "ru": "Последние заказы оператора"},
    "search_again": {"en": "Search again", "uz": "Yana qidirish", "ru": "Искать снова"},
    "search_prompt": {"en": "Enter phone or name to search client:", "uz": "Mijozni qidirish uchun telefon yoki ism kiriting:", "ru": "Введите телефон или имя для поиска клиента:"},
    "search_results": {"en": "Found clients: {count}", "uz": "Topilgan mijozlar: {count}", "ru": "Найдено клиентов: {count}"},
    "search_too_short": {"en": "Search query is too short.", "uz": "Qidiruv sorovi juda qisqa.", "ru": "Слишком короткий поисковый запрос."},
    "select_address": {"en": "Select delivery address:", "uz": "Yetkazish manzilini tanlang:", "ru": "Выберите адрес доставки:"},
    "select_client_language": {"en": "Select client language:", "uz": "Mijoz tilini tanlang:", "ru": "Выберите язык клиента:"},
    "select_payment": {"en": "Select payment method:", "uz": "Tolov usulini tanlang:", "ru": "Выберите способ оплаты:"},
    "select_products": {"en": "Select products:", "uz": "Mahsulotlarni tanlang:", "ru": "Выберите товары:"},
    "select_quantity": {"en": "Select quantity:", "uz": "Miqdorni tanlang:", "ru": "Выберите количество:"},
    "share_location": {"en": "Send Location", "uz": "Joylashuvni yuborish", "ru": "Отправить геолокацию"},
    "skip_notes": {"en": "Skip notes", "uz": "Izohsiz davom etish", "ru": "Пропустить примечание"},
    "subtotal": {"en": "Subtotal", "uz": "Oraliq jami", "ru": "Промежуточный итог"},
    "user_already_exists": {"en": "A client with this phone already exists.", "uz": "Bu telefon bilan mijoz allaqachon mavjud.", "ru": "Клиент с таким телефоном уже существует."},
    "user_created": {"en": "Client created successfully.", "uz": "Mijoz muvaffaqiyatli yaratildi.", "ru": "Клиент успешно создан."},
    "user_exists": {"en": "Client already exists.", "uz": "Mijoz allaqachon mavjud.", "ru": "Клиент уже существует."},
}

# Suffix-keyed, seeded under the `staff.sales.` prefix (see
# `_add_curated_keys` / `_auto_family_translation`). The explicit
# `staff.sales.notify.*` rows live in STAFF_TRANSLATIONS and keep winning,
# because `_curated_value` consults that map before the families.
SALES_TEXT_TRANSLATIONS = {
    "error.duplicate": {"en": "A similar outlet or customer already exists nearby.", "uz": "Yaqin atrofda shunga o'xshash savdo nuqtasi yoki mijoz allaqachon bor.", "ru": "Похожая торговая точка или клиент уже есть поблизости."},
    "error.pin_required": {"en": "A location pin is required.", "uz": "Joylashuv belgisi kerak.", "ru": "Нужна геометка."},
    "error.phone_required": {"en": "A contact phone is required.", "uz": "Aloqa telefoni kerak.", "ru": "Нужен контактный телефон."},
    "error.stage_invalid": {"en": "This action is not possible at the outlet's current stage.", "uz": "Bu amal savdo nuqtasining hozirgi bosqichida mumkin emas.", "ru": "Действие невозможно на текущем этапе точки."},
    "error.approval_failed": {"en": "Activation failed at one step. Try again or ask an admin.", "uz": "Faollashtirish bir bosqichda to'xtadi. Qayta urinib ko'ring yoki adminga murojaat qiling.", "ru": "Активация прервалась на одном из шагов. Повторите или обратитесь к администратору."},
    "error.phone_taken": {"en": "This phone already belongs to another customer.", "uz": "Bu telefon boshqa mijozga tegishli.", "ru": "Этот телефон уже принадлежит другому клиенту."},
    "error.district_invalid": {"en": "Unknown district.", "uz": "Noma'lum tuman.", "ru": "Неизвестный район."},
    "hub.title": {"en": "My outlets", "uz": "Mening savdo nuqtalarim", "ru": "Мои точки"},
    # Names the due list first, because that row is the agent's day (L95: the
    # hint used to describe Prospects only, under a keyboard whose first and
    # primary button is the due list).
    "hub.hint": {"en": "Start with the due list — that is today's work. Prospects are outlets not yet activated. Tap any outlet to open its card.", "uz": "Bugungi va kechikkan ro'yxatidan boshlang — bu bugungi ish. Nomzodlar — hali faollashtirilmagan nuqtalar. Kartani ochish uchun istalgan nuqtani bosing.", "ru": "Начните со списка «На сегодня и просроченные» — это и есть работа на день. Кандидаты — ещё не активированные точки. Нажмите на любую точку, чтобы открыть карточку."},
    "hub.prospects": {"en": "Prospects", "uz": "Nomzodlar", "ru": "Кандидаты"},
    "hub.all": {"en": "All outlets", "uz": "Barcha nuqtalar", "ru": "Все точки"},
    "hub.nearby": {"en": "Nearby", "uz": "Yaqin atrofda", "ru": "Рядом"},
    "list.title_prospects": {"en": "Prospects", "uz": "Nomzodlar", "ru": "Кандидаты"},
    "list.title_all": {"en": "All my outlets", "uz": "Barcha savdo nuqtalarim", "ru": "Все мои точки"},
    "list.empty": {"en": "Nothing here yet.", "uz": "Hozircha hech narsa yo'q.", "ru": "Пока пусто."},
    "list.next": {"en": "Next", "uz": "Keyingi", "ru": "Далее"},
    "list.prev": {"en": "Back", "uz": "Oldingi", "ru": "Назад"},
    "card.stage": {"en": "Stage", "uz": "Bosqich", "ru": "Этап"},
    "card.class": {"en": "Class", "uz": "Toifa", "ru": "Класс"},
    "card.contact": {"en": "Contact", "uz": "Aloqa", "ru": "Контакт"},
    "card.address": {"en": "Address", "uz": "Manzil", "ru": "Адрес"},
    "card.receivable": {"en": "Owes", "uz": "Qarzdorlik", "ru": "Долг"},
    "card.bottles": {"en": "Bottles at outlet", "uz": "Nuqtadagi idishlar", "ru": "Бутылей на точке"},
    "card.notes": {"en": "Notes", "uz": "Izohlar", "ru": "Заметки"},
    "card.last_orders": {"en": "Last orders", "uz": "So'nggi buyurtmalar", "ru": "Последние заказы"},
    "card.request_activation": {"en": "Request activation", "uz": "Faollashtirishni so'rash", "ru": "Запросить активацию"},
    "card.navigate": {"en": "Navigate", "uz": "Yo'l ko'rsatish", "ru": "Маршрут"},
    "card.back_to_hub": {"en": "My outlets", "uz": "Mening nuqtalarim", "ru": "Мои точки"},
    "card.activation_requested": {"en": "Activation requested. An operator or admin will review it.", "uz": "Faollashtirish so'raldi. Operator yoki admin ko'rib chiqadi.", "ru": "Активация запрошена. Оператор или админ рассмотрит заявку."},
    # Stages and types mirror `business_app/models/sales.py` — see the loops in
    # `_add_dynamic_keys`, which is what puts these in front of /health.
    "stage.prospect": {"en": "Prospect", "uz": "Nomzod", "ru": "Кандидат"},
    "stage.trial": {"en": "Trial", "uz": "Sinov", "ru": "Пробный"},
    "stage.activation_requested": {"en": "Awaiting activation", "uz": "Faollashtirish kutilmoqda", "ru": "Ожидает активации"},
    "stage.active": {"en": "Active", "uz": "Faol", "ru": "Активна"},
    "stage.at_risk": {"en": "At risk", "uz": "Xavf ostida", "ru": "Под риском"},
    "stage.dormant": {"en": "Dormant", "uz": "Uxlab yotgan", "ru": "Спящая"},
    "stage.lost": {"en": "Lost", "uz": "Yo'qotilgan", "ru": "Потеряна"},
    "type.grocery_store": {"en": "Grocery store", "uz": "Oziq-ovqat do'koni", "ru": "Продуктовый магазин"},
    "type.workplace": {"en": "Workplace", "uz": "Ish joyi", "ru": "Офис"},
    "type.individual": {"en": "Individual", "uz": "Jismoniy shaxs", "ru": "Частное лицо"},
    # Field onboarding ("New outlet"), step by step.
    "new.choose_type": {"en": "What kind of outlet is this?", "uz": "Bu qanday savdo nuqtasi?", "ru": "Какой это тип точки?"},
    "new.enter_name": {"en": "Enter the outlet name (as on the sign):", "uz": "Savdo nuqtasi nomini kiriting (peshlavhadagidek):", "ru": "Введите название точки (как на вывеске):"},
    "new.enter_contact_name": {"en": "Contact person's name (or skip):", "uz": "Aloqa uchun shaxs ismi (yoki o'tkazib yuboring):", "ru": "Имя контактного лица (или пропустите):"},
    "new.enter_contact_phone": {"en": "Contact phone, e.g. 90 123 45 67 (or skip — needed before activation):", "uz": "Aloqa telefoni, masalan 90 123 45 67 (yoki o'tkazing — faollashtirishdan oldin kerak bo'ladi):", "ru": "Контактный телефон, напр. 90 123 45 67 (или пропустите — понадобится до активации):"},
    "new.share_pin": {"en": "Share the outlet location: tap the button while standing at the door, or type the address.", "uz": "Savdo nuqtasi joylashuvini yuboring: eshik oldida turib tugmani bosing yoki manzilni yozing.", "ru": "Отправьте геолокацию точки: нажмите кнопку у входа или введите адрес."},
    "new.share_pin_hint": {"en": "Use the button below or type the address.", "uz": "Quyidagi tugmadan foydalaning yoki manzilni yozing.", "ru": "Используйте кнопку ниже или введите адрес."},
    "new.share_pin_button": {"en": "📍 Share outlet location", "uz": "📍 Nuqta joylashuvini yuborish", "ru": "📍 Отправить геолокацию точки"},
    "new.pin_received": {"en": "Location saved: {address}", "uz": "Joylashuv saqlandi: {address}", "ru": "Геолокация сохранена: {address}"},
    "new.choose_class": {"en": "Estimated class (A = high volume, C = low) — or skip:", "uz": "Taxminiy toifa (A = yuqori hajm, C = past) — yoki o'tkazing:", "ru": "Предполагаемый класс (A — большой объём, C — малый) — или пропустите:"},
    "new.enter_notes": {"en": "Notes for the next visit (or skip):", "uz": "Keyingi tashrif uchun izohlar (yoki o'tkazing):", "ru": "Заметки для следующего визита (или пропустите):"},
    "new.skip": {"en": "Skip", "uz": "O'tkazib yuborish", "ru": "Пропустить"},
    "new.summary_title": {"en": "New outlet", "uz": "Yangi savdo nuqtasi", "ru": "Новая точка"},
    "new.confirm_hint": {"en": "Save this outlet?", "uz": "Bu nuqtani saqlaymizmi?", "ru": "Сохранить точку?"},
    "new.duplicates_title": {"en": "Similar records found nearby", "uz": "Yaqin atrofda o'xshash yozuvlar topildi", "ru": "Поблизости найдены похожие записи"},
    "new.link_existing": {"en": "Link", "uz": "Bog'lash", "ru": "Привязать"},
    "new.open_existing": {"en": "Open", "uz": "Ochish", "ru": "Открыть"},
    "new.create_anyway": {"en": "Create anyway", "uz": "Baribir yaratish", "ru": "Всё равно создать"},
    "new.created": {"en": "Outlet saved", "uz": "Savdo nuqtasi saqlandi", "ru": "Точка сохранена"},
    # The operator's approval queue. `approvals.reason.*` is also a dynamic
    # family — `SalesKeyboards.approval_actions` builds the key from
    # `REJECT_REASONS`, and the twin loop in
    # `staff_bot/i18n.py::_add_dynamic_family_keys` is what puts it in front of
    # /health. A fifth reason needs a row here as well as an entry there.
    "approvals.button": {"en": "Activation requests", "uz": "Faollashtirish so'rovlari", "ru": "Заявки на активацию"},
    "approvals.title": {"en": "Outlets awaiting activation", "uz": "Faollashtirishni kutayotgan nuqtalar", "ru": "Точки, ожидающие активации"},
    "approvals.empty": {"en": "No requests right now.", "uz": "Hozircha so'rovlar yo'q.", "ru": "Заявок пока нет."},
    "approvals.approve": {"en": "Approve", "uz": "Tasdiqlash", "ru": "Одобрить"},
    "approvals.reject": {"en": "Reject", "uz": "Rad etish", "ru": "Отклонить"},
    "approvals.approved": {"en": "Outlet activated. The agent has been notified.", "uz": "Nuqta faollashtirildi. Agentga xabar berildi.", "ru": "Точка активирована. Агент уведомлён."},
    "approvals.rejected": {"en": "Request rejected. The agent has been notified.", "uz": "So'rov rad etildi. Agentga xabar berildi.", "ru": "Заявка отклонена. Агент уведомлён."},
    "approvals.reason.duplicate": {"en": "Duplicate", "uz": "Takroriy", "ru": "Дубликат"},
    "approvals.reason.incomplete": {"en": "Incomplete", "uz": "To'liq emas", "ru": "Неполные данные"},
    "approvals.reason.not_customer": {"en": "Not a customer", "uz": "Mijoz emas", "ru": "Не клиент"},
    "approvals.reason.other": {"en": "Other", "uz": "Boshqa", "ru": "Другое"},
    "approvals.back": {"en": "Back to requests", "uz": "So'rovlarga qaytish", "ru": "К заявкам"},
    # ---- phase 2a: the due list, the card's visit lines and the visit loop ----
    # Only `card.overdue`, `list.overdue_suffix`, `visit.checkin_ok`,
    # `visit.checkin_far`, `visit.order_created` and `visit.closed`
    # interpolate (plan ruling 19); every other row here is a bare LABEL and
    # the handler composes the product name, the count and the quantity around
    # it -- e.g. the shelf line reads "• Pure Water 19L — On shelf: 3", built
    # by `VisitHandler`, not by a template. That is deliberate: the bot's
    # renderer drops a kwarg a template has no field for WITHOUT raising, so a
    # placeholder added on one side and not the other is a silently blank
    # screen. A `{placeholder}` present in one language only is a KeyError on
    # that agent's phone alone, which is why
    # `tests/unit/test_staff_translation_catalog_complete.py` compares the
    # sets across all three.
    "hub.due": {"en": "Due today & overdue", "uz": "Bugungi va kechikkan", "ru": "На сегодня и просроченные"},
    "list.title_due": {"en": "Due today & overdue", "uz": "Bugungi va kechikkan", "ru": "На сегодня и просроченные"},
    "list.overdue_suffix": {"en": "+{days}d", "uz": "+{days} kun", "ru": "+{days} дн."},
    "card.next_due": {"en": "Next visit due", "uz": "Keyingi tashrif", "ru": "Следующий визит"},
    "card.overdue": {"en": "overdue by {days} d", "uz": "{days} kun kechikdi", "ru": "просрочено на {days} дн."},
    "card.rate": {"en": "Consumption", "uz": "Sarfi", "ru": "Расход"},
    "card.suggested": {"en": "Suggested order", "uz": "Tavsiya etilgan buyurtma", "ru": "Рекомендуемый заказ"},
    "card.last_visit": {"en": "Last visit", "uz": "So'nggi tashrif", "ru": "Последний визит"},
    "card.start_visit": {"en": "Start visit", "uz": "Tashrifni boshlash", "ru": "Начать визит"},
    "card.resume_visit": {"en": "Resume visit", "uz": "Tashrifni davom ettirish", "ru": "Продолжить визит"},
    "visit.started": {"en": "Visit started.", "uz": "Tashrif boshlandi.", "ru": "Визит начат."},
    "visit.resume_or_abandon": {"en": "You already have an open visit. Resume it or abandon it.", "uz": "Sizda ochiq tashrif bor. Uni davom ettiring yoki bekor qiling.", "ru": "У вас уже есть открытый визит. Продолжите или отмените его."},
    "visit.checkin_prompt": {"en": "Share your location at the outlet's door, or skip.", "uz": "Savdo nuqtasi eshigi oldida joylashuvingizni yuboring yoki o'tkazib yuboring.", "ru": "Отправьте геолокацию у входа в точку или пропустите."},
    "visit.checkin_button": {"en": "📍 I'm at the outlet", "uz": "📍 Men nuqtadaman", "ru": "📍 Я на точке"},
    "visit.checkin_skip": {"en": "Skip check-in", "uz": "Belgilashni o'tkazish", "ru": "Пропустить отметку"},
    # No leading glyph on these two: `VisitHandler.receive_checkin` prefixes
    # its own `mark` (`✅` / `⚠️`, chosen from `in_radius`), so a glyph seeded
    # here as well would double it on the agent's screen.
    "visit.checkin_ok": {"en": "At the outlet ({distance} m)", "uz": "Nuqtada ({distance} m)", "ru": "На точке ({distance} м)"},
    "visit.checkin_far": {"en": "{distance} m from the pin — recorded", "uz": "Belgidan {distance} m uzoqda — qayd etildi", "ru": "{distance} м от метки — записано"},
    "visit.checkin_skipped": {"en": "Check-in skipped.", "uz": "Belgilash o'tkazib yuborildi.", "ru": "Отметка пропущена."},
    "visit.stock_title": {"en": "Stock check", "uz": "Qoldiqni tekshirish", "ru": "Проверка остатков"},
    "visit.stock_hint": {"en": "Tap a product and set what is on the shelf.", "uz": "Mahsulotni bosing va javondagi miqdorni belgilang.", "ru": "Нажмите на товар и укажите остаток на полке."},
    "visit.stock_row": {"en": "On shelf", "uz": "Javonda", "ru": "На полке"},
    # The shelf screen's two dead ends (review #01/#49/#23). An empty
    # catalogue is the state of every install until an admin ticks
    # `products.in_sales_stock_check`, and it is not an error: the visit
    # continues to the order step with an empty count. A FAILED fetch is not
    # the same thing and gets the API error plus a retry, never the empty POST.
    "visit.stock_no_products": {"en": "No products are set for the stock check. Continue to the order.", "uz": "Qoldiq tekshiruvi uchun mahsulot belgilanmagan. Buyurtmaga o'ting.", "ru": "Для проверки остатков не выбрано ни одного товара. Перейдите к заказу."},
    "visit.stock_retry": {"en": "Try again", "uz": "Qayta urinish", "ru": "Повторить"},
    "visit.stock_qty_prompt": {"en": "How many are on the shelf?", "uz": "Javonda nechta bor?", "ru": "Сколько на полке?"},
    "visit.stock_empties_prompt": {"en": "How many empties are waiting?", "uz": "Nechta bo'sh idish tayyor?", "ru": "Сколько пустой тары готово?"},
    "visit.sold_out": {"en": "Sold out", "uz": "Tugagan", "ru": "Закончился"},
    "visit.low": {"en": "Running low", "uz": "Kam qoldi", "ru": "Заканчивается"},
    "visit.stock_done": {"en": "Done", "uz": "Tayyor", "ru": "Готово"},
    "visit.stock_back": {"en": "Back to products", "uz": "Mahsulotlarga qaytish", "ru": "К товарам"},
    "visit.order_title": {"en": "Order", "uz": "Buyurtma", "ru": "Заказ"},
    "visit.order_suggested_line": {"en": "Suggested", "uz": "Tavsiya", "ru": "Рекомендуем"},
    "visit.order_last_line": {"en": "Last order", "uz": "So'nggi buyurtma", "ru": "Прошлый заказ"},
    "visit.order_none_line": {"en": "No suggestion — the shelf is full.", "uz": "Tavsiya yo'q — javon to'la.", "ru": "Рекомендации нет — полка полная."},
    "visit.order_suggested": {"en": "Order suggested", "uz": "Tavsiyani buyurtma qilish", "ru": "Заказать рекомендуемое"},
    "visit.order_edit": {"en": "Edit quantities", "uz": "Miqdorlarni o'zgartirish", "ru": "Изменить количество"},
    "visit.order_last": {"en": "Same as last order", "uz": "So'nggi buyurtmadek", "ru": "Как в прошлый раз"},
    "visit.no_order": {"en": "No order", "uz": "Buyurtmasiz", "ru": "Без заказа"},
    "visit.no_order_reason_prompt": {"en": "Why is there no order?", "uz": "Nega buyurtma yo'q?", "ru": "Почему нет заказа?"},
    "visit.order_edit_title": {"en": "Tap a line to change its quantity.", "uz": "Miqdorni o'zgartirish uchun qatorni bosing.", "ru": "Нажмите на строку, чтобы изменить количество."},
    "visit.order_done": {"en": "Continue", "uz": "Davom etish", "ru": "Продолжить"},
    # Composed onto the basket line by `open_order_line` when the product
    # carries a real floor: "• Pure Water 19L × 20 · min 2". A BARE label
    # (ruling 19) -- the number is the backend's published
    # `min_order_quantity`, printed beside it, never interpolated into it.
    "visit.order_min": {"en": "min", "uz": "kamida", "ru": "мин."},
    "visit.payment_prompt": {"en": "How will the outlet pay?", "uz": "Savdo nuqtasi qanday to'laydi?", "ru": "Как точка будет платить?"},
    # The rails screen's dead end (L105). Deliberately NOT
    # `error.outlet_not_active`: an outlet can be fully active and still be
    # offered no rail -- its COD headroom is used up, or its business account
    # is not billable -- and "activate it first" then sends the agent to an
    # operator with the wrong question. The basket is untouched and the screen
    # does not move (controller ruling 24), so this says what is blocked, not
    # what to redo.
    "visit.no_rails": {"en": "This outlet has no usable payment method right now. Ask an operator to check its account.", "uz": "Bu nuqtada hozir ishlatib bo'ladigan to'lov usuli yo'q. Hisobni tekshirishni operatordan so'rang.", "ru": "У этой точки сейчас нет доступного способа оплаты. Попросите оператора проверить её счёт."},
    "visit.day_prompt": {"en": "When should we deliver?", "uz": "Qachon yetkazamiz?", "ru": "Когда доставить?"},
    "visit.day_tomorrow": {"en": "Tomorrow", "uz": "Ertaga", "ru": "Завтра"},
    "visit.day_today": {"en": "Today", "uz": "Bugun", "ru": "Сегодня"},
    "visit.day_pick": {"en": "Pick a date", "uz": "Sanani tanlash", "ru": "Выбрать дату"},
    "visit.day_pick_prompt": {"en": "Send the delivery date as YYYY-MM-DD.", "uz": "Yetkazish sanasini YYYY-MM-DD ko'rinishida yuboring.", "ru": "Отправьте дату доставки в формате ГГГГ-ММ-ДД."},
    "visit.day_invalid": {"en": "That date is not usable. Send it as YYYY-MM-DD.", "uz": "Bu sana yaramaydi. YYYY-MM-DD ko'rinishida yuboring.", "ru": "Такая дата не подходит. Отправьте в формате ГГГГ-ММ-ДД."},
    # L106: the 🕘 line of the order receipt and the confirm card. The glyph is
    # the HANDLER's -- seeding one here would double it, the same trap
    # `visit.checkin_ok` documents -- and both ends interpolate so the window
    # can be written in either order per language.
    "visit.window_line": {"en": "Delivery window {start}–{end}", "uz": "Yetkazish oralig'i {start}–{end}", "ru": "Окно доставки {start}–{end}"},
    "visit.notes_prompt": {"en": "Notes for the driver (or skip):", "uz": "Haydovchi uchun izoh (yoki o'tkazing):", "ru": "Заметка для водителя (или пропустите):"},
    "visit.confirm_title": {"en": "Check the order before sending it.", "uz": "Yuborishdan oldin buyurtmani tekshiring.", "ru": "Проверьте заказ перед отправкой."},
    "visit.confirm": {"en": "Place order", "uz": "Buyurtma berish", "ru": "Оформить заказ"},
    "visit.back": {"en": "Back", "uz": "Orqaga", "ru": "Назад"},
    "visit.order_created": {"en": "Order {order_number} created.", "uz": "{order_number} buyurtmasi yaratildi.", "ru": "Заказ {order_number} создан."},
    "visit.order_pending_confirmation": {"en": "Waiting for the store to confirm it in their bot.", "uz": "Do'kon o'z botida tasdiqlashini kutmoqdamiz.", "ru": "Ждём подтверждения от магазина в его боте."},
    "visit.order_confirmed": {"en": "Confirmed — it is in the delivery queue.", "uz": "Tasdiqlandi — yetkazish navbatida.", "ru": "Подтверждён — в очереди на доставку."},
    "visit.order_auto_confirmed": {"en": "Confirmed automatically — it is in the delivery queue.", "uz": "Avtomatik tasdiqlandi — yetkazish navbatida.", "ru": "Подтверждён автоматически — в очереди на доставку."},
    "visit.close_outcome_prompt": {"en": "How did the visit end?", "uz": "Tashrif qanday yakunlandi?", "ru": "Чем закончился визит?"},
    "visit.close_notes_prompt": {"en": "Notes for the next visit (or skip):", "uz": "Keyingi tashrif uchun izoh (yoki o'tkazing):", "ru": "Заметка для следующего визита (или пропустите):"},
    "visit.next_visit_prompt": {"en": "When is the next visit?", "uz": "Keyingi tashrif qachon?", "ru": "Когда следующий визит?"},
    "visit.closed": {"en": "Visit closed. Next visit: {next_due}", "uz": "Tashrif yakunlandi. Keyingi tashrif: {next_due}", "ru": "Визит завершён. Следующий визит: {next_due}"},
    # Plan ruling 30: a `POST /visits/<id>/order` that never came back. POSTs
    # are not auto-retried, so the bot cannot know whether the store was just
    # billed -- and saying "failed" would be a guess in the direction that
    # produces a duplicate order. The only honest sentence is "open it and
    # look", with the Resume button beside it.
    "visit.order_maybe_landed": {"en": "The order may have been placed — open the visit to check.", "uz": "Buyurtma yuborilgan bo'lishi mumkin — tekshirish uchun tashrifni oching.", "ru": "Заказ мог быть оформлен — откройте визит и проверьте."},
    # The visit conversation's OWN timeout copy. The shared `staff.flow_timed_out`
    # says the flow was closed and "nothing was saved", which is false here: a
    # timeout does not touch the visit, the server keeps it open, the check-in
    # and the counts are already recorded, and Resume walks back into it.
    "visit.timeout": {"en": "This step timed out. The visit is still open — resume it when you are ready.", "uz": "Bu bosqich vaqti tugadi. Tashrif hali ochiq — tayyor bo'lganingizda davom ettiring.", "ru": "Время этого шага истекло. Визит всё ещё открыт — продолжите, когда будете готовы."},
    "visit.abandoned": {"en": "Visit abandoned.", "uz": "Tashrif bekor qilindi.", "ru": "Визит отменён."},
    "visit.abandon": {"en": "Abandon visit", "uz": "Tashrifni bekor qilish", "ru": "Отменить визит"},
    "visit.resume": {"en": "Resume visit", "uz": "Davom ettirish", "ru": "Продолжить визит"},
    # The four picker families. `SalesKeyboards` builds these keys with
    # f-strings from VISIT_OUTCOMES / NO_ORDER_REASONS / PAY_METHODS /
    # NEXT_VISIT_CHOICES, so they are ALSO registered in `_add_dynamic_keys`
    # below and in its twin `staff_bot/i18n.py::_add_dynamic_family_keys` —
    # the rows here are what gives them a value, the loops are what makes a
    # gap visible to /health.
    "visit.outcome.order_placed": {"en": "Order placed", "uz": "Buyurtma berildi", "ru": "Заказ оформлен"},
    "visit.outcome.no_order": {"en": "No order", "uz": "Buyurtmasiz", "ru": "Без заказа"},
    "visit.outcome.closed": {"en": "Outlet closed", "uz": "Nuqta yopiq edi", "ru": "Точка была закрыта"},
    "visit.outcome.owner_absent": {"en": "Owner absent", "uz": "Egasi yo'q edi", "ru": "Владельца не было"},
    "visit.outcome.refused": {"en": "Refused", "uz": "Rad etdi", "ru": "Отказ"},
    "visit.reason.sufficient_stock": {"en": "Enough stock", "uz": "Qoldiq yetarli", "ru": "Хватает остатка"},
    "visit.reason.cash_issue": {"en": "No money now", "uz": "Hozir puli yo'q", "ru": "Сейчас нет денег"},
    "visit.reason.price": {"en": "Price", "uz": "Narx", "ru": "Цена"},
    "visit.reason.competitor": {"en": "Competitor", "uz": "Raqobatchi", "ru": "Конкурент"},
    "visit.reason.other": {"en": "Other", "uz": "Boshqa", "ru": "Другое"},
    "visit.pay.cash": {"en": "Cash", "uz": "Naqd pul", "ru": "Наличные"},
    "visit.pay.business_account": {"en": "Business account", "uz": "Hisob raqam", "ru": "Расчётный счёт"},
    "visit.next.3": {"en": "In 3 days", "uz": "3 kundan keyin", "ru": "Через 3 дня"},
    "visit.next.7": {"en": "In 7 days", "uz": "7 kundan keyin", "ru": "Через 7 дней"},
    "visit.next.14": {"en": "In 14 days", "uz": "14 kundan keyin", "ru": "Через 14 дней"},
    "visit.next.30": {"en": "In 30 days", "uz": "30 kundan keyin", "ru": "Через 30 дней"},
    "visit.next.none": {"en": "No date", "uz": "Sanasiz", "ru": "Без даты"},
    # Photo, available at every step (D17). The kind family is drawn by
    # `SalesKeyboards.photo_kind` with an f-string, so it is ALSO registered
    # in `_add_dynamic_keys` below and in its twin in `staff_bot/i18n.py`.
    "visit.photo_kind_prompt": {"en": "What is on this photo?", "uz": "Suratda nima tasvirlangan?", "ru": "Что на этом фото?"},
    "visit.photo_kind.storefront": {"en": "Storefront", "uz": "Do'kon peshtoqi", "ru": "Витрина"},
    "visit.photo_kind.shelf": {"en": "Shelf", "uz": "Javon", "ru": "Полка"},
    "visit.photo_kind.other": {"en": "Other", "uz": "Boshqa", "ru": "Другое"},
    "visit.photo_saved": {"en": "Photo saved.", "uz": "Surat saqlandi.", "ru": "Фото сохранено."},
    # The backend's verdict, never the bot's: a SHA-256 match against this
    # agent's earlier photos. Bare, because the duplicate is still stored.
    "visit.photo_duplicate": {"en": "You have already sent this photo.", "uz": "Bu suratni allaqachon yuborgansiz.", "ru": "Вы уже отправляли это фото."},
    "visit.photo_failed": {"en": "The photo could not be saved. Send it again.", "uz": "Suratni saqlab bo'lmadi. Qaytadan yuboring.", "ru": "Фото не удалось сохранить. Отправьте ещё раз."},
    "visit.photo_forwarded": {"en": "Forwarded photos are not accepted — take the photo here.", "uz": "Uzatilgan suratlar qabul qilinmaydi — suratni shu yerda oling.", "ru": "Пересланные фото не принимаются — сделайте снимок здесь."},
    # The morning digest's section labels. Plain text: the handler wraps them
    # in <b> and escapes them, so the HTML lives in exactly one place.
    "notify.digest_due_today": {"en": "Due today", "uz": "Bugunga rejalashtirilgan", "ru": "На сегодня"},
    "notify.digest_overdue": {"en": "Overdue", "uz": "Kechikkan", "ru": "Просроченные"},
    "notify.digest_unvisited": {"en": "Not visited for a while", "uz": "Ancha vaqtdan beri tashrif buyurilmagan", "ru": "Давно без визита"},
    "notify.digest_open_visit": {"en": "You have a visit still open", "uz": "Sizda ochiq tashrif bor", "ru": "У вас есть незакрытый визит"},
    # The AGE of an unvisited row. Overdue rows reuse
    # `list.overdue_suffix` -- "how late" already has one expression, drawn
    # on the due list -- and these are two different facts.
    "notify.digest_days": {"en": "{days} d", "uz": "{days} kun", "ru": "{days} дн."},
    # `days: null` -- a shop NOBODY has ever walked into. Its own word, not a
    # zero: the backend refuses to invent an age from `created_at`, and
    # "0 d" under a 21-day heading would read as "visited today", the exact
    # opposite of what the row means.
    "notify.digest_never": {"en": "never visited", "uz": "hech qachon tashrif bo'lmagan", "ru": "ни одного визита"},
    # `more_count` -- how many due rows the BACKEND cut (Task 3 caps each due
    # section at ten, because an uncapped digest is a `send_message` that
    # raises and three retries that deliver nothing). The figure is rendered,
    # never computed here.
    "notify.digest_more": {"en": "+{count} more", "uz": "yana {count} ta", "ru": "ещё {count}"},
    "error.visit_open": {"en": "You already have an open visit.", "uz": "Sizda allaqachon ochiq tashrif bor.", "ru": "У вас уже есть открытый визит."},
    "error.visit_not_open": {"en": "This visit is already closed.", "uz": "Bu tashrif allaqachon yopilgan.", "ru": "Этот визит уже закрыт."},
    "error.visit_step": {"en": "That step is already done. The visit reopens at its current step.", "uz": "Bu bosqich allaqachon bajarilgan. Tashrif joriy bosqichdan ochiladi.", "ru": "Этот шаг уже пройден. Визит откроется на текущем шаге."},
    "error.stock_qty": {"en": "That quantity is not usable.", "uz": "Bu miqdor yaramaydi.", "ru": "Такое количество не подходит."},
    # M14: an admin un-ticking `products.in_sales_stock_check` mid-visit makes
    # the backend refuse a product the agent is standing in front of. That is
    # not a bad number, and telling them the QUANTITY is unusable sends them
    # to re-type a count that was never the problem. Placeholder-free for the
    # same reason as `error.order_min_qty` -- a 400 carries no body to the
    # staff client -- but it names the real cause and the real remedy.
    "error.stock_product": {"en": "That product is no longer on the stock-check list. Reload the products and count again.", "uz": "Bu mahsulot endi qoldiq tekshiruvi ro'yxatida yo'q. Mahsulotlarni qayta yuklang va yana sanang.", "ru": "Этот товар больше не входит в список проверки остатков. Перезагрузите товары и пересчитайте."},
    "error.outlet_not_active": {"en": "This outlet cannot take orders yet — activate it first.", "uz": "Bu nuqta hali buyurtma qabul qila olmaydi — avval faollashtiring.", "ru": "Эта точка пока не может принимать заказы — сначала активируйте её."},
    "error.order_exists": {"en": "This visit already has an order.", "uz": "Bu tashrifda buyurtma allaqachon bor.", "ru": "В этом визите заказ уже есть."},
    "error.outcome_required": {"en": "Pick how the visit ended.", "uz": "Tashrif qanday yakunlanganini tanlang.", "ru": "Выберите, чем закончился визит."},
    # SALES_ORDER_MIN_QTY. Deliberately GENERIC and placeholder-free: the
    # staff client keeps an error body on `data` for 409s only, so on this 400
    # the bot has the CODE and nothing else -- no product name, no numbers. The
    # screen it lands back on is the basket, where the floor is printed per
    # line (`visit.order_min`), which is where the agent can act on it.
    "error.order_min_qty": {"en": "One line is below this product's minimum order quantity. Raise it and try again.", "uz": "Bir qator ushbu mahsulotning eng kam buyurtma miqdoridan past. Miqdorni oshiring va qayta urinib ko'ring.", "ru": "Одна из строк ниже минимального количества заказа для этого товара. Увеличьте количество и повторите."},
    # M30's ceiling (SALES_STOCK_QTY_MAX), the exact mirror of
    # `error.order_min_qty` and generic for the same reason: on a 400 the bot
    # holds the CODE and nothing else -- no product name, no numbers. The
    # screen it lands back on is the basket, where the line can be lowered.
    "error.order_qty": {"en": "One line is above the maximum quantity allowed. Lower it and try again.", "uz": "Bir qator ruxsat etilgan eng ko'p miqdordan yuqori. Miqdorni kamaytiring va qayta urinib ko'ring.", "ru": "Одна из строк превышает максимально допустимое количество. Уменьшите его и повторите."},
    # SALES_PHOTO_INVALID. Placeholder-free like its neighbours (a 400 leaves
    # the staff client the CODE and nothing else) and it names the remedy,
    # because the cause is almost always a file picked from the gallery.
    "error.photo_invalid": {"en": "That file is not a usable photo. Send a picture taken with the camera.", "uz": "Bu fayl yaroqli surat emas. Kamerada olingan rasmni yuboring.", "ru": "Этот файл не подходит как фото. Отправьте снимок с камеры."},
    # ---- phase 2b: Nearby ----
    # `nearby.distance` is the only row here that interpolates; everything
    # else is a whole sentence or a bare label, and the glyphs (🧭 / 📍) are
    # the handler's — seeding one here would double it, the trap
    # `visit.checkin_ok` documents. The ru unit is Cyrillic `м`: a Latin `m`
    # is exactly the one-letter transliteration a human reader skims past,
    # and it is the seed's own Latin gate that catches it.
    "nearby.title": {"en": "Nearby outlets", "uz": "Yaqin atrofdagi nuqtalar", "ru": "Точки рядом"},
    "nearby.pin_prompt": {"en": "Share your location and I'll list the outlets nearest to you.", "uz": "Joylashuvingizni yuboring — sizga eng yaqin nuqtalarni ko'rsataman.", "ru": "Отправьте геолокацию — покажу ближайшие к вам точки."},
    "nearby.pin_button": {"en": "📍 Share my location", "uz": "📍 Joylashuvimni yuborish", "ru": "📍 Отправить мою геолокацию"},
    "nearby.found": {"en": "Location received.", "uz": "Joylashuv qabul qilindi.", "ru": "Геолокация получена."},
    "nearby.distance": {"en": "{distance} m", "uz": "{distance} m", "ru": "{distance} м"},
    "nearby.again": {"en": "New pin", "uz": "Yangi joylashuv", "ru": "Новая геометка"},
    # ---- phase 2b: try-out from the field ----
    # Every row is a BARE label except `tryout.created` (ruling 19) -- the
    # handler composes the product name and the quantity around them, so a
    # placeholder added here would be dropped silently by `render_translation`.
    "tryout.button": {"en": "Try-out", "uz": "Sinov uchun berish", "ru": "Пробная выдача"},
    "tryout.choose_products": {"en": "Which products go out on try-out?", "uz": "Qaysi mahsulotlar sinov uchun beriladi?", "ru": "Какие товары выдать на пробу?"},
    "tryout.quantity_prompt": {"en": "How many?", "uz": "Nechta?", "ru": "Сколько штук?"},
    "tryout.done": {"en": "Continue", "uz": "Davom etish", "ru": "Продолжить"},
    "tryout.notes_prompt": {"en": "Notes for the driver (or skip):", "uz": "Haydovchi uchun izoh (yoki o'tkazing):", "ru": "Заметка для водителя (или пропустите):"},
    "tryout.confirm_title": {"en": "Check the try-out before creating it.", "uz": "Yaratishdan oldin sinov berishni tekshiring.", "ru": "Проверьте пробную выдачу перед созданием."},
    "tryout.confirm": {"en": "Create try-out", "uz": "Sinov berishni yaratish", "ru": "Создать пробную выдачу"},
    "tryout.created": {"en": "Try-out {tryout_number} created.", "uz": "{tryout_number} sinov berish yaratildi.", "ru": "Пробная выдача {tryout_number} создана."},
    "tryout.handoff_queued": {"en": "The hand-off task is in the driver pool.", "uz": "Yetkazish topshirig'i haydovchilar navbatida.", "ru": "Задача на передачу — в очереди водителей."},
    "tryout.no_products": {"en": "No products are available for a try-out.", "uz": "Sinov uchun mahsulot yo'q.", "ru": "Нет товаров, доступных для пробной выдачи."},
    "tryout.no_phone": {"en": "This outlet has no contact phone. Add one on the outlet card, then create the try-out.", "uz": "Bu nuqtada aloqa telefoni yo'q. Uni nuqta kartasida qo'shing, so'ng sinov berishni yarating.", "ru": "У этой точки нет контактного телефона. Добавьте его в карточке точки, затем создайте пробную выдачу."},
    "tryout.open_outlet": {"en": "Open outlet", "uz": "Nuqtani ochish", "ru": "Открыть точку"},
    # SALES_TRYOUT_ITEMS_INVALID. Placeholder-free for the same reason
    # `error.order_min_qty` is: the staff client keeps an error body on `data`
    # for 409s only, so on this 400 the bot holds the CODE and nothing else.
    # The screen it lands back on is the basket, which is where a quantity can
    # be changed.
    "error.tryout_items": {"en": "Pick at least one product that can go out on try-out.", "uz": "Sinov uchun berish mumkin bo'lgan kamida bitta mahsulotni tanlang.", "ru": "Выберите хотя бы один товар, доступный для пробной выдачи."},
    # ---- phase 3: "My stats" ----
    # Every row is a BARE label: the handler composes the figure, the "%" and
    # the currency around it, so a placeholder here would be dropped silently
    # by `render_translation`. The BUTTON is deliberately not called "My
    # stats": `staff.profile.view_stats` (the driver's row, three lines above
    # it on the same keyboard) already is, word for word in uz and ru, and a
    # staff member who drives AND sells would read the same button twice.
    # Copy fixed by R26; the 📊 glyph is added by the keyboard, house-style.
    "stats.button": {"en": "Sales stats", "uz": "Savdo statistikasi", "ru": "Статистика продаж"},
    "stats.title": {"en": "My sales stats", "uz": "Savdo statistikam", "ru": "Моя статистика продаж"},
    # "not computable for this window" -- printed where the service answered
    # NULL, never where it answered 0.
    "stats.na": {"en": "—", "uz": "—", "ru": "—"},
    "stats.period_today": {"en": "Today", "uz": "Bugun", "ru": "Сегодня"},
    "stats.period_week": {"en": "Week", "uz": "Hafta", "ru": "Неделя"},
    "stats.period_month": {"en": "Month", "uz": "Oy", "ru": "Месяц"},
    "stats.section_visits": {"en": "Visits", "uz": "Tashriflar", "ru": "Визиты"},
    "stats.section_outlets": {"en": "Outlets", "uz": "Savdo nuqtalari", "ru": "Точки"},
    "stats.section_orders": {"en": "Orders", "uz": "Buyurtmalar", "ru": "Заказы"},
    "stats.section_discipline": {"en": "Discipline", "uz": "Intizom", "ru": "Дисциплина"},
    "stats.metric_planned_visits": {"en": "Planned", "uz": "Rejalashtirilgan", "ru": "Запланировано"},
    "stats.metric_completed_visits": {"en": "Completed", "uz": "Yakunlangan", "ru": "Завершено"},
    "stats.metric_plan_vs_fact_pct": {"en": "Plan vs fact", "uz": "Reja va fakt", "ru": "План и факт"},
    "stats.metric_unplanned_visits": {"en": "Unplanned", "uz": "Rejadan tashqari", "ru": "Вне плана"},
    "stats.metric_visits_per_day": {"en": "Visits per day", "uz": "Kuniga tashrif", "ru": "Визитов в день"},
    "stats.metric_strike_rate_pct": {"en": "Visits with an order", "uz": "Buyurtmali tashriflar", "ru": "Визиты с заказом"},
    "stats.metric_assigned_outlets": {"en": "Assigned", "uz": "Biriktirilgan", "ru": "Закреплено"},
    "stats.metric_active_outlets": {"en": "Active", "uz": "Faol", "ru": "Активные"},
    "stats.metric_active_share_pct": {"en": "Active share", "uz": "Faollar ulushi", "ru": "Доля активных"},
    "stats.metric_new_outlets_registered": {"en": "New registered", "uz": "Yangi qo'shilgan", "ru": "Новых добавлено"},
    "stats.metric_new_outlets_activated": {"en": "New activated", "uz": "Yangi faollashgan", "ru": "Новых активировано"},
    "stats.metric_orders_placed": {"en": "Placed", "uz": "Berilgan", "ru": "Оформлено"},
    "stats.metric_orders_delivered_paid": {"en": "Delivered and paid", "uz": "Yetkazilgan va to'langan", "ru": "Доставлено и оплачено"},
    "stats.metric_bottles_delivered_paid": {"en": "Bottles delivered", "uz": "Yetkazilgan idishlar", "ru": "Бутылей доставлено"},
    "stats.metric_revenue_delivered_paid": {"en": "Revenue", "uz": "Tushum", "ru": "Выручка"},
    "stats.metric_agent_orders_cancelled": {"en": "Cancelled", "uz": "Bekor qilingan", "ru": "Отменено"},
    "stats.metric_suggested_vs_accepted_pct": {"en": "Suggested accepted", "uz": "Tavsiyadan qabul qilingan", "ru": "Принято из рекомендованного"},
    "stats.metric_out_of_range_checkins": {"en": "Check-ins out of range", "uz": "Radiusdan tashqari belgilanishlar", "ru": "Отметки вне радиуса"},
    "stats.metric_skipped_checkins": {"en": "Check-ins skipped", "uz": "O'tkazib yuborilgan belgilanishlar", "ru": "Пропущенные отметки"},
    "stats.metric_avg_visit_minutes": {"en": "Average visit, minutes", "uz": "O'rtacha tashrif, daqiqa", "ru": "Средний визит, минут"},
}


TOKEN_TRANSLATIONS = {
    "uz": {
        "address": "manzil",
        "active": "faol",
        "add": "qoshish",
        "again": "yana",
        "already": "allaqachon",
        "api": "API",
        "assigned": "biriktirilgan",
        "auth": "auth",
        "available": "mavjud",
        "back": "orqaga",
        "cancel": "bekor",
        "cash": "naqd",
        "changed": "ozgardi",
        "client": "mijoz",
        "collection": "yigim",
        "confirm": "tasdiq",
        "count": "soni",
        "created": "yaratildi",
        "current": "joriy",
        "delivery": "yetkazish",
        "details": "batafsil",
        "enter": "kiriting",
        "error": "xatolik",
        "failed": "muvaffaqiyatsiz",
        "history": "tarix",
        "invalid": "noto'gri",
        "items": "mahsulotlar",
        "language": "til",
        "login": "kirish",
        "menu": "menyu",
        "my": "mening",
        "new": "yangi",
        "no": "yoq",
        "not": "emas",
        "notification": "bildirishnoma",
        "operator": "operator",
        "order": "buyurtma",
        "payment": "tolov",
        "phone": "telefon",
        "pool": "havza",
        "profile": "profil",
        "prompt": "sorov",
        "results": "natijalar",
        "search": "qidiruv",
        "select": "tanlash",
        "session": "sessiya",
        "settings": "sozlamalar",
        "share": "ulashish",
        "stats": "statistika",
        "status": "holat",
        "success": "muvaffaqiyat",
        "title": "sarlavha",
        "too": "juda",
        "updated": "yangilandi",
        "user": "foydalanuvchi",
        "welcome": "xush kelibsiz",
    },
    "ru": {
        "address": "адрес",
        "active": "активный",
        "add": "добавить",
        "again": "снова",
        "already": "уже",
        "api": "API",
        "assigned": "назначен",
        "auth": "авторизация",
        "available": "доступен",
        "back": "назад",
        "cancel": "отмена",
        "cash": "наличные",
        "changed": "изменен",
        "client": "клиент",
        "collection": "сбор",
        "confirm": "подтвердить",
        "count": "количество",
        "created": "создано",
        "current": "текущий",
        "delivery": "доставка",
        "details": "детали",
        "enter": "введите",
        "error": "ошибка",
        "failed": "неудачно",
        "history": "история",
        "invalid": "некорректно",
        "items": "товары",
        "language": "язык",
        "login": "вход",
        "menu": "меню",
        "my": "мой",
        "new": "новый",
        "no": "нет",
        "not": "не",
        "notification": "уведомление",
        "operator": "оператор",
        "order": "заказ",
        "payment": "оплата",
        "phone": "телефон",
        "pool": "список",
        "profile": "профиль",
        "prompt": "запрос",
        "results": "результаты",
        "search": "поиск",
        "select": "выберите",
        "session": "сессия",
        "settings": "настройки",
        "share": "отправить",
        "stats": "статистика",
        "status": "статус",
        "success": "успешно",
        "title": "заголовок",
        "too": "слишком",
        "updated": "обновлен",
        "user": "пользователь",
        "welcome": "добро пожаловать",
    },
}


def _extract_literal_keys(repo_root: Path) -> Set[str]:
    """Collect literal keys from i18n.get('...') calls in staff bot files."""
    pattern = re.compile(r"""i18n\.get\(\s*(['"])([^'"]+)\1\s*[,)]""")
    keys: Set[str] = set()

    staff_root = repo_root / "staff_bot"
    for path in staff_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for _, match in pattern.findall(text):
            if match.startswith("staff."):
                keys.add(match)
    return keys


def _add_dynamic_keys(keys: Set[str]) -> None:
    """Add f-string based key families that static regex cannot enumerate."""
    # Role labels
    for role in STAFF_BOT_ROLES:
        keys.add(f"staff.role.{role}")

    # Sales events — the family webhook_server.sales_event_handler builds with
    # f'staff.sales.notify.{event}', from the SAME tuple that decides which
    # events the bot accepts (shared/staff_constants.py::SALES_EVENTS).
    for event in SALES_EVENTS:
        keys.add(f"staff.sales.notify.{event}")

    # The outlet card families — the twin of
    # `staff_bot/i18n.py::_add_dynamic_family_keys`. The stage/type tuples
    # mirror `business_app/models/sales.py::OUTLET_STAGES / OUTLET_TYPES`;
    # keep the two sides in step, exactly as the delivery statuses above.
    for stage in ("prospect", "trial", "activation_requested", "active", "at_risk", "dormant", "lost"):
        keys.add(f"staff.sales.stage.{stage}")

    for outlet_type in ("grocery_store", "workplace", "individual"):
        keys.add(f"staff.sales.type.{outlet_type}")

    for scope in ("due", "prospects", "all"):
        keys.add(f"staff.sales.list.title_{scope}")

    # The visit conversation's four picker families. These tuples are a COPY
    # of `staff_bot/keyboards/sales.py`'s VISIT_OUTCOMES / NO_ORDER_REASONS /
    # PAY_METHODS / NEXT_VISIT_CHOICES, because this script must keep running
    # inside the business_app container, which has no `staff_bot/` tree to
    # import from (see `_add_curated_keys`). The copy is pinned by
    # `tests/unit/test_sales_visit_bot_plumbing.py::
    # test_the_seed_scripts_hand_written_tuples_match_the_keyboard_constants`,
    # so a value added there and forgotten here fails a test instead of
    # shipping a key tail to an agent.
    for outcome in ("order_placed", "no_order", "closed", "owner_absent", "refused"):
        keys.add(f"staff.sales.visit.outcome.{outcome}")

    for reason in ("sufficient_stock", "cash_issue", "price", "competitor", "other"):
        keys.add(f"staff.sales.visit.reason.{reason}")

    for method in ("cash", "business_account"):
        keys.add(f"staff.sales.visit.pay.{method}")

    for choice in ("3", "7", "14", "30", "none"):
        keys.add(f"staff.sales.visit.next.{choice}")

    for kind in ("storefront", "shelf", "other"):
        keys.add(f"staff.sales.visit.photo_kind.{kind}")

    for period in ("today", "week", "month"):
        keys.add(f"staff.sales.stats.period_{period}")

    # Delivery statuses — derived from the ENUM, never a hand-written tuple.
    # A hardcoded six-status list here (and the twin in
    # staff_bot/i18n.py::_add_dynamic_family_keys) is what hid
    # `staff.delivery.status.cancelled` from both the seeder and /health while
    # every active-delivery card rendered it as the English word "Cancelled".
    for status in DeliveryStatus:
        keys.add(f"staff.delivery.status.{status.value}")

    # Backend-supplied statuses the bot displays verbatim.
    for status in DriverCashSessionStatus:
        keys.add(f"staff.delivery.cash_session_status.{status.value}")

    for status in DriverBottleSessionStatus:
        keys.add(f"staff.delivery.bottle_session_status.{status.value}")

    for flag in RECONCILIATION_RISK_FLAGS:
        keys.add(f"staff.delivery.risk_flag.{flag}")

    # Failure reasons
    for reason in FAILED_DELIVERY_REASONS:
        keys.add(f"staff.delivery.reason.{reason}")

    # Payment labels
    for payment in PaymentMethod:
        keys.add(f"staff.delivery.payment.{payment.value}")
        keys.add(f"staff.operator.payment_{payment.value}")

    # Order status labels (operator order-pool details)
    for status in OrderStatus:
        keys.add(f"staff.order.status.{status.value}")


def _add_curated_keys(keys: Set[str]) -> None:
    """
    Add all curated key catalogs.

    This makes seeding deterministic even when staff_bot source files are not
    available in the current runtime (e.g. business_app container).
    """
    keys.update(STAFF_TRANSLATIONS.keys())
    keys.update(EXTRA_TRANSLATIONS.keys())

    for suffix in DELIVERY_TEXT_TRANSLATIONS.keys():
        keys.add(f"staff.delivery.{suffix}")

    for suffix in OPERATOR_TEXT_TRANSLATIONS.keys():
        keys.add(f"staff.operator.{suffix}")

    for suffix in SALES_TEXT_TRANSLATIONS.keys():
        keys.add(f"staff.sales.{suffix}")


def _auto_family_translation(key: str, language: str) -> Optional[str]:
    """Resolve known dynamic key families."""
    extra = EXTRA_TRANSLATIONS.get(key, {})
    if language in extra:
        return extra[language]
    if "en" in extra:
        return extra["en"]

    if key.startswith("staff.delivery."):
        suffix = key.split("staff.delivery.", 1)[1]
        if suffix in DELIVERY_TEXT_TRANSLATIONS:
            scoped = DELIVERY_TEXT_TRANSLATIONS[suffix]
            if language in scoped:
                return scoped[language]
            if "en" in scoped:
                return scoped["en"]

    if key.startswith("staff.operator."):
        suffix = key.split("staff.operator.", 1)[1]
        if suffix in OPERATOR_TEXT_TRANSLATIONS:
            scoped = OPERATOR_TEXT_TRANSLATIONS[suffix]
            if language in scoped:
                return scoped[language]
            if "en" in scoped:
                return scoped["en"]

    if key.startswith("staff.sales."):
        suffix = key.split("staff.sales.", 1)[1]
        if suffix in SALES_TEXT_TRANSLATIONS:
            scoped = SALES_TEXT_TRANSLATIONS[suffix]
            if language in scoped:
                return scoped[language]
            if "en" in scoped:
                return scoped["en"]

    if key.startswith("staff.role."):
        role = key.rsplit(".", 1)[-1]
        return ROLE_TRANSLATIONS.get(role, {}).get(language)

    if key.startswith("staff.delivery.status."):
        status = key.rsplit(".", 1)[-1]
        return DELIVERY_STATUS_TRANSLATIONS.get(status, {}).get(language)

    if key.startswith("staff.delivery.cash_session_status."):
        status = key.rsplit(".", 1)[-1]
        return CASH_SESSION_STATUS_TRANSLATIONS.get(status, {}).get(language)

    if key.startswith("staff.delivery.bottle_session_status."):
        status = key.rsplit(".", 1)[-1]
        return BOTTLE_SESSION_STATUS_TRANSLATIONS.get(status, {}).get(language)

    if key.startswith("staff.delivery.risk_flag."):
        flag = key.rsplit(".", 1)[-1]
        return RISK_FLAG_TRANSLATIONS.get(flag, {}).get(language)

    if key.startswith("staff.delivery.reason."):
        reason = key.rsplit(".", 1)[-1]
        return FAILED_REASON_TRANSLATIONS.get(reason, {}).get(language)

    if key.startswith("staff.delivery.payment."):
        payment = key.rsplit(".", 1)[-1]
        return PAYMENT_TRANSLATIONS.get(payment, {}).get(language)

    if key.startswith("staff.operator.payment_"):
        payment = key.split("staff.operator.payment_", 1)[1]
        return PAYMENT_TRANSLATIONS.get(payment, {}).get(language)

    if key.startswith("staff.order.status."):
        status = key.rsplit(".", 1)[-1]
        return ORDER_STATUS_TRANSLATIONS.get(status, {}).get(language)

    return None


def _humanize_key(key: str, language: str) -> str:
    """Convert key tail into a readable fallback phrase with light localization."""
    tail = key.split("staff.", 1)[-1] if key.startswith("staff.") else key
    tokens = [token for token in re.split(r"[._]", tail) if token]

    if language in TOKEN_TRANSLATIONS:
        translated_tokens = [TOKEN_TRANSLATIONS[language].get(token, token) for token in tokens]
    else:
        translated_tokens = tokens

    phrase = " ".join(translated_tokens).strip()
    if not phrase:
        return key
    return phrase[0].upper() + phrase[1:]


def _curated_value(key: str, language: str) -> Optional[str]:
    """Resolve from the curated map / dynamic families ONLY.

    Returns None for a key this script has no opinion about — the caller must
    then decide between "invent a humanised placeholder" (fine for a row that
    does not exist yet) and "leave the existing row alone" (mandatory: another
    seed owns it).
    """
    curated = STAFF_TRANSLATIONS.get(key, {})
    if language in curated:
        return curated[language]
    if "en" in curated:
        return curated["en"]

    dynamic_value = _auto_family_translation(key, language)
    if dynamic_value:
        return dynamic_value

    return None


def _resolve_value(key: str, language: str) -> str:
    """Resolve translation value from curated map, dynamic families, or fallback."""
    value = _curated_value(key, language)
    if value:
        return value

    # Fallback for uncatalogued keys.
    return _humanize_key(key, language)


def _validate_russian_translations(keys: Set[str]) -> None:
    """
    Ensure generated RU translations are readable Cyrillic text.
    Allows placeholders and known Latin brand tokens only.
    """
    invalid: list[tuple[str, str]] = []

    for key in sorted(keys):
        value = _resolve_value(key, "ru")
        # HTML is markup, not copy. `<b>` used to contribute two Latin letters to
        # every value that carried it, which is why the notify rows that ship HTML
        # were kept outside every caller of this validator — a gap, not a licence.
        # Placeholders are already stripped below for the same reason.
        countable = re.sub(r"<[^>]+>", "", value)
        normalized = re.sub(r"\{[^{}]+\}", "", countable)
        for token in RU_ALLOWED_LATIN_TOKENS:
            normalized = re.sub(re.escape(token), "", normalized, flags=re.IGNORECASE)

        if re.search(r"[A-Za-z]", normalized):
            invalid.append((key, value))

    if invalid:
        preview = "\n".join(f"  - {key}: {value}" for key, value in invalid[:15])
        raise ValueError(
            "Russian translation quality check failed: Latin transliteration detected.\n"
            f"{preview}\n"
            f"Total invalid RU values: {len(invalid)}"
        )


def collect_keys(repo_root: Path) -> Set[str]:
    """Every `staff.*` key this script is responsible for."""
    keys = _extract_literal_keys(repo_root)
    _add_dynamic_keys(keys)
    _add_curated_keys(keys)
    return keys


def seed_translations(keys: Set[str]) -> Dict[str, int]:
    """Upsert `keys` x LANGUAGES into category='staff_bot'. Requires an app context.

    ABSENT-ONLY for uncurated keys. ``_extract_literal_keys`` scans
    ``staff_bot/**/*.py``, so it picks up keys that a *different*, curated seed
    owns (e.g. the eight place-group strings in
    scripts/seed_place_group_staff_translations.py). For those,
    ``_curated_value`` returns None and the only value this script could write
    is ``_humanize_key`` guesswork — which would silently replace
    "Bottles at this place across all members: {union}" with "Fine place union
    hint", dropping the placeholder in all three languages while staff_bot's
    /health stays green because the row still exists. So: humanise only when
    CREATING a row that does not exist yet; never overwrite one. Behaviour for
    curated keys is unchanged.
    """
    created = 0
    updated = 0
    skipped = 0

    for key in sorted(keys):
        for lang in LANGUAGES:
            existing = Translation.query.filter_by(key=key, language=lang).first()
            curated = _curated_value(key, lang)

            if existing is not None:
                if curated is None:
                    skipped += 1
                    continue
                existing.value = curated
                existing.category = "staff_bot"
                existing.is_active = True
                updated += 1
            else:
                db.session.add(
                    Translation(
                        key=key,
                        language=lang,
                        value=curated if curated else _humanize_key(key, lang),
                        category="staff_bot",
                        is_active=True,
                    )
                )
                created += 1

    db.session.commit()
    return {"keys": len(keys), "created": created, "updated": updated, "skipped": skipped}


def _repo_root() -> Path:
    """Where to look for ``staff_bot/`` — tolerant of being piped over stdin.

    ``scripts/`` is NOT volume-mounted, so the ONLY documented way to run this is
        docker compose exec -T business_app python - < scripts/seed_staff_translations.py
    and under ``python -`` there is no ``__file__`` at all: reading it raised
    ``NameError`` before a single row was written, which made the sole documented
    deploy path a no-op.

    The fallback is safe rather than lucky. ``Path.cwd()`` is the container's
    WORKDIR ``/app``, which has no ``staff_bot/`` tree; ``_extract_literal_keys``
    rglobs a non-existent directory, which yields nothing instead of raising, so
    the key set degrades to ``_add_curated_keys`` alone. That degradation is the
    documented, pinned behaviour — see
    ``tests/unit/test_staff_translation_seed_script_regressions.py::test_seed_script_adds_curated_keys``
    ("prevents partial seeding when staff_bot source directory is unavailable in
    the runtime container"). Run from a checkout instead and ``__file__`` is
    defined, so the full source scan still happens.
    """
    if "__file__" in globals():
        return Path(__file__).resolve().parents[1]
    return Path.cwd()


def main() -> int:
    app = create_app()
    repo_root = _repo_root()

    with app.app_context():
        keys = collect_keys(repo_root)
        # _validate_russian_translations(keys)

        stats = seed_translations(keys)

        print(
            f"Staff translations seeded: keys={stats['keys']}, "
            f"created={stats['created']}, updated={stats['updated']}, "
            f"skipped_uncurated={stats['skipped']}"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
