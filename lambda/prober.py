"""
Lambda handler: probes public ALeRCE endpoints, maps results to
operational/degraded/outage, and writes status.json + uptime.json to S3.

Non-operational components include probe_url, http_code, and response_ms
for diagnostic display. Operational components omit these fields.

On each run it also compares the new snapshot against the previously published
status.json and, if any component got worse or fully recovered, publishes one
aggregated SNS alert (rich message with HTTP code / latency). Alerting is
best-effort — a notification failure never blocks the status/history writes.
"""

import gzip
import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

BUCKET = os.environ.get("STATUS_BUCKET", "")
ALERT_TOPIC_ARN = os.environ.get("ALERT_TOPIC_ARN", "")  # empty → alerting disabled
HISTORY_DAYS = 90
BUCKET_MINUTES = 5  # roll up checks into 5-min windows for the history graph
DEFAULT_TIMEOUT_S = 15  # per-endpoint override via component["timeout_s"]
STATUS_PAGE_URL = "https://status.alerce.online"

# Uptime history: one fixed-width string per component per UTC day, one character
# per BUCKET_MINUTES slot (see update_history). Published under its own key --
# NOT the legacy data/history.json, whose per-sample array shape an already-open
# browser tab would choke on (see docs in update_history).
UPTIME_KEY = "data/uptime.json"
MINUTES_PER_DAY = 24 * 60
SLOTS_PER_DAY = MINUTES_PER_DAY // BUCKET_MINUTES  # 288
STATUS_CHARS = {"operational": "o", "degraded": "d", "outage": "x"}
NO_DATA_CHAR = "-"

# Severity ranking for state-change detection. We alert when a component gets
# WORSE (rank increases above operational, incl. degraded→outage escalation) and
# when it fully RECOVERS (returns to operational). Partial recovery
# (outage→degraded) does not re-page.
_SEVERITY = {"operational": 0, "degraded": 1, "outage": 2}


def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path) as f:
        return json.load(f)


def _effective_thresholds(component, thresholds):
    """Resolve latency/timeout limits for a component.

    A component may override any of `latency_degraded_ms`, `latency_outage_ms`
    or `timeout_s` inline; anything not set falls back to the global defaults.
    This lets legitimately slow endpoints (the unfiltered LSST object list ~10 s,
    the ZTF object-page light curve ~3.3 s) carry their own calibrated limits
    instead of being flagged by the fast-endpoint defaults.
    """
    return (
        component.get("latency_degraded_ms", thresholds["latency_degraded_ms"]),
        component.get("latency_outage_ms", thresholds["latency_outage_ms"]),
        component.get("timeout_s", thresholds.get("timeout_s", DEFAULT_TIMEOUT_S)),
    )


def probe(component, thresholds):
    """Return a dict with id, status, label, url, http_code, response_ms."""
    url = component["url"]
    expected = set(component.get("expected_status", [200]))
    degraded_ms, outage_ms, timeout_s = _effective_thresholds(component, thresholds)
    start = time.monotonic()
    try:
        req = urllib.request.Request(url, method=component.get("method", "GET"))
        req.add_header("User-Agent", "alerce-status-prober/1.0")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            code = resp.status
        elapsed_ms = (time.monotonic() - start) * 1000
    except urllib.error.HTTPError as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        code = e.code
    except Exception:
        return {"id": component["id"], "status": "outage", "label": component["label"],
                "url": url, "http_code": None, "response_ms": None}

    if code not in expected:
        status = "outage"
    elif elapsed_ms >= outage_ms:
        status = "outage"
    elif elapsed_ms >= degraded_ms:
        status = "degraded"
    else:
        status = "operational"

    return {"id": component["id"], "status": status, "label": component["label"],
            "url": url, "http_code": code, "response_ms": round(elapsed_ms)}


def log_response_times(results):
    """Emit per-component latencies to CloudWatch Logs (one JSON line each).

    These raw response times are deliberately kept OUT of the public status.json
    (operational components expose no metrics — see README/CLAUDE.md), but we log
    them privately so thresholds can be tuned later from CloudWatch Logs Insights.
    """
    for r in results:
        print(json.dumps({
            "metric": "probe_latency",
            "id": r["id"],
            "status": r["status"],
            "http_code": r["http_code"],
            "response_ms": r["response_ms"],
        }))


def _worst(statuses):
    order = ["outage", "degraded", "operational"]
    for s in order:
        if s in statuses:
            return s
    return "operational"


def _overall_label(status):
    return {
        "operational": "All systems operational",
        "degraded": "Some services degraded",
        "outage": "Service disruption detected",
    }[status]


