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
CT_KEY       = os.environ.get("CONNECTEAM_API_KEY", "")
BASE_URL     = "https://api.connecteam.com"

# GitHub API sync — allows webhook to persist acknowledgements back to the repo
# so the dashboard and GitHub Actions always see up-to-date notification status.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "Yusufsai-bit/connect-care-audit")
LOG_PATH     = "notifications_log.json"

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


def send_connecteam_chat(user_id, text):
    sender_id = int(os.environ.get("CONNECTEAM_SENDER_ID", "0") or "0")
    if not sender_id:
        return False
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
    if not sent:
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


def handle_chat_reply(data):
    """
    Worker replied in Connecteam Chat.
    - Marks the notification as Acknowledged in the log.
    - If per-worker group conversations are mapped, observers already see the reply
      in the shared thread — no forwarding needed.
    - Falls back to private forwarding to each observer when no group mapping exists.
    """
    user_id = data.get("userId") or data.get("senderId")
    text    = data.get("text", "")
    if not user_id:
        return

    if int(user_id) in OBSERVER_IDS:
        return

    updated = mark_acknowledged(user_id)
    worker  = get_worker_name(user_id) if CT_KEY else str(user_id)
    print(f"  Chat reply from {worker}: '{text[:80]}' — acknowledged: {updated}")

    # If this worker has a group conversation, observers see the reply there already
    conv_map = load_worker_conversations()
    if str(user_id) in conv_map:
        return

    # Fallback: forward to each observer individually via private message
    sender_id = int(os.environ.get("CONNECTEAM_SENDER_ID", "0") or "0")
    if sender_id and CT_KEY:
        fwd = f"{worker} replied:\n\n{text}"
        for oid in OBSERVER_IDS:
            if str(oid) != str(user_id):
                send_connecteam_chat(oid, fwd)


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
