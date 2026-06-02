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

import os, sys, json, re, math
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from difflib import SequenceMatcher

# ---------------------------------------------
# CONFIG
# ---------------------------------------------

CONNECTEAM_API_KEY = os.environ.get("CONNECTEAM_API_KEY", "eef0a292-593e-4da8-aed1-204f3c7a8786")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

BASE_URL       = "https://api.connecteam.com"
TIME_CLOCK_ID  = 1776332
SCHEDULER_ID   = 1775479
AEST           = timezone(timedelta(hours=10))

# Audit thresholds
LATE_MIN              = 30     # minutes grace before flagging late
EARLY_MIN             = 30     # minutes grace before flagging early departure
MIN_NOTE_WORDS        = 50     # minimum words for a valid note
GPS_THRESHOLD_KM      = 3.0    # km radius from client address
COPY_PASTE_THRESHOLD  = 0.82   # similarity ratio to flag as copy-paste
NOTE_LATE_HOURS       = 24     # hours after clock-out before note is flagged as backdated
SHORT_SHIFT_MIN       = 15     # shifts under this many minutes are suspicious
MULTI_CLOCKIN_DAILY   = 3      # more than this many clock-ins per client per day is suspicious

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
                         "ambulance", "hospital", "police", "scratch", "bitten", "hit ",
                         "struck", "assault", "aggress", "emergency", "unconscious", "seizure"]
RESTRICTIVE_KEYWORDS = ["restrain", "physical intervention", "seclu", "locked in",
                         "blocked exit", "held down", "physically held"]
MEDICATION_KEYWORDS  = ["medication", "medicine", "tablet", "pill", " dose ", " mg",
                         "administer", "refused medic", "missed dose", "medic error"]


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
    r = requests.get(
        f"{BASE_URL}{path}",
        headers={"X-API-KEY": CONNECTEAM_API_KEY, "Accept": "application/json"},
        params=params,
        timeout=30,
    )
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
        if len(batch) < 10:
            break
        offset += len(batch)
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


