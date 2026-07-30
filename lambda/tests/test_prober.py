"""Unit tests for prober.py — run with: pytest lambda/tests/"""

import gzip
import json
import sys
import os
import time
from datetime import datetime, timezone

import pytest

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

def test_operational_components_expose_url_but_not_metrics():
    """Operational components carry probe_url (public) but never http_code/response_ms (metrics)."""
    results = [_make_result("api-object", "Object API", "operational"),
               _make_result("api-lightcurve", "Lightcurve API", "operational")]
    snapshot = prober.build_snapshot(results, CONFIG)
    for comp in snapshot["components"]:
        assert comp["probe_url"] == "https://example.com"
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


# --- per-endpoint threshold / timeout overrides ---

GLOBAL = {"latency_degraded_ms": 2000, "latency_outage_ms": 10000, "timeout_s": 15}


def test_effective_thresholds_fall_back_to_global():
    assert prober._effective_thresholds(_make_component(), GLOBAL) == (2000, 10000, 15)


def test_effective_thresholds_component_overrides_win():
    comp = _make_component()
    comp.update({"latency_degraded_ms": 25000, "latency_outage_ms": 35000, "timeout_s": 40})
    assert prober._effective_thresholds(comp, GLOBAL) == (25000, 35000, 40)


def test_effective_thresholds_timeout_defaults_when_absent_everywhere():
    # Global thresholds without timeout_s (as in the current test CONFIG) → DEFAULT_TIMEOUT_S
    _, _, timeout = prober._effective_thresholds(_make_component(), THRESHOLDS)
    assert timeout == prober.DEFAULT_TIMEOUT_S


def test_probe_slow_response_operational_under_override(monkeypatch):
    """A latency the global limits would call 'outage' stays 'operational' when the
    component raises its own thresholds (e.g. the ~20 s all-catalog crossmatch)."""
    def fake_urlopen(req, timeout):
        return _MockHTTPResponse(200)
    call = [0]
    def fake_monotonic():
        call[0] += 1
        return 0.0 if call[0] == 1 else 20.0  # 20 000 ms elapsed
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.monotonic", fake_monotonic)
    comp = _make_component()
    comp.update({"latency_degraded_ms": 25000, "latency_outage_ms": 35000})
    assert prober.probe(comp, THRESHOLDS)["status"] == "operational"


def test_probe_uses_component_timeout(monkeypatch):
    captured = {}
    def fake_urlopen(req, timeout):
        captured["timeout"] = timeout
        return _MockHTTPResponse(200)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    comp = _make_component()
    comp["timeout_s"] = 40
    prober.probe(comp, THRESHOLDS)
    assert captured["timeout"] == 40


def test_probe_uses_default_timeout_when_not_overridden(monkeypatch):
    captured = {}
    def fake_urlopen(req, timeout):
        captured["timeout"] = timeout
        return _MockHTTPResponse(200)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    prober.probe(_make_component(), THRESHOLDS)
    assert captured["timeout"] == prober.DEFAULT_TIMEOUT_S


# --- private latency logging (CloudWatch) ---

def test_log_response_times_emits_one_json_line_per_component(capsys):
    results = [
        {"id": "a", "status": "operational", "http_code": 200, "response_ms": 120},
        {"id": "b", "status": "outage", "http_code": None, "response_ms": None},
    ]
    prober.log_response_times(results)
    lines = [l for l in capsys.readouterr().out.strip().splitlines() if l.strip()]
    assert len(lines) == 2
    parsed = [json.loads(l) for l in lines]
    # Operational components' latency IS logged privately (unlike the public snapshot)
    assert parsed[0] == {"metric": "probe_latency", "id": "a", "status": "operational",
                         "http_code": 200, "response_ms": 120}
    assert parsed[1]["response_ms"] is None


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


SLOTS = prober.SLOTS_PER_DAY
T = datetime(2026, 7, 30, 14, 35, tzinfo=timezone.utc)  # slot 175 of 2026-07-30


def _row(s3, cid="api-object", day="2026-07-30"):
    return s3.written[cid][day]


def test_history_records_slot_for_the_run():
    s3 = _FakeS3()
    prober.update_history(s3, _snapshot_with({"api-object": "operational"}), now=T)
    row = _row(s3)
    assert len(row) == SLOTS
    assert row[175] == "o"
    # every other slot is explicitly "no check recorded", not a fabricated status
    assert row.count("o") == 1
    assert set(row) == {"o", "-"}


