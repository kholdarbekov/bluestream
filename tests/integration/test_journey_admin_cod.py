"""ADMIN COD JOURNEYS — an admin opens a screen, reads a figure, acts, and the
money moves. Every assertion below is on the MONEY, never on a rendered variable.

WHY THIS FILE EXISTS
--------------------
A 4-lens review found five instances of ONE defect: *a monetary figure shown to a
human, and an amount or scope posted to the engine, decided by different code*.
None was caught by 8 000+ tests. The stated reasons were:

1. the fixtures agreed by construction (debt only INSIDE a place, where
   ``max(own, cluster, place)`` and ``union(own, coworkers)`` are the same
   number);
2. tests are organised by component, so nobody owned the RELATIONSHIP between
   the row and the ceiling;
3. tests assert return values, not journeys.

Instance #3 was the ADMIN cash-collection modal: it displayed the raw
per-account ``total_outstanding_amount`` and posted ``places[0].address_id``.
Measured on the A6 rows — shown 25 000, posted a PLACE scope whose ceiling is
45 000 — the admin collected the 25 000 they were shown, Alice still owed 10 000
and **10 000 of Bob's debt was silently paid**.

So this module does three things no existing admin test does:

* it **generates** its states from ``place_state_factory`` — every person of
  every preset is swept, so no fixture can agree by construction;
* it crosses the **screen → action** seam: the figure asserted is the one the
  route publishes, the amount posted is that same figure, through the real HTTP
  admin routes;
* it asserts **whose debt moved**, per payment row, against a set derived from
  the scenario SPEC (a third derivation — spec, oracle, rows).

WHAT IS AND IS NOT COVERED HERE
-------------------------------
The admin panel is React. Everything below drives the BACKEND PAYLOAD the modal
renders from (``collect_scope`` on the customer statement, the dropdown rows,
the collected-cash preview summary) and the routes its buttons post to. The
UI half — that ``DeliveryReports.js`` reads ``collect_scope.amount`` for the
alert and posts ``collect_scope.delivery_address_id``, and degrades both
together — is covered by the admin_ui Vitest suite
(``admin_ui/src/__tests__/utils/codCollectScope.test.js`` and
``admin_ui/src/__tests__/pages/DeliveryReports.test.js``). The two suites meet
at the shape of ``collect_scope``, which is why every assertion here reads that
one object rather than recomposing either half.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Set

import pytest
from flask_jwt_extended import create_access_token

from business_app import db as _db
from business_app.models.bottle import BottleBalance, BottleFine, BottleLedger
from business_app.models.customer_link import AddressGroup
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.payment import CashCollectionEvent, Payment
from business_app.models.user import User
from business_app.services.cash_collection_service import CashCollectionService
from business_app.utils.password_security import hash_password
from shared import business_config
from shared.enums import (
    BottleLedgerEventType,
    DeliveryStatus,
    OrderStatus,
    UserRole,
    UserType,
)
from tests.integration.place_state_factory import SCENARIOS, build

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# §0  HARNESS — the admin's browser, reduced to four calls
# --------------------------------------------------------------------------- #

STATEMENT = "/api/v1/admin/staff/cash-reconciliation/customers/{cid}/statement"
COLLECT = "/api/v1/admin/staff/cash-reconciliation/collections"
SEARCH = "/api/v1/admin/staff/cash-reconciliation/users/search"
CASH_EDIT = "/api/v1/admin/orders/{oid}/collected-cash"
FINES = "/api/v1/admin/bottles/fines"
PLACE_MEMBER = "/api/v1/admin/place-groups/{gid}/addresses/{aid}"


@pytest.fixture(autouse=True)
def _place_cod_gate(app, monkeypatch):
    """Gate ON, and the Flask mirror restored afterwards — ``app`` is
    session-scoped, so a bare assignment leaks into every later test on the same
    xdist worker."""
    original = app.config.get("PLACE_COD_COLLECTION_ENABLED")
    app.config["PLACE_COD_COLLECTION_ENABLED"] = True
    monkeypatch.setattr(business_config, "PLACE_COD_COLLECTION_ENABLED", True)
    yield
    app.config["PLACE_COD_COLLECTION_ENABLED"] = original


def _admin_headers(app, user_id: int) -> Dict[str, str]:
    """``manager_or_higher_required`` reads the ``role`` CLAIM, not the DB row,
    so a bare ``create_access_token(identity=...)`` is a 403."""
    with app.app_context():
        token = create_access_token(identity=str(user_id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _open_collect_modal(client, headers, customer_id: int) -> Dict:
    """EXACTLY the payload the admin cash-collection modal renders from."""
    response = client.get(STATEMENT.format(cid=customer_id), headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def _submit_collection(client, headers, *, customer_id, amount, delivery_address_id, notes):
    """EXACTLY what the modal's submit button posts."""
    return client.post(
        COLLECT,
        json={
            "customer_id": customer_id,
            "amount": amount,
            "source": "standalone_meeting",
            "notes": notes,
            "delivery_address_id": delivery_address_id,
        },
        headers=headers,
    )


def _dropdown(client, headers, query: str) -> List[Dict]:
    """The customer dropdown behind the modal's search box."""
    response = client.get(f"{SEARCH}?q={query}&type=phone", headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]["items"]


def _outstanding(scenario) -> Dict[str, Decimal]:
    """Every debt the scenario wrote, by KEY, straight off ``payments``."""
    _db.session.expire_all()
    return {
        key: Decimal(str(_db.session.get(Payment, payment.id).outstanding_amount))
        for key, payment in scenario.payments.items()
    }


def _moved(before: Dict[str, Decimal], after: Dict[str, Decimal]) -> Dict[str, Decimal]:
    """``{debt_key: amount settled}`` — the answer to *whose debt moved*."""
    return {key: before[key] - after[key] for key in before if after[key] != before[key]}


def _reserved(payment_id: int) -> Decimal:
    payment = _db.session.get(Payment, payment_id)
    return Decimal(str((payment.provider_data or {}).get("cod_prepayment_reserved_amount", 0) or 0))


# --------------------------------------------------------------------------- #
# §1  THE THIRD DERIVATION — what a post SHOULD settle, from the SPEC alone.
#
#     The factory ships an oracle; this reads the declarative spec a second,
#     independent way and the tests assert the two agree BEFORE either is
#     compared to the rows. Nothing here imports business_app.services — the
#     whole point is not to judge production with production's own arithmetic.
# --------------------------------------------------------------------------- #


