"""Structural pins for the bottle place machinery: lock ordering, and the
single-caller fence on the one balance-DECOUPLED ledger writer.

The abort is worse than a 500 — `_allocate_to_payment` may already have enqueued
`send_payment_confirmation_task`, which does not roll back, so the customer is
told a rolled-back payment was confirmed (staff_service.py:1276-1281).

NO SQLITE TEST MAY BE CITED AS EVIDENCE FOR ANY CLAIM ABOUT THIS LADDER.
`with_for_update()` compiles to NOTHING on SQLite and `PRAGMA foreign_keys` is
off there, so every lock in the four-rung ladder is a no-op in the fast suite
and a green run says nothing whatsoever about whether the fence holds. The
dynamic evidence lives in `tests/integration/test_place_concurrency_pg_e2e.py`,
against a real migrated Postgres.

What CAN be pinned here is the CODE SHAPE, and that is what this module does —
because the realistic six-month failure mode is somebody "simplifying"
`resolve_scope_for_write` back into `resolve_scope`, or dropping `read=True` /
`key_share=True` / `populate_existing()`, and only a Postgres run noticing, if
one is still being made.

THE LADDER (spec §5.2, revised):

    rung 0  `address_groups` row      FOR NO KEY UPDATE   (lifecycle only)
    rung 1  `addresses` rows          FOR SHARE (writers) / FOR NO KEY UPDATE
                                      (lifecycle), ascending id, ONE statement
    rung 2  `bottle_balances` GROUP row    FOR UPDATE
    rung 3  `bottle_balances` ADDRESS row  FOR UPDATE

...and the whole ladder sits BELOW the payment/settlement locks.
"""
import inspect
from pathlib import Path

from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.services.order_edit_service import OrderEditService

# Source trees a call to a service-private helper could plausibly appear in.
_SOURCE_ROOTS = ("business_app", "telegram_bot", "staff_bot", "shared", "scripts")


def test_cash_cascade_runs_before_bottle_cascade():
    src = inspect.getsource(OrderEditService.apply_edit)
    cash_pos = src.index("self._cascade_cash(")
    bottle_pos = src.index("self._cascade_bottle(")
    assert cash_pos < bottle_pos, (
        "The bottle scope row is a place-wide lock and must be taken AFTER every "
        "payment lock, matching the delivery path (staff_service.py:1283). "
        "Reordering these re-opens the cross-resource deadlock."
    )


def test_staff_delivery_path_still_locks_payments_first():
    from business_app.services.staff_service import StaffService

    src = inspect.getsource(StaffService.update_delivery_status)
    assert "lock_order_settlement_candidates" in src
    assert src.index("lock_order_settlement_candidates") < src.index("OrderService().update_order_status")


def test_rung_1_writer_resolver_exists_and_uses_FOR_SHARE():
    """`resolve_scope_for_write` is the writer's rung 1, and its MODE is pinned.

    `read=True` compiles to `FOR SHARE`. That exact mode is load-bearing and was
    chosen against a verified conflict matrix on this project's Postgres 17:

      * plain `FOR UPDATE` blocks INSERTs into all SIX of `addresses`' FK
        children (`orders`, `subscriptions`, `bottle_balances`, `bottle_ledger`,
        `bottle_fines`, `place_suggestion_dismissals`) — so every bottle write
        would stall order and subscription creation at that address;
      * `FOR KEY SHARE` does NOT block `UPDATE addresses SET address_group_id`,
        i.e. it is not a fence at all — a silent no-op;
      * `FOR SHARE` blocks the membership UPDATE and the lifecycle's
        `FOR NO KEY UPDATE`, while staying compatible with itself (so N
        deliveries at one address do not serialise on the mapping) and with the
        FK-child inserts.

    A reviewer "simplifying" `read=True` away re-introduces the first of those.
    """
    src = inspect.getsource(BottleTrackingService.resolve_scope_for_write)
    assert ".with_for_update(read=True)" in src, (
        "the writer's rung-1 lock must be FOR SHARE (`read=True`). Plain "
        "FOR UPDATE here blocks order and subscription creation at the address."
    )
    # Queries COLUMNS, not the entity — immune by construction to the identity
    # map, which is why this resolver needs no populate_existing() and the
    # lifecycle's entity load does.
    assert "db.session.query(UserAddress.id, UserAddress.address_group_id)" in src


def test_get_or_create_balance_is_the_funnel_that_takes_rung_1_before_the_balance_row():
    """The fence is a property of the FUNCTION, not of 13 call sites.

    `scope is None` -> resolve under the lock FIRST, then pick the row, so
    nothing is left to re-validate. `scope is not None` -> assert the caller
    already holds it. Both arms must be present, and the resolver used must be
    the LOCKING one: swapping `resolve_scope_for_write` back to `resolve_scope`
    ships the fix as a silent no-op that every SQLite test still passes.
    """
    src = inspect.getsource(BottleTrackingService.get_or_create_balance)
    resolve_pos = src.index("resolve_scope_for_write(address_id)")
    row_pos = src.index("BottleBalance.query.filter(*criteria).with_for_update()")
    assert resolve_pos < row_pos, (
        "rung 1 (`addresses`) must be taken BEFORE rung 2/3 (`bottle_balances`). "
        "Taking the address row after the balance row is the exact reverse of "
        "the lifecycle and manufactures an ABBA on the delivery path."
    )
    assert "assert_scope_locked(scope, address_id)" in src, (
        "an EXPLICIT scope cannot be self-served; it must be asserted against "
        "the registry, or a caller can write under no lock at all"
    )
    assert "assert_reachable(scope)" in src


