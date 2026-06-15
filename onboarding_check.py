#!/usr/bin/env python3
"""
Connect Care — New Worker Onboarding & Credential Check
Runs daily at 9 AM AEST via GitHub Actions (onboarding_check.yml).

For NEW workers (joined in last 30 days):
  - Checks if onboarding packs are complete
  - Checks if all 6 required credentials are on file
  - Sends a warm, personalised Amy message listing exactly what's missing
  - Deduplicates: won't re-send to the same worker within 7 days

For ALL workers:
  - Posts a daily expired-credential summary to CC Management

Dedup state is persisted in onboarding_notified.json via GitHub API.
"""

import os, sys, json, base64, datetime, time, random, requests
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from connecteam_audit import (
    fetch_all_users,
    fetch_worker_credentials,
    fetch_onboarding_completion,
    send_worker_message,
    CONNECTEAM_API_KEY,
    CONNECTEAM_SENDER_ID,
    ANTHROPIC_API_KEY,
    AEST,
)

# ── Config ─────────────────────────────────────────────────────────────────────

NEW_WORKER_DAYS      = 30     # workers who joined within this many days are "new"
ONBOARDING_DEDUP_DAYS = 7    # don't re-send onboarding message within this window
REQUIRED_CREDENTIALS = [
    "NDIS Worker Screening",
    "Working With Children Check",
    "Police Check",
    "First Aid Certificate",
    "CPR Certificate",
    "Manual Handling",
]

CC_MGMT_CONV_ID = os.environ.get("CC_MGMT_CONV_ID", "")
GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO     = os.environ.get("GITHUB_REPO", "Yusufsai-bit/connect-care-audit")
BASE_URL        = "https://api.connecteam.com"

NOTIFIED_FILE   = "onboarding_notified.json"  # path inside the repo (GitHub API)
LOCAL_NOTIFIED  = os.path.join(os.path.dirname(os.path.abspath(__file__)), NOTIFIED_FILE)


# ── GitHub-persisted dedup store ───────────────────────────────────────────────

def _gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept":        "application/vnd.github.v3+json",
    }


def load_notified_gh() -> dict:
    """Load onboarding_notified.json from GitHub. Falls back to local file."""
    if GITHUB_TOKEN:
        try:
            r = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/contents/{NOTIFIED_FILE}",
                headers=_gh_headers(), timeout=10,
            )
            if r.ok:
                body = json.loads(base64.b64decode(r.json()["content"]).decode())
                return body
        except Exception as e:
            print(f"  [WARN] Could not load dedup state from GitHub: {e}")

    # Local fallback
    try:
        with open(LOCAL_NOTIFIED, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_notified_gh(data: dict):
    """Commit onboarding_notified.json back to GitHub and write locally."""
    # Always write locally
    try:
        with open(LOCAL_NOTIFIED, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"  [WARN] Could not write local dedup state: {e}")

    if not GITHUB_TOKEN:
        return
    try:
        content = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
        # Get current SHA (needed for update)
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{NOTIFIED_FILE}",
            headers=_gh_headers(), timeout=10,
        )
        sha = r.json().get("sha", "") if r.ok else ""

        payload = {
            "message": "chore: update onboarding dedup state [skip ci]",
            "content": content,
        }
        if sha:
            payload["sha"] = sha

        requests.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{NOTIFIED_FILE}",
            headers=_gh_headers(), json=payload, timeout=15,
        )
        print(f"  Dedup state committed to GitHub ({len(data)} entries).")
    except Exception as e:
        print(f"  [WARN] Could not commit dedup state to GitHub: {e}")


def is_recently_notified(notified: dict, user_id: str, dedup_days: int) -> bool:
    """Return True if this worker was already messaged within dedup_days."""
    entry = notified.get(str(user_id))
    if not entry:
        return False
    try:
        sent = datetime.date.fromisoformat(entry["date"])
        cutoff = datetime.date.today() - datetime.timedelta(days=dedup_days)
        return sent >= cutoff
    except Exception:
        return False


# ── Join-date detection ────────────────────────────────────────────────────────

def get_join_date(user_data: dict) -> datetime.date | None:
    """
    Extract the worker's join date from their Connecteam user record.
    Tries several field names used across different API versions.
    """
    for field in ("createdAt", "joinDate", "startDate", "employmentStartDate",
                  "created_at", "hireDate", "dateAdded"):
        raw = user_data.get(field)
        if not raw:
            continue
        # Numeric timestamp (seconds or milliseconds)
        if isinstance(raw, (int, float)):
            ts = int(raw)
            if ts > 1e10:   # milliseconds → seconds
                ts //= 1000
            return datetime.datetime.fromtimestamp(ts, tz=AEST).date()
        # ISO string
        raw_str = str(raw)[:10]
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return datetime.datetime.strptime(raw_str, fmt).date()
            except ValueError:
                continue
    return None


