#!/usr/bin/env python3
"""
FPL squad watcher.

Runs frequently (e.g. every 15 min via GitHub Actions) but only does real work:
  * once each morning (after MORNING_HOUR local time), and
  * once per gameweek, shortly before the deadline.

On each check it reports, via ntfy push notification:
  * flag / chance-of-playing changes and injury-news updates for your 15 players
  * (morning only) players who started their previous match but didn't start
    their latest one
  * (daily at PRICE_CHECK_HOUR) players FPL's official price change predictor
    projects to rise or fall at tonight's price update

Only the Python standard library is used, so there is nothing to install.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- config ----
TEAM_ID = int(os.environ.get("FPL_TEAM_ID", "167421"))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
LOCAL_TZ = ZoneInfo(os.environ.get("LOCAL_TZ", "Europe/Oslo"))
MORNING_HOUR = int(os.environ.get("MORNING_HOUR", "7"))
# Scheduled runs can start a few minutes late, so the window is a bit wider
# than 30 min. With a 15-min schedule the alert lands ~30-45 min before.
PRE_DEADLINE_MINUTES = int(os.environ.get("PRE_DEADLINE_MINUTES", "45"))
PRICE_CHECK_HOUR = int(os.environ.get("PRICE_CHECK_HOUR", "20"))
# FPL likelihood runs -5..5 (sign = direction). 4 = likely, 5 = very likely.
PRICE_MIN_LIKELIHOOD = int(os.environ.get("PRICE_MIN_LIKELIHOOD", "4"))
BENCH_LOOKBACK_HOURS = 96  # only report non-starts from matches this recent
QUIET_WHEN_NO_CHANGES = os.environ.get("QUIET_WHEN_NO_CHANGES", "true") == "true"
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
FORCE = os.environ.get("FORCE_CHECK", "").strip().lower()  # "morning" / "deadline" / "price"

API = "https://fantasy.premierleague.com/api"
STATUS_LABEL = {
    "a": "Available",
    "d": "Doubtful",
    "i": "Injured",
    "s": "Suspended",
    "u": "Unavailable",
    "n": "Not eligible",
}


# --------------------------------------------------------------- helpers ----
def get(path):
    url = f"{API}/{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (fpl-watcher)"})
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last_err = e
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def notify(title, body, priority="default", tags=""):
    print(f"--- {title} ---\n{body}\n")
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set; printed instead of sending.")
        return
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={
            # HTTP headers must be plain ASCII
            "Title": title.encode("ascii", "ignore").decode(),
            "Priority": priority,
            "Tags": tags,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


def flag_text(status, chance):
    label = STATUS_LABEL.get(status, status)
    if status != "a" and chance is not None:
        return f"{label} {chance}%"
    return label


# ----------------------------------------------------------- the checks ----
def which_checks_are_due(state, now):
    due = []
    local_now = now.astimezone(LOCAL_TZ)
    today = local_now.date().isoformat()

    if FORCE == "morning" or (
        local_now.hour >= MORNING_HOUR and state.get("last_morning") != today
    ):
        due.append("morning")

    if FORCE == "price" or (
        local_now.hour >= PRICE_CHECK_HOUR and state.get("last_price_check") != today
    ):
        due.append("price")

    nd = state.get("next_deadline")
    if FORCE == "deadline":
        due.append("deadline")
    elif nd:
        deadline = parse_time(nd["time"])
        mins = (deadline - now).total_seconds() / 60
        if 0 < mins <= PRE_DEADLINE_MINUTES and state.get("last_deadline_event") != nd["event"]:
            due.append("deadline")
    return due


def refresh_deadline_needed(state, now):
    nd = state.get("next_deadline")
    return not nd or parse_time(nd["time"]) <= now


def get_squad(events):
    """Picks from the latest gameweek whose deadline has passed."""
    started = [e for e in events if parse_time(e["deadline_time"]) <= datetime.now(timezone.utc)]
    for ev in reversed(started[-2:]):  # current GW, fall back to previous if not published yet
        try:
            return ev["id"], get(f"entry/{TEAM_ID}/event/{ev['id']}/picks/")["picks"]
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    return None, []


def flag_changes(picks, elements, teams, prev_players):
    lines, snapshot = [], {}
    for p in picks:
        el = elements[p["element"]]
        pid = str(el["id"])
        snap = {
            "status": el["status"],
            "chance": el["chance_of_playing_next_round"],
            "news": el["news"] or "",
        }
        snapshot[pid] = snap
        name = f"{el['web_name']} ({teams[el['team']]})"
        role = "XI" if p["position"] <= 11 else "Bench"
        old = prev_players.get(pid)

        if old is None:
            # New to the squad (transfer). Only mention if already flagged.
            if prev_players and snap["status"] != "a":
                lines.append(f"🆕 {name} [{role}] joined flagged: "
                             f"{flag_text(snap['status'], snap['chance'])} – {snap['news']}")
            continue
        if old == snap:
            continue

        old_flag = flag_text(old["status"], old["chance"])
        new_flag = flag_text(snap["status"], snap["chance"])
        if snap["status"] == "a" and old["status"] != "a":
            lines.append(f"✅ {name} [{role}]: {old_flag} → Available")
        elif old_flag != new_flag:
            serious = snap["status"] in ("i", "s", "u", "n") or (snap["chance"] is not None and snap["chance"] <= 25)
            icon = "🔴" if serious else "🟠"
            news = f" – {snap['news']}" if snap["news"] else ""
            lines.append(f"{icon} {name} [{role}]: {old_flag} → {new_flag}{news}")
        elif snap["news"] != old["news"]:
            lines.append(f"📰 {name} [{role}] news: {snap['news'] or '(cleared)'}")
    return lines, snapshot


def bench_surprises(picks, elements, teams, state, now):
    fixtures = {f["id"]: f for f in get("fixtures/")}
    alerted = state.get("bench_alerted", [])
    lines = []
    for p in picks:
        el = elements[p["element"]]
        history = get(f"element-summary/{el['id']}/")["history"]
        played = sorted(
            (h for h in history if fixtures.get(h["fixture"], {}).get("finished_provisional")),
            key=lambda h: h["kickoff_time"],
        )
        if len(played) < 2:
            continue
        prev, last = played[-2], played[-1]
        key = f"{el['id']}:{last['fixture']}"
        recent = now - parse_time(last["kickoff_time"]) <= timedelta(hours=BENCH_LOOKBACK_HOURS)
        if key in alerted or not recent:
            continue
        if prev.get("starts") == 1 and last.get("starts") == 0:
            role = "XI" if p["position"] <= 11 else "Bench"
            mins = last.get("minutes", 0)
            how = f"came on for {mins} min" if mins else "didn't play"
            lines.append(f"🪑 {el['web_name']} ({teams[el['team']]}) [{role}]: "
                         f"started last time, benched this time ({how})")
            alerted.append(key)
    state["bench_alerted"] = alerted[-200:]
    return lines


def price_alerts(picks, elements, teams, now, include_all=False):
    """Players FPL projects to change price at tonight's update."""
    hits, closest = [], []
    for p in picks:
        el = elements[p["element"]]
        locked = el.get("price_change_locked_until")
        if locked and parse_time(locked) > now:
            continue
        tonight = next((x for x in el.get("price_change_projections") or [] if x.get("offset") == 0), None)
        if not tonight:
            continue
        try:
            projected = float(tonight["projected_percent"])
        except (TypeError, ValueError):
            continue
        likelihood = int(tonight.get("likelihood") or 0)
        name = f"{el['web_name']} ({teams[el['team']]})"
        role = "XI" if p["position"] <= 11 else "Bench"
        price = el["now_cost"] / 10
        rising = projected > 0
        new_price = price + (0.1 if rising else -0.1)
        calib = " (FPL still calibrating)" if el.get("price_change_calibrating") else ""
        line = (f"{'📈' if rising else '📉'} {name} [{role}] £{price:.1f}m → £{new_price:.1f}m: "
                f"{abs(projected):.0f}% projected tonight{calib}")
        closest.append((abs(projected), line))
        if abs(likelihood) >= PRICE_MIN_LIKELIHOOD or abs(projected) >= 100:
            hits.append(line)
    if include_all and not hits:
        closest.sort(reverse=True)
        return [], [l for _, l in closest[:3]]
    return hits, []


