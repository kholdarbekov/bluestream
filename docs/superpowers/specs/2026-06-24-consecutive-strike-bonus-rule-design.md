# Consecutive-Strike Bonus Rule — Design Spec

**Date:** 2026-06-24
**Status:** Approved (design), pending implementation plan
**Branch:** `new_architecture`
**Author:** loyalty team

## 1. Summary

Add a new admin-configurable loyalty rule type that **composes** one or more
existing "order strike" rules (`LoyaltyStreakRule`) and awards **AquaCoins** when
each (or any) attached strike is achieved **N times consecutively**, on each
strike's own rolling-window cadence. The reward repeats every N; a missed period
resets the streak to 0.

It is implemented **statelessly and ledger-derived**, mirroring the existing
strike engine — no per-user counter columns, no Celery beat. The public
`/loyalty-guide` marketing page is updated (config-driven, trilingual) to
describe the new bonus.

### Worked example
A rule attaches strike **A** ("3 orders / 30-day window") and strike **B**
("5 orders / 40-day window, each order ≥ 60 000 UZS"), `combine_mode = all`,
`required_consecutive = 6`. The customer earns the AquaCoins bonus only when
**A has been achieved 6 times back-to-back on its 30-day clock AND B has been
achieved 6 times back-to-back on its 40-day clock**. Each strike runs on its own
window; they are tracked independently.

## 2. Decisions locked during brainstorming

| Decision | Choice |
|---|---|
| Reward type | **AquaCoins only** (discount / free-product dropped) |
| Unit of "consecutive" | **Consecutive periods, period = the attached strike's own `window_days`** (not calendar months) |
| Multiple attached strikes | **Each tracked independently on its own window**; combined via a per-rule `combine_mode` toggle (`all` = every strike must reach N; `any` = one is enough) |
| Repeat behaviour | **Repeat every N**; a missed period resets that strike's run to 0 |
| Name | `LoyaltyConsecutiveStrikeRule` / admin tab "Consecutive Strikes" |
| Reset threshold | gap **< 2 × window** between adjacent achievements ("no skipped period") |
| Evaluation | Synchronous inside `update_streak`, stateless, no scheduler |

## 3. Current-state references (read before implementing)

- Models: `business_app/models/loyalty.py` — `LoyaltyStreakRule` (lines ~258–317),
  `LoyaltyTransaction` (FIFO ledger, `extra_data` carries `action_type` /
  `streak_rule_id`), `LoyaltyProgram`.
- Service: `business_app/services/loyalty_service.py` —
  `_qualifying_order_count` (~1578), `get_streak_progress` (~1591),
  `update_streak` (~1626), `_streak_rule_in_cooldown` (~1663),
  `award_points` (~720), `get_account_dashboard_for_user` (~312 surfaces
  `streak_progress`).
- Enums/constants: `business_app/utils/constants.py` —
  `LoyaltyActionType` (~120), `LoyaltyTransactionType` (~133).
- Admin API: `business_app/api/admin.py` — streak-rule CRUD
  (`/admin/loyalty/streak-rules`).
- Admin UI: `admin_ui/src/pages/LoyaltyPrograms.js` (Streak Rules tab),
  `admin_ui/src/services/adminService.js` (~661–680).
- Public page: `business_app/frontend/routes.py` —
  `get_public_loyalty_facts()`, `get_loyalty_handbook_context()` (~828–906),
  `loyalty_guide()` route (~894); template
  `business_app/templates/frontend/loyalty_guide.html`; CSS
  `business_app/static/css/pages/loyalty-guide.css`.
- Translations seed: `scripts/seed_backend_translations.py` **and**
  `scripts/seed_data.py` (dual-seed gotcha — both are active).

## 4. Data model

### 4.1 New model `LoyaltyConsecutiveStrikeRule` (`loyalty_consecutive_strike_rules`)

