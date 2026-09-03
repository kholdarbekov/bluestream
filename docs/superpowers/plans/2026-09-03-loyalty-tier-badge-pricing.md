# Loyalty Tier Badge Pricing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every loyalty benefit follow the tier badge the customer can actually see, and show the customer the number that decides that badge.

**Architecture:** One module-level `effective_tier(account)` helper becomes the single answer to "which tier applies now", returning the higher of the stored badge and the live qualifying-points tier. The cash discount and the earning multiplier both call it, so they can never disagree. `_check_tier_upgrade` stays the only writer of the badge. Around that: the bot loyalty screen switches from lifetime to rolling-12-month points, downgrades gain an explained notification, order screens print the discount line, and the admin tier editor gains ladder validation, impact confirmation and an audit row.

**Tech Stack:** Flask, SQLAlchemy, Celery, python-telegram-bot, pytest, React (admin_ui) with Ant Design + react-query.

**Spec:** `docs/superpowers/specs/2026-09-03-loyalty-tier-lock-and-points-display-design.md` — read it before Task 1.

## Global Constraints

- **This repository root is a PRODUCTION host.** Never run the test suite in the `bluestream-business_app-1` container: its `REDIS_URL` points at production Redis and `tests/conftest.py`'s autouse `reset_redis_state` fixture calls `flushdb()`. Use the isolated runner in "Test Runner Setup" below, always.
- **Never `git add -A`.** A ~258 MB untracked `netwatch.log` sits in the tree. Stage explicit paths only.
- **No migration in this plan.** No column is added or altered.
- **No test may assert a tier percentage or threshold it did not itself seed.** Production, dev and an older migration hold three different sets, so no assertion may reference a DB-resident number. Integration tests seed through `tests/integration/tier_discount_factory.py`; unit tests may build `LoyaltyTierConfig` rows directly. Literal numbers in a test are correct exactly when that test seeded them.
- **Customer-facing copy is never rendered as a tier expiry.** A tier does not expire; `tier_valid_until` is a downgrade guarantee floor. No new copy may show that date.
- **All new customer copy ships in `en`, `uz` and `ru`.** Bot copy goes in `scripts/seed_backend_translations.py` under category `telegram`; notification copy goes in `DEFAULT_TEMPLATES` in `business_app/services/notification_service.py`.
- **Comment style:** compact and factual. Explain the invariant, never the incident history.
- **`_check_tier_upgrade` remains the only writer of `loyalty_points.current_tier`.** No task may write the badge from anywhere else.

## Test Runner Setup

Run once at the start of the session. The host has no pytest; this builds a throwaway container that cannot reach production Redis.

```bash
docker network create bs-testnet 2>/dev/null || true
docker rm -f bs-test-redis bs-test-app 2>/dev/null || true
docker run -d --name bs-test-redis --network bs-testnet --network-alias redis redis:8-alpine
docker run -d --name bs-test-app --network bs-testnet \
  -v /home/umar/bluestream:/app -w /app \
  -e REDIS_URL=redis://redis:6379/15 \
  -e BUSINESS_APP_URL=http://unroutable.invalid \
  -e TELEGRAM_BOT_TOKEN=test-token \
  -e STAFF_TELEGRAM_BOT_TOKEN=test-token \
  -e CLICK_MERCHANT_ID=1 -e CLICK_SERVICE_ID=1 -e CLICK_SECRET_KEY=test \
  -e DEFAULT_LANGUAGE=uz \
  bluestream-business_app:latest sleep infinity
```

Every test command in this plan takes the form:

```bash
docker exec bs-test-app python -m pytest <paths> -n0 --no-cov -q
```

`-n0` disables xdist (`pytest.ini` sets `-n auto`); `--no-cov` bypasses the `--cov-fail-under=80` gate that fails single-file runs.

Tear down at the end of the session:

```bash
docker rm -f bs-test-app bs-test-redis && docker network rm bs-testnet
```

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `business_app/services/loyalty_service.py` | `effective_tier` helper; discount quote; earning multiplier; badge maintenance; summary payload | 1, 2, 3, 4, 5 |
| `tests/unit/test_effective_tier.py` (new) | `effective_tier` resolution matrix | 1, 2 |
| `tests/integration/test_tier_discount_badge_pricing.py` (new) | The production shape end to end: badge Silver, points short, discount still granted | 1 |
| `business_app/utils/constants.py` | `NotificationType.LOYALTY_TIER_DOWNGRADE` | 4 |
| `business_app/services/notification_service.py` | Event→type mapping, single-channel set, downgrade templates | 4 |
| `business_app/utils/loyalty_award_dispatch.py` | `KIND_TIER_DOWNGRADE` and its drain branch | 4 |
| `tests/unit/test_tier_downgrade_notification.py` (new) | Downgrade parks exactly one event; blocked downgrade parks none | 3, 4 |
| `telegram_bot/handlers/loyalty.py` | Loyalty screen rendering | 5 |
| `scripts/seed_backend_translations.py` | Bot copy in three languages | 5 |
| `telegram_bot/utils.py` | Order summary money breakdown | 6 |
| `tests/telegram_bot/test_order_summary_discount_line.py` (new) | Breakdown renders when discounted, stays silent at zero | 6 |
| `business_app/api/admin.py` | Tier ladder validation, impact confirmation, audit row | 7 |
| `tests/integration/test_admin_tier_config_guardrails.py` (new) | 422 gap, 409 impact, audit row | 7 |
| `admin_ui/src/pages/LoyaltyPrograms.js` | Surfaces the 409 as a confirm dialog | 8 |

---

### Task 1: `effective_tier()` and the cash discount

**Files:**
- Modify: `business_app/services/loyalty_service.py:136` (insert helper after `apply_tier_discount_for_rail`), `business_app/services/loyalty_service.py:185-205` (`quote_tier_discount` body)
- Test: `tests/unit/test_effective_tier.py` (create), `tests/integration/test_tier_discount_badge_pricing.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `effective_tier(account: LoyaltyPoints) -> Optional[LoyaltyTierConfig]`, module-level in `business_app.services.loyalty_service`. Tasks 2 and 5 import it by that exact name.

- [ ] **Step 1: Write the failing unit test**

Create `tests/unit/test_effective_tier.py`:

```python
"""effective_tier resolves the tier a member's benefits follow.

Price follows the badge. The guarantee date is never consulted here — it is a
downgrade floor owned by _check_tier_upgrade, not an input to pricing.
"""

from datetime import datetime, timedelta, timezone

import pytest

from business_app import db as _db
from business_app.models.loyalty import LoyaltyPoints, LoyaltyProgram, LoyaltyTierConfig, LoyaltyTransaction
from business_app.services.loyalty_service import effective_tier
from business_app.utils.constants import LoyaltyTransactionType


@pytest.fixture
def program(db):
    program = LoyaltyProgram(name="Test program", is_active=True, is_default=True, uzs_per_point=250)
    db.session.add(program)
    db.session.commit()
    return program


@pytest.fixture
def ladder(db, program):
    """Bronze 0 / Silver 4000 / Gold 15000, at test-owned rates."""
    tiers = {}
    for name, order, floor, rate in (
        ("Bronze", 0, 0, 0),
        ("Silver", 1, 4000, 1.5),
        ("Gold", 2, 15000, 2.0),
    ):
        tier = LoyaltyTierConfig(
            program_id=program.id,
            name=name,
            display_order=order,
            min_points=floor,
            discount_percentage=rate,
            points_multiplier=1.0,
            is_active=True,
        )
        db.session.add(tier)
        tiers[name] = tier
    db.session.commit()
    return tiers


def _account(db, user, program, *, badge, qualifying, guarantee_days=365):
    account = LoyaltyPoints(
        user_id=user.id,
        program_id=program.id,
        current_tier=badge,
        total_earned=qualifying,
        current_balance=qualifying,
    )
    if guarantee_days is not None:
        account.tier_valid_until = datetime.now(timezone.utc) + timedelta(days=guarantee_days)
    db.session.add(account)
    db.session.flush()
    if qualifying:
        db.session.add(
            LoyaltyTransaction(
                user_id=user.id,
                transaction_type=LoyaltyTransactionType.EARNED,
                points=qualifying,
                remaining_points=qualifying,
                description="seed",
            )
        )
    db.session.commit()
    return account


def test_badge_wins_when_live_points_fall_short(db, sample_user, program, ladder):
    """The production defect: badge Silver, points below the raised floor."""
    account = _account(db, sample_user, program, badge="Silver", qualifying=3488)

    assert effective_tier(account).name == "Silver"


def test_live_wins_when_badge_has_not_caught_up(db, sample_user, program, ladder):
    """An admin cut the floor; the benefit must not wait for the monthly job."""
    account = _account(db, sample_user, program, badge="Bronze", qualifying=20000)

    assert effective_tier(account).name == "Gold"


def test_guarantee_date_is_not_consulted(db, sample_user, program, ladder):
    """A lapsed guarantee must not reprice a member whose badge still says Silver."""
    account = _account(db, sample_user, program, badge="Silver", qualifying=3488, guarantee_days=-30)

    assert effective_tier(account).name == "Silver"


