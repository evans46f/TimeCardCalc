
import re
from typing import List, Tuple

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

def grab_ttl_credit(raw: str) -> int:
    t = nbps(raw)
    # Preferred line
    m = re.search(r"CREDIT\s+APPLICABLE\s+TO\s+REG\s+G/SLIP\s+PAY\s*:\s*(\d{1,3}:[0-5]\d)", t, flags=re.I)
    if m:
        return to_minutes(m.group(1))
    # Alternate label
    m = re.search(r"TTL\s+.*CREDIT\s*:\s*(\d{1,3}:[0-5]\d)", t, flags=re.I)
    if m:
        return to_minutes(m.group(1))
    # Equation line: take last "= H:MM"
    for line in t.splitlines():
        if re.search(r"SUB\s+TTL\s+CREDIT", line, flags=re.I) or re.search(r"\bGUAR\b", line, flags=re.I):
            all_eq = list(re.finditer(r"=\s*(\d{1,3}:[0-5]\d)", line))
            if all_eq:
                return to_minutes(all_eq[-1].group(1))
    return 0

EXTRA_SET = {"SCC","PVEL","LOSA","ADJ-RRPY","ADJ-RR","ADJ","RRPY"}

def sum_daily_extras(raw: str) -> Tuple[int, int]:
    """Return (payTimeExtras, payOnlyTotal)"""
    t = nbps(raw)
    # Segment rows robustly, single or multi line
    seg_re = re.compile(r"(\d{2}[A-Z]{3}\s+RES\s+[A-Z0-9-]+)(.*?)(?=\d{2}[A-Z]{3}\s+RES\b|RES\s+OTHER\s+SUB\s+TTL|CREDIT\s+APPLICABLE|END OF DISPLAY|$)", re.I | re.S)
    pay_extras = 0
    pay_only = 0

    for m in seg_re.finditer(t):
        header = m.group(1)
        tail = m.group(2) or ""
        header_clean = re.sub(r"\s+", " ", header).strip()
        parts = [p for p in header_clean.split(" ") if p]
        nbr = (parts[2] if len(parts) >= 3 else "").upper()

        # Collect all H:MM on this row
        row_times = re.findall(r"\b\d{1,3}:[0-5]\d\b", header + " " + tail)

        # PAY ONLY: rows with >=5 time tokens → last is pay-only
        if len(row_times) >= 5:
            pay_only += to_minutes(row_times[-1])

        # Extras rows → last time on that row is PAY TIME
        if nbr in EXTRA_SET and row_times:
            pay_extras += to_minutes(row_times[-1])

    return pay_extras, pay_only

def compute_totals(text: str):
    """Return (total_hmm, total_decimal, breakdown_rows)"""
    t = nbps(text or "")
    if not t.strip():
        return "0:00", 0.0, [("Total", "0:00")]

    ttl_credit = grab_ttl_credit(t)

    res_assign_gslip = grab_labeled_time_flex(t, ["RES ASSIGN-G/SLIP PAY","RES ASSIGN G/SLIP PAY"])
    reroute_pay      = grab_labeled_time_flex(t, ["REROUTE PAY"])
    assign_pay       = grab_labeled_time_flex(t, ["ASSIGN PAY"])
    g_slip_pay       = grab_labeled_time_flex(t, ["G/SLIP PAY","G - SLIP PAY","G SLIP PAY"])
    s_slip_pay       = grab_labeled_time_flex(t, ["S/SLIP PAY","S - SLIP PAY","S SLIP PAY"])
    pbs_pr_pay       = grab_labeled_time_flex(t, ["PBS/PR PAY","PBS PR PAY"])

    pay_extras, pay_only = sum_daily_extras(t)

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
    return total_hmm, total_dec, rows

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

# Text input area
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

text_area = st.text_area("Paste your timecard text here:", value=(text_value or default_text), height=240)

st.divider()
colA, colB, colC = st.columns([1,1,2])

with colA:
    if st.button("Calculate", type="primary"):
        st.session_state["calc"] = True

with colB:
    clear = st.button("Clear")

if clear:
    st.session_state.pop("calc", None)
    st.experimental_rerun()

if st.session_state.get("calc"):
    hmm, dec, rows = compute_totals(text_area)
    st.subheader("Results")
    m1, m2 = st.columns(2)
    with m1:
        st.metric("Total Pay (H:MM)", hmm)
    with m2:
        st.metric("Total Pay (Decimal)", f"{dec:.2f}")

    # Breakdown table
    df = pd.DataFrame(rows, columns=["Component", "Time"])
    st.table(df.style.hide(axis="index"))

    # Download breakdown CSV
    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button("Download breakdown (CSV)", data=csv, file_name="timecard_breakdown.csv", mime="text/csv")

    # Copyable total
    st.code(hmm, language="text")

st.caption("Tip: Paste the entire report as one line or with original line breaks—both work.")