def test_the_admin_attribution_funnel_takes_rung_1_itself():
    """`_authorised_place_attribution` is the ONE acquirer the admin bodies now
    delegate to, so the source scan below accepts it in place of a direct call.
    That delegation is only safe while the funnel really takes rung 1 — pinned
    here so the indirection cannot become a hiding place.

    The membership fence is asserted alongside it deliberately: the funnel
    exists because two of the three admin write bodies had the lock and NOT the
    fence, which is how an out-of-scope (and even a nonexistent) `user_id`
    reached `bottle_ledger.user_id`, a NOT NULL FK.
    """
    src = inspect.getsource(BottleTrackingService._authorised_place_attribution)
    lock_pos = src.index("resolve_scope_for_write(address_id)")
    fence_pos = src.index("_assert_user_in_scope(user_id, address_id, scope=scope)")
    derive_pos = src.index("resolve_place_attribution_user_id(address_id, scope=scope)")
    assert lock_pos < derive_pos < fence_pos, (
        "the funnel must LOCK, then DERIVE, then FENCE: deriving after the "
        "fence would authorise a user the write does not use, and fencing "
        "before the derivation would reject the legitimate absent-user_id case"
    )


def test_every_bottle_write_entry_point_takes_rung_1():
    """No write path resolves its scope without locking the mapping first.

    The implicit-scope writers get it free from `get_or_create_balance`. The
    ones listed here pass an EXPLICIT scope (or build a key/guard off the scope
    before writing), so they must take rung 1 themselves — and the registry
    assertion in `get_or_create_balance` would refuse them at runtime if they
    did not, which is what makes this pin cheap rather than load-bearing.

    UPDATED: `set_initial_balance` and `issue_fine` now take rung 1 THROUGH
    `_authorised_place_attribution`, the shared funnel the admin write bodies
    were consolidated onto when two of them turned out to be missing the
    membership fence. Either spelling counts — but only these two, so a method
    that takes NEITHER still fails, and the funnel's own acquisition is pinned
    by `test_the_admin_attribution_funnel_takes_rung_1_itself` above.

    UPDATED AGAIN: `waive_fine` and `mark_fine_paid` take rung 1 through
    `resolve_frozen_scope_for_write`, the FROZEN-write funnel. That is not a
    third way of spelling the same thing — it is the ONLY correct spelling for
    them. When the frozen place has dissolved, the write forwards onto another
    address, and BOTH addresses must be locked in one ascending statement (spec
    §5.2); a bare `resolve_scope_for_write` in these two bodies would take the
    fine's address first and re-open the ABBA against the lifecycle that the
    funnel exists to close. The funnel's own acquisition is pinned by
    `test_the_frozen_write_funnel_locks_both_addresses_in_one_statement`.

    Note `resolve_frozen_scope_for_write(` also CONTAINS no substring match for
    `resolve_scope_for_write(`, so it has to be listed explicitly — and
    `_fine_scope` is deliberately NOT an acquirer; see
    `test_the_fine_DISPLAY_scope_takes_no_lock_at_all`.
    """
    acquirers = (
        "resolve_scope_for_write(",
        "_authorised_place_attribution(",
        "resolve_frozen_scope_for_write(",
    )
    for method in (
        BottleTrackingService.set_initial_balance,
        # ADDED: `admin_adjust_balance` used to be an implicit-scope writer that
        # got rung 1 free from `get_or_create_balance`. It now resolves once
        # through the funnel and passes that scope EXPLICITLY, so it belongs in
        # this list by the same rule as its two siblings.
        BottleTrackingService.admin_adjust_balance,
        BottleTrackingService.issue_fine,
        BottleTrackingService.waive_fine,
        BottleTrackingService.mark_fine_paid,
        BottleTrackingService.reconcile_balance,
        BottleTrackingService.record_standalone_collection,
    ):
        src = inspect.getsource(method)
        assert any(a in src for a in acquirers), (
            f"{method.__qualname__} writes bottles without taking rung 1"
        )


