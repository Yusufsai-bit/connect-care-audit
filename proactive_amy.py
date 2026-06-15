#!/usr/bin/env python3
"""
Connect Care — Proactive Amy: Daily Shift Prep + Declining Note Quality Follow-up
Runs at 8 AM AEST via GitHub Actions.

THING 1 — Shift prep messages
  Fetches tomorrow's scheduled shifts from the Connecteam scheduler, then sends
  each worker a personalised prep message via Amy that mentions client name, shift
  time, and (for Kallan / Michael) a BSP reminder.

THING 2 — Declining note quality follow-up
  Examines notified_issues.json for workers whose note-category issue count has
  risen over the last 7 days vs the prior 7 days and sends a gentle check-in.

Dedup state is stored in shift_prep_notified.json and committed back to the repo
after every run using the same GitHub API pattern as audit_and_notify.py.

Run manually:
    python proactive_amy.py
"""

import os, sys, json, base64, datetime, requests, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from collections import defaultdict

from connecteam_audit import (
    send_worker_message, fetch_all_users, fetch_scheduled_shifts,
    CONNECTEAM_API_KEY, CONNECTEAM_SENDER_ID, AEST,
)

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CC_MGMT_CONV_ID   = os.environ.get("CC_MGMT_CONV_ID", "")
if not CC_MGMT_CONV_ID:
    raise RuntimeError("CC_MGMT_CONV_ID environment variable is not set")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "Yusufsai-bit/connect-care-audit")
BASE_URL     = "https://api.connecteam.com"

SCRIPT_DIR             = os.path.dirname(os.path.abspath(__file__))
SHIFT_PREP_DEDUP_FILE  = os.path.join(SCRIPT_DIR, "shift_prep_notified.json")
NOTIFIED_FILE          = os.path.join(SCRIPT_DIR, "notified_issues.json")

# Client names that require a BSP reminder in the prep message
BSP_CLIENTS = {"kallan", "michael"}

# Note-issue categories that count toward "note quality"
NOTE_CATEGORIES = {
    "MISSING NOTES", "SHORT NOTE", "COPY PASTE NOTE", "AI GENERATED NOTE",
    # Also cover category names used by the audit engine
    "NO SHIFT NOTES", "EMPTY NOTES", "INSUFFICIENT NOTES",
    "DUPLICATE/COPY-PASTE NOTES", "POSSIBLE AI-GENERATED NOTE",
    "FAILS NDIS STANDARD",
}

# Re-notify window for declining quality messages (days)
DECLINING_DEDUP_DAYS = 14


# ── GitHub-persisted file helper ──────────────────────────────────────────────

def _gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }


def _gh_read(filename: str) -> tuple[dict, str]:
    """Read a JSON file from GitHub. Returns (body_dict, sha)."""
    if not GITHUB_TOKEN:
        return {}, ""
    try:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}",
            headers=_gh_headers(), timeout=10,
        )
        if r.ok:
            raw = r.json()
            return json.loads(base64.b64decode(raw["content"]).decode()), raw.get("sha", "")
        return {}, ""
    except Exception as e:
        print(f"  [WARN] GitHub read {filename} failed: {e}")
        return {}, ""


