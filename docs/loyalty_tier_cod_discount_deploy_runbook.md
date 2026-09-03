# Deploy runbook: loyalty tier discount on cash-on-delivery orders

What's shipping: loyalty tiers now carry a real discount, granted only when an order is paid
cash-on-delivery (cash at the door, or a card transfer to the driver at the door). It is
computed by one server-side formula, revoked if the order later moves to Click/Payme/card, and
stated with its condition on every surface that publishes it. The COD debt cap also gained a
second arm (a debt-amount floor, not just a debt count) so the discount can't push a customer's
balance into a shortfall that then blocks them from using COD at all.

Read this top to bottom before running anything — steps 4 and 6 have real "ship English to
customers" / "ship a stale promise" failure modes if skipped or done partially.

**No tier percentage appears anywhere below.** Production's rates are the owner's to set (step 9)
and differ from dev's.

## 0. Prerequisites

- This plan's staged changes are committed and the code is live on the deploy host through your
  normal release process (git pull / equivalent). `business_app`, `telegram_bot` and
  `celery_worker` read their source at container start, so a plain restart (no image rebuild) is
  enough to pick up the change — this codebase's established pattern for a pure-Python change
  with no new dependency. `admin_ui` is a compiled React bundle and needs an actual rebuild
  (step 8).
- Migration `a1b7c3d9e5f2` (`down_revision: b6d1e8f4a207`) has already run clean on dev; this
  runbook applies it to production for the first time.
- Run the full gate first and do not proceed on red:
  ```bash
  bash scripts/precommit-backend-tests.sh
  bash -c 'cd admin_ui && npm test --silent'
  ```
  No new routes were added by this plan, so `tests/contract/test_openapi_snapshot.py` needs no
  `UPDATE_API_SNAPSHOT=1` regeneration here.

## 1. Apply the migration

The `-e` flag is mandatory — an inline `FLASK_APP=...` right after `exec` exits 127, it does not
set the variable:

```bash
docker compose exec -e FLASK_APP=business_app business_app flask db upgrade
docker compose exec -e FLASK_APP=business_app business_app flask db current
```

Expect `a1b7c3d9e5f2 (head)`.

Confirm the column and its CHECK landed on the real engine (SQLite, which the test suite runs
against, doesn't enforce the CHECK the way Postgres does):

```bash
docker compose exec postgres psql -U postgres -d bluestream_db -c "\d orders" | grep -i tier_discount
```

Expect a `tier_discount | numeric(10,2) | not null default 0.00` column and
`ck_orders_tier_discount_nonneg` in the Check constraints block. Every pre-existing order lands
on the `0.00` default — correct, since those orders were priced before this feature existed.

## 2. Seed translations — TWO scripts, not one

This is the step most likely to ship raw keys or English to Uzbek/Russian customers if done
half-way, because the copy this plan added is split across two seeder files that use two
different i18n mechanisms. Both were confirmed by reading the staged diff directly (every `| t`,
`i18n.get(...)` and `get_translation(...)` call site this plan added or touched), not by trusting
a task list — one of the two was missing from the original task brief.

`scripts/` is **not** mounted into the `business_app` container — pipe both over stdin, never
`docker cp` (that duplicates the tree):

```bash
docker compose exec -T business_app python - < scripts/seed_backend_translations.py
docker compose exec -T business_app python - < scripts/seed_prepayment_translations.py
```

**`seed_backend_translations.py`** — one run, four namespaces, because they all live in the same
`BACKEND_TRANSLATIONS` dict:
- `api.loyalty.tier_discount_condition` (category `api`) — the sentence `GET /api/v1/loyalty/tiers`
  publishes and `/my-loyalty` renders next to the rate.
- `loyalty_guide.tier.label_discount`, `loyalty_guide.tier.perk_discount` (updated copy) plus a new
  `loyalty_guide.cod.*` block (category `loyalty_guide`) — the `/loyalty-guide` COD-discount
  section and its worked example.
- `ui.orders.money_breakdown`, `ui.orders.subtotal`, `ui.orders.subscription_discount`,
  `ui.orders.loyalty_discount`, `ui.orders.tier_discount`, `ui.orders.delivery_fee`
  (category `ui`) — the admin Orders-detail money breakdown.
- Web checkout summary — `'Discount'`, `'Reward discount'`, `'{tier} discount ({percent}%)'`,
  `'Pay cash on delivery and save {amount}'`. These are **dotless, literal-English keys**: the
  Jinja `| t` filter looks them up by the exact English string, so the key IS the English value.
  `checkout.html`'s `render_page_data` island (`discount`, `reward_discount`,
  `tier_discount_line`, `cod_savings`) already reads them — that template change is staged
  alongside this seed data, nothing further to wire up.
