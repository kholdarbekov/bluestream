"""Returnable bottle tracking: balances, ledger, fines, and driver accountability."""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple, TYPE_CHECKING

from flask import current_app
from sqlalchemy import case, delete, event as sa_event, false as sa_false, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session as SASession, joinedload, selectinload

from business_app import db
from business_app.models.bottle import (
    BottleBalance,
    BottleFine,
    BottleLedger,
    DriverBottleSession,
    DriverBottleSessionOrder,
    DriverBottleSessionUnit,
    DriverBottleTransfer,
    DriverBottleTransferUnit,
    DriverSessionMembership,
)
from business_app.models.delivery import Delivery as DeliveryModel, DeliveryPerson
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product
from business_app.models.user import User, UserAddress
from business_app.services.bottle_scope import BottleScope  # noqa: F401 — re-exported for callers
from shared.enums import (
    BottleFineStatus,
    BottleLedgerEventType,
    DeliveryStatus,
    DriverBottleSessionStatus,
    DriverBottleTransferStatus,
    DriverSessionMembershipStatus,
    OrderStatus,
    UserStatus,
)
from shared.staff_constants import BOTTLE_RETURN_COLUMN_CEILING, MAX_BOTTLES_PER_SESSION
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from business_app.utils.exceptions import (
    ConfigurationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from business_app.utils.state_validators import DELIVERY_BOTTLES_PROMISED_STATES, DRIVER_HELD_ORDER_STATUSES
from business_app.utils.transactions import transactional

if TYPE_CHECKING:
    from business_app.models.delivery import Delivery

logger = logging.getLogger(__name__)


class TripUnits(NamedTuple):
    """What something puts on a trip: returnable 18.9 L bottles and counted one-way units ({product_id: count})."""

    bottles: Decimal
    units: Dict[int, int]

    @property
    def any(self) -> bool:
        return self.bottles != 0 or any(v != 0 for v in self.units.values())


class TripFree(NamedTuple):
    """What a crew can still promise from one trip: free 18.9 L bottles and free units per one-way product."""

    bottles: int
    units: Dict[int, int]


class Shortfall(NamedTuple):
    """One line a crew cannot cover. `product_id None` is the 18.9 L full bottles."""

    product_id: Optional[int]
    product_name: Optional[str]
    available: int
    required: int
    shortfall: int


class LoadScreen(NamedTuple):
    """What the driver's Load screen needs, read once (`BottleTrackingService.load_screen`).

    `products` are the products the trip counts, by id. When he may not open a trip, `refusal_code` is the open's
    refusal, `refusal_details` its details and `refusal_session` the trip it names (its unit lines loaded);
    `open_session` is that trip only when it is his own. A later build adds its fields after these, with defaults.
    """

    products: List[Product]
    refusal_code: Optional[str]
    refusal_details: Dict
    refusal_session: Optional[DriverBottleSession]
    open_session: Optional[DriverBottleSession]


# How a trip's row is locked (`with_for_update` flags): FOR NO KEY UPDATE. Send, close, force-close, the count and the
# resolve of a transfer, and the Delivered tally's UPDATE still queue behind one another. An order binding, a co-driver
# membership or a transfer referencing the trip only takes FOR KEY SHARE on it (their foreign-key check), and that does
# not wait: a plain FOR UPDATE made the assignment and the progress guard, which bind after locking DeliveryPerson rows,
# wait on a close that then wrote a DeliveryPerson row itself (a deadlock).
SESSION_ROW_LOCK = {"key_share": True}

# What the admin resolve route asks of its caller (`validate_admin_action` in business_app/api/admin_bottles.py).
TRANSFER_DISPUTE_RESOLVE_PERMISSIONS: Tuple[str, ...] = ("manage_orders",)


@dataclass(frozen=True)
class TransferCountOutcome:
    """What `BottleTrackingService.confirm_bottle_transfer` answers: the transfer as counted, and whether the admins'
    Slack alerts channel took the dispute alert (always False for a confirmed count). The confirm route publishes both,
    so the receiver's reply says an admin was told only when one was (spec 2026-11-09 §4.4)."""

    transfer: DriverBottleTransfer
    admins_alerted: bool


class TransferRejectOutcome(NamedTuple):
    """What `BottleTrackingService.reject_bottle_transfer` answers: the transfer as rejected, whether the sender's trip
    had closed before the reject (so its discrepancies were recomputed), and whether the admins' Slack alerts channel
    took the reject alert. The reject route publishes the first two."""

    transfer: DriverBottleTransfer
    sender_trip_was_closed: bool
    admins_alerted: bool


@dataclass(frozen=True)
class FrozenScopeTarget:
    """Where a write against a FROZEN scope lands, and WHAT it had to do to get there.

    Produced only by `BottleTrackingService.resolve_frozen_scope_for_write`,
    which documents the four answers. A plain `BottleScope` cannot carry this:
    the caller has to know both which ADDRESS to attribute the ledger row to (an
    address scope's ledger predicate is `address_id = X AND address_group_id IS
    NULL`, so scope and attribution are the SAME fact there) and whether the
    frozen place is a dead end it must refuse by its own name.
    """

    # The scope to pass as `scope=` — never None. When `unreachable` it is the
    # FROZEN scope handed straight back, so a caller that ignores the flag
    # behaves exactly as it did before the forwarding pointer existed.
    scope: "BottleScope"
    # The address the ledger row must be ATTRIBUTED to. Equal to
    # `anchor_address_id` unless the write was forwarded past a dissolve.
    address_id: int
    # The address the frozen reference itself named (the fine's address, the
    # delivery row's address).
    anchor_address_id: int
    from_group_id: Optional[int]
    # A dissolved place's `dissolved_onto_address_id` was followed.
    forwarded: bool
    # The frozen place is DISSOLVED and there is nowhere honest to book. The
    # caller refuses; nothing here invents a destination.
    unreachable: bool

    def audit(self) -> Dict[str, Any]:
        """Provenance for the ledger row's metadata — empty unless forwarded.

        The forwarded entry is attributed to the SURVIVOR because it has to be
        (see `address_id`), so without this the door the episode actually went
        through would be lost from the record entirely.
        """
        if not self.forwarded:
            return {}
        return {
            "forwarded_from_place_group_id": self.from_group_id,
            "forwarded_to_address_id": self.address_id,
            "attributed_through_address_id": self.anchor_address_id,
        }


# =====================================================================
# THE SCOPE-LOCK REGISTRY (spec §5.2, revised — the four-rung ladder)
# =====================================================================
#
# NOT the safety mechanism. Postgres row locks are; see `resolve_scope_for_write`
# and `CustomerLinkService._load_addresses`. This is the COVERAGE PROBE:
# `with_for_update()` compiles to NOTHING on SQLite, so the fast suite is
# structurally blind to every lock in the ladder — but this dict is pure Python
# and fires there. Running the existing suite with it armed turns every test
# into a detector for a bottle write path nobody enumerated.
#
# It also converts a real hazard into a red test rather than an invisible hole:
# `business_app/utils/transactions.py`'s `atomic_transaction` is NOT
# nesting-aware, so an inner `@transactional` commits its caller's work and
# silently DROPS every row lock the caller was holding. Clearing the registry on
# commit/rollback means the next explicit-scope write after such a commit is
# refused by name instead of writing under no lock at all.
_SCOPE_LOCK_REGISTRY_KEY = "bottle_scope_locks"


def _scope_lock_registry() -> dict:
    """The per-DB-transaction set of `addresses` / `address_groups` rows held."""
    info = db.session.info
    registry = info.get(_SCOPE_LOCK_REGISTRY_KEY)
    if registry is None:
        registry = {"addresses": set(), "groups": set()}
        info[_SCOPE_LOCK_REGISTRY_KEY] = registry
    return registry


def _clear_scope_lock_registry(session, *_args, **_kwargs) -> None:
    """Locks die with the DB transaction, so the registry must die with it too."""
    session.info.pop(_SCOPE_LOCK_REGISTRY_KEY, None)


for _lifecycle_event in ("after_commit", "after_rollback", "after_soft_rollback"):
    sa_event.listen(SASession, _lifecycle_event, _clear_scope_lock_registry)

# Merge-review entries that belong to the PLACE rather than to the member whose
# `(user_id, address_id)` they carry (spec §7.4). `bottle_ledger` requires both
# columns NOT NULL — decision 4's named ledger rests on them — so a place-level
# correction has to borrow an attribution it does not mean. Every derived
# per-member quantity must therefore skip these, and every member-facing view
# must not present them as that member's own.
#
# `merge_exclude` is NOT here: a reversal is attributed to the very entry it
# neutralises, so its attribution is correct and load-bearing.
PLACE_LEVEL_LEDGER_KEY_PREFIXES = ("merge_correction:", "merge_backfill:")
PLACE_LEVEL_LEDGER_SOURCES = frozenset({"merge_correction", "merge_backfill"})

# The ONLY idempotency-key namespaces a balance-DECOUPLED ledger entry may use.
# ENFORCED, not documentation: `_create_ledger_backfill_entry` rejects any key
# outside this tuple, and rejects a missing key outright.
#
# It is load-bearing for conservation, not cosmetic. The invariant
# `Σ balances after − before == Σ COUPLED quantities` is only checkable because
# every decoupled entry is identifiable BY ITS KEY — an unkeyed decoupled write
# would be silently counted as coupled by
# `tests/unit/test_place_merge_review.py`'s `_coupled_quantities`, and the pin
# would pass while the two figures diverged. Adding a namespace here is
# therefore a deliberate act that must come with its own conservation split.
BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES = ("merge_backfill:",)

# The namespaces reserved for CLIENT-SUPPLIED idempotency tokens, declared next
# to the two server-side namespaces above so all three are visible together.
#
# ENFORCED, not documentation. `uq_bottle_ledger_idempotency` is UNIQUE ON THE
# KEY ALONE (models/bottle.py:102) and `_create_ledger_entry`'s lookup carries
# no scope predicate, so an unvalidated client token is a global namespace every
# customer and every event type shares. A driver who could post `delivery:123`
# would make the REAL delivery of order 123 silently no-op and echo his own row
# back — the exact failure `set_initial_balance` documents in its docstring, and
# the one this repo already logged for the sibling tryout ledger
# (docs/sqlite_fk_enforcement_followup.md:126-129).
#
# Three fences, ALL required:
#   1. the token may not contain ':' and may not carry a trailing newline (the
#      pattern below, applied with `fullmatch` — `$` alone matches before a
#      trailing '\n', which would smuggle a control character into the stored
#      key and into `_create_ledger_entry_with_status`'s "Duplicate ledger entry
#      skipped: %s" log line),
#   2. the STORED key is composed server-side as
#      `{namespace}:client:{actor_user_id}:{token}`, so the client never controls
#      the whole string and two drivers' tokens cannot collide, and
#   3. a dedup HIT is compared against the incoming request before it is honoured
#      (`_assert_replay_matches_collection` / `_assert_replay_matches_fine`) —
#      without (3) one driver can replay a single token to silently suppress
#      collections and fines at OTHER customers, at HTTP 200, with no ledger row
#      and no session-tally bump.
# Neither namespace collides with PLACE_LEVEL_ / BALANCE_DECOUPLED_ prefixes, so
# the `notlike()` conservation split is unaffected — a client-keyed collection
# stays INCLUDED in its member's own-sum, which is correct.
CLIENT_IDEMPOTENCY_NAMESPACES = ("collect", "fine")
CLIENT_IDEMPOTENCY_TOKEN_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{8,64}\Z")


def format_bottle_quantity(value) -> str:
    """Render a returnable-bottle quantity as a normalized decimal string.

    Drops insignificant trailing zeros ("4.00" -> "4", "1.50" -> "1.5") without
    int() truncation, so schema-permitted fractional Numeric(12,2) quantities
    survive. ``None``/0 render as "0".
    """
    dec = Decimal(str(value if value is not None else 0))
    return format(dec.normalize(), "f")


class BottleTrackingService:
    """Manages returnable bottle balances, ledger, fines, and driver accountability."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _as_decimal(value: Any) -> Decimal:
        """Coerce a caller-supplied quantity/amount to Decimal, REFUSING NaN/±Inf.

        THE SSOT COERCION for every bottle quantity and fine amount, which is
        exactly why the finiteness refusal belongs here and not at each call
        site: Python's `json` parser accepts the bare `NaN` / `Infinity` /
        `-Infinity` literals, `str(float('nan'))` is `'nan'`, and
        `Decimal('nan')` is a perfectly constructible value — so an unguarded
        non-finite number travels the whole write path into the column. On
        Postgres `numeric` ACCEPTS 'NaN', so it is PERSISTED and unrepairable:
        `reconcile_balance` sets `balance = ledger_sum` and that sum is
        non-finite too.

        The per-call positivity guards cannot stand in for this, and — verified,
        not assumed — the two non-finite values defeat them DIFFERENTLY, because
        `decimal` is not IEEE-754:

          * `Decimal('NaN') <= 0` RAISES `decimal.InvalidOperation`. An ORDERING
            comparison against a decimal NaN is an error, not a False (only `==`
            is False). Nothing catches it, so the route answered 500 to a
            malformed request.
          * `Decimal('Infinity') <= 0` is a well-defined `False`, so +Infinity
            walked straight past every positivity guard and reached the column.
            (`Decimal('-Infinity') <= 0` is True, so the negative sign was
            already caught by those guards — the two signs are NOT symmetric.)

        `CustomerLinkService._validated_bottles_leaving` carries the same
        refusal for the §7.1 split; §13 defines no error code for it, so this
        400s on the message alone for the same reason.
        """
        result = Decimal(str(value or 0))
        if not result.is_finite():
            raise ValidationError("Bottle quantity must be a finite number")
        return result

    @staticmethod
    def compose_client_idempotency_key(namespace: str, actor_user_id: int, token: Optional[str]) -> Optional[str]:
        """Validate a CLIENT-SUPPLIED retry token and namespace it server-side.

        Returns None when no token was supplied — the un-keyed path every
        internal caller, every test and every legacy client still takes, and the
        reason `bottle_ledger.idempotency_key` stays nullable.

        `int(actor_user_id)` is a FENCE, not cosmetics. `get_jwt_identity()`
        (business_app/api/staff.py) hands back a string `sub`, so this value is
        client-adjacent by construction. (Note the narrow claim: `f"{'5'}"` and
        `f"{5}"` are the same string, so the coercion is NOT what makes a
        driver's own retry dedup.) What it does buy is that the actor half of the
        composed key is provably an integer: an identity that is not
        int-coercible raises here instead of being interpolated verbatim, which
        is the only way a ':' could otherwise reach the key from outside the
        token — and the token's own pattern already forbids one.

        `fullmatch`, not `match`: Python's `$` also matches just before a
        trailing newline, so `match` would accept "AAAAAAAA\\n" and store a
        control character in the key and in the dedup log line.

        Mirrors `_create_ledger_backfill_entry`'s prefix fence, the house
        precedent for server-side key-shape validation.
        """
        if not token:
            return None
        if namespace not in CLIENT_IDEMPOTENCY_NAMESPACES:
            # A programming error, never client input — every call site passes a
            # literal. ConfigurationError maps to 500 (utils/error_handlers.py);
            # a bare ValueError would map to 400 INVALID_VALUE and report our bug
            # to the driver as their validation failure.
            raise ConfigurationError(f"unknown client idempotency namespace: {namespace!r}")
        if not isinstance(token, str) or not CLIENT_IDEMPOTENCY_TOKEN_PATTERN.fullmatch(token):
            raise ValidationError(
                "idempotency_key must be 8-64 characters of A-Z, a-z, 0-9, '_' or '-'",
                error_code="BOTTLE_IDEMPOTENCY_KEY_INVALID",
            )
        return f"{namespace}:client:{int(actor_user_id)}:{token}"

    @staticmethod
    def _assert_replay_matches_collection(existing: BottleLedger, *, user_id, address_id, quantity: Decimal) -> None:
        """A dedup HIT must be the SAME collection, not merely the same token.

        `_create_ledger_entry_with_status`'s lookup has no user/address/quantity
        predicate, so without this a driver who reuses one token for every
        customer gets HTTP 200, no ledger row, no balance move and — since the
        tally moved inside the dedup fence — no session-tally bump either, so his
        trip still closes at `discrepancy == 0` while he keeps the bottles. Same
        class as the defect this repo logged for the tryout ledger
        (docs/sqlite_fk_enforcement_followup.md:126-129).

        POST-FETCH comparison, NEVER a predicate on the lookup: a scoped lookup
        would miss the row and turn the silent no-op into an IntegrityError 500
        against the single-column UNIQUE — see `set_initial_balance`'s docstring.

        `user_id`/`address_id` are int-coercible by the time we get here:
        `_assert_user_in_scope` has already matched them against a real
        `UserAddress` row.
        """
        if (
            int(existing.user_id) != int(user_id)
            or int(existing.address_id) != int(address_id)
            or existing.event_type != BottleLedgerEventType.STANDALONE_COLLECTION
            or BottleTrackingService._as_decimal(existing.quantity) != quantity
        ):
            raise ConflictError(
                "idempotency_key was already used for a different collection",
                error_code="BOTTLE_IDEMPOTENCY_KEY_REUSED",
            )

    @staticmethod
    def _assert_replay_matches_fine(
        existing: BottleFine, *, user_id, address_id, quantity: Decimal, fine_amount: Decimal
    ) -> None:
        """Same rule for the money-carrying half; see the sibling above.

        Deliberately NOT status-gated. If the four fields match, the replay IS
        the original intent, so returning the original fine is the correct
        idempotent answer even after it was PAID or WAIVED. A genuinely new fine
        for the same customer and amount arrives on a NEW flow with a NEW token
        and lands normally.
        """
        if (
            int(existing.user_id) != int(user_id)
            or int(existing.address_id) != int(address_id)
            or BottleTrackingService._as_decimal(existing.quantity) != quantity
            or BottleTrackingService._as_decimal(existing.fine_amount) != fine_amount
        ):
            raise ConflictError(
                "idempotency_key was already used for a different fine",
                error_code="BOTTLE_IDEMPOTENCY_KEY_REUSED",
            )

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def latest_timestamp(*values: Optional[datetime]) -> Optional[datetime]:
        """The most recent of several `last_delivery_at` / `last_return_at` values.

        THE SSOT for merging a place's provenance columns across a membership
        edit (`absorb_address_into_group` -> `_absorb_joiners_into_group` step 5,
        and `release_group_history_to_address`'s dissolve). "Most recent wins" is
        the only merge that keeps the column's meaning: a place is last served on
        the latest date ANY of its member addresses was served, and taking the
        first non-null instead would report a stale date whenever the joiners are
        absorbed in id order rather than in date order.

        NAIVE VALUES ARE TREATED AS UTC purely for the comparison, and the
        ORIGINAL value is returned unchanged. The column is
        `DateTime(timezone=True)`, which Postgres honours and SQLite does not —
        so within one transaction one operand can be an aware `datetime` still
        in the identity map while the other came back naive from the test
        backend, and a bare `max()` would raise `TypeError: can't compare
        offset-naive and offset-aware datetimes` in the middle of an admin merge.
        Normalising only the sort key keeps the stored value byte-identical to
        whatever the dialect produced.
        """
        present = [value for value in values if value is not None]
        if not present:
            return None
        return max(
            present,
            key=lambda value: value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc),
        )

    # ------------------------------------------------------------------
    # BOUNDING THE WAITER (M2) — `lock_timeout` + a renderable failure
    # ------------------------------------------------------------------
    #
    # The ladder makes a driver's DELIVERED submission take `FOR SHARE` on the
    # `addresses` row, which now BLOCKS against the place lifecycle and the
    # account-merge path. Neither of those was contended before the ladder
    # landed, and neither is instantaneous: `_absorb_joiners_into_group` ends in
    # `recompute_balance_after`, which rewrites a place's whole ledger timeline
    # row by row inside the lock window (measured: ~3.9s at 5000 entries on a
    # fast box; prod is a Raspberry Pi 5).
    #
    # So the driver gets a BOUND, and the bound belongs on the WAITER only. The
    # lifecycle is the holder, it is doing legitimate bounded work, and killing
    # it mid-merge would free nothing a retry could not.

    # 55P03. Raised by BOTH `lock_timeout` and `NOWAIT`, so the two are
    # indistinguishable here by design — either way the answer is "someone else
    # holds this, come back".
    _LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"

    @staticmethod
    def _is_lock_not_available(exc: BaseException) -> bool:
        """True for Postgres 55P03, however SQLAlchemy wrapped it."""
        return getattr(getattr(exc, "orig", None), "pgcode", None) == BottleTrackingService._LOCK_NOT_AVAILABLE_SQLSTATE

    @staticmethod
    def _apply_scope_lock_timeout() -> None:
        """`SET LOCAL lock_timeout` for the rest of THIS transaction.

        `SET LOCAL` — so it dies with the transaction and can never leak onto a
        pooled connection and silently bound an unrelated later request.

        Postgres only. On SQLite (the fast suite) `with_for_update()` compiles
        to nothing, there is no lock to time out, and the statement is a syntax
        error — so this is a no-op there rather than a spurious failure.

        Applied at rung 1 and therefore inherited by rungs 2 and 3, so every
        wait in the ladder is bounded, not just the first.

        BOUNDING and CONVERTING are separate decisions, and only rung 1 does
        both. A rung 2/3 timeout still raises the raw `DBAPIError`, because that
        contention is writer-against-writer on one place's balance row — it
        predates this change, it is fast, and `test_place_concurrency_pg_e2e.py`
        asserts its SQLSTATE directly. Rung 1 is the contention the ladder newly
        created (driver against admin lifecycle), and it is the one a driver
        needs rendered.
        """
        try:
            dialect = db.session.get_bind().dialect.name
        except Exception:  # noqa: BLE001 — no bind resolvable ⇒ nothing to bound
            return
        if dialect != "postgresql":
            return
        timeout_ms = current_app.config.get("BOTTLE_SCOPE_LOCK_TIMEOUT_MS", 5000)
        if not timeout_ms or int(timeout_ms) <= 0:
            return
        # NEVER CLOBBER AN EXPLICIT BOUND. `lock_timeout` is "0" (disabled)
        # unless something deliberately set it — which in production is nothing,
        # so this default always applies there. But a caller that HAS set one
        # (an operational script, a concurrency test pinning a tighter window)
        # has stated an intent more specific than this default, and silently
        # widening it would both slow those paths down and quietly defeat the
        # bound they asked for.
        if db.session.execute(text("SHOW lock_timeout")).scalar() not in ("0", "0ms", None):
            return
        # Interpolated, NOT a bound parameter, and deliberately so: Postgres
        # `SET` takes no placeholders. A bound `:ms` only appears to work
        # because psycopg2 binds CLIENT-side; under any server-side-binding
        # driver (psycopg3 with prepared statements) it becomes a syntax error
        # and every bottle write breaks at once. `int()` above is what makes
        # this injection-proof — the value can only ever be digits.
        db.session.execute(text(f"SET LOCAL lock_timeout = '{int(timeout_ms)}ms'"))

    @staticmethod
    def _raise_scope_busy(address_id: int, exc: BaseException) -> None:
        """Turn 55P03 into a NAMED, retryable domain error and clear the session.

        The rollback is MANDATORY, not tidiness: Postgres has already aborted
        this transaction, so every later statement on it fails with 25P02 and
        the driver's real error would be replaced by an unrelated one. It also
        leaves the session usable for whatever renders the response.

        `ConflictError` (409), not a 500 and not a 503:
          * a 500 is what the driver gets TODAY — `ExceptionMapper` has no entry
            for `OperationalError`, so it falls through to the unmapped default
            ("An unexpected error occurred", `error_code: None`) and is logged
            CRITICAL. Transient lock contention is not an incident.
          * a 503 renders in the staff bot as "service unavailable", which tells
            a driver the backend is down and invites them to stop trying.
          * 409 says what is true — someone else holds this place right now —
            and the staff bot already passes `error_code` through on that
            branch, so `BOTTLE_SCOPE_LOCK_TIMEOUT` reaches
            `API_ERROR_CODE_KEY_MAP` and renders in the driver's own language
            with an explicit "try again in a moment".
        """
        logger.warning(
            "[BOTTLE] scope lock timed out for address=%s after %sms — %s",
            address_id,
            current_app.config.get("BOTTLE_SCOPE_LOCK_TIMEOUT_MS", 5000),
            exc.__class__.__name__,
        )
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001 — the session is already doomed
            logger.exception("[BOTTLE] rollback after scope lock timeout failed")
        # The message NAMES THE MECHANISM on purpose. It is not what the driver
        # reads — the staff bot resolves copy from `error_code` via
        # `API_ERROR_CODE_KEY_MAP`, so a driver always gets the translated
        # `staff.error.api.scope_busy` text. This string is what reaches logs,
        # the admin UI and any non-bot API consumer, and there "lock timeout" is
        # the single most useful word in it.
        conflict = ConflictError(
            "This address is temporarily locked by an administrative update "
            "(lock timeout); nothing was saved. Please try again in a moment.",
            error_code="BOTTLE_SCOPE_LOCK_TIMEOUT",
            details={"address_id": address_id},
        )
        # Carry the DBAPI cause so the SQLSTATE stays introspectable — callers
        # and tests that ask "was this really 55P03?" must not have to parse a
        # sentence to find out.
        conflict.orig = getattr(exc, "orig", None)
        raise conflict from exc

    @staticmethod
    def resolve_scope(address_id: int) -> "BottleScope":
        """The place this address's bottles belong to (spec section 3).

        A missing address raises rather than silently resolving to itself: the
        singleton fallback in `get_address_group_member_ids` conflates "missing"
        with "ungrouped", and a balance keyed to a non-existent address would
        violate the FK on Postgres while passing silently in the FK-off suite.
        """
        from business_app.models.user import UserAddress
        from business_app.services.bottle_scope import BottleScope

        row = (
            db.session.query(UserAddress.id, UserAddress.address_group_id).filter(UserAddress.id == address_id).first()
        )
        if row is None:
            raise NotFoundError(f"Address {address_id} not found")
        _, group_id = row
        return BottleScope.for_group(group_id) if group_id is not None else BottleScope.for_address(address_id)

    # ------------------------------------------------------------------
    # RUNG 1 of the ladder — the SCOPE FENCE on `addresses`
    # ------------------------------------------------------------------

    @staticmethod
    def register_scope_lock(*, address_ids=(), group_ids=()) -> None:
        """Record rows this transaction has just taken a ladder lock on.

        Called by `resolve_scope_for_write` (rung 1, writer mode) and by the
        lifecycle's rung-0 / rung-1 acquirers in `CustomerLinkService`.
        """
        registry = _scope_lock_registry()
        registry["addresses"].update(int(a) for a in address_ids if a is not None)
        registry["groups"].update(int(g) for g in group_ids if g is not None)

    @staticmethod
    def assert_scope_locked(scope: "BottleScope", address_id: Optional[int]) -> None:
        """Refuse an EXPLICIT-scope balance write whose mapping is not pinned.

        An explicit `scope=` means "this specific place", which
        `get_or_create_balance` cannot self-serve — `_split_bottles_out_of_place`
        writes a group AND an address scope in one breath. So the function
        cannot resolve; it ASSERTS, and the caller must already hold either

          * the `addresses` row the write is attributed to (rung 1), or
          * the `address_groups` row of the scope being written (rung 0).

        This is a coverage probe, not the fence itself — see the module-level
        note on `_SCOPE_LOCK_REGISTRY_KEY`. It is what makes "no caller may
        forget the lock" a runtime property instead of a review convention, and
        it is the ONLY part of the ladder that is visible on SQLite.
        """
        registry = _scope_lock_registry()
        if address_id is not None and int(address_id) in registry["addresses"]:
            return
        if scope.is_grouped and int(scope.group_id) in registry["groups"]:
            return
        if not scope.is_grouped and scope.address_id is not None and int(scope.address_id) in registry["addresses"]:
            return
        raise ValidationError(
            f"Bottle write to {scope} (address {address_id}) without holding the "
            "scope lock — see BottleTrackingService.resolve_scope_for_write",
            error_code="BOTTLE_SCOPE_LOCK_NOT_HELD",
        )

    @staticmethod
    def assert_reachable(scope: "BottleScope") -> None:
        """A group-scoped balance row may only be MINTED for a live place.

        A place with zero members is unreachable: `resolve_scope` sends every
        reader — customer, driver, admin panel, sweep — somewhere else, so a row
        minted here would be the `orphaned_place_balances` violation §7.3's
        dissolve exists to eliminate. Second layer only: with the ladder in
        place this can no longer happen by race, but a frozen fine scope or a
        future path could still ask for it, and a named refusal beats silent
        corruption.

        STILL THE REFUSAL SITE for a fine frozen to a place that dissolved
        WITHOUT leaving a forwarding pointer. `resolve_frozen_scope_for_write`
        follows `address_groups.dissolved_onto_address_id` when there is one, so
        the ordinary dissolve no longer reaches this at all; what still does is a
        place dissolved before that column existed whose audit row could not be
        backfilled, or one whose survivor address has since been deleted. There
        is genuinely nowhere to book those, so they keep refusing here.

        `release_group_history_to_address` legitimately violates this — the
        zero-remaining dissolve arm clears the last pointer BEFORE releasing the
        history — and passes `allow_memberless=True` for exactly that reason.
        """
        from business_app.models.user import UserAddress

        if not scope.is_grouped:
            return
        member = db.session.query(UserAddress.id).filter(UserAddress.address_group_id == scope.group_id).first()
        if member is None:
            raise ValidationError(
                f"Place group {scope.group_id} has no member addresses; refusing to "
                "create a bottle balance no address can reach",
                error_code="BOTTLE_SCOPE_UNREACHABLE",
            )

    @staticmethod
    def resolve_scope_for_write(address_id: int) -> "BottleScope":
        """`resolve_scope`, but with RUNG 1 of the ladder held (FOR SHARE).

        THE WRITER MODE IS `FOR SHARE`, and it is not decoration. `addresses` has
        SIX foreign-key children — `orders`, `subscriptions`, `bottle_balances`,
        `bottle_ledger`, `bottle_fines`, `place_suggestion_dismissals`. Verified
        on this project's Postgres 17:

          * `FOR SHARE` vs `FOR SHARE`            -> COMPATIBLE. N concurrent
            deliveries at one address do NOT serialise on the mapping; they
            serialise on the balance row exactly as they did before.
          * `FOR SHARE` vs an FK-child INSERT     -> COMPATIBLE. Order and
            subscription creation at this address are untouched.
          * `FOR SHARE` vs `UPDATE addresses SET address_group_id = …`
                                                  -> BLOCKS. That is the fence.
          * `FOR SHARE` vs `FOR NO KEY UPDATE`    -> BLOCKS, so the lifecycle
            and a writer exclude each other.
          * plain `FOR UPDATE`                    -> BLOCKS EVERY FK-child
            INSERT. Never use it here; "simplifying" `read=True` away turns
            every bottle write into a stall on order creation at that address.
          * `FOR KEY SHARE`                       -> does NOT block the
            membership UPDATE. Too weak; a silent no-op.

        This queries COLUMNS, not the `UserAddress` entity, so it is immune BY
        CONSTRUCTION to the identity-map trap that makes `populate_existing()`
        mandatory on the lifecycle's entity load: `with_for_update()` re-reads
        the row and then DISCARDS the columns for an object already in the
        session, and `Session.get()` emits no SQL at all. A column query has no
        identity to be stale.

        Held until COMMIT (Postgres releases row locks only there), so the value
        returned here is the value the mapping still has when the balance row is
        written — and, if this transaction waited behind a membership change,
        Postgres's EvalPlanQual re-check hands back the WINNER's COMMITTED
        `address_group_id`, not the snapshot.
        """
        from business_app.models.user import UserAddress
        from business_app.services.bottle_scope import BottleScope

        # Bound the wait BEFORE taking the lock, and keep it for rungs 2/3 (see
        # `_apply_scope_lock_timeout`). Without this the driver waits for an
        # admin's whole place-merge transaction with no limit and no rendering.
        BottleTrackingService._apply_scope_lock_timeout()
        try:
            row = (
                db.session.query(UserAddress.id, UserAddress.address_group_id)
                .filter(UserAddress.id == address_id)
                .order_by(UserAddress.id.asc())
                .with_for_update(read=True)
                .first()
            )
        except OperationalError as exc:
            if BottleTrackingService._is_lock_not_available(exc):
                BottleTrackingService._raise_scope_busy(address_id, exc)
            raise
        if row is None:
            raise NotFoundError(f"Address {address_id} not found")
        _, group_id = row
        # Only the ADDRESS is registered. A shared lock on one member does not
        # pin the group's member SET — that is rung 0, and only the lifecycle
        # takes it.
        BottleTrackingService.register_scope_lock(address_ids=[address_id])
        return BottleScope.for_group(group_id) if group_id is not None else BottleScope.for_address(address_id)

    # ------------------------------------------------------------------
    # RUNG 1 for a write that belongs to a PAST EPISODE (spec §7.3)
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_frozen_scope_for_write(anchor_address_id: int, group_id: Optional[int]) -> "FrozenScopeTarget":
        """Where a write against a FROZEN scope must land TODAY, with rung 1 held.

        THE ONE FUNNEL FOR EVERY WRITE THAT BELONGS TO AN EARLIER EPISODE — a
        fine settled or waived long after it was issued
        (`bottle_fines.address_group_id`), and an order correction booked long
        after the delivery it corrects (the `delivery:{order}` ledger row). Both
        FREEZE the place they were booked to so the two halves of one physical
        handover cannot end up in two different ledgers, and freezing is only
        coherent because the lifecycle CARRIES frozen references:
        `absorb_address_into_group` and `release_group_history_to_address`
        re-stamp `bottle_fines` alongside `bottle_ledger`.

        THE ONE REFERENCE THE LIFECYCLE DELIBERATELY DOES NOT CARRY is a
        DEPARTED member's. §7.1/§7.3 leave its rows stamped with the group it
        left, because NULLing them would drop the place's history into a departed
        address's own scope and mint bottles onto someone who left with nothing.
        When that group later DISSOLVES, the frozen reference names a place with
        no members and no `bottle_balances` row — and booking there would
        re-INSERT exactly the orphan §7.3's dissolve exists to delete.

        `address_groups.dissolved_onto_address_id` is the way out: the dissolve
        records which address it released the place's history onto, so a frozen
        reference to a dead place FOLLOWS the history instead of refusing. The
        pointer names an ADDRESS and this resolves that address's LIVE scope, so
        a survivor that has since joined a new place forwards to THAT place; the
        pointer therefore never chains and is never rewritten (a dissolved group
        can never be re-populated — `PLACE_GROUP_DISSOLVED`).

        THE FOUR ANSWERS, and `FrozenScopeTarget` names which one you got:

          * `group_id is None`  -> `for_address(anchor)`. Nothing was frozen.
          * the place is LIVE   -> `for_group(group_id)`. UNCHANGED, and this is
            every ordinary call: the freeze stands.
          * the place DISSOLVED and left a pointer -> the SURVIVOR's live scope,
            `forwarded=True`, and `address_id` moves to the survivor. That move
            is MANDATORY, not cosmetic: an ADDRESS scope's ledger predicate is
            `address_id = X AND address_group_id IS NULL`, so an entry attributed
            to the anchor while its balance moved on the survivor's row would put
            the ledger and the balance in two different scopes and drift both.
            The original door survives in the entry metadata (`audit()`).
          * the place DISSOLVED with NO pointer -> `unreachable=True` and the
            FROZEN scope is handed back untouched, so the CALLER refuses exactly
            as it did before this column existed. Reachable two ways: the place
            dissolved before the pointer existed and its audit row could not be
            backfilled, or the survivor address has since been deleted (the FK is
            ON DELETE SET NULL). Never invent a destination here.

        LOCK ORDERING (spec §5.2). Rung 1 is taken HERE, on EVERY `addresses` row
        this write can touch, in ONE ASCENDING statement — never as two separate
        acquisitions. Two `FOR SHARE` holders never block each other, but the
        lifecycle's rung 1 is `FOR NO KEY UPDATE` over its whole member set
        ascending, so a forwarding writer that took `addresses(anchor)` and THEN
        `addresses(survivor)` out of id order would be a textbook ABBA against it
        the moment both addresses had since joined the same place. One statement
        with `ORDER BY id` makes that unrepresentable: `LockRows` sits above
        `Sort`, so ordering the query orders the acquisition. It is also why both
        callers resolve through this funnel BEFORE taking any other rung-1 lock.

        The liveness pre-read below is UNLOCKED and cannot be stale in a
        direction that matters. A group only ever goes live -> dissolved, and the
        dissolve writes the pointer in the SAME transaction that empties the
        group, so under READ COMMITTED this sees either the whole pre-image
        (members present -> the LIVE arm, i.e. exactly today's behaviour and
        today's guarantees, with `assert_reachable` still standing behind it) or
        the whole post-image (no members, pointer set). There is no torn state
        between the two.
        """
        from business_app.models.customer_link import AddressGroup
        from business_app.models.user import UserAddress
        from business_app.services.bottle_scope import BottleScope

        anchor_id = int(anchor_address_id)
        if group_id is None:
            BottleTrackingService.resolve_scope_for_write(anchor_id)
            return FrozenScopeTarget(
                scope=BottleScope.for_address(anchor_id),
                address_id=anchor_id,
                anchor_address_id=anchor_id,
                from_group_id=None,
                forwarded=False,
                unreachable=False,
            )

        group_id = int(group_id)
        live = db.session.query(UserAddress.id).filter(UserAddress.address_group_id == group_id).first() is not None
        survivor_id = (
            None
            if live
            else db.session.query(AddressGroup.dissolved_onto_address_id).filter(AddressGroup.id == group_id).scalar()
        )
        if survivor_id is None:
            # Live place, or a dead one with nowhere to forward to. Both hand the
            # FROZEN scope straight back; only `unreachable` tells them apart, and
            # only the caller knows what to call the refusal.
            BottleTrackingService.resolve_scope_for_write(anchor_id)
            return FrozenScopeTarget(
                scope=BottleScope.for_group(group_id),
                address_id=anchor_id,
                anchor_address_id=anchor_id,
                from_group_id=group_id,
                forwarded=False,
                unreachable=not live,
            )

        survivor_id = int(survivor_id)
        wanted = sorted({anchor_id, survivor_id})
        BottleTrackingService._apply_scope_lock_timeout()
        try:
            rows = (
                db.session.query(UserAddress.id, UserAddress.address_group_id)
                .filter(UserAddress.id.in_(wanted))
                .order_by(UserAddress.id.asc())
                .with_for_update(read=True)
                .all()
            )
        except OperationalError as exc:
            if BottleTrackingService._is_lock_not_available(exc):
                BottleTrackingService._raise_scope_busy(survivor_id, exc)
            raise
        mapping = {int(r[0]): r[1] for r in rows}
        BottleTrackingService.register_scope_lock(address_ids=list(mapping))
        if anchor_id not in mapping:
            raise NotFoundError(f"Address {anchor_id} not found")
        if survivor_id not in mapping:
            # ON DELETE SET NULL means a database with foreign keys ON cannot
            # reach this; one running without them can (the fast suite is SQLite
            # with FKs OFF). A vanished survivor is treated exactly like a cleared
            # pointer — refuse, never book onto an address that is not there.
            return FrozenScopeTarget(
                scope=BottleScope.for_group(group_id),
                address_id=anchor_id,
                anchor_address_id=anchor_id,
                from_group_id=group_id,
                forwarded=False,
                unreachable=True,
            )
        survivor_group_id = mapping[survivor_id]
        return FrozenScopeTarget(
            scope=(
                BottleScope.for_group(survivor_group_id)
                if survivor_group_id is not None
                else BottleScope.for_address(survivor_id)
            ),
            address_id=survivor_id,
            anchor_address_id=anchor_id,
            from_group_id=group_id,
            forwarded=True,
            unreachable=False,
        )

    @staticmethod
    def _assert_user_in_scope(user_id: int, address_id: int, scope: "BottleScope" = None) -> None:
        """The user must own the address, or own a member address of its place.

        Replaces the pre-place `balance.user_id == user_id` check, which Task 2
        made impossible (bottle_balances no longer has user_id) but whose intent
        — you cannot act on a stranger's address — still holds, and matters more
        now that one address reaches a whole shared place.

        `scope` is threaded from the caller's SINGLE locked resolution where one
        exists. Without it this authorises against one unlocked read of
        membership while the caller writes against another: a member departing
        between the two lets an authorised coworker's collection land in a
        stranger's private scope.
        """
        from business_app.models.user import UserAddress

        scope = scope or BottleTrackingService.resolve_scope(address_id)
        q = db.session.query(UserAddress.id).filter(UserAddress.user_id == user_id)
        q = (
            q.filter(UserAddress.address_group_id == scope.group_id)
            if scope.is_grouped
            else q.filter(UserAddress.id == scope.address_id)
        )
        if q.first() is None:
            raise ValidationError(
                "User does not belong to this address's place",
                error_code="BOTTLE_SCOPE_MEMBERSHIP_REQUIRED",
            )

    @staticmethod
    def _place_member_address_ids(scope: "BottleScope") -> List[int]:
        """Every address at this place, LOWEST ID FIRST.

        The single ordering rule behind both `representative_address_id` (what
        the admin UI sends) and the derived attribution user (what the audit row
        records). Two copies of "which address represents this place" would drift
        into a UI that opens one place and a write that books another.
        """
        from business_app.models.user import UserAddress

        if scope.is_grouped:
            rows = (
                db.session.query(UserAddress.id)
                .filter(UserAddress.address_group_id == scope.group_id)
                .order_by(UserAddress.id.asc())
                .all()
            )
            return [r[0] for r in rows]
        return [scope.address_id] if scope.address_id is not None else []

    @staticmethod
    def resolve_place_attribution_user_id(address_id: int, scope: "BottleScope" = None) -> int:
        """The user an admin place-write is booked against when none is named.

        `bottle_balances` has no `user_id` at all, so this is NOT a balance axis:
        `bottle_ledger.user_id` / `bottle_fines.user_id` are NOT NULL audit
        stamps recording which member's address the write went through. Deriving
        one therefore cannot move a bottle.

        Pinned to the owner of the place's REPRESENTATIVE address — the lowest
        member address id, the same rule `serialize_bottle_balance` publishes as
        `representative_address_id`. Every member address of a place resolves to
        the same answer, so two identical calls can never attribute to two
        different coworkers.

        `scope` is threaded from the caller's single locked resolution so the
        attribution and the write describe the same membership (see
        `_assert_user_in_scope`).
        """
        from business_app.models.user import UserAddress

        scope = scope or BottleTrackingService.resolve_scope(address_id)
        member_ids = BottleTrackingService._place_member_address_ids(scope)
        # `resolve_scope` already rejected a missing address, and that address is
        # itself a member of whatever scope it produced — so this is empty only
        # for a scope built by hand from a group that has since been emptied.
        representative_id = member_ids[0] if member_ids else address_id
        owner_id = db.session.query(UserAddress.user_id).filter(UserAddress.id == representative_id).scalar()
        if owner_id is None:
            raise NotFoundError(f"Address {representative_id} not found")
        return owner_id

    def _authorised_place_attribution(self, user_id: Optional[int], address_id: int) -> Tuple["BottleScope", int]:
        """RUNG 1 + attribution + the membership fence, in the ONE fixed order.

        THE SHARED FUNNEL FOR THE ADMIN PLACE WRITES. All three admin bodies —
        `admin_adjust_balance`, `set_initial_balance`, `issue_fine` — need
        exactly this triple, and it has to happen in exactly this order:

          1. ONE locked resolution (`resolve_scope_for_write`), threaded into
             everything below and into the write itself, so the authorisation
             and the write can never describe two different memberships.
          2. DERIVE the audit stamp when no member was named. `user_id` is
             OPTIONAL BY DESIGN on all three routes (an admin adjusts a PLACE
             and there is no coworker picker anywhere), and a derived one is a
             member by construction — which is why derivation must come BEFORE
             the fence rather than be excused from it.
          3. FENCE an EXPLICITLY supplied one. Only `issue_fine` ever did this;
             the other two accepted a stranger's id, booking a stranger onto
             the place's ledger where `GET /bottles/ledger?user_id=<stranger>`
             then disclosed it — and accepted an id that does not exist AT ALL,
             straight into `bottle_ledger.user_id` / `bottle_fines.user_id`,
             which are NOT NULL FOREIGN KEYS (an IntegrityError 500 on
             Postgres; on the FK-off test backend, a committed dangling FK no
             test can see). `_assert_user_in_scope` closes both halves at once:
             a nonexistent user owns no address anywhere, so it never reaches
             the FK.

        Extracted rather than pasted into the two missing bodies BECAUSE that
        is the defect's real shape: the fence existed and one of three callers
        remembered it. A future admin write path now gets it by calling the
        funnel, not by remembering a convention.

        NOT used by `record_standalone_collection`, deliberately: its `user_id`
        is REQUIRED (a driver names the customer they collected from), so it
        must not gain the "derive when absent" arm. It keeps its own explicit
        `resolve_scope_for_write` + `_assert_user_in_scope` pair.
        """
        scope = self.resolve_scope_for_write(address_id)
        if user_id is None:
            user_id = self.resolve_place_attribution_user_id(address_id, scope=scope)
        self._assert_user_in_scope(user_id, address_id, scope=scope)
        return scope, user_id

    # ------------------------------------------------------------------
    # Balance management
    # ------------------------------------------------------------------

    @staticmethod
    def get_balance_row(scope: "BottleScope") -> Optional[BottleBalance]:
        """RUNG 2/3: this scope's balance row FOR UPDATE — locked, never created.

        For readers that must not mint a row (`reconcile_balance`): a place that
        has never moved a bottle must not gain a 0.00 row just for being looked
        at, and minting one is how the `orphaned_place_balances` class comes
        back.
        """
        return BottleBalance.query.filter(*scope.balance_filter()).with_for_update().first()

    @staticmethod
    def get_or_create_balance(
        address_id: int, scope: "BottleScope" = None, *, allow_memberless: bool = False
    ) -> BottleBalance:
        """Get the PLACE's balance row LOCKED FOR UPDATE, creating a zero row first.

        THE SSOT FUNNEL FOR RUNGS 1→2/3. Locking serialises concurrent
        read-modify-write on the same place — matching cash_collection_service's
        pattern — so concurrent deliveries/returns/adjustments cannot lose
        updates. That lock is shared by every member of a place; see the
        lock-ordering rule in `OrderEditService` (spec §5.2). The scope uniques
        make the create race safe via ON CONFLICT DO NOTHING + re-select. Runs
        inside the caller's transaction; does NOT commit.

        The two arms are principled, not a convenience split:

        * `scope is None` — the caller means "wherever this address lives". Rung
          1 is taken HERE, BEFORE the balance row is chosen, via
          `resolve_scope_for_write`. Nothing is left to re-validate afterwards:
          the mapping was pinned before the row was picked, so the row picked is
          the row the mapping still names at COMMIT. This covers
          `record_bottles_delivered`, `record_bottles_returned`,
          `record_standalone_collection`, `admin_adjust_balance` and
          `OrderEditService._cascade_bottle` with ZERO call-site edits.
        * `scope is not None` — the caller means a SPECIFIC place the address may
          not currently map to (the §7.1 split writes the group scope and the
          address scope in one breath). The function cannot self-serve, so it
          asserts the caller already holds the lock; see `assert_scope_locked`.

        `allow_memberless` exists for exactly ONE call site — the §7.3 dissolve's
        `release_group_history_to_address` — because the zero-remaining arm
        clears the departing address's pointer BEFORE releasing the history, so
        the group legitimately has no members at that instant.
        """
        if scope is None:
            scope = BottleTrackingService.resolve_scope_for_write(address_id)
        else:
            BottleTrackingService.assert_scope_locked(scope, address_id)
        criteria = scope.balance_filter()

        # NOT wrapped, deliberately. Rungs 2/3 contend writer-against-writer on
        # one place's balance row — a fast, already-existing contention that this
        # change does not alter. Rung 1 is the NEW one (writer against the admin
        # lifecycle), and it is the only one converted, so the lifecycle's own
        # balance-row waits keep raising the raw DBAPIError that
        # `tests/integration/test_place_concurrency_pg_e2e.py` asserts SQLSTATE on.
        balance = BottleBalance.query.filter(*criteria).with_for_update().first()
        if balance is not None:
            # Spec §13: a row reaching a write path with two scope keys (or
            # none) is unreachable through `BottleScope`, so it can only come
            # from a database that predates `ck_bottle_balance_scope`. Reject it
            # by name here rather than letting it corrupt a balance silently.
            BottleTrackingService.assert_scope_row_valid(balance)
            return balance

        if not allow_memberless:
            BottleTrackingService.assert_reachable(scope)
        insert_stmt = (
            pg_insert(BottleBalance.__table__)
            .values(balance=Decimal("0.00"), **scope.balance_defaults())
            .on_conflict_do_nothing(index_elements=[scope.conflict_column()])
        )
        db.session.execute(insert_stmt)
        db.session.flush()
        balance = BottleBalance.query.filter(*criteria).with_for_update().first()
        BottleTrackingService.assert_scope_row_valid(balance)
        return balance

    def _update_balance(
        self,
        address_id: int,
        quantity_delta: Decimal,
        *,
        is_delivery: bool = False,
        is_return: bool = False,
        scope: "BottleScope" = None,
        allow_memberless: bool = False,
    ) -> BottleBalance:
        """Atomically update the place's balance and timestamp fields."""
        balance = self.get_or_create_balance(address_id, scope=scope, allow_memberless=allow_memberless)
        balance.balance = (balance.balance or Decimal("0.00")) + quantity_delta
        now = self._utc_now()
        if is_delivery:
            balance.last_delivery_at = now
        if is_return:
            balance.last_return_at = now
        return balance

    # ------------------------------------------------------------------
    # Ledger writes
    # ------------------------------------------------------------------

    def _create_ledger_entry_with_status(
        self,
        *,
        user_id: int,
        address_id: int,
        event_type: BottleLedgerEventType,
        quantity: Decimal,
        actor_user_id: int = None,
        order_id: int = None,
        delivery_id: int = None,
        notes: str = None,
        idempotency_key: str = None,
        metadata: dict = None,
        scope: "BottleScope" = None,
        allow_memberless: bool = False,
    ) -> Tuple[BottleLedger, bool]:
        """Create a ledger entry and update the materialized balance.

        THE LEDGER STAMP IS READ BACK OFF THE LOCKED BALANCE ROW, never resolved
        separately. `_update_balance` -> `get_or_create_balance` is the one place
        rung 1 is taken and the balance row is chosen, so the row that moves and
        the `address_group_id` the entry carries are the SAME OBJECT by
        construction. Resolving the scope again here — as this used to, with an
        unlocked `resolve_scope` — is what let a delivery move one place's
        balance while stamping its ledger row to another.

        Returns ``(entry, created)``. ``created`` is False on exactly one path:
        the idempotency short-circuit that returns a PRE-EXISTING row, which
        returns BEFORE `_update_balance`, so no balance moved and no lock was
        taken. A caller with a side effect that must not repeat — a session
        tally, or arithmetic that assumes the stored figure moved — MUST gate on
        it. `_create_ledger_entry` keeps the one-value contract for the 16
        callers that have no such side effect.

        NOTE: ``created is False`` implies a key WAS supplied — the dedup query
        only runs inside `if idempotency_key:` — which is what lets
        `record_standalone_collection` scope its replay-payload check to the
        client-token path without a second condition.
        """
        # Check idempotency
        if idempotency_key:
            existing = BottleLedger.query.filter_by(idempotency_key=idempotency_key).first()
            if existing:
                logger.info("Duplicate ledger entry skipped: %s", idempotency_key)
                return existing, False

        # Determine if delivery or return for balance timestamp tracking
        is_delivery = event_type in (
            BottleLedgerEventType.DELIVERY,
            BottleLedgerEventType.INITIAL_BALANCE,
        )
        is_return = event_type in (
            BottleLedgerEventType.RETURN_ON_DELIVERY,
            BottleLedgerEventType.STANDALONE_COLLECTION,
        )

        balance_record = self._update_balance(
            address_id,
            quantity,
            is_delivery=is_delivery,
            is_return=is_return,
            scope=scope,
            allow_memberless=allow_memberless,
        )
        # The scope the balance ACTUALLY landed on, read back off the locked row.
        scope = (
            BottleScope.for_group(balance_record.address_group_id)
            if balance_record.address_group_id is not None
            else BottleScope.for_address(balance_record.address_id)
        )

        entry = BottleLedger(
            user_id=user_id,
            address_id=address_id,
            address_group_id=scope.group_id,
            order_id=order_id,
            delivery_id=delivery_id,
            event_type=event_type,
            quantity=quantity,
            balance_after=balance_record.balance,
            actor_user_id=actor_user_id,
            occurred_at=self._utc_now(),
            notes=notes,
            idempotency_key=idempotency_key,
            entry_metadata=metadata or {},
        )
        db.session.add(entry)
        db.session.flush()
        return entry, True

    def _create_ledger_entry(self, **kwargs) -> BottleLedger:
        """Back-compatible one-value view of `_create_ledger_entry_with_status`.

        Kept so the other 16 call sites — including the two `**shared` splats in
        this module, `customer_link_service.py`'s pair, and the cross-module
        reach-in at `order_edit_service.py` — compile and behave identically.
        `_create_ledger_entry_with_status` is keyword-only, so `**kwargs`
        forwarding is total.
        """
        entry, _created = self._create_ledger_entry_with_status(**kwargs)
        return entry

    def _create_ledger_backfill_entry(
        self,
        *,
        scope: "BottleScope",
        user_id: int,
        address_id: int,
        quantity: Decimal,
        actor_user_id: int = None,
        notes: str = None,
        idempotency_key: str = None,
        metadata: dict = None,
    ) -> BottleLedger:
        """THE ONLY balance-DECOUPLED ledger writer in this codebase.

        Writes a `bottle_ledger` row and deliberately does NOT touch
        `bottle_balances`. Its single sanctioned use is spec §7.4's merge
        backfill: ALIGNING THE LEDGER TO THE BALANCE THE PLACE ALREADY CARRIES,
        so a place whose stored figure was set by hand before it was ever
        grouped gets a ledger that finally explains that figure. The quantity
        is signed and BOTH directions are real — the ledger may have recorded
        too little (an opening balance it never had, positive) or too much (a
        surplus it over-recorded, negative) — so nothing here, including the
        caller's `notes`, may assume one sign.

        Two fences keep this narrow, and both are enforced rather than advisory:
        `idempotency_key` MUST be present and MUST sit in one of
        `BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES` (below), and
        `tests/integration/test_bottle_place_lock_order.py` pins that this
        method has exactly ONE call site in the tree.

        NOTHING ELSE MAY USE THIS. Every real bottle movement — delivery,
        return, collection, fine, admin adjustment, place split, dissolve,
        merge exclusion, merge correction — is a movement of actual crates and
        must go through `_create_ledger_entry`, which moves the balance in the
        same breath. That coupling is the reason the two figures cannot silently
        drift apart, and it is why `_create_ledger_entry` must NOT be loosened
        to take a "skip the balance" flag: a flag is an invitation, a separate
        narrowly-named method is a fence.

        WHY A DECOUPLED WRITE IS THE RIGHT INSTRUMENT HERE, AND THE ONLY ONE.
        A balance-coupled append moves the stored figure and the ledger sum by
        the SAME amount, so it can never close a pre-existing gap between them —
        their difference is invariant under it. Recording the missing opening
        balance is precisely the operation that is a ledger fact and NOT a
        bottle movement: no crate arrives, no crate leaves, the place holds
        exactly what it held a second ago. Moving the balance too would mint
        the drift a second time.

        `balance_after` is computed from this scope's ledger, not from the
        balance row, for the same reason. Callers that append further entries
        should still finish with `recompute_balance_after(scope)`.
        """
        # The key fence. A decoupled entry MUST be identifiable by its key: the
        # conservation split (`Σ balances == Σ COUPLED`) is only checkable
        # because decoupled rows can be told apart from coupled ones, and an
        # unkeyed one would be counted as coupled and pass a pin it violates.
        if not idempotency_key or not idempotency_key.startswith(BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES):
            raise ValidationError(
                "A balance-decoupled ledger entry requires an idempotency key in one of "
                f"{list(BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES)}; got {idempotency_key!r}. "
                "Every real bottle movement belongs in _create_ledger_entry instead.",
                error_code="BOTTLE_DECOUPLED_KEY_REQUIRED",
            )
        existing = BottleLedger.query.filter_by(idempotency_key=idempotency_key).first()
        if existing:
            logger.info("Duplicate ledger backfill skipped: %s", idempotency_key)
            return existing

        quantity = Decimal(str(quantity or 0))
        ledger_sum = Decimal(
            str(
                db.session.query(func.coalesce(func.sum(BottleLedger.quantity), Decimal("0.00")))
                .filter(*scope.ledger_filter())
                .scalar()
                or 0
            )
        )
        entry = BottleLedger(
            user_id=user_id,
            address_id=address_id,
            address_group_id=scope.group_id,
            event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT,
            quantity=quantity,
            balance_after=ledger_sum + quantity,
            actor_user_id=actor_user_id,
            occurred_at=self._utc_now(),
            notes=notes,
            idempotency_key=idempotency_key,
            entry_metadata=metadata or {},
        )
        db.session.add(entry)
        db.session.flush()
        return entry

    # ------------------------------------------------------------------
    # Public ledger operations
    # ------------------------------------------------------------------

    def record_bottles_delivered(
        self,
        order_id: int,
        user_id: int,
        address_id: int,
        quantity: Decimal,
        actor_user_id: int = None,
    ) -> BottleLedger:
        """Record bottles delivered to customer via an order (+quantity)."""
        logger.info(
            "[BOTTLE] record_bottles_delivered order=%s user=%s address=%s qty=%s actor=%s",
            order_id,
            user_id,
            address_id,
            quantity,
            actor_user_id,
        )
        entry = self._create_ledger_entry(
            user_id=user_id,
            address_id=address_id,
            event_type=BottleLedgerEventType.DELIVERY,
            quantity=self._as_decimal(quantity),
            actor_user_id=actor_user_id,
            order_id=order_id,
            idempotency_key=f"delivery:{order_id}",
            metadata={"source": "order_delivery"},
        )
        logger.info(
            "[BOTTLE] record_bottles_delivered OK order=%s ledger_id=%s balance_after=%s",
            order_id,
            entry.id,
            entry.balance_after,
        )
        return entry

    def record_bottles_returned(
        self,
        user_id: int,
        address_id: int,
        quantity: Decimal,
        *,
        order_id: int = None,
        delivery_id: int = None,
        actor_user_id: int = None,
        notes: str = None,
    ) -> BottleLedger:
        """Record bottles returned by customer during a delivery (-quantity)."""
        logger.info(
            "[BOTTLE] record_bottles_returned order=%s delivery=%s user=%s address=%s qty=%s actor=%s",
            order_id,
            delivery_id,
            user_id,
            address_id,
            quantity,
            actor_user_id,
        )
        qty = self._as_decimal(quantity)
        if qty <= 0:
            raise ValidationError("Return quantity must be positive")
        entry = self._create_ledger_entry(
            user_id=user_id,
            address_id=address_id,
            event_type=BottleLedgerEventType.RETURN_ON_DELIVERY,
            quantity=-qty,
            actor_user_id=actor_user_id,
            order_id=order_id,
            delivery_id=delivery_id,
            notes=notes,
            idempotency_key=f"return:{order_id}:{delivery_id}" if order_id else None,
            metadata={"source": "return_on_delivery"},
        )
        logger.info(
            "[BOTTLE] record_bottles_returned OK order=%s ledger_id=%s balance_after=%s",
            order_id,
            entry.id,
            entry.balance_after,
        )
        return entry

    @transactional
    def record_standalone_collection(
        self,
        user_id: int,
        address_id: int,
        quantity: Decimal,
        actor_user_id: int,
        notes: str = None,
        idempotency_key: str = None,
    ) -> BottleLedger:
        """Record standalone bottle pickup by driver outside order flow (-quantity).

        ONE locked resolution serves both the authorisation and the write. With
        two unlocked reads a member departing in between lets an authorised
        coworker's collection land in a stranger's private scope.

        `idempotency_key` is the driver's PER-INTENT retry token, minted once at
        the confirm step. It is validated and namespaced server-side
        (`compose_client_idempotency_key`) and a dedup hit is compared against
        this request (`_assert_replay_matches_collection`) before it is honoured.
        Composed AFTER `_assert_user_in_scope` so a replay from a departed
        coworker is still refused with BOTTLE_SCOPE_MEMBERSHIP_REQUIRED rather
        than silently deduped — `test_place_lifecycle_full_e2e.py:2749` pins that
        ordering; never hoist the dedup above the scope guard.
        """
        scope = self.resolve_scope_for_write(address_id)
        self._assert_user_in_scope(user_id, address_id, scope=scope)
        qty = self._as_decimal(quantity)
        if qty <= 0:
            raise ValidationError("Collection quantity must be positive")

        stored_key = self.compose_client_idempotency_key("collect", actor_user_id, idempotency_key)

        try:
            entry, created = self._create_ledger_entry_with_status(
                user_id=user_id,
                address_id=address_id,
                event_type=BottleLedgerEventType.STANDALONE_COLLECTION,
                quantity=-qty,
                actor_user_id=actor_user_id,
                notes=notes,
                idempotency_key=stored_key,
                scope=scope,
                metadata={"source": "standalone_collection"},
            )
        except IntegrityError:
            # THE CHECK-THEN-INSERT RACE. The idempotency SELECT holds no lock
            # that excludes a peer, so two concurrent same-key writes both miss
            # it and the btree UNIQUE makes the loser raise. Roll the whole
            # transaction back (this method is always entered TOP-LEVEL from a
            # route under a non-nesting @transactional, so nothing else is in
            # flight), then re-read in the fresh transaction the rollback starts.
            # On Postgres the losing INSERT blocked until the winner committed,
            # so the row is visible under READ COMMITTED.
            #
            # NOT a SAVEPOINT: `begin_nested()` on pysqlite RELEASEs into a
            # COMMIT after SELECT-only work, which would split this method into
            # two transactions on the engine the whole acceptance suite runs on.
            if not stored_key:
                raise
            db.session.rollback()
            entry = BottleLedger.query.filter_by(idempotency_key=stored_key).first()
            if entry is None:
                raise  # not our unique constraint — let it map as before
            created = False

        if not created:
            # A hit on the key ALONE is not proof of a replay: the lookup carries
            # no user/address/quantity predicate, so one token reused at every
            # door would silently suppress real collections.
            self._assert_replay_matches_collection(entry, user_id=user_id, address_id=address_id, quantity=-qty)

        # THE TALLY LIVES INSIDE THE DEDUP FENCE. `update_session_delivery_tally`
        # is a bare increment with no idempotency of its own, so calling
        # it on a REPLAY credits the driver's session with bottles that were
        # already counted — and the damage is UNRECOVERABLE: `admin_adjust_balance`
        # repairs the CUSTOMER's balance and no admin surface can touch
        # `bottles_collected_from_customers`, so the trip closes with a fabricated
        # surplus against the driver forever.
        #
        # `created is True` for every UNKEYED call (the dedup branch is inside
        # `if idempotency_key:`), so behaviour without a token is byte-identical
        # to before this fence existed.
        if created:
            # Tally against the driver's open session so session inventory stays accurate
            self.update_session_delivery_tally(
                actor_user_id,
                bottles_collected=int(qty),
            )
        return entry

    @transactional
    def admin_adjust_balance(
        self,
        user_id: Optional[int],
        address_id: int,
        adjustment: Decimal,
        actor_user_id: int,
        notes: str,
        *,
        idempotency_key: Optional[str] = None,
    ) -> BottleLedger:
        """Admin manually adjusts a PLACE's balance. Positive = more bottles owed.

        `idempotency_key` (optional) makes the write replay-safe: a second call
        with the same key returns the stored row and moves nothing. A key hit
        counts as a replay only when the stored row has the same address,
        quantity and ADMIN_ADJUSTMENT type; any other match is a
        `BOTTLE_ADJUSTMENT_KEY_CONFLICT` (409), never a silent no-op.

        `user_id=None` means "no member named" — the admin adjusts the place, and
        the audit stamp is derived (see `resolve_place_attribution_user_id`).
        An explicitly named one is FENCED: see `_authorised_place_attribution`,
        the shared funnel this now goes through with the other two admin bodies.
        """
        if not notes:
            raise ValidationError("Notes are required for admin adjustments")
        scope, user_id = self._authorised_place_attribution(user_id, address_id)
        quantity = self._as_decimal(adjustment)
        entry, created = self._create_ledger_entry_with_status(
            user_id=user_id,
            address_id=address_id,
            event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT,
            quantity=quantity,
            actor_user_id=actor_user_id,
            notes=notes,
            idempotency_key=idempotency_key,
            scope=scope,
            metadata={"source": "admin_adjustment"},
        )
        if not created and (
            int(entry.address_id) != int(address_id)
            or entry.event_type != BottleLedgerEventType.ADMIN_ADJUSTMENT
            or self._as_decimal(entry.quantity) != quantity
        ):
            raise ConflictError(
                "idempotency_key was already used for a different adjustment",
                error_code="BOTTLE_ADJUSTMENT_KEY_CONFLICT",
            )
        return entry

    @transactional
    def set_initial_balance(
        self,
        user_id: Optional[int],
        address_id: int,
        quantity: Decimal,
        actor_user_id: int,
        notes: str = None,
    ) -> BottleLedger:
        """Set the starting bottle balance for a place (admin data population).

        `user_id=None` means "no member named"; the audit stamp is derived (see
        `resolve_place_attribution_user_id`). An explicitly named one is FENCED
        through the shared `_authorised_place_attribution` funnel, and it matters
        more here than anywhere else: an initial balance is ONE-SHOT per place
        (`BOTTLE_INITIAL_BALANCE_EXISTS`), so a stranger's stamp accepted here
        could never be re-seeded away.

        NO IDEMPOTENCY KEY, DELIBERATELY. It was vestigial and actively harmful.
        `uq_bottle_ledger_idempotency` is UNIQUE on the KEY ALONE, so
        `_create_ledger_entry`'s duplicate lookup carries no scope predicate:
        an `initial:place:{G}` row left behind by a DISSOLVED place, or an
        `initial:addr:{A}` row that survived A's join re-stamp, made a later
        legitimate call silently no-op and return 200 echoing ANOTHER
        CUSTOMER'S ledger row. Adding a scope predicate to that lookup is not
        the fix — the UNIQUE index would then turn the no-op into an
        IntegrityError 500.

        The real guard is STRUCTURAL and always was: "this place has no history
        yet", which also stops two coworkers each seeding the same office. It
        now runs under rung 1 (`addresses` FOR SHARE) AND the balance row's
        FOR UPDATE, so concurrency cannot defeat it either.
        """
        qty = self._as_decimal(quantity)
        # ONE locked resolution, threaded into the attribution, the fence and the
        # write, so they cannot describe two different memberships.
        scope, user_id = self._authorised_place_attribution(user_id, address_id)

        balance = self.get_or_create_balance(address_id, scope=scope)  # holds FOR UPDATE
        has_history = db.session.query(BottleLedger.id).filter(*scope.ledger_filter()).first() is not None
        if has_history or (balance.balance or Decimal("0.00")) != Decimal("0.00"):
            raise ValidationError(
                "This place already has a bottle balance or ledger history",
                error_code="BOTTLE_INITIAL_BALANCE_EXISTS",
            )

        return self._create_ledger_entry(
            user_id=user_id,
            address_id=address_id,
            event_type=BottleLedgerEventType.INITIAL_BALANCE,
            quantity=qty,
            actor_user_id=actor_user_id,
            notes=notes or "Initial balance set by admin",
            scope=scope,
            metadata={"source": "initial_balance"},
        )

    # ------------------------------------------------------------------
    # Place lifecycle: joining a place (spec §7.2)
    # ------------------------------------------------------------------

    @staticmethod
    def absorb_address_into_group(address_id: int, group_id: int) -> Dict[str, Any]:
        """Move an address's OWN-scope bottle history into a place group (spec §7.2).

        Selector is `address_id = a AND address_group_id IS NULL`. The IS NULL
        arm is mandatory: after a departure an address's rows stay stamped with
        its FORMER group (spec §7.1), and a bare `address_id = a` would drag
        that whole place's history into the new group on a re-join — which §5.1
        calls routine.

        Both halves of the address's own scope move, and they move TOGETHER:
        the ledger rows are re-stamped, and the address-keyed `bottle_balances`
        row is removed after its figure is handed back to the caller to credit
        onto the place. Dropping the row without carrying its value would
        DESTROY bottles — a balance is not always re-derivable from this
        scope's ledger (a place whose figure was seeded before the ledger
        existed has a row and no entries), and `reconcile_balance` would then
        rebuild the place at 0.

        CONCURRENCY: the address's row is taken FOR UPDATE and the figure is
        read off the LOCKED row, never by a separate unlocked SUM. Under READ
        COMMITTED an unlocked read-then-delete is a lost update — a delivery or
        return committing at this address in between would be deleted away while
        the stale figure was credited, destroying exactly the bottles this
        method exists to conserve.

        LOCK ORDERING (spec §5.2): the caller MUST already hold the destination
        GROUP's row, because this takes the ADDRESS's. That is the order
        `CustomerLinkService._split_bottles_out_of_place` fixed for every
        two-row acquisition, and taking them the other way round is an ABBA
        deadlock against a concurrent removal of the same (group, address) pair.

        Does NOT touch the group's balance or `balance_after`; the caller
        absorbs every joiner first, then credits and re-runs the snapshots once.

        THE ROW'S PROVENANCE COLUMNS COME BACK WITH ITS FIGURE, for exactly the
        same reason the figure does. `last_delivery_at` / `last_return_at` are
        the only place facts NO later pass can rebuild — `recompute_balance_after`
        rewrites `balance_after`, `reconcile_balance` rewrites `balance`, and
        both ignore these two columns entirely — so a value dropped here is
        dropped for good. They are not internal either: `BottleTracking.js`
        renders `last_delivery_at` as an admin table column and
        `get_customer_summary` publishes both per address, so a place served
        yesterday and grouped today reads NEVER SERVED. Handing them back rather
        than writing them here keeps this method's contract intact — it absorbs
        one joiner and the CALLER writes the place's single row once, after every
        joiner has been absorbed (see `_absorb_joiners_into_group` step 5).

        Returns {'entry_ids': [...], 'absorbed_balance': Decimal,
                 'last_delivery_at': datetime|None, 'last_return_at': datetime|None}.
        """
        entry_ids = sorted(
            r[0]
            for r in db.session.query(BottleLedger.id)
            .filter(
                BottleLedger.address_id == address_id,
                BottleLedger.address_group_id.is_(None),
            )
            .all()
        )
        if entry_ids:
            db.session.query(BottleLedger).filter(BottleLedger.id.in_(entry_ids)).update(
                {BottleLedger.address_group_id: group_id}, synchronize_session=False
            )

        # The address's FROZEN FINE SCOPES move with its history, same selector.
        # Fines used to be frozen and NEVER re-stamped, while
        # `OrderEditService._cascade_bottle` RE-RESOLVED live — two policies that
        # cannot both be right. The ruling is FREEZE: a correction belongs to the
        # episode it corrects, not to today's geography. Freeze is only coherent
        # if the lifecycle CARRIES the frozen references, which is what this does.
        db.session.query(BottleFine).filter(
            BottleFine.address_id == address_id,
            BottleFine.address_group_id.is_(None),
        ).update({BottleFine.address_group_id: group_id}, synchronize_session=False)

        # `uq_bottle_balance_addr` makes this at most one row. FOR UPDATE, so
        # the figure read below and the row deleted after it are the same
        # version — see CONCURRENCY above.
        own_row = (
            BottleBalance.query.filter(
                BottleBalance.address_id == address_id,
                BottleBalance.address_group_id.is_(None),
            )
            .with_for_update()
            .first()
        )
        absorbed = Decimal("0.00")
        last_delivery_at = last_return_at = None
        if own_row is not None:
            absorbed = Decimal(str(own_row.balance or 0))
            # Read BEFORE the delete: after it the instance is expunged on flush
            # and these attributes are no longer loadable.
            last_delivery_at = own_row.last_delivery_at
            last_return_at = own_row.last_return_at
            db.session.delete(own_row)
        db.session.flush()
        return {
            "entry_ids": entry_ids,
            "absorbed_balance": absorbed,
            "last_delivery_at": last_delivery_at,
            "last_return_at": last_return_at,
        }

    # ------------------------------------------------------------------
    # Place lifecycle: dissolving a place onto its last member (spec §7.3)
    # ------------------------------------------------------------------

    @staticmethod
    def release_group_history_to_address(
        group_id: int, address_id: int, *, acting_admin_id: int, event_id: int, reason: str
    ) -> Dict[str, Any]:
        """Dissolve a place onto its last member (spec §7.3).

        ONLY the surviving address's own entries are re-stamped to its own
        scope. Entries attributed to members who left EARLIER keep
        `address_group_id = group_id`: NULLing those would place the place's
        history inside a departed address's own scope under the §3.1 predicate,
        and `reconcile_balance` would then mint bottles onto an address that
        left with nothing. The (now memberless) `AddressGroup` row is KEPT for
        the same reason — `bottle_ledger.address_group_id` is a foreign key and
        those entries still carry it. Nothing resolves to a memberless group, so
        the row is inert; it is the anchor of a history that has nowhere else to
        live. Step 6 stamps `dissolved_onto_address_id` on it, which is what
        turns that inert anchor into a FORWARDING POINTER: a departed member's
        frozen fine or delivery row can then be settled onto the scope that
        actually holds the crates instead of being refused for ever.

        CONSERVATION. The group's `bottle_balances` row is the authoritative
        figure for the place, and this method MOVES it: the part the re-stamped
        history explains is carried across directly, and the unexplained
        remainder crosses as ONE paired ADMIN_ADJUSTMENT summing to zero. The
        survivor's row therefore lands on exactly what the group's row held, and
        the group's row lands on 0 and is deleted — which is the orphan class
        §7.3 exists to close.

        Deliberately NOT `reconcile_balance`. Rebuilding either scope from its
        ledger sums would DESTROY the balance of any place whose row is not
        ledger-derived — a figure seeded before the ledger existed has a row and
        no entries (see `absorb_address_into_group`) — and repairing drift is
        `reconcile_balance`'s own job, not a membership edit's. This mirrors the
        carry discipline `CustomerLinkService._absorb_joiners_into_group` uses
        for the inverse operation.

        LOCK ORDERING (spec §5.2, revised). Deadlock-freedom here rests on
        ORDERING ALONE, never on a fence. Every transaction acquires a prefix of
        ONE total order:

            rung 0  `address_groups` row      FOR NO KEY UPDATE  (lifecycle)
            rung 1  `addresses` rows          FOR SHARE for a writer that only
                                              READS membership, FOR NO KEY UPDATE
                                              for the lifecycle; ascending id,
                                              ONE statement
            rung 2  `bottle_balances` GROUP row    FOR UPDATE
            rung 3  `bottle_balances` ADDRESS row  FOR UPDATE

        The whole ladder sits BELOW the payment/settlement locks
        (`staff_service.py`'s `lock_order_settlement_candidates`,
        `order_edit_service.py`'s cash-before-bottles rule).

        This method is entered holding rungs 0 and 1 already (its only caller is
        `_dissolve_if_last_member`, under `remove_address_from_group`'s locked
        member set), and takes rung 2 then rung 3 — the group's
        `bottle_balances` row FIRST, then the surviving ADDRESS's. It is taken
        via `get_or_create_balance`, so it is held even when the place never
        carried one; that removes the late-acquisition branch entirely. The
        transient row is deleted again at the end, so nothing is left behind.

        The three two-balance-row acquirers — this method,
        `CustomerLinkService._split_bottles_out_of_place` and
        `CustomerLinkService._absorb_joiners_into_group` — are all lifecycle
        operations on ONE place, and all three hold that place's
        `address_groups` row (rung 0) first. They are therefore MUTUALLY
        EXCLUSIVE. Every other bottle path — delivery, return, standalone
        collection, admin adjustment, fine, reconcile, order-edit cascade —
        takes exactly ONE balance row and so cannot be half of an ABBA cycle.

        WHAT WOULD FALSIFY THIS: a lock acquired out of rung order; a write to
        `bottle_balances`/`bottle_ledger` for an address whose row lock is not
        held; or a write to `addresses.address_group_id` by any path — script,
        data fix, admin tool, new route — that does not first hold the target
        `address_groups` row. The last is an invariant on the TABLE, not on this
        method.

        Returns {'inherited': Decimal, 'entry_ids': [...]}.
        """
        from business_app.models.customer_link import AddressGroup
        from business_app.models.user import UserAddress
        from business_app.services.bottle_scope import BottleScope

        scope_g, scope_a = BottleScope.for_group(group_id), BottleScope.for_address(address_id)
        service = BottleTrackingService()

        # 1. The GROUP row FOR UPDATE, before any address row — see LOCK ORDERING.
        #    `address_id` only satisfies the signature; the explicit scope wins.
        #    `allow_memberless` is MANDATORY here and nowhere else: the §7.3
        #    zero-remaining arm clears the departing address's pointer at
        #    `_dissolve_if_last_member`'s caller BEFORE calling in, so the group
        #    genuinely has no members at this instant and the reachability guard
        #    would 500 the dissolve.
        group_row = service.get_or_create_balance(address_id, scope=scope_g, allow_memberless=True)
        place_total = Decimal(str(group_row.balance or 0))

        # 2. Re-stamp ONLY the survivor's own entries out of the group. Ids and
        #    quantities come back in ONE query — a second SUM over ids already
        #    materialised here would be a pure extra round trip.
        rows = (
            db.session.query(BottleLedger.id, BottleLedger.quantity)
            .filter(
                BottleLedger.address_group_id == group_id,
                BottleLedger.address_id == address_id,
            )
            .all()
        )
        entry_ids = sorted(r[0] for r in rows)
        own_sum = sum((Decimal(str(r[1] or 0)) for r in rows), Decimal("0.00"))
        if entry_ids:
            db.session.query(BottleLedger).filter(BottleLedger.id.in_(entry_ids)).update(
                {BottleLedger.address_group_id: None}, synchronize_session=False
            )
        # The survivor's FROZEN FINE SCOPES follow its history out of the group
        # — the mirror of `absorb_address_into_group`'s re-stamp, and what makes
        # the FREEZE ruling coherent rather than a second inconsistency. Fines
        # attributed to members who left EARLIER keep the group, exactly like
        # their ledger rows.
        db.session.query(BottleFine).filter(
            BottleFine.address_group_id == group_id,
            BottleFine.address_id == address_id,
        ).update({BottleFine.address_group_id: None}, synchronize_session=False)
        # The bulk UPDATE ran with synchronize_session=False, so the identity map
        # still holds pre-move column values. Flush first: expiring with pending
        # changes would discard them.
        db.session.flush()
        db.session.expire_all()

        # 3. Carry the part of the place figure the re-stamped history explains.
        #    A MOVE, so the group is debited by exactly what the address is
        #    credited. The address row is taken SECOND (lock ordering above).
        if own_sum != 0:
            group_row.balance = (group_row.balance or Decimal("0.00")) - own_sum
            own_row = service.get_or_create_balance(address_id, scope=scope_a)
            own_row.balance = (own_row.balance or Decimal("0.00")) + own_sum
            db.session.flush()

        # 4. Whatever the survivor's own history cannot explain crosses as ONE
        #    paired move summing to zero, so the group's row lands on 0.
        inherited = place_total - own_sum
        if inherited != 0:
            owner_id = db.session.query(UserAddress.user_id).filter(UserAddress.id == address_id).scalar()
            shared = dict(
                user_id=owner_id,
                address_id=address_id,
                event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT,
                actor_user_id=acting_admin_id,
                notes="Place dissolved onto its last member",
                metadata={
                    "source": "place_dissolve",
                    "acting_admin_id": acting_admin_id,
                    "reason": reason,
                    "place_group_id": group_id,
                },
            )
            service._create_ledger_entry(
                quantity=-inherited,
                scope=scope_g,
                idempotency_key=f"place_dissolve:{group_id}:{event_id}:out",
                **shared,
            )
            service._create_ledger_entry(
                quantity=inherited,
                scope=scope_a,
                idempotency_key=f"place_dissolve:{group_id}:{event_id}:in",
                **shared,
            )

        # 4b. INHERIT THE PLACE'S PROVENANCE before its row is destroyed.
        #     `last_delivery_at` / `last_return_at` are the only two facts on the
        #     group's row that neither the carry above nor any later pass can
        #     rebuild — `recompute_balance_after` writes `balance_after`,
        #     `reconcile_balance` writes `balance`, and both ignore these columns
        #     — so the DELETE in step 5 is where a place served yesterday starts
        #     reading NEVER SERVED on the admin table and in the customer drawer.
        #     "Most recent wins" (`latest_timestamp`), because the survivor may
        #     carry a later own-scope date from before it ever joined.
        #
        #     NO ROW IS MINTED FOR A DATE ALONE — `get_balance_row`, not
        #     `get_or_create_balance`. Steps 3 and 4 already created the
        #     survivor's row for any place that carried a figure at all; a place
        #     that dissolves on exactly 0.00 with no history to re-stamp must not
        #     gain a row just for being dissolved, which is the same ruling
        #     `_absorb_joiners_into_group` step 5 makes on the way in. The row is
        #     already held FOR UPDATE by this transaction when it exists, so this
        #     acquires no new lock and cannot disturb the rung order.
        if group_row.last_delivery_at is not None or group_row.last_return_at is not None:
            own_row = BottleTrackingService.get_balance_row(scope_a)
            if own_row is not None:
                own_row.last_delivery_at = BottleTrackingService.latest_timestamp(
                    own_row.last_delivery_at, group_row.last_delivery_at
                )
                own_row.last_return_at = BottleTrackingService.latest_timestamp(
                    own_row.last_return_at, group_row.last_return_at
                )
                db.session.flush()

        # 5. Rebuild the running snapshots on BOTH split timelines, then drop the
        #    group's now-zero row: no address can resolve to it any more, and
        #    leaving it is exactly the `orphaned_place_balances` violation.
        BottleTrackingService.recompute_balance_after(scope_g)
        BottleTrackingService.recompute_balance_after(scope_a)
        db.session.query(BottleBalance).filter(BottleBalance.address_group_id == group_id).delete(
            synchronize_session=False
        )

        # 6. THE FORWARDING POINTER, written in the SAME transaction that
        #    destroys the group's balance row — the two facts are one fact.
        #    Every reference §7.1/§7.3 deliberately did NOT re-stamp (a DEPARTED
        #    member's fines and ledger rows) still names this group, and from
        #    here on there is no member address to resolve it through. This is
        #    the only record of where its history went that a WRITE PATH can
        #    resolve: the same survivor also lands in
        #    `CustomerLinkEvent.event_metadata`, but that is an audit blob keyed
        #    by episode, not by group.
        #
        #    A bulk UPDATE, not an ORM attribute set: the `expire_all()` below
        #    would discard a pending change (same reason as step 2), and the
        #    caller holds this row at RUNG 0 (`_lock_place_group`), so the write
        #    takes no new lock and cannot disturb the rung order.
        #
        #    Write-ONCE by construction — `add_addresses_to_group` refuses a
        #    memberless group as a join target (`PLACE_GROUP_DISSOLVED`), so a
        #    group dissolves exactly once and this can never be overwritten with
        #    a second, contradictory destination.
        db.session.query(AddressGroup).filter(AddressGroup.id == group_id).update(
            {AddressGroup.dissolved_onto_address_id: address_id}, synchronize_session=False
        )
        db.session.flush()
        db.session.expire_all()
        logger.info(
            "[PLACE] dissolved onto last member group=%s address=%s inherited=%s " "restamped=%s event=%s admin=%s",
            group_id,
            address_id,
            inherited,
            len(entry_ids),
            event_id,
            acting_admin_id,
        )
        return {"inherited": inherited, "entry_ids": entry_ids}

    # ------------------------------------------------------------------
    # Place lifecycle: reviewing a merge before committing it (spec §7.4)
    # ------------------------------------------------------------------

    # An unbounded preview would render every entry a busy place ever produced
    # into one JSON response. The route rejects above this rather than paging,
    # because a partially-shown ledger is a merge decided on partial evidence.
    MERGE_PREVIEW_MAX_ENTRIES = 500

    @staticmethod
    def build_merge_preview(
        address_ids: List[int],
        group_id: Optional[int] = None,
        excluded_ledger_entry_ids: Optional[List[int]] = None,
        *,
        strict_exclusions: bool = True,
    ) -> Dict[str, Any]:
        """The merged, chronological ledger an admin decides against (spec §7.4).

        The candidate set is each joining address's OWN-scope entries
        (`address_id = a AND address_group_id IS NULL` — the §7.2 selector, so a
        re-join cannot pull a former group's rows in) plus, when joining an
        EXISTING group, that group's entries.

        TWO FIGURES, NOT ONE. `computed_balance` is what the merged LEDGER sums
        to; `stored_balance` is what the joining places' `bottle_balances` rows
        actually HOLD. They are routinely different, and the difference is not
        a bug in this code: addresses were manually adjusted before grouping
        and those adjustments were carried, not re-derived (spec §7.2 —
        rebuilding from ledger sums would zero any place whose row was seeded
        before the ledger). `drift = stored_balance - computed_balance` is that
        gap, and repairing it is exactly what the merge review exists for, so
        the preview shows it rather than hiding it behind one number.

        `resulting_balance` stays §7.4's definition — `computed_balance -
        excluded_total` — so the spec's own arithmetic is still reported.
        `projected_place_balance` is what the place will actually HOLD, AND what
        its ledger will sum to, after committing what is previewed here:
        `stored_balance - excluded_total`, in one expression with no branch. A
        merge review first BACKFILLS the ledger onto the carried balance
        without moving that balance (`CustomerLinkService._apply_merge_review`),
        which lands both figures on `stored_balance`; the exclusions then move
        both equally. With a `resulting_balance` override the place holds
        exactly the stated number, on both figures. That is what makes a
        sequence of previews CONVERGE instead of chasing itself.

        The two agree whenever `drift` is 0, which is every place whose figure
        the ledger already explains — so §7.4's "the override is measured
        against the post-exclusion figure" is unchanged wherever it was ever
        meaningful. On a DRIFTED place the override is measured against
        `projected_place_balance`, because measuring it against a figure the
        place does not hold is exactly what made an admin stating 10 get 15.

        `balance_after` on the returned rows is the CURRENT stored value; the
        merged running total is attached as `preview_balance_after` so nothing
        on a live row is mutated by a read. Writing the merged total onto the
        real column would let a preview of a merge the admin then CANCELS
        rewrite the history of two places that never joined.

        Read-only, and deliberately takes no lock: it is a decision aid, and the
        figures it produces are re-derived under the join's own locks before
        anything is written (`CustomerLinkService._validate_merge_review`). The
        `preview_entry_ids` staleness guard is what closes the window between
        the two.

        A missing address or group RAISES (`NotFoundError` -> the admin route's
        404, spec §13's last line) rather than being skipped. Skipping it would
        return a confident preview of a DIFFERENT merge than the one asked for,
        and the `resulting_balance` override would then be measured against it.
        Validated here, not at the route, so the check cannot drift away from
        the query it protects and so the API layer keeps no model access.

        `strict_exclusions` likewise REJECTS an excluded id outside the
        candidate set (`MERGE_EXCLUSION_NOT_ELIGIBLE`) rather than ignoring it,
        so the decision aid and the committer cannot disagree about the same
        input. Only `_validate_merge_review` passes False, because it runs the
        same check itself in §7.4's fixed guard order (reason, staleness,
        eligibility, double-exclusion) and must not have it jump the queue.
        """
        from business_app.models.customer_link import AddressGroup

        wanted = [int(a) for a in (address_ids or [])]
        found = {r[0] for r in db.session.query(UserAddress.id).filter(UserAddress.id.in_(wanted)).all()}
        missing = [a for a in wanted if a not in found]
        if missing:
            raise NotFoundError(f"Address {missing[0]} not found")
        if group_id is not None and AddressGroup.query.get(group_id) is None:
            raise NotFoundError(f"Place group {group_id} not found")

        clauses = [(BottleLedger.address_id.in_(wanted)) & (BottleLedger.address_group_id.is_(None))]
        if group_id is not None:
            clauses.append(BottleLedger.address_group_id == group_id)
        entries = (
            BottleLedger.query.options(joinedload(BottleLedger.user))
            .filter(or_(*clauses))
            .order_by(BottleLedger.occurred_at.asc(), BottleLedger.id.asc())
            .all()
        )

        # What those same scopes actually HOLD today. Mirrors the ledger clauses
        # exactly so the two figures can never describe different sets.
        stored_clauses = [(BottleBalance.address_id.in_(wanted)) & (BottleBalance.address_group_id.is_(None))]
        if group_id is not None:
            stored_clauses.append(BottleBalance.address_group_id == group_id)
        stored = (
            db.session.query(func.coalesce(func.sum(BottleBalance.balance), Decimal("0.00")))
            .filter(or_(*stored_clauses))
            .scalar()
        )
        stored = Decimal(str(stored or 0))

        excluded = {int(i) for i in (excluded_ledger_entry_ids or [])}
        entry_ids = [e.id for e in entries]
        if strict_exclusions:
            stray = sorted(excluded - set(entry_ids))
            if stray:
                raise ValidationError(
                    f"Ledger entry {stray[0]} is not part of this merge",
                    error_code="MERGE_EXCLUSION_NOT_ELIGIBLE",
                )

        running = Decimal("0.00")
        computed = Decimal("0.00")
        excluded_total = Decimal("0.00")
        for entry in entries:
            quantity = Decimal(str(entry.quantity or 0))
            running += quantity
            entry.preview_balance_after = running  # transient attribute, not a column
            computed += quantity
            if entry.id in excluded:
                excluded_total += quantity
        resulting = computed - excluded_total
        return {
            "entries": entries,
            "entry_ids": entry_ids,
            "computed_balance": computed,
            "stored_balance": stored,
            "drift": stored - computed,
            "excluded_total": excluded_total,
            "resulting_balance": resulting,
            "projected_place_balance": stored - excluded_total,
        }

    @staticmethod
    def assert_scope_row_valid(balance: BottleBalance) -> None:
        """Exactly one scope key (spec §13, BOTTLE_SCOPE_INVALID).

        `ck_bottle_balance_scope` enforces this in the database — including on
        the SQLite test backend, which does honour CHECK constraints (it is
        FOREIGN KEYS that are off there). What the CHECK cannot give is an
        early, NAMED failure: it fires at FLUSH time as an opaque
        `IntegrityError` that every caller's bare `except` converts into
        "referenced by existing records". This is the in-process mirror, so a
        row that would violate the rule is rejected with its own error code
        before it reaches the database, and so the invariant still holds on any
        database that predates the constraint (the nightly
        `invalid_scope_balances` sweep covers those).
        """
        if balance is None:
            return
        if (balance.address_group_id is None) == (balance.address_id is None):
            raise ValidationError(
                f"Bottle balance {balance.id} must have exactly one scope key",
                error_code="BOTTLE_SCOPE_INVALID",
            )

    @staticmethod
    def recompute_balance_after(scope: "BottleScope") -> int:
        """Rewrite `balance_after` across a scope's whole timeline (spec §7.2 step 4).

        Ordered by (occurred_at, id). `occurred_at` alone is unstable — paired
        entries written inside one transaction share a timestamp (FINE_ISSUED
        quantity 0 beside FINE_PAID; the two place_leave halves) — which would
        make the result non-deterministic across reruns.

        `balance_after` is a derived snapshot, not a source of truth:
        `reconcile_balance` already recomputes balances from ledger sums.

        The join path deliberately does NOT call `reconcile_balance` — it
        CARRIES each joiner's balance across instead (spec §7.2), because
        rebuilding from ledger sums would zero any place whose row is not
        ledger-derived. One consequence is written down here so the next reader
        finds it: on such a place the summary figure (`get_place_balance`) and
        the last `balance_after` this method writes can legitimately disagree,
        because they have different sources. Nothing is destroyed either way,
        and `reconcile_balance` remains the deliberate way to reconcile them.
        """
        rows = (
            BottleLedger.query.filter(*scope.ledger_filter())
            .order_by(BottleLedger.occurred_at.asc(), BottleLedger.id.asc())
            .all()
        )
        running = Decimal("0.00")
        for row in rows:
            running += Decimal(str(row.quantity or 0))
            row.balance_after = running
        db.session.flush()
        return len(rows)

    # ------------------------------------------------------------------
    # Order bottle calculation
    # ------------------------------------------------------------------

    @staticmethod
    def trip_units_for_lines(lines: Iterable[Tuple[Optional["Product"], int]]) -> "TripUnits":
        """What `(product, quantity)` lines put on a driver's trip: THE one bottle sum.

        `bottles` adds `Product.returnable_bottles_for` (the item->bottle SSOT; a mixed order of 3x19L returnable and
        4x10L not totals 3, not 7: TG_000095_26). `units` counts the quantity of every counted one-way unit product
        (`is_trip_one_way_unit`). Quantities keep their sign, so an edit passes its deltas; zero entries are dropped.
        """
        bottles = Decimal("0.00")
        units: Dict[int, int] = {}
        for product, quantity in lines:
            if product is None:
                continue
            bottles += product.returnable_bottles_for(quantity)
            if product.is_trip_one_way_unit:
                units[product.id] = units.get(product.id, 0) + int(quantity)
        return TripUnits(bottles, {pid: n for pid, n in units.items() if n != 0})

    @staticmethod
    def trip_units_for_order(order: Order) -> "TripUnits":
        """What an order puts on the trip (reward lines count)."""
        items = order.order_items if hasattr(order, "order_items") else []
        return BottleTrackingService.trip_units_for_lines((item.product, item.quantity) for item in items)

    @staticmethod
    def counted_trip_products() -> List["Product"]:
        """Every product the setting counts on the trip, by id. The setting, not `is_active`, decides."""
        return Product.query.filter(Product.trip_one_way_unit_clause()).order_by(Product.id).all()

    @staticmethod
    def calculate_bottles_for_order(order: Order) -> Decimal:
        """Sum returnable bottles across all items in an order (the bottle half of `trip_units_for_order`)."""
        items = order.order_items if hasattr(order, "order_items") else []
        logger.debug(
            "[BOTTLE] calculate_bottles_for_order order=%s item_count=%s",
            order.id,
            len(items),
        )
        total = BottleTrackingService.trip_units_for_order(order).bottles
        logger.debug("[BOTTLE] calculate_bottles_for_order order=%s total=%s", order.id, total)
        return total

    # ------------------------------------------------------------------
    # Fine management (always manual)
    # ------------------------------------------------------------------

    @staticmethod
    def _fine_scope(fine: BottleFine) -> "BottleScope":
        """The scope FROZEN on the fine at issue, read without taking any lock.

        A pure accessor over the two columns the fine already carries, and it
        stays that way. `serialize_bottle_fine_row` calls it PER ROW to label the
        admin fines table, so this must not touch the ladder: routing it through
        `resolve_frozen_scope_for_write` would take rung-1 `FOR SHARE` on up to
        two `addresses` rows for every fine rendered, make a read-only list
        endpoint contend with the place lifecycle, and let it fail with
        `BOTTLE_SCOPE_BUSY` while an admin merges a place. A display label is
        never worth a row lock.

        THE WRITE PATHS DO NOT USE THIS. `waive_fine` and `mark_fine_paid` call
        `resolve_frozen_scope_for_write` directly, because a write against a
        frozen scope needs three things this cannot give: rung 1 held on every
        address it can touch, the forwarding arm for a place that has since
        DISSOLVED, and the `unreachable` flag that tells the caller to refuse.
        The split is deliberate — the reader wants the place the fine NAMES, the
        writer wants the place that holds the CRATES, and after a dissolve those
        are different.
        """
        from business_app.services.bottle_scope import BottleScope

        return (
            BottleScope.for_group(fine.address_group_id)
            if fine.address_group_id is not None
            else BottleScope.for_address(fine.address_id)
        )

    @transactional
    def issue_fine(
        self,
        user_id: Optional[int],
        address_id: int,
        quantity: Decimal,
        fine_amount: Decimal,
        actor_user_id: int,
        notes: str = None,
        idempotency_key: str = None,
    ) -> BottleFine:
        """Manually issue a fine for missing bottles at a place.

        `address_group_id` is frozen at issue time and is the scope EVERY one of
        this fine's ledger entries is written to, so a later ungrouping cannot
        split the FINE_ISSUED / FINE_PAID pair across two ledgers.

        `user_id=None` means "no member named"; the audit stamp is derived (see
        `resolve_place_attribution_user_id`). The derivation runs BEFORE the
        scope assertion so a derived member passes it by construction while an
        explicitly-named stranger is still rejected.

        `idempotency_key` is the driver's PER-INTENT retry token, minted once at
        the last state before this money-carrying POST. It is validated and
        namespaced server-side (`compose_client_idempotency_key`) and a dedup hit
        is compared against this request (`_assert_replay_matches_fine`) before
        it is honoured. Composed AFTER `_authorised_place_attribution` so a
        replay from a stranger is still refused rather than silently deduped.
        The admin route supplies none, so its fines keep a NULL key — and NULLs
        are DISTINCT under a plain UNIQUE on both engines.
        """
        # ONE locked resolution (rung 1), threaded through the derivation, the
        # authorisation and the write — see `_authorised_place_attribution`,
        # which this body's own sequence became when the other two admin write
        # paths were found to be missing it.
        scope, user_id = self._authorised_place_attribution(user_id, address_id)

        qty = self._as_decimal(quantity)
        amount = self._as_decimal(fine_amount)
        if qty <= 0:
            raise ValidationError("Fine quantity must be positive")
        if amount <= 0:
            raise ValidationError("Fine amount must be positive")

        # THE FENCE SITS HERE, above the BottleFine construction, and BELOW the
        # authorisation and validity guards. Above them, a replay from a stranger
        # or with a malformed amount would silently succeed; below the row
        # construction, the money would already be minted — the FINE_ISSUED
        # ledger entry carries quantity=0, so keying the LEDGER alone protects
        # nothing. `waive_fine` and `mark_fine_paid` are fenced by their status
        # check; `issue_fine` was fenced by nothing.
        stored_key = self.compose_client_idempotency_key("fine", actor_user_id, idempotency_key)
        if stored_key:
            existing = BottleFine.query.filter_by(idempotency_key=stored_key).first()
            if existing is not None:
                self._assert_replay_matches_fine(
                    existing,
                    user_id=user_id,
                    address_id=address_id,
                    quantity=qty,
                    fine_amount=amount,
                )
                logger.info("Duplicate fine issue skipped: %s", stored_key)
                return existing

        fine = BottleFine(
            user_id=user_id,
            address_id=address_id,
            address_group_id=scope.group_id,
            quantity=qty,
            fine_amount=amount,
            status=BottleFineStatus.PENDING,
            issued_by=actor_user_id,
            issued_at=self._utc_now(),
            notes=notes,
            idempotency_key=stored_key,
        )
        db.session.add(fine)
        try:
            db.session.flush()  # assign fine.id BEFORE the ledger metadata reads it
        except IntegrityError:
            # THE CHECK-THEN-INSERT RACE. Without this the loser is an unhandled
            # IntegrityError: `ExceptionMapper.EXCEPTION_MAPPING` has NO
            # SQLAlchemy entry, so it maps to HTTP 500 + a CRITICAL log — i.e.
            # the retry-safety fix would turn a duplicate into an outage.
            # Rollback-and-requery, never a SAVEPOINT: `begin_nested()` RELEASEs
            # into a COMMIT on pysqlite after SELECT-only work, which would split
            # this method in two and commit the money-carrying BottleFine while
            # its FINE_ISSUED ledger row ran in a separate transaction — exactly
            # the divergence this fence exists to prevent.
            if not stored_key:
                raise
            db.session.rollback()
            existing = BottleFine.query.filter_by(idempotency_key=stored_key).first()
            if existing is None:
                raise
            self._assert_replay_matches_fine(
                existing,
                user_id=user_id,
                address_id=address_id,
                quantity=qty,
                fine_amount=amount,
            )
            return existing

        # Shortage is evaluated against the PLACE (spec section 4.3), recorded so
        # the decision stays auditable after the balance moves on.
        place_balance_at_issue = float(self.get_place_balance(address_id))

        self._create_ledger_entry(
            user_id=user_id,
            address_id=address_id,
            event_type=BottleLedgerEventType.FINE_ISSUED,
            quantity=Decimal("0"),
            actor_user_id=actor_user_id,
            scope=scope,
            notes=f"Fine issued: {qty} bottles, {amount} UZS" + (f" — {notes}" if notes else ""),
            metadata={
                "fine_id": fine.id,
                "fine_quantity": float(qty),
                "fine_amount": float(amount),
                "place_balance_at_issue": place_balance_at_issue,
            },
        )

        db.session.flush()
        return fine

    @transactional
    def waive_fine(self, fine_id: int, actor_user_id: int, notes: str = None) -> BottleFine:
        """Waive an existing fine."""
        fine = BottleFine.query.get(fine_id)
        if not fine:
            raise NotFoundError("Fine not found")
        if fine.status in (BottleFineStatus.PAID, BottleFineStatus.WAIVED):
            raise ConflictError(f"Fine is already {fine.status.value}")

        # RUNG 1 before any bottle write, even though the scope written is the
        # one FROZEN at issue: the ladder's fence is on the `addresses` row this
        # entry is attributed to, not on the scope it lands in. Taken by
        # `resolve_frozen_scope_for_write`, which is also the ONLY place that may
        # take it — the forwarding arm needs the fine's address and the dissolved
        # place's survivor locked in ONE ascending statement, and a separate
        # acquisition here would break that ordering.
        #
        # NOT `_fine_scope`: that is the lock-free DISPLAY reader the serializer
        # uses, and it answers a different question (the place the fine names,
        # not the place that holds the crates).
        target = self.resolve_frozen_scope_for_write(fine.address_id, fine.address_group_id)

        fine.status = BottleFineStatus.WAIVED
        fine.waived_at = self._utc_now()
        fine.waived_by = actor_user_id
        if notes:
            fine.notes = (fine.notes or "") + f"\nWaived: {notes}"

        # Record in ledger, in the scope frozen at issue — or, when that place
        # has DISSOLVED, in the scope its history was released onto. An
        # unreachable frozen scope reaches `assert_reachable` unchanged and is
        # refused there by name.
        self._create_ledger_entry(
            user_id=fine.user_id,
            address_id=target.address_id,
            event_type=BottleLedgerEventType.FINE_REVERSED,
            quantity=Decimal("0"),
            actor_user_id=actor_user_id,
            scope=target.scope,
            notes=f"Fine #{fine.id} waived" + (f" — {notes}" if notes else ""),
            metadata={"fine_id": fine.id, **target.audit()},
        )

        db.session.flush()
        return fine

    @transactional
    def mark_fine_paid(self, fine_id: int, actor_user_id: int, notes: str = None) -> BottleFine:
        """Mark a fine as paid and reduce the customer's bottle balance by the fine quantity."""
        fine = BottleFine.query.get(fine_id)
        if not fine:
            raise NotFoundError("Fine not found")
        if fine.status in (BottleFineStatus.PAID, BottleFineStatus.WAIVED):
            raise ConflictError(f"Fine is already {fine.status.value}")

        # RUNG 1 — see `waive_fine`.
        target = self.resolve_frozen_scope_for_write(fine.address_id, fine.address_group_id)

        fine.status = BottleFineStatus.PAID
        fine.paid_at = self._utc_now()
        if notes:
            fine.notes = (fine.notes or "") + f"\nPaid: {notes}"

        # Reduce the place balance by the fine quantity: the customer has settled
        # the monetary debt, so the bottles are accounted for. When the place has
        # DISSOLVED, "the place" is the scope its history was released onto —
        # which is where those bottles physically are.
        self._create_ledger_entry(
            user_id=fine.user_id,
            address_id=target.address_id,
            event_type=BottleLedgerEventType.FINE_PAID,
            quantity=-self._as_decimal(fine.quantity),
            actor_user_id=actor_user_id,
            scope=target.scope,
            idempotency_key=f"fine_paid:{fine.id}",
            notes=f"Fine #{fine.id} paid" + (f" — {notes}" if notes else ""),
            metadata={"fine_id": fine.id, **target.audit()},
        )

        db.session.flush()
        return fine

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    @staticmethod
    def get_place_balance(address_id: int) -> Decimal:
        """The bottle balance of the PHYSICAL PLACE this address belongs to.

        One number, one row — there is no union to compute and no per-person
        slice to reconcile against it. Returns 0 when the place has no row yet.
        """
        scope = BottleTrackingService.resolve_scope(address_id)
        total = (
            db.session.query(func.coalesce(func.sum(BottleBalance.balance), Decimal("0.00")))
            .filter(*scope.balance_filter())
            .scalar()
        )
        return Decimal(str(total or 0))

    @staticmethod
    def get_place_balances_by_group(group_ids: List[int]) -> Dict[int, Decimal]:
        """The bottle balance of MANY GROUPED places, in ONE query.

        The batch form of `get_place_balance` for callers that render a whole
        page of places at once (the admin "Grouped Addresses" tab). Calling the
        singular reader per row would re-resolve a scope and issue a SELECT per
        rendered group — the same N+1 the COD figures beside it are deliberately
        written as one grouped query to avoid.

        Selects on exactly `BottleScope.for_group(id).balance_filter()`'s
        grouped arm (`address_group_id == id`), so a group's entry here and its
        `get_place_balance` are the same number by construction — keep the two
        in step if that filter ever changes. `func.sum` mirrors the singular
        reader rather than leaning on the scope unique.

        A place that has never moved a bottle is ABSENT from the mapping rather
        than present as 0: reading a balance must not mint a row (see
        `get_balance_row`), and callers default the miss themselves.
        """
        ids = [int(group_id) for group_id in (group_ids or []) if group_id is not None]
        if not ids:
            return {}
        rows = (
            db.session.query(
                BottleBalance.address_group_id,
                func.coalesce(func.sum(BottleBalance.balance), Decimal("0.00")),
            )
            .filter(BottleBalance.address_group_id.in_(ids))
            .group_by(BottleBalance.address_group_id)
            .all()
        )
        return {int(group_id): Decimal(str(total or 0)) for group_id, total in rows}

    @staticmethod
    def suggested_bottles_leaving(group_id: int, address_id: int, place_balance: Decimal = None) -> Decimal:
        """Spec §7.1's pre-fill: this address's OWN attributed entries at the place.

        `bottles_leaving` defaults to 0, which for a member who genuinely holds
        empties is data loss by default — so the remove dialog shows a derived
        suggestion the admin can accept or override. Clamped to
        [0, place balance]: the address's own sum can exceed what the place
        actually holds (a coworker over-returned) and can itself be negative.

        The clamp is what keeps the suggestion and
        `CustomerLinkService._validated_bottles_leaving` in agreement — an
        unclamped prefill would be a value the dialog's own OK button rejects.

        `place_balance` is an optional hoist for callers computing this for
        EVERY member of one group: all members resolve to the same place, so
        re-reading (and re-resolving the scope for) that one number per member
        is pure N+1. Omit it and it is read here.

        PLACE-LEVEL CORRECTIONS ARE EXCLUDED (spec §7.4). `merge_correction` and
        `merge_backfill` entries belong to the PLACE, not to a member: they carry a
        member's `(user_id, address_id)` only because `bottle_ledger` requires
        both NOT NULL. Counting them here would inflate exactly one coworker's
        departure pre-fill by the whole place-level correction, and an admin
        accepting the default would split the place's bottles onto the wrong
        person — a real quantity error, not a display one.

        `merge_exclude` entries are DELIBERATELY still counted. A reversal is
        attributed to the very entry it neutralises, so it cancels that
        address's own contribution; dropping it would leave the excluded
        quantity in the pre-fill, which is the same bug in the other direction.
        `coalesce` is load-bearing: `NULL NOT LIKE ...` is NULL, so an
        unqualified `notlike` would silently drop every entry without an
        idempotency key — which is most of them.
        """
        idempotency_key = func.coalesce(BottleLedger.idempotency_key, "")
        own_sum = (
            db.session.query(func.coalesce(func.sum(BottleLedger.quantity), Decimal("0.00")))
            .filter(
                BottleLedger.address_group_id == group_id,
                BottleLedger.address_id == address_id,
                *[idempotency_key.notlike(f"{prefix}%") for prefix in PLACE_LEVEL_LEDGER_KEY_PREFIXES],
            )
            .scalar()
        )
        place = place_balance if place_balance is not None else BottleTrackingService.get_place_balance(address_id)
        return max(Decimal("0.00"), min(Decimal(str(own_sum or 0)), place))

    @staticmethod
    def get_place_balance_row(address_id: int) -> Optional[BottleBalance]:
        """The place's balance row, or None when it has never moved a bottle."""
        scope = BottleTrackingService.resolve_scope(address_id)
        return BottleBalance.query.filter(*scope.balance_filter()).first()

    @staticmethod
    def get_customer_scopes(user_id: int) -> List[BottleBalance]:
        """The DISTINCT places this customer's addresses belong to.

        Deduplication is mandatory, not cosmetic: one person with two addresses
        at the same place would otherwise be counted twice. The one-customer form
        of `get_customer_scopes_by_user`, which holds the rule.
        """
        return BottleTrackingService.get_customer_scopes_by_user([user_id]).get(user_id, [])

    @staticmethod
    def get_customer_scopes_by_user(user_ids: Iterable[int]) -> Dict[int, List[BottleBalance]]:
        """`{user_id: the DISTINCT places its addresses belong to}`, in two queries for any number
        of customers (a page of worklist rows). A user with no place balance is absent.

        A place is the address group when the address is grouped, else the address itself. Each
        user's list holds a place once, however many of its addresses sit there, highest balance
        first.
        """
        ids = sorted({int(user_id) for user_id in user_ids})
        if not ids:
            return {}
        addresses = (
            db.session.query(UserAddress.user_id, UserAddress.id, UserAddress.address_group_id)
            .filter(UserAddress.user_id.in_(ids))
            .all()
        )
        users_by_group: Dict[int, set] = {}
        user_by_solo: Dict[int, int] = {}
        for owner_id, address_id, group_id in addresses:
            if group_id is not None:
                users_by_group.setdefault(group_id, set()).add(owner_id)
            else:
                user_by_solo[address_id] = owner_id
        if not users_by_group and not user_by_solo:
            return {}

        clauses = []
        if users_by_group:
            clauses.append(BottleBalance.address_group_id.in_(sorted(users_by_group)))
        if user_by_solo:
            clauses.append(
                (BottleBalance.address_id.in_(sorted(user_by_solo))) & (BottleBalance.address_group_id.is_(None))
            )
        balances = (
            BottleBalance.query.filter(or_(*clauses))
            .options(joinedload(BottleBalance.address))
            .order_by(BottleBalance.balance.desc())
            .all()
        )
        scopes: Dict[int, List[BottleBalance]] = {}
        for balance in balances:
            if balance.address_group_id is not None:
                owners = users_by_group.get(balance.address_group_id, ())
            else:
                owners = (user_by_solo[balance.address_id],) if balance.address_id in user_by_solo else ()
            for owner_id in sorted(owners):
                scopes.setdefault(owner_id, []).append(balance)
        return scopes

    @staticmethod
    def get_customer_place_rows(user_id: int) -> List[Dict]:
        """The customer's DISTINCT places, each paired with an address they own.

        One row per PLACE, not per (user, address) pair: two coworkers at one
        office share ONE pool, and one customer owning two addresses at the same
        place must not see it twice. Each row still carries an `address_id` the
        customer owns, because the driver posts collections and fines against an
        address and the write path resolves the place from it.

        There is deliberately no per-person balance and no `bottle_balance_id`:
        the pool has no per-member slice (spec decision 4), and fines are keyed
        by address now.
        """
        from business_app.models.user import UserAddress

        # A grouped place row has no address_id of its own, so map each place
        # back to an address this customer owns in it (lowest id wins, stably).
        own_group_addresses = {}
        own_solo_addresses = {}
        for addr in UserAddress.query.filter(UserAddress.user_id == user_id).order_by(UserAddress.id.asc()).all():
            if addr.address_group_id is not None:
                own_group_addresses.setdefault(addr.address_group_id, addr)
            else:
                own_solo_addresses[addr.id] = addr

        rows = []
        for place in BottleTrackingService.get_customer_scopes(user_id):
            if place.address_group_id is not None:
                addr = own_group_addresses.get(place.address_group_id)
            else:
                addr = own_solo_addresses.get(place.address_id)
            if addr is None:
                continue
            rows.append(
                {
                    "address_id": addr.id,
                    "address_title": addr.title,
                    "full_address": addr.full_address,
                    "is_grouped": place.address_group_id is not None,
                    "place_group_id": place.address_group_id,
                    # The empties physically at this place, whoever owns them.
                    # `place` IS the addr's scope row (unique per address_group_id /
                    # address_id — see bottle_balances' uq_bottle_balance_group /
                    # uq_bottle_balance_addr), already loaded by get_customer_scopes:
                    # use it directly instead of re-resolving scope + re-summing.
                    "place_balance": float(place.balance or 0),
                }
            )
        return rows

    def get_customer_summary(self, user_id: int) -> Dict:
        """Aggregate bottle stats for a customer, keyed by PLACE.

        There is deliberately no scalar total: a shared place's balance belongs
        to the place, so summing it per member would report the same bottles
        once per coworker. `cluster_scopes` lists each distinct place instead.

        Do not sum `place_balance` across `addresses` rows — a user with two
        addresses at one place appears twice; use `cluster_scopes` for totals.
        """
        from business_app.models.user import UserAddress
        from business_app.services.customer_link_service import CustomerLinkService

        active_fines = BottleFine.query.filter(
            BottleFine.user_id == user_id,
            BottleFine.status.in_([BottleFineStatus.PENDING, BottleFineStatus.INVOICED]),
        ).count()
        total_fine_amount = (
            db.session.query(func.coalesce(func.sum(BottleFine.fine_amount), 0))
            .filter(
                BottleFine.user_id == user_id,
                BottleFine.status.in_([BottleFineStatus.PENDING, BottleFineStatus.INVOICED]),
            )
            .scalar()
        )

        cluster_ids = CustomerLinkService().get_cluster_user_ids(user_id)
        own_addresses = UserAddress.query.filter(UserAddress.user_id == user_id).all()

        addresses = []
        for addr in own_addresses:
            scope = self.resolve_scope(addr.id)
            row = self.get_place_balance_row(addr.id)
            addresses.append(
                {
                    "address_id": addr.id,
                    "address_title": addr.title,
                    "full_address": addr.full_address,
                    "place_balance": float(row.balance if row else 0),
                    "last_delivery_at": row.last_delivery_at.isoformat() if row and row.last_delivery_at else None,
                    "last_return_at": row.last_return_at.isoformat() if row and row.last_return_at else None,
                    "address_group_id": scope.group_id,
                    "is_grouped": scope.is_grouped,
                }
            )

        cluster_scopes = [
            {
                "address_group_id": b.address_group_id,
                "address_id": b.address_id,
                "balance": float(b.balance or 0),
                "is_shared": b.address_group_id is not None,
            }
            for b in self.get_customer_scopes(user_id)
        ]

        return {
            "user_id": user_id,
            "addresses": addresses,
            "active_fines_count": active_fines,
            "total_fine_amount": float(total_fine_amount or 0),
            "is_linked": len(cluster_ids) > 1,
            "cluster_member_ids": sorted(cluster_ids),
            "cluster_scopes": cluster_scopes,
        }

    def get_customer_bottle_overview(self, user_id: int) -> Dict:
        """Customer-facing /bottles payload, keyed by PLACE (spec section 6).

        Iterates ADDRESSES, not balance rows: membership of a place must not
        depend on having personally taken a delivery, or a coworker who never
        received one at their own door sees an empty screen while the driver at
        that door is offered the place total.

        `place_members` carries NAMES ONLY — decision 4 removes the per-person
        balance everywhere, while keeping the ledger fully attributed.
        """
        from business_app.models.user import UserAddress
        from business_app.services.customer_link_service import CustomerLinkService

        link = CustomerLinkService()
        cluster_ids = link.get_cluster_user_ids(user_id)
        addresses = (
            UserAddress.query.filter(UserAddress.user_id.in_(cluster_ids)).options(joinedload(UserAddress.user)).all()
        )
        # The viewer's own address must win the scope-dedup race: otherwise a
        # linked sibling's address can become the representative row and the
        # caller sees `is_own: False` on a place they own an address at, which
        # also mis-sorts it into the siblings tier below.
        addresses.sort(key=lambda a: a.user_id != user_id)

        def _name(user) -> Optional[str]:
            if not user:
                return None
            return f"{user.first_name or ''} {user.last_name or ''}".strip() or None

        rows = []
        seen_scopes = set()
        for addr in addresses:
            scope = self.resolve_scope(addr.id)
            key = ("g", scope.group_id) if scope.is_grouped else ("a", scope.address_id)
            if key in seen_scopes:
                continue
            seen_scopes.add(key)

            balance_row = self.get_place_balance_row(addr.id)
            place_members = []
            if scope.is_grouped:
                member_address_ids = link.get_address_group_member_ids(addr.id)
                owner_ids = {
                    r[0]
                    for r in db.session.query(UserAddress.user_id).filter(UserAddress.id.in_(member_address_ids)).all()
                }
                place_members = [
                    {"member_name": _name(u), "is_own": u.id == user_id}
                    for u in User.query.filter(User.id.in_(owner_ids)).all()
                ]

            rows.append(
                {
                    "address_id": addr.id,
                    "address_title": addr.title,
                    "full_address": addr.full_address,
                    "owner_user_id": addr.user_id,
                    "owner_name": _name(addr.user),
                    "is_own": addr.user_id == user_id,
                    "is_grouped": scope.is_grouped,
                    "place_group_id": scope.group_id,
                    "place_balance": float(balance_row.balance if balance_row else 0),
                    "place_members": place_members,
                }
            )
        rows.sort(key=lambda r: (not r["is_own"], -r["place_balance"]))
        return {"is_linked": len(cluster_ids) > 1, "balances": rows}

    @staticmethod
    def get_place_ledger(address_id: int, page: int = 1, per_page: int = 20) -> Dict:
        """Paginated ledger for the PHYSICAL PLACE this address belongs to —
        every member's deliveries and returns, in one chronological sequence
        (spec section 6). Returns ORM rows; customer serialization goes through
        serialize_customer_place_ledger_entry (redacted)."""
        scope = BottleTrackingService.resolve_scope(address_id)
        query = (
            BottleLedger.query.options(
                joinedload(BottleLedger.user),
                joinedload(BottleLedger.address),
                joinedload(BottleLedger.order),
            )
            .filter(*scope.ledger_filter())
            .order_by(BottleLedger.occurred_at.desc(), BottleLedger.id.desc())
        )
        total = query.count()
        items = query.offset((page - 1) * per_page).limit(per_page).all()
        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if per_page else 0,
        }

    @staticmethod
    def get_cluster_ledger(user_id: int, page: int = 1, per_page: int = 20) -> Dict:
        """Paginated ledger across the DISTINCT places of every address owned by
        this user's linked cluster (spec section 6).

        Scoped by place, not by `BottleLedger.user_id` — at a shared place a
        coworker's entries are part of the same pool, and one person owning two
        addresses in one place must not see them twice. Rows span scopes, so
        `balance_after` is NOT monotonic here; Plan D suppresses that column in
        this view. Unlinked users resolve to their own singleton cluster.
        """
        from business_app.models.user import UserAddress
        from business_app.services.customer_link_service import CustomerLinkService

        cluster_ids = CustomerLinkService().get_cluster_user_ids(user_id)
        addresses = (
            db.session.query(UserAddress.id, UserAddress.address_group_id)
            .filter(UserAddress.user_id.in_(cluster_ids))
            .all()
        )
        group_ids = {g for _, g in addresses if g is not None}
        solo_ids = {a for a, g in addresses if g is None}
        if not group_ids and not solo_ids:
            return {"items": [], "total": 0, "page": page, "per_page": per_page, "pages": 0}

        clauses = []
        if group_ids:
            clauses.append(BottleLedger.address_group_id.in_(group_ids))
        if solo_ids:
            clauses.append((BottleLedger.address_id.in_(solo_ids)) & (BottleLedger.address_group_id.is_(None)))
        query = (
            BottleLedger.query.options(
                joinedload(BottleLedger.user),
                joinedload(BottleLedger.address),
                joinedload(BottleLedger.actor_user),
            )
            .filter(or_(*clauses))
            .order_by(BottleLedger.occurred_at.desc(), BottleLedger.id.desc())
        )
        total = query.count()
        items = query.offset((page - 1) * per_page).limit(per_page).all()
        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if per_page else 0,
        }

    @staticmethod
    def get_order_bottle_summary(order: Order) -> Dict[str, Any]:
        """Read-only SSOT for the delivered bottle summary (design §3.1).

        Returns Decimals for exact rendering by the webhook payload builder:
          - expected_bottles: what the order's items imply (readiness-guard input only)
          - delivery_recorded: whether the DELIVERY ledger row (idempotency key
            delivery:{order_id}) exists yet — the guard keys on this, not on a zero qty
          - bottles_delivered: quantity from that DELIVERY row; 0 if absent
          - bottles_collected: abs value of the RETURN_ON_DELIVERY row for the order; 0 if absent
          - balance: the PLACE balance (the address group when the delivery
            address is grouped, else the address itself); 0 if no row
          - place_balance_before_delivery: the place balance just before the
            DELIVERY row (balance_after - quantity); None without that row
          - delivery_place_group_id: the DELIVERY row's address_group_id; None
            without that row or when the address was ungrouped. Both are exact
            only until the place is regrouped (they read the stored stamp).
        """
        expected_bottles = BottleTrackingService.calculate_bottles_for_order(order)

        delivery_row = BottleLedger.query.filter_by(idempotency_key=f"delivery:{order.id}").first()
        delivery_recorded = delivery_row is not None
        bottles_delivered = Decimal(str(delivery_row.quantity)) if delivery_row is not None else Decimal("0")
        place_balance_before_delivery = (
            Decimal(str(delivery_row.balance_after)) - Decimal(str(delivery_row.quantity))
            if delivery_row is not None
            else None
        )
        delivery_place_group_id = delivery_row.address_group_id if delivery_row is not None else None

        return_row = (
            BottleLedger.query.filter_by(
                order_id=order.id,
                event_type=BottleLedgerEventType.RETURN_ON_DELIVERY,
            )
            .order_by(BottleLedger.occurred_at.desc())
            .first()
        )
        bottles_collected = abs(Decimal(str(return_row.quantity))) if return_row is not None else Decimal("0")

        balance = Decimal("0")
        if order.delivery_address_id is not None:
            # PLACE balance so a delivery reached via a second phone still reflects the
            # physical empties at that place. Ungrouped == single pair (spec section 3).
            balance = BottleTrackingService.get_place_balance(order.delivery_address_id)

        return {
            "expected_bottles": expected_bottles,
            "delivery_recorded": delivery_recorded,
            "bottles_delivered": bottles_delivered,
            "bottles_collected": bottles_collected,
            "balance": balance,
            "place_balance_before_delivery": place_balance_before_delivery,
            "delivery_place_group_id": delivery_place_group_id,
        }

    @staticmethod
    def get_all_balances(
        page: int = 1,
        per_page: int = 20,
        min_balance: float = None,
        user_id: int = None,
        search: str = None,
    ) -> Dict:
        """Get paginated list of all PLACE balances with optional filters.

        `user_id` and `search` are membership filters now, not owner filters:
        a place row has no owner, so they select the places a person's (or a
        matching person's) addresses belong to.
        """
        query = BottleBalance.query.options(
            joinedload(BottleBalance.address_group),
            joinedload(BottleBalance.address),
        )
        if min_balance is not None:
            query = query.filter(BottleBalance.balance >= Decimal(str(min_balance)))
        if user_id:
            from business_app.models.user import UserAddress

            addresses = (
                db.session.query(UserAddress.id, UserAddress.address_group_id)
                .filter(UserAddress.user_id == user_id)
                .all()
            )
            group_ids = {g for _, g in addresses if g is not None}
            solo_ids = {a for a, g in addresses if g is None}
            clauses = []
            if group_ids:
                clauses.append(BottleBalance.address_group_id.in_(group_ids))
            if solo_ids:
                clauses.append((BottleBalance.address_id.in_(solo_ids)) & (BottleBalance.address_group_id.is_(None)))
            query = query.filter(or_(*clauses)) if clauses else query.filter(sa_false())
        if search:
            # A place row has no single owner, so a name/phone match on ANY member
            # selects the place. `addresses` has one FK to users, so the join's ON
            # clause is pinned explicitly (never a bare join(User)).
            from business_app.models.user import UserAddress

            matched = (
                db.session.query(UserAddress.id, UserAddress.address_group_id)
                .join(User, UserAddress.user_id == User.id)
                .filter(
                    or_(
                        User.first_name.ilike(f"%{search}%"),
                        User.last_name.ilike(f"%{search}%"),
                        User.phone.ilike(f"%{search}%"),
                    )
                )
                .all()
            )
            group_ids = {g for _, g in matched if g is not None}
            solo_ids = {a for a, g in matched if g is None}
            clauses = []
            if group_ids:
                clauses.append(BottleBalance.address_group_id.in_(group_ids))
            if solo_ids:
                clauses.append((BottleBalance.address_id.in_(solo_ids)) & (BottleBalance.address_group_id.is_(None)))
            query = query.filter(or_(*clauses)) if clauses else query.filter(sa_false())

        query = query.order_by(BottleBalance.balance.desc())
        total = query.count()
        items = query.offset((page - 1) * per_page).limit(per_page).all()
        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if per_page else 0,
        }

    @staticmethod
    def get_all_fines(
        page: int = 1,
        per_page: int = 20,
        status: str = None,
        user_id: int = None,
    ) -> Dict:
        """Get paginated list of fines."""
        query = BottleFine.query.options(
            joinedload(BottleFine.user),
            joinedload(BottleFine.address),
            joinedload(BottleFine.issuer),
        )
        if status:
            query = query.filter(BottleFine.status == BottleFineStatus(status))
        if user_id:
            query = query.filter(BottleFine.user_id == user_id)

        query = query.order_by(BottleFine.issued_at.desc())
        total = query.count()
        items = query.offset((page - 1) * per_page).limit(per_page).all()
        from business_app.serializers.bottle_serializers import serialize_bottle_fine_row

        return {
            "items": [serialize_bottle_fine_row(f) for f in items],
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if per_page else 0,
        }

    @staticmethod
    def get_all_ledger_entries(
        page: int = 1,
        per_page: int = 20,
        user_id: int = None,
        address_id: int = None,
        event_type: str = None,
    ) -> Dict:
        """Get paginated ledger entries with optional filters.

        ``address_id`` is a PLACE filter — it resolves to the address's place
        and returns every member's movements there (and raises NotFoundError
        for an unknown address, as `resolve_scope` does). ``user_id`` stays an
        ATTRIBUTION filter: "what did this person move", which is exactly the
        question the admin ledger screen's person filter asks.

        Returns ORM objects in ``items`` so callers can use serializer
        functions that access relationships (actor_user, address, etc.).
        """
        query = BottleLedger.query.options(
            joinedload(BottleLedger.user),
            joinedload(BottleLedger.address),
            joinedload(BottleLedger.actor_user),
        ).order_by(BottleLedger.occurred_at.desc())
        if user_id:
            query = query.filter(BottleLedger.user_id == user_id)
        if address_id:
            scope = BottleTrackingService.resolve_scope(address_id)
            query = query.filter(*scope.ledger_filter())
        if event_type:
            query = query.filter(BottleLedger.event_type == BottleLedgerEventType(event_type))

        total = query.count()
        items = query.offset((page - 1) * per_page).limit(per_page).all()
        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if per_page else 0,
        }

    # ------------------------------------------------------------------
    # Dashboard stats
    # ------------------------------------------------------------------

    @staticmethod
    def _scope_label(balance: BottleBalance) -> str:
        """Human-readable identity for a place row. Never null.

        AddressGroup.label is nullable, so fall back to the member addresses'
        titles and finally to the id — the admin dashboard and serializer both
        need something to render.
        """
        from business_app.models.user import UserAddress

        if balance.address_group_id is not None:
            label = balance.address_group.label if balance.address_group else None
            if label:
                return label
            titles = [
                t
                for (t,) in db.session.query(UserAddress.title)
                .filter(UserAddress.address_group_id == balance.address_group_id)
                .all()
                if t
            ]
            return ", ".join(sorted(set(titles))) or f"Place #{balance.address_group_id}"
        if balance.address is not None and balance.address.title:
            return balance.address.title
        return f"Address #{balance.address_id}"

    @staticmethod
    def _scope_member_address_ids(balance: BottleBalance) -> List[int]:
        """Every address belonging to this balance's place, lowest id first.

        A grouped balance row has `address_id IS NULL` (ck_bottle_balance_scope),
        so admin surfaces have no id to send to the address-keyed routes. Any
        member id works — `resolve_scope` expands it back to the group.

        Delegates to `_place_member_address_ids` so the id this publishes as
        `representative_address_id` and the member the service derives an audit
        attribution from are ordered by ONE rule.
        """
        if balance.address_group_id is None and balance.address_id is None:
            return []
        scope = (
            BottleScope.for_group(balance.address_group_id)
            if balance.address_group_id is not None
            else BottleScope.for_address(balance.address_id)
        )
        return BottleTrackingService._place_member_address_ids(scope)

    @staticmethod
    def _scope_member_names(balance: BottleBalance) -> List[str]:
        """Names of every customer whose address belongs to this place."""
        from business_app.models.user import UserAddress

        if balance.address_group_id is not None:
            owner_ids = {
                r[0]
                for r in db.session.query(UserAddress.user_id)
                .filter(UserAddress.address_group_id == balance.address_group_id)
                .all()
            }
        elif balance.address is not None:
            owner_ids = {balance.address.user_id}
        else:
            owner_ids = set()
        if not owner_ids:
            return []
        return sorted(
            n
            for n in (
                f"{u.first_name or ''} {u.last_name or ''}".strip()
                for u in User.query.filter(User.id.in_(owner_ids)).all()
            )
            if n
        )

    @staticmethod
    def get_dashboard_stats() -> Dict:
        """Aggregate stats for the admin bottle tracking dashboard, keyed by PLACE.

        `places_with_balance` replaces the old `customers_with_balance`, and
        `top_debtors` are places rather than people: two coworkers sharing one
        office are ONE debtor holding one pool, not a 6/1 split (spec section 3).
        """
        total_bottles_out = (
            db.session.query(func.coalesce(func.sum(BottleBalance.balance), 0))
            .filter(BottleBalance.balance > 0)
            .scalar()
        )

        places_with_balance = BottleBalance.query.filter(BottleBalance.balance > 0).count()

        active_fines = BottleFine.query.filter(
            BottleFine.status.in_([BottleFineStatus.PENDING, BottleFineStatus.INVOICED])
        ).count()

        total_fine_amount = (
            db.session.query(func.coalesce(func.sum(BottleFine.fine_amount), 0))
            .filter(BottleFine.status.in_([BottleFineStatus.PENDING, BottleFineStatus.INVOICED]))
            .scalar()
        )

        # Top debtors — one row per PLACE, already the aggregate (no GROUP BY).
        top_places = (
            BottleBalance.query.filter(BottleBalance.balance > 0).order_by(BottleBalance.balance.desc()).limit(10).all()
        )
        top_debtor_details = [
            {
                "address_group_id": row.address_group_id,
                "address_id": row.address_id,
                "name": BottleTrackingService._scope_label(row),
                "total_balance": float(row.balance or 0),
            }
            for row in top_places
        ]

        return {
            "total_bottles_out": float(total_bottles_out or 0),
            "places_with_balance": places_with_balance,
            "active_fines": active_fines,
            "total_fine_amount": float(total_fine_amount or 0),
            "top_debtors": top_debtor_details,
        }

    # ------------------------------------------------------------------
    # Balance reconciliation
    # ------------------------------------------------------------------

    @transactional
    def reconcile_balance(self, address_id: int) -> Dict:
        """Recalculate a PLACE's balance from its ledger and report the discrepancy.

        EVERY READ IS BELOW THE LOCK, and the order is the whole correctness
        argument. This used to evaluate `SUM(bottle_ledger.quantity)` FIRST and
        take the balance row FOR UPDATE second, so a delivery committing while
        it waited was compared against a PRE-delivery ledger sum and then
        overwritten away — the only balance writer in the codebase that appends
        no ledger entry, silently eating a committed, ledger-backed delivery and
        reporting it to the admin as a repair. Two figures that never described
        the same world.

        Holding the place's single balance row (rung 2/3) excludes every
        concurrent writer at that place, and rung 1 excludes the lifecycle's
        ledger re-stamps, so the sum below and the balance above describe ONE
        world and the operation is idempotent.

        A place with NO balance row is reported as zeros and NOTHING is written:
        minting a row here is how the `orphaned_place_balances` class comes back.
        """
        scope = self.resolve_scope_for_write(address_id)

        balance = self.get_balance_row(scope)
        if balance is None:
            return {
                "address_group_id": scope.group_id,
                "address_id": scope.address_id,
                "previous_balance": 0.0,
                "recalculated_balance": 0.0,
                "discrepancy": 0.0,
                "corrected": False,
            }
        ledger_sum = (
            db.session.query(func.coalesce(func.sum(BottleLedger.quantity), 0)).filter(*scope.ledger_filter()).scalar()
        )
        current = float(balance.balance or 0)
        expected = float(ledger_sum or 0)
        discrepancy = round(current - expected, 2)

        if discrepancy != 0:
            logger.warning(
                "Bottle balance discrepancy for place group=%s address=%s: current=%s expected=%s diff=%s",
                scope.group_id,
                scope.address_id,
                current,
                expected,
                discrepancy,
            )
            balance.balance = Decimal(str(expected))
            db.session.flush()

        return {
            "address_group_id": scope.group_id,
            "address_id": scope.address_id,
            "previous_balance": current,
            "recalculated_balance": expected,
            "discrepancy": discrepancy,
            "corrected": discrepancy != 0,
        }

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Driver bottle sessions
    # ------------------------------------------------------------------

    @staticmethod
    def _anchor_actor_at_warehouse(driver_user_id: int, actor_user_id: Optional[int]) -> None:
        """Move the driver's stored position to the depot, if they were there.

        Opening and closing a session are physical acts at the single
        warehouse — but only when the DRIVER performs them. `actor_user_id`
        exists so a non-driver can act on a driver's session, and such a caller
        knows nothing about where the driver is standing; anchoring on their
        behalf would plant a confident, wrong pin on the dispatch map and hand
        route optimization a false origin. So: driver-acting only.

        The write itself belongs to StaffService, the one owner of
        DeliveryPerson location columns — this method decides *whether*, never
        *what*. Imported inside the function because StaffService reaches back
        into BottleTrackingService the same way.
        """
        if actor_user_id is not None and int(actor_user_id) != int(driver_user_id):
            return

        from business_app.services.staff_service import StaffService

        StaffService.anchor_driver_at_warehouse(driver_user_id)

    @staticmethod
    def _assert_bottle_count_within(field: str, value: int, ceiling: int) -> None:
        """The SERVICE-side half of the session bottle bounds.

        The floor guards beside each call site have always been here; the
        ceilings lived only in the staff bot and in the pydantic request bodies,
        which means they only ever bounded a count that arrived over HTTP. Every
        other caller — a Celery task, an admin path, a script, a fixture that
        graduates into production code — reached a 4-byte PostgreSQL integer
        with an unbounded number and took the write down as a DataError 500
        carrying no hint about which field or which value.

        The numbers themselves are NOT restated here: `MAX_BOTTLES_PER_SESSION`
        and `BOTTLE_RETURN_COLUMN_CEILING` are the same names the bot and the
        serializers read (shared/staff_constants.py documents what each one is
        for and why the return bound is deliberately not the load-out one).
        `ValidationError` is the same type the floor guards raise, so a caller
        sees one shape whichever end of the range it missed.
        """
        if value > ceiling:
            raise ValidationError(f"{field} cannot exceed {ceiling}")

    def open_bottle_session(
        self,
        driver_user_id: int,
        bottles_loaded: int,
        *,
        units: Optional[Dict[int, int]] = None,
        actor_user_id: int = None,
        notes: str = None,
    ) -> DriverBottleSession:
        """Open a trip session for the driver, then offer the pool to the new capacity (see `_open_bottle_session`).

        The re-offer is queued only after the open has committed, so a refused open offers nothing.
        """
        session = self._open_bottle_session(
            driver_user_id, bottles_loaded, units=units, actor_user_id=actor_user_id, notes=notes
        )
        self._reoffer_pool_after_capacity_gain()
        return session

    @staticmethod
    def _reoffer_pool_after_capacity_gain() -> None:
        """Publish `reoffer_pool_deliveries_task`: a crew just gained free full bottles, so an order auto-assign refused
        for want of them may fit now. Call it only after the write has committed. The write is already done, so a broker
        failure is logged, never raised: the stale sweep re-offers the pool anyway."""
        from business_app.tasks.delivery_monitoring_tasks import reoffer_pool_deliveries_task

        try:
            reoffer_pool_deliveries_task.delay()
        except Exception:  # noqa: BLE001
            logger.exception("[BOTTLE] could not queue the pool re-offer after a capacity gain")

    @transactional
    def _open_bottle_session(
        self,
        driver_user_id: int,
        bottles_loaded: int,
        *,
        units: Optional[Dict[int, int]] = None,
        actor_user_id: int = None,
        notes: str = None,
    ) -> DriverBottleSession:
        """Open a new trip for the driver (load from warehouse): `bottles_loaded` 18.9 L and `units`
        ({product_id: count}) of the one-way products the trip counts.

        In this order:
          1. The driver's row in the first rung (`_lock_delivery_persons`: his DeliveryPerson row, or his users row when
             he has no delivery profile), so his join, a reopening edit of his closed trip and this open queue on one
             row. Loading happens AT the depot, so his stored position is then anchored there (his DeliveryPerson row;
             see `_anchor_actor_at_warehouse`), and flushed.
          2. `LOCK TABLE driver_bottle_sessions IN ROW EXCLUSIVE MODE`, then the products the setting counts: a save of
             the setting (SHARE) waits for this open, or this open reads what it committed. Lock order: DeliveryPerson
             row, then the trips table.
          3. The refusals: the driver's own open trip, or a trip he co-drives (`_assert_free_to_open`); a product the
             trip does not count (TRIP_UNIT_NOT_COUNTED); a negative number or one above `MAX_BOTTLES_PER_SESSION`;
             nothing loaded at all (`_assert_load_not_empty`). A refusal rolls the anchor back with everything else.
          4. The trip, then one line per counted product, an omitted one at 0.

        The DB partial unique index on (driver_user_id) WHERE status='open' is a second safety net against concurrent
        opens.
        """
        units = dict(units or {})
        self._lock_delivery_persons([driver_user_id])
        self._anchor_actor_at_warehouse(driver_user_id, actor_user_id)
        db.session.flush()
        self.lock_trip_table("ROW EXCLUSIVE")
        counted = self.counted_trip_products()
        self._assert_free_to_open(driver_user_id)

        if bottles_loaded < 0:
            raise ValidationError("bottles_loaded cannot be negative")
        self._assert_bottle_count_within("bottles_loaded", bottles_loaded, MAX_BOTTLES_PER_SESSION)
        counted_ids = {product.id for product in counted}
        for product_id, quantity in units.items():
            if product_id not in counted_ids:
                raise ValidationError(
                    f"Product {product_id} is not counted on the bottle trip",
                    details={"product_id": product_id},
                    error_code="TRIP_UNIT_NOT_COUNTED",
                )
            if quantity < 0:
                raise ValidationError(f"units[{product_id}] cannot be negative")
            self._assert_bottle_count_within(f"units[{product_id}]", quantity, MAX_BOTTLES_PER_SESSION)
        self._assert_load_not_empty(bottles_loaded, units)

        session = DriverBottleSession(
            driver_user_id=driver_user_id,
            bottles_loaded=bottles_loaded,
            status=DriverBottleSessionStatus.OPEN,
            loaded_by_user_id=actor_user_id or driver_user_id,
            started_at=self._utc_now(),
            notes=notes,
        )
        db.session.add(session)
        db.session.flush()
        for product in counted:
            self._unit_line(session, product.id, create=True).loaded = units.get(product.id, 0)
        db.session.flush()
        return session

    def _assert_free_to_open(self, driver_user_id: int) -> None:
        """A driver opens a trip only when he has none of his own and co-drives none (`_open_trip_conflict`).

        Both refusals name the trip in the way: `details` = `_open_refusal_details` (for his own trip, the owner is
        himself), so the bot can say whose trip he is on.
        """
        conflict = self._open_trip_conflict(driver_user_id)
        if conflict is None:
            return
        code, trip = conflict
        if code == "BOTTLE_SESSION_ALREADY_OPEN":
            raise ConflictError(
                f"Driver already has an open bottle session (id={trip.id}). "
                "Close the current session before starting a new one.",
                details=self._open_refusal_details(trip),
                error_code="BOTTLE_SESSION_ALREADY_OPEN",
            )
        raise ConflictError(
            f"Driver is an active co-driver on session {trip.id}. Leave that trip before opening one.",
            details=self._open_refusal_details(trip),
            error_code="BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE",
        )

    @staticmethod
    def _assert_load_not_empty(bottles_loaded: int, units: Dict[int, int]) -> None:
        """THE empty-load rule: a trip opens with at least one number above 0, 18.9 L or a unit.

        The request model and the bot's keypad only bound each number (0 to `MAX_BOTTLES_PER_SESSION`); this is the
        one place that refuses a load of nothing.
        """
        if bottles_loaded == 0 and not any(units.values()):
            raise ValidationError(
                "Load at least one bottle or unit to open a trip",
                error_code="BOTTLE_SESSION_EMPTY_LOAD",
            )

    def load_screen(self, driver_user_id: int) -> LoadScreen:
        """What the Load screen shows, in one read: the products the trip counts, and the open's refusal if he may
        not open one (the same reader `_assert_free_to_open` refuses from, so the screen and the open agree)."""
        products = self.counted_trip_products()
        conflict = self._open_trip_conflict(driver_user_id)
        if conflict is None:
            return LoadScreen(products, None, {}, None, None)
        code, trip = conflict
        refusal_session = db.session.get(
            DriverBottleSession,
            trip.id,
            options=[
                joinedload(DriverBottleSession.driver),
                joinedload(DriverBottleSession.unit_lines).joinedload(DriverBottleSessionUnit.product),
            ],
            populate_existing=True,
        )
        own = refusal_session if code == "BOTTLE_SESSION_ALREADY_OPEN" else None
        return LoadScreen(products, code, self._open_refusal_details(refusal_session), refusal_session, own)

    @transactional
    def close_bottle_session(
        self,
        driver_user_id: int,
        bottles_returned_to_warehouse: int,
        *,
        units_returned: Optional[Dict[int, int]] = None,
        actor_user_id: int = None,
        notes: str = None,
        anchor_at_warehouse: bool = True,
    ) -> DriverBottleSession:
        """Close the driver's active trip session (return to warehouse).

        `units_returned` ({product_id: count}) answers every line on the trip, read after its lock: one left out is
        BOTTLE_SESSION_RETURN_INCOMPLETE (`details.missing_product_ids`), a product with no line TRIP_UNIT_NOT_COUNTED.
        Computes and persists both discrepancies (`_recompute_discrepancies`).
        Raises NotFoundError if no open session exists.

        The session is read under its row lock (and re-read), so a transfer send and this close serialize either way: a
        close behind a send seals the counters the send left, and a send behind a close finds no open session.

        A driver may close at any time. Any still-undelivered order bound to the
        session is released here (its binding deleted) rather than blocking the
        close; it re-binds to the driver's next open session on the next forward
        transition (late-bind guard), or an admin delivers it unbound.

        The return happens AT the depot, so the driver's stored position is
        anchored there — see `_anchor_actor_at_warehouse`. Deliberately NOT
        mirrored in `admin_force_close_session`: an admin closing an abandoned
        session is at a desk, and the driver is wherever they actually are.
        `anchor_at_warehouse=False` is the same reasoning for a trip that closes
        mid-route (`_auto_close_sender_if_emptied`), which locks no DeliveryPerson row.

        Lock order: the anchor writes the driver's DeliveryPerson row BEFORE the
        session row is locked, the order every other path keeps (DeliveryPerson rows,
        then the trip: assignment and the progress guard bind an order after their
        DeliveryPerson lock, and Delivered adds to the trip's tallies after writing the
        deliverer's row). Anchoring after the session lock deadlocked with them. A
        refused close rolls the anchor back with everything else.
        """
        if anchor_at_warehouse:
            self._anchor_actor_at_warehouse(driver_user_id, actor_user_id)
            db.session.flush()
        session = self._get_open_session_or_raise(driver_user_id, lock=True)

        if bottles_returned_to_warehouse < 0:
            raise ValidationError("bottles_returned_to_warehouse cannot be negative")
        self._assert_bottle_count_within(
            "bottles_returned_to_warehouse",
            bottles_returned_to_warehouse,
            BOTTLE_RETURN_COLUMN_CEILING,
        )
        self._write_line_returns(session, units_returned or {}, every_line=True)

        # Release still-undelivered orders instead of blocking the close. Deleting
        # the binding (vs. leaving it on the now-closed session) lets the order re-bind
        # to the trip that carries it next. The Delivered tally credits only an OPEN
        # trip, re-reading the binding under the trip's lock, so a Delivered that waited
        # on this close finds it gone and adds nothing to the sealed counters.
        released = self._release_open_bindings_for_session(session.id)
        if released:
            logger.info(
                "[BOTTLE] close_bottle_session released %s carried order binding(s) for session=%s: %s",
                len(released),
                session.id,
                released,
            )

        session.bottles_returned_to_warehouse = bottles_returned_to_warehouse
        session.status = DriverBottleSessionStatus.CLOSED
        session.closed_at = self._utc_now()
        session.closed_by_user_id = actor_user_id or driver_user_id
        self._recompute_discrepancies(session)
        if notes:
            session.notes = (session.notes or "") + f"\n{notes}" if session.notes else notes
        revoked = self.revoke_all_memberships(session.id)
        if revoked:
            logger.info(
                "[BOTTLE] close_bottle_session revoked %s co-driver membership(s) for session=%s", revoked, session.id
            )
        db.session.flush()
        return session

    @transactional
    def admin_force_close_session(
        self,
        session_id: int,
        actor_user_id: int,
        *,
        bottles_returned_to_warehouse: int = 0,
        units_returned: Optional[Dict[int, int]] = None,
        reason: str,
    ) -> DriverBottleSession:
        """Admin force-closes an abandoned open session.

        The discrepancy will reflect the full unaccounted load. `units_returned` ({product_id: count}) is read like the
        driver's close, except that a line left out is 0 returned.
        A reason is mandatory for the audit trail.

        The session is read under its row lock (and re-read) before its OPEN check, as `close_bottle_session` reads
        it: a force-close behind a transfer send seals the counters the send left, and a send behind it finds no open
        session. Locks: the one session row, never a DeliveryPerson row.
        """
        session = self._lock_session(session_id)
        if not session:
            raise NotFoundError("Bottle session not found")
        if session.status != DriverBottleSessionStatus.OPEN:
            raise ConflictError(f"Session is already {session.status.value}, cannot force close")
        if not reason or not reason.strip():
            raise ValidationError("A reason is required for force-closing a session")
        # THE BOUND BELONGS TO THE FIELD, NOT TO THE CALLER. This is the third
        # writer of `bottles_returned_to_warehouse`; leaving it out would keep
        # the DataError alive on the admin path after the driver's two were
        # fixed. (The floor here CLAMPS rather than raises — `max(0, ...)`
        # below — which is pre-existing and deliberate for an admin cleaning up
        # an abandoned session; a value the column cannot hold has no such
        # sensible clamp, so it is refused.)
        self._assert_bottle_count_within(
            "bottles_returned_to_warehouse",
            bottles_returned_to_warehouse,
            BOTTLE_RETURN_COLUMN_CEILING,
        )
        self._write_line_returns(session, units_returned or {}, every_line=False)

        # Mirror the normal-close release: an abandoned session's still-undelivered
        # orders must not stay bound to this sealed session (see
        # _release_open_bindings_for_session). They re-bind to whichever session
        # physically carries them next.
        released = self._release_open_bindings_for_session(session.id)
        if released:
            logger.info(
                "[BOTTLE] admin_force_close_session released %s carried order binding(s) for session=%s: %s",
                len(released),
                session.id,
                released,
            )

        session.bottles_returned_to_warehouse = max(0, bottles_returned_to_warehouse)
        session.status = DriverBottleSessionStatus.FORCE_CLOSED
        session.force_closed = True
        session.force_close_reason = reason.strip()
        session.closed_at = self._utc_now()
        session.closed_by_user_id = actor_user_id
        self._recompute_discrepancies(session)
        revoked = self.revoke_all_memberships(session.id)
        if revoked:
            logger.info(
                "[BOTTLE] admin_force_close_session revoked %s co-driver membership(s) for session=%s",
                revoked,
                session.id,
            )
        db.session.flush()
        return session

    def reopen_session(
        self,
        session_id: int,
        actor_user_id: int,
        *,
        reason: str,
        commit: bool = True,
    ) -> DriverBottleSession:
        """Reopen a CLOSED / FORCE_CLOSED bottle session for retroactive adjustment.

        Used when an admin edits a delivered order whose driver session has
        already been closed. The session transitions back to OPEN so the
        order-edit cascade can write balancing ledger entries that re-tally
        cleanly. The driver re-closes the session afterward (the returns and
        discrepancies, every line's included, are reset so close-time math runs
        from the updated tallies).

        Raises:
            ValidationError: reason is empty, or status is not CLOSED/FORCE_CLOSED.
            ConflictError: the driver is on another trip, his own (partial unique
                index `uq_dbs_driver_open` would otherwise be violated) or one he
                co-drives (`reopen_conflict`).
        """
        if not reason or not reason.strip():
            raise ValidationError("A reason is required to reopen a bottle session")

        # The trip row's lock: its lines are reset below. On the order-edit path it comes after the driver's
        # DeliveryPerson row and the Order, product and balance rows (DeliveryPerson -> Order -> product -> balance ->
        # trip), so the conflict below is read while his open and his join wait.
        session = self._lock_session(session_id)
        if not session:
            raise NotFoundError("Bottle session not found")

        if session.status not in {
            DriverBottleSessionStatus.CLOSED,
            DriverBottleSessionStatus.FORCE_CLOSED,
        }:
            raise ValidationError(
                f"Cannot reopen session {session.id}: status is "
                f"{session.status.value if hasattr(session.status, 'value') else session.status}; "
                "only CLOSED or FORCE_CLOSED sessions can be reopened.",
                error_code="BOTTLE_SESSION_NOT_REOPENABLE",
            )

        if self.reopen_conflict(session):
            raise ConflictError(
                f"Cannot reopen session {session.id}: the driver has another open trip or rides on one; "
                "close or leave it first.",
                error_code="BOTTLE_SESSION_ACTIVE_CONFLICT",
            )

        previous_status = session.status.value if hasattr(session.status, "value") else session.status
        session.status = DriverBottleSessionStatus.OPEN
        session.reopened_at = self._utc_now()
        session.reopened_by_user_id = actor_user_id
        session.reopened_reason = reason.strip()
        session.reopen_count = (session.reopen_count or 0) + 1
        # Reset close-side state so the post-adjustment re-close recomputes
        # discrepancy from the updated tallies.
        session.bottles_returned_to_warehouse = None
        session.closed_at = None
        session.closed_by_user_id = None
        session.discrepancy = None
        db.session.expire(session, ["unit_lines"])
        for line in session.unit_lines:
            line.returned = None
            line.discrepancy = None
        # The session was force-closed by admin previously? Drop the flag so the
        # next normal close treats it as a clean trip.
        session.force_closed = False

        audit_logger.log_event(
            event_type=AuditEventType.SESSION_REOPENED,
            action="driver_bottle_session_reopened",
            severity=AuditSeverity.HIGH,
            resource_type="driver_bottle_session",
            resource_id=str(session.id),
            additional_data={
                "driver_user_id": session.driver_user_id,
                "actor_user_id": actor_user_id,
                "previous_status": previous_status,
                "reopen_count": session.reopen_count,
                "reason": session.reopened_reason,
            },
        )

        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return session

    def get_open_session(self, driver_user_id: int, *, lock: bool = False) -> Optional[DriverBottleSession]:
        """Return the driver's current open session, or None.

        `lock` locks the row until the transaction ends and re-reads it (`populate_existing`). A close that commits
        while the lock waits takes the row out of the `status = 'open'` match, so the read then finds nothing. The
        lock is `SESSION_ROW_LOCK` (FOR NO KEY UPDATE), so binding an order to the trip never waits on it.
        """
        logger.debug("[BOTTLE] get_open_session driver=%s", driver_user_id)
        query = DriverBottleSession.query.filter_by(
            driver_user_id=driver_user_id,
            status=DriverBottleSessionStatus.OPEN,
        )
        if lock:
            query = query.with_for_update(**SESSION_ROW_LOCK).populate_existing()
        session = query.first()
        logger.debug(
            "[BOTTLE] get_open_session driver=%s → %s",
            driver_user_id,
            f"session_id={session.id}" if session else "None",
        )
        return session

    def _get_open_session_or_raise(self, driver_user_id: int, *, lock: bool = False) -> DriverBottleSession:
        session = self.get_open_session(driver_user_id, lock=lock)
        if not session:
            raise NotFoundError(
                "No open bottle session found for this driver",
                error_code="BOTTLE_SESSION_NOT_FOUND",
            )
        return session

    # ------------------------------------------------------------------
    # Co-driver session membership
    # ------------------------------------------------------------------

    def get_effective_session(self, driver_user_id: int, *, lock: bool = False) -> Optional[DriverBottleSession]:
        """Return the session this driver should operate under.

        Priority:
          1. Driver's own OPEN session (if they have one).
          2. The OPEN session they have joined as a co-driver member.
          3. None — driver has no access to any session.

        `lock` reads either session under its row lock (and re-reads it), like `get_open_session`: a close that commits
        while the lock waits takes the row out of the OPEN match, so a closed session is never returned locked.
        """
        own = self.get_open_session(driver_user_id, lock=lock)
        if own:
            return own
        membership = self.get_active_membership(driver_user_id)
        if membership:
            query = DriverBottleSession.query.filter_by(id=membership.session_id, status=DriverBottleSessionStatus.OPEN)
            if lock:
                query = query.with_for_update(**SESSION_ROW_LOCK).populate_existing()
            return query.first()
        return None

    @staticmethod
    def crew_driver_ids(session: DriverBottleSession) -> List[int]:
        """The session owner plus every ACTIVE co-driver, sorted: the drivers who share one truck."""
        member_ids = [
            row[0]
            for row in db.session.query(DriverSessionMembership.member_driver_id).filter(
                DriverSessionMembership.session_id == session.id,
                DriverSessionMembership.status == DriverSessionMembershipStatus.ACTIVE,
            )
        ]
        return sorted({int(session.driver_user_id), *(int(m) for m in member_ids)})

    @staticmethod
    def promised_stops_query(driver_ids: Iterable[int]):
        """The orders of the stops these drivers have set off on with bottles from their truck: the delivery On the
        way or Arrived (`DELIVERY_BOTTLES_PROMISED_STATES`), owned by one of them, its order one a driver holds
        (`DRIVER_HELD_ORDER_STATUSES`, the ETA engine's own set). An order closed on the Orders page while its
        delivery stayed on the road no longer counts. Read by the crew's free full bottles and by `leave_session`.
        """
        return Order.query.join(DeliveryModel, DeliveryModel.order_id == Order.id).filter(
            DeliveryModel.delivery_person_id.in_(list(driver_ids)),
            DeliveryModel.status.in_(DELIVERY_BOTTLES_PROMISED_STATES),
            Order.status.in_(DRIVER_HELD_ORDER_STATUSES),
        )

    def free_on_trip(self, session: DriverBottleSession, *, exclude_delivery_id: Optional[int] = None) -> TripFree:
        """What the crew can still promise: on board minus the stops already under way, per 18.9 L and per unit line.

        The one capacity number. Empties never count, ASSIGNED / PICKED_UP stops do not reserve, and the
        crew shares one number (counted by who owns the delivery, not by the order's session binding;
        `promised_stops_query`). A reopened session is correction-only, and a closed one is a finished trip,
        so neither has any.

        It reads the stops on their way before the trip's tallies. The progress guard relies on that
        order (see `_lock_crew_delivery_persons`).
        """
        if session.status != DriverBottleSessionStatus.OPEN or session.reopened_at is not None:
            return TripFree(0, {})
        query = self.promised_stops_query(self.crew_driver_ids(session)).options(
            joinedload(Order.order_items).joinedload(OrderItem.product)
        )
        if exclude_delivery_id is not None:
            query = query.filter(DeliveryModel.id != exclude_delivery_id)
        promised = self.trip_units_for_lines((i.product, i.quantity) for o in query.all() for i in o.order_items)
        # The tallies after the stops, never before: a tally the caller expired reloads here.
        bottles = max(0, session.full_bottles_on_board - int(promised.bottles))
        lines = DriverBottleSessionUnit.query.filter_by(session_id=session.id).populate_existing().all()
        return TripFree(
            bottles, {line.product_id: max(0, line.on_board - promised.units.get(line.product_id, 0)) for line in lines}
        )

    def free_full_bottles_for_session(
        self, session: DriverBottleSession, *, exclude_delivery_id: Optional[int] = None
    ) -> int:
        """Free 18.9 L bottles of the trip (`free_on_trip`)."""
        return self.free_on_trip(session, exclude_delivery_id=exclude_delivery_id).bottles

    def free_units_for_session(
        self, session: DriverBottleSession, product_id: int, *, exclude_delivery_id: Optional[int] = None
    ) -> int:
        """Free units of one one-way product on the trip: 0 for a closed or reopened trip or a missing line."""
        return self.free_on_trip(session, exclude_delivery_id=exclude_delivery_id).units.get(product_id, 0)

    def free_full_bottles(self, driver_user_id: int, *, exclude_delivery_id: Optional[int] = None) -> Optional[int]:
        """Free full bottles of the session this driver operates under, or None when they have none."""
        session = self.get_effective_session(driver_user_id)
        if session is None:
            return None
        return self.free_full_bottles_for_session(session, exclude_delivery_id=exclude_delivery_id)

    def get_active_membership(self, driver_user_id: int) -> Optional[DriverSessionMembership]:
        """Return the driver's current active co-driver membership, if any."""
        return DriverSessionMembership.query.filter_by(
            member_driver_id=driver_user_id,
            status=DriverSessionMembershipStatus.ACTIVE,
        ).first()

    def _open_trip_conflict(self, driver_id: int) -> Optional[Tuple[str, DriverBottleSession]]:
        """THE reader of "this driver is already on a trip": `(error code, the trip)`, or None.

        His own open trip (BOTTLE_SESSION_ALREADY_OPEN), else the trip of his active co-driver membership
        (BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE), whatever that trip's status. The open, the Load screen and the join
        refuse from it, each with its own words.
        """
        own = self.get_open_session(driver_id)
        if own is not None:
            return "BOTTLE_SESSION_ALREADY_OPEN", own
        membership = self.get_active_membership(driver_id)
        if membership is not None:
            return "BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE", membership.session
        return None

    def reopen_conflict(self, session: DriverBottleSession) -> Optional[str]:
        """Why `session` cannot be reopened, or None: BOTTLE_SESSION_ACTIVE_CONFLICT while its driver is on another
        trip, his own or one he co-drives (`_open_trip_conflict`). Read-only, no lock: the order-edit preview shows it,
        and `reopen_session` refuses on it."""
        if self._open_trip_conflict(session.driver_user_id) is not None:
            return "BOTTLE_SESSION_ACTIVE_CONFLICT"
        return None

    @staticmethod
    def _open_refusal_details(session: DriverBottleSession) -> Dict:
        """The `details` of an open refused for `session`: the trip and its owner, so the driver is told whose trip."""
        return {
            "session_id": session.id,
            "owner_user_id": session.driver_user_id,
            "owner_name": session.driver.full_name,
        }

    def get_joinable_sessions(self, excluding_driver_id: int) -> List[DriverBottleSession]:
        """OPEN sessions this driver may ask to join.

        Refuses up front when the driver could not join any of them anyway
        (their own open session, or already a co-driver), so the bot says why
        before the driver picks one.
        """
        self._assert_free_to_join(int(excluding_driver_id))
        return (
            DriverBottleSession.query.filter(
                DriverBottleSession.status == DriverBottleSessionStatus.OPEN,
                DriverBottleSession.driver_user_id != excluding_driver_id,
            )
            .order_by(DriverBottleSession.started_at.desc())
            .all()
        )

    def _assert_free_to_join(self, driver_id: int) -> None:
        """A driver joins a trip only when he has none of his own and co-drives none (`_open_trip_conflict`). No
        details: the bot keeps its join sentences, never the open's "you are on X's trip"."""
        conflict = self._open_trip_conflict(driver_id)
        if conflict is None:
            return
        code, trip = conflict
        if code == "BOTTLE_SESSION_ALREADY_OPEN":
            raise ConflictError(
                "Close your own open session before joining another driver's session",
                error_code="BOTTLE_SESSION_ALREADY_OPEN",
            )
        raise ConflictError(
            f"Already an active co-driver member of session {trip.id}. Leave that session before joining another.",
            error_code="BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE",
        )

    def _assert_can_join(self, member_driver_id: int, session_id: int) -> DriverBottleSession:
        """Every rule for "this driver may join this session", in one place.

        Shared by the request (which writes nothing) and `join_session` (which
        writes the membership), so a request is never sent that the approval
        would refuse for a reason that was already true. `_join_session` locks
        the trip first, so `query.get` returns the row its lock re-read.
        """
        session = DriverBottleSession.query.get(session_id)
        if not session:
            # The session the driver picked, not their own: its own code, so
            # the bot never tells a joiner "you have no open session".
            raise NotFoundError(
                "Bottle session not found",
                error_code="BOTTLE_SESSION_TARGET_NOT_FOUND",
            )
        if session.driver_user_id == member_driver_id:
            raise ValidationError(
                "Cannot join your own session",
                error_code="BOTTLE_SESSION_JOIN_OWN",
            )
        if session.status != DriverBottleSessionStatus.OPEN:
            raise ValidationError(
                "Can only join an OPEN session",
                error_code="BOTTLE_SESSION_NOT_OPEN",
            )
        self._assert_free_to_join(member_driver_id)
        return session

    def join_session(
        self,
        member_driver_id: int,
        session_id: int,
        *,
        notes: str = None,
        invited_by_user_id: int = None,
    ) -> DriverSessionMembership:
        """Add the driver to another driver's open session as a co-driver (see `_join_session`), then offer the pool.

        The crew now shares the owner's free full bottles, so the re-offer is queued after the membership has
        committed. Every way in (own join, invite, an approved request) lands here, so each queues it exactly once.
        """
        membership = self._join_session(
            member_driver_id, session_id, notes=notes, invited_by_user_id=invited_by_user_id
        )
        self._reoffer_pool_after_capacity_gain()
        return membership

    @transactional
    def _join_session(
        self,
        member_driver_id: int,
        session_id: int,
        *,
        notes: str = None,
        invited_by_user_id: int = None,
    ) -> DriverSessionMembership:
        """Allow a driver to join another driver's open session as a co-driver.

        Locks, in the spec's order (DeliveryPerson rows, then the trip), before any rule is read:
          1. The joiner's DeliveryPerson row FOR UPDATE. His own open writes that row first (the depot anchor), so a
             join and his open queue here, and the later one is refused for the earlier (BOTTLE_SESSION_ALREADY_OPEN
             behind an open, BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE for an open behind a join).
          2. The trip's row (`_lock_session`, re-read), the lock a close takes: a join behind a close sees it closed
             (BOTTLE_SESSION_NOT_OPEN), and a close behind a join revokes the membership it wrote.
        `_assert_can_join` then reads the trip the lock re-read.

        Raises:
          - ConflictError if the driver already has their own OPEN session.
          - ConflictError if the driver is already an active member of another session.
          - NotFoundError if the target session is not found.
          - ValidationError if the target session is not OPEN.
          - ValidationError if driver tries to join their own session.
        """
        # JWT identities arrive as str (`sub`); every column compared below is int.
        member_driver_id = int(member_driver_id)
        self._lock_delivery_persons([member_driver_id])
        self._lock_session(session_id)
        session = self._assert_can_join(member_driver_id, session_id)

        membership = DriverSessionMembership(
            session_id=session_id,
            session_owner_id=session.driver_user_id,
            member_driver_id=member_driver_id,
            status=DriverSessionMembershipStatus.ACTIVE,
            notes=notes,
            invited_by_user_id=invited_by_user_id,
        )
        db.session.add(membership)
        db.session.flush()
        logger.info(
            "[BOTTLE] join_session member=%s joined session=%s (owner=%s)",
            member_driver_id,
            session_id,
            session.driver_user_id,
        )
        return membership

    @transactional
    def leave_session(self, member_driver_id: int) -> DriverSessionMembership:
        """Voluntarily leave the current co-driver session membership.

        Raises NotFoundError if the driver has no active membership, and ConflictError
        BOTTLE_SESSION_LEAVE_HAS_PROMISED_STOPS (details `count`) while he owns a stop the crew's bottles are
        promised to (`promised_stops_query`): leaving would take it out of the crew's sum while its bottles stay on
        the truck, so they could be promised twice (spec A8). He delivers or fails those stops first. Assigned and
        Picked-up stops hold no bottles (A10) and never block a leave.
        """
        membership = self.get_active_membership(member_driver_id)
        if not membership:
            raise NotFoundError(
                "No active co-driver session membership found",
                error_code="BOTTLE_SESSION_MEMBERSHIP_NOT_FOUND",
            )
        promised = self.promised_stops_query([int(member_driver_id)]).count()
        if promised:
            raise ConflictError(
                f"Finish your {promised} stop(s) on the way before leaving the session",
                details={"count": int(promised)},
                error_code="BOTTLE_SESSION_LEAVE_HAS_PROMISED_STOPS",
            )
        membership.status = DriverSessionMembershipStatus.LEFT
        membership.left_at = self._utc_now()
        db.session.flush()
        logger.info(
            "[BOTTLE] leave_session member=%s left session=%s",
            member_driver_id,
            membership.session_id,
        )
        return membership

    def _add_codriver_for_owner(
        self,
        owner_session: DriverBottleSession,
        member_driver_id: int,
        *,
        trip_closed: Callable[[], ConflictError],
    ) -> DriverSessionMembership:
        """`join_session` on the OWNER's initiative (invite, or approving a request).

        `join_session`'s refusals are worded for the joiner; the owner is the one
        reading these. Same facts, the right person.

        `trip_closed` builds the caller's refusal for "your trip is no longer open": the one the caller answers when it
        reads the trip closed. The caller read it open, but a close (the owner's own on another phone, or an office
        force-close) can commit before the join locks the trip, and the join then refuses with the joiner's
        BOTTLE_SESSION_NOT_OPEN. It is answered with the caller's own refusal, so the timing never picks the code.
        """
        try:
            return self.join_session(
                member_driver_id,
                owner_session.id,
                invited_by_user_id=owner_session.driver_user_id,
            )
        except ValidationError as exc:
            if exc.error_code == "BOTTLE_SESSION_NOT_OPEN":
                raise trip_closed() from exc
            raise
        except ConflictError as exc:
            if exc.error_code == "BOTTLE_SESSION_ALREADY_OPEN":
                raise ConflictError(
                    "The invited driver has their own open bottle session",
                    error_code="BOTTLE_INVITEE_HAS_SESSION",
                ) from exc
            if exc.error_code == "BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE":
                raise ConflictError(
                    "The invited driver is already in another driver's session",
                    error_code="BOTTLE_INVITEE_IN_OTHER_SESSION",
                ) from exc
            raise

    @staticmethod
    def _no_trip_to_invite_to() -> ConflictError:
        return ConflictError(
            "You must have an open bottle session to invite co-drivers",
            error_code="BOTTLE_SESSION_REQUIRED_TO_INVITE",
        )

    def invite_driver_to_session(self, owner_driver_id: int, member_driver_id: int) -> DriverSessionMembership:
        """The owner adds a colleague to their open session, instantly (ruling R4)."""
        owner_session = self.get_open_session(int(owner_driver_id))
        if not owner_session:
            raise self._no_trip_to_invite_to()
        return self._add_codriver_for_owner(
            owner_session, int(member_driver_id), trip_closed=self._no_trip_to_invite_to
        )

    def request_to_join_session(self, requester_id: int, session_id: int) -> DriverBottleSession:
        """Ask the session's owner to let this driver join. Writes nothing (ruling R6):
        the request IS the owner's Telegram message, and approving re-checks it all."""
        from business_app.services import bottle_notifications

        requester_id = int(requester_id)
        session = self._assert_can_join(requester_id, int(session_id))
        bottle_notifications.notify_join_requested(session, User.query.get(requester_id))
        return session

    def _owner_session_for_request(
        self, owner_driver_id: int, session_id: int, *, must_be_open: bool
    ) -> DriverBottleSession:
        """The session a request named, provided the caller owns it (and, to
        approve, still has it open: a request for a session since closed must
        never land the requester on a newer one they did not ask for)."""
        session = DriverBottleSession.query.get(session_id)
        if (
            session is None
            or session.driver_user_id != owner_driver_id
            or (must_be_open and session.status != DriverBottleSessionStatus.OPEN)
        ):
            raise self._stale_join_request()
        return session

    @staticmethod
    def _stale_join_request() -> ConflictError:
        return ConflictError(
            "This join request is no longer valid",
            error_code="BOTTLE_JOIN_REQUEST_STALE",
        )

    def _requester_for_answer(self, requester_id: int) -> User:
        """The requester, provided they are still an active driver (accounts can
        be merged away or deactivated between the request and the answer)."""
        requester = self._active_drivers_query().filter(User.id == requester_id).first()
        if requester is None:
            raise ConflictError(
                "This join request is no longer valid",
                error_code="BOTTLE_JOIN_REQUEST_STALE",
            )
        return requester

    def approve_join_request(self, owner_driver_id: int, session_id: int, requester_id: int) -> DriverSessionMembership:
        """The owner says yes. A repeat tap returns the membership already made
        and tells nobody twice."""
        from business_app.services import bottle_notifications

        owner_driver_id, requester_id = int(owner_driver_id), int(requester_id)
        session = self._owner_session_for_request(owner_driver_id, int(session_id), must_be_open=True)
        self._requester_for_answer(requester_id)
        existing = self.get_active_membership(requester_id)
        if existing is not None and existing.session_id == session.id:
            return existing
        membership = self._add_codriver_for_owner(session, requester_id, trip_closed=self._stale_join_request)
        bottle_notifications.notify_join_answered(session, membership.member_driver, approved=True)
        return membership

    def decline_join_request(self, owner_driver_id: int, session_id: int, requester_id: int) -> User:
        """The owner says no. Writes nothing; tells the requester."""
        from business_app.services import bottle_notifications

        owner_driver_id, requester_id = int(owner_driver_id), int(requester_id)
        session = self._owner_session_for_request(owner_driver_id, int(session_id), must_be_open=False)
        requester = self._requester_for_answer(requester_id)
        existing = self.get_active_membership(requester_id)
        if existing is not None and existing.session_id == session.id:
            # A duplicate request message, answered after the first was approved.
            raise ConflictError(
                "This driver is already in your session",
                error_code="BOTTLE_JOIN_REQUEST_ALREADY_MEMBER",
            )
        bottle_notifications.notify_join_answered(session, requester, approved=False)
        return requester

    def revoke_all_memberships(self, session_id: int) -> int:
        """Revoke all active memberships for a session (called on close/force-close).

        Returns the count of memberships revoked.
        """
        now = self._utc_now()
        memberships = DriverSessionMembership.query.filter_by(
            session_id=session_id,
            status=DriverSessionMembershipStatus.ACTIVE,
        ).all()
        for m in memberships:
            m.status = DriverSessionMembershipStatus.REVOKED
            m.left_at = now
        return len(memberships)

    @staticmethod
    def _active_drivers_query():
        """Users who may work as a driver right now.

        Holds the `delivery_driver` role through users.role OR staff_roles
        (`StaffService.staff_role_member_filter`, the one spelling), has an
        ACTIVE account, and has no DeliveryPerson row or an active one: the
        reading `StaffService.assert_delivery_person_active` applies at the
        staff-bot door, so nobody listed here is locked out of the bot.
        """
        from business_app.services.staff_service import StaffService

        return User.query.outerjoin(DeliveryPerson, DeliveryPerson.user_id == User.id).filter(
            StaffService.staff_role_member_filter("delivery_driver"),
            User.status == UserStatus.ACTIVE,
            or_(DeliveryPerson.id.is_(None), DeliveryPerson.is_active.is_(True)),
        )

    def _transfer_recipients_query(self, sender_driver_id: int):
        """The ONE definition of "may receive bottles from this sender".

        `list_transfer_recipients` lists it and `initiate_bottle_transfer` guards
        with it, so the picker never offers a driver the send refuses. A
        co-driver on the sender's own session is left out: same truck.
        """
        query = self._active_drivers_query().filter(User.id != sender_driver_id)
        sender_session = self.get_open_session(sender_driver_id)
        if sender_session is not None:
            truck_mates = select(DriverSessionMembership.member_driver_id).where(
                DriverSessionMembership.session_id == sender_session.id,
                DriverSessionMembership.status == DriverSessionMembershipStatus.ACTIVE,
            )
            query = query.filter(~User.id.in_(truck_mates))
        return query

    def list_transfer_recipients(self, sender_driver_id: int) -> List[Dict[str, Any]]:
        """Every driver this sender may transfer bottles to, with their session state.

        `bottle_status` is published here so the bot renders the hint without
        re-deriving it: `own_session`, `co_driver` or `none`.
        """
        sender_driver_id = int(sender_driver_id)
        drivers = (
            self._transfer_recipients_query(sender_driver_id).order_by(User.first_name, User.last_name, User.id).all()
        )
        ids = [driver.id for driver in drivers]
        owners = {
            row.driver_user_id
            for row in DriverBottleSession.query.filter(
                DriverBottleSession.driver_user_id.in_(ids),
                DriverBottleSession.status == DriverBottleSessionStatus.OPEN,
            )
        }
        members = {
            row.member_driver_id
            for row in DriverSessionMembership.query.filter(
                DriverSessionMembership.member_driver_id.in_(ids),
                DriverSessionMembership.status == DriverSessionMembershipStatus.ACTIVE,
            )
        }
        return [
            {
                "user_id": driver.id,
                "name": driver.full_name,
                "phone": driver.phone,
                "bottle_status": (
                    "own_session" if driver.id in owners else "co_driver" if driver.id in members else "none"
                ),
            }
            for driver in drivers
        ]

    def list_eligible_co_drivers(self, owner_driver_id: int) -> List[Dict[str, Any]]:
        """Drivers who can be invited to ``owner_driver_id``'s open session.

        Eligibility: active delivery driver, not the owner, no own open session,
        and not currently a member of any other session. Encapsulated here so
        the staff API stays free of direct ``User.query`` access (boundary rule
        enforced by ``test_api_boundary_coupling_scores_do_not_regress``).
        """
        owner_session = self.get_open_session(owner_driver_id)
        if not owner_session:
            raise ConflictError(
                "You must have an open bottle session to invite co-drivers",
                error_code="BOTTLE_SESSION_REQUIRED_TO_INVITE",
            )

        drivers = self._active_drivers_query().filter(User.id != owner_driver_id).all()

        eligible: List[Dict[str, Any]] = []
        for driver in drivers:
            if self.get_open_session(driver.id):
                continue
            if self.get_active_membership(driver.id):
                continue
            eligible.append(
                {
                    "user_id": driver.id,
                    "name": f"{driver.first_name or ''} {driver.last_name or ''}".strip(),
                    "phone": driver.phone,
                }
            )
        return eligible

    # ------------------------------------------------------------------
    # Order binding & capacity enforcement
    # ------------------------------------------------------------------

    def bind_order_to_session(
        self,
        session_id: int,
        order_id: int,
        *,
        accepted_by_driver_id: int = None,
    ) -> DriverBottleSessionOrder:
        """Attach an order to a session. Idempotent — safe to call multiple times.

        accepted_by_driver_id: the driver who actually accepted the order.
        May differ from the session owner when a co-driver (member) accepts.
        """
        logger.info(
            f"[BOTTLE] bind_order_to_session session={session_id} order={order_id} accepted_by={accepted_by_driver_id}"
        )
        existing = DriverBottleSessionOrder.query.filter_by(order_id=order_id).first()
        if existing:
            if existing.session_id != session_id:
                logger.warning(
                    f"[BOTTLE] bind_order_to_session CONFLICT order={order_id} already bound to session={existing.session_id}, requested session={session_id}"  # noqa: E501
                )
                raise ConflictError(f"Order {order_id} is already bound to session {existing.session_id}")
            logger.info(
                f"[BOTTLE] bind_order_to_session order={order_id} already bound to session={session_id} (idempotent)"
            )
            return existing  # already bound to this session

        binding = DriverBottleSessionOrder(
            session_id=session_id,
            order_id=order_id,
            accepted_by_driver_id=accepted_by_driver_id,
        )
        db.session.add(binding)
        db.session.flush()
        logger.info(f"[BOTTLE] bind_order_to_session OK binding_id={binding.id}")
        return binding

    def rebind_order_to_session(
        self,
        order_id: int,
        new_session_id: int,
        *,
        accepted_by_driver_id: int = None,
    ) -> DriverBottleSessionOrder:
        """Move an existing order binding to a different session (carry-over).

        Used when an order accepted under a now-closed session is carried
        forward and delivered under the driver's new open session. Updates the
        existing ``DriverBottleSessionOrder`` row in place so the UNIQUE
        ``order_id`` invariant holds; falls back to creating a binding if none
        exists yet. Idempotent when the order is already on ``new_session_id``.

        Unlike :meth:`bind_order_to_session` (which refuses to move a binding),
        this is the deliberate cross-session migration path: the bottle tally
        follows the open session the driver is actually operating under.
        """
        binding = DriverBottleSessionOrder.query.filter_by(order_id=order_id).first()
        if binding is None:
            return self.bind_order_to_session(new_session_id, order_id, accepted_by_driver_id=accepted_by_driver_id)
        if binding.session_id == new_session_id:
            return binding
        old_session_id = binding.session_id
        binding.session_id = new_session_id
        if accepted_by_driver_id is not None:
            binding.accepted_by_driver_id = accepted_by_driver_id
        db.session.flush()
        logger.info(
            "[BOTTLE] rebind_order_to_session order=%s session %s→%s accepted_by=%s",
            order_id,
            old_session_id,
            new_session_id,
            accepted_by_driver_id,
        )
        return binding

    def unbind_order(self, order_id: int) -> bool:
        """Remove an order's bottle-session binding (e.g. when the delivery is
        returned to the pool and no driver owns it). Idempotent; returns True if
        a binding row was deleted. The order rebinds on the next assignment."""
        binding = DriverBottleSessionOrder.query.filter_by(order_id=order_id).first()
        if binding is None:
            return False
        db.session.delete(binding)
        db.session.flush()
        logger.info("[BOTTLE] unbind_order order=%s removed binding from session=%s", order_id, binding.session_id)
        return True

    def trip_shortfall(
        self, session: DriverBottleSession, trip_units: TripUnits, *, exclude_delivery_id: Optional[int] = None
    ) -> List[Shortfall]:
        """The lines of `trip_units` the crew's free figures cannot cover: the 18.9 L bottles first, then each unit
        product by id. Empty when everything is covered. One read of `free_on_trip`."""
        free = self.free_on_trip(session, exclude_delivery_id=exclude_delivery_id)
        short: List[Shortfall] = []
        required = int(trip_units.bottles)
        if required > free.bottles:
            short.append(Shortfall(None, None, free.bottles, required, required - free.bottles))
        for product_id in sorted(trip_units.units):
            required = trip_units.units[product_id]
            available = free.units.get(product_id, 0)
            if required > available:
                product = db.session.get(Product, product_id)
                short.append(Shortfall(product_id, product.name, available, required, required - available))
        return short

    def assert_order_within_session_capacity(
        self, session: DriverBottleSession, trip_units: TripUnits, *, exclude_delivery_id: Optional[int] = None
    ) -> None:
        """Raise `BOTTLE_SESSION_CAPACITY_EXCEEDED` from the first line the crew's free figures cannot cover.

        `details` carries the numbers the staff bot prints ("4 left, 5 needed, 1 short"); a unit line adds the
        product's id and name. `exclude_delivery_id` keeps a stop from counting against itself when it is already
        promised.
        """
        shortfalls = self.trip_shortfall(session, trip_units, exclude_delivery_id=exclude_delivery_id)
        if not shortfalls:
            return
        first = shortfalls[0]
        details = {"available": first.available, "required": first.required, "shortfall": first.shortfall}
        if first.product_id is None:
            what = "full bottle(s)"
        else:
            what = f"{first.product_name} unit(s)"
            details = {"product_id": first.product_id, "product_name": first.product_name, **details}
        raise ValidationError(
            f"Session {session.id} only has {first.available} {what} available; cannot deliver {first.required}.",
            details=details,
            error_code="BOTTLE_SESSION_CAPACITY_EXCEEDED",
        )

    @staticmethod
    def _strict_enforcement_enabled() -> bool:
        """Whether session-invariant violations should raise (strict) or warn (legacy)."""
        try:
            return bool(current_app.config.get("BOTTLE_SESSION_ENFORCEMENT_STRICT", False))
        except RuntimeError:
            return False

    # The moves out of ASSIGNED that put the order on the truck: Picked up, or a jump past it.
    _PROGRESS_FROM_ASSIGNED = frozenset(
        {DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT, DeliveryStatus.ARRIVED, DeliveryStatus.DELIVERED}
    )

    @classmethod
    def _progress_counts_bottles(cls, from_status: DeliveryStatus, to_status: DeliveryStatus) -> bool:
        """Whether this move must find the crew's free full bottles covering the order (spec A6, A8).

        Two steps count, whether or not the order is already bound: leaving ASSIGNED (Picked up, or straight to
        On the way), and entering the promised set (On the way), which is where the bottles are reserved. An
        ASSIGNED -> IN_TRANSIT jump is both, and both are the one check. Arrived and Delivered of a stop already
        on its way are not counted again: its bottles are already promised.
        """
        leaving_assigned = from_status == DeliveryStatus.ASSIGNED and to_status in cls._PROGRESS_FROM_ASSIGNED
        entering_promised = (
            to_status in DELIVERY_BOTTLES_PROMISED_STATES and from_status not in DELIVERY_BOTTLES_PROMISED_STATES
        )
        return leaving_assigned or entering_promised

    def _lock_crew_delivery_persons(self, session: DriverBottleSession) -> None:
        """Lock every crew member's DeliveryPerson row, in user-id order, until this transaction ends, and make
        the count that follows reload the trip's full-bottle tallies and its unit lines.

        Two taps that would promise the same full bottles (two co-drivers, or one driver twice) queue here, so
        the later one counts the earlier one's stop. This is the first rung of the spec's lock order (2026-11-18
        §6.2): DeliveryPerson rows → Order row → product rows → trip rows (a transfer: DeliveryPerson rows → transfer
        row → trip rows). A trip row lock may follow it in the same transaction (a Delivered tallies the trip later);
        no path locks a DeliveryPerson row after a trip row. The lock is a no-op on SQLite.

        The session was read before this lock, and the lock may have waited on a crew member's delivery. A
        delivery commits two things at once: its stop leaves the stops on their way, and its bottles leave the
        trip's tallies. The count reads them in two statements. So the tallies are expired here and reload in
        `free_full_bottles_for_session`, after it has read the stops. A delivery that commits before the stops
        are read is then in both (`free_on_trip` reads the stops first, then the tallies and the unit lines). One
        that commits between the two reads is in the tallies only: that
        under-counts by its bottles (a refusal the driver can retry), and never promises them twice. Some
        completions (an admin's Delivered on the Orders page) write no DeliveryPerson row, so they do not wait for
        this lock and can commit at any point of the count. The reload is a plain SELECT and locks nothing.
        """
        self._lock_delivery_persons(self.crew_driver_ids(session))
        db.session.expire(session, [*DriverBottleSession.FULL_ON_BOARD_TALLIES, "unit_lines"])

    @staticmethod
    def _lock_delivery_persons(user_ids: Iterable[int], *, key_share: bool = False) -> None:
        """Lock one row per driver until this transaction ends, in user-id order: the first rung of the spec's lock
        order (DeliveryPerson rows, then the trip). It is his DeliveryPerson row, FOR UPDATE. A driver with none (a
        staff account given the driver role without a delivery profile, whom the staff bot still lets in) has his users
        row locked instead, FOR NO KEY UPDATE, so rows that only point at him never wait on it: his open, his join and
        a count or resolve that opens him a trip still queue on one row. A no-op on SQLite.

        `key_share=True` takes the DeliveryPerson row FOR KEY SHARE instead (the users row is unchanged): it still
        queues behind, and holds off, the FOR UPDATE above, but never a plain UPDATE of the row (his counters, his
        position, his cash collected), which paths that hold an Order or payment row write. For a holder that only
        needs his open and his join kept out (an order edit's reopen check, `OrderEditService._lock_bound_driver`)."""
        ids = sorted({int(user_id) for user_id in user_ids})
        if not ids:
            return
        profiled = {
            user_id
            for (user_id,) in db.session.query(DeliveryPerson.user_id)
            .filter(DeliveryPerson.user_id.in_(ids))
            .order_by(DeliveryPerson.user_id)
            .with_for_update(key_share=key_share, read=key_share)
        }
        without_profile = [user_id for user_id in ids if user_id not in profiled]
        if without_profile:
            (
                db.session.query(User.id)
                .filter(User.id.in_(without_profile))
                .order_by(User.id)
                .with_for_update(key_share=True)
                .all()
            )

    def assert_driver_can_progress_delivery(
        self,
        delivery: "Delivery",
        *,
        to_status: DeliveryStatus,
        enforce_capacity: bool = True,
    ) -> Optional[DriverBottleSession]:
        """Guard called before a delivery moves on to ``to_status`` after assignment.

        Ensures the order is delivered under an OPEN bottle session whose
        binding follows the session the driver is currently operating under, so
        bottle counts tally against the load the order is physically on. Orders
        with nothing for a trip (no returnable bottle, no counted unit) need no session and return ``None``.

        Role since the assignment SSOT landed: ``DeliveryAssignmentService.
        assign_driver`` now binds the order to the driver's session at
        assignment time, so the common case is already bound here. This guard is
        the *post-assignment backstop* — it (re)binds when the binding is absent
        or stale because the driver had no open session at assignment time, or
        rotated/closed their session between assignment and progress. It is no
        longer the primary fix for missing-binding-at-assignment.

        Free full bottles (spec A6, A8): when the move leaves ASSIGNED or enters
        On the way (``_progress_counts_bottles``), the crew's free full bottles,
        not counting this stop, must cover the order, or
        ``BOTTLE_SESSION_CAPACITY_EXCEEDED`` is raised. Bound or not: the order
        being on the trip already does not skip the count. A move into the
        promised set first locks the crew's DeliveryPerson rows, so two taps
        cannot promise the same bottles. ``enforce_capacity=False`` (admin status
        moves, spec A9) skips the count but not the trip requirement below.

        Carry-over / late-bind: whenever the driver has an open session, the
        order is (re)bound onto it. This covers
        both an order accepted under a now-closed/different session (carry-over)
        and an order that was never bound at all because it was assigned outside
        the bot-accept flow — admin assignment and ``auto_assign_delivery_task``
        set the driver without creating a binding (late-bind). Either way the
        order lives on the session the driver is physically operating under.

        Only when the driver has *no* open session at all is this a hard error:
        under strict enforcement (``BOTTLE_SESSION_ENFORCEMENT_STRICT``) it
        raises ``BOTTLE_SESSION_REQUIRED``; in legacy mode it logs and returns
        ``None`` so the legacy flow is unaffected. Returns the session the order
        is (now) bound to, or ``None`` when no enforcement applies.
        """
        order = getattr(delivery, "order", None)
        if not order:
            return None
        trip_units = self.trip_units_for_order(order)
        if not trip_units.any:
            return None

        strict = self._strict_enforcement_enabled()

        def _violation(message: str, error_code: str) -> None:
            if strict:
                raise ValidationError(message, error_code=error_code)
            logger.warning(
                "[BOTTLE] (legacy) %s order=%s delivery=%s code=%s",
                message,
                order.id,
                delivery.id,
                error_code,
            )

        if delivery.delivery_person_id is None:
            _violation(
                f"Delivery {delivery.id} has no driver assigned; " "cannot validate bottle session.",
                "BOTTLE_SESSION_REQUIRED",
            )
            return None

        binding = DriverBottleSessionOrder.query.filter_by(order_id=order.id).first()
        bound = DriverBottleSession.query.get(binding.session_id) if binding else None
        effective = self.get_effective_session(delivery.delivery_person_id)

        if effective is not None:
            if to_status in DELIVERY_BOTTLES_PROMISED_STATES:
                self._lock_crew_delivery_persons(effective)
            if enforce_capacity and self._progress_counts_bottles(delivery.status, to_status):
                self.assert_order_within_session_capacity(effective, trip_units, exclude_delivery_id=delivery.id)

        # Happy path: the order is already on the driver's current open session.
        if (
            binding is not None
            and bound is not None
            and bound.status == DriverBottleSessionStatus.OPEN
            and effective is not None
            and effective.id == bound.id
        ):
            return bound

        # (Re)bind the order onto the driver's current open session. One path
        # covers two shapes that both mean "the order needs to live on the
        # session the driver is physically operating under":
        #   * carry-over — the order was bound under a different / now-closed
        #     session, but the driver has since opened (or joined) another, and
        #   * late-bind — the order was never bound at all because it was
        #     assigned outside the bot-accept flow (admin assignment /
        #     auto_assign_delivery_task create no binding), then surfaced to a
        #     driver who has an open session.
        # Capacity was judged above, for the moves that count bottles.
        # rebind_order_to_session creates the binding when none exists yet, so
        # both shapes share this code path.
        if effective is not None:
            previous_session_id = binding.session_id if binding else None
            self.rebind_order_to_session(
                order.id,
                effective.id,
                accepted_by_driver_id=delivery.delivery_person_id,
            )
            logger.info(
                "[BOTTLE] %s order=%s delivery=%s session=%s→%s",
                "carry-over" if binding is not None else "late-bind",
                order.id,
                delivery.id,
                previous_session_id,
                effective.id,
            )
            return effective

        # The driver has no open session at all — they must open a new one
        # before they can progress (and thereby take ownership of) this order.
        if binding is None:
            _violation(
                f"Order {order.id} has no bottle-session binding and driver "
                f"{delivery.delivery_person_id} has no open bottle session; open a "
                "session to continue delivering this order.",
                "BOTTLE_SESSION_REQUIRED",
            )
        else:
            _violation(
                f"Order {order.id}'s bound session {binding.session_id} is not open and "
                f"driver {delivery.delivery_person_id} has no open bottle session; open a "
                "new session to continue delivering this order.",
                "BOTTLE_SESSION_REQUIRED",
            )
        return None

    # Orders whose binding is historical: kept for the closed trip's tally, never moved or dropped.
    TERMINAL_BINDING_ORDER_STATUSES = (OrderStatus.DELIVERED, OrderStatus.CANCELLED, OrderStatus.RETURNED)

    @staticmethod
    def order_binding_is_historical(order) -> bool:
        """True when the order is finished, so its session binding must be left exactly as it is."""
        return order.status in BottleTrackingService.TERMINAL_BINDING_ORDER_STATUSES

    @staticmethod
    def _open_binding_criteria(session_id: int) -> tuple:
        """The one rule for an open binding: on this session, its order not in a terminal status. The session is a
        condition on the binding row itself, so a DELETE that waited on a row another transaction moved re-checks it
        on the moved row; the order's status is read as of the statement."""
        return (
            DriverBottleSessionOrder.session_id == session_id,
            DriverBottleSessionOrder.order_id.in_(
                select(Order.id).where(Order.status.notin_(BottleTrackingService.TERMINAL_BINDING_ORDER_STATUSES))
            ),
        )

    @staticmethod
    def _open_bindings_query_for_session(session_id: int):
        """Bindings on this session whose order is not in a terminal status."""
        return DriverBottleSessionOrder.query.filter(*BottleTrackingService._open_binding_criteria(session_id))

    @staticmethod
    def _open_bindings_count_for_session(session_id: int) -> int:
        """Count bindings on this session whose order is not in a terminal status."""
        return BottleTrackingService._open_bindings_query_for_session(session_id).count()

    def _release_open_bindings_for_session(self, session_id: int) -> list:
        """Release (unbind) every non-terminal order bound to this session so it can
        close. Terminal (delivered/cancelled/returned) bindings are KEPT for the
        historical tally. Returns the released order ids.

        Deleting the binding — rather than leaving it on the now-closed session — is
        deliberate: each released order re-binds to the driver's next open session via
        the late-bind guard when next progressed. The delivery-time tally credits only
        an OPEN trip, read under its lock with the binding re-read
        (``OrderService._record_delivery_bottles``), so a Delivered that waited on this
        close finds the binding gone and adds nothing to the sealed counters.

        One DELETE whose WHERE is `_open_binding_criteria`, never a read and then a delete by id: a binding the
        progress guard of the order's driver is moving onto his own trip right now (`rebind_order_to_session`) makes
        the DELETE wait for that move, and Postgres then re-checks the WHERE on the moved row, which is no longer on
        this session, so it stays where the guard put it (delivered or not). A delete by id removed it from his trip.
        """
        released = db.session.execute(
            delete(DriverBottleSessionOrder)
            .where(*self._open_binding_criteria(session_id))
            .returning(DriverBottleSessionOrder.order_id)
            .execution_options(synchronize_session="fetch")
        )
        return sorted(released.scalars().all())

    def update_session_delivery_tally(
        self,
        driver_user_id: int,
        *,
        bottles_delivered: int = 0,
        bottles_collected: int = 0,
        units: Optional[Dict[int, int]] = None,
    ) -> Optional[DriverBottleSession]:
        """Increment session delivery/collection counters after each ledger write.

        Uses the driver's *effective* session — their own OPEN session if they
        have one, otherwise the session they have joined as a co-driver member.
        No-op if the driver has no effective session (backward-compatible).
        `units` ({product_id: n}) goes to the session's unit lines (`add_to_session_tallies`).
        The session is read without a lock; `add_to_session_tallies` locks it and adds nothing to one a close sealed
        meanwhile. Returns the session tallied, or None.
        """
        logger.info(
            "[BOTTLE] update_session_delivery_tally driver=%s delivered=%s collected=%s units=%s",
            driver_user_id,
            bottles_delivered,
            bottles_collected,
            units,
        )
        session = self.get_effective_session(driver_user_id)
        if not session:
            logger.info(
                "[BOTTLE] update_session_delivery_tally driver=%s no effective session, skipping", driver_user_id
            )
            return None
        if not self.add_to_session_tallies(
            session, delivered=bottles_delivered, collected=bottles_collected, units=units
        ):
            return None
        logger.info(
            "[BOTTLE] update_session_delivery_tally OK session=%s delivered+%s=%s collected+%s=%s",
            session.id,
            bottles_delivered,
            session.bottles_delivered,
            bottles_collected,
            session.bottles_collected_from_customers,
        )
        return session

    @staticmethod
    def add_to_session_tallies(
        session: DriverBottleSession,
        *,
        delivered: int = 0,
        collected: int = 0,
        units: Optional[Dict[int, int]] = None,
    ) -> bool:
        """Add to the trip's Delivered and collected-empties tallies, and to its unit lines' Delivered
        (`units`, {product_id: n}), in SQL (`tally = tally + n`), never below 0.

        Every writer of these tallies goes through here. Two crew members' Delivered taps on one truck lock
        different rows until they reach this one, so a total read earlier and written back overwrote the other tap's
        bottles. The UPDATE adds to what the row holds once its lock is granted. The trip row is locked first
        (`_lock_session`), also when nothing is added to its own columns: a Delivered of units only then serializes
        with a send or a close of the trip like any other (spec 2026-11-18 §6.2). A product the trip has no line for
        (the trip did not count it when it opened) is logged and skipped. The UPDATEs do not touch the loaded objects,
        so what they wrote is expired: the trip's tallies, `delivered` on each line written (the one object the
        identity map holds for that row) and the trip's `unit_lines`; the next read in this transaction loads the
        sums. Lock order: a Delivered has already written the deliverer's DeliveryPerson row and locked the product
        rows; this takes the trip row's lock after them.

        A tally lands only on an open trip (spec 2026-11-18 §6.5): a closed trip's figures are sealed by the typed
        return. The status is read under the lock, so a close or a force-close that committed while this waited (a
        try-out hand-off read the trip open before it) is seen: nothing is added, it is logged, and False is returned.
        True when the trip is open and tallied.
        """
        locked = BottleTrackingService._lock_session(session.id)
        if locked is None or locked.status != DriverBottleSessionStatus.OPEN:
            logger.warning(
                "[BOTTLE] trip=%s is %s; not tallied: delivered=%s collected=%s units=%s",
                session.id,
                "gone" if locked is None else locked.status.value,
                delivered,
                collected,
                units,
            )
            return False
        values = {}
        for column, amount in (
            (DriverBottleSession.bottles_delivered, delivered),
            (DriverBottleSession.bottles_collected_from_customers, collected),
        ):
            if amount:
                total = column + int(amount)
                values[column.key] = case((total < 0, 0), else_=total)
        if values:
            db.session.execute(
                update(DriverBottleSession)
                .where(DriverBottleSession.id == session.id)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            db.session.expire(session, list(values))
        if not units:
            return True
        for product_id, amount in sorted(units.items()):
            if not amount:
                continue
            line = BottleTrackingService._unit_line(session, product_id)
            if line is None:
                logger.warning("[BOTTLE] trip=%s has no line for product=%s; not tallied", session.id, product_id)
                continue
            total = DriverBottleSessionUnit.delivered + int(amount)
            db.session.execute(
                update(DriverBottleSessionUnit)
                .where(DriverBottleSessionUnit.id == line.id)
                .values(delivered=case((total < 0, 0), else_=total))
                .execution_options(synchronize_session=False)
            )
            db.session.expire(line, ["delivered"])
        db.session.expire(session, ["unit_lines"])
        return True

    # ------------------------------------------------------------------
    # Driver-to-driver bottle transfers
    # ------------------------------------------------------------------

    def initiate_bottle_transfer(
        self,
        sender_driver_id: int,
        receiver_driver_id: int,
        *,
        full_quantity: int = 0,
        empty_quantity: int = 0,
        units: Optional[Dict[int, int]] = None,
        send_all: bool = False,
        notes: str = None,
    ) -> DriverBottleTransfer:
        """Record the hand-off, then tell the receiver.

        The push is queued only after `_record_bottle_transfer` has committed,
        so a refused send tells nobody anything.
        """
        from business_app.services import bottle_notifications

        transfer = self._record_bottle_transfer(
            sender_driver_id,
            receiver_driver_id,
            full_quantity=full_quantity,
            empty_quantity=empty_quantity,
            units=units,
            send_all=send_all,
            notes=notes,
        )
        bottle_notifications.notify_transfer_received(transfer)
        return transfer

    @transactional
    def _record_bottle_transfer(
        self,
        sender_driver_id: int,
        receiver_driver_id: int,
        *,
        full_quantity: int = 0,
        empty_quantity: int = 0,
        units: Optional[Dict[int, int]] = None,
        send_all: bool = False,
        notes: str = None,
    ) -> DriverBottleTransfer:
        """Sender hands full bottles, empties and one-way units to another driver, mid-route (spec 2026-11-09 T1-T3,
        2026-11-18 §6.7).

        `units` is `{product_id: quantity}`, each a line on the sender's trip (TRIP_UNIT_NOT_COUNTED otherwise); a
        line left out sends none. `send_all` sends everything on the truck as read under the lock: the full bottles
        promised to his own On-the-way stops included (T3), and every line's transferable. Otherwise each number is
        capped by its own line (T2): BOTTLE_TRANSFER_EXCEEDS_INVENTORY names the `kind` that is too high (`full`,
        `empty`, or `product` with its `product_id` and `product_name`). A total of 0 over every line is
        BOTTLE_TRANSFER_NOTHING_TO_SEND. Every line leaves the sender's trip at once (pessimistic), each into its own
        counter; the receiver's are credited when he counts them. The transfer keeps one unit line for every line on
        the sender's trip, declared 0 when nothing of it was sent, so the receiver answers exactly those.

        The sender's session row is locked (and re-read) first, and its lines are read after that lock, so two sends,
        or a send racing a delivery, cannot both spend the same bottles or units. A transfer path never locks a
        DeliveryPerson row or a product row (the lock order).
        """
        # JWT identities arrive as str (`sub`); every column compared below is int.
        sender_driver_id = int(sender_driver_id)
        receiver_driver_id = int(receiver_driver_id)
        units = dict(units or {})
        if sender_driver_id == receiver_driver_id:
            raise ValidationError("Sender and receiver cannot be the same driver")
        if full_quantity < 0 or empty_quantity < 0 or any(quantity < 0 for quantity in units.values()):
            raise ValidationError("Transfer quantities cannot be negative")

        sender_session = self._get_open_session_or_raise(sender_driver_id, lock=True)
        receiver = self._transfer_recipients_query(sender_driver_id).filter(User.id == receiver_driver_id).first()
        if receiver is None:
            raise ValidationError(
                "This driver can't receive bottles right now",
                error_code="BOTTLE_TRANSFER_RECEIVER_INVALID",
            )
        db.session.expire(sender_session, ["unit_lines"])
        lines = {line.product_id: line for line in sender_session.unit_lines}

        if send_all:
            full_quantity, empty_quantity = sender_session.transferable_full, sender_session.transferable_empties
            units = {product_id: line.transferable for product_id, line in lines.items()}
        for product_id in units:
            if product_id not in lines:
                raise ValidationError(
                    f"Product {product_id} is not counted on this trip",
                    details={"product_id": product_id},
                    error_code="TRIP_UNIT_NOT_COUNTED",
                )
        if full_quantity + empty_quantity + sum(units.values()) == 0:
            raise ValidationError(
                "Nothing to transfer: every line is 0.",
                error_code="BOTTLE_TRANSFER_NOTHING_TO_SEND",
            )
        for kind, requested, available in (
            ("full", full_quantity, sender_session.transferable_full),
            ("empty", empty_quantity, sender_session.transferable_empties),
        ):
            if requested > available:
                raise ValidationError(
                    f"Cannot transfer {requested} {kind} bottle(s); the sender has {available} on the truck.",
                    error_code="BOTTLE_TRANSFER_EXCEEDS_INVENTORY",
                    details={"kind": kind, "requested": int(requested), "available": int(available)},
                )
        for product_id, requested in sorted(units.items()):
            line = lines[product_id]
            if requested > line.transferable:
                raise ValidationError(
                    f"Cannot transfer {requested} of {line.product.name}; the sender has {line.transferable} on the "
                    "trip.",
                    error_code="BOTTLE_TRANSFER_EXCEEDS_INVENTORY",
                    details={
                        "kind": "product",
                        "product_id": product_id,
                        "product_name": line.product.name,
                        "requested": int(requested),
                        "available": int(line.transferable),
                    },
                )

        sender_session.bottles_transferred_out = (sender_session.bottles_transferred_out or 0) + full_quantity
        sender_session.empties_transferred_out = (sender_session.empties_transferred_out or 0) + empty_quantity
        for product_id, sent in units.items():
            if sent:
                lines[product_id].transferred_out = (lines[product_id].transferred_out or 0) + sent

        transfer = DriverBottleTransfer(
            sender_session_id=sender_session.id,
            sender_driver_id=sender_driver_id,
            receiver_driver_id=receiver_driver_id,
            declared_quantity=full_quantity,
            declared_empty_quantity=empty_quantity,
            status=DriverBottleTransferStatus.PENDING,
            notes=notes,
            unit_lines=[
                DriverBottleTransferUnit(product_id=product_id, declared=units.get(product_id, 0))
                for product_id in sorted(lines)
            ],
        )
        db.session.add(transfer)
        db.session.flush()
        return transfer

    @staticmethod
    def lock_trip_table(mode: str) -> None:
        """`LOCK TABLE driver_bottle_sessions IN <mode> MODE` until this transaction ends; nothing on SQLite.

        `"SHARE"` is the product trip-setting save's (after its product row), `"ROW EXCLUSIVE"` every trip-creating
        path's (before it reads the setting). The two modes conflict, so a save and a trip open queue behind each
        other and each reads what the other committed.
        """
        if mode not in ("SHARE", "ROW EXCLUSIVE"):
            raise ValueError(f"Unknown trips-table lock mode: {mode!r}")
        if db.session.get_bind().dialect.name != "postgresql":
            return
        db.session.execute(text(f"LOCK TABLE {DriverBottleSession.__tablename__} IN {mode} MODE"))

    @staticmethod
    def open_trip_owners() -> List[Dict[str, Any]]:
        """Every OPEN trip, by id, with its owner (`details.open_trips` of TRIP_UNIT_SETTING_BLOCKED)."""
        sessions = (
            DriverBottleSession.query.options(joinedload(DriverBottleSession.driver))
            .filter(DriverBottleSession.status == DriverBottleSessionStatus.OPEN)
            .order_by(DriverBottleSession.id)
            .all()
        )
        return [
            {
                "session_id": session.id,
                "session_ref": session.session_ref,
                "driver_user_id": session.driver_user_id,
                "driver_name": session.driver.full_name,
            }
            for session in sessions
        ]

    @staticmethod
    def _lock_session(session_id: int) -> DriverBottleSession:
        """The session row, locked (`SESSION_ROW_LOCK`) until this transaction ends and re-read, so a write based on its
        tallies sees the last committed ones. A no-op lock on SQLite."""
        return db.session.get(DriverBottleSession, session_id, with_for_update=SESSION_ROW_LOCK, populate_existing=True)

    @staticmethod
    def _unit_line(
        session: DriverBottleSession, product_id: int, *, create: bool = False
    ) -> Optional[DriverBottleSessionUnit]:
        """THE way to get, or with `create` make, a trip's line for one product: read fresh from the database.

        The caller holds the trip's row lock (`_lock_session`, `get_open_session(lock=True)` or
        `get_effective_session(lock=True)`) or inserted this trip in the current transaction, so no other writer
        makes the same line meanwhile; `uq_dbsu_session_product` is the backstop. A path that needs every line of a
        trip after the lock expires `session.unit_lines` and reads it again.
        """
        line = (
            DriverBottleSessionUnit.query.filter_by(session_id=session.id, product_id=product_id)
            .populate_existing()
            .one_or_none()
        )
        if line is None and create:
            line = DriverBottleSessionUnit(
                product_id=product_id, loaded=0, delivered=0, transferred_in=0, transferred_out=0
            )
            session.unit_lines.append(line)
            db.session.flush()
        return line

    @classmethod
    def _sender_line(
        cls, sender_session: DriverBottleSession, transfer: DriverBottleTransfer, product_id: int
    ) -> DriverBottleSessionUnit:
        """The sender's trip line a transfer line was debited from (`_unit_line`, under the trip's lock): the resolve
        and the reject correct its `transferred_out`. A transfer line exists only for a line the sender's trip had, so
        a missing one is a broken invariant, never a user error."""
        line = cls._unit_line(sender_session, product_id)
        if line is None:
            raise RuntimeError(f"Transfer {transfer.id}: sender trip {sender_session.id} has no line {product_id}")
        return line

    def _write_line_returns(
        self, session: DriverBottleSession, units_returned: Dict[int, int], *, every_line: bool
    ) -> None:
        """Write each of the trip's lines' `returned` from `units_returned` ({product_id: count}), the lines read after
        the trip's lock. `every_line`: the driver's close must answer each line (BOTTLE_SESSION_RETURN_INCOMPLETE); the
        office force-close takes one left out as 0. A product with no line on the trip is TRIP_UNIT_NOT_COUNTED."""
        db.session.expire(session, ["unit_lines"])
        lines = {line.product_id: line for line in session.unit_lines}
        missing = [product_id for product_id in lines if product_id not in units_returned]
        if every_line and missing:
            raise ValidationError(
                "Type the return for every line on the trip",
                details={"missing_product_ids": missing},
                error_code="BOTTLE_SESSION_RETURN_INCOMPLETE",
            )
        for product_id, quantity in units_returned.items():
            if product_id not in lines:
                raise ValidationError(
                    f"Product {product_id} is not counted on this trip",
                    details={"product_id": product_id},
                    error_code="TRIP_UNIT_NOT_COUNTED",
                )
            if quantity < 0:
                raise ValidationError(f"units_returned[{product_id}] cannot be negative")
            self._assert_bottle_count_within(f"units_returned[{product_id}]", quantity, BOTTLE_RETURN_COLUMN_CEILING)
        for product_id, line in lines.items():
            line.returned = units_returned.get(product_id, 0)

    @staticmethod
    def _recompute_discrepancies(session: DriverBottleSession) -> None:
        """Both discrepancies of a trip that is closed: the 18.9 L one, and each line's that has its return written (a
        line without one has nothing to compare). Never added together."""
        session.compute_discrepancy()
        for line in session.unit_lines:
            if line.returned is not None:
                line.compute_discrepancy()

    @staticmethod
    def _lock_transfer(transfer_id: int) -> Optional[DriverBottleTransfer]:
        """The transfer row, locked and re-read before its status is judged: a second tap on Confirm (or a second
        resolve) waits here and then sees what the first one wrote. Lock order (spec 2026-11-18 §6.2): DeliveryPerson
        rows → transfer row → trip rows. A count and a resolve lock the receiver's DeliveryPerson row before this one;
        a reject takes no DeliveryPerson row; every transfer path locks its trip rows after it."""
        return db.session.get(DriverBottleTransfer, transfer_id, with_for_update=True, populate_existing=True)

    def confirm_bottle_transfer(
        self,
        transfer_id: int,
        receiver_driver_id: int,
        *,
        confirmed_full: Optional[int] = None,
        confirmed_empty: Optional[int] = None,
        confirmed_units: Optional[Dict[int, int]] = None,
        confirm_as_declared: bool = False,
        notes: str = None,
    ) -> TransferCountOutcome:
        """Record the receiver's count (`_record_transfer_count`), then act on it once it has committed.

        A refused count commits nothing and does nothing after. Each step after the commit is best-effort
        (`_after_commit_step`), because the count stands whatever they do: a failed step is logged and the session
        rolled back, the steps after it still run, and the receiver still gets the transfer he counted.
          - CONFIRMED: the sender's trip closes if the hand-off emptied it (T4), then the sender is told the count and
            whether his trip closed.
          - DISPUTED: the dispute is posted to the admins' Slack alerts channel, then the sender is told both counts,
            and that an admin will check only if Slack took the post (T5; Slack per owner decision 2026-11-10).
          - Full bottles credited to the receiver (either outcome): the pool is offered to auto-assign again (§4.2).
        Returns the transfer, read again with both drivers, and whether Slack took the dispute alert. That last
        read is a step too: if it fails, the transfer as read right after the commit stands in for it, so the receiver
        still gets the count he made.
        """
        self._record_transfer_count(
            transfer_id,
            receiver_driver_id,
            confirmed_full=confirmed_full,
            confirmed_empty=confirmed_empty,
            confirmed_units=confirmed_units,
            confirm_as_declared=confirm_as_declared,
            notes=notes,
        )
        as_committed, admins_alerted = self._after_transfer_counted(transfer_id)
        transfer = self._after_commit_step(
            transfer_id, "final read", lambda: self._committed_transfer(transfer_id), as_committed
        )
        return TransferCountOutcome(transfer, admins_alerted)

    @staticmethod
    def _committed_transfer(transfer_id: int) -> DriverBottleTransfer:
        """The transfer as committed, with both drivers and its unit lines (with their products) loaded in the same
        read: what a step after the count or reject, and `serialize_bottle_transfer`, need. The commit (and any failed
        step's rollback) expired the rows read before it, so nothing relies on those."""
        return db.session.get(
            DriverBottleTransfer,
            transfer_id,
            options=[
                joinedload(DriverBottleTransfer.sender_driver),
                joinedload(DriverBottleTransfer.receiver_driver),
                selectinload(DriverBottleTransfer.unit_lines).joinedload(DriverBottleTransferUnit.product),
            ],
            populate_existing=True,
        )

    @staticmethod
    def _after_commit_step(transfer_id: int, step: str, run, default=None):
        """One step after a transfer's count or reject has committed, isolated from the rest: its result, or `default`
        if it raised. Raising would answer the receiver 500 for a count or reject that stands (and his bot would retry
        into "not pending"). The rollback clears a session a failed step left unusable, so the next step and the
        route's answer start clean; nothing committed is left to lose. The log line names the step and the commit,
        not the kind of write. `best_effort_step` is the order flows' twin: it returns nothing and its log line names
        an order."""
        try:
            return run()
        except Exception:  # noqa: BLE001 - after commit; the count or reject stands
            db.session.rollback()
            logger.exception("[BOTTLE] %s after transfer %s's commit failed", step, transfer_id)
            return default

    @staticmethod
    def _kept_as_read(transfer: DriverBottleTransfer) -> DriverBottleTransfer:
        """Detach the transfer, both its drivers and its unit lines with their products, every column loaded, so no
        later commit or rollback expires them and nothing reloads them: the answer `confirm_bottle_transfer` falls back
        on when its final read fails. The steps read the transfer afresh, so none of them uses this copy."""
        rows = (
            transfer,
            transfer.sender_driver,
            transfer.receiver_driver,
            *transfer.unit_lines,
            *(line.product for line in transfer.unit_lines),
        )
        for row in rows:
            if row is not None and row in db.session:
                db.session.expunge(row)
        return transfer

    def _after_transfer_counted(self, transfer_id: int) -> Tuple[Optional[DriverBottleTransfer], bool]:
        """The post-commit steps of `confirm_bottle_transfer`, in the order the sender's message depends on. Each reads
        the transfer afresh (`_committed_transfer`) inside its own guard (`_after_commit_step`). Returns the transfer as
        read right after the commit (`_kept_as_read`; None if that read failed) and whether the admins' Slack alerts
        channel took the dispute alert."""
        from business_app.services import bottle_notifications

        def step(name, run, default=None):
            return self._after_commit_step(transfer_id, name, run, default)

        counted = step("read", lambda: self._kept_as_read(self._committed_transfer(transfer_id)))
        if counted is None:
            return None, False
        confirmed, credited_full = counted.status == DriverBottleTransferStatus.CONFIRMED, counted.confirmed_quantity
        admins_alerted = False
        if confirmed:
            auto_closed = step(
                "auto-close", lambda: self._auto_close_sender_if_emptied(self._committed_transfer(transfer_id)), False
            )
            step(
                "sender message",
                lambda: bottle_notifications.notify_transfer_outcome_to_sender(
                    self._committed_transfer(transfer_id), auto_closed=auto_closed
                ),
            )
        else:
            alerted = step(
                "dispute alert",
                lambda: bottle_notifications.notify_admins_transfer_disputed(self._committed_transfer(transfer_id)),
                False,
            )
            admins_alerted = alerted is True
            step(
                "sender message",
                lambda: bottle_notifications.notify_transfer_outcome_to_sender(
                    self._committed_transfer(transfer_id), auto_closed=False, admins_alerted=admins_alerted
                ),
            )
        if self._gains_capacity(credited_full or 0):
            step("pool re-offer", self._reoffer_pool_after_capacity_gain)
        return counted, admins_alerted

    @staticmethod
    def _gains_capacity(full_bottles: int) -> bool:
        """THE one answer to "did this write give a crew full bottles worth re-offering the pool" after a transfer's
        count or reject: only full bottles do (spec 2026-11-18 §6.7); empties or one-way units alone re-offer nothing,
        and those orders wait for the stale sweep."""
        return full_bottles > 0

    def reject_bottle_transfer(self, transfer_id: int, receiver_driver_id: int) -> TransferRejectOutcome:
        """The receiver rejects a pending transfer (`_record_transfer_reject`), then the steps after its commit.

        A refused reject commits nothing and does nothing after. Each step after the commit is best-effort
        (`_after_commit_step`, as for a count), because the reject stands whatever they do; see
        `_after_transfer_rejected`. A reject never auto-closes anything. Returns the transfer, read again with both
        drivers and its lines (the read right after the commit stands in if that read fails), whether the sender's trip
        had closed, and whether Slack took the alert.
        """
        sender_trip_was_closed = self._record_transfer_reject(transfer_id, receiver_driver_id)
        as_committed, admins_alerted = self._after_transfer_rejected(transfer_id, sender_trip_was_closed)
        transfer = self._after_commit_step(
            transfer_id, "final read", lambda: self._committed_transfer(transfer_id), as_committed
        )
        return TransferRejectOutcome(transfer, sender_trip_was_closed, admins_alerted)

    def _after_transfer_rejected(
        self, transfer_id: int, sender_trip_was_closed: bool
    ) -> Tuple[Optional[DriverBottleTransfer], bool]:
        """The post-commit steps of `reject_bottle_transfer`, each reading the transfer afresh (`_committed_transfer`)
        inside its own guard (`_after_commit_step`): the admins' Slack alert (every time), the sender's push, then the
        pool re-offer when full bottles went back to a trip that is OPEN (`_gains_capacity`; a closed trip's bottles
        are at the depot). The alert and the push run even when the read right after the commit failed, since each
        reads the transfer itself; only the re-offer needs that read (its full-bottle count) and is skipped without it.
        Returns the transfer as read right after the commit (`_kept_as_read`; None if that read failed) and whether
        Slack took the alert."""
        from business_app.services import bottle_notifications

        def step(name, run, default=None):
            return self._after_commit_step(transfer_id, name, run, default)

        rejected = step("read", lambda: self._kept_as_read(self._committed_transfer(transfer_id)))
        alerted = step(
            "reject alert",
            lambda: bottle_notifications.notify_admins_transfer_rejected(
                self._committed_transfer(transfer_id), sender_trip_was_closed=sender_trip_was_closed
            ),
            False,
        )
        step(
            "sender message",
            lambda: bottle_notifications.notify_transfer_rejected_to_sender(
                self._committed_transfer(transfer_id), sender_trip_was_closed=sender_trip_was_closed
            ),
        )
        if (
            rejected is not None
            and self._gains_capacity(rejected.declared_quantity or 0)
            and not sender_trip_was_closed
        ):
            step("pool re-offer", self._reoffer_pool_after_capacity_gain)
        return rejected, alerted is True

    @transactional
    def _record_transfer_reject(self, transfer_id: int, receiver_driver_id: int) -> bool:
        """The named receiver rejects a PENDING transfer (spec 2026-11-18 §6.7): every line goes back to the trip it
        left (`sender_session_id`), even a closed one. Returns whether that trip had closed (`sender_trip_was_closed`).

        Every line's `transferred_out` there drops by what it declared: the full bottles, the empties and each one-way
        unit line. A trip that is no longer OPEN gets both its discrepancies recomputed (`_recompute_discrepancies`),
        as the office resolve does: they were computed at close from the counters this reject corrects. The transfer
        becomes REJECTED with `rejected_at`; its `confirmed_*` stay empty, nothing is credited to the receiver and
        nothing opens for him. Another driver, the sender included: BOTTLE_TRANSFER_NOT_RECEIVER; a transfer already
        confirmed, disputed, resolved or rejected: BOTTLE_TRANSFER_NOT_PENDING.

        Locks, in the count's order: the transfer row before its status check, then the sender's trip row. A close
        takes DeliveryPerson → trip row and never a transfer row, so the two serialize on the trip row without a cycle:
        a close behind a reject seals the restored counters, a reject behind a close recomputes the sealed trip.
        """
        # JWT identities arrive as str (`sub`); every column compared below is int.
        receiver_driver_id = int(receiver_driver_id)
        transfer = self._lock_transfer(transfer_id)
        if not transfer:
            raise NotFoundError("Transfer not found", error_code="BOTTLE_TRANSFER_NOT_FOUND")
        if transfer.receiver_driver_id != receiver_driver_id:
            raise ConflictError(
                "Only the designated receiver can reject this transfer",
                error_code="BOTTLE_TRANSFER_NOT_RECEIVER",
            )
        if transfer.status != DriverBottleTransferStatus.PENDING:
            raise ConflictError(
                f"Transfer is already {transfer.status.value}",
                error_code="BOTTLE_TRANSFER_NOT_PENDING",
            )

        sender_session = self._lock_session(transfer.sender_session_id)
        declared_full, declared_empty = transfer.declared_quantity, transfer.declared_empty_quantity or 0
        sender_session.bottles_transferred_out = (sender_session.bottles_transferred_out or 0) - declared_full
        sender_session.empties_transferred_out = (sender_session.empties_transferred_out or 0) - declared_empty
        for line in transfer.unit_lines:
            self._sender_line(sender_session, transfer, line.product_id).transferred_out -= line.declared
        db.session.flush()
        sender_trip_was_closed = sender_session.status != DriverBottleSessionStatus.OPEN
        if sender_trip_was_closed:
            db.session.expire(sender_session, ["unit_lines"])
            self._recompute_discrepancies(sender_session)

        transfer.status = DriverBottleTransferStatus.REJECTED
        transfer.rejected_at = self._utc_now()
        db.session.flush()
        return sender_trip_was_closed

    @transactional
    def _auto_close_sender_if_emptied(self, transfer: DriverBottleTransfer) -> bool:
        """Close the sender's trip if this confirmed hand-off left nothing for it to do (spec 2026-11-09 T4).

        Its own transaction, after the count has committed. Every condition is judged here, under the sender session's
        row lock, against what the rows say now: never from the count's snapshot. Closes only if all hold:
          - the trip the bottles left is still OPEN;
          - nothing is left on it (`transferable_any`: no full bottle, no empty, nothing on any unit line);
          - the crew holds no delivery in the driver's active list (Assigned, Picked up, On the way, Arrived): the
            workload count `StaffService.get_active_delivery_counts` keeps over `ACTIVE_DELIVERY_STATUSES`;
          - nobody co-drives it (ACTIVE memberships);
          - no other transfer from it waits for its receiver's count.
        Otherwise the trip stays open, silently: the driver closes it at the warehouse as usual.

        The close is an ordinary one (bindings and memberships released, discrepancies computed) with 0 returned to the
        warehouse on every line, the sender as the closer and a note naming the transfer. The sender is not moved to
        the depot: the hand-off happened mid-route. Lock order: the one session row, and no DeliveryPerson row. Returns
        whether it closed.
        """
        from business_app.services.staff_service import StaffService

        session = self._lock_session(transfer.sender_session_id)
        if session is None or session.status != DriverBottleSessionStatus.OPEN:
            return False
        db.session.expire(session, ["unit_lines"])
        if session.transferable_any:
            return False
        crew = self.crew_driver_ids(session)
        if any(StaffService.get_active_delivery_counts(crew).values()):
            return False
        if len(crew) > 1:  # the owner plus an ACTIVE co-driver
            return False
        another_pending = (
            db.session.query(DriverBottleTransfer.id)
            .filter(
                DriverBottleTransfer.sender_session_id == session.id,
                DriverBottleTransfer.status == DriverBottleTransferStatus.PENDING,
                DriverBottleTransfer.id != transfer.id,
            )
            .first()
        )
        if another_pending is not None:
            return False

        self.close_bottle_session(
            session.driver_user_id,
            0,
            units_returned={line.product_id: 0 for line in session.unit_lines},
            actor_user_id=transfer.sender_driver_id,
            notes=f"Closed automatically after transfer {transfer.transfer_ref}: nothing left on the truck.",
            anchor_at_warehouse=False,
        )
        logger.info("[BOTTLE] session=%s closed automatically after transfer %s", session.id, transfer.id)
        return True

    @transactional
    def _record_transfer_count(
        self,
        transfer_id: int,
        receiver_driver_id: int,
        *,
        confirmed_full: Optional[int] = None,
        confirmed_empty: Optional[int] = None,
        confirmed_units: Optional[Dict[int, int]] = None,
        confirm_as_declared: bool = False,
        notes: str = None,
    ) -> DriverBottleTransfer:
        """Receiver confirms (or disputes) a pending transfer, line by line (spec 2026-11-09 T5, 2026-11-18 §6.7).

        As declared: every line counts what was declared. Otherwise both 18.9 L counts and `confirmed_units`
        ({product_id: count}), whose products must be exactly the transfer's unit lines, declared 0 included
        (BOTTLE_TRANSFER_COUNT_INCOMPLETE, `details.missing_product_ids` / `details.extra_product_ids`). Only the
        transfer's lines decide what is counted: the product setting is never read here.

        Every line matches → CONFIRMED; any line differs → DISPUTED, and the receiver's truck holds what he counted
        until an admin resolves it. The receiver's state AT CONFIRM TIME decides where it lands: their own open
        session, else the session they co-drive (one truck, one load), else a session opened for them by this
        hand-off. A receiver with nothing open who counts 0 on every line opens nothing; the dispute goes to the
        admin like any other mismatch. On the landing trip each unit line's count is added to its `transferred_in`,
        the line made first when the trip lacks it; each transfer line keeps its `confirmed`.

        Locks, in the spec's order: the receiver's DeliveryPerson row first, so a join of this receiver (which locks
        that row before the trip it joins) and a trip this count opens for him serialize: behind a count that opened
        a trip the join is refused, and a count behind a join lands on the joined trip. Then the transfer row before
        its status check, then the landing session: DeliveryPerson row → transfer row → session rows.
        """
        # JWT identities arrive as str (`sub`); every column compared below is int.
        receiver_driver_id = int(receiver_driver_id)
        self._lock_delivery_persons([receiver_driver_id])
        transfer = self._lock_transfer(transfer_id)
        if not transfer:
            raise NotFoundError("Transfer not found", error_code="BOTTLE_TRANSFER_NOT_FOUND")
        if transfer.receiver_driver_id != receiver_driver_id:
            raise ConflictError(
                "Only the designated receiver can confirm this transfer",
                error_code="BOTTLE_TRANSFER_NOT_RECEIVER",
            )
        if transfer.status != DriverBottleTransferStatus.PENDING:
            raise ConflictError(
                f"Transfer is already {transfer.status.value}",
                error_code="BOTTLE_TRANSFER_NOT_PENDING",
            )
        declared = (transfer.declared_quantity, transfer.declared_empty_quantity or 0)
        declared_units = {line.product_id: line.declared for line in transfer.unit_lines}
        if confirm_as_declared:
            confirmed_full, confirmed_empty = declared
            confirmed_units = dict(declared_units)
        elif confirmed_full is None or confirmed_empty is None:
            raise ValidationError("Confirm as declared, or give both the full and the empty count")
        else:
            confirmed_units = dict(confirmed_units or {})
            missing = sorted(set(declared_units) - set(confirmed_units))
            extra = sorted(set(confirmed_units) - set(declared_units))
            if missing or extra:
                raise ValidationError(
                    "Count every line of the transfer, and only those",
                    details={"missing_product_ids": missing, "extra_product_ids": extra},
                    error_code="BOTTLE_TRANSFER_COUNT_INCOMPLETE",
                )
        if min(confirmed_full, confirmed_empty, *confirmed_units.values()) < 0:
            raise ValidationError("Confirmed counts cannot be negative")

        receiver_session = self._receiver_session_for(
            transfer, receiver_driver_id, confirmed_full + confirmed_empty + sum(confirmed_units.values())
        )

        if receiver_session is not None:
            receiver_session.bottles_transferred_in = (receiver_session.bottles_transferred_in or 0) + confirmed_full
            receiver_session.empties_transferred_in = (receiver_session.empties_transferred_in or 0) + confirmed_empty
            for line in transfer.unit_lines:
                landing_line = self._unit_line(receiver_session, line.product_id, create=True)
                landing_line.transferred_in += confirmed_units[line.product_id]
            transfer.receiver_session_id = receiver_session.id
        for line in transfer.unit_lines:
            line.confirmed = confirmed_units[line.product_id]
        transfer.confirmed_quantity = confirmed_full
        transfer.confirmed_empty_quantity = confirmed_empty
        transfer.confirmed_at = self._utc_now()
        if notes:
            transfer.notes = (transfer.notes or "") + f"\n{notes}"

        if (confirmed_full, confirmed_empty) == declared and confirmed_units == declared_units:
            transfer.status = DriverBottleTransferStatus.CONFIRMED
        else:
            transfer.status = DriverBottleTransferStatus.DISPUTED
            if notes:
                transfer.dispute_notes = notes

        db.session.flush()
        return transfer

    def _receiver_session_for(
        self, transfer: DriverBottleTransfer, receiver_driver_id: int, total: int
    ) -> Optional[DriverBottleSession]:
        """The one rule for where a transfer's bottles land (confirm and admin resolve).

        The receiver's own open session, else the session they co-drive, else a
        session opened by this hand-off; nothing to land (a total of 0 over every
        line: full, empties and each one-way unit) opens nothing.
        Confirm-time only: the receiver is on that truck right now. An admin
        resolve differs (see `admin_resolve_transfer_dispute`).

        Read under the session's row lock, held until the confirm commits: a trip
        whose close commits while this waits is no longer OPEN, so it is passed
        over and the rule lands the bottles on what the receiver has now. A
        closed trip's discrepancy is sealed; crediting it would leave it off.
        """
        session = self.get_effective_session(receiver_driver_id, lock=True)
        if session is None and total > 0:
            session = self._open_session_for_transfer(receiver_driver_id, transfer)
        return session

    def _open_session_for_transfer(
        self,
        receiver_driver_id: int,
        transfer: DriverBottleTransfer,
        *,
        actor_user_id: int = None,
    ) -> DriverBottleSession:
        """A receiver with nothing open gets a session that starts with the hand-off.

        `actor_user_id` is who caused the open when it is not the receiver (an
        admin resolving a dispute); it is recorded as `loaded_by_user_id`.

        Not `open_bottle_session`: nothing was loaded at the warehouse (0 is the
        true load here, and that method refuses it), and the hand-off happens
        mid-route, so the driver's stored position is NOT moved to the depot.
        The receiver closes it at the warehouse like any other session; the
        partial unique index on one open session per driver still guards a
        concurrent open.

        Like every path that creates a trip, it takes the trips table in ROW EXCLUSIVE mode before it reads which
        products the trip counts (a save of that setting waits, or is read), and gives the new trip one line at 0
        per counted product. A count or a resolve reads the setting nowhere else; the lines the hand-off carries are
        made by its landing (`_unit_line(..., create=True)`), so a product both counted and carried has one line.
        """
        self.lock_trip_table("ROW EXCLUSIVE")
        counted = self.counted_trip_products()
        session = DriverBottleSession(
            driver_user_id=receiver_driver_id,
            bottles_loaded=0,
            status=DriverBottleSessionStatus.OPEN,
            loaded_by_user_id=actor_user_id or receiver_driver_id,
            started_at=self._utc_now(),
            notes=f"Opened by transfer {transfer.transfer_ref}",
        )
        db.session.add(session)
        db.session.flush()
        for product in counted:
            self._unit_line(session, product.id, create=True)
        return session

    @transactional
    def admin_resolve_transfer_dispute(
        self,
        transfer_id: int,
        actor_user_id: int,
        *,
        resolved_full: int,
        resolved_empty: int,
        resolution_notes: str,
        resolved_units: Optional[Dict[int, int]] = None,
    ) -> DriverBottleTransfer:
        """Admin arbitrates a disputed transfer: the full bottles, the empties and each one-way unit line that really
        moved.

        `resolved_units` ({product_id: count}) answers every line the transfer carries and no other
        (BOTTLE_TRANSFER_COUNT_INCOMPLETE, `details.missing_product_ids` / `details.extra_product_ids`).

        Adjusts every component on the sender's session (declared → resolved) and the receiver's (confirmed →
        resolved). A transfer that was never credited (the receiver counted 0 on every line with nothing open) lands the
        resolved counts on the receiver's OWN open session, else one opened for them with the admin as
        `loaded_by_user_id`, and only when the total over every line is above 0. Unlike confirm it never uses the
        session the receiver co-drives: the hand-off is in the past, and that truck's owner was not party to it. So
        while he co-drives one, a resolve that would open him a trip is refused instead (409
        BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE, `details` = `_open_refusal_details` of that trip, from
        `_assert_free_to_open`, the open's own rule): a trip of his own would be his second place to be. The refusal
        comes before any write, so the transfer stays DISPUTED with nothing changed; the office resolves once he has
        left that trip, and a resolve of 0 on every line, which opens nothing, goes through.

        A touched session that is no longer OPEN gets its discrepancies recomputed (`_recompute_discrepancies`): they
        were computed at close from the counts this resolve corrects. Each transfer line keeps what the admin resolved.

        Locks, in the spec's order: the receiver's DeliveryPerson row first (a trip this resolve opens for him and a
        join of his serialize, as in the count), then the transfer row before its status check, then the sessions in id
        order: DeliveryPerson row → transfer row → session rows.
        """
        resolved_units = dict(resolved_units or {})
        # The receiver of a transfer never changes, so it is read before the locks that need it.
        peeked = db.session.get(DriverBottleTransfer, transfer_id)
        if not peeked:
            raise NotFoundError("Transfer not found")
        self._lock_delivery_persons([peeked.receiver_driver_id])
        transfer = self._lock_transfer(transfer_id)
        if transfer.status != DriverBottleTransferStatus.DISPUTED:
            raise ConflictError("Can only resolve DISPUTED transfers")
        if not resolution_notes or not resolution_notes.strip():
            raise ValidationError("resolution_notes is required")
        declared_units = {line.product_id: line.declared for line in transfer.unit_lines}
        missing = sorted(set(declared_units) - set(resolved_units))
        extra = sorted(set(resolved_units) - set(declared_units))
        if missing or extra:
            raise ValidationError(
                "Resolve every line of the transfer, and only those",
                details={"missing_product_ids": missing, "extra_product_ids": extra},
                error_code="BOTTLE_TRANSFER_COUNT_INCOMPLETE",
            )
        if min(resolved_full, resolved_empty, *resolved_units.values()) < 0:
            raise ValidationError("Resolved counts cannot be negative")
        total = resolved_full + resolved_empty + sum(resolved_units.values())

        landing_id = transfer.receiver_session_id
        if landing_id is None and total > 0:
            own = self.get_open_session(transfer.receiver_driver_id)
            if own is not None:
                landing_id = own.id
            else:
                # This resolve will open him a trip: refused, before anything is written, while he is on one.
                self._assert_free_to_open(transfer.receiver_driver_id)
        locked = {
            session_id: self._lock_session(session_id)
            for session_id in sorted({transfer.sender_session_id, landing_id} - {None})
        }

        # The sender's session: replace what he declared with what really left his truck.
        sender_session = locked[transfer.sender_session_id]
        declared_full, declared_empty = transfer.declared_quantity, transfer.declared_empty_quantity or 0
        sender_session.bottles_transferred_out = (
            (sender_session.bottles_transferred_out or 0) + resolved_full - declared_full
        )
        sender_session.empties_transferred_out = (
            (sender_session.empties_transferred_out or 0) + resolved_empty - declared_empty
        )
        for product_id, declared in declared_units.items():
            self._sender_line(sender_session, transfer, product_id).transferred_out += (
                resolved_units[product_id] - declared
            )
        touched = [sender_session]

        # The receiver's: replace what he counted with what really arrived. Never credited: he counted nothing.
        credited = transfer.receiver_session_id is not None
        if credited:
            landing = locked[landing_id]
            credited_full, credited_empty = transfer.confirmed_quantity or 0, transfer.confirmed_empty_quantity or 0
        elif total > 0:
            landing = locked.get(landing_id) or self._open_session_for_transfer(
                transfer.receiver_driver_id, transfer, actor_user_id=actor_user_id
            )
            credited_full, credited_empty = 0, 0
        else:
            landing = None
        if landing is not None:
            landing.bottles_transferred_in = (landing.bottles_transferred_in or 0) + resolved_full - credited_full
            landing.empties_transferred_in = (landing.empties_transferred_in or 0) + resolved_empty - credited_empty
            for line in transfer.unit_lines:
                landing_line = self._unit_line(landing, line.product_id, create=True)
                landing_line.transferred_in += resolved_units[line.product_id] - (
                    (line.confirmed or 0) if credited else 0
                )
            transfer.receiver_session_id = landing.id
            touched.append(landing)
        for line in transfer.unit_lines:
            line.resolved = resolved_units[line.product_id]

        db.session.flush()
        for session in touched:
            if session.status != DriverBottleSessionStatus.OPEN:
                db.session.expire(session, ["unit_lines"])
                self._recompute_discrepancies(session)

        transfer.confirmed_quantity = resolved_full
        transfer.confirmed_empty_quantity = resolved_empty
        transfer.status = DriverBottleTransferStatus.RESOLVED
        transfer.resolved_at = self._utc_now()
        transfer.resolved_by_user_id = actor_user_id
        transfer.resolution_notes = resolution_notes.strip()
        db.session.flush()
        return transfer

    # ------------------------------------------------------------------
    # Session read operations
    # ------------------------------------------------------------------

    @staticmethod
    def get_session_detail(session_id: int) -> Optional[DriverBottleSession]:
        """Fetch a session with orders, transfers, and memberships pre-loaded."""
        return DriverBottleSession.query.options(
            joinedload(DriverBottleSession.driver),
            joinedload(DriverBottleSession.session_orders)
            .joinedload(DriverBottleSessionOrder.order)
            .joinedload(Order.user),
            joinedload(DriverBottleSession.session_orders)
            .joinedload(DriverBottleSessionOrder.order)
            .joinedload(Order.order_items)
            .joinedload(OrderItem.product),
            joinedload(DriverBottleSession.session_orders).joinedload(DriverBottleSessionOrder.accepted_by_driver),
            joinedload(DriverBottleSession.transfers_out).joinedload(DriverBottleTransfer.receiver_driver),
            joinedload(DriverBottleSession.transfers_in).joinedload(DriverBottleTransfer.sender_driver),
            joinedload(DriverBottleSession.memberships).joinedload(DriverSessionMembership.member_driver),
            joinedload(DriverBottleSession.unit_lines).joinedload(DriverBottleSessionUnit.product),
        ).get(session_id)

    @staticmethod
    def get_driver_sessions(
        driver_user_id: int,
        page: int = 1,
        per_page: int = 20,
        status: str = None,
    ) -> Dict:
        """Get paginated session history for a specific driver."""
        query = DriverBottleSession.query.filter_by(driver_user_id=driver_user_id).options(
            joinedload(DriverBottleSession.driver)
        )

        if status:
            query = query.filter(DriverBottleSession.status == DriverBottleSessionStatus(status))

        query = query.order_by(DriverBottleSession.started_at.desc())
        total = query.count()
        items = query.offset((page - 1) * per_page).limit(per_page).all()
        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if per_page else 0,
        }

    @staticmethod
    def get_all_sessions(
        page: int = 1,
        per_page: int = 20,
        driver_user_id: int = None,
        status: str = None,
        only_discrepancies: bool = False,
        start_date: date = None,
        end_date: date = None,
    ) -> Dict:
        """Get paginated session list for admin with optional filters."""
        query = DriverBottleSession.query.options(
            joinedload(DriverBottleSession.driver),
            selectinload(DriverBottleSession.unit_lines).joinedload(DriverBottleSessionUnit.product),
        )

        if driver_user_id:
            query = query.filter(DriverBottleSession.driver_user_id == driver_user_id)
        if status:
            query = query.filter(DriverBottleSession.status == DriverBottleSessionStatus(status))
        if only_discrepancies:
            query = query.filter(DriverBottleSession.has_discrepancy)
        if start_date:
            query = query.filter(func.date(DriverBottleSession.started_at) >= start_date)
        if end_date:
            query = query.filter(func.date(DriverBottleSession.started_at) <= end_date)

        query = query.order_by(DriverBottleSession.started_at.desc())
        total = query.count()
        items = query.offset((page - 1) * per_page).limit(per_page).all()
        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if per_page else 0,
        }

    @staticmethod
    def get_pending_transfers_for_driver(
        driver_user_id: int,
    ) -> List[DriverBottleTransfer]:
        """Return transfers pending confirmation by this driver (as receiver).

        Rows for `serialize_bottle_transfer`, which reads both drivers' names and the unit lines' products.
        """
        return (
            DriverBottleTransfer.query.filter_by(
                receiver_driver_id=driver_user_id,
                status=DriverBottleTransferStatus.PENDING,
            )
            .options(
                joinedload(DriverBottleTransfer.sender_driver),
                joinedload(DriverBottleTransfer.receiver_driver),
                joinedload(DriverBottleTransfer.sender_session),
                selectinload(DriverBottleTransfer.unit_lines).joinedload(DriverBottleTransferUnit.product),
            )
            .order_by(DriverBottleTransfer.sent_at.desc())
            .all()
        )

    @staticmethod
    def get_all_transfers(
        page: int = 1,
        per_page: int = 20,
        status: str = None,
        driver_user_id: int = None,
    ) -> Dict:
        """Get paginated transfer list for admin: the rows the staff routes publish, with names and the full / empty
        split (`serialize_bottle_transfer`)."""
        from business_app.serializers.bottle_serializers import serialize_bottle_transfer

        query = DriverBottleTransfer.query.options(
            joinedload(DriverBottleTransfer.sender_driver),
            joinedload(DriverBottleTransfer.receiver_driver),
            selectinload(DriverBottleTransfer.unit_lines).joinedload(DriverBottleTransferUnit.product),
        )

        if status:
            query = query.filter(DriverBottleTransfer.status == DriverBottleTransferStatus(status))
        if driver_user_id:
            query = query.filter(
                or_(
                    DriverBottleTransfer.sender_driver_id == driver_user_id,
                    DriverBottleTransfer.receiver_driver_id == driver_user_id,
                )
            )

        query = query.order_by(DriverBottleTransfer.sent_at.desc())
        total = query.count()
        items = query.offset((page - 1) * per_page).limit(per_page).all()
        return {
            "items": [serialize_bottle_transfer(t) for t in items],
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if per_page else 0,
        }
