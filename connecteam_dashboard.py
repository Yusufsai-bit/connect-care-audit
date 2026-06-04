"""
Connect Care Services — NDIS Compliance Audit Dashboard
Run: streamlit run connecteam_dashboard.py
"""

import os
import io
import datetime
import pandas as pd
import plotly.express as px
import streamlit as st

# Inject API keys from secrets before importing the audit engine
_ct_key = st.secrets.get("CONNECTEAM_API_KEY", "")
_ai_key = st.secrets.get("ANTHROPIC_API_KEY", "")
if _ct_key:
    os.environ["CONNECTEAM_API_KEY"] = _ct_key
if _ai_key:
    os.environ["ANTHROPIC_API_KEY"] = _ai_key

from connecteam_audit import run_audit, fetch_all_users

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Connect Care Compliance",
    page_icon="🛡️",
    layout="wide",
)

st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; }
    .metric-card {
        border-radius: 12px; padding: 1.2rem 1rem;
        text-align: center; border-left: 5px solid #ccc;
        background: #f8f9fa;
    }
    .metric-card.critical { border-left-color: #d62728; background: #fff5f5; }
    .metric-card.high     { border-left-color: #ff7f0e; background: #fff8f0; }
    .metric-card.medium   { border-left-color: #e6c200; background: #fffdf0; }
    .metric-card.total    { border-left-color: #4c78a8; background: #f0f4ff; }
    .metric-number { font-size: 2.4rem; font-weight: 700; line-height: 1; }
    .metric-label  { font-size: 0.85rem; color: #666; margin-top: 0.3rem; }
    .metric-delta  { font-size: 0.78rem; margin-top: 0.2rem; }
    .delta-up   { color: #d62728; }
    .delta-down { color: #2ca02c; }
    .delta-same { color: #888; }
    .alert-banner {
        background: #fff0f0; border: 1.5px solid #d62728;
        border-radius: 10px; padding: 0.9rem 1.2rem;
        margin-bottom: 1rem; font-weight: 600; color: #d62728;
    }
    .section-title {
        font-size: 1.05rem; font-weight: 600; color: #333;
        margin: 1.2rem 0 0.5rem; padding-bottom: 0.3rem;
        border-bottom: 2px solid #eee;
    }
    .contact-card {
        background: #f8f9fa; border-radius: 8px;
        padding: 0.7rem 1rem; margin-bottom: 0.4rem;
        display: flex; gap: 1.5rem; align-items: center;
    }
    .score-badge {
        display: inline-block; border-radius: 20px;
        padding: 2px 12px; font-size: 0.85rem; font-weight: 700;
        color: white;
    }
    div[data-testid="stTabs"] button { font-size: 0.95rem; font-weight: 500; }
</style>
""", unsafe_allow_html=True)

# ── Config ────────────────────────────────────────────────────────────────────

SEV_ORDER  = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
SEV_COLOUR = {"CRITICAL": "#d62728", "HIGH": "#ff7f0e", "MEDIUM": "#e6c200",
              "LOW": "#2ca02c", "INFO": "#aec7e8"}
SEV_TINT   = {"CRITICAL": "#fff5f5", "HIGH": "#fff8f0", "MEDIUM": "#fffdf0",
              "LOW": "#f0fff0", "INFO": "#f0f6ff"}
SEV_EMOJI  = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢", "INFO": "⚪"}

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
}

def plain(cat):
    return PLAIN_LABELS.get(cat, cat.title())

# ── Helpers ───────────────────────────────────────────────────────────────────

def compliance_score(worker_df):
    """0–100 score. Deducts per issue severity. Red <60, amber 60–79, green ≥80."""
    deductions = {"CRITICAL": 15, "HIGH": 8, "MEDIUM": 3, "LOW": 1}
    total = worker_df["Severity"].map(deductions).fillna(0).sum()
    return max(0, 100 - int(total))

def score_colour(score):
    if score >= 80: return "#2ca02c"
    if score >= 60: return "#ff7f0e"
    return "#d62728"

def score_label(score):
    if score >= 80: return "Good"
    if score >= 60: return "Needs Attention"
    return "At Risk"

def to_excel(dfs: dict) -> bytes:
    """Export a dict of {sheet_name: dataframe} to Excel bytes."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for sheet, df in dfs.items():
            df.to_excel(writer, sheet_name=sheet[:31], index=False)
    return buf.getvalue()

def colour_row(row):
    tint = SEV_TINT.get(row["Severity"], "#ffffff")
    return [f"background-color: {tint}"] * len(row)

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
def load_prev_audit(days_back: int):
    """Runs audit for 2× the period to get previous-period count."""
    issues = run_audit(days_back * 2)
    return len(issues)

@st.cache_data(ttl=600, show_spinner=False)
def load_worker_contacts():
    """Returns {full_name: {phone, email}} from Connecteam."""
    try:
        users = fetch_all_users()
        contacts = {}
        for u in users.values():
            name = f"{u.get('firstName', '')} {u.get('lastName', '')}".strip()
            if name:
                contacts[name] = {
                    "phone": u.get("phoneNumber") or u.get("phone") or "",
                    "email": u.get("email") or "",
                }
        return contacts
    except Exception:
        return {}

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("### 🛡️ Connect Care")
    st.markdown("##### Compliance Dashboard")
    st.divider()

    period_mode = st.radio("Audit period", ["Quick select", "Custom dates"], horizontal=True)
    today = datetime.date.today()

    if period_mode == "Quick select":
        days_back  = st.selectbox("Period", [7, 14, 30], index=0,
                                  format_func=lambda d: f"Last {d} days",
                                  label_visibility="collapsed")
        start_date = today - datetime.timedelta(days=days_back)
    else:
        date_range = st.date_input("From / To",
                                   value=(today - datetime.timedelta(days=7), today),
                                   max_value=today, label_visibility="collapsed")
        if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
            start_date = date_range[0]
        else:
            start_date = date_range[0] if date_range else today - datetime.timedelta(days=7)
        days_back = max((today - start_date).days, 1)

    compare_prev = st.checkbox("Compare with previous period", value=False)

    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("🔄  Refresh Data", use_container_width=True, type="primary"):
        st.cache_data.clear()
        st.rerun()

    with st.spinner("Loading…"):
        all_issues, fetched_at = load_audit(days_back)
        prev_total = load_prev_audit(days_back) if compare_prev else None

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

df_all = pd.DataFrame([
    {"Severity": i.severity, "Category": i.category, "Issue": plain(i.category),
     "Worker": i.worker, "Client": i.client, "Date": i.date, "Detail": i.detail}
    for i in all_issues
])
df_all["_rank"] = df_all["Severity"].map({s: idx for idx, s in enumerate(SEV_ORDER)})
df_all = df_all.sort_values("_rank").drop(columns="_rank")

counts     = df_all["Severity"].value_counts()
n_critical = counts.get("CRITICAL", 0)
n_high     = counts.get("HIGH", 0)
n_medium   = counts.get("MEDIUM", 0)
n_total    = len(df_all)

# Week-on-week deltas
def delta_html(current, previous_total, days_back):
    if previous_total is None:
        return ""
    prev = previous_total - n_total   # approx previous period count
    diff = current - (prev * current / max(n_total, 1))
    # simpler: just compare overall totals
    return ""

def period_delta_badge(current_count, prev_total):
    """Returns HTML delta string comparing current vs estimated previous period."""
    if prev_total is None:
        return ""
    est_prev = prev_total - n_total
    diff = current_count - round(est_prev * current_count / max(n_total, 1))
    if diff > 0:
        return f'<div class="metric-delta delta-up">▲ {diff} vs prev period</div>'
    elif diff < 0:
        return f'<div class="metric-delta delta-down">▼ {abs(diff)} vs prev period</div>'
    else:
        return f'<div class="metric-delta delta-same">— same as prev period</div>'

# ── Header ────────────────────────────────────────────────────────────────────

st.markdown("## NDIS Compliance Audit")
st.caption(f"{start_date.strftime('%d %b %Y')} – {today.strftime('%d %b %Y')}  ·  {n_total} issues found")

if n_critical > 0:
    st.markdown(
        f'<div class="alert-banner">⚠️  {n_critical} critical issue{"s" if n_critical > 1 else ""} '
        f'require immediate attention — see the Action Required tab below.</div>',
        unsafe_allow_html=True)

# Metric cards
c1, c2, c3, c4 = st.columns(4)
for col, cls, colour, label, count in [
    (c1, "critical", "#d62728", "🔴 Critical",     n_critical),
    (c2, "high",     "#ff7f0e", "🟠 High Priority", n_high),
    (c3, "medium",   "#b8980a", "🟡 Medium",        n_medium),
    (c4, "total",    "#4c78a8", "📋 Total Issues",  n_total),
]:
    delta = period_delta_badge(count, prev_total)
    col.markdown(
        f'<div class="metric-card {cls}">'
        f'<div class="metric-number" style="color:{colour}">{count}</div>'
        f'<div class="metric-label">{label}</div>'
        f'{delta}</div>',
        unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# ── Tabs ──────────────────────────────────────────────────────────────────────

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "🚨  Action Required",
    "👤  By Worker",
    "👥  By Client",
    "📋  All Issues",
    "📥  Export",
])

# ─────────────────────────────────────────────────
# TAB 1 — Action Required
# ─────────────────────────────────────────────────
with tab1:
    st.markdown('<div class="section-title">Issues Needing Immediate Action</div>',
                unsafe_allow_html=True)
    urgent = df_all[df_all["Severity"].isin(["CRITICAL", "HIGH"])].copy()

    if urgent.empty:
        st.success("✅ No critical or high priority issues.")
    else:
        summary = (urgent.groupby(["Severity", "Issue"]).size()
                   .reset_index(name="Count"))
        summary["_rank"] = summary["Severity"].map({s: i for i, s in enumerate(SEV_ORDER)})
        summary = summary.sort_values(["_rank", "Count"], ascending=[True, False]).drop(columns="_rank")

        for _, row in summary.iterrows():
            colour = SEV_COLOUR[row["Severity"]]
            tint   = SEV_TINT[row["Severity"]]
            emoji  = SEV_EMOJI[row["Severity"]]
            st.markdown(
                f'<div style="background:{tint};border-left:4px solid {colour};'
                f'border-radius:8px;padding:0.7rem 1rem;margin-bottom:0.5rem;'
                f'display:flex;justify-content:space-between;align-items:center;">'
                f'<span>{emoji} <strong>{row["Issue"]}</strong></span>'
                f'<span style="background:{colour};color:white;border-radius:20px;'
                f'padding:2px 12px;font-size:0.85rem;font-weight:600;">'
                f'{row["Count"]} case{"s" if row["Count"] > 1 else ""}</span></div>',
                unsafe_allow_html=True)

    late_inc = df_all[df_all["Category"] == "LATE INCIDENT REPORTING"]
    if not late_inc.empty:
        st.markdown('<div class="section-title">⚠️ Late Incident Reports — NDIS Commission Risk</div>',
                    unsafe_allow_html=True)
        st.warning(
            f"{len(late_inc)} incident report(s) were filed late. "
            "NDIS requires serious incidents within 24 hours. Review these urgently.")
        for _, row in late_inc.iterrows():
            st.markdown(f"- **{row['Worker']}** · {row['Detail']}")

    restrict = df_all[df_all["Category"] == "RESTRICTIVE PRACTICE MENTIONED"]
    if not restrict.empty:
        st.markdown('<div class="section-title">🚫 Restrictive Practice — Authorisation Required</div>',
                    unsafe_allow_html=True)
        st.error(
            f"{len(restrict)} note(s) mention restrictive practice. "
            "Formal authorisation documentation is required under NDIS rules.")
        for _, row in restrict.iterrows():
            st.markdown(f"- **{row['Worker']}** · {row['Client']} · {row['Date']}")

# ─────────────────────────────────────────────────
# TAB 2 — By Worker
# ─────────────────────────────────────────────────
with tab2:
    st.markdown('<div class="section-title">Worker Compliance Scores</div>',
                unsafe_allow_html=True)
    st.caption("Score starts at 100. Deducts 15 per Critical, 8 per High, 3 per Medium, 1 per Low.")

    workers_df = df_all[df_all["Worker"] != "(team)"]
    worker_summary = (workers_df.groupby(["Worker", "Severity"]).size()
                      .unstack(fill_value=0))
    for col in SEV_ORDER:
        if col not in worker_summary.columns:
            worker_summary[col] = 0
    worker_summary = worker_summary[[c for c in SEV_ORDER if c in worker_summary.columns]]
    worker_summary["Total"] = worker_summary.sum(axis=1)
    worker_summary["Score"] = [
        compliance_score(workers_df[workers_df["Worker"] == w])
        for w in worker_summary.index
    ]
    worker_summary["Status"] = worker_summary["Score"].apply(score_label)
    worker_summary = worker_summary.sort_values("Score", ascending=True)

    st.dataframe(
        worker_summary,
        use_container_width=True,
        column_config={
            "CRITICAL": st.column_config.NumberColumn("🔴 Critical"),
            "HIGH":     st.column_config.NumberColumn("🟠 High"),
            "MEDIUM":   st.column_config.NumberColumn("🟡 Medium"),
            "LOW":      st.column_config.NumberColumn("🟢 Low"),
            "Total":    st.column_config.NumberColumn("Total Issues"),
            "Score":    st.column_config.ProgressColumn(
                            "Compliance Score", min_value=0, max_value=100,
                            format="%d%%"),
            "Status":   st.column_config.TextColumn("Status"),
        },
    )

    st.markdown('<div class="section-title">Drill Into a Worker</div>',
                unsafe_allow_html=True)

    workers_list = sorted(workers_df["Worker"].unique())
    selected_worker = st.selectbox("Select worker", ["— pick a worker —"] + workers_list,
                                   key="worker_select")

    if selected_worker != "— pick a worker —":
        score = compliance_score(workers_df[workers_df["Worker"] == selected_worker])
        sc    = score_colour(score)
        sl    = score_label(score)

        # Score badge + contact details side by side
        col_score, col_contact = st.columns([1, 2])

        with col_score:
            st.markdown(
                f'<div style="text-align:center;padding:1rem;">'
                f'<div style="font-size:3rem;font-weight:700;color:{sc}">{score}</div>'
                f'<div style="font-size:0.9rem;color:{sc};font-weight:600">{sl}</div>'
                f'<div style="font-size:0.75rem;color:#888;margin-top:0.3rem">Compliance Score</div>'
                f'</div>', unsafe_allow_html=True)

        with col_contact:
            with st.spinner("Loading contact…"):
                contacts = load_worker_contacts()
            info = contacts.get(selected_worker, {})
            phone = info.get("phone", "")
            email = info.get("email", "")
            st.markdown('<div style="margin-top:0.8rem">', unsafe_allow_html=True)
            if phone:
                st.markdown(f"📞 **Phone:** [{phone}](tel:{phone})")
            else:
                st.markdown("📞 **Phone:** _not on file_")
            if email:
                st.markdown(f"✉️ **Email:** [{email}](mailto:{email})")
            else:
                st.markdown("✉️ **Email:** _not on file_")
            st.markdown('</div>', unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        wdf = workers_df[workers_df["Worker"] == selected_worker][
            ["Severity", "Issue", "Client", "Date", "Detail"]]
        st.dataframe(wdf.style.apply(colour_row, axis=1),
                     use_container_width=True, hide_index=True)

# ─────────────────────────────────────────────────
# TAB 3 — By Client
# ─────────────────────────────────────────────────
with tab3:
    st.markdown('<div class="section-title">Client Issue Summary</div>',
                unsafe_allow_html=True)

    client_summary = (df_all.groupby(["Client", "Severity"]).size()
                      .unstack(fill_value=0))
    for col in SEV_ORDER:
        if col not in client_summary.columns:
            client_summary[col] = 0
    client_summary = client_summary[[c for c in SEV_ORDER if c in client_summary.columns]]
    client_summary["Total"] = client_summary.sum(axis=1)
    client_summary = client_summary.sort_values("CRITICAL", ascending=False)

    st.dataframe(
        client_summary,
        use_container_width=True,
        column_config={
            "CRITICAL": st.column_config.NumberColumn("🔴 Critical"),
            "HIGH":     st.column_config.NumberColumn("🟠 High"),
            "MEDIUM":   st.column_config.NumberColumn("🟡 Medium"),
            "LOW":      st.column_config.NumberColumn("🟢 Low"),
            "Total":    st.column_config.NumberColumn("Total"),
        },
    )

    # Form completion rates per client
    FORM_CATS = {
        "Kallan Jordan":  ["MISSING FORM -- KALLAN"],
        "Evan Gatt":      ["MISSING FORM -- EVAN"],
        "Michael Lawrie": ["MISSING FORM -- MICHAEL"],
        "Joshua Gatt":    ["FORM FREQUENCY -- JOSHUA"],
        "Nada Haliem":    ["FORM FREQUENCY -- NADA"],
        "John":           ["FORM FREQUENCY -- JOHN"],
        "Nicole Loveless":["FORM FREQUENCY -- NICOLE"],
    }

    clients_with_forms = [c for c in FORM_CATS if c in df_all["Client"].unique() or
                          any(df_all["Category"].str.contains(c.split()[0].lower(), case=False))]

    if clients_with_forms or any(df_all["Category"].str.startswith("MISSING FORM") |
                                  df_all["Category"].str.startswith("FORM FREQUENCY")):
        st.markdown('<div class="section-title">Form Completion This Period</div>',
                    unsafe_allow_html=True)
        st.caption("Missing days = days where required forms were not submitted.")

        form_issues = df_all[df_all["Category"].str.startswith("MISSING FORM") |
                             df_all["Category"].str.startswith("FORM FREQUENCY")]

        if not form_issues.empty:
            form_summary = (form_issues.groupby(["Client", "Issue"])
                            .size().reset_index(name="Missing Days"))
            for client_name, grp in form_summary.groupby("Client"):
                st.markdown(f"**{client_name}**")
                for _, frow in grp.iterrows():
                    total_days = df_all[df_all["Client"] == client_name]["Date"].nunique()
                    missing    = frow["Missing Days"]
                    submitted  = max(total_days - missing, 0)
                    pct        = round(submitted / total_days * 100) if total_days > 0 else 0
                    bar_col    = "#2ca02c" if pct >= 80 else "#ff7f0e" if pct >= 50 else "#d62728"
                    st.markdown(
                        f'<div style="margin-bottom:0.4rem">'
                        f'<span style="font-size:0.85rem">{frow["Issue"]}</span> — '
                        f'<strong style="color:{bar_col}">{pct}%</strong> '
                        f'<span style="color:#888;font-size:0.8rem">({submitted}/{total_days} days)</span>'
                        f'</div>',
                        unsafe_allow_html=True)

    st.markdown('<div class="section-title">Drill Into a Client</div>',
                unsafe_allow_html=True)
    clients_list = sorted(df_all["Client"].unique())
    selected_client = st.selectbox("Select client", ["— pick a client —"] + clients_list,
                                   key="client_select")

    if selected_client != "— pick a client —":
        cdf = df_all[df_all["Client"] == selected_client][
            ["Severity", "Issue", "Worker", "Date", "Detail"]]
        st.dataframe(cdf.style.apply(colour_row, axis=1),
                     use_container_width=True, hide_index=True)

# ─────────────────────────────────────────────────
# TAB 4 — All Issues
# ─────────────────────────────────────────────────
with tab4:
    st.markdown('<div class="section-title">All Issues</div>', unsafe_allow_html=True)

    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        f_sev = st.multiselect("Severity", SEV_ORDER, default=SEV_ORDER)
    with fc2:
        f_worker = st.multiselect("Worker", sorted(df_all["Worker"].unique()),
                                  placeholder="All workers")
    with fc3:
        f_client = st.multiselect("Client", sorted(df_all["Client"].unique()),
                                  placeholder="All clients")

    df_filtered = df_all.copy()
    if f_sev:    df_filtered = df_filtered[df_filtered["Severity"].isin(f_sev)]
    if f_worker: df_filtered = df_filtered[df_filtered["Worker"].isin(f_worker)]
    if f_client: df_filtered = df_filtered[df_filtered["Client"].isin(f_client)]

    st.caption(f"Showing {len(df_filtered)} of {n_total} issues")

    display = df_filtered[["Severity", "Issue", "Worker", "Client", "Date", "Detail"]].copy()
    st.dataframe(
        display.style.apply(colour_row, axis=1),
        use_container_width=True, height=600, hide_index=True,
        column_config={
            "Severity": st.column_config.TextColumn(width="small"),
            "Issue":    st.column_config.TextColumn("Issue Type", width="medium"),
            "Worker":   st.column_config.TextColumn(width="medium"),
            "Client":   st.column_config.TextColumn(width="medium"),
            "Date":     st.column_config.TextColumn(width="small"),
            "Detail":   st.column_config.TextColumn(width="large"),
        },
    )

# ─────────────────────────────────────────────────
# TAB 5 — Export
# ─────────────────────────────────────────────────
with tab5:
    st.markdown('<div class="section-title">Export Audit Data</div>', unsafe_allow_html=True)
    st.markdown("Download the full audit for this period to share with your manager or attach as evidence.")

    period_label = f"{start_date.strftime('%d-%b-%Y')}_to_{today.strftime('%d-%b-%Y')}"

    # Build worker summary for export
    ws_export = worker_summary.reset_index()

    # Build client summary for export
    cs_export = client_summary.reset_index()

    # Build all issues for export
    export_df = df_all[["Severity", "Issue", "Worker", "Client", "Date", "Detail"]].copy()
    export_df.rename(columns={"Issue": "Issue Type"}, inplace=True)

    col_xl, col_csv = st.columns(2)

    with col_xl:
        st.markdown("#### Excel (.xlsx)")
        st.markdown("Three sheets: All Issues, Worker Summary, Client Summary.")
        excel_bytes = to_excel({
            "All Issues":      export_df,
            "Worker Summary":  ws_export,
            "Client Summary":  cs_export,
        })
        st.download_button(
            label="⬇️  Download Excel",
            data=excel_bytes,
            file_name=f"ConnectCare_Audit_{period_label}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            type="primary",
        )

    with col_csv:
        st.markdown("#### CSV")
        st.markdown("All issues in a single CSV file.")
        st.download_button(
            label="⬇️  Download CSV",
            data=export_df.to_csv(index=False),
            file_name=f"ConnectCare_Audit_{period_label}.csv",
            mime="text/csv",
            use_container_width=True,
        )

    st.divider()
    st.markdown("**What's included in the export:**")
    st.markdown(f"""
- **{len(export_df)} issues** across {export_df['Worker'].nunique()} workers and {export_df['Client'].nunique()} clients
- Period: {start_date.strftime('%d %b %Y')} – {today.strftime('%d %b %Y')}
- Generated: {fetched_at}
""")
