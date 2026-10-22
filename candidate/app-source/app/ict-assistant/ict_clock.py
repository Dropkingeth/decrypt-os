#!/usr/bin/env python3
"""ICT session clock in New York time. Standard library only, no network.

Usage:
  ict_clock.py              human-readable status
  ict_clock.py --json       Waybar-style JSON {"text", "tooltip", "class"}
  ict_clock.py --at 2021-10-11T14:05:00Z   status for a given instant (testing)

The windows match knowledge/04-time-and-sessions.md. They are study
references, not trading signals.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import sys
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")

# (name, kind, start "HH:MM", end "HH:MM"); end "24:00" means midnight.
WINDOWS = (
    ("Asian killzone", "killzone", "20:00", "24:00"),
    ("London killzone", "killzone", "02:00", "05:00"),
    ("New York killzone (forex)", "killzone", "07:00", "10:00"),
    ("New York AM killzone (indices)", "killzone", "08:30", "11:00"),
    ("London close killzone", "killzone", "10:00", "12:00"),
    ("New York lunch", "lunch", "12:00", "13:30"),
    ("New York PM session", "killzone", "13:30", "16:00"),
    ("London Silver Bullet", "silver_bullet", "03:00", "04:00"),
    ("AM Silver Bullet", "silver_bullet", "10:00", "11:00"),
    ("PM Silver Bullet", "silver_bullet", "14:00", "15:00"),
    ("London macro 1", "macro", "02:33", "03:00"),
    ("London macro 2", "macro", "04:03", "04:30"),
    ("NY AM macro 1", "macro", "08:50", "09:10"),
    ("NY AM macro 2", "macro", "09:50", "10:10"),
    ("NY AM macro 3", "macro", "10:50", "11:10"),
    ("NY lunch macro", "macro", "11:50", "12:10"),
    ("NY PM macro", "macro", "13:10", "13:40"),
    ("Last-hour macro", "macro", "15:15", "15:45"),
)
KIND_ORDER = {"silver_bullet": 0, "macro": 1, "killzone": 2, "lunch": 3}


def minutes(text):
    hours, mins = text.split(":")
    return int(hours) * 60 + int(mins)


def label(text):
    return "00:00" if text == "24:00" else text


def futures_open(ny):
    """CME index futures: Sunday 18:00 to Friday 17:00, daily pause 17:00-18:00.

    Holidays and early closes are not modelled.
    """
    day, minute = ny.weekday(), ny.hour * 60 + ny.minute  # Monday = 0
    if day == 5:
        return False
    if day == 6:
        return minute >= 18 * 60
    if day == 4 and minute >= 17 * 60:
        return False
    return not (17 * 60 <= minute < 18 * 60)


def active_windows(ny):
    minute = ny.hour * 60 + ny.minute
    found = [w for w in WINDOWS if minutes(w[2]) <= minute < minutes(w[3])]
    return sorted(found, key=lambda w: KIND_ORDER[w[1]])


def next_window(ny):
    """Next window start within the coming 7 days that falls in futures hours."""
    base = ny.replace(second=0, microsecond=0)
    best = None
    for offset in range(0, 8):
        day = base + timedelta(days=offset)
        for window in WINDOWS:
            start_minute = minutes(window[2])
            start = datetime(day.year, day.month, day.day, start_minute // 60, start_minute % 60,
                             tzinfo=NEW_YORK)
            if start <= ny or not futures_open(start):
                continue
            if best is None or start < best[1]:
                best = (window, start)
        if best is not None:
            return best
    return None


def snapshot(now=None):
    now = datetime.now(timezone.utc) if now is None else now
    if now.tzinfo is None:
        raise ValueError("Use a timezone-aware time.")
    ny = now.astimezone(NEW_YORK)
    open_now = futures_open(ny)
    active = active_windows(ny) if open_now else []
    upcoming = next_window(ny)
    result = {
        "newYorkTime": ny.strftime("%a %Y-%m-%d %H:%M %Z"),
        "futuresOpen": open_now,
        "active": [{"name": w[0], "kind": w[1], "start": w[2], "end": label(w[3])} for w in active],
        "next": None,
    }
    if upcoming is not None:
        window, start = upcoming
        result["next"] = {"name": window[0], "kind": window[1], "start": window[2],
                          "minutesUntil": int((start - ny).total_seconds() // 60)}
    return result


def describe(snap):
    lines = ["New York time: " + snap["newYorkTime"]]
    if not snap["futuresOpen"]:
        lines.append("Futures market closed (weekend or daily pause).")
    if snap["active"]:
        lines.append("Now: " + "; ".join(f'{w["name"]} {w["start"]}-{w["end"]}' for w in snap["active"]))
    elif snap["futuresOpen"]:
        lines.append("Now: no killzone or macro window.")
    if snap["next"]:
        n = snap["next"]
        hours, mins = divmod(n["minutesUntil"], 60)
        lines.append(f'Next: {n["name"]} at {n["start"]} NY (in {hours}h {mins:02d}m)')
    return "\n".join(lines)


def waybar(snap):
    if snap["active"]:
        first = snap["active"][0]
        text, css = first["name"], first["kind"]
    elif not snap["futuresOpen"]:
        text, css = "Market closed", "closed"
    else:
        text, css = "No killzone", "idle"
    return {"text": text, "tooltip": describe(snap), "class": css}


def main(argv=None):
    parser = argparse.ArgumentParser(description="ICT session clock (New York time).")
    parser.add_argument("--json", action="store_true", help="Waybar-style JSON output")
    parser.add_argument("--at", help="ISO-8601 instant with timezone, for testing")
    args = parser.parse_args(argv)
    when = None
    if args.at:
        when = datetime.fromisoformat(args.at.replace("Z", "+00:00"))
    snap = snapshot(when)
    print(json.dumps(waybar(snap)) if args.json else describe(snap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
