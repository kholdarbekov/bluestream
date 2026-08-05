# scripts/seed_ui_bottle_tracking_linked_accounts.py
"""Seed the `bottle_tracking` namespace keys for the PLACE-detail drawer.

Bottle balances are keyed on the PLACE (the address group when one exists, else
the address), so the drawer shows one member list, ONE pool and one place-scoped
ledger. Five keys that encoded the old people-keyed model were deleted with their
call sites: `this_account_only_label`, `combined_cluster_balance_label`,
`combined_at_place_label`, `cluster_ledger_heading` and
`fine_place_union_balance_label` (a place holds one pool, so `balance_label` IS
the union — there is no second number to label).

category='ui_bottle_tracking', BARE keys (see AdminUiTranslationService), English
values MUST match the inline t(key, { defaultValue: '...' }) fallbacks in
admin_ui/src/pages/BottleTracking.js. bulk_create_or_update upserts, so re-running
is safe.

DO NOT add a generic bare key here that another ui_* seed already owns.
`translations` is unique on (key, language) ONLY and bulk_create_or_update
REASSIGNS `category`, so a bare key seeded from two scripts is ONE row with two
claimed owners: whichever runs last wins and the loser's namespace bundle silently
drops it. `phone` used to be seeded here and in scripts/seed_ui_tryouts_translations.py
(identical trilingual values) — it now lives only in the latter. BottleTracking.js
still resolves `t('phone', …)` because i18n.js sets fallbackNS: ['common'] and the
`common` bundle is the union of every ui_* category (AdminUiTranslationService.
get_translations).

Run inside the business_app container (scripts/ is not mounted, so pipe it in):
    docker compose exec -T business_app python - < scripts/seed_ui_bottle_tracking_linked_accounts.py
Then restart business_app so the translations API serves them:
    docker compose restart business_app
"""
from business_app import create_app
from business_app.models.translation import Translation

CATEGORY = "ui_bottle_tracking"

# key: (en, uz, ru)   — en MUST match the BottleTracking.js defaultValue exactly
KEYS = {
    "details_button":                 ("Details", "Batafsil", "Подробнее"),
    "customer_detail_title":          ("Customer Bottle Detail", "Mijoz idishlari tafsiloti", "Детали тары клиента"),
    # NOTE: no "phone" here — scripts/seed_ui_tryouts_translations.py owns that
    # bare key (see the module docstring).
    # NOT "linked accounts" any more. The alert is triggered by
    # `placeDetailTarget.is_shared_place` (BottleTracking.js:1335), which is TRUE
    # for any shared place — two coworkers at one office whose accounts were
    # never linked included. Calling that "Linked accounts detected" re-created
    # the exact people-vs-place conflation this plan exists to kill, so the copy
    # names the PLACE, never the relationship between the accounts.
    "linked_accounts_alert_title":    ("Shared place", "Umumiy joy", "Общее место"),
    "linked_member_count_label": (
        "Shared place — one pool across {{count}} accounts",
        "Umumiy joy — {{count}} ta hisob uchun yagona balans",
        "Общее место — один пул на {{count}} аккаунтов",
    ),
    "addresses_heading":              ("Addresses", "Manzillar", "Адреса"),
    "grouped_tag":                    ("grouped", "guruhlangan", "сгруппировано"),
    "balance_label":                  ("Balance", "Balans", "Баланс"),
    "member":                         ("Member", "A'zo", "Участник"),
    # Place-detail drawer (BottleTracking.js:1330, :1352, :1372). The drawer is a
    # PLACE now: one member list, ONE pool, one ledger — no per-account slice and
    # no cluster-wide roll-up, so the three keys those used to need are gone.
    "members":                        ("Members", "A'zolar", "Участники"),
    "place_balance_label":            ("Bottles at this place", "Ushbu joydagi idishlar", "Тара в этом месте"),
    "place_ledger_heading":           ("Place Ledger", "Joy reestri", "Журнал места"),
}


def run():
    app = create_app()
    with app.app_context():
        data = {"en": {}, "uz": {}, "ru": {}}
        for key, (en, uz, ru) in KEYS.items():
            data["en"][key] = en
            data["uz"][key] = uz
            data["ru"][key] = ru
        Translation.bulk_create_or_update(data, category=CATEGORY)
        print(f"Seeded {len(KEYS)} keys x3 langs into category '{CATEGORY}'.")


if __name__ == "__main__":
    run()