def test_history_slot_index_tracks_time_of_day():
    for hh, mm, idx in [(0, 0, 0), (0, 4, 0), (0, 5, 1), (12, 0, 144), (23, 55, 287)]:
        s3 = _FakeS3()
        prober.update_history(s3, _snapshot_with({"c": "operational"}),
                              now=T.replace(hour=hh, minute=mm))
        assert _row(s3, "c")[idx] == "o", (hh, mm, idx)


def test_history_same_slot_rerun_is_idempotent():
    """A duplicate invocation in one slot must not add a second sample.

    EventBridge -> Lambda is at-least-once, so this is a real sequence, not a
    hypothetical: with per-day counters it would inflate the day's totals forever.
    """
    s3 = _FakeS3()
    snap = _snapshot_with({"api-object": "operational"})
    prober.update_history(s3, snap, now=T)
    first = _row(s3)
    s3._data = s3.written
    prober.update_history(s3, snap, now=T)
    assert _row(s3) == first


def test_history_same_slot_status_change_overwrites():
    s3 = _FakeS3()
    prober.update_history(s3, _snapshot_with({"api-object": "operational"}), now=T)
    s3._data = s3.written
    prober.update_history(s3, _snapshot_with({"api-object": "outage"}), now=T)
    row = _row(s3)
    assert row[175] == "x"
    assert row.count("x") == 1 and row.count("o") == 0


def test_history_missed_runs_leave_gaps_not_fabricated_data():
    """A Lambda outage must be recorded as 'no data', distinguishable from 'up'."""
    s3 = _FakeS3()
    snap = _snapshot_with({"api-object": "operational"})
    prober.update_history(s3, snap, now=T.replace(hour=0, minute=0))
    s3._data = s3.written
    prober.update_history(s3, snap, now=T.replace(hour=6, minute=0))  # 6 h gap
    row = _row(s3)
    assert row[0] == "o" and row[72] == "o"
    assert set(row[1:72]) == {"-"}
    assert row.count("o") == 2


def test_history_day_rollover_needs_no_reconciliation():
    """Crossing midnight just starts a new row; nothing is rolled up or moved."""
    s3 = _FakeS3()
    snap = _snapshot_with({"api-object": "operational"})
    prober.update_history(s3, snap, now=T.replace(hour=23, minute=55))
    s3._data = s3.written
    prober.update_history(s3, snap, now=T.replace(day=31, hour=0, minute=0))
    days = s3.written["api-object"]
    assert sorted(days) == ["2026-07-30", "2026-07-31"]
    assert days["2026-07-30"][287] == "o"
    assert days["2026-07-31"][0] == "o"


def test_history_multi_day_outage_leaves_days_absent():
    """Days with no runs at all get no key -- the frontend renders those 'no data'."""
    s3 = _FakeS3()
    snap = _snapshot_with({"api-object": "operational"})
    prober.update_history(s3, snap, now=T)
    s3._data = s3.written
    prober.update_history(s3, snap, now=T.replace(month=8, day=3))  # 4-day gap
    assert sorted(s3.written["api-object"]) == ["2026-07-30", "2026-08-03"]


def test_history_prunes_beyond_retention():
    s3 = _FakeS3(existing={"api-object": {
        "2026-01-01": "o" * SLOTS,          # far outside the 90-day window
        "2026-07-29": "o" * SLOTS,
    }})
    prober.update_history(s3, _snapshot_with({"api-object": "operational"}), now=T)
    assert "2026-01-01" not in s3.written["api-object"]
    assert "2026-07-29" in s3.written["api-object"]


def test_history_prunes_keys_absent_from_the_snapshot():
    """A renamed/removed component must age out, not persist forever.

    The old implementation only ever pruned ids present in the current snapshot, so
    four renamed api-lsst-* keys were pinned in the object indefinitely.
    """
    s3 = _FakeS3(existing={
        "api-object": {"2026-07-29": "o" * SLOTS},
        "renamed-away": {"2026-01-01": "o" * SLOTS},   # only stale days -> key goes
        "paused-probe": {"2026-07-29": "o" * SLOTS},   # recent days -> key stays
    })
    prober.update_history(s3, _snapshot_with({"api-object": "operational"}), now=T)
    assert "renamed-away" not in s3.written
    assert s3.written["paused-probe"] == {"2026-07-29": "o" * SLOTS}