def _cluster_keys(spec, person_key: str) -> Set[str]:
    parent = {person.key: person.key for person in spec.people}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for person in spec.people:
        if person.linked_to:
            a, b = find(person.key), find(person.linked_to)
            if a != b:
                parent[b] = a
    root = find(person_key)
    return {person.key for person in spec.people if find(person.key) == root}


def _open_delivered(spec):
    return [
        debt
        for debt in spec.debts
        if debt.status == OrderStatus.DELIVERED and debt.resolved_outstanding() > Decimal("0.00")
    ]


def _settleable_debt_keys(scenario, person_key: str) -> List[str]:
    """The debts a collection posted by this person from the ADMIN surface can
    settle, in the order the engine's rings walk them.

    * a grocery account is FORCED personal — its cash is mirrored onto a
      corporate contract and may never co-mingle;
    * exactly one grouped place ⇒ ring 1 (the place, any owner, oldest first)
      then ring 2 (the poster's own cluster, minus ring 1);
    * zero or two-plus places ⇒ the cluster's own debt only (decision E7).

    Declaration order IS age order in the factory, so ``spec.debts`` order is
    oldest-first order.
    """
    spec = scenario.spec
    people_by_key = {person.key: person for person in spec.people}
    place_of_address = {address.key: address.place for address in spec.addresses}
    cluster = _cluster_keys(spec, person_key)
    open_delivered = _open_delivered(spec)

    if people_by_key[person_key].grocery:
        return [debt.key for debt in open_delivered if debt.owner == person_key]

    places: List[str] = []
    for address in spec.addresses:
        if address.owner in cluster and address.place and address.place not in places:
            places.append(address.place)

    own = [debt for debt in open_delivered if debt.owner in cluster]
    if len(places) != 1:
        return [debt.key for debt in own]

    ring1 = [
        debt for debt in open_delivered if debt.at is not None and place_of_address[debt.at] == places[0]
    ]
    ring1_keys = {debt.key for debt in ring1}
    ring2 = [debt for debt in own if debt.key not in ring1_keys]
    return [debt.key for debt in ring1 + ring2]


def _settleable_total(scenario, keys) -> Decimal:
    by_key = {debt.key: debt for debt in scenario.spec.debts}
    return sum((by_key[key].resolved_outstanding() for key in keys), Decimal("0.00"))


#: Every person of every preset. The catalogue is swept, not sampled — the
#: documented root cause was a fixture that could not express the divergent
#: state, so the state set is generated here rather than chosen.
def _all_people():
    cases = []
    for name in sorted(SCENARIOS):
        for person in SCENARIOS[name].people:
            cases.append(pytest.param(name, person.key, id=f"{name}-{person.key}"))
    return cases


#: 🔴 ONE SWEEP, NO EXCEPTIONS — and the absence of a mark here is load-bearing.
#: ``_GROCERY_PIN`` used to sit on this line: an ``xfail(strict=True)`` for
#: ``grocery_at_place-mart``, the ONE generated case where the shown figure and
#: the settled set parted company. It was marked on the MONEY sweep only,
#: because the DISPLAY sweep passed for that person — which was exactly the
#: problem: the payload was internally consistent and described a settlement the
#: engine would not perform. The display now asks the engine before widening
#: anyone (``cod_collect_ceiling.place_widening_applies``), so the money sweep
#: and the display sweep walk the identical case list. Do not re-introduce a
#: per-case mark: a case that needs one IS the defect.
ALL_PEOPLE = _all_people()
ALL_PEOPLE_MONEY = ALL_PEOPLE


# --------------------------------------------------------------------------- #
# §2  THE COLLECTION JOURNEY — open the modal, collect the figure, follow the
#     money.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario_name,person_key", ALL_PEOPLE)
def test_the_modal_publishes_one_object_and_it_matches_the_scenario(
    app, client, db, admin_user, scenario_name, person_key
):
    """The figure the modal SHOWS and the address it POSTS come from one dict,
    and that dict describes the world the scenario built.

    This is the screen half of the seam. It is asserted against the factory's
    ORACLE — arithmetic over the declarative spec — never against a second
    reading of production.
    """
    scenario = build(db, scenario_name)
    headers = _admin_headers(app, admin_user.id)
    expected = scenario.expect(person_key)

    statement = _open_collect_modal(client, headers, scenario.user(person_key).id)
    scope = statement["collect_scope"]

    assert scope["scope_type"] == expected.collect_scope_type
    assert Decimal(str(scope["amount"])) == expected.collect_scope_amount
    assert scope["debt_count"] == expected.collect_scope_debt_count
    if expected.collect_scope_type == "place":
        assert scope["delivery_address_id"] in expected.collect_scope_address_ids
    else:
        # Decision E7: a degradation drops the ADDRESS together with the figure.
        # Keeping the place scope while falling back on the number is the exact
        # shape that settled a coworker's debt.
        assert scope["delivery_address_id"] is None
    # The un-widened fallback rides along so no caller ever recomposes it.
    assert Decimal(str(scope["cluster_amount"])) == expected.cluster_delivered_outstanding


@pytest.mark.parametrize("scenario_name,person_key", ALL_PEOPLE_MONEY)
def test_collecting_the_figure_the_modal_shows_settles_exactly_the_debts_it_names(
    app, client, db, admin_user, scenario_name, person_key
):
    """🔴 THE ONE THAT WOULD HAVE CAUGHT DEFECT #3.

    Read the figure off the real route, post that same figure through the real
    route, then read every payment row in the world and assert WHICH ones moved
    and BY HOW MUCH. Nothing here trusts a returned summary.

    Three derivations must agree: the SPEC (``_settleable_debt_keys``), the
    ORACLE (``engine_settleable_total``) and the ROWS.
    """
    scenario = build(db, scenario_name)
    headers = _admin_headers(app, admin_user.id)
    expected = scenario.expect(person_key)

    # Derivation 1 (spec) vs derivation 2 (oracle) — before any row is read.
    settleable_keys = _settleable_debt_keys(scenario, person_key)
    assert _settleable_total(scenario, settleable_keys) == expected.engine_settleable_total
    assert len(settleable_keys) == expected.engine_settleable_debt_count

    scope = _open_collect_modal(client, headers, scenario.user(person_key).id)["collect_scope"]
    before = _outstanding(scenario)

    response = _submit_collection(
        client,
        headers,
        customer_id=scenario.user(person_key).id,
        amount=scope["amount"],
        delivery_address_id=scope["delivery_address_id"],
        notes=f"admin collected the published figure from {person_key}",
    )
    assert response.status_code == 201, response.get_json()

    after = _outstanding(scenario)
    moved = _moved(before, after)

    # WHOSE debt moved — the question no component test asks.
    assert set(moved) == set(settleable_keys)
    # …and by how much, in total. The figure advertised IS the debt settled.
    assert sum(moved.values(), Decimal("0.00")) == Decimal(str(scope["amount"]))
    # Nothing was left over to become the payer's credit: an advertised figure
    # that is larger than the settleable set parks the difference in a wallet,
    # which is money taken against a debt that was never there.
    event = _db.session.get(
        CashCollectionEvent, response.get_json()["data"]["cash_collection_event"]["id"]
    )
    assert Decimal(str(event.unapplied_amount)) == Decimal("0.00")
    # Every named debt is fully cleared, not partially nibbled.
    assert all(after[key] == Decimal("0.00") for key in settleable_keys)


