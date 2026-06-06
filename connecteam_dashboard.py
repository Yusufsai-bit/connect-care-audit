"""
Connect Care Services — NDIS Compliance Audit Dashboard
Run: streamlit run connecteam_dashboard.py
"""

import os
import io
import calendar
import datetime
import json
import pandas as pd
import streamlit as st

_ct_key  = st.secrets.get("CONNECTEAM_API_KEY", "")
_ai_key  = st.secrets.get("ANTHROPIC_API_KEY", "")
_tw_sid  = st.secrets.get("TWILIO_ACCOUNT_SID", "")
_tw_tok  = st.secrets.get("TWILIO_AUTH_TOKEN", "")
_tw_num  = st.secrets.get("TWILIO_NUMBER", "")
if _ct_key:  os.environ["CONNECTEAM_API_KEY"]  = _ct_key
if _ai_key:  os.environ["ANTHROPIC_API_KEY"]   = _ai_key
if _tw_sid:  os.environ["TWILIO_ACCOUNT_SID"]  = _tw_sid
if _tw_tok:  os.environ["TWILIO_AUTH_TOKEN"]   = _tw_tok
if _tw_num:  os.environ["TWILIO_NUMBER"]       = _tw_num

from connecteam_audit import (
    run_audit, fetch_all_users, fetch_user_custom_fields,
    send_worker_message, add_worker_profile_note,
    create_worker_task, fetch_task_boards,
    send_sms, send_whatsapp,
)

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(page_title="Connect Care Compliance", page_icon="🛡️", layout="wide")

