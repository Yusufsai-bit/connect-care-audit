"""
Amy Smart Reply — Webhook Server
Receives Connecteam "Chat message created" events, reads conversation context,
and generates a reply via Claude Haiku.

Deploy on Render: uvicorn amy_webhook:app --host 0.0.0.0 --port $PORT
"""

import os, json, time, base64, logging, requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
CONNECTEAM_API_KEY   = os.environ.get("CONNECTEAM_API_KEY", "")
CONNECTEAM_SENDER_ID = os.environ.get("CONNECTEAM_SENDER_ID", "")
ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN         = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO          = os.environ.get("GITHUB_REPO", "Yusufsai-bit/connect-care-audit")
CC_MGMT_CONV_ID      = os.environ.get("CC_MGMT_CONV_ID", "4a14c09d-bc9f-46f2-9ad9-a728d6ddcbf6")
BASE_URL             = "https://api.connecteam.com"
CONVO_LOG_FILE       = "amy_conversation_log.json"
CONVO_EXPIRY_DAYS    = 7

conversation_log: dict = {}

app = FastAPI()


# ── GitHub persistence ─────────────────────────────────────────────────────────

def load_from_github() -> dict:
    try:
        r = requests.get(
            f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{CONVO_LOG_FILE}",
            timeout=10,
        )
        if r.ok:
            data = r.json()
            # Expire entries older than CONVO_EXPIRY_DAYS
            cutoff = time.time() - CONVO_EXPIRY_DAYS * 86400
            return {
                uid: v for uid, v in data.items()
                if v.get("messages") and v["messages"][-1].get("ts", 0) >= cutoff
            }
    except Exception as e:
        logger.warning(f"Could not load conversation log from GitHub: {e}")
    return {}


def save_to_github(data: dict):
    if not GITHUB_TOKEN:
        logger.warning("GITHUB_TOKEN not set — skipping GitHub persist")
        return
    try:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CONVO_LOG_FILE}",
            headers={"Authorization": f"token {GITHUB_TOKEN}"},
            timeout=10,
        )
        sha = r.json().get("sha", "") if r.ok else ""
        encoded = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
        requests.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CONVO_LOG_FILE}",
            headers={"Authorization": f"token {GITHUB_TOKEN}", "Content-Type": "application/json"},
            json={
                "message": "chore: update amy conversation log [skip ci]",
                "content": encoded,
                "sha": sha,
            },
            timeout=15,
        )
        logger.info("Conversation log saved to GitHub")
    except Exception as e:
        logger.error(f"Failed to save to GitHub: {e}")


# ── Startup ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    global conversation_log
    conversation_log = load_from_github()
    logger.info(f"Loaded {len(conversation_log)} worker conversation(s) from GitHub")


# ── Reply generation ───────────────────────────────────────────────────────────

