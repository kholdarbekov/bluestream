"""Admin order address change: the shared predicates (T1), the options (T2) and the write (T3).

`block_code` is the one answer to "may this order's address change". It reads the reschedule's own
status window (`OrderScheduleService.schedule_window_block`) and the same-day agent hold, so the
button the admin sees and the write it sends cannot disagree. The options and the PATCH ask the same
`_disabled_reason`, so the dropdown never offers an address the save refuses, or the reverse.
"""

import inspect
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import update

from business_app import db
from business_app.models.delivery import Delivery, DeliveryRoute, DeliveryStatusHistory
from business_app.models.order import Order, OrderStatusHistory
from business_app.models.sales_visits import AgentOrderApproval
from business_app.models.user import UserAddress
from business_app.services.delivery_service import DeliveryService
from business_app.services.order_address_change_service import (
    ADDRESS_CHANGE_PERMISSIONS,
    OrderAddressChangeService,
)
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.utils.audit_logger import AuditEventType, AuditSeverity
from business_app.utils.exceptions import ConflictError, ValidationError
from shared.enums import DeliveryStatus as S
from shared.enums import OrderStatus, PaymentMethod, UserRole
from tests.integration.sales_pay_builders import claim_headers, make_outlet, staff_agent
from tests.integration.test_admin_order_reschedule import (  # noqa: F401  fixtures
    DRIVER_TELEGRAM_ID,
    PRE_STATUSES,
    _seed,
    audit_events,
    rostered_driver,
)
from tests.unit._scope_money_helpers import delivered_cod_order, make_address, make_place_group, make_user
from tests.unit.test_delivery_service_business_rules import _patch_task_publish
from tests.unit.test_place_cod_cap import _LIMIT, _OVER_EACH

pytestmark = pytest.mark.integration

LOCKED = [
    (OrderStatus.DELIVERED, S.DELIVERED, "ORDER_ADDRESS_LOCKED", "ORDER_NOT_RESCHEDULABLE"),
    (OrderStatus.CANCELLED, None, "ORDER_ADDRESS_LOCKED", "ORDER_NOT_RESCHEDULABLE"),
    (OrderStatus.RETURNED, S.RETURNED, "ORDER_ADDRESS_LOCKED", "ORDER_NOT_RESCHEDULABLE"),
    (OrderStatus.CONFIRMED, S.CANCELLED, "DELIVERY_ADDRESS_LOCKED", "DELIVERY_NOT_RESCHEDULABLE"),
]


def _ids(pre):
    return pre.value if pre else "no-row"


@pytest.mark.parametrize("pre", PRE_STATUSES, ids=_ids)
def test_every_reschedulable_state_may_change_its_address(db, user_address, rostered_driver, local_noon, pre):
    order = db.session.get(Order, _seed(user_address, rostered_driver, pre, local_noon.date()).order_id)

    assert OrderAddressChangeService.block_code(order) is None
    assert OrderAddressChangeService.assert_open(order) is None


@pytest.mark.parametrize("order_status,pre,code,reschedule_code", LOCKED)
def test_locked_states_answer_their_code_and_the_reschedule_reads_the_same_window(
    db, user_address, rostered_driver, local_noon, order_status, pre, code, reschedule_code
):
    order = db.session.get(
        Order, _seed(user_address, rostered_driver, pre, local_noon.date(), order_status=order_status).order_id
    )

    assert (OrderAddressChangeService.block_code(order), OrderScheduleService.reschedule_block_code(order)) == (
        code,
        reschedule_code,
    )
    with pytest.raises(ValidationError) as refused:
        OrderAddressChangeService.assert_open(order)
    assert refused.value.error_code == code


def _hold(db, order_id, customer, *, phone):
    """A pending same-day agent hold on the order (D4): only the approval queue may act on it."""
    agent = staff_agent(db, phone=phone, first_name="Agent")
    outlet = make_outlet(db, onboarded_by=agent, created_at=datetime.now(UTC), user=customer)
    db.session.add(
        AgentOrderApproval(
            order_id=order_id,
            outlet_id=outlet.id,
            agent_user_id=agent.id,
            earlier_order_ids=[],
            status="pending",
            requested_at=datetime.now(UTC),
        )
    )
    db.session.commit()


def test_a_pending_agent_hold_blocks_with_the_existing_code(db, user_address, rostered_driver, local_noon, sample_user):
    order = db.session.get(Order, _seed(user_address, rostered_driver, S.PENDING, local_noon.date()).order_id)
    _hold(db, order.id, sample_user, phone="+998901239777")

    assert OrderAddressChangeService.block_code(order) == "ORDER_AWAITING_STAFF_APPROVAL"
    with pytest.raises(ConflictError) as held:
        OrderAddressChangeService.assert_open(order)
    assert held.value.details["path"] == "/sales/order-approvals"


@pytest.mark.parametrize("pre", PRE_STATUSES, ids=_ids)
def test_the_driver_holding_the_stop_is_the_reschedule_guards_answer(
    db, user_address, rostered_driver, local_noon, pre
):
    order = db.session.get(Order, _seed(user_address, rostered_driver, pre, local_noon.date()).order_id)

    expected = rostered_driver.id if pre in {S.ASSIGNED, S.PICKED_UP, S.IN_TRANSIT, S.ARRIVED} else None
    # FAILED keeps its driver on the row but no longer holds the stop (D2).
    assert OrderScheduleService.driver_holding_stop(order.delivery) == expected


