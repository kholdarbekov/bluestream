# Monitoring Subdomains Runbook

This document covers the five monitoring subdomains under `aqua-element.uz`,
their auth model, dashboard provisioning, and the one-time setup required
before first deploy.

## Topology

```
                                 Cloudflare TLS
                                       │
                                       ▼
                                 nginx :81  (origin)
                                       │
        ┌──────────────┬───────────────┼───────────────┬──────────────┐
        ▼              ▼               ▼               ▼              ▼
flower.aqua-       prometheus.    grafana.        loki.        alertmanager.
element.uz         aqua-element.  aqua-element.   aqua-        aqua-element.
                   uz             uz              element.uz   uz
   │                   │              │                │              │
   │                   │              │                │              │
   ▼                   ▼              ▼                ▼              ▼
 flower:5555      prometheus:9090   grafana:3000    loki:3100   alertmanager:9093
 (FLOWER_BASIC_   (htpasswd)        (admin login    (htpasswd)  (htpasswd)
  AUTH)                              + RBAC)
```

| Subdomain                       | Outer auth (nginx)         | Inner auth                 |
|---------------------------------|----------------------------|----------------------------|
| `flower.aqua-element.uz`        | passthrough                | `FLOWER_BASIC_AUTH`        |
| `prometheus.aqua-element.uz`    | htpasswd_monitoring        | none                       |
| `loki.aqua-element.uz`          | htpasswd_monitoring        | none                       |
| `alertmanager.aqua-element.uz`  | htpasswd_monitoring        | none                       |
| `grafana.aqua-element.uz`       | passthrough                | Grafana admin login + RBAC |

## First-time setup

### 1. DNS records (Cloudflare)

Add these as CNAMEs to the existing `aqua-element.uz` origin (or A records to
the origin IP), all **proxied** (orange cloud) so TLS terminates at Cloudflare:

```
flower         CNAME  aqua-element.uz   ☁ Proxied
prometheus     CNAME  aqua-element.uz   ☁ Proxied
grafana        CNAME  aqua-element.uz   ☁ Proxied
loki           CNAME  aqua-element.uz   ☁ Proxied
alertmanager   CNAME  aqua-element.uz   ☁ Proxied
```

Cloudflare SSL/TLS mode stays at the existing setting (Flexible per
INF-002). Origin TLS hardening is a separate audit item and not in scope here.

### 2. Generate the htpasswd file (one-time)

```bash
./scripts/manage-monitoring-auth.sh init umar
# Prompts twice for a password, bcrypt-hashes it, writes to
# secrets/htpasswd_monitoring (chmod 644 — nginx in container reads it).
```

Add additional operators later:

```bash
./scripts/manage-monitoring-auth.sh add ops
./scripts/manage-monitoring-auth.sh rotate umar
./scripts/manage-monitoring-auth.sh remove ex_employee
./scripts/manage-monitoring-auth.sh list
```

After any change, reload nginx (no downtime):

```bash
./scripts/manage-monitoring-auth.sh reload
```

### 3. Generate Grafana admin password

```bash
echo "GRAFANA_PASSWORD=$(openssl rand -base64 24)" >> .env
```

Grafana now refuses to start without `GRAFANA_PASSWORD` set in `.env`
(the `:?` requirement in `docker-compose.production.yml`).

### 4. Generate the Postgres monitoring role password + apply role

`postgres_exporter` connects to PostgreSQL using a dedicated `monitoring_ro`
role with `pg_monitor` privileges (no data access). The role's password
lives at `secrets/postgres_monitoring_password` and is mounted into the
exporter as a Docker secret.

```bash
# (a) Generate a random password (32 chars) into the secret file:
./scripts/manage-secrets.sh create postgres_monitoring_password

# (b) Apply the role to the running postgres container:
docker compose exec -T postgres psql \
    -U postgres -d "${POSTGRES_DB:-bluestream_db}" \
    -v monitoring_password="$(cat secrets/postgres_monitoring_password)" \
    -f - < monitoring/postgres/init-monitoring-role.sql
```

