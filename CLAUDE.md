# ALeRCE Status Website

Public status page for the ALeRCE astronomical broker, deployed at `status.alerce.online`.

See `README.md` for user-facing setup, local dev, and deployment instructions. This file documents conventions and constraints that aren't obvious from the code.

## Architecture (Stage 1 — public probes only)

```
EventBridge (5 min) → Lambda prober → S3 (data bucket) ← CloudFront ← user
                            │          ↑
                            │   GitHub Actions on push to main
                            │   (frontend sync + incidents.json)
                            └→ SNS (alerts topic) → email → Slack
```

- [lambda/prober.py](lambda/prober.py) — HTTP-probes every endpoint in [lambda/config.json](lambda/config.json), writes `status.json` + `uptime.json` to S3, and publishes state-change alerts to SNS (see **Alerting**). Standard library only at runtime (plus `boto3`, which is in the Lambda runtime).
- [frontend/](frontend/) — plain HTML/CSS/JS. No build step, no framework.
- [incidents/incidents.json](incidents/incidents.json) — edited by PR; CI uploads it to S3 on merge to `main`. The `data/` copy is a local-dev artifact written by [scripts/dev_server.py](scripts/dev_server.py).
- [infrastructure/](infrastructure/) — Terraform; applied **locally** by admins with an SSO session via [scripts/tf-apply.sh](scripts/tf-apply.sh) (not from CI). See `README.md`. State is in `s3://alerce-terraform-state/`, locked via DynamoDB.

Stage 2 (pipeline + Prometheus signals from on-prem) is **deferred and not yet designed** — don't preemptively add hooks, abstractions, or config for it.

## Conventions and constraints

**No frameworks, no dependencies.** Python stdlib for the prober/dev server, plain HTML/CSS/JS for the frontend. Don't add Flask, React, requests, etc. `boto3` is the only allowed runtime import beyond stdlib (it's preinstalled in the Lambda runtime).

