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
import time
import random
import threading
import datetime
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

AEST = ZoneInfo("Australia/Melbourne")

# Paths
NOTIFICATIONS_FILE = os.environ.get("NOTIFICATIONS_FILE", "notifications_log.json")
WEBHOOK_SECRET     = os.environ.get("WEBHOOK_SECRET", "")
PORT               = int(os.environ.get("PORT", "8080"))
MANAGER_NUMBER     = os.environ.get("MANAGER_NUMBER", "+61431836771")
OBSERVER_IDS       = {2149475, 9736871, 2201497}  # Yusuf, Nada, Faduma — keep in sync with audit.py

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
AMY_SENDER_IDS    = {SENDER_ID} if SENDER_ID else set()  # derived so it stays in sync with env
ALL_SYSTEM_IDS    = OBSERVER_IDS | AMY_SENDER_IDS
MANAGER_USER_ID   = 2149475  # Yusuf
CC_MGMT_CONV_ID   = os.environ.get("CC_MGMT_CONV_ID", "4a14c09d-bc9f-46f2-9ad9-a728d6ddcbf6")

# GitHub API sync — allows webhook to persist acknowledgements back to the repo
# so the dashboard and GitHub Actions always see up-to-date notification status.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "Yusufsai-bit/connect-care-audit")
LOG_PATH     = "notifications_log.json"

PENDING_RELAY_FILE  = os.path.join(os.path.dirname(__file__) or ".", "pending_relay_queue.json")
SHIFT_NOTIFIED_FILE = os.path.join(os.path.dirname(__file__) or ".", "shift_notified.json")

QUIET_START = 19  # 7 PM AEST — no worker messages after this hour
QUIET_END   = 6   # 6 AM AEST — no worker messages before this hour


def _is_quiet_hours():
    hour = datetime.datetime.now(AEST).hour
    return hour >= QUIET_START or hour < QUIET_END


def _worker_send(user_id, msg):
    """Send a chat message to a worker, but only between 6 AM and 7 PM AEST."""
    if _is_quiet_hours():
        ts = datetime.datetime.now(AEST).strftime("%I:%M %p")
        print(f"  [quiet hours {ts}] skipping message to user {user_id}")
        return False
    return send_connecteam_chat(user_id, msg)


