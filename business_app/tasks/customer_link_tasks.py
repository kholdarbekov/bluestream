"""Nightly invariant checks for the canonical-customer link layer (Phase 2).

Cross-customer place groups are a SANCTIONED state
(docs/superpowers/specs/2026-07-24-place-groups-and-cluster-wallet-design.md §3)
— the Phase-1 cross-customer-group violation check is gone. Checked now:

- negative_place_balances: a PLACE (`bottle_balances` row) with balance < 0.
  One balance row per place (spec 2026-07-27 §3) means this is a single flat
  query. The old `negative_group_unions` N+1 loop over the per-group union
  helper is gone, and so is the `stranded_negative_pairs` check it fed: a
  negative pair inside a non-negative union is no longer representable once
  there is only one balance row per place, so that state cannot occur any
  more.
- orphaned_place_balances: a `bottle_balances` group row whose member
  addresses have all left the group. Driven from `bottle_balances`, NOT from
  `addresses`: an orphan has no address pointing at it any more, so an
  address-driven sweep is structurally blind to exactly the rows it needs to
  find. It is the only check here assembled from TWO statements — and therefore
  two snapshots — so every candidate is re-verified before it is reported (see
  `_confirm_orphaned_place_balances`): a join committing MID-SWEEP used to be
  reported as an orphaned place, on the one alarm whose documented operator
  response is destructive.
- stranded_fine_scopes: a PENDING `bottle_fines` row frozen (at issue time) to
  a place group that has no members left. Nothing else here queries
  `bottle_fines` at all, so this state is invisible until somebody SETTLES the
  fine — at which point `mark_fine_paid` writes through the frozen scope and
  MINTS a balance row for a memberless group, which the orphan check above then
  reports, one destructive write too late. See `_stranded_fine_scopes` for what
  it deliberately does not flag (the §7.1 sanctioned departed-member freeze).
- stranded_address_balances: the inverse — an address-KEYED row whose address
  has since joined a place group, so every place-scoped read resolves past it
  and its bottles are invisible without being deleted (spec §7.2). Its only
  known producer is FIXED: `add_addresses_to_group` / `create_place_group` now
  re-scope an existing balance onto the group they join, via
  `BottleTrackingService.absorb_address_into_group`. The check STAYS as a
  backstop — it now catches a direct DB edit, a restore from a pre-re-scoping
  dump, or a future write path that sets `address_group_id` without going
  through `absorb_address_into_group`. Neither the negative nor the orphan
  check can see such a row.
- invalid_scope_balances: a `bottle_balances` row violating the one-scope-key
  rule (spec §13, `BOTTLE_SCOPE_INVALID`) — both keys set, or neither. Such a
  row is unreachable through `BottleScope` and would be counted by no place,
  or by two. `ck_bottle_balance_scope` blocks it at write time, so this finds
  what the constraint could not: rows written before it existed, or on a
  database where a rebuild dropped it. Driven from `bottle_balances`, like the
  two checks above it, because an address- or group-driven sweep cannot reach a
  row that belongs to neither.
- orphaned_canonical_pointers: user.canonical_customer_id -> missing canonical.
- grocery_or_entity_members: a non-INDIVIDUAL user linked into a cluster or
  owning a grouped address (remediation per spec §5.8: the money-path backstop
  already blocks scoped collections; admin unlinks/ungroups, then normal
  operation resumes).
- events_missing_scope_snapshot: non-voided cluster/place events with a NULL
  scope_snapshot (corrections could not replay their frozen scope).
- allocation_stamp_mismatches: dual audit stamps inconsistent with their
  event/payment rows — POST-MIGRATION allocations only (pre-migration stamps
  are NULL by design and skipped).
- event_conservation_violations: SUM(live allocations) + unapplied != amount.
- stamp_incoherent_ledger_entries: ORACLE 2 of the locking design
  (`.superpowers/sdd/2026-07-29-place-ledger-e2e/DESIGN-locking.md` §6). A
  `bottle_ledger` row stamped to NO group whose OWN address is currently in a
  place group. The two balance buckets above are structurally blind to it: the
  BALANCE side can be perfectly reachable while a ledger row records the same
  physical movement against a scope the place's history never shows. It is the
  ledger half of "a scope resolved before the lock and never re-validated" —
  a delivery that resolved to the address's own scope, blocked, and woke up
  after the address had been absorbed into a place.

  It deliberately does NOT flag the inverse — a row stamped with a group the
  address has since LEFT. That is the SANCTIONED §7.1 outcome: a split leaves
  the departing address's history with the place it was made at, and only the
  agreed `bottles_leaving` quantity travels. Flagging it would make the check
  fire on correct behaviour and be switched off within a week.
- duplicate_rescoped_ledger_entries: ORACLE 3 of the same design section —
  membership/history AGREEMENT, the one thing the reachability buckets and the
  stamp check both miss, because in this failure BOTH places are internally
  consistent. A ledger entry may be in the CUSTODY of at most one place at a
  time. `_absorb_joiners_into_group` claims entries (recorded on the audit
  event as `rescoped_ledger_entry_ids`) and `release_group_history_to_address`
  hands them back (`dissolved_rescoped_ledger_entry_ids`), so replaying the
  `customer_link_events` in id order reconstructs custody exactly. An absorb
  that claims an entry ANOTHER live episode already holds is a flat
  contradiction: two joins absorbed one address.

  DEVIATION FROM THE DESIGN'S LITERAL WORDING, stated so it is not mistaken
  for an oversight. §6.3 spells this as "the union of `rescoped_ledger_entry_ids`
  across all events must contain no duplicates". That formulation is UNSOUND
  here: a dissolve releases entries back to an address's own scope, and a
  later re-group legitimately claims the very same ids a second time. Every
  dissolve-then-regroup — an ordinary admin sequence, and one the randomised
  soak generates on its own — would be reported. The custody replay below
  catches exactly the contradiction §6.3 is aiming at and reports nothing for
  the legal sequence; both cases are pinned by tests.
- group_check_errors: reserved for isolated per-group check failures. The
  negative/orphan place checks above are flat queries with nothing per-group
  left to isolate, so this stays empty until a future per-group check needs
  it again.
"""