| Column | Type | Constraints / notes |
|---|---|---|
| `id` | Integer | PK |
| `program_id` | Integer | FK→`loyalty_programs.id`, NOT NULL, indexed |
| `name` | String(100) | NOT NULL, `@translatable` (en/ru/uz) |
| `required_consecutive` | Integer | NOT NULL, CHECK `>= 1` (UI enforces ≥ 2 as meaningful) |
| `combine_mode` | String(8) | NOT NULL, default `'all'`; values `'all'` \| `'any'` |
| `bonus_points` | Integer | NOT NULL, CHECK `>= 0` (AquaCoins reward) |
| `is_active` | Boolean | default True |
| `starts_at` | DateTime(tz) | nullable (effective window) |
| `ends_at` | DateTime(tz) | nullable (effective window) |
| `display_order` | Integer | default 0 |
| `created_at` / `updated_at` | DateTime(tz) | TimestampMixin |

Methods (reuse existing patterns): `to_dict()` (returns the default-language
`name`; per-language localization is done at the surface layer via the
translation system — `get_all_translations` in the admin feed and the
language-keyed `i18n()` in `get_public_loyalty_facts`),
`is_effective(now)` (active + within `[starts_at, ends_at]`, tz-normalized via
`ensure_utc`).

Relationship: `strikes` → M2M to `LoyaltyStreakRule` via association table.

### 4.2 Association table `loyalty_consec_rule_strikes`

| Column | Type | Notes |
|---|---|---|
| `consecutive_strike_rule_id` | Integer | FK→`loyalty_consecutive_strike_rules.id`, ON DELETE CASCADE |
| `streak_rule_id` | Integer | FK→`loyalty_streak_rules.id`, ON DELETE CASCADE |

Primary key = `(consecutive_strike_rule_id, streak_rule_id)`. No extra columns
(N and combine_mode live on the parent).

### 4.3 Migration

Single Alembic migration (head off the latest loyalty migration on
`new_architecture`): create both tables + CHECK constraints in the style of
`b8e3c9f5d2a4`. No data backfill (forward-only). Test on a DB copy before
`flask db upgrade`.

### 4.4 Enum

Add `LoyaltyActionType.CONSECUTIVE_STREAK_BONUS = "consecutive_streak_bonus"`.
Ledger `transaction_type` remains `BONUS`.

## 5. Evaluation algorithm (stateless, ledger-derived)

### 5.1 Per-strike consecutive count
`_strike_consecutive_count(user_id, strike_rule, now) -> int`
1. Query the user's achievement timestamps for this strike from the ledger:
   `LoyaltyTransaction` rows where `extra_data.action_type == "streak_bonus"`
   and `extra_data.streak_rule_id == strike_rule.id`, ordered by `created_at`
   descending.
2. `W = strike_rule.window_days`. Walk pairwise from the most recent: run starts
   at 1 (the latest achievement); for each older adjacent achievement, if the gap
   to the next-newer one is **< 2·W days**, increment the run; otherwise stop.
3. Return the run length (the current unbroken run ending at the latest
   achievement). Returns 0 if there are no achievements.

Rationale for `< 2·W`: the strike's own cooldown already forces achievements
≥ W apart, so "achieved again before a full extra window lapses" ⇔ gap < 2·W ⇔
"no skipped period." This is the precise reading of "consecutive periods, period
= window," re-anchored per achievement (anchor-free, fully stateless).

### 5.2 Combined count
For a `LoyaltyConsecutiveStrikeRule` over its attached strikes:
- `combine_mode == 'all'` → `combined = min(per-strike counts)`
- `combine_mode == 'any'` → `combined = max(per-strike counts)`

Edge cases: a rule with zero attached strikes never fires (and is rejected at
create/update). A rule attaching an inactive/ineffective strike still reads that
strike's historical achievements but new ones stop accruing (acceptable;
documented).

