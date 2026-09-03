# Make the price follow the tier badge, and show the number that decides it

**Date:** 2026-09-03
**Branch:** new_architecture
**Status:** approved, not yet planned

## Problem

A Silver member opened the Telegram payment picker and saw a plain
`💰 Наличными` button and an undiscounted `Итого: 54,000 UZS`. Silver carries a
1.5% cash-on-delivery discount, so the button should have read
`💰 Наличными −1.5% 🏷` and the confirm screen should have shown a `−810 UZS`
tier line.

The render path is not at fault. Production logs show
`POST /api/v1/orders/cart/estimate` returned 200 at 09:28:11 UTC, two minutes
after both containers restarted onto code whose in-container SHA-256 is
byte-identical to the host tree. Every `ru` translation key the screens use is
seeded and active. The quote itself carried `tier_discount: 0.00`.

The cause is that the system holds **two different answers to "what tier is this
member"**:

| Consumer | Basis | Answer for user 1 |
|---|---|---|
| Every display surface | stored `loyalty_points.current_tier` | Silver |
| Earning multiplier (`calculate_points_for_purchase`) | stored `current_tier` | Silver (1.05×) |
| **Cash discount (`quote_tier_discount`)** | **recomputed live from trailing-365d points** | **Bronze (0%)** |

`_check_tier_upgrade` promoted user 1 to Silver on 2026-08-31 and set a 365-day
downgrade guarantee (`tier_valid_until = 2027-08-31`; see §1a — a floor on
demotion, not an expiry). On 2026-09-02 at 18:01 an admin raised
`Silver.min_points` from 3000 to 4000 (and Gold 12000→15000, Platinum
→50000, plus the discount rates). User 1's 3,488 qualifying points now fall below
the new floor, so the live recomputation returns Bronze while the badge — and
every screen — still says Silver.

**Blast radius:** 12 members carry a Silver badge but live-resolve to Bronze:
user ids 1, 8, 10, 25, 40, 54, 56, 58, 68, 115, 236, 281. All 12 hold an
unexpired downgrade guarantee (2027-08-31 to 2027-09-02), so no data backfill is
required. Across all 1,223 orders in production, zero have ever carried
`tier_discount > 0`.

`loyalty_tier_configs` has no `audit_logs` resource type, so the threshold change
left no audit trail.

### Non-cause, recorded to prevent a wasted fix

`Bronze.max_points` is 3000 while `Silver.min_points` is 4000, leaving a visible
3001–3999 band. `LoyaltyTierConfig.get_tier_for_points` selects purely on
`min_points` descending and never reads `max_points`, so closing that band
changes no pricing behaviour. It is a display defect only (see §5, §6).

## Design

### 1. `effective_tier()` — price follows the badge

A module-level helper in `business_app/services/loyalty_service.py`, beside
`clamp_tier_discount` and `apply_tier_discount_for_rail`:

```python
def effective_tier(account) -> Optional[LoyaltyTierConfig]:
    """The tier the member's badge shows — the one their benefits follow.

    Price follows the badge, never a second opinion, so a threshold edit
    can never reprice a member without also visibly demoting them. The
    live tier is still considered so a badge that has not caught up yet
    can only ever help, never hold someone back.

    tier_valid_until is deliberately NOT consulted here. It is a
    downgrade guarantee owned by _check_tier_upgrade, not an expiry, and
    reading it here would drop the discount up to a month before the
    badge changed.
    """
    stored = LoyaltyTierConfig.query.filter_by(
        name=account.current_tier, program_id=account.program_id, is_active=True
    ).first()
    live = LoyaltyTierConfig.get_tier_for_points(
        LoyaltyService().calculate_qualifying_points(account.user_id),
        account.program_id,
    )
    candidates = [t for t in (stored, live) if t]
    return max(candidates, key=lambda t: t.display_order, default=None)
```

