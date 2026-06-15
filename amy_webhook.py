"""
Amy Smart Reply — Webhook Server
Receives Connecteam "Chat message created" events, reads conversation context,
and generates a reply via Claude Haiku.

Deploy on Render: uvicorn amy_webhook:app --host 0.0.0.0 --port $PORT
"""

import os, json, time, base64, logging, re, datetime, requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

try:
    from zoneinfo import ZoneInfo
    AEST = ZoneInfo("Australia/Melbourne")
except ImportError:
    class _AEST:
        def utcoffset(self, dt): return datetime.timedelta(hours=10)
        def tzname(self, dt): return "AEST"
        def dst(self, dt): return datetime.timedelta(0)
    AEST = _AEST()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
CONNECTEAM_API_KEY   = os.environ.get("CONNECTEAM_API_KEY", "")
CONNECTEAM_SENDER_ID = os.environ.get("CONNECTEAM_SENDER_ID", "")
ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN         = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO          = os.environ.get("GITHUB_REPO", "Yusufsai-bit/connect-care-audit")
CC_MGMT_CONV_ID      = os.environ.get("CC_MGMT_CONV_ID", "")
if not CC_MGMT_CONV_ID:
    raise RuntimeError("CC_MGMT_CONV_ID environment variable is not set")
BASE_URL             = "https://api.connecteam.com"

STAFF_IDS        = {"2149475", "9736871", "2201497"}  # Yusuf, Nada, Faduma
CONVO_LOG_FILE   = "amy_conversation_log.json"
CONVO_EXPIRY_DAYS = 7

conversation_log  = {}
_time_clock_id    = None
_scheduler_id     = None
_users_cache      = []
_users_cache_ts   = 0.0

app = FastAPI()


# ── GitHub persistence ─────────────────────────────────────────────────────────

def load_from_github() -> dict:
    try:
        r = requests.get(
            f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{CONVO_LOG_FILE}",
            timeout=10,
        )
        if r.ok:
            data = r.json()
            cutoff = time.time() - CONVO_EXPIRY_DAYS * 86400
            return {
                uid: v for uid, v in data.items()
                if v.get("messages") and v["messages"][-1].get("ts", 0) >= cutoff
            }
    except Exception as e:
        logger.warning(f"Could not load conversation log from GitHub: {e}")
    return {}


def save_to_github(data: dict):
    if not GITHUB_TOKEN:
        logger.warning("GITHUB_TOKEN not set — skipping GitHub persist")
        return
    try:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CONVO_LOG_FILE}",
            headers={"Authorization": f"token {GITHUB_TOKEN}"},
            timeout=10,
        )
        sha = r.json().get("sha", "") if r.ok else ""
        encoded = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
        requests.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CONVO_LOG_FILE}",
            headers={"Authorization": f"token {GITHUB_TOKEN}", "Content-Type": "application/json"},
            json={"message": "chore: update amy conversation log [skip ci]", "content": encoded, "sha": sha},
            timeout=15,
        )
        logger.info("Conversation log saved to GitHub")
    except Exception as e:
        logger.error(f"Failed to save to GitHub: {e}")


# ── Startup ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    global conversation_log
    conversation_log = load_from_github()
    logger.info(f"Loaded {len(conversation_log)} worker conversation(s) from GitHub")
    _discover_resource_ids()


# ── Connecteam helpers ─────────────────────────────────────────────────────────

def _h():
    return {"X-API-KEY": CONNECTEAM_API_KEY}


def _discover_resource_ids():
    global _time_clock_id, _scheduler_id
    if not CONNECTEAM_API_KEY:
        return
    try:
        r = requests.get(f"{BASE_URL}/time-clock/v1/time-clocks", headers=_h(), timeout=10)
        if r.ok:
            clocks = r.json().get("data", {}).get("timeClocks", [])
            if clocks:
                _time_clock_id = clocks[0].get("id")
                logger.info(f"Time clock ID: {_time_clock_id}")
    except Exception as e:
        logger.warning(f"Time clock discovery failed: {e}")
    try:
        r = requests.get(f"{BASE_URL}/scheduler/v1/schedulers", headers=_h(), timeout=10)
        if r.ok:
            schedulers = r.json().get("data", {}).get("schedulers", [])
            if schedulers:
                _scheduler_id = schedulers[0].get("id")
                logger.info(f"Scheduler ID: {_scheduler_id}")
    except Exception as e:
        logger.warning(f"Scheduler discovery failed: {e}")


