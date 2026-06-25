#!/usr/bin/env python3
"""
Connect Care — Weekly Compliance Digest
Runs every Monday at 7 AM AEST via GitHub Actions.

Loads notified_issues.json for the last 7 days, counts issues by severity,
category, and worker, then uses Claude Haiku to write a plain-English narrative
summary — as if Amy is giving a weekly hand-over to management.

Posts the narrative to CC Management chat and prints it to stdout.

Run manually:
    python weekly_digest.py
"""

import os, sys, json, datetime, requests
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from collections import defaultdict

from connecteam_audit import (
    CONNECTEAM_API_KEY, CONNECTEAM_SENDER_ID, AEST,
)

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CC_MGMT_CONV_ID   = os.environ.get("CC_MGMT_CONV_ID", "")
if not CC_MGMT_CONV_ID:
    raise RuntimeError("CC_MGMT_CONV_ID environment variable is not set")

BASE_URL    = "https://api.connecteam.com"
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
NOTIFIED_FILE = os.path.join(SCRIPT_DIR, "notified_issues.json")

DIGEST_WINDOW_DAYS = 7


# ── Data loading ──────────────────────────────────────────────────────────────

def load_notified_issues() -> dict:
    try:
        with open(NOTIFIED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def collect_week_issues(notified: dict, now: datetime.datetime) -> list[dict]:
    """Return all issue records from the last DIGEST_WINDOW_DAYS days."""
    cutoff_ts  = now.timestamp() - (DIGEST_WINDOW_DAYS * 86400)
    cutoff_str = (now - datetime.timedelta(days=DIGEST_WINDOW_DAYS)).strftime("%Y-%m-%d")
    week_issues = []

    for fp, v in notified.items():
        if not isinstance(v, dict):
            continue

        # Resolve timestamp — prefer sent_ts, fall back to date string
        sent_ts = v.get("sent_ts")
        if not sent_ts:
            date_str = v.get("date", "")
            if not date_str or date_str < cutoff_str:
                continue
            try:
                dt = datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=AEST)
                sent_ts = dt.timestamp()
            except ValueError:
                continue

        if sent_ts < cutoff_ts:
            continue

        week_issues.append({
            "worker":       v.get("worker", "Unknown"),
            "category":     v.get("category", ""),
            "client":       v.get("client", ""),
            "date":         v.get("date", ""),
            "acknowledged": v.get("acknowledged", False),
            "cred":         v.get("cred", False),
            "cred_type":    v.get("cred_type", ""),
            "fp":           fp,
        })

    return week_issues


# ── Stats calculation ─────────────────────────────────────────────────────────

SEVERITY_CATEGORIES = {
    "CRITICAL": {
        "NO CLOCK-IN", "RESTRICTIVE PRACTICE MENTIONED", "INCOMPLETE INCIDENT REPORT",
        "LATE INCIDENT REPORTING", "EXPIRED CREDENTIAL", "NOTE DOESN'T MAKE SENSE",
    },
    "HIGH": {
        "LATE CLOCK-IN", "EARLY CLOCK-OUT", "MISSING CLOCK-OUT", "GPS MISMATCH",
        "GPS DATA MISSING", "AUTO CLOCK-OUT", "SUSPICIOUSLY SHORT SHIFT",
        "FAILS NDIS STANDARD", "MISSING FORM -- KALLAN", "MISSING FORM -- EVAN",
        "MISSING FORM -- MICHAEL", "FORM FREQUENCY -- JOSHUA", "FORM FREQUENCY -- NADA",
        "FORM FREQUENCY -- JOHN", "FORM FREQUENCY -- NICOLE",
        "DUPLICATE/COPY-PASTE NOTES", "CROSS-WORKER COPY-PASTE NOTES",
        "UNAUTHORISED CLIENT ACCESS", "ONBOARDING INCOMPLETE",
        "UNDERSTAFFED -- RATIO BREACH", "OPEN SHIFT",
        "POSSIBLE LATE INCIDENT REPORTING", "NO SHIFT NOTES", "EMPTY NOTES",
        "CREDENTIAL EXPIRING SOON",
    },
}

NOTE_CATEGORIES = {
    "MISSING NOTES", "SHORT NOTE", "COPY PASTE NOTE", "AI GENERATED NOTE",
    "NO SHIFT NOTES", "EMPTY NOTES", "INSUFFICIENT NOTES",
    "DUPLICATE/COPY-PASTE NOTES", "CROSS-WORKER COPY-PASTE NOTES",
    "POSSIBLE AI-GENERATED NOTE", "FAILS NDIS STANDARD",
}

CRED_CATEGORIES = {"EXPIRED CREDENTIAL", "CREDENTIAL EXPIRING SOON"}


def infer_severity(category: str) -> str:
    if category in SEVERITY_CATEGORIES["CRITICAL"]:
        return "CRITICAL"
    if category in SEVERITY_CATEGORIES["HIGH"]:
        return "HIGH"
    return "MEDIUM"