def test_badge_naming_a_missing_config_falls_back_to_live(db, sample_user, program, ladder):
    account = _account(db, sample_user, program, badge="Diamond", qualifying=3488)

    assert effective_tier(account).name == "Bronze"


def test_inactive_badge_tier_is_ignored(db, sample_user, program, ladder):
    ladder["Silver"].is_active = False
    db.session.commit()
    account = _account(db, sample_user, program, badge="Silver", qualifying=3488)

    assert effective_tier(account).name == "Bronze"


def test_returns_none_when_no_tiers_configured(db, sample_user, program):
    account = _account(db, sample_user, program, badge="Silver", qualifying=3488)

    assert effective_tier(account) is None
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker exec bs-test-app python -m pytest tests/unit/test_effective_tier.py -n0 --no-cov -q
```

Expected: collection error — `ImportError: cannot import name 'effective_tier' from 'business_app.services.loyalty_service'`.

- [ ] **Step 3: Add the helper**

In `business_app/services/loyalty_service.py`, immediately after `apply_tier_discount_for_rail` ends (the `return new_tier_discount` at line 136) and before `class LoyaltyService:`:

```python
def effective_tier(account) -> Optional[LoyaltyTierConfig]:
    """The tier whose benefits ``account`` is entitled to right now.

    The stored badge counts, so a threshold edit can never reprice a member
    without also visibly demoting them. The live tier counts too, so a badge
    that has not caught up with a lowered threshold can only help. The higher
    of the two wins.

    ``tier_valid_until`` is deliberately not read here: it is a downgrade
    guarantee owned by ``_check_tier_upgrade``, not an expiry, and consulting
    it would drop a benefit before the badge the customer sees changed.
    """
    if account is None:
        return None

    stored = LoyaltyTierConfig.query.filter_by(
        name=account.current_tier,
        program_id=account.program_id,
        is_active=True,
    ).first()
    live = LoyaltyTierConfig.get_tier_for_points(
        LoyaltyService().calculate_qualifying_points(account.user_id),
        account.program_id,
    )
    candidates = [tier for tier in (stored, live) if tier is not None]
    return max(candidates, key=lambda tier: tier.display_order, default=None)
```

- [ ] **Step 4: Run the unit test to verify it passes**

```bash
docker exec bs-test-app python -m pytest tests/unit/test_effective_tier.py -n0 --no-cov -q
```

Expected: `6 passed`.

- [ ] **Step 5: Write the failing integration test**

Create `tests/integration/test_tier_discount_badge_pricing.py`:

```python
"""The cash discount follows the badge, not a live recomputation.

Reproduces the production shape that broke: a member promoted to Silver, then
an admin raising Silver's floor above their trailing-365-day points. The badge
still reads Silver, so the discount must still apply.
"""

from decimal import Decimal

from business_app.models.loyalty import LoyaltyPoints
from business_app.services.loyalty_service import LoyaltyService
from shared.enums import PaymentMethod
from tests.integration.tier_discount_factory import seed_account, seed_program, seed_tier

TIER_RATE = Decimal("1.5")
BASIS = Decimal("54000")


def _silver_badge_below_floor(db, user):
    program = seed_program(db)
    seed_tier(db, program, name="Bronze", rate=Decimal("0"), min_points=0, display_order=0)
    seed_tier(db, program, name="Silver", rate=TIER_RATE, min_points=4000, display_order=1)
    seed_account(db, user, program, qualifying_points=3488)

    account = LoyaltyPoints.query.filter_by(user_id=user.id).first()
    account.current_tier = "Silver"
    db.session.commit()
    return account


def test_cash_quote_uses_the_badge(db, sample_user):
    _silver_badge_below_floor(db, sample_user)

    quote = LoyaltyService().quote_tier_discount(sample_user, BASIS, PaymentMethod.CASH)

    assert quote.tier_name == "Silver"
    assert quote.percentage == TIER_RATE
    assert quote.amount == (BASIS * TIER_RATE / Decimal("100")).quantize(Decimal("0.01"))


def test_click_quote_stays_zero(db, sample_user):
    _silver_badge_below_floor(db, sample_user)

    quote = LoyaltyService().quote_tier_discount(sample_user, BASIS, PaymentMethod.CLICK)

    assert quote.amount == Decimal("0.00")
    assert quote.tier_name is None
```

- [ ] **Step 6: Run it to verify it fails**

```bash
docker exec bs-test-app python -m pytest tests/integration/test_tier_discount_badge_pricing.py -n0 --no-cov -q
```

Expected: `test_cash_quote_uses_the_badge` FAILS with `assert None == 'Silver'` — the live recomputation still returns Bronze.

- [ ] **Step 7: Point the quote at the badge**

In `business_app/services/loyalty_service.py`, replace lines 190-197 of `quote_tier_discount` (from `account = LoyaltyPoints.query...` through the `if tier is None:` block):

```python
        account = LoyaltyPoints.query.filter_by(user_id=user.id).first()
        if account is None:
            return NO_TIER_DISCOUNT

        tier = effective_tier(account)
        if tier is None:
            return NO_TIER_DISCOUNT
```

Update gate 3's docstring bullet in the same method to read:

```
        3. A tier resolves for them — the higher of their badge and their
           live qualifying points (``effective_tier``).
```

An absent account cannot hold a non-zero tier: points are only ever written alongside an account, so this is behaviour-preserving.

- [ ] **Step 8: Run both test files to verify they pass**

```bash
docker exec bs-test-app python -m pytest tests/unit/test_effective_tier.py tests/integration/test_tier_discount_badge_pricing.py -n0 --no-cov -q
```

Expected: `8 passed`.

- [ ] **Step 9: Run the existing tier-discount suite for regressions**

```bash
docker exec bs-test-app python -m pytest tests/integration/test_tier_discount_never_fiscalized.py tests/integration/test_tier_discount_order_creation.py tests/integration/test_tier_discount_rail_flip.py tests/integration/test_tier_discount_stacking.py tests/integration/test_cart_estimate_quote_surface.py -n0 --no-cov -q
```

Expected: all pass. If one fails, it seeded a badge inconsistent with its points — fix the fixture, not `effective_tier`.

- [ ] **Step 10: Commit**

```bash
git add business_app/services/loyalty_service.py tests/unit/test_effective_tier.py tests/integration/test_tier_discount_badge_pricing.py
git commit -m "fix(loyalty): price the COD discount off the tier badge, not a live recomputation

effective_tier returns the higher of the stored badge and the live
qualifying-points tier, so a threshold edit can never silently reprice a
member whose badge still shows the higher tier. Restores the discount for
the 12 members stranded by the 2026-09-02 Silver floor raise.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: The earning multiplier uses the same tier

**Files:**
- Modify: `business_app/services/loyalty_service.py:255-257` (`calculate_points_for_purchase`)
- Test: `tests/unit/test_effective_tier.py` (append)

**Interfaces:**
- Consumes: `effective_tier(account)` from Task 1.
- Produces: nothing new.

`_get_tier_multiplier(tier_name, program_id)` keeps its signature — `tests/unit/test_loyalty_service_business_rules.py:88` monkeypatches it, and that seam must survive. Only the tier *name* handed to it changes.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_effective_tier.py`:

```python
def test_earning_multiplier_follows_the_same_tier_as_the_discount(db, sample_user, program, ladder):
    """Earning and the discount must never resolve different tiers."""
    from business_app.services.loyalty_service import LoyaltyService

    ladder["Silver"].points_multiplier = 1.05
    db.session.commit()
    _account(db, sample_user, program, badge="Silver", qualifying=3488)

    # 250 UZS per point, so 250000 UZS is 1000 base points before the multiplier.
    points = LoyaltyService().calculate_points_for_purchase(sample_user.id, 250000)

    assert points == 1050
```

- [ ] **Step 2: Run it to confirm it passes**

```bash
docker exec bs-test-app python -m pytest tests/unit/test_effective_tier.py::test_earning_multiplier_follows_the_same_tier_as_the_discount -n0 --no-cov -q
```

Expected: PASS. This is a characterization test, not the TDD red step — it pins the behaviour the refactor in Step 5 must preserve. The red step for this task is Step 3. Do not "fix" this test if it passes; that is the required outcome.

- [ ] **Step 3: Add the test that actually fails**

Append to `tests/unit/test_effective_tier.py`:

```python
def test_earning_multiplier_uses_live_tier_when_badge_lags(db, sample_user, program, ladder):
    """An admin cut Gold's floor; earning must not wait for the monthly job."""
    from business_app.services.loyalty_service import LoyaltyService

    ladder["Gold"].points_multiplier = 1.10
    db.session.commit()
    _account(db, sample_user, program, badge="Bronze", qualifying=20000)

    points = LoyaltyService().calculate_points_for_purchase(sample_user.id, 250000)

    assert points == 1100
