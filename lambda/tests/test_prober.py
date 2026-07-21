"""Unit tests for prober.py — run with: pytest lambda/tests/"""

import gzip
import json
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import prober

THRESHOLDS = {"latency_degraded_ms": 2000, "latency_outage_ms": 10000}

CONFIG = {
    "thresholds": THRESHOLDS,
    "components": [
        {"id": "api-object", "label": "Object API", "group": "apis",
         "url": "https://example.com", "method": "GET", "expected_status": [200]},
        {"id": "api-lightcurve", "label": "Lightcurve API", "group": "apis",
         "url": "https://example.com", "method": "GET", "expected_status": [200]},
    ],
}


def _make_result(cid, label, status, code=200, ms=100):
    return {"id": cid, "status": status, "label": label,
            "url": "https://example.com", "http_code": code, "response_ms": ms}


# --- field structure ---

def test_operational_components_have_no_diagnostic_fields():
    """Operational components must not expose probe_url, http_code, or response_ms."""
    results = [_make_result("api-object", "Object API", "operational"),
               _make_result("api-lightcurve", "Lightcurve API", "operational")]
    snapshot = prober.build_snapshot(results, CONFIG)
    for comp in snapshot["components"]:
        assert "probe_url" not in comp
        assert "http_code" not in comp
        assert "response_ms" not in comp


def test_non_operational_components_have_diagnostic_fields():
    """Degraded/outage components must expose probe_url, http_code, and response_ms."""
    results = [_make_result("api-object", "Object API", "operational"),
               _make_result("api-lightcurve", "Lightcurve API", "degraded", code=200, ms=3000)]
    snapshot = prober.build_snapshot(results, CONFIG)
    degraded = next(c for c in snapshot["components"] if c["id"] == "api-lightcurve")
    assert degraded["probe_url"] == "https://example.com"
    assert degraded["http_code"] == 200
    assert degraded["response_ms"] == 3000


def test_description_propagates_to_snapshot():
    """Component descriptions in config should round-trip into the snapshot."""
    config = {
        "thresholds": THRESHOLDS,
        "components": [
            {"id": "api-object", "label": "Object API", "group": "apis",
             "description": "Object metadata API.",
             "url": "https://example.com", "method": "GET", "expected_status": [200]},
            {"id": "api-no-desc", "label": "No Desc", "group": "apis",
             "url": "https://example.com", "method": "GET", "expected_status": [200]},
        ],
    }
    results = [_make_result("api-object", "Object API", "operational"),
               _make_result("api-no-desc", "No Desc", "operational")]
    snapshot = prober.build_snapshot(results, config)
    by_id = {c["id"]: c for c in snapshot["components"]}
    assert by_id["api-object"]["description"] == "Object metadata API."
    assert "description" not in by_id["api-no-desc"]


def test_snapshot_fields():
    results = [_make_result("api-object", "Object API", "operational"),
               _make_result("api-lightcurve", "Lightcurve API", "operational")]
    snapshot = prober.build_snapshot(results, CONFIG)

    assert "status" in snapshot
    assert "status_label" in snapshot
    assert "updated_at" in snapshot
    assert "components" in snapshot

    for comp in snapshot["components"]:
        required = {"id", "label", "group", "status", "status_label", "checked_at"}
        assert required.issubset(comp.keys())


# --- status mapping ---

def test_worst_outage_wins():
    results = [_make_result("api-object", "Object API", "operational"),
               _make_result("api-lightcurve", "Lightcurve API", "outage", code=500, ms=100)]
    snapshot = prober.build_snapshot(results, CONFIG)
    assert snapshot["status"] == "outage"


def test_worst_degraded_beats_operational():
    results = [_make_result("api-object", "Object API", "operational"),
               _make_result("api-lightcurve", "Lightcurve API", "degraded", code=200, ms=3000)]
    snapshot = prober.build_snapshot(results, CONFIG)
    assert snapshot["status"] == "degraded"


def test_all_operational():
    results = [_make_result("api-object", "Object API", "operational"),
               _make_result("api-lightcurve", "Lightcurve API", "operational")]
    snapshot = prober.build_snapshot(results, CONFIG)
    assert snapshot["status"] == "operational"


def test_unknown_component_defaults_to_outage():
    """If a component doesn't appear in probe results, treat as outage."""
    snapshot = prober.build_snapshot([], CONFIG)
    for comp in snapshot["components"]:
        assert comp["status"] == "outage"


# --- probe() mapping (uses mock server) ---

class _MockHTTPResponse:
    def __init__(self, status):
        self.status = status
    def __enter__(self):
        return self
    def __exit__(self, *a):
        pass


