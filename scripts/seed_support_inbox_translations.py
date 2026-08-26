"""Seed the Support Inbox's new admin-UI strings.

Category is `ui`, NOT `admin_ui`: AdminUiTranslationService maps the `common`
namespace to SHARED_UI_CATEGORY = "ui", and that is where every existing
ui.support.* row already lives. `admin_ui` is not a served category at all.

The key set below was collected via:

    grep -rho "ui\\.support\\.[a-z_]*" admin_ui/src/components/support/ admin_ui/src/pages/SupportInbox.js | sort -u

and filtered down to the keys that were NOT already present in the
`translations` table (checked via psql) — those pre-existing keys (title,
send, sent, send_failed, delivery_failed, message_placeholder,
search_placeholder, select_conversation, select_user, no_conversations,
new_message, empty_thread, conversations, unread) already carry all three
languages from earlier work and are left untouched here.
"""
from business_app import create_app, db
from business_app.models.translation import Translation

TRANSLATIONS = {
    "en": {
        "ui.support.today": "Today",
        "ui.support.yesterday": "Yesterday",
        "ui.support.forwarded_from": "Forwarded from {{name}}",
        "ui.support.forwarded_from_hidden": "Forwarded from a hidden sender",
        "ui.support.attachment_too_large": "Attachment is too large for Telegram to serve (over 20 MB)",
        "ui.support.attachment_unavailable": "Attachment is unavailable",
        "ui.support.download": "Download",
        "ui.support.open_in_maps": "Open in maps",
        "ui.support.attach": "Attach a file",
        "ui.support.attach_pin": "Attach a pin",
        "ui.support.send_pin": "Send pin",
        "ui.support.bad_coordinates": "Could not read those coordinates",
        "ui.support.unsupported_attachment": "Unsupported attachment",
    },
    "ru": {
        "ui.support.today": "Сегодня",
        "ui.support.yesterday": "Вчера",
        "ui.support.forwarded_from": "Переслано от {{name}}",
        "ui.support.forwarded_from_hidden": "Переслано от скрытого отправителя",
        "ui.support.attachment_too_large": "Вложение слишком большое для Telegram (более 20 МБ)",
        "ui.support.attachment_unavailable": "Вложение недоступно",
        "ui.support.download": "Скачать",
        "ui.support.open_in_maps": "Открыть на карте",
        "ui.support.attach": "Прикрепить файл",
        "ui.support.attach_pin": "Прикрепить точку",
        "ui.support.send_pin": "Отправить точку",
        "ui.support.bad_coordinates": "Не удалось распознать координаты",
        "ui.support.unsupported_attachment": "Вложение не поддерживается",
    },
    "uz": {
        "ui.support.today": "Bugun",
        "ui.support.yesterday": "Kecha",
        "ui.support.forwarded_from": "{{name}} dan yuborilgan",
        "ui.support.forwarded_from_hidden": "Yashirin yuboruvchidan yuborilgan",
        "ui.support.attachment_too_large": "Ilova Telegram uchun juda katta (20 MB dan ortiq)",
        "ui.support.attachment_unavailable": "Ilova mavjud emas",
        "ui.support.download": "Yuklab olish",
        "ui.support.open_in_maps": "Xaritada ochish",
        "ui.support.attach": "Fayl biriktirish",
        "ui.support.attach_pin": "Nuqta biriktirish",
        "ui.support.send_pin": "Nuqta yuborish",
        "ui.support.bad_coordinates": "Koordinatalarni o'qib bo'lmadi",
        "ui.support.unsupported_attachment": "Ilova qo'llab-quvvatlanmaydi",
    },
}

app = create_app()
with app.app_context():
    Translation.bulk_create_or_update(TRANSLATIONS, category="ui")
    db.session.commit()
    print(f"Seeded {sum(len(v) for v in TRANSLATIONS.values())} rows into category 'ui'")