def load_pending_relay():
    try:
        with open(PENDING_RELAY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_pending_relay(queue):
    try:
        with open(PENDING_RELAY_FILE, "w", encoding="utf-8") as f:
            json.dump(queue, f, indent=2)
    except Exception as e:
        print(f"[WARN] Could not save relay queue: {e}")


def load_shift_notified():
    """Load persisted shift-notification keys; prune entries older than 24h."""
    try:
        with open(SHIFT_NOTIFIED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        cutoff = time.time() - 86400
        return {k: v for k, v in data.items() if v >= cutoff}
    except Exception:
        return {}


def save_shift_notified(mapping):
    try:
        with open(SHIFT_NOTIFIED_FILE, "w", encoding="utf-8") as f:
            json.dump(mapping, f, indent=2)
    except Exception as e:
        print(f"[WARN] Could not save shift-notified log: {e}")


# Queue of complex worker replies waiting for a manager response from CC Management.
# Each entry: {"worker_id": ..., "worker_name": ..., "reply": ..., "issues": [...]}
# Loaded from disk on startup so it survives Railway restarts.
PENDING_RELAY_QUEUE = load_pending_relay()

# Message-ID dedup ring buffer — prevents processing the same event twice when
# Connecteam retries on timeout (entries expire after 1 hour).
_SEEN_IDS: dict = {}
_SEEN_LOCK = threading.Lock()

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


def get_conversation_history(user_id, n=8):
    """Return last n turns as list of {role, text, ts} dicts."""
    memory = load_amy_memory()
    hist = memory.get(str(user_id), [])
    if isinstance(hist, str):
        hist = [{"role": "amy", "text": hist, "ts": ""}] if hist else []
    return hist[-n:]


def append_to_conversation(user_id, role, text):
    """Append a turn to this worker's conversation history (cap at 20 entries)."""
    memory = load_amy_memory()
    hist = memory.get(str(user_id), [])
    if isinstance(hist, str):
        hist = [{"role": "amy", "text": hist, "ts": ""}] if hist else []
    hist.append({"role": role, "text": text[:500], "ts": datetime.datetime.now(AEST).strftime("%d %b %H:%M")})
    memory[str(user_id)] = hist[-20:]
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
    """Mark all Sent notifications for this worker as Acknowledged."""
    notifs  = load_notifications()
    changed = False
    for n in notifs:
        if str(n.get("worker_id")) == str(user_id) and n.get("status") == "Sent":
            n["status"]          = "Acknowledged"
            n["acknowledged_at"] = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")
            changed = True
    if changed:
        save_notifications(notifs)
    return changed


def mark_resolved(user_id):
    """Mark all Sent/Acknowledged notifications for this worker as Resolved (issue confirmed fixed)."""
    notifs  = load_notifications()
    changed = False
    now_str = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")
    for n in notifs:
        if str(n.get("worker_id")) == str(user_id) and n.get("status") in ("Sent", "Acknowledged"):
            n["status"]       = "Resolved"
            n["resolved_at"]  = now_str
            if not n.get("acknowledged_at"):
                n["acknowledged_at"] = now_str
            changed = True
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
        if not r.ok:
            print(f"[WARN] ct_get {path} returned {r.status_code}: {r.text[:200]}")
            return {}
        return r.json()
    except Exception as e:
        print(f"[WARN] ct_get {path} failed: {e}")
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
            json={"senderId": SENDER_ID, "text": text[:4000]},
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
        print("[WARN] CONNECTEAM_SENDER_ID not set — message not sent to worker")
        return False

    # Try group conversation first
    conv_map = load_worker_conversations()
    conv_id  = conv_map.get(str(user_id))
    if conv_id:
        try:
            r = requests.post(
                f"{BASE_URL}/chat/v1/conversations/{conv_id}/message",
                headers={"X-API-KEY": CT_KEY, "Content-Type": "application/json"},
                json={"senderId": sender_id, "text": text[:4000]},
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
            json={"senderId": sender_id, "text": text[:4000]},
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
    first  = worker.split()[0] if worker else "there"

    flags = [
        f"system auto clocked you out at {client} — you may not have clocked out manually",
        "check your times are right and add notes if you haven't yet",
    ]
    msg = _generate_shift_end_msg(first, client, flags)

    sent = _worker_send(user_id, msg)
    if sent:
        append_to_conversation(user_id, "amy", msg)
    elif not _is_quiet_hours():
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

    msg = _generate_shift_end_msg(first, client_name, worker_flags)

    sent = _worker_send(user_id, msg)
    if sent:
        append_to_conversation(user_id, "amy", msg)
        # Burn shift-end dedup key so the scheduled check doesn't double-message (C7)
        today = datetime.datetime.now(AEST).strftime("%Y-%m-%d")
        notified = load_shift_notified()
        notified[f"{user_id}_clockout_{today}"] = time.time()
        save_shift_notified(notified)
    elif not _is_quiet_hours():
        phone = get_worker_phone(user_id)
        if phone:
            send_msg(phone, msg)

    print(f"  Clock-out check: {worker_name} ({client_name}) — flags: {worker_flags}")


def load_worker_conversations():
    """Load worker_id → conversation_id mapping from worker_conversations.json."""
    path = os.path.join(os.path.dirname(__file__) or ".", "worker_conversations.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print("[WARN] worker_conversations.json not found — all messages will fall back to private chat")
        return {}
    except Exception as e:
        print(f"[WARN] Could not load worker_conversations.json: {e}")
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
    If the worker claims to have done something, check Connecteam to see if it's true.
    Returns (plain-English verification string, is_resolved: bool).
    """
    text_lower = text.lower()
    claim_keywords = ["submitted", "done", "updated", "fixed", "added", "sent", "completed",
                      "uploaded", "filled", "put in", "just did", "sorted", "logged",
                      "clocked", "clock out", "clocked out", "clocking out"]
    if not any(w in text_lower for w in claim_keywords):
        return "", False

    results    = []
    all_verified = True

    today     = datetime.date.today().isoformat()
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    data      = ct_get(f"/time-clock/v1/time-clocks/{TIME_CLOCK_ID}/time-activities",
                       {"startDate": yesterday, "endDate": today})
    by_user   = (data.get("data") or {}).get("timeActivitiesByUsers") or []
    activity  = next((e for e in by_user if str(e.get("userId")) == str(user_id)), None)
    shifts    = (activity.get("shifts") or []) if activity else []

    # Check notes
    if any(w in text_lower for w in ["note", "notes", "shift note", "progress note"]):
        found_note = False
        for shift in shifts:
            atts = shift.get("shiftAttachments") or []
            note = get_note_text(atts)
            if note and len(note.split()) >= 10:
                found_note = True
                break
        results.append("shift notes: VERIFIED ✓" if found_note else "shift notes: NOT FOUND — can't see them yet")
        if not found_note:
            all_verified = False

    # Check clock-out
    if any(w in text_lower for w in ["clocked out", "clock out", "clocking out", "clocked off"]):
        clocked_out = any(
            (s.get("end") or {}).get("timestamp") for s in shifts
        )
        results.append("clock-out: VERIFIED ✓" if clocked_out else "clock-out: NOT FOUND in time clock")
        if not clocked_out:
            all_verified = False

    verified_str = ", ".join(results)
    resolved = bool(results) and all_verified
    return verified_str, resolved


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
        import re as _re
        raw = response.content[0].text.strip()
        if "```" in raw:
            raw = _re.sub(r"```(?:json)?\n?", "", raw).strip()
        return raw
    except Exception as e:
        print(f"  [WARN] Claude call failed: {e}")
        return None


def generate_amy_reply(worker_name, text, issues, verification="", history=None):
    """
    Classify worker message and generate Amy's reply.
    history: list of {role: "amy"|"worker", text, ts} dicts (last 8 turns)
    Returns (is_complex: bool, reply_text: str).
    """
    import re as _re
    first = worker_name.split()[0]

    if not ANTHROPIC_API_KEY:
        return False, f"Hey {first}, got your message — I'll be in touch."

    issues_summary = "\n".join(
        f"- [{i.get('Severity','?')}] {i.get('Issue','?')}: {i.get('Detail','')[:100]}"
        for i in (issues or [])[:5]
    ) or "No open compliance issues."

    history_lines = ""
    if history:
        for turn in history[-6:]:
            label = "Amy" if turn["role"] == "amy" else first
            history_lines += f"{label}: {turn['text']}\n"

    verif_line = f"\nVerification result: {verification}" if verification else ""

    prompt = f"""You are Amy, a support coordinator at Connect Care in Melbourne. You're texting {first} on a work chat app about their NDIS shifts.

Their open compliance issues:
{issues_summary}

{f"Recent conversation:{chr(10)}{history_lines}" if history_lines else ""}
{first} just said: "{text}"{verif_line}

Decide: SIMPLE (you can handle it — they explained, sorted, or it's minor) or COMPLEX (needs a manager, they're disputing something, needs investigation).

Write Amy's reply. Non-negotiable rules:
- Sound like a real person texting, not a compliance system. Casual, warm, direct.
- NEVER use these words/phrases: noted, acknowledged, please note, please be advised, I need you to, ensure that, outstanding issues, at your earliest convenience, flagged, I have logged, this matter, please ensure, going forward, action this, your attention, I will escalate
- NO bullet points, NO numbered lists
- If this isn't the first message (check history), don't open with "Hi {first}" — vary your opener
- Max 2 sentences. Get to the point.
- If notes verified present: "yeah can see them now, all good" style
- If notes not found yet: "can't see them yet — did they save properly?" style
- If COMPLEX: "leave it with me, I'll sort it out" NOT "I will escalate this to management"
- If worker is asking something unrelated to compliance (shift swaps, leave etc): classify COMPLEX

JSON only — no other text:
{{"is_complex": true/false, "reply": "..."}}"""

    raw = _call_claude(prompt, max_tokens=500)
    if not raw:
        return False, f"Hey {first}, got it — I'll be in touch."
    try:
        clean = raw.strip()
        if "```" in clean:
            clean = _re.sub(r"```(?:json)?", "", clean).strip()
        result = json.loads(clean)
        reply = result.get("reply") or ""
        if not reply:
            return False, f"Hey {first}, got it."
        return result.get("is_complex", False), reply
    except Exception:
        m = _re.search(r'"reply"\s*:\s*"([^"]+)"', raw)
        if m:
            return False, m.group(1)
        return False, f"Hey {first}, got it."


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
        print(f"  [chat] skipped — userId={user_id!r} text={text[:40]!r} keys={list(data.keys())}")
        return

    uid = int(user_id)

    # Ignore Amy's own messages and observer messages outside CC Management
    if uid in AMY_SENDER_IDS:
        return

    # ── Manager guidance in CC Management → compose and send to worker ────────
    if conv_id == CC_MGMT_CONV_ID and uid in OBSERVER_IDS:
        if PENDING_RELAY_QUEUE:
            # Try to match which worker the manager is replying about by name mention
            text_lower = text.lower()
            matched_idx = 0  # default to oldest
            for i, r in enumerate(PENDING_RELAY_QUEUE):
                first_name = r["worker_name"].split()[0].lower()
                if first_name in text_lower:
                    matched_idx = i
                    break
            relay = PENDING_RELAY_QUEUE.pop(matched_idx)
            save_pending_relay(PENDING_RELAY_QUEUE)
            wid      = relay["worker_id"]
            wname    = relay["worker_name"]
            composed = compose_from_guidance(wname, text, relay["reply"], relay.get("issues", []))
            _worker_send(wid, composed)
            append_to_conversation(wid, "amy", composed)
            print(f"  Amy sent composed response to {wname} based on manager guidance")
            if PENDING_RELAY_QUEUE:
                _post_to_cc_mgmt(PENDING_RELAY_QUEUE[0])
        return

    # ── Ignore other observer messages ────────────────────────────────────────
    if uid in OBSERVER_IDS:
        return

    worker = get_worker_name(user_id) if CT_KEY else str(user_id)
    print(f"  Message from {worker}: '{text[:80]}'")

    # ── Verify any claims the worker is making ─────────────────────────────────
    verification, is_resolved = verify_worker_claims(user_id, text)
    if verification:
        print(f"  Verification: {verification}")

    # ── Generate Amy's reply ───────────────────────────────────────────────────
    issues   = get_worker_issues(user_id)
    history  = get_conversation_history(user_id)
    append_to_conversation(user_id, "worker", text)
    is_complex, amy_reply = generate_amy_reply(worker, text, issues, verification, history)

    # H12: Mark Resolved when Connecteam confirms the issue is actually fixed.
    # Otherwise Acknowledge if they replied (but don't mark resolved yet).
    if is_resolved:
        mark_resolved(user_id)
        print(f"  Marked RESOLVED for {worker} — verified by Connecteam API")
    elif not is_complex or verification:
        mark_acknowledged(user_id)

    if amy_reply and SENDER_ID and CT_KEY:
        _worker_send(user_id, amy_reply)
        append_to_conversation(user_id, "amy", amy_reply)
        print(f"  Amy replied ({'holding' if is_complex else 'closed'}): '{amy_reply[:80]}'")

    # ── Complex → ask CC Management what to say ───────────────────────────────
    if is_complex:
        relay = {"worker_id": user_id, "worker_name": worker, "reply": text, "issues": issues}
        PENDING_RELAY_QUEUE.append(relay)
        save_pending_relay(PENDING_RELAY_QUEUE)
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
    ok = post_to_conversation(CC_MGMT_CONV_ID, msg)
    if not ok:
        print(f"[ERROR] Failed to post relay to CC Management (conv {CC_MGMT_CONV_ID}) — "
              f"manager won't see {worker}'s message. Check CC_MGMT_CONV_ID env var.")


def handle_form_submitted(data):
    """Log form submissions for audit trail."""
    form_id   = data.get("formId")
    user_id   = data.get("submittingUserId") or data.get("userId")
    worker    = get_worker_name(user_id) if (user_id and CT_KEY) else str(user_id)
    print(f"  Form {form_id} submitted by {worker}")


# ── Shift-end compliance checker (background thread) ─────────────────────────

SCHEDULER_ID    = 1775479


def _shift_check_loop():
    """Poll every 10 min for shifts that ended 20-55 min ago and haven't been checked."""
    time.sleep(15)  # brief startup delay
    while True:
        try:
            _check_recently_ended_shifts()
        except Exception as e:
            print(f"[shift-check] error: {e}")
        time.sleep(600)


def _deadline_check_loop():
    """Fire once per day at 5 PM AEST — SMS manager about workers still unresolved (H3)."""
    last_fired = None
    while True:
        time.sleep(60)
        now = datetime.datetime.now(AEST)
        today = now.strftime("%Y-%m-%d")
        if now.hour == 17 and now.minute < 5 and today != last_fired:
            last_fired = today
            try:
                _run_5pm_deadline_check()
            except Exception as e:
                print(f"[5PM] error: {e}")


def _run_5pm_deadline_check():
    """Alert manager about workers who haven't responded by 5 PM deadline."""
    today   = datetime.datetime.now(AEST).strftime("%Y-%m-%d")
    notifs  = load_notifications()
    pending = [
        n for n in notifs
        if n.get("status") in ("Sent", "Escalated")
        and (n.get("audit_date") == today or n.get("sent_at_iso", "").startswith(today))
        and not n.get("dry_run")
    ]
    if not pending:
        print("[5PM] All workers responded — no action needed")
        return
    names = sorted({n.get("worker", "") for n in pending})
    msg = (
        f"5 PM deadline: {len(names)} worker(s) still haven't responded — "
        f"{', '.join(names)}. Consider calling them directly."
    )
    send_msg(MANAGER_NUMBER, msg)
    print(f"[5PM] Manager alerted about: {', '.join(names)}")


def _check_recently_ended_shifts():
    if not CT_KEY:
        return
    now_ts = time.time()
    aest_now = datetime.datetime.now(AEST)
    today_start = int(aest_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    today_end   = int(aest_now.replace(hour=23, minute=59, second=59, microsecond=0).timestamp())

    r = requests.get(
        f"{BASE_URL}/scheduler/v1/schedulers/{SCHEDULER_ID}/shifts",
        headers={"X-API-KEY": CT_KEY},
        params={"startTime": today_start, "endTime": today_end, "limit": 50},
        timeout=15,
    )
    if not r.ok:
        return

    shifts = (r.json().get("data") or {}).get("shifts") or []

    for shift in shifts:
        end_ts   = shift.get("endTime", 0)
        shift_id = shift.get("id", "")
        age      = now_ts - end_ts

        # Only act on shifts that ended between 20 and 55 minutes ago
        if not (20 * 60 <= age <= 55 * 60):
            continue

        assigned = shift.get("assignedUserIds") or []
        job_id   = shift.get("jobId")

        notified = load_shift_notified()
        for uid in assigned:
            if uid in OBSERVER_IDS:
                continue
            key = f"{uid}_{shift_id}"
            if key in notified:
                continue
            try:
                sent = _run_shift_end_check(uid, job_id, end_ts, shift)
                # Only burn the dedup key if the check actually ran (sent or all-clear).
                # If the API was down and nothing happened, leave key unburned so we retry.
                if sent is not None:
                    notified[key] = time.time()
                    save_shift_notified(notified)
            except Exception as e:
                print(f"[shift-check] check failed for user {uid}: {e}")


def _run_shift_end_check(user_id, job_id, sched_end_ts, shift):
    """
    Check a single worker's shift compliance after it should have ended.
    Returns True if the check completed (message sent or all-clear), None if the
    Connecteam API was unreachable (so the caller can skip burning the dedup key).
    """
    worker_name = get_worker_name(user_id)
    client_name = get_job_name(job_id) if job_id else "your client"
    first       = worker_name.split()[0]
    end_dt      = datetime.datetime.fromtimestamp(sched_end_ts, tz=AEST)
    today       = end_dt.strftime("%Y-%m-%d")

    data    = ct_get(f"/time-clock/v1/time-clocks/{TIME_CLOCK_ID}/time-activities",
                     {"startDate": today, "endDate": today})
    by_user = (data.get("data") or {}).get("timeActivitiesByUsers") or []

    # If we got an empty dict back (API error), don't burn the dedup key
    if not data:
        print(f"[shift-check] API error fetching time-activities — will retry next poll")
        return None

    activity = None
    for entry in by_user:
        if str(entry.get("userId")) == str(user_id):
            all_shifts = entry.get("shifts") or []
            # Match the shift closest to the scheduled start (sched_end - shift_duration)
            if all_shifts:
                activity = min(all_shifts,
                               key=lambda s: abs((s.get("start") or {}).get("timestamp", 0)
                                                 - (sched_end_ts - 6 * 3600)))
            break

    flags = []

    if not activity:
        flags.append(f"no clock-in found for your {end_dt.strftime('%I:%M %p').lstrip('0')} shift at {client_name}")
    else:
        clock_out = (activity.get("end") or {}).get("timestamp")
        if not clock_out:
            flags.append(f"still clocked in at {client_name} — shift was scheduled to finish at {end_dt.strftime('%I:%M %p').lstrip('0')}")

        atts  = activity.get("shiftAttachments") or []
        note  = get_note_text(atts)
        if not note or len(note.split()) < 10:
            flags.append("shift notes haven't come through yet")

    if not flags:
        print(f"[shift-check] {worker_name} ({client_name}) all good")
        return True

    # C7: skip if clock-out handler already messaged this worker today
    today = datetime.datetime.now(AEST).strftime("%Y-%m-%d")
    already_notified = load_shift_notified()
    if f"{user_id}_clockout_{today}" in already_notified:
        print(f"[shift-check] skipping {worker_name} — already messaged at clock-out")
        return True

    msg = _generate_shift_end_msg(first, client_name, flags)
    sent = _worker_send(user_id, msg)
    if sent:
        append_to_conversation(user_id, "amy", msg)
    print(f"[shift-check] notified {worker_name}: {flags}")
    return True


def _generate_shift_end_msg(first_name, client_name, flags):
    """Ask Claude to write a natural follow-up — falls back to a simple template."""
    issues_desc = " and ".join(flags)
    if not ANTHROPIC_API_KEY:
        return f"Hey {first_name}, just checking on your shift at {client_name} — {issues_desc}. Can you sort that when you get a chance?"

    prompt = f"""You are Amy, a support coordinator at Connect Care. Text {first_name} about their shift at {client_name}.

Issue(s): {issues_desc}

Write a casual, natural follow-up — like a real person texting a colleague. 2 sentences max.
Rules: no bullet points, no "please note", no "outstanding", no "ensure", no "I need you to".
Don't open with "Hi" every time. Vary your opener. Sound human.
Just the message, nothing else."""

    result = _call_claude(prompt)
    return result or f"Hey {first_name}, just a quick one — {issues_desc} for {client_name}. Can you jump on that?"


# ── Event log ring buffer (last 30 events for /debug endpoint) ────────────────

_EVENT_LOG: list = []
_EVENT_LOG_LOCK = threading.Lock()

def _log_event(event, data, raw_body=None):
    """Store a snapshot in the ring buffer; cap at 30 entries."""
    entry = {
        "ts":    datetime.datetime.now(AEST).strftime("%d %b %Y %I:%M:%S %p"),
        "event": event,
        "keys":  list(data.keys()) if isinstance(data, dict) else [],
        "data":  {k: str(v)[:200] for k, v in (data.items() if isinstance(data, dict) else {})},
    }
    with _EVENT_LOG_LOCK:
        _EVENT_LOG.insert(0, entry)
        del _EVENT_LOG[30:]


def _process_event(payload):
    event = payload.get("eventType", "")
    data  = payload.get("data", {})

    # Dedup — Connecteam retries on timeout; don't process the same event twice
    msg_id = str(data.get("messageId") or data.get("id") or "")
    if msg_id:
        with _SEEN_LOCK:
            if msg_id in _SEEN_IDS:
                return
            _SEEN_IDS[msg_id] = time.time()
            cutoff = time.time() - 3600
            for k in list(_SEEN_IDS):
                if _SEEN_IDS[k] < cutoff:
                    del _SEEN_IDS[k]

    _log_event(event, data)
    print(f"Event: {event}  keys={list(data.keys()) if isinstance(data, dict) else '?'}")

    el = event.lower()
    if event in ("timeActivityClockIn", "Time activity clock in") or el == "clock_in":
        handle_clock_in(data)
    elif event in ("timeActivityClockOut", "Time activity clock out") or el == "clock_out":
        handle_clock_out(data)
    elif event in ("timeActivityAutoClockOut", "Time activity auto clock out") or el == "auto_clock_out":
        handle_auto_clock_out(data)
    elif event in ("timeActivityAdminEdit", "Time activity admin edit", "timeActivityAdminAdd", "Time activity admin add") or el in ("admin_edit", "admin_add", "admin_delete"):
        handle_admin_time_edit(data)
    elif event in ("chatMessageCreated", "Chat message created") or el == "chat_message_created":
        handle_chat_reply(data)
    elif event in ("formSubmission", "Form Submission") or el == "form_submission":
        handle_form_submitted(data)
    elif "shift" in el:
        handle_shift_change(event, data)
    elif "user" in el and el not in ("chatmessagecreated", "chat_message_created"):
        handle_user_change(event, data)


# ── HTTP Server ───────────────────────────────────────────────────────────────

class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[{self.address_string()}] {format % args}")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/debug":
            body = json.dumps(_EVENT_LOG, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/status":
            status = {
                "sender_id":         SENDER_ID,
                "sender_id_live":    int(os.environ.get("CONNECTEAM_SENDER_ID", "0") or "0"),
                "amy_ids":           list(AMY_SENDER_IDS),
                "ct_key_set":        bool(CT_KEY),
                "ct_key_live":       bool(os.environ.get("CONNECTEAM_API_KEY")),
                "ai_key_set":        bool(ANTHROPIC_API_KEY),
                "ai_key_live":       bool(os.environ.get("ANTHROPIC_API_KEY")),
                "relay_queue":       len(PENDING_RELAY_QUEUE),
                "uptime":            datetime.datetime.now(AEST).isoformat(),
            }
            body = json.dumps(status, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Connect Care webhook receiver - running.")

    def do_POST(self):
        length  = int(self.headers.get("Content-Length", 0))
        body    = self.rfile.read(length)

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

        # Respond immediately — Claude calls can take 5-15s and Connecteam retries on timeout
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

        threading.Thread(target=_process_event, args=(payload,), daemon=True).start()


if __name__ == "__main__":
    print(f"Starting Connect Care webhook receiver on port {PORT}...")
    threading.Thread(target=_shift_check_loop, daemon=True).start()
    print("Shift-end compliance checker started (polls every 10 min).")
    threading.Thread(target=_deadline_check_loop, daemon=True).start()
    print("5 PM deadline checker started.")
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    server.serve_forever()
