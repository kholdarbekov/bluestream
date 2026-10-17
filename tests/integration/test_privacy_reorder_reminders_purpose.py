"""The privacy notice names the reorder-reminder purpose (spec §16, §17 item 6; plan Task 24c).

ZRU-547 Art. 19 wants each processing purpose stated. The sentence is seeded copy in uz/ru/en and
renders on /privacy in the reader's language. The tests read the SEEDED values from the seed
script, so the page, the seed and the test cannot drift. The copy awaits counsel review before
REORDER_REMINDERS_MODE=on.
"""

import re

import pytest
from markupsafe import escape

from business_app.utils.translations import set_translation
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for

pytestmark = [pytest.mark.integration, pytest.mark.api]

PURPOSE_KEY = "landing.privacy.reorder_reminders_purpose"
LANGUAGES = ("en", "uz", "ru")
# Amendment 21: each language names "reorder reminders based on your order history".
PURPOSE_WORDS = {
    "en": "reorder reminders based on your order history",
    "uz": "buyurtmalar tarixingizga asoslangan qayta buyurtma eslatmalari",
    "ru": "напоминания о повторном заказе на основе истории ваших заказов",
}
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def test_the_purpose_sentence_is_seeded_in_three_languages_with_one_placeholder_set():
    values = BACKEND_TRANSLATIONS[PURPOSE_KEY]

    for language in LANGUAGES:
        assert PURPOSE_WORDS[language] in values[language].lower(), language
        assert _PLACEHOLDER.findall(values[language]) == [], language
        # Customer copy never tells customers to call (house rule).
        assert "tel:" not in values[language] and "+998" not in values[language], language


def test_each_request_renders_its_own_language_without_clearing_the_template_cache(app, db):
    """`|t` with a per-request language must look the sentence up on every request.

    The first render is ru, so a constant-folded `|t` would bake ru into the compiled template and
    the later en and uz pages would show it. No jinja cache is cleared between the requests.
    """
    seeded = BACKEND_TRANSLATIONS[PURPOSE_KEY]
    for language in LANGUAGES:
        set_translation(PURPOSE_KEY, language, seeded[language], category=_category_for(PURPOSE_KEY))

    for language in ("ru", "en", "uz"):
        # A fresh client per request: the session-scoped `client` can carry another test's `?lang=`
        # session. `?lang=uz` is the default language, which redirects to the bare path; follow it.
        response = app.test_client().get(f"/privacy?lang={language}", follow_redirects=True)

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert 'id="privacy-reorder-reminders"' in html
        # Autoescaped like every `|t` output (the uz copy has apostrophes).
        assert str(escape(seeded[language])) in html, f"/privacy did not render the {language} sentence"
        for other in LANGUAGES:
            if other != language:
                assert str(escape(seeded[other])) not in html, f"the {language} page shows the {other} sentence"
        assert PURPOSE_KEY not in html
