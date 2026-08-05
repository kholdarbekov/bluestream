"""Plan E Task 4 — owner ruling A3-bis rule 3, under owner ruling A7.

A coworker's row on the driver's COD debtor list carries the debt of the
GROUPED ADDRESSES they belong to, not only their own orders. Composed in
StaffService from public CashCollectionService readers; the engine is frozen
(plan C1).

A7 (2026-08-05) removed the 🏢 PLACE ROW from this list entirely: *"in staff bot
there won't be any 'office' row in debtors list. The debtors list only shows the
users, and the office debt is included in each coworker's debt."* So every
assertion below is about PERSON rows, and
`test_the_staff_list_never_emits_a_place_row` pins the absence of the other
family — in both gate states.
"""

from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.customer_link import CanonicalCustomer
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.user import User, UserAddress
from business_app.services.staff_service import StaffService
from business_app.utils.password_security import hash_password
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus, UserRole, UserType

LAT, LNG = 41.2746, 69.2061


# ---------------------------------------------------------------------------
# Factories — mirrored from tests/unit/test_place_cod_read_surfaces.py:37-77
# (`_user` / `_address` / `_delivered_cod_debt` / `_link`). The only change is
# that `_delivered_cod_debt` threads the amount through subtotal/total/payment
# so a 20 000 or 90 000 order is internally consistent instead of a 15 000
# order carrying a different outstanding figure.
# ---------------------------------------------------------------------------


def _user(db, email, phone):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name=email.split("@")[0], user_type=UserType.INDIVIDUAL,
             role=UserRole.CUSTOMER, is_verified=True,
             created_at=datetime.now(UTC))
    db.session.add(u)
    db.session.commit()
    return u


def _address(db, user, *, group=None):
    a = UserAddress(user_id=user.id, title="work", full_address="1 Office St, Tashkent",
                    street_address="1 Office St", city="Tashkent",
                    address_group_id=group.id if group is not None else None,
                    latitude=LAT, longitude=LNG)
    db.session.add(a)
    db.session.commit()
    return a


def _delivered_cod_debt(db, user, order_number, *, address=None, outstanding=Decimal("15000.00")):
    order = Order(user_id=user.id, order_number=order_number, status=OrderStatus.DELIVERED,
                  subtotal=outstanding, delivery_fee=Decimal("0.00"),
                  discount_amount=Decimal("0.00"), loyalty_discount=Decimal("0.00"),
                  total_amount=outstanding, payment_method=PaymentMethod.CASH,
                  delivery_address_id=address.id if address else None,
                  created_at=datetime.now(UTC))
    db.session.add(order)
    db.session.flush()
    payment = Payment(order_id=order.id, user_id=user.id, payment_method=PaymentMethod.CASH,
                      amount=outstanding, currency="UZS", status=PaymentStatus.PENDING,
                      payment_id=f"pay_{order_number}", outstanding_amount=outstanding,
                      created_at=datetime.now(UTC))
    db.session.add(payment)
    db.session.commit()
    return order, payment


def _group(db, label):
    from business_app.models.customer_link import AddressGroup

    g = AddressGroup(label=label)
    db.session.add(g)
    db.session.commit()
    return g


def _link(db, users):
    canonical = CanonicalCustomer(primary_user_id=users[0].id)
    db.session.add(canonical)
    db.session.commit()
    for u in users:
        u.canonical_customer_id = canonical.id
    db.session.commit()
    return canonical


def _rows(result, row_type):
    return [r for r in result["items"] if r.get("row_type") == row_type]


def _person_row(result, user_id):
    for row in _rows(result, "person"):
        if user_id in (row.get("member_user_ids") or [row["id"]]):
            return row
    return None


def _list(app, *, gate=True):
    """Drive the REAL composition. Every test goes through here — no test may
    define its own `result` out of thin air (plan C4-bis)."""
    app.config["PLACE_COD_COLLECTION_ENABLED"] = gate
    return StaffService().paginate_cod_debtors_for_staff(page=1, per_page=50)