def generate_reply(worker_first: str, history: str) -> str:
    if not ANTHROPIC_API_KEY:
        return "Got it, thanks for letting me know."
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            messages=[{"role": "user", "content": f"""You are Amy, a coordinator at Connect Care (an NDIS disability support provider).
You previously sent a compliance message to {worker_first} about issues from their shift.

Conversation so far:
{history}

Reply as Amy. Rules:
- Casual and friendly — like texting a colleague
- If they're explaining something, acknowledge it and say what needs to change going forward
- If they say they've fixed it or will fix it, say great and confirm what you need to see (e.g. "just make sure it's in the system")
- If they ask a question, answer it helpfully and briefly
- If they mention anything serious (incident, injury, conflict, safety issue), say you'll follow up and let the manager know
- 1-3 sentences max — keep it short
- No sign-off, no corporate language
- Output just the message, nothing else"""}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude reply failed: {e}")
        return "Got it, thanks for letting me know."


# ── Connecteam helpers ────────────────────────────────────────────────────────

def get_worker_name(user_id: str) -> str:
    """Look up a worker's name from Connecteam. Returns 'Unknown' on failure."""
    try:
        r = requests.get(
            f"{BASE_URL}/users/v1/users/{user_id}",
            headers={"X-API-KEY": CONNECTEAM_API_KEY},
            timeout=10,
        )
        if r.ok:
            u = (r.json().get("data") or {}).get("user") or r.json().get("data") or {}
            first = u.get("firstName", "")
            last  = u.get("lastName", "")
            name  = f"{first} {last}".strip()
            return name or f"User {user_id}"
    except Exception:
        pass
    return f"User {user_id}"


def send_message(conv_id: str, text: str) -> bool:
    if not CONNECTEAM_API_KEY or not CONNECTEAM_SENDER_ID:
        logger.warning("Connecteam credentials missing")
        return False
    try:
        r = requests.post(
            f"{BASE_URL}/chat/v1/conversations/{conv_id}/message",
            headers={"X-API-KEY": CONNECTEAM_API_KEY, "Content-Type": "application/json"},
            json={"senderId": int(CONNECTEAM_SENDER_ID), "text": text[:4000]},
            timeout=15,
        )
        return r.ok
    except Exception as e:
        logger.error(f"Send message failed: {e}")
        return False


# ── Webhook endpoint ───────────────────────────────────────────────────────────

@app.post("/webhook/connecteam")
async def handle_webhook(request: Request):
    global conversation_log
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = payload.get("eventType", "")
    logger.info(f"Webhook received: {event_type}")

    if event_type != "chat_message_created":
        return JSONResponse({"status": "ignored"})

    data         = payload.get("data", {})
    sender_id    = str(data.get("senderId") or data.get("userId") or data.get("senderUserId") or "")
    conv_id      = str(data.get("conversationId") or data.get("conversation_id") or "")
    message_text = str(data.get("text") or data.get("content") or data.get("message") or "").strip()

    # Ignore Amy's own messages to prevent infinite loops
    if sender_id == str(CONNECTEAM_SENDER_ID):
        return JSONResponse({"status": "self_message"})

    # If sender not in memory, reload from GitHub in case audit ran since startup
    if sender_id not in conversation_log:
        conversation_log = load_from_github()
        logger.info(f"Reloaded conversation log from GitHub: {len(conversation_log)} workers")

    if sender_id not in conversation_log:
        # No compliance context — acknowledge the worker and flag to CC Management
        worker_name = get_worker_name(sender_id)
        logger.info(f"No context for {worker_name} ({sender_id}) — escalating to CC Management")
        send_message(conv_id, "Got it, give me a sec.")
        send_message(
            CC_MGMT_CONV_ID,
            f"{worker_name} just messaged Amy:\n\n\"{message_text}\"\n\nNo compliance context on file — what should she reply?"
        )
        return JSONResponse({"status": "escalated_to_management"})

    if not message_text:
        return JSONResponse({"status": "empty_message"})

    convo        = conversation_log[sender_id]
    worker_name  = convo.get("worker_name", "worker")
    worker_first = worker_name.split()[0]

    # Append worker's message
    convo["messages"].append({"sender": "worker", "text": message_text, "ts": int(time.time())})

    # Build history for Claude (last 10 messages)
    history = "\n".join(
        f"{'Amy' if m['sender'] == 'amy' else worker_name}: {m['text']}"
        for m in convo["messages"][-10:]
    )

    reply = generate_reply(worker_first, history)

    target_conv_id = convo.get("conversation_id") or conv_id
    if not target_conv_id:
        logger.error(f"No conversation_id for user {sender_id}")
        return JSONResponse({"status": "no_conv_id"})

    ok = send_message(target_conv_id, reply)
    if ok:
        convo["messages"].append({"sender": "amy", "text": reply, "ts": int(time.time())})
        save_to_github(conversation_log)
        logger.info(f"✓ Replied to {worker_name}: {reply[:80]}")
        return JSONResponse({"status": "replied"})
    else:
        logger.error(f"Failed to send reply to {worker_name}")
        return JSONResponse({"status": "send_failed"})


@app.get("/health")
async def health():
    return {"status": "ok", "workers_tracked": len(conversation_log)}
