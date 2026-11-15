"""Seed the strings the shared-component integration adds (website redesign step 3, integrator "integrate-ui").

Same pattern as `scripts/seed_web_ui_shell_translations.py`: keys under `web.integration.*`, en / ru / uz for every
key, Uzbek Latin with ‘ in o‘/g‘ and ’ for the tutuq belgisi, `Translation.bulk_create_or_update` (idempotent).

- `web.integration.date_year`: a calendar date with its year, for people ("14 November 2026" / "14 ноября 2026" /
  "2026-yil 14-noyabr"). {day} is the day of the month, {month} the month's name from `web.display_labels.month.N`,
  {year} the four-digit year. Used by partials/offer_facts.html for the "Prices as of" line, inside
  <time datetime="YYYY-MM-DD"> so machines keep the ISO date.

Run on the dev database (scripts/ is not mounted in the container, so pipe it in):
    docker compose exec -T business_app python - < scripts/seed_web_ui_integration_translations.py
"""

from business_app import create_app, db
from business_app.models.translation import Translation

CATEGORY = "web"

TRANSLATIONS = {
    "en": {
        "web.integration.date_year": "{day} {month} {year}",
    },
    "ru": {
        "web.integration.date_year": "{day} {month} {year}",
    },
    "uz": {
        "web.integration.date_year": "{year}-yil {day}-{month}",
    },
}

if __name__ == "__main__":
    # Guarded so tests can import TRANSLATIONS without writing to a database.
    app = create_app()
    with app.app_context():
        Translation.bulk_create_or_update(TRANSLATIONS, category=CATEGORY)
        db.session.commit()
        print(f"Seeded {sum(len(v) for v in TRANSLATIONS.values())} rows into category '{CATEGORY}'")