def _get_users() -> list:
    global _users_cache, _users_cache_ts
    if _users_cache and time.time() - _users_cache_ts < 300:
        return _users_cache
    try:
        r = requests.get(f"{BASE_URL}/users/v1/users", headers=_h(), timeout=15)
        if r.ok:
            _users_cache = r.json().get("data", {}).get("users", [])
            _users_cache_ts = time.time()
    except Exception as e:
        logger.warning(f"Users fetch failed: {e}")
    return _users_cache


def _uid(obj: dict) -> str:
    return str(obj.get("id") or obj.get("userId") or obj.get("user_id") or "")


def _user_name(user_id, users: list) -> str:
    s = str(user_id)
    for u in users:
        if _uid(u) == s:
            return f"{u.get('firstName', '')} {u.get('lastName', '')}".strip() or f"User {s}"
    return f"User {s}"


def _find_user(name: str, users: list):
    nl = name.lower()
    for u in users:
        full = f"{u.get('firstName', '')} {u.get('lastName', '')}".lower()
        if nl in full:
            return u
    return None


def _parse_dt(s: str):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _fmt(dt) -> str:
    if not dt:
        return "?"
    try:
        return dt.astimezone(AEST).strftime("%H:%M")
    except Exception:
        return str(dt)


def _date_range(period: str):
    today = datetime.date.today()
    if period == "tomorrow":
        d = today + datetime.timedelta(days=1)
        return d.isoformat(), d.isoformat()
    if period == "yesterday":
        d = today - datetime.timedelta(days=1)
        return d.isoformat(), d.isoformat()
    if period == "last_week":
        mon = today - datetime.timedelta(days=today.weekday() + 7)
        return mon.isoformat(), (mon + datetime.timedelta(days=6)).isoformat()
    if period in ("this_week", "week"):
        mon = today - datetime.timedelta(days=today.weekday())
        return mon.isoformat(), (mon + datetime.timedelta(days=6)).isoformat()
    if period == "this_weekend":
        days = (5 - today.weekday()) % 7 or 7
        sat = today + datetime.timedelta(days=days)
        return sat.isoformat(), (sat + datetime.timedelta(days=1)).isoformat()
    return today.isoformat(), today.isoformat()


# ── Data fetchers ──────────────────────────────────────────────────────────────

