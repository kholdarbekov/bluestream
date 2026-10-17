"""Analytics "Inactive customers" reads the shared delivery rule (spec §9.3, D9; plan Task 24a).

Rows stay per user with per-user totals, but "last delivery" is the user's CLUSTER's latest
delivered instant (the DELIVERED history row, never `Order.created_at`), and days are local
Tashkent days. Driven through the route the Analytics tab calls, with the query string it sends.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from business_app import db as app_db
from business_app.models.customer_link import CanonicalCustomer
from business_app.models.user import User
from business_app.services.analytics_service import AnalyticsService
from business_app.services.reorder_reminders.facts import customer_key
from shared.enums import EntitySubtype, OrderStatus, UserRole, UserStatus, UserType
from tests.integration.reorder_reminder_builders import (
    NO_ADDRESS,
    WORKLIST_NOW,
    delivered_order,
    legacy_delivered_order,
    link,
    make_customer,
    set_status,
)
from tests.integration.reorder_reminder_builders import store_fixture  # noqa: F401  (the `store` fixture)
from tests.integration.sales_pay_builders import freeze_local_now

pytestmark = pytest.mark.integration

INACTIVE = "/api/v1/admin/analytics/inactive-customers"
UTC = timezone.utc


def _response(client, headers, **params):
    response = client.get(INACTIVE, query_string=params, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def _inactive(client, headers, **params):
    # `paginated_response` publishes `{"data": {"items": [...]}, "meta": {...}}`.
    return {item["user_id"]: item for item in _response(client, headers, **params)["data"]["items"]}


def test_a_siblings_recent_delivery_keeps_the_whole_cluster_active(client, db, monkeypatch, manager_claim_headers):
    freeze_local_now(monkeypatch, WORKLIST_NOW)
    primary = make_customer(db, first_name="Ulugbek")
    sibling = make_customer(db, first_name="Umid")
    link(db, primary, sibling)
    delivered_order(db, primary, datetime(2026, 8, 1, 7, 0, tzinfo=UTC))
    delivered_order(db, sibling, datetime(2026, 10, 10, 7, 0, tzinfo=UTC))

    items = _inactive(client, manager_claim_headers, days_since=30, include_never_ordered="false")

    assert primary.id not in items and sibling.id not in items


def test_a_lapsed_cluster_lists_every_member_with_their_own_totals_and_the_clusters_last_delivery(
    client, db, monkeypatch, manager_claim_headers
):
    freeze_local_now(monkeypatch, WORKLIST_NOW)
    primary = make_customer(db, first_name="Ulugbek")
    sibling = make_customer(db, first_name="Umid")
    link(db, primary, sibling)
    delivered_order(db, primary, datetime(2026, 7, 1, 7, 0, tzinfo=UTC))
    delivered_order(db, sibling, datetime(2026, 8, 20, 7, 0, tzinfo=UTC))
    delivered_order(db, sibling, datetime(2026, 8, 25, 7, 0, tzinfo=UTC))

    items = _inactive(client, manager_claim_headers, days_since=30, include_never_ordered="false")

    for member in (primary, sibling):
        assert items[member.id]["last_order_date"] == "2026-08-25T07:00:00+00:00"
        assert items[member.id]["days_since_last_order"] == 51  # 25 Aug -> 15 Oct, local days
        assert items[member.id]["never_ordered"] is False
    assert (items[primary.id]["total_orders"], items[primary.id]["total_spent"]) == (1, 15000.0)
    assert (items[sibling.id]["total_orders"], items[sibling.id]["total_spent"]) == (2, 30000.0)


def test_days_are_local_and_the_instant_is_the_delivery_not_the_order(client, db, monkeypatch, manager_claim_headers):
    freeze_local_now(monkeypatch, WORKLIST_NOW)  # 15 Oct 10:00 local
    late_evening = make_customer(db, first_name="Vali")
    after_midnight = make_customer(db, first_name="Zafar")
    ordered_long_ago = make_customer(db, first_name="Yulduz")
    delivered_order(db, late_evening, datetime(2026, 9, 14, 18, 30, tzinfo=UTC))  # 14 Sep 23:30 local
    delivered_order(db, after_midnight, datetime(2026, 9, 14, 19, 30, tzinfo=UTC))  # 15 Sep 00:30 local
    # Placed in July, delivered on 5 October: the delivery instant counts, never the creation date.
    long_wait = delivered_order(db, ordered_long_ago, datetime(2026, 10, 5, 7, 0, tzinfo=UTC))
    long_wait.created_at = datetime(2026, 7, 1, 7, 0, tzinfo=UTC)
    db.session.commit()

    items = _inactive(client, manager_claim_headers, days_since=31, include_never_ordered="false")

    assert late_evening.id in items
    assert (items[late_evening.id]["days_since_last_order"], items[late_evening.id]["last_order_date"]) == (
        31,
        "2026-09-14T18:30:00+00:00",
    )
    assert items[late_evening.id]["total_orders"] == 1
    assert after_midnight.id not in items  # 30 local days
    assert ordered_long_ago.id not in items  # delivered 10 days ago, whatever its creation date


def test_a_legacy_delivered_order_with_no_history_row_counts_as_never_delivered(client, db, monkeypatch, manager_claim_headers):
    freeze_local_now(monkeypatch, WORKLIST_NOW)
    legacy = make_customer(db, first_name="Xurshid")
    # DELIVERED with no DELIVERED history row: the legacy shape dev still holds (Review Focus #2).
    legacy_delivered_order(db, legacy, datetime(2026, 9, 30, 7, 0, tzinfo=UTC))

    with_never = _inactive(client, manager_claim_headers, days_since=30, include_never_ordered="true")
    without_never = _inactive(client, manager_claim_headers, days_since=30, include_never_ordered="false")

    assert with_never[legacy.id]["never_ordered"] is True
    assert with_never[legacy.id]["total_orders"] == 1
    assert with_never[legacy.id]["last_order_date"] is None
    assert legacy.id not in without_never


def test_a_legacy_order_beside_a_real_delivery_does_not_move_the_last_delivery(client, db, monkeypatch, manager_claim_headers):
    freeze_local_now(monkeypatch, WORKLIST_NOW)
    customer = make_customer(db, first_name="Sardor")
    delivered_order(db, customer, datetime(2026, 8, 1, 7, 0, tzinfo=UTC))
    legacy_delivered_order(db, customer, datetime(2026, 10, 12, 7, 0, tzinfo=UTC))  # no delivered instant

    items = _inactive(client, manager_claim_headers, days_since=30, include_never_ordered="false")

    assert items[customer.id]["last_order_date"] == "2026-08-01T07:00:00+00:00"
    assert items[customer.id]["total_orders"] == 2


def test_a_never_delivered_customer_is_measured_from_sign_up_in_local_days(client, db, monkeypatch, manager_claim_headers):
    freeze_local_now(monkeypatch, WORKLIST_NOW)
    old_signup = make_customer(db, first_name="Dilnoza", created_at=datetime(2026, 9, 1, 7, 0, tzinfo=UTC))
    recent_signup = make_customer(db, first_name="Eldor", created_at=datetime(2026, 10, 10, 7, 0, tzinfo=UTC))

    items = _inactive(client, manager_claim_headers, days_since=30, include_never_ordered="true")

    assert items[old_signup.id]["never_ordered"] is True
    assert items[old_signup.id]["days_since_last_order"] == 44  # 1 Sep -> 15 Oct
    assert recent_signup.id not in items


def test_an_order_that_is_no_longer_delivered_does_not_keep_the_customer_active(client, db, monkeypatch, manager_claim_headers):
    freeze_local_now(monkeypatch, WORKLIST_NOW)
    customer = make_customer(db, first_name="Botir")
    delivered_order(db, customer, datetime(2026, 8, 1, 7, 0, tzinfo=UTC))
    reverted = delivered_order(db, customer, datetime(2026, 10, 12, 7, 0, tzinfo=UTC))
    set_status(db, reverted, OrderStatus.CANCELLED)  # the DELIVERED history row stays; the status moved on

    items = _inactive(client, manager_claim_headers, days_since=30, include_never_ordered="false")

    assert items[customer.id]["last_order_date"] == "2026-08-01T07:00:00+00:00"
    assert items[customer.id]["total_orders"] == 1


def test_only_active_customers_are_listed(client, db, monkeypatch, manager_claim_headers):
    freeze_local_now(monkeypatch, WORKLIST_NOW)
    active = make_customer(db, first_name="Active")
    inactive = make_customer(db, first_name="Inactive", status=UserStatus.INACTIVE)
    staff = make_customer(db, first_name="Driver", role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)
    for user in (active, inactive, staff):
        delivered_order(db, user, datetime(2026, 8, 1, 7, 0, tzinfo=UTC))

    items = _inactive(client, manager_claim_headers, days_since=30, include_never_ordered="false")

    assert active.id in items
    assert inactive.id not in items and staff.id not in items


@pytest.mark.parametrize(
    "customer_type, expected",
    [
        ("all", {"individual", "workplace", "grocery"}),
        ("individual", {"individual"}),
        ("workplace", {"workplace"}),
        ("grocery", {"grocery"}),
    ],
)
def test_every_customer_type_the_tab_offers_filters_the_list(
    client, db, monkeypatch, admin_claim_headers, customer_type, expected
):
    freeze_local_now(monkeypatch, WORKLIST_NOW)
    people = {
        "individual": make_customer(db, first_name="Person"),
        "workplace": make_customer(
            db, first_name="Office", user_type=UserType.ENTITY, entity_subtype=EntitySubtype.WORKPLACE, company_name="Office LLC"
        ),
        "grocery": make_customer(
            db, first_name="Shop", user_type=UserType.ENTITY, entity_subtype=EntitySubtype.GROCERY_STORE, company_name="Shop LLC"
        ),
    }
    for user in people.values():
        delivered_order(db, user, datetime(2026, 8, 1, 7, 0, tzinfo=UTC))

    items = _inactive(client, admin_claim_headers, days_since=30, customer_type=customer_type, include_never_ordered="false")

    assert {name for name, user in people.items() if user.id in items} == expected


def test_the_list_is_most_inactive_first_and_pages_without_losing_or_repeating_a_row(
    client, db, monkeypatch, manager_claim_headers
):
    freeze_local_now(monkeypatch, WORKLIST_NOW)
    ordered_ids = []
    for index in range(5):  # the oldest delivery first
        customer = make_customer(db, first_name=f"Page{index}")
        delivered_order(db, customer, datetime(2026, 6, 1, 7, 0, tzinfo=UTC) + timedelta(days=10 * index))
        ordered_ids.append(customer.id)

    pages = [
        _response(client, manager_claim_headers, days_since=30, include_never_ordered="false", page=page, per_page=2)
        for page in (1, 2, 3)
    ]

    assert [body["meta"]["total"] for body in pages] == [5, 5, 5]
    assert [[item["user_id"] for item in body["data"]["items"]] for body in pages] == [
        ordered_ids[0:2],
        ordered_ids[2:4],
        ordered_ids[4:5],
    ]


def test_an_unlinked_account_never_joins_a_cluster_whose_canonical_id_equals_its_user_id(
    client, db, monkeypatch, manager_claim_headers
):
    freeze_local_now(monkeypatch, WORKLIST_NOW)
    solo = make_customer(db, first_name="Solo")
    linked_a = make_customer(db, first_name="LinkA")
    linked_b = make_customer(db, first_name="LinkB")
    # The collision the key's prefix exists for: canonical id == the unlinked account's user id.
    db.session.add(CanonicalCustomer(id=solo.id, primary_user_id=linked_a.id))
    db.session.flush()
    for member in (linked_a, linked_b):
        member.canonical_customer_id = solo.id
    db.session.commit()
    delivered_order(db, solo, datetime(2026, 10, 10, 7, 0, tzinfo=UTC))
    delivered_order(db, linked_a, datetime(2026, 8, 1, 7, 0, tzinfo=UTC))

    items = _inactive(client, manager_claim_headers, days_since=30, include_never_ordered="false")

    assert solo.id not in items  # delivered five days ago
    assert linked_a.id in items  # its cluster (A and B) last took water on 1 August


def test_customer_key_sql_is_the_sql_twin_of_customer_key(store):
    """Pinned on SQLite and on Postgres: the `||` spelling, the CAST and the prefixes."""
    from business_app.services.reorder_reminders.facts import customer_key_sql

    solo = make_customer(store, first_name="Solo")
    linked_a = make_customer(store, first_name="LinkA")
    linked_b = make_customer(store, first_name="LinkB")
    link(store, linked_a, linked_b)
    users = [solo, linked_a, linked_b]

    rows = store.session.query(User.id, customer_key_sql(User)).filter(User.id.in_([user.id for user in users])).all()

    assert dict(rows) == {user.id: customer_key(user.id, user.canonical_customer_id) for user in users}


def test_the_tab_costs_the_same_statements_whatever_the_number_of_customers(
    client, db, monkeypatch, manager_claim_headers, count_queries
):
    """No N+1: the delivery history is read once per call, in the one statement that filters, sorts and pages."""
    freeze_local_now(monkeypatch, WORKLIST_NOW)

    def lapsed_customers(prefix: str, how_many: int) -> None:
        for index in range(how_many):
            first = make_customer(db, first_name=f"{prefix}A{index}")
            second = make_customer(db, first_name=f"{prefix}B{index}")
            link(db, first, second)
            delivered_order(db, first, datetime(2026, 7, 1, 7, 0, tzinfo=UTC))
            delivered_order(db, second, datetime(2026, 8, 1, 7, 0, tzinfo=UTC))

    def statements(**params):
        """`(SQL statements the request issued, total rows the tab reports)`."""
        with count_queries() as counter:
            body = _response(client, manager_claim_headers, per_page=100, **params)
        return counter.count, body["meta"]["total"]

    lapsed_customers("Small", 2)
    small_statements, small_total = statements(days_since=30, include_never_ordered="false")
    lapsed_customers("Large", 12)
    large_statements, large_total = statements(days_since=30, include_never_ordered="false")

    assert (small_total, large_total) == (4, 28)
    assert large_statements == small_statements
    assert large_statements <= 8  # auth + role check + count + page: a handful, never one per customer


def test_the_service_keeps_its_signature_and_item_keys(db, monkeypatch):
    freeze_local_now(monkeypatch, WORKLIST_NOW)
    customer = make_customer(db, first_name="Keys")
    delivered_order(db, customer, datetime(2026, 8, 1, 7, 0, tzinfo=UTC))

    result = AnalyticsService().get_inactive_customers(
        days_since=30, customer_type="all", include_never_ordered=False, page=1, per_page=50
    )

    assert set(result) == {"items", "total", "threshold_days"}
    assert result["threshold_days"] == 30
    assert set(result["items"][0]) == {
        "user_id",
        "full_name",
        "phone",
        "customer_type",
        "entity_subtype",
        "total_orders",
        "total_spent",
        "last_order_date",
        "days_since_last_order",
        "never_ordered",
    }


def test_the_one_statement_answers_the_same_on_sqlite_and_on_postgres(store, monkeypatch):
    """The key's `||` concatenation, the GROUP BY on it and the join on it are real SQL on Postgres."""
    freeze_local_now(monkeypatch, WORKLIST_NOW)
    primary = make_customer(store, first_name="Ulugbek")
    sibling = make_customer(store, first_name="Umid")
    link(store, primary, sibling)
    solo = make_customer(store, first_name="Solo")
    lapsed_solo = make_customer(store, first_name="Lapsed")
    delivered_order(store, primary, datetime(2026, 7, 1, 7, 0, tzinfo=UTC))
    delivered_order(store, sibling, datetime(2026, 10, 10, 7, 0, tzinfo=UTC))  # the cluster is active
    delivered_order(store, solo, datetime(2026, 10, 12, 7, 0, tzinfo=UTC))
    delivered_order(store, lapsed_solo, datetime(2026, 9, 14, 18, 30, tzinfo=UTC))  # 31 local days

    result = AnalyticsService().get_inactive_customers(days_since=31, include_never_ordered=False)

    assert [(item["user_id"], item["days_since_last_order"], item["last_order_date"]) for item in result["items"]] == [
        (lapsed_solo.id, 31, "2026-09-14T18:30:00+00:00")
    ]
    assert result["total"] == 1


