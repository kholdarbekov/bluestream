"""The admin Lapsed-customers worklist (spec §9.1): rows, the summary and the 30-day metrics.

Read-only. Every field the page shows is computed here, so the admin UI re-derives nothing:
- the effective setting is `EffectiveSetting.to_dict(include_reason=True)` (Task 8, amendment 15),
  and `contact_restriction` comes from the one eligibility function;
- "days since" is `eligibility.days_since` (amendment 8), and `can_resolve` is the model's
  `is_resolvable` (amendment 4);
- the recipient's language is `shared.i18n_rendering.reminder_language`, the one the reminder is
  written in;
- stuck orders come from `is_in_progress` (D12);
- open debt is the COD cap's own NET figure (`CashCollectionService`), and bottles are the
  place-scoped balances (`BottleTrackingService`), deduplicated by `BottleBalance.id`;
- a row's dormant outlets are the ones the daily run recorded on its `outlet_dormant` escalation;
- the reminder history shows one outcome per step (see `_history`);
- `pending_close` is the run's own close question (`engine.close_decision`), asked now;
- the badge counts, the page header and the Analytics cards (D17) all read `summary()`: each
  figure is defined once, there, and no surface recomputes it.

The row set follows the mode (§9.1, §13) through `ReminderConfig.row_set_is_dry_run` (amendment 7):
dry-run rows in `dry_run`, live rows in `on` and `off`. The badge counts and the 30-day metrics read
the same row set (in `dry_run` the sends are the `reminder_simulated` events, §13).

A page costs a fixed number of queries, whatever its size: the customers, events, places, orders,
users, bottles and debts of the page's episodes are each read in one batch. A row whose customer
could not be loaded costs its account's cluster lookup on top (a fault path only).
"""

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func, or_, select

from business_app import db
from business_app.models.customer_link import AddressGroup
from business_app.models.order import Order
from business_app.models.reorder_reminder import (
    ACTIVE_EPISODE_STATUSES,
    CHANNELS,
    ESCALATION_FLAGS,
    FLAG_OUTLET_DORMANT,
    OUTCOMES,
    ReorderReminderEpisode,
    ReorderReminderEvent,
    is_resolvable,
)
from business_app.models.user import User, UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.services.reorder_reminders.config import ReminderConfig, current_config
from business_app.services.reorder_reminders.eligibility import REASON_BLOCKED, days_since, evaluate, is_in_progress
from business_app.services.reorder_reminders.engine import close_decision
from business_app.services.reorder_reminders.facts import (
    LiveOrderFacts,
    ReminderCustomer,
    cluster_member_ids,
    customer_key,
    id_chunks,
    load_customers,
)
from business_app.services.reorder_reminders.messaging import DETAIL_ALREADY_CLAIMED, DETAIL_BOT_BLOCKED
from business_app.services.reorder_reminders.settings import ACTOR_CUSTOMER, DETAIL_OFF
from business_app.services.sales.outlet_service import user_display_name
from business_app.utils import local_windows
from business_app.utils.order_timing import delivered_order_instants_select
from business_app.utils.timezone_utils import ensure_utc
from business_app.utils.user_search import build_user_search_filter
from shared.i18n_rendering import reminder_language

logger = logging.getLogger(__name__)

TABS = {
    "follow_up": ("escalated",),
    "reminding": ("open",),
    "paused": ("suspended",),
    "resolved": ("resolved",),
}
# The tabs the nav badge and the Analytics cards count (§9.1, D17); paused rows are not counted.
COUNTED_TABS = ("follow_up", "reminding")

# The keys of one worklist row, held to the admin UI fixture by
# tests/unit/test_lapsed_customers_ui_contract.py.
ROW_KEYS = (
    "episode_id",
    "status",
    "is_dry_run",
    "recipient",
    "phones",
    "last_delivery_at",
    "days_since",
    "usual_gap_days",
    "gap_source",
    "threshold_days",
    "due_on",
    "days_overdue",
    "step",
    "next_action_on",
    "reminders_sent",
    "last_channel",
    "last_reminder_at",
    "escalation_flag",
    "escalated_at",
    "suspended_reason",
    "suspended_at",
    "resolved_at",
    "outcome",
    "outcome_note",
    "resolved_by",
    "history",
    "reply_excerpt",
    "stuck_orders",
    "setting",
    "contact_restriction",
    "places",
    "outlets",
    "bottles_held",
    "open_debt",
    "lifetime_deliveries",
    "can_resolve",
    "can_turn_off_reminders",
    "pending_close",
)