def fetch_form_submissions(form_id):
    try:
        data = ct_get(f"/forms/v1/forms/{form_id}/form-submissions")
        return data["data"]["formSubmissions"]
    except Exception:
        return []


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

    payload = json.dumps([
        {
            "id":             n["id"],
            "worker":         n["worker"],
            "client":         n["client"],
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
- issues: [list of specific problems -- empty if none]
- severity: "PASS", "LOW", "MEDIUM", or "HIGH"

Return ONLY a valid JSON array. No explanation, no markdown."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        results = json.loads(response.content[0].text)
        return {r["id"]: r for r in results}
    except Exception as e:
        print(f"  [WARNING] Claude assessment failed: {e}")
        return {}


# ---------------------------------------------
# MAIN AUDIT
# ---------------------------------------------

def run_audit(days_back=7):
    now       = datetime.now(AEST)
    start_dt  = now - timedelta(days=days_back)
    start_date = start_dt.strftime("%Y-%m-%d")
    end_date   = now.strftime("%Y-%m-%d")
    start_ts   = int(start_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    end_ts     = int(now.replace(hour=23, minute=59, second=59, microsecond=0).timestamp())

    print(f"\nNDIS Compliance Audit -- {start_dt.strftime('%d %b')} to {now.strftime('%d %b %Y')}")
    print("Fetching data from Connecteam...")

    users              = fetch_all_users()
    jobs               = fetch_all_jobs()
    scheduled_shifts   = fetch_scheduled_shifts(start_ts, end_ts)
    activities_by_user = fetch_time_activities(start_date, end_date)

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
                issues.append(Issue("HIGH", "MISSING CLOCK-OUT", name, client, dlabel,
                    "Clocked in but never clocked out -- shift still open."))
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

            # Missing clock-out
            if not clock_out:
                issues.append(Issue("HIGH", "MISSING CLOCK-OUT", name, client, dlabel,
                    "Clocked in but never clocked out."))
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
                    "-- possible clock error or fraudulent entry."))

            # GPS check
            if job_id:
                job     = jobs.get(job_id, {})
                job_gps = job.get("gps", {})
                job_lat = job_gps.get("latitude", 0)
                job_lon = job_gps.get("longitude", 0)
                if job_lat != 0 and job_lon != 0:
                    loc = act["start"].get("locationData", {})
                    if isinstance(loc, dict):
                        c_lat = loc.get("latitude", 0)
                        c_lon = loc.get("longitude", 0)
                        if c_lat != 0 and c_lon != 0:
                            dist = haversine_km(job_lat, job_lon, c_lat, c_lon)
                            if dist > GPS_THRESHOLD_KM:
                                issues.append(Issue("HIGH", "GPS MISMATCH", name, client, dlabel,
                                    f"Clocked in {dist:.1f}km from client's address "
                                    f"({loc.get('address', 'unknown')})."))

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

            # Missing signature
            if attachments and not has_signature(attachments):
                issues.append(Issue("MEDIUM", "MISSING SIGNATURE", name, client, dlabel,
                    "Required participant/client signature not completed."))

            note_text = get_note_text(attachments)

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
                issues.append(Issue("MEDIUM", "MEDICATION MENTIONED -- VERIFY FORM FILED",
                    name, client, dlabel,
                    f"Note mentions: {', '.join(med_hits[:3])}. Confirm medication form was submitted."))

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
            print(f"Assessing {len(notes_for_claude)} notes with Claude AI...")
            batch_size    = 10
            claude_results = {}
            for i in range(0, len(notes_for_claude), batch_size):
                batch   = notes_for_claude[i:i + batch_size]
                results = assess_notes_with_claude(batch)
                claude_results.update(results)

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
        else:
            print("  [INFO] ANTHROPIC_API_KEY not set -- skipping AI note quality assessment.")

    # -- Copy-paste / duplicate notes detection --
    for uid, note_list in worker_notes.items():
        name = uname(uid)
        for i in range(len(note_list)):
            for j in range(i + 1, len(note_list)):
                d1, c1, t1 = note_list[i]
                d2, c2, t2 = note_list[j]
                if len(t1) > 40 and len(t2) > 40:
                    sim = similarity(t1, t2)
                    if sim >= COPY_PASTE_THRESHOLD:
                        # Avoid duplicate issue entries for same pair
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
    for form_name, form_id in INCIDENT_FORMS.items():
        submissions = fetch_form_submissions(form_id)

        for sub in submissions:
            sub_ts      = sub.get("submissionTimestamp", 0)
            if sub_ts < start_ts:
                continue  # outside our audit window

            sub_dt      = ts_aest(sub_ts)
            submitter   = uname(sub.get("submittingUserId")) if sub.get("submittingUserId") else "Unknown"
            dlabel      = sub_dt.strftime("%a %d-%b")
            entry_num   = sub.get("entryNum", "?")
            answers     = sub.get("answers", [])

            # Completeness -- any free-text/open-ended answer with content?
            has_description = any(
                a.get("questionType") in ("freeText", "openEnded")
                and str(a.get("value", a.get("freeText", ""))).strip()
                for a in answers
            )
            if not has_description:
                issues.append(Issue("CRITICAL", "INCOMPLETE INCIDENT REPORT",
                    submitter, form_name, dlabel,
                    f"Entry #{entry_num} -- submitted with no written description. "
                    "Non-compliant with NDIS incident documentation requirements."))

            # Timeliness -- gap between incident occurrence and submission
            incident_ts = next(
                (a["timestamp"] for a in answers
                 if a.get("questionType") == "datetime" and a.get("timestamp")),
                None
            )
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

    # ── PER-WEEK FORM FREQUENCY CHECKS ───────────────────────────────────────
    # For each client, count how many general incident reports were filed this week
    # by workers assigned to that client. Flag if below minimum.

    def week_incident_count_for_client(title_keyword):
        workers = workers_for_client(title_keyword)
        return sum(
            1 for s in fetch_form_submissions(FORMS["Incident Report"])
            if s.get("submissionTimestamp", 0) >= start_ts
            and s.get("submittingUserId") in workers
        )

    # Joshua -- 2x incident report, 2x ABC form per week
    joshua_workers = workers_for_client("josh")
    joshua_incidents = week_incident_count_for_client("josh")
    joshua_abc = count_subs_in_window(FORMS["Joshua: ABC Form"], joshua_workers)

    if joshua_incidents < 2:
        issues.append(Issue("HIGH", "FORM FREQUENCY -- JOSHUA",
            "(team)", "Joshua Gatt", end_date,
            f"Incident Report submitted {joshua_incidents}x this week -- minimum is 2."))
    if joshua_abc < 2:
        issues.append(Issue("HIGH", "FORM FREQUENCY -- JOSHUA",
            "(team)", "Joshua Gatt", end_date,
            f"Joshua ABC Form submitted {joshua_abc}x this week -- minimum is 2."))

    # Nada Haliem -- 2x incident report per week
    nada_incidents = week_incident_count_for_client("nada")
    if nada_incidents < 2:
        issues.append(Issue("HIGH", "FORM FREQUENCY -- NADA",
            "(team)", "Nada Haliem", end_date,
            f"Incident Report submitted {nada_incidents}x this week -- minimum is 2."))

    # John -- 2x incident report per week (covers John A and John Auzagelis)
    john_incidents = week_incident_count_for_client("john")
    if john_incidents < 2:
        issues.append(Issue("HIGH", "FORM FREQUENCY -- JOHN",
            "(team)", "John", end_date,
            f"Incident Report submitted {john_incidents}x this week -- minimum is 2."))

    # Nicole -- 1x incident report per week
    nicole_incidents = week_incident_count_for_client("nicole")
    if nicole_incidents < 1:
        issues.append(Issue("HIGH", "FORM FREQUENCY -- NICOLE",
            "(team)", "Nicole Loveless", end_date,
            f"Incident Report submitted {nicole_incidents}x this week -- minimum is 1."))

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

    print("\n" + "=" * 72)
    print(f"  NDIS COMPLIANCE AUDIT  -  {start_dt.strftime('%d %b')} -> {now.strftime('%d %b %Y')}")
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
