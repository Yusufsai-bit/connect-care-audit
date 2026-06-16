#!/usr/bin/env python3
"""
Connect Care — Invoice Reconciliation Against Schedule
======================================================
Compares each worker's scheduled shifts against their clock data for a pay period.
Workers bill according to their schedule — so the check is:
  - Scheduled but never clocked in → NOT billable
  - Clocked in but GPS shows wrong location → flag for review
  - Clocked but time wildly off from schedule → flag for review
  - Unscheduled shift clocked → needs prior approval

Outputs a per-worker billing summary to CC Management.

Run manually:
    python invoice_check.py                      # current pay period
    python invoice_check.py 2026-06-01 2026-06-15  # specific date range

GitHub Actions: runs Monday 8 AM AEST (start of each work week).
"""

import os, sys, json, datetime, math, requests
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from connecteam_audit import (
    fetch_all_users, fetch_scheduled_shifts,
    CONNECTEAM_API_KEY, CONNECTEAM_SENDER_ID,
    TIME_CLOCK_ID, SCHEDULER_ID, AEST,
    ct_get, haversine_km,
)

# ── Config ─────────────────────────────────────────────────────────────────────

CC_MGMT_CONV_ID  = os.environ.get("CC_MGMT_CONV_ID", "")
BASE_URL         = "https://api.connecteam.com"
GPS_THRESHOLD_KM = 0.5
TIME_TOLERANCE_H = 0.5    # 30 min — clock within this of scheduled = fine
OBSERVER_IDS     = {2149475, 9736871, 2201497}

# ── Helpers ────────────────────────────────────────────────────────────────────

def _aest_now():
    return datetime.datetime.now(AEST)


def current_pay_period():
    """Return (start_date, end_date) for the current fortnight pay period."""
    today = datetime.date.today()
    if today.day <= 15:
        return datetime.date(today.year, today.month, 1), datetime.date(today.year, today.month, 15)
    else:
        import calendar
        last = calendar.monthrange(today.year, today.month)[1]
        return datetime.date(today.year, today.month, 16), datetime.date(today.year, today.month, last)


def ts(dt): return int(dt.timestamp())
def fmt_h(h): return f"{int(h)}h {round((h % 1) * 60)}m" if h else "0h"
def fmt_ts(t): return datetime.datetime.fromtimestamp(t, tz=AEST).strftime("%d %b %H:%M")


def fetch_clock_data(start_date: datetime.date, end_date: datetime.date) -> dict:
    """Returns {user_id: [shift_dict, ...]} for the period."""
    data = ct_get(
        f"/time-clock/v1/time-clocks/{TIME_CLOCK_ID}/time-activities",
        {"startDate": start_date.isoformat(), "endDate": end_date.isoformat()},
    )
    by_user = (data.get("data") or {}).get("timeActivitiesByUsers") or []
    result = {}
    for entry in by_user:
        uid = str(entry.get("userId", ""))
        result[uid] = entry.get("shifts") or []
    return result


def fetch_jobs() -> dict:
    """Returns {job_id: job_dict}."""
    data = ct_get("/jobs/v1/jobs", {"limit": 200})
    jobs = (data.get("data") or {}).get("jobs") or []
    return {str(j.get("id", "")): j for j in jobs}


def post_to_management(text: str):
    sender_id = int(CONNECTEAM_SENDER_ID or "0")
    if not sender_id or not CONNECTEAM_API_KEY or not CC_MGMT_CONV_ID:
        print(f"[DRY RUN] CC Management:\n{text[:300]}\n")
        return
    parts = [text[i:i+3900] for i in range(0, len(text), 3900)]
    for part in parts:
        try:
            requests.post(
                f"{BASE_URL}/chat/v1/conversations/{CC_MGMT_CONV_ID}/message",
                headers={"X-API-KEY": CONNECTEAM_API_KEY, "Content-Type": "application/json"},
                json={"senderId": sender_id, "text": part},
                timeout=15,
            )
        except Exception as e:
            print(f"[ERROR] CC Management post failed: {e}")


# ── Core reconciliation ────────────────────────────────────────────────────────