@pytest.fixture(autouse=True)
def _restore_place_gate(app):
    """`app` is SESSION-scoped (tests/conftest.py:113), so `_list`'s
    ``app.config[...] = gate`` would otherwise leak into every later test on the
    same xdist worker — notably
    ``test_place_cod_collection_gate.py::test_flask_config_mirrors_the_shared_literal``,
    which asserts the Flask mirror still equals the shared literal. Hygiene
    only: it restores the value AFTER each test and changes no assertion.
    """
    original = app.config.get("PLACE_COD_COLLECTION_ENABLED")
    yield
    app.config["PLACE_COD_COLLECTION_ENABLED"] = original


# ---------------------------------------------------------------------------
# HALF 1 — widen the person rows that already exist
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_person_row_includes_a_coworkers_debt_at_the_shared_place(app, db, place):
    """THE RULE 3 REGRESSION.

    Alice owes 15 000 on her own order; Bob owes 20 000 on his, both delivered
    to addresses in the same AddressGroup. Alice's row must read 35 000 —
    "if the order is not paid at delivery then the debt is of ALL the
    coworkers' debt" (R3).
    """
    alice = User.query.get(place["a1"].user_id)
    bob = User.query.get(place["a2"].user_id)
    _delivered_cod_debt(db, alice, "ORD-E4-A", address=place["a1"], outstanding=Decimal("15000.00"))
    _delivered_cod_debt(db, bob, "ORD-E4-B", address=place["a2"], outstanding=Decimal("20000.00"))
    result = _list(app)

    alice = _person_row(result, place["a1"].user_id)
    assert alice is not None
    assert alice["total_outstanding_amount"] == 35000.0
    assert isinstance(alice["total_outstanding_amount"], (int, float))  # C6
    assert alice["active_cod_debt_count"] == 2


@pytest.mark.unit
def test_the_coworkers_row_is_widened_symmetrically(app, db, place):
    """Same office, seen from the other side. Rule 3 is not directional."""
    alice = User.query.get(place["a1"].user_id)
    bob_user = User.query.get(place["a2"].user_id)
    _delivered_cod_debt(db, alice, "ORD-E4-A", address=place["a1"], outstanding=Decimal("15000.00"))
    _delivered_cod_debt(db, bob_user, "ORD-E4-B", address=place["a2"], outstanding=Decimal("20000.00"))
    result = _list(app)

    bob = _person_row(result, place["a2"].user_id)
    assert bob is not None
    assert bob["total_outstanding_amount"] == 35000.0
    assert bob["active_cod_debt_count"] == 2


@pytest.mark.unit
def test_a_members_own_debt_is_not_double_counted(app, db, place):
    """The correctness argument. Alice's own office order is ALREADY in her
    person row (Payment.user_id == Alice) and ALSO in the place statement. Only
    items whose owner_user_id is outside her cluster may be added."""
    alice_user = User.query.get(place["a1"].user_id)
    _delivered_cod_debt(db, alice_user, "ORD-E4-A", address=place["a1"], outstanding=Decimal("15000.00"))
    result = _list(app)

    alice = _person_row(result, place["a1"].user_id)
    assert alice is not None
    assert alice["total_outstanding_amount"] == 15000.0   # NOT 30000
    assert alice["active_cod_debt_count"] == 1            # NOT 2


# ---------------------------------------------------------------------------
# HALF 2 — the debt-free coworker gets a row at all
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_debt_free_coworker_gets_a_row_carrying_the_office_debt(app, db, place):
    """🔴 THE OTHER HALF OF RULE 3. Do not delete this test.

    ONLY Alice owes — 15 000, delivered to a1. Bob owns a2 in the same group and
    has never left an order unpaid, so `_open_cod_debtors_query` returns nothing
    for him (cash_collection_service.py:1552-1561 filters on his OWN
    outstanding_amount). Before this task he had NO ROW AT ALL, and a driver
    standing in front of Bob at the office door could not collect the office's
    debt from him. Under R3 that debt is genuinely his.
    """
    alice_user = User.query.get(place["a1"].user_id)
    _delivered_cod_debt(db, alice_user, "ORD-E4-A", address=place["a1"], outstanding=Decimal("15000.00"))
    # Bob (place["a2"].user_id) creates NO order.
    result = _list(app)

    bob = _person_row(result, place["a2"].user_id)
    assert bob is not None, "a debt-free coworker at an indebted place must still be reachable"
    assert bob["total_outstanding_amount"] == 15000.0
    assert isinstance(bob["total_outstanding_amount"], (int, float))  # C6
    assert bob["active_cod_debt_count"] == 1
    assert bob["row_type"] == "person"