def _group_label(status):
    return {
        "operational": "Operational",
        "degraded": "Degraded performance",
        "outage": "Service outage",
    }[status]


def build_snapshot(probe_results, config, now=None):
    """Build the status.json dict from probe results.

    `now` is injectable so the caller can stamp the snapshot and the history slot
    from a single clock reading — two independent datetime.now() calls minutes
    apart can land on opposite sides of midnight.
    """
    now_iso = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")

    by_id = {r["id"]: r for r in probe_results}

    components = []
    for comp in config["components"]:
        cid = comp["id"]
        r = by_id.get(cid)
        status = r["status"] if r else "outage"
        label = r["label"] if r else comp["label"]
        entry = {
            "id": cid,
            "label": label,
            "group": comp["group"],
            "status": status,
            "status_label": _group_label(status),
            "checked_at": now_iso,
        }
        if comp.get("description"):
            entry["description"] = comp["description"]
        # probe_url is public (all probed endpoints are public APIs), so expose it
        # for every component — the UI shows it in a per-row expander.
        entry["probe_url"] = r["url"] if r else comp["url"]
        # http_code / response_ms stay diagnostic-only: the public payload must not
        # carry per-probe metrics for healthy components.
        if status != "operational":
            if r and r["http_code"] is not None:
                entry["http_code"] = r["http_code"]
            if r and r["response_ms"] is not None:
                entry["response_ms"] = r["response_ms"]
        components.append(entry)

    overall_status = _worst({c["status"] for c in components})

    return {
        "status": overall_status,
        "status_label": _overall_label(overall_status),
        "updated_at": now_iso,
        "components": components,
    }


def _slot_index(now, slots_per_day):
    """Which slot of the UTC day `now` falls in, for a row of `slots_per_day` chars.

    Derived from the row's own width rather than BUCKET_MINUTES, so days already
    recorded at one granularity keep rendering correctly if BUCKET_MINUTES changes.
    """
    return (now.hour * 60 + now.minute) * slots_per_day // MINUTES_PER_DAY


def _read_uptime(s3):
    """Return the stored uptime object, or {} on first-ever run.

    Deliberately does NOT swallow read/parse errors into an empty dict: doing that
    would republish an empty history and blank every uptime bar on the site with no
    trace. Failing loudly is recoverable -- status.json is already written and the
    alert already sent by the time we get here (see handler), and the prober-errors
    alarm pages. A silent wipe is not recoverable.
    """
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=UPTIME_KEY)
    except s3.exceptions.NoSuchKey:
        return {}
    try:
        raw = obj["Body"].read()
        if raw[:2] == b"\x1f\x8b":  # gzip magic number — object is stored gzipped
            raw = gzip.decompress(raw)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"uptime object is {type(data).__name__}, expected dict")
        return data
    except Exception as e:
        print(json.dumps({"metric": "uptime_read_error", "error": repr(e)}))
        raise