_TAB_ORDER = {
    "reminding": (ReorderReminderEpisode.next_action_on.asc(), ReorderReminderEpisode.id.asc()),
    "paused": (ReorderReminderEpisode.suspended_at.desc(), ReorderReminderEpisode.id.desc()),
    "resolved": (ReorderReminderEpisode.resolved_at.desc(), ReorderReminderEpisode.id.desc()),
}

# One customer state, one name: the bot route's 410 (`bot_blocked`) is the block the send-time
# channel rule records as `blocked` (§12, amendment 11).
_HISTORY_DETAIL = {DETAIL_BOT_BLOCKED: REASON_BLOCKED}

# The 30-day metrics (§9.1). Live rows read `reminder_sent` where dry-run rows read
# `reminder_simulated` (§13).
_METRICS_WINDOW = timedelta(days=30)
_BLOCK_AFTER_REMINDER = timedelta(hours=72)
_SENT_KIND = {False: "reminder_sent", True: "reminder_simulated"}


def days_overdue(episode: ReorderReminderEpisode, today: date) -> int:
    """Whole local days past the due day D, never negative."""
    return max(0, (today - episode.due_on).days)


def lifetime_deliveries(customer: Optional[ReminderCustomer]) -> int:
    """The cluster's own delivered orders (§9.1 "lifetime deliveries")."""
    return len(customer.own_deliveries) if customer is not None else 0


def follow_up_sort_key(
    episode: ReorderReminderEpisode, customer: Optional[ReminderCustomer], today: date
) -> Tuple[int, int, int]:
    """*Needs follow-up* order (§9.1): lifetime deliveries, then days overdue, both descending.

    The morning digest's top five use the same key, so the email and the page agree on who comes
    first. The episode id breaks ties, so the order is total.
    """
    return (-lifetime_deliveries(customer), -days_overdue(episode, today), episode.id)


def _tab_query(cfg: ReminderConfig, tab: str):
    """The episodes of one tab: the mode's row set (amendment 7) in the tab's statuses. The tab's
    rows, its pager total and its badge count all start here, so they cannot disagree."""
    episode = ReorderReminderEpisode
    return episode.query.filter(episode.is_dry_run == cfg.row_set_is_dry_run, episode.status.in_(TABS[tab]))


def customers_by_member(user_ids: Iterable[int], today: date) -> Dict[int, ReminderCustomer]:
    """`{member user id: their customer}` for the clusters of `user_ids`, from the batched loader.

    The loader returns every listed user's cluster, out-of-scope ones included (suspended, outlet,
    admin-off, staff, no deliveries left). A cluster whose facts raise is logged by the loader and
    absent here, so its rows degrade to the episode's own columns; a database fault is raised.
    """
    ids = sorted({int(user_id) for user_id in user_ids})
    if not ids:
        return {}
    customers: Dict[int, ReminderCustomer] = {}
    for customer in load_customers(today=today, user_ids=ids):
        for member_id in customer.member_ids:
            customers[member_id] = customer
    return customers


class ReorderReminderWorklist:
    """The staff side of the lapse episodes: what the admin UI lists, counts and resolves."""

    @staticmethod
    def list_rows(
        *, tab: str, flag: Optional[str], search: Optional[str], page: int, per_page: int
    ) -> Tuple[List[dict], int]:
        """One page of worklist rows and the tab's total (§9.1).

        *Needs follow-up* sorts in Python, because lifetime deliveries are a cluster fact the
        loader computes. That costs a fixed number of queries but loads EVERY escalated row of the
        row set and its customer's facts on each request, and the set is not small: D11 escalates
        the whole historical backlog as `long_lapsed` on the first runs, so expect one row per
        long-lapsed customer until staff resolve them. Time and memory grow with that backlog; if
        the tab gets slow, store the sort key on the episode rather than page in SQL by a column
        that is not the spec's order. *Being reminded* sorts by next action, *Paused* by most
        recently paused and *Resolved* by most recently resolved, all in SQL, one page at a time.
        """
        cfg = current_config()
        today = local_windows.local_date()
        episode = ReorderReminderEpisode
        query = _tab_query(cfg, tab)
        if flag:
            query = query.filter(episode.escalation_flag == flag)
        if search:
            matching_user_ids = _cluster_user_ids_matching(search)
            if matching_user_ids is not None:
                query = query.filter(episode.user_id.in_(matching_user_ids))

        offset = (page - 1) * per_page
        if tab == "follow_up":
            candidates = query.all()
            customers = customers_by_member((row.user_id for row in candidates), today)
            candidates.sort(key=lambda row: follow_up_sort_key(row, customers.get(row.user_id), today))
            return _rows(candidates[offset : offset + per_page], customers, cfg=cfg, today=today), len(candidates)
        total = query.count()
        episodes = query.order_by(*_TAB_ORDER[tab]).offset(offset).limit(per_page).all()
        customers = customers_by_member((row.user_id for row in episodes), today)
        return _rows(episodes, customers, cfg=cfg, today=today), total

    @staticmethod
    def summary() -> dict:
        """Mode, read-only settings, badge counts, 30-day metrics and vocabularies (§9.1).

        Counts and metrics read the row set the page shows; suspended rows are not counted.
        `counts.follow_up` is the nav badge and the Analytics *Needs follow-up* card (D17).
        """
        cfg = current_config()
        # UTC: SQLite stores instants as naive UTC text, so a local-zone bound would shift the window.
        now = local_windows.local_now().astimezone(timezone.utc)
        return {
            "mode": cfg.mode,
            # Published so the page never re-derives the active row set from the mode (amendment 7).
            "row_set_is_dry_run": cfg.row_set_is_dry_run,
            "settings": _settings_view(cfg),
            "counts": {tab: _tab_query(cfg, tab).count() for tab in COUNTED_TABS},
            "metrics_30d": _metrics_30d(is_dry_run=cfg.row_set_is_dry_run, now=now),
            "outcomes": list(OUTCOMES),
            "flags": list(ESCALATION_FLAGS),
            "tabs": list(TABS),
        }


