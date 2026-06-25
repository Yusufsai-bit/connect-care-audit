#!/usr/bin/env python3
"""
Connect Care Services -- NDIS Compliance Auditor
Audits Connecteam shift data against NDIS Practice Standards.

Usage:
    python connecteam_audit.py           # audits past 7 days
    python connecteam_audit.py 14        # audits past 14 days

Environment variables required:
    CONNECTEAM_API_KEY   -- Connecteam REST API key
    ANTHROPIC_API_KEY    -- Anthropic API key (for note quality assessment)
"""

import os, sys, json, re, math, time, random, concurrent.futures, functools, hashlib
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
from difflib import SequenceMatcher

WORKER_CONVERSATIONS_FILE = os.path.join(os.path.dirname(__file__), "worker_conversations.json")

# ---------------------------------------------
# CONFIG
# ---------------------------------------------

CONNECTEAM_API_KEY = os.environ.get("CONNECTEAM_API_KEY", "")
if not CONNECTEAM_API_KEY:
    sys.exit("ERROR: CONNECTEAM_API_KEY environment variable is not set.")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER    = os.environ.get("TWILIO_NUMBER", "")
TWILIO_WA_NUMBER      = os.environ.get("TWILIO_WHATSAPP_NUMBER", TWILIO_FROM_NUMBER)
CONNECTEAM_SENDER_ID      = int(os.environ.get("CONNECTEAM_SENDER_ID", "0") or "0")
COMPLIANCE_INDICATOR_ID   = int(os.environ.get("COMPLIANCE_INDICATOR_ID", "0") or "0")
CC_MGMT_CONV_ID           = os.environ.get("CC_MGMT_CONV_ID", "")

BASE_URL       = "https://api.connecteam.com"
TIME_CLOCK_ID  = 1776332
SCHEDULER_ID   = 1775479
AEST           = ZoneInfo("Australia/Melbourne")

# Audit thresholds
LATE_MIN              = 30     # minutes grace before flagging late
EARLY_MIN             = 30     # minutes grace before flagging early departure
MIN_NOTE_WORDS        = 50     # minimum words for a valid note
GPS_THRESHOLD_KM      = 0.5    # km radius from client address (overridden by geofence if configured)
COPY_PASTE_THRESHOLD  = 0.82   # similarity ratio to flag as copy-paste
NOTE_LATE_HOURS       = 24     # hours after clock-out before note is flagged as backdated
SHORT_SHIFT_MIN       = 15     # shifts under this many minutes are suspicious
MULTI_CLOCKIN_DAILY   = 3      # more than this many clock-ins per client per day is suspicious
BREAK_REQUIRED_AFTER_HOURS = 5.0  # Fair Work: break required after this many hours
BREAK_MINIMUM_MINS         = 30   # minimum break duration required (minutes)

# Shift attachment field IDs
NOTES_FIELD = "65cbb88e-6c3a-41b1-8822-975caed50def"
SIG_FIELD   = "261a70f7-a760-20f1-36df-78df4f6056a6"

# Support ratio rules
# Kallan Jordan (matched by job title) -- day shifts = 2:1, overnight = 1:1
# All other clients = 1:1
KALLAN_TITLE_MATCH = "kallan jordan"

# Kallan overnight detection: shift starts at or after 20:00 OR spans midnight
OVERNIGHT_START_HOUR = 20

# Form IDs
FORMS = {
    "Kallan Incident Report":    2825220,
    "Safety Hazard Report":      2825225,
    "Kallan Medication Form":    3012456,
    "Kallan Behaviour Tracking": 3018810,
    "Michael Medications Plan":  4865646,
    "Medication Incident Form":  7294261,
    "Incident Report":           9786979,
    "Kallan: ABC Form":          11694853,
    "Kallan Sleep Observation":  14773151,
    "Kallan Daily Cleaning":     15252440,
    "Joshua: ABC Form":          15535225,
}

# Peter Eronmwon's user ID (required for Michael medication check)
PETER_USER_ID = 2200746

# Management observers — receive a CC copy of every compliance message sent to workers
# and a forward of every worker reply. Yusuf, Nada, Faduma.
COMPLIANCE_OBSERVER_IDS = [2149475, 9736871, 2201497]

# GPS overrides — fallback coordinates when the Connecteam job record has no GPS configured.
# Format: lowercase keyword in job title → (latitude, longitude, radius_km)
# Coordinates verified via OpenStreetMap Nominatim geocoder.
CLIENT_GPS_OVERRIDES = {
    "john": (-37.67282, 144.99437, 0.2),   # 14 Linton Dr, Thomastown VIC 3074
}

# Job title keywords for client matching
CLIENT_TITLES = {
    "kallan":  "Kallan Jordan",
    "evan":    "Evan Gatt",
    "joshua":  "Joshua Gatt",
    "josh":    "Joshua Gatt",
    "nada":    "Nada Haliem",
    "john":    "John",        # covers John A and John Auzagelis
    "nicole":  "Nicole Loveless",
    "michael": "Michael Lawrie",
}

# Safeguarding keyword groups
INCIDENT_KEYWORDS    = ["fall", "fell", "fallen", "injur", "hurt", "bruise", "bleed",
                         "ambulance", "hospital", "police", "scratch", "bitten", r"\bhit\b",
                         "struck", "assault", "aggress", "emergency", "unconscious", "seizure"]
RESTRICTIVE_KEYWORDS = ["restrain", "physical intervention", "seclu", "locked in",
                         "blocked exit", "held down", "physically held"]
MEDICATION_KEYWORDS  = ["medication", "medicine", "tablet", "pill", " dose ", " mg",
                         "administer", "refused medic", "missed dose", "medic error"]

# NDIS Reportable Incident Types (Incident Management and Reportable Incidents Rules 2018)
# Type 1 — notify NDIS Commission within 24 hours
TYPE1_INCIDENT_KEYWORDS = [
    "death", "died", "passed away", "deceased",
    "serious injur", "hospitalised", "hospitalized", "surgery", "icu", "intensive care",
    "abuse", "neglect", "exploit", "financial abuse",
    "assault", "attacked", "beaten", "physical attack",
    "sexual", "inappropriate touching", "inappropriate contact",
    "unexplained absence", "missing person", "cannot locate", "could not locate",
    "restrictive practice", "restrain", "seclusion", "physical intervention",
    "unlawful sexual", "sexual misconduct",
]
# Type 2 — notify NDIS Commission within 5 business days
TYPE2_INCIDENT_KEYWORDS = [
    "near miss", "close call",
    "medication error", "wrong dose", "wrong medication",
    "property damage", "stolen", "theft",
    "verbal aggress", "verbal altercation", "verbal threat",
    "fall", "fell", "slip", "trip", "bump",
    "unauthorised absence",
]


# ---------------------------------------------
# ISSUE MODEL
# ---------------------------------------------

SEV_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}

class Issue:
    def __init__(self, severity, category, worker, client, date, detail):
        self.severity = severity
        self.category = category
        self.worker   = worker
        self.client   = client
        self.date     = date
        self.detail   = detail


# ---------------------------------------------
# CONNECTEAM API HELPERS
# ---------------------------------------------

def ct_get(path, params=None):
    for attempt in range(4):
        try:
            r = requests.get(
                f"{BASE_URL}{path}",
                headers={"X-API-KEY": CONNECTEAM_API_KEY, "Accept": "application/json"},
                params=params,
                timeout=30,
            )
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.SSLError:
            if attempt < 3:
                time.sleep(1 + attempt)
                continue
            raise
    r.raise_for_status()
    return r.json()


def fetch_all_users():
    users, offset = {}, 0
    while True:
        data  = ct_get("/users/v1/users", {"offset": offset})
        batch = data["data"]["users"]
        for u in batch:
            users[u["userId"]] = u
        total = data.get("paging", {}).get("total", 0)
        offset += len(batch)
        if offset >= total or not batch:
            break
    return users


def fetch_all_jobs():
    jobs, offset = {}, 0
    while True:
        data  = ct_get("/jobs/v1/jobs", {"offset": offset, "limit": 50})
        batch = data["data"]["jobs"]
        for j in batch:
            jobs[j["jobId"]] = j
        total = data.get("paging", {}).get("total", 0)
        offset += len(batch)
        if offset >= total or not batch:
            break
    return jobs


def fetch_scheduled_shifts(start_ts, end_ts):
    data = ct_get(f"/scheduler/v1/schedulers/{SCHEDULER_ID}/shifts",
                  {"startTime": start_ts, "endTime": end_ts})
    return data["data"]["shifts"]


def fetch_time_activities(start_date, end_date):
    data = ct_get(f"/time-clock/v1/time-clocks/{TIME_CLOCK_ID}/time-activities",
                  {"startDate": start_date, "endDate": end_date})
    by_user = {}
    for entry in data["data"]["timeActivitiesByUsers"]:
        by_user[entry["userId"]] = entry["shifts"]
    return by_user


def fetch_pending_shift_ids(start_date, end_date):
    """Return a set of shift IDs that have pending amendment requests.
    These shifts must not be flagged for missing/empty notes — the worker
    may have submitted notes in the amendment that the API can't yet return.
    """
    try:
        data = ct_get(
            f"/time-clock/v1/time-clocks/{TIME_CLOCK_ID}/time-activities",
            {"startDate": start_date, "endDate": end_date, "approvalStatus": "pending"},
        )
        pending_ids = set()
        for entry in data["data"]["timeActivitiesByUsers"]:
            for shift in entry["shifts"]:
                sid = shift.get("id")
                if sid:
                    pending_ids.add(str(sid))
        print(f"[pending amendments] {len(pending_ids)} shift(s) with pending edits — notes check skipped for these.")
        return pending_ids
    except Exception as e:
        print(f"[pending amendments] fetch failed ({e}) — no shifts will be skipped.")
        return set()


_FORM_SUBS_CACHE: dict = {}  # pre-populated at start of run_audit

def fetch_form_submissions(form_id):
    if form_id in _FORM_SUBS_CACHE:
        return _FORM_SUBS_CACHE[form_id]
    try:
        data = ct_get(f"/forms/v1/forms/{form_id}/form-submissions", {"limit": 100})
        result = data["data"]["formSubmissions"]
        _FORM_SUBS_CACHE[form_id] = result
        return result
    except Exception:
        return []


def fetch_geofences():
    """Fetch geofences configured for the time clock."""
    try:
        data = ct_get(f"/time-clock/v1/time-clocks/{TIME_CLOCK_ID}/geofences")
        raw = data.get("data") or {}
        fences = raw.get("geofences", raw.get("items", []))
        if isinstance(fences, dict):
            fences = list(fences.values())
        return fences if isinstance(fences, list) else []
    except Exception:
        return []


def fetch_manual_breaks(start_date, end_date):
    """Fetch manual break records. Returns dict: str(timeActivityId) -> list of break records."""
    try:
        data = ct_get(f"/time-clock/v1/time-clocks/{TIME_CLOCK_ID}/manual-breaks",
                      {"startDate": start_date, "endDate": end_date})
        raw = data.get("data") or {}
        result = {}
        # Try common response shapes
        for key in ("manualBreaks", "breaks", "items"):
            entries = raw.get(key)
            if entries and isinstance(entries, list):
                for b in entries:
                    act_id = str(b.get("timeActivityId") or b.get("activityId") or "")
                    if act_id:
                        result.setdefault(act_id, []).append(b)
                break
        return result
    except Exception:
        return {}


def fetch_user_unavailabilities(start_ts, end_ts):
    """Return dict: userId -> list of (start_ts, end_ts) for approved unavailability/leave."""
    try:
        data = ct_get("/scheduler/v1/schedulers/user-unavailability",
                      {"startTime": start_ts, "endTime": end_ts})
        unavail = defaultdict(list)
        raw = data.get("data") or {}
        for key in ("userUnavailabilities", "unavailabilities", "items"):
            entries = raw.get(key)
            if entries and isinstance(entries, list):
                for u in entries:
                    uid = u.get("userId")
                    if uid:
                        unavail[uid].append((
                            u.get("startTime", u.get("start", 0)),
                            u.get("endTime",   u.get("end",   0)),
                        ))
                break
        return dict(unavail)
    except Exception:
        return {}