def test_the_fine_DISPLAY_scope_takes_no_lock_at_all():
    """A label is never worth a row lock, and this one is rendered PER ROW.

    `serialize_bottle_fine_row` calls `_fine_scope` for every fine in the admin
    fines table. Routing that through `resolve_frozen_scope_for_write` — the
    write funnel — would take rung-1 `FOR SHARE` on up to two `addresses` rows
    per rendered fine, put a read-only list endpoint into contention with the
    place lifecycle, and let `GET /admin/bottles/fines` fail with
    `BOTTLE_SCOPE_BUSY` while an admin merges a place. It also returns a
    `FrozenScopeTarget`, which has no `balance_filter()`, so the serializer 500s
    outright. Both happened; this pin is why they cannot happen twice.

    The two questions are genuinely different, which is why there are two
    functions: the READER wants the place the fine NAMES (frozen, for the
    label), and the WRITER wants the place that holds the CRATES (forwarded,
    after a dissolve).
    """
    import ast
    import textwrap

    src = inspect.getsource(BottleTrackingService._fine_scope)
    # The DOCSTRING names the funnel (to say why it is not used), so the pin has
    # to read the CODE. Every call this function actually makes, by name:
    fn = ast.parse(textwrap.dedent(src)).body[0]
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
    }
    forbidden = {
        "resolve_frozen_scope_for_write",
        "resolve_scope_for_write",
        "with_for_update",
        "_authorised_place_attribution",
    }
    assert not (called & forbidden), (
        "_fine_scope is a lock-free display accessor called per rendered row; it "
        f"must not enter the ladder, but it calls {sorted(called & forbidden)}"
    )
    assert "BottleScope.for_group(fine.address_group_id)" in src, (
        "the display scope is the FROZEN one, read straight off the fine's own "
        "columns"
    )
    # ...and the serializer must keep getting something it can filter with.
    from business_app.serializers import bottle_serializers

    ser = inspect.getsource(bottle_serializers.serialize_bottle_fine_row)
    assert "scope.balance_filter()" in ser, (
        "the serializer filters on the returned scope, so _fine_scope must "
        "return a BottleScope — not the write funnel's FrozenScopeTarget"
    )


def test_the_frozen_write_funnel_locks_both_addresses_in_one_statement():
    """The forwarding arm's rung 1 is ONE ascending acquisition, never two.

    `resolve_frozen_scope_for_write` is the only place in the codebase that has
    to lock TWO `addresses` rows for a single bottle write: the frozen
    reference's own address, and the address a dissolved place forwarded its
    history onto. Two `FOR SHARE` holders never block each other, so the hazard
    is not writer-against-writer — it is the LIFECYCLE, which takes
    `FOR NO KEY UPDATE` over its whole member set ascending. A forwarding writer
    that took `addresses(anchor)` and THEN `addresses(survivor)` out of id order
    is a textbook ABBA against it the moment both addresses have since joined the
    same place.

    `LockRows` sits above `Sort` in the plan, so ordering the query orders the
    acquisition — which is why the fix is a single `IN (...) ORDER BY id`
    statement rather than a careful sequence of two. `with_for_update()` compiles
    to NOTHING on SQLite, so no functional test in the fast suite can see any of
    this; the static pin is the evidence.
    """
    src = inspect.getsource(BottleTrackingService.resolve_frozen_scope_for_write)
    assert "sorted({anchor_id, survivor_id})" in src, (
        "the two addresses must be de-duplicated and SORTED before they are locked"
    )
    lock_block = src[src.index("wanted = sorted("):]
    assert lock_block.count(".with_for_update(read=True)") == 1, (
        "the forwarding arm must take rung 1 in ONE statement — a second "
        "acquisition re-opens the ABBA against the lifecycle"
    )
    assert lock_block.index(".filter(UserAddress.id.in_(wanted))") < lock_block.index(
        ".with_for_update(read=True)"
    )
    assert ".order_by(UserAddress.id.asc())" in lock_block, (
        "LockRows sits above Sort: without ORDER BY id the acquisition order is "
        "whatever the plan happens to produce"
    )


def test_the_order_edit_cascade_resolves_the_FUNNEL_before_its_own_rung_1():
    """Funnel FIRST, bare acquisition second — and only on the arm that needs it.

    `_cascade_bottle` used to take `resolve_scope_for_write(delivery_address_id)`
    and only then call `_frozen_bottle_scope`. On the forwarding arm that is the
    out-of-order two-step `resolve_frozen_scope_for_write`'s single statement
    exists to prevent: anchor locked alone, survivor locked afterwards, ABBA
    against the lifecycle whenever `survivor_id < anchor_id`.

    The funnel already takes rung 1 on the anchor on every arm that returns a
    target, so the bare acquisition is correct for exactly one case — an order
    whose delivery booked no bottles, where there is no frozen episode and the
    funnel returns None.
    """
    src = inspect.getsource(OrderEditService._cascade_bottle)
    funnel_pos = src.index("self._frozen_bottle_scope(order)")
    bare_pos = src.index("resolve_scope_for_write(order.delivery_address_id)")
    assert funnel_pos < bare_pos, (
        "_cascade_bottle must resolve the FROZEN-write funnel before taking rung "
        "1 itself; the funnel needs both addresses in one ascending statement"
    )
    assert "if target is None:" in src[funnel_pos:bare_pos], (
        "the bare acquisition must be guarded on the no-frozen-episode arm — "
        "unconditionally re-taking it is the out-of-order two-step again"
    )


