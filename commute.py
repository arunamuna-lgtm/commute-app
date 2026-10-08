"""
Morning commute agent: Woolpit -> West Suffolk Hospital car park.

Runs every ~10 minutes on weekday mornings (GitHub Actions).
  * Logs live travel times for each route to data/commute_log.csv
  * Sends one morning briefing to your phone (ntfy) with ETA + leave-by time
  * Sends an extra alert if traffic turns bad after the briefing
  * Once enough history exists, adds a data-driven "safe leave time"
  * Phone overrides: send "820" (or "off") to your ntfy topic the evening
    before, and the next morning's briefing works back from that time
"""

import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, date
from pathlib import Path
from statistics import quantiles
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- settings
CONFIG = json.loads(Path(__file__).with_name("config.json").read_text())

TZ = ZoneInfo("Europe/London")
HOME = CONFIG["home"]                    # "lat,lon"
WORK = CONFIG["work"]                    # "lat,lon" of the car park entrance
ARRIVE_BY = CONFIG["arrive_by"]          # "08:50"
BUFFER_MIN = CONFIG["buffer_minutes"]    # safety margin added to live ETA
WINDOW_START = CONFIG["collect_from"]    # "06:00"
WINDOW_END = CONFIG["collect_until"]     # "08:50"
ALERT_JUMP_MIN = CONFIG["alert_if_worse_by_minutes"]
MIN_DAYS_FOR_MODEL = CONFIG["min_days_for_model"]
# Briefing comes this long before your arrival time (08:50 -> 07:20, 08:20 -> 06:50)
BRIEF_BEFORE = CONFIG.get("brief_minutes_before_arrival", 90)

TOMTOM_KEY = os.environ.get("TOMTOM_KEY", "").strip()
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

DATA = Path(__file__).with_name("data")
LOG = DATA / "commute_log.csv"
STATE = DATA / "state.json"
FIELDS = ["timestamp", "date", "weekday", "depart_hhmm", "route", "km",
          "live_min", "typical_min", "free_flow_min", "delay_min"]