def _fetch_time_activities(start: str, end: str, user_ids: list = None) -> list:
    if not _time_clock_id:
        _discover_resource_ids()
    if not _time_clock_id:
        return []
    params = {"startDate": start, "endDate": end}
    if user_ids:
        params["userIds"] = user_ids
    try:
        r = requests.get(
            f"{BASE_URL}/time-clock/v1/time-clocks/{_time_clock_id}/time-activities",
            headers=_h(), params=params, timeout=15,
        )
        if r.ok:
            return r.json().get("data", {}).get("timeActivities", [])
        logger.warning(f"Time activities {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"Time activities failed: {e}")
    return []


def _fetch_shifts(start: str, end: str) -> list:
    if not _scheduler_id:
        _discover_resource_ids()
    if not _scheduler_id:
        return []
    try:
        r = requests.get(
            f"{BASE_URL}/scheduler/v2/schedulers/{_scheduler_id}/shifts",
            headers=_h(), params={"startDate": start, "endDate": end}, timeout=15,
        )
        if r.ok:
            return r.json().get("data", {}).get("shifts", [])
        logger.warning(f"Shifts {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"Shifts failed: {e}")
    return []


def _fetch_time_off() -> list:
    try:
        r = requests.get(f"{BASE_URL}/time-off/v1/requests", headers=_h(), timeout=15)
        if r.ok:
            return r.json().get("data", {}).get("requests", [])
    except Exception as e:
        logger.warning(f"Time off failed: {e}")
    return []


def _fetch_timesheet(start: str, end: str, user_ids: list = None) -> dict:
    if not _time_clock_id:
        _discover_resource_ids()
    if not _time_clock_id:
        return {}
    params = {"startDate": start, "endDate": end}
    if user_ids:
        params["userIds"] = user_ids
    try:
        r = requests.get(
            f"{BASE_URL}/time-clock/v1/time-clocks/{_time_clock_id}/timesheet",
            headers=_h(), params=params, timeout=15,
        )
        if r.ok:
            return r.json().get("data", {})
    except Exception as e:
        logger.warning(f"Timesheet failed: {e}")
    return {}


def _fetch_tasks() -> list:
    all_tasks = []
    try:
        r = requests.get(f"{BASE_URL}/tasks/v1/taskboards", headers=_h(), timeout=10)
        if not r.ok:
            return []
        boards = r.json().get("data", {}).get("taskBoards", [])
        for board in boards[:3]:
            bid = board.get("id")
            if not bid:
                continue
            r2 = requests.get(f"{BASE_URL}/tasks/v1/taskboards/{bid}/tasks", headers=_h(), timeout=15)
            if r2.ok:
                tasks = r2.json().get("data", {}).get("tasks", [])
                for t in tasks:
                    t["_board"] = board.get("name") or board.get("title") or ""
                all_tasks.extend(tasks)
    except Exception as e:
        logger.warning(f"Tasks failed: {e}")
    return all_tasks


def _fetch_forms() -> list:
    results = []
    try:
        r = requests.get(f"{BASE_URL}/forms/v1/forms", headers=_h(), timeout=10)
        if not r.ok:
            return []
        for form in r.json().get("data", {}).get("forms", [])[:5]:
            fid = form.get("id")
            subs = []
            if fid:
                sr = requests.get(f"{BASE_URL}/forms/v1/forms/{fid}/form-submissions", headers=_h(), timeout=10)
                if sr.ok:
                    subs = sr.json().get("data", {}).get("formSubmissions", [])
            results.append({"name": form.get("name") or form.get("title") or "Form", "id": fid, "subs": subs})
    except Exception as e:
        logger.warning(f"Forms failed: {e}")
    return results


def _fetch_pay_rates(user_id: str = None) -> list:
    try:
        r = requests.get(f"{BASE_URL}/pay-rates/v1/pay-rates", headers=_h(), timeout=10)
        if r.ok:
            rates = r.json().get("data", {}).get("payRates", [])
            if user_id:
                rates = [x for x in rates if str(x.get("userId") or x.get("user_id") or "") == str(user_id)]
            return rates
    except Exception as e:
        logger.warning(f"Pay rates failed: {e}")
    return []


def _fetch_unavailabilities(start: str, end: str) -> list:
    if not _scheduler_id:
        return []
    try:
        r = requests.get(
            f"{BASE_URL}/scheduler/v2/schedulers/user-unavailability",
            headers=_h(), params={"startDate": start, "endDate": end}, timeout=15,
        )
        if r.ok:
            return r.json().get("data", {}).get("userUnavailabilities", [])
    except Exception as e:
        logger.warning(f"Unavailabilities failed: {e}")
    return []


# ── Intent detection ───────────────────────────────────────────────────────────

_KW_CLOCK    = {"clock", "clocked", "late", "attendance", "checked in", "check in",
                "who's in", "who is in", "not in", "on site", "arrived", "clocking",
                "clock out", "clocked out", "still in", "still clocked"}
_KW_SCHEDULE = {"shift", "roster", "schedule", "scheduled", "working", "on duty",
                "rota", "who's on", "who is on", "on shift", "next shift", "start time"}
_KW_TIME_OFF = {"time off", "leave", "sick", "holiday", "unavailable", "day off",
                "absence", "absent", "annual leave", "sick leave", "away"}
_KW_HOURS    = {"hours", "timesheet", "total hours", "worked", "how long",
                "how many hours", "overtime"}
_KW_TASKS    = {"task", "to-do", "todo", "overdue", "taskboard", "checklist", "outstanding"}
_KW_FORMS    = {"form", "submission", "submitted", "filled", "incident", "report form", "completed form"}
_KW_USERS    = {"staff", "employees", "team members", "list of", "active users", "who are our"}
_KW_PAY      = {"pay rate", "pay rates", "wage", "wages", "salary", "hourly rate"}

_STOP = {"amy", "hey", "hi", "did", "anyone", "can", "you", "check", "tell", "me",
         "what", "who", "how", "when", "is", "are", "the", "a", "an", "for", "of",
         "in", "on", "at", "to", "has", "have", "been", "was", "were", "any", "today",
         "tomorrow", "yesterday", "this", "last", "week", "weekend", "connect", "care",
         "there", "their", "they", "just", "please", "could", "would", "should"}


def _detect_intent(text: str) -> dict:
    t = text.lower()
    intent = {
        "time_clock": any(k in t for k in _KW_CLOCK),
        "scheduler":  any(k in t for k in _KW_SCHEDULE),
        "time_off":   any(k in t for k in _KW_TIME_OFF),
        "hours":      any(k in t for k in _KW_HOURS),
        "tasks":      any(k in t for k in _KW_TASKS),
        "forms":      any(k in t for k in _KW_FORMS),
        "users":      any(k in t for k in _KW_USERS),
        "pay":        any(k in t for k in _KW_PAY),
        "period":     "today",
        "person":     None,
    }
    if "tomorrow" in t:
        intent["period"] = "tomorrow"
    elif "yesterday" in t:
        intent["period"] = "yesterday"
    elif "last week" in t:
        intent["period"] = "last_week"
    elif any(k in t for k in ("this week", "week")):
        intent["period"] = "this_week"
    elif any(k in t for k in ("weekend", "saturday", "sunday")):
        intent["period"] = "this_weekend"

    words = re.findall(r'\b[A-Z][a-z]+\b', text)
    names = [w for w in words if w.lower() not in _STOP]
    if names:
        intent["person"] = " ".join(names)

    return intent


# ── Context builder ────────────────────────────────────────────────────────────

def build_context(message_text: str) -> str:
    intent = _detect_intent(message_text)
    needs_data = any(intent[k] for k in ("time_clock", "scheduler", "time_off", "hours", "tasks", "forms", "users", "pay"))
    if not needs_data:
        return ""

    users  = _get_users()
    start, end = _date_range(intent["period"])
    now_aest   = datetime.datetime.now(AEST).strftime("%A, %d %B %Y %H:%M")

    target_uid  = None
    target_uids = None
    if intent["person"] and users:
        u = _find_user(intent["person"], users)
        if u:
            target_uid  = _uid(u)
            target_uids = [int(target_uid)] if target_uid else None

    parts = [f"Current time (AEST): {now_aest}. Data period: {start} to {end}."]

    # ── Clock-in vs scheduled shift comparison ─────────────────────────────────
    if intent["time_clock"]:
        activities = _fetch_time_activities(start, end, target_uids)
        shifts     = _fetch_shifts(start, end)

        act_map   = {}
        for a in activities:
            act_map.setdefault(_uid(a), []).append(a)

        shift_map = {}
        for s in shifts:
            shift_map.setdefault(_uid(s), []).append(s)

        all_uids = sorted(set(act_map) | set(shift_map))
        lines = [f"\n=== CLOCK-IN DATA ({start}) ==="]
        for uid in all_uids:
            if not uid:
                continue
            name = _user_name(uid, users)
            acts = act_map.get(uid, [])
            shs  = shift_map.get(uid, [])
            sch_start = _parse_dt((shs[0].get("startTime") or shs[0].get("start") or "") if shs else "")

            if acts:
                for a in acts:
                    ci = _parse_dt(a.get("clockInTime") or a.get("startTime") or a.get("clockIn") or "")
                    co = _parse_dt(a.get("clockOutTime") or a.get("endTime") or a.get("clockOut") or "")
                    row = [name]
                    if ci:
                        row.append(f"in {_fmt(ci)}")
                        if sch_start:
                            diff = int((ci - sch_start).total_seconds() / 60)
                            if diff > 5:
                                row.append(f"⚠ {diff}min LATE (sched {_fmt(sch_start)})")
                            elif diff < -5:
                                row.append(f"{abs(diff)}min early")
                            else:
                                row.append("on time")
                    if co:
                        row.append(f"out {_fmt(co)}")
                    elif ci:
                        row.append("still clocked in")
                    lines.append("  " + " | ".join(row))
            else:
                sched = f"sched {_fmt(sch_start)}" if sch_start else "scheduled today"
                lines.append(f"  {name} | NOT clocked in ({sched})")

        if len(lines) == 1:
            lines.append("  No clock-in records or scheduled shifts found.")
        parts.append("\n".join(lines))

    # ── Roster / scheduled shifts ──────────────────────────────────────────────
    elif intent["scheduler"]:
        shifts = _fetch_shifts(start, end)
        lines  = [f"\n=== SCHEDULED SHIFTS ({start} to {end}) ==="]
        for s in shifts:
            uid  = _uid(s)
            name = _user_name(uid, users)
            st   = _parse_dt(s.get("startTime") or s.get("start") or "")
            et   = _parse_dt(s.get("endTime") or s.get("end") or "")
            job  = s.get("jobName") or s.get("job") or ""
            st_s = st.astimezone(AEST).strftime("%a %d %b %H:%M") if st else "?"
            line = f"  {name}: {st_s} → {_fmt(et)}"
            if job:
                line += f" ({job})"
            lines.append(line)
        if len(lines) == 1:
            lines.append("  No shifts found for this period.")
        parts.append("\n".join(lines))

    # ── Timesheet / hours worked ───────────────────────────────────────────────
    if intent["hours"]:
        sheet = _fetch_timesheet(start, end, target_uids)
        lines = [f"\n=== TIMESHEET ({start} to {end}) ==="]
        entries = (sheet.get("timesheetEntries") or sheet.get("users") or
                   sheet.get("entries") or [])
        if entries:
            for e in entries:
                uid   = _uid(e)
                name  = _user_name(uid, users)
                total = e.get("totalMinutes") or e.get("total") or e.get("minutes") or 0
                if isinstance(total, (int, float)) and total > 0:
                    h, m = divmod(int(total), 60)
                    lines.append(f"  {name}: {h}h {m}m")
                else:
                    lines.append(f"  {name}: {e.get('totalHours') or total}")
        elif sheet:
            lines.append(f"  Raw: {json.dumps(sheet)[:400]}")
        else:
            lines.append("  No timesheet data found.")
        parts.append("\n".join(lines))

    # ── Time off requests ──────────────────────────────────────────────────────
    if intent["time_off"]:
        reqs  = _fetch_time_off()
        lines = ["\n=== TIME OFF REQUESTS ==="]
        for req in reqs[:25]:
            uid     = _uid(req)
            name    = _user_name(uid, users)
            status  = req.get("status") or req.get("approvalStatus") or "?"
            start_d = req.get("startDate") or req.get("from") or req.get("start") or ""
            end_d   = req.get("endDate") or req.get("to") or req.get("end") or ""
            ptype   = req.get("policyTypeName") or req.get("type") or req.get("reason") or ""
            lines.append(f"  {name}: {ptype} {start_d}–{end_d} [{status}]")
        if len(lines) == 1:
            lines.append("  No time off requests found.")
        parts.append("\n".join(lines))

    # ── Unavailabilities ───────────────────────────────────────────────────────
        unavail = _fetch_unavailabilities(start, end)
        if unavail:
            lines2 = [f"\n=== UNAVAILABILITIES ({start} to {end}) ==="]
            for u in unavail[:15]:
                uid   = _uid(u)
                name  = _user_name(uid, users)
                start_d = u.get("startDate") or u.get("start") or ""
                end_d   = u.get("endDate") or u.get("end") or ""
                reason  = u.get("reason") or u.get("note") or ""
                lines2.append(f"  {name}: {start_d}–{end_d}" + (f" ({reason})" if reason else ""))
            parts.append("\n".join(lines2))

    # ── Tasks ──────────────────────────────────────────────────────────────────
    if intent["tasks"]:
        tasks = _fetch_tasks()
        lines = ["\n=== TASKS ==="]
        for task in tasks[:25]:
            title  = task.get("title") or task.get("name") or "Untitled"
            status = task.get("status") or task.get("statusName") or ""
            board  = task.get("_board", "")
            due    = task.get("dueDate") or task.get("due_date") or ""
            a_id   = str(task.get("assigneeId") or task.get("assignedUserId") or "")
            assignee = _user_name(a_id, users) if a_id else "Unassigned"
            row = f"  [{status}] {title}"
            if board:
                row += f" ({board})"
            row += f" — {assignee}"
            if due:
                row += f" | due {due}"
            lines.append(row)
        if len(lines) == 1:
            lines.append("  No tasks found.")
        parts.append("\n".join(lines))

    # ── Forms & submissions ────────────────────────────────────────────────────
    if intent["forms"]:
        forms_data = _fetch_forms()
        lines = ["\n=== FORMS & SUBMISSIONS ==="]
        for fd in forms_data:
            lines.append(f"  {fd['name']}: {len(fd['subs'])} submission(s)")
            for sub in fd["subs"][:5]:
                uid   = _uid(sub)
                sname = _user_name(uid, users)
                ts    = sub.get("submittedAt") or sub.get("createdAt") or ""
                lines.append(f"    - {sname} ({ts[:10] if ts else 'unknown date'})")
        if len(lines) == 1:
            lines.append("  No form data found.")
        parts.append("\n".join(lines))

    # ── Pay rates ──────────────────────────────────────────────────────────────
    if intent["pay"]:
        rates = _fetch_pay_rates(target_uid)
        lines = ["\n=== PAY RATES ==="]
        for rate in rates[:20]:
            uid  = _uid(rate)
            name = _user_name(uid, users)
            amt  = rate.get("rate") or rate.get("amount") or rate.get("hourlyRate") or "?"
            rtype = rate.get("type") or rate.get("rateType") or ""
            lines.append(f"  {name}: ${amt}/hr" + (f" ({rtype})" if rtype else ""))
        if len(lines) == 1:
            lines.append("  No pay rate data found.")
        parts.append("\n".join(lines))

    # ── Team list ──────────────────────────────────────────────────────────────
    if intent["users"] and not any(intent[k] for k in ("time_clock", "scheduler", "time_off")):
        lines = ["\n=== TEAM MEMBERS ==="]
        for u in users:
            name   = f"{u.get('firstName', '')} {u.get('lastName', '')}".strip()
            role   = u.get("jobTitle") or u.get("role") or u.get("position") or ""
            status = u.get("status") or ""
            lines.append(f"  {name}" + (f" — {role}" if role else "") + (f" [{status}]" if status and status != "active" else ""))
        if len(lines) == 1:
            lines.append("  No users found.")
        parts.append("\n".join(lines))

    return "\n".join(parts)


# ── Worker profiles (persistent memory across sessions) ───────────────────────

PROFILES_FILE = "worker_profiles.json"
_profiles_cache: dict = {}
_profiles_loaded = False


def _load_profiles() -> dict:
    global _profiles_cache, _profiles_loaded
    if _profiles_loaded:
        return _profiles_cache
    try:
        r = requests.get(
            f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{PROFILES_FILE}",
            timeout=10,
        )
        _profiles_cache = r.json() if r.ok else {}
    except Exception:
        _profiles_cache = {}
    _profiles_loaded = True
    return _profiles_cache


def _save_profiles(profiles: dict):
    global _profiles_cache
    _profiles_cache = profiles
    if not GITHUB_TOKEN:
        return
    try:
        import base64
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{PROFILES_FILE}",
            headers=headers, timeout=10,
        )
        sha     = r.json().get("sha", "") if r.ok else ""
        content = base64.b64encode(json.dumps(profiles, indent=2).encode()).decode()
        payload = {"message": "chore: update worker profiles [skip ci]", "content": content}
        if sha:
            payload["sha"] = sha
        requests.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{PROFILES_FILE}",
            headers=headers, json=payload, timeout=15,
        )
    except Exception as e:
        logger.warning(f"Failed to save worker profiles: {e}")


