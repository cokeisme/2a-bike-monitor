"""
CDC2W bike-booking monitor -> Telegram.

Alerts you about class 2A/2 test dates, in two groups:
  - AVAILABLE ON APP: dates now inside the app's booking window
  - HIDDEN: dates already in the database but not yet shown on the app
Each date line says how many bikes are free and whether bike 159 is available.

Setup:
  pip install requests
  export FIREBASE_API_KEY="AIza..."      # copy from the token?key=... URL
  export TELEGRAM_BOT_TOKEN="123456:ABC..."
  export TELEGRAM_CHAT_ID="123456789"

Test without Telegram (one check, prints what it sees):
  python cdc2w_monitor.py --once

Run for real (keeps running, checks every 10 min):
  python cdc2w_monitor.py

One check then exit (used by the GitHub Actions workflow):
  python cdc2w_monitor.py --check
"""
import json
import os
import platform
import random
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

API_KEY = os.environ["FIREBASE_API_KEY"]
DB_URL = os.getenv(
    "DB_URL",
    "https://cdc2w-fleet-default-rtdb.asia-southeast1.firebasedatabase.app/state.json",
)
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

WATCH_BIKE_TAG = os.getenv("WATCH_BIKE_TAG", "159")
BIKE_CLASSES = {"2A", "2"}                                  # "Class 2A / 2" tab
# The app shows today + 14 days (e.g. 19 Oct -> 2 Nov), so only dates inside
# that window count as "new dates shown on the app".
WINDOW_DAYS = int(os.getenv("BOOKING_WINDOW_DAYS", "14"))
CHECK_EVERY = int(os.getenv("CHECK_EVERY_SECONDS", "600"))  # 10 min
STATE_FILE = "seen_state.json"   # remembers which dates were already reported
AUTH_FILE = "auth_state.json"    # remembers the anonymous sign-in so we don't create a new one every run
TZ = ZoneInfo("Asia/Singapore")
INACTIVE = {"cancelled", "canceled", "completed"}          # reservations that don't block a bike

SITE = {"Origin": "https://2wfleetmanagement.netlify.app",
        "Referer": "https://2wfleetmanagement.netlify.app/"}


# ---------- auth: anonymous Firebase sign-in, same as the web app ----------
class Auth:
    def __init__(self):
        self.id_token = None
        self.refresh = None
        self.expires = 0.0
        try:
            with open(AUTH_FILE) as f:
                self.refresh = json.load(f).get("refresh")
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            with open(AUTH_FILE, "w") as f:
                json.dump({"refresh": self.refresh}, f)
        except OSError:
            pass

    def token(self):
        if self.id_token and time.time() < self.expires - 120:
            return self.id_token
        if self.refresh:  # reuse the saved anonymous sign-in
            r = requests.post(
                f"https://securetoken.googleapis.com/v1/token?key={API_KEY}",
                data={"grant_type": "refresh_token", "refresh_token": self.refresh},
                headers=SITE, timeout=20)
            if r.ok:
                j = r.json()
                self.id_token, self.refresh, ttl = j["id_token"], j["refresh_token"], int(j["expires_in"])
                self.expires = time.time() + ttl
                self._save()
                return self.id_token
            self.refresh = None  # saved sign-in no longer valid -> make a new one
        r = requests.post(
            f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={API_KEY}",
            json={"returnSecureToken": True}, headers=SITE, timeout=20)
        r.raise_for_status()
        j = r.json()
        self.id_token, self.refresh, ttl = j["idToken"], j["refreshToken"], int(j["expiresIn"])
        self.expires = time.time() + ttl
        self._save()
        return self.id_token


def fetch_state(auth):
    r = requests.get(DB_URL, params={"auth": auth.token()}, headers=SITE, timeout=30)
    r.raise_for_status()
    data = r.json()
    return json.loads(data) if isinstance(data, str) else data  # blob is double-encoded


# ---------- parsing (finds the lists by their fields, not by key name) ----------
def find_lists(node, keys):
    if isinstance(node, list):
        if node and isinstance(node[0], dict) and keys <= node[0].keys():
            yield node
        for x in node:
            yield from find_lists(x, keys)
    elif isinstance(node, dict):
        vals = list(node.values())
        if vals and all(isinstance(v, dict) for v in vals) and keys <= vals[0].keys():
            yield vals
        for v in vals:
            yield from find_lists(v, keys)


def is_2a_track(track):
    t = str(track).upper()
    return t.startswith("2") and not t.startswith("2B")


