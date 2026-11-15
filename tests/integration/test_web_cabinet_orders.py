"""My orders, the order modal and My subscriptions after the redesign (website redesign step 3, group "cabinet", unit
"cabinet-orders", 2026-11-14).

Everything is driven the way the customer's browser drives it: the pages are fetched with the storefront login
cookie, and the list is read with exactly the query orders.js builds (`apiRequest('/orders?' + URLSearchParams(...))`:
`page`, `per_page`, and the two filters GET /api/v1/orders honours, `status` and the period as `start_date`).

- Owner decision 4: "My orders: pagination renders; filters work (or are removed if they cannot work honestly)". The
  pages after the tenth order are reachable; every status and every period a customer can pick in the page's own
  selects returns exactly those orders; the search box (the endpoint has no search) and the Filter button are gone.
- An order card names its address with the one rule the subscription page uses (`delivery_address_label`).
- The pages carry the hooks their scripts need, in uz, ru and en, with no raw `web.cabinet.` key.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

import pytest

from business_app.models.order import Order
from business_app.models.user import UserAddress
from scripts.seed_web_ui_cabinet_translations import TRANSLATIONS as CABINET
from shared.enums import OrderStatus, PaymentMethod
from tests.integration.test_web_cabinet_frame import _browser, _get, _island, cabinet_copy  # noqa: F401  (fixture)
from tests.integration.test_web_password_reset_and_change import WEB_HEADERS

pytestmark = pytest.mark.integration

LANGS = ("uz", "ru", "en")
REPO = Path(__file__).resolve().parents[2]
ORDERS_JS = (REPO / "business_app" / "static" / "js" / "pages" / "orders.js").read_text("utf-8")
# orders.js loadOrders(): URLSearchParams(Object.assign({ page: page, per_page: N }, currentFilters)).
PER_PAGE = int(re.search(r"\{ page: page, per_page: (\d+) \}", ORDERS_JS).group(1))


def _js_query(page, **filters):
    """The query string orders.js sends: page, per_page, then the filters in the order applyFilters() sets them."""
    params = {"page": page, "per_page": PER_PAGE}
    for name in ("status", "start_date"):
        if filters.get(name):
            params[name] = filters[name]
    return urlencode(params)


# The exact queries orders.js sends for each period at one instant, recorded once and shared with the jsdom test
# (admin_ui web/orderSurfaces.test.js asserts the shipped script sends these strings): the endpoint side sends the same
# strings, so no copy of the script's period rule lives here (review CAB-R20).
PERIODS = json.loads((REPO / "tests" / "fixtures" / "web_cabinet_orders" / "period_queries.json").read_text("utf-8"))
PERIOD_NOW = datetime.fromisoformat(PERIODS["now"].replace("Z", "+00:00"))


def _list(app, user, query):
    """GET /api/v1/orders the way apiRequest sends it (no trailing slash: the app redirects to the route, 308)."""
    response = _browser(app, user).get(f"/api/v1/orders?{query}", headers=WEB_HEADERS, follow_redirects=True)
    body = response.get_json()
    assert response.status_code == 200, body
    return body["data"]


def _order(db, user, number, *, status=OrderStatus.DELIVERED, created_at=None, address=None):
    order = Order(
        user_id=user.id,
        order_number=number,
        status=status,
        payment_method=PaymentMethod.CASH,
        subtotal=Decimal("19000.00"),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("19000.00"),
        created_at=created_at or datetime.now(timezone.utc),
        delivery_address_id=address.id if address else None,
    )
    db.session.add(order)
    return order


def _numbers(data):
    return [order["order_number"] for order in data["orders"]]


# --- pagination -------------------------------------------------------------------------------------------------------


def test_every_order_after_the_tenth_is_reachable_through_the_pager_orders_js_reads(app, db, sample_user):
    start = datetime.now(timezone.utc) - timedelta(days=2)
    for n in range(23):
        _order(db, sample_user, f"PG_{n:06d}_26", created_at=start + timedelta(minutes=n))
    db.session.commit()

    first = _list(app, sample_user, _js_query(1))
    last = _list(app, sample_user, _js_query(3))

    # The names orders.js updatePagination() reads: page, pages, per_page, total.
    assert first["pagination"] == {
        "page": 1, "pages": 3, "per_page": PER_PAGE, "total": 23, "has_next": True, "has_prev": False,
    }
    assert last["pagination"]["page"] == 3 and last["pagination"]["pages"] == 3
    assert _numbers(first) == [f"PG_{n:06d}_26" for n in range(22, 12, -1)]  # the newest ten, newest first
    assert _numbers(last) == ["PG_000002_26", "PG_000001_26", "PG_000000_26"]  # the oldest three
    for name in ("pagination.pages", "pagination.page", "pagination.total", "pagination.per_page"):
        assert name in ORDERS_JS, f"orders.js no longer reads {name}"


# --- the filters a customer can pick ---------------------------------------------------------------------------------


def _options(soup, select_id):
    return [option["value"] for option in soup.select(f"#{select_id} option")]


def test_every_status_the_page_offers_lists_exactly_the_orders_in_that_status(app, db, sample_user, cabinet_copy):
    start = datetime.now(timezone.utc) - timedelta(days=3)
    expected = {}
    for n, status in enumerate(OrderStatus):
        for copy in range(2 if status is OrderStatus.DELIVERED else 1):
            number = f"ST_{n}{copy}_26"
            _order(db, sample_user, number, status=status, created_at=start + timedelta(minutes=10 * n + copy))
            expected.setdefault(status.value, []).append(number)
    db.session.commit()

    soup, _ = _get(app, db, sample_user, "/my-orders", "ru")
    offered = _options(soup, "statusFilter")

    assert offered == [""] + [status.value for status in OrderStatus]
    for value in offered[1:]:
        data = _list(app, sample_user, _js_query(1, status=value))
        assert sorted(_numbers(data)) == sorted(expected[value]), value
        assert {order["status"] for order in data["orders"]} == {value}
        assert data["pagination"]["total"] == len(expected[value])
    # "All statuses" sends no status at all.
    assert _list(app, sample_user, _js_query(1))["pagination"]["total"] == sum(map(len, expected.values()))


def test_every_period_the_page_offers_sends_a_start_date_the_endpoint_honours(app, db, sample_user, cabinet_copy):
    """Orders aged from the recorded instant; each period's query is the one orders.js sent at that instant."""
    ages = {"AGE_3": 3, "AGE_20": 20, "AGE_60": 60, "AGE_200": 200, "AGE_400": 400}
    for number, days in ages.items():
        _order(db, sample_user, number, created_at=PERIOD_NOW - timedelta(days=days))
    db.session.commit()

    soup, _ = _get(app, db, sample_user, "/my-orders", "uz")
    offered = _options(soup, "dateFilter")

    assert offered == ["", "7", "30", "90", "365"]
    assert sorted(PERIODS["periods"]) == sorted(offered[1:])  # every option has its recorded query
    for days in offered[1:]:
        data = _list(app, sample_user, PERIODS["periods"][days])
        assert sorted(_numbers(data)) == sorted(n for n, age in ages.items() if age < int(days)), days
    assert _list(app, sample_user, _js_query(1))["pagination"]["total"] == len(ages)