```

- [ ] **Step 4: Run it to verify it fails**

```bash
docker exec bs-test-app python -m pytest tests/unit/test_effective_tier.py::test_earning_multiplier_uses_live_tier_when_badge_lags -n0 --no-cov -q
```

Expected: FAIL with `assert 1000 == 1100` — the badge says Bronze at 1.0×.

- [ ] **Step 5: Resolve the tier through `effective_tier`**

In `business_app/services/loyalty_service.py`, replace lines 255-257:

```python
        # Get tier-based multiplier from database (preferred) or constants (fallback)
        current_tier = account.current_tier or "Bronze"
        multiplier = self._get_tier_multiplier(current_tier, account.program_id)
```

with:

```python
        # Same tier the discount uses, so the two benefits cannot disagree.
        tier = effective_tier(account)
        current_tier = tier.name if tier else (account.current_tier or "Bronze")
        multiplier = self._get_tier_multiplier(current_tier, account.program_id)
```

- [ ] **Step 6: Run the file to verify all pass**

```bash
docker exec bs-test-app python -m pytest tests/unit/test_effective_tier.py -n0 --no-cov -q
```

Expected: `9 passed`.

- [ ] **Step 7: Run the loyalty business-rule regressions**

```bash
docker exec bs-test-app python -m pytest tests/unit/test_loyalty_service_business_rules.py -n0 --no-cov -q
```

Expected: all pass — the `_get_tier_multiplier` monkeypatch seam is unchanged.

- [ ] **Step 8: Commit**

```bash
git add business_app/services/loyalty_service.py tests/unit/test_effective_tier.py
git commit -m "fix(loyalty): resolve the earning multiplier through effective_tier

Earning read the badge while the discount recomputed live, so one benefit of
a tier could be granted while the other was refused. Both now resolve the
same tier; the _get_tier_multiplier seam is preserved.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Stale `points_to_next_tier` and the negative progress bar

**Files:**
- Modify: `business_app/services/loyalty_service.py:1729-1737` (`_check_tier_upgrade` CASE 2), `business_app/services/loyalty_service.py:451-460` (`get_account_dashboard_for_user` return)
- Test: `tests/unit/test_tier_downgrade_notification.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: nothing new. Task 4 appends to the same test file.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_tier_downgrade_notification.py`:

```python
"""Badge maintenance: what _check_tier_upgrade does when a downgrade is blocked."""

from datetime import datetime, timedelta, timezone

import pytest

from business_app.models.loyalty import LoyaltyPoints, LoyaltyProgram, LoyaltyTierConfig, LoyaltyTransaction
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.constants import LoyaltyTransactionType


@pytest.fixture
def program(db):
    program = LoyaltyProgram(name="Test program", is_active=True, is_default=True, uzs_per_point=250)
    db.session.add(program)
    db.session.commit()
    return program


@pytest.fixture
def ladder(db, program):
    for name, order, floor in (("Bronze", 0, 0), ("Silver", 1, 4000), ("Gold", 2, 15000)):
        db.session.add(
            LoyaltyTierConfig(
                program_id=program.id,
                name=name,
                display_order=order,
                min_points=floor,
                discount_percentage=0,
                points_multiplier=1.0,
                is_active=True,
            )
        )
    db.session.commit()


def _account(db, user, program, *, badge, qualifying, guarantee_days, stale_next=99999):
    account = LoyaltyPoints(
        user_id=user.id,
        program_id=program.id,
        current_tier=badge,
        total_earned=qualifying,
        current_balance=qualifying,
        points_to_next_tier=stale_next,
        tier_valid_until=datetime.now(timezone.utc) + timedelta(days=guarantee_days),
    )
    db.session.add(account)
    db.session.flush()
    db.session.add(
        LoyaltyTransaction(
            user_id=user.id,
            transaction_type=LoyaltyTransactionType.EARNED,
            points=qualifying,
            remaining_points=qualifying,
            description="seed",
        )
    )
    db.session.commit()
    return account


def test_blocked_downgrade_refreshes_points_to_next_tier(db, sample_user, program, ladder):
    """The guarantee holds the badge, but the next-tier target must stay true."""
    account = _account(db, sample_user, program, badge="Silver", qualifying=3488, guarantee_days=365)

    LoyaltyService()._check_tier_upgrade(account)
    db.session.commit()

    assert account.current_tier == "Silver"
    assert account.points_to_next_tier == 15000 - 3488


def test_dashboard_progress_never_goes_negative(db, sample_user, program, ladder):
    """A member below their own badge's floor must not render a negative bar."""
    _account(db, sample_user, program, badge="Silver", qualifying=3488, guarantee_days=365)

    dashboard = LoyaltyService().get_account_dashboard_for_user(sample_user.id)

    assert dashboard["tier_progress"]["current"] >= 0
    assert dashboard["tier_progress"]["points_needed"] >= 0
```

- [ ] **Step 2: Run it to verify it fails**

```bash
docker exec bs-test-app python -m pytest tests/unit/test_tier_downgrade_notification.py -n0 --no-cov -q
```

Expected: `test_blocked_downgrade_refreshes_points_to_next_tier` FAILS with `assert 99999 == 11512`. Note whether the dashboard test also fails; if it passes, keep it as a pin.

- [ ] **Step 3: Refresh the target on the blocked path**

In `business_app/services/loyalty_service.py`, replace the CASE 2 block:

```python
        elif target_weight < current_weight:
            if not account.tier_valid_until or ensure_utc(account.tier_valid_until) < now:
                # Lock expired, and points support lower tier -> Downgrade
                account.current_tier = target_tier_name
                account.tier_valid_until = None

                # Recalculate next tier target
                self._update_points_to_next_tier(account, target_tier_config)
```

with:

```python
        elif target_weight < current_weight:
            if not account.tier_valid_until or ensure_utc(account.tier_valid_until) < now:
                # Guarantee lapsed and points support the lower tier -> downgrade
                account.current_tier = target_tier_name
                account.tier_valid_until = None

                # Recalculate next tier target
                self._update_points_to_next_tier(account, target_tier_config)
            elif current_tier_config:
                # Guarantee holds the badge; the next-tier target must still
                # track the member's real qualifying points.
                self._update_points_to_next_tier(account, current_tier_config)
```

- [ ] **Step 4: Clamp the dashboard progress**

In `business_app/services/loyalty_service.py`, in `get_account_dashboard_for_user`, replace the `tier_progress` dict:

```python
            "tier_progress": {
                "current": current_progress,
                "next_tier_points": next_tier_progress_target,
                "points_needed": points_needed,
            },
```

with:

```python
            "tier_progress": {
                # A member below their own badge's floor would otherwise render
                # a negative bar width on the customer loyalty page.
                "current": max(0, current_progress),
                "next_tier_points": next_tier_progress_target,
                "points_needed": max(0, points_needed),
            },
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
docker exec bs-test-app python -m pytest tests/unit/test_tier_downgrade_notification.py -n0 --no-cov -q
```

Expected: `2 passed`.

- [ ] **Step 6: Commit**

```bash
git add business_app/services/loyalty_service.py tests/unit/test_tier_downgrade_notification.py
git commit -m "fix(loyalty): keep points_to_next_tier true when the guarantee blocks a downgrade

CASE 2 skipped the refresh on the blocked path, so members held at a tier by
their guarantee kept a target computed against a retired threshold. Also
clamps the dashboard tier-progress figures, which went negative for a member
sitting below their own badge's floor.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Explained downgrade notification

**Files:**
- Modify: `business_app/utils/constants.py:63` (add enum member), `business_app/services/notification_service.py:72-87` (event map and single-channel set), `business_app/services/notification_service.py:4593` (templates, after the `loyalty_tier_upgrade` email entry), `business_app/utils/loyalty_award_dispatch.py:23` and `:52-66` (kind and drain branch), `business_app/services/loyalty_service.py` (`_send_tier_downgrade_notification` next to `_send_tier_upgrade_notification` at line 2301, and the CASE 2 park)
- Test: `tests/unit/test_tier_downgrade_notification.py` (append)

**Interfaces:**
- Consumes: the CASE 2 structure from Task 3.
- Produces: `KIND_TIER_DOWNGRADE = "tier_downgrade"` in `business_app.utils.loyalty_award_dispatch`; `LoyaltyService._send_tier_downgrade_notification(user_id, *, tier, tier_config_id, qualifying_points, required_points)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_tier_downgrade_notification.py`:

```python
def test_real_downgrade_parks_exactly_one_downgrade_event(db, sample_user, program, ladder):
    from business_app import db as _db
    from business_app.utils.loyalty_award_dispatch import KIND_TIER_DOWNGRADE, PENDING_KEY

    account = _account(db, sample_user, program, badge="Silver", qualifying=3488, guarantee_days=-1)

    LoyaltyService()._check_tier_upgrade(account)

    parked = [e for e in _db.session.info.get(PENDING_KEY, []) if e["kind"] == KIND_TIER_DOWNGRADE]
    assert len(parked) == 1
    assert parked[0]["user_id"] == sample_user.id
    assert parked[0]["tier"] == "Bronze"
    assert parked[0]["qualifying_points"] == 3488
    assert parked[0]["required_points"] == 4000
    assert account.current_tier == "Bronze"


