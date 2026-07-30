"""Unit tests for scripts/backfill_uptime.py — run with: pytest lambda/tests/

The backfill is a one-shot that runs against the production data bucket, so its
conversion is worth testing even though it executes once: a silent mistake here
would carry a wrong 90-day history forward permanently.
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

os.environ.setdefault("STATUS_BUCKET", "test")

import prober
import backfill_uptime

SLOTS = prober.SLOTS_PER_DAY
NOW = datetime(2026, 7, 30, 14, 35, tzinfo=timezone.utc)


def _legacy(*entries):
    return [{"ts": ts, "status": status} for ts, status in entries]


def test_converts_samples_to_positional_rows():
    history = {"api-object": _legacy(
        ("2026-07-30T00:00:00+00:00", "operational"),
        ("2026-07-30T14:35:00+00:00", "degraded"),
        ("2026-07-29T23:55:00+00:00", "outage"),
    )}
    uptime, report = backfill_uptime.convert(history, now=NOW, keep_ids={"api-object"})
    assert uptime["api-object"]["2026-07-30"][0] == "o"
    assert uptime["api-object"]["2026-07-30"][175] == "d"
    assert uptime["api-object"]["2026-07-29"][287] == "x"
    assert report["samples_kept"] == 3
    assert all(len(row) == SLOTS for row in uptime["api-object"].values())


def test_conversion_agrees_with_the_prober():
    """The converter must place a sample in the same slot the prober would.

    Two implementations derive the slot independently (one from an ISO string, one
    from a datetime); if they ever disagree, the backfilled days would be shifted
    relative to every day recorded afterwards.
    """
    for hh, mm in [(0, 0), (0, 5), (9, 17), (14, 35), (23, 55)]:
        ts = f"2026-07-30T{hh:02d}:{mm:02d}:00+00:00"
        converted, _ = backfill_uptime.convert(
            {"c": _legacy((ts, "operational"))}, now=NOW, keep_ids={"c"})

        class _S3:
            class exceptions:
                class NoSuchKey(Exception):
                    pass

            def get_object(self, **kw):
                raise self.exceptions.NoSuchKey()

            def put_object(self, **kw):
                pass

        live = {}
        s3 = _S3()
        s3.put_object = lambda **kw: live.update(kw)
        snap = {"components": [{"id": "c", "status": "operational"}]}
        prober.update_history(s3, snap, now=NOW.replace(hour=hh, minute=mm))
        import gzip, json
        written = json.loads(gzip.decompress(live["Body"]))
        assert converted["c"]["2026-07-30"] == written["c"]["2026-07-30"], ts


def test_drops_keys_absent_from_config():
    history = {
        "api-object": _legacy(("2026-07-30T00:00:00+00:00", "operational")),
        "api-lsst-objects": _legacy(("2026-07-30T00:00:00+00:00", "operational")),
    }
    uptime, report = backfill_uptime.convert(history, now=NOW, keep_ids={"api-object"})
    assert set(uptime) == {"api-object"}
    assert report["dropped_keys"] == [("api-lsst-objects", 1)]


def test_keeps_every_key_when_not_filtering():
    history = {"gone": _legacy(("2026-07-30T00:00:00+00:00", "operational"))}
    uptime, _ = backfill_uptime.convert(history, now=NOW, keep_ids=None)
    assert set(uptime) == {"gone"}


def test_drops_samples_outside_retention():
    history = {"c": _legacy(
        ("2026-01-01T00:00:00+00:00", "operational"),   # older than 90 days
        ("2026-07-30T00:00:00+00:00", "operational"),
    )}
    uptime, report = backfill_uptime.convert(history, now=NOW, keep_ids={"c"})
    assert sorted(uptime["c"]) == ["2026-07-30"]
    assert report["samples_older_than_cutoff"] == 1


def test_last_sample_wins_within_a_slot_and_is_counted():
    history = {"c": _legacy(
        ("2026-07-30T14:35:00+00:00", "operational"),
        ("2026-07-30T14:37:00+00:00", "outage"),   # same 5-min slot
    )}
    uptime, report = backfill_uptime.convert(history, now=NOW, keep_ids={"c"})
    assert uptime["c"]["2026-07-30"][175] == "x"
    assert report["slot_collisions"] == 1


def test_skips_malformed_entries_without_failing():
    history = {"c": _legacy(
        ("2026-07-30T14:35:00+00:00", "operational"),
        ("", "operational"),
        ("2026-07-30T15:00:00+00:00", "bogus-status"),
    )}
    uptime, report = backfill_uptime.convert(history, now=NOW, keep_ids={"c"})
    assert report["samples_kept"] == 1
    assert uptime["c"]["2026-07-30"].count("o") == 1


def test_output_is_readable_by_the_prober():
    """The prober must be able to append to a backfilled object without rebuilding
    it -- that is the whole point of converting before the deploy."""
    history = {"c": _legacy(("2026-07-30T00:00:00+00:00", "operational"))}
    uptime, _ = backfill_uptime.convert(history, now=NOW, keep_ids={"c"})

    import gzip, json

    class _S3:
        class exceptions:
            class NoSuchKey(Exception):
                pass

        def __init__(self):
            self.written = None

        def get_object(self, **kw):
            class _B:
                def read(inner):
                    return json.dumps(uptime).encode()
            return {"Body": _B()}

        def put_object(self, **kw):
            self.written = json.loads(gzip.decompress(kw["Body"]))

    s3 = _S3()
    prober.update_history(s3, {"components": [{"id": "c", "status": "degraded"}]}, now=NOW)
    row = s3.written["c"]["2026-07-30"]
    assert row[0] == "o"      # backfilled sample survived
    assert row[175] == "d"    # new sample appended