def _settings_view(cfg: ReminderConfig) -> List[dict]:
    """The tunables, read-only, in display order (a list: Flask sorts JSON object keys)."""
    return [
        {"key": "fallback_days", "value": cfg.fallback_days},
        {"key": "ratio", "value": cfg.ratio},
        {"key": "min_deliveries", "value": cfg.min_deliveries},
        {"key": "max_per_episode", "value": cfg.max_per_episode},
        {"key": "gap_days", "value": cfg.gap_days},
        {"key": "snooze_days", "value": cfg.snooze_days},
        {"key": "escalate_after_days", "value": cfg.escalate_after_days},
        {"key": "fresh_days", "value": cfg.fresh_days},
        {"key": "live_order_grace_days", "value": cfg.live_order_grace_days},
        {"key": "local_time", "value": cfg.local_time.strftime("%H:%M")},
    ]


def _metrics_30d(*, is_dry_run: bool, now: datetime) -> dict:
    """The §9.1 metrics over the last 30 days, for one row set. The ONE definition of each figure:
    the page header and the Analytics cards read them from `summary()` and compute none of them.

    Everything that can happen to a lapse more than once comes from the append-only events: a send
    is a `reminder_sent` row, which Task 13's row lock keeps to one per step (a `reminder_failed`
    beside it, `already_claimed` included, is not a second outcome and never a failure here), and an
    escalation is an `escalated` event carrying its flag. The episode's `escalation_flag` column
    holds only the LATEST flag, so it cannot answer "what escalated in the window": a lapse can
    escalate again (the amendment-18 `outlet_dormant` transition). Only the close outcomes read the
    columns, because a row closes once and a closed row never changes again.
    """
    since = now - _METRICS_WINDOW
    sent_kind = _SENT_KIND[is_dry_run]
    episode, event = ReorderReminderEpisode, ReorderReminderEvent

    lapses_started = (
        db.session.query(func.count(event.id))
        .filter(event.kind == "opened", event.is_dry_run == is_dry_run, event.created_at >= since)
        .scalar()
        or 0
    )
    sent_by_channel = dict(
        db.session.query(event.channel, func.count(event.id))
        .filter(event.kind == sent_kind, event.is_dry_run == is_dry_run, event.created_at >= since)
        .group_by(event.channel)
        .all()
    )
    reminded = select(event.episode_id).where(event.kind == sent_kind, event.episode_id.isnot(None))
    closed = episode.query.filter(
        episode.is_dry_run == is_dry_run, episode.status == "closed", episode.closed_at >= since
    )
    escalated_by_flag = dict(
        db.session.query(event.detail, func.count(event.id))
        .filter(event.kind == "escalated", event.is_dry_run == is_dry_run, event.created_at >= since)
        .group_by(event.detail)
        .all()
    )
    return {
        "lapses_started": lapses_started,
        # One count per channel the model knows, so a new channel cannot be dropped silently.
        "reminders_sent": {channel: sent_by_channel.get(channel, 0) for channel in CHANNELS},
        # `superseded` never counts; a reorder after an escalation is "before contact" instead.
        "reordered_after_reminder": closed.filter(
            episode.close_reason == "ordered", episode.escalated_at.is_(None), episode.id.in_(reminded)
        ).count(),
        "place_resupplied": closed.filter(episode.close_reason == "place_resupplied", episode.id.in_(reminded)).count(),
        "reordered_before_contact": closed.filter(
            episode.close_reason == "ordered",
            episode.escalated_at.isnot(None),
            or_(episode.escalation_flag.is_(None), episode.escalation_flag != "stuck_order"),
        ).count(),
        "stuck_order_delivered": closed.filter(
            episode.close_reason == "ordered", episode.escalation_flag == "stuck_order"
        ).count(),
        "escalated_by_flag": {flag: escalated_by_flag.get(flag, 0) for flag in ESCALATION_FLAGS},
        "switched_off_by_customers": _switched_off_by_customers(since),
        "blocks_after_reminder": _blocks_after_reminder(since, sent_kind=sent_kind, is_dry_run=is_dry_run),
    }