@pytest.mark.parametrize(
    "scenario_name,person_key",
    [
        ("a6_canonical", "alice"),
        ("a6_canonical", "bob"),
        ("three_member_place", "ben"),
        ("sibling_owns_place_address", "alice_a"),
        ("two_places_one_cluster", "alice"),
        ("solo_ungrouped_debtor", "sam"),
        ("dissolved_place", "dana"),
    ],
)
def test_the_screen_reads_zero_after_collecting_what_the_screen_offered(
    app, client, db, admin_user, scenario_name, person_key
):
    """screen → action → screen. The admin collects "everything this person can
    settle", reopens the modal, and it must say there is nothing left.

    A surface showing a figure smaller than its own post settles would still
    read non-zero here; a surface showing a larger one would have parked the
    difference as credit and *also* read non-zero. Only a screen and an engine
    that name the SAME set close this loop.
    """
    scenario = build(db, scenario_name)
    headers = _admin_headers(app, admin_user.id)
    customer_id = scenario.user(person_key).id

    scope = _open_collect_modal(client, headers, customer_id)["collect_scope"]
    assert scope["amount"] > 0, "this case must have something to collect"

    assert (
        _submit_collection(
            client,
            headers,
            customer_id=customer_id,
            amount=scope["amount"],
            delivery_address_id=scope["delivery_address_id"],
            notes="collect all",
        ).status_code
        == 201
    )

    reopened = _open_collect_modal(client, headers, customer_id)["collect_scope"]
    assert Decimal(str(reopened["amount"])) == Decimal("0.00")
    assert reopened["debt_count"] == 0
    assert Decimal(str(reopened["cluster_amount"])) == Decimal("0.00")


def test_the_place_total_and_the_collect_ceiling_agree_only_in_the_fixture_that_agreed_by_construction(
    app, client, db, admin_user
):
    """THE SEAM GUARD — proof this file is not testing a world where the bug is
    invisible.

    ``debt_inside_place_only`` is the fixture the original suite had: all debt
    inside the place, so the place's own total and the collect ceiling are the
    same number and any expression that mixes them looks correct.
    ``a6_canonical`` adds 10 000 of Alice's debt OUTSIDE the place, and the two
    figures part company — 35 000 vs 45 000.

    A test green on the first and red on the second has found the seam; one that
    only ever ran on the first proves nothing.
    """
    headers = _admin_headers(app, admin_user.id)

    agreeing = build(db, "debt_inside_place_only")
    statement = _open_collect_modal(client, headers, agreeing.user("alice").id)
    place = statement["places"][0]
    assert Decimal(str(place["place_open_cod_debt_total"])) == Decimal("35000.00")
    assert Decimal(str(place["place_collect_ceiling_amount"])) == Decimal("35000.00")
    assert Decimal(str(statement["collect_scope"]["amount"])) == Decimal("35000.00")

    diverging = build(db, "a6_canonical")
    statement = _open_collect_modal(client, headers, diverging.user("alice").id)
    place = statement["places"][0]
    assert Decimal(str(place["place_open_cod_debt_total"])) == Decimal("35000.00")
    assert Decimal(str(place["place_collect_ceiling_amount"])) == Decimal("45000.00")
    assert Decimal(str(statement["collect_scope"]["amount"])) == Decimal("45000.00")
    # And the per-account headline the modal used to render is a THIRD number.
    assert Decimal(str(statement["total_outstanding_amount"])) == Decimal("25000.00")


def test_alices_published_figure_pays_bobs_debt_and_bobs_pays_alices(app, client, db, admin_user):
    """WHOSE DEBT MOVED, by name, in both directions — the measured defect stated
    as its own test.

    Alice: 10 000 at home + 15 000 at office G. Bob: 20 000 at G.
    The shipped modal showed Alice 25 000 and posted a PLACE scope; collecting
    that 25 000 cleared her 15 000 office order and paid **10 000 of Bob's** while
    her 10 000 home debt survived. The published figure is 45 000, and collecting
    it settles all three — the coworker's debt is paid DELIBERATELY and in full,
    which is the whole difference between a place collection and a mistake.
    """
    scenario = build(db, "a6_canonical")
    headers = _admin_headers(app, admin_user.id)

    scope = _open_collect_modal(client, headers, scenario.user("alice").id)["collect_scope"]
    before = _outstanding(scenario)
    assert (
        _submit_collection(
            client,
            headers,
            customer_id=scenario.user("alice").id,
            amount=scope["amount"],
            delivery_address_id=scope["delivery_address_id"],
            notes="alice paid for the office",
        ).status_code
        == 201
    )
    assert _moved(before, _outstanding(scenario)) == {
        "alice_home": Decimal("10000.00"),
        "alice_office": Decimal("15000.00"),
        "bob_office": Decimal("20000.00"),
    }

    # The mirror image: Bob's 35 000 settles ALICE's office debt and his own,
    # and never touches her ungrouped home debt — that one is outside the place
    # and outside his cluster.
    reversed_scenario = build(db, "a6_canonical", namespace=91)
    scope = _open_collect_modal(client, headers, reversed_scenario.user("bob").id)["collect_scope"]
    before = _outstanding(reversed_scenario)
    assert (
        _submit_collection(
            client,
            headers,
            customer_id=reversed_scenario.user("bob").id,
            amount=scope["amount"],
            delivery_address_id=scope["delivery_address_id"],
            notes="bob paid for the office",
        ).status_code
        == 201
    )
    assert _moved(before, _outstanding(reversed_scenario)) == {
        "alice_office": Decimal("15000.00"),
        "bob_office": Decimal("20000.00"),
    }