def test_the_order_edit_cascade_UNWRAPS_the_funnels_answer():
    """A `FrozenScopeTarget` is not a `BottleScope`, and passing it is silent-ish.

    `_frozen_bottle_scope` returns the funnel's full answer — scope, attribution
    address, and whether the write was forwarded. Handing that object straight to
    `_create_ledger_entry(scope=...)` raises `AttributeError: 'FrozenScopeTarget'
    object has no attribute 'balance_filter'` deep inside `get_or_create_balance`,
    which `_cascade_bottle` catches and re-raises as a generic "Bottle adjust
    failed" — i.e. every post-delivery correction 400s with a message that names
    the product rather than the cause. It shipped exactly once.

    The ATTRIBUTION half is the one no exception would have caught. An address
    scope's ledger predicate is `address_id = X AND address_group_id IS NULL`, so
    a forwarded entry attributed to the anchor while its balance moved on the
    survivor's row puts the entry and the balance in two different scopes and
    drifts both — self-consistently, and invisibly to global conservation.
    """
    src = inspect.getsource(OrderEditService._cascade_bottle)
    assert "target.scope if target is not None else None" in src, (
        "the funnel's answer must be UNWRAPPED to a BottleScope before it is "
        "passed as scope="
    )
    assert "target.address_id if target is not None" in src, (
        "a forwarded ledger row must be ATTRIBUTED to the survivor — scope and "
        "attribution are the same fact for an address scope"
    )
    assert "target.audit()" in src, (
        "the door the episode came through must survive in the entry metadata"
    )
    assert "address_id=booking_address_id," in src


def test_reconcile_reads_the_ledger_sum_BELOW_the_lock():
    """The one place where statement ORDER is the entire fix.

    Reading `SUM(bottle_ledger.quantity)` before taking the balance row compares
    a FRESH balance against a STALE sum — two figures that never described the
    same world — and reconcile is the only balance writer that appends no ledger
    entry, so it silently eats whatever committed while it waited.
    """
    src = inspect.getsource(BottleTrackingService.reconcile_balance)
    lock_pos = src.index("self.get_balance_row(scope)")
    sum_pos = src.index("func.sum(BottleLedger.quantity)")
    assert lock_pos < sum_pos, (
        "reconcile_balance must take the balance row FOR UPDATE BEFORE it sums "
        "the ledger"
    )
    assert src.index("resolve_scope_for_write(address_id)") < lock_pos


def test_the_lifecycle_locking_load_carries_populate_existing_and_an_id_ORDER():
    """The single easiest way to ship this fix as a no-op.

    `with_for_update()` does NOT imply `populate_existing()`. SQLAlchemy re-reads
    the row in the database, acquires the lock correctly, and then DISCARDS the
    columns when the object is already in the identity map — so
    `_assert_place_group_eligible` evaluates `address_group_id` on the PRE-IMAGE
    and the join race is not closed at all, with every lock in place. Verified on
    this project's Postgres + SQLAlchemy 2.0.43. `Session.get()` is worse: it
    emits no SQL whatsoever.

    `ORDER BY id` in ONE statement is the other half: `LockRows` sits above
    `Sort` in the plan, so ordering the query orders the lock acquisition.
    Without it, two joins over {A,B} and {B,A} are an ABBA.

    `key_share=True` compiles to `FOR NO KEY UPDATE` — it fences identically to
    `FOR UPDATE` here while letting order/subscription creation at member
    addresses through.

    HONEST LIMIT, MEASURED. The design asked for a BREAK-TEST: remove
    `populate_existing()` and show the join-race test goes red. IT DOES NOT.
    Removing it and re-running
    `test_place_merge_review_full_e2e.py::test_two_concurrent_joins_of_the_same_
    address_must_not_both_commit` (and its REAL_POSTGRES sibling) leaves both
    GREEN. The reason is not that the trap is imaginary — the probe behind it is
    real and reproducible — it is that no path in this codebase currently reaches
    `_load_addresses` with those `UserAddress` entities already in the session's
    identity map: every admin route resolves membership through COLUMN queries
    (`get_address_place_group_id`, `remove_address_from_group`'s step-1 read),
    so the locking load is always the first entity load and there is nothing
    stale to discard.

    `populate_existing()` therefore stays as a REQUIREMENT ON THE SHAPE, pinned
    here, and NOT as something a green suite validates. The day a caller loads
    the address first — a serializer, a permission check, a new route — its
    absence would silently re-open the join race with every lock still correctly
    acquired. That is exactly the failure this pin exists to prevent, and it is
    the one part of the ladder no runtime test in this repository can catch.
    """
    for method in (CustomerLinkService._load_addresses, CustomerLinkService._lock_place_group):
        src = inspect.getsource(method)
        assert ".populate_existing()" in src, (
            f"{method.__qualname__} locks without populate_existing() — it will "
            "evaluate the fence on the stale identity-map value and close nothing"
        )
        assert ".with_for_update(key_share=True)" in src, (
            f"{method.__qualname__} must use FOR NO KEY UPDATE, not plain FOR UPDATE"
        )
    load_src = inspect.getsource(CustomerLinkService._load_addresses)
    assert ".order_by(UserAddress.id.asc())" in load_src
    assert load_src.index(".order_by(") < load_src.index(".with_for_update("), (
        "the ORDER BY and the lock must be one statement"
    )
    # Query.get() reads the identity map and may emit no SQL at all; it must
    # never appear on a fenced path.
    assert "UserAddress.query.get(" not in inspect.getsource(
        CustomerLinkService.remove_address_from_group
    ), "remove_address_from_group must read its address off the LOCKED member set"


