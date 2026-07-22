# ALeRCE Status Website

Public status page for the ALeRCE astronomical broker, deployed at `status.alerce.online`.

## Architecture (Stage 1)

- **Lambda** (`lambda/prober.py`) — runs every 5 minutes via EventBridge, HTTP-probes all public endpoints **sequentially** (to avoid bursting shared backends), writes `status.json` + `history.json` to S3.
- **S3 + CloudFront** — static frontend served at `status.alerce.online`; data files served under `/data/*`.
- **Incidents** — `incidents/incidents.json` in this repo; CI uploads it to S3 on every push to `main`.

Raw metrics (latency, HTTP codes) are never exposed in the output JSON — only `operational / degraded / outage` labels. Per-probe response times are logged **privately** to the Lambda's CloudWatch Logs (`metric=probe_latency`), so thresholds can be tuned from Logs Insights without publishing latency.

Stage 2 (pipeline + Prometheus signals from on-prem) is not yet designed.

## Alerting

Notifications go to Slack via an SNS topic (`alerce-status-<env>-alerts`) with an
email subscription to a Slack channel's email-integration address — the same
CloudWatch → email → Slack path used elsewhere at ALeRCE.

Three sources publish to that topic:

- **Component state changes** — on each run the prober diffs the new snapshot
  against the previously published `status.json` and, when something changed,
  publishes **one aggregated** message. It alerts when a component gets *worse*
  (operational → degraded/outage, including degraded → outage escalation) and
  when it *fully recovers* (back to operational). Partial recovery
  (outage → degraded) does not re-page, and the first run after a deploy sets a
  baseline silently. The message carries the HTTP code / latency for down
  components — these stay **private to the alert**, never added to the public
  `status.json`. Alerting is best-effort: a notification failure never blocks the
  status/history writes.
- **`alerce-status-<env>-prober-dead`** — the prober Lambda hasn't fired in 10 min.
- **`alerce-status-<env>-prober-errors`** — the prober Lambda raised an error.

**One-time setup:** the prober's `sns:Publish` requires the `alerce-status-boundary`
permissions boundary to allow `sns:Publish` on `alerce-status-*` topics (applied
out-of-band). And SNS email subscriptions can't be auto-confirmed by Terraform —
after the first apply, open the confirmation email that lands in the Slack channel
and click **Confirm subscription**. Until then, publishes succeed but nothing is
delivered.

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

### Code changes (Lambda or frontend)

Open a PR to `main`. `main` is protected: the `test` check must pass and a repo **admin** must approve (enforced via [.github/CODEOWNERS](.github/CODEOWNERS)); repo admins bypass branch protection and can self-merge their own PRs. Merging **auto-deploys** — the **Deploy** workflow runs `test`, then deploys straight to the `production` environment with no manual approval step. The auto-deploy role (`alerce-status-website-github-deploy-v2`) is scoped to specific resource ARNs and carries a permissions boundary — it cannot touch IAM or create resources.

### Infrastructure changes (Terraform)

Terraform is **applied locally by admins**, not from CI. This removes the CI's standing IAM privilege.

1. `aws sso login --profile default`
2. `cd infrastructure && terraform init`
3. Edit `.tf`, run `terraform plan -var "route53_zone_id=Z..." -var "environment=production" -var "alert_email=..."`.
4. Open a PR, paste the plan output, get a review, merge.
5. From a clean checkout of `main`:
   ```bash
   ./scripts/tf-apply.sh -var "route53_zone_id=Z..." -var "environment=production" -var "alert_email=..."
   ```
6. Announce in #devops Slack.

`alert_email` is the endpoint for the alerts SNS topic — typically a Slack channel's email-integration address (matching the existing ALeRCE CloudWatch → email → Slack pattern). It's a `sensitive`, no-default variable, so it's supplied at apply time and never committed. See [Alerting](#alerting) for the one-time subscription-confirmation step.