def test_a_coworker_who_owes_nothing_can_still_settle_the_office(app, client, db, admin_user):
    """The debt-free coworker holding the office's cash.

    Ben has no orders at all. The modal must still offer him the place's 12 000,
    and collecting it must settle ANN's and CARA's office orders while leaving
    Ann's ungrouped home debt alone. The allocation stamps a beneficiary that is
    not the payer — the one place in the system where those differ by design.
    """
    scenario = build(db, "three_member_place")
    headers = _admin_headers(app, admin_user.id)
    ben_id = scenario.user("ben").id

    scope = _open_collect_modal(client, headers, ben_id)["collect_scope"]
    assert Decimal(str(scope["amount"])) == Decimal("12000.00")
    assert Decimal(str(scope["cluster_amount"])) == Decimal("0.00")  # he owes nothing himself

    before = _outstanding(scenario)
    response = _submit_collection(
        client,
        headers,
        customer_id=ben_id,
        amount=scope["amount"],
        delivery_address_id=scope["delivery_address_id"],
        notes="ben handed over the office cash",
    )
    assert response.status_code == 201
    assert _moved(before, _outstanding(scenario)) == {
        "ann_office": Decimal("5000.00"),
        "cara_office": Decimal("7000.00"),
    }

    event = _db.session.get(
        CashCollectionEvent, response.get_json()["data"]["cash_collection_event"]["id"]
    )
    assert event.customer_id == ben_id
    beneficiaries = {allocation.beneficiary_user_id for allocation in event.allocations}
    assert beneficiaries == {scenario.user("ann").id, scenario.user("cara").id}
    assert ben_id not in beneficiaries


def test_a_pending_order_inflates_the_headline_but_not_what_a_collection_settles(
    app, client, db, admin_user
):
    """The PENDING shape — where the divergence was widest (95 000 vs 45 000).

    Cash offered against a PENDING order settles nothing: the engine's candidate
    rings select DELIVERED orders only. The modal must therefore publish 45 000
    while the per-account headline still reads 95 000, and collecting the
    published figure must leave the pending order completely untouched — no
    settlement, and no reservation either.
    """
    scenario = build(db, "a6_with_pending")
    headers = _admin_headers(app, admin_user.id)

    statement = _open_collect_modal(client, headers, scenario.user("alice").id)
    assert Decimal(str(statement["total_outstanding_amount"])) == Decimal("95000.00")
    scope = statement["collect_scope"]
    assert Decimal(str(scope["amount"])) == Decimal("45000.00")

    before = _outstanding(scenario)
    response = _submit_collection(
        client,
        headers,
        customer_id=scenario.user("alice").id,
        amount=scope["amount"],
        delivery_address_id=scope["delivery_address_id"],
        notes="collected the collectible figure",
    )
    assert response.status_code == 201

    after = _outstanding(scenario)
    assert _moved(before, after) == {
        "alice_home": Decimal("10000.00"),
        "alice_office": Decimal("15000.00"),
        "bob_office": Decimal("20000.00"),
    }
    assert after["alice_pending"] == Decimal("70000.00")
    assert _reserved(scenario.payment("alice_pending").id) == Decimal("0.00")
    event = _db.session.get(
        CashCollectionEvent, response.get_json()["data"]["cash_collection_event"]["id"]
    )
    assert Decimal(str(event.unapplied_amount)) == Decimal("0.00")


def test_collecting_the_old_pending_inclusive_headline_takes_50000_it_cannot_settle(
    app, client, db, admin_user
):
    """The harm the published figure avoids, made explicit.

    This posts the figure the modal USED to display — the per-account headline
    that counts a PENDING order — and follows it. Exactly the same 45 000 of
    delivered debt settles; the other 50 000 cannot settle anything, so it is
    swept onto the customer's not-yet-delivered order as a reservation. The
    admin who trusted the headline collected half again as much cash as the
    debt they were settling.

    Characterisation, not a defect: the engine's behaviour here is right. It is
    the number the human was given that was wrong, which is why the assertion is
    on the cash outcome and not on the headline itself.
    """
    scenario = build(db, "a6_with_pending")
    headers = _admin_headers(app, admin_user.id)

    statement = _open_collect_modal(client, headers, scenario.user("alice").id)
    headline = statement["total_outstanding_amount"]
    scope = statement["collect_scope"]

    before = _outstanding(scenario)
    response = _submit_collection(
        client,
        headers,
        customer_id=scenario.user("alice").id,
        amount=headline,
        delivery_address_id=scope["delivery_address_id"],
        notes="admin trusted the per-account headline",
    )
    assert response.status_code == 201

    after = _outstanding(scenario)
    settled = sum(_moved(before, after).values(), Decimal("0.00"))
    assert settled == Decimal("45000.00")
    assert Decimal(str(headline)) - settled == Decimal("50000.00")
    # The overshoot is parked against an order that has not been delivered.
    assert after["alice_pending"] == Decimal("70000.00")
    assert _reserved(scenario.payment("alice_pending").id) == Decimal("50000.00")


def test_two_places_degrade_the_figure_and_the_address_together(app, client, db, admin_user):
    """Decision E7 — ambiguity must not be guessed.

    Alice owns an address in G1 and in G2, so nothing on the admin surface can
    name which office a collection is for. The modal must fall back to her own
    cluster figure (10 000) AND drop the address; the 35 000 union across both
    places must appear nowhere in the payload, and a collection must settle only
    her own debt.
    """
    scenario = build(db, "two_places_one_cluster")
    headers = _admin_headers(app, admin_user.id)

    statement = _open_collect_modal(client, headers, scenario.user("alice").id)
    scope = statement["collect_scope"]
    assert scope["scope_type"] == "cluster"
    assert scope["delivery_address_id"] is None
    assert Decimal(str(scope["amount"])) == Decimal("10000.00")
    assert 35000.0 not in [
        statement["total_outstanding_amount"],
        scope["amount"],
        scope["cluster_amount"],
        *[place["place_collect_ceiling_amount"] for place in statement["places"]],
        *[place["place_open_cod_debt_total"] for place in statement["places"]],
    ]

    before = _outstanding(scenario)
    assert (
        _submit_collection(
            client,
            headers,
            customer_id=scenario.user("alice").id,
            amount=scope["amount"],
            delivery_address_id=scope["delivery_address_id"],
            notes="ambiguous place, cluster collection",
        ).status_code
        == 201
    )
    assert _moved(before, _outstanding(scenario)) == {"alice_g1": Decimal("10000.00")}


