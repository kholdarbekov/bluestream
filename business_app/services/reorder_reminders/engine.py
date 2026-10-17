"""The daily reorder-reminder run: a lapse-episode state machine (spec §6, §12, §13).

One run loads the batched facts (`facts.load_customers`), asks the one rule
(`eligibility.evaluate`) once per customer, and moves each customer's episode through the §6
lifecycle in the §12 order:

1. mode `on`: drop every dry-run row, events first, then episodes (explicitly: the SQLite suite
   runs with foreign keys off). Events with no episode are never dry-run, so opt-outs survive;
2. close on a newer delivery, on BOTH row sets, in every mode. Mode `off` stops here, so worklist
   rows still close when customers reorder;
3-6. per customer, in the active row set (`cfg.row_set_is_dry_run`, the one spelling of it):
   reconcile identity, suspend or resume, the stuck check, then open a newly due lapse, or an
   outlet-path row (D21), which may be the customer's own row resumed (contract amendment 18);
7. due reminder steps, oldest `next_action_on` first;
8. mode `on`: re-publish sends whose publish was lost.

Each customer, and each due step, commits on its own and fails on its own: an error is logged
with the customer key, counted, and every other customer is still processed. A unit's transition
counts, and its evaluation, reach the run only once that unit commits, so a rolled-back unit
counts as one error and nothing else. A customer whose facts fail to load is skipped by the loader
and counted here too (contract amendment 17). Ladder
transitions are conditional UPDATEs guarded by status, step and date (§6 Scheduling), so a
concurrent *Later* tap wins and a duplicate run cannot double-step. The caller reads `now` once,
and every event is stamped with it (`record_event_at`).
"""

import logging
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Dict, Iterable, Optional, Set, Tuple, TypeVar

from sqlalchemy import func, select

from business_app import db
from business_app.models.order import Order
from business_app.models.reorder_reminder import (
    ACTIVE_EPISODE_STATUSES,
    FLAG_OUTLET_DORMANT,
    SUSPENDABLE_EPISODE_STATUSES,
    ReorderReminderEpisode,
    ReorderReminderEvent,
)
from business_app.services.reorder_reminders.config import MODE_OFF, MODE_ON, ReminderConfig
from business_app.services.reorder_reminders.eligibility import (
    PATH_CUSTOMER,
    PATH_OUTLET,
    REASON_BLOCKED,
    REASON_NO_CHANNEL,
    REASON_STOPPED,
    Evaluation,
    evaluate,
)
from business_app.services.reorder_reminders.events import record_event_at
from business_app.services.reorder_reminders.facts import DormantOutletFacts, ReminderCustomer, load_customers
from business_app.utils.local_windows import local_date
from business_app.utils.reorder_reminder_dispatch import queue_reminder_send
from business_app.utils.timezone_utils import ensure_utc

logger = logging.getLogger(__name__)

Episode = ReorderReminderEpisode
Delivered = Tuple[int, datetime]
# One dormant spell of one outlet (D21): its id and the instant it went dormant, as the
# `outlet_dormant` escalation payload records it.
Spell = Tuple[int, Optional[str]]
T = TypeVar("T")

_CHUNK = 500
_THRESHOLD_STEP = Decimal("0.1")  # reorder_reminder_episodes.threshold_days is numeric(5,1)
# Out-of-scope reasons that are not suspend reasons (§10 CK): an account with no usable member
# keeps its lapse suspended as `no_recipient`.
_NO_RECIPIENT_REASONS = ("staff", "inactive", "no_recipient")
# §12 step 8: a send still `reminder_queued` this long after it was queued is published again.
REPUBLISH_AFTER = timedelta(minutes=30)


