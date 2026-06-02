"""
Connect Care Services — NDIS Compliance Audit Dashboard
Run: streamlit run connecteam_dashboard.py
"""

import os
import sys
import datetime
import pandas as pd
import plotly.express as px
import streamlit as st

# Inject API keys from secrets before importing the audit engine
# Only override if the secret is actually set — otherwise let the audit script use its own defaults
_ct_key = st.secrets.get("CONNECTEAM_API_KEY", "")
_ai_key = st.secrets.get("ANTHROPIC_API_KEY", "")
if _ct_key:
    os.environ["CONNECTEAM_API_KEY"] = _ct_key
if _ai_key:
    os.environ["ANTHROPIC_API_KEY"] = _ai_key

import io
from connecteam_audit import run_audit

# ── Config ────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Connect Care Audit",
    page_icon="🛡️",
    layout="wide",
)

SEV_ORDER  = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
SEV_COLOUR = {
    "CRITICAL": "#d62728",
    "HIGH":     "#ff7f0e",
    "MEDIUM":   "#e6c200",
    "LOW":      "#2ca02c",
    "INFO":     "#aec7e8",
}
SEV_EMOJI  = {
    "CRITICAL": "🔴",
    "HIGH":     "🟠",
    "MEDIUM":   "🟡",
    "LOW":      "🟢",
    "INFO":     "⚪",
}

# ── Password gate ─────────────────────────────────────────────────────────────

def login_screen():
    st.markdown("## 🛡️ Connect Care — Compliance Audit")
    st.markdown("Enter your password to access the dashboard.")
    pw = st.text_input("Password", type="password", key="pw_input")
    if st.button("Login"):
        if pw == st.secrets.get("DASHBOARD_PASSWORD", ""):
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect password.")

if not st.session_state.get("authenticated"):
    login_screen()
    st.stop()

# ── Audit data (cached 5 min) ─────────────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def load_audit(days_back: int):
    issues = run_audit(days_back)
    fetched_at = datetime.datetime.now().strftime("%d %b %Y %I:%M %p")
    return issues, fetched_at

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## ⚙️ Controls")
    days_back = st.selectbox("Audit window", [7, 14, 30], index=0,
                             format_func=lambda d: f"Past {d} days")

    if st.button("▶ Run Audit", use_container_width=True, type="primary"):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.markdown("### Filters")

    with st.spinner("Loading audit data…"):
        all_issues, fetched_at = load_audit(days_back)

    if not all_issues:
        st.success("No issues found.")
        st.stop()

    df_all = pd.DataFrame([
        {
            "Severity": i.severity,
            "Category": i.category,
            "Worker":   i.worker,
            "Client":   i.client,
            "Date":     i.date,
            "Detail":   i.detail,
        }
        for i in all_issues
    ])

    sev_filter = st.multiselect(
        "Severity",
        options=SEV_ORDER,
        default=SEV_ORDER,
    )
    worker_filter = st.multiselect(
        "Worker",
        options=sorted(df_all["Worker"].unique()),
        default=[],
        placeholder="All workers",
    )
    client_filter = st.multiselect(
        "Client",
        options=sorted(df_all["Client"].unique()),
        default=[],
        placeholder="All clients",
    )
    cat_filter = st.multiselect(
        "Category",
        options=sorted(df_all["Category"].unique()),
        default=[],
        placeholder="All categories",
    )

    st.divider()
    if st.button("Logout", use_container_width=True):
        st.session_state.authenticated = False
        st.rerun()

# ── Apply filters ─────────────────────────────────────────────────────────────

df = df_all.copy()
if sev_filter:
    df = df[df["Severity"].isin(sev_filter)]
if worker_filter:
    df = df[df["Worker"].isin(worker_filter)]
if client_filter:
    df = df[df["Client"].isin(client_filter)]
if cat_filter:
    df = df[df["Category"].isin(cat_filter)]

# Enforce display order for severity
df["_sev_rank"] = df["Severity"].map({s: i for i, s in enumerate(SEV_ORDER)})
df = df.sort_values("_sev_rank").drop(columns="_sev_rank")

# ── Header ────────────────────────────────────────────────────────────────────

st.markdown("## 🛡️ Connect Care — NDIS Compliance Audit")
st.caption(f"Data last fetched: {fetched_at}  ·  Showing {len(df)} of {len(df_all)} issues")

# ── Metric row ────────────────────────────────────────────────────────────────

counts_all = df_all["Severity"].value_counts()
counts_flt = df["Severity"].value_counts()