def calculate_stats(week_issues: list[dict]) -> dict:
    by_severity  = defaultdict(int)
    by_category  = defaultdict(int)
    by_worker    = defaultdict(int)
    acked_workers: set[str] = set()
    unacked_workers: set[str] = set()
    cred_workers: list[str]  = []
    note_workers = defaultdict(int)

    for iss in week_issues:
        worker   = iss["worker"]
        category = iss["category"]

        if worker.lower() in {"(team)", "unknown", ""}:
            sev = infer_severity(category)
            by_severity[sev] += 1
            by_category[category] += 1
            continue

        sev = infer_severity(category)
        by_severity[sev] += 1
        by_category[category] += 1
        by_worker[worker] += 1

        if iss["acknowledged"]:
            acked_workers.add(worker)
        else:
            unacked_workers.add(worker)

        if iss["cred"] or category in CRED_CATEGORIES:
            cred_workers.append(f"{worker} ({iss['cred_type'] or category})")

        if category in NOTE_CATEGORIES:
            note_workers[worker] += 1

    # Workers who haven't acknowledged anything
    true_unacked = unacked_workers - acked_workers

    return {
        "total":         len(week_issues),
        "by_severity":   dict(by_severity),
        "by_category":   dict(by_category),
        "by_worker":     dict(by_worker),
        "acked_workers": sorted(acked_workers),
        "unacked_workers": sorted(true_unacked),
        "cred_workers":  cred_workers,
        "note_workers":  dict(note_workers),
    }


# ── Narrative generation ──────────────────────────────────────────────────────

def build_narrative(stats: dict, now: datetime.datetime) -> str:
    """Ask Claude Haiku to write a plain-English coordinator-style weekly summary."""
    week_label = (now - datetime.timedelta(days=7)).strftime("%-d %b") + " – " + now.strftime("%-d %b %Y")

    # Build a rich context block so Claude can write specific, named details
    top_workers = sorted(stats["by_worker"].items(), key=lambda x: -x[1])[:5]
    top_cats    = sorted(stats["by_category"].items(), key=lambda x: -x[1])[:8]
    note_detail = sorted(stats["note_workers"].items(), key=lambda x: -x[1])[:5]
    cred_detail = stats["cred_workers"][:5]
    unacked     = stats["unacked_workers"][:5]

    context = f"""Week: {week_label}
Total issues flagged: {stats['total']}
By severity: CRITICAL={stats['by_severity'].get('CRITICAL', 0)}, HIGH={stats['by_severity'].get('HIGH', 0)}, MEDIUM={stats['by_severity'].get('MEDIUM', 0)}

Workers with most issues: {', '.join(f"{n} ({c})" for n, c in top_workers) or 'none'}
Top issue categories: {', '.join(f"{c} ({n})" for c, n in top_cats) or 'none'}
Note-quality issues by worker: {', '.join(f"{n} ({c})" for n, c in note_detail) or 'none'}
Credential issues: {', '.join(cred_detail) or 'none'}
Workers who have not acknowledged messages: {', '.join(unacked) or 'all acknowledged'}
Workers who acknowledged at least one message: {', '.join(stats['acked_workers'][:5]) or 'none'}"""

    if ANTHROPIC_API_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            prompt = f"""You are Amy, a coordinator at a disability support provider. Write a casual, plain-English weekly compliance hand-over for the management team.

Data from this week:
{context}

Rules:
- Write like you're giving a verbal hand-over to a colleague — conversational, not a report
- Use real worker first names from the data (e.g. "Roja", "Abdi", "Peter")
- Mention specific issue types by name (e.g. "GPS issues", "missing notes", "late clock-ins")
- Mention clients by name where relevant (e.g. "at Kallan's", "at Michael's place")
- DO NOT use bullet points — write flowing prose, 3-5 short paragraphs
- If there are no critical issues, say so clearly
- If credential issues exist, flag which workers and what credential
- If workers haven't responded to messages, mention it
- Keep total length under 300 words
- End with one actionable sentence for management (e.g. "Worth chasing X about Y")
- Output just the narrative, nothing else"""

            resp = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception as e:
            print(f"  [WARNING] Claude narrative generation failed: {e}")

    # Plain-text fallback
    lines = [f"Weekly compliance digest — {week_label}.", ""]
    lines.append(f"Total issues: {stats['total']} "
                 f"(CRITICAL: {stats['by_severity'].get('CRITICAL', 0)}, "
                 f"HIGH: {stats['by_severity'].get('HIGH', 0)}, "
                 f"MEDIUM: {stats['by_severity'].get('MEDIUM', 0)}).")

    if top_workers:
        lines.append("Workers with most issues this week: " +
                     ", ".join(f"{n} ({c})" for n, c in top_workers) + ".")

    if note_detail:
        lines.append("Note quality issues: " +
                     ", ".join(f"{n} ({c} issues)" for n, c in note_detail) + ".")

    if cred_detail:
        lines.append("Credential issues requiring follow-up: " + ", ".join(cred_detail) + ".")

    if unacked:
        lines.append("Workers who have not responded: " + ", ".join(unacked) + ".")

    return "\n".join(lines)


