#!/usr/bin/env python3
"""
Connect Care — 5PM Accountability Check
========================================
Runs at 5PM AEST via GitHub Actions (replaces the old 5PM audit duplicate).

Checks who received an Amy compliance message today and hasn't acknowledged it,
then posts a concise end-of-day outstanding list to CC Management.

No new worker messages are sent — this is management-facing only.
"""

import os
import sys
import json
import datetime
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from connecteam_audit import CONNECTEAM_API_KEY, CONNECTEAM_SENDER_ID, AEST

# ── Config ─────────────────────────────────────────────────────────────────────

CC_MGMT_CONV_ID = os.environ.get("CC_MGMT_CONV_ID", "")
if not CC_MGMT_CONV_ID:
    raise RuntimeError("CC_MGMT_CONV_ID environment variable is not set")

BASE_URL      = "https://api.connecteam.com"
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
NOTIFIED_FILE = os.path.join(SCRIPT_DIR, "notified_issues.json")

# How far back to look for "today's messages" (in hours)
WINDOW_HOURS = 18


def _load_notified() -> dict:
    try:
        with open(NOTIFIED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _post_to_management(text: str):
    sender_id = int(CONNECTEAM_SENDER_ID or "0")
    if not sender_id or not CONNECTEAM_API_KEY:
        print(f"[DRY RUN] Would post to CC Management:\n{text}\n")
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
        else:
            print("  Posted to CC Management.")
    except Exception as e:
        print(f"  [ERROR] CC Management post: {e}")


def main():
    now      = datetime.datetime.now(AEST)
    run_label = now.strftime("%a %d %b, %I:%M %p AEST")

    print(f"\n{'='*60}")
    print(f"Connect Care 5PM Accountability Check — {run_label}")
    print(f"{'='*60}\n")

    notified  = _load_notified()
    cutoff_ts = now.timestamp() - (WINDOW_HOURS * 3600)

    # Find messages sent in the last WINDOW_HOURS that haven't been acknowledged
    outstanding = {}   # worker_name -> [category, ...]
    for fp, v in notified.items():
        if not isinstance(v, dict):
            continue
        sent_ts = v.get("sent_ts", 0)
        if not sent_ts or sent_ts < cutoff_ts:
            continue
        if v.get("acknowledged") or v.get("escalated_48h"):
            continue
        # Skip management-only and internal fingerprints
        worker = v.get("worker", "")
        if not worker or worker.lower() in {"(team)", "unknown", ""}:
            continue
        if fp.startswith(("strike_esc|", "invoice_report|", "declining_notes|")):
            continue
        cat = v.get("category", "issue")
        outstanding.setdefault(worker, []).append(cat)

    print(f"Workers with unacknowledged messages from the last {WINDOW_HOURS}h: {len(outstanding)}")

    if not outstanding:
        print("All clear — everyone has responded or no messages were sent today.")
        return

    lines = [f"End of day ({now.strftime('%a %d %b')}) — {len(outstanding)} worker(s) still outstanding:"]
    for worker, cats in sorted(outstanding.items()):
        # Deduplicate and summarise categories
        unique_cats = list(dict.fromkeys(cats))
        summary     = ", ".join(c.lower() for c in unique_cats[:3])
        if len(unique_cats) > 3:
            summary += f" + {len(unique_cats) - 3} more"
        lines.append(f"  • {worker}: {summary}")
    lines.append("\nThese workers haven't replied to Amy. Worth a quick follow-up call if any are critical.")

    msg = "\n".join(lines)
    print(f"\n{msg}\n")
    _post_to_management(msg)

    print(f"\n{'='*60}")
    print("Accountability check done.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