Taking the maximum rather than `stored` alone matters when an admin *lowers* a
threshold: live jumps immediately while the badge waits for the member's next
award or the monthly job, and the benefit should not wait with it.

**Callers changed:**

- `LoyaltyService.quote_tier_discount` (loyalty_service.py:192-201) replaces its
  `get_tier_for_points(calculate_qualifying_points(...))` lookup with
  `effective_tier(account)`. Gates 1 (`is_cod_rail`), 2 (`is_user_loyalty_eligible`)
  and 4 (`percentage > 0`) are unchanged.
- `LoyaltyService.calculate_points_for_purchase` / `_get_tier_multiplier`
  (loyalty_service.py:245-256) route through `effective_tier` so the earning
  multiplier and the discount can never diverge again.

Neither caller can lose a benefit from this change: both previously read the
badge or something lower, and `effective_tier` never returns below the badge.

`_check_tier_upgrade` keeps using live qualifying points and remains the **only**
writer of the badge. It owns the stored tier and must never read its own output.

### 1a. What `tier_valid_until` is, and is not

A tier does **not** expire. `tier_valid_until` is a downgrade *guarantee floor*:
`_check_tier_upgrade` CASE 2 demotes only when live points fall below the
threshold **and** that date has passed, and CASE 3 pushes it forward another 365
days every time the member is re-confirmed — which happens on every award. For an
active member it rolls forward indefinitely. The column comment
(`models/loyalty.py:425`) and the web page's `guaranteed_until` label
(`static/js/pages/loyalty.js:109-113`) already state it this way.

This behaviour is unchanged by this work. It is recorded here because it is the
only thing currently protecting the 12 affected members, and because customer
copy must never render this date as an expiry.

**Known gap, deliberately not solved here.** The system cannot distinguish "the
member's activity declined" from "an admin raised the bar under them" — CASE 2
compares live points against whatever the threshold is *today*, with no memory of
the bar the member actually cleared. Today only the guarantee window separates
those cases, and it protects both indiscriminately. Grandfathering the attained
threshold (a `tier_attained_min_points` column) would solve it properly; that is
a separate task, and §4's impact confirmation is the interim mitigation.

### 2. Bot loyalty screen shows the deciding number

`LoyaltyService.get_points_summary_for_user` (loyalty_service.py:380-390) gains
`qualifying_points`, `next_tier`, `points_to_next_tier`, `points_needed_to_keep`
and the effective tier's `discount_percentage`.

**No date is shown.** A tier does not expire (§1a), and any date on this screen
reads as one — it would also shift forward silently on every order.
`points_needed_to_keep` is the actionable equivalent, reusing
`get_requalification_info` (loyalty_service.py:1780-1793):
`max(0, current_tier.min_points − qualifying_points)`.

`telegram_bot/handlers/loyalty.py:242-255` stops reading `lifetime_earned` and
renders:

```
🏆 Мои AquaCoins

🥈 Уровень: Silver
   Скидка 1,5% при оплате наличными
   ⚠️ Ещё 512 AquaCoins, чтобы сохранить уровень

🏆 Текущий баланс: 988 AquaCoins
📈 За последние 12 месяцев: 3 488 AquaCoins
   До Gold: ещё 11 512
```

When `points_needed_to_keep` is 0 the warning line becomes `✅ Статус закреплён`.
The 512 above is real: it is what user 1 now needs to clear Silver's raised
4,000 bar. The member keeps the discount meanwhile, because price follows the
badge.

New keys, seeded in `en`/`ru`/`uz` through `scripts/seed_backend_translations.py`
under category `telegram`:

| Key | en |
|---|---|
| `telegram.loyalty.qualifying_12m` | Last 12 months |
| `telegram.loyalty.tier_line` | Level: {tier} |
| `telegram.loyalty.tier_cod_perk` | {pct}% off when you pay cash on delivery |
| `telegram.loyalty.tier_secured` | Status secured |
| `telegram.loyalty.tier_keep_hint` | {points} more AquaCoins to keep this level |
| `telegram.loyalty.to_next_tier` | To {tier}: {points} more |