Re-run step (b) any time you rotate the password — the SQL is idempotent.

### 5. Deploy

```bash
./scripts/deploy.sh
```

`deploy.sh` will:
- Create empty placeholders for `secrets/htpasswd_monitoring` and
  `secrets/postgres_monitoring_password` if missing (so Docker bind mounts
  don't fail), with a warning to populate them.
- `docker compose up -d --build`
- Wait for `business_app` health
- Restart nginx (refreshes upstream DNS for the new monitoring containers)
- Smoke-test `http://localhost:81/health`

### 6. Verify

```bash
# Each subdomain returns 401 without creds (Grafana redirects to /login):
for h in flower prometheus loki alertmanager; do
    code=$(curl -sk -o /dev/null -w '%{http_code}' \
            -H "Host: ${h}.aqua-element.uz" http://localhost:81/)
    echo "$h: $code"
done
# Expected: flower=401 prometheus=401 loki=401 alertmanager=401

curl -sk -o /dev/null -w '%{http_code}\n' \
    -H "Host: grafana.aqua-element.uz" http://localhost:81/
# Expected: 302 (→ /login)

# With credentials, Prometheus answers queries:
curl -sk -u "umar:PASS" -H "Host: prometheus.aqua-element.uz" \
    "http://localhost:81/api/v1/query?query=up" | jq '.status'
# → "success"

# Every Prometheus target should be healthy:
curl -sk -u "umar:PASS" -H "Host: prometheus.aqua-element.uz" \
    "http://localhost:81/api/v1/targets" \
    | jq '.data.activeTargets[] | {job, health}'
# Expected health: up for prometheus, business_app, postgres, redis,
# celery, node, cadvisor, nginx, alertmanager
```

## Dashboards

Grafana auto-loads every JSON file under
`monitoring/grafana/dashboards/` at startup, with `foldersFromFilesStructure: true`
so each subdirectory becomes a Grafana folder.

| Folder                  | Dashboards                                                                | Source(s)                                                                |
|-------------------------|---------------------------------------------------------------------------|--------------------------------------------------------------------------|
| `00-bluestream/`        | Flask App, Business Critical, Alerts Overview                             | Custom                                                                   |
| `10-host-and-docker/`   | Node Exporter Full, Docker overview (cAdvisor + node + state)             | grafana.com [1860](https://grafana.com/grafana/dashboards/1860), [21154](https://grafana.com/grafana/dashboards/21154) |
| `20-data-stores/`       | PostgreSQL Overview, Redis                                                | grafana.com [14114](https://grafana.com/grafana/dashboards/14114), [763](https://grafana.com/grafana/dashboards/763) |
| `30-async/`             | Celery (in-house), Celery Tasks Dashboard                                 | Existing + grafana.com [20076](https://grafana.com/grafana/dashboards/20076) |
| `40-edge/`              | NGINX (nginx-prometheus-exporter)                                         | grafana.com [12708](https://grafana.com/grafana/dashboards/12708)        |
| `50-logs/`              | Loki Logs                                                                 | grafana.com [13639](https://grafana.com/grafana/dashboards/13639)        |

**Loki dashboards are empty by design** until log shipping lands in OTel
migration plan Phase 1 ([docs/otel_migration_plan.md](otel_migration_plan.md)).
The datasource is provisioned and ready; panels start populating the moment
ingest begins, no Grafana redeploy needed.

### Updating dashboards

The community dashboards were downloaded from grafana.com and have their
datasource UIDs hardcoded to `prometheus` / `loki` (the UIDs declared in
`monitoring/grafana/datasources/datasources.yml`). To refresh one:

```bash
# Pull latest revision from grafana.com:
curl -fsSL "https://grafana.com/api/dashboards/<ID>/revisions/latest/download" \
    -o monitoring/grafana/dashboards/<folder>/<name>.json

# Re-run the placeholder transform (drops __inputs, replaces ${ds_prometheus} → prometheus):
python3 -c "
import json, re, pathlib
p = pathlib.Path('monitoring/grafana/dashboards/<folder>/<name>.json')
d = json.loads(p.read_text())
d.pop('__inputs', None); d.pop('__requires', None); d['id'] = None
if 'templating' in d:
    d['templating']['list'] = [v for v in d['templating'].get('list', []) if v.get('type') != 'datasource']
s = json.dumps(d, indent=2)
for k, uid in {'ds_prometheus':'prometheus','DS_PROMETHEUS':'prometheus','DS_PROM':'prometheus','ds_loki':'loki','DS_LOKI':'loki','datasource':'prometheus'}.items():
    s = s.replace('\${' + k + '}', uid)
p.write_text(s)
"
```

## Managing alerts

Prometheus alert rules: [monitoring/alert_rules.yml](../monitoring/alert_rules.yml)
(13 rules covering CPU/memory/disk, DB/Redis/app health, payment webhooks,
Celery queue depth + failure rate + stuck tasks).

Routing + receivers: [monitoring/alertmanager.yml](../monitoring/alertmanager.yml)
(routes to `slack-alerts` receiver via Slack webhook URL stored at
`secrets/slack_webhook_url.txt`).

**Silencing an alert** — three ways, pick whichever is convenient:

1. **Grafana** (recommended for ad-hoc): https://grafana.aqua-element.uz/alerting/silences
   → `New silence`. Uses the Alertmanager datasource so silences appear at
   alertmanager.aqua-element.uz too.
2. **Alertmanager UI**: https://alertmanager.aqua-element.uz/#/silences →
   `New Silence`.
3. **amtool** (CLI) for scripted maintenance.

## Troubleshooting

| Symptom                                                    | Likely cause                                                      | Fix                                                                                                              |
|------------------------------------------------------------|-------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------|
| `flower.aqua-element.uz` → 502                             | Flower container down                                              | `docker compose ps flower`; check `FLOWER_BASIC_AUTH` is set                                                     |
| `prometheus.*` → 500 "auth_basic_user_file: No such…"      | `secrets/htpasswd_monitoring` missing                              | `./scripts/manage-monitoring-auth.sh init <user>`                                                                |
| `prometheus.*` → 401 with valid creds                      | htpasswd not reloaded after change                                 | `./scripts/manage-monitoring-auth.sh reload`                                                                     |
| Grafana login screen, but `admin` / config password fails  | `GRAFANA_PASSWORD` not set or changed without container restart    | `echo GRAFANA_PASSWORD=… >> .env && docker compose up -d --force-recreate grafana`                               |
| Postgres dashboard empty                                   | `monitoring_ro` role missing or wrong password                     | Re-run [Section 4](#4-generate-the-postgres-monitoring-role-password--apply-role)                                |
| Loki dashboards empty                                      | Expected — log shipping is OTel Phase 1, not landed yet            | n/a                                                                                                              |
| `nginx_status 403` from nginx_exporter                     | Bridge subnet mismatch (custom Docker network)                     | Add the subnet to the `allow` list in `nginx/conf.d/sites/00-stub-status.conf`                                   |
| Alertmanager UI loads but `Active silences` stays empty    | Grafana's Alertmanager datasource UID drift                        | Confirm `uid: alertmanager` in `monitoring/grafana/datasources/datasources.yml`                                  |

## Out of scope (deferred)

- Loki log shipping (Promtail / OTel Collector). See OTel migration plan
  Phase 1 in [docs/otel_migration_plan.md](otel_migration_plan.md).
- Origin-side TLS / certs. See INF-002 in [docs/audit/06-deployment-and-infra.md](audit/06-deployment-and-infra.md).
- Grafana SSO/SAML. Grafana admin login + UI-created users is sufficient for
  the current operator count.
- Tempo / distributed traces. Also OTel Phase 1.