# ---------------------------------------------------------------- helpers
def hhmm(s: str, day: date) -> datetime:
    h, m = map(int, s.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


def fetch_routes() -> list[dict]:
    """Ask TomTom for the fastest route + up to 2 alternatives, live traffic."""
    url = (
        f"https://api.tomtom.com/routing/1/calculateRoute/{HOME}:{WORK}/json"
        f"?key={quote(TOMTOM_KEY)}&traffic=true&travelMode=car&departAt=now"
        f"&maxAlternatives=2&computeTravelTimeFor=all&instructionsType=text"
    )
    with urlopen(url, timeout=30) as r:
        payload = json.load(r)

    try:
        save_route_debug(payload)
    except Exception as e:  # never let diagnostics break the briefing
        print(f"route debug not saved: {e}")

    routes = []
    for rt in payload.get("routes", []):
        s = rt["summary"]
        live = s["travelTimeInSeconds"] / 60
        typical = s.get("historicTrafficTravelTimeInSeconds", s["travelTimeInSeconds"]) / 60
        free = s.get("noTrafficTravelTimeInSeconds", s["travelTimeInSeconds"]) / 60
        routes.append({
            "route": label_route(rt),
            "km": round(s["lengthInMeters"] / 1000, 1),
            "live_min": round(live, 1),
            "typical_min": round(typical, 1),
            "free_flow_min": round(free, 1),
            "delay_min": round(s.get("trafficDelayInSeconds", 0) / 60, 1),
        })
    routes.sort(key=lambda r: r["live_min"])

    # If two alternatives still share a name, number them so they stay distinct.
    seen: dict[str, int] = {}
    for r in routes:
        n = seen.get(r["route"], 0) + 1
        seen[r["route"]] = n
        if n > 1:
            r["route"] = f"{r['route']} (alt {n})"
    return routes


def label_route(rt: dict) -> str:
    """
    Name a route so alternatives are distinguishable:
      'A14 J44 via A1302'                  leaves the A14 at J44 onto the A1302
      'A14 J44 via A1302 (off-on at J46)'  same, but hops through J46's slip roads
      'A1088/A143'                         a non-A14 route, named by its main roads
    """
    instructions = rt.get("guidance", {}).get("instructions", [])
    roads = []
    for ins in instructions:
        for rn in ins.get("roadNumbers", []) or []:
            if rn not in roads:
                roads.append(rn)

    if "A14" not in roads:
        return "/".join(roads[:2]) if roads else "Back roads"

    def is_exit(ins: dict) -> bool:
        return bool(ins.get("exitNumber")) and "EXIT" in str(ins.get("maneuver", "")).upper()

    def jn(ins: dict) -> str:
        return "J" + str(ins["exitNumber"]).strip().lstrip("Jj")

    # Exits taken AFTER joining the A14. The last one is where the route finally
    # leaves for Bury; any earlier ones are "off and back on" hops through a
    # junction's slip roads (TomTom does this to skip a queue on the main line).
    joined = next((i for i, ins in enumerate(instructions)
                   if "A14" in (ins.get("roadNumbers") or [])), None)
    exits = [(i, ins) for i, ins in enumerate(instructions)
             if joined is not None and i > joined and is_exit(ins)]

    if exits:
        last_i, last = exits[-1]
        label = f"A14 {jn(last)}"
        for nxt in instructions[last_i:last_i + 3]:
            r = next((x for x in (nxt.get("roadNumbers") or []) if x != "A14"), None)
            if r:
                label += f" via {r}"
                break
        hops = [jn(ins) for _, ins in exits[:-1]]
        if hops:
            label += f" (off-on at {', '.join(hops)})"
        return label

    # No numbered exit: name the first road taken after leaving the A14.
    for ins in instructions[(joined or 0) + 1:]:
        r = next((x for x in (ins.get("roadNumbers") or []) if x != "A14"), None)
        if r:
            return f"A14 via {r}"
    return "A14"


def save_route_debug(payload: dict):
    """Keep a compact copy of the latest route instructions, to check labels."""
    out = []
    for rt in payload.get("routes", []):
        out.append({
            "km": round(rt["summary"]["lengthInMeters"] / 1000, 1),
            "label": label_route(rt),
            "steps": [
                {k: ins.get(k) for k in ("maneuver", "roadNumbers", "exitNumber", "street")
                 if ins.get(k)}
                for ins in rt.get("guidance", {}).get("instructions", [])
            ],
        })
    DATA.mkdir(exist_ok=True)
    (DATA / "last_routes.json").write_text(json.dumps(out, indent=1))


def notify(title: str, body: str, priority: str = "default", tags: str = "car"):
    if not NTFY_TOPIC:
        print(f"[no NTFY_TOPIC] {title}\n{body}")
        return
    req = Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Tags": tags},
        method="POST",
    )
    urlopen(req, timeout=30)


def load_state(today: str) -> dict:
    try:
        st = json.loads(STATE.read_text())
    except Exception:
        st = {}
    if st.get("date") != today:
        st = {"date": today, "briefed": False, "alerted": False, "brief_best_min": None}
    return st


def append_log(now: datetime, routes: list[dict]):
    DATA.mkdir(exist_ok=True)
    new = not LOG.exists()
    with LOG.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        for r in routes:
            w.writerow({
                "timestamp": now.isoformat(timespec="minutes"),
                "date": now.date().isoformat(),
                "weekday": now.strftime("%a"),
                "depart_hhmm": now.strftime("%H:%M"),
                **r,
            })