State is in `s3://alerce-terraform-state/status-website/terraform.tfstate`, locked via the `alerce-terraform-state-lock` DynamoDB table.

**Never run `terraform apply` outside `scripts/tf-apply.sh`** — the wrapper refuses to apply from a feature branch or a dirty working tree.

### First-time setup (one-off, for reference)

GitHub Actions secrets required by the **Deploy** workflow:
- `AWS_AUTO_DEPLOY_ROLE_ARN` — narrow auto-deploy role (created by Terraform / out-of-band)
- `LAMBDA_FUNCTION_NAME`, `SITE_BUCKET`, `DATA_BUCKET`, `CLOUDFRONT_DISTRIBUTION_ID` — populated after the first `terraform apply`.

### Posting an incident or maintenance note

See [below](#posting-an-incident-or-maintenance-note).

## Configuring probed endpoints

Edit `lambda/config.json`. Fields per component:

| Field | Description |
|---|---|
| `id` | Unique identifier (used in history tracking) |
| `label` | Display name in the UI |
| `group` | `apis` (ZTF), `apis_lsst` (multi-survey / LSST), or `frontends`. A new group value also needs a container in `frontend/index.html` and an entry in the `groups` map in `frontend/app.js`. |
| `url` | URL to probe |
| `method` | HTTP method (usually `GET`) |
| `expected_status` | List of acceptable HTTP status codes |
| `latency_degraded_ms` | *(optional)* per-endpoint override of the global degraded threshold |
| `latency_outage_ms` | *(optional)* per-endpoint override of the global outage threshold |
| `timeout_s` | *(optional)* per-endpoint request timeout override |

Global defaults (`thresholds` key), used when a component doesn't override them:
- `latency_degraded_ms` — response slower than this → degraded (default 4000 ms)
- `latency_outage_ms` — response slower than this → outage (default 10000 ms)
- `timeout_s` — request timeout (default 15 s)

Slow-by-design endpoints (e.g. the all-catalog crossmatch ~20 s, the object-ranking query ~13 s) set their own higher `latency_*`/`timeout_s` so the tight fast-endpoint defaults don't flag them. `python lambda/prober.py` prints a slowest-first latency table to help calibrate these.

## Posting an incident or maintenance note

Both incidents and maintenance windows live in [incidents/incidents.json](incidents/incidents.json). They're distinguished by the optional `type` field: omit it (or set `"incident"`) for an unplanned incident; set `"maintenance"` for a planned window. The frontend renders both under the **Incidents & Maintenance** card; maintenance entries get a purple accent and a "Maintenance" tag.

### Workflow

1. Edit [incidents/incidents.json](incidents/incidents.json) using [scripts/incident.py](scripts/incident.py) (or by hand — it's a plain JSON array).
2. Open a PR, get it reviewed, merge to `main`.
3. The **Deploy** workflow uploads it to the data bucket. The live page polls `incidents.json` every 60 s, so it appears within ~1 minute of deploy.

The helper script handles UTC timestamps, status validation, and auto-fills `resolved_at` on close:

```bash
# Open a new incident
python scripts/incident.py open 2026-05-15-api-degraded \
    --title "Elevated error rate on object API" \
    --components apis --severity major \
    --message "Investigating reports of API errors."

# Append an update during the incident
python scripts/incident.py update 2026-05-15-api-degraded \
    --message "Rolled back the bad deploy; monitoring."

# Close it
python scripts/incident.py update 2026-05-15-api-degraded \
    --status resolved --message "Resolved after rolling restart."

# Schedule a maintenance window
python scripts/incident.py open 2026-06-01-db-upgrade \
    --type maintenance --title "Database upgrade" \
    --start 2026-06-01T03:00:00Z --components apis \
    --message "Maintenance window scheduled for Jun 1, 03:00–04:00 UTC."
```

To preview locally before opening the PR, run `python scripts/dev_server.py` and load `http://localhost:8000` — the dev server copies `incidents/incidents.json` into `data/` on startup.

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