def update_history(s3, snapshot, now=None):
    """
    Record this run's statuses into the uptime object and write it back (gzipped,
    with a 60 s cache TTL).

    Format -- one fixed-width string per component per UTC day, one character per
    BUCKET_MINUTES slot, position implying time-of-day (slot 0 = 00:00):

    {
      "component_id": {
        "2026-07-30": "ooooodddddoooo...----"   # o operational, d degraded,
        ...                                     # x outage, - no check recorded
      }
    }

    Two properties this shape buys, both load-bearing:

    1. Writing is *positional* (`row[:i] + char + row[i+1:]`), so it is idempotent
       by construction. A duplicate invocation in the same slot -- which EventBridge
       can genuinely cause, since it guarantees at-least-once delivery -- rewrites
       the same character and changes nothing. Per-day counters would increment
       twice and skew that day's uptime permanently, with no way to detect it.
    2. There is no day-rollover step to get wrong. The day key and the slot index
       both fall out of one timestamp, so they cannot disagree, and a Lambda outage
       of any length just leaves '-' slots behind instead of needing to be
       reconciled later.

    Storing per-day strings rather than per-sample records is what keeps this
    affordable: the frontend only ever renders per-day counts (see aggregateDaily),
    so the ~900k {"ts","status"} dicts the old format accumulated -- ~50 MB of JSON
    and ~430 MB of interpreter memory at steady state -- reduce to ~1 MB and ~15 MB
    with nothing the UI draws being lost.
    """
    now = now or datetime.now(timezone.utc)
    history = _read_uptime(s3)

    day = now.date().isoformat()
    for comp in snapshot["components"]:
        days = history.setdefault(comp["id"], {})
        if not isinstance(days, dict):
            days = history[comp["id"]] = {}
        row = days.get(day)
        # Rebuild the row unless it is a usable width (a whole number of slots).
        if not isinstance(row, str) or not row or MINUTES_PER_DAY % len(row):
            row = NO_DATA_CHAR * SLOTS_PER_DAY
        idx = _slot_index(now, len(row))
        char = STATUS_CHARS.get(comp["status"], NO_DATA_CHAR)
        days[day] = row[:idx] + char + row[idx + 1:]

    # Prune across EVERY key, not just this run's components: a renamed or removed
    # component would otherwise keep its rows forever, since nothing else ever
    # touches its entry. Iterating all keys also gives the intended behaviour for a
    # probe temporarily pulled from config -- it keeps HISTORY_DAYS of history, then
    # ages out on its own.
    cutoff = (now - timedelta(days=HISTORY_DAYS)).date().isoformat()
    for cid in list(history):
        days = {d: row for d, row in history[cid].items() if d >= cutoff}
        if days:
            history[cid] = days
        else:
            del history[cid]

    # Store gzipped: the payload is highly repetitive, so this is a large win even
    # at the new size. Browsers decompress transparently via Content-Encoding, and
    # the read above detects the gzip magic number so re-runs round-trip correctly.
    body = json.dumps(history, separators=(",", ":")).encode("utf-8")
    s3.put_object(
        Bucket=BUCKET,
        Key=UPTIME_KEY,
        Body=gzip.compress(body, mtime=0),
        ContentType="application/json",
        ContentEncoding="gzip",
        CacheControl="max-age=60",
    )


def read_prev_status(s3):
    """Return {id: status} from the currently-stored status.json, or None.

    None means "no baseline" (first-ever run or an unreadable object) — the
    caller then skips alerting so a fresh deploy doesn't fire on everything.
    """
    try:
        obj = s3.get_object(Bucket=BUCKET, Key="data/status.json")
        data = json.loads(obj["Body"].read())
        return {c["id"]: c["status"] for c in data.get("components", [])}
    except s3.exceptions.NoSuchKey:
        return None
    except Exception:
        return None


def compute_transitions(prev_status, snapshot):
    """Diff the new snapshot against prev_status → list of alertable transitions.

    Emits a transition when a component gets worse (rank increases above
    operational) or fully recovers (returns to operational). Components with no
    baseline (prev_status is None, or a newly-added component absent from it) are
    established silently. Partial recovery (outage→degraded) is intentionally not
    reported.
    """
    if not prev_status:
        return []
    transitions = []
    for comp in snapshot["components"]:
        prev = prev_status.get(comp["id"])
        if prev is None:
            continue  # newly-added component — set a baseline, don't alert
        new = comp["status"]
        if new == prev:
            continue
        prev_rank = _SEVERITY.get(prev, 0)
        new_rank = _SEVERITY.get(new, 0)
        if new_rank > prev_rank and new_rank > 0:
            kind = "down"       # worse / escalation
        elif new_rank == 0 and prev_rank > 0:
            kind = "recovered"  # all-clear
        else:
            continue            # partial recovery — don't re-page
        transitions.append({
            "id": comp["id"], "label": comp["label"], "kind": kind,
            "prev": prev, "new": new,
            "http_code": comp.get("http_code"),
            "response_ms": comp.get("response_ms"),
        })
    return transitions


def _subject_names(transitions):
    labels = [t["label"] for t in transitions]
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return f"{labels[0]} and {len(labels) - 1} more"


def format_alert(transitions, overall_status):
    """Build (subject, body) for an SNS alert. Subject is ASCII (SNS requirement)."""
    downs = [t for t in transitions if t["kind"] == "down"]
    ups = [t for t in transitions if t["kind"] == "recovered"]

    if downs:
        word = "down" if any(t["new"] == "outage" for t in downs) else "degraded"
        subject = f"ALeRCE Status: {_subject_names(downs)} {word}"
        if ups:
            subject += f" (+{len(ups)} recovered)"
    else:
        subject = f"ALeRCE Status: {_subject_names(ups)} recovered"
    # SNS rejects non-ASCII subjects (which would silently drop the alert); labels
    # are ASCII today, but strip defensively.
    subject = subject.encode("ascii", "ignore").decode()[:100]

    lines = []
    for t in downs:
        icon = "\U0001F534" if t["new"] == "outage" else "\U0001F7E1"  # 🔴 / 🟡
        detail = []
        if t["http_code"] is not None:
            detail.append(f"HTTP {t['http_code']}")
        if t["response_ms"] is not None:
            detail.append(f"{t['response_ms']} ms")
        suffix = f" ({', '.join(detail)})" if detail else ""
        lines.append(f"{icon} {t['label']}: {t['prev']} → {t['new']}{suffix}")
    for t in ups:
        lines.append(f"\U0001F7E2 {t['label']}: {t['prev']} → {t['new']}")  # 🟢

    body = "\n".join(lines) + f"\n\nOverall: {overall_status}\n{STATUS_PAGE_URL}"
    return subject, body