def test_the_removal_climbs_rung_0_then_rung_1_then_rung_2_in_that_order():
    """The relocation that kills the confirmed 40P01.

    The old shape took `addresses(A)` -> `bottle_balances(G)` -> `addresses(B)`,
    straddling rung 2 with rung 1 on both sides; two removals from a two-member
    place were a textbook ABBA. The new header takes the group row, then the
    WHOLE member set (which contains both A and B), then the bottle rows — so
    `_dissolve_if_last_member`'s survivor un-point is a lock UPGRADE, not a
    fresh acquisition below rung 2.

    The cap read is hoisted under rung 2 in the same move, which closes the
    "a concurrent return pushes a validated split past the cap" defect for free.
    """
    src = inspect.getsource(CustomerLinkService.remove_address_from_group)
    rung0 = src.index("self._lock_place_group(group_id)")
    rung1 = src.index("UserAddress.address_group_id == group_id")
    rung2 = src.index("get_balance_row(BottleScope.for_group(group_id))")
    cap = src.index("self._validated_bottles_leaving(")
    assert rung0 < rung1 < rung2 < cap, (
        "the removal must climb address_groups -> addresses -> bottle_balances "
        "and validate the §7.1 cap UNDER the group balance row's lock; "
        f"got rung0={rung0} rung1={rung1} rung2={rung2} cap={cap}"
    )
    assert ".with_for_update(key_share=True)" in src, (
        "the member set must be locked FOR NO KEY UPDATE in one ORDER BY id "
        "statement — that predicate lock is sound only because rung 0 pins the set"
    )


def test_create_place_group_never_ADOPTS_an_existing_group_id():
    """`create_place_group`'s rung-0 exemption is an EXISTENCE claim, and only that.

    The `address_groups` row does not exist until this transaction INSERTs it,
    and a row that does not exist cannot be locked by anybody. That is why no
    rung-0 acquisition is needed here — NOT because "no other transaction will
    do X", which is the exact shape of argument that was accepted in this
    feature once before and later proven false.

    The claim is only true while this path never adopts an existing id. If it
    ever does, it must take rung 0 via `_lock_place_group` first.
    """
    src = inspect.getsource(CustomerLinkService.create_place_group)
    assert "AddressGroup(canonical_customer_id=None, label=label)" in src
    assert "AddressGroup.query.get(" not in src
    assert "_lock_place_group(" not in src, (
        "if create_place_group ever adopts an existing group id, this exemption "
        "is void and it must take rung 0 first — update the argument, not the test"
    )


def test_no_advisory_locks_anywhere_in_the_bottle_or_place_machinery():
    """Row locks, not advisory locks, and the difference is not stylistic.

    An advisory lock cannot stop a writer nobody enumerated: a plain
    `UPDATE addresses SET address_group_id = …` from a script, a data fix or a
    future route sails straight past it, while it BLOCKS against a held
    `FOR SHARE` (verified). "A missed acquisition site" is precisely the risk row
    locks structurally remove.
    """
    root = Path(__file__).resolve().parents[2]
    hits = []
    for tree in _SOURCE_ROOTS:
        for path in sorted((root / tree).rglob("*.py")):
            if "pg_advisory" in path.read_text(encoding="utf-8"):
                hits.append(str(path.relative_to(root)))
    assert hits == [], f"advisory locks are not the mechanism here: {hits}"


def test_the_order_edit_cascade_takes_rung_1_and_FREEZES_its_scope():
    """A correction belongs to the EPISODE it corrects, not to today's geography.

    Passing no scope re-resolves live, so an edit to an already-DELIVERED order
    books into whatever place the address belongs to NOW — the coworkers' pool
    if it joined one since, its own scope if it left one — while the original
    delivery stays where it was. The `+n` and the `-n` of one physical handover
    then sit in two different ledgers.

    Rung 1 is taken inside `_cascade_bottle`, i.e. at the start of the BOTTLE
    work, never at the top of `apply_edit`; see
    `test_cash_cascade_runs_before_bottle_cascade`.
    """
    src = inspect.getsource(OrderEditService._cascade_bottle)
    assert "resolve_scope_for_write(order.delivery_address_id)" in src
    assert "scope=frozen_scope," in src, (
        "_cascade_bottle must pass the FROZEN scope of the delivery it corrects"
    )
    frozen = inspect.getsource(OrderEditService._frozen_bottle_scope)
    assert 'f"delivery:{order.id}"' in frozen
    assert "BOTTLE_CORRECTION_SCOPE_NOT_LIVE" in frozen, (
        "the dissolve-onto-a-different-survivor arm must REFUSE by name rather "
        "than re-minting an orphan balance row"
    )


def test_the_lifecycle_re_stamps_bottle_fines_alongside_bottle_ledger():
    """FREEZE is only coherent if the lifecycle CARRIES the frozen references.

    `bottle_fines` used to freeze its scope at issue and never move again, while
    the order-edit cascade re-resolved live. Those two policies cannot both be
    right. Now both freeze, and both are carried by the join and the dissolve.
    """
    absorb = inspect.getsource(BottleTrackingService.absorb_address_into_group)
    release = inspect.getsource(BottleTrackingService.release_group_history_to_address)
    for name, src in (("absorb_address_into_group", absorb), ("release_group_history_to_address", release)):
        assert "BottleFine.address_group_id" in src, (
            f"{name} re-stamps bottle_ledger but leaves bottle_fines behind, so a "
            "frozen fine scope points at a place its address has left"
        )