cols = st.columns(5)
for col, sev in zip(cols, SEV_ORDER):
    total = counts_all.get(sev, 0)
    shown = counts_flt.get(sev, 0)
    delta = f"{shown} shown" if shown != total else None
    col.metric(
        label=f"{SEV_EMOJI[sev]} {sev}",
        value=total,
        delta=delta,
        delta_color="off",
    )

st.divider()

# ── Charts ────────────────────────────────────────────────────────────────────

col1, col2 = st.columns(2)

with col1:
    st.markdown("#### Issues by Worker")
    worker_counts = (
        df.groupby(["Worker", "Severity"])
        .size()
        .reset_index(name="Count")
    )
    worker_order = (
        worker_counts.groupby("Worker")["Count"].sum()
        .sort_values(ascending=True).index.tolist()
    )
    fig = px.bar(
        worker_counts,
        x="Count", y="Worker",
        color="Severity",
        color_discrete_map=SEV_COLOUR,
        category_orders={"Worker": worker_order, "Severity": SEV_ORDER},
        orientation="h",
        height=max(300, len(worker_order) * 30),
    )
    fig.update_layout(margin=dict(l=0, r=0, t=0, b=0), legend_title_text="")
    st.plotly_chart(fig, use_container_width=True)

with col2:
    st.markdown("#### Issues by Category")
    cat_counts = (
        df.groupby(["Category", "Severity"])
        .size()
        .reset_index(name="Count")
    )
    cat_order = (
        cat_counts.groupby("Category")["Count"].sum()
        .sort_values(ascending=True).index.tolist()
    )
    fig2 = px.bar(
        cat_counts,
        x="Count", y="Category",
        color="Severity",
        color_discrete_map=SEV_COLOUR,
        category_orders={"Category": cat_order, "Severity": SEV_ORDER},
        orientation="h",
        height=max(300, len(cat_order) * 30),
    )
    fig2.update_layout(margin=dict(l=0, r=0, t=0, b=0), legend_title_text="")
    st.plotly_chart(fig2, use_container_width=True)

col3, col4 = st.columns(2)

with col3:
    st.markdown("#### Issues by Client")
    client_counts = (
        df.groupby(["Client", "Severity"])
        .size()
        .reset_index(name="Count")
    )
    client_order = (
        client_counts.groupby("Client")["Count"].sum()
        .sort_values(ascending=True).index.tolist()
    )
    fig3 = px.bar(
        client_counts,
        x="Count", y="Client",
        color="Severity",
        color_discrete_map=SEV_COLOUR,
        category_orders={"Client": client_order, "Severity": SEV_ORDER},
        orientation="h",
        height=max(300, len(client_order) * 30),
    )
    fig3.update_layout(margin=dict(l=0, r=0, t=0, b=0), legend_title_text="")
    st.plotly_chart(fig3, use_container_width=True)

with col4:
    st.markdown("#### Severity Breakdown")
    sev_totals = df["Severity"].value_counts().reset_index()
    sev_totals.columns = ["Severity", "Count"]
    fig4 = px.pie(
        sev_totals,
        names="Severity",
        values="Count",
        color="Severity",
        color_discrete_map=SEV_COLOUR,
        hole=0.45,
    )
    fig4.update_layout(margin=dict(l=0, r=0, t=0, b=0), legend_title_text="")
    st.plotly_chart(fig4, use_container_width=True)

st.divider()

# ── Full issues table ─────────────────────────────────────────────────────────

st.markdown("#### All Issues")

def colour_row(row):
    colour = SEV_COLOUR.get(row["Severity"], "#ffffff")
    # Light background tint
    tint = {
        "CRITICAL": "#ffd7d7",
        "HIGH":     "#ffe8cc",
        "MEDIUM":   "#fffacc",
        "LOW":      "#d4f4d4",
        "INFO":     "#eaf3ff",
    }.get(row["Severity"], "#ffffff")
    return [f"background-color: {tint}"] * len(row)

display_df = df[["Severity", "Category", "Worker", "Client", "Date", "Detail"]].copy()
styled = display_df.style.apply(colour_row, axis=1)

st.dataframe(
    styled,
    use_container_width=True,
    height=600,
    column_config={
        "Severity": st.column_config.TextColumn(width="small"),
        "Category": st.column_config.TextColumn(width="medium"),
        "Worker":   st.column_config.TextColumn(width="medium"),
        "Client":   st.column_config.TextColumn(width="medium"),
        "Date":     st.column_config.TextColumn(width="small"),
        "Detail":   st.column_config.TextColumn(width="large"),
    },
    hide_index=True,
)
