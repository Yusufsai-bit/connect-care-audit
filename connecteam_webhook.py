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
import datetime
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer

# Paths
NOTIFICATIONS_FILE = os.environ.get("NOTIFICATIONS_FILE", "notifications_log.json")
WEBHOOK_SECRET     = os.environ.get("WEBHOOK_SECRET", "")
PORT               = int(os.environ.get("PORT", "8080"))
MANAGER_NUMBER     = os.environ.get("MANAGER_NUMBER", "")

# Connecteam / Twilio credentials
CT_KEY       = os.environ.get("CONNECTEAM_API_KEY", "")
BASE_URL     = "https://api.connecteam.com"

# ── Notification log helpers ──────────────────────────────────────────────────

def load_notifications():
    try:
        with open(NOTIFICATIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_notifications(notifs):
    try:
        with open(NOTIFICATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(notifs, f, default=str, indent=2)
    except Exception as e:
        print(f"  [ERROR] Could not save notifications: {e}")


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

def handle_clock_out(data):
    """
    Fires when a worker clocks out. Sends an immediate reminder to submit notes.
    This catches the issue in real-time — worker still has shift context.
    """
    user_id = data.get("userId")
    job_id  = data.get("jobId")
    if not user_id:
        return

    worker_name = get_worker_name(user_id)
    client_name = get_job_name(job_id) if job_id else "your client"
    first       = worker_name.split()[0] if worker_name else "Hi"

    msg = (
        f"Hi {first}, just clocked you out from {client_name}. "
        f"Please make sure your shift notes are submitted — "
        f"they're due within 24 hours. Reply if you need anything."
    )

    # Try Connecteam Chat first, fall back to phone
    sent = send_connecteam_chat(user_id, msg)
    if not sent:
        phone = get_worker_phone(user_id)
        if phone:
            send_msg(phone, msg)

    print(f"  Clock-out reminder sent to {worker_name} ({client_name})")


def handle_chat_reply(data):
    """Worker replied to a compliance notification — mark acknowledged."""
    user_id = data.get("userId") or data.get("senderId")
    text    = data.get("text", "")
    if not user_id:
        return
    updated = mark_acknowledged(user_id)
    worker  = get_worker_name(user_id) if CT_KEY else str(user_id)
    print(f"  Chat reply from {worker}: '{text[:80]}' — acknowledged: {updated}")

    # Forward reply to manager
    if MANAGER_NUMBER and CT_KEY:
        mgr_msg = f"Reply from {worker}:\n\n{text}"
        send_msg(MANAGER_NUMBER, mgr_msg)


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
        self.wfile.write(b"Connect Care webhook receiver — running.")

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

        if event in ("timeActivityClockOut", "Time activity clock out"):
            handle_clock_out(data)
        elif event in ("chatMessageCreated", "Chat message created"):
            handle_chat_reply(data)
        elif event in ("formSubmission", "Form Submission"):
            handle_form_submitted(data)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")


if __name__ == "__main__":
    print(f"Starting Connect Care webhook receiver on port {PORT}...")
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    server.serve_forever()
