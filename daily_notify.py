#!/usr/bin/env python3
"""
Connect Care — Daily Automated Compliance Notifier
Runs automatically via GitHub Actions every morning at 7 AM AEST.
Audits yesterday's shifts, sends WhatsApp/SMS to every worker with
CRITICAL or HIGH issues, and appends results to notifications_log.json.

Run manually:
    python daily_notify.py
"""

import os, sys, json, datetime, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from connecteam_audit import (
    run_audit, fetch_all_users,
    send_worker_message, send_sms,
    CONNECTEAM_SENDER_ID, TWILIO_FROM_NUMBER,
    AEST,
)

NOTIFICATIONS_FILE = os.path.join(os.path.dirname(__file__), "notifications_log.json")
MANAGER_NUMBER     = os.environ.get("MANAGER_NUMBER", "")   # e.g. +61481140097
NOTIFY_SEVERITIES  = {"CRITICAL", "HIGH"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_log():
    try:
        with open(NOTIFICATIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_log(entries):
    with open(NOTIFICATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, default=str, indent=2)


def build_message(worker_name, issues, period_label):
    first = worker_name.split()[0]
    SEV_ICON = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}
    lines = [
        f"Hi {first},",
        "",
        f"From your shift {period_label} — I need you to sort these out by 5 PM today:",
        "",
    ]
    for i, iss in enumerate(issues[:8]):
        icon   = SEV_ICON.get(iss["severity"], "•")
        detail = iss["detail"][:87] + "…" if len(iss["detail"]) > 90 else iss["detail"]
        lines.append(f"{icon} {iss['client']} — {detail}")
    if len(issues) > 8:
        lines.append(f"(+ {len(issues) - 8} more)")
    lines += [
        "",
        "Reply and let me know what happened and what you've done to fix it — need to hear back by 5 PM.",
        "",
        "Cheers",
    ]
    return "\n".join(lines)


def send(worker_id, text):
    """Send via Connecteam Chat. Returns (True, result) or (False, error)."""
    return send_worker_message(worker_id, text)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now       = datetime.datetime.now(AEST)
    yesterday = now - datetime.timedelta(days=1)
    period    = f"yesterday ({yesterday.strftime('%a %d %b')})"

    print(f"\n{'='*60}")
    print(f"Connect Care Daily Notifier — {now.strftime('%d %b %Y %I:%M %p AEST')}")
    print(f"Auditing: {yesterday.strftime('%d %b %Y')}")
    print(f"{'='*60}\n")

    # Run audit for yesterday only
    issues = run_audit(days_back=1)
    if not issues:
        print("No issues found for yesterday — nothing to send.")
        return

    # Group by worker
    by_worker = {}
    for iss in issues:
        if iss.severity not in NOTIFY_SEVERITIES:
            continue
        w = iss.worker
        if w not in by_worker:
            by_worker[w] = []
        by_worker[w].append({
            "severity": iss.severity,
            "category": iss.category,
            "client":   iss.client or "",
            "date":     iss.date or "",
            "detail":   iss.detail or "",
        })

    if not by_worker:
        print("No CRITICAL/HIGH issues found — nothing to send.")
        return

    print(f"Workers to notify: {len(by_worker)}")

    # Load contacts
    users     = fetch_all_users()
    contacts  = {
        f"{u.get('firstName','')} {u.get('lastName','')}".strip(): {
            "userId": u.get("userId"),
            "phone":  u.get("phoneNumber") or u.get("phone") or "",
        }
        for u in users.values()
    }

    dry_run = not bool(CONNECTEAM_SENDER_ID)
    if dry_run:
        print("⚠️  DRY RUN — CONNECTEAM_SENDER_ID not set (Communications Hub upgrade pending).")
        print("   Messages will be logged but NOT sent. Add CONNECTEAM_SENDER_ID to activate.\n")
    else:
        print("Channel: Connecteam Chat\n")

    log      = load_log()
    sent_ok  = []
    sent_err = []

    for wname, issues_list in by_worker.items():
        winfo  = contacts.get(wname, {})
        wid    = winfo.get("userId")
        wphone = winfo.get("phone", "").strip()

        msg = build_message(wname, issues_list, period)
        sev_counts = {}
        for i in issues_list:
            sev_counts[i["severity"]] = sev_counts.get(i["severity"], 0) + 1

        if dry_run:
            print(f"  ~ Would notify {wname} ({len(issues_list)} issues)")
            status = "Pending"
            sent_ok.append(wname)
        else:
            ok, result = send(wid, msg)
            if ok:
                print(f"  ✓ Sent to {wname}")
                status = "Sent"
                sent_ok.append(wname)
            else:
                print(f"  ✗ Failed {wname}: {result}")
                sent_err.append(wname)
                continue

        log.insert(0, {
            "id":              str(uuid.uuid4())[:8],
            "worker":          wname,
            "worker_id":       wid,
            "sent_at":         now.strftime("%d %b %Y, %I:%M %p"),
            "sent_at_iso":     now.isoformat(),
            "period":          period,
            "severity_counts": sev_counts,
            "issue_count":     len(issues_list),
            "issues": [{
                "Severity": i["severity"],
                "Issue":    i["category"],
                "Client":   i["client"],
                "Date":     i["date"],
                "Detail":   i["detail"],
            } for i in issues_list],
            "message_sent":       msg,
            "channel":            "connecteam" if not dry_run else "pending",
            "status":             status,
            "acknowledged_at":    None,
            "resolved_at":        None,
            "manager_notes":      "",
            "automated":          True,
            "dry_run":            dry_run,
        })

    save_log(log)

    print(f"\n{'='*60}")
    print(f"Done — {len(sent_ok)} sent, {len(sent_err)} failed")
    if sent_ok:  print(f"  Sent:   {', '.join(sent_ok)}")
    if sent_err: print(f"  Failed: {', '.join(sent_err)}")
    print(f"{'='*60}\n")

    # Notify manager via SMS if MANAGER_NUMBER is set
    if MANAGER_NUMBER and sent_ok:
        summary = (
            f"Connect Care daily audit complete.\n"
            f"{len(sent_ok)} workers notified, {len(sent_err)} failed.\n"
            f"Workers: {', '.join(sent_ok)}"
        )
        send_sms(MANAGER_NUMBER, summary)
        print(f"Manager summary sent to {MANAGER_NUMBER}")


if __name__ == "__main__":
    main()