def reconcile(start_date: datetime.date, end_date: datetime.date):
    start_ts = ts(datetime.datetime.combine(start_date, datetime.time.min).replace(tzinfo=AEST))
    end_ts   = ts(datetime.datetime.combine(end_date,   datetime.time.max).replace(tzinfo=AEST))

    print(f"Fetching data for {start_date} – {end_date}...")
    users    = fetch_all_users()          # {uid: user_dict}
    shifts   = fetch_scheduled_shifts(start_ts, end_ts)
    clocks   = fetch_clock_data(start_date, end_date)
    jobs     = fetch_jobs()

    def uname(uid):
        u = users.get(str(uid)) or users.get(uid) or {}
        return f"{u.get('firstName','')} {u.get('lastName','')}".strip() or f"User {uid}"

    def jname(jid):
        j = jobs.get(str(jid)) or {}
        return j.get("title") or j.get("name") or f"Client {jid}"

    def job_gps(jid):
        j = jobs.get(str(jid)) or {}
        gps = j.get("gps") or {}
        return gps.get("latitude", 0), gps.get("longitude", 0)

    # Group scheduled shifts by worker
    sched_by_worker = {}  # uid -> [shift]
    for shift in shifts:
        for uid in (shift.get("assignedUserIds") or []):
            uid = str(uid)
            if int(uid) in OBSERVER_IDS:
                continue
            sched_by_worker.setdefault(uid, []).append(shift)

    results = []  # list of worker result dicts

    all_uids = sorted(set(list(sched_by_worker.keys()) + list(clocks.keys())))

    for uid in all_uids:
        if int(uid) in OBSERVER_IDS:
            continue
        name           = uname(uid)
        worker_sched   = sched_by_worker.get(uid, [])
        worker_clocks  = clocks.get(uid, [])

        scheduled_h    = 0.0
        billable_h     = 0.0
        unbillable_h   = 0.0
        flags          = []

        # Index clock entries by approximate start time for matching
        clock_used = set()

        for shift in worker_sched:
            sched_start = shift.get("startTime", 0)
            sched_end   = shift.get("endTime", 0)
            sched_dur_h = (sched_end - sched_start) / 3600
            scheduled_h += sched_dur_h
            job_id       = str(shift.get("jobId") or "")
            client       = jname(job_id)
            shift_label  = f"{fmt_ts(sched_start)} at {client}"

            # Find matching clock entry (within 4h of scheduled start)
            matched = None
            for i, ck in enumerate(worker_clocks):
                if i in clock_used:
                    continue
                ck_start = (ck.get("start") or {}).get("timestamp", 0)
                if abs(ck_start - sched_start) < 14400:  # 4h window
                    matched = ck
                    clock_used.add(i)
                    break

            if not matched:
                # No clock-in — not billable
                unbillable_h += sched_dur_h
                flags.append(f"NOT BILLABLE — no clock-in for {shift_label} ({fmt_h(sched_dur_h)} scheduled)")
                continue

            ck_start = (matched.get("start") or {}).get("timestamp", 0)
            ck_end   = (matched.get("end")   or {}).get("timestamp", 0)

            if not ck_end:
                # Never clocked out — schedule hours are billable but flag it
                billable_h += sched_dur_h
                flags.append(f"REVIEW — never clocked out for {shift_label} (billing scheduled hours {fmt_h(sched_dur_h)})")
                continue

            actual_dur_h = (ck_end - ck_start) / 3600

            # GPS check
            loc  = (matched.get("start") or {}).get("locationData") or {}
            clat = loc.get("latitude", 0)
            clon = loc.get("longitude", 0)
            jlat, jlon = job_gps(job_id)
            if jlat and jlon and clat and clon:
                dist = haversine_km(jlat, jlon, clat, clon)
                if dist > GPS_THRESHOLD_KM:
                    flags.append(
                        f"GPS FLAG — clocked in {dist:.1f}km from {client} on {fmt_ts(sched_start)[:6]} "
                        f"— verify shift occurred at correct address before approving"
                    )

            # Time discrepancy check
            late_h  = (ck_start - sched_start) / 3600
            early_h = (sched_end - ck_end) / 3600

            if late_h > TIME_TOLERANCE_H or early_h > TIME_TOLERANCE_H:
                actual_label = f"{fmt_ts(ck_start)} – {fmt_ts(ck_end)}"
                flags.append(
                    f"TIME DISCREPANCY — {shift_label}: scheduled {fmt_h(sched_dur_h)}, "
                    f"clocked {fmt_h(actual_dur_h)} ({actual_label}). Billing scheduled hours."
                )

            # Auto clock-out
            if matched.get("isAutoClockOut"):
                flags.append(f"AUTO CLOCK-OUT — {shift_label}: system forced clock-out, verify actual finish time")

            # Bill scheduled hours (workers bill to schedule, not to clock)
            billable_h += sched_dur_h

        # Unscheduled clocks (not matched to any shift)
        for i, ck in enumerate(worker_clocks):
            if i in clock_used:
                continue
            ck_start = (ck.get("start") or {}).get("timestamp", 0)
            ck_end   = (ck.get("end")   or {}).get("timestamp", 0)
            job_id   = str(ck.get("jobId") or "")
            client   = jname(job_id)
            dur_h    = (ck_end - ck_start) / 3600 if ck_end else 0
            flags.append(
                f"UNSCHEDULED SHIFT — clocked {fmt_ts(ck_start)} at {client} "
                f"({fmt_h(dur_h)}) — not on roster, needs approval before billing"
            )

        if worker_sched or worker_clocks:
            results.append({
                "name":         name,
                "uid":          uid,
                "scheduled_h":  scheduled_h,
                "billable_h":   billable_h,
                "unbillable_h": unbillable_h,
                "flags":        flags,
            })

    return results


def build_report(results, start_date, end_date):
    period = f"{start_date.strftime('%d %b')} – {end_date.strftime('%d %b %Y')}"
    lines  = [f"Invoice reconciliation — {period}\n"]

    clean    = [r for r in results if not r["flags"]]
    flagged  = [r for r in results if r["flags"]]

    if flagged:
        lines.append(f"⚠️ {len(flagged)} worker(s) need review before invoices are approved:\n")
        for r in sorted(flagged, key=lambda x: x["name"]):
            lines.append(
                f"{r['name']} — scheduled {fmt_h(r['scheduled_h'])}, "
                f"billable {fmt_h(r['billable_h'])}"
                + (f", unbillable {fmt_h(r['unbillable_h'])}" if r["unbillable_h"] else "")
            )
            for f in r["flags"]:
                lines.append(f"  • {f}")
            lines.append("")

    if clean:
        names = ", ".join(r["name"] for r in sorted(clean, key=lambda x: x["name"]))
        lines.append(f"✅ {len(clean)} worker(s) all clear — {names}")

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) == 3:
        start_date = datetime.date.fromisoformat(sys.argv[1])
        end_date   = datetime.date.fromisoformat(sys.argv[2])
    else:
        start_date, end_date = current_pay_period()

    print(f"\nInvoice reconciliation: {start_date} – {end_date}\n{'='*50}")
    results = reconcile(start_date, end_date)
    report  = build_report(results, start_date, end_date)

    print(f"\n{report}")
    post_to_management(report)
    print("\nPosted to CC Management.")


if __name__ == "__main__":
    main()