def test_a_world_with_no_debt_is_a_journey_that_moves_no_money(app, client, db, admin_user):
    """The control that catches an assertion which passes because it measures
    nothing. Every figure zero, the dropdown empty, and a zero collection moves
    nothing."""
    scenario = build(db, "zero_everything")
    headers = _admin_headers(app, admin_user.id)
    nora_id = scenario.user("nora").id

    scope = _open_collect_modal(client, headers, nora_id)["collect_scope"]
    assert (scope["amount"], scope["debt_count"], scope["delivery_address_id"]) == (0.0, 0, None)
    assert _dropdown(client, headers, scenario.user("nora").phone) == []

    before = _outstanding(scenario)
    assert before == {}
    response = _submit_collection(
        client,
        headers,
        customer_id=nora_id,
        amount=0,
        delivery_address_id=None,
        notes="nothing was owed and nothing was collected",
    )
    assert response.status_code == 201
    event = _db.session.get(
        CashCollectionEvent, response.get_json()["data"]["cash_collection_event"]["id"]
    )
    assert Decimal(str(event.amount)) == Decimal("0.00")
    assert event.allocations == []


def test_the_grocery_modal_offers_only_the_place_the_engine_will_settle(
    app, client, db, admin_user
):
    """A grocery entity sharing a place with an individual.

    Mart owes 8 000; Alice owes 10 000 at the same place. The modal USED TO offer
    18 000 with a PLACE scope and Mart's grouped address, so an admin collecting
    "everything" took 18 000 in cash — and the engine's grocery backstop then
    forced PERSONAL scope, settled Mart's 8 000, and parked 10 000 in Mart's
    wallet while Alice still owed it. This test was ``xfail(strict=True)`` and
    asserted the intended contract, so the fix flipped it to pass unchanged
    apart from the two scope assertions below, which are new.

    THE RESOLUTION, and it is the opposite of the obvious one: the engine's
    refusal is right, so the MODAL must stop offering the place — not the engine
    start honouring it. The modal now reads ONE resolved ``collect_scope`` whose
    figure and address both come from asking
    ``resolve_allocation_scope`` what a post here would actually do.
    """
    scenario = build(db, "grocery_at_place")
    headers = _admin_headers(app, admin_user.id)

    scope = _open_collect_modal(client, headers, scenario.user("mart").id)["collect_scope"]
    # The offer degrades in BOTH halves — an address kept while the figure falls
    # back is the "P0-degraded" defect in its own right.
    assert scope["scope_type"] == "cluster"
    assert scope["delivery_address_id"] is None

    before = _outstanding(scenario)
    response = _submit_collection(
        client,
        headers,
        customer_id=scenario.user("mart").id,
        amount=scope["amount"],
        delivery_address_id=scope["delivery_address_id"],
        notes="admin collected the figure the shop was shown",
    )
    assert response.status_code == 201

    after = _outstanding(scenario)
    settled = sum(_moved(before, after).values(), Decimal("0.00"))
    event = _db.session.get(
        CashCollectionEvent, response.get_json()["data"]["cash_collection_event"]["id"]
    )

    # THE INVARIANT: the figure advertised is the debt settled.
    assert settled == Decimal(str(scope["amount"])), (
        f"admin was shown {scope['amount']} but the collection settled {settled}; "
        f"{event.unapplied_amount} became the grocery's credit and "
        f"{after['alice_office']} of the coworker's debt is still open"
    )
    assert Decimal(str(event.unapplied_amount)) == Decimal("0.00")
    # The shop's own debt, and only it. The coworker's 10 000 was never
    # advertised to this admin and is therefore untouched — still open, still
    # hers, and still collectible from her own row (which the sweep walks).
    assert settled == Decimal("8000.00")
    assert after["alice_office"] == Decimal("10000.00")


# --------------------------------------------------------------------------- #
# §3  THE CUSTOMER DROPDOWN — the row an admin picks FROM must be the figure
#     they then collect.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "scenario_name,person_key",
    [
        ("a6_canonical", "alice"),
        ("a6_canonical", "bob"),
        ("a6_with_pending", "alice"),
        ("three_member_place", "ann"),
        ("three_member_place", "ben"),
        ("three_member_place", "cara"),
        ("sibling_owns_place_address", "alice_a"),
        ("two_places_one_cluster", "alice"),
        ("solo_ungrouped_debtor", "sam"),
    ],
)
def test_the_dropdown_row_is_the_same_pair_the_modal_posts(
    app, client, db, admin_user, scenario_name, person_key
):
    """The dropdown advertises a count and a figure; the modal the admin lands on
    posts a scope. Before the fix these came from different reads — the row was
    built from the RAW per-account statement — so a person could be listed at
    one number and collected at another.
    """
    scenario = build(db, scenario_name)
    headers = _admin_headers(app, admin_user.id)
    user = scenario.user(person_key)

    rows = _dropdown(client, headers, user.phone)
    assert [row["id"] for row in rows] == [user.id]
    row = rows[0]

    scope = _open_collect_modal(client, headers, user.id)["collect_scope"]
    assert Decimal(str(row["total_outstanding_amount"])) == Decimal(str(scope["amount"]))
    assert row["active_cod_debt_count"] == scope["debt_count"]


@pytest.mark.parametrize(
    "scenario_name,person_key",
    [
        ("a6_canonical", "alice"),
        ("a6_canonical", "bob"),
        ("three_member_place", "ben"),
        ("debt_free_coworker", "bob"),
        ("sibling_owns_place_address", "bob"),
    ],
)
def test_the_dropdown_count_is_the_number_of_debts_the_collection_actually_clears(
    app, client, db, admin_user, scenario_name, person_key
):
    """The count beside the figure is not decoration: it says how many debts the
    money will land on. Assert it against the payment rows that actually move,
    not against the number the row carried."""
    scenario = build(db, scenario_name)
    headers = _admin_headers(app, admin_user.id)
    user = scenario.user(person_key)

    row = _dropdown(client, headers, user.phone)[0]
    scope = _open_collect_modal(client, headers, user.id)["collect_scope"]

    before = _outstanding(scenario)
    assert (
        _submit_collection(
            client,
            headers,
            customer_id=user.id,
            amount=row["total_outstanding_amount"],
            delivery_address_id=scope["delivery_address_id"],
            notes="collected the dropdown figure",
        ).status_code
        == 201
    )
    moved = _moved(before, _outstanding(scenario))
    assert len(moved) == row["active_cod_debt_count"]
    assert sum(moved.values(), Decimal("0.00")) == Decimal(str(row["total_outstanding_amount"]))