def test_status_and_period_together_and_the_filtered_empty_answer(app, db, sample_user):
    now = PERIOD_NOW
    _order(db, sample_user, "BOTH_NEW_DELIVERED", status=OrderStatus.DELIVERED, created_at=now - timedelta(days=2))
    _order(db, sample_user, "BOTH_OLD_DELIVERED", status=OrderStatus.DELIVERED, created_at=now - timedelta(days=50))
    _order(db, sample_user, "BOTH_NEW_CANCELLED", status=OrderStatus.CANCELLED, created_at=now - timedelta(days=1))
    db.session.commit()

    both = _list(app, sample_user, PERIODS["status_and_period"]["query"])
    none = _list(app, sample_user, PERIODS["status_and_period_empty"]["query"])

    assert _numbers(both) == ["BOTH_NEW_DELIVERED"]
    # What orders.js turns into the "No orders match these filters" state with its reset link, and no pager.
    assert none["orders"] == []
    assert none["pagination"]["total"] == 0 and none["pagination"]["pages"] == 0


# --- the card's address -------------------------------------------------------------------------------------------------


def test_a_card_names_the_address_by_its_title_else_its_street_line(app, db, sample_user):
    titled = UserAddress(user_id=sample_user.id, title="Офис", full_address="Toshkent, Chilonzor 7, 12")
    untitled = UserAddress(user_id=sample_user.id, title=None, full_address="Toshkent, Yunusobod 4, 8")
    db.session.add_all([titled, untitled])
    db.session.flush()
    now = datetime.now(timezone.utc)
    _order(db, sample_user, "ADDR_TITLED", created_at=now - timedelta(minutes=2), address=titled)
    _order(db, sample_user, "ADDR_UNTITLED", created_at=now - timedelta(minutes=1), address=untitled)
    _order(db, sample_user, "ADDR_NONE", created_at=now)
    db.session.commit()

    labels = {o["order_number"]: o.get("delivery_address_label") for o in _list(app, sample_user, _js_query(1))["orders"]}

    assert labels == {"ADDR_TITLED": "Офис", "ADDR_UNTITLED": "Toshkent, Yunusobod 4, 8", "ADDR_NONE": None}
    assert "order.delivery_address_label" in ORDERS_JS