def test_absorb_joiners_locks_group_row_before_calling_into_address_absorb():
    """Join path (Plan C Task 3): group row FOR UPDATE before the address row's.

    `_absorb_joiners_into_group` takes the destination group's `bottle_balances`
    row FOR UPDATE itself, then calls `BottleTrackingService.absorb_address_into_group`
    per joiner — which is the function that takes the address's own row FOR
    UPDATE (in bottle_tracking_service.py; see the test below). The two locks
    live in different functions, so this pins the only place their relative
    order is visible: the group lock must be taken before the call that leads
    to the address lock, not after. Reversing this re-opens the exact ABBA
    deadlock `_split_bottles_out_of_place`'s docstring warns about, between a
    concurrent join and removal.
    """
    src = inspect.getsource(CustomerLinkService._absorb_joiners_into_group)
    group_lock_pos = src.index("place_scope.balance_filter()).with_for_update()")
    absorb_call_pos = src.index("bottles.absorb_address_into_group(")
    assert group_lock_pos < absorb_call_pos, (
        "The group's bottle_balances row must be locked FOR UPDATE before "
        "calling into absorb_address_into_group (which locks the address's "
        "own row). Taking the address lock first would deadlock against a "
        "concurrent place removal, which locks the group row first."
    )


def test_release_group_history_locks_the_group_row_before_the_address_row():
    """Dissolve path (Plan C Task 4): the THIRD two-row acquisition.

    `release_group_history_to_address` takes the dissolving GROUP's
    `bottle_balances` row FOR UPDATE (via `get_or_create_balance`, so it holds
    the row even when the place never carried one) and only then the SURVIVING
    address's. Taking them the other way round is an ABBA deadlock against a
    concurrent join, which locks the group row first.

    Note the group row is acquired UNCONDITIONALLY at the top, not lazily on the
    `own_sum != 0` branch: a late acquisition would put rung 2 after rung 3 on
    this path. `with_for_update()` is a no-op on SQLite, so a green functional
    test proves nothing about locking — this static pin is the evidence.

    `allow_memberless=True` is part of the pinned text on purpose. It is the ONE
    sanctioned violation of `assert_reachable`, and it is not optional: the §7.3
    zero-remaining arm clears the departing address's pointer BEFORE calling in,
    so without the flag the dissolve 500s on an ordinary last-member removal.
    """
    src = inspect.getsource(BottleTrackingService.release_group_history_to_address)
    group_lock_pos = src.index(
        "get_or_create_balance(address_id, scope=scope_g, allow_memberless=True)"
    )
    address_lock_pos = src.index("get_or_create_balance(address_id, scope=scope_a)")
    assert group_lock_pos < address_lock_pos, (
        "The dissolving group's bottle_balances row must be locked FOR UPDATE "
        "before the surviving address's own row. Reversing this re-opens the "
        "ABBA deadlock against a concurrent place join."
    )
    # ...and the paired ADMIN_ADJUSTMENT keeps the same order: `:out` on the
    # group scope before `:in` on the address scope.
    assert src.index("scope=scope_g,\n                idempotency_key=") < src.index(
        "scope=scope_a,\n                idempotency_key="
    ), "The dissolve's `:out` half (group scope) must be written before its `:in` half."


def test_merge_review_validates_before_the_absorb_and_corrects_after_it():
    """Merge review (Plan C Task 5): the two halves straddle the absorb.

    The GUARDS must run against the PRE-absorb world — after the absorb every
    joiner's entries carry the group and the §7.2 selector
    (`address_id = a AND address_group_id IS NULL`) finds nothing, so the
    preview being validated would no longer be the one the admin saw, and the
    staleness and eligibility checks would both silently pass on an empty set.
    They must also precede the first WRITE, so a rejected merge leaves no
    flushed `AddressGroup` for the next commit on this session to adopt.

    The CORRECTIONS must run after it: they are scoped to the group, and their
    `recompute_balance_after` pass has to see the absorbed history plus the
    adjustments, in that order.
    """
    from business_app.services.customer_link_service import CustomerLinkService

    for entry_point in (CustomerLinkService.create_place_group,
                        CustomerLinkService.add_addresses_to_group):
        src = inspect.getsource(entry_point)
        validate_pos = src.index("self._validate_merge_review(")
        absorb_pos = src.index("self._absorb_joiners_into_group(")
        apply_pos = src.index("self._apply_merge_review(")
        assert validate_pos < absorb_pos < apply_pos, (
            f"{entry_point.__qualname__}: the §7.4 guards must run before the absorb "
            "(the preview selector is pre-absorb, and a rejection must not have "
            "written anything) and the corrections after it."
        )


def test_merge_review_corrections_take_only_the_group_row():
    """Narrowness guard (cannot be red-first): `_apply_merge_review` must stay a
    SINGLE-row acquirer.

    Every ledger entry it appends is written on `BottleScope.for_group(...)`, so
    the only `bottle_balances` row it touches is the destination group's — the
    row the join path already holds. Adding an address-scoped write here would
    silently make it the FOURTH two-row acquisition in the codebase, alongside
    `_split_bottles_out_of_place`, `_absorb_joiners_into_group` and
    `release_group_history_to_address`, without anyone re-deriving the ordering
    or the membership fence. `with_for_update()` is a no-op on SQLite, so no
    functional test can catch that; this static pin is the evidence.
    """
    from business_app.services.customer_link_service import CustomerLinkService

    src = inspect.getsource(CustomerLinkService._apply_merge_review)
    assert "BottleScope.for_group(group_id)" in src
    assert "for_address(" not in src, (
        "_apply_merge_review must write only on the group scope. An address-scoped "
        "write makes it a two-row acquirer and needs the lock-ordering and "
        "membership-fence analysis the other three carry."
    )


