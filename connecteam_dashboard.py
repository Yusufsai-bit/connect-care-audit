"""
Connect Care Services — NDIS Compliance Dashboard
Run: streamlit run connecteam_dashboard.py
"""

import os, io, calendar, datetime, json, uuid
import pandas as pd
import streamlit as st

# ── Secrets → env vars ────────────────────────────────────────────────────────
for _k, _e in [
    ("CONNECTEAM_API_KEY",    "CONNECTEAM_API_KEY"),
    ("ANTHROPIC_API_KEY",     "ANTHROPIC_API_KEY"),
    ("CONNECTEAM_SENDER_ID",  "CONNECTEAM_SENDER_ID"),
]:
    _v = st.secrets.get(_k, "")
    if _v: os.environ[_e] = _v

CT_CHAT_READY = bool(st.secrets.get("CONNECTEAM_SENDER_ID", ""))

from connecteam_audit import (
    run_audit, fetch_all_users,
    send_worker_message, add_worker_profile_note,
)

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(page_title="Connect Care", page_icon="🛡️", layout="wide")

st.markdown("""
<style>
    .block-container { padding-top: 1.2rem; padding-bottom: 1rem; }
    .kpi { border-radius:10px; padding:1.1rem 1rem; text-align:center; }
    .kpi-num  { font-size:2.2rem; font-weight:700; line-height:1; }
    .kpi-lbl  { font-size:0.8rem; color:#666; margin-top:0.25rem; }
    .chip { display:inline-block; border-radius:20px; padding:2px 10px;
            font-size:0.78rem; font-weight:600; color:#fff; }
    .row-card { border-left:4px solid #eee; border-radius:6px;
                padding:0.5rem 0.8rem; margin-bottom:0.4rem; background:#fafafa; }
</style>
""", unsafe_allow_html=True)

# ── Constants ─────────────────────────────────────────────────────────────────

SEV_ORDER  = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
SEV_COLOUR = {"CRITICAL":"#d62728","HIGH":"#ff7f0e","MEDIUM":"#e6c200","LOW":"#2ca02c","INFO":"#aec7e8"}
SEV_TINT   = {"CRITICAL":"#fff5f5","HIGH":"#fff8f0","MEDIUM":"#fffdf0","LOW":"#f0fff0","INFO":"#f0f6ff"}
SEV_EMOJI  = {"CRITICAL":"🔴","HIGH":"🟠","MEDIUM":"🟡","LOW":"🟢","INFO":"⚪"}
EXCLUDED   = {"(team)", "(unassigned)"}

