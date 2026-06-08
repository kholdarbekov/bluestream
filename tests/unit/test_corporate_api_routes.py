"""Route tests for corporate contract admin/staff API endpoints."""

from types import SimpleNamespace
from unittest.mock import Mock

from flask_jwt_extended import create_access_token

from business_app.models.user import User, UserAddress
from business_app.utils.exceptions import ValidationError
from shared.enums import UserRole, UserType
from business_app.utils.password_security import hash_password


class _ContractStub:
    def __init__(
        self,
        contract_id: int = 11,
        *,
        is_loyalty_points_eligible: bool = False,
        allows_debt: bool = False,
    ):
        self.id = contract_id
        self.contract_number = "CTR-0011"
        self.name = "Corporate 11"
        self.currency = "UZS"
        self.is_loyalty_points_eligible = is_loyalty_points_eligible
        self.allows_debt = allows_debt
        self.prepayment_account = None
        self.product_prices = []

    def to_dict(self):
        return {
            "id": self.id,
            "contract_number": self.contract_number,
            "name": self.name,
            "currency": self.currency,
            "is_loyalty_points_eligible": self.is_loyalty_points_eligible,
            "allows_debt": self.allows_debt,
        }


def _admin_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(
            identity=str(user_id),
            additional_claims={"role": UserRole.ADMIN.value},
        )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _operator_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _create_operator_user(db) -> User:
    operator = User(
        email='operator.api@example.com',
        phone='+998901117733',
        password_hash=hash_password('OperatorPassword123!'),
        first_name='Operator',
        last_name='API',
        user_type=UserType.STAFF,
        role=UserRole.OPERATOR,
        is_verified=True,
    )
    db.session.add(operator)
    db.session.commit()
    return operator