def _switched_off_by_customers(since: datetime) -> int:
    """Customers who really switched reminders off in the window (§9.1, amendment 16): DISTINCT
    customers, so off, back on and off again is one, and so are two linked accounts of one person
    (`customer_key`). Never dry-run (§10).

    A `setting_changed` event with detail `off` and a customer payload. A customer's `already_off`
    attempt changed nothing and an admin off is not the customer's choice, so neither counts. The
    actor is read from the payload in Python: JSON-path filters differ between SQLite and Postgres.
    The accounts' identities are resolved in batches of ids (`id_chunks`).
    """
    event = ReorderReminderEvent
    rows = (
        db.session.query(event.user_id, event.payload)
        .filter(
            event.kind == "setting_changed",
            event.detail == DETAIL_OFF,
            event.is_dry_run.is_(False),
            event.created_at >= since,
        )
        .all()
    )
    switcher_ids = {
        user_id
        for user_id, payload in rows
        if isinstance(payload, dict) and payload.get("changed_by_kind") == ACTOR_CUSTOMER
    }
    if not switcher_ids:
        return 0
    canonical_by_user: Dict[int, Optional[int]] = {}
    for chunk in id_chunks(switcher_ids):
        canonical_by_user.update(db.session.query(User.id, User.canonical_customer_id).filter(User.id.in_(chunk)).all())
    return len({customer_key(user_id, canonical_by_user.get(user_id)) for user_id in switcher_ids})


def _blocks_after_reminder(since: datetime, *, sent_kind: str, is_dry_run: bool) -> int:
    """`blocked` events within 72 h after a reminder to the same user (§9.1).

    A block is timed by the instant the customer blocked the bot, the `at` of its payload (the shape
    `TelegramReachabilityService` writes), never by the event's `created_at`: that is when the
    report reached us, and a report can come days late (a restart dropped the update; the recovery
    middleware told us on the customer's next message). The 30-day window runs on the same instant.

    The summary is polled by every open page, so reminders are read only for the users who blocked,
    and not at all when nobody did.
    """
    event = ReorderReminderEvent
    # A report is never earlier than the block, so `created_at >= since` only narrows the read; the
    # window itself is applied to the block instant below.
    reported = (
        db.session.query(event.id, event.user_id, event.payload)
        .filter(event.kind == "blocked", event.created_at >= since)
        .all()
    )
    blocks: List[Tuple[int, datetime]] = []
    for event_id, user_id, payload in reported:
        blocked_at = _block_instant(event_id, payload)
        if blocked_at is not None and blocked_at >= since:
            blocks.append((user_id, blocked_at))
    if not blocks:
        return 0
    sent_at: Dict[int, List[datetime]] = {}
    for chunk in id_chunks({user_id for user_id, _ in blocks}):
        sends = (
            db.session.query(event.user_id, event.created_at)
            .filter(
                event.user_id.in_(chunk),
                event.kind == sent_kind,
                event.is_dry_run == is_dry_run,
                event.created_at >= since - _BLOCK_AFTER_REMINDER,
            )
            .all()
        )
        for user_id, created_at in sends:
            sent_at.setdefault(user_id, []).append(ensure_utc(created_at))
    return sum(
        1
        for user_id, blocked_at in blocks
        if any(
            timedelta(0) <= blocked_at - reminded_at <= _BLOCK_AFTER_REMINDER
            for reminded_at in sent_at.get(user_id, ())
        )
    )


def _block_instant(event_id: int, payload: Any) -> Optional[datetime]:
    """When the customer blocked the bot: the `at` of a `blocked` event's payload, in UTC. A payload
    that does not have that shape costs this one block (logged), never the summary. A warning, not
    an error: the summary is polled, and one drifted event would otherwise log on every poll."""
    try:
        return ensure_utc(datetime.fromisoformat(payload["at"]))
    except (KeyError, TypeError, ValueError):
        logger.warning(
            "reorder_reminders: `blocked` event %s has an unreadable instant; not counted in blocks_after_reminder",
            event_id,
        )
        return None


