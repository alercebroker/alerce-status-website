# ALeRCE Status Website

Public status page for the ALeRCE astronomical broker, deployed at `status.alerce.online`.

## Architecture (Stage 1)

- **Lambda** (`lambda/prober.py`) — runs every minute via EventBridge, HTTP-probes all public endpoints, writes `status.json` + `history.json` to S3.
- **S3 + CloudFront** — static frontend served at `status.alerce.online`; data files served under `/data/*`.
- **Incidents** — `incidents/incidents.json` in this repo; CI uploads it to S3 on every push to `main`.

Raw metrics (latency, HTTP codes) are never exposed in the output JSON — only `operational / degraded / outage` labels.

Stage 2 (pipeline + Prometheus signals from on-prem) is not yet designed.

## Local development

```bash
# Full local stack: probes real endpoints, serves the site at localhost:8000
python scripts/dev_server.py
# PORT=9000 python scripts/dev_server.py  ← optional port override

# Run the prober once (dry-run, no writes)
python lambda/prober.py

# Run tests
pip install pytest
pytest lambda/tests/ -v
```

## Deployment

### First deploy

1. Set up GitHub Actions secrets:
   - `AWS_DEPLOY_ROLE_ARN` — IAM role with OIDC trust for this repo
   - `ROUTE53_ZONE_ID` — hosted zone for `alerce.online`
   - After first Terraform apply, also add: `LAMBDA_FUNCTION_NAME`, `SITE_BUCKET`, `DATA_BUCKET`, `CLOUDFRONT_DISTRIBUTION_ID`

2. Run Terraform (manual via GitHub Actions → **Terraform** workflow → action: `plan` then `apply`).

3. Merge to `main` — the **Deploy** workflow tests, packages the Lambda, syncs frontend, and uploads incidents.

### Ongoing

- **Code changes** (Lambda or frontend): push to `main` → deploys automatically.
- **Infrastructure changes**: run the Terraform workflow manually with `action=apply`.
- **Posting an incident or maintenance note**: see [below](#posting-an-incident-or-maintenance-note).

## Configuring probed endpoints

Edit `lambda/config.json`. Fields per component:

| Field | Description |
|---|---|
| `id` | Unique identifier (used in history tracking) |
| `label` | Display name in the UI |
| `group` | `apis` or `frontends` |
| `url` | URL to probe |
| `method` | HTTP method (usually `GET`) |
| `expected_status` | List of acceptable HTTP status codes |

Thresholds (`thresholds` key):
- `latency_degraded_ms` — response slower than this → degraded (default 2000 ms)
- `latency_outage_ms` — response slower than this → outage (default 10000 ms)

## Posting an incident or maintenance note

Both incidents and maintenance windows live in [incidents/incidents.json](incidents/incidents.json). They're distinguished by the optional `type` field: omit it (or set `"incident"`) for an unplanned incident; set `"maintenance"` for a planned window. The frontend renders both under the **Incidents & Maintenance** card; maintenance entries get a purple accent and a "Maintenance" tag.

### Workflow

1. Edit [incidents/incidents.json](incidents/incidents.json) — it's a JSON array; append a new object (or update an existing one to add an update or mark it terminal).
2. Open a PR, get it reviewed, merge to `main`.
3. The **Deploy** workflow uploads it to the data bucket. The live page polls `incidents.json` every 60 s, so it appears within ~1 minute of deploy.

To test locally before opening the PR, run `python scripts/dev_server.py` and load `http://localhost:8000` — the dev server copies `incidents/incidents.json` into `data/` on startup.

### Incident entry

```json
{
  "id": "2026-01-01-api-degraded",
  "title": "Elevated error rate on object API",
  "status": "investigating",
  "severity": "major",
  "started_at": "2026-01-01T12:00:00Z",
  "resolved_at": "2026-01-01T14:00:00Z",
  "components": ["apis"],
  "updates": [
    {"at": "2026-01-01T12:05:00Z", "message": "Investigating reports of API errors."},
    {"at": "2026-01-01T14:00:00Z", "message": "Resolved after rolling restart."}
  ]
}
```

### Scheduled maintenance entry

```json
{
  "id": "2026-02-10-db-upgrade",
  "type": "maintenance",
  "title": "Database upgrade — object API briefly unavailable",
  "status": "scheduled",
  "started_at": "2026-02-10T03:00:00Z",
  "components": ["apis"],
  "updates": [
    {"at": "2026-02-05T12:00:00Z", "message": "Maintenance window scheduled for Feb 10, 03:00–04:00 UTC."}
  ]
}
```

Bump `status` to `in_progress` when the window starts and `completed` when it ends.

### Fields

| Field | Required | Notes |
|---|---|---|
| `id` | yes | Stable slug, conventionally `YYYY-MM-DD-short-slug`. |
| `type` | optional | `"incident"` (default) or `"maintenance"`. Drives the visual distinction. |
| `title` | yes | Shown as the headline. |
| `status` | yes | Incidents: `investigating`, `identified`, `monitoring`, `resolved`. Maintenance: `scheduled`, `in_progress`, `completed`. Drives the badge color. |
| `severity` | optional | `minor`, `major`, or `critical`. Informational only. |
| `started_at` | yes | ISO 8601 UTC (`Z` suffix). Drives sort order and the 30-day visibility window. For scheduled maintenance, set to the planned start (future timestamps are fine). |
| `resolved_at` | when terminal | ISO 8601 UTC. Set when the entry reaches its terminal status. |
| `components` | optional | Free-form list, typically `apis` and/or `frontends`. |
| `updates` | optional | Append-only log; each item needs `at` (ISO 8601 UTC) and `message`. |

### Visibility rules

- Active entries (status is not `resolved` or `completed`) are always shown, sorted before terminal ones.
- Terminal entries are hidden once `started_at` is older than 30 days.