def fetch_onboarding_completion(active_user_ids):
    """
    Returns dict: userId -> list of pack names not yet completed.
    Only includes workers with at least one incomplete pack.
    """
    result = defaultdict(list)
    try:
        packs_data = ct_get("/onboarding/v1/packs")
        raw_packs  = packs_data.get("data") or {}
        packs      = raw_packs.get("packs", raw_packs.get("items", []))
        if isinstance(packs, dict):
            packs = list(packs.values())
        if not isinstance(packs, list):
            return {}
    except Exception:
        return {}

    for pack in packs:
        pack_id   = pack.get("packId") or pack.get("id")
        pack_name = pack.get("name", f"Pack {pack_id}")
        if not pack_id:
            continue
        try:
            a_data      = ct_get(f"/onboarding/v1/packs/{pack_id}/assignments")
            raw_assign  = a_data.get("data") or {}
            assignments = raw_assign.get("assignments", raw_assign.get("items", []))
            if isinstance(assignments, dict):
                assignments = list(assignments.values())
            if not isinstance(assignments, list):
                continue
        except Exception:
            continue
        for a in assignments:
            uid = a.get("userId")
            if uid not in active_user_ids:
                continue
            completed = bool(a.get("completedAt") or a.get("isCompleted") or a.get("completed"))
            if not completed:
                result[uid].append(pack_name)
    return dict(result)


@functools.lru_cache(maxsize=1)
def fetch_user_custom_fields():
    """Fetch custom field definitions. Exported for dashboard use."""
    try:
        data   = ct_get("/users/v1/custom-fields", {"limit": 100})
        raw    = data.get("data") or {}
        fields = raw.get("customFields", raw.get("items", []))
        if isinstance(fields, dict):
            fields = list(fields.values())
        return fields if isinstance(fields, list) else []
    except Exception:
        return []


def fetch_task_boards():
    """Fetch all task boards. Exported for dashboard use."""
    try:
        data   = ct_get("/tasks/v1/taskboards")
        raw    = data.get("data") or {}
        boards = raw.get("taskBoards", raw.get("boards", raw.get("items", [])))
        if isinstance(boards, dict):
            boards = list(boards.values())
        return boards if isinstance(boards, list) else []
    except Exception:
        return []


def ct_post(path, body):
    """POST helper — returns (True, response_json) or (False, error_string)."""
    for attempt in range(3):
        try:
            r = requests.post(
                f"{BASE_URL}{path}",
                headers={
                    "X-API-KEY":    CONNECTEAM_API_KEY,
                    "Accept":       "application/json",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=30,
            )
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return True, r.json()
        except requests.exceptions.HTTPError as e:
            try:
                detail = e.response.json()
            except Exception:
                detail = e.response.text
            return False, f"HTTP {e.response.status_code}: {detail}"
        except Exception as e:
            return False, str(e)
    return False, "Rate limited after 3 retries"


def ct_put(path, body):
    """PUT helper — returns (True, response_json) or (False, error_string)."""
    try:
        r = requests.put(
            f"{BASE_URL}{path}",
            headers={
                "X-API-KEY":    CONNECTEAM_API_KEY,
                "Accept":       "application/json",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=30,
        )
        r.raise_for_status()
        return True, r.json()
    except requests.exceptions.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text
        return False, f"HTTP {e.response.status_code}: {detail}"
    except Exception as e:
        return False, str(e)


def fetch_worker_credentials(users):
    """
    Reads document expiry dates from Connecteam user custom fields.
    Returns dict: user_id -> {credential_type -> date}
    """
    DOC_KEYWORDS = {
        "NDIS Worker Screening":       ["ndis", "screening", "worker screening"],
        "Working With Children Check": ["wwcc", "working with children", "children check"],
        "Police Check":                ["police"],
        "First Aid Certificate":       ["first aid"],
        "CPR Certificate":             ["cpr"],
        "Manual Handling":             ["manual handling"],
    }
    try:
        fields = fetch_user_custom_fields()
    except Exception:
        return {}

    cred_fields = {}
    for field in fields:
        fname = (field.get("name") or "").lower()
        for cred_type, keywords in DOC_KEYWORDS.items():
            if any(kw in fname for kw in keywords):
                fid = field.get("customFieldId") or field.get("id")
                if fid:
                    cred_fields[str(fid)] = cred_type
                break

    expected_types = set(DOC_KEYWORDS.keys())
    found_types    = set(cred_fields.values())
    missing_types  = expected_types - found_types
    if missing_types:
        print(f"  [WARNING] Credential custom fields not found in Connecteam for: {', '.join(sorted(missing_types))}. "
              f"Workers with these credentials will not be monitored.")
    if not cred_fields:
        return {}

    result = {}
    for uid, udata in users.items():
        custom_vals = udata.get("customFields", udata.get("customFieldValues", []))
        if not custom_vals:
            continue
        creds = {}
        for val in (custom_vals if isinstance(custom_vals, list) else []):
            fid = str(val.get("customFieldId") or val.get("fieldId") or "")
            if fid not in cred_fields:
                continue
            raw = val.get("value") or val.get("dateValue") or ""
            if not raw:
                continue
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
                try:
                    from datetime import date as _date
                    expiry = datetime.strptime(str(raw)[:10], fmt).date()
                    creds[cred_fields[fid]] = expiry
                    break
                except ValueError:
                    continue
        if creds:
            result[uid] = creds
    return result


def auto_detect_ids():
    """
    Auto-detect all Connecteam IDs that are currently hardcoded.
    Returns dict with keys: time_clock_id, scheduler_id, sender_id,
    compliance_indicator_id, form_ids (dict name->id)
    """
    detected = {}

    # Time clock
    d = ct_get("/time-clock/v1/time-clocks")
    clocks = (d.get("data") or {}).get("timeClocks") or (d.get("data") or {}).get("items") or []
    if clocks:
        detected["time_clock_id"] = clocks[0].get("id") or clocks[0].get("timeClockId")

    # Scheduler
    d = ct_get("/scheduler/v1/schedulers")
    schedulers = (d.get("data") or {}).get("schedulers") or (d.get("data") or {}).get("items") or []
    if schedulers:
        detected["scheduler_id"] = schedulers[0].get("id") or schedulers[0].get("schedulerId")

    # Publisher / sender
    d = ct_get("/publishers/v1/publishers")
    publishers = (d.get("data") or {}).get("publishers") or (d.get("data") or {}).get("items") or []
    if publishers:
        detected["sender_id"] = publishers[0].get("id") or publishers[0].get("publisherId")

    # Compliance performance indicator
    d = ct_get("/users/v1/performance-indicators")
    indicators = (d.get("data") or {}).get("indicators") or (d.get("data") or {}).get("items") or []
    for ind in indicators:
        if "compliance" in (ind.get("name") or "").lower():
            detected["compliance_indicator_id"] = ind.get("id") or ind.get("indicatorId")
            break

    # Form IDs by name
    d = ct_get("/forms/v1/forms")
    forms_list = (d.get("data") or {}).get("forms") or (d.get("data") or {}).get("items") or []
    detected["form_ids"] = {}
    for form in forms_list:
        name = form.get("name") or form.get("title") or ""
        fid  = form.get("id") or form.get("formId")
        if name and fid:
            detected["form_ids"][name] = fid

    return detected


@functools.lru_cache(maxsize=4)
def fetch_timesheet(start_date, end_date):
    """Payroll/billing totals per worker with pay rates. Max 45-day window."""
    d = ct_get(
        f"/time-clock/v1/time-clocks/{TIME_CLOCK_ID}/timesheet",
        {"startDate": start_date, "endDate": end_date},
    )
    return (d.get("data") or {}).get("timesheetEntries") or (d.get("data") or {}).get("entries") or []


def fetch_pay_rates():
    d = ct_get("/pay-rates/v1/pay-rates")
    return (d.get("data") or {}).get("payRates") or (d.get("data") or {}).get("items") or []


def lock_worker_days(user_id, dates):
    """Lock time entries for a worker on a list of YYYY-MM-DD date strings."""
    locked = [{"date": d, "isLocked": True} for d in dates]
    return ct_put(
        f"/time-clock/v1/time-clocks/{TIME_CLOCK_ID}/users/{user_id}/lock-days",
        {"lockDays": locked},
    )


def add_worker_note(user_id, text):
    """Add a permanent compliance note to the worker's Connecteam HR profile."""
    return ct_post(f"/users/v1/users/{user_id}/notes", {"note": text[:2000]})


def push_daily_info_note(text, date_str=None):
    """Push compliance summary to Connecteam Daily Info section."""
    if date_str is None:
        date_str = datetime.now(AEST).strftime("%Y-%m-%d")
    return ct_post("/daily-info/v1/daily-notes", {"date": date_str, "note": text[:5000]})


def fetch_time_off_policies():
    d = ct_get("/time-off/v1/policy-types")
    return (d.get("data") or {}).get("policyTypes") or (d.get("data") or {}).get("items") or []


def fetch_time_off_balances(policy_type_id):
    d = ct_get(f"/time-off/v1/policy-types/{policy_type_id}/balances")
    return (d.get("data") or {}).get("balances") or (d.get("data") or {}).get("items") or []


def fetch_shift_layers():
    d = ct_get(f"/scheduler/v1/schedulers/{SCHEDULER_ID}/shift-layers")
    return (d.get("data") or {}).get("layers") or (d.get("data") or {}).get("items") or []


def fetch_shift_layer_values(layer_id):
    d = ct_get(f"/scheduler/v1/schedulers/{SCHEDULER_ID}/shift-layers/{layer_id}/values")
    return (d.get("data") or {}).get("values") or (d.get("data") or {}).get("items") or []


def fetch_open_tasks(task_board_id):
    d = ct_get(f"/tasks/v1/taskboards/{task_board_id}/tasks", {"status": "open", "limit": 100})
    return (d.get("data") or {}).get("tasks") or (d.get("data") or {}).get("items") or []


def create_geofence(name, lat, lon, radius_m=200):
    """Create a geofence for a client address via the API."""
    return ct_post(
        f"/time-clock/v1/time-clocks/{TIME_CLOCK_ID}/geofences",
        {"name": name, "latitude": lat, "longitude": lon, "radius": int(radius_m)},
    )


def write_compliance_score(user_id, score, date_str=None):
    """
    Write a compliance score (0-100) to the worker's Connecteam performance profile.
    Requires COMPLIANCE_INDICATOR_ID — create a 'Compliance Score' performance
    indicator in Connecteam (Settings → Performance) then set the env var.
    Returns (True, response) or (False, error_string).
    """
    if not COMPLIANCE_INDICATOR_ID:
        return False, "COMPLIANCE_INDICATOR_ID not set — create indicator in Connecteam Settings → Performance."
    if date_str is None:
        date_str = datetime.now(AEST).strftime("%Y-%m-%d")
    return ct_put(
        f"/users/v1/users/{user_id}/performance/{date_str}",
        {"indicators": [{"indicatorId": COMPLIANCE_INDICATOR_ID, "value": round(float(score), 1)}]},
    )


def register_webhooks(webhook_url, secret=""):
    """
    Programmatically register all compliance webhooks in Connecteam.
    Returns list of (ok, name, detail) tuples.

    Note: scheduler and chat webhooks cannot be registered via the v1 API
    (different versioning) — register those manually in Connecteam Settings.
    """
    targets = [
        # Time clock events — featureType + eventTypes both required
        {"name": "Compliance — Clock In",        "featureType": "time_activity", "eventTypes": ["clock_in"]},
        {"name": "Compliance — Clock Out",       "featureType": "time_activity", "eventTypes": ["clock_out"]},
        {"name": "Compliance — Auto Clock Out",  "featureType": "time_activity", "eventTypes": ["auto_clock_out"]},
        {"name": "Compliance — Admin Time Edit", "featureType": "time_activity", "eventTypes": ["admin_edit"]},
        {"name": "Compliance — Admin Time Add",  "featureType": "time_activity", "eventTypes": ["admin_add"]},
        # Forms
        {"name": "Compliance — Form Submitted",  "featureType": "forms",         "eventTypes": ["form_submission"]},
        # User HR events
        {"name": "Compliance — User Created",    "featureType": "users",         "eventTypes": ["user_created"]},
        {"name": "Compliance — User Updated",    "featureType": "users",         "eventTypes": ["user_updated"]},
        {"name": "Compliance — User Archived",   "featureType": "users",         "eventTypes": ["user_archived"]},
    ]
    results = []
    for t in targets:
        body = {
            "name":        t["name"],
            "url":         webhook_url,
            "isDisabled":  False,
            "featureType": t["featureType"],
            "eventTypes":  t["eventTypes"],
        }
        if secret:
            body["secretKey"] = secret
        ok, detail = ct_post("/settings/v1/webhooks", body)
        results.append((ok, t["name"], detail))
    return results


def fetch_conversations():
    """Return all chat conversations this account can see."""
    conversations = []
    page = 1
    while True:
        data = ct_get("/chat/v1/conversations", {"page": page, "limit": 100})
        items = (data.get("data") or {}).get("conversations") or data.get("conversations") or []
        if not items:
            break
        conversations.extend(items)
        total = (data.get("data") or {}).get("total") or data.get("total") or 0
        if len(conversations) >= total or not items:
            break
        page += 1
    return conversations


def load_worker_conversations():
    """Load worker_id → conversation_id mapping from disk. Returns {} if file missing."""
    try:
        with open(WORKER_CONVERSATIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_worker_conversations(mapping):
    """Persist worker_id → conversation_id mapping to disk."""
    with open(WORKER_CONVERSATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2)


def detect_worker_conversations():
    """
    Scan all Connecteam group (team) conversations and match their titles to
    worker names. The API doesn't return member lists, so we match by title.
    Handles both full names ("Abdul Latif") and first-name-only titles ("Peter").
    Saves and returns {str(worker_user_id): conversation_id}.
    """
    convs   = fetch_conversations()
    users   = fetch_all_users()
    obs_set = {str(oid) for oid in COMPLIANCE_OBSERVER_IDS}

    # Build lookup structures, excluding management observers
    workers_data = {}  # uid -> {full, first}
    for uid, u in users.items():
        if str(uid) in obs_set:
            continue
        first = (u.get("firstName") or "").strip().lower()
        last  = (u.get("lastName") or "").strip().lower()
        full  = f"{first} {last}".strip()
        if full:
            workers_data[str(uid)] = {"full": full, "first": first}

    mapping = {}
    already_claimed = set()  # prevent two convs claiming same worker

    for conv in convs:
        c       = conv.get("data") or conv
        conv_id = c.get("conversationId") or c.get("id")
        title   = (c.get("title") or c.get("name") or "").strip().lower()
        ctype   = c.get("type", "")
        if not conv_id or not title or ctype not in ("team", "group"):
            continue

        best_score, best_uid = 0.0, None

        for uid, wd in workers_data.items():
            if uid in already_claimed:
                continue
            full  = wd["full"]
            first = wd["first"]

            # 1. Exact full-name match
            if title == full:
                best_score, best_uid = 1.0, uid
                break

            # 2. Exact first-name match
            if title == first:
                score = 0.95
                if score > best_score:
                    best_score, best_uid = score, uid
                continue

            # 3. Title is a prefix of the worker's first name (e.g. "Irfan" -> "Irfanullah")
            if first.startswith(title) and len(title) >= 4:
                score = 0.90
                if score > best_score:
                    best_score, best_uid = score, uid
                continue

            # 4. Fuzzy full-name match
            score = SequenceMatcher(None, title, full).ratio()
            if score > best_score:
                best_score, best_uid = score, uid

        if best_score >= 0.72 and best_uid:
            mapping[best_uid] = conv_id
            already_claimed.add(best_uid)

    save_worker_conversations(mapping)
    return mapping


def send_worker_message(user_id, text, worker_name=None):
    """
    Send a Connecteam chat message to the worker's group conversation.
    Never falls back to private — if the group conv is missing or fails,
    returns (False, reason) so the issue is visible rather than silent.
    Returns (True, message_id) on success or (False, error_string) on failure.
    """
    if not CONNECTEAM_SENDER_ID:
        return False, "CONNECTEAM_SENDER_ID not set."

    conv_map = load_worker_conversations()
    conv_id  = conv_map.get(str(user_id))

    label = worker_name or f"User {user_id}"

    def _fail(reason):
        print(f"[ERROR] send_worker_message failed for {label}: {reason}")
        fail_alert = f"Amy couldn't send a message to {label} ({reason}). Message was:\n\n{text[:500]}"
        if CC_MGMT_CONV_ID:
            ct_post(f"/chat/v1/conversations/{CC_MGMT_CONV_ID}/message",
                    {"senderId": CONNECTEAM_SENDER_ID, "text": fail_alert[:1000]})
        return False, reason

    if not conv_id:
        return _fail("no group conversation mapped — run detect_worker_conversations() to fix")

    # Split into 950-char chunks to stay under Connecteam's 1000-char limit
    MAX_CHUNK = 950
    chunks = [text[i:i + MAX_CHUNK] for i in range(0, len(text), MAX_CHUNK)] if len(text) > MAX_CHUNK else [text]

    last_msg_id = None
    for chunk in chunks:
        ok, result = ct_post(
            f"/chat/v1/conversations/{conv_id}/message",
            {"senderId": CONNECTEAM_SENDER_ID, "text": chunk},
        )
        if not ok:
            return _fail(f"group conv {conv_id} returned error: {result}")
        last_msg_id = (result.get("data") or {}).get("messageId") or result.get("messageId")

    return True, last_msg_id


def add_worker_profile_note(user_id, text, title="Compliance Notification"):
    """
    Add a note to the worker's Connecteam HR profile.
    Returns (True, note_id) on success or (False, error_string) on failure.
    """
    ok, result = ct_post(
        f"/users/v1/users/{user_id}/notes",
        {"text": text[:1000], "title": title},
    )
    if ok:
        note_id = (result.get("data") or {}).get("id")
        return True, note_id
    return False, result


def send_sms(to_number, text):
    """Send SMS via Twilio. Returns (True, message_sid) or (False, error_string)."""
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER]):
        return False, "Twilio credentials not configured — add TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_NUMBER to secrets."
    try:
        from twilio.rest import Client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(
            from_=TWILIO_FROM_NUMBER,
            body=text[:1600],
            to=to_number,
        )
        return True, msg.sid
    except Exception as e:
        return False, str(e)


def send_whatsapp(to_number, text, sandbox=False):
    """
    Send a WhatsApp message via Twilio.
    sandbox=True uses the Twilio test sandbox number.
    Returns (True, message_sid) or (False, error_string).
    """
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN]):
        return False, "Twilio credentials not configured."
    try:
        from twilio.rest import Client
        client   = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        from_num = "whatsapp:+14155238886" if sandbox else f"whatsapp:{TWILIO_WA_NUMBER}"
        msg = client.messages.create(
            from_=from_num,
            body=text[:1600],
            to=f"whatsapp:{to_number}",
        )
        return True, msg.sid
    except Exception as e:
        return False, str(e)


