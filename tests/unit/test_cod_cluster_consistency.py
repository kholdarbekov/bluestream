"""Plan 2b / Task 9 — cluster-consistency of COD *read* surfaces.

Nothing here moves money. These are the display / decision-support surfaces that
used to compute their own per-account answer to "is this customer COD
restricted / how many open COD debts do they have", and therefore contradicted
what ``get_cod_restriction_context`` actually enforces:

  * the debtor-list rows (admin ``list_users_with_open_cod_debts`` and the staff
    bot's ``paginate_users_with_open_cod_debts``),
  * the COD statement's ``active_cod_debt_count`` (which the staff-bot COD
    customer search filters on),
  * the admin customer-map pins,
  * the COD debt-limit breach notification fired on delivery.

Every surface must now give the CLUSTER answer for a linked customer, and a
byte-identical answer to before for an unlinked, non-exempt, non-grocery one.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app.services.cash_collection_service import CashCollectionService
from tests.unit._scope_money_helpers import (
    delivered_cod_order,
    link_users,
    make_address,
    make_user,
)

_LIMIT = CashCollectionService.COD_ACTIVE_DEBT_LIMIT


# ---------------------------------------------------------------------------
# 1. Debtor rows
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDebtorRowClusterFlag:
    def test_debtor_row_restricted_via_sibling_debt(self, db):
        u1, u2 = make_user(db), make_user(db)
        link_users(db, [u1, u2])
        delivered_cod_order(db, u1)
        delivered_cod_order(db, u2)
        svc = CashCollectionService()
        rows = svc.list_users_with_open_cod_debts()
        # Plan 2c collapses the cluster into ONE row; it carries the cluster's
        # summed count and the cluster restriction flag.
        cluster_rows = [r for r in rows if set(r["member_user_ids"]) == {u1.id, u2.id}]
        assert len(cluster_rows) == 1
        assert cluster_rows[0]["active_cod_debt_count"] == 2
        assert cluster_rows[0]["cod_restricted"] is True
        # ...while neither individual account is at the cap on its own: the
        # restriction is genuinely cluster-derived, not a per-account count.
        assert svc.get_active_cod_debt_count(u1.id) == 1
        assert svc.get_active_cod_debt_count(u2.id) == 1

    def test_exempt_debtor_row_not_restricted(self, db):
        u = make_user(db, exempt=True)
        delivered_cod_order(db, u)
        delivered_cod_order(db, u)
        rows = CashCollectionService().list_users_with_open_cod_debts()
        row = next(r for r in rows if r["id"] == u.id)
        assert row["cod_restricted"] is False  # was True pre-2b (raw count)

    def test_grocery_debtor_row_not_restricted(self, db):
        u = make_user(db, grocery=True)
        delivered_cod_order(db, u)
        delivered_cod_order(db, u)
        rows = CashCollectionService().list_users_with_open_cod_debts()
        row = next(r for r in rows if r["id"] == u.id)
        assert row["cod_restricted"] is False  # was True pre-2b (raw count)

    def test_unlinked_debtor_rows_unchanged(self, db):
        """Regression: an unlinked, non-exempt customer sees exactly the old row."""
        under, at_cap = make_user(db), make_user(db)
        delivered_cod_order(db, under)
        for _ in range(_LIMIT):
            delivered_cod_order(db, at_cap)

        rows = {r["id"]: r for r in CashCollectionService().list_users_with_open_cod_debts()}
        assert rows[under.id]["active_cod_debt_count"] == 1
        assert rows[under.id]["cod_restricted"] is False
        assert rows[at_cap.id]["active_cod_debt_count"] == _LIMIT
        assert rows[at_cap.id]["cod_restricted"] is True
        # Row shape parity with the admin list serialization: every pre-2c key
        # survives, plus the three additive cluster keys, which degrade to a
        # singleton for an unlinked account.
        assert set(rows[under.id]) == {
            "id", "first_name", "last_name", "phone", "role", "user_type",
            "active_cod_debt_count", "total_outstanding_amount", "cod_restricted",
            "row_type", "cluster_member_count", "member_user_ids",
        }
        assert rows[under.id]["row_type"] == "person"
        assert rows[under.id]["cluster_member_count"] == 1
        assert rows[under.id]["member_user_ids"] == [under.id]

    def test_paginated_debtor_rows_use_the_cluster_flag(self, db):
        u1, u2 = make_user(db), make_user(db)
        link_users(db, [u1, u2])
        delivered_cod_order(db, u1)
        delivered_cod_order(db, u2)
        page = CashCollectionService().paginate_users_with_open_cod_debts(page=1, per_page=50)
        # One collapsed row for the pair (plan 2c), carrying the cluster figures.
        cluster_rows = [r for r in page["items"] if set(r.get("member_user_ids", [])) == {u1.id, u2.id}]
        assert len(cluster_rows) == 1
        assert cluster_rows[0]["active_cod_debt_count"] == 2
        assert cluster_rows[0]["cod_restricted"] is True
        # No place group exists here, so the list carries person rows only.
        assert {r["row_type"] for r in page["items"]} == {"person"}

    def test_paginated_debtor_rows_unchanged_for_unlinked(self, db):
        solo = make_user(db)
        delivered_cod_order(db, solo)
        page = CashCollectionService().paginate_users_with_open_cod_debts(page=1, per_page=50)
        row = next(r for r in page["items"] if r["id"] == solo.id)
        assert row["active_cod_debt_count"] == 1
        assert row["cod_restricted"] is False


# ---------------------------------------------------------------------------
# 2. COD statement
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStatementClusterCount:
    def test_statement_count_is_cluster_wide(self, db):
        u1, u2 = make_user(db), make_user(db)
        link_users(db, [u1, u2])
        delivered_cod_order(db, u1)
        statement = CashCollectionService().get_customer_cod_statement(u2.id)
        # u2 has 0 own debts but the cluster owes 1 — search only_with_open_cod
        # keys on this figure.
        assert statement["active_cod_debt_count"] == 1
        # Known and deliberate asymmetry: `items` / the money totals stay
        # per-account (Phase 2 keeps the ledger per-account), so a linked
        # sibling reports a cluster count against an empty item list.
        assert statement["items"] == []
        assert statement["total_outstanding_amount"] == 0.0

    def test_statement_count_unchanged_for_unlinked(self, db):
        solo = make_user(db)
        delivered_cod_order(db, solo)
        delivered_cod_order(db, solo)
        svc = CashCollectionService()
        statement = svc.get_customer_cod_statement(solo.id)
        assert statement["active_cod_debt_count"] == svc.get_active_cod_debt_count(solo.id) == 2
        assert statement["cod_restricted"] is True

    def test_statement_zero_for_debt_free_unlinked_customer(self, db):
        solo = make_user(db)
        statement = CashCollectionService().get_customer_cod_statement(solo.id)
        assert statement["active_cod_debt_count"] == 0
        assert statement["cod_restricted"] is False


# ---------------------------------------------------------------------------
# 3. Staff COD customer search (rides on the statement figure)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCodSearchClusterAwareness:
    def test_search_surfaces_linked_sibling_with_no_own_debt(self, db):
        from business_app.services.staff_service import StaffService

        u1, u2 = make_user(db), make_user(db)
        link_users(db, [u1, u2])
        delivered_cod_order(db, u1)
        items = StaffService.search_customers_for_cod_collection(
            u2.phone, "phone", only_with_open_cod=True
        )
        assert [i["id"] for i in items] == [u2.id]
        assert items[0]["active_cod_debt_count"] == 1
        # …and the row now carries the CLUSTER's debt, not u2's empty account.
        #
        # CHANGED DELIBERATELY (A6/R-B, admin collect-scope P0). This used to
        # assert 0.0, on the reasoning that "the driver is meant to find the
        # person, not the row". But this search feeds the ADMIN cash-collection
        # modal's customer dropdown, and collecting from u2 settles the whole
        # cluster's delivered COD debt — so a 0 here was a row advertising a
        # figure that describes none of what its own collection settles. That
        # split (a number for the human, a scope for the engine) is the defect
        # this whole seam exists to prevent; the row is now
        # `collect_scope.amount`, the same value the modal displays and posts.
        assert items[0]["total_outstanding_amount"] == 15000.0

    def test_search_still_filters_out_debt_free_unlinked_customer(self, db):
        from business_app.services.staff_service import StaffService

        solo = make_user(db)
        items = StaffService.search_customers_for_cod_collection(
            solo.phone, "phone", only_with_open_cod=True
        )
        assert items == []


# ---------------------------------------------------------------------------
# 4. Customer map pins
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCustomerMapClusterFlag:
    def test_map_pin_restricted_via_sibling(self, db):
        from business_app.services.customer_map_service import CustomerMapService

        u1, u2 = make_user(db), make_user(db)
        link_users(db, [u1, u2])
        a1 = make_address(db, u1)
        delivered_cod_order(db, u1, address=a1)
        delivered_cod_order(db, u2)
        pins = CustomerMapService.get_customer_map_pins()
        pin = next(p for p in pins if p["user_id"] == u1.id)
        assert pin["active_cod_debt_count"] == 1  # per-address/per-account slice
        assert pin["cod_restricted"] is True

    def test_map_pin_unchanged_for_unlinked(self, db):
        from business_app.services.customer_map_service import CustomerMapService

        under, at_cap = make_user(db), make_user(db)
        a_under, a_cap = make_address(db, under), make_address(db, at_cap)
        delivered_cod_order(db, under, address=a_under)
        for _ in range(_LIMIT):
            delivered_cod_order(db, at_cap, address=a_cap)

        pins = {p["user_id"]: p for p in CustomerMapService.get_customer_map_pins()}
        assert pins[under.id]["active_cod_debt_count"] == 1
        assert pins[under.id]["cod_restricted"] is False
        assert pins[at_cap.id]["active_cod_debt_count"] == _LIMIT
        assert pins[at_cap.id]["cod_restricted"] is True

    def test_map_pin_honours_exemption(self, db):
        from business_app.services.customer_map_service import CustomerMapService

        u = make_user(db, exempt=True)
        a = make_address(db, u)
        for _ in range(_LIMIT):
            delivered_cod_order(db, u, address=a)
        pin = next(p for p in CustomerMapService.get_customer_map_pins() if p["user_id"] == u.id)
        assert pin["cod_restricted"] is False


# ---------------------------------------------------------------------------
# 5. The batched flag helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBatchedFlags:
    def test_get_cod_restricted_flags_matches_single_calls(self, db):
        u1, u2, u3 = make_user(db), make_user(db), make_user(db, exempt=True)
        link_users(db, [u1, u2])
        delivered_cod_order(db, u1)
        delivered_cod_order(db, u2)
        delivered_cod_order(db, u3)
        svc = CashCollectionService()
        flags = svc.get_cod_restricted_flags([u1.id, u2.id, u3.id])
        for uid in (u1.id, u2.id, u3.id):
            assert flags[uid] == svc.is_customer_cod_restricted(uid)

    def test_get_cod_restricted_flags_matches_single_calls_grocery_and_debt_free(self, db):
        grocer, clean, at_cap = make_user(db, grocery=True), make_user(db), make_user(db)
        for _ in range(_LIMIT):
            delivered_cod_order(db, grocer)
            delivered_cod_order(db, at_cap)
        svc = CashCollectionService()
        ids = [grocer.id, clean.id, at_cap.id]
        flags = svc.get_cod_restricted_flags(ids)
        assert flags == {uid: svc.is_customer_cod_restricted(uid) for uid in ids}
        assert flags[grocer.id] is False
        assert flags[clean.id] is False
        assert flags[at_cap.id] is True

    def test_get_cod_restricted_flags_empty_input(self, db):
        assert CashCollectionService().get_cod_restricted_flags([]) == {}

    def test_get_cod_restricted_flags_unknown_user_is_not_restricted(self, db):
        svc = CashCollectionService()
        assert svc.get_cod_restricted_flags([987654321]) == {987654321: False}

    def test_get_cod_restricted_flags_is_bounded_not_n_plus_one(self, db, count_queries):
        """The list surfaces it feeds (unpaginated customer map, 200-row admin
        debtor list) must not turn one query into thousands."""
        users = [make_user(db) for _ in range(6)]
        for u in users:
            delivered_cod_order(db, u)
        db.session.commit()
        # Read the ids *outside* the counter: touching an expired instance would
        # otherwise charge a per-user refresh SELECT to the helper.
        user_ids = [u.id for u in users]

        svc = CashCollectionService()
        with count_queries() as counter:
            flags = svc.get_cod_restricted_flags(user_ids)

        assert set(flags) == set(user_ids)
        assert counter.count <= 4, f"expected a bounded batch, issued {counter.count} queries"


# ---------------------------------------------------------------------------
# 6. COD debt-limit breach notification
# ---------------------------------------------------------------------------


def _cash_delivery(db, order, driver, *, address):
    from business_app.models.delivery import Delivery, DeliveryPerson
    from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod
    from datetime import UTC, datetime

    profile = DeliveryPerson(
        user_id=driver.id,
        full_name="Cluster Driver",
        phone=driver.phone,
        email=driver.email,
        is_active=True,
        is_available=True,
    )
    db.session.add(profile)

    order.payment_method = PaymentMethod.CASH
    order.status = OrderStatus.OUT_FOR_DELIVERY
    order.delivery_address_id = address.id
    db.session.flush()
    CashCollectionService().ensure_cod_payment_for_order(order)

    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver.id,
        status=DeliveryStatus.ARRIVED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _deliver_with_no_cash(delivery, driver):
    from business_app.services.staff_service import StaffService

    with patch("business_app.tasks.notification_tasks.send_delivery_update_task.delay"), patch(
        "business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"
    ), patch.object(StaffService, "_notify_customer_cod_debt_limit") as notify:
        StaffService.update_delivery_status(
            delivery_id=delivery.id,
            new_status="delivered",
            staff_user_id=driver.id,
            metadata={"cash_collected": "0.00", "notes": "Customer will pay later"},
        )
    return notify


@pytest.mark.unit
class TestBreachNotification:
    def test_breach_fires_when_a_sibling_debt_completes_the_cluster_cap(
        self, db, sample_order, sample_user, delivery_driver
    ):
        sibling = make_user(db)
        link_users(db, [sample_user, sibling])
        for _ in range(_LIMIT - 1):
            delivered_cod_order(db, sibling)
        address = make_address(db, sample_user)

        delivery = _cash_delivery(db, sample_order, delivery_driver, address=address)
        notify = _deliver_with_no_cash(delivery, delivery_driver)

        # Pre-count (cluster) = LIMIT-1, post = LIMIT -> the cap was just reached.
        # The old per-account count saw 0 -> 1 and stayed silent.
        notify.assert_called_once_with(sample_user.id)

    def test_breach_still_fires_for_an_unlinked_customer(
        self, db, sample_order, sample_user, delivery_driver
    ):
        for _ in range(_LIMIT - 1):
            delivered_cod_order(db, sample_user)
        address = make_address(db, sample_user)

        delivery = _cash_delivery(db, sample_order, delivery_driver, address=address)
        notify = _deliver_with_no_cash(delivery, delivery_driver)

        notify.assert_called_once_with(sample_user.id)

    def test_no_breach_when_the_cap_is_not_reached(
        self, db, sample_order, sample_user, delivery_driver
    ):
        address = make_address(db, sample_user)
        delivery = _cash_delivery(db, sample_order, delivery_driver, address=address)
        notify = _deliver_with_no_cash(delivery, delivery_driver)

        # 0 -> 1 open debts: below the cap, so no warning.
        notify.assert_not_called()

    def test_notification_copy_states_the_actionable_balance_not_a_bare_count(
        self, db, sample_user
    ):
        """Task 30A: the count alone is not actionable — the customer cannot
        reduce "2 debts" directly, only the money. The message must now name
        the actual NET total and the live threshold, both read from the SSOT
        / the real restriction context, never a hardcoded figure. A debt this
        far over the floor clears it regardless of the configured value."""
        from business_app.services.staff_service import StaffService
        from shared.business_config import COD_DEBT_AMOUNT_THRESHOLD

        sample_user.telegram_id = 424242
        db.session.commit()

        each = Decimal(COD_DEBT_AMOUNT_THRESHOLD) + Decimal("1000.00")
        for _ in range(_LIMIT):
            delivered_cod_order(db, sample_user, total=each)
        expected_total = each * _LIMIT

        with patch(
            "business_app.services.notification_service.NotificationService.send_notification"
        ) as send:
            StaffService._notify_customer_cod_debt_limit(sample_user.id)

        template = send.call_args.kwargs["template_override"]
        assert f"{expected_total:,.0f}" in template.content
        assert f"{COD_DEBT_AMOUNT_THRESHOLD:,}" in template.content
        assert template.content == template.get_translated("content", "en")
        assert "You have 2 outstanding cash on delivery debts." not in template.content
        assert f"You have {_LIMIT} or more outstanding cash on delivery debts." not in template.content

    def test_notification_says_nothing_when_the_amount_arm_is_not_met(self, db, sample_user):
        """A count-only breach (Uzbek-banknote shortfalls under the floor) must
        never fire this notification — the engine does not restrict it, so
        claiming a restriction here would be exactly the false threshold the
        comment above this method warns against."""
        from business_app.services.staff_service import StaffService

        sample_user.telegram_id = 424242
        db.session.commit()

        for _ in range(_LIMIT):
            delivered_cod_order(db, sample_user, total=Decimal("280.00"))

        with patch(
            "business_app.services.notification_service.NotificationService.send_notification"
        ) as send:
            StaffService._notify_customer_cod_debt_limit(sample_user.id)

        send.assert_not_called()


@pytest.mark.unit
def test_debt_free_cluster_flag_is_false(db):
    """Sanity: linking two debt-free accounts restricts neither."""
    u1, u2 = make_user(db), make_user(db)
    link_users(db, [u1, u2])
    svc = CashCollectionService()
    assert svc.get_cod_restricted_flags([u1.id, u2.id]) == {u1.id: False, u2.id: False}
    assert Decimal(str(svc.get_customer_prepaid_balance(u1.id))) == Decimal("0.00")