import logging
from decimal import Decimal

from celery import shared_task
from sqlalchemy import Text, cast, func, or_

from business_app import db
from business_app.models.bottle import BottleBalance, BottleFine, BottleLedger
from business_app.models.customer_link import CanonicalCustomer, CustomerLinkEvent
from business_app.models.payment import CashCollectionAllocation, CashCollectionEvent, Payment
from business_app.models.user import User, UserAddress
from shared.enums import BottleFineStatus, UserType

logger = logging.getLogger(__name__)


def _confirm_orphaned_place_balances(candidates: list) -> list:
    """Re-verify orphan candidates against the world at the END of the sweep.

    ONE statement, scoped to the candidate ids, asking both halves again: does
    the group still carry a `bottle_balances` row, and does it still have no
    member addresses? A candidate produced purely by read skew — a place joined
    between the membership read and the balance read — answers "it has members
    now" and is dropped.

    This is a CONFIRMATION, not a snapshot: it cannot make the two original
    reads agree, it can only refuse to report a candidate the database no
    longer agrees with. That is the whole requirement, because the two error
    directions are not symmetric — a missed violation is reported by the next
    nightly run, a false one invites an irreversible operator write tonight.
    """
    if not candidates:
        return []
    still_populated = {
        g
        for (g,) in db.session.query(UserAddress.address_group_id)
        .filter(UserAddress.address_group_id.in_(candidates))
        .distinct()
        .all()
    }
    still_have_a_row = {
        g
        for (g,) in db.session.query(BottleBalance.address_group_id)
        .filter(BottleBalance.address_group_id.in_(candidates))
        .all()
    }
    return sorted(g for g in candidates if g in still_have_a_row and g not in still_populated)