def test_blocked_downgrade_parks_nothing(db, sample_user, program, ladder):
    from business_app import db as _db
    from business_app.utils.loyalty_award_dispatch import PENDING_KEY

    account = _account(db, sample_user, program, badge="Silver", qualifying=3488, guarantee_days=365)

    LoyaltyService()._check_tier_upgrade(account)

    assert _db.session.info.get(PENDING_KEY, []) == []


def test_requalifying_at_the_same_tier_parks_nothing(db, sample_user, program, ladder):
    from business_app import db as _db
    from business_app.utils.loyalty_award_dispatch import PENDING_KEY

    account = _account(db, sample_user, program, badge="Silver", qualifying=5000, guarantee_days=10)

    LoyaltyService()._check_tier_upgrade(account)

    assert _db.session.info.get(PENDING_KEY, []) == []


def test_downgrade_event_is_mapped_to_its_own_notification_type(db):
    from business_app.services.notification_service import LOYALTY_EVENT_NOTIFICATION_TYPES
    from business_app.utils.constants import NotificationType

    assert LOYALTY_EVENT_NOTIFICATION_TYPES["tier_downgrade"] is NotificationType.LOYALTY_TIER_DOWNGRADE
```

- [ ] **Step 2: Run it to verify it fails**

```bash
docker exec bs-test-app python -m pytest tests/unit/test_tier_downgrade_notification.py -n0 --no-cov -q
```

Expected: `ImportError: cannot import name 'KIND_TIER_DOWNGRADE'`.

- [ ] **Step 3: Add the notification type**

In `business_app/utils/constants.py`, after line 62 (`LOYALTY_TIER_UPGRADE = "loyalty_tier_upgrade"`):

```python
    LOYALTY_TIER_DOWNGRADE = "loyalty_tier_downgrade"
```

- [ ] **Step 4: Map the event and mark it single-channel**

In `business_app/services/notification_service.py`, extend the two collections:

```python
LOYALTY_EVENT_NOTIFICATION_TYPES = {
    "earned": NotificationType.LOYALTY_REWARD,
    "redeemed": NotificationType.REWARD_REDEEMED,
    "tier_upgrade": NotificationType.LOYALTY_TIER_UPGRADE,
    "tier_downgrade": NotificationType.LOYALTY_TIER_DOWNGRADE,
    "points_expired": NotificationType.LOYALTY_POINTS_EXPIRED,
}

# Customer-facing AquaCoins messages delivered on exactly ONE channel
# (Telegram when the bot is connected, else email) — never both, never SMS.
_LOYALTY_SINGLE_CHANNEL_TYPES = frozenset(
    {
        NotificationType.LOYALTY_REWARD,
        NotificationType.LOYALTY_TIER_UPGRADE,
        NotificationType.LOYALTY_TIER_DOWNGRADE,
        NotificationType.LOYALTY_POINTS_EXPIRED,
    }
)
```

In the same file, extend the tier-label injection at line 848 so the downgrade copy can name the tier too:

```python
        if (
            notif_type in (NotificationType.LOYALTY_TIER_UPGRADE, NotificationType.LOYALTY_TIER_DOWNGRADE)
            and "tier_label" not in template_data
        ):
            template_data["tier_label"] = self._loyalty_tier_label(
                template_data.get("tier_config_id"), template_data.get("tier") or "", language
            )
```

- [ ] **Step 5: Add the templates**

In `business_app/services/notification_service.py`, in `DEFAULT_TEMPLATES`, immediately after the `("loyalty_tier_upgrade", "email")` entry closes:

```python
    # Loyalty tier downgrade - Telegram. States the reason and both numbers so
    # a lost cash discount is never a silent surprise.
    ("loyalty_tier_downgrade", "telegram"): {
        "name": "loyalty_tier_downgrade_telegram",
        "translations": {
            "uz": {
                "content": """ℹ️ <b>Darajangiz o'zgardi</b>

Yangi daraja: {tier_label}
So'nggi 12 oy: {qualifying_points} AquaCoins ({required_points} talab qilinadi)""",
            },
            "ru": {
                "content": """ℹ️ <b>Ваш уровень изменился</b>

Новый уровень: {tier_label}
За последние 12 месяцев: {qualifying_points} AquaCoins (нужно {required_points})""",
            },
            "en": {
                "content": """ℹ️ <b>Your level has changed</b>

New level: {tier_label}
Last 12 months: {qualifying_points} AquaCoins ({required_points} required)""",
            },
        },
    },
    # Loyalty tier downgrade - Email
    ("loyalty_tier_downgrade", "email"): {
        "name": "loyalty_tier_downgrade_email",
        "translations": {
            "uz": {
                "subject": "Darajangiz o'zgardi: {tier_label} - {{company_name}}",
                "content": """<h2>Darajangiz o'zgardi</h2>
<p>Yangi daraja: <strong>{tier_label}</strong></p>
<p>So'nggi 12 oy: {qualifying_points} AquaCoins ({required_points} talab qilinadi)</p>""",
            },
            "ru": {
                "subject": "Ваш уровень изменился: {tier_label} - {{company_name}}",
                "content": """<h2>Ваш уровень изменился</h2>
<p>Новый уровень: <strong>{tier_label}</strong></p>
<p>За последние 12 месяцев: {qualifying_points} AquaCoins (нужно {required_points})</p>""",
            },
            "en": {
                "subject": "Your level has changed: {tier_label} - {{company_name}}",
                "content": """<h2>Your level has changed</h2>
<p>New level: <strong>{tier_label}</strong></p>
<p>Last 12 months: {qualifying_points} AquaCoins ({required_points} required)</p>""",
            },
        },
    },
```

- [ ] **Step 6: Add the dispatch kind and its drain branch**

In `business_app/utils/loyalty_award_dispatch.py`, after `KIND_TIER_UPGRADE = "tier_upgrade"`:

```python
KIND_TIER_DOWNGRADE = "tier_downgrade"
```

In `_drain_on_commit`, replace the dispatch body:

```python
                if entry.get("kind") == KIND_TIER_UPGRADE:
                    service._send_tier_upgrade_notification(
                        entry["user_id"],
                        tier=entry.get("tier"),
                        tier_config_id=entry.get("tier_config_id"),
                        balance=entry.get("balance"),
                    )
                else:
```

with:

```python
                if entry.get("kind") == KIND_TIER_UPGRADE:
                    service._send_tier_upgrade_notification(
                        entry["user_id"],
                        tier=entry.get("tier"),
                        tier_config_id=entry.get("tier_config_id"),
                        balance=entry.get("balance"),
                    )
                elif entry.get("kind") == KIND_TIER_DOWNGRADE:
                    service._send_tier_downgrade_notification(
                        entry["user_id"],
                        tier=entry.get("tier"),
                        tier_config_id=entry.get("tier_config_id"),
                        qualifying_points=entry.get("qualifying_points"),
                        required_points=entry.get("required_points"),
                    )
                else:
```

The `sorted()` key is `e.get("kind") != KIND_AWARD`, so awards still drain first and the new kind sorts alongside upgrades. Only one tier event can be parked per account per transaction, so their relative order is not observable.

- [ ] **Step 7: Add the sender**

In `business_app/services/loyalty_service.py`, immediately after `_send_tier_upgrade_notification` ends (line 2324):

```python
    def _send_tier_downgrade_notification(
        self,
        user_id: int,
        *,
        tier: str,
        tier_config_id: int = None,
        qualifying_points: int = 0,
        required_points: int = 0,
    ):
        """Tell a member their tier changed, and why.

        A downgrade removes a cash discount, so it states the trailing-365-day
        figure alongside the threshold it fell short of.
        """
        from ..tasks.notification_tasks import send_loyalty_notification_task

        send_loyalty_notification_task.delay(
            user_id,
            "tier_downgrade",
            {
                "tier": tier,
                "tier_config_id": tier_config_id,
                "qualifying_points": qualifying_points,
                "required_points": required_points,
            },
        )
```

- [ ] **Step 8: Park the event on a real downgrade**

In `_check_tier_upgrade` CASE 2, inside the branch that performs the downgrade, after `self._update_points_to_next_tier(account, target_tier_config)`:

```python
                from business_app.utils.loyalty_award_dispatch import KIND_TIER_DOWNGRADE, PENDING_KEY

                db.session.info.setdefault(PENDING_KEY, []).append(
                    {
                        "kind": KIND_TIER_DOWNGRADE,
                        "user_id": account.user_id,
                        "tier": target_tier_name,
                        "tier_config_id": target_tier_config.id,
                        "qualifying_points": qualifying_points,
                        "required_points": current_tier_config.min_points if current_tier_config else 0,
                    }
                )
