"""
Connecteam Webhook Receiver
Handles real-time events from Connecteam:
  - Clock out     → immediate notes reminder if no notes detected
  - Chat reply    → auto-marks notification as Acknowledged in dashboard
  - Form submit   → logs compliance form submission

Deploy to Railway or Render (free tier):
  1. Create new project → Deploy from GitHub → select connect-care-audit repo
  2. Set start command: python connecteam_webhook.py
  3. Copy the public URL (e.g. https://connect-care-webhook.up.railway.app)
  4. In dashboard → Setup Webhooks button → paste URL → registers automatically
     OR manually in Connecteam → Settings → Webhooks → Add Webhook for each event.
  5. Set env var WEBHOOK_SECRET in Railway + in Connecteam webhook config (same value).

Environment variables:
  WEBHOOK_SECRET      — shared secret for request verification (optional but recommended)
  CONNECTEAM_API_KEY  — for looking up worker names/details
  TWILIO_ACCOUNT_SID  — for sending SMS clock-out reminders
  TWILIO_AUTH_TOKEN
  TWILIO_NUMBER
  TWILIO_WHATSAPP_NUMBER
  CONNECTEAM_SENDER_ID
  MANAGER_NUMBER      — manager's mobile for critical alerts
  ANTHROPIC_API_KEY   — enables AI-powered chat replies (optional, falls back to template)
  PORT                — default 8080
"""

import os
import sys
import json
import hmac
import hashlib
import math
import datetime
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer

# Paths
NOTIFICATIONS_FILE = os.environ.get("NOTIFICATIONS_FILE", "notifications_log.json")
WEBHOOK_SECRET     = os.environ.get("WEBHOOK_SECRET", "")
PORT               = int(os.environ.get("PORT", "8080"))
MANAGER_NUMBER     = os.environ.get("MANAGER_NUMBER", "+61431836771")
OBSERVER_IDS       = {2149475, 9736871, 2201497}  # Yusuf, Nada, Faduma

# Time clock constants (mirrors connecteam_audit.py)
TIME_CLOCK_ID    = 1776332
NOTES_FIELD      = "65cbb88e-6c3a-41b1-8822-975caed50def"
GPS_THRESHOLD_KM = 0.5   # allowed radius from client address
SHORT_SHIFT_MIN  = 15    # shifts under this are suspicious

# GPS fallbacks for jobs with no coordinates in Connecteam
CLIENT_GPS_OVERRIDES = {
    "john": (-37.67282, 144.99437, 0.2),
}

if not WEBHOOK_SECRET:
    print("[WARNING] WEBHOOK_SECRET is not set — any caller can POST to this endpoint. Set it in Railway env vars.")

# Connecteam / Twilio credentials
CT_KEY            = os.environ.get("CONNECTEAM_API_KEY", "")
BASE_URL          = "https://api.connecteam.com"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SENDER_ID         = int(os.environ.get("CONNECTEAM_SENDER_ID", "0") or "0")
MANAGER_USER_ID   = 2149475  # Yusuf
CC_MGMT_CONV_ID   = os.environ.get("CC_MGMT_CONV_ID", "4a14c09d-bc9f-46f2-9ad9-a728d6ddcbf6")

# GitHub API sync — allows webhook to persist acknowledgements back to the repo
# so the dashboard and GitHub Actions always see up-to-date notification status.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "Yusufsai-bit/connect-care-audit")
LOG_PATH     = "notifications_log.json"

# Queue of complex worker replies waiting for a manager response from CC Management.
# Each entry: {"worker_id": ..., "worker_name": ..., "reply": ..., "issues": [...]}
PENDING_RELAY_QUEUE = []

AMY_MEMORY_FILE = os.path.join(os.path.dirname(__file__) or ".", "amy_conversation_log.json")