def _stranded_fine_scopes() -> list:
    """PENDING fines frozen to a place that no longer exists as a place.

    `bottle_fines.address_group_id` is the scope AT ISSUE, frozen so that a
    later ungrouping cannot split the FINE_ISSUED / FINE_PAID pair across two
    ledgers (`BottleTrackingService._fine_scope`). Freezing is right; what has
    never been checked is whether the frozen scope is still REACHABLE. A fine
    stamped to a group that has since DISSOLVED settles into that memberless
    group: `mark_fine_paid` writes through `_fine_scope`, `get_or_create_balance`
    MINTS a `bottle_balances` row for a group no address resolves to, and the
    sweep finally notices it — as an `orphaned_place_balances` violation, one
    destructive write too late. Detecting it BEFORE settlement is the entire
    value: a PENDING fine can still be re-scoped by hand; the orphaned balance
    row it becomes cannot be un-created.

    DEVIATION FROM THE TRIAGED WORDING, stated so it is not mistaken for an
    oversight — the same kind of deviation, and for the same reason, as the
    custody replay's departure from design §6.3's literal phrasing. The fix
    shape asked for "a PENDING fine whose `address_group_id` is not among the
    live group ids, OR whose `address_id` is no longer a member of that group".
    The second arm alone reports a SANCTIONED state: §7.1/§7.3 deliberately do
    NOT re-stamp a departed member's frozen references, so an ordinary removal
    from a place that still has members leaves exactly that shape behind, on
    purpose, with the fine's scope still fully reachable and its ledger pair
    still intact. A check that fires on correct behaviour is muted within a
    week, and the residue would be back with a green test in front of it. What
    is flagged is the fine whose frozen group has NO members left at all —
    which covers the second arm's real cases (the departing member WAS the last
    one out) without covering the legal ones.
    """
    stamped = (
        db.session.query(BottleFine.id, BottleFine.address_group_id)
        .filter(
            BottleFine.status == BottleFineStatus.PENDING,
            BottleFine.address_group_id.isnot(None),
        )
        .all()
    )
    if not stamped:
        return []
    group_ids = {g for _, g in stamped}
    live = {
        g
        for (g,) in db.session.query(UserAddress.address_group_id)
        .filter(UserAddress.address_group_id.in_(group_ids))
        .distinct()
        .all()
    }
    return sorted(fine_id for fine_id, group_id in stamped if group_id not in live)


