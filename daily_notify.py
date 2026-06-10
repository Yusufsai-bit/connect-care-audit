#!/usr/bin/env python3
"""
Connect Care — Daily Automated Compliance Notifier
Runs automatically via GitHub Actions every morning at 7 AM AEST.
Audits yesterday's shifts, sends Connecteam Chat messages to every worker with
CRITICAL or HIGH issues (plus MEDIUM credential expiry warnings), and:
  - Appends results to notifications_log.json

Run manually:
    python daily_notify.py
"""

import os, sys, json, datetime, uuid, requests
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from connecteam_audit import (
    run_audit, fetch_all_users,
    lock_worker_days, add_worker_note, push_daily_info_note,
    send_worker_message,
    CONNECTEAM_SENDER_ID, CONNECTEAM_API_KEY,
    AEST,
)

NOTIFICATIONS_FILE = os.path.join(os.path.dirname(__file__), "notifications_log.json")
CC_MGMT_CONV_ID    = os.environ.get("CC_MGMT_CONV_ID", "4a14c09d-bc9f-46f2-9ad9-a728d6ddcbf6")
BASE_URL           = "https://api.connecteam.com"


def post_to_cc_management(text):
    """Post a message to CC Management group chat (replaces SMS to manager)."""
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
NOTIFY_SEVERITIES  = {"CRITICAL", "HIGH"}
CRED_CATEGORIES    = {"EXPIRED CREDENTIAL", "CREDENTIAL EXPIRING SOON"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_log():
    try:
        with open(NOTIFICATIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_log(entries):
    # Trim to last 30 days to keep file size manageable
    cutoff = (datetime.datetime.now(AEST) - datetime.timedelta(days=30)).isoformat()
    entries = [e for e in entries if e.get("sent_at_iso", "9999") >= cutoff]
    with open(NOTIFICATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, default=str, indent=2)


def build_message(worker_name, issues, period_label):
    """Generate a natural compliance message via Claude, falling back to plain text."""
    from connecteam_audit import ANTHROPIC_API_KEY
    first = worker_name.split()[0]

    issue_lines = []
    for iss in issues[:8]:
        issue_lines.append(f"- [{iss['severity']}] {iss['client']}: {iss['detail']}")
    if len(issues) > 8:
        issue_lines.append(f"(plus {len(issues) - 8} more issues)")
    issues_block = "\n".join(issue_lines)

    if ANTHROPIC_API_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            prompt = f"""You are Amy, a coordinator at Connect Care. Write a message to a support worker called {first} about compliance issues from their shift {period_label}.

Issues:
{issues_block}

Write it as a natural, conversational text message — like a real person would send, not a system. Keep it brief and direct. Mention the specific issues clearly but don't make it sound robotic or corporate. No bullet points with icons. No "I need you to sort these out by 5 PM" phrasing. Ask them to reply and let you know what happened. 4-6 sentences max. Just the message text."""
            resp = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception:
            pass

    # Plain text fallback
    lines = [f"Hi {first},\n"]
    lines.append(f"Just following up on your shift {period_label} — there are a few things that need sorting:\n")
    for iss in issues[:8]:
        lines.append(f"- {iss['client']}: {iss['detail']}")
    if len(issues) > 8:
        lines.append(f"(plus {len(issues) - 8} more)")
    lines.append(f"\nCan you let me know what happened and what you've done to fix it?\n\nCheers")
    return "\n".join(lines)


def build_credential_message(worker_name, cred_issues):
    first = worker_name.split()[0]
    SEV_ICON = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡"}
    lines = [
        f"Hi {first},",
        "",
        "Your NDIS worker credentials need attention:",
        "",
    ]
    for iss in cred_issues:
        icon = SEV_ICON.get(iss["severity"], "🟡")
        lines.append(f"{icon} {iss['detail']}")
    lines += [
        "",
        "Please action these as soon as possible and let me know once renewed.",
        "",
        "Cheers",
    ]
    return "\n".join(lines)


def send(worker_id, text, worker_name=None):
    return send_worker_message(worker_id, text, worker_name=worker_name)


# ── Post-notification helpers ─────────────────────────────────────────────────

def _lock_prior_days(contacts, now):
    """Lock time entries for days 3+ days ago to prevent backdating."""
    lock_date = (now - datetime.timedelta(days=3)).strftime("%Y-%m-%d")
    locked = 0
    for wname, winfo in contacts.items():
        wid = winfo.get("userId")
        if not wid:
            continue
        ok, _ = lock_worker_days(wid, [lock_date])
        if ok:
            locked += 1
    if locked:
        print(f"Locked time entries for {locked} workers on {lock_date}.")


def _push_daily_info(issues, now, sent_count, failed_count):
    """Push compliance summary to Connecteam Daily Info section."""
    from collections import Counter
    sev_counts = Counter(i.severity for i in issues)
    lines = [
        f"\U0001f4cb NDIS Compliance Audit — {now.strftime('%d %b %Y')}",
        "",
        f"\U0001f534 CRITICAL: {sev_counts.get('CRITICAL', 0)}  "
        f"\U0001f7e0 HIGH: {sev_counts.get('HIGH', 0)}  "
        f"\U0001f7e1 MEDIUM: {sev_counts.get('MEDIUM', 0)}  "
        f"\U0001f7e2 LOW: {sev_counts.get('LOW', 0)}",
        "",
        f"Workers notified: {sent_count}  |  Failed: {failed_count}",
    ]
    if sev_counts.get("CRITICAL", 0) > 0:
        crit = [i for i in issues if i.severity == "CRITICAL"][:3]
        lines.append("")
        lines.append("Critical items:")
        for c in crit:
            lines.append(f"  • {c.worker} — {c.category} ({c.client})")
    text = "\n".join(lines)
    ok, result = push_daily_info_note(text, now.strftime("%Y-%m-%d"))
    if ok:
        print("Daily Info note pushed to Connecteam.")
    else:
        print(f"Daily Info push skipped: {result}")


def _add_critical_profile_notes(issues, contacts, now):
    """Write a permanent note to the worker's Connecteam profile for CRITICAL breaches."""
    # Group CRITICAL issues by worker
    by_worker = {}
    for iss in issues:
        if iss.severity == "CRITICAL":
            by_worker.setdefault(iss.worker, []).append(iss)

    noted = 0
    for wname, crit_issues in by_worker.items():
        wid = (contacts.get(wname) or {}).get("userId")
        if not wid:
            continue
        lines = [f"[{now.strftime('%d %b %Y')} — Automated Audit]"]
        for iss in crit_issues[:5]:
            lines.append(f"CRITICAL — {iss.category}: {iss.client} on {iss.date}. {iss.detail}")
        note_text = "\n".join(lines)
        ok, _ = add_worker_note(wid, note_text)
        if ok:
            noted += 1
    if noted:
        print(f"Critical breach notes added to {noted} worker profiles.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now           = datetime.datetime.now(AEST)
    yesterday     = now - datetime.timedelta(days=1)
    audit_date    = yesterday.strftime("%Y-%m-%d")   # used for dedup — never changes format
    period        = f"yesterday ({yesterday.strftime('%a %d %b')})"  # human label for messages

    print(f"\n{'='*60}")
    print(f"Connect Care Daily Notifier — {now.strftime('%d %b %Y %I:%M %p AEST')}")
    print(f"Auditing: {yesterday.strftime('%d %b %Y')}")
    print(f"{'='*60}\n")

    issues = run_audit(days_back=1)

    # Load contacts upfront — needed for both notifications and score writing
    users    = fetch_all_users()
    contacts = {
        f"{u.get('firstName','')} {u.get('lastName','')}".strip(): {
            "userId": u.get("userId"),
            "phone":  u.get("phoneNumber") or u.get("phone") or "",
        }
        for u in users.values()
    }

    # ── Credential issues only — workers get shift compliance msgs at shift-end ──
    # Shift compliance (missing notes, GPS, clock-in/out) is handled in real-time
    # by the Railway shift-end check 30 min after each shift finishes.
    # The morning job only sends credential expiry/renewal reminders (which have
    # no shift-end trigger) and posts the full audit summary to CC Management.

    cred_notify = {}   # workers with credential issues to message directly
    all_flagged = {}   # all workers with any notifiable issue (for CC Management summary)

    # M10: skip credential categories already notified within 7 days
    log            = load_log()
    seven_days_ago = (now - datetime.timedelta(days=7)).isoformat()
    recent_cred_notifs: dict = {}
    for e in log:
        if e.get("sent_at_iso", "") < seven_days_ago:
            continue
        if e.get("status") in ("Sent", "Acknowledged", "Escalated", "Resolved"):
            wn = e.get("worker", "")
            for iss in e.get("issues", []):
                cat = iss.get("Issue", "")
                if cat in CRED_CATEGORIES:
                    recent_cred_notifs.setdefault(wn, set()).add(cat)

    for iss in issues:
        notifiable = (
            iss.severity in NOTIFY_SEVERITIES
            or (iss.severity == "MEDIUM" and iss.category in CRED_CATEGORIES)
        )
        if not notifiable:
            continue
        issue_dict = {
            "severity": iss.severity, "category": iss.category,
            "client": iss.client or "", "date": iss.date or "", "detail": iss.detail or "",
        }
        all_flagged.setdefault(iss.worker, []).append(issue_dict)
        if iss.category in CRED_CATEGORIES:
            already_sent = recent_cred_notifs.get(iss.worker, set())
            if iss.category not in already_sent:
                cred_notify.setdefault(iss.worker, []).append(issue_dict)
            else:
                print(f"  M10: {iss.worker} — skipping '{iss.category}' (notified in last 7 days)")

    print(f"Workers with any issue: {len(all_flagged)}")
    print(f"Workers with credential issues to notify: {len(cred_notify)}")

    dry_run  = not bool(CONNECTEAM_SENDER_ID)
    sent_ok  = []
    sent_err = []

    already_notified = {
        e["worker"]
        for e in log
        if e.get("audit_date") == audit_date and e.get("status") in ("Sent", "Pending")
        and any(i.get("Issue") in CRED_CATEGORIES for i in e.get("issues", []))
    }
    if already_notified:
        print(f"Already sent credential notice today (skipping): {', '.join(sorted(already_notified))}")

    for wname, cred_issues in cred_notify.items():
        if wname in already_notified:
            continue
        wid = (contacts.get(wname) or {}).get("userId")
        msg = build_credential_message(wname, cred_issues)
        sev_counts = {}
        for i in cred_issues:
            sev_counts[i["severity"]] = sev_counts.get(i["severity"], 0) + 1

        if dry_run:
            print(f"  ~ Would send credential notice to {wname}")
            status = "Pending"
            sent_ok.append(wname)
        else:
            ok, result = send(wid, msg, worker_name=wname)
            if ok:
                print(f"  ✓ Credential notice sent to {wname}")
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
            "audit_date":      audit_date,
            "period":          period,
            "severity_counts": sev_counts,
            "issue_count":     len(cred_issues),
            "issues":          [{"Severity": i["severity"], "Issue": i["category"],
                                 "Client": i["client"], "Date": i["date"],
                                 "Detail": i["detail"]} for i in cred_issues],
            "message_sent":    msg,
            "channel":         "connecteam" if not dry_run else "pending",
            "status":          status,
            "acknowledged_at": None,
            "resolved_at":     None,
            "manager_notes":   "",
            "automated":       True,
            "dry_run":         dry_run,
        })

    save_log(log)

    print(f"\n{'='*60}")
    print(f"Credential notices — {len(sent_ok)} sent, {len(sent_err)} failed")
    print(f"{'='*60}\n")

    # ── Post full audit summary to CC Management ──────────────────────────────
    # Shift compliance issues are handled in real-time by Railway (30 min after shift end).
    # This gives managers a morning overview of everything flagged.
    if all_flagged:
        crit_workers = [w for w, iss in all_flagged.items()
                        if any(i["severity"] == "CRITICAL" for i in iss)]
        lines = [f"Morning audit — {yesterday.strftime('%a %d %b')}:\n"]
        if crit_workers:
            lines.append(f"CRITICAL: {', '.join(crit_workers)}")
        lines.append(f"{len(all_flagged)} worker(s) with issues in total.")
        if sent_ok:
            lines.append(f"Credential notices sent to: {', '.join(sent_ok)}")
        lines.append("Shift compliance messages will fire 30 min after each shift ends today.")
        post_to_cc_management("\n".join(lines))
    else:
        post_to_cc_management(f"Morning audit — {yesterday.strftime('%a %d %b')}: all clear, no issues found.")

    # ── Lock time entries for days > 2 days ago ───────────────────────────
    _lock_prior_days(contacts, now)

    # ── Push compliance summary to Connecteam Daily Info ─────────────────
    _push_daily_info(issues, now, len(sent_ok), len(sent_err))

    # ── Add profile notes for CRITICAL breaches ───────────────────────────
    _add_critical_profile_notes(issues, contacts, now)


if __name__ == "__main__":
    main()
