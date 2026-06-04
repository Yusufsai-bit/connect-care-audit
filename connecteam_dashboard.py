"""
Connect Care Services — NDIS Compliance Audit Dashboard
Run: streamlit run connecteam_dashboard.py
"""

import os
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

from connecteam_audit import run_audit

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
        background: #f8f9fa;
        border-radius: 12px;
        padding: 1.2rem 1rem;
        text-align: center;
        border-left: 5px solid #ccc;
    }
    .metric-card.critical { border-left-color: #d62728; background: #fff5f5; }
    .metric-card.high     { border-left-color: #ff7f0e; background: #fff8f0; }
    .metric-card.medium   { border-left-color: #e6c200; background: #fffdf0; }
    .metric-card.ok       { border-left-color: #2ca02c; background: #f0fff0; }
    .metric-number { font-size: 2.4rem; font-weight: 700; line-height: 1; }
    .metric-label  { font-size: 0.85rem; color: #666; margin-top: 0.3rem; }
    .alert-banner {
        background: #fff0f0;
        border: 1.5px solid #d62728;
        border-radius: 10px;
        padding: 0.9rem 1.2rem;
        margin-bottom: 1rem;
        font-weight: 600;
        color: #d62728;
    }
    .section-title {
        font-size: 1.1rem;
        font-weight: 600;
        color: #333;
        margin: 1.2rem 0 0.5rem;
        padding-bottom: 0.3rem;
        border-bottom: 2px solid #eee;
    }
    div[data-testid="stTabs"] button { font-size: 0.95rem; font-weight: 500; }
</style>
""", unsafe_allow_html=True)

# ── Severity config ───────────────────────────────────────────────────────────

SEV_ORDER  = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
SEV_COLOUR = {
    "CRITICAL": "#d62728",
    "HIGH":     "#ff7f0e",
    "MEDIUM":   "#e6c200",
    "LOW":      "#2ca02c",
    "INFO":     "#aec7e8",
}
SEV_TINT = {
    "CRITICAL": "#fff5f5",
    "HIGH":     "#fff8f0",
    "MEDIUM":   "#fffdf0",
    "LOW":      "#f0fff0",
    "INFO":     "#f0f6ff",
}
SEV_EMOJI = {
    "CRITICAL": "🔴",
    "HIGH":     "🟠",
    "MEDIUM":   "🟡",
    "LOW":      "🟢",
    "INFO":     "⚪",
}

# Plain English category labels
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

def plain(category):
    return PLAIN_LABELS.get(category, category.title())

# ── Password gate ─────────────────────────────────────────────────────────────

if not st.session_state.get("authenticated"):
    st.markdown("<br><br>", unsafe_allow_html=True)
    col_a, col_b, col_c = st.columns([1, 1.2, 1])
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

# ── Audit data ────────────────────────────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def load_audit(days_back: int):
    issues = run_audit(days_back)
    fetched_at = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")
    return issues, fetched_at

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("### 🛡️ Connect Care")
    st.markdown("##### Compliance Dashboard")
    st.divider()

    period_mode = st.radio(
        "Audit period",
        ["Quick select", "Custom dates"],
        horizontal=True,
    )

    today = datetime.date.today()

    if period_mode == "Quick select":
        days_back = st.selectbox(
            "Period",
            [7, 14, 30],
            index=0,
            format_func=lambda d: f"Last {d} days",
            label_visibility="collapsed",
        )
        start_date = today - datetime.timedelta(days=days_back)
    else:
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
        days_back = max((today - start_date).days, 1)

    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("🔄  Refresh Data", use_container_width=True, type="primary"):
        st.cache_data.clear()
        st.rerun()

    with st.spinner("Loading…"):
        all_issues, fetched_at = load_audit(days_back)

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
    {
        "Severity":    i.severity,
        "Category":    i.category,
        "Issue":       plain(i.category),
        "Worker":      i.worker,
        "Client":      i.client,
        "Date":        i.date,
        "Detail":      i.detail,
    }
    for i in all_issues
])

df_all["_rank"] = df_all["Severity"].map({s: idx for idx, s in enumerate(SEV_ORDER)})
df_all = df_all.sort_values("_rank").drop(columns="_rank")

counts = df_all["Severity"].value_counts()
n_critical = counts.get("CRITICAL", 0)
n_high     = counts.get("HIGH", 0)
n_medium   = counts.get("MEDIUM", 0)
n_total    = len(df_all)

# ── Header ────────────────────────────────────────────────────────────────────

st.markdown("## NDIS Compliance Audit")

st.caption(f"{start_date.strftime('%d %b %Y')} – {today.strftime('%d %b %Y')}  ·  {n_total} issues found")

# Alert banner
if n_critical > 0:
    st.markdown(
        f'<div class="alert-banner">⚠️  {n_critical} critical issue{"s" if n_critical > 1 else ""} '
        f'require immediate attention — see the Action Required tab below.</div>',
        unsafe_allow_html=True,
    )

# Metric cards
c1, c2, c3, c4 = st.columns(4)
with c1:
    st.markdown(f"""<div class="metric-card critical">
        <div class="metric-number" style="color:#d62728">{n_critical}</div>
        <div class="metric-label">🔴 Critical</div>
    </div>""", unsafe_allow_html=True)
with c2:
    st.markdown(f"""<div class="metric-card high">
        <div class="metric-number" style="color:#ff7f0e">{n_high}</div>
        <div class="metric-label">🟠 High Priority</div>
    </div>""", unsafe_allow_html=True)
with c3:
    st.markdown(f"""<div class="metric-card medium">
        <div class="metric-number" style="color:#b8980a">{n_medium}</div>
        <div class="metric-label">🟡 Medium</div>
    </div>""", unsafe_allow_html=True)
with c4:
    ok_count = counts.get("LOW", 0) + counts.get("INFO", 0)
    st.markdown(f"""<div class="metric-card ok">
        <div class="metric-number" style="color:#333">{n_total}</div>
        <div class="metric-label">📋 Total Issues</div>
    </div>""", unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# ── Tabs ──────────────────────────────────────────────────────────────────────

tab1, tab2, tab3, tab4 = st.tabs([
    "🚨  Action Required",
    "👤  By Worker",
    "👥  By Client",
    "📋  All Issues",
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
        # Group by issue type for a clean summary
        summary = (
            urgent.groupby(["Severity", "Issue"])
            .size()
            .reset_index(name="Count")
            .sort_values(["Severity", "Count"], ascending=[True, False])
        )
        summary["_rank"] = summary["Severity"].map({s: i for i, s in enumerate(SEV_ORDER)})
        summary = summary.sort_values("_rank").drop(columns="_rank")

        for _, row in summary.iterrows():
            colour = SEV_COLOUR[row["Severity"]]
            tint   = SEV_TINT[row["Severity"]]
            emoji  = SEV_EMOJI[row["Severity"]]
            st.markdown(
                f"""<div style="background:{tint}; border-left:4px solid {colour};
                    border-radius:8px; padding:0.7rem 1rem; margin-bottom:0.5rem;
                    display:flex; justify-content:space-between; align-items:center;">
                    <span>{emoji} <strong>{row['Issue']}</strong></span>
                    <span style="background:{colour}; color:white; border-radius:20px;
                        padding:2px 12px; font-size:0.85rem; font-weight:600;">
                        {row['Count']} case{"s" if row['Count'] > 1 else ""}
                    </span>
                </div>""",
                unsafe_allow_html=True,
            )

    # Late incident reporting section (always needs human follow-up)
    late_inc = df_all[df_all["Category"] == "LATE INCIDENT REPORTING"]
    if not late_inc.empty:
        st.markdown('<div class="section-title">Late Incident Reports — NDIS Commission Risk</div>',
                    unsafe_allow_html=True)
        st.warning(
            f"⚠️ {len(late_inc)} incident report(s) were filed late. "
            "NDIS requires serious incidents within 24 hours. Review these with your manager."
        )
        for _, row in late_inc.iterrows():
            st.markdown(f"- **{row['Worker']}** · {row['Detail']}")

# ─────────────────────────────────────────────────
# TAB 2 — By Worker
# ─────────────────────────────────────────────────
with tab2:
    st.markdown('<div class="section-title">Worker Issue Summary</div>',
                unsafe_allow_html=True)
    st.caption("Click a worker's name to see their specific issues below.")

    # Worker summary table
    worker_summary = (
        df_all[df_all["Worker"] != "(team)"]
        .groupby(["Worker", "Severity"])
        .size()
        .unstack(fill_value=0)
    )
    for col in SEV_ORDER:
        if col not in worker_summary.columns:
            worker_summary[col] = 0
    worker_summary = worker_summary[
        [c for c in SEV_ORDER if c in worker_summary.columns]
    ]
    worker_summary["Total"] = worker_summary.sum(axis=1)
    worker_summary = worker_summary.sort_values("CRITICAL", ascending=False)

    st.dataframe(
        worker_summary,
        use_container_width=True,
        column_config={
            "CRITICAL": st.column_config.NumberColumn("🔴 Critical"),
            "HIGH":     st.column_config.NumberColumn("🟠 High"),
            "MEDIUM":   st.column_config.NumberColumn("🟡 Medium"),
            "LOW":      st.column_config.NumberColumn("🟢 Low"),
            "Total":    st.column_config.NumberColumn("Total"),
        },
    )

    st.markdown('<div class="section-title">Drill Into a Worker</div>',
                unsafe_allow_html=True)
    workers = sorted(df_all[df_all["Worker"] != "(team)"]["Worker"].unique())
    selected_worker = st.selectbox("Select worker", ["— pick a worker —"] + workers)

    if selected_worker != "— pick a worker —":
        wdf = df_all[df_all["Worker"] == selected_worker][
            ["Severity", "Issue", "Client", "Date", "Detail"]
        ]
        def colour_row(row):
            tint = SEV_TINT.get(row["Severity"], "#ffffff")
            return [f"background-color: {tint}"] * len(row)
        st.dataframe(
            wdf.style.apply(colour_row, axis=1),
            use_container_width=True,
            hide_index=True,
        )

# ─────────────────────────────────────────────────
# TAB 3 — By Client
# ─────────────────────────────────────────────────
with tab3:
    st.markdown('<div class="section-title">Client Issue Summary</div>',
                unsafe_allow_html=True)

    client_summary = (
        df_all.groupby(["Client", "Severity"])
        .size()
        .unstack(fill_value=0)
    )
    for col in SEV_ORDER:
        if col not in client_summary.columns:
            client_summary[col] = 0
    client_summary = client_summary[
        [c for c in SEV_ORDER if c in client_summary.columns]
    ]
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

    st.markdown('<div class="section-title">Drill Into a Client</div>',
                unsafe_allow_html=True)
    clients = sorted(df_all["Client"].unique())
    selected_client = st.selectbox("Select client", ["— pick a client —"] + clients)

    if selected_client != "— pick a client —":
        cdf = df_all[df_all["Client"] == selected_client][
            ["Severity", "Issue", "Worker", "Date", "Detail"]
        ]
        def colour_row_c(row):
            tint = SEV_TINT.get(row["Severity"], "#ffffff")
            return [f"background-color: {tint}"] * len(row)
        st.dataframe(
            cdf.style.apply(colour_row_c, axis=1),
            use_container_width=True,
            hide_index=True,
        )

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
    if f_sev:
        df_filtered = df_filtered[df_filtered["Severity"].isin(f_sev)]
    if f_worker:
        df_filtered = df_filtered[df_filtered["Worker"].isin(f_worker)]
    if f_client:
        df_filtered = df_filtered[df_filtered["Client"].isin(f_client)]

    st.caption(f"Showing {len(df_filtered)} of {n_total} issues")

    display = df_filtered[["Severity", "Issue", "Worker", "Client", "Date", "Detail"]].copy()

    def colour_all(row):
        tint = SEV_TINT.get(row["Severity"], "#ffffff")
        return [f"background-color: {tint}"] * len(row)

    st.dataframe(
        display.style.apply(colour_all, axis=1),
        use_container_width=True,
        height=600,
        hide_index=True,
        column_config={
            "Severity": st.column_config.TextColumn(width="small"),
            "Issue":    st.column_config.TextColumn("Issue Type", width="medium"),
            "Worker":   st.column_config.TextColumn(width="medium"),
            "Client":   st.column_config.TextColumn(width="medium"),
            "Date":     st.column_config.TextColumn(width="small"),
            "Detail":   st.column_config.TextColumn(width="large"),
        },
    )
