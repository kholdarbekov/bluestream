"""Seed the admin-UI translations for the personal-card-transfer allocation preview.

The preview tells an admin, before they confirm, that a transfer larger than the
target order's outstanding will spill onto the customer's other delivered COD
debts (and only becomes prepaid credit when no debt can absorb it).

Category ``ui`` with fully-dotted keys — the default admin_ui namespace, matching
the sibling ``ui.orders.*`` rows. (This is NOT the ``ui_staff``/bare-key
convention used by ``t('staff:…')``.)

Run (``scripts/`` is not mounted into the container, so pipe via stdin):

    docker compose exec -T business_app python - \
        < scripts/seed_personal_card_preview_translations.py

Then restart/rebuild admin_ui so the cache picks the new rows up.
"""

from __future__ import annotations

from business_app import create_app, db
from business_app.models.translation import Translation

TRANSLATIONS = {
    "en": {
        "ui.orders.personal_card_preview_title": "Where this payment will go",
        "ui.orders.personal_card_preview_loading": "Calculating allocation…",
        "ui.orders.personal_card_preview_this_order": "Applied to this order",
        "ui.orders.personal_card_preview_still_owing": "still owing",
        "ui.orders.personal_card_preview_other_debt": "Applied to debt",
        "ui.orders.personal_card_preview_credit": "Left as customer credit",
        "ui.orders.personal_card_preview_credit_hint": (
            "No outstanding delivered order can absorb this. It stays as prepaid "
            "credit for future orders."
        ),
    },
    "ru": {
        "ui.orders.personal_card_preview_title": "Куда пойдёт этот платёж",
        "ui.orders.personal_card_preview_loading": "Расчёт распределения…",
        "ui.orders.personal_card_preview_this_order": "Зачтено в этот заказ",
        "ui.orders.personal_card_preview_still_owing": "остаток долга",
        "ui.orders.personal_card_preview_other_debt": "Зачтено в долг",
        "ui.orders.personal_card_preview_credit": "Остаётся как аванс клиента",
        "ui.orders.personal_card_preview_credit_hint": (
            "Нет доставленных заказов с задолженностью, чтобы зачесть эту сумму. "
            "Она останется авансом для будущих заказов."
        ),
    },
    "uz": {
        "ui.orders.personal_card_preview_title": "Ushbu to'lov qayerga yo'naltiriladi",
        "ui.orders.personal_card_preview_loading": "Taqsimlash hisoblanmoqda…",
        "ui.orders.personal_card_preview_this_order": "Ushbu buyurtmaga hisoblandi",
        "ui.orders.personal_card_preview_still_owing": "qoldiq qarz",
        "ui.orders.personal_card_preview_other_debt": "Qarzga hisoblandi",
        "ui.orders.personal_card_preview_credit": "Mijoz avansi sifatida qoladi",
        "ui.orders.personal_card_preview_credit_hint": (
            "Bu summani qoplaydigan yetkazilgan qarzdor buyurtma yo'q. U kelgusi "
            "buyurtmalar uchun avans bo'lib qoladi."
        ),
    },
}


def main() -> int:
    app = create_app()
    with app.app_context():
        Translation.bulk_create_or_update(TRANSLATIONS, category="ui")
        db.session.commit()

        seeded = sum(len(rows) for rows in TRANSLATIONS.values())
        print(f"Seeded/updated {seeded} translation rows (category='ui').")

        for language in TRANSLATIONS:
            missing = [
                key
                for key in TRANSLATIONS[language]
                if not Translation.query.filter_by(key=key, language=language).first()
            ]
            status = "OK" if not missing else f"MISSING {missing}"
            print(f"  {language}: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
