"""What the reorder-reminder rule reads about a customer, loaded in a few batched queries.

A customer is a linked-identity cluster (`users.canonical_customer_id`), else one user, and every
fact is read across the cluster (spec §4). Place facts follow D18: a delivery to ANY address of a
place group the customer's addresses belong to restarts their clock (the supply anchor), and
anyone's order still on its way to such an address may pause them. An order with no delivery
address counts only for the person who placed it.

Nothing here decides anything: `eligibility.evaluate` is the rule. The loaders return frozen
dataclasses, so the rule stays pure and table-testable (spec §5). Facts are AS OF `today`: a
delivery or an order dated after it does not exist yet, so a run can be replayed for a past day.
One customer's facts failing never costs the others theirs (contract amendment 17).
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple

from sqlalchemy import String, case, cast, literal_column, select
from sqlalchemy.exc import DBAPIError

from business_app import db
from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.reorder_reminder import ACTIVE_EPISODE_STATUSES, ReorderReminderEpisode
from business_app.models.subscription import Subscription
from business_app.models.user import User
from business_app.services.customer_link_service import CustomerLinkService
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.services.order_service import OrderService
from business_app.services.reorder_reminders.settings import EffectiveSetting, ReorderReminderSettingsService
from business_app.services.sales.outlet_service import OutletService
from business_app.utils.delivery_cadence import delivery_instants_by_customer, place_supply_by_customer
from business_app.utils.local_windows import local_date
from business_app.utils.order_timing import delivered_order_instants_select
from business_app.utils.state_validators import ACTIVE_ORDER_STATUSES
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import LIVE_SUBSCRIPTION_STATUSES
from shared.user_names import greeting_name

logger = logging.getLogger(__name__)

_CHUNK = 500
Delivered = Tuple[int, datetime]


@dataclass(frozen=True)
class MemberFacts:
    """One linked account, as the recipient ranking and the channel rule read it."""

    user_id: int
    status: str
    is_staff: bool
    can_order: bool
    telegram_id: Optional[str]
    is_bot_active: bool
    telegram_blocked_at: Optional[datetime]
    last_bot_interaction: Optional[datetime]
    email: Optional[str]
    created_at: datetime
    preferred_language: Optional[str]
    greeting_name: Optional[str]


@dataclass(frozen=True)
class LiveOrderFacts:
    """An order in ACTIVE_ORDER_STATUSES: the customer's own, or anyone's to one of its place addresses."""

    order_id: int
    user_id: int
    delivery_address_id: Optional[int]
    created_on: date
    delivery_date: Optional[date]
    awaiting_new_date: bool
    is_own: bool


@dataclass(frozen=True)
class DormantOutletFacts:
    """A dormant outlet on a member's account (D21), with the agent whose plan it is on."""

    outlet_id: int
    name: str
    user_id: int
    dormant_since: Optional[datetime]
    agent_user_id: Optional[int]


@dataclass(frozen=True)
class ReminderCustomer:
    key: str
    member_ids: Tuple[int, ...]
    members: Tuple[MemberFacts, ...]
    own_deliveries: Tuple[Delivered, ...]
    supply: Optional[Delivered]
    place_group_ids: Tuple[int, ...]
    live_orders: Tuple[LiveOrderFacts, ...]
    has_live_subscription: bool
    has_any_outlet: bool
    dormant_outlets: Tuple[DormantOutletFacts, ...]
    setting: EffectiveSetting

    @property
    def own_anchor(self) -> Optional[Delivered]:
        """The newest delivered order across the cluster's own orders."""
        return self.own_deliveries[0] if self.own_deliveries else None