@contextmanager
def _captured_statements():
    """Every `(statement, parameters)` the app's engine executes inside the block."""
    captured = []

    def grab(conn, cursor, statement, parameters, context, executemany):
        captured.append((statement, parameters))

    event.listen(app_db.engine, "before_cursor_execute", grab)
    try:
        yield captured
    finally:
        event.remove(app_db.engine, "before_cursor_execute", grab)


def _page_statement(captured):
    """The one SELECT that carries the page (it has the LIMIT and reads the delivery history)."""
    pages = [
        (statement, parameters)
        for statement, parameters in captured
        if statement.lstrip().upper().startswith("SELECT")
        and " LIMIT " in statement.upper()
        and "order_status_history" in statement
    ]
    assert len(pages) == 1, [statement for statement, _ in captured]
    return pages[0]


def _lapsed_clusters(store, how_many: int) -> None:
    for index in range(how_many):
        first = make_customer(store, first_name=f"PinA{index}")
        second = make_customer(store, first_name=f"PinB{index}")
        link(store, first, second)
        delivered_order(store, first, datetime(2026, 7, 1, 7, 0, tzinfo=UTC))
        delivered_order(store, second, datetime(2026, 8, 1, 7, 0, tzinfo=UTC))


def test_the_page_statement_reads_the_delivery_history_in_one_derived_table(store, monkeypatch):
    """Structural pin the statement count cannot give: a correlated per-row subquery is still ONE
    statement, but it repeats the history aggregate once per use (column, WHERE, ORDER BY), and
    re-runs it per user row. One `FROM order_status_history` means one aggregate, joined once."""
    freeze_local_now(monkeypatch, WORKLIST_NOW)
    _lapsed_clusters(store, 3)

    with _captured_statements() as captured:
        result = AnalyticsService().get_inactive_customers(days_since=30, include_never_ordered=False)

    statement, _ = _page_statement(captured)
    assert result["total"] == 6
    assert statement.count("FROM order_status_history") == 1