```

`current_tier_config` is resolved at the top of the method, before the badge is overwritten, so `required_points` is the floor of the tier just lost.

- [ ] **Step 9: Run the tests to verify they pass**

```bash
docker exec bs-test-app python -m pytest tests/unit/test_tier_downgrade_notification.py -n0 --no-cov -q
```

Expected: `6 passed`.

- [ ] **Step 10: Run the notification regressions**

```bash
docker exec bs-test-app python -m pytest \
  tests/unit/test_loyalty_tier_upgrade_notification.py \
  tests/unit/test_loyalty_award_notification_dispatch.py \
  tests/unit/test_notification_loyalty_reward_telegram_template.py \
  tests/integration/test_loyalty_api_integration.py -n0 --no-cov -q
```

Expected: all pass. `test_loyalty_award_notification_dispatch.py` is the one that guards the drain ordering you just extended — if it fails, the new kind broke the `sorted()` key, not the templates.

- [ ] **Step 11: Commit**

```bash
git add business_app/utils/constants.py business_app/services/notification_service.py business_app/utils/loyalty_award_dispatch.py business_app/services/loyalty_service.py tests/unit/test_tier_downgrade_notification.py
git commit -m "feat(loyalty): notify a tier downgrade with its reason

A downgrade removes a cash discount and previously said nothing. It now
dispatches through the same post-commit path as the upgrade, on its own
notification type and templates, naming the trailing-365-day figure and the
threshold it fell short of. A downgrade blocked by the guarantee, and a
re-qualification at the same tier, both stay silent.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Bot loyalty screen shows the deciding number

**Files:**
- Modify: `business_app/services/loyalty_service.py:380-390` (`get_points_summary_for_user`), `telegram_bot/handlers/loyalty.py:240-256`, `scripts/seed_backend_translations.py`
- Test: `tests/integration/test_loyalty_api_integration.py` (append)

**Interfaces:**
- Consumes: `effective_tier(account)` from Task 1.
- Produces: `GET /api/v1/loyalty/points` payload keys `qualifying_points`, `tier`, `tier_discount_percentage`, `next_tier`, `points_to_next_tier`, `points_needed_to_keep`.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_loyalty_api_integration.py`:

```python
def test_points_summary_reports_the_qualifying_figure(db, sample_user):
    """The bot screen must show what decides the tier, not the lifetime total."""
    from decimal import Decimal

    from business_app.models.loyalty import LoyaltyPoints
    from business_app.services.loyalty_service import LoyaltyService
    from tests.integration.tier_discount_factory import seed_account, seed_program, seed_tier

    program = seed_program(db)
    seed_tier(db, program, name="Bronze", rate=Decimal("0"), min_points=0, display_order=0)
    seed_tier(db, program, name="Silver", rate=Decimal("1.5"), min_points=4000, display_order=1)
    seed_tier(db, program, name="Gold", rate=Decimal("2"), min_points=15000, display_order=2)
    seed_account(db, sample_user, program, qualifying_points=3488, balance=988)
    account = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
    account.current_tier = "Silver"
    db.session.commit()

    summary = LoyaltyService().get_points_summary_for_user(sample_user.id)

    assert summary["qualifying_points"] == 3488
    assert summary["lifetime_earned"] == 3488
    assert summary["tier"] == "Silver"
    assert summary["tier_discount_percentage"] == 1.5
    assert summary["next_tier"] == "Gold"
    assert summary["points_to_next_tier"] == 15000 - 3488
    assert summary["points_needed_to_keep"] == 4000 - 3488
```

- [ ] **Step 2: Run it to verify it fails**

```bash
docker exec bs-test-app python -m pytest tests/integration/test_loyalty_api_integration.py::test_points_summary_reports_the_qualifying_figure -n0 --no-cov -q
```

Expected: FAIL with `KeyError: 'qualifying_points'`.

- [ ] **Step 3: Extend the summary payload**

In `business_app/services/loyalty_service.py`, replace `get_points_summary_for_user`:

```python
    def get_points_summary_for_user(self, user_id: int) -> Dict[str, Any]:
        """Get points summary payload for API."""
        account = self.get_or_create_loyalty_account(user_id)
        return {
            "points_balance": account.current_balance or 0,
            "lifetime_points": account.total_earned or 0,
            "current_balance": account.current_balance or 0,
            "lifetime_earned": account.total_earned or 0,
            "tier": account.current_tier,
            "next_tier_threshold": account.points_to_next_tier or 0,
        }
```

with:

```python
    def get_points_summary_for_user(self, user_id: int) -> Dict[str, Any]:
        """Get points summary payload for API.

        ``qualifying_points`` is the trailing-365-day figure that actually
        decides the tier; ``lifetime_earned`` is retained for the surfaces that
        still publish it. ``points_needed_to_keep`` is what a member must earn
        to clear their own tier's current floor.
        """
        account = self.get_or_create_loyalty_account(user_id)
        qualifying_points = self.calculate_qualifying_points(user_id)
        tier = effective_tier(account)
        next_tier = self._get_next_tier_info(account)
        requalification = self.get_requalification_info(user_id)
        return {
            "points_balance": account.current_balance or 0,
            "lifetime_points": account.total_earned or 0,
            "current_balance": account.current_balance or 0,
            "lifetime_earned": account.total_earned or 0,
            "qualifying_points": qualifying_points,
            "tier": tier.name if tier else account.current_tier,
            "tier_discount_percentage": float(tier.discount_percentage or 0) if tier else 0.0,
            "next_tier": next_tier["tier"] if next_tier else None,
            "points_to_next_tier": next_tier["points_needed"] if next_tier else 0,
            "points_needed_to_keep": requalification["points_needed_to_keep"],
            "next_tier_threshold": account.points_to_next_tier or 0,
        }
```

- [ ] **Step 4: Run it to verify it passes**

```bash
docker exec bs-test-app python -m pytest tests/integration/test_loyalty_api_integration.py -n0 --no-cov -q
```

Expected: all pass. `_get_next_tier_info` (loyalty_service.py:2218-2243) returns `{"tier": str, "points_needed": int, "threshold": int}` and resolves "current" from the badge, so for a Silver badge on 3,488 points it yields Gold and `15000 − 3488`.

- [ ] **Step 5: Seed the bot copy**

In `scripts/seed_backend_translations.py`, add to the `BACKEND_TRANSLATIONS` dict alongside the other `telegram.loyalty.*` entries:

```python
    'telegram.loyalty.qualifying_12m': {
        'category': 'telegram',
        'en': 'Last 12 months',
        'ru': 'За последние 12 месяцев',
        'uz': "So'nggi 12 oy",
    },
    'telegram.loyalty.tier_line': {
        'category': 'telegram',
        'en': 'Level: {tier}',
        'ru': 'Уровень: {tier}',
        'uz': 'Daraja: {tier}',
    },
    'telegram.loyalty.tier_cod_perk': {
        'category': 'telegram',
        'en': '{pct}% off when you pay cash on delivery',
        'ru': 'Скидка {pct}% при оплате наличными при доставке',
        'uz': "Yetkazib berishda naqd to'lasangiz {pct}% chegirma",
    },
    'telegram.loyalty.tier_secured': {
        'category': 'telegram',
        'en': 'Status secured',
        'ru': 'Статус закреплён',
        'uz': 'Holat mustahkamlangan',
    },
    'telegram.loyalty.tier_keep_hint': {
        'category': 'telegram',
        'en': '{points} more AquaCoins to keep this level',
        'ru': 'Ещё {points} AquaCoins, чтобы сохранить уровень',
        'uz': "Darajani saqlash uchun yana {points} AquaCoins",
    },
    'telegram.loyalty.to_next_tier': {
        'category': 'telegram',
        'en': 'To {tier}: {points} more',
        'ru': 'До {tier}: ещё {points}',
        'uz': '{tier} gacha: yana {points}',
    },
```

Match the surrounding entries' exact dict shape — open the file and copy the shape of a neighbouring `telegram.loyalty.*` key rather than assuming the keys above are named the same as that file's schema.

- [ ] **Step 6: Render the new screen**

In `telegram_bot/handlers/loyalty.py`, replace lines 240-256 (from `if points_response.success:` through the `lifetime_earned` append):

```python
                if points_response.success:
                    points_data = self._unwrap_response_data(points_response)
                    current_points = points_data.get('current_balance', points_data.get('points_balance', 0))
                    lifetime_points = points_data.get('lifetime_earned', points_data.get('lifetime_points', 0))
                else:
                    current_points = lifetime_points = 0
```

with:

```python
                if points_response.success:
                    points_data = self._unwrap_response_data(points_response)
                    current_points = points_data.get('current_balance', points_data.get('points_balance', 0))
                    qualifying_points = points_data.get('qualifying_points', 0)
                else:
                    points_data = {}
                    current_points = qualifying_points = 0
```

and replace the two message lines:

```python
            loyalty_text += f"🏆 {i18n.get('telegram.loyalty.current_balance', language)}: {current_points} {points_unit}\n"
            loyalty_text += f"📈 {i18n.get('telegram.loyalty.lifetime_earned', language)}: {lifetime_points} {points_unit}\n\n"
