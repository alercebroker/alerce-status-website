# ALeRCE Status Website

Public status page for the ALeRCE astronomical broker, deployed at `status.alerce.online`.

See `README.md` for user-facing setup, local dev, and deployment instructions. This file documents conventions and constraints that aren't obvious from the code.

## Architecture (Stage 1 — public probes only)

```
EventBridge (1 min) → Lambda prober → S3 (data bucket) ← CloudFront ← user
                                       ↑
                              GitHub Actions on push to main
                              (frontend sync + incidents.json)
```

- [lambda/prober.py](lambda/prober.py) — HTTP-probes every endpoint in [lambda/config.json](lambda/config.json), writes `status.json` + `history.json` to S3. Standard library only at runtime (plus `boto3`, which is in the Lambda runtime).
- [frontend/](frontend/) — plain HTML/CSS/JS. No build step, no framework.
- [incidents/incidents.json](incidents/incidents.json) — edited by PR; CI uploads it to S3 on merge to `main`. The `data/` copy is a local-dev artifact written by [scripts/dev_server.py](scripts/dev_server.py).
- [infrastructure/](infrastructure/) — Terraform; applied **locally** by admins with an SSO session via [scripts/tf-apply.sh](scripts/tf-apply.sh) (not from CI). See `README.md`. State is in `s3://alerce-terraform-state/`, locked via DynamoDB.

Stage 2 (pipeline + Prometheus signals from on-prem) is **deferred and not yet designed** — don't preemptively add hooks, abstractions, or config for it.

## Conventions and constraints

**No frameworks, no dependencies.** Python stdlib for the prober/dev server, plain HTML/CSS/JS for the frontend. Don't add Flask, React, requests, etc. `boto3` is the only allowed runtime import beyond stdlib (it's preinstalled in the Lambda runtime).

**No raw metrics in the output JSON for operational components.** `status.json` exposes only `operational | degraded | outage` labels for healthy components. `probe_url`, `http_code`, and `response_ms` are included only when a component is non-operational, for diagnostic display. Don't add latency series, percentiles, or per-probe details to the public payload.

**HA was a deliberate choice over simplicity.** The Lambda + S3 + CloudFront design exists because the status page must stay up even when ALeRCE infra is down. Don't propose collapsing to a single EC2 / single-region setup.

**Status mapping** lives in `probe()`: HTTP code not in `expected_status` → outage; latency ≥ `latency_outage_ms` → outage; ≥ `latency_degraded_ms` → degraded; else operational. `expected_status` per endpoint can include `404` when "404 from a real API" is still proof the service is up.

**History is bucketed in 5-minute windows** (`BUCKET_MINUTES`) and kept for 90 days (`HISTORY_DAYS`). Re-runs within the same bucket are idempotent (existing entry for that `ts` is replaced).

## Adding / changing probes

Edit [lambda/config.json](lambda/config.json). Each component needs `id` (stable — used as the history key), `label`, `group` (`apis` or `frontends`), `url`, `method`, `expected_status`. Run `python lambda/prober.py` locally to dry-run.

## Posting an incident

Use [scripts/incident.py](scripts/incident.py) (`open` / `update` subcommands) to edit [incidents/incidents.json](incidents/incidents.json), then PR + merge. The script handles UTC timestamps, validates status against the incident vs. maintenance vocabularies, and auto-fills `resolved_at` on terminal status. Format documented in `README.md`. The deploy workflow uploads it to the data bucket; no Lambda involvement.

## Tests

`pytest lambda/tests/ -v`. The deploy workflow blocks on tests passing. When changing prober logic, update / add tests in [lambda/tests/test_prober.py](lambda/tests/test_prober.py).

## Things to verify before claiming a frontend change works

There's no headless test for the frontend. For UI work, run `python scripts/dev_server.py` and load `http://localhost:8000` — the dev server runs the real prober every 60 s and serves `data/*` locally. If you can't run a browser, say so explicitly rather than claiming success.

## Security constraints (read before touching AWS or CI)

**This repo is public.** Nothing identifying about the ALeRCE AWS environment may be committed: no account IDs, ARNs with account IDs, Route53 zone IDs, bucket names tied to other projects, IAM role names, access keys, or internal hostnames. The only ARNs allowed in the repo are AWS-managed policy ARNs (e.g. `arn:aws:iam::aws:policy/...`), which are identical for every AWS user. Everything else lives in GitHub Actions secrets.

**The ALeRCE AWS account holds valuable data and has a large budget.** Treat any new AWS permission as a blast-radius decision, not a convenience. Specifically:

- **Scope IAM policies to `alerce-status-*` resources.** All Terraform-created resources use `local.name_prefix = "alerce-status-${var.environment}"`. The deploy role's permissions must use that prefix so this project cannot touch other ALeRCE infrastructure.
- **No `*:*` permissions.** Don't grant `s3:*` on `*`, `iam:*` on `*`, or `PowerUserAccess` — even temporarily. If a Terraform resource needs a new permission, add the specific action scoped to the specific resource.
- **OIDC trust must be scoped.** The GitHub OIDC role trusts only `repo:alercebroker/alerce-status-website` with `environment:production` or `environment:staging`. Don't widen this to `*` refs or other repos.
- **Don't touch resources outside this project.** No edits to other ALeRCE buckets, Lambdas, IAM roles, or Route53 records (other than the validation/alias records this project owns under `alerce.online`). If a fix seems to require it, stop and ask.

**Before any AWS-affecting action, state the blast radius.** What resources can this touch? What's the cost ceiling if it goes wrong? What does it depend on outside this project? Ask before applying if the answer is "I'm not sure."

**Public Actions logs.** Workflow logs on a public repo are world-readable. `aws-actions/configure-aws-credentials` masks the account ID automatically, but be aware that any new step printing AWS responses may need `--output text --query '<field>'` to avoid leaking resource ARNs from unrelated accounts.
