"""
Local dev server: probes endpoints and serves the status website at localhost:8000.

  /        → frontend/
  /data/*  → data/   (written by the prober every 60 s)

Usage:
  python dev_server.py
  PORT=9000 python dev_server.py
"""

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

sys.path.insert(0, str(ROOT / "lambda"))
import prober

PROBE_INTERVAL = 60


class _LocalS3:
    """Fake boto3 S3 client that reads/writes local files under DATA_DIR."""

    class exceptions:
        class NoSuchKey(Exception):
            pass

    def get_object(self, **kw):
        path = ROOT / kw["Key"]
        if not path.exists():
            raise self.exceptions.NoSuchKey()
        return {"Body": path.read_bytes()}

    def put_object(self, **kw):
        body = kw["Body"]
        path = ROOT / kw["Key"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body.encode() if isinstance(body, str) else body)


def run_prober():
    from concurrent.futures import ThreadPoolExecutor, as_completed

    config = prober.load_config()
    results = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {
            pool.submit(prober.probe, c, config["thresholds"]): c
            for c in config["components"]
        }
        for f in as_completed(futs):
            results.append(f.result())

    snapshot = prober.build_snapshot(results, config)
    s3 = _LocalS3()
    s3.put_object(
        Bucket="local",
        Key="status.json",
        Body=json.dumps(snapshot, separators=(",", ":")),
        ContentType="application/json",
    )
    prober.update_history(s3, snapshot)
    print(f"[prober] {snapshot['updated_at']} — {snapshot['status']}")


def prober_loop():
    while True:
        time.sleep(PROBE_INTERVAL)
        try:
            run_prober()
        except Exception as e:
            print(f"[prober] error: {e}")


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
