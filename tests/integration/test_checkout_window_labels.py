"""The checkout's delivery windows are translated words; the times come from checkout.js (D25, §11)."""

import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from business_app.models.translation import Translation
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for
from tests.integration.contract_pricing_factory import customer_headers

pytestmark = pytest.mark.integration

WINDOW_KEYS = ["window_morning", "window_afternoon", "window_evening"]
EXPECTED = {
    "en": {"window_morning": "Morning", "window_afternoon": "Afternoon", "window_evening": "Evening"},
    "uz": {"window_morning": "Ertalab", "window_afternoon": "Kunduzi", "window_evening": "Kechqurun"},
    "ru": {"window_morning": "Утро", "window_afternoon": "День", "window_evening": "Вечер"},
}
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def seeded(db):
    keys = ["api.subscriptions.time.any_time"] + [f"web.checkout.{k}" for k in WINDOW_KEYS]
    for key in keys:
        for language, value in BACKEND_TRANSLATIONS[key].items():
            db.session.add(Translation(key=key, language=language, value=value, category=_category_for(key)))
    db.session.commit()


@pytest.mark.parametrize("language", ["en", "uz", "ru"])
def test_the_checkout_windows_are_translated(app, db, sample_user, language):
    sample_user.preferred_language = language
    db.session.commit()
    app.jinja_env.cache.clear()
    resp = app.test_client().get(f"/checkout?lang={language}", headers=customer_headers(app, sample_user), follow_redirects=True)
    assert resp.status_code == 200
    island = json.loads(BeautifulSoup(resp.get_data(as_text=True), "html.parser").select_one("script#page-data").string)
    i18n = island["i18n"]

    for key in WINDOW_KEYS:
        assert i18n[key] == EXPECTED[language][key]
    assert i18n["window_anytime"] == BACKEND_TRANSLATIONS["api.subscriptions.time.any_time"][language]


def test_checkout_js_adds_the_times_from_its_own_window_table():
    source = (ROOT / "business_app/static/js/pages/checkout.js").read_text("utf-8")
    assert "preset.label + ' (' + preset.start + '–' + preset.end + ')'" in source
