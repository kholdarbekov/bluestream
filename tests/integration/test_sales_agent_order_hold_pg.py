"""REAL POSTGRES: the same-day hold under concurrency (compensation spec §10.5).

T-HOLD-8: two placements at one outlet behind a barrier. `place_order` locks the visit, then the
OUTLET, before it asks the same-day rule, so the second waits for the first's commit and sees its
order: exactly one is held.

T-HOLD-14: a manager's approval and the store's own cancel behind a barrier. Both take the order
row first (the decision then the hold row; the cancel then `close_on_cancel`), so one waits for
the other: no deadlock, no 500, and the hold row and the order agree. When the approval commits
first, the store's cancel meets an ordinary CONFIRMED order with a driverless delivery, which it
may lawfully cancel (`customer_cancel_block_code`); that is two sequential decisions, not a lost
race, so the test accepts it. Three end states pass (PR17): OA2 409 with the row and the order
cancelled; OA2 200 with the cancel refused (400 `ORDER_NOT_CUSTOMER_CANCELLABLE`), the row approved
and the order confirmed; OA2 200 and the cancel 200, the row approved and the order cancelled. This
departs from the spec's "exactly one wins" (§10.5); the owner gets that wording in the Task 14
handover.

Each thread pushes its own app context, i.e. its own session. Every row is built through the real
services; nothing here hand-writes a hold.
"""

import os
import threading
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from flask import current_app
from flask_jwt_extended import create_access_token

from business_app.models.order import Order
from business_app.models.product import Product, ProductCategory
from business_app.models.sales import SalesAgentProfile
from business_app.models.sales_visits import AgentOrderApproval, Visit
from business_app.models.user import User, UserAddress
from business_app.services.sales.outlet_service import OutletService
from business_app.services.sales.visit_service import VisitService
from business_app.utils.password_security import hash_password
from shared.enums import EntitySubtype, OrderStatus, UserRole, UserType
from tests.integration.sales_pay_builders import APPROVALS
from tests.integration.test_outlet_create import GROCERY
from tests.unit.test_outlet_dedupe import PIN, _customer
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.integration

HELD = "awaiting_staff_approval"