def test_no_delivery_means_no_driver_holds_the_stop():
    assert OrderScheduleService.driver_holding_stop(None) is None


def test_the_permissions_are_the_edit_orders_pair():
    assert ADDRESS_CHANGE_PERMISSIONS == ("manage_orders", "edit_orders")


# ---------------------------------------------------------------------------
# T2: the options, the detail metadata and GET /admin/orders/<id>/address-options.
# ---------------------------------------------------------------------------

OPTIONS_URL = "/api/v1/admin/orders/{}/address-options"
DETAIL_URL = "/api/v1/admin/orders/{}"


def _address(db, user, full_address, *, pin=(41.33, 69.32), title=None, is_default=False, **fields):
    address = UserAddress(
        user_id=user.id,
        full_address=full_address,
        city="Tashkent",
        latitude=pin[0] if pin else None,
        longitude=pin[1] if pin else None,
        title=title,
        is_default=is_default,
        **fields,
    )
    db.session.add(address)
    db.session.commit()
    return address


def _force_coords(db, address, lat, lng):
    """Write coordinates the zone listener would refuse, as a legacy row holds them."""
    db.session.execute(update(UserAddress).where(UserAddress.id == address.id).values(latitude=lat, longitude=lng))
    db.session.commit()
    db.session.expire(address)


def _error_code(resp):
    return resp.get_json()["data"]["error_code"]


def by_id(body, address_id):
    return next(option for option in body["options"] if option["id"] == address_id)