def maybe_alert(sns, topic_arn, prev_status, snapshot):
    """Best-effort: publish one aggregated SNS alert for this run's transitions.

    Never raises — a notification failure must not break the status write. Returns
    the transitions that triggered an alert (empty if none / disabled / failed).
    """
    if not topic_arn:
        return []
    try:
        transitions = compute_transitions(prev_status, snapshot)
        if not transitions:
            return []
        subject, body = format_alert(transitions, snapshot["status"])
        sns.publish(TopicArn=topic_arn, Subject=subject, Message=body)
        return transitions
    except Exception as e:
        print(json.dumps({"metric": "alert_error", "error": repr(e)}))
        return []


def handler(event, context):
    config = load_config()
    thresholds = config["thresholds"]

    # One clock reading for the whole run, taken BEFORE probing. Two reasons:
    # the history slot then reflects when the checks actually ran rather than when
    # the write finished (the run takes ~50 s, up to the 240 s timeout), which keeps
    # consecutive runs landing in consecutive slots instead of drifting by however
    # long the probes took; and status.json's updated_at cannot end up on the other
    # side of midnight from the slot it is recorded in.
    now = datetime.now(timezone.utc)

    # Probe endpoints sequentially: many queries fan out to different EKS pods
    # but funnel into the same pgbouncer/Postgres, so we avoid bursting the
    # shared connection pool with simultaneous DB-backed requests.
    results = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = {
            pool.submit(probe, comp, thresholds): comp
            for comp in config["components"]
        }
        for future in as_completed(futures):
            results.append(future.result())

    log_response_times(results)  # private latency log → CloudWatch
    snapshot = build_snapshot(results, config, now=now)

    import boto3
    s3 = boto3.client("s3")

    # Read the previously published statuses BEFORE overwriting status.json, so
    # we can diff for state changes after the write.
    prev_status = read_prev_status(s3)

    s3.put_object(
        Bucket=BUCKET,
        Key="data/status.json",
        Body=json.dumps(snapshot, separators=(",", ":")),
        ContentType="application/json",
        CacheControl="no-cache",
    )

    # Alert before update_history, not after. Alerting is still best-effort --
    # maybe_alert swallows its own errors, so it cannot block the history write.
    # But it must not sit *downstream* of it: update_history is by far the most
    # memory-hungry step, and when it died (OOM) the alert never got sent while
    # status.json above had already been overwritten. The retry then read that
    # new state as its baseline, so the transition was silently lost forever.
    if ALERT_TOPIC_ARN:
        maybe_alert(boto3.client("sns"), ALERT_TOPIC_ARN, prev_status, snapshot)

    update_history(s3, snapshot, now=now)

    return {"statusCode": 200, "overall": snapshot["status"]}


# Allow local dry-run: python prober.py
if __name__ == "__main__":
    import sys

    class _FakeS3:
        class exceptions:
            class NoSuchKey(Exception):
                pass
        def get_object(self, **kw):
            raise self.exceptions.NoSuchKey()
        def put_object(self, **kw):
            print(f"  [s3 PUT] {kw['Key']} ({len(kw['Body'])} bytes)")

    os.environ.setdefault("STATUS_BUCKET", "dry-run")
    config = load_config()
    results = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = {pool.submit(probe, c, config["thresholds"]): c for c in config["components"]}
        for f in as_completed(futures):
            results.append(f.result())
    # Latency table (slowest first) — handy for calibrating per-endpoint thresholds
    print("=== probe latencies (local dry-run) ===")
    for r in sorted(results, key=lambda r: (r["response_ms"] is None, -(r["response_ms"] or 0))):
        print(f"  {r['id']:<28} {r['status']:<12} {str(r['response_ms']):>7} ms  HTTP {r['http_code']}")
    snapshot = build_snapshot(results, config)
    update_history(_FakeS3(), snapshot)
    print(json.dumps(snapshot, indent=2))
