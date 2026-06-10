#!/usr/bin/env python3
"""
Connect Care — Noon Escalation Check
Runs automatically via GitHub Actions every day at noon AEST (2 AM UTC).
Finds workers who were notified this morning but haven't replied yet,
then posts a summary to CC Management so managers can decide whether to escalate.
Amy does NOT re-message workers directly — managers give the green light first.

Run manually:
    python escalate_notify.py
"""

import os, sys, json, datetime, requests
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from connecteam_audit import CONNECTEAM_SENDER_ID, CONNECTEAM_API_KEY, AEST

NOTIFICATIONS_FILE = os.path.join(os.path.dirname(__file__), "notifications_log.json")
CC_MGMT_CONV_ID    = os.environ.get("CC_MGMT_CONV_ID", "4a14c09d-bc9f-46f2-9ad9-a728d6ddcbf6")
BASE_URL           = "https://api.connecteam.com"


def load_log():
    try:
        with open(NOTIFICATIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_log(entries):
    with open(NOTIFICATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, default=str, indent=2)


def post_to_cc_management(text):
    """Post a message to CC Management group chat."""
    sender_id = int(CONNECTEAM_SENDER_ID or "0")
    if not sender_id or not CONNECTEAM_API_KEY:
        print(f"  [DRY RUN] Would post to CC Management: {text[:120]}")
        return False
    try:
        r = requests.post(
            f"{BASE_URL}/chat/v1/conversations/{CC_MGMT_CONV_ID}/message",
            headers={"X-API-KEY": CONNECTEAM_API_KEY, "Content-Type": "application/json"},
            json={"senderId": sender_id, "text": text[:4000]},
            timeout=15,
        )
        return r.ok
    except Exception as e:
        print(f"  [ERROR] CC Management post failed: {e}")
        return False


def main():
    now   = datetime.datetime.now(AEST)
    today = now.strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"Connect Care Noon Check — {now.strftime('%d %b %Y %I:%M %p AEST')}")
    print(f"{'='*60}\n")

    log     = load_log()
    pending = [
        e for e in log
        if e.get("status") == "Sent"
        and not e.get("acknowledged_at")
        and (e.get("audit_date") == today or e.get("sent_at_iso", "").startswith(today))
        and not e.get("dry_run")
    ]

    already_escalated = {
        e.get("worker") for e in log
        if e.get("status") == "Escalated"
        and e.get("sent_at_iso", "").startswith(today)
    }
    pending = [e for e in pending if e.get("worker") not in already_escalated]

    if already_escalated:
        print(f"Already escalated today (skipping): {', '.join(sorted(already_escalated))}")

    if not pending:
        print("Everyone has responded — nothing to escalate.")
        return

    print(f"No reply yet from {len(pending)} worker(s)")

    # Build a clear summary for the managers
    lines = []
    for e in pending:
        wname  = e.get("worker", "Unknown")
        count  = e.get("issue_count", 1)
        sevs   = e.get("severity_counts", {})
        sev_str = ", ".join(f"{v} {k}" for k, v in sevs.items()) if sevs else f"{count} issue(s)"
        lines.append(f"  • {wname} — {sev_str} — no reply since 7 AM")

    msg = (
        f"Noon check: {len(pending)} worker(s) haven't responded to this morning's compliance message yet:\n\n"
        + "\n".join(lines)
        + f"\n\nDeadline is 5 PM. Reply here with 'Amy, message [name]' if you'd like me to follow up with someone, "
        f"or I'll alert you again at 5 PM with anyone still unresolved."
    )

    ok = post_to_cc_management(msg)
    if ok:
        print(f"Posted noon summary to CC Management")
        # Mark as Escalated so we don't re-post at noon tomorrow
        for e in pending:
            e["status"]       = "Escalated"
            e["escalated_at"] = now.strftime("%d %b %Y, %I:%M %p")
        save_log(log)
    else:
        print(f"Failed to post to CC Management")

    print(f"\n{'='*60}")
    print(f"Done — {len(pending)} worker(s) in noon summary")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