def is_new_worker(user_data: dict, cutoff_date: datetime.date) -> bool:
    jd = get_join_date(user_data)
    return (jd is not None) and (jd >= cutoff_date)


# ── Message generation ─────────────────────────────────────────────────────────

def build_onboarding_message(
    worker_name: str,
    missing_packs: list[str],
    missing_creds: list[str],
) -> str:
    """
    Generate a warm, helpful Amy message for a new worker about missing
    onboarding or credentials. Uses Claude Haiku; falls back to plain text.
    """
    first = worker_name.split()[0]

    items = []
    if missing_packs:
        for pack in missing_packs:
            items.append(f"Onboarding pack not yet completed: {pack}")
    if missing_creds:
        for cred in missing_creds:
            items.append(f"Credential not yet on file: {cred}")

    items_block = "\n".join(f"- {item}" for item in items)

    if ANTHROPIC_API_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            prompt = f"""Write a short, warm message from Amy (a care coordinator) to a new support worker named {first} who has recently joined the team.

Missing items that need to be completed:
{items_block}

Rules:
- Start with "Hi {first},"
- Tone: warm, welcoming, supportive — they're new and still getting settled
- Briefly explain WHY each item matters (NDIS compliance keeps everyone safe)
- Tell them exactly what to do and where to find it (check the Connecteam app / onboarding section)
- Make it feel like a helpful nudge, not a warning
- Keep it short — 3–5 sentences max
- No corporate language — write like a real person texting a colleague
- No sign-off line needed
- Output just the message, nothing else"""

            resp = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=350,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception as e:
            print(f"  [WARN] Claude message generation failed: {e}")

    # Plain-text fallback
    lines = [
        f"Hi {first}, welcome to the team!",
        "",
        "Just a quick heads-up — there are a few things we still need from you to get you fully set up for NDIS compliance:",
        "",
    ]
    lines.extend(f"• {item}" for item in items)
    lines += [
        "",
        "You can find everything in the Connecteam app under the Onboarding section.",
        "Please get these sorted when you can — it's important for keeping our clients safe.",
        "Let me know if you need any help finding anything!",
    ]
    return "\n".join(lines)


# ── Management post ────────────────────────────────────────────────────────────