def test_the_balance_decoupled_ledger_writer_has_exactly_one_caller():
    """`_create_ledger_backfill_entry` may be called from ONE place, for ever.

    Every other ledger write goes through `_create_ledger_entry`, which moves
    the `bottle_balances` row in the same breath. That coupling is the ONLY
    reason the stored figure and the ledger sum cannot silently drift apart,
    and this method is the first and only door in it.

    A second decoupled writer ON THE MERGE PATH would be caught by
    `test_a_reviewed_merge_leaves_the_place_balance_equal_to_its_ledger_sum`. A
    decoupled writer on any OTHER path — delivery, standalone collection, the
    §7.1 split, the §7.3 dissolve — would be caught by NOTHING: the result is a
    silent, permanent stored-vs-ledger divergence that no test notices and that
    the admin panel's Reconcile button then "fixes" by overwriting the balance.

    The leading-underscore convention is not a fence here, demonstrably:
    `business_app/services/order_edit_service.py` already reaches across a
    module boundary into `self.bottle_service._create_ledger_entry(...)`, with
    a comment acknowledging it. A future author following that precedent is not
    hypothetical, which is why this is pinned statically rather than trusted.

    Docstring and comment mentions are unaffected — only a real CALL has the
    trailing `(`.
    """
    from business_app.services.bottle_tracking_service import (
        BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES,
    )

    root = Path(__file__).resolve().parents[2]
    needle = "_create_ledger_backfill_entry("
    call_sites = []
    for tree in _SOURCE_ROOTS:
        for path in sorted((root / tree).rglob("*.py")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if needle not in line or line.lstrip().startswith(("def ", "async def ")):
                    continue
                call_sites.append(f"{path.relative_to(root)}:{lineno}")

    # Compared by FILE, not by line, so ordinary edits above the call site do
    # not make this pin cry wolf — but the count is pinned at exactly one.
    assert [site.rsplit(":", 1)[0] for site in call_sites] == [
        "business_app/services/customer_link_service.py"
    ], (
        "`_create_ledger_backfill_entry` is the ONLY balance-decoupled ledger writer and must "
        "keep exactly ONE call site — `CustomerLinkService._apply_merge_review`. Found "
        f"{len(call_sites)}: {call_sites}. A decoupled write anywhere else silently divorces "
        "the stored balance from the ledger sum, with no test to catch it, and the admin "
        "panel's Reconcile button then 'fixes' the divergence by overwriting the balance. "
        "A real bottle movement belongs in `_create_ledger_entry`, which moves the balance "
        "in the same breath."
    )

    # ...and that single call must use a key inside the enforced namespace, so
    # the constant below is load-bearing rather than decorative. A decoupled
    # entry outside it would be counted as COUPLED by the conservation pin in
    # tests/unit/test_place_merge_review.py and pass a check it violates.
    from business_app.services.customer_link_service import CustomerLinkService

    src = inspect.getsource(CustomerLinkService._apply_merge_review)
    call = src[src.index(needle):]
    key_line = next(line for line in call.splitlines() if "idempotency_key=" in line)
    assert any(f'"{prefix}' in key_line for prefix in BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES), (
        "The backfill's idempotency key must sit in BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES "
        f"({list(BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES)}); got: {key_line.strip()}"
    )


def test_absorb_address_into_group_locks_before_deleting_own_row():
    """Join path (Plan C Task 3): the address's balance is read under FOR UPDATE,
    not a bare SUM, before its row is deleted — the lost-update fix.

    Reading the figure off an unlocked row and deleting it afterwards is a
    lost update: a delivery or return committing at this address in between
    would be deleted away while the stale figure was credited onto the group,
    destroying bottles. `with_for_update()` must precede the delete so the
    figure and the row deleted are the same version.
    """
    from business_app.services.bottle_tracking_service import BottleTrackingService

    src = inspect.getsource(BottleTrackingService.absorb_address_into_group)
    lock_pos = src.index(".with_for_update()")
    delete_pos = src.index("db.session.delete(own_row)")
    assert lock_pos < delete_pos, (
        "The address's bottle_balances row must be locked FOR UPDATE before "
        "it is deleted, so the absorbed balance is read off the same locked "
        "row version that gets removed. Reading it via an unlocked SUM (or "
        "locking after the delete) reintroduces the lost-update bug."
    )


# --------------------------------------------------------------------------- #
# The design's remaining mandatory pin: "a grep-pin over every writer of
# `bottle_balances.balance`" (DESIGN-locking.md §6, the mandatory block).
# --------------------------------------------------------------------------- #
#
# Every other pin in this module guards a KNOWN path. This one guards the paths
# that do not exist yet — which is the only class the four-rung ladder cannot
# defend itself against. The ladder's whole guarantee is "the row is locked
# before it is written"; a NEW writer added six months from now inherits none of
# it, and nothing in the fast suite, the Postgres suite, or the nightly sweep
# would say a word, because a bypassing writer is perfectly self-consistent.
#
# So the writer SET is frozen here. Adding a writer is allowed; adding one
# SILENTLY is not.

_BALANCE_WRITERS_UNDER_THE_LADDER = {
    # rung 2/3 acquirer for every single-row path: delivery, return, standalone
    # collection, admin adjustment, fine, order-edit cascade. Its caller holds
    # the balance row FOR UPDATE via get_or_create_balance before it is entered.
    "business_app/services/bottle_tracking_service.py::_update_balance",
    # §7.3 dissolve. Enters holding rungs 0 and 1, takes rung 2 (group row) then
    # rung 3 (survivor's row) — it writes BOTH, hence two sites in one function.
    "business_app/services/bottle_tracking_service.py::release_group_history_to_address",
    # The repair path. Takes the row FOR UPDATE before it recomputes; defect 11
    # was precisely this method reading the ledger sum BEFORE that lock.
    "business_app/services/bottle_tracking_service.py::reconcile_balance",
    # §7.2 join. Holds rung 0 (the group) and rung 1 (the member addresses,
    # ascending id, one statement) before it touches the place's row.
    "business_app/services/customer_link_service.py::_absorb_joiners_into_group",
}


def _balance_writer_sites() -> set:
    """Every `<something>.balance = / += / -=` in a module that knows BottleBalance.

    AST, not a regex, so a write split across lines or buried in a branch cannot
    hide from it. Scoped to files that mention `BottleBalance` at all, which is
    what keeps an unrelated model's `.balance` from making this fire on
    something that has nothing to do with the ladder — the surest way to get a
    pin like this switched off.
    """
    import ast

    root = Path(__file__).resolve().parents[2]
    sites = set()
    for tree_name in _SOURCE_ROOTS:
        for path in sorted((root / tree_name).rglob("*.py")):
            # Migrations are excluded deliberately: they run offline, single
            # threaded, against a table nobody else is touching, and they are
            # the one place a raw balance rewrite is legitimate.
            if "migrations" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if "BottleBalance" not in text:
                continue
            rel = str(path.relative_to(root))
            for node in ast.walk(ast.parse(text)):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for sub in ast.walk(node):
                    targets = (
                        sub.targets
                        if isinstance(sub, ast.Assign)
                        else [sub.target] if isinstance(sub, ast.AugAssign)
                        else []
                    )
                    for target in targets:
                        if isinstance(target, ast.Attribute) and target.attr == "balance":
                            sites.add(f"{rel}::{node.name}")
    return sites


def test_every_writer_of_bottle_balances_balance_is_ENUMERATED():
    """The frozen writer set. A NEW writer fails this until it is triaged.

    This test failing is not a defect report — it is a question: does the new
    writer hold the balance row FOR UPDATE, and did it climb rungs 0/1 first if
    it is a lifecycle operation? If yes, add it to the set WITH the one-line
    reason the four existing entries carry. If no, it is defect 9/10 again, in a
    new place, and the fix is in the writer, not here.
    """
    found = _balance_writer_sites()
    unexpected = sorted(found - _BALANCE_WRITERS_UNDER_THE_LADDER)
    missing = sorted(_BALANCE_WRITERS_UNDER_THE_LADDER - found)

    assert not unexpected, (
        "NEW writer(s) of `bottle_balances.balance` that no rung of the ladder "
        f"is known to cover: {unexpected}. The ladder guarantees 'locked before "
        "written'; a writer added outside the enumerated set inherits none of "
        "that guarantee and produces a perfectly self-consistent wrong scope, "
        "which is exactly the shape of defects 9 and 10. Confirm the new site "
        "holds its balance row FOR UPDATE (and rungs 0/1 first, if it is a "
        "lifecycle op), then add it to _BALANCE_WRITERS_UNDER_THE_LADDER with a "
        "reason."
    )
    assert not missing, (
        f"enumerated balance writer(s) no longer exist: {missing}. If one was "
        "renamed or removed, update the set — but check FIRST that its locking "
        "moved with it rather than being dropped."
    )


def test_no_balance_write_BYPASSES_the_orm_row_lock_via_bulk_update_or_raw_sql():
    """The two shapes the AST scan above structurally cannot see.

    A bulk `query.update({BottleBalance.balance: ...})` and a raw
    `UPDATE bottle_balances SET balance = ...` both write the column without
    ever loading — and therefore without ever locking — a row. Either one walks
    straight past all four rungs while the enumerated-writer pin stays green,
    because neither is an attribute assignment.
    """
    root = Path(__file__).resolve().parents[2]
    offenders = []
    for tree_name in _SOURCE_ROOTS:
        for path in sorted((root / tree_name).rglob("*.py")):
            if "migrations" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            lowered = text.lower()
            if "BottleBalance.balance:" in text or "{BottleBalance.balance" in text:
                offenders.append(f"{path.relative_to(root)} (ORM bulk update)")
            if "update bottle_balances" in lowered:
                offenders.append(f"{path.relative_to(root)} (raw SQL)")
    assert offenders == [], (
        "a balance write that never loads a row, and so never locks one: "
        f"{offenders}. Route it through `get_or_create_balance` + "
        "`_update_balance` so it climbs the ladder like every other writer."
    )