def test_a_debt_free_coworker_is_selectable_at_the_offices_figure(app, client, db, admin_user):
    """The measured consequence of gating the dropdown on the person's OWN debt:
    a coworker standing at an indebted workplace returned ZERO rows, so the
    admin could not select the very person holding the office's cash."""
    scenario = build(db, "debt_free_coworker")
    headers = _admin_headers(app, admin_user.id)

    rows = _dropdown(client, headers, scenario.user("bob").phone)
    assert [row["id"] for row in rows] == [scenario.user("bob").id]
    assert Decimal(str(rows[0]["total_outstanding_amount"])) == Decimal("15000.00")
    assert rows[0]["active_cod_debt_count"] == 1
    assert rows[0]["order_count"] == 0  # he has never ordered anything


def test_a_person_whose_debt_is_settled_leaves_the_dropdown(app, client, db, admin_user):
    """The loop closed on the dropdown as well as the modal: after the money
    lands, the person is no longer offered for collection."""
    scenario = build(db, "a6_canonical")
    headers = _admin_headers(app, admin_user.id)
    alice, bob = scenario.user("alice"), scenario.user("bob")

    assert _dropdown(client, headers, alice.phone)
    assert _dropdown(client, headers, bob.phone)

    scope = _open_collect_modal(client, headers, alice.id)["collect_scope"]
    assert (
        _submit_collection(
            client,
            headers,
            customer_id=alice.id,
            amount=scope["amount"],
            delivery_address_id=scope["delivery_address_id"],
            notes="alice cleared the office",
        ).status_code
        == 201
    )

    # Alice's collection cleared Bob's office debt too, so BOTH rows must go.
    assert _dropdown(client, headers, alice.phone) == []
    assert _dropdown(client, headers, bob.phone) == []


# --------------------------------------------------------------------------- #
# §4  THE ORDER-EDIT JOURNEY — an admin corrects the cash a driver recorded,
#     days after the delivery.
# --------------------------------------------------------------------------- #


@pytest.fixture
def cod_driver(db):
    driver = User(
        email="journey.driver@example.com",
        phone="+998900000779",
        password_hash=hash_password("Passw0rd!"),
        first_name="Journey",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(driver)
    db.session.commit()
    db.session.add(
        DeliveryPerson(
            user_id=driver.id,
            full_name="Journey Driver",
            phone=driver.phone,
            email=driver.email,
            is_active=True,
            is_available=True,
        )
    )
    db.session.commit()
    return driver


def _deliver_and_collect(scenario, driver, debt_key: str, amount: Decimal):
    """The driver's half of the journey: a real delivery-completion collection at
    a grouped address, which FREEZES a place scope on the event."""
    order = scenario.order(debt_key)
    now = datetime.now(timezone.utc)
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver.id,
        status=DeliveryStatus.DELIVERED,
        scheduled_date=now,
        scheduled_time_slot="09:00-12:00",
        actual_delivery_time=now,
    )
    _db.session.add(delivery)
    _db.session.commit()
    event = CashCollectionService().post_collection(
        customer_id=order.user_id,
        amount=amount,
        source="delivery_completion",
        collector_user_id=driver.id,
        recorded_by_user_id=driver.id,
        order_id=order.id,
        delivery_id=delivery.id,
    )
    return order, delivery, event


def test_correcting_collected_cash_up_settles_the_coworker_the_preview_warned_about(
    app, client, db, admin_user, cod_driver
):
    """🔴 THE STANDARD THIS EFFORT IS BUILDING TOWARD, driven end to end.

    ``OrderCashEditService.preview`` replays the event's FROZEN
    ``AllocationScope.from_event``, so the figure the admin approves and the set
    the correction settles are the same object by construction. Here the driver
    recorded 15 000 at the office; the admin corrects it to 35 000, and the extra
    20 000 does NOT become the customer's credit — it settles BOB's order at that
    same place, because that is the scope the cash was collected under.

    The preview must say so before the admin commits: ``customer_credit_delta``
    is 0 and the warning names the scope-wide truth.
    """
    scenario = build(db, "a6_canonical")
    order, _, event = _deliver_and_collect(scenario, cod_driver, "alice_office", Decimal("15000"))
    assert event.scope_type == "place"
    assert _outstanding(scenario)["alice_office"] == Decimal("0.00")

    headers = _admin_headers(app, admin_user.id)
    preview = client.post(
        f"{CASH_EDIT.format(oid=order.id)}/preview", json={"new_amount": 35000}, headers=headers
    )
    assert preview.status_code == 200
    plan = preview.get_json()["data"]
    assert plan["is_editable"] is True
    assert Decimal(str(plan["applied_to_order"])) == Decimal("15000.00")
    # NOT a credit: the surplus has somewhere real to land inside the frozen scope.
    assert Decimal(str(plan["customer_credit_delta"])) == Decimal("0.00")
    assert any("customer_has_other_unpaid_cod_orders" in w for w in plan["warnings"])

    before = _outstanding(scenario)
    applied = client.post(
        CASH_EDIT.format(oid=order.id),
        json={"new_amount": 35000, "reason": "driver actually took 35000 at the office"},
        headers=headers,
    )
    assert applied.status_code == 200, applied.get_json()

    after = _outstanding(scenario)
    assert _moved(before, after) == {"bob_office": Decimal("20000.00")}
    assert after["alice_office"] == Decimal("0.00")
    assert after["alice_home"] == Decimal("10000.00")  # outside the frozen place scope

    replacement = _db.session.get(
        CashCollectionEvent, applied.get_json()["data"]["replacement_event_id"]
    )
    assert replacement.scope_type == "place"
    assert Decimal(str(replacement.unapplied_amount)) == Decimal("0.00")