@pytest.mark.unit
def test_a_synthesised_row_survives_the_staff_keyboard(app, db, place):
    """Invariant 3b. `DeliveryKeyboards.cod_debtor_list` reads `c['id']` with a
    BARE subscript (staff_bot/keyboards/delivery.py:263) — its own docstring at
    :225-228 records that a row missing `id` once killed the entire list with
    KeyError. A synthesised row must be indistinguishable in shape.
    """
    from staff_bot.keyboards.delivery import DeliveryKeyboards

    alice_user = User.query.get(place["a1"].user_id)
    _delivered_cod_debt(db, alice_user, "ORD-E4-A", address=place["a1"], outstanding=Decimal("15000.00"))
    result = _list(app)
    bob = _person_row(result, place["a2"].user_id)

    markup = DeliveryKeyboards.cod_debtor_list("en", result["items"], 1, 1)
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"staff_cod_customer_{bob['id']}" in callbacks


@pytest.mark.unit
def test_a_debt_free_linked_sibling_does_not_get_a_second_row(app, db, place):
    """Cluster coverage, not member_user_ids coverage. Pinned by Step 6 inversion 4.

    Alice and Alice-2 are ONE person (same canonical_customer_id). Alice owes
    15 000; Alice-2 owns a third address in the same group and owes nothing, so
    she is absent from Alice's `member_user_ids`. A naive "is this id in any
    row?" coverage test would synthesise a row for her and the driver would see
    the same human twice.

    🔴 ASSERT ON THE SIBLING'S OWN ID -- do not weaken this back to "is a1's
    user_id in exactly one row?". The duplicate this test exists to catch
    carries `id = alice_two.id` and `member_user_ids = [alice_two.id]`: a
    DIFFERENT id (so `len(set(ids))` is unchanged) that does NOT contain
    Alice-1's (so a filter keyed on `place["a1"].user_id` still finds exactly
    one row). An earlier draft asserted only those two things and could not fail
    against the defect it names. The only assertions that can see the duplicate
    are the ones that name `alice_two`, which is why the fixture MUST bind her.
    """
    alice_one = User.query.get(place["a1"].user_id)
    _delivered_cod_debt(db, alice_one, "ORD-E4-A", address=place["a1"], outstanding=Decimal("15000.00"))
    alice_two = _user(db, "alice.two@example.com", "+998900000311")
    _address(db, alice_two, group=place["group"])   # THIRD address in the office group
    _link(db, [alice_one, alice_two])
    result = _list(app)

    people = _rows(result, "person")
    ids = [r["id"] for r in people]
    assert len(ids) == len(set(ids))
    # 🔴 The load-bearing assertion: the debt-free sibling gets NO row of her
    # own, because half 1 already put the office's debt on Alice's row.
    assert alice_two.id not in ids, "one human, one row -- the sibling is covered by Alice's row"
    # And she was not smuggled in as a second row keyed on any of her accounts
    # either: exactly ONE person row may represent the Alice cluster.
    alice_account_ids = {place["a1"].user_id, alice_two.id}
    alice_rows = [
        r for r in people
        if alice_account_ids & ({r["id"]} | set(r.get("member_user_ids") or []))
    ]
    assert len(alice_rows) == 1
    # Bob (place["a2"]) is a genuinely separate person and DOES get his own
    # synthesised row -- proof this test rejects duplicates, not synthesis.
    assert place["a2"].user_id in ids