# ---------------------------------------------------------------- phone overrides
def next_commute_day(sent: datetime) -> date:
    """A message applies to the next weekday morning: today if sent before
    09:00 on a weekday, otherwise the following weekday (Fri evening -> Mon)."""
    d = sent.date()
    if d.weekday() < 5 and sent.time() < datetime.strptime("09:00", "%H:%M").time():
        return d
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def parse_override(text: str) -> str | None:
    """'820', '8:20', '08.20', '0820' -> '08:20';  'off'/'skip' -> 'off';
    'normal'/'reset' -> 'normal'. Anything else -> None (ignored)."""
    t = text.strip().lower()
    if t in ("off", "skip", "no", "day off"):
        return "off"
    if t in ("normal", "reset", "default"):
        return "normal"
    m = re.fullmatch(r"(\d{1,2})[:.]?(\d{2})", t)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if 5 <= h <= 11 and 0 <= mi < 60:
        return f"{h:02d}:{mi:02d}"
    return None


OVERRIDES = Path(__file__).with_name("data") / "overrides.json"


def sync_overrides() -> dict:
    """
    ntfy.sh only keeps messages for about 12 hours, so every run (including the
    extra daytime/evening runs) copies any new instructions into
    data/overrides.json, keyed by the morning they apply to. Latest message wins.
    """
    try:
        saved = json.loads(OVERRIDES.read_text())
    except Exception:
        saved = {}
    if NTFY_TOPIC:
        since = int((datetime.now(TZ) - timedelta(days=2)).timestamp())
        url = f"https://ntfy.sh/{NTFY_TOPIC}/json?poll=1&since={since}"
        try:
            with urlopen(Request(url, headers={"User-Agent": "commute-agent/1.0"}), timeout=30) as r:
                raw = r.read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"Couldn't read phone messages ({type(e).__name__}); using saved ones.")
            raw = ""
        for line in raw.splitlines():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("event") != "message" or msg.get("title"):
                continue  # skip the agent's own briefings (they always have a title)
            value = parse_override(msg.get("message", ""))
            if not value:
                continue
            sent = int(msg["time"])
            day = next_commute_day(datetime.fromtimestamp(sent, TZ)).isoformat()
            if day not in saved or saved[day]["sent"] <= sent:
                saved[day] = {"value": value, "sent": sent}
    # forget mornings that have passed
    today = datetime.now(TZ).date().isoformat()
    saved = {d: v for d, v in sorted(saved.items()) if d >= today}
    DATA.mkdir(exist_ok=True)
    OVERRIDES.write_text(json.dumps(saved, indent=1))
    return saved


def phone_override(today: date, saved: dict | None = None) -> tuple[str, str] | None:
    """The instruction for `today` as (value, sent_at), or None."""
    saved = sync_overrides() if saved is None else saved
    entry = saved.get(today.isoformat())
    if not entry:
        return None
    sent = datetime.fromtimestamp(entry["sent"], TZ)
    return entry["value"], f"{sent:%a %H:%M}"


def todays_plan(today: date, saved: dict | None = None) -> dict:
    """{"arrive": "08:20", "note": "..."}  or  {"arrive": None, ...} for no briefing."""
    ov = phone_override(today, saved)
    if ov is None or ov[0] == "normal":
        return {"arrive": ARRIVE_BY, "note": ""}
    value, when = ov
    if value == "off":
        return {"arrive": None, "note": f"Day off (your message {when})"}
    return {"arrive": value, "note": f"from your message {when}"}


