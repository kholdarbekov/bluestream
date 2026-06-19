"""Route-level regressions for loyalty API delegation."""

from unittest.mock import Mock

from flask_jwt_extended import create_access_token

from business_app.utils.exceptions import NotFoundError, ValidationError


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=user_id, additional_claims={'role': 'admin'})
    return {'Authorization': f'Bearer {token}'}


def test_get_loyalty_points_route_delegates_to_service(client, app, sample_user, monkeypatch):
    service = Mock()
    service.get_points_summary_for_user.return_value = {
        'points_balance': 120,
        'lifetime_points': 500,
        'tier': 'Bronze',
        'next_tier_threshold': 200,
    }
    monkeypatch.setattr('business_app.api.loyalty.get_loyalty_service', lambda: service)

    response = client.get('/api/v1/loyalty/points', headers=_auth_headers(app, sample_user.id))

    assert response.status_code == 200
    service.get_points_summary_for_user.assert_called_once()
    assert int(service.get_points_summary_for_user.call_args.args[0]) == sample_user.id


def test_get_points_history_route_maps_validation_error(client, app, sample_user, monkeypatch):
    service = Mock()
    service.get_filtered_points_history_for_user.side_effect = ValidationError('Invalid transaction type')
    monkeypatch.setattr('business_app.api.loyalty.get_loyalty_service', lambda: service)

    response = client.get(
        '/api/v1/loyalty/points/history?type=bad-type',
        headers=_auth_headers(app, sample_user.id),
    )

    assert response.status_code == 400


def test_gift_points_route_maps_not_found(client, app, sample_user, monkeypatch):
    service = Mock()
    service.gift_points_by_phone.side_effect = NotFoundError('Recipient not found')
    monkeypatch.setattr('business_app.api.loyalty.get_loyalty_service', lambda: service)

    response = client.post(
        '/api/v1/loyalty/gift-points',
        headers=_auth_headers(app, sample_user.id),
        json={'recipient_phone': '+998900000000', 'points_amount': 10},
    )

    assert response.status_code == 404


def test_get_rewards_route_maps_can_redeem_from_service(client, app, sample_user, monkeypatch):
    reward = Mock()
    reward.id = 7
    reward.points_cost = 10
    service = Mock()
    service.get_rewards_for_user.return_value = {
        'rewards': [reward],
        'can_redeem_by_id': {7: True},
        'user_points_balance': 100,
        'categories': [],
    }
    monkeypatch.setattr('business_app.api.loyalty.get_loyalty_service', lambda: service)
    monkeypatch.setattr(
        'business_app.api.loyalty.serialize_loyalty_reward',
        lambda reward, language=None: {'id': reward.id, 'points_cost': reward.points_cost},
    )

    response = client.get('/api/v1/loyalty/rewards', headers=_auth_headers(app, sample_user.id))

    assert response.status_code == 200
    body = response.get_json()
    data = body.get('data', body)  # tolerate success_response envelope
    assert data['rewards'][0]['can_redeem'] is True
    assert data['rewards'][0]['points_needed'] == 0  # max(0, 10 - 100)


def test_admin_create_free_product_reward_with_quantity(client, app, admin_token, db, sample_category):
    from decimal import Decimal
    from business_app import db as _db
    from business_app.models.loyalty import LoyaltyProgram, LoyaltyReward
    from business_app.models.product import Product, ProductSizeEnum
    program = LoyaltyProgram(name="DefaultP", is_active=True, is_default=True, uzs_per_point=250)
    product = Product(name="Bottle", base_price=Decimal("8000"), category_id=sample_category.id,
                      size=ProductSizeEnum.SIZE_19L, is_active=True)
    _db.session.add_all([program, product]); _db.session.commit()

    resp = client.post(
        "/api/v1/admin/loyalty/rewards",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"program_id": program.id, "name": "Free 2 bottles", "reward_type": "free_product",
              "points_cost": 200, "free_product_id": product.id, "free_product_quantity": 2},
    )
    assert resp.status_code in (200, 201), resp.get_data(as_text=True)
    r = LoyaltyReward.query.filter_by(name="Free 2 bottles").first()
    assert r is not None and r.free_product_quantity == 2


def test_admin_create_reward_rejects_dead_types(client, app, admin_token, db):
    """voucher and free_delivery are removed reward types; create must reject them."""
    from business_app import db as _db
    from business_app.models.loyalty import LoyaltyProgram
    program = LoyaltyProgram(name="DeadTypeP", is_active=True, is_default=True, uzs_per_point=250)
    _db.session.add(program); _db.session.commit()

    for dead_type in ("voucher", "free_delivery"):
        resp = client.post(
            "/api/v1/admin/loyalty/rewards",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"program_id": program.id, "name": f"dead-{dead_type}",
                  "reward_type": dead_type, "points_cost": 100},
        )
        assert resp.status_code == 400, f"{dead_type}: {resp.get_data(as_text=True)}"


def test_admin_update_reward_sets_free_product_quantity(client, app, admin_token, db, sample_category):
    from decimal import Decimal
    from business_app import db as _db
    from business_app.models.loyalty import LoyaltyProgram, LoyaltyReward
    from business_app.models.product import Product, ProductSizeEnum
    program = LoyaltyProgram(name="UpdP", is_active=True, is_default=False, uzs_per_point=250)
    product = Product(name="Bottle2", base_price=Decimal("8000"), category_id=sample_category.id,
                      size=ProductSizeEnum.SIZE_19L, is_active=True)
    _db.session.add_all([program, product]); _db.session.commit()
    reward = LoyaltyReward(program_id=program.id, name="upd-qty", reward_type="free_product",
                           points_cost=10, free_product_id=product.id, free_product_quantity=1, is_active=True)
    _db.session.add(reward); _db.session.commit()

    resp = client.put(
        f"/api/v1/admin/loyalty/rewards/{reward.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"free_product_quantity": 3},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    _db.session.refresh(reward)
    assert reward.free_product_quantity == 3


def test_admin_update_free_product_reward_with_null_money_fields(client, app, admin_token, db, sample_category):
    """Editing a free_product reward sends discount_value/min_order_value as null
    (it has no discount). The update must NOT crash with decimal.ConversionSyntax."""
    from decimal import Decimal
    from business_app import db as _db
    from business_app.models.loyalty import LoyaltyProgram, LoyaltyReward
    from business_app.models.product import Product, ProductSizeEnum
    program = LoyaltyProgram(name="NullP", is_active=True, is_default=False, uzs_per_point=250)
    product = Product(name="Bottle3", base_price=Decimal("8000"), category_id=sample_category.id,
                      size=ProductSizeEnum.SIZE_19L, is_active=True)
    _db.session.add_all([program, product]); _db.session.commit()
    reward = LoyaltyReward(program_id=program.id, name="null-money", reward_type="free_product",
                           points_cost=10, free_product_id=product.id, free_product_quantity=1, is_active=True)
    _db.session.add(reward); _db.session.commit()

    resp = client.put(
        f"/api/v1/admin/loyalty/rewards/{reward.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"discount_value": None, "min_order_value": None, "free_product_quantity": 2},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    _db.session.refresh(reward)
    assert reward.free_product_quantity == 2
    assert reward.discount_value is None
    assert reward.min_order_value == Decimal("0.00")