def test_history_carries_no_probe_metrics():
    """The public payload must never gain latency/HTTP detail (see CLAUDE.md).

    Structural, not substring-based: per-day counts legitimately contain digits, so
    the old "'200' not in json" check would have passed while meaning nothing.
    """
    s3 = _FakeS3()
    snap = _snapshot_with({"api-object": "degraded"})
    snap["components"][0].update(http_code=500, response_ms=2000, probe_url="http://x")
    prober.update_history(s3, snap, now=T)
    assert set(s3.written) == {"api-object"}
    for day, row in s3.written["api-object"].items():
        assert isinstance(row, str) and set(row) <= set("odx-")


def test_history_stored_gzipped_with_cache_ttl():
    """uptime.json is written gzipped with Content-Encoding and a 60 s TTL."""
    s3 = _FakeS3()
    prober.update_history(s3, _snapshot_with({"api-object": "operational"}), now=T)
    assert s3.put_kwargs["Key"] == prober.UPTIME_KEY
    assert s3.put_kwargs["ContentEncoding"] == "gzip"
    assert s3.put_kwargs["CacheControl"] == "max-age=60"
    assert s3.put_kwargs["Body"][:2] == b"\x1f\x8b"  # gzip magic number


def test_history_round_trips_gzipped_object():
    """A previously-gzipped object must be decompressed on read-back, not treated
    as corrupt (which would silently wipe accumulated history)."""
    s3 = _FakeS3()
    snap = _snapshot_with({"api-object": "operational"})
    prober.update_history(s3, snap, now=T)
    stored = s3.put_kwargs["Body"]  # gzipped bytes now "in S3"

    class _GzS3(_FakeS3):
        def get_object(self, **kw):
            return {"Body": _FakeBody(stored)}

    s3b = _GzS3()
    prober.update_history(s3b, snap, now=T.replace(minute=40))
    row = _row(s3b)
    assert row[175] == "o" and row[176] == "o"


def test_history_read_failure_raises_instead_of_wiping():
    """An unreadable object must NOT be silently republished as empty.

    Losing 30 days of bars leaves no trace and cannot be undone; raising is
    recoverable -- status.json is already written, the alert already sent, and the
    prober-errors alarm pages.
    """
    class _CorruptS3(_FakeS3):
        def get_object(self, **kw):
            return {"Body": _FakeBody(b"{not json")}

    s3 = _CorruptS3()
    with pytest.raises(Exception):
        prober.update_history(s3, _snapshot_with({"api-object": "operational"}), now=T)
    assert s3.written is None  # nothing was published


def test_history_row_width_defines_granularity():
    """A day already stored at another granularity keeps its own width.

    The row length is what tells the frontend how much time each character covers,
    so changing BUCKET_MINUTES must not corrupt days recorded under the old value.
    """
    s3 = _FakeS3(existing={"api-object": {"2026-07-30": "-" * 144}})  # 10-min slots
    prober.update_history(s3, _snapshot_with({"api-object": "operational"}), now=T)
    row = _row(s3)
    assert len(row) == 144
    assert row[87] == "o"  # (14*60+35) * 144 // 1440


# --- state-change alerting ---

def _c(cid, status, label=None, http_code=None, response_ms=None):
    e = {"id": cid, "label": label or cid, "status": status}
    if http_code is not None:
        e["http_code"] = http_code
    if response_ms is not None:
        e["response_ms"] = response_ms
    return e


def _snap(*comps, overall="operational"):
    return {"status": overall, "components": list(comps)}


class _FakeSNS:
    def __init__(self, fail=False):
        self.fail = fail
        self.published = []

    def publish(self, **kw):
        if self.fail:
            raise RuntimeError("sns unavailable")
        self.published.append(kw)
        return {"MessageId": "test"}


def test_transitions_no_baseline_is_silent():
    """First run (no prev status.json) must not alert on anything."""
    snap = _snap(_c("a", "outage"), _c("b", "degraded"))
    assert prober.compute_transitions(None, snap) == []


def test_transition_operational_to_outage_is_down():
    t = prober.compute_transitions({"a": "operational"},
                                   _snap(_c("a", "outage", http_code=502, response_ms=120)))
    assert len(t) == 1
    assert t[0]["kind"] == "down"
    assert t[0]["prev"] == "operational" and t[0]["new"] == "outage"
    assert t[0]["http_code"] == 502 and t[0]["response_ms"] == 120