def _cluster_user_ids_matching(search: str) -> Optional[List[int]]:
    """Every user in the clusters of the users the search matches (§9.1): a sibling's phone finds the row.

    The widening is the batched loader's own cluster rule (`facts.cluster_member_ids`), so the search,
    the rows and the run expand a customer in one place (final review D1). None for a query that
    tokenizes to nothing, so the caller skips the filter.
    """
    search_filter = build_user_search_filter(search)
    if search_filter is None:
        return None
    matched = {user_id for (user_id,) in db.session.query(User.id).filter(search_filter).all()}
    return sorted(cluster_member_ids(matched))


def _rows(
    episodes: Sequence[ReorderReminderEpisode],
    customers: Mapping[int, ReminderCustomer],
    *,
    cfg: ReminderConfig,
    today: date,
) -> List[dict]:
    """The page's rows, every fact read once for the whole page."""
    if not episodes:
        return []
    customer_of = {row.id: customers.get(row.user_id) for row in episodes}
    # A row whose customer could not be loaded keeps the episode's own account for its phones...
    members = {
        row.id: customer_of[row.id].member_ids if customer_of[row.id] is not None else (row.user_id,)
        for row in episodes
    }
    # ...but its bottles and debt are still the cluster's figures, never one account's share.
    figure_members = {**members, **_degraded_clusters(row for row in episodes if customer_of[row.id] is None)}
    events = _events_by_episode([row.id for row in episodes])
    outlets = {row.id: _escalated_outlets(row, events.get(row.id, [])) for row in episodes}
    groups = _place_groups(
        sorted(
            {
                group_id
                for customer in customer_of.values()
                if customer is not None
                for group_id in customer.place_group_ids
            }
        )
    )
    place_last = _last_place_deliveries(groups)
    stuck = {row.id: _stuck_live_orders(customer_of[row.id], cfg=cfg, today=today) for row in episodes}
    orders = _orders_by_id([order.order_id for live in stuck.values() for order in live])
    users = _users_by_id(_user_ids_to_show(episodes, members, groups, outlets))
    bottles = _bottles_held(figure_members)
    debts = CashCollectionService().get_net_open_cod_debt_totals_by_cluster(figure_members)
    return [
        _row(
            row,
            customer_of[row.id],
            member_ids=members[row.id],
            users=users,
            events=events.get(row.id, []),
            outlets=outlets[row.id],
            groups=groups,
            place_last=place_last,
            stuck=stuck[row.id],
            orders=orders,
            bottles_held=bottles[row.id],
            open_debt=debts[row.id],
            cfg=cfg,
            today=today,
        )
        for row in episodes
    ]


