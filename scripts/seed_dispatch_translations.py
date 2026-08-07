"""Seed admin-UI dispatch translations (namespace `delivery` / category ui_delivery).

Run it piped into the container — `scripts/` is NOT mounted into business_app:

    docker compose exec -T business_app python - < scripts/seed_dispatch_translations.py
"""

from business_app import create_app, db
from business_app.models.translation import Translation

KEYS = {
    "ui.nav.dispatch": ("Dispecher", "Диспетчер", "Dispatch"),
    "ui.dispatch.title": ("Dispecher", "Диспетчер", "Dispatch"),
    "ui.dispatch.layer_orders": ("Faol buyurtmalar", "Активные заказы", "Active orders"),
    "ui.dispatch.layer_drivers": ("Kuryerlar va marshrutlar", "Курьеры и маршруты", "Drivers & routes"),
    "ui.dispatch.stops": ("to'xtash", "остановок", "stops"),
    "ui.dispatch.metrics_stale_hint": (
        "Bu marshrut o'zgargandan beri masofa/vaqt qayta o'lchanmagan — ular joriy to'xtashlarga to'g'ri kelmasligi mumkin",
        "Маршрут изменился с момента последнего расчёта расстояния/времени — они могут не соответствовать текущим остановкам",
        "This route changed since these figures were last measured — they may not match the current stops",
    ),
    "ui.dispatch.save_route": ("Marshrutni saqlash", "Сохранить маршрут", "Save route"),
    "ui.dispatch.discard": ("Bekor qilish", "Отменить", "Discard"),
    "ui.dispatch.reoptimize": ("Optimalga qaytarish", "Сбросить к оптимальному", "Reset to optimal"),
    "ui.dispatch.reoptimize_confirm": (
        "Qo'lda tartib o'chirilib, qayta optimallashtirilsinmi?",
        "Очистить ручной порядок и переоптимизировать?",
        "Clear the manual order and re-optimise?",
    ),
    "ui.dispatch.pin_hint": (
        "Qayta optimallashtirishda bu to'xtash shu o'rinda qoladi",
        "Эта остановка останется на своём месте при переоптимизации",
        "Keep this stop at this position when re-optimising",
    ),
    "ui.dispatch.pool_confirm": (
        "Bu to'xtash umumiy ro'yxatga qaytarilsinmi?",
        "Вернуть эту остановку в общий пул?",
        "Return this stop to the unassigned pool?",
    ),
    "ui.dispatch.locked_by": ("Belgilagan:", "Задал:", "Set by"),
    "ui.dispatch.dispatch": ("dispecher", "диспетчер", "dispatch"),
    "ui.dispatch.saved": ("Marshrut saqlandi", "Маршрут сохранён", "Route saved"),
    "ui.dispatch.save_failed": ("Marshrutni saqlab bo'lmadi", "Не удалось сохранить маршрут", "Could not save the route"),
    "ui.dispatch.move_failed": ("To'xtashni ko'chirib bo'lmadi", "Не удалось перенести остановку", "Could not move this stop"),
    "ui.dispatch.pool_failed": (
        "To'xtashni umumiy ro'yxatga qaytarib bo'lmadi",
        "Не удалось вернуть остановку в пул",
        "Could not pool this stop",
    ),
    "ui.dispatch.reoptimize_failed": (
        "Qayta optimallashtirish muvaffaqiyatsiz",
        "Переоптимизация не удалась",
        "Re-optimisation failed",
    ),
    "ui.dispatch.polling_paused": (
        "Avto-yangilanish to'xtatildi — saqlanmagan o'zgarishlar",
        "Автообновление приостановлено — есть несохранённые изменения",
        "Auto-refresh paused — unsaved changes",
    ),
    "ui.dispatch.unmapped": ("koordinatasiz buyurtmalar", "заказы без координат", "orders without coordinates"),
    "ui.dispatch.unmapped_title": (
        "Xaritada yo'q (koordinata yo'q)",
        "Нет на карте (нет координат)",
        "Not on the map (no coordinates)",
    ),
    "ui.dispatch.no_routes": ("Bugun rejalashtirilgan marshrut yo'q", "На сегодня нет маршрутов", "No planned routes today"),
    "ui.dispatch.conflict_title": (
        "Siz tahrirlayotganda marshrut o'zgardi",
        "Маршрут изменился, пока вы его редактировали",
        "This route changed while you were editing it",
    ),
    "ui.dispatch.conflict_body": (
        "Qoralamani bekor qiling va yangilangan marshrutda qayta qo'llang.",
        "Отмените черновик и примените изменения к обновлённому маршруту.",
        "Discard your draft and re-apply it on the refreshed route.",
    ),
    "ui.dispatch.conflict_reload": ("Marshrutni yangilash", "Обновить маршрут", "Reload route"),
    "ui.dispatch.unassigned": ("Tayinlanmagan", "Не назначен", "Unassigned"),
    "ui.dispatch.overdue": ("Muddati o'tgan", "Просрочен", "Overdue"),
    "ui.dispatch.pinned": ("Mahkamlangan", "Закреплён", "Pinned"),
    "ui.dispatch.active_stops": ("faol to'xtash", "активных остановок", "active stops"),
    "ui.dispatch.pool_title": ("Umumiy ro'yxat", "Общий пул", "Unassigned pool"),
    "ui.dispatch.pool_empty": ("Kutayotgan narsa yo'q", "Ничего не ожидает", "Nothing waiting"),
    "ui.dispatch.assign": ("Tayinlash", "Назначить", "Assign"),
    "ui.dispatch.no_drivers_available": (
        "Kuryerlar mavjud emas",
        "Нет доступных курьеров",
        "No drivers available",
    ),
    "ui.dispatch.no_other_drivers": (
        "Boshqa kuryer yo'q",
        "Нет других курьеров",
        "No other drivers available",
    ),
    "ui.users.map.layer_orders": ("Faol buyurtmalar", "Активные заказы", "Active orders"),
    "ui.users.map.layer_drivers": ("Kuryerlar va marshrutlar", "Курьеры и маршруты", "Drivers & routes"),
    "ui.users.map.open_dispatch": ("Dispecherda ochish ↗", "Открыть в Диспетчере ↗", "Open in Dispatch ↗"),
}

app = create_app()
with app.app_context():
    delivery_payload = {"uz": {}, "ru": {}, "en": {}}
    users_payload = {"uz": {}, "ru": {}, "en": {}}
    for key, (uz, ru, en) in KEYS.items():
        target = users_payload if key.startswith("ui.users.") else delivery_payload
        target["uz"][key] = uz
        target["ru"][key] = ru
        target["en"][key] = en

    Translation.bulk_create_or_update(delivery_payload, category="ui_delivery")
    Translation.bulk_create_or_update(users_payload, category="ui_users")
    db.session.commit()
    print(f"Seeded {len(KEYS)} dispatch translation keys x3 languages")