@dataclass
class RunSummary:
    mode: str
    scanned: int = 0
    opened: int = 0
    queued: int = 0
    simulated: int = 0
    skipped: Dict[str, int] = field(default_factory=dict)
    escalated: Dict[str, int] = field(default_factory=dict)
    closed: Dict[str, int] = field(default_factory=dict)
    suspended: int = 0
    resumed: int = 0
    republished: int = 0
    errors: int = 0

    def add(self, other: "RunSummary") -> None:
        """Fold one committed unit's counts into this summary."""
        for column in fields(self):
            if column.name == "mode":
                continue
            value = getattr(other, column.name)
            if isinstance(value, dict):
                bucket = getattr(self, column.name)
                for key, count in value.items():
                    bucket[key] = bucket.get(key, 0) + count
            else:
                setattr(self, column.name, getattr(self, column.name) + value)


class ReorderReminderEngine:
    def __init__(self, cfg: ReminderConfig, *, now: datetime):
        self.cfg = cfg
        self.now = ensure_utc(now)
        self.today = local_date(self.now)
        self.summary = RunSummary(mode=cfg.mode)
        self._customers_by_user: Dict[int, ReminderCustomer] = {}
        self._evaluations: Dict[str, Evaluation] = {}
        self._active_by_key: Dict[str, int] = {}
        # The running unit's transition counts; folded into `summary` only when it commits.
        self._unit: Optional[RunSummary] = None

    def run(self) -> RunSummary:
        """The whole §12 run order; commits per customer."""
        if self.cfg.mode == MODE_ON:
            self._purge_dry_run_rows()
        load_errors: list = []
        customers = load_customers(today=self.today, errors=load_errors)
        # Amendment 17: the loader logged each skipped customer's key; the run counts them.
        self.summary.errors += len(load_errors)
        self.summary.scanned = len(customers)
        self._customers_by_user = {user_id: customer for customer in customers for user_id in customer.member_ids}
        self._close_superseded()
        if self.cfg.mode == MODE_OFF:
            return self._finish()
        self._active_by_key = self._active_episodes_by_key()
        for customer in customers:
            evaluation = self._isolated(customer.key, lambda customer=customer: self._prepare(customer))
            if evaluation is not None:
                # Only a committed unit's evaluation: the ladder never steps a half-applied customer.
                self._evaluations[customer.key] = evaluation
        self._act_on_due_steps()
        if self.cfg.mode == MODE_ON:
            self._isolated("re-publish", self._republish_stale_queued)
        return self._finish()

    # ------------------------------------------------------------------ plumbing
    def _finish(self) -> RunSummary:
        logger.info("reorder_reminders run: %s", asdict(self.summary))
        return self.summary

    def _isolated(self, label: str, work: Callable[[], T]) -> Optional[T]:
        """Run one unit and commit it; return its result. On any error: roll it back, log it with
        `label`, count it, return None and go on (master Review Focus #4). The unit counts into
        `_unit`, which reaches the summary only after the commit, so a rolled-back unit adds nothing
        but its error."""
        unit = self._unit = RunSummary(mode=self.cfg.mode)
        try:
            result = work()
            db.session.commit()
        except Exception:  # noqa: BLE001 - one customer must never stop the run
            db.session.rollback()
            self.summary.errors += 1
            logger.exception("reorder_reminders: %s failed and was skipped", label)
            return None
        finally:
            self._unit = None
        self.summary.add(unit)
        return result

    @staticmethod
    def _count(bucket: Dict[str, int], key: str) -> None:
        bucket[key] = bucket.get(key, 0) + 1

    def _event(self, episode: Episode, kind: str, **fields) -> None:
        record_event_at(self.now, user_id=episode.user_id, kind=kind, episode=episode, **fields)

    def _transition(self, episode: Episode, from_statuses: Iterable[str], *, extra: Iterable = (), **values) -> bool:
        """One conditional UPDATE (§6 Scheduling): it applies only while the row is still in
        `from_statuses` and every `extra` clause holds. False when a concurrent writer moved it first."""
        values.setdefault("updated_at", self.now)
        changed = Episode.query.filter(
            Episode.id == episode.id, Episode.status.in_(tuple(from_statuses)), *extra
        ).update(values, synchronize_session=False)
        db.session.expire(episode)
        return changed == 1

    @staticmethod
    def _anchor_owners(order_ids: Iterable[int]) -> Dict[int, int]:
        """`{order_id: the user who owns it now}`: the cluster owning a lapse's anchor is its customer."""
        ids = sorted(set(order_ids))
        owners: Dict[int, int] = {}
        for offset in range(0, len(ids), _CHUNK):
            rows = db.session.query(Order.id, Order.user_id).filter(Order.id.in_(ids[offset : offset + _CHUNK])).all()
            owners.update((order_id, user_id) for order_id, user_id in rows)
        return owners

    # ------------------------------------------------------------------ §12 step 1
    def _purge_dry_run_rows(self) -> None:
        ids = [episode_id for (episode_id,) in db.session.query(Episode.id).filter(Episode.is_dry_run.is_(True))]
        for offset in range(0, len(ids), _CHUNK):
            chunk = ids[offset : offset + _CHUNK]
            ReorderReminderEvent.query.filter(ReorderReminderEvent.episode_id.in_(chunk)).delete(
                synchronize_session=False
            )
            Episode.query.filter(Episode.id.in_(chunk)).delete(synchronize_session=False)
        db.session.commit()

    # ------------------------------------------------------------------ §12 step 2
    def _close_superseded(self) -> None:
        rows = (
            db.session.query(Episode.id, Episode.anchor_order_id)
            .filter(Episode.status.in_(ACTIVE_EPISODE_STATUSES))
            .order_by(Episode.id)
            .all()
        )
        owners = self._anchor_owners(anchor_id for _episode_id, anchor_id in rows)
        for episode_id, anchor_id in rows:
            customer = self._customers_by_user.get(owners.get(anchor_id))
            if customer is None:
                # Every active anchor's owner is seeded into the load, so a missing customer is one
                # whose facts failed to load this run (logged and counted, amendment 17). With no
                # facts there is no pair to compare: never close blind; tomorrow's run decides.
                continue
            self._isolated(
                f"{customer.key} episode {episode_id}",
                lambda episode_id=episode_id, customer=customer: self._close_if_superseded(episode_id, customer),
            )

    def _close_if_superseded(self, episode_id: int, customer: ReminderCustomer) -> None:
        episode = db.session.get(Episode, episode_id)
        decision = close_decision(episode, customer.own_anchor, customer.supply)
        if decision is None:
            return
        reason, superseding = decision
        if not self._transition(
            episode,
            ACTIVE_EPISODE_STATUSES,
            status="closed",
            closed_at=self.now,
            close_reason=reason,
            next_action_on=None,
        ):
            return
        payload = {"order_id": superseding[0], "delivered_at": superseding[1].isoformat()} if superseding else None
        self._event(episode, "closed", detail=reason, payload=payload)
        self._count(self._unit.closed, reason)

    def _active_episodes_by_key(self) -> Dict[str, int]:
        """After close, a customer holds at most one active episode per row set: any other one had a
        superseded pair and was just closed."""
        rows = (
            db.session.query(Episode.id, Order.user_id)
            .join(Order, Order.id == Episode.anchor_order_id)
            .filter(Episode.is_dry_run.is_(self.cfg.row_set_is_dry_run), Episode.status.in_(ACTIVE_EPISODE_STATUSES))
            .order_by(Episode.id)
            .all()
        )
        by_key: Dict[str, int] = {}
        for episode_id, owner_id in rows:
            customer = self._customers_by_user.get(owner_id)
            if customer is not None:
                by_key.setdefault(customer.key, episode_id)
        return by_key

    # ------------------------------------------------------------------ §12 steps 3-6
    def _prepare(self, customer: ReminderCustomer) -> Evaluation:
        """§12 steps 3-6 for one customer; returns the evaluation the ladder steps on."""
        evaluation = evaluate(customer, today=self.today, cfg=self.cfg)
        episode_id = self._active_by_key.get(customer.key)
        if episode_id is not None:
            episode = db.session.get(Episode, episode_id)
            self._reconcile(episode, customer, evaluation)
            self._suspend_or_resume(episode, evaluation)
            # After the suspend, so a lapse suspended `outlet` in this very run (its account gained an
            # already-dormant outlet) becomes the outlet row now, not tomorrow.
            self._resume_as_outlet_row(episode, customer, evaluation)
            self._escalate_if_stuck(episode, evaluation)
        elif evaluation.path == PATH_CUSTOMER:
            self._open_if_due(evaluation)
        elif evaluation.path == PATH_OUTLET:
            self._open_outlet_row(customer, evaluation)
        return evaluation

    def _reconcile(self, episode: Episode, customer: ReminderCustomer, evaluation: Evaluation) -> None:
        """§6 Identity. The lapse follows the run-time recipient: re-pointed, never closed and
        reopened. With no recipient it rests with the anchor order's owner, inside the owning cluster,
        before it is suspended.

        An outlet row stays with its dormant outlet's account (D21) while that account is in the
        cluster that owns the anchor. Once it is not (an admin unlinked it), the row moves back into
        the cluster: to its first dormant outlet's account, else the recipient, else the anchor
        owner. Otherwise the row would hold the one-active slot of an account that is now a customer
        of its own, and that customer's own row could never open.
        """
        target = None
        if episode.escalation_flag == FLAG_OUTLET_DORMANT:
            if episode.user_id in customer.member_ids:
                return
            if customer.dormant_outlets:
                target, _payload = self._outlet_row_target(customer)
        if target is None and evaluation.recipient is not None:
            target = evaluation.recipient.user_id
        if target is None:
            target = db.session.query(Order.user_id).filter(Order.id == episode.anchor_order_id).scalar()
        if target is not None and target != episode.user_id:
            self._transition(episode, ACTIVE_EPISODE_STATUSES, user_id=target)

    @staticmethod
    def _suspend_reason(episode: Episode, evaluation: Evaluation) -> Optional[str]:
        reason = evaluation.out_of_scope_reason
        if reason is None or reason == "no_deliveries":
            return None  # no own delivery left: the close step owns that case
        if reason == "outlet" and episode.escalation_flag == FLAG_OUTLET_DORMANT:
            return None  # the outlet path's own row
        if reason in _NO_RECIPIENT_REASONS:
            return "no_recipient"
        return reason

    def _suspend_or_resume(self, episode: Episode, evaluation: Evaluation) -> None:
        """§6 Suspend and resume: `next_action_on`, `step` and `reminders_sent` never change, so
        the stale guard and the caps still hold after a resume.

        A row still out of scope for ANOTHER reason takes the current one in place, with no event:
        the suspension was already recorded and counted. Its reason must be current, because
        contract amendment 18 resumes only a row suspended `outlet`.
        """
        reason = self._suspend_reason(episode, evaluation)
        if reason is not None and episode.status in SUSPENDABLE_EPISODE_STATUSES:
            if self._transition(
                episode,
                SUSPENDABLE_EPISODE_STATUSES,
                status="suspended",
                suspended_at=self.now,
                suspended_reason=reason,
            ):
                self._event(episode, "suspended", detail=reason)
                self._unit.suspended += 1
        elif reason is not None and episode.status == "suspended" and episode.suspended_reason != reason:
            self._transition(episode, ("suspended",), suspended_reason=reason)
        elif reason is None and episode.status == "suspended":
            back = "escalated" if episode.escalated_at is not None else "open"
            if self._transition(episode, ("suspended",), status=back, suspended_at=None, suspended_reason=None):
                self._event(episode, "resumed", detail=back)
                self._unit.resumed += 1

    def _escalate_if_stuck(self, episode: Episode, evaluation: Evaluation) -> None:
        """§12 step 5: an open lapse whose own live orders are all stuck goes to staff, due or not (D12)."""
        if episode.status == "open" and evaluation.own_live_all_stuck:
            self._escalate(episode, "stuck_order")

    def _escalate(self, episode: Episode, flag: str, *, extra: Iterable = ()) -> None:
        """A direct escalation (no date guard), or the end of the ladder (`extra` carries its guard)."""
        if self._transition(
            episode,
            ("open",),
            extra=extra,
            status="escalated",
            escalated_at=self.now,
            escalation_flag=flag,
            next_action_on=None,
        ):
            self._event(episode, "escalated", detail=flag)
            self._count(self._unit.escalated, flag)

    def _open_if_due(self, evaluation: Evaluation) -> None:
        """§6 Open, first matching rule wins: stuck order -> paused -> fresh -> long lapsed."""
        if not (evaluation.in_scope and evaluation.due):
            return
        if evaluation.own_live_all_stuck:
            self._create(evaluation, status="escalated", flag="stuck_order")
        elif evaluation.paused:
            return
        elif evaluation.fresh:
            self._create(evaluation, status="open")
        else:
            self._create(evaluation, status="escalated", flag="long_lapsed")

    @staticmethod
    def _outlet_row_target(customer: ReminderCustomer) -> Tuple[int, dict]:
        """Where an outlet row sits (the first dormant outlet's account, D21) and the `escalated`
        payload Task 20's worklist reads. One builder for the opened and the resumed outlet row."""
        dormant = sorted(customer.dormant_outlets, key=lambda row: row.outlet_id)
        payload = {
            "outlets": [
                {
                    "outlet_id": row.outlet_id,
                    "name": row.name,
                    "dormant_since": _dormant_since_text(row),
                    "agent_user_id": row.agent_user_id,
                }
                for row in dormant
            ]
        }
        return dormant[0].user_id, payload

    def _spells_already_flagged(self, customer: ReminderCustomer) -> bool:
        """True when every dormant spell the customer has now (outlet id + dormant_since) was already
        named by an `outlet_dormant` escalation of the cluster's accounts in the active row set.

        Final review A1 (ruling): one row per dormant spell, not per (own anchor, supply anchor).
        On a chain whose other branch keeps buying, every delivery moves the account's anchor and
        closes the outlet row `ordered` while the branch stays dormant; without this the same spell
        would come back to the worklist and the digest every week. A new spell (another outlet, or
        the same one dormant again later) is flagged as usual. Read from the append-only events,
        whose payload is the one `_outlet_row_target` writes, so a closed or resolved row counts.
        """
        current = {_spell(row) for row in customer.dormant_outlets}
        payloads = (
            db.session.query(ReorderReminderEvent.payload)
            .join(Episode, Episode.id == ReorderReminderEvent.episode_id)
            .filter(
                Episode.user_id.in_(customer.member_ids),
                Episode.is_dry_run.is_(self.cfg.row_set_is_dry_run),
                ReorderReminderEvent.kind == "escalated",
                ReorderReminderEvent.detail == FLAG_OUTLET_DORMANT,
            )
            .all()
        )
        flagged: Set[Spell] = set()
        for (payload,) in payloads:
            outlets = payload.get("outlets") if isinstance(payload, dict) else None
            for outlet in outlets if isinstance(outlets, list) else ():
                if isinstance(outlet, dict):
                    flagged.add((outlet.get("outlet_id"), outlet.get("dormant_since")))
        return current <= flagged

    def _open_outlet_row(self, customer: ReminderCustomer, evaluation: Evaluation) -> None:
        """D21: an outlet account never gets a reminder. A dormant outlet puts it on the worklist once
        per (own anchor, supply anchor), escalated `outlet_dormant`, with no message, and once per
        dormant spell (`_spells_already_flagged`)."""
        if not customer.dormant_outlets or evaluation.own_anchor is None:
            return
        if self._spells_already_flagged(customer):
            return
        user_id, payload = self._outlet_row_target(customer)
        self._create(evaluation, status="escalated", flag=FLAG_OUTLET_DORMANT, user_id=user_id, payload=payload)

    def _resume_as_outlet_row(self, episode: Episode, customer: ReminderCustomer, evaluation: Evaluation) -> None:
        """Contract amendment 18 (D21): a customer-path lapse suspended `outlet` for the customer's
        CURRENT pair, on an account whose outlet has turned dormant, resumes directly as the outlet
        row: escalated `outlet_dormant` on the dormant outlet's account, keeping its history. The
        pair never opens a second row (§6). The re-escalation is new to the managers, so the digest
        carries it again (`digested_at` cleared)."""
        pair = (
            (evaluation.own_anchor[0], evaluation.supply[0])
            if evaluation.own_anchor is not None and evaluation.supply is not None
            else None
        )
        if not (
            evaluation.path == PATH_OUTLET
            and customer.dormant_outlets
            and episode.status == "suspended"
            and episode.suspended_reason == "outlet"
            and episode.escalation_flag != FLAG_OUTLET_DORMANT
            and pair == (episode.anchor_order_id, episode.supply_order_id)
        ):
            return
        if self._spells_already_flagged(customer):
            return  # one row per dormant spell: this one is already on the worklist's history
        user_id, payload = self._outlet_row_target(customer)
        if not self._transition(
            episode,
            ("suspended",),
            extra=(Episode.suspended_reason == "outlet",),
            status="escalated",
            suspended_at=None,
            suspended_reason=None,
            escalated_at=self.now,
            escalation_flag=FLAG_OUTLET_DORMANT,
            digested_at=None,
            next_action_on=None,
            user_id=user_id,
        ):
            return  # a concurrent writer moved it first: the next steps read its new state
        self._event(episode, "resumed", detail="escalated")
        self._unit.resumed += 1
        self._event(episode, "escalated", detail=FLAG_OUTLET_DORMANT, payload=payload)
        self._count(self._unit.escalated, FLAG_OUTLET_DORMANT)

    def _create(
        self,
        evaluation: Evaluation,
        *,
        status: str,
        flag: Optional[str] = None,
        user_id: Optional[int] = None,
        payload: Optional[dict] = None,
    ) -> None:
        anchor, supply = evaluation.own_anchor, evaluation.supply
        if self._pair_exists(anchor[0], supply[0]):
            return  # after escalated / resolved / closed, a pair never opens twice (§6)
        escalated = status == "escalated"
        threshold = evaluation.threshold.quantize(_THRESHOLD_STEP, rounding=ROUND_HALF_UP)
        episode = Episode(
            user_id=user_id if user_id is not None else evaluation.recipient.user_id,
            anchor_order_id=anchor[0],
            anchor_delivered_at=anchor[1],
            supply_order_id=supply[0],
            supply_delivered_at=supply[1],
            usual_gap_days=evaluation.usual_gap,
            threshold_days=threshold,
            due_on=evaluation.due_on,
            status=status,
            step=0,
            reminders_sent=0,
            next_action_on=None if escalated else self.today,
            escalated_at=self.now if escalated else None,
            escalation_flag=flag,
            is_dry_run=self.cfg.row_set_is_dry_run,
            created_at=self.now,
            updated_at=self.now,
        )
        db.session.add(episode)
        db.session.flush()
        self._event(
            episode,
            "opened",
            payload={
                "usual_gap": evaluation.usual_gap,
                "threshold": str(threshold),
                "due_on": evaluation.due_on.isoformat(),
            },
        )
        self._unit.opened += 1
        if escalated:
            self._event(episode, "escalated", detail=flag, payload=payload)
            self._count(self._unit.escalated, flag)

    def _pair_exists(self, anchor_order_id: int, supply_order_id: int) -> bool:
        return (
            db.session.query(Episode.id)
            .filter_by(
                anchor_order_id=anchor_order_id,
                supply_order_id=supply_order_id,
                is_dry_run=self.cfg.row_set_is_dry_run,
            )
            .first()
            is not None
        )

    # ------------------------------------------------------------------ §12 step 7
    def _act_on_due_steps(self) -> None:
        """Every due open lapse in the active row set, oldest `next_action_on` first, then id. Each
        step is its own unit, stepped on its customer's committed evaluation from steps 3-6."""
        rows = (
            db.session.query(Episode.id, Episode.anchor_order_id)
            .filter(
                Episode.status == "open",
                Episode.is_dry_run.is_(self.cfg.row_set_is_dry_run),
                Episode.next_action_on <= self.today,
            )
            .order_by(Episode.next_action_on, Episode.id)
            .all()
        )
        owners = self._anchor_owners(anchor_id for _episode_id, anchor_id in rows)
        for episode_id, anchor_id in rows:
            customer = self._customers_by_user.get(owners.get(anchor_id))
            evaluation = self._evaluations.get(customer.key) if customer is not None else None
            if evaluation is None:
                continue  # its customer failed to load or to prepare in this run: never step blind
            self._isolated(
                f"{customer.key} episode {episode_id}",
                lambda episode_id=episode_id, customer=customer, evaluation=evaluation: self._step(
                    episode_id, customer, evaluation
                ),
            )

    def _step(self, episode_id: int, customer: ReminderCustomer, evaluation: Evaluation) -> None:
        """§6 "Reminder step k", or the end-of-ladder escalation once step = MAX.

        An out-of-scope customer takes no step: a scope fact is not a channel fact (§5), so its
        channel is trusted only in scope, and the suspend and close steps own its row. A paused
        customer takes no step and keeps its `next_action_on`. Neither does a lapse whose pair has
        moved (the customer reordered) but whose close failed in this run: the next run's close
        step closes it, and no reminder or escalation may reach a customer who already ordered.
        Otherwise the step always advances, in this order: stale (more than GAP_DAYS late) ->
        skipped `stale`; a message not allowed -> skipped with the reason, in every mode; dry-run
        -> simulated, counted in `reminders_sent`; on -> queued and published after the commit.
        """
        if not evaluation.in_scope:
            self._count(self._unit.skipped, "out_of_scope")
            return
        if evaluation.paused:
            self._count(self._unit.skipped, "paused")
            return
        episode = db.session.get(Episode, episode_id)
        if episode.status != "open" or episode.next_action_on is None or episode.next_action_on > self.today:
            return  # a concurrent writer (a Later tap, a reply) moved it after the due query
        if close_decision(episode, customer.own_anchor, customer.supply) is not None:
            self._count(self._unit.skipped, "pending_close")  # the close rule's own answer, not a second one
            return
        due_guard = (Episode.step == episode.step, Episode.next_action_on <= self.today)
        if episode.step >= self.cfg.max_per_episode:
            self._escalate(episode, self._end_flag(episode, evaluation), extra=due_guard)
            return
        step = episode.step + 1
        stale = (self.today - episode.next_action_on).days > self.cfg.gap_days
        channel = evaluation.channel
        # The dry-run row set is the simulated one: it records what would have been sent.
        simulated = not stale and channel.channel is not None and self.cfg.row_set_is_dry_run
        wait = self.cfg.gap_days if step < self.cfg.max_per_episode else self.cfg.escalate_after_days
        values = {"step": step, "next_action_on": self.today + timedelta(days=wait)}
        if simulated:
            values["reminders_sent"] = Episode.reminders_sent + 1
        if not self._transition(episode, ("open",), extra=due_guard, **values):
            return  # a concurrent writer (a Later tap, a duplicate run) moved it first
        if stale:
            self._event(episode, "reminder_skipped", step=step, detail="stale")
            self._count(self._unit.skipped, "stale")
        elif channel.channel is None:
            self._event(episode, "reminder_skipped", step=step, detail=channel.reason)
            self._count(self._unit.skipped, channel.reason)
        elif simulated:
            self._event(episode, "reminder_simulated", step=step, channel=channel.channel)
            self._unit.simulated += 1
        else:
            self._event(episode, "reminder_queued", step=step, channel=channel.channel)
            queue_reminder_send(episode.id, step)
            self._unit.queued += 1

    @staticmethod
    def _end_flag(episode: Episode, evaluation: Evaluation) -> str:
        """§6 End-of-ladder escalation: the first flag that applies. `stopped` (customer-off) and
        `blocked` (the recipient blocked the bot, D19) are the channel rule's own reasons, read from
        `eligibility.channel_for`, never derived again here."""
        if evaluation.channel.reason in (REASON_STOPPED, REASON_BLOCKED):
            return evaluation.channel.reason
        events = {
            (kind, detail)
            for kind, detail in db.session.query(ReorderReminderEvent.kind, ReorderReminderEvent.detail)
            .filter(ReorderReminderEvent.episode_id == episode.id)
            .all()
        }
        # Dry-run rows read `reminder_simulated` where live rows read `reminder_sent` (§13).
        reached = any(kind in ("reminder_sent", "reminder_simulated") for kind, _detail in events)
        if ("reminder_skipped", REASON_NO_CHANNEL) in events and not reached:
            return REASON_NO_CHANNEL
        if any(kind == "later" for kind, _detail in events):
            return "snoozed"
        return "no_response"

    # ------------------------------------------------------------------ §12 step 8
    def _republish_stale_queued(self) -> None:
        """A send whose latest event is still `reminder_queued` at the episode's current step,
        REPUBLISH_AFTER or more on, is published again. Safe: the bot route dedupes on its request
        id and the send task re-checks everything. It recovers a publish the after-commit hook lost.
        Runs in mode `on` only, so the active row set here is always the live one."""
        steps = dict(
            db.session.query(Episode.id, Episode.step)
            .filter(Episode.status == "open", Episode.is_dry_run.is_(self.cfg.row_set_is_dry_run))
            .all()
        )
        cutoff = self.now - REPUBLISH_AFTER
        ids = sorted(steps)
        for offset in range(0, len(ids), _CHUNK):
            latest = (
                select(func.max(ReorderReminderEvent.id))
                .where(ReorderReminderEvent.episode_id.in_(ids[offset : offset + _CHUNK]))
                .group_by(ReorderReminderEvent.episode_id)
            )
            for row in ReorderReminderEvent.query.filter(ReorderReminderEvent.id.in_(latest)).all():
                if (
                    row.kind == "reminder_queued"
                    and row.step == steps.get(row.episode_id)
                    and ensure_utc(row.created_at) <= cutoff
                ):
                    queue_reminder_send(row.episode_id, row.step)
                    self._unit.republished += 1


