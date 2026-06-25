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

import os, sys, json, hashlib, datetime, requests, random, time, base64
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from collections import defaultdict

from connecteam_audit import (
    run_audit, fetch_all_users,
    send_worker_message, load_worker_conversations,
    lock_worker_days, add_worker_note,
    CONNECTEAM_SENDER_ID, CONNECTEAM_API_KEY,
    AEST,
)

# ── Config ────────────────────────────────────────────────────────────────────

DAYS_BACK         = int(os.environ.get("AUDIT_DAYS_BACK", "1"))
NOTIFY_SEVERITIES = {"CRITICAL", "HIGH"}

SKIP_CATEGORIES = set()  # nothing skipped — credentials handled here now

# Note categories that count toward declining quality trend
NOTE_QUALITY_CATEGORIES = {
    "NO SHIFT NOTES", "EMPTY NOTES", "INSUFFICIENT NOTES",
    "DUPLICATE/COPY-PASTE NOTES", "POSSIBLE AI-GENERATED NOTE",
    "FAILS NDIS STANDARD", "NOT PERSON-CENTRED", "NO PLAN GOAL REFERENCE",
}
DECLINING_DEDUP_DAYS = 14  # re-notify window for declining quality messages

CRED_CATEGORIES         = {"EXPIRED CREDENTIAL", "CREDENTIAL EXPIRING SOON"}
CRED_DEDUP_DAYS_EXPIRED = 1   # re-notify daily for expired credentials
CRED_DEDUP_DAYS_SOON    = 7   # re-notify weekly for expiring-soon credentials

PENDING_CATEGORIES      = {"PENDING AMENDMENT -- WORKER NOTICE", "PENDING AMENDMENT -- REVIEW REQUIRED"}
PENDING_DEDUP_DAYS      = 7   # only re-notify about the same pending amendment once a week

# Rostering/management issues — go to CC Management only, never to the individual worker
MANAGEMENT_ONLY_CATEGORIES = {
    "UNDERSTAFFED -- RATIO BREACH",
    "PENDING AMENDMENT -- REVIEW REQUIRED",
}

CC_MGMT_CONV_ID   = os.environ.get("CC_MGMT_CONV_ID", "")
if not CC_MGMT_CONV_ID:
    raise RuntimeError("CC_MGMT_CONV_ID environment variable is not set")
BASE_URL          = "https://api.connecteam.com"
NOTIFIED_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notified_issues.json")
STRIKE_LOG_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strike_log.json")
CONVO_LOG_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "amy_conversation_log.json")
DEDUP_EXPIRY_DAYS = 2   # forget fingerprints older than this
STRIKE_LOG_DAYS   = 30  # how far back strike history looks


# ── Worker profile updater (GitHub-persisted) ─────────────────────────────────

PROFILES_FILE = "worker_profiles.json"
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO   = os.environ.get("GITHUB_REPO", "Yusufsai-bit/connect-care-audit")


def _update_worker_profile_gh(uid: str, updates: dict):
    """Read-modify-write worker_profiles.json on GitHub."""
    if not GITHUB_TOKEN:
        return
    try:
        gh_headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{PROFILES_FILE}",
            headers=gh_headers, timeout=10,
        )
        if r.ok:
            body = json.loads(base64.b64decode(r.json()["content"]).decode())
            sha  = r.json().get("sha", "")
        else:
            body, sha = {}, ""
        existing = body.get(uid, {})
        existing.update(updates)
        body[uid] = existing
        content = base64.b64encode(json.dumps(body, indent=2).encode()).decode()
        payload = {"message": "chore: update worker profiles [skip ci]", "content": content}
        if sha:
            payload["sha"] = sha
        requests.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{PROFILES_FILE}",
            headers=gh_headers, json=payload, timeout=15,
        )
    except Exception as e:
        print(f"  [WARN] Worker profile update failed: {e}")


# ── Conversation log (for Amy smart reply) ────────────────────────────────────

