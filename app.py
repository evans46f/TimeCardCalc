import re
from typing import List, Tuple, Dict, Any

import streamlit as st
import pandas as pd

# ==============================
# Helpers
# ==============================

def to_minutes(s: str) -> int:
    if not isinstance(s, str):
        return 0
    m = re.match(r"^(\d{1,3}):([0-5]\d)$", s.strip())
    if not m:
        return 0
    return int(m.group(1)) * 60 + int(m.group(2))

def from_minutes(mins: int) -> str:
    mins = max(0, int(mins))
    h, m = divmod(mins, 60)
    return f"{h}:{m:02d}"

def nbps(text: str) -> str:
    return (text or "").replace("\u00A0", " ")

def grab_labeled_time_flex(text: str, variants: List[str]) -> int:
    t = nbps(text)
    for v in variants:
        pattern = re.sub(r"\s+", r"\\s+", v)
        pattern = re.sub(r"([\-/])", r"\\\1", pattern)
        m = re.search(pattern + r"\s*:\s*(\d{1,3}:[0-5]\d)", t, flags=re.I)
        if m:
            return to_minutes(m.group(1))
    return 0

def grab_ttl_credit(raw: str) -> Tuple[int, str]:
    t = nbps(raw)
    m = re.search(r"CREDIT\s+APPLICABLE\s+TO\s+REG\s+G/\s*SLIP\s+PAY\s*:\s*(\d{1,3}:[0-5]\d)", t, flags=re.I)
    if m:
        return to_minutes(m.group(1)), "CREDIT APPLICABLE line"
    m = re.search(r"TTL\s+.*CREDIT\s*:\s*(\d{1,3}:[0-5]\d)", t, flags=re.I)
    if m:
        return to_minutes(m.group(1)), "TTL CREDIT label"
    for line in t.splitlines():
        if "=" in line and re.search(r"\d{1,3}:[0-5]\d", line):
            all_eq = list(re.finditer(r"=\s*(\d{1,3}:[0-5]\d)", line))
            if all_eq:
                return to_minutes(all_eq[-1].group(1)), "Equation fallback"
    return 0, "Not found"

EXTRA_NBR_CODES = {"SCC","PVEL","LOSA","ADJ-RRPY","ADJ-RR","ADJ","RRPY"}

ROW_SEGMENT_RE = re.compile(
    r"(?P<date>\d{2}[A-Z]{3})\s+RES\s+(?P<nbr>[A-Z0-9-]+)(?P<tail>.*?)(?=\d{2}[A-Z]{3}\s+RES\b|RES\s+OTHER\s+SUB\s+TTL|CREDIT\s+APPLICABLE|END OF DISPLAY|$)",
    re.I | re.S
)

def segment_rows(raw: str) -> List[Dict[str, Any]]:
    t = nbps(raw)
    rows = []
    for m in ROW_SEGMENT_RE.finditer(t):
        date = (m.group("date") or "").upper()
        nbr  = (m.group("nbr") or "").upper()
        seg  = (m.group(0) or "")
        times = re.findall(r"\b\d{1,3}:[0-5]\d\b", seg)
        rows.append({"date": date, "nbr": nbr, "times": times, "segment": seg})
    return rows

def sum_daily_extras_with_debug(raw: str) -> Tuple[int, int, List[Dict[str, Any]]]:
    pay_extras = 0
    pay_only   = 0
    debug_rows = []

    for row in segment_rows(raw):
        nbr = row["nbr"]
        times = row["times"]
        counted_extra = 0
        counted_po    = 0
        reasons = []

        if len(times) >= 5:
            counted_po = to_minutes(times[-1])
            pay_only += counted_po
            reasons.append(f"PAY ONLY added {times[-1]} (>=5 time tokens)")

        if nbr in EXTRA_NBR_CODES and times:
            counted_extra = to_minutes(times[-1])
            pay_extras += counted_extra
            reasons.append(f"EXTRA added {times[-1]} (NBR={nbr})")

        debug_rows.append({
            "Date": row["date"],
            "NBR": nbr,
            "All times on row": ", ".join(times) if times else "",
            "Extra added (H:MM)": from_minutes(counted_extra) if counted_extra else "",
            "Pay Only added (H:MM)": from_minutes(counted_po) if counted_po else "",
            "Notes": " | ".join(reasons) if reasons else ""
        })

    return pay_extras, pay_only, debug_rows