@pytest.mark.unit
def test_a_linked_siblings_office_debt_reaches_the_person_who_owes_elsewhere(app, db, place):
    """🔴 THE SEAM BETWEEN THE TWO HALVES (invariant 3c). Do not delete this test.

    Alice-1 and Alice-2 are ONE linked person. Alice-1's only debt is 15 000 on
    an UNGROUPED home order and she owns NO address in the office group;
    Alice-2 owns an address in the office group and owes nothing; Bob owes
    20 000 at a1 in that same group.

    With half 1 discovering groups through `member_user_ids` (debtors only),
    Alice-1's row found NO group and was never widened -- and half 2 then
    SKIPPED Alice-2 because her cluster key was already covered by Alice-1's
    row. The office's 20 000 was invisible for that person entirely: neither
    half touched them. That is owner rule 3 re-opened, so this test is the pin
    that keeps both halves asking the same question.
    """
    alice_one = _user(db, "alice.one@example.com", "+998900000312")
    home = _address(db, alice_one)                    # UNGROUPED
    _delivered_cod_debt(db, alice_one, "ORD-E4-HOME", address=home, outstanding=Decimal("15000.00"))
    alice_two = _user(db, "alice.two@example.com", "+998900000313")
    _address(db, alice_two, group=place["group"])     # THIRD address in the office group
    _link(db, [alice_one, alice_two])
    bob = User.query.get(place["a1"].user_id)
    _delivered_cod_debt(db, bob, "ORD-E4-B", address=place["a1"], outstanding=Decimal("20000.00"))
    result = _list(app)

    people = _rows(result, "person")
    alice_rows = [
        r for r in people
        if alice_one.id in (r.get("member_user_ids") or [r["id"]]) or r["id"] == alice_one.id
    ]
    assert len(alice_rows) == 1, "one linked person, one row"
    # 15 000 of her own + the office's 20 000, which only Alice-2 owns her way into.
    assert alice_rows[0]["total_outstanding_amount"] == 35000.0
    assert alice_rows[0]["active_cod_debt_count"] == 2


# ---------------------------------------------------------------------------
# 🔴 THE E7 SEAM — a row must never advertise a total the collect flow refuses
#
# `get_customer_cod_statement` puts EVERY grouped address of the cluster into
# `statement["places"]`, indebted or not (cash_collection_service.py:1948-1952),
# and `CashCollectionHandler._resolve_scope_address_id` returns None when there
# is more than one — ALWAYS, since A7 removed the place screen and with it the
# only control that could ever name a place for the driver.
# No address => `_resolved_place` yields 0.0 => no widened ceiling and, for a
# debt-free member, NO COLLECT BUTTON AT ALL. So for such a cluster the list
# must stay exactly as it is today: E7's "ambiguity must not be guessed",
# applied one screen earlier.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_debt_free_member_of_two_places_gets_no_synthesised_row(app, db, place):
    """A synthesised row the driver cannot collect from is a DEAD END.

    Alice owes 15 000 at the office. Bob owns a2 in that office group AND an
    address in a second, entirely DEBT-FREE group — so his statement carries two
    `places`, `_resolve_scope_address_id` refuses to guess, `place_total` is
    0.0, and `can_collect = (own count > 0) or place_total > 0` is FALSE. The
    row would exist only to be tapped and offer nothing.

    ⚠️ A7 makes this guard COST something: the 🏢 place row used to be the
    remaining route to that debt and is now gone, so this office is collectible
    only from a member whose own cluster owns exactly one place (Alice, here).
    Widening Bob anyway would advertise a total his collect flow refuses, which
    is the split A6/A7 exist to close — so the guard stays and the cost is
    recorded rather than hidden.
    """
    alice_user = User.query.get(place["a1"].user_id)
    bob_user = User.query.get(place["a2"].user_id)
    _delivered_cod_debt(db, alice_user, "ORD-E4-A", address=place["a1"], outstanding=Decimal("15000.00"))
    _address(db, bob_user, group=_group(db, "second office"))   # debt-free 2nd place
    result = _list(app)

    assert _person_row(result, place["a2"].user_id) is None, (
        "an ambiguous-place coworker must not get a row the collect flow refuses"
    )
    # Alice owns exactly ONE group, so she is untouched by the guard and her own
    # (un-widened, nothing foreign to add) row still exists — and she is the
    # doorway to this office's debt now that the 🏢 row is gone (A7/R-F).
    assert _person_row(result, place["a1"].user_id)["total_outstanding_amount"] == 15000.0
    assert _rows(result, "place") == []


