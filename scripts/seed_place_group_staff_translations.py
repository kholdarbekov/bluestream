# scripts/seed_place_group_staff_translations.py
"""Seed the Phase-2c place-group / linked-account STAFF-BOT keys.

category='staff_bot' with DOTTED `staff.*` keys — the namespace
scripts/seed_staff_translations.py owns. This is a standalone ADDITIVE script,
not an edit of that generator: the generator resolves any key it does not
recognise to a humanised fallback (`_humanize_key`), so curating these eight
strings here keeps them out of its guesswork. None of the eight appears in
that file (asserted by tests/unit/test_place_group_translation_seeds.py), so
the two seeds never fight over the same row.

Deploy ordering matters here more than anywhere else in the plan: staff_bot's
/health enumerates every literal `staff.*` key used under staff_bot/ and
returns 503 while any of them is missing from the catalog
(staff_bot/i18n.py:180-200, staff_bot/webhook_server.py:170-195). Seed BEFORE
or WITH the staff_bot deploy, never after.

Run inside the business_app container (scripts/ is NOT volume-mounted, so pipe
the file in over stdin — piped stdin still executes as __main__):
    docker compose exec -T business_app python - < scripts/seed_place_group_staff_translations.py
Then restart the bot (the translation cache is per-process):
    docker compose restart staff_bot
"""

from business_app import create_app
from business_app.models.translation import Translation

CATEGORY = "staff_bot"

# {key: {language: value}}. These are LABELS: the caller appends the amount,
# the count and the (escaped) place label, so none of them interpolates —
# except fine_place_union_hint, which is a full sentence.
KEYS = {
    # Order card + at-door cash prompt (staff_bot/utils/formatters.py).
    "staff.delivery.place_cod_total": {
        "en": "Workplace COD debt",
        "uz": "Ish joyi naqd qarzi",
        "ru": "Долг наличными по адресу",
    },
    # NB: `staff.delivery.place_statement_title` and `staff.delivery.place_members`
    # used to live here, for the place-statement SCREEN. Owner ruling A7 deleted
    # that screen ("the debtors list only shows the users"), so no staff_bot
    # handler reads them any more and a key seeded but never read is exactly what
    # tests/unit/test_place_group_translation_seeds.py forbids. Rows already
    # seeded in an environment are harmless leftovers — this seeder is additive
    # and never deletes.
    # Customer statement: cluster-wide line + its member count.
    "staff.delivery.cluster_debt_total": {
        "en": "Total across linked accounts",
        "uz": "Bog'langan hisoblar bo'yicha jami",
        "ru": "Итого по связанным аккаунтам",
    },
    "staff.delivery.cluster_members": {
        "en": "accounts",
        "uz": "hisoblar",
        "ru": "аккаунтов",
    },
    # 🔴 THE HEADLINE ON THE DRIVER'S COD STATEMENT SCREEN — the figure
    # `CashCollectionHandler._collect_offer` will actually offer at "💸 Collect
    # full". It replaces `staff.delivery.total_outstanding`, which labelled the
    # RAW per-account, PENDING-inclusive engine figure: on the canonical A6 rows
    # that screen read "Total outstanding: 25 000" (95 000 with one pending
    # order) and then offered 45 000. The label must NOT be a synonym of "total
    # outstanding" in any language — its whole job is to say *collectible now*,
    # so a driver reading a bigger number elsewhere on the screen knows which one
    # the flow acts on.
    #
    # ⚠️ THE EN VALUE MUST NOT BE "Collectible now". That string is byte-identical
    # to what BOTH bots print for a MISSING row (the fallback humanises the last
    # key segment: underscores to spaces, capitalised), so a seeded EN row would
    # be indistinguishable from an unseeded one. This seeder's whole job is to
    # overwrite the generator's humanised junk, and it cannot be shown to have
    # done so for a value equal to that junk —
    # `test_the_curated_place_seeds_repair_the_generators_damage_in_all_three_languages`
    # fails on exactly that collision. "Can collect now" also mirrors the uz/ru
    # phrasing, which are both "it is possible to collect now" rather than an
    # adjective.
    "staff.delivery.collectible_now": {
        "en": "Can collect now",
        "uz": "Hozir yig'ish mumkin",
        "ru": "Можно собрать сейчас",
    },
    # Disambiguates the cluster-wide debt COUNT from this one account's, which
    # the driver otherwise has to infer from an empty item list.
    "staff.delivery.account_cod_debts": {
        "en": "This account's debts",
        "uz": "Shu hisob qarzlari",
        "ru": "Долги этого аккаунта",
    },
    # Bottle-fine prompt: a place holds ONE pool, so the driver fines against the
    # place's balance, never one account's slice (spec §8). "Union" is dead
    # domain vocabulary — there is nothing left to union — but the `{union}`
    # KWARG NAME stays: staff_bot/handlers/delivery/bottle_collection.py:729 and
    # its over-returned sibling both pass `union=`, and a rename would make
    # `str.format` raise, which staff_bot/i18n.py:118-121 catches and turns into
    # the RAW template — a literal "{union}" on the driver's screen.
    "staff.delivery.fine_place_union_hint": {
        "en": "This place holds {union} bottles across all members",
        "uz": "Ushbu joyda barcha a'zolar bo'yicha {union} ta idish bor",
        "ru": "В этом месте по всем участникам {union} ед. тары",
    },
    # COD cap, PLACE arm (staff_bot/handlers/operator/create_order.py). The
    # operator reads this out on the phone, so it must name the workplace as
    # the cause — but spec §7 lets only a COUNT cross, never a coworker's name
    # or phone. Register matches the sibling staff.operator.cod_restricted.
    "staff.operator.cod_restricted_place": {
        "en": (
            "Cash on delivery is unavailable: the workplace at this delivery address has "
            "{place_active_cod_debt_count} outstanding COD debts. Offer a card payment method."
        ),
        "uz": (
            "Yetkazib berishda naqd to'lash mavjud emas: ushbu yetkazib berish manzilidagi "
            "ish joyida {place_active_cod_debt_count} ta to'lanmagan naqd qarz bor. "
            "Karta to'lovini taklif qiling."
        ),
        "ru": (
            "Оплата наличными при доставке недоступна: по рабочему адресу доставки есть "
            "{place_active_cod_debt_count} непогашенных задолженностей. "
            "Предложите оплату картой."
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