def compute_totals(text: str) -> Tuple[str, float, List[Tuple[str, str]], List[Dict[str, Any]], str]:
    t = nbps(text or "")
    if not t.strip():
        return "0:00", 0.0, [("Total", "0:00")], [], "Not found"

    ttl_credit, ttl_src = grab_ttl_credit(t)

    res_assign_gslip = grab_labeled_time_flex(t, ["RES ASSIGN-G/SLIP PAY","RES ASSIGN G/SLIP PAY"])
    reroute_pay      = grab_labeled_time_flex(t, ["REROUTE PAY"])
    assign_pay       = grab_labeled_time_flex(t, ["ASSIGN PAY"])
    g_slip_pay       = grab_labeled_time_flex(t, ["G/SLIP PAY","G - SLIP PAY","G SLIP PAY"])
    s_slip_pay       = grab_labeled_time_flex(t, ["S/SLIP PAY","S - SLIP PAY","S SLIP PAY"])
    pbs_pr_pay       = grab_labeled_time_flex(t, ["PBS/PR PAY","PBS PR PAY"])

    pay_extras, pay_only, debug_rows = sum_daily_extras_with_debug(t)

    rows = [
        ("TTL CREDIT", from_minutes(ttl_credit)),
        ("RES ASSIGN-G/SLIP PAY", from_minutes(res_assign_gslip)),
        ("REROUTE PAY", from_minutes(reroute_pay)),
        ("ASSIGN PAY", from_minutes(assign_pay)),
        ("G/SLIP PAY", from_minutes(g_slip_pay)),
        ("S/SLIP PAY", from_minutes(s_slip_pay)),
        ("PBS/PR PAY", from_minutes(pbs_pr_pay)),
        ("PAY TIME (SCC/PVEL/LOSA/ADJ-RRPY)", from_minutes(pay_extras)),
        ("PAY ONLY (rows)", from_minutes(pay_only)),
    ]
    total_mins = sum(to_minutes(v) for _, v in rows)
    rows.append(("Total", from_minutes(total_mins)))
    total_hmm = from_minutes(total_mins)
    total_dec = round(total_mins / 60.0, 2)
    return total_hmm, total_dec, rows, debug_rows, ttl_src

# ==============================
# Streamlit UI
# ==============================

st.set_page_config(page_title="Timecard Pay Calculator", layout="wide")

st.title("🧮 Timecard Pay Calculator")
st.caption("Paste your Monthly Time Data (single line or multi-line). The app computes total pay hours accurately.")

with st.sidebar:
    st.header("Input")
    uploaded = st.file_uploader("Upload timecard text (.txt)", type=["txt"])
    example_btn = st.button("Load Example")

default_text = ""
if example_btn:
    default_text = (
        "MONTHLY TIME DATA 10/23/25 20:37:57 "
        "BID PERIOD: 01OCT25 - 31OCT25 ATL 320 B INIT LOT: 0513 "
        "NAME: EVANS,JOHN EMP NBR:0618143 "
        "TEMP IN BANK BANK ADJ IN BANK ALV - 1:08 0:00 - 1:08 77:45 "
        "ADDTL DAY ROT BLOCK SKED PAY PAY DATE DES NBR HRS TIME TIME CREDIT ONLY "
        "06OCT RES SCC 1:00 1:00 "
        "09OCT RES SCC 1:00 1:00 "
        "11OCT RES 0991 1:50 10:30 10:30 "
        "15OCT RES 5999 5:14 10:30 10:30 10:30 0:07 "
        "19OCT RES 0198 5:06 7:21 7:21 7:21 1:22 "
        "19OCT RES ADJ-RRPY 1:53 1:53 "
        "20OCT RES PVEL 10:00 10:00 "
        "22OCT RES LOSA 10:00 10:00 "
        "RES OTHER SUB TTL PAYBACK BANK OPT 1 TTL BANK OPT 1 CREDIT GUAR GUAR CREDIT NEG BANK AWD CREDIT LIMIT "
        "17:51 + 39:43 + 0:00 = 57:34 - 0:00 + 0:00 = 57:34 82:00 "
        "CREDIT APPLICABLE TO REG G/S SLIP PAY: 57:34 "
        "G/SLIP PAY : 0:00 ASSIGN PAY: 0:00 RES ASSIGN-G/SLIP PAY: 10:30 REROUTE PAY: 10:30 "
        "S/SLIP PAY : 0:00 PBS/PR PAY : 0:00 END OF DISPLAY"
    )

text_value = ""
if uploaded is not None:
    try:
        text_value = uploaded.read().decode("utf-8", errors="ignore")
    except Exception:
        text_value = uploaded.read().decode("latin1", errors="ignore")

text_area = st.text_area("Paste your timecard text here:", value=(text_value or default_text), height=260)

st.divider()
colA, colB = st.columns([1,1])

calc = colA.button("Calculate", type="primary")
clear = colB.button("Clear")

if clear:
    st.session_state.pop("calc", None)
    st.experimental_rerun()

if calc:
    st.session_state["calc"] = True

if st.session_state.get("calc"):
    hmm, dec, rows, debug_rows, ttl_src = compute_totals(text_area)

    st.subheader("Results")
    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric("Total Pay (H:MM)", hmm)
    with m2:
        st.metric("Total Pay (Decimal)", f"{dec:.2f}")
    with m3:
        st.metric("TTL Credit Source", ttl_src)

    df = pd.DataFrame(rows, columns=["Component", "Time"])
    st.table(df)

    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button("Download breakdown (CSV)", data=csv, file_name="timecard_breakdown.csv", mime="text/csv")

    with st.expander("🔎 Parse Debug (what got counted and why)"):
        dbg_df = pd.DataFrame(debug_rows)
        st.write("Each timecard row the parser detected:")
        st.dataframe(dbg_df, use_container_width=True)
        st.caption("Rules: (1) If NBR is in {SCC,PVEL,LOSA,ADJ-RRPY,ADJ-RR,ADJ,RRPY}, the last time on that row is added as Extra Pay. (2) If a row has ≥5 time tokens, the last is added as Pay Only.")

st.caption("Tip: Works with one-line or multi-line timecards. No data is stored; parsing happens in memory.")