@pytest.mark.unit
def test_a_debtor_who_owns_two_places_is_not_widened(app, db, place):
    """The half-1 half of the same seam, and it costs money if it is wrong.

    Alice owes 15 000, Bob owes 20 000, both at the office. Alice ALSO owns an
    address in a second debt-free group, so her ceiling collapses to her own
    15 000 (`max(own, cluster, 0.0)`). Advertising 35 000 invites the driver to
    over-type, and the surplus posts with `delivery_address_id=None` =>
    CLUSTER/PERSONAL scope => Bob's 20 000 becomes ALICE'S PREPAID CREDIT, which
    is verbatim the failure decision E7 exists to prevent.

    Bob owns one group only, so HE is still widened — the guard is per-cluster,
    not a global off-switch.
    """
    alice_user = User.query.get(place["a1"].user_id)
    bob_user = User.query.get(place["a2"].user_id)
    _delivered_cod_debt(db, alice_user, "ORD-E4-A", address=place["a1"], outstanding=Decimal("15000.00"))
    _delivered_cod_debt(db, bob_user, "ORD-E4-B", address=place["a2"], outstanding=Decimal("20000.00"))
    _address(db, alice_user, group=_group(db, "second office"))   # debt-free 2nd place
    result = _list(app)

    alice = _person_row(result, place["a1"].user_id)
    assert alice["total_outstanding_amount"] == 15000.0, "her ceiling is 15 000; do not advertise 35 000"
    assert alice["active_cod_debt_count"] == 1
    bob = _person_row(result, place["a2"].user_id)
    assert bob["total_outstanding_amount"] == 35000.0, "one place, one unambiguous ceiling — still widened"


@pytest.mark.unit
def test_a_second_place_owned_only_by_a_linked_sibling_also_blocks_widening(app, db, place):
    """Ambiguity is a property of the PERSON, not of one phone account.

    `statement["places"]` is built from the whole cluster
    (cash_collection_service.py:1949-1952), so a second place owned only by
    Alice's debt-free linked sibling makes ALICE'S OWN screen ambiguous. A guard
    that looked only at the accounts already in play — the place members and the
    debtors — would never see that sibling and would widen Alice anyway.
    """
    alice_one = User.query.get(place["a1"].user_id)
    bob_user = User.query.get(place["a2"].user_id)
    _delivered_cod_debt(db, alice_one, "ORD-E4-A", address=place["a1"], outstanding=Decimal("15000.00"))
    _delivered_cod_debt(db, bob_user, "ORD-E4-B", address=place["a2"], outstanding=Decimal("20000.00"))
    alice_two = _user(db, "alice.two@example.com", "+998900000315")
    _address(db, alice_two, group=_group(db, "second office"))   # debt-free 2nd place
    _link(db, [alice_one, alice_two])
    result = _list(app)

    alice = _person_row(result, place["a1"].user_id)
    assert alice["total_outstanding_amount"] == 15000.0
    assert alice["active_cod_debt_count"] == 1


