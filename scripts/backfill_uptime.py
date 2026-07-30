"""One-shot converter: legacy data/history.json -> compact data/uptime.json.

Run this ONCE, BEFORE deploying the prober that writes uptime.json. The prober
never reads the legacy object, so there is no race: this script creates the new
key, the deploy then starts maintaining it, and the old history.json is left
frozen in place so any already-open browser tab (running the old app.js, which it
will never reload) keeps rendering its last-known bars instead of throwing.

Format change, per component:
    [{"ts": "...T14:35:00+00:00", "status": "operational"}, ...]   ~50 MB at steady state
    {"2026-07-30": "ooooddd...---"}                                 ~1 MB

Blast radius: reads data/history.json and writes data/uptime.json in ONE bucket
(the status site's own data bucket). Writes nothing else, deletes nothing, and
touches no other AWS resource. Default is a dry run that writes nothing at all.

Usage:
  # local files (dev data, or an object you downloaded by hand)
  python scripts/backfill_uptime.py --in-file data/history.json --out-file data/uptime.json

  # against S3: report first, then apply
  python scripts/backfill_uptime.py --bucket BUCKET
  python scripts/backfill_uptime.py --bucket BUCKET --apply
"""
import argparse
import gzip
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lambda"))
import prober

HISTORY_KEY = "data/history.json"


def _maybe_gunzip(raw):
    return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw


def convert(history, now=None, keep_ids=None):
    """Legacy {cid: [{ts, status}]} -> {cid: {day: row}}. Returns (uptime, report)."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=prober.HISTORY_DAYS)).date().isoformat()

    uptime, dropped_keys, samples, stale, collisions = {}, [], 0, 0, 0
    for cid, entries in history.items():
        if keep_ids is not None and cid not in keep_ids:
            dropped_keys.append((cid, len(entries)))
            continue
        days = {}
        for entry in entries:
            ts = entry.get("ts") or ""
            char = prober.STATUS_CHARS.get(entry.get("status"))
            if len(ts) < 16 or char is None:
                continue
            day = ts[:10]
            if day < cutoff:
                stale += 1
                continue
            # Slot from time-of-day, exactly as the prober derives it.
            hh, mm = int(ts[11:13]), int(ts[14:16])
            idx = (hh * 60 + mm) // prober.BUCKET_MINUTES
            row = days.get(day) or prober.NO_DATA_CHAR * prober.SLOTS_PER_DAY
            if row[idx] != prober.NO_DATA_CHAR:
                collisions += 1
            days[day] = row[:idx] + char + row[idx + 1:]
            samples += 1
        if days:
            uptime[cid] = days

    report = {
        "components_in": len(history),
        "components_out": len(uptime),
        "dropped_keys": dropped_keys,
        "samples_kept": samples,
        "samples_older_than_cutoff": stale,
        "slot_collisions": collisions,
        "cutoff_day": cutoff,
        "days_out": sum(len(d) for d in uptime.values()),
    }
    return uptime, report


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", help="S3 bucket holding data/history.json")
    ap.add_argument("--in-file", help="read a local history.json instead of S3")
    ap.add_argument("--out-file", help="write a local uptime.json instead of S3")
    ap.add_argument("--apply", action="store_true",
                    help="actually write to S3 (default: dry run, writes nothing)")
    ap.add_argument("--keep-all", action="store_true",
                    help="keep component keys absent from lambda/config.json "
                         "(default: drop them -- they are renamed/removed probes "
                         "that the old prober could never age out)")
    args = ap.parse_args()

    if not args.in_file and not args.bucket:
        ap.error("need --in-file or --bucket")

    if args.in_file:
        raw = Path(args.in_file).read_bytes()
    else:
        import boto3
        raw = boto3.client("s3").get_object(
            Bucket=args.bucket, Key=HISTORY_KEY)["Body"].read()
    history = json.loads(_maybe_gunzip(raw))

    keep_ids = None
    if not args.keep_all:
        keep_ids = {c["id"] for c in prober.load_config()["components"]}

    uptime, report = convert(history, keep_ids=keep_ids)
    body = json.dumps(uptime, separators=(",", ":")).encode()
    packed = gzip.compress(body, mtime=0)

    print(json.dumps(report, indent=2))
    print(f"\nin : {len(raw) / 1e6:.2f} MB stored, {len(_maybe_gunzip(raw)) / 1e6:.2f} MB raw")
    print(f"out: {len(packed) / 1e3:.1f} KB stored, {len(body) / 1e3:.1f} KB raw")
    for cid, n in report["dropped_keys"]:
        print(f"  dropped key (not in config): {cid} ({n} samples)")

    if args.out_file:
        Path(args.out_file).write_bytes(body)  # dev server serves this decompressed
        print(f"\nwrote {args.out_file}")
        return
    if not args.apply:
        print(f"\nDRY RUN — nothing written. Re-run with --apply to write "
              f"s3://{args.bucket}/{prober.UPTIME_KEY}")
        return

    import boto3
    boto3.client("s3").put_object(
        Bucket=args.bucket, Key=prober.UPTIME_KEY, Body=packed,
        ContentType="application/json", ContentEncoding="gzip",
        CacheControl="max-age=60",
    )
    print(f"\nwrote s3://{args.bucket}/{prober.UPTIME_KEY}")


if __name__ == "__main__":
    os.environ.setdefault("STATUS_BUCKET", "backfill")
    main()
