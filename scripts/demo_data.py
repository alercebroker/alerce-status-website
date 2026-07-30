"""Generate a fresh, current-anchored demo dataset for `DEMO=1` dev_server.

The committed demo/*.json snapshots freeze their timestamps, so once real time
moves past them the data falls outside the frontend's 30-day uptime window and
trips the "data is N minutes old" stale warning. This regenerates status +
history + incidents anchored to *now*, with realistic intra-day variety
(288 five-minute samples/day) so the per-day uptime fractions and threshold bar
coloring are actually exercised (with 1 sample/day, "worst-of-day" and "the
fraction" are identical, so the old snapshot couldn't demonstrate them).

Curated content (component labels/descriptions, incident text) is reused from
demo/status.json and demo/incidents.json; only the timestamps and the history
are synthesized. Stdlib only, consistent with the rest of the repo.
"""
import datetime as dt
import json
from pathlib import Path
from random import Random

ROOT = Path(__file__).parent.parent
DEMO_DIR = ROOT / "demo"

BUCKET_MINUTES = 5
SAMPLES_PER_DAY = 24 * 60 // BUCKET_MINUTES   # 288
WINDOW_DAYS = 30                              # today + 29 prior days (frontend shows 30)

# Must match prober.STATUS_CHARS / NO_DATA_CHAR.
STATUS_CHARS = {"operational": "o", "degraded": "d", "outage": "x"}
NO_DATA_CHAR = "-"
CHAR_STATUS = {v: k for k, v in STATUS_CHARS.items()}

# Match prober._group_label / _overall_label so the demo renders like production.
GROUP_LABEL = {"operational": "Operational",
               "degraded": "Degraded performance",
               "outage": "Service outage"}
OVERALL_LABEL = {"operational": "All systems operational",
                 "degraded": "Some services degraded",
                 "outage": "Service disruption detected"}

