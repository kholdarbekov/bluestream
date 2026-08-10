"""Regressions for the DeliveryPerson.id vs User.id confusion in try-out tasks.

Production symptom: the admin try-out drawer's Tasks tab rendered CUSTOMER names
in the Driver column, because the admin UI posts a ``delivery_persons.id`` into
``tryout_tasks.assigned_driver_user_id`` -- a column FK'd to ``users.id``.

Every existing try-out test misses this because none of them make the two id
spaces diverge: the shared ``delivery_driver`` fixture is a bare ``User`` with no
``DeliveryPerson`` row at all, so ``DeliveryPerson.id`` and ``DeliveryPerson.user_id``
can never disagree. The fixtures below deliberately reproduce the production
shape -- a driver whose ``DeliveryPerson.id`` collides with a *different*,
customer-owned ``users.id``.

These tests must not lean on the FK constraint: the suite runs SQLite with
foreign keys OFF, so a bad id is accepted by the database either way. They
assert on the serializer contract and on the service's own validation instead.
"""

from datetime import UTC, datetime

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.delivery import DeliveryPerson
from business_app.models.user import User
from business_app.services.tryout_service import TryoutService
from business_app.utils.exceptions import ValidationError
from shared.enums import TryoutTaskStatus, UserRole, UserType
from business_app.utils.password_security import hash_password


def _admin_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id), additional_claims={'role': 'admin'})
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


