"""
Connecteam Webhook Receiver
Listens for "Chat message created" events from Connecteam.
When a worker replies to a compliance notification, marks it as Acknowledged
in notifications_log.json so the dashboard updates automatically.

Deploy to Railway or Render (free tier):
  1. Create new project → Deploy from GitHub → select connect-care-audit repo
  2. Set start command: python connecteam_webhook.py
  3. Copy the public URL (e.g. https://connect-care-webhook.up.railway.app)
  4. In Connecteam → Settings → Webhooks → Add Webhook:
       Event: Chat message created
       URL:   https://your-app.up.railway.app/webhook
  5. Set env var WEBHOOK_SECRET to a random string, add same to Connecteam webhook config.

Run locally: python connecteam_webhook.py
"""

import os
import json
import hmac
import hashlib
import datetime
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

NOTIFICATIONS_FILE = os.environ.get("NOTIFICATIONS_FILE", "notifications_log.json")
WEBHOOK_SECRET     = os.environ.get("WEBHOOK_SECRET", "")
PORT               = int(os.environ.get("PORT", "8080"))


def load_notifications():
    try:
        with open(NOTIFICATIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_notifications(notifs):
    with open(NOTIFICATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(notifs, f, default=str, indent=2)


def mark_acknowledged(user_id: int):
    """Find the most recent unacknowledged notification for this worker and mark it."""
    notifs = load_notifications()
    changed = False
    for n in notifs:
        if (
            str(n.get("worker_id")) == str(user_id)
            and n.get("status") == "Sent"
        ):
            n["status"] = "Acknowledged"
            n["acknowledged_at"] = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")
            changed = True
            break  # only update the most recent one
    if changed:
        save_notifications(notifs)
    return changed


class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[{self.address_string()}] {format % args}")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Connecteam webhook receiver is running.")

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Verify signature if secret is set
        if WEBHOOK_SECRET:
            sig_header = self.headers.get("X-Connecteam-Signature", "")
            expected = hmac.new(
                WEBHOOK_SECRET.encode(), body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig_header, expected):
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"Invalid signature")
                return

        try:
            payload = json.loads(body)
        except Exception:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid JSON")
            return

        event_type = payload.get("eventType", "")
        print(f"Received event: {event_type}")

        if event_type == "chatMessageCreated":
            data    = payload.get("data", {})
            user_id = data.get("userId") or data.get("senderId")
            text    = data.get("text", "")
            if user_id:
                updated = mark_acknowledged(user_id)
                print(f"  Worker {user_id} replied: '{text[:80]}' — acknowledged: {updated}")

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")


if __name__ == "__main__":
    print(f"Starting webhook receiver on port {PORT}...")
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    server.serve_forever()