def _get(client, headers, order_id):
    resp = client.get(OPTIONS_URL.format(order_id), headers=headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["data"]


def _detail(client, headers, order_id):
    return client.get(DETAIL_URL.format(order_id), headers=headers).get_json()["data"]["order"]


def test_the_options_publish_every_saved_address_with_its_reason(
    client, db, admin_claim_headers, sample_user, second_sample_user, user_address, rostered_driver, local_noon
):
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")
    unpinned = _address(db, sample_user, "3 No Pin St", pin=None)
    legacy = _address(db, sample_user, "4 Far Away")
    _force_coords(db, legacy, 41.0, 64.0)
    zero = _address(db, sample_user, "5 Zero St")
    _force_coords(db, zero, 0.0, 0.0)  # RF5: a falsy coordinate is still a coordinate
    _address(db, second_sample_user, "6 Not Yours")
    seeded = _seed(user_address, rostered_driver, S.ASSIGNED, local_noon.date())

    body = _get(client, admin_claim_headers, seeded.order_id)

    reasons = {o["id"]: (o["selectable"], o["disabled_reason"]) for o in body["options"]}
    assert reasons == {
        user_address.id: (False, "CURRENT"),
        office.id: (True, None),
        unpinned.id: (False, "NO_PIN"),
        legacy.id: (False, "OUTSIDE_ZONE"),
        zero.id: (False, "OUTSIDE_ZONE"),
    }
    assert body["options"][0]["id"] == user_address.id  # default first
    assert by_id(body, office.id)["full_address"] == "2 Work St, Tashkent"
    assert body["current_address_id"] == user_address.id and body["current_branch"] is None
    assert body["driver"] == {"id": rostered_driver.id, "name": rostered_driver.full_name}


@pytest.mark.parametrize(
    "headers_fixture,status",
    [
        ("admin_claim_headers", 200),
        ("manager_claim_headers", 200),
        ("operator_auth_headers", 403),
        ("driver_auth_headers", 403),
    ],
    ids=["admin", "manager", "operator", "driver"],
)
def test_only_admins_and_managers_read_the_options(
    request, client, user_address, rostered_driver, local_noon, headers_fixture, status
):
    oid = _seed(user_address, rostered_driver, S.SCHEDULED, local_noon.date()).order_id

    headers = request.getfixturevalue(headers_fixture)

    assert client.get(OPTIONS_URL.format(oid), headers=headers).status_code == status


def test_an_unknown_order_is_a_404(client, admin_claim_headers):
    assert client.get(OPTIONS_URL.format(987654), headers=admin_claim_headers).status_code == 404


@pytest.mark.parametrize(
    "headers_fixture,expected", [("manager_claim_headers", True), ("operator_auth_headers", False)]
)
def test_the_detail_flag_knows_who_is_asking(
    request, client, user_address, rostered_driver, local_noon, headers_fixture, expected
):  # D14
    oid = _seed(user_address, rostered_driver, S.ASSIGNED, local_noon.date()).order_id

    order = _detail(client, request.getfixturevalue(headers_fixture), oid)

    assert order["can_change_address"] is expected and order["change_address_block_code"] is None
    assert order["change_address_driver"] == {"id": rostered_driver.id, "name": rostered_driver.full_name}


def test_a_failed_stop_names_no_driver_and_a_locked_order_publishes_its_code(
    client, db, admin_claim_headers, user_address, rostered_driver, local_noon
):
    failed = _seed(user_address, rostered_driver, S.FAILED, local_noon.date())
    delivered = _seed(user_address, rostered_driver, S.DELIVERED, local_noon.date(), order_status=OrderStatus.DELIVERED)

    assert _detail(client, admin_claim_headers, failed.order_id)["change_address_driver"] is None  # D2
    locked = _detail(client, admin_claim_headers, delivered.order_id)
    assert locked["can_change_address"] is False and locked["change_address_block_code"] == "ORDER_ADDRESS_LOCKED"
    assert locked["change_address_driver"] is None
    resp = client.get(OPTIONS_URL.format(delivered.order_id), headers=admin_claim_headers)
    assert resp.status_code == 400 and _error_code(resp) == "ORDER_ADDRESS_LOCKED"


def test_a_held_order_refuses_its_options_with_the_approval_path(
    client, db, admin_claim_headers, user_address, rostered_driver, local_noon, sample_user
):  # D4
    oid = _seed(user_address, rostered_driver, S.PENDING, local_noon.date()).order_id
    _hold(db, oid, sample_user, phone="+998901239778")

    resp = client.get(OPTIONS_URL.format(oid), headers=admin_claim_headers)

    assert resp.status_code == 409 and resp.get_json()["data"]["details"]["path"] == "/sales/order-approvals"
    assert _error_code(resp) == "ORDER_AWAITING_STAFF_APPROVAL"
    detail = _detail(client, admin_claim_headers, oid)
    assert detail["can_change_address"] is False
    assert detail["change_address_block_code"] == "ORDER_AWAITING_STAFF_APPROVAL"


def _two_branches(db, sample_user, user_address, office, *, second_onboarder=None):
    first = staff_agent(db, phone="+998901238431", first_name="Alisher")
    o1 = make_outlet(db, onboarded_by=first, created_at=datetime.now(UTC), user=sample_user)
    o2 = make_outlet(db, onboarded_by=second_onboarder or first, created_at=datetime.now(UTC), user=sample_user)
    o1.address_id, o2.address_id = user_address.id, office.id  # branch mode
    db.session.commit()
    return first, o1, o2


def test_a_branch_owned_by_another_agent_is_flagged_and_named(
    client, db, admin_claim_headers, sample_user, user_address, rostered_driver, local_noon
):  # D5 / R7
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")
    bekzod = staff_agent(db, phone="+998901238433", first_name="Bekzod")
    _, o1, o2 = _two_branches(db, sample_user, user_address, office, second_onboarder=bekzod)
    oid = _seed(user_address, rostered_driver, S.SCHEDULED, local_noon.date()).order_id

    body = _get(client, admin_claim_headers, oid)

    office_opt = by_id(body, office.id)
    assert office_opt["agent_differs"] is True
    assert office_opt["branch"] == {
        "outlet_id": o2.id,
        "outlet_name": o2.name,
        "agent_id": bekzod.id,
        "agent_name": bekzod.full_name,
    }
    assert body["current_branch"]["outlet_id"] == o1.id
    assert by_id(body, user_address.id)["agent_differs"] is False


def test_the_same_agent_on_both_branches_does_not_differ(
    client, db, admin_claim_headers, sample_user, user_address, rostered_driver, local_noon
):
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")
    _two_branches(db, sample_user, user_address, office)
    oid = _seed(user_address, rostered_driver, S.SCHEDULED, local_noon.date()).order_id

    assert by_id(_get(client, admin_claim_headers, oid), office.id)["agent_differs"] is False


def test_a_single_outlet_account_never_differs(
    client, db, admin_claim_headers, sample_user, user_address, rostered_driver, local_noon
):
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")
    alisher = staff_agent(db, phone="+998901238431", first_name="Alisher")
    outlet = make_outlet(db, onboarded_by=alisher, created_at=datetime.now(UTC), user=sample_user)
    oid = _seed(user_address, rostered_driver, S.SCHEDULED, local_noon.date()).order_id

    body = _get(client, admin_claim_headers, oid)

    assert [o["agent_differs"] for o in body["options"]] == [False, False]
    assert by_id(body, office.id)["branch"]["outlet_id"] == outlet.id


@pytest.mark.parametrize(
    "admin_onboarder,expected", [(False, (False, "SALES_PAY_SELF_DECISION")), (True, (True, None))]
)
def test_a_manager_who_is_the_new_branchs_agent_sees_it_greyed(
    app, client, db, sample_user, user_address, rostered_driver, local_noon, admin_onboarder, expected
):  # R13
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")
    role = UserRole.ADMIN if admin_onboarder else UserRole.MANAGER
    mansur = staff_agent(db, phone="+998901238432", first_name="Mansur", role=role)
    _two_branches(db, sample_user, user_address, office, second_onboarder=mansur)
    oid = _seed(user_address, rostered_driver, S.SCHEDULED, local_noon.date()).order_id

    opt = by_id(_get(client, claim_headers(app, mansur, role.value), oid), office.id)

    assert (opt["selectable"], opt["disabled_reason"]) == expected


def test_a_manager_who_onboarded_a_single_outlet_account_may_move_its_order_anywhere(
    app, client, db, address_fanout, sample_user, user_address, rostered_driver, local_noon
):  # R13: the one outlet claims every address, so no outlet moves and nobody's pay changes
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")
    mansur = staff_agent(db, phone="+998901238432", first_name="Mansur", role=UserRole.MANAGER)
    make_outlet(db, onboarded_by=mansur, created_at=datetime.now(UTC), user=sample_user)
    oid = _seed(user_address, rostered_driver, S.SCHEDULED, local_noon.date()).order_id
    mansur_headers = claim_headers(app, mansur, "manager")

    opt = by_id(_get(client, mansur_headers, oid), office.id)

    assert (opt["selectable"], opt["disabled_reason"]) == (True, None)
    resp = _change(client, mansur_headers, oid, {"address_id": office.id})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert _address_of(oid) == office.id
    assert address_fanout.calls["replenish"] == []  # D13: the outlet keeps the order


@pytest.mark.parametrize("manager_is", ["onboarder", "assigned_agent"])
def test_a_manager_who_is_either_pay_subject_of_the_new_branch_is_greyed_and_refused(
    app, client, db, address_fanout, sample_user, user_address, rostered_driver, local_noon, manager_is
):  # R13: a moved outlet's subjects are its due owner (the assignee) AND its onboarder
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")
    mansur = staff_agent(db, phone="+998901238432", first_name="Mansur", role=UserRole.MANAGER)
    bekzod = staff_agent(db, phone="+998901238433", first_name="Bekzod")
    onboarder, assignee = (mansur, bekzod) if manager_is == "onboarder" else (bekzod, mansur)
    _, _, o2 = _two_branches(db, sample_user, user_address, office, second_onboarder=onboarder)
    o2.assigned_agent_user_id = assignee.id
    db.session.commit()
    oid = _seed(user_address, rostered_driver, S.SCHEDULED, local_noon.date()).order_id
    mansur_headers = claim_headers(app, mansur, "manager")

    opt = by_id(_get(client, mansur_headers, oid), office.id)

    assert (opt["selectable"], opt["disabled_reason"]) == (False, "SALES_PAY_SELF_DECISION")
    resp = _change(client, mansur_headers, oid, {"address_id": office.id})
    data = resp.get_json()["data"]
    assert (resp.status_code, data["error_code"], data["details"]) == (
        403,
        "SALES_PAY_SELF_DECISION",
        {"agent_user_id": mansur.id},
    )
    assert _address_of(oid) == user_address.id


def test_a_cash_order_greys_a_capped_place_and_a_card_order_does_not(
    client, db, admin_claim_headers, sample_user, user_address, rostered_driver, local_noon
):  # R11
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")
    coworker = make_user(db)
    coworker_address = make_address(db, coworker)
    make_place_group(db, coworker_address, office)
    for _ in range(_LIMIT):
        delivered_cod_order(db, coworker, address=coworker_address, total=_OVER_EACH)
    seeded = _seed(user_address, rostered_driver, S.SCHEDULED, local_noon.date())

    opt = by_id(_get(client, admin_claim_headers, seeded.order_id), office.id)
    assert (opt["selectable"], opt["disabled_reason"]) == (False, "COD_PLACE_CAPPED")

    db.session.get(Order, seeded.order_id).payment_method = PaymentMethod.CARD
    db.session.commit()
    opt = by_id(_get(client, admin_claim_headers, seeded.order_id), office.id)
    assert (opt["selectable"], opt["disabled_reason"]) == (True, None)


# ---------------------------------------------------------------------------
# T3: PATCH /admin/orders/<id>/delivery-address, the write.
# ---------------------------------------------------------------------------

CHANGE_URL = "/api/v1/admin/orders/{}/delivery-address"


class _AddressFanOut:
    """What each post-commit seam of an address change was asked to do, in call order."""

    SEAMS = ("driver", "route", "optimize", "replenish")

    def __init__(self):
        self.calls = {seam: [] for seam in self.SEAMS}
        self.failing = set()

    def record(self, seam, call):
        self.calls[seam].append(call)
        if seam in self.failing:
            raise RuntimeError(f"simulated {seam} outage")


@pytest.fixture
def address_fanout(monkeypatch):
    """The address change's post-commit seams, each recorded against the real signature it stands in for.

    `ReplenishmentService.recompute_outlet` is spied on and then runs for real, so the outlet's due
    date is still republished; the recorder keeps the outlet's id. The driver notice and the quiet
    re-optimize are Celery `.delay`s, bound to their task's `.run`; the card refresh is the bot webhook
    call. A call with the wrong shape raises TypeError at the seam, and the recorder never sees it.
    `.failing` names the seams to fail.
    """
    from business_app.services.sales.replenishment_service import ReplenishmentService
    from business_app.tasks import staff_tasks
    from business_app.tasks.delivery_tasks import optimize_driver_route_task
    from business_app.utils import bot_webhook

    recorder = _AddressFanOut()
    real_recompute = ReplenishmentService.recompute_outlet
    recompute_signature = inspect.signature(real_recompute)

    def _recompute(*args, **kwargs):
        recorder.record("replenish", recompute_signature.bind(*args, **kwargs).arguments["outlet"].id)
        return real_recompute(*args, **kwargs)

    notice_signature = inspect.signature(staff_tasks.notify_order_address_changed.run)
    optimize_signature = inspect.signature(optimize_driver_route_task.run)
    route_signature = inspect.signature(bot_webhook.notify_route_updated)

    def _notice(*args, **kwargs):
        recorder.record("driver", dict(notice_signature.bind(*args, **kwargs).arguments))

    def _route(*args, **kwargs):
        bound = route_signature.bind(*args, **kwargs)
        bound.apply_defaults()
        recorder.record("route", dict(bound.arguments))

    def _optimize(*args, **kwargs):
        bound = optimize_signature.bind(*args, **kwargs)
        bound.apply_defaults()
        recorder.record("optimize", dict(bound.arguments))

    # Through the instance `__dict__`, never `setattr`: its undo would leave the conftest's bound
    # no-op on the task instance, shadowing every later class-level `Task.delay` recorder.
    _patch_task_publish(monkeypatch, staff_tasks.notify_order_address_changed, "delay", _notice)
    _patch_task_publish(monkeypatch, optimize_driver_route_task, "delay", _optimize)
    monkeypatch.setattr(bot_webhook, "notify_route_updated", _route)
    with patch.object(ReplenishmentService, "recompute_outlet", new=staticmethod(_recompute)):
        yield recorder


def _change(client, headers, order_id, body):
    """The body the Change address modal sends."""
    return client.patch(CHANGE_URL.format(order_id), headers=headers, json=body)


def _address_of(order_id):
    db.session.expire_all()
    return db.session.get(Order, order_id).delivery_address_id


def _row(seeded):
    """What an address change must never move: the stop's status and driver, the price, and the
    commission subjects (R5, D10, R9, R12)."""
    db.session.expire_all()
    order = db.session.get(Order, seeded.order_id)
    delivery = order.delivery
    return (
        delivery.status if delivery is not None else None,
        delivery.delivery_person_id if delivery is not None else None,
        order.total_amount,
        order.delivery_fee,
        order.created_by_staff_id,
        order.order_source,
    )


def _timeline_counts(seeded):
    """The customer-visible timelines (R4): an address change adds no row to either."""
    return (
        OrderStatusHistory.query.filter_by(order_id=seeded.order_id).count(),
        DeliveryStatusHistory.query.filter_by(delivery_id=seeded.delivery_id).count(),
    )


def _history(client, headers, order_id):
    resp = client.get(f"/api/v1/admin/orders/{order_id}/edit-history", headers=headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["data"]["entries"]


@pytest.mark.parametrize("pre", PRE_STATUSES, ids=_ids)
def test_every_open_state_moves_to_the_new_address_and_stays_on_its_route(
    client, db, admin_claim_headers, address_fanout, sample_user, user_address, rostered_driver, local_noon, pre
):
    seeded = _seed(user_address, rostered_driver, pre, local_noon.date())
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")
    before, counts_before = _row(seeded), _timeline_counts(seeded)

    resp = _change(client, admin_claim_headers, seeded.order_id, {"addressId": office.id})  # camelCase accepted

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["data"]["order"]["delivery_address"]["id"] == office.id
    assert _address_of(seeded.order_id) == office.id
    assert _row(seeded) == before
    assert _timeline_counts(seeded) == counts_before
    if seeded.route_id is not None:
        assert db.session.get(DeliveryRoute, seeded.route_id).optimized_order == [seeded.delivery_id]


@pytest.mark.parametrize("order_status,pre,code,_reschedule_code", LOCKED)
def test_locked_orders_refuse_and_keep_their_address(
    client,
    db,
    admin_claim_headers,
    address_fanout,
    sample_user,
    user_address,
    rostered_driver,
    local_noon,
    order_status,
    pre,
    code,
    _reschedule_code,
):
    oid = _seed(user_address, rostered_driver, pre, local_noon.date(), order_status=order_status).order_id
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")

    resp = _change(client, admin_claim_headers, oid, {"address_id": office.id})

    assert (resp.status_code, _error_code(resp)) == (400, code)
    assert _address_of(oid) == user_address.id


EXPECTED_REFUSAL = {
    "CURRENT": (400, "ADDRESS_CURRENT"),
    "NO_PIN": (400, "ADDRESS_NO_PIN"),
    "OUTSIDE_ZONE": (400, "ADDRESS_OUTSIDE_ZONE"),
    "COD_PLACE_CAPPED": (400, "COD_DEBT_LIMIT_REACHED"),
    "SALES_PAY_SELF_DECISION": (403, "SALES_PAY_SELF_DECISION"),
}


def test_every_option_the_backend_publishes_is_what_the_write_does(
    app,
    client,
    db,
    admin_claim_headers,
    address_fanout,
    sample_user,
    second_sample_user,
    user_address,
    rostered_driver,
    local_noon,
):  # anti-goal 1, both directions
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")
    _address(db, sample_user, "3 No Pin St", pin=None)
    legacy = _address(db, sample_user, "4 Far Away")
    _force_coords(db, legacy, 41.0, 64.0)
    capped = _address(db, sample_user, "5 Capped St")
    coworker = make_user(db)
    coworker_address = make_address(db, coworker)
    make_place_group(db, coworker_address, capped)
    for _ in range(_LIMIT):
        delivered_cod_order(db, coworker, address=coworker_address, total=_OVER_EACH)
    _address(db, sample_user, "6 Plain St")
    foreign = _address(db, second_sample_user, "7 Not Yours")
    mansur = staff_agent(db, phone="+998901238432", first_name="Mansur", role=UserRole.MANAGER)
    _two_branches(db, sample_user, user_address, office, second_onboarder=mansur)
    oid = _seed(user_address, rostered_driver, S.SCHEDULED, local_noon.date()).order_id
    mansur_headers = claim_headers(app, mansur, "manager")

    options = _get(client, mansur_headers, oid)["options"]

    assert {o["disabled_reason"] for o in options if not o["selectable"]} == set(EXPECTED_REFUSAL)
    for option in (o for o in options if not o["selectable"]):
        resp = _change(client, mansur_headers, oid, {"address_id": option["id"]})
        assert (resp.status_code, _error_code(resp)) == EXPECTED_REFUSAL[option["disabled_reason"]], option
        assert _address_of(oid) == user_address.id
    [pickable] = [o for o in options if o["selectable"]]
    assert _change(client, mansur_headers, oid, {"address_id": pickable["id"]}).status_code == 200
    assert _address_of(oid) == pickable["id"]
    resp = _change(client, admin_claim_headers, oid, {"address_id": foreign.id})  # never an option
    assert (resp.status_code, _error_code(resp)) == (400, "INVALID_DELIVERY_ADDRESS")


def test_a_reason_the_write_has_no_code_for_still_refuses_the_save(
    monkeypatch,
    client,
    db,
    admin_claim_headers,
    address_fanout,
    sample_user,
    user_address,
    rostered_driver,
    local_noon,
):  # anti-goal 1: the write guard fails closed
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")
    oid = _seed(user_address, rostered_driver, S.ASSIGNED, local_noon.date()).order_id
    real = OrderAddressChangeService._disabled_reason.__func__

    def _with_a_new_reason(cls, order, address, **kwargs):
        # A sixth reason, added to `_disabled_reason` before anyone gives it a write code.
        return "NEW_REASON" if address.id == office.id else real(cls, order, address, **kwargs)

    monkeypatch.setattr(OrderAddressChangeService, "_disabled_reason", classmethod(_with_a_new_reason))

    opt = by_id(_get(client, admin_claim_headers, oid), office.id)
    assert (opt["selectable"], opt["disabled_reason"]) == (False, "NEW_REASON")

    resp = _change(client, admin_claim_headers, oid, {"address_id": office.id})

    assert resp.status_code == 500, resp.get_data(as_text=True)
    assert _address_of(oid) == user_address.id
    assert _history(client, admin_claim_headers, oid) == []
    assert address_fanout.calls == {seam: [] for seam in _AddressFanOut.SEAMS}


@pytest.mark.parametrize(
    "headers_fixture,status",
    [("manager_claim_headers", 200), ("operator_auth_headers", 403), ("driver_auth_headers", 403)],
    ids=["manager", "operator", "driver"],
)
def test_the_role_matrix(
    request,
    client,
    db,
    address_fanout,
    sample_user,
    user_address,
    rostered_driver,
    local_noon,
    headers_fixture,
    status,
):  # R1
    oid = _seed(user_address, rostered_driver, S.SCHEDULED, local_noon.date()).order_id
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")

    resp = _change(client, request.getfixturevalue(headers_fixture), oid, {"address_id": office.id})

    assert resp.status_code == status
    assert _address_of(oid) == (office.id if status == 200 else user_address.id)


def test_the_change_is_recorded_once_in_history_and_audit(
    client,
    db,
    admin_claim_headers,
    admin_user,
    audit_events,
    address_fanout,
    sample_user,
    user_address,
    rostered_driver,
    local_noon,
):  # R8, D6
    seeded = _seed(user_address, rostered_driver, S.SCHEDULED, local_noon.date())
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")
    body = {"address_id": office.id, "note": "  Customer called  "}

    resp = _change(client, admin_claim_headers, seeded.order_id, body)

    assert resp.status_code == 200, resp.get_data(as_text=True)
    [entry] = _history(client, admin_claim_headers, seeded.order_id)
    diff = {
        "kind": "address_change",
        "address_before": {
            "id": user_address.id,
            "title": None,
            "full_address": "1 Test St, Tashkent",
            "latitude": 41.3111,
            "longitude": 69.2797,
        },
        "address_after": {
            "id": office.id,
            "title": "Office",
            "full_address": "2 Work St, Tashkent",
            "latitude": 41.33,
            "longitude": 69.32,
        },
        "driver_notified": False,
        "branch_before": None,
        "branch_after": None,
        "self_decided": False,
    }
    assert (entry["diff"], entry["reason"], entry["is_post_delivery"], entry["edited_by_user"]["id"]) == (
        diff,
        "Customer called",
        False,
        admin_user.id,
    )
    [event] = [e for e in audit_events if e["action"] == "order_address_changed"]
    assert (event["event_type"], event["severity"], event["resource_type"], event["resource_id"]) == (
        AuditEventType.ORDER_UPDATED,
        AuditSeverity.HIGH,
        "order",
        str(seeded.order_id),
    )
    assert event["additional_data"] == {"order_number": seeded.order_number, "history_id": entry["id"], **diff}


def test_a_driver_held_stop_records_the_notice_and_marks_route_metrics_stale(
    client, db, admin_claim_headers, address_fanout, sample_user, user_address, rostered_driver, local_noon
):  # D2, D9
    seeded = _seed(user_address, rostered_driver, S.ASSIGNED, local_noon.date())
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")

    resp = _change(client, admin_claim_headers, seeded.order_id, {"address_id": office.id})

    assert resp.status_code == 200, resp.get_data(as_text=True)
    [entry] = _history(client, admin_claim_headers, seeded.order_id)
    assert entry["diff"]["driver_notified"] is True
    db.session.expire_all()
    assert db.session.get(DeliveryRoute, seeded.route_id).extra_data["metrics_stale"] is True


@pytest.mark.parametrize("pre", [S.SCHEDULED, S.ASSIGNED], ids=_ids)
def test_distance_follows_the_new_address_and_only_a_pool_row_gets_a_new_eta(
    client, db, admin_claim_headers, address_fanout, sample_user, user_address, rostered_driver, local_noon, pre
):  # D11
    seeded = _seed(user_address, rostered_driver, pre, local_noon.date())
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")
    before_eta = db.session.get(Delivery, seeded.delivery_id).estimated_delivery_time

    resp = _change(client, admin_claim_headers, seeded.order_id, {"address_id": office.id})

    assert resp.status_code == 200, resp.get_data(as_text=True)
    db.session.expire_all()
    delivery = db.session.get(Delivery, seeded.delivery_id)
    assert delivery.distance_km == DeliveryService().distance_km(office) and delivery.distance_km != 3.2
    assert (delivery.estimated_delivery_time != before_eta) is (pre == S.SCHEDULED)


@pytest.mark.parametrize("notes,expected", [("Ring twice", None), ("Leave at the door", "Leave at the door")])
def test_copied_subscription_instructions_are_cleared_and_typed_notes_kept(
    client,
    db,
    admin_claim_headers,
    address_fanout,
    sample_user,
    user_address,
    rostered_driver,
    local_noon,
    notes,
    expected,
):  # D12
    user_address.delivery_instructions = "Ring twice"
    seeded = _seed(user_address, rostered_driver, S.SCHEDULED, local_noon.date())
    db.session.get(Order, seeded.order_id).delivery_notes = notes
    db.session.commit()
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")

    resp = _change(client, admin_claim_headers, seeded.order_id, {"address_id": office.id})

    assert resp.status_code == 200, resp.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Order, seeded.order_id).delivery_notes == expected


def test_a_note_over_500_characters_is_refused_uncoded(
    client, db, admin_claim_headers, address_fanout, sample_user, user_address, rostered_driver, local_noon
):
    oid = _seed(user_address, rostered_driver, S.SCHEDULED, local_noon.date()).order_id
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")

    resp = _change(client, admin_claim_headers, oid, {"address_id": office.id, "note": "x" * 501})

    assert resp.status_code == 400 and "error_code" not in (resp.get_json().get("data") or {})
    assert _address_of(oid) == user_address.id


def test_a_held_order_is_refused_409_with_the_approval_path(
    client, db, admin_claim_headers, address_fanout, sample_user, user_address, rostered_driver, local_noon
):  # D4
    oid = _seed(user_address, rostered_driver, S.PENDING, local_noon.date()).order_id
    _hold(db, oid, sample_user, phone="+998901239779")
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")

    resp = _change(client, admin_claim_headers, oid, {"address_id": office.id})

    data = resp.get_json()["data"]
    assert (resp.status_code, data["error_code"], data["details"]["path"]) == (
        409,
        "ORDER_AWAITING_STAFF_APPROVAL",
        "/sales/order-approvals",
    )
    assert _address_of(oid) == user_address.id


def test_a_manager_who_is_a_pay_subject_is_refused_and_an_admin_records_self_decided(
    app, client, db, admin_claim_headers, address_fanout, sample_user, user_address, rostered_driver, local_noon
):  # R13
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")
    mansur = staff_agent(db, phone="+998901238432", first_name="Mansur", role=UserRole.MANAGER)
    _, _, o2 = _two_branches(db, sample_user, user_address, office, second_onboarder=mansur)
    oid = _seed(user_address, rostered_driver, S.SCHEDULED, local_noon.date()).order_id

    refused = _change(client, claim_headers(app, mansur, "manager"), oid, {"address_id": office.id})

    assert (refused.status_code, _error_code(refused)) == (403, "SALES_PAY_SELF_DECISION")
    assert _address_of(oid) == user_address.id

    adil = staff_agent(db, phone="+998901238434", first_name="Adil", role=UserRole.ADMIN)
    o2.onboarded_by_user_id = adil.id  # adil, an admin, is now the pay subject instead
    db.session.commit()

    allowed = _change(client, claim_headers(app, adil, "admin"), oid, {"address_id": office.id})

    assert allowed.status_code == 200, allowed.get_data(as_text=True)
    [entry] = _history(client, admin_claim_headers, oid)
    assert entry["diff"]["self_decided"] is True


def test_moving_between_branches_recomputes_both_outlets(
    client, db, admin_claim_headers, address_fanout, sample_user, user_address, rostered_driver, local_noon
):  # D13
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")
    _, o1, o2 = _two_branches(db, sample_user, user_address, office)
    oid = _seed(user_address, rostered_driver, S.SCHEDULED, local_noon.date()).order_id

    resp = _change(client, admin_claim_headers, oid, {"address_id": office.id})

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert sorted(address_fanout.calls["replenish"]) == sorted([o1.id, o2.id])
    [entry] = _history(client, admin_claim_headers, oid)
    assert (entry["diff"]["branch_before"]["outlet_id"], entry["diff"]["branch_after"]["outlet_id"]) == (o1.id, o2.id)


def test_a_replenishment_outage_still_answers_200(
    client,
    db,
    admin_claim_headers,
    audit_events,
    address_fanout,
    sample_user,
    user_address,
    rostered_driver,
    local_noon,
):
    address_fanout.failing = {"replenish"}
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")
    _two_branches(db, sample_user, user_address, office)
    oid = _seed(user_address, rostered_driver, S.SCHEDULED, local_noon.date()).order_id

    resp = _change(client, admin_claim_headers, oid, {"address_id": office.id})

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert _address_of(oid) == office.id
    assert len([e for e in audit_events if e["action"] == "order_address_changed"]) == 1  # later steps still ran


def test_a_legacy_order_without_an_address_can_be_given_one(
    client, db, admin_claim_headers, address_fanout, sample_user, user_address, rostered_driver, local_noon
):  # RF4
    oid = _seed(user_address, rostered_driver, S.SCHEDULED, local_noon.date()).order_id
    db.session.get(Order, oid).delivery_address_id = None
    db.session.commit()
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")

    assert "CURRENT" not in {o["disabled_reason"] for o in _get(client, admin_claim_headers, oid)["options"]}
    resp = _change(client, admin_claim_headers, oid, {"address_id": office.id})

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert _address_of(oid) == office.id
    [entry] = _history(client, admin_claim_headers, oid)
    assert entry["diff"]["address_before"] is None


# ---------------------------------------------------------------------------
# T4: the driver fan-out -- one loud notice, a silent card refresh, a quiet re-sort.
# ---------------------------------------------------------------------------

DRIVER_HELD_PRE = [S.ASSIGNED, S.PICKED_UP, S.IN_TRANSIT, S.ARRIVED]
NO_DRIVER_PRE = [None, S.SCHEDULED, S.PENDING, S.FAILED, S.RESCHEDULED]


@pytest.mark.parametrize("pre", DRIVER_HELD_PRE, ids=_ids)
def test_the_driver_on_the_stop_is_told_once_and_the_route_resorts_quietly(
    client, db, admin_claim_headers, address_fanout, sample_user, user_address, rostered_driver, local_noon, pre
):
    seeded = _seed(user_address, rostered_driver, pre, local_noon.date())
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office", district="Yunusobod", apartment_number="12")

    resp = _change(client, admin_claim_headers, seeded.order_id, {"address_id": office.id})

    assert resp.status_code == 200, resp.get_data(as_text=True)
    [call] = address_fanout.calls["driver"]
    assert (call["delivery_id"], call["driver_id"]) == (seeded.delivery_id, rostered_driver.id)
    p = call["payload"]
    keys = ("telegram_id", "delivery_id", "delivery_address_id", "old_address", "new_address", "lat", "lng")
    assert {k: p[k] for k in keys} == {
        "telegram_id": int(DRIVER_TELEGRAM_ID),
        "delivery_id": seeded.delivery_id,
        "delivery_address_id": office.id,
        "old_address": "1 Test St, Tashkent",
        "new_address": "2 Work St, Tashkent",
        "lat": 41.33,
        "lng": 69.32,
    }
    assert p["door"] == {
        "address": "2 Work St, Tashkent",
        "district": "Yunusobod",
        "apartment_number": "12",
        "floor_number": "",
        "landmark": "",
        "delivery_instructions": "",
    }
    assert (p["order_info"]["order_number"], p["order_info"]["delivery_address"]) == (
        seeded.order_number,
        "2 Work St, Tashkent",
    )
    assert address_fanout.calls["route"] == [
        {
            "driver_id": rostered_driver.id,
            "sound": False,
            "materiality": None,
            "trigger": "address_change",
            "event_id": None,
        }
    ]
    assert address_fanout.calls["optimize"] == [{"driver_id": rostered_driver.id, "trigger": "address_change"}]


@pytest.mark.parametrize("pre", NO_DRIVER_PRE, ids=_ids)
def test_no_driver_on_the_stop_means_no_fan_out(
    client, db, admin_claim_headers, address_fanout, sample_user, user_address, rostered_driver, local_noon, pre
):  # R6, D2
    seeded = _seed(user_address, rostered_driver, pre, local_noon.date())
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")

    resp = _change(client, admin_claim_headers, seeded.order_id, {"address_id": office.id})

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert (address_fanout.calls["driver"], address_fanout.calls["route"], address_fanout.calls["optimize"]) == (
        [],
        [],
        [],
    )


def test_a_broker_outage_on_the_driver_notice_still_refreshes_and_resorts(
    client, db, admin_claim_headers, address_fanout, sample_user, user_address, rostered_driver, local_noon
):
    address_fanout.failing = {"driver"}
    seeded = _seed(user_address, rostered_driver, S.ASSIGNED, local_noon.date())
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")

    resp = _change(client, admin_claim_headers, seeded.order_id, {"address_id": office.id})

    assert resp.status_code == 200, resp.get_data(as_text=True)
    [entry] = _history(client, admin_claim_headers, seeded.order_id)
    assert entry["diff"]["driver_notified"] is True  # the notice was due; queuing failed
    assert (len(address_fanout.calls["route"]), len(address_fanout.calls["optimize"])) == (1, 1)


def test_a_legacy_order_without_an_address_tells_the_driver_an_empty_old_address(
    client, db, admin_claim_headers, address_fanout, sample_user, user_address, rostered_driver, local_noon
):  # RF4
    seeded = _seed(user_address, rostered_driver, S.ASSIGNED, local_noon.date())
    db.session.get(Order, seeded.order_id).delivery_address_id = None
    db.session.commit()
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")

    resp = _change(client, admin_claim_headers, seeded.order_id, {"address_id": office.id})

    assert resp.status_code == 200, resp.get_data(as_text=True)
    [call] = address_fanout.calls["driver"]
    assert call["payload"]["old_address"] == ""
