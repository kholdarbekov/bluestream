# Bottle session enforcement — implementation plan

Status: implemented (PR 1 — additive, flag-gated)
Owner: backend
Trigger incident: order `AD_000205_26` (id=324) delivered on 2026-05-15 06:01:41 UTC by driver user_id=116 without any open bottle session. See "Incident summary" below.

Code changes landed:
- `business_app/services/bottle_tracking_service.py` — new `assert_driver_can_progress_delivery`, `_strict_enforcement_enabled`, `_open_bindings_count_for_session`; `close_bottle_session` now refuses close when undelivered bindings remain.
- `business_app/services/staff_service.py` — `accept_order` now binds at accept; `update_delivery_status` now calls the new guard.
- `business_app/services/delivery_service.py` — `update_delivery_status`, `mark_delivery_arrived`, `begin_delivery_in_transit` now call the guard.
- `business_app/services/order_service.py` — replaced the silent skip-tally branch with a `_handle_missing_bottle_session_on_delivery` helper; tally now reads the bound session, not effective; `ValidationError` from the bottle block is re-raised instead of swallowed.
- `business_app/tasks/delivery_tasks.py` — `process_delivery_confirmation_task` calls the guard up front and short-circuits on `ValidationError` instead of retrying.
- `business_app/api/delivery.py` — `complete_delivery` returns 400 synchronously when the guard fails.
- `business_app/config/base.py` — new `BOTTLE_SESSION_ENFORCEMENT_STRICT` flag (default `False`).
- `tests/unit/test_bottle_session_integration.py` — updated stale test + 14 new tests including the AD_000205_26 regression.
- `scripts/backfill_order_324_bottle_session.py` — one-shot reconciliation script with `--dry-run` / `--commit` modes.

To enable strict enforcement (PR 2): set `BOTTLE_SESSION_ENFORCEMENT_STRICT=true` in the relevant environment.

---

## 1. Incident summary

Order 324 (admin-created, 5× returnable 19L bottle) was accepted by driver 116 on 2026-05-14 15:11:41 while session 28 was open (valid). Session 28 was then closed at 15:52:54 the same day. The driver completed `picked_up → in_transit → arrived → delivered` the next morning between 05:06 and 06:01 on 2026-05-15 with **no** open session for that driver. The next session (id=30) was only opened at 07:59:40 — after the delivery was already marked delivered — when the driver tried to accept the next order.

Symptoms observed in DB:
- `driver_bottle_session_orders` has 0 rows for `order_id=324` (order was never bound to any session).
- None of sessions 28/29/30 has the 5 bottles in its `bottles_delivered` tally (truck-side ledger is desynced).
- Customer-side `bottle_ledger` rows 204 + 205 were written correctly (customer balance is intact).
- `delivery_status_history` shows the entire flow ran through the staff bot.