def _row(
    episode: ReorderReminderEpisode,
    customer: Optional[ReminderCustomer],
    *,
    member_ids: Sequence[int],
    users: Dict[int, User],
    events: List[ReorderReminderEvent],
    outlets: Optional[List[dict]],
    groups: Dict[int, dict],
    place_last: Dict[Tuple[int, int], datetime],
    stuck: List[LiveOrderFacts],
    orders: Dict[int, Any],
    bottles_held: Decimal,
    open_debt: Decimal,
    cfg: ReminderConfig,
    today: date,
) -> dict:
    recipient = users.get(episode.user_id)
    setting = customer.setting if customer is not None else None
    return {
        "episode_id": episode.id,
        "status": episode.status,
        "is_dry_run": bool(episode.is_dry_run),
        "recipient": {
            "user_id": episode.user_id,
            "name": user_display_name(recipient) if recipient is not None else None,
            # THE language the reminder is written in (the one rule, final review B1/E1), never the
            # raw stored value: staff follow up in the language the customer really reads.
            "reminder_language": reminder_language(recipient.preferred_language) if recipient is not None else None,
        },
        "phones": _phones(member_ids, episode.user_id, users),
        "last_delivery_at": episode.supply_delivered_at,
        # The one "days since" rule (amendment 8), the one the reminder copy and the email print.
        "days_since": days_since(episode.supply_delivered_at, today),
        "usual_gap_days": episode.usual_gap_days,
        # The episode stores no usual gap when the preset threshold applied (§4 "Threshold").
        "gap_source": "personal" if episode.usual_gap_days is not None else "preset",
        "threshold_days": episode.threshold_days,
        "due_on": episode.due_on,
        "days_overdue": days_overdue(episode, today),
        "step": episode.step,
        "next_action_on": episode.next_action_on,
        "reminders_sent": episode.reminders_sent,
        "last_channel": episode.last_channel,
        "last_reminder_at": episode.last_reminder_at,
        "escalation_flag": episode.escalation_flag,
        "escalated_at": episode.escalated_at,
        "suspended_reason": episode.suspended_reason,
        "suspended_at": episode.suspended_at,
        "resolved_at": episode.resolved_at,
        "outcome": episode.outcome,
        "outcome_note": episode.outcome_note,
        "resolved_by": _person(users.get(episode.resolved_by_user_id)),
        "history": _history(events),
        "reply_excerpt": _reply_excerpt(episode, events),
        "stuck_orders": [_stuck_order_view(order, orders, today) for order in stuck],
        # The one published shape of the effective setting (amendment 15), the reason included for staff.
        "setting": setting.to_dict(include_reason=True) if setting is not None else None,
        # From the CURRENT setting and block state (§9.1), never from the flag: the one rule's answer.
        "contact_restriction": (
            evaluate(customer, today=today, cfg=cfg).contact_restriction if customer is not None else None
        ),
        "places": _places(customer, users, groups, place_last),
        # None when the escalation's payload could not be read (logged), so the page can say so.
        "outlets": (
            None
            if outlets is None
            else [
                {
                    "outlet_id": outlet["outlet_id"],
                    "name": outlet["name"],
                    "dormant_since": outlet["dormant_since"],
                    "agent": _person(users.get(outlet["agent_user_id"])),
                }
                for outlet in outlets
            ]
        ),
        "bottles_held": bottles_held,
        "open_debt": open_debt,
        "lifetime_deliveries": lifetime_deliveries(customer),
        # The model's one resolvability rule (amendment 4); Task 12's write guard uses its SQL twin.
        "can_resolve": is_resolvable(episode),
        # A live row only (§13), and only while reminders are on: the action is *Turn off*, so a
        # row whose effective setting is already off has nothing to turn off. Recording a
        # do-not-contact over a customer's own Stop is Resolve → `do_not_contact`, which upgrades
        # it to admin-off (D20, amendment 16).
        "can_turn_off_reminders": (
            episode.status == "open" and not episode.is_dry_run and (setting is None or setting.enabled)
        ),
        "pending_close": _pending_close(episode, customer),
    }


def _pending_close(episode: ReorderReminderEpisode, customer: Optional[ReminderCustomer]) -> bool:
    """Will the next daily run close this lapse? The run's own question, `engine.close_decision`
    over the customer's current (own anchor, supply) pair, asked now: a follow-up whose customer
    just reordered is not an open job, though it stays listed until the run closes it. Only an
    active lapse closes (§6: resolved rows stay resolved), and with no facts there is no pair to
    compare, so a degraded row is never flagged (the run does not close blind either)."""
    if customer is None or episode.status not in ACTIVE_EPISODE_STATUSES:
        return False
    return close_decision(episode, customer.own_anchor, customer.supply) is not None


def _reply_excerpt(episode: ReorderReminderEpisode, events: Sequence[ReorderReminderEvent]) -> Optional[str]:
    """The latest reply's excerpt. Task 12's reply hook writes `payload={"excerpt": ...}`; a payload
    that does not have that shape costs this field of this row only (logged), never the page."""
    replies = [event for event in events if event.kind == "replied"]
    if not replies:
        return None
    try:
        return replies[-1].payload["excerpt"]
    except (KeyError, TypeError):
        logger.error(
            "reorder_reminders: episode %s has an unreadable `replied` payload (event %s); reply excerpt not shown",
            episode.id,
            replies[-1].id,
        )
        return None


def _degraded_clusters(episodes: Iterable[ReorderReminderEpisode]) -> Dict[int, Tuple[int, ...]]:
    """`{episode id: its account's cluster}` for rows whose customer could not be loaded, from the
    one cluster rule (`CustomerLinkService.get_cluster_user_ids`), each account looked up once."""
    link_service = CustomerLinkService()
    clusters: Dict[int, Tuple[int, ...]] = {}
    by_episode: Dict[int, Tuple[int, ...]] = {}
    for episode in episodes:
        if episode.user_id not in clusters:
            clusters[episode.user_id] = tuple(link_service.get_cluster_user_ids(episode.user_id))
        by_episode[episode.id] = clusters[episode.user_id]
    return by_episode


def _person(user: Optional[User]) -> Optional[dict]:
    return {"user_id": user.id, "name": user_display_name(user)} if user is not None else None