def _dormant_since_text(row: DormantOutletFacts) -> Optional[str]:
    """How an `outlet_dormant` payload records when the outlet went dormant: the one spelling the
    payload and the spell comparison share."""
    return row.dormant_since.isoformat() if row.dormant_since else None


def _spell(row: DormantOutletFacts) -> Spell:
    return (row.outlet_id, _dormant_since_text(row))


def close_decision(
    episode: Episode, anchor: Optional[Delivered], supply: Optional[Delivered]
) -> Optional[Tuple[str, Optional[Delivered]]]:
    """§6 Close. None while the episode's (own anchor, supply anchor) pair is still the customer's.

    A newer own delivery closes `ordered` when it happened after the episode was created, else
    `superseded` (a link brought in a sibling's earlier delivery). Otherwise a newer supply
    delivery closes `place_resupplied` (D18), or `superseded` when it predates the episode. A pair
    that moved backwards (an unlink, an address leaving its group) closes `superseded`, naming the
    order that moved: the own anchor if it did, else the supply anchor.
    """
    created = ensure_utc(episode.created_at)
    if (
        anchor is not None
        and anchor[0] != episode.anchor_order_id
        and anchor[1] > ensure_utc(episode.anchor_delivered_at)
    ):
        return ("ordered" if anchor[1] > created else "superseded"), anchor
    if (
        supply is not None
        and supply[0] != episode.supply_order_id
        and supply[1] > ensure_utc(episode.supply_delivered_at)
    ):
        return ("place_resupplied" if supply[1] > created else "superseded"), supply
    anchor_moved = anchor is None or anchor[0] != episode.anchor_order_id
    supply_moved = supply is None or supply[0] != episode.supply_order_id
    if anchor_moved or supply_moved:
        return "superseded", anchor if anchor_moved else supply
    return None
