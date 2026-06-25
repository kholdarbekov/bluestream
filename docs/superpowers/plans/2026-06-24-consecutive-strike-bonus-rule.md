# Consecutive-Strike Bonus Rule — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an admin-configurable loyalty rule that awards AquaCoins when a customer achieves one or more existing "order strike" rules N times consecutively (each strike on its own rolling-window cadence), and surface it on the public `/loyalty-guide` page.

**Architecture:** A new `LoyaltyConsecutiveStrikeRule` model composes existing `LoyaltyStreakRule` rows via a many-to-many table. Evaluation is fully stateless and ledger-derived (no per-user counters, no scheduler): a per-strike "consecutive run" is computed by walking that strike's achievement timestamps in the loyalty ledger and breaking on any skipped period (gap ≥ 2× window). Awards are issued synchronously from inside the existing `update_streak()` call, with idempotency and "repeat every N" derived from counting prior meta-awards within the current run.

**Tech Stack:** Flask + SQLAlchemy + Alembic (backend), pytest (backend tests), React + antd + React-Query (admin UI), Vitest (UI tests), Jinja2 + DB-backed translations (public page).

## Global Constraints

- **Reward type is AquaCoins only.** No discount / free-product payout for this rule.
- **Consecutive cadence = each attached strike's own `window_days`.** A strike achievement is "consecutive" with the previous one iff the gap between them is `< 2 × window_days` (no fully-skipped period); a larger gap resets that strike's run to 0.
- **Combine modes:** `combine_mode = 'all'` → every attached strike must reach N (uses `min` of per-strike counts); `'any'` → one is enough (uses `max`).
- **Repeat every N**, idempotent, stateless. No Celery beat; evaluate inside `update_streak()`.
- **Stateless / ledger-derived** like the existing strike engine — no new per-user counter columns.
- **Migration head to branch off:** `c1f2a3b4d5e6` (set `down_revision = "c1f2a3b4d5e6"`).
- **Tests run inside the `business_app` Docker container**, not host Python (the root conftest pulls heavy deps). Admin UI Vitest runs on the host.
- **Translations are DB-backed.** The `loyalty_guide.*` key family lives ONLY in `scripts/seed_backend_translations.py` (verified: 114 keys there, 0 in `scripts/seed_data.py`) — add the new keys there only; do NOT add them to `seed_data.py` (it does not own this family; adding there would be dead). `scripts/` is NOT mounted into `business_app`; reseed via `docker compose exec -T business_app python - < scripts/seed_backend_translations.py`.
- **Commits are performed by the user, not the agent.** Each task ends by staging changes (`git add …`) and stopping; do NOT run `git commit`/`git push`.
- **No hardcoded user-facing copy** in templates — every label goes through the `| t` translation filter.
- Follow existing local patterns: loyalty admin rule CRUD lives in `business_app/api/admin.py` (mirrors `streak-rules`); business *evaluation* logic lives in `business_app/services/loyalty_service.py`.

---

## File Structure

**Backend**
- Modify `business_app/utils/constants.py` — add `LoyaltyActionType.CONSECUTIVE_STREAK_BONUS`.
- Modify `business_app/models/loyalty.py` — add `loyalty_consec_rule_strikes` association table + `LoyaltyConsecutiveStrikeRule` model.
- Create `business_app/migrations/versions/e2c5a8f1b3d7_consecutive_strike_rules.py` — two tables + CHECK constraints.
- Modify `business_app/services/loyalty_service.py` — import the model; add `_strike_achievement_times`, `_strike_consecutive_run`, `get_consecutive_strike_progress`, `_consecutive_awards_since`, `update_consecutive_strikes`; map the new action type in `award_points`; call evaluation from `update_streak`; surface progress in the two dashboard dicts.
- Modify `business_app/api/admin.py` — 4 REST endpoints mirroring the streak-rule CRUD.
- Modify `business_app/frontend/routes.py` — add `consecutive_strike_rules` to `get_public_loyalty_facts`, `get_loyalty_handbook_context`, and the `/api/public/loyalty.json` feed.

**Public page**
- Modify `business_app/templates/frontend/loyalty_guide.html` — new config-driven card + one FAQ entry.
- Modify `scripts/seed_backend_translations.py` — new trilingual keys (the `loyalty_guide.*` family lives only here; `seed_data.py` does not own it).

**Admin UI**
- Modify `admin_ui/src/services/adminService.js` — 4 service methods.
- Modify `admin_ui/src/pages/LoyaltyPrograms.js` — new "Consecutive Strikes" tab (table + modal + mutations).
- Create `admin_ui/src/__tests__/pages/LoyaltyPrograms.consecutive.test.js` — tab + create-payload tests.

**Tests**
- Create `tests/unit/test_loyalty_consecutive_strike_rules.py` — model + evaluation unit tests.
- Create `tests/integration/test_admin_consecutive_strike_rules.py` — admin CRUD + dashboard surfacing.

---

### Task 1: Model, enum, association table & migration

**Files:**
- Modify: `business_app/utils/constants.py:120-130`
- Modify: `business_app/models/loyalty.py` (append after `LoyaltyStreakRule`, ends line 316)
- Create: `business_app/migrations/versions/e2c5a8f1b3d7_consecutive_strike_rules.py`
- Test: `tests/unit/test_loyalty_consecutive_strike_rules.py`

**Interfaces:**
- Produces: `LoyaltyActionType.CONSECUTIVE_STREAK_BONUS` (value `"consecutive_streak_bonus"`); `LoyaltyConsecutiveStrikeRule` model with columns `id, program_id, name, required_consecutive, combine_mode, bonus_points, is_active, starts_at, ends_at, display_order`, relationship `strikes` (→ list of `LoyaltyStreakRule`), methods `to_dict()` and `is_effective(now)`; association table `loyalty_consec_rule_strikes`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_loyalty_consecutive_strike_rules.py`:

```python
from datetime import datetime, timezone, timedelta

from business_app import db
from business_app.models.loyalty import (
    LoyaltyProgram,
    LoyaltyStreakRule,
    LoyaltyConsecutiveStrikeRule,
)
from business_app.utils.constants import LoyaltyActionType


def _program():
    p = LoyaltyProgram(name="Default", is_active=True, is_default=True)
    db.session.add(p)
    db.session.commit()
    return p


def _strike(program, name="3 in 30", required_orders=3, window_days=30, bonus_points=300):
    r = LoyaltyStreakRule(
        program_id=program.id,
        name=name,
        required_orders=required_orders,
        window_days=window_days,
        bonus_points=bonus_points,
        is_active=True,
    )
    db.session.add(r)
    db.session.commit()
    return r


def test_action_type_value():
    assert LoyaltyActionType.CONSECUTIVE_STREAK_BONUS.value == "consecutive_streak_bonus"


def test_model_to_dict_and_strikes(app):
    with app.app_context():
        program = _program()
        s1 = _strike(program, name="3 in 30", window_days=30)
        s2 = _strike(program, name="5 in 40", required_orders=5, window_days=40)
        rule = LoyaltyConsecutiveStrikeRule(
            program_id=program.id,
            name="6-in-a-row Champion",
            required_consecutive=6,
            combine_mode="all",
            bonus_points=1000,
            is_active=True,
        )
        rule.strikes = [s1, s2]
        db.session.add(rule)
        db.session.commit()

        d = rule.to_dict()
        assert d["required_consecutive"] == 6
        assert d["combine_mode"] == "all"
        assert d["bonus_points"] == 1000
        assert sorted(d["strike_rule_ids"]) == sorted([s1.id, s2.id])
        assert {s["name"] for s in d["strikes"]} == {"3 in 30", "5 in 40"}


def test_is_effective_window(app):
    with app.app_context():
        program = _program()
        now = datetime.now(timezone.utc)
        rule = LoyaltyConsecutiveStrikeRule(
            program_id=program.id,
            name="r",
            required_consecutive=6,
            combine_mode="all",
            bonus_points=100,
            is_active=True,
            starts_at=now - timedelta(days=1),
            ends_at=now + timedelta(days=1),
        )
        db.session.add(rule)
        db.session.commit()
        assert rule.is_effective(now) is True

        rule.is_active = False
        assert rule.is_effective(now) is False
        rule.is_active = True
        rule.ends_at = now - timedelta(hours=1)
        assert rule.is_effective(now) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec -T business_app python -m pytest tests/unit/test_loyalty_consecutive_strike_rules.py -v`
Expected: FAIL with `ImportError: cannot import name 'LoyaltyConsecutiveStrikeRule'` / `AttributeError: CONSECUTIVE_STREAK_BONUS`.

- [ ] **Step 3a: Add the enum value**

In `business_app/utils/constants.py`, inside `class LoyaltyActionType`, add after `STREAK_BONUS = "streak_bonus"`:

```python
    STREAK_BONUS = "streak_bonus"
    CONSECUTIVE_STREAK_BONUS = "consecutive_streak_bonus"