PLAIN = {
    "NO CLOCK-IN":                              "Didn't clock in",
    "LATE CLOCK-IN":                            "Late to start shift",
    "EARLY CLOCK-OUT":                          "Left shift early",
    "MISSING CLOCK-OUT":                        "Forgot to clock out",
    "AUTO CLOCK-OUT":                           "System closed shift",
    "OPEN SHIFT":                               "No worker assigned",
    "UNSCHEDULED SHIFT":                        "Unrostered shift",
    "SUSPICIOUSLY SHORT SHIFT":                 "Very short shift",
    "MULTIPLE CLOCK-INS SAME CLIENT/DAY":       "Multiple clock-ins same client",
    "UNDERSTAFFED -- RATIO BREACH":             "Not enough staff",
    "OVERSTAFFED -- POSSIBLE OVERBILLING":      "Too many staff",
    "GPS MISMATCH":                             "Wrong location at clock-in",
    "NO SHIFT NOTES":                           "No notes written",
    "EMPTY NOTES":                              "Notes left blank",
    "INSUFFICIENT NOTES":                       "Notes too short",
    "LATE NOTE SUBMISSION":                     "Notes written late",
    "MISSING SIGNATURE":                        "No participant signature",
    "DUPLICATE/COPY-PASTE NOTES":               "Copy-pasted notes",
    "POSSIBLE AI-GENERATED NOTE":               "Possible AI-written notes",
    "FAILS NDIS STANDARD":                      "Doesn't meet NDIS standard",
    "NOT PLAIN ENGLISH":                        "Unclear notes",
    "NOTE DOESN'T MAKE SENSE":                  "Incoherent notes",
    "SUBJECTIVE LANGUAGE":                      "Opinions instead of observations",
    "NOT PERSON-CENTRED":                       "Not person-centred",
    "INCIDENT KEYWORD -- VERIFY REPORT FILED":  "Incident — check report filed",
    "RESTRICTIVE PRACTICE MENTIONED":           "Restrictive practice — needs authorisation",
    "MEDICATION MENTIONED -- VERIFY FORM FILED":"Medication — check form filed",
    "INCOMPLETE INCIDENT REPORT":               "Incomplete incident report",
    "LATE INCIDENT REPORTING":                  "Late incident report",
    "MISSING FORM -- KALLAN":                   "Kallan form not submitted",
    "MISSING FORM -- EVAN":                     "Evan form not submitted",
    "MISSING FORM -- MICHAEL":                  "Michael form not submitted",
    "FORM FREQUENCY -- JOSHUA":                 "Joshua weekly form missing",
    "FORM FREQUENCY -- NADA":                   "Nada weekly form missing",
    "FORM FREQUENCY -- JOHN":                   "John weekly form missing",
    "FORM FREQUENCY -- NICOLE":                 "Nicole weekly form missing",
    "APPROVED LEAVE":                           "Approved leave",
    "BREAK COMPLIANCE":                         "No break on long shift",
    "CROSS-WORKER COPY-PASTE NOTES":            "Identical notes across workers",
    "ONBOARDING INCOMPLETE":                    "Onboarding not complete",
    "UNAUTHORISED CLIENT ACCESS":               "Unauthorised client access",
    "EXPIRED CREDENTIAL":                       "Credential expired",
    "CREDENTIAL EXPIRING SOON":                 "Credential expiring soon",
}

REQUIRED_DOCS = [
    "NDIS Worker Screening",
    "Working With Children Check",
    "Police Check",
    "First Aid Certificate",
    "CPR Certificate",
    "Manual Handling Training",
]

def plain(cat): return PLAIN.get(cat, cat.title())

# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_date(s):
    today = datetime.date.today()
    for fmt in ["%Y-%m-%d", "%a %d-%b"]:
        try:
            d = datetime.datetime.strptime(s.strip(), fmt).date()
            if fmt == "%a %d-%b":
                d = d.replace(year=today.year)
                if d > today + datetime.timedelta(days=30):
                    d = d.replace(year=today.year - 1)
            return d
        except Exception:
            pass
    return None

def compliance_score(wdf):
    deduct = {"CRITICAL":15,"HIGH":8,"MEDIUM":3,"LOW":1}
    return max(0, 100 - int(wdf["Severity"].map(deduct).fillna(0).sum()))

def score_colour(s): return "#2ca02c" if s>=80 else "#ff7f0e" if s>=60 else "#d62728"
def score_label(s):  return "Good" if s>=80 else "Needs Attention" if s>=60 else "At Risk"

def colour_row(row):
    t = SEV_TINT.get(row["Severity"], "#fff")
    return [f"background-color:{t}"] * len(row)

def to_excel(dfs):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for sheet, df in dfs.items():
            df.to_excel(w, sheet_name=sheet[:31], index=False)
    return buf.getvalue()

def pay_cycles(today):
    y, m = today.year, today.month
    last = calendar.monthrange(y, m)[1]
    mn   = today.strftime("%b %Y")
    pm = m-1 if m>1 else 12; py = y if m>1 else y-1
    plast = calendar.monthrange(py, pm)[1]
    pmn = datetime.date(py, pm, 1).strftime("%b %Y")
    return [
        (f"Pay Cycle 1 — 1–15 {mn}",     datetime.date(y,m,1),    datetime.date(y,m,15)),
        (f"Pay Cycle 2 — 16–{last} {mn}", datetime.date(y,m,16),   datetime.date(y,m,last)),
        (f"Pay Cycle 1 — 1–15 {pmn}",     datetime.date(py,pm,1),  datetime.date(py,pm,15)),
        (f"Pay Cycle 2 — 16–{plast} {pmn}",datetime.date(py,pm,16),datetime.date(py,pm,plast)),
    ]

