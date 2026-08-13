# Routing Engine (Phase 2) — Deploy & Rollback Runbook

Scope: self-hosted OSRM primary matrix provider, cache split, Google
next-leg ETA, warehouse anchor. Spec:
`docs/superpowers/specs/2026-08-11-driver-route-optimization-ux-design.md` §8.

This release also carries Plan 1 (staff bot push-notification fixes). The
two are staged together and deploy together — §4 below gives the combined,
order-sensitive restart sequence. If you only care about the OSRM parts,
read §1–§3 and §5–§7 and skip the staff_bot-ordering step in §4.

## 1. Pre-flight on the Pi (BLOCKING)

```bash
ssh <prod-host>
getconf PAGESIZE
```

- **Expected: `4096`.** Raspberry Pi OS on Pi 5 defaults to a 16 KB-page
  kernel, which breaks jemalloc-linked binaries inside Docker. If this
  prints `16384`: add `kernel=kernel8.img` to `/boot/firmware/config.txt`,
  reboot, and re-check BEFORE continuing.
- **Correction (2026-08-13): OSRM is NOT one of the affected binaries.** An
  earlier revision of this doc claimed it was. Verified against both
  `v5.27.1` and `v26.8.0-debian`: `find / -iname '*jemalloc*'` returns
  nothing and `ldd $(which osrm-routed)` links only glibc. Keep this
  pre-flight anyway — other services in the stack (Redis in particular is
  commonly jemalloc-linked) are still exposed — but do not skip an OSRM
  deploy on account of it, and do not cite OSRM as the reason.
- Capacity (verified 2026-08-10): 15.6 GB RAM total / 11.3 GB available,
  191 GB free disk. The prepared extract is ~1–1.5 GB and is mmapped
  (`osrm` container limit in `docker-compose.yml` is 2 GB; reservation 512 MB).

## 2. Prepare data OFF the Pi and ship it

Preprocessing needs several GB of RAM — never run it on the Pi. OSRM's
data fingerprint checks OS, endianness and pointer size (NOT CPU
architecture) plus the OSRM version, so files prepared in a linux container
on a workstation load fine on the Pi — as long as the image version matches
`docker-compose.yml`'s `osrm` service exactly (`ghcr.io/project-osrm/osrm-backend:v26.8.0-debian`
in both places as of this writing — confirm with
`grep -n 'OSRM_IMAGE=' scripts/prepare_osrm_data.sh; grep -n "image: ghcr.io/project-osrm" docker-compose.yml`
before you run the script, in case either has drifted since this doc was written).

**Dataset compatibility policy (v26 CalVer series).** Upstream declares
prepared data incompatible across **every monthly release** — only same-month
patch bumps keep it loadable. Verified at source level: the fingerprint magic
byte changed `'N'`→`'O'` between v26.5.0 and v26.6.0 when packed OSM-ID width
went 34→40 bits. So bumping the image tag is never a one-line edit; it always
means re-running the pipeline off-Pi and re-shipping ~1 GB. **Pin
deliberately, upgrade on purpose, never chase `latest`.**

```bash
# On the workstation:
bash scripts/prepare_osrm_data.sh
rsync -av --progress ./osrm_data/ <prod-host>:<repo>/osrm_data/
```

**Validate the prepared data before shipping** (v26+ only — `--trial` starts
the engine, loads the dataset and exits, so a fingerprint or missing-file
problem surfaces on the workstation instead of on the Pi):

```bash
docker run --rm -v "$PWD/osrm_data:/data:ro" \
  ghcr.io/project-osrm/osrm-backend:v26.8.0-debian \
  osrm-routed --algorithm mld --mmap --trial=1 /data/uzbekistan-latest.osrm
# Expect: "shutdown completed", exit 0.

# And confirm you are shipping every file the binary requires:
docker run --rm ghcr.io/project-osrm/osrm-backend:v26.8.0-debian \
  osrm-routed --list-inputs --algorithm MLD
```