# --- the pages ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_my_orders_page_has_its_hooks_and_no_raw_keys(app, db, sample_user, cabinet_copy, lang):
    soup, html = _get(app, db, sample_user, "/my-orders", lang)

    assert "web.cabinet." not in html
    for hook in ("#ordersList", "#loadingOrders", "#emptyState", "#paginationSection", "#ordersPagination",
                 "#ordersPageInfo", "#statusFilter", "#dateFilter"):
        assert soup.select_one(hook) is not None, hook
    assert [b["data-empty"] for b in soup.select("#emptyState [data-empty]")] == ["first", "filtered", "failed"]
    # Native selects (script.js turns every select without `ignore` into a nice-select).
    assert "ignore" in soup.select_one("#statusFilter")["class"] and "ignore" in soup.select_one("#dateFilter")["class"]
    # The status options read in the page's language: the seeded words, not the enum.
    labels = {o["value"]: o.get_text(strip=True) for o in soup.select("#statusFilter option") if o["value"]}
    assert labels == {code: CABINET[lang][f"web.cabinet.status.{code}"] for code in labels}
    # Gone: the search box (GET /orders has no search) and the redundant Filter button.
    assert soup.select_one("#searchOrder") is None and soup.select_one('[data-action="apply-filters"]') is None
    # The island hands orders.js every status its pills can show, in words.
    island = _island(soup, "page-data")
    assert set(island["i18n"]["status_labels"]) == set(island["status_tones"])
    assert all(island["i18n"]["status_labels"].values())
    # The shared order modal, skinned, after the base scripts; Leaflet is not linked up front (map-helper.js loads the
    # vendored copy on the first map, from the URLs the modal's island publishes) and never comes from a CDN.
    modal = soup.select_one("#orderDetailModal")
    assert modal is not None and "ae-modal" in modal["class"]
    modal_words = _island(soup, "order-detail-modal-data")["i18n"]
    assert modal_words["no_address"] == CABINET[lang]["web.cabinet.modal.no_address"]
    assert modal_words["details_failed"] == CABINET[lang]["web.cabinet.modal.load_failed"]
    assert not soup.select('link[href*="leaflet"]') and not soup.select('script[src*="leaflet"]')
    assert "unpkg.com" not in html
    assert re.search(r'src="/static/js/pages/orders\.js\?v=\d+"', html)


@pytest.mark.parametrize("lang", LANGS)
def test_my_subscriptions_page_has_its_shell_and_no_raw_keys(app, db, sample_user, cabinet_copy, lang):
    soup, html = _get(app, db, sample_user, "/my-subscriptions", lang)

    assert "web.cabinet." not in html
    assert soup.select_one("div.subs#subs") is not None
    assert soup.select_one("section#subscription") is not None and soup.select_one("ul#subs-strip") is not None
    island = _island(soup, "page-data")
    assert island["empty_perks"] == [
        CABINET[lang][f"web.cabinet.subs.perk_{name}"] for name in ("auto", "skip", "change")
    ]
    # The empty state's action opens the builder on the home page.
    assert island["builder_url"].endswith("#subscription-constructor")
    assert re.search(r'href="/static/css/pages/my-subscriptions\.css\?v=\d+"', html)