# A few components get light random degraded flicker so the 30-day bars look alive.
NOISY = {"api-ztf-objects", "api-ztf-magstats", "api-ztf-classifiers", "frontend-explorer"}


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _floor_bucket(t):
    return t.replace(minute=(t.minute // BUCKET_MINUTES) * BUCKET_MINUTES,
                     second=0, microsecond=0)


def _worst(statuses):
    for s in ("outage", "degraded", "operational"):
        if s in statuses:
            return s
    return "operational"


def _spans(cid, d, count):
    """(start, length, status) overrides laid over an all-operational day.

    `d` is days-ago (0 = today); `count` is that day's sample count (partial for
    today). Spans anchored to "now" use `count` as the end index so an ongoing
    incident runs up to the current bucket. The showcase days each demonstrate
    one branch of the frontend's threshold coloring.
    """
    end = count
    s = []
    if cid == "api-lsst-probability":
        if d == 0:
            s.append((max(0, end - 24), 24, "outage"))    # ongoing ~2 h outage (matches current status)
        elif d == 3:
            s.append((60, 120, "outage"))                 # an earlier hard-down day -> RED
    if cid == "api-stamps" and d == 0:
        s.append((max(0, end - 42), 42, "degraded"))      # ongoing ~3.5 h degraded (matches current status)
    if cid == "api-ztf-lightcurve" and d == 9:
        s.append((150, 1, "outage"))                      # SHOWCASE: 1 failed check of 288 -> stays GREEN
    if cid == "api-ztf-features" and d == 5:
        s.append((120, 8, "outage"))                      # SHOWCASE: ~40 min down (2.8%) -> YELLOW
    if cid == "api-crossmatch" and d == 14:
        s.append((100, 18, "outage"))                     # SHOWCASE: 90 min down (6.3%) -> RED
    if cid == "api-lsst-lightcurve" and d == 20:
        s.append((90, 66, "degraded"))                    # SHOWCASE: 5.5 h slow, never down -> YELLOW @ 100% up
    return s


def _noise(cid, d, count, rng):
    if d != 0 and cid in NOISY and rng.random() < 0.15:
        start = rng.randint(0, max(0, count - 4))
        return [(start, rng.randint(1, 3), "degraded")]
    return []


def _day(count, cid, d, rng):
    """One day as a SAMPLES_PER_DAY-char row: recorded slots then '-' for the rest.

    Matches the prober's uptime format (see prober.update_history) -- one character
    per 5-min slot, position implying time of day.
    """
    st = ["operational"] * count
    for start, length, status in _spans(cid, d, count) + _noise(cid, d, count, rng):
        for i in range(start, min(start + length, count)):
            st[i] = status
    row = "".join(STATUS_CHARS[s] for s in st)
    return row + NO_DATA_CHAR * (SAMPLES_PER_DAY - count)


def _reanchor_incidents(now):
    """Shift every incident timestamp by whole days so the newest lands on today,
    preserving relative spacing and time-of-day (kills the stale look)."""
    incidents = json.loads((DEMO_DIR / "incidents.json").read_text())
    times = []
    for i in incidents:
        for k in ("started_at", "resolved_at"):
            if i.get(k):
                times.append(_parse(i[k]))
        for u in i.get("updates", []):
            if u.get("at"):
                times.append(_parse(u["at"]))
    if not times:
        return incidents
    shift = dt.timedelta(days=(now.date() - max(times).date()).days)
    rebase = lambda ts: _iso(_parse(ts) + shift)
    for i in incidents:
        for k in ("started_at", "resolved_at"):
            if i.get(k):
                i[k] = rebase(i[k])
        for u in i.get("updates", []):
            if u.get("at"):
                u["at"] = rebase(u["at"])
    return incidents


def build(now):
    """Return (status, history, incidents) anchored to `now` (a tz-aware UTC datetime)."""
    now = now.astimezone(dt.timezone.utc).replace(microsecond=0)
    midnight = now.replace(hour=0, minute=0, second=0)
    today_count = (_floor_bucket(now) - midnight) // dt.timedelta(minutes=BUCKET_MINUTES) + 1

    template = json.loads((DEMO_DIR / "status.json").read_text())
    comp_ids = [c["id"] for c in template["components"]]

    rng = Random(20260722)  # fixed seed -> stable bars across runs (nice for screenshots)
    history = {}
    today_key = midnight.date().isoformat()
    for cid in comp_ids:
        days = {}
        for d in range(WINDOW_DAYS - 1, -1, -1):        # oldest -> today
            day = (midnight - dt.timedelta(days=d)).date().isoformat()
            count = today_count if d == 0 else SAMPLES_PER_DAY
            days[day] = _day(count, cid, d, rng)
        history[cid] = days

    now_iso = _iso(now)
    for c in template["components"]:
        # current status = today's latest recorded slot
        cur = CHAR_STATUS[history[c["id"]][today_key][today_count - 1]]
        c["status"] = cur
        c["status_label"] = GROUP_LABEL[cur]
        c["checked_at"] = now_iso
        if cur == "outage":
            c["http_code"], c["response_ms"] = 503, 0
        elif cur == "degraded":
            c["http_code"], c["response_ms"] = 200, 4800
        else:
            c.pop("http_code", None)
            c.pop("response_ms", None)
    overall = _worst({c["status"] for c in template["components"]})
    template["status"] = overall
    template["status_label"] = OVERALL_LABEL[overall]
    template["updated_at"] = now_iso

    return template, history, _reanchor_incidents(now)


def _write(data_dir, name, obj):
    (Path(data_dir) / name).write_text(json.dumps(obj, separators=(",", ":")))


def write_demo(data_dir, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    status, history, incidents = build(now)
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    _write(data_dir, "status.json", status)
    _write(data_dir, "uptime.json", history)
    _write(data_dir, "incidents.json", incidents)
    return status


def tick(data_dir, now=None):
    """Keep DEMO data fresh between full rebuilds: refresh updated_at each call and
    write today's current slot per component (positional, so it's idempotent like
    the real prober) so the page never trips the stale-data warning during a
    session."""
    now = (now or dt.datetime.now(dt.timezone.utc)).replace(microsecond=0)
    data_dir = Path(data_dir)
    status = json.loads((data_dir / "status.json").read_text())
    now_iso = _iso(now)
    status["updated_at"] = now_iso
    for c in status["components"]:
        c["checked_at"] = now_iso
    _write(data_dir, "status.json", status)

    day = now.date().isoformat()
    idx = (now.hour * 60 + now.minute) // BUCKET_MINUTES
    history = json.loads((data_dir / "uptime.json").read_text())
    for c in status["components"]:
        days = history.setdefault(c["id"], {})
        row = days.get(day) or NO_DATA_CHAR * SAMPLES_PER_DAY
        char = STATUS_CHARS.get(c["status"], NO_DATA_CHAR)
        days[day] = row[:idx] + char + row[idx + 1:]
    _write(data_dir, "uptime.json", history)