def load_customers(
    *, today: date, user_ids: Optional[Iterable[int]] = None, errors: Optional[list] = None
) -> List[ReminderCustomer]:
    """Every customer the daily run may act on, or the clusters of `user_ids`.

    With no `user_ids`: everyone with an own delivered order (E3), plus the owners of every
    active episode's anchor order, so a lapse whose anchor stopped counting (a returned order)
    still meets its customer and is closed.

    Per-customer isolation (contract amendment 17, master Review Focus #4): the facts are read in
    batches, and when a batch raises (a corrupt address group, a deleted outlet user) each
    customer is assembled again on its own. A customer whose facts still raise is logged with its
    key, appended to `errors` as `(key, exc)` when a list is given, and skipped; every other
    customer is returned. A batch of one customer IS that customer, so its fault is recorded once,
    never retried. The happy path keeps its fixed query count; only a failing load pays
    per-customer queries. Read-only, so it never rolls back: a caller may hold work of its own.

    A database fault (`DBAPIError`) is raised, never retried: Postgres has aborted the transaction,
    so every retry would fail too. The per-customer retry covers faults in a customer's data.
    """
    seeds = _seed_user_ids() if user_ids is None else {int(user_id) for user_id in user_ids}
    clusters = _clusters(seeds)
    if not clusters:
        return []
    try:
        return _assemble(clusters, today=today)
    except DBAPIError:
        raise
    except Exception as exc:  # noqa: BLE001 - find the customer at fault and keep the rest
        if len(clusters) == 1:
            _skip(next(iter(clusters)), exc, errors)
            return []
        logger.warning("reorder_reminders: a batched fact load failed; loading customer by customer", exc_info=True)
    customers: List[ReminderCustomer] = []
    for key, ids in clusters.items():
        try:
            customers.extend(_assemble({key: ids}, today=today))
        except DBAPIError:
            raise
        except Exception as exc:  # noqa: BLE001 - one customer's facts must never cost the others theirs
            _skip(key, exc, errors)
    return customers


def _skip(key: str, exc: Exception, errors: Optional[list]) -> None:
    """Record one customer's fact fault: logged with its key, reported when a list is given."""
    logger.error("reorder_reminders: facts for customer %s failed and were skipped: %s", key, exc, exc_info=exc)
    if errors is not None:
        errors.append((key, exc))


def load_customer_for_user(user_id: int, *, today: date) -> Optional[ReminderCustomer]:
    """The customer `user_id` belongs to, or None when that user does not exist.

    One customer has nothing to isolate: a fault in its facts is raised, never read as "no such
    user", so the send-time re-check cannot record a wrong refusal for it.
    """
    errors: list = []
    customers = load_customers(today=today, user_ids=[user_id], errors=errors)
    if errors:
        raise errors[0][1]
    for customer in customers:
        if user_id in customer.member_ids:
            return customer
    return None


def id_chunks(ids: Iterable[int]):
    """The ids, sorted and cut into batches that fit one `IN` list (the bind-parameter ceiling)."""
    ordered = sorted(ids)
    for offset in range(0, len(ordered), _CHUNK):
        yield ordered[offset : offset + _CHUNK]


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    return ensure_utc(value) if value is not None else None


def customer_key(user_id: int, canonical_id: Optional[int]) -> str:
    """THE identity of a customer (spec §4): the linked-identity cluster when the account is linked,
    else the account itself. Whoever counts customers (the loader's clusters, the worklist's
    switch-off metric) keys them here; the prefix keeps a canonical id from meeting a user id."""
    return f"c:{canonical_id}" if canonical_id is not None else f"u:{user_id}"


def customer_key_sql(user):
    """`customer_key` as a SQL expression over `user` (`User` or an alias of it), for a query that
    groups or joins by customer (the Analytics inactive-customers list). The same two classes and
    the same prefixes, so a canonical id still never meets a user id;
    tests/integration/test_inactive_customers_cluster_delivery.py pins the two spellings together.
    The prefixes are literal SQL (not bind parameters) so the same expression can sit in a SELECT
    and its GROUP BY."""
    return case(
        (
            user.canonical_customer_id.isnot(None),
            literal_column("'c:'", String) + cast(user.canonical_customer_id, String),
        ),
        else_=literal_column("'u:'", String) + cast(user.id, String),
    )