def _gh_write(filename: str, body: dict, sha: str, message: str):
    """Write a JSON file to GitHub."""
    if not GITHUB_TOKEN:
        return
    try:
        content = base64.b64encode(json.dumps(body, indent=2).encode()).decode()
        payload = {"message": message, "content": content}
        if sha:
            payload["sha"] = sha
        r = requests.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}",
            headers=_gh_headers(), json=payload, timeout=15,
        )
        if not r.ok:
            print(f"  [WARN] GitHub write {filename} failed: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"  [WARN] GitHub write {filename} failed: {e}")


# ── Shift prep dedup ──────────────────────────────────────────────────────────

def load_shift_prep_dedup() -> dict:
    """Load shift_prep_notified.json from disk; fall back to GitHub."""
    try:
        with open(SHIFT_PREP_DEDUP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        body, _ = _gh_read("shift_prep_notified.json")
        return body


def save_shift_prep_dedup(data: dict):
    """Save to disk and push to GitHub."""
    with open(SHIFT_PREP_DEDUP_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    _, sha = _gh_read("shift_prep_notified.json")
    _gh_write(
        "shift_prep_notified.json", data, sha,
        f"chore: update shift prep dedup {datetime.datetime.now(AEST).strftime('%Y-%m-%d')} [skip ci]",
    )


def shift_prep_key(worker_id: str, date_str: str, client_name: str) -> str:
    return f"{worker_id}|{date_str}|{client_name.lower()}"


# ── Message builders ──────────────────────────────────────────────────────────

def build_shift_prep_message(worker_first: str, client_name: str,
                              shift_start: str, shift_end: str,
                              needs_bsp: bool) -> str:
    """Generate a brief shift prep message via Claude Haiku."""
    if ANTHROPIC_API_KEY:
        try:
            import anthropic
            client_ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            bsp_line = (
                f"- Include a reminder about following {client_name}'s Behaviour Support Plan (BSP) "
                "during the shift — keep it brief, not preachy."
                if needs_bsp else ""
            )
            prompt = f"""Write a short shift prep text message from Amy (a coordinator) to a support worker named {worker_first}.

Details:
- Worker first name: {worker_first}
- Client: {client_name}
- Tomorrow's shift: {shift_start} to {shift_end}

Rules:
- Start with "Hi {worker_first},"
- 2-3 sentences max — short and friendly, like a text from a colleague
- Mention the client's name naturally (e.g. "at Kallan's tomorrow", "for your shift with Michael")
- Mention the shift start time
- Zero corporate language — no "please be advised", "I am writing", "as per", "kindly"
- No sign-off
{bsp_line}
- Output just the message, nothing else"""

            resp = client_ai.messages.create(
                model="claude-haiku-4-5",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception as e:
            print(f"  [WARNING] Claude message generation failed: {e}")

    # Plain-text fallback
    bsp_note = f" Remember to follow the BSP." if needs_bsp else ""
    return (
        f"Hi {worker_first}, just a heads-up you've got a shift at {client_name}'s "
        f"tomorrow starting at {shift_start}.{bsp_note} Let me know if anything comes up."
    )


def build_declining_notes_message(worker_first: str, total_issues: int,
                                   recent_count: int, prior_count: int) -> str:
    """Generate a gentle check-in message for a worker with declining note quality."""
    if ANTHROPIC_API_KEY:
        try:
            import anthropic
            client_ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            prompt = f"""Write a short, gentle text message from Amy (a coordinator) to a support worker named {worker_first}.

Context:
- The worker has had {total_issues} note-related issues flagged in the last 2 weeks
- In the most recent week: {recent_count} note issues
- In the week before that: {prior_count} note issues
- The pattern is getting worse, not better

Rules:
- Start with "Hi {worker_first},"
- 2-3 sentences max
- Acknowledge the pattern without being accusatory — be warm and curious, not managerial
- Ask if anything is making notes harder to complete
- Zero corporate language — no "it has come to my attention", "I am writing to", "compliance"
- No sign-off
- Output just the message, nothing else"""

            resp = client_ai.messages.create(
                model="claude-haiku-4-5",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception as e:
            print(f"  [WARNING] Claude message generation failed: {e}")

    # Plain-text fallback
    return (
        f"Hi {worker_first}, I've noticed your shift notes have had a few issues over "
        f"the last couple of weeks — {recent_count} this week vs {prior_count} the week before. "
        "Is there anything making notes harder to get done at the moment? Happy to chat."
    )


# ── Management notification ───────────────────────────────────────────────────

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


# ── THING 1: Shift prep messages ──────────────────────────────────────────────

def run_shift_prep(now: datetime.datetime, dry_run: bool, dedup: dict) -> list[str]:
    """
    Fetch tomorrow's shifts and send each worker a prep message.
    Returns a list of worker names messaged.
    """
    print("\n--- THING 1: Shift Prep Messages ---")

    tomorrow      = now + datetime.timedelta(days=1)
    day_start_ts  = int(tomorrow.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    day_end_ts    = int(tomorrow.replace(hour=23, minute=59, second=59, microsecond=0).timestamp())
    date_str      = tomorrow.strftime("%Y-%m-%d")

    print(f"Fetching shifts for {tomorrow.strftime('%a %d %b %Y')}...")

    try:
        shifts = fetch_scheduled_shifts(day_start_ts, day_end_ts)
    except Exception as e:
        print(f"  [ERROR] Failed to fetch scheduled shifts: {e}")
        return []

    print(f"  Found {len(shifts)} shift(s) scheduled for tomorrow.")

    users       = fetch_all_users()
    uid_to_user = {str(uid): u for uid, u in users.items()}

    # Gather unique worker+client combos for tomorrow
    prep_targets: list[dict] = []
    seen_pairs: set[str]     = set()

    for shift in shifts:
        assigned = shift.get("assignedUserIds", [])
        sched_start = shift.get("startTime", 0)
        sched_end   = shift.get("endTime", 0)
        job_id      = shift.get("jobId", "")

        # Resolve client name from job title (uses module-level _jobs_cache)
        client_name = _resolve_client_name(job_id) if job_id else "(unknown client)"

        start_label = datetime.datetime.fromtimestamp(sched_start, tz=AEST).strftime("%I:%M %p") if sched_start else "TBD"
        end_label   = datetime.datetime.fromtimestamp(sched_end, tz=AEST).strftime("%I:%M %p") if sched_end else "TBD"

        for uid in assigned:
            uid_str = str(uid)
            pair    = f"{uid_str}|{client_name}"
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            prep_targets.append({
                "uid":         uid_str,
                "client_name": client_name,
                "start_label": start_label,
                "end_label":   end_label,
            })

    print(f"  {len(prep_targets)} unique worker-client pair(s) to prep.")

    messaged = []
    for target in prep_targets:
        uid_str     = target["uid"]
        client_name = target["client_name"]
        start_label = target["start_label"]
        end_label   = target["end_label"]

        dedup_key = shift_prep_key(uid_str, date_str, client_name)
        if dedup_key in dedup:
            print(f"  [SKIP] Already prepped {uid_str} for {client_name} on {date_str}")
            continue

        user = uid_to_user.get(uid_str, {})
        worker_name  = f"{user.get('firstName', '')} {user.get('lastName', '')}".strip()
        worker_first = user.get("firstName", "there") or "there"

        needs_bsp = any(kw in client_name.lower() for kw in BSP_CLIENTS)

        print(f"  Generating prep message for {worker_name} → {client_name} ({start_label})...")
        message = build_shift_prep_message(
            worker_first, client_name, start_label, end_label, needs_bsp
        )

        if dry_run:
            print(f"  [DRY RUN] Would send to {worker_name}:\n    {message[:200]}\n")
            dedup[dedup_key] = {"date": now.strftime("%Y-%m-%d"), "worker": worker_name, "client": client_name}
            messaged.append(worker_name)
        else:
            ok, result = send_worker_message(int(uid_str), message, worker_name=worker_name)
            if ok:
                print(f"  Sent prep to {worker_name} for {client_name}")
                dedup[dedup_key] = {
                    "date":    now.strftime("%Y-%m-%d"),
                    "worker":  worker_name,
                    "client":  client_name,
                    "sent_ts": int(now.timestamp()),
                }
                messaged.append(worker_name)
                time.sleep(1)  # be polite to Connecteam rate limits
            else:
                print(f"  [WARN] Failed to send prep to {worker_name}: {result}")

    return messaged


_jobs_cache: dict = {}

def _resolve_client_name(job_id) -> str:
    """Resolve job_id to a human client name. Caches jobs list on first call."""
    global _jobs_cache
    if not _jobs_cache:
        try:
            from connecteam_audit import fetch_all_jobs
            _jobs_cache = fetch_all_jobs()
        except Exception:
            pass
    job = _jobs_cache.get(job_id, {})
    return job.get("title", f"Job {job_id}") if job else f"Job {job_id}"


# ── THING 2: Declining note quality follow-up ─────────────────────────────────

def load_notified_issues() -> dict:
    """Load notified_issues.json from disk."""
    try:
        with open(NOTIFIED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def run_declining_notes(now: datetime.datetime, dry_run: bool, dedup: dict) -> list[str]:
    """
    Find workers with a rising trend of note issues and send a gentle check-in.
    Returns a list of worker names messaged.
    """
    print("\n--- THING 2: Declining Note Quality Follow-up ---")

    notified = load_notified_issues()
    if not notified:
        print("  notified_issues.json is empty — nothing to analyse.")
        return []

    # Partition timestamps into recent 7 days vs prior 7 days
    now_ts   = now.timestamp()
    recent_cutoff = now_ts - (7  * 86400)
    prior_cutoff  = now_ts - (14 * 86400)

    recent_counts: dict[str, int] = defaultdict(int)  # worker_name -> count in last 7 days
    prior_counts:  dict[str, int] = defaultdict(int)  # worker_name -> count in prior 7 days
    total_counts:  dict[str, int] = defaultdict(int)  # worker_name -> count in last 14 days

    for fp, v in notified.items():
        if not isinstance(v, dict):
            continue
        category   = v.get("category", "")
        worker     = v.get("worker", "")
        sent_ts    = v.get("sent_ts")

        if not worker or worker.lower() in {"(team)", "unknown", ""}:
            continue
        if category not in NOTE_CATEGORIES:
            continue
        if not sent_ts:
            # Fall back to date string if sent_ts missing
            date_str = v.get("date", "")
            if not date_str:
                continue
            try:
                dt = datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=AEST)
                sent_ts = dt.timestamp()
            except ValueError:
                continue

        if sent_ts >= prior_cutoff:
            total_counts[worker] += 1
            if sent_ts >= recent_cutoff:
                recent_counts[worker] += 1
            else:
                prior_counts[worker] += 1

    # Identify declining workers
    declining = []
    for worker, total in total_counts.items():
        if total < 3:
            continue
        recent = recent_counts.get(worker, 0)
        prior  = prior_counts.get(worker, 0)
        if recent > prior:
            declining.append((worker, total, recent, prior))

    print(f"  {len(declining)} worker(s) flagged with declining note quality.")

    if not declining:
        return []

    # Resolve names → UIDs
    users       = fetch_all_users()
    name_to_uid = {
        f"{u.get('firstName', '')} {u.get('lastName', '')}".strip(): str(uid)
        for uid, u in users.items()
    }
    uid_to_user = {str(uid): u for uid, u in users.items()}

    DEDUP_KEY_PREFIX = "declining_notes"
    messaged = []

    for worker_name, total, recent, prior in declining:
        uid_str = name_to_uid.get(worker_name)
        if not uid_str:
            print(f"  [SKIP] {worker_name} — no UID found")
            continue

        dedup_key = f"{DEDUP_KEY_PREFIX}|{uid_str}"
        if dedup_key in dedup:
            last_sent = dedup[dedup_key].get("date", "")
            try:
                last_dt = datetime.datetime.strptime(last_sent, "%Y-%m-%d").replace(tzinfo=AEST)
                if (now - last_dt).days < DECLINING_DEDUP_DAYS:
                    print(f"  [SKIP] {worker_name} — declining note message sent {(now - last_dt).days}d ago")
                    continue
            except ValueError:
                pass

        user         = uid_to_user.get(uid_str, {})
        worker_first = user.get("firstName", worker_name.split()[0]) or worker_name.split()[0]

        print(f"  Generating declining-notes message for {worker_name} "
              f"(total={total}, recent={recent}, prior={prior})...")
        message = build_declining_notes_message(worker_first, total, recent, prior)

        if dry_run:
            print(f"  [DRY RUN] Would send to {worker_name}:\n    {message[:200]}\n")
            dedup[dedup_key] = {"date": now.strftime("%Y-%m-%d"), "worker": worker_name}
            messaged.append(worker_name)
        else:
            ok, result = send_worker_message(int(uid_str), message, worker_name=worker_name)
            if ok:
                print(f"  Sent declining-notes message to {worker_name}")
                dedup[dedup_key] = {
                    "date":    now.strftime("%Y-%m-%d"),
                    "worker":  worker_name,
                    "sent_ts": int(now.timestamp()),
                }
                messaged.append(worker_name)
                time.sleep(1)
            else:
                print(f"  [WARN] Failed to send to {worker_name}: {result}")

    return messaged


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now      = datetime.datetime.now(AEST)
    dry_run  = not bool(CONNECTEAM_SENDER_ID)
    run_label = now.strftime("%a %d %b, %I:%M %p AEST")

    print(f"\n{'='*60}")
    print(f"Proactive Amy — {run_label}")
    print(f"dry_run={dry_run}")
    print(f"{'='*60}")

    dedup = load_shift_prep_dedup()
    print(f"Loaded {len(dedup)} existing dedup entries.\n")

    # THING 1 — shift prep
    prep_messaged = run_shift_prep(now, dry_run, dedup)

    # THING 2 — declining note quality
    declining_messaged = run_declining_notes(now, dry_run, dedup)

    # Persist dedup state
    save_shift_prep_dedup(dedup)
    print(f"\nDedup state saved: {len(dedup)} entries.")

    # Summary to CC Management
    summary_lines = [f"Proactive Amy run ({run_label})."]
    if prep_messaged:
        summary_lines.append(f"\nShift prep messages sent to {len(prep_messaged)} worker(s): "
                             f"{', '.join(prep_messaged)}")
    else:
        summary_lines.append("\nNo shift prep messages sent (all already notified or no shifts tomorrow).")
    if declining_messaged:
        summary_lines.append(f"\nDeclining note quality check-ins sent to: {', '.join(declining_messaged)}")

    if prep_messaged or declining_messaged:
        post_to_management("\n".join(summary_lines))

    print(f"\n{'='*60}")
    print(f"Done — {len(prep_messaged)} prep + {len(declining_messaged)} declining-notes sent")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