def test_transition_operational_to_degraded_is_down():
    t = prober.compute_transitions({"a": "operational"}, _snap(_c("a", "degraded")))
    assert [x["kind"] for x in t] == ["down"]


def test_transition_degraded_to_outage_escalates():
    t = prober.compute_transitions({"a": "degraded"}, _snap(_c("a", "outage")))
    assert [x["kind"] for x in t] == ["down"]


def test_transition_partial_recovery_is_silent():
    """outage → degraded stays 'in incident' — no re-page."""
    assert prober.compute_transitions({"a": "outage"}, _snap(_c("a", "degraded"))) == []


def test_transition_full_recovery_is_all_clear():
    for prev in ("outage", "degraded"):
        t = prober.compute_transitions({"a": prev}, _snap(_c("a", "operational")))
        assert [x["kind"] for x in t] == ["recovered"]


def test_transition_unchanged_is_silent():
    assert prober.compute_transitions({"a": "outage"}, _snap(_c("a", "outage"))) == []


def test_transition_new_component_sets_baseline_silently():
    """A component absent from the previous snapshot is baselined, not alerted."""
    assert prober.compute_transitions({"other": "operational"},
                                      _snap(_c("new-comp", "outage"))) == []


def test_format_alert_subject_is_ascii_and_bounded():
    t = prober.compute_transitions({"a": "operational"}, _snap(_c("a", "outage", label="X")))
    subject, _ = prober.format_alert(t, "outage")
    assert subject.isascii()
    assert len(subject) <= 100


def test_format_alert_body_carries_diagnostics_and_link():
    t = prober.compute_transitions(
        {"a": "operational"},
        _snap(_c("a", "outage", label="Object API", http_code=502, response_ms=340), overall="outage"))
    subject, body = prober.format_alert(t, "outage")
    assert "Object API" in subject
    assert "Object API" in body
    assert "operational → outage" in body
    assert "HTTP 502" in body and "340 ms" in body
    assert prober.STATUS_PAGE_URL in body


def test_format_alert_recovery_message():
    t = prober.compute_transitions({"a": "outage"}, _snap(_c("a", "operational", label="Object API")))
    _, body = prober.format_alert(t, "operational")
    assert "operational" in body and "Object API" in body


def test_maybe_alert_publishes_once_on_transition():
    sns = _FakeSNS()
    fired = prober.maybe_alert(sns, "arn:topic", {"a": "operational"}, _snap(_c("a", "outage")))
    assert len(fired) == 1
    assert len(sns.published) == 1
    assert sns.published[0]["TopicArn"] == "arn:topic"
    assert "Subject" in sns.published[0] and "Message" in sns.published[0]


def test_maybe_alert_no_publish_without_transitions():
    sns = _FakeSNS()
    assert prober.maybe_alert(sns, "arn:topic", {"a": "operational"}, _snap(_c("a", "operational"))) == []
    assert sns.published == []


def test_maybe_alert_disabled_when_topic_unset():
    sns = _FakeSNS()
    assert prober.maybe_alert(sns, "", {"a": "operational"}, _snap(_c("a", "outage"))) == []
    assert sns.published == []


def test_maybe_alert_swallows_publish_failure():
    """An SNS failure must not propagate — the status write already succeeded."""
    sns = _FakeSNS(fail=True)
    assert prober.maybe_alert(sns, "arn:topic", {"a": "operational"}, _snap(_c("a", "outage"))) == []


# --- handler ordering: alerting must not sit downstream of update_history ---

def _run_handler(monkeypatch, s3, sns, update_history):
    """Drive handler() with fake AWS clients and a single stubbed probe."""
    import types

    fake_boto3 = types.SimpleNamespace(
        client=lambda name: {"s3": s3, "sns": sns}[name]
    )
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setattr(prober, "load_config", lambda: {
        "thresholds": THRESHOLDS,
        "components": [CONFIG["components"][0]],
    })
    monkeypatch.setattr(prober, "probe", lambda comp, th: _make_result(
        comp["id"], comp["label"], "outage", code=500))
    monkeypatch.setattr(prober, "update_history", update_history)
    monkeypatch.setattr(prober, "ALERT_TOPIC_ARN", "arn:topic")
    return prober.handler({}, None)


