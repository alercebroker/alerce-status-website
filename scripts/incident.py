"""
Helper for editing incidents/incidents.json.

Generates correctly-formatted UTC timestamps, validates status values, and
auto-fills resolved_at on terminal status. Output goes through the normal
PR/commit-to-main flow — the deploy workflow uploads it to S3.

Usage:
  # Open a new incident (status defaults to 'investigating', started_at to now)
  python scripts/incident.py open 2026-05-15-api-degraded \\
      --title "Elevated error rate on object API" \\
      --components apis \\
      --severity major \\
      --message "Investigating reports of API errors."

  # Append an update
  python scripts/incident.py update 2026-05-15-api-degraded \\
      --message "Rolled back the bad deploy; monitoring."

  # Close it (auto-sets resolved_at)
  python scripts/incident.py update 2026-05-15-api-degraded \\
      --status resolved \\
      --message "Resolved after rolling restart."

  # Schedule maintenance for a future window
  python scripts/incident.py open 2026-06-01-db-upgrade \\
      --type maintenance \\
      --title "Database upgrade" \\
      --start 2026-06-01T03:00:00Z \\
      --components apis \\
      --message "Maintenance window scheduled for Jun 1, 03:00-04:00 UTC."
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

INCIDENTS_FILE = Path(__file__).parent.parent / "incidents" / "incidents.json"

STATUSES = {
    "incident": ["investigating", "identified", "monitoring", "resolved"],
    "maintenance": ["scheduled", "in_progress", "completed"],
}
TERMINAL = {"resolved", "completed"}
SEVERITIES = ["minor", "major", "critical"]


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_ts(s):
    if not s.endswith("Z"):
        sys.exit(f"timestamp must end with Z (UTC): {s}")
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        sys.exit(f"invalid ISO-8601 timestamp: {s}")
    return s


def load():
    with INCIDENTS_FILE.open() as f:
        return json.load(f)


def save(entries):
    with INCIDENTS_FILE.open("w") as f:
        json.dump(entries, f, indent=2)
        f.write("\n")


def find(entries, id_):
    for e in entries:
        if e["id"] == id_:
            return e
    return None


def cmd_open(args):
    entries = load()
    if find(entries, args.id):
        sys.exit(f"incident already exists: {args.id}")
    type_ = args.type
    status = args.status or ("scheduled" if type_ == "maintenance" else "investigating")
    if status not in STATUSES[type_]:
        sys.exit(f"invalid status for {type_}: {status!r} (allowed: {STATUSES[type_]})")
    started = parse_ts(args.start) if args.start else now_utc()
    entry = {"id": args.id, "title": args.title, "status": status, "started_at": started}
    if type_ != "incident":
        entry["type"] = type_
    if args.severity:
        if args.severity not in SEVERITIES:
            sys.exit(f"invalid severity: {args.severity} (allowed: {SEVERITIES})")
        entry["severity"] = args.severity
    if args.components:
        entry["components"] = args.components
    if args.message:
        entry["updates"] = [{"at": now_utc(), "message": args.message}]
    if status in TERMINAL:
        entry["resolved_at"] = now_utc()
    entries.append(entry)
    save(entries)
    print(f"opened {args.id} ({type_}, {status})")


def cmd_update(args):
    if not args.message and not args.status:
        sys.exit("nothing to do: pass --message and/or --status")
    entries = load()
    entry = find(entries, args.id)
    if not entry:
        sys.exit(f"incident not found: {args.id}")
    type_ = entry.get("type", "incident")
    if args.status:
        if args.status not in STATUSES[type_]:
            sys.exit(f"invalid status for {type_}: {args.status!r} (allowed: {STATUSES[type_]})")
        entry["status"] = args.status
        if args.status in TERMINAL and "resolved_at" not in entry:
            entry["resolved_at"] = now_utc()
    if args.message:
        entry.setdefault("updates", []).append({"at": now_utc(), "message": args.message})
    save(entries)
    print(f"updated {args.id} (status={entry['status']})")


def main():
    p = argparse.ArgumentParser(description="Edit incidents/incidents.json.")
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("open", help="Create a new incident or maintenance entry.")
    o.add_argument("id", help="Stable slug, conventionally YYYY-MM-DD-short-slug.")
    o.add_argument("--title", required=True)
    o.add_argument("--type", choices=["incident", "maintenance"], default="incident")
    o.add_argument("--status", help="Default: 'investigating' (incident) or 'scheduled' (maintenance).")
    o.add_argument("--severity", help=f"One of {SEVERITIES}.")
    o.add_argument("--components", nargs="+", help="e.g. apis frontends")
    o.add_argument("--start", help="ISO-8601 UTC. Default: now. Use for scheduled maintenance.")
    o.add_argument("--message", help="First update message.")
    o.set_defaults(func=cmd_open)

    u = sub.add_parser("update", help="Append an update and/or change status.")
    u.add_argument("id")
    u.add_argument("--message")
    u.add_argument("--status", help=f"Incident: {STATUSES['incident']}. Maintenance: {STATUSES['maintenance']}.")
    u.set_defaults(func=cmd_update)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