@pytest.fixture
def diverged_driver(db):
    """A driver whose DeliveryPerson.id != DeliveryPerson.user_id, with a CUSTOMER
    sitting on the users.id that equals that DeliveryPerson.id.

    This is the production shape (prod had delivery_persons.id=7 -> user_id=175,
    while users.id=7 was a customer). Without this divergence the bug is invisible.
    """
    decoy = User(
        email='decoy.customer@example.com',
        phone='+998900000101',
        password_hash=hash_password('CustomerPassword123!'),
        first_name='Decoy',
        last_name='Customer',
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(decoy)

    driver_user = User(
        email='diverged.driver@example.com',
        phone='+998900000102',
        password_hash=hash_password('DriverPassword123!'),
        first_name='Diverged',
        last_name='Driver',
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(driver_user)
    db.session.flush()

    person = DeliveryPerson(
        user_id=driver_user.id,
        full_name='Diverged Driver',
        phone='+998900000102',
        is_active=True,
        is_available=True,
    )
    db.session.add(person)
    db.session.commit()

    assert person.id != person.user_id, 'fixture must diverge the two id spaces'
    return {'person': person, 'driver_user': driver_user, 'decoy': decoy}


def test_delivery_personnel_endpoint_publishes_user_id(client, app, admin_user, diverged_driver):
    """The admin driver-picker endpoint must publish the users.id to assign by.

    Fails today: serialize_delivery_person_admin emits only `id`
    (= delivery_persons.id), so the UI's `driver.user_id || driver.id` fallback
    always ships the wrong id space.
    """
    response = client.get(
        '/api/v1/admin/delivery-personnel',
        headers=_admin_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    items = response.get_json()['data']['items']
    assert items, 'expected the seeded delivery person in the payload'

    row = next(item for item in items if item['id'] == diverged_driver['person'].id)
    assert row['user_id'] == diverged_driver['person'].user_id


def test_assign_task_rejects_a_non_driver_user_id(client, app, admin_user, db, diverged_driver, sample_product):
    """Assigning a delivery_persons.id that resolves to a customer must 400, not persist.

    This is the exact production write: the UI sent delivery_persons.id=7 and the
    backend stored it, so the Tasks tab rendered users.id=7 -- a customer.
    """
    tryout = TryoutService.create_tryout(
        {
            'trial_contact': {'first_name': 'Trial', 'phone': '+998900000103'},
            'address': {'full_address': 'Some address'},
            'items': [{'product_id': sample_product.id, 'quantity': 1}],
        },
        admin_user.id,
    )
    task = tryout.tasks[0]
    decoy_id = diverged_driver['decoy'].id

    response = client.put(
        f'/api/v1/admin/tryout-tasks/{task.id}/assign',
        headers=_admin_headers(app, admin_user.id),
        json={'assigned_driver_user_id': decoy_id},
    )

    assert response.status_code == 400
    db.session.expire_all()
    assert TryoutService._load_task(task.id).assigned_driver_user_id != decoy_id


def test_assign_task_accepts_a_real_driver_user_id(client, app, admin_user, db, diverged_driver, sample_product):
    """The corrected id space must still work end to end."""
    tryout = TryoutService.create_tryout(
        {
            'trial_contact': {'first_name': 'Trial', 'phone': '+998900000104'},
            'address': {'full_address': 'Some address'},
            'items': [{'product_id': sample_product.id, 'quantity': 1}],
        },
        admin_user.id,
    )
    task = tryout.tasks[0]
    driver_user_id = diverged_driver['person'].user_id

    response = client.put(
        f'/api/v1/admin/tryout-tasks/{task.id}/assign',
        headers=_admin_headers(app, admin_user.id),
        json={'assigned_driver_user_id': driver_user_id},
    )

    assert response.status_code == 200
    db.session.expire_all()
    assert TryoutService._load_task(task.id).assigned_driver_user_id == driver_user_id


def test_admin_who_is_not_a_driver_is_never_written_in_as_the_driver(
    app, admin_user, db, sample_product
):
    """An admin completing a handoff must not become the assigned driver.

    The `or actor_user_id` fallbacks silently assigned the acting admin, which
    left the auto-created pickup task pointing at a non-driver -- invisible to
    every driver in the staff bot AND excluded from the open pool, because the
    pool only widens to `assigned_driver_user_id IS NULL`.
    """
    tryout = TryoutService.create_tryout(
        {
            'trial_contact': {'first_name': 'Trial', 'phone': '+998900000105'},
            'address': {'full_address': 'Some address'},
            'items': [{'product_id': sample_product.id, 'quantity': 1}],
            'complete_handoff': True,
        },
        admin_user.id,
    )

    for task in tryout.tasks:
        assert task.assigned_driver_user_id != admin_user.id, (
            f'{task.task_type} task was assigned to the acting admin'
        )

    pickup = next((t for t in tryout.tasks if t.task_type.value == 'pickup'), None)
    if pickup is not None:
        assert pickup.assigned_driver_user_id is None
        assert pickup.status == TryoutTaskStatus.OPEN, 'unassigned pickup must stay in the driver pool'


def test_driver_creating_a_tryout_is_still_auto_assigned(app, db, diverged_driver, sample_product):
    """The staff-bot flow depends on the actor fallback when the actor IS a driver.

    Pins the half of the fallback that must survive the fix.
    """
    driver_user = diverged_driver['driver_user']

    tryout = TryoutService.create_tryout(
        {
            'trial_contact': {'first_name': 'Trial', 'phone': '+998900000106'},
            'address': {'full_address': 'Some address'},
            'items': [{'product_id': sample_product.id, 'quantity': 1}],
            'complete_handoff': True,
        },
        driver_user.id,
        source='driver',
    )

    handoff = next(t for t in tryout.tasks if t.task_type.value == 'handoff')
    assert handoff.assigned_driver_user_id == driver_user.id


def test_staff_create_survives_a_driver_with_no_delivery_profile(client, app, db, delivery_driver, sample_product):
    """A `delivery_driver` with no DeliveryPerson row must not 400 the staff create.

    `require_staff_roles` lets them through (assert_delivery_person_active is a
    no-op without a profile), so the try-out create must degrade to an unassigned
    task in the open pool rather than rejecting the request outright.
    """
    with app.app_context():
        token = create_access_token(identity=str(delivery_driver.id))
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    response = client.post(
        '/api/v1/staff/tryouts',
        headers=headers,
        json={
            'trial_contact': {'first_name': 'Trial', 'phone': '+998900000108'},
            'address': {'full_address': 'Some address'},
            'items': [{'product_id': sample_product.id, 'quantity': 1}],
        },
    )

    assert response.status_code == 201, response.get_json()


def test_a_legacy_corrupt_task_can_still_be_completed(app, admin_user, db, diverged_driver, sample_product):
    """Already-persisted bad ids must not become an outage.

    Validation belongs at the inbound boundary. Re-validating a row that the old
    code already wrote would make every legacy-corrupt task un-completable, which
    is strictly worse than the display bug it came from.
    """
    tryout = TryoutService.create_tryout(
        {
            'trial_contact': {'first_name': 'Trial', 'phone': '+998900000109'},
            'address': {'full_address': 'Some address'},
            'items': [{'product_id': sample_product.id, 'quantity': 1}],
        },
        admin_user.id,
    )
    task = tryout.tasks[0]

    # Simulate what the buggy admin UI persisted: a customer's users.id.
    task.assigned_driver_user_id = diverged_driver['decoy'].id
    db.session.commit()

    TryoutService.complete_handoff_task(task.id, admin_user.id)

    reloaded = TryoutService._load_task(task.id)
    assert reloaded.status == TryoutTaskStatus.COMPLETED
    assert reloaded.assigned_driver_user_id != diverged_driver['decoy'].id


def test_create_tryout_rejects_a_non_driver_assigned_driver(app, admin_user, db, diverged_driver, sample_product):
    """The create path must reject the wrong id space too, not just /assign."""
    with pytest.raises(ValidationError):
        TryoutService.create_tryout(
            {
                'trial_contact': {'first_name': 'Trial', 'phone': '+998900000107'},
                'address': {'full_address': 'Some address'},
                'items': [{'product_id': sample_product.id, 'quantity': 1}],
                'assigned_driver_user_id': diverged_driver['decoy'].id,
            },
            admin_user.id,
        )