def create_worker_task(task_board_id, user_id, title, description, due_ts=None):
    """
    Create a Connecteam task assigned to the worker as an acknowledgement request.
    Returns (True, task_id) on success or (False, error_string) on failure.
    """
    body = {
        "userIds": [int(user_id)],
        "title":   title[:255],
        "status":  "open",
    }
    if due_ts:
        body["dueDate"] = int(due_ts)
    ok, result = ct_post(f"/tasks/v1/taskboards/{task_board_id}/tasks", body)
    if ok:
        task_id = (result.get("data") or {}).get("taskId") or (result.get("data") or {}).get("id")
        return True, task_id
    return False, result


# ---------------------------------------------
# HELPERS
# ---------------------------------------------

def ts_aest(ts):
    return datetime.fromtimestamp(ts, tz=AEST)


def date_label(ts):
    return ts_aest(ts).strftime("%a %d-%b")


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_note_text(attachments):
    """Extract free-text note from shiftAttachments list."""
    for att in (attachments or []):
        if att.get("shiftAttachmentId") != NOTES_FIELD:
            continue
        val = att.get("attachment")
        if not val:
            return ""
        if isinstance(val, dict):
            return val.get("freeText", "")
        # PowerShell serialised format: @{freeText=some text}
        m = re.match(r"@\{freeText=(.*)\}", str(val), re.DOTALL)
        if m:
            return m.group(1)
        return str(val)
    return ""


def has_signature(attachments):
    for att in (attachments or []):
        if att.get("shiftAttachmentId") == SIG_FIELD:
            return bool(att.get("attachment"))
    return False


def word_count(text):
    return len(text.split()) if text else 0


def similarity(a, b):
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def contains_keywords(text, keywords):
    t = text.lower()
    return [kw.strip() for kw in keywords if kw in t]


def is_overnight(start_ts, end_ts):
    """True if shift starts at/after OVERNIGHT_START_HOUR or spans midnight."""
    start_dt = ts_aest(start_ts)
    end_dt   = ts_aest(end_ts)
    spans_midnight = end_dt.date() > start_dt.date()
    late_start     = start_dt.hour >= OVERNIGHT_START_HOUR
    return spans_midnight or late_start


# ---------------------------------------------
# CLAUDE NOTE QUALITY ASSESSMENT
# ---------------------------------------------

_NOTE_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "note_assessment_cache.json")
_note_cache: dict = {}

def _load_note_cache():
    global _note_cache
    try:
        with open(_NOTE_CACHE_FILE, "r", encoding="utf-8") as f:
            _note_cache = json.load(f)
    except Exception:
        _note_cache = {}

def _save_note_cache():
    try:
        with open(_NOTE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_note_cache, f)
    except Exception:
        pass

def _note_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()


def assess_notes_with_claude(notes_batch):
    """
    Evaluate a batch of shift notes against NDIS standards.
    Returns dict keyed by note id.
    """
    if not ANTHROPIC_API_KEY or not notes_batch:
        return {}

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    except ImportError:
        print("  [WARNING] anthropic package not found -- skipping AI note assessment.")
        return {}

    # Pseudonymise participant names before sending to external AI API
    def _pseudo(name: str) -> str:
        return f"Client {name[0].upper()}" if name else "Client"

    payload = json.dumps([
        {
            "id":             n["id"],
            "worker":         n["worker"].split()[0] if n.get("worker") else "Worker",
            "client":         _pseudo(n["client"]),
            "date":           n["date"],
            "duration_hours": n["duration_hours"],
            "text":           n["text"][:2000],
        }
        for n in notes_batch
    ], indent=2)

    prompt = f"""You are a senior NDIS compliance auditor reviewing support worker shift/progress notes.
Evaluate each note strictly against the NDIS Practice Standards for progress documentation.

NDIS standards require notes to:
- Be factual and objective (observations, not opinions)
- Describe supports actually provided
- Mention participant's mood/wellbeing/condition
- Note any incidents, risks, or concerns
- Use person-centred, respectful language (use participant's name)
- Be written in plain, clear English
- Make logical sense from start to finish

Notes to evaluate:
{payload}

For EACH note return a JSON object with:
- id: (same as input)
- passes_ndis_standard: true/false -- meets minimum NDIS documentation requirements
- is_plain_english: true/false -- clear and understandable to any reader
- sounds_ai_generated: true/false -- looks templated, suspiciously formal, or AI-written (watch for: "I commenced", "upon arrival", "it is important to note", copy-paste sentence starters, unnaturally consistent structure across the note)
- makes_sense: true/false -- coherent and internally consistent
- is_person_centred: true/false -- uses name, reflects dignity and voice
- uses_subjective_language: true/false -- opinions instead of observations
- mentions_mood_or_condition: true/false
- mentions_supports_provided: true/false
- references_plan_goals: true/false -- note links support provided to participant's NDIS plan goals or outcomes (e.g. "to support X with independence", "as per support plan", references to specific goals)
- issues: [list of specific problems -- empty if none]
- severity: "PASS", "LOW", "MEDIUM", or "HIGH"

Return ONLY a valid JSON array. No explanation, no markdown."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
            raw = raw.rsplit("```", 1)[0].strip()
        results = json.loads(raw)
        return {r["id"]: r for r in results}
    except Exception as e:
        print(f"  [WARNING] Claude assessment failed: {e}")
        # Log failed note IDs so they can be manually reviewed
        failed_ids = [n["id"] for n in notes_batch]
        _failure_log = os.path.join(os.path.dirname(__file__), "note_assessment_failures.json")
        try:
            existing = json.load(open(_failure_log, encoding="utf-8")) if os.path.exists(_failure_log) else []
            existing.append({"timestamp": str(datetime.now(AEST)), "error": str(e), "note_ids": failed_ids})
            json.dump(existing[-200:], open(_failure_log, "w", encoding="utf-8"), indent=2)
        except Exception:
            pass
        return {}


def assess_incident_reports_with_claude(reports_batch):
    """
    Evaluate a batch of incident report descriptions against NDIS standards.
    Returns dict keyed by report id.
    """
    if not ANTHROPIC_API_KEY or not reports_batch:
        return {}

    try:
        import anthropic
        ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    except ImportError:
        print("  [WARNING] anthropic package not found -- skipping AI incident report assessment.")
        return {}

    def _pseudo(name):
        return f"Client {name[0].upper()}" if name else "Client"

    payload = json.dumps([
        {
            "id":     r["id"],
            "worker": r["worker"].split()[0] if r.get("worker") else "Worker",
            "client": _pseudo(r["client"]),
            "date":   r["date"],
            "text":   r["text"][:2000],
        }
        for r in reports_batch
    ], indent=2)

    prompt = f"""You are a senior NDIS compliance auditor reviewing incident reports written by support workers.