def test_handler_alerts_even_when_history_write_dies(monkeypatch):
    """Regression: an OOM in update_history must not swallow the alert.

    status.json is overwritten before update_history runs, so if the alert were
    sent afterwards a crash there would lose the transition permanently — the
    retry reads the already-published state as its baseline and sees no change.
    """
    s3 = _FakeS3(existing={"components": [{"id": "api-object", "status": "operational"}]})
    sns = _FakeSNS()

    def boom(_s3, _snapshot, now=None):
        raise MemoryError("simulated Runtime.OutOfMemory")

    # The error must still propagate, so the Lambda Errors metric / alarm fires.
    try:
        _run_handler(monkeypatch, s3, sns, boom)
        raise AssertionError("handler should surface the history-write failure")
    except MemoryError:
        pass

    # ...but the operational → outage transition was published first.
    assert len(sns.published) == 1
    assert "Object API" in sns.published[0]["Message"]  # alerts carry the label, not the id
    assert "operational → outage" in sns.published[0]["Message"]


def test_handler_writes_history_on_the_happy_path(monkeypatch):
    """The reorder must not drop the history write when nothing fails."""
    s3 = _FakeS3(existing={"components": [{"id": "api-object", "status": "operational"}]})
    sns = _FakeSNS()
    calls = []
    result = _run_handler(monkeypatch, s3, sns,
                          lambda _s3, snap, now=None: calls.append((snap, now)))
    assert len(calls) == 1
    assert result["statusCode"] == 200
    assert len(sns.published) == 1
    # One clock reading for the whole run: the snapshot and the history slot must
    # come from the same instant, or they can straddle midnight.
    snap, now = calls[0]
    assert now is not None
    assert snap["updated_at"] == now.isoformat(timespec="seconds")


def test_read_prev_status_parses_components():
    s3 = _FakeS3(existing={"components": [{"id": "a", "status": "outage"},
                                          {"id": "b", "status": "operational"}]})
    assert prober.read_prev_status(s3) == {"a": "outage", "b": "operational"}


def test_read_prev_status_none_when_missing():
    assert prober.read_prev_status(_FakeS3()) is None


# --- real config.json ↔ frontend wiring ---
#
# app.js skips any component whose `group` has no entry in its groups map, and an
# entry whose container div is missing renders nowhere. Both failures are silent:
# the component is probed, alerted on and recorded in history, but never appears
# on the page. These tests tie the shipped config to the shipped frontend.

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _real_config():
    with open(os.path.join(os.path.dirname(__file__), "..", "config.json")) as f:
        return json.load(f)


def _frontend(name):
    with open(os.path.join(REPO_ROOT, "frontend", name)) as f:
        return f.read()


def test_real_config_component_ids_are_unique():
    """Ids key the history series — a duplicate would silently merge two components."""
    ids = [c["id"] for c in _real_config()["components"]]
    assert len(ids) == len(set(ids))


def test_every_group_is_rendered_by_the_frontend():
    groups = {c["group"] for c in _real_config()["components"]}
    app_js = _frontend("app.js")
    index_html = _frontend("index.html")
    for group in groups:
        assert f"{group}:" in app_js, f"group '{group}' missing from the groups map in app.js"
        # app.js maps the group id to a container whose id uses dashes (apis_lsst → apis-lsst-rows)
        container = f'id="{group.replace("_", "-")}-rows"'
        assert container in index_html, f"group '{group}' has no {container} container in index.html"


def test_real_config_thresholds_are_ordered():
    """degraded < outage <= timeout, per component — otherwise a state is unreachable.

    The timeout must not fire *before* the outage threshold, or the latency-outage
    branch is dead code and every slow response is recorded as a connection failure
    (null http_code/response_ms) instead of a timed one. Equality is allowed and
    means the diagnostics are lost only past the threshold itself
    (api-ztf-htmx-crossmatch is deliberately at that boundary).
    """
    config = _real_config()
    for comp in config["components"]:
        degraded, outage, timeout_s = prober._effective_thresholds(comp, config["thresholds"])
        assert degraded < outage, f"{comp['id']}: degraded threshold must be below outage"
        assert outage <= timeout_s * 1000, (
            f"{comp['id']}: timeout ({timeout_s}s) fires before the outage threshold "
            f"({outage}ms), so a slow response is recorded as a connection failure"
        )
