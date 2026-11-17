"""The website's order pages print a courtesy line as the split the API publishes (final fix wave, B1).

A courtesy line is charged `2 x 18,000 + 2 x 20,000`, net of a discount. The order pages drew `quantity x unit_price`
("4 x 20 000") beside the net line total, so the visitor saw a line that did not multiply out. Every page now words the
line from the published fields only (`courtesy_quantity`, `courtesy_unit_price`, `regular_quantity`, `unit_price`).

The two server-rendered pages are driven here through their routes with the customer's own token, on a courtesy order
placed through the bot's real order body. The two scripts (orders.js, order-detail-modal.js) are covered by
admin_ui/src/__tests__/web/orderPagesCourtesy.test.js.
"""

import pytest
from bs4 import BeautifulSoup

from business_app.models.translation import Translation
from business_app.utils import display_labels
from scripts.seed_web_ui_cabinet_translations import TRANSLATIONS as CABINET
from scripts.seed_web_ui_shop_translations import TRANSLATIONS as SHOP
from tests.integration.price_courtesy_builders import NEW_PRICE, OLD_PRICE, courtesy_world, water
from tests.integration.test_price_courtesy_order_create import _order, _place

pytestmark = pytest.mark.integration

PAGES = ("/order-confirmation?order_id={}&lang=en", "/order-tracking?order_id={}&lang=en")
KEYS = ("web.shop.cart.courtesy_part", "web.cabinet.orders.qty_price")


@pytest.fixture
def client(app):
    """A fresh client per test: the session-scoped one carries JWT cookies from test to test."""
    return app.test_client()


@pytest.fixture
def w(app, db, sample_product, admin_user):
    return courtesy_world(app, db, water(db, sample_product), admin_user)


@pytest.fixture
def words(app, db):
    """The two keys the line is worded from, as the seeds ship them, with the templates compiled afresh."""
    for group in (SHOP, CABINET):
        for key in KEYS:
            if key in group["en"]:
                db.session.add(Translation(key=key, language="en", value=group["en"][key], category="web", is_active=True))
    db.session.commit()
    app.jinja_env.cache.clear()
    yield
    app.jinja_env.cache.clear()


@pytest.mark.parametrize("url", PAGES, ids=["confirmation", "tracking"])
def test_an_order_page_prints_the_courtesy_split_not_quantity_times_price(client, db, w, words, url):
    order = _order(db, _place(client, w.customer, w.product))

    response = client.get(url.format(order.id), headers=w.customer.headers)

    assert response.status_code == 200, response.get_data(as_text=True)[:400]
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    (line,) = soup.select(".ae-order-lines .ae-co-line")
    meta = line.select_one(".ae-co-line__qty").get_text(" ", strip=True)
    kept, new = display_labels.money(OLD_PRICE), display_labels.money(NEW_PRICE)
    assert meta == f"2 × {kept} (your price, kept for you) + 2 × {new}", meta
    assert f"4 × {new}" not in soup.get_text(" ", strip=True)
