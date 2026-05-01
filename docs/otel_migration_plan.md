# OpenTelemetry Migration Plan

## Context

BlueStream currently has separate Prometheus exporters (`celery_exporter`, `redis_exporter`) and a fully provisioned Loki instance with persistent storage that **receives zero logs today**. Sentry is wired across all services for error tracking. The goal of this migration is to adopt OpenTelemetry to unlock the "three pillars" — metrics, traces, and logs — with logs landing in the existing Loki and traces enabling end-to-end debugging across `telegram_bot → business_app → celery_worker → DB`.

The migration must:
1. Preserve every alert in [monitoring/alert_rules.yml](../monitoring/alert_rules.yml) — they key off custom metric names and `up{job=...}` labels that must remain scrape-able.
2. Coexist with the existing Sentry SDK 2.35.0 setup (errors-only Sentry; OTel owns tracing).
3. Avoid breaking the sophisticated existing logging stack — `StructuredFormatter`, `SecuritySensitiveFilter` PII redaction, named specialized loggers (security/performance/business/database), X-Request-ID middleware, file rotation under [logs/](../logs/).
4. Wire Loki end-to-end: log shipping in, Grafana datasource in, trace_id click-through to traces.
5. Be phased and individually reversible — no big-bang cutover.

**Side benefit discovered during exploration:** The current Prometheus `postgres` scrape job at [monitoring/prometheus.yml:21-24](../monitoring/prometheus.yml#L21-L24) has been silently broken since day one — it scrapes `postgres:5432` (wire protocol, not HTTP) and triggers the same "Possible SECURITY ATTACK" log spam Redis was emitting. Phase 2 fixes this.

---

## Architecture Decisions

- **Metrics backend stays Prometheus.** Alerts depend on `prometheus_client`-emitted metric names; replacing the emit path is multi-week work for no operator-visible benefit.
- **Traces backend: Grafana Tempo** (single binary, persistent volume, native Grafana integration with TraceQL → LogQL/PromQL correlation).
- **Logs backend: existing Loki** with native OTLP endpoint at `/otlp/v1/logs` (Loki 3.x, `allow_structured_metadata: true` already enabled at [monitoring/loki-config.yml:49](../monitoring/loki-config.yml#L49)).
- **Collector topology: single gateway Collector** (`otel/opentelemetry-collector-contrib`). Agent-per-container is unjustified for a single-host docker-compose deployment.
- **Keep `celery_exporter`.** OTel's CeleryInstrumentor emits per-task spans; it does NOT emit the queue-depth / sent / failed / runtime histogram series that `CeleryQueueBackup`, `CeleryTaskFailureRateHigh`, `CeleryTaskStuck` alerts depend on. Different telemetry, no overlap.
- **Drop the `loki` exporter; use `otlphttp` to Loki.** The standalone `loki` exporter is in deprecation; native OTLP preserves trace_id/span_id as queryable Loki structured metadata.
- **Sentry: errors-only.** Set `traces_sample_rate=0.0` and `profiles_sample_rate=0.0`. No `sentry-sdk` version bump needed.

---

## Phase 1 — Infra foundation (no app code changes)

**Goal:** Collector + Tempo running; Grafana sees Loki and Tempo; container stdout flowing into Loki via `filelog`. Existing services and metrics untouched.

**Files to create:**
- [monitoring/otel-collector-config.yml](../monitoring/otel-collector-config.yml) — receivers (`otlp`, `filelog`), processors (`memory_limiter`, `batch`, `resource`, `attributes/scrub`), exporters (`otlphttp/loki`, `otlp/tempo`, `prometheus`, `debug`), extensions (`health_check`).
- [monitoring/tempo-config.yml](../monitoring/tempo-config.yml) — local filesystem storage at `/var/tempo`, OTLP receivers on 4317/4318, **block_retention 720h to match Loki's 30-day window** (avoids log→trace 404s).
- [monitoring/grafana/datasources/loki.yml](../monitoring/grafana/datasources/loki.yml) — Loki datasource with `derivedFields.TraceID` linking to Tempo.
- [monitoring/grafana/datasources/tempo.yml](../monitoring/grafana/datasources/tempo.yml) — Tempo datasource with `tracesToLogsV2` linking to Loki and `serviceMap` linking to Prometheus.

**Files to modify:**
- [docker-compose.production.yml](../docker-compose.production.yml) — add `otel_collector` (image `otel/opentelemetry-collector-contrib:0.110.0`, mount `/var/lib/docker/containers:/var/lib/docker/containers:ro`, expose 4317/4318/8889/13133, 512M limit) and `tempo` (image `grafana/tempo:2.6.1`, `tempo_data` named volume, port `127.0.0.1:3200:3200`, 512M limit). Add `tempo_data:` to volumes block.

**Verification:**
```bash
# Collector ready
docker compose exec otel_collector wget -qO- http://localhost:13133

# Tempo ready
curl -s http://127.0.0.1:3200/ready

# Logs flowing — in Grafana → Explore → Loki:
{container_id=~".+"} | json
```

**Rollback:** Comment out `otel_collector` and `tempo` services, delete the new datasource files, `docker compose up -d`. Zero app impact.

---

## Phase 2 — Replace infra exporters with Collector receivers

**Goal:** Collector scrapes Postgres + Redis natively (fixes the broken `postgres` scrape); Prometheus scrapes Collector's `:8889` for those metrics. `redis_exporter` removed.

**Files to modify:**
- [monitoring/otel-collector-config.yml](../monitoring/otel-collector-config.yml) — add `redis` receiver (`endpoint: redis:6379`, password from env) and `postgresql` receiver (`endpoint: postgres:5432`, user/password/db from env). Add to metrics pipeline. Pass `REDIS_PASSWORD`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` into `otel_collector.environment`.
- [monitoring/prometheus.yml](../monitoring/prometheus.yml) — replace `redis` and `postgres` jobs to scrape `otel_collector:8889` with `metric_relabel_configs` keeping `redis_*` and `postgresql_*` metrics under their original job labels (preserves `up{job="redis"|"postgres"}` for alerts).
- [docker-compose.yml](../docker-compose.yml) — remove the `redis_exporter` service.

**Verification:**
```bash
# Both jobs UP — Prometheus UI → Status → Targets
# OTel-emitted metrics
curl -s http://otel_collector:8889/metrics | grep -E '^(redis_|postgresql_)' | head
# Existing alerts inactive — Prometheus UI → Alerts → DatabaseConnectionIssues / RedisConnectionIssues
```

**Rollback:** Restore `redis_exporter`, revert `prometheus.yml` and `otel-collector-config.yml`.

---

## Phase 3 — Trace + log instrumentation in business_app + Celery

**Goal:** Flask + Celery emit OTel spans; SQLAlchemy / Redis / psycopg2 / aiohttp-client / requests auto-instrumented. Logs gain `trace_id`/`span_id` and ship via OTel logging bridge → Collector → Loki. Sentry switches to errors-only.

**Files to modify:**
- [requirements.txt](../requirements.txt) — add the OTel block (versions in **Dependencies** section below).
- [business_app/config/production.py:331-332](../business_app/config/production.py#L331-L332) — change `traces_sample_rate=0.05` and `profiles_sample_rate=0.01` to `0.0`. Comment that OTel owns tracing.
- [scripts/gunicorn.conf.py](../scripts/gunicorn.conf.py) — extend the existing `post_fork` hook (don't move OTel init to import time — `BatchSpanProcessor` spawns a background thread that's fork-unsafe in the master). After the existing DB pool dispose block, call `init_otel("business_app")` then run `FlaskInstrumentor().instrument_app(app, request_hook=...)`, `SQLAlchemyInstrumentor().instrument(engine=db.engine)`, `Psycopg2Instrumentor`, `RedisInstrumentor`, `RequestsInstrumentor`, `AioHttpClientInstrumentor`. The Flask `request_hook` should bridge `g.request_id` (already populated by [business_app/__init__.py:168](../business_app/__init__.py#L168)) onto the span as `bluestream.request_id`.
- [business_app/tasks/celery_app.py](../business_app/tasks/celery_app.py) — add a `@worker_process_init.connect` handler (sibling to the existing `@before_task_publish` / `@task_prerun` block at lines 214-235) that calls `init_otel("celery_worker")` and instruments `Celery`, `SQLAlchemy`, `Psycopg2`, `Redis`, `Requests`. Use `worker_process_init` (not `worker_init`) to avoid double-init in the prefork master.
- [docker-compose.production.yml](../docker-compose.production.yml) — add `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel_collector:4317` and `APP_VERSION=${APP_VERSION:-unknown}` env to `business_app` and `celery_worker`.

**Files to create:**
- [business_app/utils/otel.py](../business_app/utils/otel.py) — `init_otel(service_name)` function: idempotent (`_INITIALIZED` guard); creates `TracerProvider` + `LoggerProvider` with OTLP gRPC exporters; attaches `LoggingHandler` to root logger with the existing `SecuritySensitiveFilter` from [business_app/utils/logging_config.py](../business_app/utils/logging_config.py) re-applied (logging filters live on handlers, not records — must be re-attached); calls `LoggingInstrumentor().instrument(set_logging_format=False)` to inject `trace_id`/`span_id` into LogRecord extras so the existing `StructuredFormatter` emits them in stdout JSON too.

**Reuse existing utilities:**
- `SecuritySensitiveFilter` from [business_app/utils/logging_config.py](../business_app/utils/logging_config.py) — re-attach to OTel `LoggingHandler`.
- `setup_enhanced_logging(app)` at [business_app/__init__.py:553](../business_app/__init__.py#L553) — leave UNCHANGED. OTel `LoggingHandler` attaches in parallel; existing file/console handlers untouched.
- `g.request_id` middleware at [business_app/__init__.py:168](../business_app/__init__.py#L168) — bridge to span attribute via Flask `request_hook`.
- Existing celery signals at [business_app/tasks/celery_app.py:214-235](../business_app/tasks/celery_app.py#L214-L235) — keep as-is; OTel propagates W3C `traceparent` independently.

**Verification:**
```bash
# Spans in Tempo
curl -s http://127.0.0.1/api/v1/health
# Grafana → Explore → Tempo → service.name="business_app" → expand: GET /api/v1/health with SQLAlchemy/Redis children

# Logs gained trace_id
# Grafana → Explore → Loki → {service_name="business_app"} | json | trace_id != ""
# Click any line with trace_id → "View trace" jumps to Tempo

# Celery span chain
# Trigger an order → Tempo trace tree shows: POST /orders (business_app) → publish → celery_worker

# Sentry transactions empty (errors only)
```

**Rollback:** Revert `gunicorn.conf.py`, `celery_app.py`, `production.py` Sentry sample rates, env additions. `business_app/utils/otel.py` becomes dead code; safe to leave.

---

## Phase 4 — Trace + log instrumentation in telegram_bot + staff_bot

**Goal:** Bots emit OTel spans for incoming Telegram updates, asyncpg queries, httpx calls, aiohttp webhook server. W3C `traceparent` flows through `httpx` to `business_app`, completing the bot → API → Celery → DB chain.

**Files to modify:**
- [requirements.txt](../requirements.txt) — add `opentelemetry-instrumentation-httpx`, `-asyncpg`, `-aiohttp-server` (same `0.48b0` train).
- [telegram_bot/bot.py:14-35](../telegram_bot/bot.py#L14-L35) — call `init_bot_otel("telegram_bot")` immediately after Sentry init and **before** `from telegram import ...` (line 37) so HTTPX is patched before python-telegram-bot grabs its references. Set Sentry `traces_sample_rate=0.0` and `profiles_sample_rate=0.0` here too.
- [staff_bot/bot.py](../staff_bot/bot.py) — identical changes.
- [docker-compose.yml](../docker-compose.yml) — add `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel_collector:4317` and `APP_VERSION=${APP_VERSION:-unknown}` env to `telegram_bot` and `staff_bot`.

**Files to create:**
- [telegram_bot/otel_init.py](../telegram_bot/otel_init.py) — async-friendly bot variant of `init_otel`. Same `_INITIALIZED` guard. Instruments `HTTPXClient`, `AsyncPG`, `AioHttpServer`. Crucially: also attaches the OTel `LoggingHandler` to each named bot logger (`bot`, `handlers`, `api_client`, `utils`, `database`, `config`, `webhook_server`) because [telegram_bot/logging_config.py](../telegram_bot/logging_config.py) sets `propagate: False` on these loggers — root-only attachment would miss them.
- [staff_bot/otel_init.py](../staff_bot/otel_init.py) — identical.

**Reuse existing utilities:**
- Bot `setup_logging()` at [telegram_bot/logging_config.py:9-127](../telegram_bot/logging_config.py#L9-L127) — UNCHANGED. OTel handler attaches in parallel.
- HTTPX client in [telegram_bot/api_client.py](../telegram_bot/api_client.py) — no code change; `HTTPXClientInstrumentor` injects `traceparent` automatically.

**Verification:**
```bash
# End-to-end trace
# Send a Telegram message that triggers an order
# Tempo: telegram_bot span → HTTP POST /api/v1/orders → business_app → celery_worker → DB

# Bot logs have trace_id
# {service_name="telegram_bot"} | json | trace_id != ""

# Sentry still gets errors but no transactions (force a bot exception to verify)
```

**Rollback:** Revert bot.py changes; remove new `otel_init.py` files.

---

## Phase 5 — Migrate Python services from filelog → OTLP push only

**Goal:** Stop double-shipping logs from instrumented Python services (filelog + OTLP). Keep filelog for non-Python containers (`nginx`, `postgres`, `redis`, `flower`, `celery_exporter`).

**Files to modify:**
- [docker-compose.yml](../docker-compose.yml) — add `tag: "{{.Name}}"` to the `logging.options` of `business_app`, `celery_worker`, `celery_beat`, `telegram_bot`, `staff_bot`. This puts the container name into the json-file metadata so the Collector can filter on it.
- [monitoring/otel-collector-config.yml](../monitoring/otel-collector-config.yml) — add an early `filter` operator in the `filelog` receiver pipeline that drops records whose `attributes.attrs.tag` matches the Python service container names.

**Verification:**
```bash
# Pick a unique log line ("Worker spawned") — should appear ONCE in Loki for business_app
# {service_name="business_app"} | json |~ "Worker spawned" | head 10

# Non-Python containers still flowing via filelog
# {container_id=~".+", service_name=""} | json — postgres/nginx/redis lines visible
```

**Rollback:** Remove the `filter` operator. Brief dual-shipping during reconciliation; harmless.

---

## Dependencies (additions to [requirements.txt](../requirements.txt))

```
# OpenTelemetry — coordinated 1.27.0 / 0.48b0 train
opentelemetry-api==1.27.0
opentelemetry-sdk==1.27.0
opentelemetry-exporter-otlp-proto-grpc==1.27.0
opentelemetry-exporter-otlp-proto-http==1.27.0
opentelemetry-instrumentation==0.48b0
opentelemetry-instrumentation-flask==0.48b0
opentelemetry-instrumentation-sqlalchemy==0.48b0
opentelemetry-instrumentation-psycopg2==0.48b0
opentelemetry-instrumentation-redis==0.48b0
opentelemetry-instrumentation-celery==0.48b0
opentelemetry-instrumentation-requests==0.48b0
opentelemetry-instrumentation-aiohttp-client==0.48b0
opentelemetry-instrumentation-aiohttp-server==0.48b0
opentelemetry-instrumentation-httpx==0.48b0
opentelemetry-instrumentation-asyncpg==0.48b0
opentelemetry-instrumentation-logging==0.48b0
```

`sentry-sdk==2.35.0` stays — supports errors-only mode.

---

## Risks and gotchas (codebase-specific)

1. **gunicorn `preload_app=True` + OTel init order.** `BatchSpanProcessor` spawns a background flusher thread; if init runs at import time, the lock is inherited but the thread isn't, so workers deadlock on first export. Init MUST happen in `post_fork`. Same constraint as the existing `prometheus_client` multiproc setup.
2. **Celery prefork double-init.** Use `worker_process_init` only — `worker_init` fires in the master and would inherit-then-deadlock identically.
3. **`SecuritySensitiveFilter` is per-handler, not per-record.** The OTel `LoggingHandler` is a separate handler — must explicitly `addFilter(SecuritySensitiveFilter())` or unredacted secrets reach Loki. Collector's `attributes/scrub` is second-line, not primary.
4. **Bot named loggers have `propagate: False`** — root-only attachment misses them. Phase 4 explicitly attaches the OTel handler to each named logger.
5. **Tempo retention vs Loki retention.** Set Tempo `block_retention: 720h` (30d) to match Loki, so trace_id click-through always resolves. Costs more disk but eliminates log→trace 404s during incident debugging.
6. **`traces_sample_rate=0.0` mode for Sentry** — keeps error capture (FlaskIntegration / CeleryIntegration / SqlalchemyIntegration / RedisIntegration / AsyncioIntegration) without creating duplicate transactions vs OTel.
7. **`migrate` one-shot service** — does NOT need OTel init. Don't touch its env.
8. **Loki structured metadata** — `allow_structured_metadata: true` already at [monitoring/loki-config.yml:49](../monitoring/loki-config.yml#L49). Without this, Loki silently drops trace_id from OTLP payloads.
9. **Grafana datasource race on first deploy** — datasources are read at Grafana startup; after a Phase 1 `up -d`, `docker compose restart grafana` to ensure provisioning sees Tempo/Loki.
10. **Async context propagation in bots** — OTel uses `contextvars` (asyncio-safe). One quirk: `loop.run_in_executor(...)` doesn't propagate context. Verified-empty today via grep, but watch for future regressions.

---

## Critical files for implementation

- [scripts/gunicorn.conf.py](../scripts/gunicorn.conf.py) — extend `post_fork` (Phase 3)
- [business_app/tasks/celery_app.py](../business_app/tasks/celery_app.py) — add `worker_process_init` signal (Phase 3)
- [business_app/utils/otel.py](../business_app/utils/otel.py) — NEW, shared init for backend + celery (Phase 3)
- [business_app/utils/logging_config.py](../business_app/utils/logging_config.py) — `SecuritySensitiveFilter` reused, file unchanged (Phase 3)
- [business_app/config/production.py](../business_app/config/production.py) — Sentry sample rates → 0 (Phase 3)
- [telegram_bot/otel_init.py](../telegram_bot/otel_init.py), [staff_bot/otel_init.py](../staff_bot/otel_init.py) — NEW (Phase 4)
- [telegram_bot/bot.py](../telegram_bot/bot.py), [staff_bot/bot.py](../staff_bot/bot.py) — early init call (Phase 4)
- [monitoring/otel-collector-config.yml](../monitoring/otel-collector-config.yml) — NEW (Phase 1, extended through Phases 2 & 5)
- [monitoring/tempo-config.yml](../monitoring/tempo-config.yml) — NEW (Phase 1)
- [monitoring/grafana/datasources/loki.yml](../monitoring/grafana/datasources/loki.yml), [monitoring/grafana/datasources/tempo.yml](../monitoring/grafana/datasources/tempo.yml) — NEW (Phase 1)
- [monitoring/prometheus.yml](../monitoring/prometheus.yml) — `redis` + `postgres` jobs retargeted (Phase 2)
- [docker-compose.production.yml](../docker-compose.production.yml) — `otel_collector`, `tempo` services + env additions (Phases 1, 3)
- [docker-compose.yml](../docker-compose.yml) — remove `redis_exporter` (Phase 2), bot env (Phase 4), logging tags (Phase 5)
- [requirements.txt](../requirements.txt) — OTel deps (Phases 3 & 4)

---

## End-to-end verification (after Phase 5)

1. **Trigger a real order via Telegram bot.**
2. **Open Grafana → Explore → Tempo**, search `service.name="telegram_bot"` for the last minute, expand the trace.
3. **Confirm the span tree:** `telegram_bot.handle_message` → `HTTP POST /api/v1/orders` (httpx) → `business_app POST /api/v1/orders` (Flask) → `SQLAlchemy INSERT` → `celery.publish process_order` → `celery_worker process_order` → `INSERT/UPDATE` spans → response.
4. **Click any span → "Logs for this span"** — should jump to Loki and show that service's log lines for the trace, with `trace_id` highlighted.
5. **Open Grafana → Explore → Loki**, query `{service_name=~"business_app|telegram_bot|celery_worker"} | json | trace_id != ""` — every line shows a `trace_id`.
6. **Force a 500 error** (e.g., bad payload to a backend route) — Sentry issue appears with stack trace + breadcrumbs; Sentry transactions tab stays empty.
7. **Prometheus alerts unchanged:** `Status → Targets` shows `postgres`, `redis`, `business_app`, `celery`, `alertmanager` all UP. `Alerts` tab shows nothing newly firing.

If all seven pass: migration complete.
