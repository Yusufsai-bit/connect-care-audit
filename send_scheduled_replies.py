"""
One-off scheduled reply sender — runs via GitHub Actions at 7:47 AM AEST 17 Jun 2026.
Sends pre-approved replies to Mohamed and Peter.
"""

import os
import sys
import json
import requests

API_KEY   = os.environ["CONNECTEAM_API_KEY"]
SENDER_ID = int(os.environ["CONNECTEAM_SENDER_ID"])
BASE_URL  = "https://api.connecteam.com"

MESSAGES = {
    "Mohamed Liban": (
        "Hey Mohamed, the gap makes sense. The flagged ones are specifically the Kallan Jordan "
        "notes from the 16th — two came through looking nearly identical, can you resubmit those? "
        "Notes from the 14th onwards are looking good though."
    ),
    "Peter": (
        "Thanks for clarifying Peter. Going forward, only clock in once you're actually at the "
        "client's house — if Joshua's mum asks you to come later or leave earlier than scheduled, "
        "just let me know so I can update the records. Makes things a lot easier on the payroll side."
    ),
}


def get_users():
    r = requests.get(
        f"{BASE_URL}/users/v1/users",
        headers={"X-API-KEY": API_KEY},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("data", {}).get("users", [])


def find_user(name_fragment, users):
    nl = name_fragment.lower()
    for u in users:
        full = f"{u.get('firstName','')} {u.get('lastName','')}".strip().lower()
        if nl in full:
            return u
    return None


def send_private(user_id, text):
    r = requests.post(
        f"{BASE_URL}/chat/v1/conversations/privateMessage/{user_id}",
        headers={"X-API-KEY": API_KEY, "Content-Type": "application/json"},
        json={"senderId": SENDER_ID, "text": text},
        timeout=15,
    )
    return r.ok, r.status_code


def main():
    users = get_users()
    failed = []

    for name, msg in MESSAGES.items():
        u = find_user(name, users)
        if not u:
            print(f"ERROR: could not find user '{name}'")
            failed.append(name)
            continue
        uid = u.get("id") or u.get("userId")
        ok, code = send_private(uid, msg)
        if ok:
            print(f"Sent to {name} (id={uid})")
        else:
            print(f"FAILED for {name} (id={uid}) — HTTP {code}")
            failed.append(name)

    if failed:
        print(f"\nFailed: {failed}")
        sys.exit(1)
    print("\nAll messages sent.")


if __name__ == "__main__":
    main()
