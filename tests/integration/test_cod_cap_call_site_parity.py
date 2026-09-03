"""Every COD-cap decision point must agree, for the same customer, at the same moment.

The read surfaces that OFFER cash and the write guards that ACCEPT it are ONE
decision. Six sites used to spell that decision out separately; two of them
already disagreed with each other in production (the batch customer map used a
narrower open-debt clause than the checkout guard, so a Click order delivered
and left unpaid was "allowed" on the map and "restricted" at checkout for the
same customer at the same moment). This is the guard that makes a seventh
copy -- or a regression in any of the already-collapsed sites -- fail loudly,
by naming exactly which site's answer diverged.

Unlike ``tests/unit/test_cod_cap_enforcement_points.py`` (Task 8), which pins
each site INDIVIDUALLY against its own hand-picked place-arm scenario, this
file asks EVERY site the SAME question about the SAME customer in the SAME
test, and asserts they produced the SAME boolean. Two sites can each look
correct in isolation and still disagree with each other; only asking them
together, side by side, catches that.

Sites covered here (14 checks across 4 read surfaces + 5 write guards, found
by grep-walking every caller of ``is_customer_cod_restricted`` /
``get_cod_restricted_flags`` / ``get_cod_restriction_context`` /
``validate_customer_can_use_cod`` / ``cod_cap_reached`` in business_app/).
Every surface with a real HTTP endpoint is driven over that endpoint, not the
service method directly, so a regression in a route's own auth decorator or
body validation also fails this pin -- sites 1-4 have no route of their own
(they are internal service methods other guards delegate to) and are called
directly on purpose:

  1. CashCollectionService.is_customer_cod_restricted        (person SSOT)
  2. CashCollectionService.get_cod_restricted_flags          (batch, person)
  3. CashCollectionService.get_cod_restriction_context        (both arms)
  4. CashCollectionService.validate_customer_can_use_cod      (raises or not)
  5. GET  /api/v1/payments/methods                                        (customer menu, flag)
  6. GET  /api/v1/payments/methods                                        (customer menu, cash hidden)
  7. POST /api/v1/orders/                                                 (the write guard)
  8. CustomerMapService.get_customer_map_pins                            (admin map pin)
  9. GET  /api/v1/admin/users/<id>/payment-methods                        (staff/admin menu, flag)
 10. GET  /api/v1/admin/users/<id>/payment-methods                        (staff/admin menu, cash hidden)
 11. POST /api/v1/staff/operator/orders                                   (staff phone-order write guard)
 12. POST /api/v1/admin/orders/<id>/payment-method/preview                (switch-to-cash write guard)
 13. POST /api/v1/admin/orders/<id>/collected-cash/preview                (admin cash-correction warning)
 14. GET  /api/v1/admin/staff/cash-reconciliation/users/search            (cash-collection search dropdown)

Sites 9-14 were not in the plan's minimum list; they surfaced from the audit
and are exhaustiveness, not padding -- each is an independent expression that
could, in principle, re-derive the cap instead of delegating to it.

Deliberately NOT in this loop, with the reason recorded so a future reader
does not "fix" the gap:

* ``staff_service.py:_notify_customer_cod_debt_limit`` (the breach
  notification) asks an EDGE question ("did this delivery just close the
  rail?"), not a state question. It reaches the same SSOT via
  ``is_customer_cod_restricted`` / ``get_cod_restriction_context`` and is
  pinned by ``tests/unit/test_cod_breach_notification_exemptions.py``.
* ``staff_service.py``'s synthesised place-debtor row (~line 3121, the staff
  bot's driver-collect list, NOT the same as site 14's search dropdown) consumes
  ``get_cod_restricted_flags`` (site 2) directly for its own ``cod_restricted``
  key -- it is a consumer of an already-covered site, not an independent one.
* ``CashCollectionService.post_collection``'s PERSONAL_CARD_TRANSFER path is
  documented as EXEMPT from the cap by design (the debt it creates is settled
  in the same transaction) -- it is not a "restricted?" decision site at all.
* ``SubscriptionService.process_subscription_billing`` routes through
  ``OrderService.create_order`` -> the same ``_resolve_payment_method`` that
  backs site 7; it is wiring onto an already-covered site, not a copy.

Customer scenarios exercised: below both arms (clean customer); count arm met,
amount arm not (the case the owner's rule change exists for); both arms met;
amount arm met, count arm not; COD-exempt; grocery-store; and the PLACE arm
(a customer with zero personal debt, delivering to an office grouped with a
capped coworker -- restricted at every address-aware site, unrestricted at
every documented person-arm-only site, which is the correct, intentional
split, not a bug).
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Dict, Optional

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.order import Order
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.customer_map_service import CustomerMapService
from business_app.utils.exceptions import ValidationError
from shared.business_config import COD_ACTIVE_DEBT_LIMIT, COD_DEBT_AMOUNT_THRESHOLD
from shared.enums import CashCollectionSource, EntitySubtype, PaymentMethod, UserType
from tests.unit._scope_money_helpers import delivered_cod_order, make_address, make_place_group, make_user

CASH = "cash"
_CAP_WARNING = "correction_pushes_cod_over_cap"
_SWITCH_BLOCK = "cod_debt_limit_reached"

# Amounts derived from the SSOT config, never hardcoded, so this pin keeps
# meaning whatever COD_ACTIVE_DEBT_LIMIT / COD_DEBT_AMOUNT_THRESHOLD are set to.
_THRESHOLD = Decimal(COD_DEBT_AMOUNT_THRESHOLD)
_LIMIT = COD_ACTIVE_DEBT_LIMIT
# LIMIT debts of this size sum to threshold/2 -- comfortably UNDER the amount floor.
_UNDER_EACH = (_THRESHOLD / _LIMIT / 2).quantize(Decimal("0.01"))
# LIMIT debts of this size sum to threshold*2 -- comfortably OVER the amount floor.
_OVER_EACH = (_THRESHOLD / _LIMIT * 2).quantize(Decimal("0.01"))
# One debt of this size clears the amount floor on its own, regardless of LIMIT.
_HUGE_EACH = (_THRESHOLD * 5).quantize(Decimal("0.01"))
# One fewer than the count floor -- the count arm cannot fire no matter the amount.
_UNMET_COUNT = max(_LIMIT - 1, 0)


def _auth(app, user_id, role=None):
    claims = {"role": role} if role else None
    with app.app_context():
        token = create_access_token(identity=str(user_id), additional_claims=claims)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def orderer(db, sample_user, user_address):
    sample_user.phone_verified_at = datetime.now(UTC)
    db.session.commit()
    return sample_user


def _build_debts(db, user, address, count: int, each: Decimal) -> Optional[Order]:
    """``count`` open COD debts of ``each`` UZS, delivered to ``address``.

    Order 0 (when count >= 1) additionally carries a REAL, unvoided
    CashCollectionEvent (source DELIVERY_COMPLETION, amount 0) so site 13
    (OrderCashEditService.preview) has an event to resolve and correct. Its
    Payment row is otherwise identical to every other background debt built
    here -- the event changes nothing any other site reads -- so this does not
    perturb sites 1-12's counts or totals.

    Returns that order (the "event order"), or None when count == 0: a clean
    customer has nothing to preview a correction against, so site 13 is
    skipped for that scenario by the caller.
    """
    event_order: Optional[Order] = None
    for i in range(count):
        order, _payment = delivered_cod_order(db, user, address=address, total=each)
        if i == 0:
            event = CashCollectionService().post_collection(
                customer_id=user.id,
                amount=Decimal("0.00"),
                source="standalone_meeting",
                order_id=order.id,
                recorded_by_user_id=user.id,
                notes="no cash collected at door",
            )
            event.source = CashCollectionSource.DELIVERY_COMPLETION
            db.session.commit()
            event_order = order
    return event_order


def _every_site_says(
    app, client, db, user, product, address, headers, admin_user, operator_user, *, event_order: Optional[Order]
) -> Dict[str, bool]:
    """Ask every covered site the same question about ``user`` at ``address``;
    return {site_name: restricted?}. A caller asserts every value is equal."""
    svc = CashCollectionService()
    answers: Dict[str, bool] = {}

    # 1 -- person-arm SSOT, address-less.
    answers["is_customer_cod_restricted"] = svc.is_customer_cod_restricted(user.id)

    # 2 -- batched person-arm SSOT.
    answers["get_cod_restricted_flags"] = svc.get_cod_restricted_flags([user.id])[user.id]

    # 3 -- both arms, address-aware.
    ctx = svc.get_cod_restriction_context(user.id, delivery_address_id=address.id)
    answers["get_cod_restriction_context"] = ctx["cod_restricted"]

    # 4 -- the validator every other write guard below delegates to.
    try:
        svc.validate_customer_can_use_cod(user.id, delivery_address_id=address.id)
        answers["validate_customer_can_use_cod"] = False
    except ValidationError as exc:
        assert exc.error_code == "COD_DEBT_LIMIT_REACHED", exc
        answers["validate_customer_can_use_cod"] = True

    # 5/6 -- the customer's own payment-methods menu.
    methods = client.get(f"/api/v1/payments/methods?delivery_address_id={address.id}", headers=headers)
    assert methods.status_code == 200, methods.get_json()
    menu = methods.get_json()["data"]
    answers["payments_methods_restriction_flag"] = menu["payment_restrictions"]["cod_restricted"]
    answers["payments_methods_hides_cash"] = not any(m["method"] == CASH for m in menu["available_methods"])

    # 7 -- the write guard itself.
    created = client.post(
        "/api/v1/orders/",
        json={
            "items": [{"product_id": product.id, "quantity": 2}],
            "delivery_address_id": address.id,
            "payment_method": CASH,
        },
        headers=headers,
    )
    assert created.status_code in (201, 400), created.get_json()
    if created.status_code == 400:
        # Guard against a false-positive "restricted" reading caused by some
        # unrelated 400 (stock, zone, ...): the COD guard's message always
        # starts with this phrase (see validate_customer_can_use_cod).
        msg = (created.get_json() or {}).get("message") or ""
        assert "cash on delivery" in msg.lower(), created.get_json()
    answers["create_order_refuses_cash"] = created.status_code == 400

    # 8 -- the admin map pin (consumes get_cod_restricted_flags, address-less).
    pin = next(p for p in CustomerMapService.get_customer_map_pins() if p["user_id"] == user.id)
    answers["customer_map_pin"] = pin["cod_restricted"]

    # 9/10 -- the parallel staff/admin menu (StaffService.get_client_payment_methods,
    # shared by the operator phone-order screen and the admin order builder).
    admin_menu_resp = client.get(
        f"/api/v1/admin/users/{user.id}/payment-methods?delivery_address_id={address.id}",
        headers=_auth(app, admin_user.id, role="admin"),
    )
    assert admin_menu_resp.status_code == 200, admin_menu_resp.get_json()
    admin_menu = admin_menu_resp.get_json()["data"]
    answers["admin_menu_restriction_flag"] = admin_menu["payment_restrictions"]["cod_restricted"]
    answers["admin_menu_hides_cash"] = not any(m["method"] == CASH for m in admin_menu["available_methods"])

    # 11 -- the staff phone-order write guard: a second, independent
    # order-creation entry point from the customer-facing POST above. Driven
    # over its real HTTP route (not StaffService.create_phone_order directly)
    # so a regression in @require_staff_roles or the route's own body
    # validation would also make this fail.
    phone_order_resp = client.post(
        "/api/v1/staff/operator/orders",
        json={
            "client_id": user.id,
            "items": [{"product_id": product.id, "quantity": 2}],
            "delivery_address_id": address.id,
            "payment_method": CASH,
        },
        headers=_auth(app, operator_user.id),
    )
    assert phone_order_resp.status_code in (201, 400), phone_order_resp.get_json()
    if phone_order_resp.status_code == 400:
        assert phone_order_resp.get_json().get("error_code") == "COD_DEBT_LIMIT_REACHED", phone_order_resp.get_json()
    answers["staff_phone_order_refuses_cash"] = phone_order_resp.status_code == 400

    # 12 -- switching an EXISTING order onto cash mints a fresh COD obligation,
    # so it must clear the same cap. The target order carries ZERO balance of
    # its own -- it never perturbs any other site's count/total -- because the
    # guard it exercises (validate_customer_can_use_cod) reads only the
    # CUSTOMER's current state, never this order's. Driven over the real
    # admin route (not OrderPaymentMethodEditService.preview directly) so a
    # regression in @validate_admin_action or the route's body validation
    # would also make this fail.
    ba_order, ba_payment = delivered_cod_order(
        db, user, address=address, total=Decimal("100.00"), outstanding=Decimal("0.00")
    )
    ba_order.payment_method = PaymentMethod.BUSINESS_ACCOUNT
    ba_payment.payment_method = PaymentMethod.BUSINESS_ACCOUNT
    db.session.commit()
    switch_resp = client.post(
        f"/api/v1/admin/orders/{ba_order.id}/payment-method/preview",
        json={"new_method": "cash"},
        headers=_auth(app, admin_user.id, role="admin"),
    )
    assert switch_resp.status_code == 200, switch_resp.get_json()
    switch_plan = switch_resp.get_json()["data"]
    answers["order_payment_method_edit_blocks_switch_to_cash"] = any(
        r.startswith(_SWITCH_BLOCK) for r in switch_plan["blocking_reasons"]
    )

    # 13 -- the admin cash-correction preview's cap warning. A NO-OP preview
    # (new_amount == the 0 already recorded on event_order's event) leaves the
    # order's own outstanding untouched, so the preview's projected
    # count/total collapse to exactly TODAY's cluster count/total -- i.e. this
    # reduces to the same predicate as every other site here, not a forecast.
    # Skipped for the clean customer: there is no order to correct. Driven
    # over the real admin route, same reasoning as site 12.
    if event_order is not None:
        cash_resp = client.post(
            f"/api/v1/admin/orders/{event_order.id}/collected-cash/preview",
            json={"new_amount": "0.00"},
            headers=_auth(app, admin_user.id, role="admin"),
        )
        assert cash_resp.status_code == 200, cash_resp.get_json()
        cash_plan = cash_resp.get_json()["data"]
        answers["order_cash_edit_preview_warns_over_cap"] = any(
            w.startswith(_CAP_WARNING) for w in cash_plan["warnings"]
        )

    # 14 -- the admin/staff cash-collection search dropdown
    # (StaffService.search_customers_for_cod_collection), reached over its
    # real admin route. Same kind of read-only status display as the customer
    # map (site 8) -- historically the exact thing that drifts -- so it gets
    # the same HTTP-driven treatment. ``only_with_open_cod=false`` so the
    # clean-customer scenario still surfaces a row; the phone substring is
    # this user's full canonical number, which nothing else seeded in this
    # module can accidentally prefix-match.
    search_resp = client.get(
        f"/api/v1/admin/staff/cash-reconciliation/users/search"
        f"?q={user.phone}&type=phone&only_with_open_cod=false",
        headers=_auth(app, admin_user.id, role="admin"),
    )
    assert search_resp.status_code == 200, search_resp.get_json()
    search_row = next(i for i in search_resp.get_json()["data"]["items"] if i["id"] == user.id)
    answers["cod_collection_search_restriction_flag"] = search_row["cod_restricted"]

    return answers


@pytest.mark.integration
@pytest.mark.parametrize(
    "debt_count,each,expected",
    [
        (_LIMIT, _UNDER_EACH, False),  # count arm met, amount arm not -- the owner's rule-change case
        (_LIMIT, _OVER_EACH, True),  # both arms met
        (_UNMET_COUNT, _HUGE_EACH, False),  # amount arm met on its own, count arm not -- AND, not OR
        (0, Decimal("0.00"), False),  # clean customer -- below both arms
    ],
)
def test_all_call_sites_agree(
    app,
    db,
    client,
    orderer,
    user_address,
    sample_product,
    auth_headers,
    admin_user,
    operator_user,
    debt_count,
    each,
    expected,
):
    event_order = _build_debts(db, orderer, user_address, debt_count, each)

    answers = _every_site_says(
        app,
        client,
        db,
        orderer,
        sample_product,
        user_address,
        auth_headers,
        admin_user,
        operator_user,
        event_order=event_order,
    )

    assert set(answers.values()) == {expected}, answers


@pytest.mark.integration
def test_exempt_customer_is_unrestricted_everywhere(
    app, db, client, orderer, user_address, sample_product, auth_headers, admin_user, operator_user
):
    orderer.cod_debt_check_exempt = True
    db.session.commit()
    # Debts that would restrict any ordinary customer (both arms comfortably
    # met), so this proves the exemption actually FIRES rather than the
    # scenario coincidentally being unrestricted anyway.
    event_order = _build_debts(db, orderer, user_address, _LIMIT, _OVER_EACH)

    answers = _every_site_says(
        app,
        client,
        db,
        orderer,
        sample_product,
        user_address,
        auth_headers,
        admin_user,
        operator_user,
        event_order=event_order,
    )

    assert set(answers.values()) == {False}, answers


@pytest.mark.integration
def test_grocery_store_is_unrestricted_everywhere(
    app, db, client, orderer, user_address, sample_product, auth_headers, admin_user, operator_user
):
    orderer.user_type = UserType.ENTITY
    orderer.entity_subtype = EntitySubtype.GROCERY_STORE
    db.session.commit()
    # Same over-the-cap debt load as the exempt scenario: proves the
    # structural grocery-store exemption fires too, not just admin exemption.
    event_order = _build_debts(db, orderer, user_address, _LIMIT, _OVER_EACH)

    answers = _every_site_says(
        app,
        client,
        db,
        orderer,
        sample_product,
        user_address,
        auth_headers,
        admin_user,
        operator_user,
        event_order=event_order,
    )

    assert set(answers.values()) == {False}, answers


@pytest.mark.integration
def test_place_arm_restricts_address_aware_sites_only(
    app, db, client, orderer, user_address, sample_product, auth_headers, admin_user, operator_user
):
    """A customer with ZERO personal debt, whose delivery address is grouped
    with a coworker's capped office, must be refused cash AT THAT ADDRESS by
    every site that takes a delivery address -- and must still show
    unrestricted on every site documented as PERSON-arm-only
    (``is_customer_cod_restricted``, ``get_cod_restricted_flags``, the
    customer map). That split is not a bug: it is the exact boundary
    ``cash_collection_service.py`` draws ("callers that know the destination
    address must use get_cod_restriction_context / validate_customer_can_use_cod
    so the PLACE arm is evaluated too").

    Sites 12/13 (the two admin correction previews) are person-arm-scoped
    scenarios by construction elsewhere in this file (their target order's own
    delivery address IS the orderer's cluster address); place-arm coverage for
    each is pinned individually elsewhere instead of rebuilt here with a
    second, place-scoped target order:
      * site 12 (``OrderPaymentMethodEditService.preview``) by
        ``tests/unit/test_cod_cap_enforcement_points.py::TestSwitchToCashEnforcement::test_unwind_to_cash_blocked_by_place_arm``
      * site 13 (``OrderCashEditService.preview``) by
        ``tests/unit/test_corrections_frozen_scope.py::test_cap_warning_fires_on_the_PLACE_arm_with_the_cluster_under_the_limit``
    """
    coworker = make_user(db)
    coworker_address = make_address(db, coworker)
    make_place_group(db, coworker_address, user_address, label="capped-office")
    for _ in range(_LIMIT):
        delivered_cod_order(db, coworker, address=coworker_address, total=_OVER_EACH)

    # orderer needs SOME order history for the customer-map pin to exist at
    # all (its query INNER-joins on last_order); fully settled, so it adds
    # nothing to either arm's count or total.
    delivered_cod_order(db, orderer, address=user_address, total=Decimal("100.00"), outstanding=Decimal("0.00"))

    svc = CashCollectionService()

    person_arm_only = {
        "is_customer_cod_restricted": svc.is_customer_cod_restricted(orderer.id),
        "get_cod_restricted_flags": svc.get_cod_restricted_flags([orderer.id])[orderer.id],
        "customer_map_pin": next(
            p for p in CustomerMapService.get_customer_map_pins() if p["user_id"] == orderer.id
        )["cod_restricted"],
    }
    assert set(person_arm_only.values()) == {False}, person_arm_only

    place_arm_aware: Dict[str, bool] = {}

    ctx = svc.get_cod_restriction_context(orderer.id, delivery_address_id=user_address.id)
    assert ctx["restriction_scope"] == "place", ctx
    place_arm_aware["get_cod_restriction_context"] = ctx["cod_restricted"]

    try:
        svc.validate_customer_can_use_cod(orderer.id, delivery_address_id=user_address.id)
        place_arm_aware["validate_customer_can_use_cod"] = False
    except ValidationError as exc:
        assert exc.error_code == "COD_DEBT_LIMIT_REACHED", exc
        place_arm_aware["validate_customer_can_use_cod"] = True

    methods = client.get(f"/api/v1/payments/methods?delivery_address_id={user_address.id}", headers=auth_headers)
    assert methods.status_code == 200, methods.get_json()
    menu = methods.get_json()["data"]
    place_arm_aware["payments_methods_restriction_flag"] = menu["payment_restrictions"]["cod_restricted"]
    place_arm_aware["payments_methods_hides_cash"] = not any(m["method"] == CASH for m in menu["available_methods"])

    created = client.post(
        "/api/v1/orders/",
        json={
            "items": [{"product_id": sample_product.id, "quantity": 2}],
            "delivery_address_id": user_address.id,
            "payment_method": CASH,
        },
        headers=auth_headers,
    )
    assert created.status_code == 400, created.get_json()
    msg = (created.get_json() or {}).get("message") or ""
    assert "cash on delivery" in msg.lower(), created.get_json()
    place_arm_aware["create_order_refuses_cash"] = True

    admin_menu_resp = client.get(
        f"/api/v1/admin/users/{orderer.id}/payment-methods?delivery_address_id={user_address.id}",
        headers=_auth(app, admin_user.id, role="admin"),
    )
    assert admin_menu_resp.status_code == 200, admin_menu_resp.get_json()
    admin_menu = admin_menu_resp.get_json()["data"]
    place_arm_aware["admin_menu_restriction_flag"] = admin_menu["payment_restrictions"]["cod_restricted"]
    place_arm_aware["admin_menu_hides_cash"] = not any(m["method"] == CASH for m in admin_menu["available_methods"])

    phone_order_resp = client.post(
        "/api/v1/staff/operator/orders",
        json={
            "client_id": orderer.id,
            "items": [{"product_id": sample_product.id, "quantity": 2}],
            "delivery_address_id": user_address.id,
            "payment_method": CASH,
        },
        headers=_auth(app, operator_user.id),
    )
    assert phone_order_resp.status_code == 400, phone_order_resp.get_json()
    assert phone_order_resp.get_json().get("error_code") == "COD_DEBT_LIMIT_REACHED", phone_order_resp.get_json()
    place_arm_aware["staff_phone_order_refuses_cash"] = True

    assert set(place_arm_aware.values()) == {True}, place_arm_aware

    # site 14 (the cash-collection search dropdown) is address-less, like the
    # sites in ``person_arm_only`` above -- it must stay False here too.
    search_resp = client.get(
        f"/api/v1/admin/staff/cash-reconciliation/users/search"
        f"?q={orderer.phone}&type=phone&only_with_open_cod=false",
        headers=_auth(app, admin_user.id, role="admin"),
    )
    assert search_resp.status_code == 200, search_resp.get_json()
    search_row = next(i for i in search_resp.get_json()["data"]["items"] if i["id"] == orderer.id)
    assert search_row["cod_restricted"] is False, search_row