- `telegram.orders.estimate_discount_line`, `estimate_reward_line`, `estimate_tier_line`,
  `estimate_payable`, `estimate_cod_savings` (category `telegram`) — the bot's checkout quote
  block.

**`seed_prepayment_translations.py`** — **not in the original deploy checklist; found only by
tracing every `i18n.get(...)` call the diff added.** One new key:
`telegram.orders.cod_restricted_person` (category `telegram`) — the actionable "your balance is
{amount} UZS, over the {threshold} UZS limit" notice the bot now shows a customer who personally
trips the COD debt cap's new amount arm. Its own implementation report flags this seeding step as
outstanding, and the code degrades safely if you skip it — the bot falls back to the older,
count-only `cod_restricted_has_debts` copy rather than a broken key — but the discount-plus-cap
launch is incomplete without it, so seed it in the same pass.

Nothing added here is an `@translatable` model column, so the `DEFAULT_LANGUAGE=uz` trap (EN
canonical column, no `en` row ⇒ English surfaces render Uzbek) does not apply. Every key above
carries an explicit `en`, `uz` and `ru` value.

Expect both scripts to tail with their own `✓ ... SEEDING COMPLETED` and a non-zero "New
translations added" count.

Verify a sample actually committed, in all three languages:

```bash
docker compose exec postgres psql -U postgres -d bluestream_db -c \
  "SELECT key, language, category FROM translations WHERE key IN
   ('api.loyalty.tier_discount_condition','loyalty_guide.cod.title','ui.orders.tier_discount',
    'telegram.orders.cod_restricted_person') ORDER BY key, language;"
```

Expect 12 rows — four keys × `en`/`ru`/`uz` — with categories `api`, `loyalty_guide`, `ui` and
`telegram` respectively.

**That query proves presence — it does not prove these two rows actually changed.**
`loyalty_guide.tier.label_discount` and `loyalty_guide.tier.perk_discount` are not new keys, they
are **updates**: before this plan, they read as an unconditional tier discount, with no mention of
cash-on-delivery. The seeder upserts, so it will update them — but its own tail counts them under
"Existing translations updated", never under "New translations added". A deployer who only watches
the "New translations added" number climb (satisfied by the genuinely new `loyalty_guide.cod.*`
keys alone) can see a healthy-looking count and still be looking at the old unconditional promise,
because an update that silently no-ops leaves no dent in either counter you'd notice. Check the
values themselves, in all three languages:

```bash
docker compose exec postgres psql -U postgres -d bluestream_db -c \
  "SELECT key, language, value FROM translations WHERE key IN
   ('loyalty_guide.tier.label_discount','loyalty_guide.tier.perk_discount')
   ORDER BY key, language;"
```

Expect six rows reading the new, conditional copy — not the old unconditional one:
- `label_discount` → en `cash-order discount`, uz `naqd buyurtma chegirmasi`,
  ru `скидка за оплату наличными`.
- `perk_discount` → en `{pct}% off when you pay cash on delivery`,
  uz `Yetkazib berishda naqd to'lasangiz {pct}% chegirma`,
  ru `Скидка {pct}% при оплате наличными при доставке`.

If any row instead reads as a bare "discount"/"chegirma"/"скидка" with no mention of cash or
delivery, that update did not commit — `/loyalty-guide` is still making the pre-deploy promise for
that language, and step 6's `#lg-cod-discount` check below will not catch it (it checks that the
new section exists, not that the old copy nearby was corrected). Do not move on until all six read
as above.

## 3. Flush caches — two of them, and they are not the same thing

```bash
docker compose exec -T business_app python - <<'PY'
from business_app import create_app
from business_app.utils.translations import translation_service
import redis

app = create_app()
with app.app_context():
    translation_service.clear_cache()                       # translations:*
    client = redis.from_url(app.config["REDIS_URL"])
    keys = client.keys("response:*:/api/v1/loyalty/tiers*")  # cache_response(3600)
    if keys:
        client.delete(*keys)
    print("cleared translation cache;", len(keys), "cached /loyalty/tiers responses dropped")
PY
```

Why both, and why this is not optional:

- **Translation cache** (`translations:*`) — ordinary reseed hygiene, covers everything in step 2.
- **`GET /api/v1/loyalty/tiers` response cache** — this endpoint is wrapped in
  `@cache_response(3600)`, a server-side Redis cache keyed on
  `response:{language}:{path}:{query}:{hash}`, with no code-version component. A deploy does not
  invalidate it on its own. If the endpoint was hit any time in the hour before this deploy, the
  stale cached payload — missing `tier_discount_condition` — keeps being served for up to 3600
  more seconds, and `/my-loyalty` renders the tier percentage with the COD condition **silently
  omitted**. That is the exact false promise this plan exists to remove, live again for up to an
  hour after go-live if this step is skipped.