@pytest.mark.unit
def test_an_all_ambiguous_place_has_no_staff_bot_doorway(app, db, place):
    """⚠️ A7's COST, PINNED SO IT CANNOT BE 'FIXED' BY ACCIDENT.

    When EVERY member of an indebted place also owns an address in a second
    group, the E7 guard widens nobody and synthesises nobody — and A7 has taken
    away the 🏢 row that used to be the remaining route. The office's debt is
    then not collectible from the staff bot at all; it must be settled from the
    admin surface (`get_customer_cod_statement_for_admin`, which resolves its own
    scope) or the second grouping removed.

    This is not a defect to be patched by widening anyway: widening would
    advertise a total `_resolved_place` refuses to price, which is exactly the
    show-vs-settle split A6 and A7 exist to close. It is a cost, and it is
    written down here rather than discovered in production.
    """
    alice_user = User.query.get(place["a1"].user_id)
    bob_user = User.query.get(place["a2"].user_id)
    _delivered_cod_debt(db, alice_user, "ORD-E4-A", address=place["a1"], outstanding=Decimal("15000.00"))
    _delivered_cod_debt(db, bob_user, "ORD-E4-B", address=place["a2"], outstanding=Decimal("20000.00"))
    _address(db, alice_user, group=_group(db, "alice second office"))
    _address(db, bob_user, group=_group(db, "bob second office"))
    result = _list(app)

    # Each still shows their OWN debt, and nothing of the other's.
    assert _person_row(result, place["a1"].user_id)["total_outstanding_amount"] == 15000.0
    assert _person_row(result, place["a2"].user_id)["total_outstanding_amount"] == 20000.0
    # And there is no third row through which the office's 35 000 is reachable.
    assert _rows(result, "place") == []
    assert all(r["total_outstanding_amount"] != 35000.0 for r in _rows(result, "person"))


# ---------------------------------------------------------------------------
# The majority case, the gate, and ordering
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_ungrouped_debtor_is_untouched(app, db, sample_user, user_address):
    """Spec 2.2: no admin group, no shared debt. This is the majority case and
    it must be bit-for-bit today's behaviour.

    `user_address` (tests/conftest.py:312) is `sample_user`'s and is UNGROUPED
    (`address_group_id` is never set), which is exactly the condition.
    """
    from business_app.services.cash_collection_service import CashCollectionService

    _delivered_cod_debt(db, sample_user, "ORD-E4-SOLO", address=user_address,
                        outstanding=Decimal("12000.00"))
    result = _list(app)
    # Nothing is grouped, so the engine's paginator prepends no place rows and
    # the two are still identical, block for block.
    expected = CashCollectionService().paginate_users_with_open_cod_debts(page=1, per_page=50)

    assert result == expected
    assert _person_row(result, sample_user.id)["total_outstanding_amount"] == 12000.0


@pytest.mark.unit
def test_gate_off_is_the_engines_person_rows_and_nothing_else(app, db, place):
    """Invariant 2, under A7. With the gate off the composition must return the
    engine's OWN person rows — same rows, same order — with no widening and no
    synthesis, so the rollback path stays a true money no-op.

    It is no longer equality with `paginate_users_with_open_cod_debts`, because
    that method still prepends the 🏢 place rows A7 deleted from this surface.
    Equality is asserted against the engine's person-row reader instead, which is
    the very list that paginator is built from
    (cash_collection_service.py:1815-1816) — so "the engine decides who is on the
    list" is still pinned, just without the family the driver can no longer tap.
    """
    from business_app.services.cash_collection_service import CashCollectionService

    alice_user = User.query.get(place["a1"].user_id)
    _delivered_cod_debt(db, alice_user, "ORD-E4-A", address=place["a1"], outstanding=Decimal("15000.00"))
    # Bob (place["a2"].user_id) is debt-free — the half-2 fixture.
    result = _list(app, gate=False)

    engine = CashCollectionService()
    assert result["items"] == engine.list_users_with_open_cod_debts(limit=1000)[:50]
    # The place rows the engine's own paginator would prepend are absent, and the
    # pagination block counts the list the driver actually sees.
    assert engine.get_place_cod_debtor_rows(), "the fixture must have an indebted place"
    assert _rows(result, "place") == []
    assert result["pagination"]["total"] == len(result["items"])
    # The half-2 row must NOT exist with the gate off — that is what makes
    # rollback a true no-op.
    assert _person_row(result, place["a2"].user_id) is None


