# Click PSP Rewrite + Fiscalization

Status legend:
- `pending`
- `in_progress`
- `completed`
- `blocked`

Updated: 2026-03-12

## Overall
- Status: `completed`
- Scope: one-time orders only
- Default card provider target: Click
- Business-account marking-code consumption: manual toggle, default `OFF`
- Marking-code admin tooling: manual + CSV

## Workstream Status

### 1. Documentation Review And Surface Discovery
- Status: `completed`
- Notes:
  - Official Click documentation reviewed for Shop API and Merchant API.
  - Existing backend, admin UI, web checkout, and Telegram payment surfaces mapped.
  - Current broken and stale Click integration points identified.

### 2. Payment And Fiscal Data Model
- Status: `completed`
- Deliverables:
  - Add product fiscal profile model/data.
  - Add product marking code inventory model/data.
  - Add order-item marking code allocation history model/data.
  - Add payment fiscalization aggregate/state model/data.
  - Add payment-level marking-code consumption policy for business/admin flows.
  - Generate and review migration.
 - Notes:
   - Added `product_fiscal_profiles`, `product_marking_codes`, `order_item_marking_code_allocations`, and `payment_fiscalizations`.
   - Added `payments.consume_marking_codes`.
   - Generated, reviewed, and applied Alembic migration `f66d4fcce111`.

### 3. Click Provider Rewrite
- Status: `completed`
- Deliverables:
  - Replace old Click link generation and callback handling.
  - Implement Shop API `Prepare` and `Complete`.
  - Implement Merchant API status, refund/reversal, and fiscalization calls.
  - Preserve Payme as readable non-default fallback.
 - Notes:
 - Replaced stale Click code with dedicated provider services and compatibility wrapper.
  - Click now owns card/default prepaid flows while Payme remains available as fallback.
  - Fixed the missing `get_payment_service` import in the order retry surface discovered during verification.
  - Verified and corrected Click `Complete` handling so callback cancellation/error states no longer promote payments to success, and `Complete` signatures now include `merchant_prepare_id` per the official Shop API contract.
  - Corrected Click callback IP allowlist usage to honor `CLICK_CALLBACK_ALLOWLIST` with backward compatibility for the legacy key.

### 4. Fiscalization Workflow
- Status: `completed`
- Deliverables:
  - Build canonical Click fiscalization payloads.
  - Include per-item `labels` marking codes when required.
  - Reserve/release/use marking codes with correct prepaid/COD/business-account rules.
  - Add async retryable fiscalization execution and admin retry support.
 - Notes:
   - Implemented marking-code reservation, release, usage ledger, business-account manual consumption toggle, and Click fiscalization queue/worker flow.
   - Click payloads now include item-level fiscal fields plus `Labels` for marked goods.

### 5. Admin APIs And Admin UI
- Status: `completed`
- Deliverables:
  - Extend product admin payloads with fiscal fields and counters.
  - Add marking-code CRUD and CSV import/export endpoints.
  - Add admin order payment/fiscalization visibility and business-account toggle.
  - Add admin retry for failed fiscalization.
 - Notes:
 - Products admin page now includes fiscal profile editing, marking-code inventory, manual add/edit/archive, CSV import/export, and low-stock visibility.
  - Orders admin page now shows payment provider/transaction/receipt data, marking-code summary, business-account consumption toggle, and fiscalization retry.
  - Orders admin detail now exposes fiscalization audit trail, Click callback history, payment transactions, and marking-code activity for operator review.
  - Added audit logging for fiscalization retries, marking-code reservation/release/use, and business-account manual consumption.

### 6. Web Checkout And Payment Status Surfaces
- Status: `completed`
- Deliverables:
  - Make Click the default card payment path.
  - Return usable payment links from order/payment creation flows.
  - Fix pending/cancel/retry flow to use real retry API behavior.
 - Notes:
 - Checkout card choice now routes to Click.
  - Order creation and retry surfaces now return real payment URLs and use the actual retry endpoint.
  - Pending payment page now reconciles through the provider-backed payment status endpoint instead of polling stale order detail state.
  - Public payment methods now advertise only supported Click, Payme, and COD options.

### 7. Telegram Flow
- Status: `completed`
- Deliverables:
  - Route one-time card checkout to Click instead of Payme.
  - Update retry and switch-method behavior to stay aligned with backend.
 - Notes:
   - Telegram payment links now route card/checkouts through Click and preserve Payme as explicit fallback.

### 8. Tests And Verification
- Status: `completed`
- Deliverables:
  - Unit coverage for Click callbacks, fiscal payloads, and marking-code rules.
  - API coverage for admin and customer payment surfaces.
  - UI coverage for admin fiscal workflows and Click-first entry points.
 - Notes:
 - Added backend tests for Click callback idempotency, fiscalization payload labels, business-account marking-code consumption, and retry-payment API.
  - Added admin UI tests for fiscalization retry, product marking-code inventory, and admin service integration methods.
  - Applied migration to the development database and verified the current Alembic head.
  - Added remediation coverage for Click `Complete` cancellation behavior, complete-signature verification, callback allowlist enforcement, supported public payment methods, and CSV duplicate/import handling.

## Progress Log
- 2026-03-11: Created tracker file and marked documentation/surface discovery complete.
- 2026-03-11: Completed Click provider rewrite, fiscalization data model, admin product/order fiscal workflows, checkout/bot Click-first routing, targeted backend/UI tests, and migration `f66d4fcce111`.
- 2026-03-12: Closed the six post-implementation audit findings by fixing Click callback cancellation handling and complete signatures, provider-backed pending-page reconciliation, callback allowlist config wiring, public payment method exposure, fiscalization/marking-code audit trails, and CSV duplicate reporting for marking-code imports.