def doc_status(v):
    if not v or str(v).strip() in ("","nan","None"):
        return "❌","Missing","#d62728"
    try:
        d = datetime.date.fromisoformat(str(v).strip())
        n = (d - datetime.date.today()).days
        if n < 0:   return "❌", f"Expired {abs(n)}d ago","#d62728"
        if n <= 60: return "⚠️", f"Expires in {n}d","#ff7f0e"
        return "✅", f"Valid · {d.strftime('%d %b %Y')}","#2ca02c"
    except Exception:
        return "❓","Invalid date","#888"

# ── Notification store ────────────────────────────────────────────────────────

_NOTIF_FILE = os.path.join(os.path.dirname(__file__), "notifications_log.json")

def _load_notifs():
    try:
        with open(_NOTIF_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return []

def _save_notifs(ns):
    try:
        with open(_NOTIF_FILE, "w", encoding="utf-8") as f: json.dump(ns, f, default=str, indent=2)
    except Exception: pass

def _init_notifs():
    if "notifications" not in st.session_state:
        st.session_state.notifications = _load_notifs()

# ── Password gate ─────────────────────────────────────────────────────────────

if not st.session_state.get("authenticated"):
    st.markdown("<br><br>", unsafe_allow_html=True)
    _, col, _ = st.columns([1,1,1])
    with col:
        st.markdown("## 🛡️ Connect Care")
        st.markdown("##### Compliance Dashboard")
        st.markdown("<br>", unsafe_allow_html=True)
        pw = st.text_input("Password", type="password", placeholder="Enter password…")
        if st.button("Sign in", use_container_width=True, type="primary"):
            if pw == st.secrets.get("DASHBOARD_PASSWORD",""):
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Incorrect password.")
    st.stop()

# ── Data loaders ──────────────────────────────────────────────────────────────

_AUDIT_DAYS = 45

@st.cache_data(ttl=1800, show_spinner=False)
def load_audit():
    issues = run_audit(_AUDIT_DAYS)
    return issues, datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")

@st.cache_data(ttl=3600, show_spinner=False)
def _load_contacts_raw():
    try:
        users = fetch_all_users()
        return {
            f"{u.get('firstName','')} {u.get('lastName','')}".strip(): {
                "phone":  u.get("phoneNumber") or u.get("phone") or "",
                "userId": u.get("userId"),
            }
            for u in users.values()
        }
    except Exception: return {}

def get_contacts():
    if "contacts" not in st.session_state:
        st.session_state.contacts = _load_contacts_raw()
    return st.session_state.contacts

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("### 🛡️ Connect Care")
    st.caption("Compliance Dashboard")
    st.divider()

    today     = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    cycles    = pay_cycles(today)

    period_opts = (
        [f"Yesterday — {yesterday.strftime('%a %d %b')}"]
        + [c[0] for c in cycles]
        + ["Custom range"]
    )
    period_choice = st.selectbox("Period", period_opts, label_visibility="collapsed")

    if period_choice == "Custom range":
        dr = st.date_input("Dates", value=(today - datetime.timedelta(days=7), today),
                           max_value=today, label_visibility="collapsed")
        start_date = dr[0] if isinstance(dr,(list,tuple)) and len(dr)==2 else today-datetime.timedelta(days=7)
        end_date   = dr[1] if isinstance(dr,(list,tuple)) and len(dr)==2 else today
    elif period_choice.startswith("Yesterday"):
        start_date = end_date = yesterday
    else:
        _, start_date, end_date = next(c for c in cycles if c[0]==period_choice)

    st.markdown("<br>", unsafe_allow_html=True)

    if st.button("🔄 Refresh", use_container_width=True, type="primary"):
        st.cache_data.clear()
        if "contacts" in st.session_state: del st.session_state["contacts"]
        st.rerun()

    with st.spinner("Loading data…"):
        all_issues, fetched_at = load_audit()

    st.markdown(f"<small style='color:#aaa'>Updated {fetched_at}</small>", unsafe_allow_html=True)
    st.divider()
    if st.button("Sign out", use_container_width=True):
        st.session_state.authenticated = False
        st.rerun()

# ── Build dataframe ───────────────────────────────────────────────────────────

_init_notifs()

if not all_issues:
    st.success("✅ No compliance issues found.")
    st.stop()

df_raw = pd.DataFrame([
    {"Severity": i.severity, "Category": i.category, "Issue": plain(i.category),
     "Worker": i.worker, "Client": i.client or "", "Date": i.date or "", "Detail": i.detail or ""}
    for i in all_issues
])
df_raw["_d"] = df_raw["Date"].apply(parse_date)
df = df_raw[(df_raw["_d"] >= start_date) & (df_raw["_d"] <= end_date)].drop(columns="_d").copy()
df["_r"] = df["Severity"].map({s:i for i,s in enumerate(SEV_ORDER)})
df = df.sort_values("_r").drop(columns="_r")

df_staff = df[~df["Worker"].isin(EXCLUDED)].copy()

n_crit  = int((df["Severity"]=="CRITICAL").sum())
n_high  = int((df["Severity"]=="HIGH").sum())
n_med   = int((df["Severity"]=="MEDIUM").sum())
n_total = len(df)

period_str = (yesterday.strftime("%a %d %b") if start_date==end_date==yesterday
              else f"{start_date.strftime('%d %b')} – {end_date.strftime('%d %b %Y')}")

# ── Page header ───────────────────────────────────────────────────────────────

st.markdown(f"## Connect Care &nbsp; <small style='font-size:1rem;color:#888;font-weight:400'>{period_str}</small>", unsafe_allow_html=True)

if n_crit:
    st.error(f"⚠️ {n_crit} critical issue{'s' if n_crit>1 else ''} need immediate attention.")

c1,c2,c3,c4 = st.columns(4)
for col, bg, fg, lbl, num in [
    (c1,"#fff5f5","#d62728","🔴 Critical",    n_crit),
    (c2,"#fff8f0","#ff7f0e","🟠 High",        n_high),
    (c3,"#fffdf0","#b8980a","🟡 Medium",      n_med),
    (c4,"#f0f4ff","#4c78a8","📋 Total Issues",n_total),
]:
    col.markdown(
        f'<div class="kpi" style="background:{bg}">'
        f'<div class="kpi-num" style="color:{fg}">{num}</div>'
        f'<div class="kpi-lbl">{lbl}</div></div>',
        unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# ── Tabs ──────────────────────────────────────────────────────────────────────

tab_overview, tab_workers, tab_clients, tab_docs, tab_messages = st.tabs([
    "🚨 Action Required",
    "👤 Workers",
    "👥 Clients",
    "📄 Documents",
    "📬 Messages",
])

# ═══════════════════════════════════════════════════════
# TAB 1 — Action Required
# ═══════════════════════════════════════════════════════
with tab_overview:
    urgent = df[df["Severity"].isin(["CRITICAL","HIGH"])]

    if urgent.empty:
        st.success("✅ No critical or high priority issues this period.")
    else:
        # Group by worker
        for worker in sorted(urgent["Worker"].unique()):
            if worker in EXCLUDED: continue
            wissues = urgent[urgent["Worker"]==worker]
            crit_n = int((wissues["Severity"]=="CRITICAL").sum())
            high_n = int((wissues["Severity"]=="HIGH").sum())
            badge = ""
            if crit_n: badge += f'<span class="chip" style="background:#d62728">{crit_n} Critical</span> '
            if high_n: badge += f'<span class="chip" style="background:#ff7f0e">{high_n} High</span>'
            with st.expander(f"**{worker}** &nbsp; {badge}", expanded=crit_n>0):
                for _, row in wissues.iterrows():
                    c = SEV_COLOUR[row["Severity"]]
                    st.markdown(
                        f'<div class="row-card" style="border-left-color:{c}">'
                        f'{SEV_EMOJI[row["Severity"]]} <strong>{row["Issue"]}</strong>'
                        f' &nbsp;·&nbsp; {row["Client"]} &nbsp;·&nbsp; '
                        f'<span style="color:#888;font-size:0.85rem">{row["Date"]}</span><br>'
                        f'<span style="color:#444;font-size:0.88rem">{row["Detail"]}</span></div>',
                        unsafe_allow_html=True)

    # Special flags
    for cat, title, colour in [
        ("RESTRICTIVE PRACTICE MENTIONED",           "🚫 Restrictive Practice — authorisation required", "#d62728"),
        ("LATE INCIDENT REPORTING",                  "⏰ Late Incident Reports — NDIS risk", "#ff7f0e"),
        ("INCIDENT KEYWORD -- VERIFY REPORT FILED",  "📋 Incident Mentioned — confirm report filed", "#ff7f0e"),
        ("ONBOARDING INCOMPLETE",                    "🎓 Onboarding Incomplete", "#d62728"),
        ("UNAUTHORISED CLIENT ACCESS",               "🔑 Unauthorised Client Access", "#d62728"),
    ]:
        rows = df[df["Category"]==cat]
        if not rows.empty:
            st.markdown(f"<hr style='margin:1rem 0'>", unsafe_allow_html=True)
            st.markdown(f"**{title}** ({len(rows)})")
            for _, r in rows.iterrows():
                st.markdown(f"- **{r['Worker']}** · {r['Client']} · {r['Detail']}")

# ═══════════════════════════════════════════════════════
# TAB 2 — Workers
# ═══════════════════════════════════════════════════════
with tab_workers:
    if df_staff.empty:
        st.info("No staff issues this period.")
    else:
        # Scores table
        ws = df_staff.groupby(["Worker","Severity"]).size().unstack(fill_value=0)
        for col in SEV_ORDER:
            if col not in ws.columns: ws[col] = 0
        ws = ws[[c for c in SEV_ORDER if c in ws.columns]]
        ws["Total"]  = ws.sum(axis=1)
        ws["Score"]  = [compliance_score(df_staff[df_staff["Worker"]==w]) for w in ws.index]
        ws["Status"] = ws["Score"].apply(score_label)
        ws = ws.sort_values("Score")

        st.dataframe(ws, use_container_width=True, column_config={
            "CRITICAL": st.column_config.NumberColumn("🔴"),
            "HIGH":     st.column_config.NumberColumn("🟠"),
            "MEDIUM":   st.column_config.NumberColumn("🟡"),
            "LOW":      st.column_config.NumberColumn("🟢"),
            "Score":    st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%d%%"),
        })

        st.divider()

        # Worker drill-down
        selected = st.selectbox("Select a worker to review", ["—"] + sorted(ws.index.tolist()))
        if selected != "—":
            score = compliance_score(df_staff[df_staff["Worker"]==selected])
            sc = score_colour(score)

            col_s, col_info = st.columns([1,3])
            with col_s:
                st.markdown(
                    f'<div style="text-align:center;padding:1.5rem 0">'
                    f'<div style="font-size:2.8rem;font-weight:700;color:{sc}">{score}</div>'
                    f'<div style="color:{sc};font-weight:600">{score_label(score)}</div></div>',
                    unsafe_allow_html=True)
            with col_info:
                contacts = get_contacts()
                info = contacts.get(selected, {})
                phone = info.get("phone","")
                st.markdown("<br>", unsafe_allow_html=True)
                st.markdown(f"📞 {'['+phone+'](tel:'+phone+')' if phone else '_no phone_'}")

            wdf = df_staff[df_staff["Worker"]==selected][["Severity","Issue","Client","Date","Detail"]]
            st.dataframe(wdf.style.apply(colour_row, axis=1),
                         use_container_width=True, hide_index=True)

            # Download
            st.download_button(
                "⬇️ Download issues (CSV)",
                data=wdf.to_csv(index=False),
                file_name=f"{selected.replace(' ','_')}_{period_str.replace(' ','_')}.csv",
                mime="text/csv",
            )

            # Notify
            st.markdown("#### Send message")
            worker_id = info.get("userId")
            if not worker_id:
                st.warning("No Connecteam user ID found for this worker.")
            else:
                default_msg = (
                    f"Hi {selected.split()[0]},\n\n"
                    f"Following up on your shift {period_str.lower()} — there are a few things that need sorting:\n\n"
                    + "\n".join(
                        f"{'🔴' if r['Severity']=='CRITICAL' else '🟠' if r['Severity']=='HIGH' else '🟡'} "
                        f"{r['Client']} — {r['Detail']}"
                        for _,r in wdf.iterrows()
                        if r["Severity"] in ("CRITICAL","HIGH","MEDIUM")
                    )[:6*120]  # cap length
                    + "\n\nCan you let me know what happened?\n\nCheers"
                )
                msg = st.text_area("Message", value=default_msg, height=220, key=f"msg_{selected}")
                if st.button("📤 Send via Connecteam", type="primary", key=f"send_{selected}"):
                    if not CT_CHAT_READY:
                        st.error("CONNECTEAM_SENDER_ID not set in secrets — can't send.")
                    else:
                        ok, err = send_worker_message(worker_id, msg)
                        if ok:
                            sev_counts = wdf["Severity"].value_counts().to_dict()
                            st.session_state.notifications.insert(0, {
                                "id": str(uuid.uuid4())[:8],
                                "worker": selected, "worker_id": worker_id,
                                "sent_at": datetime.datetime.now().strftime("%d %b %Y, %I:%M %p"),
                                "sent_at_iso": datetime.datetime.now().isoformat(),
                                "period": period_str, "severity_counts": sev_counts,
                                "issue_count": len(wdf),
                                "issues": wdf[["Severity","Issue","Client","Date","Detail"]].to_dict("records"),
                                "message_sent": msg, "status": "Sent",
                                "acknowledged_at": None, "resolved_at": None, "manager_notes": "",
                            })
                            _save_notifs(st.session_state.notifications)
                            st.success(f"✅ Message sent to {selected}. Logged in Messages tab.")
                        else:
                            st.error(f"Failed: {err}")

# ═══════════════════════════════════════════════════════
# TAB 3 — Clients
# ═══════════════════════════════════════════════════════
with tab_clients:
    cs = df.groupby(["Client","Severity"]).size().unstack(fill_value=0)
    for col in SEV_ORDER:
        if col not in cs.columns: cs[col] = 0
    cs = cs[[c for c in SEV_ORDER if c in cs.columns]]
    cs["Total"] = cs.sum(axis=1)
    cs = cs.sort_values("CRITICAL" if "CRITICAL" in cs.columns else "Total", ascending=False)

    st.dataframe(cs, use_container_width=True, column_config={
        "CRITICAL": st.column_config.NumberColumn("🔴 Critical"),
        "HIGH":     st.column_config.NumberColumn("🟠 High"),
        "MEDIUM":   st.column_config.NumberColumn("🟡 Medium"),
        "LOW":      st.column_config.NumberColumn("🟢 Low"),
        "Total":    st.column_config.NumberColumn("Total"),
    })

    st.divider()
    selected_client = st.selectbox("Select a client to review", ["—"] + sorted(df["Client"].unique()))
    if selected_client != "—":
        cdf = df[df["Client"]==selected_client][["Severity","Issue","Worker","Date","Detail"]]
        st.dataframe(cdf.style.apply(colour_row, axis=1), use_container_width=True, hide_index=True)
        st.download_button(
            "⬇️ Download (CSV)",
            data=cdf.to_csv(index=False),
            file_name=f"{selected_client.replace(' ','_')}_{period_str.replace(' ','_')}.csv",
            mime="text/csv",
        )

# ═══════════════════════════════════════════════════════
# TAB 4 — Documents
# ═══════════════════════════════════════════════════════
with tab_docs:
    st.caption("Track NDIS-required document expiry dates for each worker. ✅ Valid · ⚠️ Expiring within 60 days · ❌ Expired / missing")

    # Load saved data from secrets (persistent) or session state (in-session edits)
    if "doc_data" not in st.session_state:
        saved = st.secrets.get("DOCUMENTS_JSON","")
        try:    st.session_state.doc_data = json.loads(saved) if saved else {}
        except: st.session_state.doc_data = {}

    # Load staff names from contacts (already cached)
    contacts_for_docs = get_contacts()
    staff_names = sorted(k for k in contacts_for_docs if k and k not in EXCLUDED)

    if not staff_names:
        st.warning("Could not load staff list. Check API key.")
    else:
        rows = [{"Worker": w, **{d: st.session_state.doc_data.get(w,{}).get(d,"") for d in REQUIRED_DOCS}}
                for w in staff_names]
        doc_df = pd.DataFrame(rows)

        edited = st.data_editor(
            doc_df, use_container_width=True, hide_index=True,
            column_config={
                "Worker": st.column_config.TextColumn("Worker", disabled=True, width="medium"),
                **{d: st.column_config.TextColumn(d, width="small") for d in REQUIRED_DOCS}
            },
            key="doc_editor",
        )

        col_save, col_dl = st.columns([1,1])
        with col_save:
            if st.button("💾 Save", type="primary", use_container_width=True):
                st.session_state.doc_data = {
                    row["Worker"]: {d: str(row[d]) if row[d] else "" for d in REQUIRED_DOCS}
                    for _, row in edited.iterrows()
                }
                st.success("Saved for this session.")
        with col_dl:
            export_rows = [
                {"Worker": row["Worker"], "Document": d,
                 "Expiry": row[d], "Status": f"{doc_status(row[d])[0]} {doc_status(row[d])[1]}"}
                for _, row in edited.iterrows() for d in REQUIRED_DOCS
            ]
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as w:
                pd.DataFrame(export_rows).to_excel(w, index=False)
            st.download_button(
                "⬇️ Download Excel", data=buf.getvalue(),
                file_name=f"ConnectCare_Documents_{today.strftime('%d-%b-%Y')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

        # Alerts only
        alerts = [
            {"Worker": row["Worker"], "Document": d, "Status": f"{doc_status(row[d])[0]} {doc_status(row[d])[1]}"}
            for _, row in edited.iterrows() for d in REQUIRED_DOCS
            if doc_status(row[d])[0] in ("❌","⚠️")
        ]
        if alerts:
            st.markdown("---")
            st.markdown(f"**{len(alerts)} document(s) need attention**")
            st.dataframe(pd.DataFrame(alerts), use_container_width=True, hide_index=True)
        else:
            st.success("✅ All documents on file and valid.")

        with st.expander("How to save document data permanently"):
            st.markdown("""
1. Click **Download Excel** and keep a local copy
2. To persist across sessions: go to [share.streamlit.io](https://share.streamlit.io) → your app → ⚙️ Settings → Secrets → add:
```
DOCUMENTS_JSON = '<paste the JSON from "Download JSON" below>'
```
""")
            st.download_button(
                "⬇️ Download JSON backup",
                data=json.dumps(st.session_state.doc_data, indent=2),
                file_name="documents_backup.json",
                mime="application/json",
            )

# ═══════════════════════════════════════════════════════
# TAB 5 — Messages
# ═══════════════════════════════════════════════════════
with tab_messages:
    notifs = st.session_state.get("notifications", [])

    # Summary row
    n_sent = sum(1 for n in notifs if n["status"]=="Sent")
    n_ack  = sum(1 for n in notifs if n["status"]=="Acknowledged")
    n_res  = sum(1 for n in notifs if n["status"]=="Resolved")
    m1,m2,m3,m4 = st.columns(4)
    m1.metric("Total Sent",    len(notifs))
    m2.metric("Awaiting Reply", n_sent)
    m3.metric("Acknowledged",  n_ack)
    m4.metric("Resolved",      n_res)

    st.divider()

    if not notifs:
        st.info("No messages sent yet. Use the Workers tab to notify a worker.")
    else:
        STATUS_COLOUR = {"Sent":"#ff7f0e","Acknowledged":"#4c78a8","Resolved":"#2ca02c"}

        f_status = st.radio("Show", ["All","Awaiting Reply","Acknowledged","Resolved"], horizontal=True)
        fmap = {"All": None, "Awaiting Reply": "Sent", "Acknowledged": "Acknowledged", "Resolved": "Resolved"}
        filtered = notifs if not fmap[f_status] else [n for n in notifs if n["status"]==fmap[f_status]]

        for n in filtered:
            sc = STATUS_COLOUR.get(n["status"],"#888")
            crit = n["severity_counts"].get("CRITICAL",0)
            high = n["severity_counts"].get("HIGH",0)
            with st.expander(
                f"**{n['worker']}** · {n['sent_at']} · "
                f"{'🔴'+str(crit)+' · ' if crit else ''}"
                f"{'🟠'+str(high)+' · ' if high else ''}"
                f"{n['status']}",
                expanded=False
            ):
                col_l, col_r = st.columns([1,1])
                with col_l:
                    st.markdown(f"**Period:** {n['period']}  ·  **Issues:** {n['issue_count']}")
                    for iss in n["issues"][:6]:
                        st.markdown(f"- {SEV_EMOJI.get(iss['Severity'],'•')} {iss['Issue']} · {iss['Client']}")
                    if len(n["issues"])>6: st.caption(f"…+{len(n['issues'])-6} more")
                with col_r:
                    st.code(n["message_sent"], language=None)

                real_idx = notifs.index(n)
                a1,a2 = st.columns(2)
                with a1:
                    if n["status"]=="Sent" and st.button("✅ Mark Acknowledged", key=f"ack_{n['id']}"):
                        notifs[real_idx]["status"] = "Acknowledged"
                        notifs[real_idx]["acknowledged_at"] = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")
                        _save_notifs(notifs); st.rerun()
                with a2:
                    if n["status"]!="Resolved" and st.button("🏁 Mark Resolved", key=f"res_{n['id']}"):
                        notifs[real_idx]["status"] = "Resolved"
                        notifs[real_idx]["resolved_at"] = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")
                        if not notifs[real_idx]["acknowledged_at"]:
                            notifs[real_idx]["acknowledged_at"] = notifs[real_idx]["resolved_at"]
                        _save_notifs(notifs); st.rerun()

                note = st.text_input("Notes", value=n.get("manager_notes",""),
                                     placeholder="e.g. Worker called and confirmed…",
                                     key=f"note_{n['id']}")
                if note != n.get("manager_notes",""):
                    notifs[real_idx]["manager_notes"] = note

        st.divider()
        if notifs:
            notif_df = pd.DataFrame([{
                "Worker":       n["worker"],    "Sent":    n["sent_at"],
                "Period":       n["period"],    "Issues":  n["issue_count"],
                "Status":       n["status"],    "Notes":   n.get("manager_notes",""),
            } for n in notifs])
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as w:
                notif_df.to_excel(w, index=False)
            st.download_button(
                "⬇️ Download message log",
                data=buf.getvalue(),
                file_name=f"ConnectCare_Messages_{today.strftime('%d-%b-%Y')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