def check(auth, today):
    """Returns {iso_date: {"free", "watch", "visible"}} for all upcoming class 2A/2 dates.
    visible=True: inside the app's window. visible=False: in the database but
    still hidden on the app."""
    state = fetch_state(auth)
    bikes = next(find_lists(state, {"tag", "plate", "status"}), [])
    resv = next(find_lists(state, {"tag", "date", "status"}), [])
    days = next(find_lists(state, {"id", "date", "track"}), [])
    if not (bikes and days):
        raise RuntimeError("Unexpected data layout: bikes or test days not found")

    booked = {(str(r["tag"]), r["date"]) for r in resv
              if str(r.get("status", "")).lower() not in INACTIVE}
    watch = next((b for b in bikes if str(b["tag"]) == WATCH_BIKE_TAG), None)
    last = today + timedelta(days=WINDOW_DAYS)

    out = {}
    for d in days:
        try:
            dt = datetime.strptime(d["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if not (dt >= today and is_2a_track(d["track"])):
            continue
        free = [b for b in bikes if b.get("class") in BIKE_CLASSES
                and b["status"] == "available" and (str(b["tag"]), d["date"]) not in booked]
        if watch is None:
            w = "not in fleet list"
        elif watch["status"] != "available":
            w = f"unavailable ({watch['status']})"
        elif (str(watch["tag"]), d["date"]) in booked:
            w = "already booked"
        else:
            w = "AVAILABLE"
        out[d["date"]] = {"free": len(free), "watch": w, "visible": dt <= last}
    return out


# ---------- telegram + state ----------
def run_footer():
    """When and where this check ran (added to every Telegram message)."""
    when = datetime.now(TZ).strftime("%a %d %b %Y, %H:%M:%S SGT")
    if os.getenv("GITHUB_ACTIONS") == "true":
        kind = "manual" if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch" else "scheduled"
        where = f"GitHub Actions ({kind} run #{os.getenv('GITHUB_RUN_NUMBER', '?')}, {os.getenv('RUNNER_OS', '?')} runner)"
        repo, run_id = os.getenv("GITHUB_REPOSITORY"), os.getenv("GITHUB_RUN_ID")
        server = os.getenv("GITHUB_SERVER_URL", "https://github.com")
        link = f"\nRun log: {server}/{repo}/actions/runs/{run_id}" if repo and run_id else ""
    else:
        where = f"{platform.node()} ({platform.system()}), local script"
        link = ""
    return f"\n\n--\nChecked: {when}\nRan on: {where}{link}"


def send(msg):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT, "text": msg + run_footer(),
                            "disable_web_page_preview": "true"}, timeout=15)
    r.raise_for_status()


def date_line(iso, i):
    day = datetime.strptime(iso, "%Y-%m-%d").date()
    s = f"- {day.strftime('%a %d %b')}: {i['free']} bikes free | Bike {WATCH_BIKE_TAG}: {i['watch']}"
    if not i["visible"]:
        shows = day - timedelta(days=WINDOW_DAYS)
        s += f" | expected on app ~{shows.strftime('%a %d %b')}"
    return s


def describe(info, dates, vis_title="AVAILABLE ON APP",
             hid_title="HIDDEN (in database, not on app yet)"):
    vis = sorted(d for d in dates if info[d]["visible"])
    hid = sorted(d for d in dates if not info[d]["visible"])
    parts = []
    if vis:
        parts.append(f"{vis_title}:\n" + "\n".join(date_line(d, info[d]) for d in vis))
    if hid:
        parts.append(f"{hid_title}:\n" + "\n".join(date_line(d, info[d]) for d in hid))
    return "\n\n".join(parts)


def load_state():
    """Returns (known_dates, dates_already_reported_as_on_app) or None on first run."""
    try:
        with open(STATE_FILE) as f:
            j = json.load(f)
        return set(j["known"]), set(j["visible"])
    except (OSError, ValueError, KeyError):
        return None


def save_state(known, visible, today):
    cut = today.isoformat()
    known = {d for d in known if d >= cut}
    visible = {d for d in visible if d >= cut}
    with open(STATE_FILE, "w") as f:
        json.dump({"known": sorted(known), "visible": sorted(visible)}, f)
    return known, visible


def inspect():
    """--days: print test-day records + database layout (no bike/learner rows)."""
    state = fetch_state(Auth())
    print("Top-level keys:")
    for k, v in state.items():
        size = len(v) if isinstance(v, (list, dict)) else "-"
        print(f"  {k}: {type(v).__name__} ({size})")
        small = json.dumps(v)
        if not isinstance(v, list) and len(small) < 300:
            print(f"     value: {small}")
    print("\nTest-day records:")
    for d in next(find_lists(state, {"id", "date", "track"}), []):
        print(" ", json.dumps(d))


def main():
    if "--days" in sys.argv:
        return inspect()
    once = "--once" in sys.argv       # print only, no Telegram
    single = "--check" in sys.argv    # one real check with Telegram, then exit (for GitHub Actions)
    if not once and not (TG_TOKEN and TG_CHAT):
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")

    auth = Auth()
    state = load_state()
    fails, warned = 0, False

    while True:
        try:
            today = datetime.now(TZ).date()
            info = check(auth, today)
            if once:
                print(describe(info, info) or "No upcoming 2A/2 dates found.")
                return
            now_all = set(info)
            now_vis = {d for d, i in info.items() if i["visible"]}
            if state is None:  # first ever run: baseline
                send("Monitor started.\n\n" + (describe(info, info) or "No upcoming 2A/2 dates."))
                state = save_state(now_all, now_vis, today)
            else:
                known, seen_vis = state
                new_vis = now_vis - seen_vis            # newly shown on the app
                new_hid = (now_all - now_vis) - known   # newly added, still hidden
                if new_vis or new_hid:
                    send("2A/2 test date update!\n\n" + describe(
                        info, new_vis | new_hid,
                        "NOW AVAILABLE ON APP",
                        "NEW HIDDEN DATE (in database, not on app yet)"))
                state = save_state(known | now_all, seen_vis | now_vis, today)
            fails, warned = 0, False
            if single:
                return
        except Exception as e:
            if once or single:
                raise  # non-zero exit, so GitHub marks the run as failed and emails you
            fails += 1
            print("Check failed:", e)
            if fails >= 5 and not warned:
                try:
                    send(f"Monitor is failing: {e}")
                    warned = True
                except Exception:
                    pass
        time.sleep(CHECK_EVERY + random.randint(0, 30))


if __name__ == "__main__":
    main()