**No raw metrics in the output JSON for operational components.** `status.json` carries the `operational | degraded | outage` label plus `probe_url` (the endpoint being checked — public info, surfaced in the UI's per-row expander) for **every** component. `http_code` and `response_ms` are included **only** when a component is non-operational, for diagnostic display. Don't add latency series, percentiles, or per-probe metrics to the public payload for healthy components. Raw `response_ms` for **every** probe (operational included) is instead logged **privately** to the Lambda's CloudWatch Logs — one JSON line per probe, `metric=probe_latency` (see `log_response_times()`) — for threshold tuning via Logs Insights. `http_code`/`response_ms` for non-operational components also ride the **private SNS alert** message (see **Alerting**). Keep metrics in the logs and alerts, never in the public payload for healthy components.

**Alerting is edge-triggered and best-effort.** On each run the prober reads the previously published `status.json` (before overwriting it) and diffs statuses in `compute_transitions()`: it alerts when a component gets *worse* (severity rank increases above operational, incl. degraded→outage) and when it *fully recovers* to operational — partial recovery (outage→degraded) is intentionally silent, and a missing baseline (first run / newly-added component) sets state without alerting. All of a run's transitions are aggregated into **one** SNS message (`format_alert()` — ASCII subject, since SNS rejects non-ASCII subjects). `maybe_alert()` never raises: a notification failure must not block the `status.json`/`uptime.json` writes. Alerting is disabled (silently skipped) when `ALERT_TOPIC_ARN` is unset, so the local dry-run and dev server don't publish. The two Lambda self-health alarms (`prober-dead`, `prober-errors`) route to the same topic; `prober-dead` relies on `treat_missing_data = "breaching"` because a stopped Lambda emits *no* `Invocations` datapoint.

**HA was a deliberate choice over simplicity.** The Lambda + S3 + CloudFront design exists because the status page must stay up even when ALeRCE infra is down. Don't propose collapsing to a single EC2 / single-region setup.

**Status mapping** lives in `probe()`: HTTP code not in `expected_status` → outage; latency ≥ `latency_outage_ms` → outage; ≥ `latency_degraded_ms` → degraded; else operational. `expected_status` per endpoint can include `404` when "404 from a real API" is still proof the service is up. Thresholds default to the global `thresholds` block, but any component may override `latency_degraded_ms`, `latency_outage_ms`, and/or `timeout_s` inline (`_effective_thresholds()`). This "hybrid" scheme keeps fast endpoints on tight absolute defaults while giving legitimately slow endpoints (unfiltered LSST object list ~10 s, ZTF object-page light curve ~3.3 s) their own calibrated limits.

Overrides are calibrated from the private `metric=probe_latency` log, targeting a degraded threshold of roughly **1.3–2× the observed healthy p99** — tight enough to catch a real regression, loose enough to absorb routine tail jitter. Recalibrate the same way rather than by intuition, and **exclude known incident windows first**: the 22-23/07/2026 catshtm outage inflated the crossmatch p99.9 by ~20 s, and calibrating against a contaminated distribution is how thresholds drift loose. Two failure shapes matter here. Endpoints that degrade *gradually* (the object lists, the light curves) need a threshold above their real p99. Endpoints that are **bimodal — fast or hung, with nothing in between** (all three crossmatch probes: p95 ≤ 0.8 s, then straight to >20 s) can sit near the global default at almost no false-positive cost, and a loose limit on them is actively harmful: a 20 s crossmatch reported `operational` is the exact failure the page exists to show. For those, prefer a `timeout_s` a little above `latency_outage_ms` so a hung request still records a latency instead of a null.

**Probes run sequentially** (`ThreadPoolExecutor(max_workers=1)`): many endpoints fan out to different EKS pods but share one pgbouncer/Postgres, so concurrent probing would burst the shared pool. A full sequential run can take ~2-3 min with the slow endpoints — hence the Lambda `timeout = 240` and the 5-min schedule (no overlap).

**Uptime history is one fixed-width string per component per UTC day** in `uptime.json` — one character per `BUCKET_MINUTES` slot (`o` operational, `d` degraded, `x` outage, `-` no check recorded), position implying time of day, kept for 90 days (`HISTORY_DAYS`). Three consequences worth knowing before touching `update_history()`:

- **Writes are positional** (`row[:i] + char + row[i+1:]`), so a re-run in the same slot is idempotent *by construction* — which matters because EventBridge→Lambda is at-least-once. Never replace this with per-day counters: a duplicate invocation would increment twice and skew that day's uptime permanently, undetectably.
- **There is no day-rollover step.** The day key and the slot index both derive from one timestamp, so they cannot disagree, and a Lambda outage of any length just leaves `-` slots. `handler()` takes a single clock reading *before* probing and threads it through `build_snapshot()` and `update_history()` — two independent `datetime.now()` calls minutes apart can straddle midnight, and taking it before the run keeps consecutive runs in consecutive slots instead of drifting by however long the probes took.
- **The row's length defines its granularity** (`1440 / len`), read that way by both the prober and `aggregateDaily()`. So changing `BUCKET_MINUTES` doesn't invalidate days already recorded — don't hardcode 288 on either side.

This format exists because the old per-sample shape (`{"ts","status"}` per component per bucket) was the Lambda's binding memory constraint: at steady state it reached 907k entries / 52.6 MB of JSON, and its read-modify-write measured **421.8 MB** of peak interpreter memory (vs **4.4 MB** now) — that is what OOM-killed the prober on 28/07/2026 and forced `memory_size` 256 → 1024. Nothing the UI draws was lost, because `aggregateDaily()` only ever rendered per-day counts.

`uptime.json` is written **gzipped** (`Content-Encoding: gzip`, `Cache-Control: max-age=60`); the read-back detects the gzip magic byte so the cycle round-trips, and the local dev server stores it decompressed on disk. `status.json` stays uncompressed and `no-cache`. A read or parse failure **raises** rather than falling back to an empty dict — publishing `{}` would blank every uptime bar with no trace, while raising is recoverable (status.json is already written and the alert already sent by that point, and `prober-errors` pages).

The legacy `data/history.json` is **frozen, not deleted** — an already-open browser tab runs old `app.js` that it will never reload, and feeding it the new shape throws mid-render *after* the component containers are cleared, leaving a green banner over an empty page. Converting is [scripts/backfill_uptime.py](scripts/backfill_uptime.py) (one-shot, run **before** deploying; dry-run by default). Retention prunes across **every** key, not just the current config's — the old code only pruned ids present in the snapshot, so four renamed `api-lsst-*` keys were pinned in the object forever.

## Adding / changing probes

Edit [lambda/config.json](lambda/config.json). Each component needs `id` (stable — used as the history key), `label`, `group`, `url`, `method`, `expected_status`. Optional per-endpoint overrides: `latency_degraded_ms`, `latency_outage_ms`, `timeout_s` (any omitted key falls back to the global `thresholds`). Run `python lambda/prober.py` locally to dry-run — it prints a slowest-first latency table for calibrating those overrides.

Groups are `apis` (ZTF), `apis_lsst` (multi-survey / LSST), `tap` (TAP / data access), or `frontends`. Both object pages are HTMX-rendered by the same `multisurveys-apis` services, just mounted per survey: the **LSST** site loads `api-lsst.alerce.online/{service}_api/htmx/*`; the **ZTF** site loads `api.alerce.online/v2/{service}/htmx/*` (e.g. `/v2/object_details/htmx/object/{oid}`, `/v2/xmatch/htmx/crossmatch/{oid}`) — each service is its own target group. We probe these real `htmx/*` render endpoints (verified from a browser HAR, not the stale `ztf_explorer`/`alerts/v1` source) rather than `openapi.json` liveness. The older `alerts/v1`/`ztf/v1` REST probes are kept as backend coverage alongside the htmx ones. Adding a **new** group value also requires a matching container `<div id="<group>-rows">` in [frontend/index.html](frontend/index.html) and an entry in the `groups` map in [frontend/app.js](frontend/app.js).

### TAP probes (`tap` group)

`tap.alerce.online` is a **GAVO DaCHS** instance running alongside the other web services, but its Postgres is **on-prem, reached over a site-to-site VPN** — which is why every ADQL probe carries a ~0.5 s floor before any query work, and why the `tap` probes are the only ones whose latency depends on a link this project can't see.

Non-obvious constraints, measured 28/07/2026:

- **Never probe `/` or `/tap/`** — both return **502** from the nginx sidecar (nothing is mounted at the root), so a probe there reports the service down while it is serving queries fine. This also breaks the "table/column browser at `tap.alerce.online`" that the TAP-migration comms point users to; it's a live web-services issue, not a status-site one. Working paths are `/tap/availability`, `/tap/capabilities`, `/tap/tables`, `/tap/sync` and `/__system__/adql/query/form`.
- **`/tap/availability` is the liveness probe** — the IVOA VOSI endpoint, answered by DaCHS without touching the database (~0.55 s). It reports `upSince`, useful when reading a restart after the fact.
- **DaCHS returns HTTP 500 on a broken query path** (bad table, bad ADQL, missing `QUERY`), not a 200 carrying a VOTable error. So the ADQL probes detect a dead database on status code alone — no body parsing needed, and `expected_status: [200]` is sufficient.
- **The ADQL probes must stay trivially cheap.** The TAP service runs **one replica with no Kubernetes probes**, so the status page must not be a load source: each query is an indexed `TOP 1`/`TOP 5` with `FORMAT=csv`, returning <100 B. Never probe with an unbounded scan — `SELECT TOP 1 oid FROM alerce_tap.object` with no `WHERE` takes **~4.9 s** versus ~1.5 s for the same query keyed by `oid`.
- **Latency baseline ~1.0 s** for all three ADQL probes (indexed ZTF lookup, indexed multi-survey lookup, `CONTAINS`/`CIRCLE` cone search — the spatial path is no more expensive than the keyed one), measured over 12 days to 05/08/2026: p50 0.98-0.99 s, p95 1.08-1.76 s, p99 1.87-3.33 s. The body is tight but the tail is **not** Gaussian — p99.9 reaches 7.1-7.7 s, so the single-replica burst concern is real even though σ on the body is small. Their original `6000 / 15000 / 20 s` guess has since been **recalibrated away**: all three now run on the global `4000 / 10000 / 15 s`, which flags the p99.9 burst (correctly — 7 s against a 1 s baseline *is* degraded) at ~0.4 % of runs, and replaces an `latency_outage_ms` of 15 s that the observed max of 8.6 s made **unreachable**.
- The probes reuse the **same objects as the existing API probes** (`ZTF18aaawjhl`, LSST `170591527609303944`), so a TAP-vs-API discrepancy is a genuine difference in the serving path, not a difference in the object.

## Posting an incident

Use [scripts/incident.py](scripts/incident.py) (`open` / `update` subcommands) to edit [incidents/incidents.json](incidents/incidents.json), then PR + merge. The script handles UTC timestamps, validates status against the incident vs. maintenance vocabularies, and auto-fills `resolved_at` on terminal status. Format documented in `README.md`. The deploy workflow uploads it to the data bucket; no Lambda involvement.

## Tests

`pytest lambda/tests/ -v`. The deploy workflow blocks on tests passing. When changing prober logic, update / add tests in [lambda/tests/test_prober.py](lambda/tests/test_prober.py).

## Things to verify before claiming a frontend change works

There's no headless test for the frontend. For UI work, run `python scripts/dev_server.py` and load `http://localhost:8000` — the dev server runs the real prober every 60 s and serves `data/*` locally. If you can't run a browser, say so explicitly rather than claiming success.

## Security constraints (read before touching AWS or CI)

**This repo is public.** Nothing identifying about the ALeRCE AWS environment may be committed: no account IDs, ARNs with account IDs, Route53 zone IDs, bucket names tied to other projects, IAM role names, access keys, or internal hostnames. The only ARNs allowed in the repo are AWS-managed policy ARNs (e.g. `arn:aws:iam::aws:policy/...`), which are identical for every AWS user. Everything else lives in GitHub Actions secrets.

**The ALeRCE AWS account holds valuable data and has a large budget.** Treat any new AWS permission as a blast-radius decision, not a convenience. Specifically:

- **Scope IAM policies to `alerce-status-*` resources.** All Terraform-created resources use `local.name_prefix = "alerce-status-${var.environment}"`. The deploy role's permissions must use that prefix so this project cannot touch other ALeRCE infrastructure. The prober's `sns:Publish` grant is scoped to its own alerts topic ARN, and the out-of-band `alerce-status-boundary` permits `sns:Publish` **only** on `arn:aws:sns:*:<acct>:alerce-status-*` — keep both scoped if you touch alerting.
- **No `*:*` permissions.** Don't grant `s3:*` on `*`, `iam:*` on `*`, or `PowerUserAccess` — even temporarily. If a Terraform resource needs a new permission, add the specific action scoped to the specific resource.
- **OIDC trust must be scoped.** The GitHub OIDC role trusts only `repo:alercebroker/alerce-status-website` with `environment:production` or `environment:staging`. Don't widen this to `*` refs or other repos.
- **Don't touch resources outside this project.** No edits to other ALeRCE buckets, Lambdas, IAM roles, or Route53 records (other than the validation/alias records this project owns under `alerce.online`). If a fix seems to require it, stop and ask.

**Deploys are automatic on merge to `main`** — there is no manual approval step in the `production` environment, so merge authorization *is* deploy authorization. `main` requires a passing `test` check and an approving review from a repo **admin** (enforced via [.github/CODEOWNERS](.github/CODEOWNERS)); repo admins bypass branch protection and can self-merge. Widening who can merge to `main` — or who is listed as a code owner — widens who can deploy to production.

**Before any AWS-affecting action, state the blast radius.** What resources can this touch? What's the cost ceiling if it goes wrong? What does it depend on outside this project? Ask before applying if the answer is "I'm not sure."

**Public Actions logs.** Workflow logs on a public repo are world-readable. `aws-actions/configure-aws-credentials` masks the account ID automatically, but be aware that any new step printing AWS responses may need `--output text --query '<field>'` to avoid leaking resource ARNs from unrelated accounts.