def load_convo_log() -> dict:
    try:
        with open(CONVO_LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_convo_log(log: dict):
    with open(CONVO_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)


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
        # Fix #1: purge epoch-0 legacy entries (sent_ts=0 means timestamp was never set)
        cleaned = {
            k: v for k, v in raw.items()
            if v.get("date", "9999") >= cutoff and v.get("sent_ts", 1) != 0
        }
        purged = len(raw) - len(cleaned)
        if purged:
            print(f"  [cleanup] Purged {purged} entries with invalid timestamps from dedup cache.")
        return cleaned
    except Exception:
        return {}


def save_notified(notified: dict):
    with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
        json.dump(notified, f, indent=2)


# ── Message generation ────────────────────────────────────────────────────────

def _load_strike_log() -> list:
    """Load strike history (30-day window), pruning stale entries on load."""
    cutoff = (datetime.datetime.now(AEST) - datetime.timedelta(days=STRIKE_LOG_DAYS)).strftime("%Y-%m-%d")
    try:
        with open(STRIKE_LOG_FILE, "r", encoding="utf-8") as f:
            entries = json.load(f)
        return [e for e in entries if e.get("date", "") >= cutoff]
    except Exception:
        return []


def _append_strikes(worker_name: str, categories: list, today: str):
    """Append one entry per category to the strike log and save."""
    entries = _load_strike_log()
    for cat in categories:
        entries.append({"worker": worker_name, "category": cat, "date": today})
    try:
        with open(STRIKE_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)
    except Exception as e:
        print(f"  [WARN] Strike log write failed: {e}")


def _get_strike_count(worker_name: str, category: str, _notified_unused: dict = None) -> int:
    """Count how many times this worker has been notified for this category in the last 30 days."""
    return sum(
        1 for e in _load_strike_log()
        if e.get("worker") == worker_name and e.get("category") == category
    )


def _format_history(history: list, first: str) -> str:
    """Format conversation turns as a readable block for Claude prompts."""
    if not history:
        return ""
    lines = []
    for turn in history[-6:]:
        label = "Amy" if turn.get("role") == "amy" else first
        lines.append(f"{label}: {turn.get('text', '')}")
    return "\n".join(lines)


def build_worker_message(worker_name: str, issues: list, strike_counts: dict = None,
                         history: list = None) -> str:
    """Generate a compliance message via Claude Haiku, informed by conversation history."""
    from connecteam_audit import ANTHROPIC_API_KEY
    first = worker_name.split()[0]
    strike_counts = strike_counts or {}

    # Prioritise: CRITICAL first, then HIGH, cap at 3 so the message isn't a wall of issues
    sorted_issues = sorted(issues, key=lambda i: (0 if i.severity == "CRITICAL" else 1 if i.severity == "HIGH" else 2))
    top_issues    = sorted_issues[:3]
    overflow      = len(issues) - len(top_issues)

    issue_lines = []
    for iss in top_issues:
        strike = strike_counts.get(iss.category, 0)
        strike_note = f" [3rd+ offence]" if strike >= 2 else f" [2nd offence]" if strike == 1 else ""
        issue_lines.append(f"- [{iss.severity}]{strike_note} {iss.category} | {iss.client or 'N/A'} | {iss.date or 'N/A'}: {iss.detail}")
    if overflow:
        issue_lines.append(f"({overflow} further issue(s) — address these first, then I'll follow up)")
    issues_block = "\n".join(issue_lines)

    has_critical  = any(i.severity == "CRITICAL" for i in top_issues)
    max_strikes   = max((strike_counts.get(i.category, 0) for i in top_issues), default=0)

    if max_strikes >= 2:
        tone_instruction = (
            "This worker has been flagged for the same issue 3 or more times. "
            "Be direct and clear that this pattern needs to stop — name the fact that it's happened again. "
            "No corporate language, but drop the softness. No threats, just firmness. "
            "Example: 'Abdi, this is the third time now I've had to flag a late incident report. That's not okay — these need to go in within 30 minutes of the shift ending, every time.'"
        )
    elif max_strikes == 1:
        tone_instruction = (
            "This worker has been flagged for this before. Acknowledge it's a repeat — briefly, not harshly. "
            "Example: 'Hey, this one's come up before — just want to make sure we get it sorted this time.'"
        )
    else:
        tone_instruction = "Casual and curious — first time flagging these issues. Lead with a question where the cause is unclear (GPS, late clock-in, short shift). Be direct where the cause is obvious (no clock-in at all, no notes at all)."

    history_block = _format_history(history, first)
    history_section = f"\nRecent conversation with {first}:\n{history_block}\n" if history_block else ""
    opener_rule = (
        f"- Do NOT open with 'Hi {first},' — there's an ongoing conversation above, pick up the thread naturally."
        if history_block else f"- Start with 'Hey {first},' (casual, not 'Hi')"
    )
    deadline_rule = (
        "- This is urgent — ask them to reply today."
        if has_critical else
        "- No hard deadline — just ask them to get back to you when they can today. Do NOT say '5 PM'."
    )

    if ANTHROPIC_API_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            prompt = f"""You are Amy, a coordinator at Connect Care. Write a short text message to {first} about issues from their recent shifts.
{history_section}
Issues to raise (most serious listed first):
{issues_block}

Tone guidance: {tone_instruction}

Rules:
{opener_rule}
- Write like a real person texting — short, warm but direct
- For ambiguous issues (GPS mismatch, short shift, late clock-in): open with a genuine question — "where were you when you clocked in?" not a statement of wrongdoing
- For clear-cut issues (no clock-in at all, no notes submitted): be direct — "did you work that shift?" or "I've got no notes from that shift"
- Use the client's name naturally ("at Kallan's", "at Joshua's place") and the actual day ("on Tuesday", "yesterday")
- Raise at most 2–3 things — if there are more, focus on the most serious and say you'll follow up on the rest
- Zero corporate language — no "identified", "compliance", "noted", "I am writing", "please be advised", "regarding"
- No sign-off, no closing line
- If the issue is missing notes or forms: remind them it needs to be done within 30 mins of the shift ending — casual, not a lecture
- ONLY reference issues explicitly listed above — do not add, infer, or mention anything not in the list
{deadline_rule}
- Output just the message text, nothing else"""

            resp = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception as e:
            print(f"  [WARNING] Claude message generation failed: {e}")

    # Plain-text fallback
    fallback = [f"Hi {first},"]
    for iss in issues[:10]:
        fallback.append(f"for your shift on {iss.date or 'recent shift'} at {iss.client or 'your client'}: {iss.detail}")
    if len(issues) > 10:
        fallback.append(f"there are also {len(issues) - 10} other issues on file.")
    fallback.append("Can you sort these out and let me know.")
    return " ".join(fallback)


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
    def _split_at_lines(t, max_chars=950):
        if len(t) <= max_chars:
            return [t]
        chunks, current = [], ""
        for line in t.split("\n"):
            candidate = (current + "\n" + line).lstrip("\n")
            if len(candidate) > max_chars:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks
    for chunk in _split_at_lines(text):
        try:
            r = requests.post(
                f"{BASE_URL}/chat/v1/conversations/{CC_MGMT_CONV_ID}/message",
                headers={"X-API-KEY": CONNECTEAM_API_KEY, "Content-Type": "application/json"},
                json={"senderId": sender_id, "text": chunk},
                timeout=15,
            )
            if not r.ok:
                print(f"  [WARNING] CC Management post failed: {r.status_code}")
        except Exception as e:
            print(f"  [ERROR] CC Management post: {e}")


# ── Credential message ────────────────────────────────────────────────────────

def build_credential_message(worker_name: str, cred_issues: list,
                              history: list = None) -> str:
    from connecteam_audit import ANTHROPIC_API_KEY
    first    = worker_name.split()[0]
    expired  = [i for i in cred_issues if i.category == "EXPIRED CREDENTIAL"]
    expiring = [i for i in cred_issues if i.category != "EXPIRED CREDENTIAL"]

    history_block   = _format_history(history, first)
    history_section = f"\nRecent conversation with {first}:\n{history_block}\n" if history_block else ""
    opener_rule     = (
        f"Do NOT open with 'Hi {first},' — there's an ongoing conversation above, pick up naturally."
        if history_block else f"Start with 'Hi {first},'"
    )

    cred_lines = []
    for iss in expired:
        cred_lines.append(f"EXPIRED: {iss.detail}")
    for iss in expiring:
        cred_lines.append(f"EXPIRING SOON: {iss.detail}")
    cred_block = "\n".join(cred_lines)

    if ANTHROPIC_API_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            prompt = f"""You are Amy, a coordinator at Connect Care. Write a short text to {first} about their credentials.
{history_section}
Credential issues:
{cred_block}

Rules:
- {opener_rule}
- Write like a real person texting — casual, direct
- If expired: make clear they can't work until it's sorted, ask them to send through updated docs
- If expiring soon: give them a heads up and ask them to sort it before it lapses
- End with: ask them to reply with a photo or PDF of the renewed certificate
- Don't repeat anything already in the conversation above
- No sign-off, no corporate language
- Output just the message, nothing else"""
            resp = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception as e:
            print(f"  [WARNING] Claude credential message failed: {e}")

    # Plain-text fallback
    lines = [f"Hi {first},"]
    if expired:
        lines.append("these credentials have expired and need renewing immediately:")
        for iss in expired:
            lines.append(f"  • {iss.detail}")
        lines.append("You can't work shifts until these are current.")
    if expiring:
        lines.append("these are expiring soon — sort them before they lapse:")
        for iss in expiring:
            lines.append(f"  • {iss.detail}")
    lines.append("Once renewed, reply here with a photo or PDF and I'll update your records.")
    return "\n".join(lines)


def _check_credential_followups(now: datetime.datetime, notified: dict, name_to_uid: dict,
                                 conv_map: dict, dry_run: bool):
    """Send a 3-day follow-up to workers who haven't provided updated credentials."""
    threshold = int((now - datetime.timedelta(days=3)).timestamp())
    sender_id = int(CONNECTEAM_SENDER_ID or "0")

    for fp, v in notified.items():
        if not isinstance(v, dict):
            continue
        if v.get("cred_followup_sent") or v.get("acknowledged"):
            continue
        if v.get("cred") != True:
            continue
        sent_ts = v.get("sent_ts")
        if not sent_ts or sent_ts > threshold:
            continue

        wname = v.get("worker", "")
        first = wname.split()[0] if wname else "there"
        uid   = name_to_uid.get(wname)
        if not uid:
            continue

        msg = (
            f"Hey {first}, just following up — still waiting on your updated credential documents. "
            f"Can you send them through today? Can't approve shifts until they're on file."
        )
        if dry_run:
            print(f"  [DRY RUN] Credential follow-up to {wname}")
            v["cred_followup_sent"] = True
            continue

        if not sender_id or not CONNECTEAM_API_KEY:
            continue

        conv_id = conv_map.get(str(uid))
        sent = False
        if conv_id:
            try:
                r = requests.post(
                    f"{BASE_URL}/chat/v1/conversations/{conv_id}/message",
                    headers={"X-API-KEY": CONNECTEAM_API_KEY, "Content-Type": "application/json"},
                    json={"senderId": sender_id, "text": msg},
                    timeout=15,
                )
                sent = r.ok
            except Exception:
                pass
        if not sent:
            try:
                r = requests.post(
                    f"{BASE_URL}/chat/v1/conversations/privateMessage/{uid}",
                    headers={"X-API-KEY": CONNECTEAM_API_KEY, "Content-Type": "application/json"},
                    json={"senderId": sender_id, "text": msg},
                    timeout=15,
                )
                sent = r.ok
            except Exception:
                pass

        if sent:
            print(f"  ✓ Credential follow-up sent to {wname}")
            v["cred_followup_sent"] = True
        else:
            print(f"  ✗ Credential follow-up failed for {wname}")


# ── Time entry locking ────────────────────────────────────────────────────────

def _lock_prior_days(name_to_uid: dict, now: datetime):
    """Lock time entries older than 3 days to prevent backdating."""
    lock_date = (now - datetime.timedelta(days=3)).strftime("%Y-%m-%d")
    locked = 0
    for uid in name_to_uid.values():
        ok, _ = lock_worker_days(uid, [lock_date])
        if ok:
            locked += 1
    if locked:
        print(f"Locked time entries for {locked} worker(s) on {lock_date}.")


# ── CRITICAL profile notes ────────────────────────────────────────────────────

def _add_critical_profile_notes(issues: list, name_to_uid: dict, now: datetime):
    """Write a permanent note to the worker's Connecteam profile for CRITICAL breaches."""
    by_worker = defaultdict(list)
    for iss in issues:
        if iss.severity == "CRITICAL":
            by_worker[iss.worker].append(iss)

    noted = 0
    for wname, crit_issues in by_worker.items():
        uid = name_to_uid.get(wname)
        if not uid:
            continue
        lines = [f"[{now.strftime('%d %b %Y')} — Automated Audit]"]
        for iss in crit_issues[:5]:
            lines.append(f"CRITICAL — {iss.category}: {iss.client} on {iss.date}. {iss.detail}")
        ok, _ = add_worker_note(uid, "\n".join(lines))
        if ok:
            noted += 1
    if noted:
        print(f"Critical breach notes added to {noted} worker profile(s).")


# ── No-reply 48-hour escalation ───────────────────────────────────────────────

def check_unacknowledged_escalations(now: datetime.datetime, notified: dict, dry_run: bool):
    """
    Two-stage no-reply follow-up:
    - 24h: Amy sends a follow-up directly to the worker
    - 48h: escalate to CC Management for manager to call them directly
    """
    threshold_24h = int((now - datetime.timedelta(hours=24)).timestamp())
    threshold_48h = int((now - datetime.timedelta(hours=48)).timestamp())

    needs_24h_followup = {}   # worker_name -> {uid, cats}
    needs_48h_escalate = {}   # worker_name -> [cats]

    users       = fetch_all_users()
    name_to_uid = {
        f"{u.get('firstName','')} {u.get('lastName','')}".strip(): uid
        for uid, u in users.items()
    }

    for fp, v in notified.items():
        if not isinstance(v, dict):
            continue
        if v.get("acknowledged") or v.get("escalated_48h"):
            continue
        sent_ts = v.get("sent_ts")
        if not sent_ts:
            continue
        wname = v.get("worker", "Unknown")
        cat   = v.get("category", "compliance issue")

        if sent_ts <= threshold_48h and not v.get("followup_24h_sent"):
            needs_48h_escalate.setdefault(wname, []).append(cat)
        elif sent_ts <= threshold_24h and not v.get("followup_24h_sent"):
            uid = name_to_uid.get(wname)
            if uid:
                needs_24h_followup.setdefault(wname, {"uid": uid, "cats": []})["cats"].append(cat)

    # ── 24h worker follow-up ──────────────────────────────────────────────────
    if needs_24h_followup:
        print(f"\n24h follow-up: {len(needs_24h_followup)} worker(s) haven't replied yet.")
        sender_id = int(CONNECTEAM_SENDER_ID or "0")
        conv_map  = load_worker_conversations() if not dry_run else {}
        for wname, info in needs_24h_followup.items():
            first     = wname.split()[0]
            uid       = info["uid"]
            top_cat   = info["cats"][0] if info["cats"] else "the issue I flagged"
            # Rephrase the category into plain English for the follow-up
            cat_plain = (
                top_cat.lower()
                .replace("no clock-in", "missing clock-in")
                .replace("_", " ")
            )
            msg = (
                f"{first} — I messaged you yesterday and haven't heard back. "
                f"This one's important, I need a reply from you today. "
                f"It's about the {cat_plain} — what happened?"
            )
            if dry_run:
                print(f"  [DRY RUN] 24h follow-up to {wname}: {msg[:100]}")
            elif sender_id and CONNECTEAM_API_KEY:
                conv_id = conv_map.get(str(uid))
                sent = False
                if conv_id:
                    try:
                        r = requests.post(
                            f"{BASE_URL}/chat/v1/conversations/{conv_id}/message",
                            headers={"X-API-KEY": CONNECTEAM_API_KEY, "Content-Type": "application/json"},
                            json={"senderId": sender_id, "text": msg},
                            timeout=15,
                        )
                        sent = r.ok
                    except Exception:
                        pass
                if not sent:
                    try:
                        r = requests.post(
                            f"{BASE_URL}/chat/v1/conversations/privateMessage/{uid}",
                            headers={"X-API-KEY": CONNECTEAM_API_KEY, "Content-Type": "application/json"},
                            json={"senderId": sender_id, "text": msg},
                            timeout=15,
                        )
                        sent = r.ok
                    except Exception:
                        pass
                if sent:
                    print(f"  ✓ 24h follow-up sent to {wname}")
                else:
                    print(f"  ✗ 24h follow-up failed for {wname}")
        # Mark so we don't send again
        for fp, v in notified.items():
            if isinstance(v, dict) and v.get("worker") in needs_24h_followup:
                sent_ts = v.get("sent_ts")
                if sent_ts and sent_ts <= threshold_24h and not v.get("followup_24h_sent"):
                    v["followup_24h_sent"] = True
    else:
        print("24h follow-up check: all workers replied or not yet due.")

    # ── 48h manager escalation ────────────────────────────────────────────────
    if not needs_48h_escalate:
        print("48h escalation check: no workers overdue.")
        return

    print(f"\n48h escalation: {len(needs_48h_escalate)} worker(s) still unresponsive.")
    # Send a separate, actionable message per worker so each one is easy to act on
    for wname, cats in needs_48h_escalate.items():
        top_cat   = cats[0] if cats else "a compliance issue"
        cat_plain = top_cat.lower().replace("_", " ")
        msg = (
            f"{wname} hasn't replied to Amy's messages in over 48 hours. "
            f"The flagged issue is: {cat_plain}. "
            f"Can someone give them a call and sort this out directly?"
        )
        if not dry_run:
            sender_id = int(CONNECTEAM_SENDER_ID or "0")
            if sender_id and CONNECTEAM_API_KEY:
                try:
                    requests.post(
                        f"https://api.connecteam.com/chat/v1/conversations/{CC_MGMT_CONV_ID}/message",
                        headers={"X-API-KEY": CONNECTEAM_API_KEY, "Content-Type": "application/json"},
                        json={"senderId": sender_id, "text": msg},
                        timeout=15,
                    )
                    print(f"  Posted 48h escalation for {wname}.")
                except Exception as e:
                    print(f"  [ERROR] Failed to post escalation for {wname}: {e}")
        else:
            print(f"  [DRY RUN] Would post: {msg}")

    for fp, v in notified.items():
        if isinstance(v, dict) and not v.get("acknowledged") and v.get("worker") in needs_48h_escalate:
            sent_ts = v.get("sent_ts")
            if sent_ts and sent_ts <= threshold_48h:
                v["escalated_48h"] = True


# ── Declining note quality check-in ──────────────────────────────────────────

def build_declining_notes_message(worker_first: str, total_issues: int,
                                   recent_count: int, prior_count: int,
                                   history: list = None) -> str:
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    history_block   = _format_history(history, worker_first)
    history_section = f"\nRecent conversation with {worker_first}:\n{history_block}\n" if history_block else ""
    opener_rule     = (
        f"Do NOT open with 'Hi {worker_first},' — there's an ongoing conversation above, pick up naturally."
        if history_block else f"Start with 'Hi {worker_first},'"
    )
    if ANTHROPIC_API_KEY:
        try:
            import anthropic
            client_ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            prompt = (
                f"Write a short, gentle text message from Amy (a coordinator) to a support worker named {worker_first}.\n"
                f"{history_section}\n"
                f"Context:\n"
                f"- The worker has had {total_issues} note-related issues flagged in the last 2 weeks\n"
                f"- In the most recent week: {recent_count} note issues\n"
                f"- In the week before that: {prior_count} note issues\n"
                f"- The pattern is getting worse, not better\n\n"
                f"Rules:\n"
                f"- {opener_rule}\n"
                f"- 2-3 sentences max\n"
                f"- Acknowledge the pattern without being accusatory — be warm and curious, not managerial\n"
                f"- Ask if anything is making notes harder to complete\n"
                f"- Don't repeat anything already covered in the conversation above\n"
                f"- Zero corporate language — no \"it has come to my attention\", \"I am writing to\", \"compliance\"\n"
                f"- No sign-off\n"
                f"- Output just the message, nothing else"
            )
            resp = client_ai.messages.create(
                model="claude-haiku-4-5",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception as e:
            print(f"  [WARNING] Claude declining-notes message generation failed: {e}")

    return (
        f"Hi {worker_first}, I've noticed your shift notes have had a few issues over "
        f"the last couple of weeks — {recent_count} this week vs {prior_count} the week before. "
        "Is there anything making notes harder to get done at the moment? Happy to chat."
    )


def run_declining_notes(now: datetime.datetime, notified: dict,
                         name_to_uid: dict, dry_run: bool,
                         convo_log: dict = None) -> list:
    """
    Find workers with a rising trend of note-quality issues and send a gentle check-in.
    Uses the already-loaded notified dict for both trend analysis and dedup.
    Returns a list of worker names messaged.
    """
    print("\n--- Declining Note Quality Check ---")

    now_ts        = now.timestamp()
    recent_cutoff = now_ts - (7  * 86400)
    prior_cutoff  = now_ts - (14 * 86400)

    recent_counts = defaultdict(int)
    prior_counts  = defaultdict(int)
    total_counts  = defaultdict(int)

    for fp, v in notified.items():
        if not isinstance(v, dict):
            continue
        category = v.get("category", "")
        worker   = v.get("worker", "")
        if not worker or worker.lower() in {"(team)", "unknown", ""}:
            continue
        if category not in NOTE_QUALITY_CATEGORIES:
            continue
        if fp.startswith("declining_notes|"):
            continue

        sent_ts = v.get("sent_ts")
        if not sent_ts:
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

    declining = [
        (worker, total, recent_counts.get(worker, 0), prior_counts.get(worker, 0))
        for worker, total in total_counts.items()
        if total >= 3 and recent_counts.get(worker, 0) > prior_counts.get(worker, 0)
    ]

    print(f"  {len(declining)} worker(s) with declining note quality trend.")
    if not declining:
        return []

    uid_to_user = {}
    try:
        users = fetch_all_users()
        uid_to_user = {str(uid): u for uid, u in users.items()}
    except Exception as e:
        print(f"  [WARN] Could not fetch users for declining notes: {e}")

    messaged = []
    for worker_name, total, recent, prior in declining:
        uid = name_to_uid.get(worker_name)
        if not uid:
            print(f"  [SKIP] {worker_name} — no UID found")
            continue

        dedup_key = f"declining_notes|{uid}"
        if dedup_key in notified:
            last_sent = notified[dedup_key].get("date", "")
            try:
                last_dt = datetime.datetime.strptime(last_sent, "%Y-%m-%d").replace(tzinfo=AEST)
                if (now - last_dt).days < DECLINING_DEDUP_DAYS:
                    print(f"  [SKIP] {worker_name} — check-in sent {(now - last_dt).days}d ago")
                    continue
            except ValueError:
                pass

        user         = uid_to_user.get(str(uid), {})
        worker_first = user.get("firstName", worker_name.split()[0]) or worker_name.split()[0]

        worker_history = (convo_log or {}).get(str(uid), [])
        if isinstance(worker_history, dict):
            worker_history = worker_history.get("messages", [])
        print(f"  Generating check-in for {worker_name} (total={total}, recent={recent}, prior={prior}, history={len(worker_history)} turns)...")
        message = build_declining_notes_message(worker_first, total, recent, prior, history=worker_history)

        if dry_run:
            print(f"  [DRY RUN] Would send to {worker_name}: {message[:120]}")
            notified[dedup_key] = {"date": now.strftime("%Y-%m-%d"), "worker": worker_name, "category": "declining_notes"}
            messaged.append(worker_name)
        else:
            ok, result = send_worker_message(uid, message, worker_name=worker_name)
            if ok:
                print(f"  ✓ Check-in sent to {worker_name}")
                notified[dedup_key] = {
                    "date":     now.strftime("%Y-%m-%d"),
                    "worker":   worker_name,
                    "category": "declining_notes",
                    "sent_ts":  int(now.timestamp()),
                }
                messaged.append(worker_name)
                time.sleep(1)
            else:
                print(f"  ✗ Failed to send to {worker_name}: {result}")

    return messaged


# ── Client-level risk rollup ──────────────────────────────────────────────────

def _post_client_risk_summary(issues: list, now: datetime.datetime, dry_run: bool):
    """Alert CC Management when a single client has 3+ issues from 2+ different workers today."""
    by_client = defaultdict(lambda: defaultdict(list))
    for iss in issues:
        if not iss.client or iss.client.lower() in {"(team)", "unknown", ""}:
            continue
        if iss.severity not in {"CRITICAL", "HIGH"}:
            continue
        by_client[iss.client][iss.worker].append(iss)

    alerts = [
        (client, workers_map)
        for client, workers_map in by_client.items()
        if sum(len(v) for v in workers_map.values()) >= 3 and len(workers_map) >= 2
    ]

    if not alerts:
        return

    for client, workers_map in alerts:
        total = sum(len(v) for v in workers_map.values())
        lines = [f"⚠️ Client risk — {client}: {total} issues across {len(workers_map)} workers today."]
        for worker, worker_issues in sorted(workers_map.items()):
            cats = ", ".join(i.category for i in worker_issues[:3])
            lines.append(f"  • {worker}: {cats}")
        msg = "\n".join(lines)
        print(f"  Client risk alert: {client} ({total} issues, {len(workers_map)} workers)")
        if not dry_run:
            post_to_management(msg)
        else:
            print(f"  [DRY RUN] {msg}")


# ── GPS pattern counter ────────────────────────────────────────────────────────

def _count_gps_mismatches(worker_name: str, client: str, notified: dict) -> int:
    """Count GPS mismatch notifications for this worker+client in the last 30 days."""
    cutoff_ts = (datetime.datetime.now(AEST) - datetime.timedelta(days=30)).timestamp()
    count = 0
    for v in notified.values():
        if not isinstance(v, dict):
            continue
        if (v.get("worker") == worker_name
                and v.get("category") == "GPS MISMATCH"
                and v.get("client") == client
                and v.get("sent_ts", 0) >= cutoff_ts):
            count += 1
    return count


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Random delay so messages don't always land at the exact same time each day
    delay = random.randint(0, 25 * 60)  # 0–25 minutes in seconds
    print(f"Waiting {delay // 60}m {delay % 60}s before sending (randomised)...")
    time.sleep(delay)

    now        = datetime.datetime.now(AEST)
    run_label  = now.strftime("%a %d %b, %I:%M %p AEST")
    dry_run    = not bool(CONNECTEAM_SENDER_ID)

    print(f"\n{'='*60}")
    print(f"Connect Care Shift Compliance Notifier — {run_label}")
    print(f"Auditing last {DAYS_BACK} day(s) | dry_run={dry_run}")
    print(f"{'='*60}\n")

    # Safety guard — never send worker messages outside 6 AM – 7 PM AEST
    hour = now.hour
    if hour >= 19 or hour < 6:
        print(f"[QUIET HOURS] It is {now.strftime('%I:%M %p AEST')} — no worker messages sent outside 6 AM–7 PM. Exiting.")
        return

    notified = load_notified()
    print(f"Loaded {len(notified)} existing fingerprints from dedup cache.\n")

    # ── Check for 48h no-reply escalations before running new audit ──────────
    check_unacknowledged_escalations(now, notified, dry_run)

    issues = run_audit(DAYS_BACK)

    # ── Auto-resolve: mark notified issues as resolved if they're no longer flagged ──
    current_fps = {issue_fingerprint(i) for i in issues}
    resolved_by_worker = defaultdict(list)  # worker_name -> [category, ...]
    for fp, v in notified.items():
        if isinstance(v, dict) and not v.get("acknowledged") and not v.get("pending"):
            if fp not in current_fps:
                v["acknowledged"] = True
                v["resolved_date"] = now.strftime("%Y-%m-%d")
                resolved_by_worker[v.get("worker", "")].append(v.get("category", ""))

    if resolved_by_worker:
        print(f"Auto-resolved issues for {len(resolved_by_worker)} worker(s) — sending close-the-loop messages.\n")
        sender_id = int(CONNECTEAM_SENDER_ID or "0")
        conv_map  = load_worker_conversations() if not dry_run else {}
        # Re-use the already-loaded users dict after it's built below — build it now
        _resolve_users = fetch_all_users()
        _resolve_uid_map = {
            f"{u.get('firstName','')} {u.get('lastName','')}".strip(): uid
            for uid, u in _resolve_users.items()
        }
        for wname, cats in resolved_by_worker.items():
            if not wname:
                continue
            uid = _resolve_uid_map.get(wname)
            if not uid:
                continue
            first = wname.split()[0]
            msg   = f"Hey {first}, looks like that's all sorted now — appreciate you getting on it."
            if dry_run:
                print(f"  [DRY RUN] Close-the-loop to {wname}: {msg}")
                continue
            if not sender_id or not CONNECTEAM_API_KEY:
                continue
            conv_id = conv_map.get(str(uid))
            sent = False
            if conv_id:
                try:
                    r = requests.post(
                        f"{BASE_URL}/chat/v1/conversations/{conv_id}/message",
                        headers={"X-API-KEY": CONNECTEAM_API_KEY, "Content-Type": "application/json"},
                        json={"senderId": sender_id, "text": msg},
                        timeout=15,
                    )
                    sent = r.ok
                except Exception:
                    pass
            if sent:
                print(f"  ✓ Close-the-loop sent to {wname}")
            else:
                print(f"  ✗ Close-the-loop failed for {wname}")

    # Build user ID lookup
    users        = fetch_all_users()
    name_to_uid  = {
        f"{u.get('firstName','')} {u.get('lastName','')}".strip(): uid
        for uid, u in users.items()
    }

    # Separate worker issues from team-level issues
    worker_new_issues = defaultdict(list)   # worker_name -> [Issue, ...]
    cred_new_issues   = defaultdict(list)   # worker_name -> [Issue, ...] (credential only)
    team_new_issues   = []

    TEAM_NAMES = {"(team)", "unknown", ""}

    # Credential dedup — expired credentials re-notify daily, expiring-soon weekly
    cutoff_expired = (now - datetime.timedelta(days=CRED_DEDUP_DAYS_EXPIRED)).strftime("%Y-%m-%d")
    cutoff_soon    = (now - datetime.timedelta(days=CRED_DEDUP_DAYS_SOON)).strftime("%Y-%m-%d")
    recent_cred_fps = {
        fp for fp, v in notified.items()
        if v.get("cred") and v.get("date", "") >= (
            cutoff_expired if v.get("cred_type") == "EXPIRED CREDENTIAL" else cutoff_soon
        )
    }

    # Pending amendment dedup — 7-day window so worker/management aren't re-notified
    # every 2 days while an amendment sits waiting for approval
    cutoff_pending = (now - datetime.timedelta(days=PENDING_DEDUP_DAYS)).strftime("%Y-%m-%d")
    recent_pending_fps = {
        fp for fp, v in notified.items()
        if v.get("pending") and v.get("date", "") >= cutoff_pending
    }

    for iss in issues:
        if iss.severity not in NOTIFY_SEVERITIES and not (
            iss.severity == "MEDIUM" and iss.category in (
                CRED_CATEGORIES | {"UNSCHEDULED SHIFT"} | PENDING_CATEGORIES
            )
        ):
            continue
        if iss.category in SKIP_CATEGORIES:
            continue

        fp = issue_fingerprint(iss)

        if iss.category in CRED_CATEGORIES:
            if fp not in recent_cred_fps:
                cred_new_issues[iss.worker].append(iss)
            continue  # credentials handled separately below

        if iss.category in PENDING_CATEGORIES:
            if fp in recent_pending_fps:
                continue  # already notified within last 7 days
        elif fp in notified:
            continue  # already messaged

        if iss.category in MANAGEMENT_ONLY_CATEGORIES or iss.worker.lower() in TEAM_NAMES:
            team_new_issues.append(iss)
        else:
            worker_new_issues[iss.worker].append(iss)

    print(f"New worker-level issues: {sum(len(v) for v in worker_new_issues.values())} "
          f"across {len(worker_new_issues)} worker(s)")
    print(f"New credential issues:   {sum(len(v) for v in cred_new_issues.values())} "
          f"across {len(cred_new_issues)} worker(s)")
    print(f"New team-level issues:   {len(team_new_issues)}\n")

    # ── Message each worker ───────────────────────────────────────────────────
    sent_ok    = []
    sent_err   = []
    convo_log  = load_convo_log()
    conv_map   = load_worker_conversations()  # uid -> conversation_id

    for worker_name, worker_issues in sorted(worker_new_issues.items()):
        uid = name_to_uid.get(worker_name)
        if not uid:
            print(f"  [SKIP] {worker_name} — no user ID found in Connecteam")
            continue

        # Pending amendment notices are not compliance failures — exclude from strike count
        # so they never trigger a sterner tone
        strike_counts = {
            iss.category: (0 if iss.category in PENDING_CATEGORIES
                           else _get_strike_count(worker_name, iss.category))
            for iss in worker_issues
        }
        max_strike = max(
            (v for cat, v in strike_counts.items() if cat not in PENDING_CATEGORIES),
            default=0
        )
        history       = convo_log.get(str(uid), [])
        if isinstance(history, dict):
            history = history.get("messages", [])
        print(f"  Generating message for {worker_name} ({len(worker_issues)} issues, max strikes={max_strike}, history={len(history)} turns)...")
        message = build_worker_message(worker_name, worker_issues, strike_counts, history=history)

        if dry_run:
            print(f"  [DRY RUN] Would send to {worker_name}:\n{message[:200]}...\n")
            sent_ok.append(worker_name)
            for iss in worker_issues:
                fp = issue_fingerprint(iss)
                notified[fp] = {"date": now.strftime("%Y-%m-%d"), "worker": worker_name, "category": iss.category, "client": iss.client or "", "sent_ts": int(now.timestamp())}
        else:
            ok, result = send_worker_message(uid, message, worker_name=worker_name)
            if ok:
                print(f"  ✓ Sent to {worker_name}")
                sent_ok.append(worker_name)
                issue_summary = "; ".join(
                    f"{i.category} ({i.client or 'N/A'})" for i in worker_issues[:3]
                )
                for iss in worker_issues:
                    fp = issue_fingerprint(iss)
                    notified[fp] = {
                        "date": now.strftime("%Y-%m-%d"), "worker": worker_name,
                        "sent_ts": int(now.timestamp()), "acknowledged": False,
                        "category": iss.category, "client": iss.client or "",
                        "pending": iss.category in PENDING_CATEGORIES,
                    }
                # Write to 30-day strike log before counting repeats
                notifiable_cats = [
                    iss.category for iss in worker_issues
                    if iss.category not in PENDING_CATEGORIES
                ]
                _append_strikes(worker_name, notifiable_cats, now.strftime("%Y-%m-%d"))

                # Repeat offender alert to CC Management (3rd+ strike per category)
                repeat_cats = [cat for cat, count in strike_counts.items() if count >= 2]
                if repeat_cats:
                    repeat_msg = (
                        f"Repeat offender — {worker_name} has been flagged 3+ times for: "
                        f"{', '.join(repeat_cats)}. Amy has sent a firm message. Consider a formal warning."
                    )
                    post_to_management(repeat_msg)

                # Strike threshold escalation (Fix #3): 5+ total strikes → formal review alert
                STRIKE_ESCALATION_THRESHOLD = 5
                total_30d_strikes = sum(1 for e in _load_strike_log() if e.get("worker") == worker_name)
                if total_30d_strikes >= STRIKE_ESCALATION_THRESHOLD:
                    esc_fp   = f"strike_esc|{worker_name}"
                    week_ago = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
                    if esc_fp not in notified or notified[esc_fp].get("date", "") < week_ago:
                        post_to_management(
                            f"🔴 High strike count — {worker_name} has accumulated "
                            f"{total_30d_strikes} compliance strikes in the last 30 days. "
                            f"Amy's messages alone aren't resolving this. Recommend a direct "
                            f"management conversation or formal performance review."
                        )
                        notified[esc_fp] = {
                            "date":     now.strftime("%Y-%m-%d"),
                            "worker":   worker_name,
                            "category": "strike_escalation",
                            "sent_ts":  int(now.timestamp()),
                        }

                # GPS pattern annotation (Fix #5): include repeat GPS count in manager summary
                gps_issues = [i for i in worker_issues if i.category == "GPS MISMATCH"]
                for gps_iss in gps_issues:
                    prior = _count_gps_mismatches(worker_name, gps_iss.client or "", notified)
                    if prior >= 2:
                        post_to_management(
                            f"📍 GPS pattern — {worker_name} has now had {prior + 1} GPS mismatch "
                            f"alerts at {gps_iss.client or 'same client'} in the last 30 days. "
                            f"This is likely a consistent clock-in location issue worth investigating."
                        )

                # Unscheduled shift alert to CC Management
                unscheduled = [i for i in worker_issues if i.category == "UNSCHEDULED SHIFT"]
                for u in unscheduled:
                    post_to_management(
                        f"📋 Unscheduled shift — {worker_name} clocked in at {u.client} on {u.date} "
                        f"with no roster entry. Amy has asked them for an explanation. "
                        f"Once they reply, approve or reject the hours."
                    )

                # Save to conversation log so smart reply has context
                convo_log[str(uid)] = {
                    "worker_name":     worker_name,
                    "conversation_id": conv_map.get(str(uid), ""),
                    "messages": [{"sender": "amy", "text": message, "ts": int(now.timestamp())}],
                }
                # Update persistent worker profile so Amy remembers this issue
                _update_worker_profile_gh(str(uid), {
                    "worker_name":       worker_name,
                    "last_issue_date":   now.strftime("%d %b %Y"),
                    "last_issue_summary": issue_summary,
                    "open_issues": [i.category for i in worker_issues if i.severity == "CRITICAL"],
                })
            else:
                print(f"  ✗ Failed {worker_name}: {result}")
                sent_err.append(worker_name)

    save_convo_log(convo_log)

    # ── Client-level risk rollup (Fix #4) ────────────────────────────────────
    _post_client_risk_summary(issues, now, dry_run)

    # ── Credential expiry notifications ───────────────────────────────────────
    cred_sent = []
    for worker_name, cred_issues in sorted(cred_new_issues.items()):
        uid = name_to_uid.get(worker_name)
        if not uid:
            print(f"  [SKIP] {worker_name} — no user ID for credential notice")
            continue
        cred_history = convo_log.get(str(uid), [])
        if isinstance(cred_history, dict):
            cred_history = cred_history.get("messages", [])
        msg = build_credential_message(worker_name, cred_issues, history=cred_history)
        if dry_run:
            print(f"  [DRY RUN] Would send credential notice to {worker_name}")
            cred_sent.append(worker_name)
        else:
            ok, result = send_worker_message(uid, msg, worker_name=worker_name)
            if ok:
                print(f"  ✓ Credential notice sent to {worker_name}")
                cred_sent.append(worker_name)
                for iss in cred_issues:
                    fp = issue_fingerprint(iss)
                    notified[fp] = {"date": now.strftime("%Y-%m-%d"), "worker": worker_name, "cred": True, "cred_type": iss.category, "category": iss.category, "client": iss.client or "", "sent_ts": int(now.timestamp())}
            else:
                print(f"  ✗ Credential notice failed for {worker_name}: {result}")

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
    if cred_sent:
        summary_lines.append(f"\nCredential notices sent to: {', '.join(cred_sent)}")

    if team_new_issues:
        summary_lines.append(f"\nTeam issues (not sent to workers):")
        # Pending amendments get their full detail shown (worker name + date) so they're actionable
        pending_alerts = [i for i in team_new_issues if i.category == "PENDING AMENDMENT -- REVIEW REQUIRED"]
        other_issues   = [i for i in team_new_issues if i.category != "PENDING AMENDMENT -- REVIEW REQUIRED"]
        if pending_alerts:
            summary_lines.append("Pending amendments — action required in Connecteam:")
            for iss in pending_alerts:
                summary_lines.append(f"  ⏳ {iss.detail}")
        if other_issues:
            by_client = defaultdict(list)
            for iss in other_issues:
                by_client[iss.client or "N/A"].append(iss.category)
            for client, cats in by_client.items():
                summary_lines.append(f"- {client}: {', '.join(cats)}")
        for iss in team_new_issues:
            fp = issue_fingerprint(iss)
            notified[fp] = {"date": now.strftime("%Y-%m-%d"), "worker": iss.worker, "category": iss.category, "client": iss.client or "", "pending": iss.category in PENDING_CATEGORIES, "sent_ts": int(now.timestamp())}

    has_issues = bool(sent_ok or sent_err or team_new_issues or cred_sent)

    if has_issues:
        mgmt_msg = "\n".join(summary_lines)
        print(f"\nPosting summary to CC Management...")
        print(f"  {mgmt_msg[:200]}")
        post_to_management(mgmt_msg)
    else:
        print(f"\nAll clear — no new issues found. Skipping CC Management post.")

    # ── Save dedup state ──────────────────────────────────────────────────────
    save_notified(notified)
    print(f"\nDedup cache updated: {len(notified)} fingerprints saved.")

    # ── Credential 3-day follow-up ────────────────────────────────────────────
    _check_credential_followups(now, notified, name_to_uid, conv_map, dry_run)

    # ── Lock time entries older than 3 days (prevents backdating) ────────────
    _lock_prior_days(name_to_uid, now)

    # ── Add permanent profile notes for CRITICAL breaches ────────────────────
    if not dry_run:
        _add_critical_profile_notes(issues, name_to_uid, now)

    # ── Invoice reconciliation — once per pay period only (Fix #7 dedup) ────────
    try:
        from invoice_check import reconcile, build_report, current_pay_period
        start_date, end_date = current_pay_period()
        inv_fp = f"invoice_report|{start_date}|{end_date}"
        if inv_fp in notified:
            print("Invoice reconciliation: already posted for this pay period, skipping.")
        else:
            inv_results = reconcile(start_date, end_date)
            flagged     = [r for r in inv_results if r["flags"]]
            if flagged:
                inv_report = build_report(inv_results, start_date, end_date)
                post_to_management(inv_report)
                notified[inv_fp] = {
                    "date":     now.strftime("%Y-%m-%d"),
                    "category": "invoice_report",
                    "worker":   "(team)",
                    "sent_ts":  int(now.timestamp()),
                }
                print(f"Invoice reconciliation: {len(flagged)} worker(s) flagged — posted.")
            else:
                print("Invoice reconciliation: all workers clear for current pay period.")
    except Exception as e:
        print(f"  [WARN] Invoice reconciliation skipped: {e}")

    # ── Declining note quality check-in ──────────────────────────────────────
    declining_messaged = run_declining_notes(now, notified, name_to_uid, dry_run, convo_log=convo_log)
    if declining_messaged:
        post_to_management(
            f"Note quality check-ins sent to: {', '.join(declining_messaged)}"
        )
    save_notified(notified)  # re-save with declining_notes dedup keys included

    print(f"\n{'='*60}")
    print(f"Done — {len(sent_ok)} shift + {len(cred_sent)} credential sent, {len(sent_err)} failed")
    if declining_messaged:
        print(f"       {len(declining_messaged)} declining-notes check-in(s) sent")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