def _phones(member_ids: Sequence[int], recipient_id: int, users: Dict[int, User]) -> List[dict]:
    """Every phone in the cluster, each with its own block marker (D19: a block is per account)."""
    phones = []
    for user_id in sorted(member_ids):
        user = users.get(user_id)
        if user is None:
            continue
        phones.append(
            {
                "user_id": user_id,
                "name": user_display_name(user),
                "phone": user.phone,
                "is_recipient": user_id == recipient_id,
                "blocked_bot": user.telegram_blocked_at is not None,
                "blocked_since": user.telegram_blocked_at,
            }
        )
    return phones


def _history(events: Sequence[ReorderReminderEvent]) -> List[dict]:
    """The row's event history, with one outcome per reminder step (Tasks 13 and 18a):

    - a step with a `reminder_sent` was sent: no `reminder_failed` beside it is shown, whatever its
      detail (a racing copy, an earlier attempt);
    - `reminder_failed('already_claimed')` is another copy handling the step, never a failure;
    - the bot route's 410 (`bot_blocked`) and the send-time refusal (`blocked`) both read `blocked`.
    """
    sent_steps = {event.step for event in events if event.kind == "reminder_sent"}
    return [
        {
            "kind": event.kind,
            "step": event.step,
            "channel": event.channel,
            "detail": _HISTORY_DETAIL.get(event.detail, event.detail),
            "at": event.created_at,
        }
        for event in events
        if not (
            event.kind == "reminder_failed" and (event.step in sent_steps or event.detail == DETAIL_ALREADY_CLAIMED)
        )
    ]


def _events_by_episode(episode_ids: List[int]) -> Dict[int, List[ReorderReminderEvent]]:
    grouped: Dict[int, List[ReorderReminderEvent]] = {}
    events = (
        ReorderReminderEvent.query.filter(ReorderReminderEvent.episode_id.in_(episode_ids))
        .order_by(ReorderReminderEvent.created_at.asc(), ReorderReminderEvent.id.asc())
        .all()
    )
    for event in events:
        grouped.setdefault(event.episode_id, []).append(event)
    return grouped


def _escalated_outlets(episode: ReorderReminderEpisode, events: Sequence[ReorderReminderEvent]) -> Optional[List[dict]]:
    """The dormant outlets behind an `outlet_dormant` row (D21), as the daily run recorded them on
    the row's latest `outlet_dormant` escalation: Task 11a's payload
    `{"outlets": [{outlet_id, name, dormant_since, agent_user_id}]}`, never rebuilt from the
    outlets' current stages. A payload without that shape costs this field of this row only: it is
    logged and None is returned, never the page."""
    if episode.escalation_flag != FLAG_OUTLET_DORMANT:
        return []
    escalations = [event for event in events if event.kind == "escalated" and event.detail == FLAG_OUTLET_DORMANT]
    if not escalations:
        return []
    try:
        return [
            {
                "outlet_id": outlet["outlet_id"],
                "name": outlet["name"],
                "dormant_since": outlet["dormant_since"],
                "agent_user_id": outlet["agent_user_id"],
            }
            for outlet in escalations[-1].payload["outlets"]
        ]
    except (KeyError, TypeError):
        logger.error(
            "reorder_reminders: episode %s has an unreadable `outlet_dormant` payload (event %s); outlets not shown",
            episode.id,
            escalations[-1].id,
        )
        return None


def _place_groups(group_ids: List[int]) -> Dict[int, dict]:
    """`{group id: {"label", "addresses": [(address id, owner id, full address)]}}`, addresses by id."""
    if not group_ids:
        return {}
    labels = dict(db.session.query(AddressGroup.id, AddressGroup.label).filter(AddressGroup.id.in_(group_ids)).all())
    groups = {group_id: {"label": labels.get(group_id), "addresses": []} for group_id in group_ids}
    rows = (
        db.session.query(UserAddress.id, UserAddress.user_id, UserAddress.full_address, UserAddress.address_group_id)
        .filter(UserAddress.address_group_id.in_(group_ids))
        .order_by(UserAddress.id.asc())
        .all()
    )
    for address_id, owner_id, full_address, group_id in rows:
        groups[group_id]["addresses"].append((address_id, owner_id, full_address))
    return groups