def test_the_previewed_credit_is_the_credit_the_correction_actually_creates(
    app, client, db, admin_user, cod_driver
):
    """Correct beyond what the frozen scope can absorb. 60 000 against a scope
    holding 45 000 of debt leaves 15 000, and the preview must name that number
    before the admin commits — the modal promising one outcome and the
    confirmation causing another is the defect class this file exists for."""
    scenario = build(db, "a6_canonical")
    order, _, _ = _deliver_and_collect(scenario, cod_driver, "alice_office", Decimal("15000"))
    headers = _admin_headers(app, admin_user.id)

    plan = client.post(
        f"{CASH_EDIT.format(oid=order.id)}/preview", json={"new_amount": 60000}, headers=headers
    ).get_json()["data"]
    assert Decimal(str(plan["customer_credit_delta"])) == Decimal("15000.00")

    before = _outstanding(scenario)
    applied = client.post(
        CASH_EDIT.format(oid=order.id),
        json={"new_amount": 60000, "reason": "driver took 60000 for the whole office"},
        headers=headers,
    )
    assert applied.status_code == 200

    assert _moved(before, _outstanding(scenario)) == {
        "alice_home": Decimal("10000.00"),
        "bob_office": Decimal("20000.00"),
    }
    replacement = _db.session.get(
        CashCollectionEvent, applied.get_json()["data"]["replacement_event_id"]
    )
    assert Decimal(str(replacement.unapplied_amount)) == Decimal("15000.00")
    assert Decimal(str(replacement.unapplied_amount)) == Decimal(
        str(plan["customer_credit_delta"])
    )


def test_correcting_collected_cash_to_zero_restores_every_debt_it_had_settled(
    app, client, db, admin_user, cod_driver
):
    """Conservation across a two-step admin journey.

    The admin corrects up (35 000, which reaches a coworker's order) and then
    discovers the driver collected nothing at all. Correcting to 0 must unwind
    BOTH settlements — the order's own and the coworker's — leaving the world
    byte-identical to the pre-collection state.
    """
    scenario = build(db, "a6_canonical")
    order, _, _ = _deliver_and_collect(scenario, cod_driver, "alice_office", Decimal("15000"))
    headers = _admin_headers(app, admin_user.id)

    assert (
        client.post(
            CASH_EDIT.format(oid=order.id),
            json={"new_amount": 35000, "reason": "first correction, upward"},
            headers=headers,
        ).status_code
        == 200
    )
    assert _outstanding(scenario) == {
        "alice_home": Decimal("10000.00"),
        "alice_office": Decimal("0.00"),
        "bob_office": Decimal("0.00"),
    }

    plan = client.post(
        f"{CASH_EDIT.format(oid=order.id)}/preview", json={"new_amount": 0}, headers=headers
    ).get_json()["data"]
    assert Decimal(str(plan["projected_outstanding"])) == Decimal("15000.00")
    assert plan["projected_payment_status"] == "pending"

    assert (
        client.post(
            CASH_EDIT.format(oid=order.id),
            json={"new_amount": 0, "reason": "no cash was collected at all"},
            headers=headers,
        ).status_code
        == 200
    )
    # Every debt back where the scenario built it.
    assert _outstanding(scenario) == {
        "alice_home": Decimal("10000.00"),
        "alice_office": Decimal("15000.00"),
        "bob_office": Decimal("20000.00"),
    }


def test_the_corrected_world_is_the_world_the_collect_modal_then_shows(
    app, client, db, admin_user, cod_driver
):
    """The two admin surfaces must not disagree after one of them writes.

    Correcting the delivery cash up to 35 000 settles Bob's office order; the
    collection modal for Bob — a different screen, a different composition — must
    then show 0, not the 35 000 it showed before.
    """
    scenario = build(db, "a6_canonical")
    order, _, _ = _deliver_and_collect(scenario, cod_driver, "alice_office", Decimal("15000"))
    headers = _admin_headers(app, admin_user.id)

    bob_scope = _open_collect_modal(client, headers, scenario.user("bob").id)["collect_scope"]
    assert Decimal(str(bob_scope["amount"])) == Decimal("20000.00")

    assert (
        client.post(
            CASH_EDIT.format(oid=order.id),
            json={"new_amount": 35000, "reason": "driver took the office cash"},
            headers=headers,
        ).status_code
        == 200
    )

    bob_scope = _open_collect_modal(client, headers, scenario.user("bob").id)["collect_scope"]
    assert Decimal(str(bob_scope["amount"])) == Decimal("0.00")
    assert bob_scope["debt_count"] == 0
    alice_scope = _open_collect_modal(client, headers, scenario.user("alice").id)["collect_scope"]
    assert Decimal(str(alice_scope["amount"])) == Decimal("10000.00")  # her home debt only


# --------------------------------------------------------------------------- #
# §5  A FINE, AFTER THE PLACE HAS DISSOLVED — newly reachable through
#     ``address_groups.dissolved_onto_address_id``.
# --------------------------------------------------------------------------- #