# ── Post to management ────────────────────────────────────────────────────────

def post_to_management(text: str):
    sender_id = int(CONNECTEAM_SENDER_ID or "0")
    if not sender_id or not CONNECTEAM_API_KEY:
        print(f"  [DRY RUN] Would post to CC Management:\n{text[:300]}\n")
        return
    try:
        r = requests.post(
            f"{BASE_URL}/chat/v1/conversations/{CC_MGMT_CONV_ID}/message",
            headers={"X-API-KEY": CONNECTEAM_API_KEY, "Content-Type": "application/json"},
            json={"senderId": sender_id, "text": text[:4000]},
            timeout=15,
        )
        if not r.ok:
            print(f"  [WARNING] CC Management post failed: {r.status_code} {r.text[:200]}")
        else:
            print("  Posted to CC Management.")
    except Exception as e:
        print(f"  [ERROR] CC Management post: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now       = datetime.datetime.now(AEST)
    run_label = now.strftime("%a %d %b %Y, %I:%M %p AEST")
    dry_run   = not bool(CONNECTEAM_SENDER_ID)

    print(f"\n{'='*60}")
    print(f"Connect Care Weekly Digest — {run_label}")
    print(f"Window: last {DIGEST_WINDOW_DAYS} days | dry_run={dry_run}")
    print(f"{'='*60}\n")

    notified    = load_notified_issues()
    week_issues = collect_week_issues(notified, now)

    print(f"Issues in last {DIGEST_WINDOW_DAYS} days: {len(week_issues)}")

    if not week_issues:
        narrative = (
            f"Weekly compliance digest ({run_label}).\n\n"
            "No issues were flagged this week. All workers appear to be compliant. "
            "Nothing to action."
        )
    else:
        stats     = calculate_stats(week_issues)
        narrative = build_narrative(stats, now)

        # Print stats to stdout for GitHub Actions logs
        print("\n--- Week Stats ---")
        print(f"  Total:    {stats['total']}")
        for sev in ("CRITICAL", "HIGH", "MEDIUM"):
            print(f"  {sev}: {stats['by_severity'].get(sev, 0)}")
        print(f"  Top workers: {', '.join(f'{n} ({c})' for n, c in sorted(stats['by_worker'].items(), key=lambda x: -x[1])[:5])}")
        if stats["cred_workers"]:
            print(f"  Credential issues: {', '.join(stats['cred_workers'])}")
        if stats["unacked_workers"]:
            print(f"  Unacknowledged: {', '.join(stats['unacked_workers'])}")

    print("\n--- Narrative ---")
    print(narrative)
    print("--- End Narrative ---\n")

    header  = f"Weekly Compliance Digest — {run_label}\n{'─' * 50}\n\n"
    full_msg = header + narrative

    post_to_management(full_msg)

    # ── Invoice audit section (Fix #7) ────────────────────────────────────────
    try:
        from invoice_check import reconcile, build_report, current_pay_period
        start_date, end_date = current_pay_period()
        print(f"\nRunning invoice audit for {start_date} – {end_date}...")
        inv_results = reconcile(start_date, end_date)
        flagged     = [r for r in inv_results if r["flags"]]
        if flagged:
            inv_report = build_report(inv_results, start_date, end_date)
            inv_header = f"Invoice Reconciliation — {start_date} to {end_date}\n{'─' * 50}\n\n"
            post_to_management(inv_header + inv_report)
            print(f"Invoice audit: {len(flagged)} worker(s) flagged — posted to CC Management.")
        else:
            post_to_management(
                f"Invoice check ({start_date} – {end_date}): all workers clear — no billing discrepancies found."
            )
            print("Invoice audit: all workers clear.")
    except Exception as e:
        print(f"  [WARN] Invoice audit skipped: {e}")

    # ── Manager capability reminder (Fix #6) — posted with every weekly digest ──
    reminder = (
        "💡 Reminder — you can ask me things directly in this chat:\n"
        "• \"who's clocked in today\" — live clock-in status\n"
        "• \"show me this week's timesheet\" — hours by worker\n"
        "• \"any shifts tomorrow\" — upcoming roster\n"
        "• \"any time-off requests pending\" — leave approvals\n"
        "• \"run invoice audit\" — flag billing discrepancies for current pay period\n"
        "• \"audit [worker name]'s invoice\" — check a specific worker\n"
        "I pull from Connecteam in real time so answers are current."
    )
    post_to_management(reminder)
    print("Amy capability reminder posted.")

    print(f"\n{'='*60}")
    print("Weekly digest done.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