### 5.3 Award, repeat-every-N, idempotency
`update_consecutive_strikes(user_id, commit=True)`:
For each active, currently-effective `LoyaltyConsecutiveStrikeRule` in the user's
program:
1. `combined = ` combined count (5.2). If `combined < N` → skip.
2. `target_awards = combined // N`.
3. `run_start = ` earliest achievement timestamp of the current combined run
   (for `all`: the max of the per-strike run-start times among required strikes;
   for `any`: the run-start of the strike attaining the max). `already = ` count
   of this rule's prior meta-bonus ledger entries (`action_type ==
   "consecutive_streak_bonus"`, `consecutive_strike_rule_id == rule.id`) with
   `created_at >= run_start`.
4. If `target_awards > already`, award one row per milestone index `k` in
   `range(already + 1, target_awards + 1)` (normally one) via
   `award_points(user_id, bonus_points, rule.name,
   LoyaltyActionType.CONSECUTIVE_STREAK_BONUS, extra_data={
   "consecutive_strike_rule_id": rule.id, "milestone": k}, commit=False)` —
   so `milestone` is the running count (1, 2, 3, …), not the constant
   `target_awards`. A rule with `bonus_points <= 0` is skipped (no award row;
   `award_points` rejects non-positive points), and the admin CRUD requires
   `bonus_points >= 1`.
5. Commit once at the end when `commit=True`.

This yields "repeat every N" and idempotency with **zero stored state** —
re-running the evaluation never double-awards.

### 5.4 Trigger / integration
A new strike achievement is the only event that can advance a consecutive count,
so hook into `update_streak()` (`loyalty_service.py:~1626`): **after** the
existing strike-award loop and before/with its commit, call
`update_consecutive_strikes(user_id, commit=...)`. Resets are computed lazily at
evaluation time (a skipped period simply shortens the walked run). No Celery beat
is added.

## 6. Customer-facing progress + APIs

### 6.1 Progress
`get_consecutive_strike_progress(user_id) -> list` returns, per active/effective
rule:
```
{
  "name": <translated>,
  "required_consecutive": N,
  "combine_mode": "all" | "any",
  "bonus_points": <int>,
  "combined_current": <int, capped at N>,
  "per_strike": [
    {"strike_name": <translated>, "current": <int capped at N>, "target": N,
     "window_days": W, "active": <bool: now - last_achievement < 2*W>}
  ]
}
```
Surfaced in `get_account_dashboard_for_user` / tier-info alongside
`streak_progress` (parity with existing strikes). `active=false` signals the run
is at risk / already broken for the next achievement (display hint only).

### 6.2 Admin REST CRUD
Mirror the streak-rule endpoints in `business_app/api/admin.py`:
- `GET /admin/loyalty/consecutive-strike-rules?program_id=` → list
  (includes resolved attached-strike summaries).
- `POST /admin/loyalty/consecutive-strike-rules` → create
  (body: name + translations, required_consecutive, combine_mode,
  `bonus_points` (≥ 1), `strike_rule_ids: [..]`, is_active, display_order,
  and the optional effective-window `starts_at`/`ends_at` ISO datetimes —
  same as the streak-rule CRUD). Validates ≥1 attached strike, that strikes
  belong to the same program, and `ends_at > starts_at`.
- `PUT /admin/loyalty/consecutive-strike-rules/<id>` → update (incl. re-attach,
  and `starts_at`/`ends_at`; partial updates do not clobber the unsent field).
- `DELETE /admin/loyalty/consecutive-strike-rules/<id>` → hard delete
  (no per-user state; safe).

Business logic lives in the service layer, not the route (project convention).
Add `UPDATE_API_SNAPSHOT=1` regen for the new routes.

## 7. Admin UI

New **"Consecutive Strikes"** tab in
`admin_ui/src/pages/LoyaltyPrograms.js`, modeled on the Streak Rules tab:
- List table: name, required_consecutive, combine_mode, attached strikes (tags),
  bonus_points, status, actions.
- Create/edit modal form fields: `name` + `name_ru`/`name_uz`,
  `required_consecutive` (InputNumber min 2), `combine_mode` (Select AND/ANY),
  `bonus_points` (InputNumber min 0), **attached strikes** (multi-`Select` of the
  selected program's `LoyaltyStreakRule`s), `is_active` (Switch).
- React-Query `useQuery`/`useMutation` keyed `['loyalty-consecutive-strike-rules',
  programId]`; new `adminService` methods
  (`get/create/update/deleteLoyaltyConsecutiveStrikeRule`).
- Submit asserts (test): exact payload incl. `strike_rule_ids` and the AND/ANY
  value (per the assert-payloads-not-call-occurrence convention; antd 2-step
  modal caveat does not apply — single step).

## 8. `/loyalty-guide` page update (required)

- `get_public_loyalty_facts()` and `get_loyalty_handbook_context()` gain
  `consecutive_strike_rules`: list of `{name, required_consecutive, combine_mode,
  strike_names: [..], bonus_points}`, language-keyed — so the page **and**
  `/api/public/loyalty.json` (MemberProgram feed) both reflect it.
- New config-driven card in the Earn section (Aqua Club idiom; `fa-trophy` or
  `fa-crown`), rendered only when ≥1 effective rule exists. Each rule renders as,
  e.g.: *"Achieve [3 orders / 30 days] and [5 orders / 30 days] 6 times in a row
  → +N AquaCoins. Repeats every 6."* (AND/ANY phrasing varies by `combine_mode`).
- One new FAQ entry (q/a) about the consecutive bonus, appended to the FAQPage
  JSON-LD list.
- New trilingual keys under `loyalty_guide.earn.consec_*` (+ `faq.qN/aN`), seeded
  in **both** `scripts/seed_backend_translations.py` and `scripts/seed_data.py`,
  INSERT-on-conflict aware. No hardcoded copy in the template.

## 9. Testing (TDD — write tests first)

- **Unit (`tests/unit/`):**
  - `_strike_consecutive_count`: consecutive run, skipped-period reset (gap ≥ 2·W),
    cooldown-spaced achievements, zero achievements.
  - `update_consecutive_strikes`: AND (all reach N), ANY (one reaches N),
    below-threshold no-award, repeat-every-N (2N → second award), idempotency
    (re-run no double-award), effective-date / inactive gating, zero-strike rule
    never fires.
  - `get_consecutive_strike_progress`: caps at N, `active` flag, per-strike split.
- **Integration/API:** admin CRUD happy-path + validation (no strikes, cross-program
  strike rejected); customer dashboard includes the new progress block; snapshot
  regen.
- **Admin UI (Vitest):** tab renders rows; create modal submits exact payload
  (assert `strike_rule_ids` + `combine_mode`).
- **Migration:** apply on a DB copy, verify tables + CHECK constraints, then
  `flask db upgrade` on dev.

Run backend suite via the `business_app` container
(`bash scripts/precommit-backend-tests.sh`); admin UI via
`cd admin_ui && npm test --silent`.

## 10. Deploy / rollout notes

- Apply the migration (`flask db upgrade`) — dev first, prod after verification on
  a copy.
- Reseed translations via the mounted-path workaround (scripts are **not** mounted
  into `business_app`):
  `docker compose exec -T business_app python - < scripts/seed_backend_translations.py`.
- Restart `business_app` (+ `celery_worker` if the evaluation path is reachable
  from tasks). Rebuild `admin_ui` for the new tab.
- Commits/pushes are performed by the user, not the agent.

## 11. Out of scope (follow-ups)

- Telegram-bot display of consecutive-strike progress (customer dashboard only,
  for now).
- Discount / free-product reward types for this rule (explicitly dropped).
- Retroactive backfill of pre-existing achievements (forward-only by design).
- Per-rule custom period decoupled from strike windows (rejected — period = each
  strike's own window keeps tracking simple).

## 12. Open questions

None outstanding. Naming, reset threshold (gap < 2·W), and the synchronous
ledger-derived evaluation hook were confirmed during brainstorming.