@pytest.mark.unit
def test_no_places_means_identical_output_with_the_gate_on(app, db, sample_user, user_address):
    """Invariant 3. Both halves are no-ops when nothing is grouped, INCLUDING
    the row order — so the delegate equality must still hold with the gate ON."""
    from business_app.services.cash_collection_service import CashCollectionService

    _delivered_cod_debt(db, sample_user, "ORD-E4-SOLO", address=user_address,
                        outstanding=Decimal("12000.00"))
    assert _list(app, gate=True) == CashCollectionService().paginate_users_with_open_cod_debts(
        page=1, per_page=50
    )


@pytest.mark.unit
def test_person_rows_are_resorted_on_the_widened_total(app, db, place):
    """Invariant 4. Bob owes 1 000 of his own but shares a place where Alice
    owes 90 000; an unrelated ungrouped debtor owes 50 000. Bob's widened total
    is 91 000, so he must now rank ABOVE the 50 000 debtor."""
    alice_user = User.query.get(place["a1"].user_id)
    bob_user = User.query.get(place["a2"].user_id)
    _delivered_cod_debt(db, alice_user, "ORD-E4-A", address=place["a1"], outstanding=Decimal("90000.00"))
    _delivered_cod_debt(db, bob_user, "ORD-E4-B", address=place["a2"], outstanding=Decimal("1000.00"))
    other = _user(db, "other.debtor@example.com", "+998900000314")
    _delivered_cod_debt(db, other, "ORD-E4-OTHER", outstanding=Decimal("50000.00"))
    result = _list(app)

    people = _rows(result, "person")
    order = [r["id"] for r in people]
    bob_row = _person_row(result, place["a2"].user_id)
    assert bob_row["total_outstanding_amount"] == 91000.0
    # Widened totals: Alice 91 000 (her 90 000 + Bob's foreign 1 000), Bob 91 000
    # (his 1 000 + Alice's foreign 90 000), other 50 000. The two office rows TIE
    # at 91 000 -- deliberately, since rule 3 makes the place's debt the same
    # number for both -- so assert only that Bob outranks `other`, never that
    # Alice outranks Bob.
    assert order.index(bob_row["id"]) < order.index(other.id)


@pytest.mark.unit
@pytest.mark.parametrize("gate", [True, False])
def test_the_staff_list_never_emits_a_place_row(app, db, place, gate):
    """🔴 OWNER RULING A7 — THE PIN. Do not delete this test.

    Superseded `test_place_rows_still_come_first_and_keep_their_own_totals`,
    which asserted the opposite (`items[0]["row_type"] == "place"`) about the
    surface A7 removed.

    The engine still HAS the place row — it is a pre-existing engine concept and
    C1 freezes it — so this test asks the engine's own paginator for the same
    data and proves it DOES emit one. The absence is therefore the staff
    composition's doing, not an accident of the fixture.

    Asserted in BOTH gate states, because the staff bot has no
    `staff_cod_place_<id>` handler in either: emitting the row would ship a dead
    button and inflate the pagination block with untappable rows.
    """
    from business_app.services.cash_collection_service import CashCollectionService

    alice_user = User.query.get(place["a1"].user_id)
    bob_user = User.query.get(place["a2"].user_id)
    _delivered_cod_debt(db, alice_user, "ORD-E4-A", address=place["a1"], outstanding=Decimal("15000.00"))
    _delivered_cod_debt(db, bob_user, "ORD-E4-B", address=place["a2"], outstanding=Decimal("20000.00"))
    result = _list(app, gate=gate)

    engine_rows = CashCollectionService().paginate_users_with_open_cod_debts(
        page=1, per_page=50
    )["items"]
    assert [r for r in engine_rows if r.get("row_type") == "place"], (
        "the engine must still emit a place row, or this test proves nothing"
    )
    assert _rows(result, "place") == []
    assert all(r.get("row_type") == "person" for r in result["items"])
    assert all(r.get("id") is not None for r in result["items"]), (
        "every emitted row must be renderable by DeliveryKeyboards.cod_debtor_list"
    )
    assert result["pagination"]["total"] == len(result["items"])