st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; }
    .metric-card { border-radius:12px; padding:1.2rem 1rem; text-align:center;
                   border-left:5px solid #ccc; background:#f8f9fa; }
    .metric-card.critical { border-left-color:#d62728; background:#fff5f5; }
    .metric-card.high     { border-left-color:#ff7f0e; background:#fff8f0; }
    .metric-card.medium   { border-left-color:#e6c200; background:#fffdf0; }
    .metric-card.total    { border-left-color:#4c78a8; background:#f0f4ff; }
    .metric-number { font-size:2.4rem; font-weight:700; line-height:1; }
    .metric-label  { font-size:0.85rem; color:#666; margin-top:0.3rem; }
    .metric-delta  { font-size:0.78rem; margin-top:0.2rem; }
    .delta-up { color:#d62728; } .delta-down { color:#2ca02c; } .delta-same { color:#888; }
    .alert-banner { background:#fff0f0; border:1.5px solid #d62728; border-radius:10px;
                    padding:0.9rem 1.2rem; margin-bottom:1rem; font-weight:600; color:#d62728; }
    .section-title { font-size:1.05rem; font-weight:600; color:#333; margin:1.2rem 0 0.5rem;
                     padding-bottom:0.3rem; border-bottom:2px solid #eee; }
    div[data-testid="stTabs"] button { font-size:0.95rem; font-weight:500; }
</style>
""", unsafe_allow_html=True)

# ── Constants ─────────────────────────────────────────────────────────────────

SEV_ORDER  = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
SEV_COLOUR = {"CRITICAL":"#d62728","HIGH":"#ff7f0e","MEDIUM":"#e6c200","LOW":"#2ca02c","INFO":"#aec7e8"}
SEV_TINT   = {"CRITICAL":"#fff5f5","HIGH":"#fff8f0","MEDIUM":"#fffdf0","LOW":"#f0fff0","INFO":"#f0f6ff"}
SEV_EMOJI  = {"CRITICAL":"🔴","HIGH":"🟠","MEDIUM":"🟡","LOW":"🟢","INFO":"⚪"}

# Workers to exclude from all staff views (not real staff)
EXCLUDED_WORKERS = {"(team)", "(unassigned)"}

PLAIN_LABELS = {
    "NO CLOCK-IN":                              "Didn't clock in",
    "LATE CLOCK-IN":                            "Late to start shift",
    "EARLY CLOCK-OUT":                          "Left shift early",
    "MISSING CLOCK-OUT":                        "Forgot to clock out",
    "AUTO CLOCK-OUT":                           "System closed shift (forgot clock out)",
    "OPEN SHIFT":                               "Shift has no worker assigned",
    "UNSCHEDULED SHIFT":                        "Worked an unrostered shift",
    "SUSPICIOUSLY SHORT SHIFT":                 "Very short shift — check records",
    "MULTIPLE CLOCK-INS SAME CLIENT/DAY":       "Multiple clock-ins for same client/day",
    "UNDERSTAFFED -- RATIO BREACH":             "Not enough staff on shift",
    "OVERSTAFFED -- POSSIBLE OVERBILLING":      "Too many staff — verify billing",
    "GPS MISMATCH":                             "Clocked in from wrong location",
    "NO SHIFT NOTES":                           "No notes written",
    "EMPTY NOTES":                              "Notes left blank",
    "INSUFFICIENT NOTES":                       "Notes too short (under 50 words)",
    "LATE NOTE SUBMISSION":                     "Notes written more than 24h after shift",
    "MISSING SIGNATURE":                        "No participant signature collected",
    "DUPLICATE/COPY-PASTE NOTES":               "Notes appear copy-pasted from another shift",
    "POSSIBLE AI-GENERATED NOTE":               "Notes may be AI-written — check authenticity",
    "FAILS NDIS STANDARD":                      "Note doesn't meet NDIS requirements",
    "NOT PLAIN ENGLISH":                        "Notes are unclear or hard to understand",
    "NOTE DOESN'T MAKE SENSE":                  "Notes are incoherent or contradictory",
    "SUBJECTIVE LANGUAGE":                      "Notes use opinions instead of observations",
    "NOT PERSON-CENTRED":                       "Notes don't reflect participant's dignity",
    "INCIDENT KEYWORD -- VERIFY REPORT FILED":  "Incident mentioned — confirm report was filed",
    "RESTRICTIVE PRACTICE MENTIONED":           "Restrictive practice mentioned — authorisation required",
    "MEDICATION MENTIONED -- VERIFY FORM FILED":"Medication mentioned — confirm form was filed",
    "INCOMPLETE INCIDENT REPORT":               "Incident report submitted incomplete",
    "LATE INCIDENT REPORTING":                  "Incident report filed late",
    "MISSING FORM -- KALLAN":                   "Kallan required form not submitted",
    "MISSING FORM -- EVAN":                     "Evan required form not submitted",
    "MISSING FORM -- MICHAEL":                  "Michael required form not submitted",
    "FORM FREQUENCY -- JOSHUA":                 "Joshua weekly form quota not met",
    "FORM FREQUENCY -- NADA":                   "Nada weekly form quota not met",
    "FORM FREQUENCY -- JOHN":                   "John weekly form quota not met",
    "FORM FREQUENCY -- NICOLE":                 "Nicole weekly form quota not met",
    # New categories (7 improvements)
    "APPROVED LEAVE":                           "Absent — approved leave on file",
    "BREAK COMPLIANCE":                         "No break recorded on long shift (Fair Work)",
    "CROSS-WORKER COPY-PASTE NOTES":            "Two workers submitted near-identical notes",
    "ONBOARDING INCOMPLETE":                    "Worker has not finished mandatory onboarding",
    "UNAUTHORISED CLIENT ACCESS":               "Clocked in for a client not assigned to them",
}

REQUIRED_DOCS = [
    "NDIS Worker Screening",
    "Working With Children Check",
    "Police Check",
    "First Aid Certificate",
    "CPR Certificate",
    "Manual Handling Training",
]

def plain(cat):
    return PLAIN_LABELS.get(cat, cat.title())

# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_issue_date(date_str):
    """Parse issue date strings like 'Tue 26-May' or '2026-06-02' into a date."""
    today = datetime.date.today()
    for fmt in ["%Y-%m-%d", "%a %d-%b"]:
        try:
            d = datetime.datetime.strptime(date_str.strip(), fmt).date()
            if fmt == "%a %d-%b":
                d = d.replace(year=today.year)
                if d > today + datetime.timedelta(days=30):
                    d = d.replace(year=today.year - 1)
            return d
        except Exception:
            continue
    return None

def compliance_score(worker_df):
    deductions = {"CRITICAL": 15, "HIGH": 8, "MEDIUM": 3, "LOW": 1}
    total = worker_df["Severity"].map(deductions).fillna(0).sum()
    return max(0, 100 - int(total))

def score_colour(s):
    return "#2ca02c" if s >= 80 else "#ff7f0e" if s >= 60 else "#d62728"

def score_label(s):
    return "Good" if s >= 80 else "Needs Attention" if s >= 60 else "At Risk"

def colour_row(row):
    tint = SEV_TINT.get(row["Severity"], "#ffffff")
    return [f"background-color:{tint}"] * len(row)

def to_excel(dfs: dict) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for sheet, df in dfs.items():
            df.to_excel(w, sheet_name=sheet[:31], index=False)
    return buf.getvalue()

def pay_cycles(today):
    """Returns list of (label, start_date, end_date) for current and previous pay cycles."""
    y, m = today.year, today.month
    last = calendar.monthrange(y, m)[1]
    mn   = today.strftime("%b %Y")

    cycles = [
        (f"Pay Cycle 1 — 1–15 {mn}",    datetime.date(y, m, 1),  datetime.date(y, m, 15)),
        (f"Pay Cycle 2 — 16–{last} {mn}", datetime.date(y, m, 16), datetime.date(y, m, last)),
    ]

    # Previous month cycles
    pm = m - 1 if m > 1 else 12
    py = y if m > 1 else y - 1
    plast = calendar.monthrange(py, pm)[1]
    pmn   = datetime.date(py, pm, 1).strftime("%b %Y")
    cycles += [
        (f"Pay Cycle 1 — 1–15 {pmn}",     datetime.date(py, pm, 1),  datetime.date(py, pm, 15)),
        (f"Pay Cycle 2 — 16–{plast} {pmn}", datetime.date(py, pm, 16), datetime.date(py, pm, plast)),
    ]
    return cycles

def doc_status(expiry_str):
    """Returns (emoji, label, colour) for a document expiry date string."""
    if not expiry_str or str(expiry_str).strip() in ("", "nan", "None"):
        return "❌", "Missing", "#d62728"
    try:
        exp = datetime.date.fromisoformat(str(expiry_str).strip())
        today = datetime.date.today()
        days_left = (exp - today).days
        if days_left < 0:
            return "❌", f"Expired {abs(days_left)}d ago", "#d62728"
        elif days_left <= 60:
            return "⚠️", f"Expires in {days_left}d", "#ff7f0e"
        else:
            return "✅", f"Valid ({exp.strftime('%d %b %Y')})", "#2ca02c"
    except Exception:
        return "❓", "Invalid date", "#888"

# ── Notification helpers ──────────────────────────────────────────────────────

import uuid as _uuid

def init_notifications():
    if "notifications" not in st.session_state:
        st.session_state.notifications = []

def build_notify_message(worker_name, issues_df, period_label):
    """Build an urgent, personal-sounding message from a manager."""
    first = worker_name.split()[0]
    lines = [
        f"Hi {first},",
        "",
        f"From your shift {period_label} — I need you to sort these out by 5 PM today:",
        "",
    ]

    SEV_ICON = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}
    shown = 0
    for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]:
        rows = issues_df[issues_df["Severity"] == sev]
        for _, row in rows.iterrows():
            if shown >= 8:
                break
            icon   = SEV_ICON.get(sev, "•")
            detail = str(row.get("Detail", "")).strip()
            client = str(row.get("Client", "")).strip()
            if len(detail) > 90:
                detail = detail[:87] + "…"
            lines.append(f"{icon} {client} — {detail}")
            shown += 1
        if shown >= 8:
            break

    remaining = len(issues_df) - shown
    if remaining > 0:
        lines.append(f"(+ {remaining} more)")

    lines += [
        "",
        "Reply and let me know what happened and what you've done to fix it — need to hear back by 5 PM.",
        "",
        "Cheers",
    ]
    return "\n".join(lines)

def log_notification(worker_name, worker_id, issues_df, message_text, period_label,
                     task_id=None, profile_note_added=False):
    init_notifications()
    sev_counts = issues_df["Severity"].value_counts().to_dict()
    st.session_state.notifications.insert(0, {
        "id":                 str(_uuid.uuid4())[:8],
        "worker":             worker_name,
        "worker_id":          worker_id,
        "sent_at":            datetime.datetime.now().strftime("%d %b %Y, %I:%M %p"),
        "period":             period_label,
        "severity_counts":    sev_counts,
        "issue_count":        len(issues_df),
        "issues":             issues_df[["Severity","Issue","Client","Date","Detail"]].to_dict("records"),
        "message_sent":       message_text,
        "status":             "Sent",
        "acknowledged_at":    None,
        "resolved_at":        None,
        "manager_notes":      "",
        "task_id":            task_id,
        "profile_note_added": profile_note_added,
    })

# ── Password gate ─────────────────────────────────────────────────────────────

if not st.session_state.get("authenticated"):
    st.markdown("<br><br>", unsafe_allow_html=True)
    _, col_b, _ = st.columns([1, 1.2, 1])
    with col_b:
        st.markdown("## 🛡️ Connect Care")
        st.markdown("##### Compliance Audit Dashboard")
        st.markdown("<br>", unsafe_allow_html=True)
        pw = st.text_input("Password", type="password", placeholder="Enter password…")
        if st.button("Sign in", use_container_width=True, type="primary"):
            if pw == st.secrets.get("DASHBOARD_PASSWORD", ""):
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Incorrect password.")
    st.stop()

# ── Data loaders ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def load_audit(days_back: int):
    issues = run_audit(days_back)
    fetched_at = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")
    return issues, fetched_at

@st.cache_data(ttl=300, show_spinner=False)
def load_prev_count(days_back: int):
    issues = run_audit(days_back * 2)
    return len(issues)

@st.cache_data(ttl=600, show_spinner=False)
def load_worker_contacts():
    try:
        users = fetch_all_users()
        return {
            f"{u.get('firstName','')} {u.get('lastName','')}".strip(): {
                "phone":  u.get("phoneNumber") or u.get("phone") or "",
                "email":  u.get("email") or "",
                "userId": u.get("userId"),
            }
            for u in users.values()
        }
    except Exception:
        return {}

@st.cache_data(ttl=600, show_spinner=False)
def load_task_boards():
    try:
        boards = fetch_task_boards()
        return {
            b.get("name", f"Board {b.get('taskBoardId','?')}"): b.get("taskBoardId") or b.get("id")
            for b in boards if (b.get("taskBoardId") or b.get("id"))
        }
    except Exception:
        return {}

@st.cache_data(ttl=600, show_spinner=False)
def load_staff_names():
    try:
        users = fetch_all_users()
        return sorted(
            f"{u.get('firstName','')} {u.get('lastName','')}".strip()
            for u in users.values()
            if u.get("firstName")
        )
    except Exception:
        return []

# Keyword mapping: doc type → substrings to look for in a custom field name
_DOC_KEYWORDS = {
    "NDIS Worker Screening":         ["ndis", "screening", "worker screening"],
    "Working With Children Check":   ["wwcc", "working with children", "children check"],
    "Police Check":                  ["police"],
    "First Aid Certificate":         ["first aid"],
    "CPR Certificate":               ["cpr"],
    "Manual Handling Training":      ["manual handling"],
}

@st.cache_data(ttl=600, show_spinner=False)
def load_custom_field_docs():
    """
    Tries to read document expiry dates from Connecteam user custom fields.
    Returns dict: worker_name -> {doc_type -> expiry_string}  (only populated fields).
    Returns ({}, []) if no matching fields found.
    """
    try:
        fields = fetch_user_custom_fields()
        users  = fetch_all_users()
    except Exception:
        return {}, []

    # Find custom fields that look like document expiry fields
    matched_fields = {}  # doc_type -> customFieldId
    for field in fields:
        fname = (field.get("name") or "").lower()
        fid   = field.get("customFieldId") or field.get("id")
        ftype = (field.get("type") or "").lower()
        if not fid:
            continue
        for doc_type, keywords in _DOC_KEYWORDS.items():
            if any(kw in fname for kw in keywords):
                matched_fields[doc_type] = fid
                break

    if not matched_fields:
        return {}, []

    # Extract values from user objects (custom fields may be embedded in user data)
    result = {}
    for u in users.values():
        name = f"{u.get('firstName','')} {u.get('lastName','')}".strip()
        if not name:
            continue
        custom_vals = u.get("customFields") or u.get("customFieldValues") or []
        if isinstance(custom_vals, dict):
            custom_vals = [{"customFieldId": k, "value": v} for k, v in custom_vals.items()]
        if not custom_vals:
            continue
        worker_docs = {}
        for cv in custom_vals:
            field_id = cv.get("customFieldId") or cv.get("id")
            value    = cv.get("value") or cv.get("fieldValue") or ""
            for doc_type, mapped_id in matched_fields.items():
                if str(field_id) == str(mapped_id) and value:
                    worker_docs[doc_type] = str(value).strip()
        if worker_docs:
            result[name] = worker_docs

    return result, list(matched_fields.keys())

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("### 🛡️ Connect Care")
    st.markdown("##### Compliance Dashboard")
    st.divider()

    today     = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    cycles    = pay_cycles(today)
    cycle_labels = (
        [f"Yesterday ({yesterday.strftime('%a %d %b')})"]
        + [c[0] for c in cycles]
        + ["Custom dates"]
    )

    period_choice = st.selectbox("Audit period", cycle_labels)

    if period_choice == "Custom dates":
        date_range = st.date_input(
            "From / To",
            value=(today - datetime.timedelta(days=7), today),
            max_value=today,
            label_visibility="collapsed",
        )
        if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
            start_date, end_date = date_range
        else:
            start_date = date_range[0] if date_range else today - datetime.timedelta(days=7)
            end_date = today
    elif period_choice.startswith("Yesterday"):
        start_date, end_date = yesterday, yesterday
    else:
        _, start_date, end_date = next(c for c in cycles if c[0] == period_choice)

    # Load enough history to cover the period
    days_back = max((today - start_date).days + 1, 1)

    compare_prev = st.checkbox("Compare with previous period", value=False)

    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("🔄  Refresh Data", use_container_width=True, type="primary"):
        st.cache_data.clear()
        st.rerun()

    with st.spinner("Loading…"):
        all_issues, fetched_at = load_audit(days_back)
        prev_total = load_prev_count(days_back) if compare_prev else None

    st.markdown(f"<small style='color:#888'>Last updated<br>{fetched_at}</small>",
                unsafe_allow_html=True)
    st.divider()
    if st.button("Sign out", use_container_width=True):
        st.session_state.authenticated = False
        st.rerun()

# ── Build dataframe ───────────────────────────────────────────────────────────

if not all_issues:
    st.success("✅ No compliance issues found for this period.")
    st.stop()

df_raw = pd.DataFrame([
    {"Severity": i.severity, "Category": i.category, "Issue": plain(i.category),
     "Worker": i.worker, "Client": i.client, "Date": i.date, "Detail": i.detail}
    for i in all_issues
])

# Parse dates and filter to selected pay period
df_raw["_parsed_date"] = df_raw["Date"].apply(parse_issue_date)
df_all = df_raw[
    (df_raw["_parsed_date"] >= start_date) &
    (df_raw["_parsed_date"] <= end_date)
].drop(columns="_parsed_date").copy()

df_all["_rank"] = df_all["Severity"].map({s: i for i, s in enumerate(SEV_ORDER)})
df_all = df_all.sort_values("_rank").drop(columns="_rank")

# Staff-only dataframe (excludes team/unassigned pseudo-workers)
df_staff = df_all[~df_all["Worker"].isin(EXCLUDED_WORKERS)].copy()

counts     = df_all["Severity"].value_counts()
n_critical = counts.get("CRITICAL", 0)
n_high     = counts.get("HIGH", 0)
n_medium   = counts.get("MEDIUM", 0)
n_total    = len(df_all)

def period_delta(count, prev_total):
    if prev_total is None:
        return ""
    est_prev = prev_total - len(df_raw)
    diff = count - round(est_prev * count / max(len(df_raw), 1))
    if diff > 0:   return f'<div class="metric-delta delta-up">▲ {diff} vs prev</div>'
    elif diff < 0: return f'<div class="metric-delta delta-down">▼ {abs(diff)} vs prev</div>'
    else:          return f'<div class="metric-delta delta-same">— same as prev</div>'

# ── Header ────────────────────────────────────────────────────────────────────

st.markdown("## NDIS Compliance Audit")
st.caption(f"{start_date.strftime('%d %b %Y')} – {end_date.strftime('%d %b %Y')}  ·  {n_total} issues found")

if n_critical > 0:
    st.markdown(
        f'<div class="alert-banner">⚠️  {n_critical} critical issue{"s" if n_critical > 1 else ""} '
        f'require immediate attention — see the Action Required tab below.</div>',
        unsafe_allow_html=True)

c1, c2, c3, c4 = st.columns(4)
for col, cls, colour, label, count in [
    (c1, "critical", "#d62728", "🔴 Critical",     n_critical),
    (c2, "high",     "#ff7f0e", "🟠 High Priority", n_high),
    (c3, "medium",   "#b8980a", "🟡 Medium",        n_medium),
    (c4, "total",    "#4c78a8", "📋 Total Issues",  n_total),
]:
    col.markdown(
        f'<div class="metric-card {cls}">'
        f'<div class="metric-number" style="color:{colour}">{count}</div>'
        f'<div class="metric-label">{label}</div>'
        f'{period_delta(count, prev_total)}</div>',
        unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# ── Tabs ──────────────────────────────────────────────────────────────────────

init_notifications()

tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
    "🚨  Action Required",
    "👤  By Worker",
    "👥  By Client",
    "📄  Documents",
    "📋  All Issues",
    "📥  Export",
    "📬  Notifications",
])

# ─────────────────────────────────────────────────
# TAB 1 — Action Required
# ─────────────────────────────────────────────────
with tab1:
    st.markdown('<div class="section-title">Issues Needing Immediate Action</div>',
                unsafe_allow_html=True)
    urgent = df_all[df_all["Severity"].isin(["CRITICAL", "HIGH"])].copy()

    if urgent.empty:
        st.success("✅ No critical or high priority issues for this period.")
    else:
        summary = (urgent.groupby(["Severity", "Issue"]).size()
                   .reset_index(name="Count"))
        summary["_rank"] = summary["Severity"].map({s: i for i, s in enumerate(SEV_ORDER)})
        summary = summary.sort_values(["_rank", "Count"], ascending=[True, False]).drop(columns="_rank")
        for _, row in summary.iterrows():
            c = SEV_COLOUR[row["Severity"]]; t = SEV_TINT[row["Severity"]]; e = SEV_EMOJI[row["Severity"]]
            st.markdown(
                f'<div style="background:{t};border-left:4px solid {c};border-radius:8px;'
                f'padding:0.7rem 1rem;margin-bottom:0.5rem;display:flex;'
                f'justify-content:space-between;align-items:center;">'
                f'<span>{e} <strong>{row["Issue"]}</strong></span>'
                f'<span style="background:{c};color:white;border-radius:20px;'
                f'padding:2px 12px;font-size:0.85rem;font-weight:600;">'
                f'{row["Count"]} case{"s" if row["Count"]>1 else ""}</span></div>',
                unsafe_allow_html=True)

    late_inc = df_all[df_all["Category"] == "LATE INCIDENT REPORTING"]
    if not late_inc.empty:
        st.markdown('<div class="section-title">⚠️ Late Incident Reports — NDIS Commission Risk</div>',
                    unsafe_allow_html=True)
        st.warning(f"{len(late_inc)} incident report(s) filed late. NDIS requires serious incidents within 24 hours.")
        for _, row in late_inc.iterrows():
            st.markdown(f"- **{row['Worker']}** · {row['Detail']}")

    restrict = df_all[df_all["Category"] == "RESTRICTIVE PRACTICE MENTIONED"]
    if not restrict.empty:
        st.markdown('<div class="section-title">🚫 Restrictive Practice — Authorisation Required</div>',
                    unsafe_allow_html=True)
        st.error(f"{len(restrict)} note(s) mention restrictive practice. Formal authorisation required.")
        for _, row in restrict.iterrows():
            st.markdown(f"- **{row['Worker']}** · {row['Client']} · {row['Date']}")

    onboarding_act = df_all[df_all["Category"] == "ONBOARDING INCOMPLETE"]
    if not onboarding_act.empty:
        st.markdown('<div class="section-title">🎓 Onboarding Not Completed</div>', unsafe_allow_html=True)
        st.error(f"{len(onboarding_act)} worker(s) are delivering care with incomplete mandatory onboarding.")
        for _, row in onboarding_act.iterrows():
            st.markdown(f"- **{row['Worker']}** — {row['Detail']}")

    unauth_act = df_all[df_all["Category"] == "UNAUTHORISED CLIENT ACCESS"]
    if not unauth_act.empty:
        st.markdown('<div class="section-title">🔑 Unauthorised Client Access</div>', unsafe_allow_html=True)
        st.error(f"{len(unauth_act)} worker(s) clocked in for clients they are not assigned to.")
        for _, row in unauth_act.iterrows():
            st.markdown(f"- **{row['Worker']}** → **{row['Client']}** · {row['Detail']}")

    cross_paste = df_all[df_all["Category"] == "CROSS-WORKER COPY-PASTE NOTES"]
    if not cross_paste.empty:
        st.markdown('<div class="section-title">📋 Cross-Worker Note Sharing</div>', unsafe_allow_html=True)
        st.warning(f"{len(cross_paste)} case(s) of two workers submitting near-identical notes.")
        for _, row in cross_paste.iterrows():
            st.markdown(f"- **{row['Worker']}** · {row['Client']} · {row['Date']}")

    leave_items = df_all[df_all["Category"] == "APPROVED LEAVE"]
    if not leave_items.empty:
        st.markdown('<div class="section-title">✅ Approved Leave (informational)</div>', unsafe_allow_html=True)
        st.info(f"{len(leave_items)} shift(s) marked absent where the worker had recorded unavailability — these are not compliance failures.")
        for _, row in leave_items.iterrows():
            st.markdown(f"- **{row['Worker']}** · {row['Client']} · {row['Date']}")

# ─────────────────────────────────────────────────
# TAB 2 — By Worker
# ─────────────────────────────────────────────────
with tab2:
    st.markdown('<div class="section-title">Worker Compliance Scores</div>', unsafe_allow_html=True)
    st.caption("Score starts at 100. Deducts 15 per Critical · 8 per High · 3 per Medium · 1 per Low.")

    if df_staff.empty:
        st.info("No staff issues recorded for this period.")
    else:
        wsummary = (df_staff.groupby(["Worker", "Severity"]).size().unstack(fill_value=0))
        for col in SEV_ORDER:
            if col not in wsummary.columns: wsummary[col] = 0
        wsummary = wsummary[[c for c in SEV_ORDER if c in wsummary.columns]]
        wsummary["Total"] = wsummary.sum(axis=1)
        wsummary["Score"] = [compliance_score(df_staff[df_staff["Worker"] == w]) for w in wsummary.index]
        wsummary["Status"] = wsummary["Score"].apply(score_label)
        wsummary = wsummary.sort_values("Score", ascending=True)

        st.dataframe(wsummary, use_container_width=True, column_config={
            "CRITICAL": st.column_config.NumberColumn("🔴 Critical"),
            "HIGH":     st.column_config.NumberColumn("🟠 High"),
            "MEDIUM":   st.column_config.NumberColumn("🟡 Medium"),
            "LOW":      st.column_config.NumberColumn("🟢 Low"),
            "Total":    st.column_config.NumberColumn("Total Issues"),
            "Score":    st.column_config.ProgressColumn("Compliance Score", min_value=0, max_value=100, format="%d%%"),
            "Status":   st.column_config.TextColumn("Status"),
        })

        st.markdown('<div class="section-title">Drill Into a Worker</div>', unsafe_allow_html=True)
        workers_list = sorted(df_staff["Worker"].unique())
        selected_worker = st.selectbox("Select worker", ["— pick a worker —"] + workers_list)

        if selected_worker != "— pick a worker —":
            score = compliance_score(df_staff[df_staff["Worker"] == selected_worker])
            sc = score_colour(score); sl = score_label(score)

            col_score, col_contact = st.columns([1, 2])
            with col_score:
                st.markdown(
                    f'<div style="text-align:center;padding:1rem;">'
                    f'<div style="font-size:3rem;font-weight:700;color:{sc}">{score}</div>'
                    f'<div style="font-size:0.9rem;color:{sc};font-weight:600">{sl}</div>'
                    f'<div style="font-size:0.75rem;color:#888;margin-top:0.3rem">Compliance Score</div>'
                    f'</div>', unsafe_allow_html=True)
            with col_contact:
                contacts = load_worker_contacts()
                info = contacts.get(selected_worker, {})
                phone = info.get("phone", ""); email = info.get("email", "")
                st.markdown("<br>", unsafe_allow_html=True)
                st.markdown(f"📞 **Phone:** {'['+phone+'](tel:'+phone+')' if phone else '_not on file_'}")
                st.markdown(f"✉️ **Email:** {'['+email+'](mailto:'+email+')' if email else '_not on file_'}")

            st.markdown("<br>", unsafe_allow_html=True)
            wdf = df_staff[df_staff["Worker"] == selected_worker][["Severity","Issue","Client","Date","Detail"]]
            st.dataframe(wdf.style.apply(colour_row, axis=1), use_container_width=True, hide_index=True)

            # ── Notify this worker ──────────────────────────────────────────
            st.markdown('<div class="section-title">📩 Notify This Worker</div>', unsafe_allow_html=True)
            contacts       = load_worker_contacts()
            worker_info    = contacts.get(selected_worker, {})
            worker_user_id = worker_info.get("userId")

            if not worker_user_id:
                st.warning("Could not find Connecteam user ID for this worker.")
            else:
                if start_date == end_date == yesterday:
                    period_label_str = f"yesterday ({yesterday.strftime('%a %d %b')})"
                else:
                    period_label_str = f"{start_date.strftime('%d %b')} – {end_date.strftime('%d %b %Y')}"
                default_msg      = build_notify_message(selected_worker, wdf, period_label_str)

                with st.expander("Compose & send message", expanded=False):
                    msg_channel = st.radio(
                        "Send via",
                        ["WhatsApp (sandbox — testing)", "SMS"],
                        horizontal=True,
                        key=f"chan_{selected_worker}",
                        help="Sandbox = testing only (worker must have joined sandbox first). SMS works immediately.",
                    )
                    worker_phone = worker_info.get("phone", "")
                    if worker_phone:
                        st.caption(f"Sending to: {worker_phone}")
                    else:
                        st.warning("No phone number on file for this worker.")

                    edited_msg = st.text_area(
                        "Message",
                        value=default_msg,
                        height=260,
                        key=f"msg_{selected_worker}",
                    )

                    col_opt1, col_opt2 = st.columns(2)
                    with col_opt1:
                        add_profile_note = st.checkbox(
                            "Add note to worker's HR profile",
                            value=True,
                            help="Logs a compliance note on the worker's Connecteam profile as a permanent record.",
                        )
                    with col_opt2:
                        create_task_opt = st.checkbox(
                            "Create acknowledgement task",
                            value=False,
                            help="Creates a Connecteam task assigned to the worker so they can mark it complete.",
                        )

                    task_board_id = None
                    if create_task_opt:
                        boards = load_task_boards()
                        if boards:
                            board_name = st.selectbox("Task board", list(boards.keys()))
                            task_board_id = boards[board_name]
                        else:
                            st.info("No task boards found. Create one in Connecteam first.")
                            create_task_opt = False

                    st.markdown("<br>", unsafe_allow_html=True)
                    if st.button("📤  Send Now", type="primary", key=f"send_{selected_worker}"):
                        errors = []
                        worker_phone = worker_info.get("phone", "").strip()

                        # 1. Send via Twilio (WhatsApp sandbox or SMS)
                        if not worker_phone:
                            errors.append("No phone number on file for this worker in Connecteam.")
                            ok = False
                        elif msg_channel == "WhatsApp (sandbox — testing)":
                            ok, err = send_whatsapp(worker_phone, edited_msg, sandbox=True)
                            if not ok: errors.append(f"WhatsApp failed: {err}")
                        elif msg_channel == "WhatsApp":
                            ok, err = send_whatsapp(worker_phone, edited_msg, sandbox=False)
                            if not ok: errors.append(f"WhatsApp failed: {err}")
                        else:
                            ok, err = send_sms(worker_phone, edited_msg)
                            if not ok: errors.append(f"SMS failed: {err}")

                        # 2. Add HR profile note
                        note_ok = False
                        if add_profile_note:
                            note_ok, note_err = add_worker_profile_note(
                                worker_user_id,
                                f"Compliance notification sent {datetime.datetime.now().strftime('%d %b %Y')}.\n"
                                f"Period: {period_label_str}. Issues: {len(wdf)}.",
                                title="Compliance Notification",
                            )
                            if not note_ok:
                                errors.append(f"Profile note failed: {note_err}")

                        # 3. Create acknowledgement task
                        task_id = None
                        if create_task_opt and task_board_id:
                            task_ok, task_result = create_worker_task(
                                task_board_id,
                                worker_user_id,
                                f"Compliance issues — {period_label_str}",
                                f"Review and acknowledge {len(wdf)} compliance item(s). Reply to your manager when done.",
                                due_ts=int((datetime.datetime.now() + datetime.timedelta(days=3)).timestamp()),
                            )
                            if task_ok:
                                task_id = task_result
                            else:
                                errors.append(f"Task creation failed: {task_result}")

                        # Log it
                        if ok:
                            log_notification(
                                selected_worker, worker_user_id, wdf,
                                edited_msg, period_label_str,
                                task_id=task_id,
                                profile_note_added=note_ok,
                            )
                            if errors:
                                st.warning("Message sent, but some options failed: " + " | ".join(errors))
                            else:
                                st.success(f"✅ Message sent to {selected_worker} via Connecteam. Logged in the Notifications tab.")
                        else:
                            st.error("Failed to send: " + " | ".join(errors))

    # Onboarding status section
    onboarding_issues = df_all[df_all["Category"] == "ONBOARDING INCOMPLETE"]
    if not onboarding_issues.empty:
        st.markdown('<div class="section-title">🎓 Onboarding Incomplete</div>', unsafe_allow_html=True)
        st.warning(f"{len(onboarding_issues)} worker(s) have not finished mandatory onboarding packs.")
        for _, row in onboarding_issues.iterrows():
            st.markdown(f"- **{row['Worker']}** — {row['Detail']}")

    # Unauthorised access section
    unauth_issues = df_all[df_all["Category"] == "UNAUTHORISED CLIENT ACCESS"]
    if not unauth_issues.empty:
        st.markdown('<div class="section-title">🚫 Unauthorised Client Access</div>', unsafe_allow_html=True)
        st.error(f"{len(unauth_issues)} instance(s) of workers clocking in for clients they are not assigned to.")
        for _, row in unauth_issues.iterrows():
            st.markdown(f"- **{row['Worker']}** clocked in for **{row['Client']}** — {row['Detail']}")

    # Break compliance section
    break_issues = df_staff[df_staff["Category"] == "BREAK COMPLIANCE"]
    if not break_issues.empty:
        st.markdown('<div class="section-title">⏱️ Break Compliance (Fair Work)</div>', unsafe_allow_html=True)
        st.warning(f"{len(break_issues)} shift(s) over {5}h with no recorded break.")
        bsummary = break_issues.groupby("Worker").size().reset_index(name="Shifts Missing Break")
        st.dataframe(bsummary, use_container_width=True, hide_index=True)

# ─────────────────────────────────────────────────
# TAB 3 — By Client
# ─────────────────────────────────────────────────
with tab3:
    st.markdown('<div class="section-title">Client Issue Summary</div>', unsafe_allow_html=True)

    csummary = (df_all.groupby(["Client","Severity"]).size().unstack(fill_value=0))
    for col in SEV_ORDER:
        if col not in csummary.columns: csummary[col] = 0
    csummary = csummary[[c for c in SEV_ORDER if c in csummary.columns]]
    csummary["Total"] = csummary.sum(axis=1)
    csummary = csummary.sort_values("CRITICAL", ascending=False)

    st.dataframe(csummary, use_container_width=True, column_config={
        "CRITICAL": st.column_config.NumberColumn("🔴 Critical"),
        "HIGH":     st.column_config.NumberColumn("🟠 High"),
        "MEDIUM":   st.column_config.NumberColumn("🟡 Medium"),
        "LOW":      st.column_config.NumberColumn("🟢 Low"),
        "Total":    st.column_config.NumberColumn("Total"),
    })

    form_issues = df_all[df_all["Category"].str.startswith("MISSING FORM") |
                         df_all["Category"].str.startswith("FORM FREQUENCY")]
    if not form_issues.empty:
        st.markdown('<div class="section-title">Form Completion This Period</div>', unsafe_allow_html=True)
        fsummary = form_issues.groupby(["Client","Issue"]).size().reset_index(name="Missing Days")
        for client_name, grp in fsummary.groupby("Client"):
            st.markdown(f"**{client_name}**")
            for _, frow in grp.iterrows():
                total_days = max(df_all[df_all["Client"] == client_name]["Date"].nunique(), 1)
                missing = frow["Missing Days"]
                submitted = max(total_days - missing, 0)
                pct = round(submitted / total_days * 100)
                bar_col = "#2ca02c" if pct >= 80 else "#ff7f0e" if pct >= 50 else "#d62728"
                st.markdown(
                    f'<div style="margin-bottom:0.4rem">{frow["Issue"]} — '
                    f'<strong style="color:{bar_col}">{pct}%</strong> '
                    f'<span style="color:#888;font-size:0.8rem">({submitted}/{total_days} days)</span></div>',
                    unsafe_allow_html=True)

    st.markdown('<div class="section-title">Drill Into a Client</div>', unsafe_allow_html=True)
    selected_client = st.selectbox("Select client", ["— pick a client —"] + sorted(df_all["Client"].unique()))
    if selected_client != "— pick a client —":
        cdf = df_all[df_all["Client"] == selected_client][["Severity","Issue","Worker","Date","Detail"]]
        st.dataframe(cdf.style.apply(colour_row, axis=1), use_container_width=True, hide_index=True)

# ─────────────────────────────────────────────────
# TAB 4 — Documents
# ─────────────────────────────────────────────────
with tab4:
    st.markdown('<div class="section-title">Worker Document Tracker</div>', unsafe_allow_html=True)
    st.caption("Track NDIS-required documents for each worker. ✅ Valid  ⚠️ Expiring within 60 days  ❌ Expired or missing")

    # Load staff list from Connecteam (cached)
    with st.spinner("Loading staff list…"):
        staff_names = load_staff_names()

    if not staff_names:
        st.warning("Could not load staff list from Connecteam. Check your API key.")
        st.stop()

    # Initialise document data in session state
    if "doc_data" not in st.session_state:
        saved = st.secrets.get("DOCUMENTS_JSON", "")
        if saved:
            try:
                st.session_state.doc_data = json.loads(saved)
            except Exception:
                st.session_state.doc_data = {}
        else:
            st.session_state.doc_data = {}

    # Sync from Connecteam custom fields
    sync_col, _ = st.columns([1, 3])
    with sync_col:
        if st.button("🔄  Sync from Connecteam", help="Reads document expiry dates stored in Connecteam user custom fields and pre-fills any matching entries below."):
            with st.spinner("Reading Connecteam custom fields…"):
                cf_docs, matched_doc_types = load_custom_field_docs()
            if cf_docs:
                merged = dict(st.session_state.doc_data)
                updated = 0
                for worker, docs in cf_docs.items():
                    merged.setdefault(worker, {})
                    for doc_type, expiry in docs.items():
                        if expiry and expiry not in ("", "nan", "None"):
                            merged[worker][doc_type] = expiry
                            updated += 1
                st.session_state.doc_data = merged
                st.success(f"Synced {updated} document field(s) from Connecteam for {len(cf_docs)} worker(s). Matching fields: {', '.join(matched_doc_types)}.")
                st.rerun()
            else:
                st.info("No matching document custom fields found in Connecteam. You can still enter expiry dates manually below, or add document fields to worker profiles in Connecteam first.")

    # Build editable dataframe
    rows = []
    for worker in staff_names:
        row = {"Worker": worker}
        for doc in REQUIRED_DOCS:
            expiry = st.session_state.doc_data.get(worker, {}).get(doc, "")
            row[doc] = expiry
        rows.append(row)

    doc_df = pd.DataFrame(rows)

    st.markdown("**Enter or update expiry dates below (format: YYYY-MM-DD)**")
    edited = st.data_editor(
        doc_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Worker": st.column_config.TextColumn("Worker", disabled=True, width="medium"),
            **{doc: st.column_config.TextColumn(doc, width="small") for doc in REQUIRED_DOCS}
        },
        key="doc_editor",
    )

    if st.button("💾  Save Changes", type="primary"):
        new_data = {}
        for _, row in edited.iterrows():
            worker = row["Worker"]
            new_data[worker] = {doc: str(row[doc]) if row[doc] else "" for doc in REQUIRED_DOCS}
        st.session_state.doc_data = new_data
        st.success("Saved for this session.")

    # Status summary — highlight missing/expiring
    st.markdown('<div class="section-title">Document Status Overview</div>', unsafe_allow_html=True)

    alert_rows = []
    for _, row in edited.iterrows():
        for doc in REQUIRED_DOCS:
            emoji, label, colour = doc_status(row[doc])
            if emoji in ("❌", "⚠️"):
                alert_rows.append({
                    "Worker": row["Worker"],
                    "Document": doc,
                    "Status": f"{emoji} {label}",
                    "_colour": colour,
                })

    if not alert_rows:
        st.success("✅ All documents on file and valid.")
    else:
        alert_df = pd.DataFrame(alert_rows)
        expired_count  = sum(1 for r in alert_rows if "❌" in r["Status"])
        expiring_count = sum(1 for r in alert_rows if "⚠️" in r["Status"])
        col_e, col_w = st.columns(2)
        col_e.metric("❌ Expired / Missing", expired_count)
        col_w.metric("⚠️ Expiring Soon (60 days)", expiring_count)
        st.dataframe(alert_df[["Worker","Document","Status"]], use_container_width=True, hide_index=True)

    # Export document tracker
    st.markdown('<div class="section-title">Export & Save</div>', unsafe_allow_html=True)
    st.markdown("Download the document tracker as Excel, or copy the JSON below to paste into Streamlit Secrets to save permanently.")

    # Build full status table for export
    export_rows = []
    for _, row in edited.iterrows():
        for doc in REQUIRED_DOCS:
            emoji, label, _ = doc_status(row[doc])
            export_rows.append({"Worker": row["Worker"], "Document": doc,
                                 "Expiry Date": row[doc], "Status": f"{emoji} {label}"})
    export_doc_df = pd.DataFrame(export_rows)

    col_dl, col_json = st.columns(2)
    with col_dl:
        st.download_button(
            "⬇️  Download Excel",
            data=to_excel({"Document Tracker": export_doc_df}),
            file_name=f"ConnectCare_Documents_{today.strftime('%d-%b-%Y')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True, type="primary",
        )
    with col_json:
        json_str = json.dumps(st.session_state.doc_data, indent=2)
        st.download_button(
            "⬇️  Download JSON backup",
            data=json_str,
            file_name="documents_backup.json",
            mime="application/json",
            use_container_width=True,
        )

    with st.expander("How to save document data permanently"):
        st.markdown("""
1. Click **Download JSON backup** above
2. Open [share.streamlit.io](https://share.streamlit.io) → your app → ⚙️ Settings → Secrets
3. Add this line to your secrets:
```
DOCUMENTS_JSON = '<paste the JSON here on one line>'
```
4. Save — data will persist across sessions.
        """)

# ─────────────────────────────────────────────────
# TAB 5 — All Issues
# ─────────────────────────────────────────────────
with tab5:
    st.markdown('<div class="section-title">All Issues</div>', unsafe_allow_html=True)

    fc1, fc2, fc3 = st.columns(3)
    with fc1: f_sev = st.multiselect("Severity", SEV_ORDER, default=SEV_ORDER)
    with fc2: f_worker = st.multiselect("Worker", sorted(df_all["Worker"].unique()), placeholder="All workers")
    with fc3: f_client = st.multiselect("Client", sorted(df_all["Client"].unique()), placeholder="All clients")

    df_filtered = df_all.copy()
    if f_sev:    df_filtered = df_filtered[df_filtered["Severity"].isin(f_sev)]
    if f_worker: df_filtered = df_filtered[df_filtered["Worker"].isin(f_worker)]
    if f_client: df_filtered = df_filtered[df_filtered["Client"].isin(f_client)]

    st.caption(f"Showing {len(df_filtered)} of {n_total} issues")
    display = df_filtered[["Severity","Issue","Worker","Client","Date","Detail"]].copy()
    st.dataframe(display.style.apply(colour_row, axis=1), use_container_width=True,
                 height=600, hide_index=True, column_config={
                     "Severity": st.column_config.TextColumn(width="small"),
                     "Issue":    st.column_config.TextColumn("Issue Type", width="medium"),
                     "Worker":   st.column_config.TextColumn(width="medium"),
                     "Client":   st.column_config.TextColumn(width="medium"),
                     "Date":     st.column_config.TextColumn(width="small"),
                     "Detail":   st.column_config.TextColumn(width="large"),
                 })

# ─────────────────────────────────────────────────
# TAB 6 — Export
# ─────────────────────────────────────────────────
with tab6:
    st.markdown('<div class="section-title">Export Audit Data</div>', unsafe_allow_html=True)
    st.markdown("Download the full audit for this pay period to share with your manager or attach as evidence.")

    period_label = f"{start_date.strftime('%d-%b-%Y')}_to_{end_date.strftime('%d-%b-%Y')}"
    export_issues = df_all[["Severity","Issue","Worker","Client","Date","Detail"]].copy()
    export_issues.rename(columns={"Issue":"Issue Type"}, inplace=True)
    ws_export = wsummary.reset_index() if not df_staff.empty else pd.DataFrame()
    cs_export = csummary.reset_index()

    col_xl, col_csv = st.columns(2)
    with col_xl:
        st.markdown("#### Excel (.xlsx)")
        st.markdown("Three sheets: All Issues, Worker Summary, Client Summary.")
        sheets = {"All Issues": export_issues, "Client Summary": cs_export}
        if not ws_export.empty: sheets["Worker Summary"] = ws_export
        st.download_button("⬇️  Download Excel", data=to_excel(sheets),
                           file_name=f"ConnectCare_Audit_{period_label}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True, type="primary")
    with col_csv:
        st.markdown("#### CSV")
        st.markdown("All issues in a single CSV file.")
        st.download_button("⬇️  Download CSV", data=export_issues.to_csv(index=False),
                           file_name=f"ConnectCare_Audit_{period_label}.csv",
                           mime="text/csv", use_container_width=True)

    st.divider()
    st.markdown(f"""
**What's included:**
- **{len(export_issues)} issues** · {export_issues['Worker'].nunique()} workers · {export_issues['Client'].nunique()} clients
- Period: {start_date.strftime('%d %b %Y')} – {end_date.strftime('%d %b %Y')}
- Generated: {fetched_at}
""")

# ─────────────────────────────────────────────────
# TAB 7 — Notifications
# ─────────────────────────────────────────────────
with tab7:
    st.markdown('<div class="section-title">Worker Notifications & Acknowledgements</div>',
                unsafe_allow_html=True)
    st.caption("Send compliance notifications to workers via Connecteam chat. Track when they acknowledge and resolve each issue.")

    notifs = st.session_state.get("notifications", [])

    # ── Status summary ────────────────────────────────────────────────────────
    n_sent  = sum(1 for n in notifs if n["status"] == "Sent")
    n_ack   = sum(1 for n in notifs if n["status"] == "Acknowledged")
    n_res   = sum(1 for n in notifs if n["status"] == "Resolved")
    mc1, mc2, mc3, mc4 = st.columns(4)
    mc1.metric("📤 Total Sent",    len(notifs))
    mc2.metric("📬 Awaiting Reply", n_sent)
    mc3.metric("✅ Acknowledged",  n_ack)
    mc4.metric("🏁 Resolved",      n_res)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Bulk Notify section ───────────────────────────────────────────────────
    with st.expander("📢 Bulk Notify — send to multiple workers at once"):
        st.markdown("Select workers and send them all notifications about their HIGH/CRITICAL issues.")
        contacts = load_worker_contacts()

        workers_with_issues = (
            df_staff[df_staff["Severity"].isin(["CRITICAL","HIGH"])]
            ["Worker"].unique().tolist()
        )
        already_sent_this_period = {
            n["worker"] for n in notifs
            if n.get("period", "") == f"{start_date.strftime('%d %b')} – {end_date.strftime('%d %b %Y')}"
        }

        bulk_workers = st.multiselect(
            "Workers to notify",
            options=sorted(workers_with_issues),
            default=[w for w in sorted(workers_with_issues) if w not in already_sent_this_period],
            help="Pre-selected workers have not yet been notified this period.",
        )

        period_label_bulk = f"{start_date.strftime('%d %b')} – {end_date.strftime('%d %b %Y')}"

        if bulk_workers:
            bulk_channel = st.radio(
                "Send via",
                ["WhatsApp (sandbox — testing)", "SMS"],
                horizontal=True,
                key="bulk_channel",
            )
            st.caption(f"Will send {len(bulk_workers)} message(s). Each worker gets a personalised message listing only their own issues.")
            if st.button("📤  Send to All Selected", type="primary"):
                sent_ok, sent_fail = [], []
                for wname in bulk_workers:
                    winfo  = contacts.get(wname, {})
                    wid    = winfo.get("userId")
                    wphone = winfo.get("phone", "").strip()
                    if not wphone:
                        sent_fail.append(f"{wname} (no phone number)")
                        continue
                    wdf = df_staff[
                        (df_staff["Worker"] == wname) &
                        (df_staff["Severity"].isin(["CRITICAL","HIGH"]))
                    ][["Severity","Issue","Client","Date","Detail"]]
                    msg = build_notify_message(wname, wdf, period_label_bulk.lower())
                    if bulk_channel == "WhatsApp (sandbox — testing)":
                        ok, err = send_whatsapp(wphone, msg, sandbox=True)
                    else:
                        ok, err = send_sms(wphone, msg)
                    if ok:
                        log_notification(wname, wid, wdf, msg, period_label_bulk)
                        sent_ok.append(wname)
                    else:
                        sent_fail.append(f"{wname} ({err})")
                if sent_ok:
                    st.success(f"Sent to: {', '.join(sent_ok)}")
                if sent_fail:
                    st.error(f"Failed: {', '.join(sent_fail)}")
                if sent_ok:
                    st.rerun()

    st.divider()

    # ── Notification history ──────────────────────────────────────────────────
    if not notifs:
        st.info("No notifications sent yet. Use the 👤 By Worker tab to notify individual workers, or the Bulk Notify section above.")
    else:
        f_status = st.selectbox("Filter by status", ["All", "Sent", "Acknowledged", "Resolved"])
        filtered_notifs = notifs if f_status == "All" else [n for n in notifs if n["status"] == f_status]

        st.caption(f"Showing {len(filtered_notifs)} of {len(notifs)} notifications")

        STATUS_COLOUR = {"Sent": "#ff7f0e", "Acknowledged": "#4c78a8", "Resolved": "#2ca02c"}

        for idx, n in enumerate(filtered_notifs):
            sc = STATUS_COLOUR.get(n["status"], "#888")
            crit = n["severity_counts"].get("CRITICAL", 0)
            high = n["severity_counts"].get("HIGH", 0)
            med  = n["severity_counts"].get("MEDIUM", 0)

            with st.expander(
                f"**{n['worker']}** · {n['sent_at']} · "
                f"{'🔴 ' + str(crit) + ' Critical  ' if crit else ''}"
                f"{'🟠 ' + str(high) + ' High  ' if high else ''}"
                f"{'🟡 ' + str(med) + ' Medium  ' if med else ''}"
                f"· Status: **{n['status']}**",
                expanded=False,
            ):
                st.markdown(
                    f'<span style="background:{sc};color:white;border-radius:12px;'
                    f'padding:2px 12px;font-size:0.85rem;font-weight:600;">{n["status"]}</span>'
                    f'  &nbsp; Period: {n["period"]}  ·  {n["issue_count"]} issues',
                    unsafe_allow_html=True,
                )

                col_iss, col_msg = st.columns([1, 1])

                with col_iss:
                    st.markdown("**Issues sent:**")
                    for iss in n["issues"][:8]:
                        e = SEV_EMOJI.get(iss["Severity"], "•")
                        st.markdown(f"- {e} {iss['Issue']} — {iss['Client']}")
                    if len(n["issues"]) > 8:
                        st.caption(f"…and {len(n['issues']) - 8} more")

                with col_msg:
                    st.markdown("**Message sent:**")
                    st.code(n["message_sent"], language=None)

                # Extras
                extras = []
                if n.get("task_id"):
                    extras.append(f"Task created (ID: {n['task_id']})")
                if n.get("profile_note_added"):
                    extras.append("Profile note added")
                if extras:
                    st.caption(" · ".join(extras))

                st.markdown("<br>", unsafe_allow_html=True)

                # Status update controls
                act_col1, act_col2, act_col3 = st.columns(3)
                real_idx = notifs.index(n)

                with act_col1:
                    if n["status"] != "Acknowledged" and st.button(
                        "✅ Mark Acknowledged", key=f"ack_{n['id']}",
                        help="Worker has confirmed they are aware of the issues."
                    ):
                        notifs[real_idx]["status"] = "Acknowledged"
                        notifs[real_idx]["acknowledged_at"] = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")
                        st.rerun()

                with act_col2:
                    if n["status"] != "Resolved" and st.button(
                        "🏁 Mark Resolved", key=f"res_{n['id']}",
                        help="All issues have been fixed. Closes this notification."
                    ):
                        notifs[real_idx]["status"] = "Resolved"
                        notifs[real_idx]["resolved_at"] = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")
                        if notifs[real_idx]["acknowledged_at"] is None:
                            notifs[real_idx]["acknowledged_at"] = notifs[real_idx]["resolved_at"]
                        st.rerun()

                with act_col3:
                    if not n.get("profile_note_added") and st.button(
                        "📝 Add Profile Note", key=f"pnote_{n['id']}",
                        help="Log this notification on the worker's Connecteam HR profile."
                    ):
                        note_ok, note_err = add_worker_profile_note(
                            n["worker_id"],
                            f"Compliance notification: {n['issue_count']} issues for {n['period']}. "
                            f"Status: {n['status']}.",
                            title="Compliance Notification",
                        )
                        if note_ok:
                            notifs[real_idx]["profile_note_added"] = True
                            st.success("Note added to profile.")
                            st.rerun()
                        else:
                            st.error(f"Failed: {note_err}")

                # Manager notes
                note_key = f"note_{n['id']}"
                new_note = st.text_input(
                    "Manager notes",
                    value=n.get("manager_notes", ""),
                    placeholder="e.g. Worker called and confirmed, will fix next shift…",
                    key=note_key,
                )
                if new_note != n.get("manager_notes", ""):
                    notifs[real_idx]["manager_notes"] = new_note

                if n.get("acknowledged_at"):
                    st.caption(f"Acknowledged: {n['acknowledged_at']}")
                if n.get("resolved_at"):
                    st.caption(f"Resolved: {n['resolved_at']}")

        st.divider()
        # Export notification log
        if notifs:
            notif_export = pd.DataFrame([{
                "Worker":         n["worker"],
                "Sent At":        n["sent_at"],
                "Period":         n["period"],
                "Issues":         n["issue_count"],
                "Status":         n["status"],
                "Acknowledged":   n.get("acknowledged_at") or "",
                "Resolved":       n.get("resolved_at") or "",
                "Manager Notes":  n.get("manager_notes", ""),
                "Profile Note":   "Yes" if n.get("profile_note_added") else "No",
            } for n in notifs])
            st.download_button(
                "⬇️  Download Notification Log (Excel)",
                data=to_excel({"Notifications": notif_export}),
                file_name=f"ConnectCare_Notifications_{today.strftime('%d-%b-%Y')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=False,
            )