def _last_place_deliveries(groups: Dict[int, dict]) -> Dict[Tuple[int, int], datetime]:
    """`{(group id, user id): that user's newest delivery to any address of the group}`."""
    group_of = {address_id: group_id for group_id, group in groups.items() for address_id, _, _ in group["addresses"]}
    if not group_of:
        return {}
    instants = delivered_order_instants_select().subquery()
    rows = (
        db.session.query(instants.c.user_id, instants.c.delivery_address_id, func.max(instants.c.delivered_at))
        .filter(instants.c.delivery_address_id.in_(sorted(group_of)))
        .group_by(instants.c.user_id, instants.c.delivery_address_id)
        .all()
    )
    last: Dict[Tuple[int, int], datetime] = {}
    for user_id, address_id, delivered_at in rows:
        key = (group_of[address_id], user_id)
        at = ensure_utc(delivered_at)
        if key not in last or at > last[key]:
            last[key] = at
    return last


def _places(
    customer: Optional[ReminderCustomer],
    users: Dict[int, User],
    groups: Dict[int, dict],
    place_last: Dict[Tuple[int, int], datetime],
) -> List[dict]:
    """The shared places with their OTHER members, so staff call a place once (D18)."""
    if customer is None:
        return []
    members = set(customer.member_ids)
    blocks = []
    for group_id in customer.place_group_ids:
        group = groups.get(group_id)
        if group is None:
            continue
        own_address = next((full for _, owner_id, full in group["addresses"] if owner_id in members), None)
        others = sorted({owner_id for _, owner_id, _ in group["addresses"] if owner_id not in members})
        blocks.append(
            {
                "group_id": group_id,
                "label": group["label"],
                "address": own_address,
                "members": [
                    {
                        "user_id": user_id,
                        "name": user_display_name(users[user_id]) if user_id in users else None,
                        "phone": users[user_id].phone if user_id in users else None,
                        "last_delivery_at": place_last.get((group_id, user_id)),
                    }
                    for user_id in others
                ],
            }
        )
    return blocks


def _stuck_live_orders(
    customer: Optional[ReminderCustomer], *, cfg: ReminderConfig, today: date
) -> List[LiveOrderFacts]:
    """The customer's OWN live orders that are not plausibly on their way (D12, §4)."""
    if customer is None:
        return []
    return [
        order
        for order in customer.live_orders
        if order.is_own and not is_in_progress(order, today=today, grace_days=cfg.live_order_grace_days)
    ]


def _orders_by_id(order_ids: List[int]) -> Dict[int, Any]:
    if not order_ids:
        return {}
    rows = (
        db.session.query(Order.id, Order.order_number, Order.status).filter(Order.id.in_(sorted(set(order_ids)))).all()
    )
    return {row.id: row for row in rows}


def _stuck_order_view(order: LiveOrderFacts, orders: Dict[int, Any], today: date) -> dict:
    stored = orders.get(order.order_id)
    status = getattr(stored, "status", None)
    return {
        "order_id": order.order_id,
        "order_number": stored.order_number if stored is not None else None,
        "status": status.value if hasattr(status, "value") else status,
        "age_days": (today - order.created_on).days,
        "awaiting_new_date": order.awaiting_new_date,
    }


def _user_ids_to_show(
    episodes: Sequence[ReorderReminderEpisode],
    members: Mapping[int, Sequence[int]],
    groups: Dict[int, dict],
    outlets: Mapping[int, Optional[List[dict]]],
) -> List[int]:
    """Every account a row names: the cluster's phones, who resolved it, the place members and the
    dormant outlets' agents."""
    user_ids = set()
    for row in episodes:
        user_ids.update(members[row.id])
        user_ids.add(row.user_id)
        if row.resolved_by_user_id is not None:
            user_ids.add(row.resolved_by_user_id)
        user_ids.update(
            outlet["agent_user_id"] for outlet in outlets[row.id] or () if outlet["agent_user_id"] is not None
        )
    for group in groups.values():
        user_ids.update(owner_id for _, owner_id, _ in group["addresses"])
    return sorted(user_ids)


def _users_by_id(user_ids: List[int]) -> Dict[int, User]:
    if not user_ids:
        return {}
    return {user.id: user for user in User.query.filter(User.id.in_(user_ids)).all()}


def _bottles_held(members: Mapping[int, Sequence[int]]) -> Dict[int, Decimal]:
    """`{episode id: returnable bottles across its cluster's places}`, each balance counted once (by
    `BottleBalance.id`): two members at one place share one pool."""
    scopes = BottleTrackingService.get_customer_scopes_by_user(
        {user_id for member_ids in members.values() for user_id in member_ids}
    )
    held: Dict[int, Decimal] = {}
    for episode_id, member_ids in members.items():
        balances = {balance.id: balance.balance for user_id in member_ids for balance in scopes.get(user_id, ())}
        held[episode_id] = sum((Decimal(str(value or 0)) for value in balances.values()), Decimal("0"))
    return held