def reconcile_customer_link_invariants() -> dict:
    """Return (and log) any violations of the link-layer invariants."""

    # -- group ownership snapshot: which users own an address inside ANY place
    #    group. Feeds grocery_or_entity_members below. ONE query, no N+1. --
    group_owner_ids = {
        r[0] for r in db.session.query(UserAddress.user_id).filter(UserAddress.address_group_id.isnot(None)).all()
    }

    # -- negative places: ONE query, no N+1. A negative pair inside a positive
    #    union is no longer representable (one row per place), so the old
    #    stranded_negative_pairs check has nothing left to find and is gone. --
    negative_place_balances = sorted(
        b_id for (b_id,) in db.session.query(BottleBalance.id).filter(BottleBalance.balance < 0).all()
    )

    # -- orphaned place balances: a group row whose member addresses have all
    #    left. Driven from bottle_balances, NOT from addresses: an orphan has no
    #    address pointing at it, so an address-driven sweep is structurally
    #    blind to exactly the rows it needs to find.
    #
    #    TWO STATEMENTS, AND THEREFORE TWO SNAPSHOTS. This is the one check here
    #    assembled from a pair of independent reads, and under READ COMMITTED
    #    each read sees the world as it was when IT ran. A `create_place_group`
    #    that commits between them is invisible to the membership read and
    #    visible to the balance read, so a brand new, fully populated place is
    #    reported as ORPHANED — on the ONLY automated alarm this layer has, whose
    #    documented operator response is the DESTRUCTIVE
    #    `POST /admin/bottles/reconcile/<address_id>`. A nightly false positive
    #    both trains the operator to ignore the alarm and points them at the
    #    button that destroys a drifted place's real balance.
    #
    #    So every candidate is RE-VERIFIED against the world as it is at the END
    #    of the sweep, in one statement scoped to the candidates themselves.
    #    Read skew is asymmetric here and deliberately so: a violation that
    #    appears mid-sweep is simply reported by tomorrow's run, while a false
    #    alarm invites an irreversible write tonight. --
    live_group_ids = {
        g
        for (g,) in db.session.query(UserAddress.address_group_id)
        .filter(UserAddress.address_group_id.isnot(None))
        .distinct()
        .all()
    }
    orphan_candidates = sorted(
        {
            g_id
            for (g_id,) in db.session.query(BottleBalance.address_group_id)
            .filter(BottleBalance.address_group_id.isnot(None))
            .all()
            if g_id not in live_group_ids
        }
    )
    orphaned_place_balances = _confirm_orphaned_place_balances(orphan_candidates)

    # -- stranded address balances: an address-KEYED row whose address has since
    #    joined a place group (spec section 7.2). `resolve_scope` keys on the
    #    ADDRESS, so every place-scoped read now resolves that address to the
    #    group and the row becomes unreachable — the bottles are invisible
    #    everywhere without being deleted anywhere.
    #
    #    The one write path that used to mint these is fixed: both
    #    create_place_group and add_addresses_to_group now re-scope an existing
    #    balance onto the group they join
    #    (`BottleTrackingService.absorb_address_into_group`, spec §7.2). This
    #    check is kept as a BACKSTOP, not as a known-live defect count — what
    #    it can still surface is a direct DB edit, a restore from a dump taken
    #    before re-scoping shipped, or a future write path that sets
    #    address_group_id without routing through absorb_address_into_group.
    #    It is the exact inverse of orphaned_place_balances above — that one
    #    finds group rows with no addresses, this one finds address rows whose
    #    address is no longer its own place — and neither the negative nor the
    #    orphan check can see it. ONE join, no N+1. --
    stranded_address_balances = sorted(
        b_id
        for (b_id,) in db.session.query(BottleBalance.id)
        .join(UserAddress, UserAddress.id == BottleBalance.address_id)
        .filter(
            BottleBalance.address_id.isnot(None),
            UserAddress.address_group_id.isnot(None),
        )
        .all()
    )
    # -- invalid scope rows: exactly one of (address_group_id, address_id) must
    #    be set (spec §13). `ck_bottle_balance_scope` enforces it at write time
    #    and `BottleTrackingService.assert_scope_row_valid` mirrors it in
    #    process, so this sweep is for the rows NEITHER can reach: anything
    #    written before the constraint existed, or on a database whose table was
    #    rebuilt without it. Both violating shapes are unreachable through
    #    `BottleScope` — a two-key row would be counted by two places, a no-key
    #    row by none. ONE query, no N+1. --
    invalid_scope_balances = sorted(
        b_id
        for (b_id,) in db.session.query(BottleBalance.id)
        .filter(
            or_(
                (BottleBalance.address_group_id.is_(None)) & (BottleBalance.address_id.is_(None)),
                (BottleBalance.address_group_id.isnot(None)) & (BottleBalance.address_id.isnot(None)),
            )
        )
        .all()
    )
    # -- ORACLE 2, stamp coherence: a ledger row stamped to NO group whose own
    #    address IS in a place group. The place's history does not show a
    #    movement its own member recorded, so `get_place_ledger` and the
    #    customer's "my bottles" screen disagree about the same physical
    #    handover. ONE join, no N+1.
    #
    #    The inverse (stamped to a group the address has LEFT) is NOT flagged:
    #    that is the sanctioned §7.1 split outcome, and a check that fires on
    #    correct behaviour gets muted. --
    stamp_incoherent_ledger_entries = sorted(
        l_id
        for (l_id,) in db.session.query(BottleLedger.id)
        .join(UserAddress, UserAddress.id == BottleLedger.address_id)
        .filter(
            BottleLedger.address_group_id.is_(None),
            UserAddress.address_group_id.isnot(None),
        )
        .all()
    )

    # -- ORACLE 3, membership <-> history agreement, by CUSTODY REPLAY.
    #    `rescoped_ledger_entry_ids` = an absorb CLAIMING entries for a place;
    #    `dissolved_rescoped_ledger_entry_ids` = a dissolve HANDING THEM BACK.
    #    Replaying both in event-id order reconstructs which episode holds each
    #    entry; a claim on an entry already held is the two-joins-absorbed-one-
    #    address contradiction. A plain duplicate scan over the claim key alone
    #    would report every legal dissolve-then-regroup (see module docstring).
    #    ONE query, ordered; the replay is O(claims). --
    live_entry_custody: dict = {}
    duplicate_rescoped_ledger_entries: list = []
    for _event_id, metadata in (
        db.session.query(CustomerLinkEvent.id, CustomerLinkEvent.event_metadata)
        .order_by(CustomerLinkEvent.id.asc())
        .all()
    ):
        if not isinstance(metadata, dict):
            continue
        released = metadata.get("dissolved_rescoped_ledger_entry_ids")
        if isinstance(released, list):
            for entry_id in released:
                live_entry_custody.pop(entry_id, None)
        claimed = metadata.get("rescoped_ledger_entry_ids")
        if isinstance(claimed, list):
            for entry_id in claimed:
                if entry_id in live_entry_custody:
                    duplicate_rescoped_ledger_entries.append(entry_id)
                live_entry_custody[entry_id] = _event_id
    duplicate_rescoped_ledger_entries = sorted(set(duplicate_rescoped_ledger_entries))

    group_check_errors = []

    # -- grocery/entity fence: clusters AND place groups (spec §5.8) --
    linked_non_individual = {
        r[0]
        for r in db.session.query(User.id)
        .filter(User.canonical_customer_id.isnot(None), User.user_type != UserType.INDIVIDUAL)
        .all()
    }
    grouped_non_individual = set()
    if group_owner_ids:
        grouped_non_individual = {
            r[0]
            for r in db.session.query(User.id)
            .filter(User.id.in_(group_owner_ids), User.user_type != UserType.INDIVIDUAL)
            .all()
        }
    grocery_or_entity_members = sorted(linked_non_individual | grouped_non_individual)

    # -- orphaned canonical pointers --
    valid_canonical_ids = {r[0] for r in db.session.query(CanonicalCustomer.id).all()}
    orphaned = [
        r[0]
        for r in db.session.query(User.id, User.canonical_customer_id)
        .filter(User.canonical_customer_id.isnot(None))
        .all()
        if r[1] not in valid_canonical_ids
    ]

    # -- scoped events must carry their frozen snapshot.
    #    "No snapshot" has TWO on-disk shapes: SQL NULL (the column was omitted
    #    on insert) and JSON null (`scope_snapshot=None` passed explicitly —
    #    SQLAlchemy's JSON type serializes that to the literal 'null', so
    #    `IS NULL` alone silently misses it). Comparing the TEXT form catches
    #    both and is portable (Postgres json->text is an I/O cast; the SQLite
    #    test backend stores JSON as text already). --
    events_missing_scope_snapshot = [
        r[0]
        for r in db.session.query(CashCollectionEvent.id)
        .filter(
            CashCollectionEvent.scope_type.in_(("cluster", "place")),
            CashCollectionEvent.voided_at.is_(None),
            or_(
                CashCollectionEvent.scope_snapshot.is_(None),
                cast(CashCollectionEvent.scope_snapshot, Text) == "null",
            ),
        )
        .all()
    ]

    # -- dual-stamp consistency (post-migration allocations only: any stamp set).
    #    Joins are pinned on FK columns; never join(User) (multi-FK gotcha). --
    allocation_stamp_mismatches = []
    stamp_rows = (
        db.session.query(
            CashCollectionAllocation.id,
            CashCollectionAllocation.source_customer_id,
            CashCollectionAllocation.beneficiary_user_id,
            CashCollectionEvent.customer_id,
            Payment.user_id,
        )
        .join(
            CashCollectionEvent,
            CashCollectionAllocation.cash_collection_event_id == CashCollectionEvent.id,
        )
        .join(Payment, CashCollectionAllocation.payment_id == Payment.id)
        .filter(
            or_(
                CashCollectionAllocation.source_customer_id.isnot(None),
                CashCollectionAllocation.beneficiary_user_id.isnot(None),
            )
        )
        .all()
    )
    for alloc_id, source_id, beneficiary_id, event_customer_id, payment_user_id in stamp_rows:
        if source_id != event_customer_id or beneficiary_id != payment_user_id:
            allocation_stamp_mismatches.append(alloc_id)

    # -- per-event conservation: SUM(live allocations) + unapplied == amount --
    live_sums = dict(
        db.session.query(
            CashCollectionAllocation.cash_collection_event_id,
            func.coalesce(func.sum(CashCollectionAllocation.allocated_amount), 0),
        )
        .filter(CashCollectionAllocation.reversed_at.is_(None))
        .group_by(CashCollectionAllocation.cash_collection_event_id)
        .all()
    )
    event_conservation_violations = []
    for event_id, amount, unapplied in (
        db.session.query(CashCollectionEvent.id, CashCollectionEvent.amount, CashCollectionEvent.unapplied_amount)
        .filter(CashCollectionEvent.voided_at.is_(None))
        .all()
    ):
        allocated = Decimal(str(live_sums.get(event_id, 0) or 0))
        if allocated + Decimal(str(unapplied or 0)) != Decimal(str(amount or 0)):
            event_conservation_violations.append(event_id)

    report = {
        "negative_place_balances": negative_place_balances,
        "orphaned_canonical_pointers": orphaned,
        "grocery_or_entity_members": grocery_or_entity_members,
        "events_missing_scope_snapshot": events_missing_scope_snapshot,
        "allocation_stamp_mismatches": allocation_stamp_mismatches,
        "event_conservation_violations": event_conservation_violations,
        "orphaned_place_balances": orphaned_place_balances,
        "stranded_address_balances": stranded_address_balances,
        "stranded_fine_scopes": _stranded_fine_scopes(),
        "invalid_scope_balances": invalid_scope_balances,
        "stamp_incoherent_ledger_entries": stamp_incoherent_ledger_entries,
        "duplicate_rescoped_ledger_entries": duplicate_rescoped_ledger_entries,
        "group_check_errors": group_check_errors,
    }
    if any(report.values()):
        logger.error("Customer-link invariant violations: %s", report)
    return report


@shared_task(name="business_app.tasks.customer_link_tasks.reconcile_customer_link_invariants_task")
def reconcile_customer_link_invariants_task() -> dict:
    return reconcile_customer_link_invariants()