**Write `--trial=1`, not `--trial`.** Like `--mmap`, `--trial` is a Boost
implicit-value option, so as the last flag it swallows the positional dataset
path and dies with *"the argument ('/data/…osrm') for option '--trial' is
invalid"*. (Same family of trap as the `--max-table-size` note in §5 — this
one bit the author of this doc while writing it.) Passing the dataset path
before the flags also works.

This check is not decoration — it was verified to catch the exact failure it
exists for. Run against a dataset prepared by a *different* OSRM version it
exits **2** with:

    [error] Fingerprint did not match the expected value:
            /data/uzbekistan-latest.osrm.cells

which is precisely what you would otherwise discover only after rsyncing ~1 GB
to the Pi and restarting the service.

Output is `./osrm_data/uzbekistan-latest.osrm.*` (~1–1.5 GB, ~26 files).
Never `git add osrm_data/` — it's gitignored on purpose.

## 3. Which env file actually matters: `.env`, not `production.env`

This repo ships **two** parallel-looking provisioning paths. Only one of
them is live:

| | Live path (use this) | Separate/legacy path (do NOT use for this deploy) |
|---|---|---|
| Env file | `.env` (copy from `.env.example`) | `production.env` (copy from `production.env.example`) |
| Compose files | `docker-compose.yml` + `docker-compose.production.yml` | `docker-compose.secrets.yml` (standalone) |
| Deploy tooling | `scripts/deploy.sh`, `scripts/manage-secrets.sh` (`ENV_FILE=".env"`) | `scripts/deploy-secrets.sh` (`ENV_FILE=production.env`, Docker Swarm secrets) |

Evidence this table is checked against, not assumed:
- Every service in `docker-compose.yml` that needs app config declares
  `env_file: - .env` (six occurrences — `business_app`, `migrate`,
  `celery_worker`, `celery_beat`, `telegram_bot`, `staff_bot`).
  `docker-compose.production.yml` only overlays `GUNICORN_*`/`CELERY_WORKER_*`
  on top — it never redefines `env_file`.
- `scripts/deploy.sh` (the production deploy wrapper — see its own header
  comment) reads `.env` directly (`grep -q '^MONITORING_BASIC_AUTH=' .env`)
  and runs `docker compose -f docker-compose.yml -f docker-compose.production.yml`.
