"""
Lambda handler: probes public ALeRCE endpoints, maps results to
operational/degraded/outage, and writes status.json + history.json to S3.

Non-operational components include probe_url, http_code, and response_ms
for diagnostic display. Operational components omit these fields.
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
HISTORY_DAYS = 90
BUCKET_MINUTES = 5  # roll up checks into 5-min windows for the history graph
DEFAULT_TIMEOUT_S = 15  # per-endpoint override via component["timeout_s"]


def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path) as f:
        return json.load(f)


def _effective_thresholds(component, thresholds):
    """Resolve latency/timeout limits for a component.

    A component may override any of `latency_degraded_ms`, `latency_outage_ms`
    or `timeout_s` inline; anything not set falls back to the global defaults.
    This lets legitimately slow endpoints (the all-catalog crossmatch ~20 s, the
    object-ranking query ~13 s) carry their own calibrated limits instead of
    being flagged by the fast-endpoint defaults.
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


def build_snapshot(probe_results, config):
    """Build the status.json dict from probe results."""
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

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
        if status != "operational":
            entry["probe_url"] = r["url"] if r else comp["url"]
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


def update_history(s3, snapshot):
    """
    Read existing history.json, append a bucket for this run,
    prune to 90 days, write back (gzipped, with a 60 s cache TTL).

    History format:
    {
      "component_id": [
        {"ts": "ISO", "status": "operational"},
        ...
      ]
    }
    """
    try:
        obj = s3.get_object(Bucket=BUCKET, Key="data/history.json")
        raw = obj["Body"].read()
        if raw[:2] == b"\x1f\x8b":  # gzip magic number — object is stored gzipped
            raw = gzip.decompress(raw)
        history = json.loads(raw)
    except s3.exceptions.NoSuchKey:
        history = {}
    except Exception:
        history = {}

    now = datetime.now(timezone.utc)
    # Bucket key: round down to nearest BUCKET_MINUTES window
    bucket_ts = now.replace(
        minute=(now.minute // BUCKET_MINUTES) * BUCKET_MINUTES,
        second=0,
        microsecond=0,
    ).isoformat(timespec="seconds")

    cutoff = (now - timedelta(days=HISTORY_DAYS)).isoformat(timespec="seconds")

    for comp in snapshot["components"]:
        cid = comp["id"]
        buckets = history.get(cid, [])
        # Remove any existing entry for this time bucket (idempotent re-runs)
        buckets = [b for b in buckets if b["ts"] != bucket_ts]
        buckets.append({"ts": bucket_ts, "status": comp["status"]})
        # Prune old entries
        buckets = [b for b in buckets if b["ts"] >= cutoff]
        history[cid] = sorted(buckets, key=lambda b: b["ts"])

    # Store gzipped: this payload is highly repetitive (~5% of raw size) and
    # exceeds CloudFront's 10 MB auto-compression limit, so we compress at the
    # origin. Browsers decompress transparently via Content-Encoding. The read
    # above detects the gzip magic number, so re-runs round-trip correctly.
    body = json.dumps(history, separators=(",", ":")).encode("utf-8")
    s3.put_object(
        Bucket=BUCKET,
        Key="data/history.json",
        Body=gzip.compress(body, mtime=0),
        ContentType="application/json",
        ContentEncoding="gzip",
        CacheControl="max-age=60",
    )


def handler(event, context):
    config = load_config()
    thresholds = config["thresholds"]

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
    snapshot = build_snapshot(results, config)

    import boto3
    s3 = boto3.client("s3")

    s3.put_object(
        Bucket=BUCKET,
        Key="data/status.json",
        Body=json.dumps(snapshot, separators=(",", ":")),
        ContentType="application/json",
        CacheControl="no-cache",
    )

    update_history(s3, snapshot)

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