def test_admin_corporate_contract_list_route_delegates_to_service(
    client,
    app,
    admin_user,
    monkeypatch,
):
    stub_contract = _ContractStub(allows_debt=True)
    service = Mock()
    service.list_contracts.return_value = {
        "items": [stub_contract],
        "total": 1,
        "page": 1,
        "per_page": 20,
    }
    service.get_contracts_summary.return_value = {"total": 1, "active": 1, "with_debt": 0}
    monkeypatch.setattr("business_app.api.admin.get_corporate_contract_service", lambda: service)

    response = client.get(
        "/api/v1/admin/corporate/contracts?search=Toleген&page=1&per_page=20",
        headers=_admin_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["data"]["items"][0]["contract_number"] == "CTR-0011"
    assert payload["data"]["items"][0]["is_loyalty_points_eligible"] is False
    assert payload["data"]["items"][0]["allows_debt"] is True
    # Pagination meta now exposes navigability + KPI summary across the whole filter.
    assert payload["meta"]["total"] == 1
    assert payload["meta"]["has_next"] is False
    assert payload["meta"]["summary"] == {"total": 1, "active": 1, "with_debt": 0}
    service.list_contracts.assert_called_once()
    # search term is forwarded to the service, not just filtered client-side.
    assert service.list_contracts.call_args.kwargs.get("search") == "Toleген"
    service.get_contracts_summary.assert_called_once()


def test_admin_create_corporate_contract_route_forwards_loyalty_eligibility(
    client,
    app,
    admin_user,
    monkeypatch,
):
    stub_contract = _ContractStub(contract_id=19, is_loyalty_points_eligible=True)
    service = Mock()
    service.create_contract.return_value = stub_contract
    monkeypatch.setattr("business_app.api.admin.get_corporate_contract_service", lambda: service)

    response = client.post(
        "/api/v1/admin/corporate/contracts",
        headers=_admin_headers(app, admin_user.id),
        json={
            "user_id": 77,
            "contract_number": "CTR-0019",
            "name": "Contract 19",
            "is_loyalty_points_eligible": True,
        },
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["data"]["contract"]["is_loyalty_points_eligible"] is True
    service.create_contract.assert_called_once()
    assert service.create_contract.call_args.args[0]["is_loyalty_points_eligible"] is True


def test_admin_create_corporate_contract_route_forwards_allows_debt(
    client,
    app,
    admin_user,
    monkeypatch,
):
    stub_contract = _ContractStub(contract_id=21, allows_debt=True)
    service = Mock()
    service.create_contract.return_value = stub_contract
    monkeypatch.setattr("business_app.api.admin.get_corporate_contract_service", lambda: service)

    response = client.post(
        "/api/v1/admin/corporate/contracts",
        headers=_admin_headers(app, admin_user.id),
        json={
            "user_id": 77,
            "contract_number": "CTR-0021",
            "name": "Contract 21",
            "allows_debt": True,
        },
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["data"]["contract"]["allows_debt"] is True
    assert service.create_contract.call_args.args[0]["allows_debt"] is True


def test_admin_update_corporate_contract_route_forwards_loyalty_eligibility(
    client,
    app,
    admin_user,
    monkeypatch,
):
    stub_contract = _ContractStub(contract_id=23, is_loyalty_points_eligible=False)
    service = Mock()
    service.update_contract.return_value = stub_contract
    monkeypatch.setattr("business_app.api.admin.get_corporate_contract_service", lambda: service)

    response = client.put(
        "/api/v1/admin/corporate/contracts/23",
        headers=_admin_headers(app, admin_user.id),
        json={"is_loyalty_points_eligible": False},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["data"]["contract"]["is_loyalty_points_eligible"] is False
    service.update_contract.assert_called_once()
    assert service.update_contract.call_args.kwargs["payload"]["is_loyalty_points_eligible"] is False


def test_admin_update_corporate_contract_route_forwards_allows_debt(
    client,
    app,
    admin_user,
    monkeypatch,
):
    stub_contract = _ContractStub(contract_id=24, allows_debt=True)
    service = Mock()
    service.update_contract.return_value = stub_contract
    monkeypatch.setattr("business_app.api.admin.get_corporate_contract_service", lambda: service)

    response = client.put(
        "/api/v1/admin/corporate/contracts/24",
        headers=_admin_headers(app, admin_user.id),
        json={"allows_debt": True},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["data"]["contract"]["allows_debt"] is True
    assert service.update_contract.call_args.kwargs["payload"]["allows_debt"] is True


def test_admin_corporate_topup_route_delegates_to_service(
    client,
    app,
    admin_user,
    monkeypatch,
):
    service = Mock()
    service.topup_contract.return_value = SimpleNamespace(
        to_dict=lambda: {"id": 901, "event_type": "topup", "units": 5.0, "product_id": 44}
    )
    service.get_balance.return_value = {
        "contract_id": 9,
        "currency": "UZS",
        "summary": {
            "tracked_products_count": 1,
            "products_with_reservations_count": 0,
            "products_in_debt_count": 0,
        },
        "products": [
            {
                "product_id": 44,
                "prepaid_units": 20.0,
                "reserved_units": 2.0,
                "consumed_units": 1.0,
                "available_units": 17.0,
                "debt_units": 0.0,
            }
        ],
    }
    monkeypatch.setattr("business_app.api.admin.get_corporate_contract_service", lambda: service)

    response = client.post(
        "/api/v1/admin/corporate/contracts/9/prepayments/topup",
        headers=_admin_headers(app, admin_user.id),
        json={"product_id": 44, "units": 5, "amount": 75000, "transfer_ref": "BANK-REF-9"},
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["data"]["ledger_entry"]["event_type"] == "topup"
    service.topup_contract.assert_called_once()
    assert service.topup_contract.call_args.kwargs["product_id"] == 44
    service.get_balance.assert_called_once_with(9)


def test_admin_corporate_overlap_preview_route_delegates_to_service(
    client,
    app,
    admin_user,
    monkeypatch,
):
    service = Mock()
    service.preview_contract_price_overlaps.return_value = {
        "has_conflicts": True,
        "summary": {
            "conflicts_count": 1,
            "products_count": 1,
            "conflicting_contracts_count": 1,
        },
        "conflicts": [
            {
                "product_id": 55,
                "product_name": "Water 19L",
                "conflicting_contract": {
                    "id": 12,
                    "contract_number": "CTR-0012",
                },
            }
        ],
    }
    monkeypatch.setattr("business_app.api.admin.get_corporate_contract_service", lambda: service)

    response = client.post(
        "/api/v1/admin/corporate/contracts/overlap-preview",
        headers=_admin_headers(app, admin_user.id),
        json={
            "contract_id": 9,
            "user_id": 21,
            "start_date": "2026-03-01T00:00:00+00:00",
            "status": "active",
            "is_active": True,
            "prices": [{"product_id": 55, "is_active": True}],
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["data"]["preview"]["has_conflicts"] is True
    service.preview_contract_price_overlaps.assert_called_once()


def test_admin_create_user_allows_entity_user_type(
    client,
    app,
    db,
    admin_user,
):
    response = client.post(
        "/api/v1/admin/users",
        headers=_admin_headers(app, admin_user.id),
        json={
            "first_name": "Aziza",
            "last_name": "Rahimova",
            "phone": "+998901231199",
            "email": "corp-client@example.com",
            "user_type": "entity",
            "entity_subtype": "workplace",
            "company_name": "ACME WATER LLC",
            "tax_id": "AB-12345",
        },
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["data"]["user"]["user_type"] == "entity"
    assert payload["data"]["user"]["entity_subtype"] == "workplace"
    assert payload["data"]["user"]["company_name"] == "ACME WATER LLC"
    assert payload["data"]["user"]["tax_id"] == "AB-12345"

    created_user = db.session.get(User, payload["data"]["user"]["id"])
    assert created_user is not None
    assert created_user.normalized_user_type == "entity"
    assert created_user.company_name == "ACME WATER LLC"
    assert created_user.tax_id == "AB-12345"


def test_admin_update_user_normalizes_entity_fields(
    client,
    app,
    db,
    admin_user,
    sample_user,
):
    response = client.put(
        f"/api/v1/admin/users/{sample_user.id}",
        headers=_admin_headers(app, admin_user.id),
        json={
            "first_name": "Updated",
            "last_name": "Client",
            "phone": sample_user.phone,
            "email": "updated-client@example.com",
            "user_type": "entity",
            "company_name": "Updated Water LLC",
            "tax_id": "CD-67890",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["data"]["user"]["user_type"] == "entity"
    assert payload["data"]["user"]["company_name"] == "Updated Water LLC"
    assert payload["data"]["user"]["tax_id"] == "CD-67890"

    db.session.refresh(sample_user)
    assert sample_user.normalized_user_type == "entity"
    assert sample_user.company_name == "Updated Water LLC"
    assert sample_user.tax_id == "CD-67890"


def test_admin_create_order_returns_validation_error_for_ambiguous_contract_pricing(
    client,
    app,
    db,
    admin_user,
    sample_user,
    monkeypatch,
):
    address = UserAddress(
        user_id=sample_user.id,
        title="Office",
        full_address="Office Street 1",
        street_address="Office Street 1",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(address)
    db.session.commit()

    def _raise_ambiguous_contract_error(self, user_id, order_data):
        raise ValidationError(
            "Ambiguous contract pricing for product 2. Multiple active contracts match: 1, 2"
        )

    monkeypatch.setattr(
        "business_app.services.order_service.OrderService.create_order",
        _raise_ambiguous_contract_error,
    )

    response = client.post(
        "/api/v1/admin/orders",
        headers=_admin_headers(app, admin_user.id),
        json={
            "user_id": sample_user.id,
            "items": [{"product_id": 2, "quantity": 1}],
            "delivery_address_id": address.id,
            "payment_method": "business_account",
        },
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["success"] is False
    assert "Ambiguous contract pricing" in payload["errors"][0]


def test_staff_operator_corporate_balance_endpoint_handles_missing_contract(
    client,
    app,
    db,
    monkeypatch,
):
    operator = _create_operator_user(db)
    service = Mock()
    service.get_active_contract_balances_for_user.return_value = []
    monkeypatch.setattr("business_app.api.staff.get_corporate_contract_service", lambda: service)

    response = client.get(
        "/api/v1/staff/operator/users/777/corporate-balance",
        headers=_operator_headers(app, operator.id),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["data"]["has_active_contracts"] is False
    assert payload["data"]["contracts"] == []


def test_staff_operator_corporate_balance_endpoint_returns_balance(
    client,
    app,
    db,
    monkeypatch,
):
    operator = _create_operator_user(db)
    contract = _ContractStub(contract_id=44)
    service = Mock()
    service.get_active_contract_balances_for_user.return_value = [
        {
            "contract": {
                "id": contract.id,
                "contract_number": contract.contract_number,
                "name": contract.name,
                "currency": contract.currency,
            },
            "balance": {
                "contract_id": 44,
                "currency": "UZS",
                "summary": {
                    "tracked_products_count": 2,
                    "products_with_reservations_count": 1,
                    "products_in_debt_count": 0,
                },
                "products": [
                    {
                        "product_id": 8,
                        "product_name": "Water 19L",
                        "prepaid_units": 15.0,
                        "reserved_units": 3.0,
                        "consumed_units": 9.0,
                        "available_units": 3.0,
                        "debt_units": 0.0,
                    }
                ],
            },
        }
    ]
    monkeypatch.setattr("business_app.api.staff.get_corporate_contract_service", lambda: service)

    response = client.get(
        "/api/v1/staff/operator/users/321/corporate-balance",
        headers=_operator_headers(app, operator.id),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["data"]["has_active_contracts"] is True
    assert payload["data"]["contracts"][0]["contract"]["id"] == 44
    assert payload["data"]["contracts"][0]["balance"]["summary"]["tracked_products_count"] == 2
    assert payload["data"]["contracts"][0]["balance"]["products"][0]["available_units"] == 3.0