# ------------------------------------------------------------------ main ----
def main():
    now = datetime.now(timezone.utc)
    state = load_state()
    first_run = not state.get("players")

    due = which_checks_are_due(state, now)
    if not due and not refresh_deadline_needed(state, now):
        print("Nothing due.")
        return

    boot = get("bootstrap-static/")
    events = boot["events"]
    elements = {e["id"]: e for e in boot["elements"]}
    teams = {t["id"]: t["short_name"] for t in boot["teams"]}

    nxt = next((e for e in events if e["is_next"]), None)
    state["next_deadline"] = {"event": nxt["id"], "time": nxt["deadline_time"]} if nxt else None

    # The refreshed deadline may itself make the pre-deadline check due.
    due = which_checks_are_due(state, now)
    if not due:
        save_state(state)
        print("Deadline cache refreshed; nothing due.")
        return

    gw, picks = get_squad(events)
    if not picks:
        save_state(state)
        print("No squad found yet (season not started?).")
        return

    local_today = now.astimezone(LOCAL_TZ).date().isoformat()

    if "price" in due:
        hits, closest = price_alerts(picks, elements, teams, now, include_all=(FORCE == "price"))
        if hits:
            notify("FPL price changes tonight", "\n".join(hits), priority="high", tags="moneybag")
        elif closest:
            notify("FPL price check (test)",
                   "No one projected to change tonight. Closest:\n" + "\n".join(closest), tags="moneybag")
        if FORCE != "price":  # a manual test shouldn't use up tonight's real check
            state["last_price_check"] = local_today

    if "morning" not in due and "deadline" not in due:
        save_state(state)
        return

    flag_lines, snapshot = flag_changes(picks, elements, teams, state.get("players", {}))
    bench_lines = bench_surprises(picks, elements, teams, state, now) if "morning" in due else []
    state["players"] = snapshot

    if first_run:
        flagged = [f"{elements[p['element']]['web_name']}: "
                   f"{flag_text(elements[p['element']]['status'], elements[p['element']]['chance_of_playing_next_round'])}"
                   for p in picks if elements[p["element"]]["status"] != "a"]
        body = f"Watching {len(picks)} players from GW{gw}."
        if flagged:
            body += "\nCurrently flagged:\n" + "\n".join(flagged)
        if bench_lines:
            body += "\n\n" + "\n".join(bench_lines)
        notify("FPL watcher is live", body, tags="soccer")
    else:
        lines = flag_lines + bench_lines
        if "deadline" in due and nxt:
            dl_local = parse_time(nxt["deadline_time"]).astimezone(LOCAL_TZ)
            mins = max(0, int((parse_time(nxt["deadline_time"]) - now).total_seconds() // 60))
            title = f"GW{nxt['id']} deadline {dl_local:%H:%M} ({mins} min)"
            if lines or not QUIET_WHEN_NO_CHANGES:
                notify(title, "\n".join(lines) or "No changes in your squad.",
                       priority="high", tags="alarm_clock")
        elif lines or not QUIET_WHEN_NO_CHANGES:
            notify("FPL morning check", "\n".join(lines) or "No changes in your squad.",
                   tags="soccer")

    if "morning" in due:
        state["last_morning"] = local_today
    if "deadline" in due and nxt:
        state["last_deadline_event"] = nxt["id"]
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # make failures visible in the Actions log
        print(f"Error: {exc}", file=sys.stderr)
        raise