def _issue_fine_then_dissolve(client, headers, scenario, *, quantity=2, fine_amount=50000):
    """Issue a fine at BOB's office desk, then remove that desk from the place.

    Removing one member of a two-member place leaves exactly one, so the place
    DISSOLVES onto the survivor in the same transaction. Bob's fine is a
    DEPARTED member's reference: the dissolve deliberately does NOT re-stamp it,
    so it still names a place with no members and no balance row — the shape the
    forwarding pointer exists for.
    """
    response = client.post(
        FINES,
        json={
            "user_id": scenario.user("bob").id,
            "address_id": scenario.address("bob_office").id,
            "quantity": quantity,
            "fine_amount": fine_amount,
            "notes": "crates missing from the office",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.get_json()
    fine_id = response.get_json()["data"]["id"]
    assert _db.session.get(BottleFine, fine_id).address_group_id == scenario.place("g").id

    removal = client.delete(
        PLACE_MEMBER.format(gid=scenario.place("g").id, aid=scenario.address("bob_office").id),
        json={"reason": "bob no longer works at this office"},
        headers=headers,
    )
    assert removal.status_code == 200, removal.get_json()
    assert removal.get_json()["data"]["dissolved"] is True
    return fine_id


def test_the_dissolve_moves_the_places_bottles_onto_the_survivor_and_leaves_no_orphan(
    app, client, db, admin_user
):
    """Precondition for the two fine journeys, asserted rather than assumed: the
    place's 6 pooled bottles land on the survivor address and the group's balance
    row is gone."""
    scenario = build(db, "a6_canonical")
    headers = _admin_headers(app, admin_user.id)
    group_id = scenario.place("g").id
    survivor_id = scenario.address("alice_office").id

    assert scenario.place_expect("g").bottle_balance == Decimal("6.00")
    _issue_fine_then_dissolve(client, headers, scenario)

    _db.session.expire_all()
    assert _db.session.get(AddressGroup, group_id).dissolved_onto_address_id == survivor_id
    assert BottleBalance.query.filter_by(address_group_id=group_id).count() == 0
    survivor_row = BottleBalance.query.filter_by(address_id=survivor_id).one()
    assert Decimal(str(survivor_row.balance)) == Decimal("6.00")


def test_a_fine_frozen_to_a_dissolved_place_is_paid_onto_the_scope_that_holds_the_crates(
    app, client, db, admin_user
):
    """🔴 THE FORWARDING JOURNEY. An admin settles a fine long after the place it
    was issued at stopped existing.

    Before the forwarding pointer this was a refusal (``BOTTLE_SCOPE_UNREACHABLE``)
    or, worse, an orphan balance row minted on a place no address can reach.
    Now the write follows ``dissolved_onto_address_id`` to the survivor, and the
    bottles come off the pool that physically holds them.
    """
    scenario = build(db, "a6_canonical")
    headers = _admin_headers(app, admin_user.id)
    group_id = scenario.place("g").id
    survivor_id = scenario.address("alice_office").id
    departed_id = scenario.address("bob_office").id
    fine_id = _issue_fine_then_dissolve(client, headers, scenario, quantity=2)

    response = client.put(
        f"{FINES}/{fine_id}",
        json={"action": "mark_paid", "notes": "bob settled the fine in cash"},
        headers=headers,
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["status"] == "paid"

    _db.session.expire_all()
    # THE MONEY-EQUIVALENT: the bottles came off the SURVIVOR's pool, 6 -> 4.
    assert Decimal(str(BottleBalance.query.filter_by(address_id=survivor_id).one().balance)) == Decimal("4.00")
    # …and no orphan was minted on the dead place, nor on the departed desk.
    assert BottleBalance.query.filter_by(address_group_id=group_id).count() == 0
    assert BottleBalance.query.filter_by(address_id=departed_id).count() == 0

    entry = (
        BottleLedger.query.filter_by(event_type=BottleLedgerEventType.FINE_PAID)
        .order_by(BottleLedger.id.desc())
        .first()
    )
    # An address scope's ledger predicate is `address_id = X AND group IS NULL`,
    # so scope and attribution are the same fact: the entry MUST name the
    # survivor, or the ledger and the balance would sit in different scopes.
    assert entry.address_id == survivor_id
    assert entry.address_group_id is None
    assert Decimal(str(entry.quantity)) == Decimal("-2.00")
    # The door the episode actually came through survives in the metadata.
    assert entry.entry_metadata["fine_id"] == fine_id
    assert entry.entry_metadata["forwarded_from_place_group_id"] == group_id
    assert entry.entry_metadata["forwarded_to_address_id"] == survivor_id
    assert entry.entry_metadata["attributed_through_address_id"] == departed_id


def test_waiving_a_fine_frozen_to_a_dissolved_place_lands_in_the_same_scope_and_moves_nothing(
    app, client, db, admin_user
):
    """The waive arm. It books into the SAME forwarded scope as the paid arm — a
    waive that landed somewhere else would split one fine's FINE_ISSUED /
    FINE_REVERSED pair across two ledgers — but it moves zero bottles, because
    waiving forgives the money without accounting for the crates."""
    scenario = build(db, "a6_canonical")
    headers = _admin_headers(app, admin_user.id)
    group_id = scenario.place("g").id
    survivor_id = scenario.address("alice_office").id
    departed_id = scenario.address("bob_office").id
    fine_id = _issue_fine_then_dissolve(client, headers, scenario, quantity=2)

    response = client.put(
        f"{FINES}/{fine_id}", json={"action": "waive", "notes": "goodwill"}, headers=headers
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["status"] == "waived"

    _db.session.expire_all()
    assert Decimal(str(BottleBalance.query.filter_by(address_id=survivor_id).one().balance)) == Decimal("6.00")
    assert BottleBalance.query.filter_by(address_group_id=group_id).count() == 0

    entry = (
        BottleLedger.query.filter_by(event_type=BottleLedgerEventType.FINE_REVERSED)
        .order_by(BottleLedger.id.desc())
        .first()
    )
    assert entry.address_id == survivor_id
    assert entry.address_group_id is None
    assert Decimal(str(entry.quantity)) == Decimal("0.00")
    assert entry.entry_metadata["forwarded_from_place_group_id"] == group_id
    assert entry.entry_metadata["attributed_through_address_id"] == departed_id


def test_a_settled_fine_cannot_be_settled_twice_after_the_dissolve(app, client, db, admin_user):
    """The forwarding pointer must not become a second door into the same
    bottles: once paid, a repeat settlement is refused and the survivor's pool
    does not move again."""
    scenario = build(db, "a6_canonical")
    headers = _admin_headers(app, admin_user.id)
    survivor_id = scenario.address("alice_office").id
    fine_id = _issue_fine_then_dissolve(client, headers, scenario, quantity=2)

    assert (
        client.put(f"{FINES}/{fine_id}", json={"action": "mark_paid"}, headers=headers).status_code
        == 200
    )
    _db.session.expire_all()
    assert Decimal(str(BottleBalance.query.filter_by(address_id=survivor_id).one().balance)) == Decimal("4.00")

    repeat = client.put(f"{FINES}/{fine_id}", json={"action": "mark_paid"}, headers=headers)
    assert repeat.status_code >= 400
    _db.session.expire_all()
    assert Decimal(str(BottleBalance.query.filter_by(address_id=survivor_id).one().balance)) == Decimal("4.00")
    assert (
        BottleLedger.query.filter_by(event_type=BottleLedgerEventType.FINE_PAID).count() == 1
    )


def test_the_dissolved_places_debt_is_still_collectible_from_the_survivor(
    app, client, db, admin_user
):
    """Money side of the same dissolve: the place is gone, so the modal degrades
    to Alice's own cluster figure — and it degrades the ADDRESS with it. Bob's
    20 000 was never hers to settle once he left, and it must not be advertised
    on her screen."""
    scenario = build(db, "a6_canonical")
    headers = _admin_headers(app, admin_user.id)
    _issue_fine_then_dissolve(client, headers, scenario)

    scope = _open_collect_modal(client, headers, scenario.user("alice").id)["collect_scope"]
    assert scope["scope_type"] == "cluster"
    assert scope["delivery_address_id"] is None
    assert Decimal(str(scope["amount"])) == Decimal("25000.00")

    before = _outstanding(scenario)
    assert (
        _submit_collection(
            client,
            headers,
            customer_id=scenario.user("alice").id,
            amount=scope["amount"],
            delivery_address_id=scope["delivery_address_id"],
            notes="alice settled her own debts after the office closed",
        ).status_code
        == 201
    )
    assert _moved(before, _outstanding(scenario)) == {
        "alice_home": Decimal("10000.00"),
        "alice_office": Decimal("15000.00"),
    }
