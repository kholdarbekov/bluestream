"""The Products page while a price change holds a product (plan 2026-11-16, Task 57) against the real admin routes.

The page lists `catalogue_unit_price`, locks the price field on `price_change_pending` and leaves a refusal to the
toast. Each is driven through `/api/v1/admin/products` with the body the edit modal sends (the row it was given, echoed
back), on a product held by a SCHEDULED change scheduled through the real route.
"""

import pathlib

import pytest

from tests.integration.price_change_builders import schedule_change
from tests.unit.test_lapsed_customers_ui_contract import _declared_key_set

pytestmark = pytest.mark.integration

PAGE_TEST = (
    pathlib.Path(__file__).resolve().parents[2]
    / "admin_ui"
    / "src"
    / "__tests__"
    / "pages"
    / "Products.priceChangeLock.test.js"
)
PRODUCTS = "/api/v1/admin/products"


def _row(client, headers, product_id):
    response = client.get(PRODUCTS, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return next(item for item in response.get_json()["data"]["items"] if item["id"] == product_id)


@pytest.fixture
def held(db, client, admin_claim_headers, sample_product):
    change = schedule_change(client, admin_claim_headers, sample_product)
    return sample_product, change


def test_every_row_publishes_the_charged_price_and_the_pending_change(client, held, admin_claim_headers):
    product, change = held

    row = _row(client, admin_claim_headers, product.id)

    assert row["catalogue_unit_price"] == float(product.catalogue_unit_price)
    assert row["price"] == float(product.base_price)
    assert row["price_change_pending"] == {
        "price_change_id": change.id,
        "change_on": change.change_on.isoformat(),
    }


def test_the_edit_modal_echo_is_accepted_while_locked(client, held, admin_claim_headers):
    product, _ = held
    row = _row(client, admin_claim_headers, product.id)
    row["name"] = "Renamed while held"

    response = client.put(f"{PRODUCTS}/{product.id}", json=row, headers=admin_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _row(client, admin_claim_headers, product.id)["name"] == "Renamed while held"


def test_a_changed_price_is_refused_with_the_code_the_page_leaves_to_the_toast(
    client, held, admin_claim_headers
):
    product, _ = held
    row = _row(client, admin_claim_headers, product.id)
    row["price"] = row["price"] + 1000

    response = client.put(f"{PRODUCTS}/{product.id}", json=row, headers=admin_claim_headers)

    assert response.status_code == 400
    assert response.get_json()["data"]["error_code"] == "PRODUCT_PRICE_LOCKED_BY_PRICE_CHANGE"


def test_the_fixtures_mirror_the_serializer(client, held, admin_claim_headers):
    product, _ = held

    row = _row(client, admin_claim_headers, product.id)

    declared = _declared_key_set(PAGE_TEST, "PRODUCT_ROW_KEYS")
    assert declared <= set(row), sorted(declared - set(row))
