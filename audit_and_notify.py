#!/usr/bin/env python3
"""
Connect Care — Scheduled Shift Compliance Notifier
Runs twice daily via GitHub Actions:
  - 4:00 PM AEST  (06:00 UTC)  → catches morning shifts
  - 9:30 AM AEST  (23:30 UTC)  → catches evening/overnight shifts

For each worker with new CRITICAL or HIGH shift issues, Amy sends a
manager-tone message directly in their Connecteam group chat.
Team-level issues (missing forms, staffing ratio) go to CC Management.

Deduplication: notified_issues.json is committed back to the repo so
workers are never messaged about the same issue twice.

Run manually:
    python audit_and_notify.py
"""

import os, sys, json, hashlib, datetime, requests
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from collections import defaultdict

from connecteam_audit import (
    run_audit, fetch_all_users,
    send_worker_message,
    CONNECTEAM_SENDER_ID, CONNECTEAM_API_KEY,
    AEST,
)

# ── Config ────────────────────────────────────────────────────────────────────

DAYS_BACK         = 1          # always look back 24 hours; dedup handles overlap
NOTIFY_SEVERITIES = {"CRITICAL", "HIGH"}

# These are handled by daily_notify.py — skip here to avoid double-messaging
SKIP_CATEGORIES = {
    "EXPIRED CREDENTIAL", "CREDENTIAL EXPIRING SOON",
}

# Rostering/management issues — go to CC Management only, never to the individual worker
MANAGEMENT_ONLY_CATEGORIES = {
    "UNDERSTAFFED -- RATIO BREACH",
}

CC_MGMT_CONV_ID   = os.environ.get("CC_MGMT_CONV_ID", "4a14c09d-bc9f-46f2-9ad9-a728d6ddcbf6")
BASE_URL          = "https://api.connecteam.com"
NOTIFIED_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notified_issues.json")
DEDUP_EXPIRY_DAYS = 2   # forget fingerprints older than this


# ── Deduplication ─────────────────────────────────────────────────────────────

def issue_fingerprint(issue) -> str:
    """Stable hash for one issue — same worker+category+date = same fingerprint."""
    key = f"{issue.worker}|{issue.category}|{issue.date or ''}"
    return hashlib.md5(key.encode()).hexdigest()