def _make_component(expected_status=None):
    return {"id": "x", "label": "X", "group": "g",
            "url": "http://fake", "method": "GET",
            "expected_status": expected_status or [200]}


def test_probe_maps_unexpected_status_to_outage(monkeypatch):
    def fake_urlopen(req, timeout):
        return _MockHTTPResponse(500)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert prober.probe(_make_component([200]), THRESHOLDS)["status"] == "outage"


def test_probe_maps_timeout_to_outage(monkeypatch):
    import urllib.error
    def fake_urlopen(req, timeout):
        raise TimeoutError()
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert prober.probe(_make_component(), THRESHOLDS)["status"] == "outage"


def test_probe_fast_200_is_operational(monkeypatch):
    def fake_urlopen(req, timeout):
        return _MockHTTPResponse(200)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert prober.probe(_make_component(), THRESHOLDS)["status"] == "operational"


def test_probe_slow_response_is_degraded(monkeypatch):
    def fake_urlopen(req, timeout):
        time.sleep(0.01)  # fast in test; we override elapsed below
        return _MockHTTPResponse(200)

    call_count = [0]

    def fake_monotonic():
        call_count[0] += 1
        if call_count[0] == 1:
            return 0.0
        return 3.0  # 3000 ms elapsed → degraded

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.monotonic", fake_monotonic)
    assert prober.probe(_make_component(), THRESHOLDS)["status"] == "degraded"


# --- history update ---

class _FakeS3:
    def __init__(self, existing=None):
        self._data = existing
        self.written = None
        self.put_kwargs = None

    class exceptions:
        class NoSuchKey(Exception):
            pass

    def get_object(self, **kw):
        if self._data is None:
            raise _FakeS3.exceptions.NoSuchKey()
        return {"Body": _FakeBody(json.dumps(self._data).encode())}

    def put_object(self, **kw):
        self.put_kwargs = kw
        body = kw["Body"]
        if kw.get("ContentEncoding") == "gzip":
            body = gzip.decompress(body)
        self.written = json.loads(body)


class _FakeBody:
    def __init__(self, data):
        self._data = data
    def read(self):
        return self._data


def _snapshot_with(statuses):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "status": "operational",
        "status_label": "All systems operational",
        "updated_at": now,
        "components": [
            {"id": cid, "label": cid, "group": "g",
             "status": s, "status_label": s, "checked_at": now}
            for cid, s in statuses.items()
        ],
    }


def test_history_creates_entry():
    s3 = _FakeS3()
    snap = _snapshot_with({"api-object": "operational"})
    prober.update_history(s3, snap)
    assert "api-object" in s3.written
    assert len(s3.written["api-object"]) == 1
    assert s3.written["api-object"][0]["status"] == "operational"


def test_history_no_duplicate_buckets():
    s3 = _FakeS3()
    snap = _snapshot_with({"api-object": "operational"})
    prober.update_history(s3, snap)
    # Simulate a second call in the same 5-min window
    s3._data = s3.written
    prober.update_history(s3, snap)
    # Should still be only 1 bucket entry
    assert len(s3.written["api-object"]) == 1


def test_history_no_raw_numerics():
    s3 = _FakeS3()
    snap = _snapshot_with({"api-object": "degraded"})
    prober.update_history(s3, snap)
    history_str = json.dumps(s3.written)
    for forbidden in ["200", "404", "500", "1000", "2000"]:
        assert forbidden not in history_str


def test_history_stored_gzipped_with_cache_ttl():
    """history.json is written gzipped with Content-Encoding and a 60 s TTL."""
    s3 = _FakeS3()
    snap = _snapshot_with({"api-object": "operational"})
    prober.update_history(s3, snap)
    assert s3.put_kwargs["ContentEncoding"] == "gzip"
    assert s3.put_kwargs["CacheControl"] == "max-age=60"
    assert s3.put_kwargs["Body"][:2] == b"\x1f\x8b"  # gzip magic number


def test_history_round_trips_gzipped_object():
    """A previously-gzipped history.json must be decompressed on read-back,
    not treated as corrupt (which would silently wipe accumulated history)."""
    s3 = _FakeS3()
    snap = _snapshot_with({"api-object": "operational"})
    prober.update_history(s3, snap)
    stored = s3.put_kwargs["Body"]  # gzipped bytes now "in S3"

    class _GzS3(_FakeS3):
        def get_object(self, **kw):
            return {"Body": _FakeBody(stored)}

    s3b = _GzS3()
    prober.update_history(s3b, snap)
    assert "api-object" in s3b.written
    assert len(s3b.written["api-object"]) == 1
