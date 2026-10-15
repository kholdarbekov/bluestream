"""The admin contract-price API stores two optional prices (spec §3, §6.1)."""

from decimal import Decimal

import pytest

from business_app import db as _db
from business_app.models.corporate import (
    CorporateContractProductPrice,
    CorporatePrepaymentEventType,
    CorporatePrepaymentLedger,
)
from tests.integration.contract_pricing_factory import (
    BANK,
    CASH,
    CATALOGUE,
    admin_headers,
    make_contract,
    make_workplace,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def world(db, sample_product):
    user, _address = make_workplace(db)
    return make_contract(db, user, sample_product)


def _put_prices(app, admin, contract_id, prices):
    return app.test_client().put(
        f"/api/v1/admin/corporate/contracts/{contract_id}/prices",
        json={"prices": prices},
        headers=admin_headers(app, admin),
    )


def _served_prices(app, admin, contract_id):
    resp = app.test_client().get(
        f"/api/v1/admin/corporate/contracts/{contract_id}", headers=admin_headers(app, admin)
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["data"]["contract"]["prices"]


def test_both_prices_round_trip_through_the_admin_api(app, db, admin_user, world, sample_product):
    contract, _row = world
    resp = _put_prices(
        app,
        admin_user,
        contract.id,
        [
            {
                "product_id": sample_product.id,
                "business_account_unit_price": 11000,
                "cash_unit_price": 12500,
                "is_prepayment_eligible": True,
                "is_active": True,
                "notes": "",
            }
        ],
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)

    (price,) = _served_prices(app, admin_user, contract.id)
    assert price["business_account_unit_price"] == 11000.0
    assert price["cash_unit_price"] == 12500.0
    assert price["catalogue_unit_price"] == float(CATALOGUE)
    assert "unit_price" not in price


@pytest.mark.parametrize("bank, cash", [(None, 12500), (11000, None), (None, None)])
def test_an_empty_price_is_stored_empty_not_copied_from_the_catalogue(
    app, db, admin_user, world, sample_product, bank, cash
):
    contract, _row = world
    resp = _put_prices(
        app,
        admin_user,
        contract.id,
        [
            {
                "product_id": sample_product.id,
                "business_account_unit_price": bank,
                "cash_unit_price": cash,
                "is_prepayment_eligible": True,
                "is_active": True,
            }
        ],
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)

    (price,) = _served_prices(app, admin_user, contract.id)
    assert price["business_account_unit_price"] == (None if bank is None else float(bank))
    assert price["cash_unit_price"] == (None if cash is None else float(cash))


def test_the_old_single_price_key_is_refused_not_misread_as_catalogue(
    app, db, admin_user, world, sample_product
):
    """A cached old admin UI still posts `unit_price`. Reading it as 'both prices
    absent' would silently move the customer to the catalogue."""
    contract, row = world
    resp = _put_prices(
        app, admin_user, contract.id, [{"product_id": sample_product.id, "unit_price": 9999, "is_active": True}]
    )
    assert resp.status_code == 400
    assert "unit_price" in resp.get_data(as_text=True)

    _db.session.expire_all()
    stored = CorporateContractProductPrice.query.get(row.id)
    assert (stored.business_account_unit_price, stored.cash_unit_price) == (BANK, CASH)


def test_a_negative_price_is_refused(app, db, admin_user, world, sample_product):
    contract, _row = world
    resp = _put_prices(
        app,
        admin_user,
        contract.id,
        [{"product_id": sample_product.id, "business_account_unit_price": -1, "cash_unit_price": 12500}],
    )
    assert resp.status_code == 400


def _topup(app, admin, contract_id, product_id):
    return app.test_client().post(
        f"/api/v1/admin/corporate/contracts/{contract_id}/prepayments/topup",
        json={"product_id": product_id, "units": 10},
        headers=admin_headers(app, admin),
    )


def test_topup_without_amount_snapshots_the_bank_transfer_price(app, db, admin_user, world, sample_product):
    contract, _row = world
    resp = _topup(app, admin_user, contract.id, sample_product.id)
    assert resp.status_code in (200, 201), resp.get_data(as_text=True)

    entry = CorporatePrepaymentLedger.query.filter_by(
        contract_id=contract.id, event_type=CorporatePrepaymentEventType.TOPUP
    ).one()
    assert Decimal(str(entry.unit_price_snapshot)) == BANK


def test_topup_without_amount_falls_back_to_the_catalogue_when_the_bank_price_is_empty(
    app, db, admin_user, world, sample_product
):
    contract, row = world
    row.business_account_unit_price = None
    _db.session.commit()

    resp = _topup(app, admin_user, contract.id, sample_product.id)
    assert resp.status_code in (200, 201), resp.get_data(as_text=True)

    entry = CorporatePrepaymentLedger.query.filter_by(
        contract_id=contract.id, event_type=CorporatePrepaymentEventType.TOPUP
    ).one()
    assert Decimal(str(entry.unit_price_snapshot)) == CATALOGUE


def test_the_balance_publishes_both_prices_and_the_catalogue(app, db, admin_user, world, sample_product):
    contract, _row = world
    resp = app.test_client().get(
        f"/api/v1/admin/corporate/contracts/{contract.id}/balance", headers=admin_headers(app, admin_user)
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)

    (product,) = resp.get_json()["data"]["balance"]["products"]
    assert product["business_account_unit_price"] == float(BANK)
    assert product["cash_unit_price"] == float(CASH)
    assert product["catalogue_unit_price"] == float(CATALOGUE)
    assert "contract_unit_price" not in product


def test_the_admin_product_payload_publishes_the_catalogue_unit_price(app, db, admin_user, sample_product):
    resp = app.test_client().get(
        "/api/v1/admin/products", query_string={"per_page": 100}, headers=admin_headers(app, admin_user)
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    item = next(p for p in resp.get_json()["data"]["items"] if p["id"] == sample_product.id)
    assert item["catalogue_unit_price"] == float(CATALOGUE)