def load_notified() -> dict:
    cutoff = (datetime.datetime.now(AEST) - datetime.timedelta(days=DEDUP_EXPIRY_DAYS)).strftime("%Y-%m-%d")
    try:
        with open(NOTIFIED_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {k: v for k, v in raw.items() if v.get("date", "9999") >= cutoff}
    except Exception:
        return {}


def save_notified(notified: dict):
    with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
        json.dump(notified, f, indent=2)


# ── Message generation ────────────────────────────────────────────────────────

def build_worker_message(worker_name: str, issues: list) -> str:
    """Generate a manager-tone compliance message via Claude Haiku."""
    from connecteam_audit import ANTHROPIC_API_KEY
    first = worker_name.split()[0]

    issue_lines = []
    for iss in issues[:10]:
        issue_lines.append(f"- [{iss.severity}] {iss.category} | {iss.client or 'N/A'} | {iss.date or 'N/A'}: {iss.detail}")
    if len(issues) > 10:
        issue_lines.append(f"({len(issues) - 10} additional issues not listed)")
    issues_block = "\n".join(issue_lines)

    if ANTHROPIC_API_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            prompt = f"""Write a short casual text message from Amy (a coordinator) to a support worker named {first} about issues from their shifts.

Issues:
{issues_block}

Rules:
- Start with "Hi {first},"
- Write like you're texting a colleague — short, friendly, straight to the point
- Say what happened and what they need to do, nothing else
- Use the client's name naturally (e.g. "at Kallan's", "at Joshua's place")
- Use the actual day (e.g. "on Sunday", "yesterday")
- Zero corporate language — no "identified", "compliance", "noted", "I am writing", "please be advised", "regarding"
- No greeting phrases like "I hope this finds you well"
- No sign-off
- If the issue is missing notes or missing forms, remind them that notes and forms must be submitted within 30 minutes of the shift ending — keep it casual, not a lecture
- Example: "Hi Roja, for your shift on Sunday at Kallan's you clocked in 4.8km away from the house — can you make sure you're at the address before you clock in next time. Also the shift notes weren't submitted — just a reminder these need to be done within 30 mins of finishing up."
- Output just the message, nothing else"""

            resp = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception as e:
            print(f"  [WARNING] Claude message generation failed: {e}")

    # Plain-text fallback
    lines = [f"Hi {first},"]
    for iss in issues[:10]:
        lines.append(f"for your shift on {iss.date or 'recent shift'} at {iss.client or 'your client'}: {iss.detail}")
    if len(issues) > 10:
        lines.append(f"there are also {len(issues) - 10} other issues on file.")
    lines.append("Can you sort these out and let me know.")
    return " ".join(lines)


def build_team_summary(issues: list, run_label: str) -> str:
    """Format team-level issues for the CC Management group chat."""
    lines = [f"Scheduled audit ({run_label}) — team-level issues:\n"]
    by_cat = defaultdict(list)
    for iss in issues:
        by_cat[iss.category].append(iss)
    for cat, cat_issues in sorted(by_cat.items()):
        lines.append(f"[{cat}]")
        for iss in cat_issues[:5]:
            lines.append(f"  {iss.client or 'N/A'} | {iss.date or 'N/A'}: {iss.detail}")
        if len(cat_issues) > 5:
            lines.append(f"  ... and {len(cat_issues) - 5} more")
        lines.append("")
    return "\n".join(lines).strip()


# ── Management chat ───────────────────────────────────────────────────────────

def post_to_management(text: str):
    sender_id = int(CONNECTEAM_SENDER_ID or "0")
    if not sender_id or not CONNECTEAM_API_KEY:
        print(f"  [DRY RUN] CC Management: {text[:120]}")
        return
    try:
        r = requests.post(
            f"{BASE_URL}/chat/v1/conversations/{CC_MGMT_CONV_ID}/message",
            headers={"X-API-KEY": CONNECTEAM_API_KEY, "Content-Type": "application/json"},
            json={"senderId": sender_id, "text": text[:4000]},
            timeout=15,
        )
        if not r.ok:
            print(f"  [WARNING] CC Management post failed: {r.status_code}")
    except Exception as e:
        print(f"  [ERROR] CC Management post: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now        = datetime.datetime.now(AEST)
    run_label  = now.strftime("%a %d %b, %I:%M %p AEST")
    dry_run    = not bool(CONNECTEAM_SENDER_ID)

    print(f"\n{'='*60}")
    print(f"Connect Care Shift Compliance Notifier — {run_label}")
    print(f"Auditing last {DAYS_BACK} day(s) | dry_run={dry_run}")
    print(f"{'='*60}\n")

    issues = run_audit(DAYS_BACK)

    notified = load_notified()
    print(f"Loaded {len(notified)} existing fingerprints from dedup cache.\n")

    # Build user ID lookup
    users        = fetch_all_users()
    name_to_uid  = {
        f"{u.get('firstName','')} {u.get('lastName','')}".strip(): uid
        for uid, u in users.items()
    }

    # Separate worker issues from team-level issues
    worker_new_issues = defaultdict(list)   # worker_name -> [Issue, ...]
    team_new_issues   = []

    TEAM_NAMES = {"(team)", "unknown", ""}

    for iss in issues:
        if iss.severity not in NOTIFY_SEVERITIES:
            continue
        if iss.category in SKIP_CATEGORIES:
            continue

        fp = issue_fingerprint(iss)
        if fp in notified:
            continue  # already messaged

        if iss.category in MANAGEMENT_ONLY_CATEGORIES or iss.worker.lower() in TEAM_NAMES:
            team_new_issues.append(iss)
        else:
            worker_new_issues[iss.worker].append(iss)

    print(f"New worker-level issues: {sum(len(v) for v in worker_new_issues.values())} "
          f"across {len(worker_new_issues)} worker(s)")
    print(f"New team-level issues:   {len(team_new_issues)}\n")

    # ── Message each worker ───────────────────────────────────────────────────
    sent_ok  = []
    sent_err = []

    for worker_name, worker_issues in sorted(worker_new_issues.items()):
        uid = name_to_uid.get(worker_name)
        if not uid:
            print(f"  [SKIP] {worker_name} — no user ID found in Connecteam")
            continue

        print(f"  Generating message for {worker_name} ({len(worker_issues)} issues)...")
        message = build_worker_message(worker_name, worker_issues)

        if dry_run:
            print(f"  [DRY RUN] Would send to {worker_name}:\n{message[:200]}...\n")
            sent_ok.append(worker_name)
            for iss in worker_issues:
                fp = issue_fingerprint(iss)
                notified[fp] = {"date": now.strftime("%Y-%m-%d"), "worker": worker_name}
        else:
            ok, result = send_worker_message(uid, message, worker_name=worker_name)
            if ok:
                print(f"  ✓ Sent to {worker_name}")
                sent_ok.append(worker_name)
                for iss in worker_issues:
                    fp = issue_fingerprint(iss)
                    notified[fp] = {"date": now.strftime("%Y-%m-%d"), "worker": worker_name}
            else:
                print(f"  ✗ Failed {worker_name}: {result}")
                sent_err.append(worker_name)

    # ── Manager summary → CC Management ─────────────────────────────────────
    summary_lines = [f"Audit done ({run_label})."]

    if sent_ok:
        summary_lines.append(f"\nMessaged {len(sent_ok)} worker(s):")
        for worker_name in sent_ok:
            worker_issues = worker_new_issues[worker_name]
            reasons = ", ".join(
                f"{i.category.lower()} ({i.client or 'N/A'}, {i.date or 'N/A'})"
                for i in worker_issues[:3]
            )
            if len(worker_issues) > 3:
                reasons += f" + {len(worker_issues) - 3} more"
            summary_lines.append(f"- {worker_name}: {reasons}")
    if sent_err:
        summary_lines.append(f"\nFailed to send to: {', '.join(sent_err)}")

    if team_new_issues:
        summary_lines.append(f"\nTeam issues (not sent to workers):")
        by_client = defaultdict(list)
        for iss in team_new_issues:
            by_client[iss.client or "N/A"].append(iss.category)
        for client, cats in by_client.items():
            summary_lines.append(f"- {client}: {', '.join(cats)}")
        for iss in team_new_issues:
            fp = issue_fingerprint(iss)
            notified[fp] = {"date": now.strftime("%Y-%m-%d"), "worker": "(team)"}

    if not sent_ok and not team_new_issues:
        summary_lines.append(" No new issues found.")

    mgmt_msg = "\n".join(summary_lines)
    print(f"\nPosting summary to CC Management...")
    print(f"  {mgmt_msg[:200]}")
    post_to_management(mgmt_msg)

    # ── Save dedup state ──────────────────────────────────────────────────────
    save_notified(notified)
    print(f"\nDedup cache updated: {len(notified)} fingerprints saved.")

    print(f"\n{'='*60}")
    print(f"Done — {len(sent_ok)} sent, {len(sent_err)} failed")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