`telegram.loyalty.lifetime_earned` stays in the catalogue (other surfaces use it)
but leaves this screen. The web loyalty page needs no change —
`calculate_tier_progress` already runs on qualifying points, and it already
renders the same retention hint.

The tier line renders without the perk clause when the effective tier's
`discount_percentage` is 0, and without the "to next" line at the top tier.

### 3. Notifications

`_check_tier_upgrade` CASE 2 parks a `KIND_TIER_DOWNGRADE` entry on the same
post-commit dispatcher CASE 1 uses
(`business_app/utils/loyalty_award_dispatch.py`), carrying `user_id`, `tier`,
`tier_config_id`, `qualifying_points` and `required_points` — the latter being
`min_points` of the tier the member just lost, read before the stored tier is
overwritten. The drain loop gains
a `_send_tier_downgrade_notification` branch calling
`send_loyalty_notification_task.delay(user_id, "tier_downgrade", {...})`.

Sorting in `_drain_on_commit` keys on `kind != KIND_AWARD`; adding a third kind
keeps awards first and leaves upgrade/downgrade order stable, since only one tier
event can be parked per account per transaction.

Copy is routed by `event_type`, the mechanism from `0a92069`, so a downgrade can
never render through the AquaCoins-earned template.

Behaviour matrix:

| Transition | Message |
|---|---|
| Stored tier moves up | Congratulation (unchanged) |
| Re-qualifies at the same tier | Silent, guarantee pushed forward (unchanged) |
| Stored tier moves down | New notice naming qualifying points and the threshold missed |
| Admin edits a threshold | Nothing directly; the monthly job's resulting downgrades notify |

CASE 2 also gains the `_update_points_to_next_tier(account, current_tier_config)`
call it currently skips when the guarantee blocks a downgrade — the stale-number fix
folded in from review. `points_to_next_tier` for the 12 affected accounts is
currently computed against Gold's retired 12,000 floor (user 1 shows 8,512; the
correct figure is 11,512).

### 4. Admin tier editor guardrails

`PUT /api/v1/admin/loyalty/tiers/<id>` (`business_app/api/admin.py:7580-7620`)
and the create endpoint currently assign `min_points`/`max_points` with no
validation, no audit row, and no impact check.

Both gain, before commit:

1. **Ladder validation.** Build the post-edit tier set ordered by
   `display_order`; require `min_points` strictly increasing and each tier's
   `max_points` equal to the next tier's `min_points`, with the last `NULL`.
   Violations return `422` with `{"error": "threshold_gap" | "threshold_overlap",
   "detail": ...}`.
2. **Impact confirmation.** Count accounts whose stored tier is the edited tier,
   whose downgrade guarantee has not lapsed, and whose qualifying points fall below the new
   `min_points`. If that count is above zero and the payload lacks
   `confirm_impact: true`, return `409 impact_confirmation_required` with the
   count and tier breakdown. Re-submitting with `confirm_impact: true` proceeds.
3. **Audit row.** Write `audit_logs` with `action="loyalty_tier_updated"`,
   `resource_type="loyalty_tier_config"`, `resource_id=tier.id`, and old/new
   values for every changed field.

The existing `invalidate_cache("response:*:/api/v1/loyalty/tiers*")` call stays.
`admin_ui` surfaces the 409 as a confirmation dialog naming the affected count.

### 5. Order screens show the discount

`telegram_bot/utils.py::MessageBuilder.build_order_summary` (utils.py:802-815)
prints only number, date, total and status; the order-details screen
(`telegram_bot/handlers/orders.py:509-540`) then lists item lines. Once discounts
fire, items sum to 54,000 while the total reads 53,190 with nothing explaining
the gap.