def post_to_management(text: str):
    """Send a message to the CC Management group conversation."""
    sender_id = int(CONNECTEAM_SENDER_ID or "0")
    if not sender_id or not CONNECTEAM_API_KEY or not CC_MGMT_CONV_ID:
        print(f"  [DRY RUN] CC Management would receive:\n{text[:300]}")
        return
    try:
        r = requests.post(
            f"{BASE_URL}/chat/v1/conversations/{CC_MGMT_CONV_ID}/message",
            headers={
                "X-API-KEY":    CONNECTEAM_API_KEY,
                "Content-Type": "application/json",
            },
            json={"senderId": sender_id, "text": text[:4000]},
            timeout=15,
        )
        if r.ok:
            print("  Posted to CC Management.")
        else:
            print(f"  [WARN] CC Management post failed: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"  [ERROR] CC Management post: {e}")


# ── Expired credential summary ─────────────────────────────────────────────────

def build_expired_credential_summary(cred_data: dict, users: dict) -> str | None:
    """
    Build a daily summary of ALL workers with expired credentials.
    Returns None if there are no expired credentials.
    """
    today = datetime.date.today()
    expired_lines = []

    def uname(uid):
        u = users.get(uid)
        return f"{u['firstName']} {u['lastName']}" if u else f"User {uid}"

    for uid, creds in cred_data.items():
        name = uname(uid)
        for cred_type, expiry_date in creds.items():
            days_left = (expiry_date - today).days
            if days_left < 0:
                expired_lines.append(
                    f"  • {name} — {cred_type} expired {abs(days_left)} day(s) ago "
                    f"({expiry_date.strftime('%d %b %Y')})"
                )
            elif days_left <= 1:
                expired_lines.append(
                    f"  • {name} — {cred_type} expires tomorrow ({expiry_date.strftime('%d %b %Y')})"
                )

    if not expired_lines:
        return None

    today_str = datetime.datetime.now(AEST).strftime("%a %d %b %Y")
    lines = [f"⚠️ Expired credentials — {today_str}:", ""] + expired_lines + [
        "",
        "Workers with expired credentials must NOT provide supports until renewed.",
    ]
    return "\n".join(lines)


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    # Small random delay so the script doesn't always fire at exactly the same second
    delay = random.randint(0, 5 * 60)
    print(f"Waiting {delay // 60}m {delay % 60}s (randomised delay)...")
    time.sleep(delay)

    now          = datetime.datetime.now(AEST)
    today        = now.date()
    cutoff_date  = today - datetime.timedelta(days=NEW_WORKER_DAYS)
    dry_run      = not bool(CONNECTEAM_SENDER_ID)

    print(f"\n{'='*60}")
    print(f"Connect Care — Onboarding & Credential Check")
    print(f"Date: {now.strftime('%a %d %b %Y, %I:%M %p AEST')}")
    print(f"New worker window: last {NEW_WORKER_DAYS} days (since {cutoff_date})")
    print(f"dry_run={dry_run}")
    print(f"{'='*60}\n")

    # Load dedup state
    notified = load_notified_gh()
    print(f"Loaded {len(notified)} onboarding dedup entries.\n")

    # Fetch all users
    print("Fetching users from Connecteam...")
    users = fetch_all_users()
    print(f"  {len(users)} total users found.")

    active_user_ids = set(users.keys())

    # Identify new workers (joined in last 30 days)
    new_workers = {
        uid: udata
        for uid, udata in users.items()
        if is_new_worker(udata, cutoff_date)
    }
    print(f"  {len(new_workers)} new worker(s) in the last {NEW_WORKER_DAYS} days.\n")

    # Fetch onboarding completion for new workers only (faster)
    print("Fetching onboarding pack completion...")
    new_worker_ids = set(new_workers.keys())
    onboarding_gaps = fetch_onboarding_completion(new_worker_ids) if new_worker_ids else {}
    print(f"  {len(onboarding_gaps)} new worker(s) have incomplete onboarding packs.")

    # Fetch credentials for ALL workers (needed for expired cred summary too)
    print("Fetching worker credentials...")
    cred_data = fetch_worker_credentials(users)
    print(f"  Credentials loaded for {len(cred_data)} worker(s).\n")

    # ── Expired credential daily summary (ALL workers) ──────────────────────────
    print("Building expired credential summary...")
    expired_summary = build_expired_credential_summary(cred_data, users)
    if expired_summary:
        print(f"  Posting expired credential summary to CC Management...")
        post_to_management(expired_summary)
    else:
        print("  No expired or expiring-tomorrow credentials today.")

    # ── New worker onboarding messages ──────────────────────────────────────────
    print(f"\nChecking {len(new_workers)} new worker(s) for onboarding gaps...\n")

    sent_count   = 0
    skipped_count = 0

    def uname(uid):
        u = users.get(uid)
        return f"{u['firstName']} {u['lastName']}" if u else f"User {uid}"

    for uid, udata in sorted(new_workers.items(), key=lambda kv: uname(kv[0])):
        name = uname(uid)

        # Check if already notified this week
        if is_recently_notified(notified, uid, ONBOARDING_DEDUP_DAYS):
            print(f"  [SKIP] {name} — already notified within {ONBOARDING_DEDUP_DAYS} days")
            skipped_count += 1
            continue

        # What's missing?
        missing_packs = onboarding_gaps.get(uid, [])

        worker_creds   = cred_data.get(uid, {})
        missing_creds  = [
            cred for cred in REQUIRED_CREDENTIALS
            if cred not in worker_creds
        ]

        if not missing_packs and not missing_creds:
            print(f"  [OK] {name} — all onboarding complete, all credentials on file")
            continue

        # Build and send message
        join_date = get_join_date(udata)
        join_str  = join_date.strftime("%d %b %Y") if join_date else "recently"
        print(f"  {name} (joined {join_str}) — missing: "
              f"{len(missing_packs)} pack(s), {len(missing_creds)} credential(s)")
        if missing_packs:
            print(f"    Packs:       {', '.join(missing_packs)}")
        if missing_creds:
            print(f"    Credentials: {', '.join(missing_creds)}")

        message = build_onboarding_message(name, missing_packs, missing_creds)

        if dry_run:
            print(f"  [DRY RUN] Would send to {name}:\n    {message[:200]}\n")
            notified[str(uid)] = {
                "date":    today.isoformat(),
                "worker":  name,
                "missing_packs": missing_packs,
                "missing_creds": missing_creds,
            }
            sent_count += 1
        else:
            ok, result = send_worker_message(uid, message, worker_name=name)
            if ok:
                print(f"  ✓ Sent onboarding reminder to {name}")
                notified[str(uid)] = {
                    "date":    today.isoformat(),
                    "worker":  name,
                    "missing_packs": missing_packs,
                    "missing_creds": missing_creds,
                }
                sent_count += 1
            else:
                print(f"  ✗ Failed to send to {name}: {result}")

    # ── Persist dedup state ─────────────────────────────────────────────────────
    # Prune entries older than 90 days to keep the file small
    cutoff_iso = (today - datetime.timedelta(days=90)).isoformat()
    notified = {
        uid: v for uid, v in notified.items()
        if isinstance(v, dict) and v.get("date", "9999") >= cutoff_iso
    }
    save_notified_gh(notified)

    print(f"\n{'='*60}")
    print(f"Done — {sent_count} onboarding reminder(s) sent, {skipped_count} skipped (already notified)")
    if expired_summary:
        print(f"Expired credential summary posted to CC Management.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
