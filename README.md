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
- **Posting an incident**: edit `incidents/incidents.json`, open a PR, merge.

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

## Incident format

```json
{
  "id": "YYYY-MM-DD-short-slug",
  "title": "Human-readable title",
  "status": "investigating | identified | monitoring | resolved",
  "severity": "minor | major | critical",
  "started_at": "2026-01-01T12:00:00Z",
  "resolved_at": "2026-01-01T14:00:00Z",
  "components": ["apis", "frontends"],
  "updates": [
    {"at": "2026-01-01T12:05:00Z", "message": "Investigating reports of API errors."},
    {"at": "2026-01-01T14:00:00Z", "message": "Resolved after rolling restart."}
  ]
}
```