Code-side root cause: the session guard exists in exactly one place — `StaffService.accept_order()` ([business_app/services/staff_service.py:900-915](../business_app/services/staff_service.py#L900-L915)) — and is absent from every subsequent transition. The DELIVERED handler in `OrderService._handle_status_change_actions()` ([business_app/services/order_service.py:1474-1509](../business_app/services/order_service.py#L1474-L1509)) silently logs `no effective session — skipping tally` instead of failing.

---

## 2. Invariants to enforce

A driver completing a delivery for an order that contains returnable bottles must, **at every state transition from `assigned` onward**, satisfy:

1. **Session presence** — the driver has an effective session (own OPEN session, or active membership in another driver's session) at the moment of the transition.
2. **Session continuity** — the session used at acceptance is the same session crediting the delivery. The order is bound to that session for its lifetime.
3. **Capacity** — the session's `current_inventory` is sufficient to cover the bottles needed by every order still bound to it but not yet delivered.
4. **Closure safety** — a session cannot be closed (normal close) while it still has bound, undelivered orders.

Out of scope: customer-side ledger semantics are already correct and stay unchanged.

---

## 3. Design decisions

### 3.1 Bind at accept, not at deliver

Today the order ↔ session binding (`DriverBottleSessionOrder` row) is created inside the DELIVERED handler, gated on `if effective_session:`. That is too late: if the driver no longer has an effective session by then, the binding silently fails to be created. We move the binding to `accept_order()`, immediately after the existing capacity check. This makes the binding atomic with the assignment and avoids the "skipping tally" code path entirely for the happy case.

### 3.2 Hard fail on missing session at every transition (not just delivered)

Add a single helper — `BottleTrackingService.assert_driver_can_progress_delivery(delivery)` — that:
- Looks up the order's bound session (from `DriverBottleSessionOrder`).
- Verifies that session is still OPEN.
- Verifies that the driver's current effective session is the same session (i.e. driver hasn't switched cars / hasn't been kicked from the co-driver membership).
- Raises `ValidationError(BOTTLE_SESSION_REQUIRED | BOTTLE_SESSION_MISMATCH | BOTTLE_SESSION_CLOSED)` otherwise.

Call it from each transition point. Cheap (single indexed lookup on `driver_bottle_session_orders.order_id` which is already `UNIQUE`).

### 3.3 Replace silent "skip tally" with a hard error

[order_service.py:1506-1509](../business_app/services/order_service.py#L1506-L1509) is the line that masked this bug for an unknown length of time. After this change, hitting that branch means an invariant violation has slipped past the guards; it should raise, not log.

The DELIVERED flow becomes: *look up `DriverBottleSessionOrder` for the order → if missing, raise `BOTTLE_SESSION_REQUIRED` → tally the bound session*. The previous `get_effective_session(tally_driver_id)` lookup is removed because it could credit the wrong session if the driver's context had drifted between accept and deliver.

### 3.4 Block close of a session that still owns undelivered orders

Closing a session today is a one-step operation. Add a precondition: any `DriverBottleSessionOrder` whose order is not in a terminal status (`delivered`, `cancelled`, `returned`, `failed`) blocks normal close. Force-close (admin) remains available with an explicit reason.

Cancelled / returned orders are filtered out by the predicate itself — the binding row stays as a historical record but is ignored by the close-precondition. No soft-delete is needed (resolves Open Question §10.3).

### 3.5 Feature flag — gate at the helper, not at every call site

The `BOTTLE_SESSION_ENFORCEMENT_STRICT` flag (default `False` in PR 1, `True` in PR 2) lives **inside `assert_driver_can_progress_delivery`**. When the flag is off the helper logs a WARN and returns `None`; when on it raises. This guarantees every call site behaves consistently and lets PR 1 measure the population of at-risk in-flight orders before PR 2 flips enforcement on. The same flag also gates the new hard-error replacement in `order_service` (§4.3).

---

## 4. Code changes — file by file

### 4.1 `business_app/services/bottle_tracking_service.py`

**Add** a new method below `assert_delivery_within_session_capacity` (around line 1114):

```python
def assert_driver_can_progress_delivery(self, delivery: "Delivery") -> Optional[DriverBottleSession]:
    """
    Guard called before any post-assignment delivery transition.

    Returns the bound session if everything is consistent, or raises
    ValidationError. Returns None only when the order has no returnable
    bottles (in which case session checks are not required for this order).
    """
    order = delivery.order
    if not order:
        return None
    bottles_needed = self.calculate_bottles_for_order(order)
    if bottles_needed <= 0:
        return None

    binding = DriverBottleSessionOrder.query.filter_by(order_id=order.id).first()
    if not binding:
        raise ValidationError(
            "Order has no bottle-session binding; cannot progress delivery.",
            error_code="BOTTLE_SESSION_REQUIRED",
        )

    session = DriverBottleSession.query.get(binding.session_id)
    if not session or session.status != DriverBottleSessionStatus.OPEN:
        raise ValidationError(
            f"Bound session {binding.session_id} is not OPEN.",
            error_code="BOTTLE_SESSION_CLOSED",
        )

    effective = self.get_effective_session(delivery.delivery_person_id)
    if not effective or effective.id != session.id:
        raise ValidationError(
            "Driver's current session does not match the order's bound session.",
            error_code="BOTTLE_SESSION_MISMATCH",
        )

    return session
```

**Modify** `bind_order_to_session` (line 1067) — already idempotent, no change needed.

**Add** a precondition check inside the existing session-close path (search for the close service method, likely `close_session` or `close_driver_session`; if it lives in another service, add the same check there):

```python
open_bindings = (
    DriverBottleSessionOrder.query
    .join(Order, DriverBottleSessionOrder.order_id == Order.id)
    .filter(DriverBottleSessionOrder.session_id == session.id)
    .filter(Order.status.notin_([OrderStatus.DELIVERED, OrderStatus.CANCELLED,
                                 OrderStatus.RETURNED]))
    .count()
)
if open_bindings > 0 and not force_close:
    raise ValidationError(
        f"Cannot close session {session.id}: {open_bindings} undelivered order(s) still bound.",
        error_code="BOTTLE_SESSION_HAS_OPEN_ORDERS",
    )
```

### 4.2 `business_app/services/staff_service.py`

**Modify** `accept_order()` ([line 900-915](../business_app/services/staff_service.py#L900-L915)):

After the existing capacity check, add:

```python
binding = _bottle_svc.bind_order_to_session(
    _effective_session.id,
    delivery.order.id,
    accepted_by_driver_id=delivery_person_id,
)
current_app.logger.info(
    f"[BOTTLE] accept_order bound order={delivery.order.id} → session={_effective_session.id} binding={binding.id}"
)
```

Wrap inside the existing `if _bottles_needed > 0` branch. Idempotent per the unique constraint `uq_dbso_order`.

**Modify** `update_delivery_status()` ([line 1032 onward](../business_app/services/staff_service.py#L1032)). Right after the transition-validation block and **before** the actual `delivery.status = new_status_enum` mutation:

```python
if new_status in ("picked_up", "in_transit", "arrived", "delivered"):
    from business_app.services.bottle_tracking_service import BottleTrackingService
    BottleTrackingService().assert_driver_can_progress_delivery(delivery)
```

This is the single guard that would have blocked the AD_000205_26 incident at `picked_up`.

### 4.3 `business_app/services/order_service.py`

**Remove** the binding code from `_handle_status_change_actions()` DELIVERED branch ([lines 1489-1497](../business_app/services/order_service.py#L1489-L1497)) — binding now happens at accept.

**Replace** the silent "skip tally" branch ([lines 1506-1509](../business_app/services/order_service.py#L1506-L1509)) with a hard error:

```python
else:
    raise ValidationError(
        f"Order {order.id} reached DELIVERED with no effective session for driver={tally_driver_id}; "
        f"this should have been blocked upstream.",
        error_code="BOTTLE_SESSION_REQUIRED",
    )
```

After deploy, hitting this branch is an invariant violation worth a Sentry alert (see §7).

**Change** the tally lookup: instead of `get_effective_session(tally_driver_id)`, look up the order's bound session via `DriverBottleSessionOrder.query.filter_by(order_id=order.id).first()` and tally that session. This is the "session continuity" invariant in action — credit the load that the bottles came from, not whichever session happens to be open right now.

### 4.4 `business_app/tasks/delivery_tasks.py`

**Modify** `process_delivery_confirmation_task` (around line 921, before the `OrderService().update_order_status(..., DELIVERED)` call):

```python
from business_app.services.bottle_tracking_service import BottleTrackingService
BottleTrackingService().assert_driver_can_progress_delivery(delivery)
```

Same guard, same error semantics as staff bot.

### 4.5 `business_app/api/delivery.py`

**Modify** `complete_delivery` ([lines 525-563](../business_app/api/delivery.py#L525-L563)) — add the same guard before enqueuing the Celery task, so the driver mobile app gets the error synchronously rather than discovering it via a delayed task failure.

### 4.6 `business_app/services/delivery_service.py` — driver-app transition path

The driver mobile app reaches DELIVERED via:
`api/delivery.complete_delivery` → `process_delivery_confirmation_task` → `OrderService.update_order_status(DELIVERED)` → `_handle_status_change_actions` → `DeliveryService.complete_delivery` → `DeliveryService.update_delivery_status`. The guards in `staff_service.update_delivery_status` do not cover this path. Add the guard at the lowest common denominator:

- `update_delivery_status` ([delivery_service.py:171](../business_app/services/delivery_service.py#L171)) — call `assert_driver_can_progress_delivery(delivery)` right after the transition-validation block (line 203) and before mutating `delivery.status` (line 210), for `new_status in {PICKED_UP, IN_TRANSIT, ARRIVED, DELIVERED, FAILED}`.
- `mark_delivery_arrived` ([delivery_service.py:326](../business_app/services/delivery_service.py#L326)) — call the guard after the `must be in transit` check, before mutating `delivery.status` (line 349).
- `begin_delivery_in_transit` ([delivery_service.py:235](../business_app/services/delivery_service.py#L235)) — call the guard after the `must be assigned` check, before mutating `delivery.status` (line 257).
- `complete_delivery` ([delivery_service.py:584](../business_app/services/delivery_service.py#L584)) — already delegates to `update_delivery_status`, so it inherits the guard for free.

### 4.7 Admin UI force-complete paths (if any)

Audit any admin-panel endpoint that can set `delivery.status = delivered` or `order.status = delivered` directly. If found, gate with the same guard, with an explicit `bypass_session_check: bool` parameter that requires an admin role and writes the reason to `delivery_status_history.notes`.

---

## 5. Data backfill for order 324

Read-only investigation completed; no writes performed. Once the fix is deployed, run a one-off reconciliation script (NOT a migration — this is data, not schema):

```sql
-- 1) Bind order 324 retroactively to session 28 (the session that was open at accept time).
INSERT INTO driver_bottle_session_orders
    (session_id, order_id, accepted_by_driver_id, added_at, created_at, updated_at)
VALUES (28, 324, 116, '2026-05-14 15:11:41+00', NOW(), NOW())
ON CONFLICT (order_id) DO NOTHING;

-- 2) Credit session 28's tally for the bottles it actually delivered.
UPDATE driver_bottle_sessions
SET bottles_delivered = bottles_delivered + 5,
    bottles_collected_from_customers = bottles_collected_from_customers + 5,
    updated_at = NOW()
WHERE id = 28;
```

Before running, verify the assumption that the empties were collected (the `bottle_ledger` row 205 is `return_on_delivery -5`, so yes). The 5+5 adjustment moves session 28 from `30 loaded / 18 delivered / 12 collected` to `30 loaded / 23 delivered / 17 collected`, which still balances against `35 returned_to_warehouse + remaining` arithmetic — confirm with finance before running.

**Run the script in a transaction with explicit COMMIT/ROLLBACK and have a second engineer review the row counts before commit.** This is a one-shot prod data write.

---

## 6. Tests

### 6.1 Unit tests

`tests/services/test_bottle_tracking_service.py`:
- `assert_driver_can_progress_delivery` — happy path returns bound session.
- … raises `BOTTLE_SESSION_REQUIRED` when no binding exists.
- … raises `BOTTLE_SESSION_CLOSED` when bound session is closed.
- … raises `BOTTLE_SESSION_MISMATCH` when driver's effective session differs from bound session.
- … returns None and does not raise for orders with no returnable bottles.
- Session close — refuses normal close when undelivered bindings exist; force-close still works.

### 6.2 Integration tests

`tests/integration/test_delivery_flow.py`:
- **Regression test for AD_000205_26**: accept with open session → close session → attempt `picked_up` → expect `BOTTLE_SESSION_CLOSED`. This test alone would have caught the bug.
- Accept with open session → progress through all transitions in same session → DELIVERED → assert `driver_bottle_session_orders` row exists, session tally incremented by exactly N.
- Co-driver scenario: accept under owner session → owner closes session → member tries `picked_up` → expect rejection.
- Order with zero returnable bottles (e.g. snacks only from grocery store flow) → all transitions succeed without any session.

### 6.3 Property/edge tests
- `accept_order` retried with same `(driver, order)` after the binding row exists — idempotent, no duplicate row, no exception.
- Two concurrent `accept_order` calls for the same delivery — only one binding row created.

---

## 7. Observability

1. After deploy, the "skip tally" branch should be unreachable. Add a Sentry alert (or equivalent) on `error_code in {BOTTLE_SESSION_REQUIRED, BOTTLE_SESSION_MISMATCH, BOTTLE_SESSION_CLOSED}` raised from the post-accept guard — any occurrence is a real driver/operator issue worth on-call attention initially, then we can downgrade once stable.
2. Add a daily reconciliation query (cron or Celery beat) that flags drift between `driver_bottle_sessions.bottles_delivered` and `SUM(bottles_in_order)` of bottle-bearing orders bound to that session and in DELIVERED status. Anomalies → Slack notification.
3. Existing `[BOTTLE]` info logs stay — they were useful during this investigation.

---

## 8. Rollout

1. **PR 1** — additive: introduce `assert_driver_can_progress_delivery`, move binding into `accept_order`, leave the silent-skip branch in place but log at WARN level instead of INFO. Deploy. Watch logs for any WARN line for ~24h; that tells us how many in-flight orders are at risk.
2. **PR 2** — enforcement: add the guards in all five call sites (§4.2, §4.3, §4.4, §4.5, §4.6) and turn the silent-skip into a raise. Deploy.
3. **One-shot script** — run §5 backfill for order 324 (and anything else flagged by PR 1's WARN logs).
4. **PR 3** — session close precondition (§4.1 last block) and reconciliation cron (§7.2).

Feature-flag PR 2's enforcement (`BOTTLE_SESSION_ENFORCEMENT_STRICT`, default false) so it can be turned on per environment and rolled back instantly without redeploy if it causes operational pain.

---

## 9. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Drivers in flight when PR 2 deploys lose ability to progress orders that were accepted under an already-closed session | PR 1 measures the population first; we manually backfill bindings for any in-flight orders before flipping the flag. |
| Admin operators legitimately need to force-deliver without a session (rare edge cases) | Explicit `bypass_session_check` flag on admin-only endpoint, audited via `delivery_status_history.notes`. |
| Capacity check at accept doesn't account for already-bound-but-undelivered orders on the same session, so a driver could "overbook" their truck across multiple deliveries | Tighten `assert_delivery_within_session_capacity` to subtract bottles needed by other unfulfilled bound orders. Track as a follow-up. |
| Co-driver leaves the session mid-route, orphaning their accepted-but-undelivered orders | Either require co-driver departure to fail-out their bound orders, or transfer ownership. Out of scope for the immediate fix; ticket as follow-up. |

---

## 10. Open questions

- ~~Should `failed` transitions also require a session?~~ **Decided yes.** Failing a delivery still implies the driver physically had the bottles, so `failed` is included in the guarded transition set (§4.2 / §4.6).
- Is there an operator workflow where an admin re-completes a delivery hours after the driver left for the day? If yes, that path explicitly needs `bypass_session_check`; if no, the current "no admin force-complete" assumption is fine.
- ~~Does cancelling an order release the binding?~~ **Decided: no soft-delete needed.** The close-precondition (§4.1) filters by `Order.status.notin_([DELIVERED, CANCELLED, RETURNED])`, so cancelled bindings are simply ignored by the predicate and remain as historical records.