# ---------------------------------------------------------------- model
def history_leave_time(today: date, arrive_by: str = ARRIVE_BY) -> str | None:
    """
    From logged history: for each 10-minute departure slot, take the
    90th-percentile best-route travel time across past days. Return the
    latest slot from which you'd still reach the car park by ARRIVE_BY
    on 9 days out of 10.
    """
    if not LOG.exists():
        return None
    best: dict[tuple[str, str], float] = {}  # (date, slot) -> best live_min
    with LOG.open() as f:
        for row in csv.DictReader(f):
            if row["date"] == today.isoformat():
                continue
            h, m = map(int, row["depart_hhmm"].split(":"))
            slot = f"{h:02d}:{(m // 10) * 10:02d}"
            key = (row["date"], slot)
            v = float(row["live_min"])
            best[key] = min(v, best.get(key, v))

    days = {d for d, _ in best}
    if len(days) < MIN_DAYS_FOR_MODEL:
        return None

    by_slot: dict[str, list[float]] = {}
    for (_, slot), v in best.items():
        by_slot.setdefault(slot, []).append(v)

    deadline = hhmm(arrive_by, today)
    ok = []
    for slot, vals in by_slot.items():
        if len(vals) < MIN_DAYS_FOR_MODEL // 2:
            continue
        p90 = quantiles(vals, n=10)[-1] if len(vals) >= 2 else vals[0]
        if hhmm(slot, today) + timedelta(minutes=p90) <= deadline:
            ok.append(slot)
    return f"{max(ok)} ({len(days)} days of data)" if ok else None


# ---------------------------------------------------------------- main
def main():
    now = datetime.now(TZ)
    today = now.date()
    saved = sync_overrides()  # every run, so no phone message is lost
    if now.weekday() >= 5:
        return print("Weekend, skipping.")
    if not (hhmm(WINDOW_START, today) <= now <= hhmm(WINDOW_END, today)):
        return print(f"{now:%H:%M} outside collection window, skipping.")
    if not TOMTOM_KEY:
        sys.exit("TOMTOM_KEY not set")

    routes = fetch_routes()
    append_log(now, routes)
    state = load_state(today.isoformat())
    best = routes[0]

    lines = []
    for r in routes:
        diff = r["live_min"] - r["typical_min"]
        flag = f"  (+{diff:.0f} vs usual)" if diff >= 3 else ""
        lines.append(f"• {r['route']} {r['km']} km: {r['live_min']:.0f} min{flag}")

    plan = todays_plan(today, saved)
    arrive_by = plan["arrive"]
    if arrive_by is None:
        # Day off: keep logging traffic for the model, but stay quiet.
        STATE.write_text(json.dumps(state))
        print(f"{plan['note']}: logged traffic, no briefing.")
        return print("\n".join(lines))

    # If you sent a new time after this morning's briefing, brief again for it.
    if state["briefed"] and state.get("brief_arrive") not in (None, arrive_by):
        state.update(briefed=False, alerted=False)

    leave_by = hhmm(arrive_by, today) - timedelta(minutes=best["live_min"] + BUFFER_MIN)
    brief_time = hhmm(arrive_by, today) - timedelta(minutes=BRIEF_BEFORE)

    # ---- morning briefing
    if not state["briefed"] and now >= brief_time:
        body = "\n".join(lines)
        note = f" ({plan['note']})" if plan["note"] else ""
        body += f"\n\nLeave by {leave_by:%H:%M} via {best['route']} to park by {arrive_by}{note}."
        model = history_leave_time(today, arrive_by)
        if model:
            body += f"\nHistory says leave by {model} for 90% reliability."
        bad = best["live_min"] - best["typical_min"] >= ALERT_JUMP_MIN
        notify(f"Commute: {best['live_min']:.0f} min, leave {leave_by:%H:%M}",
               body, priority="high" if bad else "default",
               tags="warning,car" if bad else "car")
        state.update(briefed=True, brief_best_min=best["live_min"], brief_arrive=arrive_by)

    # ---- follow-up alert if it gets worse before you leave
    elif state["briefed"] and not state["alerted"] and now < leave_by + timedelta(minutes=BUFFER_MIN):
        if best["live_min"] - (state["brief_best_min"] or best["live_min"]) >= ALERT_JUMP_MIN:
            notify(f"Traffic worse: now {best['live_min']:.0f} min",
                   "\n".join(lines) + f"\n\nLeave by {leave_by:%H:%M} via {best['route']}.",
                   priority="urgent", tags="rotating_light,car")
            state["alerted"] = True

    STATE.write_text(json.dumps(state))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