- `scripts/manage-secrets.sh` sets `ENV_FILE=".env"`.
- `docs/operations/rotate-postgres-password.md` (written 2026-08-05 "against
  the real prod wiring") backs up and edits `.env`, using the same two
  compose files.
- `docker-compose.secrets.yml` is a **separate, out-of-sync** stack — it has
  no `staff_bot` service and no `osrm` service at all. If anyone ever
  deploys through `scripts/deploy-secrets.sh` instead of `scripts/deploy.sh`,
  this entire feature (and the current staff bot) silently does not exist.

**Bottom line: edit `.env` on the Pi, as this runbook does below.**
`production.env.example` has been updated with the same new keys (for
consistency / in case that path is ever revived), but it is not what the
running Pi reads today.

## 4. Enable and start (ORDER MATTERS)

On the Pi, in `<repo>/.env` add:

```
COMPOSE_PROFILES=routing
OSRM_BASE_URL=http://osrm:5000
OSRM_PUBLIC_FALLBACK_ENABLED=false
LEGACY_MATRIX_PROVIDERS_ENABLED=false
MATRIX_LIVE_ORIGIN_TTL_SECONDS=120
ROUTE_SERVICE_TIME_MINUTES=4
WAREHOUSE_LATITUDE=<real warehouse lat>
WAREHOUSE_LONGITUDE=<real warehouse lng>
# Optional (traffic-aware next-leg ETA). Leave unset to skip the tier:
GOOGLE_ROUTES_API_KEY=<key entitled to Routes API>
```

`docker compose` reads `.env` automatically for variable substitution
(`${OSRM_BASE_URL:-...}` in `docker-compose.yml`) **and** because
`business_app`/`celery_worker`/`staff_bot` all declare `env_file: - .env`,
these values also land directly in each container's environment — you do
not need to add them to `docker-compose.yml`'s `environment:` blocks too.

Env changes need a **RECREATE**, not a restart (`docker compose restart`
does not reload `.env` — only `up -d` / `up -d --force-recreate` do).

This release also contains Plan 1 changes to `staff_bot` that **must reach
drivers' phones before `business_app`/`celery_worker` do**: the new backend
mints a unique `event_id` per push (defeating the old constant-key dedup
that was silently swallowing most pushes), and the old bot doesn't know
about the new `sound` field. Old-bot + new-backend is the failure mode
(~30 duplicate messages/driver/day); new-bot + old-backend is safe. Do
**not** just run `scripts/deploy.sh` for this release — it does one
`docker compose up -d --build` for every service at once, with no ordering
guarantee between `staff_bot` and `business_app`/`celery_worker`. Use the
explicit sequence below instead.

```bash
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.production.yml"

# 1. Reseed staff bot translations BEFORE restarting staff_bot, or drivers
#    briefly see stale copy (including a diversion-offer string with an
#    inverted sign). scripts/ is not mounted into business_app — pipe it in:
$COMPOSE exec -T business_app python - < scripts/seed_staff_translations.py

# 2. Bring up the new osrm service and confirm it's healthy before anything
#    depends on it:
$COMPOSE up -d osrm
$COMPOSE ps osrm         # must reach "healthy" (60s start_period, then TCP-probed every 30s)

# 3. staff_bot FIRST (new sound field + new translations):
$COMPOSE up -d --build --force-recreate staff_bot
$COMPOSE ps staff_bot     # must show "Up"

# 4. THEN business_app + celery_worker (new event_id minting + OSRM wiring):
$COMPOSE up -d --build --force-recreate business_app celery_worker

# 5. nginx does not re-resolve upstream DNS at runtime; business_app just
#    got a new container IP (same reason scripts/deploy.sh always does this):
$COMPOSE restart nginx
```

## 5. Verify the deployed OSRM's table-size semantics

**Corrected rule** (the original plan's "the guard counts cells" claim was
empirically falsified during setup — do not repeat it). `--max-table-size=M`
rejects iff `sources.size() × destinations.size() > M²` — a cap on the
*product* against the **square** of the flag, not a per-axis or raw-count
cap. Full evidence (three flag values, independently reproduced) is in
the comment above the `osrm` service in `docker-compose.yml` and in
`task-1-report.md`. At the deployed `--max-table-size 1000`, this means a
plain symmetric table (our real usage: one coordinate list, no explicit
`sources=`/`destinations=`) is safe up to `N=1000`; our real worst case is
16 (15 stops + origin) — nowhere close.

You do not need to re-derive this boundary on every deploy. Run the smoke
test below to confirm the service answers real app-shaped requests:

```bash
# 16 coordinates = our real worst case (15 stops + origin). Must be "Ok":
COORDS16=$(python3 -c "print(';'.join(f'{69.24+i*0.003:.5f},{41.29+i*0.002:.5f}' for i in range(16)))")
curl -s "http://127.0.0.1:5001/table/v1/driving/${COORDS16}?annotations=distance,duration" | head -c 80
```

Optional, for extra confidence that the guard is the product rule and not
a naive "≤1000 coordinates total" cap (reuses the same 2 coordinates,
`sources=0` against 2000 duplicated-index `destinations=1`, i.e. `1×2000 =
2000` cells — far under `1000²` but would trip a naive per-axis-1000 cap
were one mistakenly implemented; this exact shape was run for real against
the deployed image and data during setup, see `task-1-report.md` "Fix
note"):

```bash
DST2000=$(python3 -c "print(','.join(['1']*2000))")
curl -s "http://127.0.0.1:5001/table/v1/driving/69.24000,41.29000;69.24300,41.29200?sources=0&destinations=${DST2000}&annotations=distance,duration" | head -c 80
# Expect "code":"Ok" with rows=1, cols=2000 — not "TooBig".
```

**Do not "fix" this by dropping `--max-table-size` from the command as a
simplification.** `--mmap` is a boost implicit-value option: if it is the
*last* flag before the positional dataset path, it swallows that path as
its own argument and the container fails to start
(`osrm-routed --algorithm mld --mmap /data/uzbekistan-latest.osrm` fails
with *"the argument ('/data/uzbekistan-latest.osrm') for option '--mmap'
is invalid"*). The shipped flag order
(`--algorithm mld --mmap --max-table-size 1000 /data/uzbekistan-latest.osrm`)
is only safe because `--max-table-size 1000` sits between `--mmap` and the
path. If you ever need to change flags, keep something after `--mmap` and
before the path — never remove it outright.

## 6. Prove the switch took effect (Loki)

Log shipping is Grafana Alloy → Loki (`monitoring/alloy/config.alloy`).
The label is **`service`** (compose service name), not `compose_service`
or `container` — and everything from this pipeline carries `job="docker"`:

```
{job="docker", service=~"business_app|celery_worker"} |= "distance_matrix_built source="
```

Run over the first hours after deploy. Read these in the order an operator
will actually observe them, not in order of "most impressive proof" — the
first bullet is the normal cold-start signal; the third is the strongest
cache-split proof but may not appear for hours.

Exact literal strings, in order of appearance:

- **`distance_matrix_built source=osrm_selfhosted` + `static_tier=miss
  live_tier=miss`** — **the normal first-observation signal.** Every
  cold-cache call (empty Redis, e.g. right after deploy) hits this generic
  fall-through path (`distance_matrix.py:906-916`), which always logs
  `live_tier=miss` and `static_tier=miss` together. Seeing this is expected
  and healthy — do not treat it as a problem. Both substrings are pinned by
  `tests/unit/test_distance_matrix.py::TestSourceLabelObservability::test_selfhosted_fetch_logs_the_source_label`.
- **`distance_matrix_built source=cache` + `static_tier=hit live_tier=hit`**
  — full cache hit, no network call at all. Both substrings pinned by the
  same test class's `test_full_cache_hit_logs_the_cache_label`.
- **`static_tier=hit live_tier=fetched`** — the strongest proof that the
  two-tier cache split itself is paying off (static stop↔stop sub-matrix
  reused, only the moving origin row re-fetched): this combination never
  appeared before this change. It is real production behavior
  (`distance_matrix.py:780-781`) but **is not covered by an automated
  test** (confirmed against `TestSourceLabelObservability` directly — it
  only pins the two lines above; `task-8-report.md` notes this gap
  explicitly). It's also gated behind a static-tier hit on a re-optimization
  with **more than 2 points** — 2-point calls (single-leg driver→next-stop)
  deliberately skip this path (the "degenerate" case at
  `distance_matrix.py:758-765`). Don't be alarmed if you don't see it in
  the first hours; absence here is not a failure signal the way
  `source=haversine` is.

Also useful (throttled, ≤1/300s per process — don't expect a line per call):
- **`Self-hosted OSRM unavailable`** — self-hosted tier was unreachable and
  the chain fell through. Expected rarely; frequent = investigate `osrm`.
- **`google_routes_leg_failed`** — Google Routes key configured but failing
  (silent, not logged at all, if `GOOGLE_ROUTES_API_KEY` is simply unset —
  that's a deliberate skip, not a failure).

**Failure signal — must stay rare:** any `source=haversine` means every
real provider failed for that call (self-hosted OSRM down AND the public
demo flag is off) — straight-line distance is used, sequencing/ETA degrade.
Investigate immediately if this appears more than a handful of times.

The old `source=osrm_table` (public demo), `source=here_matrix` /
`source=yandex_matrix`, and the HERE 403 / Yandex 401 warning lines must
be GONE — their absence is itself part of the proof.

## 7. Rollback levers (fastest first — no code revert, no migration)

All levers are `.env` edits followed by:
```bash
docker compose -f docker-compose.yml -f docker-compose.production.yml \
  up -d --force-recreate business_app celery_worker
docker compose -f docker-compose.yml -f docker-compose.production.yml \
  restart nginx
```

**The `restart nginx` line is not optional.** `--force-recreate business_app`
gives that container a new bridge-network IP; nginx OSS resolves upstream
hostnames once at config load and does not re-resolve at runtime (same
reason `scripts/deploy.sh` restarts nginx unconditionally after every
`up -d`, build or no-build — see its own header comment). Skip this and
every request 502s until nginx is separately recreated — i.e. reaching for
a rollback during an incident would produce a second, worse outage. This
applies to every lever below that recreates `business_app` (all of them).

1. **OSRM misbehaving (bad ETAs, crashes, OOM):** set `OSRM_BASE_URL=`
   (empty disables the tier) and `OSRM_PUBLIC_FALLBACK_ENABLED=true`. This
   restores the pre-change EFFECTIVE behaviour (public demo server), since
   HERE and Yandex were already dead (403/401). Optionally
   `docker compose stop osrm` and remove `COMPOSE_PROFILES=routing` from
   `.env`.

   **Note the second consumer (added 2026-08-13).** `OSRM_BASE_URL` no
   longer feeds only the distance matrix: `MapsService._osm_get_route` now
   resolves through the same two settings, so with `MAPS_PROVIDER=osm` the
   admin dispatch map's road geometry is served by our own engine instead of
   the public demo server. Consequences for this lever:
   - Emptying `OSRM_BASE_URL` with `OSRM_PUBLIC_FALLBACK_ENABLED=false`
     makes `get_route()` raise `ExternalServiceError` by design rather than
     silently call a third-party demo box. The dispatch map degrades to
     straight dashed legs (`OperationsMap.jsx` already handles null
     geometry) — visible, not broken.
   - If you want geometry to keep working through the rollback, set
     `OSRM_PUBLIC_FALLBACK_ENABLED=true` as step 1 already instructs, or
     switch `MAPS_PROVIDER` to `google`.
   - `MAPS_PROVIDER=google|yandex` never reaches this code path at all.
2. **Want the full legacy chain back verbatim:** additionally set
   `LEGACY_MATRIX_PROVIDERS_ENABLED=true` — NOT recommended: it re-adds
   ~1.5 s of guaranteed 403/401 failure per optimization for zero data.
3. **Google ETA misbehaving:** unset `GOOGLE_ROUTES_API_KEY` — the tier
   skips silently; matrix durations take over.
4. **Do NOT touch the `osrm` service's `command:` in `docker-compose.yml`
   as a rollback edit** — in particular, never drop `--max-table-size 1000`
   to "revert to the default." Because `--mmap` is a boost implicit-value
   flag, removing the option that follows it makes `--mmap` swallow the
   positional dataset path instead, and the container fails to start (see
   §5). If OSRM needs to come down, use lever 1 (`OSRM_BASE_URL=`) or
   `docker compose stop osrm` — never edit the command line.
5. **Verify the rollback** with the same Loki query as §6: `source=osrm_table`
   lines reappear and drivers still get routes.
6. **Rolling back `staff_bot` vs `business_app`/`celery_worker`:** if you
   need to roll back code (not just config), the ordering requirement from
   §4 runs in reverse for a rollback — reverting `business_app`/
   `celery_worker` to old code while `staff_bot` is still new is safe (new
   bot + old backend was already stated as the safe direction); reverting
   `staff_bot` alone while `business_app`/`celery_worker` stay new recreates
   the ~30-messages/driver/day duplicate-push bug. If rolling back code at
   all, roll back `business_app`/`celery_worker` first, `staff_bot` last.

The `distance_matrix:static:v2:*` / `distance_matrix:live:v2:*` Redis keys
are self-expiring — no cleanup needed either way. Static-tier TTL is 24 h
(`MATRIX_CACHE_TTL_STATIC_SECONDS`, default `_STATIC_TTL_DEFAULT = 86400`)
for free-flow (non-traffic) entries, or 30 min
(`MATRIX_CACHE_TTL_TRAFFIC_SECONDS`, default `_TRAFFIC_TTL_DEFAULT = 1800`)
for traffic-aware ones; the live-origin tier is always
`MATRIX_LIVE_ORIGIN_TTL_SECONDS` (120 s here). Old `distance_matrix:v1:*`
keys from before the cache split simply expire unread.