`build_order_summary` gains a money-breakdown block rendered between the items
and the total whenever any discount is non-zero, reading the fields
`business_app/serializers/order_serializers.py:339` already serializes
(`subtotal`, `discount_amount`, `loyalty_discount`, `tier_discount`,
`delivery_fee`, `total_amount`). It reuses the four keys
`_build_estimate_block` already renders — `telegram.orders.estimate_discount_line`,
`estimate_reward_line`, `estimate_tier_line` and `delivery_fee` — so no new copy
is seeded and the wording matches the checkout screens exactly. No arithmetic is
performed bot-side; every figure is read from the serialized order. This covers
all three call sites: order details (orders.py:510) and both order-placed
confirmations (orders.py:1420, 1515).

### 6. Web tier-progress bar

`get_account_dashboard_for_user` (loyalty_service.py:438-460) can emit a negative
`tier_progress.current` for the 12 affected members, because their qualifying
points sit below their own stored tier's floor.
`static/js/pages/loyalty.js:128-132` divides straight into a width percentage.
Clamp `current` and `points_needed` at zero server-side.

## Data changes

No backfill. §1 alone restores all 12 members: their badges already read Silver,
and the price now follows the badge.

One optional cosmetic correction: `Bronze.max_points` 3000 → 4000, so the
published tier table has no visible hole. It must be done **through the admin UI**,
because that path busts the `/loyalty/tiers` response cache; a raw SQL update
does not. It changes no pricing.

## Testing

- **Unit** — `effective_tier` matrix: badge higher than live; live higher than
  badge (an admin threshold cut); badge and live equal; stored name with no active
  config; no tiers configured at all. Plus an explicit test that the result does
  **not** change when `tier_valid_until` is moved into the past — pricing must not
  read the guarantee.
- **Integration, pinned to the real shape** — badge Silver, qualifying 3,488,
  `Silver.min_points` 4,000: a cash estimate returns
  `tier_discount` = 1.5% of subtotal and `tier_discount_percentage` = 1.5; the
  same basket on `click` returns 0.
- **Consistency** — earning multiplier and discount resolve the same tier for the
  same account, and neither ever resolves below the badge.
- **Notifications** — a blocked downgrade parks nothing and refreshes
  `points_to_next_tier`; a real downgrade parks exactly one `tier_downgrade`
  entry with both point figures; re-qualifying at the same tier parks nothing.
- **Admin** — a gapped ladder is rejected 422; a stranding edit returns 409 then
  succeeds with `confirm_impact`; an audit row lands with old and new values.
- **Bot** — the loyalty screen renders qualifying points, not lifetime; all four
  new keys resolve in `en`/`ru`/`uz`; the order screens print the tier line when
  `tier_discount > 0` and omit it at 0.
- **Regression** — `tests/integration/test_tier_discount_*.py` stay green.

Run under the isolated-runner pattern: the host has no pytest, and the prod
container's Redis flush would wipe production Redis.

## Deployment

No migration. Sequence: seed translations over stdin (`scripts/` is not mounted
into the container), flush both the translation cache and the
`response:*:/api/v1/loyalty/tiers*` entries, restart `business_app`,
`telegram_bot` and `celery_worker` (all three read source at process start under
the bind mount), then `docker compose build admin_ui && docker compose up -d admin_ui`
for the editor changes. `staff_bot` needs no restart.

Rollback: set every tier's `discount_percentage` to 0 through the admin UI. Gate 4
of `quote_tier_discount` refuses a zero rate, so this takes effect on the next
order with no deploy.

## Out of scope

- `total_earned` counts `ADJUSTMENT` points that the tier basis excludes, so the
  lifetime figure overstates. Separate task.
- Entity user 55 holds 5,380 qualifying points and a Silver badge while
  contractually excluded from loyalty. Separate task.
- The payment picker quotes cash without `reward_id` while the confirm screen
  quotes with it, so the button can advertise a rate the confirm screen's clamp
  then reduces. Pre-existing, unmasked by this work. Separate task.
- Operator phone orders still publish `tier_discount: 0.00` unconditionally, a
  known gap recorded in the deploy runbook.
