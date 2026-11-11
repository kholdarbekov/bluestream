# scripts/seed_staff_shared_place_translations.py
"""Seed the STAFF-BOT keys of the shared-place question on the Delivered screen.

When a first-order customer hands back empties, the backend opens a shared-place
case and the driver is asked ONE question right under the "Delivered" message:
is this customer the same as one of the nearby places (up to three candidates),
or none of them, or skip. After a pick the driver may type a note for the admin.

category='staff_bot' with DOTTED `staff.*` keys -- the namespace
scripts/seed_staff_translations.py owns. A standalone ADDITIVE script for the same
reason scripts/seed_staff_over_returned_translations.py is one: that generator
resolves any key it does not recognise to a humanised fallback, which would drop
the placeholders in all three languages while staff_bot's /health stays green.
None of these suffixes appears in that file (asserted by
tests/unit/test_staff_bot_shared_place_seed.py).

Names inside `parse_mode='HTML'` text are escaped by the handler; names inside
inline-button labels stay raw. A blank name renders as an em dash.

Deploy ordering: staff_bot's /health enumerates every literal `staff.*` key used
under staff_bot/ and returns 503 while any of them is missing from the catalog.
Seed BEFORE or WITH the staff_bot deploy, never after.

Run inside the business_app container (scripts/ is NOT volume-mounted, so pipe
the file in over stdin -- piped stdin still executes as __main__):
    docker compose exec -T business_app python - < scripts/seed_staff_shared_place_translations.py
Then restart the bot (the translation cache is per-process):
    docker compose restart staff_bot
"""

from business_app import create_app
from business_app.models.translation import Translation

CATEGORY = "staff_bot"

# {key: {language: value}}.
KEYS = {
    "staff.delivery.shared_place.question": {
        "en": "Is {name} the same as one of these?",
        "uz": "{name} quyidagilardan biri bilan bir xil joydami?",
        "ru": "{name} — это то же место, что и одно из этих?",
    },
    "staff.delivery.shared_place.candidate": {
        "en": "{name} · {empties} empties · {metres} m",
        "uz": "{name} · {empties} ta bo'sh idish · {metres} m",
        "ru": "{name} · пустых: {empties} · {metres} м",
    },
    "staff.delivery.shared_place.none": {
        "en": "None of these",
        "uz": "Hech biri",
        "ru": "Ничего из этого",
    },
    "staff.delivery.shared_place.skip": {
        "en": "⏭ Skip",
        "uz": "⏭ O'tkazib yuborish",
        "ru": "⏭ Пропустить",
    },
    "staff.delivery.shared_place.picked": {
        "en": "✅ {choice}",
        "uz": "✅ {choice}",
        "ru": "✅ {choice}",
    },
    "staff.delivery.shared_place.note_prompt": {
        "en": "Anything for the admin? Type it, or Skip.",
        "uz": "Adminga aytadigan gap bormi? Yozing yoki «O'tkazib yuborish»ni bosing.",
        "ru": "Что-нибудь передать админу? Напишите или нажмите «Пропустить».",
    },
    "staff.delivery.shared_place.no_candidates": {
        "en": "{name} returned empties. Anything for the admin? Type it, or Skip.",
        "uz": (
            "{name} bo'sh idishlarni qaytardi. "
            "Adminga aytadigan gap bormi? Yozing yoki «O'tkazib yuborish»ni bosing."
        ),
        "ru": "{name} вернул(а) пустую тару. Что-нибудь передать админу? Напишите или нажмите «Пропустить».",
    },
    "staff.delivery.shared_place.thanks": {
        "en": "Thanks — sent to the admin.",
        "uz": "Rahmat — adminga yuborildi.",
        "ru": "Спасибо — отправлено админу.",
    },
    "staff.delivery.shared_place.skipped": {
        "en": "Skipped.",
        "uz": "O'tkazib yuborildi.",
        "ru": "Пропущено.",
    },
    "staff.delivery.shared_place.note_too_long": {
        "en": "Please keep it to {max} characters or fewer.",
        "uz": "Iltimos, {max} ta belgidan oshirmang.",
        "ru": "Пожалуйста, уложитесь в {max} символов.",
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