```

with:

```python
            # Level first: it is what the customer's benefits follow. No date —
            # a tier does not expire, and any date here reads as one.
            tier_name = points_data.get('tier')
            if tier_name:
                loyalty_text += f"🥈 {i18n.get('telegram.loyalty.tier_line', language, tier=tier_name)}\n"
                tier_pct = float(points_data.get('tier_discount_percentage') or 0)
                if tier_pct > 0:
                    loyalty_text += f"   {i18n.get('telegram.loyalty.tier_cod_perk', language, pct=('%g' % tier_pct))}\n"
                needed_to_keep = int(points_data.get('points_needed_to_keep') or 0)
                if needed_to_keep > 0:
                    loyalty_text += f"   ⚠️ {i18n.get('telegram.loyalty.tier_keep_hint', language, points=needed_to_keep)}\n"
                else:
                    loyalty_text += f"   ✅ {i18n.get('telegram.loyalty.tier_secured', language)}\n"
                loyalty_text += "\n"

            loyalty_text += f"🏆 {i18n.get('telegram.loyalty.current_balance', language)}: {current_points} {points_unit}\n"
            loyalty_text += f"📈 {i18n.get('telegram.loyalty.qualifying_12m', language)}: {qualifying_points} {points_unit}\n"
            next_tier = points_data.get('next_tier')
            if next_tier:
                to_next = int(points_data.get('points_to_next_tier') or 0)
                loyalty_text += f"   {i18n.get('telegram.loyalty.to_next_tier', language, tier=next_tier, points=to_next)}\n"
            loyalty_text += "\n"
```

- [ ] **Step 7: Verify the bot module still imports**

```bash
docker exec bs-test-app python -m pytest tests/telegram_bot/ -k loyalty -n0 --no-cov -q
```

Expected: pass, or "no tests ran". If no loyalty bot tests exist, verify by import instead:

```bash
docker exec bs-test-app python -c "import ast,sys; ast.parse(open('/app/telegram_bot/handlers/loyalty.py').read()); print('parsed ok')"
```

- [ ] **Step 8: Commit**

```bash
git add business_app/services/loyalty_service.py telegram_bot/handlers/loyalty.py scripts/seed_backend_translations.py tests/integration/test_loyalty_api_integration.py
git commit -m "feat(loyalty): show the level and the 12-month figure that decides it

The bot screen published lifetime total_earned under \"Total earned\" while the
tier was decided by an entirely different, unshown number, and never named the
level at all. It now leads with the level, its cash-discount perk and what is
needed to keep it, then the balance and the trailing-12-month figure. No date
is shown: a tier does not expire and a date would read as one.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Order screens print the discount line

**Files:**
- Modify: `telegram_bot/utils.py:801-815` (`MessageBuilder.build_order_summary`)
- Test: `tests/telegram_bot/test_order_summary_discount_line.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: nothing new.

`build_order_summary` has three call sites — `telegram_bot/handlers/orders.py:510` (order details), `:1420` and `:1515` (order placed). All three gain the block from this one change.

- [ ] **Step 1: Write the failing test**

Create `tests/telegram_bot/test_order_summary_discount_line.py`:

```python
"""A total below the sum of the item lines must always say why."""

from telegram_bot.utils import MessageBuilder


def _order(**overrides):
    order = {
        "order_number": "TG_000401_26",
        "created_at": "2026-09-03T09:00:00+00:00",
        "subtotal": 54000,
        "discount_amount": 0,
        "loyalty_discount": 0,
        "tier_discount": 0,
        "delivery_fee": 0,
        "total_amount": 54000,
        "status": "pending",
    }
    order.update(overrides)
    return order


def test_tier_discount_is_stated(app):
    summary = MessageBuilder.build_order_summary(_order(tier_discount=810, total_amount=53190), "en")

    assert "810" in summary


def test_no_breakdown_when_nothing_is_discounted(app):
    summary = MessageBuilder.build_order_summary(_order(), "en")

    assert "810" not in summary
    assert summary.count("54,000") >= 1
```

- [ ] **Step 2: Run it to verify it fails**

```bash
docker exec bs-test-app python -m pytest tests/telegram_bot/test_order_summary_discount_line.py -n0 --no-cov -q
```

Expected: `test_tier_discount_is_stated` FAILS — `810` appears nowhere.

- [ ] **Step 3: Add the breakdown**

In `telegram_bot/utils.py`, replace the body of `build_order_summary`:

```python
    @staticmethod
    def build_order_summary(order: Dict[str, Any], language: str = 'en') -> str:
        """Build order summary message"""
        lines = [
            f"📋 {i18n.get('telegram.order.number', language, order.get('order_number', 'N/A'))}",
            f"📅 Date: {order.get('created_at', 'N/A')[:10]}",
            f"💰 {i18n.get('telegram.order.total', language, format_price(order.get('total_amount', 0)))}"
        ]

        if order.get('status'):
            from shared.constants import ORDER_STATUS_ICONS, DEFAULT_STATUS_ICON
            icon = ORDER_STATUS_ICONS.get(order['status'], DEFAULT_STATUS_ICON)
            lines.append(f"📊 Status: {icon} {order['status'].replace('_', ' ').title()}")

        return '\n'.join(lines)
```

with:

```python
    @staticmethod
    def build_order_summary(order: Dict[str, Any], language: str = 'en') -> str:
        """Build order summary message.

        Every discount that moved the total is stated. Without it the total
        sits below the sum of the item lines this screen prints beneath it,
        with nothing accounting for the difference.
        """
        lines = [
            f"📋 {i18n.get('telegram.order.number', language, order.get('order_number', 'N/A'))}",
            f"📅 Date: {order.get('created_at', 'N/A')[:10]}",
        ]

        discount_amount = float(order.get('discount_amount') or 0)
        if discount_amount > 0:
            lines.append(i18n.get(
                'telegram.orders.estimate_discount_line', language,
                amount=format_price(discount_amount),
            ))
        loyalty_discount = float(order.get('loyalty_discount') or 0)
        if loyalty_discount > 0:
            lines.append(i18n.get(
                'telegram.orders.estimate_reward_line', language,
                amount=format_price(loyalty_discount),
            ))
        tier_discount = float(order.get('tier_discount') or 0)
        if tier_discount > 0:
            lines.append(i18n.get(
                'telegram.orders.estimate_tier_line', language,
                tier_name=order.get('tier_name') or '',
                percentage='%g' % float(order.get('tier_discount_percentage') or 0),
                amount=format_price(tier_discount),
            ))
        delivery_fee = float(order.get('delivery_fee') or 0)
        if delivery_fee > 0:
            lines.append(i18n.get(
                'telegram.orders.delivery_fee', language,
                amount=format_price(delivery_fee),
            ))

        lines.append(f"💰 {i18n.get('telegram.order.total', language, format_price(order.get('total_amount', 0)))}")

        if order.get('status'):
            from shared.constants import ORDER_STATUS_ICONS, DEFAULT_STATUS_ICON
            icon = ORDER_STATUS_ICONS.get(order['status'], DEFAULT_STATUS_ICON)
            lines.append(f"📊 Status: {icon} {order['status'].replace('_', ' ').title()}")

        return '\n'.join(lines)
```

The four copy keys already exist in all three languages — they are the ones `_build_estimate_block` renders at `telegram_bot/handlers/orders.py:267-291`. No seeding is required.

- [ ] **Step 4: Run it to verify it passes**

```bash
docker exec bs-test-app python -m pytest tests/telegram_bot/test_order_summary_discount_line.py -n0 --no-cov -q
```

Expected: `2 passed`.

- [ ] **Step 5: Confirm the serializer publishes the fields the block reads**

```bash
docker exec bs-test-app python -c "
import re
src = open('/app/business_app/serializers/order_serializers.py').read()
for key in ('tier_discount', 'loyalty_discount', 'discount_amount', 'delivery_fee'):
    print(key, key in src)
"
```

Expected: all four print `True`. `tier_name` and `tier_discount_percentage` are not serialized on the order; the block renders an empty tier name and `0` percentage in that case, which the copy tolerates. If you want the tier named on these screens, that is a separate task — do not widen this one.

- [ ] **Step 6: Commit**

```bash
git add telegram_bot/utils.py tests/telegram_bot/test_order_summary_discount_line.py
git commit -m "fix(bot): state the discounts that moved an order total

The order-placed and order-details screens printed a total directly above the
item lines with no breakdown, so a discounted order showed a total lower than
its own items summed, unexplained. Reuses the checkout quote's existing copy
keys, so no new translations are needed.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Admin tier editor guardrails

**Files:**
- Modify: `business_app/api/admin.py:7482-7562` (`create_loyalty_tier_config`), `business_app/api/admin.py:7564-7625` (`update_loyalty_tier_config`)
- Test: `tests/integration/test_admin_tier_config_guardrails.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_validate_tier_ladder(program_id, tier_id, proposed) -> Optional[tuple[str, str]]` and `_count_stranded_members(tier, new_min_points) -> int`, both module-level private helpers in `business_app/api/admin.py`. Task 8 relies on the 409 response body shape defined here.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_admin_tier_config_guardrails.py`:

```python
"""The tier editor cannot silently reprice members.

A threshold edit changes what every COD order costs. It must refuse a ladder
with a hole in it, and it must make stranding existing members deliberate.
"""