Use the targeted delete above, not `FLUSHDB` — that would also drop live sessions, rate-limit
counters and every other piece of Redis-backed state.

`/api/public/loyalty.json` needs no server action: it only carries a 15-minute HTTP
`Cache-Control`, so it self-heals. Expect up to 15 minutes of stale copy there at CDNs/assistant
caches — don't page anyone over it.

A third cache is worth naming too, though it needs no action here: `checkout.js` lives under
`/static/`, a bind mount, so the new file is live the moment the code lands — nothing to flush.
But `nginx/conf.d/caching.conf` gives `application/javascript` a 1-hour `expires` with
`must-revalidate`, so a browser that already cached the pre-deploy `checkout.js` may keep running
it against the new `checkout.html` for up to an hour. That old script sums the basket itself and
has never heard of the tier discount, so the failure direction is a displayed total *higher* than
what the customer is actually charged — the server-computed order still applies the discount
correctly at checkout — never a silent undercharge. It self-heals within the hour; don't page
anyone over it.

## 4. Restart the services that hold state at process start

```bash
docker compose restart business_app telegram_bot celery_worker
```

- `business_app` — picks up the code and starts serving the new column, quote surface and copy.
- `telegram_bot` — loads its translation catalogue into memory at startup; without a restart it
  keeps serving the pre-deploy copy even though the DB rows are now updated.
- `celery_worker` — renders notification copy (e.g. the COD-cap breach notice) from the same
  in-process catalogue.

`staff_bot` needs **no** restart for this feature: the driver surface takes no new copy and no new
arithmetic — `expected_cash_to_collect` already follows the discounted total through
`net_open_receivable_amount`.

## 5. Rebuild the admin UI

```bash
docker compose build admin_ui && docker compose up -d admin_ui
```

Required because `admin_ui/src/pages/Orders.js` gained the new money-breakdown UI — a code change,
not just new copy, so a translation reseed alone will not ship it. A plain restart won't either:
this is a compiled bundle.

## 6. Verify all four public surfaces agree, live

```bash
curl -s http://localhost:5000/api/v1/loyalty/tiers | python3 -m json.tool | grep -A1 tier_discount_condition
curl -s http://localhost:5000/api/public/loyalty.json | python3 -m json.tool | grep -A3 tierDiscountCondition
curl -s "http://localhost:5000/loyalty-guide?lang=uz" | grep -c 'id="lg-cod-discount"'
curl -s "http://localhost:5000/loyalty-guide?lang=ru" | grep -o 'loyalty_guide\.[a-z_.]*' | sort -u
```

Expect: the first two print the condition sentence; the third prints `1`; the fourth prints
**nothing** — any `loyalty_guide.*` token surviving into the rendered Russian HTML means step 2
did not actually commit for that key, and a customer is looking at a raw key right now.

Then confirm the discount behaves, not just that it's described:

- **A COD order carries a non-zero `tier_discount`.** Place (or find) a real order on the `cash`
  rail for a customer who qualifies for a tier with a rate above zero, then:
  ```bash
  docker compose exec postgres psql -U postgres -d bluestream_db -c \
    "SELECT id, payment_method, subtotal, tier_discount, total_amount FROM orders
     ORDER BY id DESC LIMIT 20;"
  ```
  (`orders` has no timestamp column — `id DESC` is the recency proxy used elsewhere in this
  codebase.) The COD row's `tier_discount` should be non-zero and `total_amount` should reflect it.
- **A Click/Payme/card order carries zero.** Same query, same customer's tier, a non-COD order —
  `tier_discount` must be `0.00`.