def get_worker_profile(worker_id: str, worker_name: str) -> dict:
    profiles = _load_profiles()
    return profiles.get(worker_id, {
        "worker_name": worker_name,
        "last_issue_date": None,
        "last_issue_summary": None,
        "open_issues": [],
        "reply_count": 0,
        "no_reply_count": 0,
        "credential_status": None,
        "notes": "",
    })


def update_worker_profile(worker_id: str, updates: dict):
    profiles = _load_profiles()
    existing = profiles.get(worker_id, {})
    existing.update(updates)
    profiles[worker_id] = existing
    _save_profiles(profiles)


def _build_profile_context(profile: dict) -> str:
    """Build a short profile summary to inject into Amy's prompt."""
    lines = []
    if profile.get("last_issue_date"):
        lines.append(f"Last compliance issue: {profile['last_issue_date']}")
    if profile.get("last_issue_summary"):
        lines.append(f"Issue summary: {profile['last_issue_summary']}")
    if profile.get("open_issues"):
        lines.append(f"Still unresolved: {', '.join(profile['open_issues'][:3])}")
    if profile.get("credential_status"):
        lines.append(f"Credential status: {profile['credential_status']}")
    if profile.get("no_reply_count", 0) > 1:
        lines.append(f"Note: has ignored {profile['no_reply_count']} previous compliance messages without replying.")
    return "\n".join(lines) if lines else ""