from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.audit import AuditLog
from business_app.models.loyalty import LoyaltyPoints, LoyaltyTierConfig
from business_app.models.user import User
from shared.enums import UserRole, UserType
from tests.integration.tier_discount_factory import seed_account, seed_program, seed_tier


@pytest.fixture
def admin_headers(app, db):
    """validate_admin_action reads the DB row, so the user must be STAFF+ADMIN;
    a role claim on the JWT alone is not enough."""
    admin = User(
        email="tier-admin@example.com",
        first_name="Tier",
        last_name="Admin",
        role=UserRole.ADMIN,
        user_type=UserType.STAFF,
        is_verified=True,
    )
    db.session.add(admin)
    db.session.commit()
    with app.app_context():
        token = create_access_token(
            identity=str(admin.id),
            additional_claims={"role": admin.role.value},
        )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture
def ladder(db):
    program = seed_program(db)
    bronze = seed_tier(db, program, name="Bronze", rate=Decimal("0"), min_points=0, display_order=0)
    bronze.max_points = 3000
    silver = seed_tier(db, program, name="Silver", rate=Decimal("1.5"), min_points=3000, display_order=1)
    silver.max_points = None
    db.session.commit()
    return {"program": program, "Bronze": bronze, "Silver": silver}


def test_gapped_ladder_is_rejected(app, db, admin_headers, ladder):
    """Bronze ends at 3000; Silver may not start at 4000."""
    response = app.test_client().put(
        f"/api/v1/admin/loyalty/tiers/{ladder['Silver'].id}",
        json={"min_points": 4000},
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert response.get_json()["data"]["error_code"] == "threshold_gap"
    db.session.refresh(ladder["Silver"])
    assert ladder["Silver"].min_points == 3000


def test_stranding_edit_needs_confirmation(app, db, admin_headers, ladder):
    """Raising the floor above a member's points must be deliberate."""
    member = User(
        email="m@example.com", first_name="M", last_name="M",
        role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL,
    )
    db.session.add(member)
    db.session.commit()
    seed_account(db, member, ladder["program"], qualifying_points=3488)
    account = LoyaltyPoints.query.filter_by(user_id=member.id).first()
    account.current_tier = "Silver"
    ladder["Bronze"].max_points = 4000
    db.session.commit()

    first = app.test_client().put(
        f"/api/v1/admin/loyalty/tiers/{ladder['Silver'].id}",
        json={"min_points": 4000},
        headers=admin_headers,
    )

    assert first.status_code == 409
    assert first.get_json()["data"]["error_code"] == "impact_confirmation_required"
    assert first.get_json()["data"]["stranded_members"] == 1

    second = app.test_client().put(
        f"/api/v1/admin/loyalty/tiers/{ladder['Silver'].id}",
        json={"min_points": 4000, "confirm_impact": True},
        headers=admin_headers,
    )

    assert second.status_code == 200
    db.session.refresh(ladder["Silver"])
    assert ladder["Silver"].min_points == 4000


def test_accepted_edit_writes_an_audit_row(app, db, admin_headers, ladder):
    response = app.test_client().put(
        f"/api/v1/admin/loyalty/tiers/{ladder['Silver'].id}",
        json={"discount_percentage": 2.5},
        headers=admin_headers,
    )

    assert response.status_code == 200
    row = AuditLog.query.filter_by(resource_type="loyalty_tier_config").order_by(AuditLog.id.desc()).first()
    assert row is not None
    assert row.new_values["discount_percentage"] == 2.5
    assert row.old_values["discount_percentage"] == 1.5
```

- [ ] **Step 2: Run it to verify it fails**

```bash
docker exec bs-test-app python -m pytest tests/integration/test_admin_tier_config_guardrails.py -n0 --no-cov -q
```

Expected: all three FAIL — the endpoint returns 200 and writes no audit row. A 403 instead means the admin fixture is wrong, not the endpoint: `validate_admin_action` reads the DB row, so the user needs `user_type=UserType.STAFF` as well as `role=UserRole.ADMIN`. `tests/integration/test_admin_place_api_full_e2e.py:212-225` is the working reference.

- [ ] **Step 3: Add the helpers**

In `business_app/api/admin.py`, above `create_loyalty_tier_config`:

```python
def _validate_tier_ladder(program_id, tier_id, proposed):
    """Reject a tier ladder with an overlap or a hole in it.

    ``proposed`` is the {field: value} patch about to be applied to ``tier_id``
    (or None for a new tier). Returns (error_code, detail) or None.

    get_tier_for_points selects on min_points alone, so a hole does not change
    pricing — but it is published in the customer-facing tier table and in the
    admin UI as a band, where it reads as a range no one can occupy.
    """
    from business_app.models.loyalty import LoyaltyTierConfig

    rows = LoyaltyTierConfig.query.filter_by(program_id=program_id, is_active=True).all()
    ladder = []
    for row in rows:
        if tier_id is not None and row.id == tier_id:
            continue
        ladder.append({"name": row.name, "order": row.display_order, "min": row.min_points, "max": row.max_points})
    ladder.append(
        {
            "name": proposed.get("name", "(edited)"),
            "order": proposed.get("display_order", 0),
            "min": proposed.get("min_points", 0),
            "max": proposed.get("max_points"),
        }
    )
    ladder.sort(key=lambda entry: entry["order"])

    for lower, upper in zip(ladder, ladder[1:]):
        if upper["min"] <= lower["min"]:
            return (
                "threshold_overlap",
                f"{upper['name']} starts at {upper['min']}, not above {lower['name']}'s {lower['min']}.",
            )
        if lower["max"] is not None and lower["max"] != upper["min"]:
            return (
                "threshold_gap",
                f"{lower['name']} ends at {lower['max']} and {upper['name']} starts at {upper['min']} — "
                f"points between map to no tier.",
            )
    if ladder[-1]["max"] is not None:
        return ("threshold_gap", f"{ladder[-1]['name']} is the top tier and must have no upper bound.")
    return None


def _count_stranded_members(tier, new_min_points):
    """Members holding this tier's badge whose points fall below a raised floor.

    Their badge keeps their benefits (effective_tier), so this is not a
    breakage — it is the number of people the edit quietly moves out of
    live qualification, which an admin should see before committing.
    """
    from business_app.models.loyalty import LoyaltyPoints
    from business_app.services.loyalty_service import LoyaltyService

    if new_min_points is None or new_min_points <= (tier.min_points or 0):
        return 0
    service = LoyaltyService()
    stranded = 0
    for account in LoyaltyPoints.query.filter_by(current_tier=tier.name, program_id=tier.program_id).all():
        if service.calculate_qualifying_points(account.user_id) < new_min_points:
            stranded += 1
    return stranded
```

- [ ] **Step 4: Wire them into the update endpoint**

In `update_loyalty_tier_config`, immediately after `data = request.get_json()`:

```python
        ladder_error = _validate_tier_ladder(
            tier.program_id,
            tier.id,
            {
                "name": data.get("name", tier.name),
                "display_order": data.get("display_order", tier.display_order),
                "min_points": data.get("min_points", tier.min_points),
                "max_points": data.get("max_points", tier.max_points),
            },
        )
        if ladder_error:
            code, detail = ladder_error
            # NOT validation_error_response: it hard-codes 400 and ignores any
            # status_code passed to it (api_responses.py:265-270).
            return error_response(message=detail, status_code=422, data={"error_code": code})

        if not data.get("confirm_impact"):
            stranded = _count_stranded_members(tier, data.get("min_points"))
            if stranded:
                return error_response(
                    message=get_translation("api.loyalty.tier_impact_confirmation"),
                    status_code=409,
                    data={
                        "error_code": "impact_confirmation_required",
                        "stranded_members": stranded,
                        "tier": tier.name,
                        "new_min_points": data.get("min_points"),
                    },
                )

        _audited_fields = (
            "name",
            "display_order",
            "min_points",
            "max_points",
            "points_multiplier",
            "discount_percentage",
            "is_active",
        )
        old_values = {field: getattr(tier, field) for field in _audited_fields}
```

and immediately after the first `db.session.commit()` in that function:

```python
        from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity

        new_values = {field: getattr(tier, field) for field in _audited_fields}
        changed = {field: new_values[field] for field in _audited_fields if new_values[field] != old_values[field]}
        if changed:
            audit_logger.log_event(
                event_type=AuditEventType.SETTINGS_CHANGED,
                action="loyalty_tier_updated",
                severity=AuditSeverity.HIGH,
                resource_type="loyalty_tier_config",
                resource_id=str(tier.id),
                description=f"Loyalty tier {tier.name} updated",
                old_values={field: old_values[field] for field in changed},
                new_values=changed,
                success=True,
            )
```

`error_response` and `get_translation` are already imported at the top of `business_app/api/admin.py`; confirm with `grep -n "^from business_app.utils.api_responses import" -A 12 business_app/api/admin.py` before running the tests, and add any missing name to that existing import rather than adding a new statement.

- [ ] **Step 5: Wire the ladder check into the create endpoint**

In `create_loyalty_tier_config`, before the tier row is constructed, add the same `_validate_tier_ladder` call with `tier_id=None` and the incoming `data` as `proposed`. No impact check is needed: a brand-new tier strands no one.

- [ ] **Step 6: Add the confirmation message translation**

In `scripts/seed_backend_translations.py`, add alongside the other `api.loyalty.*` entries:

```python
    'api.loyalty.tier_impact_confirmation': {
        'category': 'api',
        'en': 'This change moves existing members out of live qualification. Confirm to proceed.',
        'ru': 'Это изменение выводит существующих участников из активной квалификации. Подтвердите, чтобы продолжить.',
        'uz': "Bu o'zgarish mavjud a'zolarni faol malakadan chiqaradi. Davom etish uchun tasdiqlang.",
    },
```

- [ ] **Step 7: Run the tests to verify they pass**

```bash
docker exec bs-test-app python -m pytest tests/integration/test_admin_tier_config_guardrails.py -n0 --no-cov -q
```

Expected: `3 passed`.

- [ ] **Step 8: Run the admin loyalty regressions**

```bash
docker exec bs-test-app python -m pytest tests/ -k "loyalty and admin" -n0 --no-cov -q
```

Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add business_app/api/admin.py scripts/seed_backend_translations.py tests/integration/test_admin_tier_config_guardrails.py
git commit -m "feat(admin): validate, confirm and audit loyalty tier threshold edits

A tier threshold edit changes what every COD order costs, and the endpoint
accepted any value with no validation, no audit row and no indication of who
it affected. It now rejects a gapped or overlapping ladder, requires explicit
confirmation when an edit moves existing members out of live qualification,
and records old and new values in audit_logs.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Admin UI surfaces the impact confirmation

**Files:**
- Modify: `admin_ui/src/pages/LoyaltyPrograms.js:169-177` (`updateTierMutation`)

**Interfaces:**
- Consumes: the 409 body `{ data: { error_code: "impact_confirmation_required", stranded_members, tier, new_min_points } }` from Task 7.
- Produces: nothing.

- [ ] **Step 1: Read the surrounding code**

```bash
sed -n 140,200p admin_ui/src/pages/LoyaltyPrograms.js
```

Note the exact import list at the top of the file — you need `Modal` from `antd`. If it is not imported, add it to the existing `antd` import rather than adding a second import statement.

- [ ] **Step 2: Add the confirmation flow**

Replace the `updateTierMutation` definition:

```javascript
  const updateTierMutation = useMutation({
    mutationFn: ({ tierId, values }) => adminService.updateLoyaltyTier(tierId, values),

    onSuccess: () => {
      message.success(t('ui.loyalty.tier_update_success', { defaultValue: 'Tier updated successfully' }));
      setTierModal({ open: false, tier: null });
      tierForm.resetFields();
      invalidateLoyaltyQueries();
    },
  });
```

with:

```javascript
  const updateTierMutation = useMutation({
    mutationFn: ({ tierId, values }) => adminService.updateLoyaltyTier(tierId, values),

    onSuccess: () => {
      message.success(t('ui.loyalty.tier_update_success', { defaultValue: 'Tier updated successfully' }));
      setTierModal({ open: false, tier: null });
      tierForm.resetFields();
      invalidateLoyaltyQueries();
    },

    // A threshold raise moves members out of live qualification. The server
    // refuses it once with a count; re-submit only after the admin agrees.
    onError: (error, variables) => {
      const payload = error?.response?.data?.data;
      if (payload?.error_code !== 'impact_confirmation_required') {
        message.error(
          error?.response?.data?.message ||
            t('ui.loyalty.tier_update_failed', { defaultValue: 'Tier update failed' }),
        );
        return;
      }
      Modal.confirm({
        title: t('ui.loyalty.tier_impact_title', { defaultValue: 'Confirm threshold change' }),
        content: t('ui.loyalty.tier_impact_body', {
          count: payload.stranded_members,
          tier: payload.tier,
          defaultValue: `${payload.stranded_members} member(s) hold the ${payload.tier} badge with fewer points than the new threshold. They keep their benefits, but will no longer qualify on points alone. Proceed?`,
        }),
        okText: t('ui.loyalty.tier_impact_ok', { defaultValue: 'Proceed' }),
        cancelText: t('ui.common.cancel', { defaultValue: 'Cancel' }),
        onOk: () =>
          updateTierMutation.mutate({
            tierId: variables.tierId,
            values: { ...variables.values, confirm_impact: true },
          }),
      });
    },
  });
```

- [ ] **Step 3: Run the admin UI tests**

The host has no node and the tree has no `node_modules`. Build a scratch runner:

```bash
mkdir -p /tmp/claude-1000/adminui && cd /home/umar/bluestream/admin_ui
docker run --rm -v /home/umar/bluestream/admin_ui:/src:ro -v /tmp/claude-1000/adminui:/work -w /work node:22-alpine \
  sh -c "cp /src/package.json /src/package-lock.json /work/ && npm ci --silent && \
         cp -r /src/src /src/vite.config.js /src/index.html /src/eslint.config.mjs /work/ && \
         npx vitest run src/pages/__tests__/LoyaltyPrograms.test.jsx 2>/dev/null || npx vitest run --silent"
```

Expected: pass. `Users.dob.test.js` failing with a one-day offset is a known container-timezone artefact, not your change.

- [ ] **Step 4: Lint the changed file**

```bash
docker run --rm -v /tmp/claude-1000/adminui:/work -w /work node:22-alpine npx --no-install eslint src/pages/LoyaltyPrograms.js
```

Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add admin_ui/src/pages/LoyaltyPrograms.js
git commit -m "feat(admin-ui): confirm a tier threshold change that strands members

Surfaces the server's 409 as a dialog naming how many members hold the badge
with fewer points than the new threshold, and re-submits with confirm_impact
only after the admin agrees.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Deployment

Not part of any task — run only after every task is committed and the owner approves the release.

0. **Correct `Bronze.max_points` FIRST, through the admin UI.** Production's ladder
   is currently Bronze `0–3000`, Silver `4000–15000`, Gold `15000–50000`,
   Platinum `50000–NULL`. Task 7's validator reads that Bronze/Silver pair as a
   `threshold_gap`, so once this ships **every** tier edit returns 422 until
   Bronze's upper bound is raised to 4000. Gold and Platinum are already
   contiguous and Platinum's `NULL` top is correct, so Bronze is the only row to
   touch. Do it through the admin UI, not SQL: that path busts the
   `/loyalty/tiers` response cache. Changing `max_points` alters no pricing —
   `get_tier_for_points` selects on `min_points` alone.

1. Seed translations (`scripts/` is not mounted into the container, so pipe over stdin):
   ```bash
   docker compose exec -T business_app python - < scripts/seed_backend_translations.py
   ```
2. Seed the new notification templates:
   ```bash
   docker compose exec -T business_app python -c "
   from business_app import create_app
   from business_app.services.notification_service import seed_notification_templates
   app = create_app()
   with app.app_context():
       seed_notification_templates()
   "
   ```
   `seed_notification_templates` is defined at `business_app/services/notification_service.py:4679` and upserts every entry in `DEFAULT_TEMPLATES`.
3. Flush the translation cache and the `/loyalty/tiers` response cache — the exact snippet is in `docs/loyalty_tier_cod_discount_deploy_runbook.md` step 3. Never `FLUSHDB`.
4. Restart the three services that read source at process start:
   ```bash
   docker compose restart business_app telegram_bot celery_worker
   ```
5. Rebuild the admin UI (a compiled bundle; a restart does not pick up Task 8):
   ```bash
   docker compose build admin_ui && docker compose up -d admin_ui
   ```
6. Verify against production data, read-only:
   ```bash
   docker compose exec postgres psql -U postgres -d bluestream_db -c \
     "SELECT user_id, current_tier FROM loyalty_points WHERE user_id IN (1,8,10,25,40,54,56,58,68,115,236,281);"
   ```
   All 12 must still read `Silver`. Then place or quote one cash order for one of them and confirm `orders.tier_discount > 0` — no order in production has ever carried a non-zero value, so the first non-zero row is the proof this shipped.

**Rollback:** set every tier's `discount_percentage` to 0 through the admin UI. Gate 4 of `quote_tier_discount` refuses a zero rate, so it takes effect on the next order with no deploy.

## Out of Scope

Confirmed defects deliberately excluded — do not fix them in this plan:

- `total_earned` counts `ADJUSTMENT` points the tier basis excludes, so the lifetime figure overstates.
- Entity user 55 holds 5,380 qualifying points and a Silver badge while contractually excluded from loyalty.
- The payment picker quotes cash without `reward_id` while the confirm screen quotes with it, so the button can advertise a rate the confirm screen's clamp then reduces.
- Operator phone orders publish `tier_discount: 0.00` unconditionally.
- Grandfathering the attained threshold (`tier_attained_min_points`), which would let the system tell a member's own decline from an admin raising the bar under them.