def _seed_user_ids() -> Set[int]:
    delivered = delivered_order_instants_select().subquery()
    seeds = {user_id for (user_id,) in db.session.execute(select(delivered.c.user_id).distinct())}
    anchor_owners = (
        db.session.query(Order.user_id)
        .join(ReorderReminderEpisode, ReorderReminderEpisode.anchor_order_id == Order.id)
        .filter(ReorderReminderEpisode.status.in_(ACTIVE_EPISODE_STATUSES))
        .distinct()
    )
    seeds.update(user_id for (user_id,) in anchor_owners)
    return seeds


def cluster_member_ids(seed_ids: Iterable[int]) -> Set[int]:
    """Every account in the clusters of `seed_ids` (spec §4): `_clusters`, the loader's one rule for
    "all accounts sharing a canonical customer", read in batches. The worklist search widens its
    matches with it, so it finds exactly the rows the loader builds for that customer (final review D1)."""
    return {user_id for ids in _clusters({int(user_id) for user_id in seed_ids}).values() for user_id in ids}


def _clusters(seeds: Set[int]) -> Dict[str, Tuple[int, ...]]:
    canonical_by_user: Dict[int, Optional[int]] = {}
    for chunk in id_chunks(seeds):
        for user_id, canonical_id in db.session.query(User.id, User.canonical_customer_id).filter(User.id.in_(chunk)):
            canonical_by_user[user_id] = canonical_id
    members: Dict[str, Set[int]] = {}
    for chunk in id_chunks({cid for cid in canonical_by_user.values() if cid is not None}):
        rows = db.session.query(User.id, User.canonical_customer_id).filter(User.canonical_customer_id.in_(chunk))
        for user_id, canonical_id in rows:
            members.setdefault(customer_key(user_id, canonical_id), set()).add(user_id)
    for user_id, canonical_id in canonical_by_user.items():
        if canonical_id is None:
            members[customer_key(user_id, None)] = {user_id}
    return {key: tuple(sorted(ids)) for key, ids in sorted(members.items())}


def _users(user_ids: Iterable[int]) -> Dict[int, User]:
    users: Dict[int, User] = {}
    for chunk in id_chunks(user_ids):
        for user in User.query.filter(User.id.in_(chunk)).all():
            users[user.id] = user
    return users


def _member(user: User) -> MemberFacts:
    return MemberFacts(
        user_id=user.id,
        status=getattr(user.status, "value", user.status),
        is_staff=bool(user.is_staff_user),
        can_order=OrderService.customer_can_order(user),
        telegram_id=user.telegram_id,
        is_bot_active=bool(user.is_bot_active),
        telegram_blocked_at=_aware(user.telegram_blocked_at),
        last_bot_interaction=_aware(user.last_bot_interaction),
        email=user.email,
        created_at=_aware(user.created_at),
        preferred_language=user.preferred_language,
        greeting_name=greeting_name(
            first_name=user.first_name,
            full_name=user.full_name,
            company_name=user.company_name,
            is_entity=bool(user.is_entity_user),
        ),
    )


def _newer(own_anchor: Optional[Delivered], place_supply: Optional[Delivered]) -> Optional[Delivered]:
    """D18: the supply anchor is the newer of the own anchor and the place supply; the own anchor
    wins a tie, so with no newer place delivery the two are the same order."""
    if place_supply is None:
        return own_anchor
    if own_anchor is None or place_supply[1] > own_anchor[1]:
        return place_supply
    return own_anchor


def _users_with_live_subscription(user_ids: Iterable[int]) -> Set[int]:
    statuses = list(LIVE_SUBSCRIPTION_STATUSES)
    found: Set[int] = set()
    for chunk in id_chunks(user_ids):
        rows = (
            db.session.query(Subscription.user_id)
            .filter(Subscription.user_id.in_(chunk), Subscription.status.in_(statuses))
            .distinct()
        )
        found.update(user_id for (user_id,) in rows)
    return found


def _live_order_rows(criterion):
    awaiting = case((OrderScheduleService.awaiting_new_date_filter(), True), else_=False).label("awaiting_new_date")
    return (
        db.session.query(
            Order.id.label("order_id"),
            Order.user_id,
            Order.delivery_address_id,
            Order.created_at,
            Order.delivery_date,
            awaiting,
        )
        .outerjoin(Delivery, Delivery.order_id == Order.id)
        .filter(Order.status.in_(ACTIVE_ORDER_STATUSES), criterion)
        .all()
    )