# ── Reply generation ───────────────────────────────────────────────────────────

def generate_reply(worker_first: str, history: str, profile_context: str = "") -> str:
    if not ANTHROPIC_API_KEY:
        return "Got it, thanks for letting me know."
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        profile_block = f"\n\nWorker history:\n{profile_context}" if profile_context else ""
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            messages=[{"role": "user", "content": f"""You are Amy, a coordinator at Connect Care (an NDIS disability support provider).
You previously sent a compliance message to {worker_first} about issues from their shift.{profile_block}

Conversation so far:
{history}

Reply as Amy. Rules:
- Casual and friendly — like texting a colleague
- If the conversation history includes a [Manager] note, follow their instruction when replying to the worker
- If they're explaining something, acknowledge it and say what needs to change going forward
- If they say they've fixed it or will fix it, say great and confirm what you need to see (e.g. "just make sure it's in the system")
- If they ask a question, answer it helpfully and briefly
- If they mention anything serious, name the right person — don't say "the manager":
  * Pay, hours, wages, timesheet, pay rate → "I'll check with Yusuf"
  * Client behaviour, client wellbeing, client concerns, incidents at the house → "I'll check with Nada"
  * Health, injury, medical, medication, nurse, wound, pain, sick → "I'll check with Fatima"
  * If it spans multiple areas, pick the most relevant one
- 1-3 sentences max — keep it short
- No sign-off, no corporate language
- Output just the message, nothing else"""}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude reply failed: {e}")
        return "Got it, thanks for letting me know."


def generate_manager_reply(manager_first: str, message_text: str, context: str = "") -> str:
    if not ANTHROPIC_API_KEY:
        return "On it."
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        context_block = f"\n\nLive Connecteam data:\n{context}" if context else ""
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=400,
            messages=[{"role": "user", "content": f"""You are Amy, a compliance coordinator at Connect Care (an NDIS disability support provider).
{manager_first} (a manager) just messaged you in the CC Management chat:

"{message_text}"{context_block}

Reply as Amy. Rules:
- You have access to live Connecteam data above — use it to give specific, accurate answers with real names and times
- If clock-in data is provided, say exactly who was late/on time/missing and by how many minutes
- If shift data is provided, list who is working and when
- If no relevant data was found for the question, say so honestly and suggest where to check in Connecteam
- Never promise to "check and get back" — answer now from the data you have, or say you don't have access to that info
- Casual but direct — like a helpful colleague
- 1-5 sentences, be specific not vague
- No sign-off
- Output just the message, nothing else"""}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude manager reply failed: {e}")
        return "On it."


# ── Connecteam send / lookup ───────────────────────────────────────────────────

def get_worker_name(user_id: str) -> str:
    try:
        r = requests.get(f"{BASE_URL}/users/v1/users/{user_id}", headers=_h(), timeout=10)
        if r.ok:
            u = (r.json().get("data") or {}).get("user") or r.json().get("data") or {}
            name = f"{u.get('firstName', '')} {u.get('lastName', '')}".strip()
            return name or f"User {user_id}"
    except Exception:
        pass
    return f"User {user_id}"


def send_message(conv_id: str, text: str) -> bool:
    if not CONNECTEAM_API_KEY or not CONNECTEAM_SENDER_ID:
        logger.warning("Connecteam credentials missing")
        return False
    try:
        r = requests.post(
            f"{BASE_URL}/chat/v1/conversations/{conv_id}/message",
            headers={**_h(), "Content-Type": "application/json"},
            json={"senderId": int(CONNECTEAM_SENDER_ID), "text": text[:4000]},
            timeout=15,
        )
        return r.ok
    except Exception as e:
        logger.error(f"Send message failed: {e}")
        return False


# ── Debug endpoint (disabled in production) ────────────────────────────────────

if os.environ.get("DEBUG", "").lower() == "true":
    @app.post("/webhook/debug")
    async def debug_webhook(request: Request):
        try:
            body = await request.body()
            try:
                payload = json.loads(body)
            except Exception:
                payload = {"raw": body.decode(errors="replace")}
            logger.info(f"DEBUG PAYLOAD: {json.dumps(payload)}")
            logger.info(f"DEBUG HEADERS: {json.dumps(dict(request.headers))}")
            return JSONResponse({"received": payload, "headers": dict(request.headers)})
        except Exception as e:
            return JSONResponse({"error": str(e)})


# ── Webhook endpoint ───────────────────────────────────────────────────────────

@app.post("/webhook/connecteam")
async def handle_webhook(request: Request):
    global conversation_log
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = payload.get("eventType", "")
    logger.info(f"Webhook received: {event_type} | full payload: {json.dumps(payload)}")

    # Accept all known Connecteam chat event type formats
    _et = event_type.lower().replace(" ", "_")
    if _et not in ("message_created", "chat_message_created", "chatmessagecreated"):
        return JSONResponse({"status": "ignored"})

    data         = payload.get("data", {})
    # Connecteam wraps the message object under data.message for chat events
    msg          = (data.get("message") if isinstance(data.get("message"), dict) else None) or data
    sender_id    = str(msg.get("senderId") or msg.get("userId") or msg.get("senderUserId") or "")
    conv_id      = str(msg.get("conversationId") or msg.get("conversation_id") or data.get("conversationId") or "")
    message_text = str(msg.get("content") or msg.get("text") or "").strip()
    is_system    = bool(msg.get("isSystem") or msg.get("is_system") or data.get("isSystem"))
    logger.info(f"Parsed: sender={sender_id} conv={conv_id} isSystem={is_system} text={message_text[:60]}")

    # Ignore automated Connecteam system notifications (shift approvals, clock-in alerts, etc.)
    if is_system:
        logger.info("Ignoring system message")
        return JSONResponse({"status": "system_message_ignored"})

    # Also ignore known Connecteam automated message patterns
    _SYSTEM_PATTERNS = (
        "shift sent for approval", "shift approved", "shift rejected", "clock-in reminder",
        "timesheet edit request", "timesheet has been edited", "timesheet edit",
        "edit request", "requested an edit",
    )
    if any(message_text.lower().startswith(p) for p in _SYSTEM_PATTERNS):
        logger.info(f"Ignoring automated pattern message: {message_text[:60]}")
        return JSONResponse({"status": "automated_message_ignored"})

    if sender_id == str(CONNECTEAM_SENDER_ID):
        return JSONResponse({"status": "self_message"})

    logger.info(f"Message in conv {conv_id} | CC_MGMT_CONV_ID={CC_MGMT_CONV_ID} | sender={sender_id}")

    if sender_id in STAFF_IDS:
        manager_name  = get_worker_name(sender_id)
        manager_first = manager_name.split()[0]
        logger.info(f"Manager {manager_name}: {message_text[:80]}")
        is_cc_mgmt = (conv_id == CC_MGMT_CONV_ID)
        try:
            context = build_context(message_text) if is_cc_mgmt else ""
        except Exception as e:
            logger.error(f"build_context failed: {e}")
            context = ""
        if context:
            logger.info(f"Context built ({len(context)} chars)")
        try:
            reply = generate_manager_reply(manager_first, message_text, context)
        except Exception as e:
            logger.error(f"generate_manager_reply failed: {e}")
            reply = "Something went wrong on my end — try again in a sec."
        send_message(conv_id, reply)
        return JSONResponse({"status": "manager_replied"})

    if sender_id not in conversation_log:
        conversation_log = load_from_github()
        logger.info(f"Reloaded conversation log: {len(conversation_log)} workers")

    if sender_id not in conversation_log:
        worker_name = get_worker_name(sender_id)
        logger.info(f"No context for {worker_name} ({sender_id}) — sending welcome")
        # Send a friendly default reply instead of spamming CC Management
        send_message(
            conv_id,
            f"Hi {worker_name.split()[0]}! 👋 I'm Amy, the Connect Care compliance assistant. "
            "I can help with shift questions, clock-in/out issues, and leave requests. "
            "What do you need?"
        )
        return JSONResponse({"status": "welcomed_new_worker"})

    if not message_text:
        return JSONResponse({"status": "empty_message"})

    convo        = conversation_log[sender_id]
    worker_name  = convo.get("worker_name", "worker")
    worker_first = worker_name.split()[0]

    convo["messages"].append({"sender": "worker", "text": message_text, "ts": int(time.time())})

    history = "\n".join(
        f"[Manager - {m.get('name', 'Manager')}]: {m['text']}" if m["sender"] == "manager"
        else f"{'Amy' if m['sender'] == 'amy' else worker_name}: {m['text']}"
        for m in convo["messages"][-10:]
    )

    profile         = get_worker_profile(sender_id, worker_name)
    profile_context = _build_profile_context(profile)
    update_worker_profile(sender_id, {
        "worker_name": worker_name,
        "reply_count": profile.get("reply_count", 0) + 1,
    })

    reply = generate_reply(worker_first, history, profile_context)

    target_conv_id = convo.get("conversation_id") or conv_id
    if not target_conv_id:
        logger.error(f"No conversation_id for user {sender_id}")
        return JSONResponse({"status": "no_conv_id"})

    ok = send_message(target_conv_id, reply)
    if ok:
        convo["messages"].append({"sender": "amy", "text": reply, "ts": int(time.time())})
        save_to_github(conversation_log)
        logger.info(f"✓ Replied to {worker_name}: {reply[:80]}")
        return JSONResponse({"status": "replied"})
    else:
        logger.error(f"Failed to send reply to {worker_name}")
        return JSONResponse({"status": "send_failed"})


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "workers_tracked": len(conversation_log),
        "time_clock_id": _time_clock_id,
        "scheduler_id": _scheduler_id,
        "users_cached": len(_users_cache),
    }
