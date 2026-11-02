"""The cabinet sidebar speaks the customer's language (D24).

The sidebar labels were English literals no seed owned, or `landing.*` keys that existed only in the
dev database. With the seed owning them, a Uzbek or Russian customer sees their own language and
never a raw `landing.` path. The customer's language is their stored preference, as it is for a
signed-in customer whose default-language `?lang=` is dropped from the URL.
"""

import pytest
from bs4 import BeautifulSoup

from business_app.models.translation import Translation
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for
from tests.integration.reorder_reminder_builders import customer_headers

pytestmark = pytest.mark.integration

SIDEBAR = {
    "uz": ["Boshqaruv paneli", "Profil sozlamalari", "Buyurtmalarim", "Manzillarim", "Obunalar", "Aqua Club", "Xavfsizlik", "Chiqish"],
    "ru": ["Панель управления", "Настройки профиля", "Мои заказы", "Мои адреса", "Подписки", "Aqua Club", "Безопасность", "Выйти"],
}


@pytest.fixture(autouse=True)
def seeded(db):
    """Every `landing.*` row exactly as the seeder ships it."""
    for key, values in BACKEND_TRANSLATIONS.items():
        if key.startswith("landing."):
            for language, value in values.items():
                db.session.add(Translation(key=key, language=language, value=value, category=_category_for(key)))
    db.session.commit()


@pytest.mark.parametrize("language", sorted(SIDEBAR))
def test_the_sidebar_is_in_the_customers_language(app, db, sample_user, language):
    sample_user.preferred_language = language
    db.session.commit()

    response = app.test_client().get("/my-orders", headers=customer_headers(app, sample_user), follow_redirects=True)

    assert response.status_code == 200
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    labels = [li.get_text(" ", strip=True) for li in soup.select(".account-menu li")]
    assert labels == SIDEBAR[language]
    assert "landing." not in soup.select_one(".account-menu").get_text()