def load_amy_memory():
    try:
        with open(AMY_MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_amy_memory(memory):
    try:
        with open(AMY_MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(memory, f, indent=2)
    except Exception:
        pass


def get_last_amy_message(user_id):
    return load_amy_memory().get(str(user_id), "")


def set_last_amy_message(user_id, text):
    memory = load_amy_memory()
    memory[str(user_id)] = text
    save_amy_memory(memory)

# ── Notification log helpers ──────────────────────────────────────────────────

def load_notifications():
    """Load from GitHub repo if token available, else fall back to local file."""
    if GITHUB_TOKEN:
        try:
            import base64
            r = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/contents/{LOG_PATH}",
                headers={"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"},
                timeout=15,
            )
            if r.ok:
                return json.loads(base64.b64decode(r.json()["content"]).decode())
        except Exception as e:
            print(f"  [WARN] GitHub log fetch failed, using local: {e}")
    try:
        with open(NOTIFICATIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_notifications(notifs):
    """Write locally and, if GitHub token available, commit back to repo."""
    try:
        with open(NOTIFICATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(notifs, f, default=str, indent=2)
    except Exception as e:
        print(f"  [ERROR] Could not save notifications locally: {e}")

    if GITHUB_TOKEN:
        try:
            import base64
            headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
            r = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/contents/{LOG_PATH}",
                headers=headers, timeout=15,
            )
            sha     = r.json().get("sha", "") if r.ok else ""
            content = base64.b64encode(json.dumps(notifs, default=str, indent=2).encode()).decode()
            payload = {"message": "chore: update notification status [skip ci]", "content": content}
            if sha:
                payload["sha"] = sha
            requests.put(
                f"https://api.github.com/repos/{GITHUB_REPO}/contents/{LOG_PATH}",
                headers=headers, json=payload, timeout=15,
            )
        except Exception as e:
            print(f"  [WARN] GitHub log sync failed: {e}")


def mark_acknowledged(user_id):
    """Mark the most recent Sent notification for this worker as Acknowledged."""
    notifs  = load_notifications()
    changed = False
    for n in notifs:
        if str(n.get("worker_id")) == str(user_id) and n.get("status") == "Sent":
            n["status"]          = "Acknowledged"
            n["acknowledged_at"] = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")
            changed = True
            break
    if changed:
        save_notifications(notifs)
    return changed


# ── Connecteam helpers ────────────────────────────────────────────────────────

def ct_get(path, params=None):
    try:
        r = requests.get(
            f"{BASE_URL}{path}",
            headers={"X-API-KEY": CT_KEY, "Accept": "application/json"},
            params=params, timeout=15,
        )
        return r.json() if r.ok else {}
    except Exception:
        return {}


def get_worker_name(user_id):
    data = ct_get(f"/users/v1/users/{user_id}")
    u    = (data.get("data") or {}).get("user") or data
    return f"{u.get('firstName','')} {u.get('lastName','')}".strip() or f"Worker {user_id}"


def get_worker_phone(user_id):
    data = ct_get(f"/users/v1/users/{user_id}")
    u    = (data.get("data") or {}).get("user") or data
    return u.get("phoneNumber") or u.get("phone") or ""


def get_job_name(job_id):
    data = ct_get(f"/jobs/v1/jobs/{job_id}")
    j    = (data.get("data") or {}).get("job") or data
    return j.get("title") or j.get("name") or f"Client {job_id}"


def get_job_full(job_id):
    """Return full job record including GPS coordinates."""
    data = ct_get(f"/jobs/v1/jobs/{job_id}")
    return (data.get("data") or {}).get("job") or data


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_note_text(attachments):
    """Extract shift note text from shiftAttachments list."""
    for att in (attachments or []):
        if att.get("shiftAttachmentId") != NOTES_FIELD:
            continue
        for key in ("value", "text", "note", "content"):
            val = att.get(key)
            if val and isinstance(val, str) and val.strip():
                return val.strip()
        fields = att.get("fields") or att.get("values") or []
        if isinstance(fields, list):
            for f in fields:
                val = f.get("value") or f.get("text") or ""
                if val and isinstance(val, str) and val.strip():
                    return val.strip()
    return ""


def fetch_latest_activity(user_id):
    """
    Fetch today's time activities and return the most recently clocked-out
    entry for this user. Returns None if not found.
    """
    today = datetime.date.today().isoformat()
    data  = ct_get(
        f"/time-clock/v1/time-clocks/{TIME_CLOCK_ID}/time-activities",
        {"startDate": today, "endDate": today},
    )
    raw = (data.get("data") or {}).get("timeActivitiesByUsers") or []
    for entry in raw:
        if str(entry.get("userId")) == str(user_id):
            shifts = entry.get("shifts") or []
            # Most recent completed shift (has an end timestamp)
            completed = [s for s in shifts if (s.get("end") or {}).get("timestamp")]
            if completed:
                return max(completed, key=lambda s: s["end"]["timestamp"])
    return None


# ── Messaging ─────────────────────────────────────────────────────────────────

def send_msg(phone, text):
    """Send via Connecteam Chat if configured, else WhatsApp, else SMS."""
    sender_id = int(os.environ.get("CONNECTEAM_SENDER_ID", "0") or "0")
    wa_number = os.environ.get("TWILIO_WHATSAPP_NUMBER", "")
    sid       = os.environ.get("TWILIO_ACCOUNT_SID", "")
    tok       = os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_num  = os.environ.get("TWILIO_NUMBER", "")

    # Prefer Connecteam Chat (needs user_id not phone, handled by caller)
    if not sid or not tok:
        return False

    try:
        from twilio.rest import Client
        client = Client(sid, tok)
        if wa_number:
            client.messages.create(
                from_=f"whatsapp:{wa_number}", body=text[:1600], to=f"whatsapp:{phone}")
        else:
            client.messages.create(from_=from_num, body=text[:1600], to=phone)
        return True
    except Exception as e:
        print(f"  [ERROR] Message send failed: {e}")
        return False


def post_to_conversation(conv_id, text):
    """Post a message to a specific Connecteam group conversation."""
    if not SENDER_ID or not CT_KEY:
        return False
    try:
        r = requests.post(
            f"{BASE_URL}/chat/v1/conversations/{conv_id}/message",
            headers={"X-API-KEY": CT_KEY, "Content-Type": "application/json"},
            json={"senderId": SENDER_ID, "text": text[:1000]},
            timeout=15,
        )
        return r.ok
    except Exception:
        return False


def send_connecteam_chat(user_id, text):
    """
    Send a message to a worker. Prefers their group conversation (so the reply
    appears in the same thread they wrote in), falls back to private message.
    """
    sender_id = int(os.environ.get("CONNECTEAM_SENDER_ID", "0") or "0")
    if not sender_id:
        return False

    # Try group conversation first
    conv_map = load_worker_conversations()
    conv_id  = conv_map.get(str(user_id))
    if conv_id:
        try:
            r = requests.post(
                f"{BASE_URL}/chat/v1/conversations/{conv_id}/message",
                headers={"X-API-KEY": CT_KEY, "Content-Type": "application/json"},
                json={"senderId": sender_id, "text": text[:1000]},
                timeout=15,
            )
            if r.ok:
                return True
        except Exception:
            pass

    # Fall back to private message
    try:
        r = requests.post(
            f"{BASE_URL}/chat/v1/conversations/privateMessage/{user_id}",
            headers={"X-API-KEY": CT_KEY, "Content-Type": "application/json"},
            json={"senderId": sender_id, "text": text[:1000]},
            timeout=15,
        )
        return r.ok
    except Exception:
        return False


# ── Event handlers ────────────────────────────────────────────────────────────

def handle_clock_in(data):
    """Real-time GPS check on clock-in — worker still on-site, actionable immediately."""
    user_id = data.get("userId")
    job_id  = data.get("jobId")
    if not user_id:
        return
    worker_name = get_worker_name(user_id)
    client_name = get_job_name(job_id) if job_id else "unknown client"
    loc         = data.get("location") or data.get("locationData") or {}
    clock_lat   = loc.get("latitude", 0)
    clock_lon   = loc.get("longitude", 0)
    print(f"  Clock-in: {worker_name} → {client_name} (GPS: {clock_lat}, {clock_lon})")
    # Log to notifications for dashboard visibility — no message sent (no action needed unless GPS fails)


def handle_admin_time_edit(data):
    """
    FRAUD DETECTION: Admin manually edited a time entry.
    Alert manager immediately — retroactive edits are a primary billing fraud vector.
    """
    user_id     = data.get("userId")
    editor_id   = data.get("adminId") or data.get("editedBy")
    job_id      = data.get("jobId")
    if not user_id:
        return

    worker      = get_worker_name(user_id)
    editor      = get_worker_name(editor_id) if editor_id else "An admin"
    client      = get_job_name(job_id) if job_id else "unknown client"
    old_start   = data.get("previousStartTime") or data.get("oldStartTime") or ""
    new_start   = data.get("newStartTime") or data.get("startTime") or ""
    old_end     = data.get("previousEndTime") or data.get("oldEndTime") or ""
    new_end     = data.get("newEndTime") or data.get("endTime") or ""

    alert = (
        f"⚠️ TIME ENTRY EDITED — possible billing adjustment.\n\n"
        f"Worker: {worker}\n"
        f"Client: {client}\n"
        f"Edited by: {editor}\n"
    )
    if old_start or old_end:
        alert += f"Was: {old_start} – {old_end}\n"
    if new_start or new_end:
        alert += f"Now: {new_start} – {new_end}\n"
    alert += "\nVerify this change is authorised and reflects actual hours worked."

    print(f"  ADMIN TIME EDIT: {editor} edited {worker}'s entry for {client}")

    if MANAGER_NUMBER and CT_KEY:
        send_msg(MANAGER_NUMBER, alert)


def handle_auto_clock_out(data):
    """System forced a clock-out — more urgent than a manual missed clock-out."""
    user_id = data.get("userId")
    job_id  = data.get("jobId")
    if not user_id:
        return

    worker = get_worker_name(user_id)
    client = get_job_name(job_id) if job_id else "unknown client"
    first  = worker.split()[0] if worker else "Hi"

    msg = (
        f"Hi {first}, your shift at {client} was automatically clocked out by the system "
        f"because you didn't clock out manually. Please check your times are correct and "
        f"submit your shift notes if you haven't already."
    )
    sent = send_connecteam_chat(user_id, msg)
    if not sent:
        phone = get_worker_phone(user_id)
        if phone:
            send_msg(phone, msg)

    print(f"  Auto clock-out alert sent to {worker} ({client})")


def handle_shift_change(event_type, data):
    """Alert manager when shifts are updated or deleted — roster manipulation detection."""
    job_id  = data.get("jobId")
    client  = get_job_name(job_id) if job_id else "unknown client"
    verb    = "updated" if "update" in event_type.lower() else "deleted"
    msg     = f"\U0001f4c5 Roster change: shift for {client} was {verb}.\n\nVerify this change was authorised."
    print(f"  Shift {verb}: {client}")
    if MANAGER_NUMBER and CT_KEY:
        send_msg(MANAGER_NUMBER, msg)


def handle_user_change(event_type, data):
    """Alert manager on any HR change — user created, promoted, demoted, archived."""
    user_id = data.get("userId")
    name    = get_worker_name(user_id) if (user_id and CT_KEY) else str(user_id)
    verb    = event_type.replace("user", "").strip().lower()
    msg     = f"\U0001f464 HR change: {name} was {verb}. Verify this change was authorised."
    print(f"  User change ({verb}): {name}")
    if MANAGER_NUMBER and CT_KEY:
        send_msg(MANAGER_NUMBER, msg)


def handle_clock_out(data):
    """
    Fires when a worker clocks out. Performs real-time checks:
      - Notes submitted?
      - GPS at clock-in within allowed radius of client address?
      - Shift suspiciously short?
    Only messages the worker if something is actually wrong.
    GPS mismatches also alert the manager immediately.
    """
    user_id = data.get("userId")
    job_id  = data.get("jobId")
    if not user_id or int(user_id) in OBSERVER_IDS:
        return

    worker_name = get_worker_name(user_id)
    client_name = get_job_name(job_id) if job_id else "your client"
    first       = worker_name.split()[0] if worker_name else "there"

    activity = fetch_latest_activity(user_id)

    worker_flags = []

    if activity:
        clock_in  = (activity.get("start") or {}).get("timestamp", 0)
        clock_out = (activity.get("end")   or {}).get("timestamp", 0)

        # --- Duration check ---
        if clock_in and clock_out:
            duration_min = (clock_out - clock_in) / 60
            if duration_min < SHORT_SHIFT_MIN:
                worker_flags.append(
                    f"your shift was only {round(duration_min)} min "
                    f"— please check your times are correct"
                )

        # --- GPS check ---
        if job_id:
            job     = get_job_full(job_id)
            job_gps = job.get("gps") or {}
            job_lat = job_gps.get("latitude", 0)
            job_lon = job_gps.get("longitude", 0)
            if job_lat == 0 or job_lon == 0:
                title_lc = (job.get("title") or "").lower()
                for kw, (ov_lat, ov_lon, _r) in CLIENT_GPS_OVERRIDES.items():
                    if kw in title_lc:
                        job_lat, job_lon = ov_lat, ov_lon
                        break
            if job_lat != 0 and job_lon != 0:
                loc   = (activity.get("start") or {}).get("locationData") or {}
                c_lat = loc.get("latitude", 0)
                c_lon = loc.get("longitude", 0)
                if c_lat != 0 and c_lon != 0:
                    dist = haversine_km(job_lat, job_lon, c_lat, c_lon)
                    if dist > GPS_THRESHOLD_KM:
                        worker_flags.append(
                            f"your GPS at clock-in was {dist:.1f}km from "
                            f"{client_name}'s address — please confirm you were at the right location"
                        )

        # --- Notes check ---
        attachments   = activity.get("shiftAttachments") or []
        note_text     = get_note_text(attachments)
        notes_missing = not note_text or len(note_text.split()) < 10
    else:
        notes_missing = True  # couldn't fetch activity — assume notes pending

    if notes_missing:
        worker_flags.append(
            "your shift notes haven't been submitted yet — "
            "please complete them within 24 hours"
        )

    if not worker_flags:
        print(f"  Clock-out check passed for {worker_name} ({client_name}) — all good")
        return

    # Build worker message
    lines = [f"Hi {first}, you've just clocked out from {client_name}."]
    for flag in worker_flags:
        lines.append(f"- Please note: {flag}.")
    lines.append("Reply if you need anything.")
    msg = "\n\n".join(lines)

    sent = send_connecteam_chat(user_id, msg)
    if sent:
        set_last_amy_message(user_id, msg)
    else:
        phone = get_worker_phone(user_id)
        if phone:
            send_msg(phone, msg)

    print(f"  Clock-out check: {worker_name} ({client_name}) — flags: {worker_flags}")


def load_worker_conversations():
    """Load worker_id → conversation_id mapping from worker_conversations.json."""
    try:
        from connecteam_audit import load_worker_conversations as _load
        return _load()
    except Exception:
        path = os.path.join(os.path.dirname(__file__), "worker_conversations.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


def get_worker_issues(user_id):
    """Return the most recent unresolved issues for this worker from the notification log."""
    notifs = load_notifications()
    for n in notifs:
        if str(n.get("worker_id")) == str(user_id) and n.get("status") in ("Sent", "Acknowledged"):
            return n.get("issues", [])
    return []


def verify_worker_claims(user_id, text):
    """
    If the worker claims to have done something (submitted notes, clocked out, etc.),
    check Connecteam to see if it's actually done.
    Returns a plain-English verification string, or "" if nothing to verify.
    """
    text_lower = text.lower()
    claim_keywords = ["submitted", "done", "updated", "fixed", "added", "sent", "completed",
                      "uploaded", "filled", "put in", "just did", "sorted", "logged"]
    if not any(w in text_lower for w in claim_keywords):
        return ""

    results = []

    # Check notes
    if any(w in text_lower for w in ["note", "notes", "shift note", "progress note"]):
        try:
            today     = datetime.date.today().isoformat()
            yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
            data      = ct_get(f"/time-clock/v1/time-clocks/{TIME_CLOCK_ID}/time-activities",
                               {"startDate": yesterday, "endDate": today})
            by_user   = (data.get("data") or {}).get("timeActivitiesByUsers") or []
            found_note = False
            for entry in by_user:
                if str(entry.get("userId")) == str(user_id):
                    for shift in (entry.get("shifts") or []):
                        atts = shift.get("shiftAttachments") or []
                        note = get_note_text(atts)
                        if note and len(note.split()) >= 10:
                            found_note = True
                            break
            results.append("shift notes: VERIFIED ✓" if found_note else "shift notes: NOT FOUND — can't see them yet")
        except Exception:
            pass

    return ", ".join(results)


def _call_claude(prompt, max_tokens=300):
    """Call Claude Haiku and return the text response. Returns None on failure."""
    if not ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic
        client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return raw
    except Exception as e:
        print(f"  [WARN] Claude call failed: {e}")
        return None


def generate_amy_reply(worker_name, text, issues, verification="", previous_amy_msg=""):
    """
    Classify the worker's message and generate Amy's reply.
    Returns (is_complex: bool, reply_text: str).
    """
    first = worker_name.split()[0]

    if not ANTHROPIC_API_KEY:
        return False, f"Thanks {first}, noted — I'll be in touch if anything else is needed."

    issues_summary = "\n".join(
        f"- [{i.get('Severity','?')}] {i.get('Issue','?')}: {i.get('Detail','')[:100]}"
        for i in (issues or [])[:5]
    ) or "No open compliance issues."

    verification_line   = f"\nVerification check: {verification}" if verification else ""
    prior_line          = f"\nYour last message to them: \"{previous_amy_msg}\"" if previous_amy_msg else ""

    prompt = f"""You are Amy, a coordinator at Connect Care. You're messaging a support worker on a work chat app.

Worker: {worker_name}
Their compliance issues:
{issues_summary}{verification_line}{prior_line}

Their message:
"{text}"

Decide:
- SIMPLE: they've sorted it, explained it, or it doesn't need escalating
- COMPLEX: they're asking something, pushing back, or it needs a management call

If SIMPLE: reply naturally, continuing from where the conversation left off. Casual, warm, brief. Max 2 sentences. Don't say "noted", "acknowledged", "I've logged this", "confirmed" or anything corporate. If verification shows notes are there, say something like "all good, can see them now". If notes aren't there yet, say "can't see them just yet — can you double check they saved properly?"
If COMPLEX: something like "Thanks [first name], leave it with me and I'll come back to you" — natural, not robotic.

JSON only — no extra text:
{{"is_complex": true/false, "reply": "..."}}

Important: sound like a real person texting, not a corporate system. No buzzwords, no formality."""

    raw = _call_claude(prompt)
    if not raw:
        return False, f"Thanks {first}, noted — I'll follow up if anything else is needed."
    try:
        result = json.loads(raw)
        return result.get("is_complex", False), result.get("reply", "")
    except Exception:
        return False, f"Thanks {first}, noted."


def compose_from_guidance(worker_name, manager_guidance, original_reply, issues):
    """
    Manager gave direction in CC Management (e.g. 'tell them to resubmit the form').
    Amy composes a proper message to send the worker.
    """
    first = worker_name.split()[0]

    if not ANTHROPIC_API_KEY:
        return manager_guidance  # Fall back to sending guidance verbatim

    issues_summary = "\n".join(
        f"- [{i.get('Severity','?')}] {i.get('Issue','?')}: {i.get('Detail','')[:100]}"
        for i in (issues or [])[:3]
    ) or "General compliance matter."

    prompt = f"""You are Amy, a coordinator at Connect Care. Write a message to a support worker based on what a manager just told you to say.

Worker: {worker_name}
Worker said: "{original_reply}"
Issues: {issues_summary}
Manager's guidance: "{manager_guidance}"

Write Amy's reply to {first} based on what the manager said. Sound like a real person texting — casual, warm, direct. Use their first name. Don't mention the manager or that you were told what to say. 2-3 sentences max. Just the message, no extra text."""

    result = _call_claude(prompt)
    return result if result else manager_guidance


def handle_chat_reply(data):
    """
    Any worker message → Amy reads it, verifies any claims, and replies.
    Flow:
      1. Manager replied in CC Management with guidance → Amy composes a proper message
         from that guidance and sends it to the pending worker.
      2. Worker sent any message → verify claims, classify, reply:
         - Simple/verified → Amy closes it out.
         - Complex → Amy holds, asks CC Management "what should I tell [worker]?"
    """
    user_id = data.get("userId") or data.get("senderId")
    text    = (data.get("text") or "").strip()
    conv_id = str(data.get("conversationId") or data.get("channelId") or "")
    if not user_id or not text:
        return

    uid = int(user_id)

    # ── Manager guidance in CC Management → compose and send to worker ────────
    if conv_id == CC_MGMT_CONV_ID and uid in OBSERVER_IDS:
        if PENDING_RELAY_QUEUE:
            relay    = PENDING_RELAY_QUEUE.pop(0)
            wid      = relay["worker_id"]
            wname    = relay["worker_name"]
            composed = compose_from_guidance(wname, text, relay["reply"], relay.get("issues", []))
            send_connecteam_chat(wid, composed)
            print(f"  Amy sent composed response to {wname} based on manager guidance")
            if PENDING_RELAY_QUEUE:
                _post_to_cc_mgmt(PENDING_RELAY_QUEUE[0])
        return

    # ── Ignore other observer messages ────────────────────────────────────────
    if uid in OBSERVER_IDS:
        return

    worker = get_worker_name(user_id) if CT_KEY else str(user_id)
    mark_acknowledged(user_id)
    print(f"  Message from {worker}: '{text[:80]}'")

    # ── Verify any claims the worker is making ─────────────────────────────────
    verification = verify_worker_claims(user_id, text)
    if verification:
        print(f"  Verification: {verification}")

    # ── Generate Amy's reply ───────────────────────────────────────────────────
    issues          = get_worker_issues(user_id)
    prior_msg       = get_last_amy_message(user_id)
    is_complex, amy_reply = generate_amy_reply(worker, text, issues, verification, prior_msg)

    if amy_reply and SENDER_ID and CT_KEY:
        send_connecteam_chat(user_id, amy_reply)
        set_last_amy_message(user_id, amy_reply)
        print(f"  Amy replied ({'holding' if is_complex else 'resolved'}): '{amy_reply[:80]}'")

    # ── Complex → ask CC Management what to say ───────────────────────────────
    if is_complex:
        relay = {"worker_id": user_id, "worker_name": worker, "reply": text, "issues": issues}
        PENDING_RELAY_QUEUE.append(relay)
        if len(PENDING_RELAY_QUEUE) == 1:
            _post_to_cc_mgmt(relay)
        print(f"  Asked CC Management for guidance on {worker} ({len(PENDING_RELAY_QUEUE)} pending)")


def _post_to_cc_mgmt(relay):
    """Ask CC Management what Amy should say to a worker."""
    worker = relay["worker_name"]
    first  = worker.split()[0]
    issues_summary = "\n".join(
        f"• [{i.get('Severity','?')}] {i.get('Issue','?')} — {i.get('Detail','')[:80]}"
        for i in (relay.get("issues") or [])[:4]
    ) or "(no open issues)"

    msg = (
        f"{first} sent Amy a message that needs a management response:\n\n"
        f"\"{relay['reply']}\"\n\n"
        f"Their open issues:\n{issues_summary}\n\n"
        f"What should Amy say back to {first}? Reply here with your guidance and Amy will compose and send the message."
    )
    post_to_conversation(CC_MGMT_CONV_ID, msg)


def handle_form_submitted(data):
    """Log form submissions for audit trail."""
    form_id   = data.get("formId")
    user_id   = data.get("submittingUserId") or data.get("userId")
    worker    = get_worker_name(user_id) if (user_id and CT_KEY) else str(user_id)
    print(f"  Form {form_id} submitted by {worker}")


# ── HTTP Server ───────────────────────────────────────────────────────────────

class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[{self.address_string()}] {format % args}")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Connect Care webhook receiver - running.")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        if WEBHOOK_SECRET:
            sig      = self.headers.get("X-Connecteam-Signature", "")
            expected = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                self.send_response(401)
                self.end_headers()
                return

        try:
            payload = json.loads(body)
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        event = payload.get("eventType", "")
        data  = payload.get("data", {})
        print(f"Event: {event}")

        if event in ("timeActivityClockIn", "Time activity clock in"):
            handle_clock_in(data)
        elif event in ("timeActivityClockOut", "Time activity clock out"):
            handle_clock_out(data)
        elif event in ("timeActivityAutoClockOut", "Time activity auto clock out"):
            handle_auto_clock_out(data)
        elif event in ("timeActivityAdminEdit", "Time activity admin edit"):
            handle_admin_time_edit(data)
        elif event in ("timeActivityAdminAdd", "Time activity admin add"):
            handle_admin_time_edit(data)  # same handler — both are admin time changes
        elif event in ("chatMessageCreated", "Chat message created"):
            handle_chat_reply(data)
        elif event in ("formSubmission", "Form Submission"):
            handle_form_submitted(data)
        elif "shift" in event.lower():
            handle_shift_change(event, data)
        elif "user" in event.lower() and event.lower() not in ("chatmessagecreated",):
            handle_user_change(event, data)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")


if __name__ == "__main__":
    print(f"Starting Connect Care webhook receiver on port {PORT}...")
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    server.serve_forever()