```

- [ ] **Step 3b: Add the association table + model**

In `business_app/models/loyalty.py`, immediately after the end of `LoyaltyStreakRule` (after line 316, before `class LoyaltyPoints`), add:

```python
loyalty_consec_rule_strikes = db.Table(
    "loyalty_consec_rule_strikes",
    Column(
        "consecutive_strike_rule_id",
        Integer,
        ForeignKey("loyalty_consecutive_strike_rules.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "streak_rule_id",
        Integer,
        ForeignKey("loyalty_streak_rules.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


@translatable("name")
class LoyaltyConsecutiveStrikeRule(db.Model, TimestampMixin):
    """Admin-configurable consecutive-strike bonus rule.

    Composes one or more ``LoyaltyStreakRule`` ("order strike") rows and awards
    ``bonus_points`` AquaCoins when each (``combine_mode='all'``) or any
    (``combine_mode='any'``) attached strike has been achieved
    ``required_consecutive`` times in a row, on each strike's own ``window_days``
    cadence. Repeats every N; a skipped period resets that strike's run to 0.
    Fully stateless / ledger-derived — no per-user counters.
    """

    __tablename__ = "loyalty_consecutive_strike_rules"

    id = Column(Integer, primary_key=True)
    program_id = Column(Integer, ForeignKey("loyalty_programs.id"), nullable=False, index=True)

    name = Column(String(100), nullable=False)  # user-facing, translatable
    required_consecutive = Column(Integer, nullable=False)
    combine_mode = Column(String(8), nullable=False, default="all")  # 'all' | 'any'
    bonus_points = Column(Integer, nullable=False)

    is_active = Column(Boolean, default=True)
    starts_at = Column(DateTime(timezone=True), nullable=True)
    ends_at = Column(DateTime(timezone=True), nullable=True)
    display_order = Column(Integer, default=0)

    program = relationship("LoyaltyProgram")
    strikes = relationship("LoyaltyStreakRule", secondary=loyalty_consec_rule_strikes)

    def to_dict(self):
        return {
            "id": self.id,
            "program_id": self.program_id,
            "name": self.name,
            "required_consecutive": self.required_consecutive,
            "combine_mode": self.combine_mode,
            "bonus_points": self.bonus_points,
            "is_active": self.is_active,
            "starts_at": self.starts_at.isoformat() if self.starts_at else None,
            "ends_at": self.ends_at.isoformat() if self.ends_at else None,
            "display_order": self.display_order,
            "strikes": [
                {
                    "id": s.id,
                    "name": s.name,
                    "required_orders": s.required_orders,
                    "window_days": s.window_days,
                }
                for s in self.strikes
            ],
            "strike_rule_ids": [s.id for s in self.strikes],
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def is_effective(self, now):
        """True when active and ``now`` is within the optional [starts_at, ends_at]."""
        from business_app.utils.timezone_utils import ensure_utc

        if not self.is_active:
            return False
        if self.starts_at and now < ensure_utc(self.starts_at):
            return False
        if self.ends_at and now > ensure_utc(self.ends_at):
            return False
        return True
```

- [ ] **Step 4: Run model/enum tests to verify they pass**

Run: `docker compose exec -T business_app python -m pytest tests/unit/test_loyalty_consecutive_strike_rules.py -v`
Expected: PASS (the 3 tests above). *(SQLite in tests creates tables from models, so no migration is needed for the test DB.)*

- [ ] **Step 5: Write the Alembic migration**

Create `business_app/migrations/versions/e2c5a8f1b3d7_consecutive_strike_rules.py`:

```python
"""consecutive strike bonus rules

Revision ID: e2c5a8f1b3d7
Revises: c1f2a3b4d5e6
Create Date: 2026-06-24

"""
from alembic import op
import sqlalchemy as sa

revision = "e2c5a8f1b3d7"
down_revision = "c1f2a3b4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "loyalty_consecutive_strike_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("program_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("required_consecutive", sa.Integer(), nullable=False),
        sa.Column("combine_mode", sa.String(length=8), nullable=False, server_default="all"),
        sa.Column("bonus_points", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["program_id"], ["loyalty_programs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("required_consecutive >= 1", name="ck_loyalty_consec_required_pos"),
        sa.CheckConstraint("bonus_points >= 0", name="ck_loyalty_consec_bonus_nonneg"),
        sa.CheckConstraint("combine_mode IN ('all', 'any')", name="ck_loyalty_consec_combine_mode"),
    )
    op.create_index(
        "ix_loyalty_consecutive_strike_rules_program_id",
        "loyalty_consecutive_strike_rules",
        ["program_id"],
    )
    op.create_table(
        "loyalty_consec_rule_strikes",
        sa.Column("consecutive_strike_rule_id", sa.Integer(), nullable=False),
        sa.Column("streak_rule_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["consecutive_strike_rule_id"],
            ["loyalty_consecutive_strike_rules.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["streak_rule_id"], ["loyalty_streak_rules.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("consecutive_strike_rule_id", "streak_rule_id"),
    )


def downgrade():
    op.drop_table("loyalty_consec_rule_strikes")
    op.drop_index(
        "ix_loyalty_consecutive_strike_rules_program_id",
        table_name="loyalty_consecutive_strike_rules",
    )
    op.drop_table("loyalty_consecutive_strike_rules")
```

- [ ] **Step 6: Verify the migration is the single head and is syntactically valid**

Run: `docker compose exec -T business_app sh -c "FLASK_APP=business_app flask db heads"`
Expected: prints exactly one head — `e2c5a8f1b3d7 (head)`.

- [ ] **Step 7: Stage changes (user commits)**

```bash
git add business_app/utils/constants.py business_app/models/loyalty.py \
  business_app/migrations/versions/e2c5a8f1b3d7_consecutive_strike_rules.py \
  tests/unit/test_loyalty_consecutive_strike_rules.py
```

---

### Task 2: Read-path service — consecutive run + progress + dashboard surfacing

**Files:**
- Modify: `business_app/services/loyalty_service.py` (import line 10-18; new methods after `_streak_rule_in_cooldown` ~line 1675; dashboard dicts at lines 314 and 1329)
- Test: `tests/unit/test_loyalty_consecutive_strike_rules.py`

**Interfaces:**
- Consumes: `LoyaltyConsecutiveStrikeRule`, `LoyaltyStreakRule`, `LoyaltyActionType.CONSECUTIVE_STREAK_BONUS` (Task 1); existing `award_points`, `LoyaltyTransaction`, module-level `ensure_utc`.
- Produces: `LoyaltyService._strike_achievement_times(user_id, strike_rule_id) -> list[datetime]` (UTC, newest-first); `LoyaltyService._strike_consecutive_run(user_id, strike_rule, now) -> tuple[int, datetime|None]` (run length, earliest-in-run timestamp); `LoyaltyService.get_consecutive_strike_progress(user_id) -> list[dict]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_loyalty_consecutive_strike_rules.py`:

```python
from business_app.models.loyalty import LoyaltyTransaction
from business_app.utils.constants import LoyaltyTransactionType
from business_app.services.loyalty_service import LoyaltyService


def _award_strike(user_id, strike_rule_id, when):
    """Insert a raw STREAK_BONUS ledger row dated ``when`` (mirrors how
    update_streak records an order-strike achievement)."""
    t = LoyaltyTransaction(
        user_id=user_id,
        transaction_type=LoyaltyTransactionType.EARNED,
        points=300,
        description="strike",
        remaining_points=300,
        extra_data={"action_type": "streak_bonus", "streak_rule_id": strike_rule_id},
    )
    db.session.add(t)
    db.session.flush()
    t.created_at = when
    db.session.commit()
    return t


def test_consecutive_run_counts_back_to_back(app, regular_user):
    with app.app_context():
        program = _program()
        s = _strike(program, window_days=30)
        now = datetime.now(timezone.utc)
        # 4 achievements ~30 days apart (each gap < 60d) -> run of 4
        for k in range(4):
            _award_strike(regular_user.id, s.id, now - timedelta(days=30 * (3 - k)))
        svc = LoyaltyService()
        count, run_start = svc._strike_consecutive_run(regular_user.id, s, now)
        assert count == 4
        assert run_start is not None


def test_consecutive_run_resets_on_skipped_period(app, regular_user):
    with app.app_context():
        program = _program()
        s = _strike(program, window_days=30)
        now = datetime.now(timezone.utc)
        # old pair, then a 90-day gap (> 60d), then 2 recent back-to-back
        _award_strike(regular_user.id, s.id, now - timedelta(days=200))
        _award_strike(regular_user.id, s.id, now - timedelta(days=170))
        _award_strike(regular_user.id, s.id, now - timedelta(days=30))
        _award_strike(regular_user.id, s.id, now - timedelta(days=1))
        svc = LoyaltyService()
        count, _ = svc._strike_consecutive_run(regular_user.id, s, now)
        assert count == 2  # only the most-recent unbroken run


def test_get_consecutive_strike_progress_caps_at_n(app, regular_user):
    with app.app_context():
        program = _program()
        s = _strike(program, window_days=30)
        now = datetime.now(timezone.utc)
        for k in range(8):
            _award_strike(regular_user.id, s.id, now - timedelta(days=30 * (7 - k)))
        rule = LoyaltyConsecutiveStrikeRule(
            program_id=program.id, name="champ", required_consecutive=6,
            combine_mode="all", bonus_points=1000, is_active=True,
        )
        rule.strikes = [s]
        db.session.add(rule)
        db.session.commit()
        svc = LoyaltyService()
        prog = svc.get_consecutive_strike_progress(regular_user.id)
        assert len(prog) == 1
        assert prog[0]["required_consecutive"] == 6
        assert prog[0]["combined_current"] == 6  # capped at N even though run is 8
        assert prog[0]["per_strike"][0]["current"] == 6
```

*(`regular_user` is the project's standard authenticated-customer fixture in `tests/conftest.py`. If a different fixture name is used in this repo, substitute the existing customer fixture.)*

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec -T business_app python -m pytest tests/unit/test_loyalty_consecutive_strike_rules.py -k "run or progress" -v`
Expected: FAIL with `AttributeError: 'LoyaltyService' object has no attribute '_strike_consecutive_run'`.

- [ ] **Step 3a: Import the model in the service**

In `business_app/services/loyalty_service.py`, add `LoyaltyConsecutiveStrikeRule` to the loyalty import block (lines 10-18):

```python
from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyTransaction,
    LoyaltyReward,
    LoyaltyProgram,
    LoyaltyStreakRule,
    LoyaltyConsecutiveStrikeRule,
    ReferralProgram,
    LoyaltyTierConfig,
)
```

- [ ] **Step 3b: Add the read-path methods**

In `business_app/services/loyalty_service.py`, immediately after `_streak_rule_in_cooldown` (ends ~line 1675), add:

```python
    def _strike_achievement_times(self, user_id: int, strike_rule_id: int) -> List[datetime]:
        """All UTC achievement timestamps for one order-strike rule, newest-first.

        Reads the loyalty ledger directly (an achievement = a STREAK_BONUS award
        carrying this ``streak_rule_id`` in ``extra_data``). Filtered in Python to
        stay portable across the JSON ``extra_data`` column, mirroring
        ``_streak_rule_in_cooldown``.
        """
        txns = LoyaltyTransaction.query.filter(LoyaltyTransaction.user_id == user_id).all()
        times: List[datetime] = []
        for t in txns:
            ed = t.extra_data or {}
            if (
                ed.get("action_type") == LoyaltyActionType.STREAK_BONUS.value
                and ed.get("streak_rule_id") == strike_rule_id
                and t.created_at is not None
            ):
                times.append(ensure_utc(t.created_at))
        times.sort(reverse=True)
        return times

    def _strike_consecutive_run(self, user_id: int, strike_rule, now: datetime):
        """Current consecutive-achievement run for one strike, ending at the latest
        achievement. Two adjacent achievements are "consecutive" iff their gap is
        ``< 2 * window_days`` (no fully-skipped period). Returns
        ``(run_length, earliest_run_timestamp)``; ``(0, None)`` if never achieved.
        """
        times = self._strike_achievement_times(user_id, strike_rule.id)
        if not times:
            return 0, None
        limit = timedelta(days=2 * strike_rule.window_days)
        run = [times[0]]
        for prev in times[1:]:
            if run[-1] - prev < limit:
                run.append(prev)
            else:
                break
        return len(run), run[-1]

    def get_consecutive_strike_progress(self, user_id: int):
        """Live progress toward each active, currently-effective consecutive-strike
        rule for the default program. Computed from the ledger — no stored counter."""
        from business_app.utils.helpers import get_current_language

        program = (
            LoyaltyProgram.query.filter_by(is_default=True, is_active=True).first()
            or LoyaltyProgram.query.filter_by(is_active=True).first()
        )
        if not program:
            return []
        now = datetime.now(timezone.utc)
        lang = get_current_language()
        out = []
        rules = (
            LoyaltyConsecutiveStrikeRule.query.filter_by(program_id=program.id, is_active=True)
            .order_by(LoyaltyConsecutiveStrikeRule.display_order.asc())
            .all()
        )
        for rule in rules:
            if not rule.is_effective(now) or not rule.strikes:
                continue
            n = rule.required_consecutive
            per_strike = []
            counts = []
            for s in rule.strikes:
                count, _ = self._strike_consecutive_run(user_id, s, now)
                last_times = self._strike_achievement_times(user_id, s.id)
                active = bool(last_times) and (now - last_times[0]) < timedelta(days=2 * s.window_days)
                counts.append(count)
                per_strike.append(
                    {
                        "strike_name": s.get_translated("name", lang),
                        "current": min(count, n),
                        "target": n,
                        "window_days": s.window_days,
                        "active": active,
                    }
                )
            combined = max(counts) if rule.combine_mode == "any" else min(counts)
            out.append(
                {
                    "name": rule.get_translated("name", lang),
                    "required_consecutive": n,
                    "combine_mode": rule.combine_mode,
                    "bonus_points": rule.bonus_points,
                    "combined_current": min(combined, n),
                    "per_strike": per_strike,
                }
            )
        return out
```

- [ ] **Step 3c: Surface progress in the two dashboard dicts**

In `business_app/services/loyalty_service.py`, at line 314 (inside `get_account_dashboard_for_user`) — directly after the existing `"streak_progress"` entry — add:

```python
            "streak_progress": self.get_streak_progress(user_id),
            "consecutive_strike_progress": self.get_consecutive_strike_progress(user_id),
```

Apply the identical addition at line 1329 (the second `"streak_progress"` entry, in the tier-info path).

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec -T business_app python -m pytest tests/unit/test_loyalty_consecutive_strike_rules.py -v`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Stage changes (user commits)**

```bash
git add business_app/services/loyalty_service.py tests/unit/test_loyalty_consecutive_strike_rules.py
```

---

### Task 3: Award-path service — `update_consecutive_strikes` + hook + idempotency

**Files:**
- Modify: `business_app/services/loyalty_service.py` (`award_points` mapping ~line 758-766; new methods after `get_consecutive_strike_progress`; call site in `update_streak` ~line 1660)
- Test: `tests/unit/test_loyalty_consecutive_strike_rules.py`

**Interfaces:**
- Consumes: `_strike_consecutive_run`, `award_points`, Task-1 model/enum.
- Produces: `LoyaltyService._consecutive_awards_since(user_id, rule_id, since_dt) -> int`; `LoyaltyService.update_consecutive_strikes(user_id, commit=True) -> bool` (returns whether anything was awarded; awards `bonus_points` per completed multiple of N; idempotent). `update_streak` calls `update_consecutive_strikes(commit=False)` after its strike loop and folds its return into the commit condition.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_loyalty_consecutive_strike_rules.py`:

```python
def _consec_rule(program, strikes, n=6, combine_mode="all", bonus=1000):
    rule = LoyaltyConsecutiveStrikeRule(
        program_id=program.id, name="champ", required_consecutive=n,
        combine_mode=combine_mode, bonus_points=bonus, is_active=True,
    )
    rule.strikes = list(strikes)
    db.session.add(rule)
    db.session.commit()
    return rule


def _consec_award_total(user_id, rule_id):
    total = 0
    for t in LoyaltyTransaction.query.filter_by(user_id=user_id).all():
        ed = t.extra_data or {}
        if (
            ed.get("action_type") == "consecutive_streak_bonus"
            and ed.get("consecutive_strike_rule_id") == rule_id
        ):
            total += t.points
    return total


def test_update_consecutive_awards_when_all_reach_n(app, regular_user):
    with app.app_context():
        program = _program()
        a = _strike(program, name="A", window_days=30)
        b = _strike(program, name="B", required_orders=5, window_days=40)
        rule = _consec_rule(program, [a, b], n=6, combine_mode="all", bonus=1000)
        now = datetime.now(timezone.utc)
        for k in range(6):
            _award_strike(regular_user.id, a.id, now - timedelta(days=30 * (5 - k)))
            _award_strike(regular_user.id, b.id, now - timedelta(days=40 * (5 - k)))
        svc = LoyaltyService()
        svc.update_consecutive_strikes(regular_user.id)
        assert _consec_award_total(regular_user.id, rule.id) == 1000


def test_update_consecutive_all_blocks_when_one_short(app, regular_user):
    with app.app_context():
        program = _program()
        a = _strike(program, name="A", window_days=30)
        b = _strike(program, name="B", window_days=40)
        rule = _consec_rule(program, [a, b], n=6, combine_mode="all", bonus=1000)
        now = datetime.now(timezone.utc)
        for k in range(6):
            _award_strike(regular_user.id, a.id, now - timedelta(days=30 * (5 - k)))
        for k in range(3):  # B only reaches 3
            _award_strike(regular_user.id, b.id, now - timedelta(days=40 * (2 - k)))
        svc = LoyaltyService()
        svc.update_consecutive_strikes(regular_user.id)
        assert _consec_award_total(regular_user.id, rule.id) == 0


def test_update_consecutive_any_awards_on_one(app, regular_user):
    with app.app_context():
        program = _program()
        a = _strike(program, name="A", window_days=30)
        b = _strike(program, name="B", window_days=40)
        rule = _consec_rule(program, [a, b], n=6, combine_mode="any", bonus=500)
        now = datetime.now(timezone.utc)
        for k in range(6):
            _award_strike(regular_user.id, a.id, now - timedelta(days=30 * (5 - k)))
        svc = LoyaltyService()
        svc.update_consecutive_strikes(regular_user.id)
        assert _consec_award_total(regular_user.id, rule.id) == 500


def test_update_consecutive_is_idempotent(app, regular_user):
    with app.app_context():
        program = _program()
        a = _strike(program, name="A", window_days=30)
        rule = _consec_rule(program, [a], n=6, combine_mode="all", bonus=1000)
        now = datetime.now(timezone.utc)
        for k in range(6):
            _award_strike(regular_user.id, a.id, now - timedelta(days=30 * (5 - k)))
        svc = LoyaltyService()
        svc.update_consecutive_strikes(regular_user.id)
        svc.update_consecutive_strikes(regular_user.id)  # re-run must not double-award
        assert _consec_award_total(regular_user.id, rule.id) == 1000


def test_update_consecutive_repeats_every_n(app, regular_user):
    with app.app_context():
        program = _program()
        a = _strike(program, name="A", window_days=30)
        rule = _consec_rule(program, [a], n=6, combine_mode="all", bonus=1000)
        now = datetime.now(timezone.utc)
        for k in range(12):  # 12 back-to-back = two completed runs of 6
            _award_strike(regular_user.id, a.id, now - timedelta(days=30 * (11 - k)))
        svc = LoyaltyService()
        svc.update_consecutive_strikes(regular_user.id)
        assert _consec_award_total(regular_user.id, rule.id) == 2000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec -T business_app python -m pytest tests/unit/test_loyalty_consecutive_strike_rules.py -k "update_consecutive" -v`
Expected: FAIL with `AttributeError: ... 'update_consecutive_strikes'`.

- [ ] **Step 3a: Map the new action type to a BONUS ledger row**

In `business_app/services/loyalty_service.py`, in `award_points`, extend the action→transaction-type mapping (after the `SURPRICE_REWARD` branch, ~line 765-766):

```python
        elif action_type == LoyaltyActionType.SURPRICE_REWARD:
            transaction_type_enum = LoyaltyTransactionType.BONUS
        elif action_type == LoyaltyActionType.CONSECUTIVE_STREAK_BONUS:
            transaction_type_enum = LoyaltyTransactionType.BONUS
```

- [ ] **Step 3b: Add the award-path methods**

In `business_app/services/loyalty_service.py`, immediately after `get_consecutive_strike_progress`, add:

```python
    def _consecutive_awards_since(self, user_id: int, rule_id: int, since_dt: datetime) -> int:
        """Number of meta-bonus awards already granted for this rule at or after
        ``since_dt`` (the start of the current combined run). Drives idempotency
        and 'repeat every N' with zero stored state."""
        count = 0
        for t in LoyaltyTransaction.query.filter(LoyaltyTransaction.user_id == user_id).all():
            ed = t.extra_data or {}
            if (
                ed.get("action_type") == LoyaltyActionType.CONSECUTIVE_STREAK_BONUS.value
                and ed.get("consecutive_strike_rule_id") == rule_id
                and t.created_at is not None
                and ensure_utc(t.created_at) >= since_dt
            ):
                count += 1
        return count

    def update_consecutive_strikes(self, user_id: int, commit: bool = True):
        """Award every active, currently-effective consecutive-strike rule the user
        now satisfies. ``combine_mode='all'`` needs every attached strike to reach
        ``required_consecutive``; ``'any'`` needs one. Awards ``bonus_points`` per
        completed multiple of N (repeat every N); idempotent via
        ``_consecutive_awards_since``."""
        program = (
            LoyaltyProgram.query.filter_by(is_default=True, is_active=True).first()
            or LoyaltyProgram.query.filter_by(is_active=True).first()
        )
        if not program:
            return

        now = datetime.now(timezone.utc)
        rules = LoyaltyConsecutiveStrikeRule.query.filter_by(program_id=program.id, is_active=True).all()
        awarded = False

        for rule in rules:
            if not rule.is_effective(now) or not rule.strikes:
                continue
            runs = [self._strike_consecutive_run(user_id, s, now) for s in rule.strikes]
            counts = [c for c, _ in runs]
            starts = [rs for _, rs in runs if rs is not None]
            n = rule.required_consecutive

            if rule.combine_mode == "any":
                combined = max(counts)
                idx = counts.index(combined)
                run_start = runs[idx][1]
            else:  # 'all'
                combined = min(counts)
                # All attached strikes must be currently running; the binding run
                # start is the latest-starting among them.
                run_start = max(starts) if len(starts) == len(rule.strikes) else None

            if combined < n or run_start is None:
                continue

            target_awards = combined // n
            already = self._consecutive_awards_since(user_id, rule.id, run_start)
            for milestone in range(already + 1, target_awards + 1):
                self.award_points(
                    user_id,
                    rule.bonus_points,
                    rule.name,
                    LoyaltyActionType.CONSECUTIVE_STREAK_BONUS,
                    extra_data={"consecutive_strike_rule_id": rule.id, "milestone": milestone},
                    commit=False,
                )
                awarded = True

        if awarded and commit:
            db.session.commit()
        return awarded
```

- [ ] **Step 3c: Hook into `update_streak`**

In `business_app/services/loyalty_service.py`, in `update_streak`, replace the tail (the `if awarded and commit:` block, ~lines 1660-1661) with:

```python
        # A new order-strike achievement is the only event that can advance a
        # consecutive-strike run, so evaluate those rules in the same transaction.
        consec_awarded = self.update_consecutive_strikes(user_id, commit=False)

        if (awarded or consec_awarded) and commit:
            db.session.commit()
```

*(Rationale: `update_consecutive_strikes(commit=False)` may stage meta-awards even when no new base strike fired this call. `award_points(commit=False)` calls `db.session.flush()`, which empties `db.session.new`, so a `dirty/new` probe would miss the flushed-but-uncommitted awards — fold the returned `consec_awarded` bool into the commit condition instead.)*

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec -T business_app python -m pytest tests/unit/test_loyalty_consecutive_strike_rules.py -v`
Expected: PASS (all unit tests).

- [ ] **Step 5: Run the existing streak tests to confirm no regression**

Run: `docker compose exec -T business_app python -m pytest tests/unit/test_loyalty_streak_rules.py -v`
Expected: PASS (unchanged).

- [ ] **Step 6: Stage changes (user commits)**

```bash
git add business_app/services/loyalty_service.py tests/unit/test_loyalty_consecutive_strike_rules.py
```

---

### Task 4: Admin REST CRUD + dashboard integration test

**Files:**
- Modify: `business_app/api/admin.py` (append after `delete_loyalty_streak_rule`, line 7357)
- Test: `tests/integration/test_admin_consecutive_strike_rules.py`

**Interfaces:**
- Consumes: `LoyaltyConsecutiveStrikeRule`, `LoyaltyStreakRule`, existing helpers `success_response`, `validation_error_response`, `not_found_response`, `internal_error_response`, decorators `@jwt_required`, `@validate_admin_action`, `@validate_json`.
- Produces: REST endpoints
  - `GET /admin/loyalty/consecutive-strike-rules?program_id=` → `{data: {consecutive_strike_rules: [...], count: n}}`
  - `POST /admin/loyalty/consecutive-strike-rules` (body: `name, required_consecutive, combine_mode, bonus_points, strike_rule_ids[], program_id?, is_active?, display_order?, translations?`)
  - `PUT /admin/loyalty/consecutive-strike-rules/<id>`
  - `DELETE /admin/loyalty/consecutive-strike-rules/<id>`

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_admin_consecutive_strike_rules.py`:

```python
from business_app import db
from business_app.models.loyalty import LoyaltyProgram, LoyaltyStreakRule


def _seed(app):
    with app.app_context():
        p = LoyaltyProgram.query.filter_by(is_default=True).first()
        if not p:
            p = LoyaltyProgram(name="Default", is_active=True, is_default=True)
            db.session.add(p)
            db.session.commit()
        s = LoyaltyStreakRule(
            program_id=p.id, name="3 in 30", required_orders=3,
            window_days=30, bonus_points=300, is_active=True,
        )
        db.session.add(s)
        db.session.commit()
        return p.id, s.id


def test_create_list_update_delete(app, admin_auth_headers):
    program_id, strike_id = _seed(app)

    # Create
    resp = app.test_client().post(
        "/api/admin/loyalty/consecutive-strike-rules",
        json={
            "name": "6-in-a-row Champion",
            "required_consecutive": 6,
            "combine_mode": "all",
            "bonus_points": 1000,
            "strike_rule_ids": [strike_id],
            "program_id": program_id,
        },
        headers=admin_auth_headers,
    )
    assert resp.status_code == 201, resp.get_json()
    rule = resp.get_json()["data"]["consecutive_strike_rule"]
    assert rule["strike_rule_ids"] == [strike_id]
    rule_id = rule["id"]

    # List
    resp = app.test_client().get(
        f"/api/admin/loyalty/consecutive-strike-rules?program_id={program_id}",
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200
    assert resp.get_json()["data"]["count"] == 1

    # Update
    resp = app.test_client().put(
        f"/api/admin/loyalty/consecutive-strike-rules/{rule_id}",
        json={"required_consecutive": 3, "combine_mode": "any"},
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200
    assert resp.get_json()["data"]["consecutive_strike_rule"]["required_consecutive"] == 3

    # Delete
    resp = app.test_client().delete(
        f"/api/admin/loyalty/consecutive-strike-rules/{rule_id}",
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200


def test_create_rejects_empty_strikes(app, admin_auth_headers):
    program_id, _ = _seed(app)
    resp = app.test_client().post(
        "/api/admin/loyalty/consecutive-strike-rules",
        json={
            "name": "bad", "required_consecutive": 6, "combine_mode": "all",
            "bonus_points": 1000, "strike_rule_ids": [], "program_id": program_id,
        },
        headers=admin_auth_headers,
    )
    assert resp.status_code == 400
```

*(`admin_auth_headers` is the project's admin-JWT fixture used by the other `tests/integration/test_admin_*` files. Match the exact fixture name those tests use.)*

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec -T business_app python -m pytest tests/integration/test_admin_consecutive_strike_rules.py -v`
Expected: FAIL with 404 (route not registered).

- [ ] **Step 3: Add the endpoints**

In `business_app/api/admin.py`, append after `delete_loyalty_streak_rule` (line 7357):

```python
def _resolve_strikes(program_id, strike_rule_ids):
    """Load LoyaltyStreakRule rows by id, requiring all to exist and belong to
    ``program_id``. Returns (rows, error_message)."""
    from business_app.models.loyalty import LoyaltyStreakRule

    ids = list(dict.fromkeys(strike_rule_ids or []))
    if not ids:
        return None, "At least one order-strike must be attached"
    rows = LoyaltyStreakRule.query.filter(LoyaltyStreakRule.id.in_(ids)).all()
    if len(rows) != len(ids):
        return None, "One or more attached strikes do not exist"
    if any(r.program_id != program_id for r in rows):
        return None, "Attached strikes must belong to the same program"
    return rows, None


@admin_bp.route("/loyalty/consecutive-strike-rules", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_loyalty", "manage_loyalty"])
def get_loyalty_consecutive_strike_rules():
    """List consecutive-strike rules (optionally filtered by program_id)."""
    try:
        from business_app.models.loyalty import LoyaltyConsecutiveStrikeRule

        program_id = request.args.get("program_id", type=int)
        query = LoyaltyConsecutiveStrikeRule.query
        if program_id:
            query = query.filter_by(program_id=program_id)
        else:
            default_program = LoyaltyProgram.query.filter_by(is_default=True, is_active=True).first()
            if default_program:
                query = query.filter_by(program_id=default_program.id)
        rules = query.order_by(LoyaltyConsecutiveStrikeRule.display_order.asc()).all()
        return success_response(
            data={
                "consecutive_strike_rules": [
                    {**rule.to_dict(), "translations": {"name": rule.get_all_translations("name")}}
                    for rule in rules
                ],
                "count": len(rules),
            }
        )
    except Exception as e:
        current_app.logger.error(f"Get consecutive-strike rules error: {e}")
        return internal_error_response("Failed to get consecutive-strike rules")


@admin_bp.route("/loyalty/consecutive-strike-rules", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_loyalty"])
@validate_json(["name", "required_consecutive", "bonus_points", "strike_rule_ids"])
def create_loyalty_consecutive_strike_rule():
    """Create a consecutive-strike rule.

    Body: name, required_consecutive (>= 1), combine_mode ('all'|'any'),
    bonus_points (>= 0), strike_rule_ids[], program_id?, is_active?,
    display_order?, translations?.
    """
    try:
        from business_app.models.loyalty import LoyaltyConsecutiveStrikeRule

        data = request.get_json()

        program_id = data.get("program_id")
        if not program_id:
            default_program = LoyaltyProgram.query.filter_by(is_default=True).first()
            if not default_program:
                return validation_error_response("No default loyalty program found")
            program_id = default_program.id

        if int(data["required_consecutive"]) < 1:
            return validation_error_response("required_consecutive must be ≥ 1")
        if int(data["bonus_points"]) < 0:
            return validation_error_response("bonus_points must be ≥ 0")
        combine_mode = data.get("combine_mode", "all")
        if combine_mode not in ("all", "any"):
            return validation_error_response("combine_mode must be 'all' or 'any'")

        strikes, err = _resolve_strikes(program_id, data["strike_rule_ids"])
        if err:
            return validation_error_response(err)

        rule = LoyaltyConsecutiveStrikeRule(
            program_id=program_id,
            name=data["name"],
            required_consecutive=int(data["required_consecutive"]),
            combine_mode=combine_mode,
            bonus_points=int(data["bonus_points"]),
            is_active=data.get("is_active", True),
            display_order=data.get("display_order", 0),
        )
        rule.strikes = strikes
        db.session.add(rule)
        db.session.commit()

        if "translations" in data:
            rule.set_translations(data["translations"])
            db.session.commit()

        current_app.logger.info(f"Consecutive-strike rule created: {rule.name} (ID: {rule.id})")
        return success_response(
            data={"consecutive_strike_rule": rule.to_dict()},
            message="Consecutive-strike rule created successfully",
            status_code=201,
        )
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create consecutive-strike rule error: {e}")
        return internal_error_response("Failed to create consecutive-strike rule")


@admin_bp.route("/loyalty/consecutive-strike-rules/<int:rule_id>", methods=["PUT"])
@jwt_required()
@validate_admin_action(["manage_loyalty"])
def update_loyalty_consecutive_strike_rule(rule_id):
    """Update a consecutive-strike rule (incl. re-attaching strikes)."""
    try:
        from business_app.models.loyalty import LoyaltyConsecutiveStrikeRule

        rule = LoyaltyConsecutiveStrikeRule.query.get(rule_id)
        if not rule:
            return not_found_response("Consecutive-strike rule not found")
        data = request.get_json()

        for field, caster in (
            ("name", str),
            ("required_consecutive", int),
            ("bonus_points", int),
            ("display_order", int),
            ("is_active", bool),
        ):
            if field in data and data[field] is not None:
                setattr(rule, field, caster(data[field]))
        if "combine_mode" in data and data["combine_mode"] is not None:
            if data["combine_mode"] not in ("all", "any"):
                return validation_error_response("combine_mode must be 'all' or 'any'")
            rule.combine_mode = data["combine_mode"]
        if "strike_rule_ids" in data:
            strikes, err = _resolve_strikes(rule.program_id, data["strike_rule_ids"])
            if err:
                return validation_error_response(err)
            rule.strikes = strikes
        if rule.required_consecutive < 1:
            return validation_error_response("required_consecutive must be ≥ 1")
        if rule.bonus_points < 0:
            return validation_error_response("bonus_points must be ≥ 0")

        db.session.commit()
        if "translations" in data:
            rule.set_translations(data["translations"])
            db.session.commit()
        return success_response(
            data={"consecutive_strike_rule": rule.to_dict()},
            message="Consecutive-strike rule updated",
        )
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update consecutive-strike rule error: {e}")
        return internal_error_response("Failed to update consecutive-strike rule")


@admin_bp.route("/loyalty/consecutive-strike-rules/<int:rule_id>", methods=["DELETE"])
@jwt_required()
@validate_admin_action(["manage_loyalty"])
def delete_loyalty_consecutive_strike_rule(rule_id):
    """Delete a consecutive-strike rule (hard delete — no per-user state)."""
    try:
        from business_app.models.loyalty import LoyaltyConsecutiveStrikeRule

        rule = LoyaltyConsecutiveStrikeRule.query.get(rule_id)
        if not rule:
            return not_found_response("Consecutive-strike rule not found")
        db.session.delete(rule)
        db.session.commit()
        return success_response(message="Consecutive-strike rule deleted successfully")
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete consecutive-strike rule error: {e}")
        return internal_error_response("Failed to delete consecutive-strike rule")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec -T business_app python -m pytest tests/integration/test_admin_consecutive_strike_rules.py -v`
Expected: PASS.

- [ ] **Step 5: Regenerate the API route snapshot**

Run: `docker compose exec -T -e UPDATE_API_SNAPSHOT=1 business_app python -m pytest tests/ -k api_snapshot -q`
Expected: PASS; the snapshot file now includes the 4 new routes. (If the repo's snapshot test has a different selector, run the snapshot test the repo uses with `UPDATE_API_SNAPSHOT=1`.)

- [ ] **Step 6: Stage changes (user commits)**

```bash
git add business_app/api/admin.py tests/integration/test_admin_consecutive_strike_rules.py
git add tests/  # include the regenerated API snapshot file
```

---

### Task 5: Admin UI — service methods, "Consecutive Strikes" tab, Vitest

**Files:**
- Modify: `admin_ui/src/services/adminService.js:661-680` (after the streak-rule methods)
- Modify: `admin_ui/src/pages/LoyaltyPrograms.js` (new tab, table columns, modal, query + mutations)
- Create: `admin_ui/src/__tests__/pages/LoyaltyPrograms.consecutive.test.js`

**Interfaces:**
- Consumes: Task-4 REST endpoints.
- Produces: `adminService.getLoyaltyConsecutiveStrikeRules`, `createLoyaltyConsecutiveStrikeRule`, `updateLoyaltyConsecutiveStrikeRule`, `deleteLoyaltyConsecutiveStrikeRule`; a new tab keyed `consecutive_strikes` in `LoyaltyPrograms`.

- [ ] **Step 1: Write the failing Vitest**

Create `admin_ui/src/__tests__/pages/LoyaltyPrograms.consecutive.test.js`:

```javascript
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import LoyaltyPrograms from '../../pages/LoyaltyPrograms';
import adminService from '../../services/adminService';
import { createWrapper } from '../testUtils';

vi.mock('../../services/adminService');

describe('LoyaltyPrograms — Consecutive Strikes tab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getLoyaltyPrograms.mockResolvedValue({
      items: [{ id: 1, name: 'Default', is_default: true, is_active: true, member_count: 0, tier_count: 0 }],
      total: 1,
    });
    adminService.getLoyaltyTiers.mockResolvedValue({ items: [] });
    adminService.getLoyaltyStreakRules.mockResolvedValue({
      streak_rules: [
        { id: 7, name: '3 in 30', required_orders: 3, window_days: 30, bonus_points: 300, min_order_amount: null, is_active: true, translations: { name: {} } },
      ],
      streak_rule_count: 1,
    });
    adminService.getLoyaltyConsecutiveStrikeRules.mockResolvedValue({
      consecutive_strike_rules: [
        { id: 1, name: '6-in-a-row', required_consecutive: 6, combine_mode: 'all', bonus_points: 1000, is_active: true, strike_rule_ids: [7], strikes: [{ id: 7, name: '3 in 30' }], translations: { name: {} } },
      ],
      count: 1,
    });
    adminService.createLoyaltyConsecutiveStrikeRule.mockResolvedValue({ id: 2 });
  });

  it('renders the consecutive-strike row after switching tabs', async () => {
    render(<LoyaltyPrograms />, { wrapper: createWrapper() });
    expect(await screen.findByText('Total Programs')).toBeInTheDocument();
    fireEvent.click(await screen.findByText('Consecutive Strikes'));
    expect(await screen.findByText('6-in-a-row')).toBeInTheDocument();
  });

  it('submits the exact create payload incl. strike_rule_ids and combine_mode', async () => {
    render(<LoyaltyPrograms />, { wrapper: createWrapper() });
    await screen.findByText('Total Programs');
    fireEvent.click(await screen.findByText('Consecutive Strikes'));
    fireEvent.click(await screen.findByText('Add Consecutive Strike'));

    const dialog = await screen.findByRole('dialog');
    fireEvent.change(dialog.querySelector('#name'), { target: { value: 'Champ' } });
    fireEvent.change(dialog.querySelector('#required_consecutive'), { target: { value: '6' } });
    fireEvent.change(dialog.querySelector('#bonus_points'), { target: { value: '1000' } });
    // combine_mode defaults to 'all'; attach strike 7 via the multi-select
    // (test util helper selects the option by label)
    // ...select interaction omitted for brevity; see existing streak test for the antd Select pattern...

    fireEvent.click(screen.getByRole('button', { name: /create/i }));
    await waitFor(() => expect(adminService.createLoyaltyConsecutiveStrikeRule).toHaveBeenCalled());
    const payload = adminService.createLoyaltyConsecutiveStrikeRule.mock.calls[0][0];
    expect(payload).toMatchObject({
      name: 'Champ',
      required_consecutive: 6,
      bonus_points: 1000,
      combine_mode: 'all',
      strike_rule_ids: [7],
    });
  });
});
```

*(The antd multi-`Select` interaction follows the same pattern the existing `LoyaltyRewards.test.js` uses for `free_product` selects — open the selector, click the option by title. Reuse that helper.)*

- [ ] **Step 2: Run the test to verify it fails**

Run: `bash -c 'cd admin_ui && npx vitest run src/__tests__/pages/LoyaltyPrograms.consecutive.test.js'`
Expected: FAIL (`getLoyaltyConsecutiveStrikeRules is not a function` / "Consecutive Strikes" tab not found).

- [ ] **Step 3a: Add the service methods**

In `admin_ui/src/services/adminService.js`, after `deleteLoyaltyStreakRule` (line 680), add:

```javascript
  // Loyalty Consecutive-Strike Rule management
  async getLoyaltyConsecutiveStrikeRules(params = {}) {
    const response = await api.get('/admin/loyalty/consecutive-strike-rules', { params });
    return response.data?.data || { consecutive_strike_rules: [], count: 0 };
  }

  async createLoyaltyConsecutiveStrikeRule(ruleData) {
    const response = await api.post('/admin/loyalty/consecutive-strike-rules', ruleData);
    return response.data?.data?.consecutive_strike_rule || response.data;
  }

  async updateLoyaltyConsecutiveStrikeRule(ruleId, ruleData) {
    const response = await api.put(`/admin/loyalty/consecutive-strike-rules/${ruleId}`, ruleData);
    return response.data?.data?.consecutive_strike_rule || response.data;
  }

  async deleteLoyaltyConsecutiveStrikeRule(ruleId) {
    const response = await api.delete(`/admin/loyalty/consecutive-strike-rules/${ruleId}`);
    return response.data;
  }
```

- [ ] **Step 3b: Add state, query, mutations in `LoyaltyPrograms.js`**

Near the streak-rule state/query/mutations (the `streakRulesQuery`, `createStreakRuleMutation`, etc.), add the parallel constructs:

```javascript
  const [consecModal, setConsecModal] = useState({ open: false, rule: null });
  const [consecForm] = Form.useForm();

  const consecRulesQuery = useQuery({
    queryKey: ['loyalty-consecutive-strike-rules', selectedProgramId],
    queryFn: () => adminService.getLoyaltyConsecutiveStrikeRules({ program_id: selectedProgramId }),
    enabled: Boolean(selectedProgramId),
    placeholderData: keepPreviousData,
  });
  const consecRules = consecRulesQuery.data?.consecutive_strike_rules || [];

  const createConsecRuleMutation = useMutation({
    mutationFn: (values) => adminService.createLoyaltyConsecutiveStrikeRule(values),
    onSuccess: () => {
      message.success(t('ui.loyalty.consec_create_success', { defaultValue: 'Consecutive-strike rule created' }));
      setConsecModal({ open: false, rule: null });
      consecForm.resetFields();
      invalidateLoyaltyQueries();
    },
  });
  const updateConsecRuleMutation = useMutation({
    mutationFn: ({ ruleId, values }) => adminService.updateLoyaltyConsecutiveStrikeRule(ruleId, values),
    onSuccess: () => {
      message.success(t('ui.loyalty.consec_update_success', { defaultValue: 'Consecutive-strike rule updated' }));
      setConsecModal({ open: false, rule: null });
      consecForm.resetFields();
      invalidateLoyaltyQueries();
    },
  });
  const deleteConsecRuleMutation = useMutation({
    mutationFn: (ruleId) => adminService.deleteLoyaltyConsecutiveStrikeRule(ruleId),
    onSuccess: () => {
      message.success(t('ui.loyalty.consec_delete_success', { defaultValue: 'Consecutive-strike rule removed' }));
      invalidateLoyaltyQueries();
    },
  });
```

Add `'loyalty-consecutive-strike-rules'` to whatever `invalidateLoyaltyQueries` invalidates (so create/update/delete refresh the table).

- [ ] **Step 3c: Add table columns + the tab + the modal**

Add a `consecRuleColumns` memo (mirror `streakRuleColumns`) with columns: name; `required_consecutive`; `combine_mode` (render `value === 'any' ? 'Any' : 'All'`); attached strikes (render `record.strikes.map(s => <Tag key={s.id}>{s.name}</Tag>)`); `bonus_points`; `is_active` (Tag); actions (edit → `setConsecModal({open:true, rule:record})` + `consecForm.setFieldsValue({...record, strike_rule_ids: record.strike_rule_ids, name_ru: record.translations?.name?.ru, name_uz: record.translations?.name?.uz})`; delete → `Modal.confirm` → `deleteConsecRuleMutation.mutate(record.id)`).

Add a new tab to the `<Tabs>` `items` array, after the `streak_rules` tab:

```jsx
{
  key: 'consecutive_strikes',
  label: t('ui.loyalty.tab_consecutive_strikes', { defaultValue: 'Consecutive Strikes' }),
  children: (
    <>
      <div className="table-actions">
        <Space wrap>
          <Select
            placeholder={t('ui.loyalty.program', { defaultValue: 'Program' })}
            style={{ width: 240 }}
            value={selectedProgramId}
            onChange={setSelectedProgramId}
            options={programs.map((program) => ({ value: program.id, label: program.name }))}
          />
        </Space>
        <Space>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            disabled={!selectedProgramId}
            onClick={() => {
              setConsecModal({ open: true, rule: null });
              consecForm.resetFields();
              consecForm.setFieldsValue({ is_active: true, combine_mode: 'all' });
            }}
          >
            {t('ui.loyalty.create_consecutive_strike', { defaultValue: 'Add Consecutive Strike' })}
          </Button>
        </Space>
      </div>
      <Table
        rowKey="id"
        columns={consecRuleColumns}
        dataSource={consecRules}
        loading={consecRulesQuery.isLoading}
        locale={{ emptyText: <EmptyState description={t('ui.loyalty.no_consecutive_strikes', { defaultValue: 'No consecutive-strike rules configured' })} /> }}
        pagination={false}
      />
    </>
  ),
}
```

Add the modal (mirror the streak modal; `<Form form={consecForm} layout="vertical" onFinish={...}>`). The `onFinish` builds the payload:

```jsx
onFinish={(values) => {
  const payload = {
    name: values.name,
    required_consecutive: Number(values.required_consecutive),
    combine_mode: values.combine_mode || 'all',
    bonus_points: Number(values.bonus_points),
    strike_rule_ids: values.strike_rule_ids || [],
    is_active: values.is_active,
    program_id: selectedProgramId,
    translations: { name: { ru: values.name_ru || undefined, uz: values.name_uz || undefined } },
  };
  if (consecModal.rule) {
    updateConsecRuleMutation.mutate({ ruleId: consecModal.rule.id, values: payload });
    return;
  }
  createConsecRuleMutation.mutate(payload);
}}
```

Form fields: `name` (`<Input id="name" />`, required), `name_ru`/`name_uz` (`<Input />`), `required_consecutive` (`<InputNumber id="required_consecutive" min={2} style={{width:'100%'}} />`, required), `combine_mode` (`<Select options={[{value:'all',label:t('ui.loyalty.combine_all',{defaultValue:'All strikes (AND)'})},{value:'any',label:t('ui.loyalty.combine_any',{defaultValue:'Any strike (OR)'})}]} />`), `bonus_points` (`<InputNumber id="bonus_points" min={0} style={{width:'100%'}} />`, required), `strike_rule_ids` (`<Select mode="multiple" options={streakRules.map(s => ({ value: s.id, label: s.name }))} />`, required), `is_active` (`<Switch />`, `valuePropName="checked"`). Footer: Cancel + `AsyncButton type="primary" htmlType="submit"`.

- [ ] **Step 4: Run the Vitest to verify it passes**

Run: `bash -c 'cd admin_ui && npx vitest run src/__tests__/pages/LoyaltyPrograms.consecutive.test.js'`
Expected: PASS.

- [ ] **Step 5: Run the existing LoyaltyPrograms tests for no regression**

Run: `bash -c 'cd admin_ui && npx vitest run src/__tests__/pages/LoyaltyPrograms.streak.test.js'`
Expected: PASS.

- [ ] **Step 6: Stage changes (user commits)**

```bash
git add admin_ui/src/services/adminService.js admin_ui/src/pages/LoyaltyPrograms.js \
  admin_ui/src/__tests__/pages/LoyaltyPrograms.consecutive.test.js
```

---

### Task 6: Public facts, handbook context & `loyalty.json` feed

**Files:**
- Modify: `business_app/frontend/routes.py` (`get_public_loyalty_facts` 732-825; `get_loyalty_handbook_context` 828-891; `/api/public/loyalty.json` builder ~2008-2065)
- Test: `tests/integration/test_admin_consecutive_strike_rules.py` (append a public-facts assertion) or the existing public-loyalty test module.

**Interfaces:**
- Consumes: `LoyaltyConsecutiveStrikeRule` (Task 1), the existing `i18n(entity, field)` helper inside `get_public_loyalty_facts`.
- Produces: `facts["consecutive_strike_rules"]` (language-keyed); `handbook["consecutive_strike_rules"]` (single-language); `loyalty.json["consecutiveStrikeBonuses"]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_admin_consecutive_strike_rules.py`:

```python
def test_public_facts_include_consecutive_rules(app):
    program_id, strike_id = _seed(app)
    with app.app_context():
        from business_app.models.loyalty import LoyaltyConsecutiveStrikeRule
        rule = LoyaltyConsecutiveStrikeRule(
            program_id=program_id, name="6-in-a-row", required_consecutive=6,
            combine_mode="all", bonus_points=1000, is_active=True,
        )
        from business_app.models.loyalty import LoyaltyStreakRule
        rule.strikes = [LoyaltyStreakRule.query.get(strike_id)]
        db.session.add(rule)
        db.session.commit()

        from business_app.frontend.routes import get_public_loyalty_facts
        facts = get_public_loyalty_facts()
        assert "consecutive_strike_rules" in facts
        assert facts["consecutive_strike_rules"][0]["required_consecutive"] == 6
        assert facts["consecutive_strike_rules"][0]["bonus_points"] == 1000
        assert facts["consecutive_strike_rules"][0]["strike_names"]  # non-empty
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `docker compose exec -T business_app python -m pytest tests/integration/test_admin_consecutive_strike_rules.py::test_public_facts_include_consecutive_rules -v`
Expected: FAIL with `KeyError: 'consecutive_strike_rules'`.

- [ ] **Step 3a: Extend `get_public_loyalty_facts`**

In `business_app/frontend/routes.py`, add `LoyaltyConsecutiveStrikeRule` to the import block at the top of `get_public_loyalty_facts` (lines 742-747), then after the `streak_rules` list comprehension (line 792) add:

```python
    consec_rows = (
        LoyaltyConsecutiveStrikeRule.query.filter_by(program_id=program.id, is_active=True)
        .order_by(LoyaltyConsecutiveStrikeRule.display_order.asc())
        .all()
        if program
        else []
    )
    consecutive_strike_rules = [
        {
            "name": i18n(r, "name"),
            "required_consecutive": r.required_consecutive,
            "combine_mode": r.combine_mode,
            "bonus_points": r.bonus_points,
            "strike_names": [{lang: s.get_translated("name", lang) for lang in languages} for s in r.strikes],
        }
        for r in consec_rows
        if r.is_effective(now) and r.strikes
    ]
```

Add it to the returned dict (after `"streak_rules": streak_rules,`, line 823):

```python
        "streak_rules": streak_rules,
        "consecutive_strike_rules": consecutive_strike_rules,
```

- [ ] **Step 3b: Extend `get_loyalty_handbook_context`**

In `get_loyalty_handbook_context`, after the `streak_rules` list (line 876) add:

```python
    consecutive_strike_rules = [
        {
            "name": r["name"].get(lang) or r["name"].get("uz"),
            "required_consecutive": r["required_consecutive"],
            "combine_mode": r["combine_mode"],
            "bonus_points": r["bonus_points"],
            "strike_names": [s.get(lang) or s.get("uz") for s in r["strike_names"]],
        }
        for r in facts["consecutive_strike_rules"]
    ]
```

Add to the returned dict (after `"streak_rules": streak_rules,`, line 886):

```python
        "streak_rules": streak_rules,
        "consecutive_strike_rules": consecutive_strike_rules,
```

- [ ] **Step 3c: Extend the `loyalty.json` feed**

In `business_app/frontend/routes.py`, in the `/api/public/loyalty.json` builder (the dict containing `"streakBonuses": facts["streak_rules"]` ~line 2065), add:

```python
        "streakBonuses": facts["streak_rules"],
        "consecutiveStrikeBonuses": facts["consecutive_strike_rules"],
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `docker compose exec -T business_app python -m pytest tests/integration/test_admin_consecutive_strike_rules.py::test_public_facts_include_consecutive_rules -v`
Expected: PASS.

- [ ] **Step 5: Stage changes (user commits)**

```bash
git add business_app/frontend/routes.py tests/integration/test_admin_consecutive_strike_rules.py
```

---

### Task 7: `/loyalty-guide` card + FAQ + trilingual seed

**Files:**
- Modify: `business_app/templates/frontend/loyalty_guide.html` (Earn section ~lines 137-150; FAQ list ~lines 15-23)
- Modify: `scripts/seed_backend_translations.py` (loyalty_guide.earn.* block, near the existing `loyalty_guide.earn.streak_line` at ~line 7215)

**Interfaces:**
- Consumes: `handbook.consecutive_strike_rules` (Task 6).
- Produces: rendered card + FAQ entry; new translation keys `loyalty_guide.earn.consec_title`, `loyalty_guide.earn.consec_line_all`, `loyalty_guide.earn.consec_line_any`, `loyalty_guide.earn.consec_repeat`, `loyalty_guide.faq.q9`, `loyalty_guide.faq.a9`.

- [ ] **Step 1: Add the card to the template**

In `business_app/templates/frontend/loyalty_guide.html`, immediately after the streak card block (the `{% if handbook.streak_rules %}…{% endif %}` ending ~line 150), add:

```html
{% if handbook.consecutive_strike_rules %}
<article class="lg-card">
  <span class="lg-ico" aria-hidden="true"><i class="fas fa-trophy"></i></span>
  <h3>{{ 'loyalty_guide.earn.consec_title' | t }}</h3>
  <ul class="lg-streak-list">
    {% for rule in handbook.consecutive_strike_rules %}
    <li>
      <strong>{{ rule.name }}</strong>
      <span>
        {% set joiner = (' loyalty_guide.earn.consec_and ' | t) if rule.combine_mode == 'all' else (' loyalty_guide.earn.consec_or ' | t) %}
        {% set strikes_text = rule.strike_names | join(joiner) %}
        {% if rule.combine_mode == 'all' %}
          {{ 'loyalty_guide.earn.consec_line_all' | t(strikes=strikes_text, n=rule.required_consecutive, pts=pts(rule.bonus_points), unit=('loyalty_guide.unit.points' | t)) }}
        {% else %}
          {{ 'loyalty_guide.earn.consec_line_any' | t(strikes=strikes_text, n=rule.required_consecutive, pts=pts(rule.bonus_points), unit=('loyalty_guide.unit.points' | t)) }}
        {% endif %}
        · {{ 'loyalty_guide.earn.consec_repeat' | t(n=rule.required_consecutive) }}
      </span>
    </li>
    {% endfor %}
  </ul>
</article>
{% endif %}
```

*(`pts(...)` and `'loyalty_guide.unit.points'` are the existing helpers used by the streak card — reuse them verbatim.)*

- [ ] **Step 2: Add the FAQ entry to the template**

In the FAQ namespace block (~lines 15-23), after the last unconditional `faq.items.append(...)` (q8) and before the surprise-conditional append, add:

```html
{% if handbook.consecutive_strike_rules %}{% set _ = faq.items.append({'q': 'loyalty_guide.faq.q9' | t, 'a': 'loyalty_guide.faq.a9' | t}) %}{% endif %}
```

- [ ] **Step 3: Add the trilingual keys to `seed_backend_translations.py`**

In `scripts/seed_backend_translations.py`, in the loyalty_guide block (near the existing `loyalty_guide.earn.streak_*` keys), add (using the file's existing `_ui_tr(en, uz, ru)` helper):

```python
    'loyalty_guide.earn.consec_title': _ui_tr(
        'Loyalty Streaks', 'Sodiqlik seriyalari', 'Серии лояльности'),
    'loyalty_guide.earn.consec_and': _ui_tr('and', 'va', 'и'),
    'loyalty_guide.earn.consec_or': _ui_tr('or', 'yoki', 'или'),
    'loyalty_guide.earn.consec_line_all': _ui_tr(
        'Achieve {strikes} {n} times in a row → +{pts} {unit}',
        '{strikes} ni ketma-ket {n} marta bajaring → +{pts} {unit}',
        'Выполните {strikes} {n} раз подряд → +{pts} {unit}'),
    'loyalty_guide.earn.consec_line_any': _ui_tr(
        'Achieve {strikes} {n} times in a row → +{pts} {unit}',
        '{strikes} dan birini ketma-ket {n} marta bajaring → +{pts} {unit}',
        'Выполните {strikes} {n} раз подряд → +{pts} {unit}'),
    'loyalty_guide.earn.consec_repeat': _ui_tr(
        'Repeats every {n}', 'Har {n} tadan keyin takrorlanadi', 'Повторяется каждые {n}'),
    'loyalty_guide.faq.q9': _ui_tr(
        'How do loyalty streaks work?',
        'Sodiqlik seriyalari qanday ishlaydi?',
        'Как работают серии лояльности?'),
    'loyalty_guide.faq.a9': _ui_tr(
        'Keep achieving the same order goal in consecutive periods. Reach the required number of consecutive achievements and you earn bonus AquaCoins — then it repeats. Skipping a period resets the streak.',
        'Bir xil buyurtma maqsadini ketma-ket davrlarda bajaring. Talab qilingan ketma-ket bajarishlar soniga yeting va bonus AquaCoins olasiz — keyin u takrorlanadi. Davrni o‘tkazib yuborsangiz seriya nolga tushadi.',
        'Достигайте одной и той же цели по заказам в последовательные периоды. Наберите нужное число последовательных достижений и получите бонусные AquaCoins — затем всё повторяется. Пропуск периода сбрасывает серию.'),
```

- [ ] **Step 4: Confirm no `seed_data.py` change is needed**

Verify the `loyalty_guide.*` family is NOT seeded by `scripts/seed_data.py`:
Run: `grep -c "loyalty_guide\." scripts/seed_data.py`
Expected: `0`. All `loyalty_guide.*` keys live in `scripts/seed_backend_translations.py` only (114 keys), so do NOT add anything to `seed_data.py` — that would be dead/inconsistent.

- [ ] **Step 5: Verify the template renders (smoke test)**

Run: `docker compose exec -T business_app python -m pytest tests/ -k "loyalty_guide or loyalty_handbook" -q`
Expected: PASS (existing page/handbook tests still pass with the new context key present). If there is no such test, instead run a render smoke check:
`docker compose exec -T business_app python -c "from business_app import create_app; app=create_app(); c=app.test_client(); r=c.get('/loyalty-guide'); print(r.status_code)"`
Expected: `200`.

- [ ] **Step 6: Stage changes (user commits)**

```bash
git add business_app/templates/frontend/loyalty_guide.html scripts/seed_backend_translations.py
```

---

### Task 8: Full-suite verification & dev migration

**Files:** none (verification only).

- [ ] **Step 1: Run the full backend suite**

Run: `bash scripts/precommit-backend-tests.sh`
Expected: PASS. (A single pre-existing midnight/COD time-of-day flake unrelated to this work is acceptable per repo history; everything loyalty-related must pass.)

- [ ] **Step 2: Run the full admin UI suite**

Run: `bash -c 'cd admin_ui && npm test --silent'`
Expected: PASS.

- [ ] **Step 3: Apply the migration on dev (after testing on a DB copy)**

Per CLAUDE.md, test on a database copy first, then:
Run: `docker compose exec business_app sh -c "FLASK_APP=business_app flask db upgrade"`
Expected: `Running upgrade c1f2a3b4d5e6 -> e2c5a8f1b3d7`.

Verify the tables exist:
Run: `docker compose exec -T postgres psql -U postgres -d bluestream_db -c "\d loyalty_consecutive_strike_rules" -c "\d loyalty_consec_rule_strikes"`
Expected: both tables print with the CHECK constraints on the parent table.

- [ ] **Step 4: Reseed translations on dev**

Run: `docker compose exec -T business_app python - < scripts/seed_backend_translations.py`
Expected: completes without error; the new `loyalty_guide.earn.consec_*` and `faq.q9/a9` keys are present.

- [ ] **Step 5: Restart services**

Run: `docker compose restart business_app celery_worker telegram_bot`
Then rebuild the admin UI image if the deploy serves a production build:
Run: `docker compose build admin_ui`

- [ ] **Step 6: Stage any remaining changes and hand off to the user**

```bash
git add -A
git status
```
Report the staged diff to the user for review and commit. Do NOT commit or push.

---

## Self-Review

**Spec coverage** (each spec §, mapped to a task):
- §4.1 model / §4.2 association / §4.3 migration / §4.4 enum → **Task 1**.
- §5.1 per-strike run / §5.2 combined / §6.1 progress → **Task 2**.
- §5.3 award + repeat-every-N + idempotency / §5.4 trigger hook → **Task 3**.
- §6.2 admin CRUD → **Task 4**; §6.1 dashboard surfacing → **Task 2 (Step 3c)**.
- §7 admin UI → **Task 5**.
- §8 public facts/handbook/loyalty.json → **Task 6**; loyalty-guide card + FAQ + trilingual seed → **Task 7**.
- §9 testing → distributed across tasks; §10 deploy → **Task 8**.
- §11 out-of-scope (bot, discount/free-product, backfill) → intentionally not implemented.

**Type/name consistency check:** `combine_mode` values `'all'`/`'any'` used consistently in model default, migration CHECK, service (`max` for `'any'`, `min` for `'all'`), admin validation, and UI. `strike_rule_ids` is the create/update key end-to-end (admin payload ↔ `_resolve_strikes` ↔ `to_dict`). Progress dict keys (`required_consecutive`, `combine_mode`, `bonus_points`, `combined_current`, `per_strike[].current/active`) match between `get_consecutive_strike_progress` (Task 2) and its test. Public-facts key `consecutive_strike_rules` and feed key `consecutiveStrikeBonuses` match between Task 6 producer and Task 6/7 consumers. Action-type value `"consecutive_streak_bonus"` matches between enum (Task 1), `award_points` mapping (Task 3), and `_consecutive_awards_since` filter (Task 3).

**Placeholder scan:** No TBD/TODO. The two "match the existing fixture name" notes (`regular_user`, `admin_auth_headers`) and the antd multi-select interaction note point to concrete existing patterns in named test files rather than leaving logic unspecified.