- **The debt cap's amount arm behaves.** The cap now refuses COD only when a customer's linked
  cluster BOTH has at least `COD_ACTIVE_DEBT_LIMIT` open delivered COD debts AND their net open
  debt exceeds `COD_DEBT_AMOUNT_THRESHOLD` (currently 10,000 UZS by default, overridable by env
  var — check `shared/business_config.py` if you've customized it). Automated coverage for the
  exact crossing behaviour lives in `tests/integration/test_cod_amount_arm_http.py`; to sanity
  check live, call `GET /api/v1/payments/methods` (or place a cash order) as a customer sitting
  right at the count limit but under the amount floor — `payment_restrictions.cod_restricted`
  should be `false` — versus a customer over both — should be `true`.
- **The admin payment-method editor revokes the discount on a rail change.** Take a COD order
  with a non-zero `tier_discount` (from the check above) and, through the admin UI, reclassify its
  payment method `cash → business_account`, then `business_account → click`. After both edits:
  ```bash
  docker compose exec postgres psql -U postgres -d bluestream_db -c \
    "SELECT id, payment_method, tier_discount, total_amount FROM orders WHERE id = <order_id>;"
  ```
  Expect `tier_discount = 0.00` and `total_amount` back to the undiscounted figure —
  `apply_tier_discount_for_rail` runs on both transitions, and neither `business_account` nor
  `click` is a COD rail.

Two more channels publish prices but are **out of scope for this deploy** — only touch them if the
owner has actually decided to:
- **Operator phone orders currently do not carry the discount at all**, by design —
  `StaffService.price_phone_order` publishes `tier_discount: 0.00` unconditionally, pending a
  follow-up task that was scoped and never built. This is a known gap, not something this deploy
  broke. If the owner wants phone orders discounted too, that's separate follow-up work, not a step
  here; if not, no action is needed, but keep announcement copy from implying the discount applies
  "however you order."

## 7. Set production's tier percentages before announcing

Every public surface in this deploy publishes whatever is in `loyalty_tier_configs` live — dev's
values are not production's, and the discount is worthless copy until real rates are set. Check
what's live:

```bash
docker compose exec postgres psql -U postgres -d bluestream_db -c \
  "SELECT display_order, name, min_points, discount_percentage, is_active FROM loyalty_tier_configs
   ORDER BY display_order;"
```

**Set the rates through the admin UI's Loyalty Programs page, not a raw SQL update.** The admin
tier-write endpoints (create/update/delete) already call
`invalidate_cache("response:*:/api/v1/loyalty/tiers*")` after committing, so an admin-UI change
busts the public cache automatically going forward — a direct SQL edit does not, and would need
step 3's cache flush repeated by hand. Do not announce the feature until the owner has confirmed
every `discount_percentage`.

## 8. Rollback

**Recommended first response, and it needs no migration or restart:** set every tier's
`discount_percentage` to `0` via the admin UI. `LoyaltyService.quote_tier_discount` refuses to
grant a discount once a tier's rate is zero (it's the fourth of four gates it checks), so this
takes effect on the next order with no deploy at all, and the admin write path already busts the
cache (step 7). This does **not** touch the schema and is fully reversible by setting the rates
back.

**This does not disarm the COD debt cap's new amount arm — that has its own, separate kill
switch.** The amount arm has no rate to zero, so a bad cap change needs its own lever: the
environment variable `COD_DEBT_AMOUNT_THRESHOLD=0` (read at import in `shared/business_config.py`,
so it needs `business_app`/`telegram_bot`/`celery_worker` restarted to take effect). Checked
against `business_app/utils/cod_cap.py`: the amount arm is `net_debt_total > COD_DEBT_AMOUNT_THRESHOLD`,
so a threshold of `0` makes it true for any scope with a strictly positive net debt — which
reproduces the pre-deploy count-only rule for every customer who actually owes money, with no code
revert. It is not a byte-for-byte restoration, though: `net_open_receivable_amount` floors each
counted debt at `0.00` (never negative), so a scope can reach `COD_ACTIVE_DEBT_LIMIT` in count while
its net total is exactly `0.00` (every counted debt fully offset by reserved prepayment credit) —
the old count-only rule would still have capped that scope, and `COD_DEBT_AMOUNT_THRESHOLD=0` will
not, because `0.00 > 0` is false. No env var closes that residual gap; only reverting the `AND` in
`cod_cap_reached` to a bare count check would. The gap only ever runs in the customer's favour (it
permits COD in a corner case the old rule forbade, never the reverse), so `COD_DEBT_AMOUNT_THRESHOLD=0`
is still the right first move if the amount arm needs to come out — just don't describe it to anyone
as an exact restoration of the old behaviour.

**If you need to remove the column itself:** the migration has a working `downgrade()` — it drops
`ck_orders_tier_discount_nonneg` then the `tier_discount` column.

- **Sequence matters.** Revert the running code to the pre-deploy version (and restart
  `business_app`/`telegram_bot`/`celery_worker`) *before* running `flask db downgrade` — the
  new code reads and writes `order.tier_discount` on every order; downgrading the schema while
  that code is still running will crash order creation immediately, not degrade gracefully.
  ```bash
  # after code is reverted and services restarted on the old version:
  docker compose exec -e FLASK_APP=business_app business_app flask db downgrade
  ```
- **What this does and does not undo.** Downgrading only drops the column — it is a schema
  rollback, not a financial one. Any order created between deploy and rollback that carried a
  non-zero `tier_discount` already had that amount baked into its `total_amount`, and if it was
  COD and already delivered, the driver already collected the discounted figure. Dropping the
  column deletes the audit trail of *why* that total was what it was; it does not, and cannot,
  claw back money already collected at the door. If a bad rate was live and orders were placed
  against it, that is a business decision (credit/debit the affected customers, or just fix the
  rate going forward) — the schema downgrade does not resolve it.
- The translation rows and cache entries added in this deploy are inert if the code is reverted —
  no cleanup needed for those on rollback.
