"""
Local dev server: probes endpoints and serves the status website at localhost:8000.

  /        → frontend/
  /data/*  → data/   (written by the prober every 60 s)

Usage:
  python dev_server.py
  PORT=9000 python dev_server.py
  DEMO=1 python dev_server.py    # serve generated demo data (fresh, dated to now), skip the live prober
"""

import gzip
import json
import os
import shutil
import sys
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).parent.parent
FRONTEND_DIR = ROOT / "frontend"
DATA_DIR = ROOT / "data"
INCIDENTS_SRC = ROOT / "incidents" / "incidents.json"
DEMO = os.environ.get("DEMO") == "1"

sys.path.insert(0, str(ROOT / "lambda"))
import prober

PROBE_INTERVAL = 60


class _Body:
    """Minimal stand-in for a boto3 StreamingBody (the prober calls .read())."""

    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class _LocalS3:
    """Fake boto3 S3 client that reads/writes local files under DATA_DIR."""

    class exceptions:
        class NoSuchKey(Exception):
            pass

    def get_object(self, **kw):
        path = ROOT / kw["Key"]
        if not path.exists():
            raise self.exceptions.NoSuchKey()
        # Must be .read()-able: returning bare bytes here raised AttributeError
        # inside update_history, which the prober then swallowed into an empty
        # history -- so the dev server silently reset the accumulated series on
        # every run and could never exercise multi-day rendering.
        return {"Body": _Body(path.read_bytes())}

    def put_object(self, **kw):
        body = kw["Body"]
        if isinstance(body, str):
            body = body.encode()
        # The prober gzips uptime.json for S3/CloudFront; the local static
        # server sends no Content-Encoding, so store it decompressed on disk.
        if kw.get("ContentEncoding") == "gzip":
            body = gzip.decompress(body)
        path = ROOT / kw["Key"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)


def run_prober():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import datetime, timezone

    # One clock reading for the run, as the Lambda handler does -- keeps the local
    # path exercising the same code shape as production.
    now = datetime.now(timezone.utc)

    config = prober.load_config()
    results = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {
            pool.submit(prober.probe, c, config["thresholds"]): c
            for c in config["components"]
        }
        for f in as_completed(futs):
            results.append(f.result())

    snapshot = prober.build_snapshot(results, config, now=now)
    s3 = _LocalS3()
    s3.put_object(
        Bucket="local",
        Key="data/status.json",
        Body=json.dumps(snapshot, separators=(",", ":")),
        ContentType="application/json",
    )
    prober.update_history(s3, snapshot, now=now)
    print(f"[prober] {snapshot['updated_at']} — {snapshot['status']}")


def prober_loop():
    while True:
        time.sleep(PROBE_INTERVAL)
        try:
            run_prober()
        except Exception as e:
            print(f"[prober] error: {e}")


def demo_loop():
    import demo_data
    while True:
        time.sleep(PROBE_INTERVAL)
        try:
            demo_data.tick(DATA_DIR)
        except Exception as e:
            print(f"[demo] error: {e}")


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path.startswith("/data/") or path == "/data":
            rel = path.lstrip("/")
            return str(ROOT / rel) if rel else str(DATA_DIR)
        rel = path.lstrip("/")
        return str(FRONTEND_DIR / rel) if rel else str(FRONTEND_DIR)

    def log_message(self, fmt, *args):
        print(f"[http] {fmt % args}")


if __name__ == "__main__":
    DATA_DIR.mkdir(exist_ok=True)

    if DEMO:
        import demo_data
        demo_data.write_demo(DATA_DIR)
        print("[init] DEMO mode — generated fresh demo data anchored to now (prober disabled)")
        threading.Thread(target=demo_loop, daemon=True).start()
    else:
        if INCIDENTS_SRC.exists():
            shutil.copy(INCIDENTS_SRC, DATA_DIR / "incidents.json")
            print("[init] copied incidents/incidents.json → data/incidents.json")

        print("[init] running initial probe (may take a few seconds)…")
        try:
            run_prober()
        except Exception as e:
            print(f"[init] prober error: {e}")

        threading.Thread(target=prober_loop, daemon=True).start()

    port = int(os.environ.get("PORT", 8000))
    print(f"[server] http://localhost:{port}")
    HTTPServer(("", port), Handler).serve_forever()
