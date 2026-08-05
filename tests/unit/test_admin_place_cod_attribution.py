"""Plan E — an admin standalone collection reaches PLACE scope, and no admin
surface gates on one person's debt count.

`post_collection` has accepted `delivery_address_id` since Plan 2b; the admin
route simply never forwarded it, so "the office paid 40 000" with no order id
parked the money as the payer's prepaid credit instead of settling the place.
"""

from unittest.mock import Mock

import pytest

from shared.enums import UserRole
from tests.unit.test_cod_collection_access_api import _manager_headers


def _post(client, app, admin_user, body):
    return client.post(
        '/api/v1/admin/staff/cash-reconciliation/collections',
        headers=_manager_headers(app, admin_user.id, UserRole.ADMIN.value),
        json=body,
    )


def _mock_post_collection(monkeypatch):
    posted = Mock(return_value=Mock(id=1, driver_cash_session_id=None,
                                    to_dict=lambda: {'id': 1}))
    monkeypatch.setattr(
        'business_app.services.cash_collection_service.CashCollectionService.post_collection',
        posted,
    )
    return posted


@pytest.fixture(autouse=True)
def _restore_place_gate(app):
    """`app` is SESSION-scoped (tests/conftest.py:113), so a test's
    ``app.config[...] = ...`` would otherwise leak into every later test on the
    same xdist worker — notably
    ``test_place_cod_collection_gate.py::test_flask_config_mirrors_the_shared_literal``,
    which asserts the Flask mirror still equals the shared literal. Hygiene
    only: it restores the value AFTER each test and changes no assertion.
    """
    original = app.config.get("PLACE_COD_COLLECTION_ENABLED")
    yield
    app.config["PLACE_COD_COLLECTION_ENABLED"] = original


@pytest.mark.unit
def test_admin_collection_forwards_the_delivery_address(
    client, app, admin_user, sample_user, monkeypatch
):
    """THE R2 REGRESSION on the admin side."""
    app.config['PLACE_COD_COLLECTION_ENABLED'] = True
    posted = _mock_post_collection(monkeypatch)

    response = _post(client, app, admin_user, {
        'customer_id': sample_user.id, 'amount': 40000,
        'source': 'standalone_meeting', 'notes': 'Office paid in cash',
        'delivery_address_id': 44,
    })

    assert response.status_code == 201
    assert posted.call_args.kwargs['delivery_address_id'] == 44


@pytest.mark.unit
def test_admin_collection_drops_the_address_when_the_gate_is_off(
    client, app, admin_user, sample_user, monkeypatch
):
    """Today's behaviour, preserved exactly: the parameter never reaches the
    engine, so scope resolution is byte-identical to Plan D's."""
    app.config['PLACE_COD_COLLECTION_ENABLED'] = False
    posted = _mock_post_collection(monkeypatch)

    _post(client, app, admin_user, {
        'customer_id': sample_user.id, 'amount': 40000,
        'source': 'standalone_meeting', 'notes': 'x', 'delivery_address_id': 44,
    })

    assert posted.call_args.kwargs['delivery_address_id'] is None


@pytest.mark.unit
def test_order_id_still_reaches_the_service_alongside_the_address(
    client, app, admin_user, sample_user, monkeypatch
):
    """C5.5: post_collection overwrites scope_address_id from the order when an
    order is supplied. The ROUTE forwards both and lets the service rank them —
    forwarding must not change that precedence."""
    app.config['PLACE_COD_COLLECTION_ENABLED'] = True
    posted = _mock_post_collection(monkeypatch)

    _post(client, app, admin_user, {
        'customer_id': sample_user.id, 'amount': 1000, 'source': 'standalone_meeting',
        'notes': 'x', 'order_id': 456, 'delivery_address_id': 44,
    })

    kwargs = posted.call_args.kwargs
    assert kwargs['order_id'] == 456
    assert kwargs['delivery_address_id'] == 44


@pytest.mark.unit
def test_a_member_gets_place_scope(app, db, place, sample_user):
    """The forwarded address does what it is forwarded for."""
    from business_app.services.cash_collection_service import CashCollectionService

    member_user_id = place["a1"].user_id
    scope = CashCollectionService().resolve_allocation_scope(
        member_user_id,
        delivery_address_id=place["a1"].id,
        source='standalone_meeting',
    )
    assert scope.scope_type == 'place'
    assert scope.group_id == place["group"].id


@pytest.mark.unit
def test_a_strangers_address_does_not_grant_place_scope(app, db, place):
    """C5.4. The forwarded id is CLIENT-SUPPLIED. The only thing stopping it
    from handing a stranger the place's debts is the membership intersection in
    resolve_allocation_scope. Pin it — do NOT duplicate it.

    🔴 THE STRANGER MUST BE BUILT HERE. Do NOT use `sample_user`: the `place`
    fixture is `def place(db, sample_user, second_sample_user)`
    (tests/conftest.py:359) and `a1 = UserAddress(user_id=sample_user.id, ...)`
    (:371), so `sample_user` IS a1's owner. An earlier draft of this plan
    asserted the opposite; it was wrong, and the assertion could only ever fail.
    """
    from business_app.models.user import User
    from business_app.services.cash_collection_service import CashCollectionService
    from business_app.utils.password_security import hash_password
    from shared.enums import UserRole, UserType

    # Built the way `second_sample_user` (tests/conftest.py:331) is, with a
    # phone that collides with neither it (+998901234570), `sample_user`
    # (+998901234567) nor `admin_user` (+998901234568) — `users.phone` is
    # UNIQUE and a collision dies at setup, before any assertion runs.
    stranger = User(
        email='stranger@example.com',
        phone='+998901234571',
        password_hash=hash_password('TestPassword123!'),
        first_name='Stran', last_name='Ger',
        user_type=UserType.INDIVIDUAL, role=UserRole.CUSTOMER, is_verified=True,
    )
    db.session.add(stranger)
    db.session.commit()

    # Belt and braces: prove the premise instead of assuming it.
    assert stranger.id not in {place["a1"].user_id, place["a2"].user_id}

    scope = CashCollectionService().resolve_allocation_scope(
        stranger.id,
        delivery_address_id=place["a1"].id,
        source='standalone_meeting',
    )
    assert scope.scope_type != 'place'


@pytest.mark.unit
def test_a_non_place_source_never_reaches_place_scope(app, db, place):
    """C5.3: PERSONAL_CARD_TRANSFER / ADMIN_ADJUSTMENT / BACKFILL are excluded
    from _PLACE_SCOPE_SOURCES by design — identifiably own money and book
    corrections are never door cash."""
    from business_app.services.cash_collection_service import CashCollectionService

    for source in ('personal_card_transfer', 'admin_adjustment', 'backfill'):
        scope = CashCollectionService().resolve_allocation_scope(
            place["a1"].user_id,
            delivery_address_id=place["a1"].id,
            source=source,
        )
        assert scope.scope_type != 'place', source