def _world(pg_db):
    """Agent A onboarded a grocery store without Telegram, and a manager reassigned it to agent B
    (Q10), so both may visit it; a manager decides; one product with stock."""
    # `pg_app` is built without the suite's REDIS_URL (TestingConfig falls back to localhost), and
    # `create_order` reserves stock in Redis: the `pay_on_postgres` precedent.
    current_app.config["REDIS_URL"] = os.environ["REDIS_URL"]
    category = ProductCategory(name="Water", description="Water products", is_active=True)
    pg_db.session.add(category)
    pg_db.session.flush()
    product = Product(
        name="Pure Water 19L",
        category_id=category.id,
        size="19L",
        volume=19.0,
        volume_unit="L",
        base_price=Decimal("15000.00"),
        stock_quantity=100,
        is_active=True,
        created_at=datetime.now(UTC),
    )
    manager = User(
        phone="+998901239133",
        password_hash=hash_password("ManagerPassword123!"),
        first_name="Malika",
        last_name="Menejer",
        user_type=UserType.STAFF,
        role=UserRole.MANAGER,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    pg_db.session.add_all([product, manager])
    pg_db.session.commit()
    agents = [
        make_sales_agent_user(pg_db, phone=phone, staff_roles=["sales_agent"])
        for phone in ("+998901239131", "+998901239132")
    ]
    for agent in agents:
        pg_db.session.add(SalesAgentProfile(user_id=agent.id, districts=["chilanzar"]))
    customer = _customer(pg_db, "+998901112299", company="Bahor", subtype=EntitySubtype.GROCERY_STORE)
    pg_db.session.add(
        UserAddress(user_id=customer.id, full_address="Chilonzor 5", latitude=PIN[0], longitude=PIN[1], is_default=True)
    )
    pg_db.session.commit()
    outlet = OutletService.create(agents[0].id, dict(GROCERY), link_user_id=customer.id)
    OutletService.assign(outlet.id, agents[1].id, actor_id=manager.id)
    return SimpleNamespace(
        product_id=product.id,
        manager_id=manager.id,
        agent_ids=[agent.id for agent in agents],
        customer_id=customer.id,
        outlet_id=outlet.id,
    )


def _payload(product_id):
    """Exactly what the staff bot posts to POST /sales/visits/<id>/order."""
    return {
        "items": [{"product_id": product_id, "quantity": 2}],
        "payment_method": "cash",
        "delivery_date": None,
        "delivery_window_start": None,
        "delivery_window_end": None,
        "delivery_notes": None,
    }


def _visit_at_order_step(agent_id, outlet_id):
    visit = VisitService.start(agent_id, outlet_id)
    VisitService.checkin(visit, latitude=None, longitude=None, horizontal_accuracy=None, skipped=True)
    VisitService.stock_check(visit, [])
    return visit.id


def _headers(app, user_id, role):
    with app.app_context():
        token = create_access_token(
            identity=str(user_id), additional_claims={"role": role} if role is not None else None
        )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _run_together(*targets):
    """Start every target behind one barrier; return once all have finished (or fail loudly)."""
    barrier = threading.Barrier(len(targets))
    threads = [threading.Thread(target=target, args=(barrier,)) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "a thread is still waiting: a lock was never released"


def test_t_hold_8_two_placements_at_one_outlet_at_once_hold_exactly_one(pg_app, pg_db):
    world = _world(pg_db)
    visit_ids = [_visit_at_order_step(agent_id, world.outlet_id) for agent_id in world.agent_ids]
    states, errors = {}, []

    def placer(agent_id, visit_id):
        def run(barrier):
            with pg_app.app_context():
                try:
                    visit = pg_db.session.get(Visit, visit_id)
                    barrier.wait(timeout=10)
                    _order, state = VisitService.place_order(visit, _payload(world.product_id), agent_id)
                    states[agent_id] = state
                except Exception as exc:  # noqa: BLE001 — named by the assertion below
                    errors.append(repr(exc))

        return run

    _run_together(*(placer(agent_id, visit_id) for agent_id, visit_id in zip(world.agent_ids, visit_ids)))

    assert errors == []
    assert sorted(states.values()) == sorted(["confirmed", HELD])
    pg_db.session.expire_all()
    [row] = AgentOrderApproval.query.filter_by(outlet_id=world.outlet_id).all()
    held_agent = next(agent_id for agent_id, state in states.items() if state == HELD)
    first_order = Order.query.filter(Order.created_by_staff_id != held_agent, Order.visit_id.in_(visit_ids)).one()
    assert (row.status, row.agent_user_id, row.earlier_order_ids) == ("pending", held_agent, [first_order.id])


def test_t_hold_14_an_approval_racing_the_stores_cancel_ends_consistent(pg_app, pg_db):
    """T-HOLD-14 as PR17 states it: no 500, no deadlock, both threads finish, and the end state is
    one of three. The spec's "exactly one wins" (§10.5) does not hold when OA2 commits first: the
    store's cancel then meets an ordinary confirmed order and may cancel it too. That wording goes
    to the owner in the Task 14 handover; the approve-then-cancel sequence has its own named test,
    `test_t_hold_14_a_store_cancel_after_the_approval_cancels_an_ordinary_order`."""
    world = _world(pg_db)
    first_visit = _visit_at_order_step(world.agent_ids[0], world.outlet_id)
    first, first_state = VisitService.place_order(
        pg_db.session.get(Visit, first_visit), _payload(world.product_id), world.agent_ids[0]
    )
    held, held_state = VisitService.place_order(
        pg_db.session.get(Visit, _visit_at_order_step(world.agent_ids[1], world.outlet_id)),
        _payload(world.product_id),
        world.agent_ids[1],
    )
    assert (first_state, held_state) == ("confirmed", HELD)
    held_id = held.id
    manager_headers = _headers(pg_app, world.manager_id, "manager")
    store_headers = _headers(pg_app, world.customer_id, None)
    answers = {}

    def caller(name, path, body, headers):
        def run(barrier):
            with pg_app.app_context():
                client = pg_app.test_client()
                barrier.wait(timeout=10)
                response = client.post(path, json=body, headers=headers)
                answers[name] = (response.status_code, response.get_json())

        return run

    _run_together(
        caller("approve", f"{APPROVALS}/{held_id}/approve", {}, manager_headers),
        caller("cancel", f"/api/v1/orders/{held_id}/cancel", {"reason": "Ordered twice"}, store_headers),
    )

    assert set(answers) == {"approve", "cancel"}, answers  # both threads finished with an answer
    (approve_status, approve_body), (cancel_status, cancel_body) = answers["approve"], answers["cancel"]
    assert 500 not in (approve_status, cancel_status), answers  # a deadlock surfaces as a 500
    pg_db.session.expire_all()
    row = AgentOrderApproval.query.filter_by(order_id=held_id).one()
    order = pg_db.session.get(Order, held_id)
    if approve_status != 200:
        # Outcome 1, the cancel won: the decision answers the one code the queue page handles.
        assert (approve_status, approve_body["error_code"]) == (409, "SALES_ORDER_APPROVAL_NOT_PENDING")
        assert cancel_status == 200
        assert (row.status, row.decided_by_user_id, order.status) == ("cancelled", world.customer_id, OrderStatus.CANCELLED)
    elif cancel_status != 200:
        # Outcome 2, the decision won and the cancel was refused with the customer route's
        # existing refusal: the row approved, the order confirmed.
        assert (cancel_status, cancel_body["data"]["error_code"]) == (400, "ORDER_NOT_CUSTOMER_CANCELLABLE")
        assert (row.status, order.status) == ("approved", OrderStatus.CONFIRMED)
    else:
        # Outcome 3, the decision won and the store then cancelled an ordinary confirmed order
        # (no driver had it yet): the row stays approved, the order is cancelled.
        assert (row.status, order.status) == ("approved", OrderStatus.CANCELLED)