The "text" field for each report contains ALL text answers from the form, labelled by question.
Evaluate the report AS A WHOLE across all answers — do not penalise a worker for missing detail
in one field if that detail is present in another field of the same report.

Evaluate each report against the NDIS Incident Management and Reportable Incidents Rules 2018.

A compliant incident report must (across all its fields combined):
- Describe what happened factually and objectively (no opinions)
- Include enough detail to reconstruct the event (who, what, when, where)
- Describe the participant's condition or behaviour before and after
- Describe the support worker's response and actions taken
- Use clear, plain English

Incident reports to evaluate:
{payload}

For EACH description return a JSON object with:
- id: (same as input)
- passes_ndis_standard: true/false
- has_sufficient_detail: true/false
- is_factual_and_objective: true/false
- describes_worker_response: true/false
- describes_participant_condition: true/false
- is_plain_english: true/false
- issues: [list of specific problems — empty if none]
- severity: "PASS", "LOW", "MEDIUM", or "HIGH"

Return ONLY a valid JSON array. No explanation, no markdown."""

    try:
        response = ai_client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
            raw = raw.rsplit("```", 1)[0].strip()
        results = json.loads(raw)
        return {r["id"]: r for r in results}
    except Exception as e:
        print(f"  [WARNING] Claude incident report assessment failed: {e}")
        return {}


# ---------------------------------------------
# MAIN AUDIT
# ---------------------------------------------

def run_audit(days_back=7, start_override=None, end_override=None, worker_id_filter=None):
    """
    start_override / end_override: datetime objects (AEST) for invoice-scoped audits.
    worker_id_filter: str user ID — when set, only issues for that worker are returned.
    """
    now       = datetime.now(AEST)
    # Randomise the grace period (20–50 min) so notifications don't fire at a
    # predictable interval — prevents it feeling like an automated system.
    _clockout_grace_secs = random.randint(20, 50) * 60
    if start_override and end_override:
        start_dt = start_override
        end_dt   = end_override
    else:
        start_dt = now - timedelta(days=days_back)
        end_dt   = now
    start_date = start_dt.strftime("%Y-%m-%d")
    end_date   = end_dt.strftime("%Y-%m-%d")
    start_ts   = int(start_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    end_ts     = int(end_dt.replace(hour=23, minute=59, second=59, microsecond=0).timestamp())

    print(f"\nNDIS Compliance Audit -- {start_dt.strftime('%d %b')} to {end_dt.strftime('%d %b %Y')}")
    if worker_id_filter:
        print(f"Scoped to worker ID: {worker_id_filter}")
    print("Fetching data from Connecteam (parallel)...")

    # Clear per-run caches so stale data is never used
    _FORM_SUBS_CACHE.clear()
    fetch_user_custom_fields.cache_clear()
    fetch_timesheet.cache_clear()

    # Fire ALL independent fetches concurrently in one batch:
    # core data + forms (×11) + timesheet + custom fields
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as _pool:
        _f_users   = _pool.submit(fetch_all_users)
        _f_jobs    = _pool.submit(fetch_all_jobs)
        _f_shifts  = _pool.submit(fetch_scheduled_shifts, start_ts, end_ts)
        _f_acts    = _pool.submit(fetch_time_activities, start_date, end_date)
        _f_pending = _pool.submit(fetch_pending_shift_ids, start_date, end_date)
        _f_geo     = _pool.submit(fetch_geofences)
        _f_breaks  = _pool.submit(fetch_manual_breaks, start_date, end_date)
        _f_unavail = _pool.submit(fetch_user_unavailabilities, start_ts, end_ts)
        _f_layers  = _pool.submit(fetch_shift_layers)
        _f_time    = _pool.submit(fetch_timesheet, start_date, end_date)
        _f_cf      = _pool.submit(fetch_user_custom_fields)
        _f_forms   = {fid: _pool.submit(fetch_form_submissions, fid) for fid in FORMS.values()}

        users              = _f_users.result()
        jobs               = _f_jobs.result()
        scheduled_shifts   = _f_shifts.result()
        activities_by_user = _f_acts.result()
        pending_shift_ids  = _f_pending.result()
        geofences          = _f_geo.result()
        breaks_by_activity = _f_breaks.result()
        unavailabilities   = _f_unavail.result()
        _layers_raw        = _f_layers.result()
        _f_time.result()   # warms lru_cache — later call in Section 9 is instant
        _f_cf.result()     # warms lru_cache — later call in Section 8 (credentials) is instant
        for fid, fut in _f_forms.items():
            try:    _FORM_SUBS_CACHE[fid] = fut.result()
            except: _FORM_SUBS_CACHE[fid] = []

    # Scope to a single worker when running an invoice audit
    if worker_id_filter:
        wid_str = str(worker_id_filter)
        activities_by_user = {k: v for k, v in activities_by_user.items() if str(k) == wid_str}

    # Onboarding needs active_user_ids — only remaining serial fetch
    active_user_ids = set(users.keys())
    onboarding_gaps = fetch_onboarding_completion(active_user_ids)

    # Shift layers (safe to fail)
    try:
        shift_layers = _layers_raw
        layer_names = {str(l.get("id") or l.get("layerId")): l.get("name", "Layer") for l in shift_layers}
    except Exception:
        shift_layers = []
        layer_names = {}

    # Build geofence radius lookup: for a given job address, find the nearest
    # configured geofence and use its radius instead of the hardcoded threshold.
    def geofence_radius_for_job(job_lat, job_lon):
        best_dist, best_radius = float("inf"), GPS_THRESHOLD_KM
        for gf in geofences:
            gf_lat  = gf.get("latitude") or gf.get("lat", 0)
            gf_lon  = gf.get("longitude") or gf.get("lng", gf.get("lon", 0))
            radius_m = gf.get("radius") or gf.get("radiusMeters", 0)
            if not (gf_lat and gf_lon and radius_m):
                continue
            d = haversine_km(job_lat, job_lon, gf_lat, gf_lon)
            if d < best_dist:
                best_dist   = d
                best_radius = radius_m / 1000.0
        return max(best_radius, 0.05)  # floor at 50 m

    # Build unavailability date lookup: uid -> set of "YYYY-MM-DD" dates on leave
    unavail_dates = defaultdict(set)
    for uid, windows in unavailabilities.items():
        for s, e in windows:
            if not s:
                continue
            cur = datetime.fromtimestamp(s, tz=AEST).date()
            end_d = datetime.fromtimestamp(e, tz=AEST).date() if e else cur
            while cur <= end_d:
                unavail_dates[uid].add(cur.strftime("%Y-%m-%d"))
                cur += timedelta(days=1)

    active_jobs = {jid: j for jid, j in jobs.items() if not j.get("isDeleted")}

    def uname(uid):
        u = users.get(uid)
        return f"{u['firstName']} {u['lastName']}" if u else f"User {uid}"

    def jname(jid):
        j = jobs.get(jid)
        return j["title"] if j else "(unknown client)"

    issues = []

    # -- build a lookup: schedulerShiftId -> list of userIds who clocked in --
    clocked_by_sched = defaultdict(list)
    for uid, acts in activities_by_user.items():
        for act in acts:
            sid = act.get("schedulerShiftId")
            if sid:
                clocked_by_sched[sid].append(uid)

    # ------------------------------------------
    # SECTION 1 -- SCHEDULED SHIFT ATTENDANCE
    # ------------------------------------------
    for shift in scheduled_shifts:
        sid         = shift["id"]
        sched_start = shift["startTime"]
        sched_end   = shift["endTime"]
        job_id      = shift.get("jobId", "")
        client      = jname(job_id)
        dlabel      = date_label(sched_start)
        s_str       = ts_aest(sched_start).strftime("%H:%M")
        e_str       = ts_aest(sched_end).strftime("%H:%M")
        assigned    = shift.get("assignedUserIds", [])

        # Open / unassigned shift
        if not assigned:
            issues.append(Issue("HIGH", "OPEN SHIFT", "(unassigned)", client, dlabel,
                f"No worker assigned to shift {s_str}–{e_str}."))

        # -- RATIO CHECK --
        is_kallan = KALLAN_TITLE_MATCH in client.lower()
        if is_kallan:
            overnight     = is_overnight(sched_start, sched_end)
            expected_ratio = 1 if overnight else 2
        else:
            expected_ratio = 1

        workers_clocked = clocked_by_sched.get(sid, [])
        actual_count    = len(workers_clocked)

        if assigned:
            if actual_count < expected_ratio and actual_count > 0:
                issues.append(Issue("HIGH", "UNDERSTAFFED -- RATIO BREACH", ", ".join(uname(u) for u in workers_clocked),
                    client, dlabel,
                    f"Expected {expected_ratio}:1 ratio -- only {actual_count} worker(s) clocked in for {s_str}–{e_str}."))
            elif actual_count == 0:
                pass  # handled by NO CLOCK-IN below
            elif actual_count > expected_ratio:
                issues.append(Issue("MEDIUM", "OVERSTAFFED -- POSSIBLE OVERBILLING", ", ".join(uname(u) for u in workers_clocked),
                    client, dlabel,
                    f"Expected {expected_ratio}:1 ratio -- {actual_count} workers clocked in for {s_str}–{e_str}. Verify funding approval."))

        for uid in assigned:
            name       = uname(uid)
            user_acts  = activities_by_user.get(uid, [])

            # Find matching clock entry
            matched = next(
                (a for a in user_acts if a.get("schedulerShiftId") == sid),
                None
            )
            # Fallback: clock entry within 4 hours of scheduled start
            if not matched:
                matched = next(
                    (a for a in user_acts if abs(a["start"]["timestamp"] - sched_start) < 14400),
                    None
                )

            if not matched:
                if sched_end > now.timestamp():
                    continue  # shift hasn't ended yet — don't audit
                shift_day = ts_aest(sched_start).strftime("%Y-%m-%d")
                if shift_day in unavail_dates.get(uid, set()):
                    issues.append(Issue("LOW", "APPROVED LEAVE", name, client, dlabel,
                        f"Rostered {s_str}–{e_str} — worker has recorded unavailability for this date (leave/approved absence)."))
                else:
                    issues.append(Issue("CRITICAL", "NO CLOCK-IN", name, client, dlabel,
                        f"Rostered {s_str}–{e_str} -- never clocked in."))
                continue

            clock_in  = matched["start"]["timestamp"]
            clock_out = matched["end"]["timestamp"] if matched.get("end") else None

            # Late clock-in
            late_secs = clock_in - sched_start
            if late_secs > (LATE_MIN * 60):
                late_min = round(late_secs / 60)
                sev = "CRITICAL" if late_min > 120 else "HIGH" if late_min > 60 else "MEDIUM"
                issues.append(Issue(sev, "LATE CLOCK-IN", name, client, dlabel,
                    f"{late_min} min late -- scheduled {s_str}, arrived {ts_aest(clock_in).strftime('%H:%M')}."))

            if not clock_out:
                if sched_end > now.timestamp():
                    continue  # shift hasn't ended yet — don't audit
                if now.timestamp() > sched_end + _clockout_grace_secs:
                    overdue_min = round((now.timestamp() - sched_end) / 60)
                    issues.append(Issue("HIGH", "MISSING CLOCK-OUT", name, client, dlabel,
                        f"Scheduled until {e_str} — still clocked in, {overdue_min} min past end time."))
            else:
                # Early clock-out
                early_secs = sched_end - clock_out
                if early_secs > (EARLY_MIN * 60):
                    early_min = round(early_secs / 60)
                    sev = "CRITICAL" if early_min > 120 else "HIGH" if early_min > 60 else "MEDIUM"
                    issues.append(Issue(sev, "EARLY CLOCK-OUT", name, client, dlabel,
                        f"Left {early_min} min early -- scheduled until {e_str}, left at {ts_aest(clock_out).strftime('%H:%M')}."))

                # Auto clock-out
                if matched.get("isAutoClockOut"):
                    issues.append(Issue("HIGH", "AUTO CLOCK-OUT", name, client, dlabel,
                        f"System force-closed shift at {ts_aest(clock_out).strftime('%H:%M')}. Worker failed to clock out manually."))

    # ------------------------------------------
    # SECTION 2 -- ALL CLOCK ENTRIES (incl. unscheduled)
    # ------------------------------------------
    for uid, user_shifts in activities_by_user.items():
        name = uname(uid)

        # Group by (job, day) for multi-clockin detection
        by_job_day = defaultdict(list)
        for act in user_shifts:
            day = ts_aest(act["start"]["timestamp"]).strftime("%Y-%m-%d")
            jid = act.get("jobId", "none")
            by_job_day[(jid, day)].append(act)

        for (jid, day), acts in by_job_day.items():
            if len(acts) > MULTI_CLOCKIN_DAILY:
                issues.append(Issue("MEDIUM", "MULTIPLE CLOCK-INS SAME CLIENT/DAY",
                    name, jname(jid) if jid != "none" else "(no client)", day,
                    f"{len(acts)} separate clock entries on same client on same day -- verify accuracy."))

        for act in user_shifts:
            clock_in  = act["start"]["timestamp"]
            clock_out = act["end"]["timestamp"] if act.get("end") else None
            job_id    = act.get("jobId")
            client    = jname(job_id) if job_id else "(no client)"
            dlabel    = date_label(clock_in)
            sched_id  = act.get("schedulerShiftId")

            # Unscheduled shift
            if not sched_id:
                issues.append(Issue("MEDIUM", "UNSCHEDULED SHIFT", name, client, dlabel,
                    f"Clocked in at {ts_aest(clock_in).strftime('%H:%M')} with no matching roster entry."))

            # Missing clock-out — flag 30 min after a reasonable shift would have ended.
            # Unscheduled shifts: use 8h as the expected max duration (no roster to compare against).
            if not clock_out:
                hours_open = (now.timestamp() - clock_in) / 3600
                if hours_open > 8.5:
                    issues.append(Issue("HIGH", "MISSING CLOCK-OUT", name, client, dlabel,
                        f"Clocked in {round(hours_open)}h ago — never clocked out (unscheduled shift)."))
                continue

            # Auto clock-out on unscheduled shift
            if act.get("isAutoClockOut") and not sched_id:
                issues.append(Issue("HIGH", "AUTO CLOCK-OUT", name, client, dlabel,
                    f"System force-closed unscheduled shift."))

            # Suspiciously short shift
            duration_min = (clock_out - clock_in) / 60
            if duration_min < SHORT_SHIFT_MIN:
                issues.append(Issue("HIGH", "SUSPICIOUSLY SHORT SHIFT", name, client, dlabel,
                    f"Shift lasted only {round(duration_min)} min "
                    f"({ts_aest(clock_in).strftime('%H:%M')}–{ts_aest(clock_out).strftime('%H:%M')}) "
                    "-- possible clock error or accidental double clock-in."))

            # Break compliance (Fair Work Act)
            duration_hours = duration_min / 60
            if duration_hours >= BREAK_REQUIRED_AFTER_HOURS:
                act_id_str   = str(act.get("id", ""))
                shift_breaks = breaks_by_activity.get(act_id_str, [])
                total_break_min = sum(
                    b.get("durationMinutes", 0) or
                    max(0, (b.get("endTime", 0) - b.get("startTime", 0)) / 60)
                    for b in shift_breaks
                )
                if total_break_min < BREAK_MINIMUM_MINS:
                    issues.append(Issue("MEDIUM", "BREAK COMPLIANCE", name, client, dlabel,
                        f"Shift was {round(duration_hours, 1)}h with no recorded break. "
                        f"Fair Work requires a {BREAK_MINIMUM_MINS}-min break after {BREAK_REQUIRED_AFTER_HOURS}h."))

            # GPS check — use actual geofence radius where configured, else GPS_THRESHOLD_KM
            if job_id:
                job     = jobs.get(job_id, {})
                job_gps = job.get("gps", {})
                job_lat = job_gps.get("latitude", 0)
                job_lon = job_gps.get("longitude", 0)
                # Fall back to manual GPS override if Connecteam job has no coordinates
                if job_lat == 0 or job_lon == 0:
                    job_title_lc = job.get("title", "").lower()
                    for kw, (ov_lat, ov_lon, _ov_r) in CLIENT_GPS_OVERRIDES.items():
                        if kw in job_title_lc:
                            job_lat, job_lon = ov_lat, ov_lon
                            break
                if job_lat != 0 and job_lon != 0:
                    radius_km = geofence_radius_for_job(job_lat, job_lon)
                    loc = act["start"].get("locationData", {})
                    if isinstance(loc, dict):
                        c_lat = loc.get("latitude", 0)
                        c_lon = loc.get("longitude", 0)
                        if c_lat != 0 and c_lon != 0:
                            dist = haversine_km(job_lat, job_lon, c_lat, c_lon)
                            if dist > radius_km:
                                issues.append(Issue("HIGH", "GPS MISMATCH", name, client, dlabel,
                                    f"Clocked in {dist:.1f}km from client's address "
                                    f"(allowed radius: {radius_km:.2f}km · "
                                    f"location: {loc.get('address', 'unknown')})."))
                        else:
                            issues.append(Issue("HIGH", "GPS DATA MISSING", name, client, dlabel,
                                "Clock-in GPS coordinates are (0,0) — location services may be "
                                "disabled on this worker's device. GPS compliance cannot be verified."))

    # ------------------------------------------
    # SECTION 2B -- ROSTER VS ACTUAL HOURS
    # ------------------------------------------
    scheduled_hours_by_worker = defaultdict(float)  # uid -> total rostered hours
    actual_hours_by_worker    = defaultdict(float)  # uid -> total clocked hours

    for shift in scheduled_shifts:
        sched_start = shift["startTime"]
        sched_end   = shift["endTime"]
        if sched_end <= now.timestamp():  # only count completed shifts
            for uid in shift.get("assignedUserIds", []):
                scheduled_hours_by_worker[uid] += (sched_end - sched_start) / 3600

    for uid, acts in activities_by_user.items():
        for act in acts:
            ci = act["start"]["timestamp"]
            co = act["end"]["timestamp"] if act.get("end") else None
            if co:
                actual_hours_by_worker[uid] += (co - ci) / 3600

    all_roster_uids = set(scheduled_hours_by_worker) | set(actual_hours_by_worker)
    for uid in all_roster_uids:
        name    = uname(uid)
        sched_h = scheduled_hours_by_worker.get(uid, 0)
        actual_h = actual_hours_by_worker.get(uid, 0)
        diff     = actual_h - sched_h
        if sched_h == 0:
            continue
        if diff > 0.75:  # clocked more than 45 min over roster
            issues.append(Issue("HIGH", "OVERBILLING RISK -- HOURS EXCEED ROSTER",
                name, "(all clients)", "this period",
                f"Clocked {actual_h:.1f}h but only rostered {sched_h:.1f}h "
                f"({diff:+.1f}h). Verify all extra hours are authorised before billing."))
        elif diff < -1.5:  # clocked more than 90 min under roster
            issues.append(Issue("MEDIUM", "UNDERBILLING -- HOURS BELOW ROSTER",
                name, "(all clients)", "this period",
                f"Clocked {actual_h:.1f}h but rostered {sched_h:.1f}h "
                f"({diff:+.1f}h). Worker may have left early or forgotten to clock in."))

    # ------------------------------------------
    # SECTION 3 -- SHIFT NOTE QUALITY
    # ------------------------------------------
    notes_for_claude = []
    note_map         = {}   # id -> (uid, act, name, client, dlabel, text)
    worker_notes     = defaultdict(list)  # uid -> [(dlabel, client, text)]

    for uid, user_shifts in activities_by_user.items():
        name = uname(uid)

        for act in user_shifts:
            clock_in     = act["start"]["timestamp"]
            clock_out    = act["end"]["timestamp"] if act.get("end") else None
            job_id       = act.get("jobId")
            client       = jname(job_id) if job_id else "(no client)"
            dlabel       = date_label(clock_in)
            act_id       = act.get("id", "")
            attachments  = act.get("shiftAttachments") or []

            # Skip note checks for shifts still in progress — consistent with 8.5h clock-out threshold
            if not clock_out and (now.timestamp() - clock_in) / 3600 <= 8.5:
                continue

            # Missing signature — check regardless of pending amendments,
            # because the signature is a separate compliance requirement.
            if attachments and not has_signature(attachments):
                issues.append(Issue("MEDIUM", "MISSING SIGNATURE", name, client, dlabel,
                    "Required participant/client signature not completed."))

            note_text = get_note_text(attachments)

            # Worker has a pending amendment — the API returns the original record,
            # not the edited version, so note content cannot be assessed yet.
            # Notify the worker their amendment was received, alert management to action it.
            if str(act_id) in pending_shift_ids:
                issues.append(Issue("MEDIUM", "PENDING AMENDMENT -- WORKER NOTICE",
                    name, client, dlabel,
                    "Worker submitted a shift amendment that is awaiting approval. "
                    "Let them know their notes have been received and are pending review — no further action needed from them."))
                issues.append(Issue("MEDIUM", "PENDING AMENDMENT -- REVIEW REQUIRED",
                    name, client, dlabel,
                    f"{name} has a shift amendment pending approval for {client} on {dlabel}. "
                    "Please review and approve or reject it in Connecteam before the next audit."))
                continue

            if not attachments:
                issues.append(Issue("HIGH", "NO SHIFT NOTES", name, client, dlabel,
                    "No shift note fields submitted at all."))
                continue

            if not note_text or not note_text.strip():
                issues.append(Issue("HIGH", "EMPTY NOTES", name, client, dlabel,
                    "Shift note field present but left completely blank."))
                continue

            clean = note_text.strip()
            wc    = word_count(clean)

            if wc < MIN_NOTE_WORDS:
                issues.append(Issue("MEDIUM", "INSUFFICIENT NOTES", name, client, dlabel,
                    f"Only {wc} words (minimum {MIN_NOTE_WORDS}). Content: '{clean[:120]}'"))
                continue  # don't queue very short notes for Claude

            # Note submitted very late (use modifiedAt as proxy)
            if clock_out:
                modified_at  = act.get("modifiedAt", clock_out)
                delay_hours  = (modified_at - clock_out) / 3600
                if delay_hours > NOTE_LATE_HOURS:
                    issues.append(Issue("MEDIUM", "LATE NOTE SUBMISSION", name, client, dlabel,
                        f"Note record modified {round(delay_hours)}h after clock-out -- possible backdating."))

            # Safeguarding keyword scan
            inc_hits  = contains_keywords(clean, INCIDENT_KEYWORDS)
            rest_hits = contains_keywords(clean, RESTRICTIVE_KEYWORDS)
            med_hits  = contains_keywords(clean, MEDICATION_KEYWORDS)

            if inc_hits:
                issues.append(Issue("HIGH", "INCIDENT KEYWORD -- VERIFY REPORT FILED",
                    name, client, dlabel,
                    f"Note mentions: {', '.join(inc_hits)}. Confirm a formal incident report was submitted."))

            if rest_hits:
                issues.append(Issue("CRITICAL", "RESTRICTIVE PRACTICE MENTIONED",
                    name, client, dlabel,
                    f"Note mentions: {', '.join(rest_hits)}. Requires formal restrictive practice authorisation and documentation."))

            if med_hits:
                issues.append(Issue("HIGH", "MEDICATION MENTIONED -- VERIFY FORM FILED",
                    name, client, dlabel,
                    f"Note mentions: {', '.join(med_hits[:3])}. A Medication Administration Record (MAR) form must be submitted for every shift where medication is given — this is a mandatory NDIS requirement. Confirm form was filed or file it now."))

            # Queue for Claude
            dur_h  = round((clock_out - clock_in) / 3600, 1) if clock_out else None
            nid    = f"{uid}_{act_id}"
            notes_for_claude.append({
                "id":             nid,
                "worker":         name,
                "client":         client,
                "date":           dlabel,
                "duration_hours": dur_h,
                "text":           clean,
            })
            note_map[nid] = (uid, act, name, client, dlabel, clean)
            worker_notes[uid].append((dlabel, client, clean))

    # -- Claude batch assessment --
    if notes_for_claude:
        if ANTHROPIC_API_KEY:
            _load_note_cache()

            # Separate cached vs uncached notes
            cached_results: dict = {}
            uncached: list = []
            for n in notes_for_claude:
                h = _note_hash(n["text"])
                n["_hash"] = h
                if h in _note_cache:
                    cached_results[n["id"]] = _note_cache[h]
                else:
                    uncached.append(n)

            print(f"Assessing {len(uncached)} notes with Claude AI"
                  f" ({len(cached_results)} cached, skipped)...")

            # Parallel batch calls for uncached notes
            batch_size = 10
            batches = [uncached[i:i + batch_size] for i in range(0, len(uncached), batch_size)]
            new_results: dict = {}
            if batches:
                max_w = min(len(batches), 15)
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as _apool:
                    futs = [_apool.submit(assess_notes_with_claude, b) for b in batches]
                    for fut in concurrent.futures.as_completed(futs):
                        new_results.update(fut.result())

            # Persist newly assessed notes to cache
            for n in uncached:
                if n["id"] in new_results:
                    _note_cache[n["_hash"]] = new_results[n["id"]]
            _save_note_cache()

            claude_results = {**cached_results, **new_results}

            for nid, a in claude_results.items():
                if nid not in note_map:
                    continue
                _, _, name, client, dlabel, _ = note_map[nid]

                if a.get("severity") == "PASS":
                    continue

                if not a.get("passes_ndis_standard"):
                    probs = "; ".join(a.get("issues", ["unspecified"]))
                    issues.append(Issue("HIGH", "FAILS NDIS STANDARD", name, client, dlabel,
                        f"Note does not meet NDIS progress note requirements. Issues: {probs}"))

                if a.get("sounds_ai_generated"):
                    issues.append(Issue("MEDIUM", "POSSIBLE AI-GENERATED NOTE", name, client, dlabel,
                        "Note has characteristics of AI-generated or templated text -- review authenticity."))

                if not a.get("is_plain_english"):
                    issues.append(Issue("MEDIUM", "NOT PLAIN ENGLISH", name, client, dlabel,
                        "Note is unclear, uses jargon, or is difficult to understand."))

                if not a.get("makes_sense"):
                    issues.append(Issue("HIGH", "NOTE DOESN'T MAKE SENSE", name, client, dlabel,
                        "Note is incoherent, contradictory, or contains nonsensical content."))

                if a.get("uses_subjective_language"):
                    issues.append(Issue("MEDIUM", "SUBJECTIVE LANGUAGE", name, client, dlabel,
                        "Note uses opinions rather than factual observations (e.g. 'was being difficult')."))

                if not a.get("is_person_centred"):
                    issues.append(Issue("MEDIUM", "NOT PERSON-CENTRED", name, client, dlabel,
                        "Note fails to reflect the participant's dignity, voice, or choices."))

                if not a.get("references_plan_goals"):
                    issues.append(Issue("MEDIUM", "NO PLAN GOAL REFERENCE", name, client, dlabel,
                        "Note does not link support provided to the participant's NDIS plan goals or outcomes. NDIS requires notes to demonstrate goal-directed support."))
        else:
            print("  [INFO] ANTHROPIC_API_KEY not set -- skipping AI note quality assessment.")

    # -- Copy-paste / duplicate notes detection (same worker, different shifts) --
    for uid, note_list in worker_notes.items():
        name = uname(uid)
        for i in range(len(note_list)):
            for j in range(i + 1, len(note_list)):
                d1, c1, t1 = note_list[i]
                d2, c2, t2 = note_list[j]
                if len(t1) > 40 and len(t2) > 40:
                    sim = similarity(t1, t2)
                    if sim >= COPY_PASTE_THRESHOLD:
                        already = any(
                            iss.category == "DUPLICATE/COPY-PASTE NOTES"
                            and iss.worker == name
                            and d1 in iss.date and d2 in iss.date
                            for iss in issues
                        )
                        if not already:
                            issues.append(Issue("HIGH", "DUPLICATE/COPY-PASTE NOTES", name,
                                f"{c1} / {c2}", f"{d1} & {d2}",
                                f"Notes are {round(sim * 100)}% identical across shifts -- "
                                "worker may be copying notes rather than documenting each shift separately."))

    # -- Cross-worker copy-paste: different workers, same client, same day --
    client_day_notes = defaultdict(list)  # (client, dlabel) -> [(uid, text)]
    for uid, note_list in worker_notes.items():
        for dlabel_n, client_n, text_n in note_list:
            client_day_notes[(client_n, dlabel_n)].append((uid, text_n))

    seen_cross_pairs = set()
    for (client_n, day_n), entries in client_day_notes.items():
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                uid1, t1 = entries[i]
                uid2, t2 = entries[j]
                if uid1 == uid2 or len(t1) < 40 or len(t2) < 40:
                    continue
                sim = similarity(t1, t2)
                if sim >= COPY_PASTE_THRESHOLD:
                    pair_key = (min(uid1, uid2), max(uid1, uid2), client_n, day_n)
                    if pair_key not in seen_cross_pairs:
                        seen_cross_pairs.add(pair_key)
                        issues.append(Issue("HIGH", "CROSS-WORKER COPY-PASTE NOTES",
                            f"{uname(uid1)} / {uname(uid2)}", client_n, day_n,
                            f"Two workers submitted {round(sim * 100)}% identical notes for "
                            f"{client_n} on {day_n} — possible template sharing or note fabrication."))

    # ------------------------------------------
    # SECTION 4 -- INCIDENT & FORM COMPLIANCE
    # ------------------------------------------
    # Only these forms get completeness + timeliness checks
    INCIDENT_FORMS = {
        "Kallan Incident Report": FORMS["Kallan Incident Report"],
        "Incident Report":        FORMS["Incident Report"],
        "Safety Hazard Report":   FORMS["Safety Hazard Report"],
        "Medication Incident Form": FORMS["Medication Incident Form"],
    }
    incident_reports_for_claude = []  # collected for AI quality assessment after the loop
    for form_name, form_id in INCIDENT_FORMS.items():
        submissions = fetch_form_submissions(form_id)

        for sub in submissions:
            sub_ts      = sub.get("submissionTimestamp", 0)
            if sub_ts < start_ts:
                continue  # outside our audit window

            sub_dt      = ts_aest(sub_ts)
            submitter   = uname(sub.get("submittingUserId")) if sub.get("submittingUserId") else "Unknown"
            entry_num   = sub.get("entryNum", "?")
            answers     = sub.get("answers", [])

            # Use the incident date recorded in the form (when it happened), not the submission
            # date (when the report was filed). This prevents flagging workers for a day they
            # didn't work just because they filed the form that day.
            incident_ts = next(
                (a["timestamp"] for a in answers
                 if a.get("questionType") == "datetime" and a.get("timestamp")),
                None
            )
            if incident_ts:
                dlabel = ts_aest(incident_ts).strftime("%a %d-%b") + f" (filed {sub_dt.strftime('%d-%b')})"
            else:
                dlabel = sub_dt.strftime("%a %d-%b")

            # Completeness -- collect ALL text fields from the form, not just the first.
            # Incident forms have separate questions for "what happened", "worker response",
            # "participant condition" etc — reading only the first field causes false negatives.
            text_parts = []
            for a in answers:
                if a.get("questionType") in ("freeText", "openEnded"):
                    label = str(a.get("questionText") or a.get("label") or "").strip()
                    val   = str(a.get("value", a.get("freeText", ""))).strip()
                    if val:
                        text_parts.append(f"{label}: {val}" if label else val)
            desc_text = "\n".join(text_parts)

            if not desc_text:
                issues.append(Issue("CRITICAL", "INCOMPLETE INCIDENT REPORT",
                    submitter, form_name, dlabel,
                    f"Entry #{entry_num} — submitted with no written description. "
                    "Non-compliant with NDIS incident documentation requirements."))
            elif len(desc_text.split()) < 20:
                issues.append(Issue("HIGH", "INCIDENT REPORT — DESCRIPTION TOO BRIEF",
                    submitter, form_name, dlabel,
                    f"Entry #{entry_num} — description is only {len(desc_text.split())} words: "
                    f'"{desc_text[:120]}". NDIS requires sufficient detail to reconstruct the event.'))

            # Queue substantive descriptions for AI quality assessment
            if desc_text and len(desc_text.split()) >= 20:
                incident_reports_for_claude.append({
                    "id":     f"{form_id}_{entry_num}",
                    "worker": submitter,
                    "client": form_name,
                    "date":   dlabel,
                    "text":   desc_text,
                    "_submitter": submitter,
                    "_form_name": form_name,
                    "_dlabel":    dlabel,
                    "_entry":     entry_num,
                })

            # Check for witness / notification fields
            answered_qs = {
                str(a.get("questionText") or a.get("label") or "").lower(): str(a.get("value", "")).strip()
                for a in answers
            }
            witness_answered = any(
                "witness" in q or "notif" in q or "guardian" in q or "family" in q
                for q, v in answered_qs.items() if v
            )
            if desc_text and not witness_answered:
                issues.append(Issue("HIGH", "INCIDENT REPORT — MISSING WITNESS/NOTIFICATION",
                    submitter, form_name, dlabel,
                    f"Entry #{entry_num} — no witness or guardian/family notification recorded. "
                    "NDIS requires documentation of who was notified after an incident."))

            # NDIS incident severity classification (Rules 2018, s.12)
            if desc_text:
                desc_lower = desc_text.lower()
                type1_hits = [kw for kw in TYPE1_INCIDENT_KEYWORDS if kw in desc_lower]
                if type1_hits:
                    issues.append(Issue("CRITICAL",
                        "TYPE 1 NOTIFIABLE INCIDENT — 24H NDIS COMMISSION REPORT REQUIRED",
                        submitter, form_name, dlabel,
                        f"Entry #{entry_num} — description contains Type 1 indicators: "
                        f"{', '.join(type1_hits[:4])}. Under the NDIS (Incident Management and "
                        "Reportable Incidents) Rules 2018, this must be notified to the NDIS "
                        "Commission within 24 hours. Escalate to management immediately."))
                else:
                    type2_hits = [kw for kw in TYPE2_INCIDENT_KEYWORDS if kw in desc_lower]
                    if type2_hits:
                        issues.append(Issue("HIGH",
                            "TYPE 2 NOTIFIABLE INCIDENT — 5-DAY NDIS COMMISSION REPORT REQUIRED",
                            submitter, form_name, dlabel,
                            f"Entry #{entry_num} — description suggests a Type 2 reportable incident: "
                            f"{', '.join(type2_hits[:4])}. If confirmed, must be notified to the NDIS "
                            "Commission within 5 business days under the NDIS Rules 2018."))

            # Timeliness -- gap between incident occurrence and submission
            if incident_ts:
                delay_h = (sub_ts - incident_ts) / 3600
                if delay_h > 120:
                    issues.append(Issue("CRITICAL", "LATE INCIDENT REPORTING",
                        submitter, form_name, dlabel,
                        f"Entry #{entry_num} -- occurred {ts_aest(incident_ts).strftime('%a %d-%b %H:%M')}, "
                        f"reported {round(delay_h / 24, 1)} days later. "
                        "NDIS requires serious incidents within 24h, others within 5 business days."))
                elif delay_h > 24:
                    issues.append(Issue("HIGH", "POSSIBLE LATE INCIDENT REPORTING",
                        submitter, form_name, dlabel,
                        f"Entry #{entry_num} -- {round(delay_h)}h between incident and report. "
                        "If this was a serious incident, the 24h NDIS Commission notification window may have passed."))

    # -- AI quality assessment for incident report descriptions --
    if incident_reports_for_claude and ANTHROPIC_API_KEY:
        print(f"Assessing {len(incident_reports_for_claude)} incident report(s) with Claude AI...")
        batch_size = 10
        batches = [incident_reports_for_claude[i:i + batch_size]
                   for i in range(0, len(incident_reports_for_claude), batch_size)]
        ir_results = {}
        max_w = min(len(batches), 5)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as _irpool:
            futs = [_irpool.submit(assess_incident_reports_with_claude, b) for b in batches]
            for fut in concurrent.futures.as_completed(futs):
                ir_results.update(fut.result())

        for r in incident_reports_for_claude:
            rid = r["id"]
            a   = ir_results.get(rid)
            if not a or a.get("severity") == "PASS":
                continue
            subm  = r["_submitter"]
            fname = r["_form_name"]
            dlbl  = r["_dlabel"]
            entry = r["_entry"]

            if not a.get("passes_ndis_standard"):
                probs = "; ".join(a.get("issues", ["unspecified"]))
                issues.append(Issue("HIGH", "INCIDENT REPORT — FAILS NDIS STANDARD",
                    subm, fname, dlbl,
                    f"Entry #{entry} — description does not meet NDIS incident documentation requirements. Issues: {probs}"))

            if not a.get("has_sufficient_detail"):
                issues.append(Issue("MEDIUM", "INCIDENT REPORT — INSUFFICIENT DETAIL",
                    subm, fname, dlbl,
                    f"Entry #{entry} — description lacks enough detail to reconstruct what happened. "
                    "Include who was involved, what occurred, when, and where."))

            if not a.get("is_factual_and_objective"):
                issues.append(Issue("MEDIUM", "INCIDENT REPORT — SUBJECTIVE LANGUAGE",
                    subm, fname, dlbl,
                    f"Entry #{entry} — description contains opinions rather than factual observations."))

            if not a.get("describes_worker_response"):
                issues.append(Issue("MEDIUM", "INCIDENT REPORT — MISSING WORKER RESPONSE",
                    subm, fname, dlbl,
                    f"Entry #{entry} — does not describe what the support worker did in response to the incident."))

            if not a.get("describes_participant_condition"):
                issues.append(Issue("MEDIUM", "INCIDENT REPORT — MISSING PARTICIPANT CONDITION",
                    subm, fname, dlbl,
                    f"Entry #{entry} — does not describe the participant's condition before or after the incident."))

    # ------------------------------------------
    # SECTION 5 -- CLIENT-SPECIFIC COMPLIANCE
    # ------------------------------------------

    # Build: job_title -> set of days shifts occurred
    # Also: job_title -> day -> set of worker user IDs
    client_shift_days     = defaultdict(set)   # "Kallan Jordan" -> {"2026-05-16", ...}
    client_shift_workers  = defaultdict(lambda: defaultdict(set))  # title -> day -> {uid}
    kallan_overnight_days = set()  # days when a Kallan overnight shift occurred

    for uid, user_shifts in activities_by_user.items():
        for act in user_shifts:
            jid = act.get("jobId")
            if not jid:
                continue
            job = jobs.get(jid, {})
            if job.get("isDeleted"):
                continue
            title = job.get("title", "")
            day   = ts_aest(act["start"]["timestamp"]).strftime("%Y-%m-%d")
            client_shift_days[title].add(day)
            client_shift_workers[title][day].add(uid)

    # Flag Kallan overnight shift days from the scheduler
    for shift in scheduled_shifts:
        jid = shift.get("jobId", "")
        job = jobs.get(jid, {})
        if "kallan" in job.get("title", "").lower():
            if is_overnight(shift["startTime"], shift["endTime"]):
                kallan_overnight_days.add(
                    ts_aest(shift["startTime"]).strftime("%Y-%m-%d"))

    # Helper: fetch submissions for a form grouped by day (within audit window)
    def subs_by_day(form_id):
        result = defaultdict(list)
        for s in fetch_form_submissions(form_id):
            ts = s.get("submissionTimestamp", 0)
            if ts >= start_ts:
                result[ts_aest(ts).strftime("%Y-%m-%d")].append(s)
        return result

    # Helper: count submissions in window matching a submitter set (optional)
    def count_subs_in_window(form_id, submitter_ids=None):
        count = 0
        for s in fetch_form_submissions(form_id):
            ts = s.get("submissionTimestamp", 0)
            if ts < start_ts:
                continue
            if submitter_ids is None or s.get("submittingUserId") in submitter_ids:
                count += 1
        return count

    # Collect all workers who appear against each client this week
    def workers_for_client(title_keyword):
        uids = set()
        for title, day_workers in client_shift_workers.items():
            if title_keyword.lower() in title.lower():
                for uid_set in day_workers.values():
                    uids |= uid_set
        return uids

    # ── KALLAN JORDAN -- per shift day ──────────────────────────────────────
    kallan_days = set()
    for title, days in client_shift_days.items():
        if "kallan" in title.lower():
            kallan_days |= days

    kallan_incident_by_day  = subs_by_day(FORMS["Kallan Incident Report"])
    kallan_abc_by_day       = subs_by_day(FORMS["Kallan: ABC Form"])
    kallan_cleaning_by_day  = subs_by_day(FORMS["Kallan Daily Cleaning"])
    kallan_sleep_by_day     = subs_by_day(FORMS["Kallan Sleep Observation"])

    for day in sorted(kallan_days):
        dlabel_d = ts_aest(datetime.strptime(day, "%Y-%m-%d").replace(
            tzinfo=AEST).timestamp()).strftime("%a %d-%b")

        if not kallan_incident_by_day.get(day):
            issues.append(Issue("HIGH", "MISSING FORM -- KALLAN",
                "(team)", "Kallan Jordan", dlabel_d,
                "Kallan Incident Report not submitted for this shift day (required every shift)."))

        if not kallan_abc_by_day.get(day):
            issues.append(Issue("HIGH", "MISSING FORM -- KALLAN",
                "(team)", "Kallan Jordan", dlabel_d,
                "Kallan ABC Form not submitted for this shift day (required every shift)."))

        if not kallan_cleaning_by_day.get(day):
            issues.append(Issue("MEDIUM", "MISSING FORM -- KALLAN",
                "(team)", "Kallan Jordan", dlabel_d,
                "Kallan Daily Cleaning Checklist not submitted (required once per day)."))

        if day in kallan_overnight_days and not kallan_sleep_by_day.get(day):
            issues.append(Issue("HIGH", "MISSING FORM -- KALLAN",
                "(team)", "Kallan Jordan", dlabel_d,
                "Kallan Sleep Observation Log not submitted for overnight shift."))

    # ── EVAN GATT -- incident report every shift day ─────────────────────────
    evan_days    = set()
    evan_workers = workers_for_client("evan")
    for title, days in client_shift_days.items():
        if "evan" in title.lower():
            evan_days |= days

    gen_incident_by_day = subs_by_day(FORMS["Incident Report"])

    for day in sorted(evan_days):
        dlabel_d = ts_aest(datetime.strptime(day, "%Y-%m-%d").replace(
            tzinfo=AEST).timestamp()).strftime("%a %d-%b")
        # Check if any Evan worker submitted on this day
        day_subs = [s for s in gen_incident_by_day.get(day, [])
                    if s.get("submittingUserId") in evan_workers]
        if not day_subs:
            issues.append(Issue("HIGH", "MISSING FORM -- EVAN",
                "(team)", "Evan Gatt", dlabel_d,
                "Incident Report not submitted for this shift day (required every shift)."))

    # ── MICHAEL LAWRIE -- Peter's medication form every shift day ────────────
    michael_days = set()
    for title, days in client_shift_days.items():
        if "michael" in title.lower():
            michael_days |= days

    michael_med_by_day = subs_by_day(FORMS["Michael Medications Plan"])

    for day in sorted(michael_days):
        dlabel_d = ts_aest(datetime.strptime(day, "%Y-%m-%d").replace(
            tzinfo=AEST).timestamp()).strftime("%a %d-%b")
        # Check Peter submitted the form on this day
        peter_subs = [s for s in michael_med_by_day.get(day, [])
                      if s.get("submittingUserId") == PETER_USER_ID]
        if not peter_subs:
            issues.append(Issue("HIGH", "MISSING FORM -- MICHAEL",
                "Peter Eronmwon", "Michael Lawrie", dlabel_d,
                "Michael Medications Plan Form not submitted by Peter (required every shift -- morning and evening medication)."))
        elif len(peter_subs) < 2:
            # Peter worked Michael -- should submit twice (morning + evening)
            michael_worker_days = client_shift_workers.get("Michael Lawrie", {})
            peter_shifts_today  = sum(
                1 for act in activities_by_user.get(PETER_USER_ID, [])
                if act.get("jobId") in [jid for jid, j in jobs.items()
                                         if "michael" in j.get("title", "").lower()]
                and ts_aest(act["start"]["timestamp"]).strftime("%Y-%m-%d") == day
            )
            if peter_shifts_today >= 2:
                issues.append(Issue("MEDIUM", "MISSING FORM -- MICHAEL",
                    "Peter Eronmwon", "Michael Lawrie", dlabel_d,
                    "Michael Medications Plan Form only submitted once -- Peter worked morning and evening so two submissions expected."))

    # ── PER-PERIOD FORM FREQUENCY CHECKS ─────────────────────────────────────
    # Scale weekly minimums by the number of weeks in the audit period so
    # invoice audits (e.g. 1–15 June = 2 weeks) use the correct threshold.
    period_weeks = max(1, round((end_ts - start_ts) / (7 * 86400)))
    period_label_freq = "this week" if period_weeks == 1 else f"this {period_weeks}-week period"

    def period_incident_count_for_client(title_keyword):
        workers = workers_for_client(title_keyword)
        return sum(
            1 for s in fetch_form_submissions(FORMS["Incident Report"])
            if s.get("submissionTimestamp", 0) >= start_ts
            and s.get("submittingUserId") in workers
        )

    # Joshua -- 2x incident report, 2x ABC form per week
    joshua_workers   = workers_for_client("josh")
    joshua_incidents = period_incident_count_for_client("josh")
    joshua_abc       = count_subs_in_window(FORMS["Joshua: ABC Form"], joshua_workers)
    joshua_min       = 2 * period_weeks

    if joshua_incidents < joshua_min:
        issues.append(Issue("HIGH", "FORM FREQUENCY -- JOSHUA",
            "(team)", "Joshua Gatt", end_date,
            f"Incident Report submitted {joshua_incidents}x {period_label_freq} -- minimum is {joshua_min}."))
    if joshua_abc < joshua_min:
        issues.append(Issue("HIGH", "FORM FREQUENCY -- JOSHUA",
            "(team)", "Joshua Gatt", end_date,
            f"Joshua ABC Form submitted {joshua_abc}x {period_label_freq} -- minimum is {joshua_min}."))

    # Nada Haliem -- 2x incident report per week
    nada_incidents = period_incident_count_for_client("nada")
    nada_min       = 2 * period_weeks
    if nada_incidents < nada_min:
        issues.append(Issue("HIGH", "FORM FREQUENCY -- NADA",
            "(team)", "Nada Haliem", end_date,
            f"Incident Report submitted {nada_incidents}x {period_label_freq} -- minimum is {nada_min}."))

    # John -- 2x incident report per week
    john_incidents = period_incident_count_for_client("john")
    john_min       = 2 * period_weeks
    if john_incidents < john_min:
        issues.append(Issue("HIGH", "FORM FREQUENCY -- JOHN",
            "(team)", "John", end_date,
            f"Incident Report submitted {john_incidents}x {period_label_freq} -- minimum is {john_min}."))

    # Nicole -- 1x incident report per week
    nicole_incidents = period_incident_count_for_client("nicole")
    nicole_min       = 1 * period_weeks
    if nicole_incidents < nicole_min:
        issues.append(Issue("HIGH", "FORM FREQUENCY -- NICOLE",
            "(team)", "Nicole Loveless", end_date,
            f"Incident Report submitted {nicole_incidents}x {period_label_freq} -- minimum is {nicole_min}."))

    # ── BSP REFERENCE CHECKING -- Kallan Jordan & Joshua Gatt ────────────────
    # These clients have active Behaviour Support Plans. Shift notes must
    # reference BSP strategies to demonstrate plan-directed support delivery.
    BSP_KEYWORDS = [
        "behaviour support", "bsp", "behaviour plan", "support plan",
        "positive behaviour", "de-escalat", "trigger", "redirect",
        "distract", "sensory", "routine", "structured activity",
        "planned strategy", "planned activity", "abc form",
    ]
    BSP_CLIENT_KEYWORDS = {"kallan", "joshua", "josh"}

    for uid, note_list in worker_notes.items():
        worker_name_bsp = uname(uid)
        for note_dlabel, note_client, note_text in note_list:
            if not note_client:
                continue
            client_lower = note_client.lower()
            if not any(kw in client_lower for kw in BSP_CLIENT_KEYWORDS):
                continue
            if word_count(note_text) < MIN_NOTE_WORDS:
                continue  # too short to expect a BSP reference
            note_lower = note_text.lower()
            if not any(kw in note_lower for kw in BSP_KEYWORDS):
                issues.append(Issue("MEDIUM", "MISSING BSP REFERENCE IN NOTE",
                    worker_name_bsp, note_client, note_dlabel,
                    f"Shift note for {note_client} does not reference the participant's "
                    "Behaviour Support Plan strategies. NDIS requires notes to demonstrate "
                    "that supports were delivered in accordance with the active BSP "
                    "(e.g. reference to de-escalation strategies, triggers, routines, "
                    "or specific BSP-directed activities)."))

    # ------------------------------------------
    # SECTION 6 -- ONBOARDING COMPLIANCE
    # ------------------------------------------
    for uid, incomplete_packs in onboarding_gaps.items():
        name = uname(uid)
        for pack_name in incomplete_packs:
            issues.append(Issue("HIGH", "ONBOARDING INCOMPLETE", name,
                "(all clients)", end_date,
                f"Worker has not completed onboarding pack: '{pack_name}'. "
                "Incomplete onboarding may mean mandatory NDIS training has not been done."))

    # ------------------------------------------
    # SECTION 7 -- WORKER-CLIENT ASSIGNMENT AUTHORISATION
    # ------------------------------------------
    # Collect unique (uid, job_id) pairs from actual clock-in data
    worker_job_pairs = set()
    for uid, acts in activities_by_user.items():
        for act in acts:
            jid = act.get("jobId")
            if jid:
                worker_job_pairs.add((uid, jid))

    unique_workers_with_clocks = {uid for uid, _ in worker_job_pairs}
    assignments_cache = {}
    for uid in unique_workers_with_clocks:
        try:
            data = ct_get(f"/users/v1/users/{uid}/assignments")
            raw  = data.get("data") or {}
            assigned_jobs = set()
            for key in ("assignments", "jobs", "items"):
                entries = raw.get(key)
                if entries and isinstance(entries, list):
                    for a in entries:
                        jid = a.get("jobId") or a.get("id")
                        if jid:
                            assigned_jobs.add(jid)
                    break
            if assigned_jobs:
                assignments_cache[uid] = assigned_jobs
        except Exception:
            continue

    seen_auth_flags = set()
    for uid, jid in worker_job_pairs:
        if uid not in assignments_cache:
            continue
        assigned_jobs = assignments_cache[uid]
        if jid not in assigned_jobs:
            flag_key = (uid, jid)
            if flag_key not in seen_auth_flags:
                seen_auth_flags.add(flag_key)
                name   = uname(uid)
                client = jname(jid)
                issues.append(Issue("HIGH", "UNAUTHORISED CLIENT ACCESS", name, client,
                    end_date,
                    f"Worker clocked in for '{client}' but is not formally assigned to this client in Connecteam HR. "
                    "Verify this was authorised by management."))

    # ------------------------------------------
    # SECTION 8 -- WORKER CREDENTIAL EXPIRY
    # ------------------------------------------
    try:
        cred_data = fetch_worker_credentials(users)
        today     = now.date()
        for uid, creds in cred_data.items():
            wname = uname(uid)
            for cred_type, expiry_date in creds.items():
                days_left = (expiry_date - today).days
                dlabel_c  = now.strftime("%a %d-%b")
                if days_left < 0:
                    issues.append(Issue("CRITICAL", "EXPIRED CREDENTIAL", wname,
                        "(all clients)", dlabel_c,
                        f"{cred_type} expired {abs(days_left)} day(s) ago on "
                        f"{expiry_date.strftime('%d %b %Y')}. Worker must NOT provide "
                        "supports until credential is renewed."))
                elif days_left <= 30:
                    issues.append(Issue("HIGH", "CREDENTIAL EXPIRING SOON", wname,
                        "(all clients)", dlabel_c,
                        f"{cred_type} expires in {days_left} day(s) on "
                        f"{expiry_date.strftime('%d %b %Y')}. Action required within 2 weeks."))
                elif days_left <= 60:
                    issues.append(Issue("MEDIUM", "CREDENTIAL EXPIRING SOON", wname,
                        "(all clients)", dlabel_c,
                        f"{cred_type} expires in {days_left} day(s) on "
                        f"{expiry_date.strftime('%d %b %Y')}. Begin renewal process now."))
    except Exception as e:
        print(f"  [WARNING] Credential expiry check failed: {e}")

    # ------------------------------------------
    # SECTION 9 — TIMESHEET BILLING AUDIT
    # ------------------------------------------
    try:
        timesheet = fetch_timesheet(start_date, end_date)
        for entry in timesheet:
            uid        = entry.get("userId")
            name       = uname(uid) if uid else "Unknown"
            total_hrs  = float(entry.get("totalHours") or entry.get("totalWorkedHours") or 0)
            pay_rate   = entry.get("payRate") or entry.get("hourlyRate")
            week_days  = days_back

            # No pay rate configured
            if pay_rate is None:
                issues.append(Issue("MEDIUM", "NO PAY RATE CONFIGURED", name,
                    "(payroll)", end_date,
                    f"Worker has no pay rate set in Connecteam — payroll and NDIS billing cannot be verified."))

            # Excessive hours (SCHADS Award: 38h/week ordinary, 10h/day max)
            weekly_cap = 38.0 * (days_back / 7)
            if total_hrs > weekly_cap and days_back >= 7:
                issues.append(Issue("HIGH", "EXCESSIVE HOURS — BILLING RISK", name,
                    "(payroll)", end_date,
                    f"Worker recorded {total_hrs:.1f}h over {days_back} days "
                    f"(SCHADS ordinary-hours cap ~{weekly_cap:.0f}h for this period). "
                    "Verify NDIS billing and check for duplicate entries."))

            # Suspiciously high pay rate (> $100/hr is almost certainly a data entry error)
            if pay_rate and float(pay_rate) > 100:
                issues.append(Issue("HIGH", "ABNORMAL PAY RATE", name,
                    "(payroll)", end_date,
                    f"Pay rate ${float(pay_rate):.2f}/hr appears unusually high. "
                    "Verify against SCHADS Award and NDIS price guide."))
    except Exception as e:
        print(f"  [WARNING] Timesheet billing audit failed: {e}")

    # ------------------------------------------
    # SECTION 10 — LEAVE BALANCE ALERTS
    # ------------------------------------------
    try:
        leave_policies = fetch_time_off_policies()
        for policy in leave_policies:
            pol_id   = policy.get("id") or policy.get("policyTypeId")
            pol_name = policy.get("name") or policy.get("title") or "Leave"
            if not pol_id:
                continue
            balances = fetch_time_off_balances(pol_id)
            for bal in balances:
                uid      = bal.get("userId")
                balance  = float(bal.get("balance") or bal.get("remainingBalance") or 0)
                name     = uname(uid) if uid else "Unknown"
                # Flag workers with negative leave balance (borrowed leave)
                if balance < 0:
                    issues.append(Issue("MEDIUM", "NEGATIVE LEAVE BALANCE", name,
                        "(HR)", end_date,
                        f"{pol_name}: balance is {balance:.1f}h — worker is in negative leave. "
                        "Verify entitlement and payroll treatment."))
    except Exception as e:
        print(f"  [WARNING] Leave balance check failed: {e}")

    # ------------------------------------------
    # SECTION 11 — PAY RATE AUDIT
    # ------------------------------------------
    try:
        pay_rates_list = fetch_pay_rates()
        workers_with_rates = {
            (r.get("userId") or r.get("user", {}).get("id")): float(r.get("rate") or r.get("payRate") or 0)
            for r in pay_rates_list
            if r.get("userId") or (r.get("user") or {}).get("id")
        }
        for uid in users:
            name = uname(uid)
            if uid not in workers_with_rates:
                issues.append(Issue("LOW", "NO PAY RATE CONFIGURED", name,
                    "(payroll)", end_date,
                    "Worker profile has no pay rate set — payroll exports will be incomplete."))
    except Exception as e:
        print(f"  [WARNING] Pay rate audit failed: {e}")

    # ------------------------------------------
    # PRINT REPORT
    # ------------------------------------------
    sorted_issues = sorted(
        issues,
        key=lambda x: (SEV_ORDER.get(x.severity, 5), x.category, x.worker)
    )

    counts = defaultdict(int)
    for iss in sorted_issues:
        counts[iss.severity] += 1

    # When scoped to one worker, drop issues belonging to other workers.
    # For team-level issues (worker="(team)"), use client_shift_days (already
    # scoped to this worker's activities) to decide which clients belong to them.
    if worker_id_filter:
        worker_name_filter = uname(worker_id_filter)
        # client_shift_days keys are Connecteam job titles (e.g. "Kallan Jordan").
        # Match by first name so "Kallan Jordan" in shifts covers "Kallan Jordan" in issues.
        worker_job_titles = {t.lower() for t in client_shift_days}

        def _client_is_workers(client_name):
            if not client_name:
                return True  # no client = team-wide, always include
            first = client_name.split()[0].lower()
            return any(first in title for title in worker_job_titles)

        sorted_issues = [
            i for i in sorted_issues
            if i.worker == worker_name_filter
            or (i.worker in {"(team)", "unknown", ""} and _client_is_workers(i.client))
        ]

    print("\n" + "=" * 72)
    print(f"  NDIS COMPLIANCE AUDIT  -  {start_dt.strftime('%d %b')} -> {end_dt.strftime('%d %b %Y')}")
    print("=" * 72)
    print(f"  CRITICAL: {counts['CRITICAL']}  |  HIGH: {counts['HIGH']}  |  MEDIUM: {counts['MEDIUM']}  |  LOW: {counts['LOW']}")
    print("=" * 72)

    current_cat = None
    current_sev = None
    for iss in sorted_issues:
        if iss.category != current_cat or iss.severity != current_sev:
            print(f"\n[{iss.severity}] {iss.category}")
            print("-" * 60)
            current_cat = iss.category
            current_sev = iss.severity
        print(f"  {iss.worker:<28} | {iss.client:<22} | {iss.date}")
        print(f"    -> {iss.detail}")

    print(f"\n\n  Total issues: {len(sorted_issues)}")
    print("=" * 72 + "\n")
    return sorted_issues


# ---------------------------------------------
# ENTRY POINT
# ---------------------------------------------

if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    run_audit(days)
