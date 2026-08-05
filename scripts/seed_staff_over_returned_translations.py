# scripts/seed_staff_over_returned_translations.py
"""Seed the four STAFF-BOT keys that make "over-returned" a first-class state.

A place goes NEGATIVE when more empties came back through that door than were
ever delivered there. Since the bottle balance was re-keyed onto the PLACE (the
address group when one exists, else the address), the driver's statement,
address picker, quantity guard, collection receipt, fine prompt and at-door
return prompt can all land on such a place — and a bare "-3" reads like a typo
at a customer's door. These four strings name the state instead.

category='staff_bot' with DOTTED `staff.*` keys — the namespace
scripts/seed_staff_translations.py owns. This is a standalone ADDITIVE script
for the same reason scripts/seed_place_group_staff_translations.py is one: that
generator resolves any key it does not recognise to a humanised fallback
(`_humanize_key`), which would replace
"Collected {quantity} bottle(s). This place is now over-returned by {remaining}."
with "Bottle collection recorded over returned" — dropping BOTH placeholders in
all three languages while staff_bot's /health stays green, because the row still
exists. None of these four suffixes appears in that file, and none of them
belongs in seed_place_group_staff_translations.py either (its guards assert an
exact 8-key set); both facts are asserted by
tests/unit/test_staff_bot_over_returned.py.

Deploy ordering: staff_bot's /health enumerates every literal `staff.*` key used
under staff_bot/ and returns 503 while any of them is missing from the catalog
(staff_bot/i18n.py:180-200, staff_bot/webhook_server.py:170-195). Seed BEFORE or
WITH the staff_bot deploy, never after.

Run inside the business_app container (scripts/ is NOT volume-mounted, so pipe
the file in over stdin — piped stdin still executes as __main__):
    docker compose exec -T business_app python - < scripts/seed_staff_over_returned_translations.py
Then restart the bot (the translation cache is per-process):
    docker compose restart staff_bot
"""

from business_app import create_app
from business_app.models.translation import Translation

CATEGORY = "staff_bot"

# {key: {language: value}}. Every count interpolated here is a MAGNITUDE — the
# handlers pass abs(balance) — so the copy has to supply the direction ("over-
# returned by 3"), never a minus sign the driver has to interpret.
KEYS = {
    # Statement body + quantity-picker guard
    # (staff_bot/handlers/delivery/bottle_collection.py).
    "staff.delivery.place_over_returned": {
        "en": "Over-returned by {count}",
        "uz": "{count} ta ortiqcha qaytarilgan",
        "ru": "Возвращено больше на {count}",
    },
    # Fine prompt hint. Sibling of staff.delivery.fine_place_union_hint, and it
    # deliberately reuses that key's `{union}` kwarg name so both branches call
    # i18n.get the same way.
    "staff.delivery.fine_place_over_returned_hint": {
        "en": "This place has over-returned by {union} bottles across all members",
        "uz": "Ushbu manzilda barcha a'zolar bo'yicha {union} ta idish ortiqcha qaytarilgan",
        "ru": "По этому адресу по всем участникам возвращено на {union} тары больше",
    },
    # At-door return prompt (staff_bot/handlers/delivery/status_update.py). The
    # existing zero-balance copy says "no empties are on record", which is
    # factually wrong here: there IS a record and it is negative.
    "staff.delivery.bottles_return_prompt_over_returned": {
        "en": (
            "How many bottles (18.9 L) did the customer return? "
            "This address has already over-returned by {count}."
        ),
        "uz": (
            "Mijoz nechta idish (18,9 l) qaytardi? "
            "Ushbu manzilda allaqachon {count} ta idish ortiqcha qaytarilgan."
        ),
        "ru": (
            "Сколько бутылей (18,9 л) вернул клиент? "
            "По этому адресу уже возвращено на {count} больше."
        ),
    },
    # Standalone-collection receipt. `remaining_balance` is now the PLACE's and
    # is NOT clamped, so a collection can leave the place over-returned.
    "staff.delivery.bottle_collection_recorded_over_returned": {
        "en": "Collected {quantity} bottle(s). This place is now over-returned by {remaining}.",
        "uz": (
            "{quantity} ta idish qabul qilindi. "
            "Endi bu manzilda {remaining} ta idish ortiqcha qaytarilgan."
        ),
        "ru": (
            "Принято тары: {quantity}. "
            "Теперь по этому адресу возвращено на {remaining} больше."
        ),
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
