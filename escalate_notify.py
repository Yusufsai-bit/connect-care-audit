#!/usr/bin/env python3
"""
Connect Care — 3 PM Escalation Notifier
Runs automatically via GitHub Actions every day at 3 PM AEST (5 AM UTC).
Finds workers who were notified this morning but haven't replied yet,
and sends them a follow-up nudge before the 5 PM deadline.

Run manually:
    python escalate_notify.py
"""

import os, sys, json, datetime
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from connecteam_audit import send_worker_message, CONNECTEAM_SENDER_ID, AEST

NOTIFICATIONS_FILE = os.path.join(os.path.dirname(__file__), "notifications_log.json")
MANAGER_NUMBER     = os.environ.get("MANAGER_NUMBER", "+61431836771")


def load_log():
    try:
        with open(NOTIFICATIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_log(entries):
    with open(NOTIFICATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, default=str, indent=2)


def build_escalation_message(worker_name, issue_count):
    first = worker_name.split()[0]
    from connecteam_audit import ANTHROPIC_API_KEY
    if ANTHROPIC_API_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            prompt = f"""You are Amy, a coordinator at Connect Care. You messaged a support worker called {first} earlier today about {issue_count} compliance issue(s) from their shift, but they haven't replied yet. It's now 3 PM and the deadline to respond is 5 PM.

Write a brief, natural follow-up nudge. Sound like a real person — casual but clear that it's time-sensitive. Don't be rude or threatening. 2-3 sentences max. No emojis. Just the message."""
            resp = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=120,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception:
            pass
    # Fallback
    return f"Hey {first}, just following up — haven't heard back from you yet about yesterday's shift issues. Can you get back to me before 5 PM today?"


def main():
    now      = datetime.datetime.now(AEST)
    today    = now.strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"Connect Care 3 PM Escalation — {now.strftime('%d %b %Y %I:%M %p AEST')}")
    print(f"{'='*60}\n")

    log     = load_log()
    pending = [
        e for e in log
        if e.get("status") == "Sent"
        and (e.get("audit_date") == today or e.get("sent_at_iso", "").startswith(today))
        and not e.get("dry_run")
    ]

    # Skip workers already escalated today
    already_escalated = {e.get("worker") for e in log if e.get("status") == "Escalated"
                         and e.get("sent_at_iso", "").startswith(today)}
    pending = [e for e in pending if e.get("worker") not in already_escalated]
    if already_escalated:
        print(f"Already escalated today (skipping): {', '.join(sorted(already_escalated))}")

    if not pending:
        print("No unacknowledged notifications from today — nothing to escalate.")
        return

    print(f"Unacknowledged workers: {len(pending)}")

    dry_run = not bool(CONNECTEAM_SENDER_ID)
    if dry_run:
        print("DRY RUN — CONNECTEAM_SENDER_ID not set.\n")

    escalated = []
    for entry in pending:
        wname = entry.get("worker", "")
        wid   = entry.get("worker_id")
        count = entry.get("issue_count", 1)

        msg = build_escalation_message(wname, count)

        if dry_run:
            print(f"  ~ Would escalate {wname} ({count} issue(s))")
            entry["status"] = "Escalated"
            escalated.append(wname)
        else:
            ok, result = send_worker_message(wid, msg, worker_name=wname)
            if ok:
                print(f"  Escalated {wname} ({count} issue(s))")
                entry["status"]       = "Escalated"
                entry["escalated_at"] = now.strftime("%d %b %Y, %I:%M %p")
                escalated.append(wname)
            else:
                print(f"  Failed {wname}: {result}")

    save_log(log)

    print(f"\n{'='*60}")
    print(f"Done — {len(escalated)} escalated")
    if escalated:
        print(f"  {', '.join(escalated)}")
    print(f"{'='*60}\n")

    # Manager SMS if anyone was escalated
    if MANAGER_NUMBER and escalated and not dry_run:
        from connecteam_audit import send_sms
        send_sms(
            MANAGER_NUMBER,
            f"3 PM escalation sent to {len(escalated)} worker(s) with no reply yet: {', '.join(escalated)}"
        )


if __name__ == "__main__":
    main()