def test_postgres_plans_the_page_statement_without_a_per_row_subplan(pg_db, monkeypatch):
    """The planner's own answer: a correlated scalar subquery shows up as a `SubPlan` node that is
    re-run for every user row; the grouped join is one aggregate and one scan of the history."""
    freeze_local_now(monkeypatch, WORKLIST_NOW)
    _lapsed_clusters(pg_db, 3)

    with _captured_statements() as captured:
        AnalyticsService().get_inactive_customers(days_since=30, include_never_ordered=False)
    statement, parameters = _page_statement(captured)

    plan = "\n".join(row[0] for row in pg_db.session.connection().exec_driver_sql("EXPLAIN " + statement, parameters))
    assert "SubPlan" not in plan, plan
    assert plan.count(" on order_status_history") == 1, plan  # the history is scanned once


def test_a_linked_member_with_no_deliveries_of_their_own_is_not_listed_while_a_sibling_took_water(
    client, db, monkeypatch, manager_claim_headers
):
    freeze_local_now(monkeypatch, WORKLIST_NOW)
    ordering_sibling = make_customer(db, first_name="Orders")
    silent_member = make_customer(db, first_name="Silent")  # signed up long ago, never ordered
    link(db, ordering_sibling, silent_member)
    delivered_order(db, ordering_sibling, datetime(2026, 10, 10, 7, 0, tzinfo=UTC))  # five days ago
    never_ordered_alone = make_customer(db, first_name="Alone")  # control: the same sign-up date, no cluster

    items = _inactive(client, manager_claim_headers, days_since=30, include_never_ordered="true")

    assert silent_member.id not in items and ordering_sibling.id not in items
    assert items[never_ordered_alone.id]["never_ordered"] is True


def test_an_order_with_no_delivery_address_counts_as_a_delivery(client, db, monkeypatch, manager_claim_headers):
    """`delivery_address_id` is nullable on the shared select; the list must not need it."""
    freeze_local_now(monkeypatch, WORKLIST_NOW)
    recent = make_customer(db, first_name="Recent")
    lapsed = make_customer(db, first_name="Lapsed")
    delivered_order(db, recent, datetime(2026, 10, 10, 7, 0, tzinfo=UTC), address=NO_ADDRESS)
    delivered_order(db, lapsed, datetime(2026, 8, 1, 7, 0, tzinfo=UTC), address=NO_ADDRESS)

    items = _inactive(client, manager_claim_headers, days_since=30, include_never_ordered="false")

    assert recent.id not in items
    assert (items[lapsed.id]["last_order_date"], items[lapsed.id]["never_ordered"]) == (
        "2026-08-01T07:00:00+00:00",
        False,
    )