def _live_orders(
    clusters: Mapping[str, Tuple[int, ...]], place_addresses_by_key: Mapping[str, Tuple[int, ...]], *, today: date
) -> Dict[str, Tuple[LiveOrderFacts, ...]]:
    owner_key = {user_id: key for key, ids in clusters.items() for user_id in ids}
    keys_by_address: Dict[int, List[str]] = {}
    for key, address_ids in place_addresses_by_key.items():
        for address_id in address_ids:
            keys_by_address.setdefault(address_id, []).append(key)
    rows = {}
    for chunk in id_chunks(owner_key):
        for row in _live_order_rows(Order.user_id.in_(chunk)):
            rows[row.order_id] = row
    for chunk in id_chunks(keys_by_address):
        for row in _live_order_rows(Order.delivery_address_id.in_(chunk)):
            rows[row.order_id] = row
    by_key: Dict[str, List[LiveOrderFacts]] = {}
    for row in rows.values():
        created_on = local_date(row.created_at)
        if created_on > today:
            continue
        own_key = owner_key.get(row.user_id)
        keys = set(keys_by_address.get(row.delivery_address_id, ())) if row.delivery_address_id is not None else set()
        if own_key is not None:
            keys.add(own_key)
        for key in keys:
            by_key.setdefault(key, []).append(
                LiveOrderFacts(
                    order_id=row.order_id,
                    user_id=row.user_id,
                    delivery_address_id=row.delivery_address_id,
                    created_on=created_on,
                    delivery_date=row.delivery_date,
                    awaiting_new_date=bool(row.awaiting_new_date),
                    is_own=key == own_key,
                )
            )
    return {key: tuple(sorted(items, key=lambda item: item.order_id)) for key, items in by_key.items()}


def _assemble(clusters: Dict[str, Tuple[int, ...]], *, today: date) -> List[ReminderCustomer]:
    all_ids = sorted({user_id for ids in clusters.values() for user_id in ids})
    users = _users(all_ids)
    own = delivery_instants_by_customer(clusters)
    groups_by_user = CustomerLinkService.place_group_ids_by_user(all_ids)
    groups_by_key = {
        key: tuple(sorted({group_id for user_id in ids for group_id in groups_by_user.get(user_id, ())}))
        for key, ids in clusters.items()
    }
    addresses_by_group = CustomerLinkService.place_group_address_ids_by_group(
        {group_id for group_ids in groups_by_key.values() for group_id in group_ids}
    )
    place_addresses_by_key = {
        key: tuple(
            sorted({address_id for group_id in group_ids for address_id in addresses_by_group.get(group_id, ())})
        )
        for key, group_ids in groups_by_key.items()
    }
    place_supply = place_supply_by_customer(place_addresses_by_key, as_of=today)
    live = _live_orders(clusters, place_addresses_by_key, today=today)
    subscribed = _users_with_live_subscription(all_ids)
    outlets = OutletService.outlet_facts_by_user(all_ids)
    settings = ReorderReminderSettingsService.effective_by_key(clusters)

    customers = []
    for key, ids in clusters.items():
        present = tuple(user_id for user_id in ids if user_id in users)
        if not present:
            continue
        own_deliveries = tuple(item for item in own.get(key, ()) if local_date(item[1]) <= today)
        customers.append(
            ReminderCustomer(
                key=key,
                member_ids=present,
                members=tuple(_member(users[user_id]) for user_id in present),
                own_deliveries=own_deliveries,
                supply=_newer(own_deliveries[0] if own_deliveries else None, place_supply.get(key)),
                place_group_ids=groups_by_key[key],
                live_orders=live.get(key, ()),
                has_live_subscription=any(user_id in subscribed for user_id in present),
                has_any_outlet=any(outlets[user_id][0] for user_id in present),
                dormant_outlets=tuple(row for user_id in present for row in outlets[user_id][1]),
                setting=settings[key],
            )
        )
    return customers
