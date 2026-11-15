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

# Each item is named as its page's own title, so the dashboard reads "My account" (its H1) and the subscriptions item
# "My subscriptions" (cabinet.subs.title) -- never the header's bare "Subscriptions", which opens the public page.
SIDEBAR = {
    "uz": ["Mening hisobim", "Profil sozlamalari", "Buyurtmalarim", "Manzillarim", "Obunalarim", "Aqua Club", "Xavfsizlik", "Chiqish"],
    "ru": ["Мой аккаунт", "Настройки профиля", "Мои заказы", "Мои адреса", "Мои подписки", "Aqua Club", "Безопасность", "Выйти"],
}

# The sidebar's keys outside `landing.*`.
SIDEBAR_KEYS = {"cabinet.subs.title"}


@pytest.fixture(autouse=True)
def seeded(db):
    """Every `landing.*` row, and the sidebar's other keys, exactly as the seeder ships them."""
    for key, values in BACKEND_TRANSLATIONS.items():
        if key.startswith("landing.") or key in SIDEBAR_KEYS:
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
    menu_text = soup.select_one(".account-menu").get_text()
    assert "landing." not in menu_text and "cabinet." not in menu_text
