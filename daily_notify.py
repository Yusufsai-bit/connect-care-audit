#!/usr/bin/env python3
"""
Connect Care — Daily Automated Compliance Notifier
Runs automatically via GitHub Actions every morning at 7 AM AEST.
Audits yesterday's shifts, sends Connecteam Chat messages to every worker with
CRITICAL or HIGH issues (plus MEDIUM credential expiry warnings), and:
  - Appends results to notifications_log.json
  - Writes per-worker compliance scores to Connecteam profiles (if configured)

Run manually:
    python daily_notify.py
"""

import os, sys, json, datetime, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from connecteam_audit import (
    run_audit, fetch_all_users, write_compliance_score,
    send_worker_message, send_sms,
    CONNECTEAM_SENDER_ID, TWILIO_FROM_NUMBER,
    AEST,
)

NOTIFICATIONS_FILE = os.path.join(os.path.dirname(__file__), "notifications_log.json")
MANAGER_NUMBER     = os.environ.get("MANAGER_NUMBER", "")
NOTIFY_SEVERITIES  = {"CRITICAL", "HIGH"}
CRED_CATEGORIES    = {"EXPIRED CREDENTIAL", "CREDENTIAL EXPIRING SOON"}

# Compliance score deductions per severity issue
SCORE_DEDUCTIONS = {"CRITICAL": 15, "HIGH": 8, "MEDIUM": 3, "LOW": 1}


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
    for iss in issues[:8]:
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


def send(worker_id, text):
    return send_worker_message(worker_id, text)


def calc_compliance_score(issues_list):
    """Score 0-100; deductions per severity, floored at 0."""
    deductions = sum(SCORE_DEDUCTIONS.get(i["severity"], 0) for i in issues_list)
    return max(0, 100 - deductions)


def _write_scores(all_by_worker, contacts, now):
    """Write compliance scores to Connecteam for all known workers."""
    date_str = now.strftime("%Y-%m-%d")
    written  = 0
    scored   = set()

    # Workers with issues → calculated score
    for wname, issues_list in all_by_worker.items():
        wid = (contacts.get(wname) or {}).get("userId")
        if not wid:
            continue
        score = calc_compliance_score(issues_list)
        ok, result = write_compliance_score(wid, score, date_str)
        if ok:
            written += 1
        elif "not set" not in str(result).lower():
            print(f"  [WARN] Score write failed for {wname}: {result}")
        scored.add(wname)

    # Workers with no issues → 100
    for wname, winfo in contacts.items():
        if wname in scored:
            continue
        wid = winfo.get("userId")
        if not wid:
            continue
        ok, _ = write_compliance_score(wid, 100.0, date_str)
        if ok:
            written += 1

    if written:
        print(f"Compliance scores written to Connecteam: {written} workers.")
    else:
        print("Compliance scores: skipped (COMPLIANCE_INDICATOR_ID not configured).")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now       = datetime.datetime.now(AEST)
    yesterday = now - datetime.timedelta(days=1)
    period    = f"yesterday ({yesterday.strftime('%a %d %b')})"

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

    if not issues:
        print("No issues found — all workers at 100% compliance.")
        _write_scores({}, contacts, now)
        return

    # ── Group issues ──────────────────────────────────────────────────────
    # notify_workers: CRITICAL/HIGH (all categories) + MEDIUM credentials
    # all_by_worker:  every issue, used for compliance scoring
    notify_workers = {}
    all_by_worker  = {}

    for iss in issues:
        w = iss.worker
        issue_dict = {
            "severity": iss.severity,
            "category": iss.category,
            "client":   iss.client or "",
            "date":     iss.date or "",
            "detail":   iss.detail or "",
        }
        all_by_worker.setdefault(w, []).append(issue_dict)

        include = (
            iss.severity in NOTIFY_SEVERITIES
            or (iss.severity == "MEDIUM" and iss.category in CRED_CATEGORIES)
        )
        if include:
            notify_workers.setdefault(w, []).append(issue_dict)

    if not notify_workers:
        print("No notifiable issues — nothing to send.")
        _write_scores(all_by_worker, contacts, now)
        return

    print(f"Workers to notify: {len(notify_workers)}")

    dry_run = not bool(CONNECTEAM_SENDER_ID)
    if dry_run:
        print("⚠️  DRY RUN — CONNECTEAM_SENDER_ID not set (Communications Hub upgrade pending).")
        print("   Messages will be logged but NOT sent. Add CONNECTEAM_SENDER_ID to activate.\n")
    else:
        print("Channel: Connecteam Chat\n")

    log      = load_log()
    sent_ok  = []
    sent_err = []

    for wname, issues_list in notify_workers.items():
        wid = (contacts.get(wname) or {}).get("userId")

        # Split into credential vs shift issues for targeted messages
        cred_issues  = [i for i in issues_list if i["category"] in CRED_CATEGORIES]
        shift_issues = [i for i in issues_list if i["category"] not in CRED_CATEGORIES]

        msgs_to_send = []
        if shift_issues:
            msgs_to_send.append(build_message(wname, shift_issues, period))
        if cred_issues:
            msgs_to_send.append(build_credential_message(wname, cred_issues))

        sev_counts = {}
        for i in issues_list:
            sev_counts[i["severity"]] = sev_counts.get(i["severity"], 0) + 1

        if dry_run:
            print(f"  ~ Would notify {wname} ({len(issues_list)} issues)")
            status = "Pending"
            sent_ok.append(wname)
        else:
            all_sent = True
            for msg in msgs_to_send:
                ok, result = send(wid, msg)
                if not ok:
                    print(f"  ✗ Failed {wname}: {result}")
                    all_sent = False
                    break
            if all_sent:
                print(f"  ✓ Sent to {wname} ({len(msgs_to_send)} message(s))")
                status = "Sent"
                sent_ok.append(wname)
            else:
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
            "message_sent":    msgs_to_send[0] if msgs_to_send else "",
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
    print(f"Done — {len(sent_ok)} sent, {len(sent_err)} failed")
    if sent_ok:  print(f"  Sent:   {', '.join(sent_ok)}")
    if sent_err: print(f"  Failed: {', '.join(sent_err)}")
    print(f"{'='*60}\n")

    # Manager SMS summary
    if MANAGER_NUMBER and sent_ok:
        summary = (
            f"Connect Care daily audit complete.\n"
            f"{len(sent_ok)} workers notified, {len(sent_err)} failed.\n"
            f"Workers: {', '.join(sent_ok)}"
        )
        send_sms(MANAGER_NUMBER, summary)
        print(f"Manager summary sent to {MANAGER_NUMBER}")

    # ── Write compliance scores to Connecteam ─────────────────────────────
    _write_scores(all_by_worker, contacts, now)


if __name__ == "__main__":
    main()
