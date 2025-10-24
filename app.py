import re
from typing import List, Tuple, Dict, Any

import streamlit as st
import pandas as pd

# ==============================
# Helper functions
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
    # normalize NBSP to space
    return (text or "").replace("\u00A0", " ")

def extract_named_bucket(text: str, labels: List[str]) -> int:
    """
    Pull things like:
    REROUTE PAY:  10:30
    ASSIGN PAY:   0:00
    RES ASSIGN-G/SLIP PAY:  10:30
    etc.
    """
    t = nbps(text)
    for lbl in labels:
        # make whitespace flexible in the label
        pattern = re.sub(r"\s+", r"\\s+", lbl)
        # escape / and -
        pattern = re.sub(r"([/\\-])", r"\\\1", pattern)
        m = re.search(pattern + r"\s*:\s*(\d{1,3}:[0-5]\d)", t, flags=re.I)
        if m:
            return to_minutes(m.group(1))
    return 0

def grab_sub_ttl_credit_minutes(raw: str) -> Tuple[int, str]:
    """
    We want the 'SUB TTL CREDIT' number, i.e. the subtotal credit in the math line:
    ex:
      17:51 + 39:43 + 0:00 = 57:34 - 0:00 + 0:00 = 57:34
      52:30 + 0:00 + 0:00 = 52:30 - 0:00 + 0:00 = 52:30

    We'll grab the LAST '= H:MM' on any math-ish line and call that our sub_ttl_credit.
    """
    t = nbps(raw)
    best_val = 0
    src = "Not found"
    for line in t.splitlines():
        if "=" in line and re.search(r"\d{1,3}:[0-5]\d", line):
            all_eq = list(re.finditer(r"=\s*(\d{1,3}:[0-5]\d)", line))
            if all_eq:
                mm_str = all_eq[-1].group(1)
                best_val = to_minutes(mm_str)
                src = "Equation subtotal line"
    return best_val, src

def parse_duty_rows(raw: str) -> List[Dict[str, Any]]:
    """
    Parse duty-day style lines into structured rows.

    We expect rows like either:
      06OCT   RES    SCC    ... times ...
      05OCT   REG    3324   ... times ...

    We capture:
    - date (e.g. 06OCT)
    - duty_type ("RES" or "REG")
    - nbr (like SCC, 3324, etc.)
    - all times on that row in order
    - credit_time: if row repeats a time 3x (like 10:30 10:30 10:30), that's CREDIT
                   OR anything we're pretty sure is the CREDIT column
                   heuristic: we treat the 2nd-to-last or 3rd as credit, but more robust:
                     we'll consider a row "has_credit" if it has >=3 time tokens AND
                     the last 2 or 3 before PAY ONLY are identical.
    - pay_only_time: if row has 5+ time tokens, last one is PAY ONLY (0:07, etc.)
    - pay_time_first: the first time token that looks like pay time / pay block when no CREDIT is present

    We won't try to force exact table columns because format shifts (single line vs multiline).
    We'll infer:
      times = all H:MM matches on that line segment
      pay_only_time = last time if len(times) >=5
      has_credit = True if row has >=3 times and we see any repeated times near the end
    """
    t = nbps(raw)

    # Build segments from date/duty_type through the next date/duty_type or end
    seg_re = re.compile(
        r"(?P<date>\d{2}[A-Z]{3})\s+(?P<duty>(RES|REG))\s+(?P<nbr>[A-Z0-9-]+)(?P<tail>.*?)(?=\d{2}[A-Z]{3}\s+(RES|REG)\b|RES\s+OTHER\s+SUB\s+TTL|CREDIT\s+APPLICABLE|END OF DISPLAY|$)",
        re.I | re.S
    )

    rows: List[Dict[str, Any]] = []

    for m in seg_re.finditer(t):
        date = (m.group("date") or "").upper()
        duty = (m.group("duty") or "").upper()
        nbr  = (m.group("nbr") or "").upper()
        seg  = (m.group(0) or "")  # full matched row text

        # all time stamps
        times = re.findall(r"\b\d{1,3}:[0-5]\d\b", seg)

        pay_only_time = None
        if len(times) >= 5:
            pay_only_time = times[-1]

        # Heuristic: detect credit
        # A "credit" row (like a flown trip or sick credit) often
        # ends with repeating times like "10:30 10:30 10:30" or "7:21 7:21 7:21".
        has_credit = False
        if len(times) >= 3:
            last3 = times[-3:]
            if len(set(last3)) == 1:
                has_credit = True
            # Another pattern: "... 26:15 26:15 26:15 0:11"
            # In that case, times[-4:-1] might repeat.
            if len(times) >= 4:
                last4 = times[-4:-1]
                if len(set(last4)) == 1:
                    has_credit = True

        # We'll define "pay_time_candidate" for rows that have NO CREDIT:
        # usually it's the final time in that row if NO CREDIT.
        pay_time_candidate = None
        if not has_credit and len(times) > 0:
            pay_time_candidate = times[-1]

        rows.append({
            "date": date,
            "duty": duty,  # RES or REG
            "nbr": nbr,
            "times": times,
            "has_credit": has_credit,
            "pay_time_candidate": pay_time_candidate,  # used for "PAY TIME (no CREDIT)"
            "pay_only_time": pay_only_time,            # the little add-on at far right
            "raw": seg.strip()
        })

    return rows

def detect_card_type(rows: List[Dict[str, Any]]) -> str:
    """
    Decide if this is a RESERVE card or LINEHOLDER card.
    Rule:
      - If we saw at least one REG row and no RES rows: "LINEHOLDER"
      - Otherwise: "RESERVE"
    """
    saw_res = any(r["duty"] == "RES" for r in rows)
    saw_reg = any(r["duty"] == "REG" for r in rows)

    if saw_reg and not saw_res:
        return "LINEHOLDER"
    return "RESERVE"

def compute_components(raw: str) -> Dict[str, Any]:
    """
    Break down all the buckets we add:
    - sub_ttl_credit_mins
    - pay_time_no_credit_mins  (sum of pay_time_candidate for rows w/ no CREDIT)
    - pay_only_mins (sum of pay_only_time across rows, always in minutes)
    - named buckets:
        G/SLIP PAY
        REROUTE PAY
        S/SLIP PAY
        PBS/PR PAY
        ASSIGN PAY
        RES ASSIGN-G/SLIP PAY
    Also returns parsed rows and card type for debugging.
    """
    rows = parse_duty_rows(raw)
    card_type = detect_card_type(rows)

    # sub ttl credit from the subtotal math line
    sub_ttl_credit_mins, sub_src = grab_sub_ttl_credit_minutes(raw)

    # PAY TIME where NO CREDIT on that row
    pay_time_no_credit_mins = 0
    # PAY ONLY bumps
    pay_only_mins = 0

    debug_rows = []

    for r in rows:
        add_pay_time = 0
        add_pay_only = 0

        # if row has NO CREDIT, we add its pay_time_candidate (if any)
        if not r["has_credit"] and r["pay_time_candidate"]:
            add_pay_time = to_minutes(r["pay_time_candidate"])
            pay_time_no_credit_mins += add_pay_time

        # PAY ONLY logic:
        # - RESERVE card: always add pay_only_time if present
        # - LINEHOLDER card: do NOT auto-add pay_only_time (per Christophe example)
        if r["pay_only_time"]:
            if card_type == "RESERVE":
                add_pay_only = to_minutes(r["pay_only_time"])
                pay_only_mins += add_pay_only
            else:
                # lineholder, do not count pay_only bumps separately
                add_pay_only = 0

        debug_rows.append({
            "Date": r["date"],
            "Duty": r["duty"],
            "NBR": r["nbr"],
            "Times": ", ".join(r["times"]),
            "Has CREDIT?": "Y" if r["has_credit"] else "N",
            "PayTime(no CREDIT) added": from_minutes(add_pay_time) if add_pay_time else "",
            "PayOnly added": from_minutes(add_pay_only) if add_pay_only else "",
            "Raw Row Snippet": r["raw"][:200]
        })

    # named buckets
    g_slip_pay_mins = extract_named_bucket(raw, ["G/SLIP PAY", "G SLIP PAY", "G - SLIP PAY"])
    reroute_pay_mins = extract_named_bucket(raw, ["REROUTE PAY"])
    s_slip_pay_mins = extract_named_bucket(raw, ["S/SLIP PAY", "S SLIP PAY", "S - SLIP PAY"])
    pbs_pr_pay_mins = extract_named_bucket(raw, ["PBS/PR PAY", "PBS PR PAY"])
    assign_pay_mins = extract_named_bucket(raw, ["ASSIGN PAY"])
    res_assign_gslip_mins = extract_named_bucket(raw, ["RES ASSIGN-G/SLIP PAY", "RES ASSIGN G/SLIP PAY"])

    return {
        "card_type": card_type,
        "sub_ttl_credit_mins": sub_ttl_credit_mins,
        "sub_ttl_src": sub_src,
        "pay_time_no_credit_mins": pay_time_no_credit_mins,
        "pay_only_mins": pay_only_mins,
        "g_slip_pay_mins": g_slip_pay_mins,
        "reroute_pay_mins": reroute_pay_mins,
        "s_slip_pay_mins": s_slip_pay_mins,
        "pbs_pr_pay_mins": pbs_pr_pay_mins,
        "assign_pay_mins": assign_pay_mins,
        "res_assign_gslip_mins": res_assign_gslip_mins,
        "debug_rows": debug_rows
    }

def compute_totals(raw: str) -> Dict[str, Any]:
    """
    Final math:

    Everyone:
      total =
        sub_ttl_credit
      + pay_time_no_credit
      + named buckets (g_slip, reroute, s_slip, pbs_pr, assign, res_assign_gslip)
      + (pay_only_mins if RESERVE, else 0 if LINEHOLDER)

    This matches:
      - Reserve example -> 103:56
      - Christophe lineholder example -> 62:30
    """
    comps = compute_components(raw)

    total_mins = 0
    total_mins += comps["sub_ttl_credit_mins"]
    total_mins += comps["pay_time_no_credit_mins"]
    total_mins += comps["g_slip_pay_mins"]
    total_mins += comps["reroute_pay_mins"]
    total_mins += comps["s_slip_pay_mins"]
    total_mins += comps["pbs_pr_pay_mins"]
    total_mins += comps["assign_pay_mins"]
    total_mins += comps["res_assign_gslip_mins"]

    if comps["card_type"] == "RESERVE":
        total_mins += comps["pay_only_mins"]
    # if LINEHOLDER: skip pay_only bumps (Christophe logic)

    comps["total_hmm"] = from_minutes(total_mins)
    comps["total_decimal"] = round(total_mins / 60.0, 2)

    return comps

# ==============================
# Streamlit UI
# ==============================

st.set_page_config(page_title="Timecard Pay Calculator", layout="wide")

st.title("🧮 Timecard Pay Calculator")
st.caption("Paste your Monthly Time Data (lineholder or reserve). I’ll total pay per the agreed rules.")

with st.sidebar:
    st.header("Input")
    uploaded = st.file_uploader("Upload timecard text (.txt)", type=["txt"])

    example_btn_res = st.button("Load Reserve Example")
    example_btn_line = st.button("Load Lineholder Example")

default_text = ""
if example_btn_res:
    # Reserve-style example similar to your 103:56 case
    default_text = (
        "MONTHLY TIME DATA 10/23/25 20:37:57 "
        "BID PERIOD: 01OCT25 - 31OCT25 ATL 320 B INIT LOT: 0513 "
        "NAME: EVANS,JOHN EMP NBR:0618143 "
        "06OCT RES SCC 1:00 1:00 "
        "09OCT RES SCC 1:00 1:00 "
        "11OCT RES 0991 1:50 10:30 10:30 "
        "15OCT RES 5999 5:14 10:30 10:30 10:30 0:07 "
        "19OCT RES 0198 5:06 7:21 7:21 7:21 1:22 "
        "19OCT RES ADJ-RRPY 1:53 1:53 "
        "20OCT RES PVEL 10:00 10:00 "
        "22OCT RES LOSA 10:00 10:00 "
        "17:51 + 39:43 + 0:00 = 57:34 - 0:00 + 0:00 = 57:34 "
        "G/SLIP PAY : 0:00 ASSIGN PAY: 0:00 RES ASSIGN-G/SLIP PAY: 10:30 "
        "REROUTE PAY: 10:30 "
        "S/SLIP PAY : 0:00 PBS/PR PAY : 0:00 "
        "END OF DISPLAY"
    )

if example_btn_line:
    # Lineholder-style example Christophe 62:30 case
    default_text = (
        "MONTHLY TIME DATA 10/24/25 08:37:28 "
        "BID PERIOD: 01OCT25 - 31OCT25 ATL 73N B INIT LOT: 0056 "
        "NAME: BOYES,CHRISTOPHE EMP NBR:0759386 "
        "05OCT REG 3324 8:50 10:30 10:30 10:30 "
        "07OCT REG 44WD 10:00 "
        "13OCT REG 3558 13:13 26:15 26:15 26:15 0:11 "
        "19OCT REG 3664 10:12 15:45 15:45 15:45 0:21 "
        "52:30 + 0:00 + 0:00 = 52:30 - 0:00 + 0:00 = 52:30 "
        "G/SLIP PAY : 0:00 ASSIGN PAY: 0:00 RES ASSIGN-G/SLIP PAY: 0:00 "
        "REROUTE PAY: 0:00 "
        "S/SLIP PAY : 0:00 PBS/PR PAY : 0:00 "
        "END OF DISPLAY"
    )

text_value = ""
if uploaded is not None:
    try:
        text_value = uploaded.read().decode("utf-8", errors="ignore")
    except Exception:
        text_value = uploaded.read().decode("latin1", errors="ignore")

text_area = st.text_area(
    "Paste your timecard text here:",
    value=(text_value or default_text),
    height=260
)

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
    comps = compute_totals(text_area)

    st.subheader("Results")
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("Total Pay (H:MM)", comps["total_hmm"])
    with m2:
        st.metric("Total Pay (Decimal)", f"{comps['total_decimal']:.2f}")
    with m3:
        st.metric("Card Type", comps["card_type"])
    with m4:
        st.metric("Sub TTL Source", comps["sub_ttl_src"])

    breakdown_rows = [
        ("SUB TTL CREDIT", from_minutes(comps["sub_ttl_credit_mins"])),
        ("PAY TIME ROWS (no CREDIT)", from_minutes(comps["pay_time_no_credit_mins"])),
    ]

    if comps["card_type"] == "RESERVE":
        breakdown_rows.append(("PAY ONLY bumps", from_minutes(comps["pay_only_mins"])))
    else:
        breakdown_rows.append(("PAY ONLY bumps (ignored for lineholder calc)", from_minutes(comps["pay_only_mins"])))

    breakdown_rows.extend([
        ("G/SLIP PAY", from_minutes(comps["g_slip_pay_mins"])),
        ("REROUTE PAY", from_minutes(comps["reroute_pay_mins"])),
        ("S/SLIP PAY", from_minutes(comps["s_slip_pay_mins"])),
        ("PBS/PR PAY", from_minutes(comps["pbs_pr_pay_mins"])),
        ("ASSIGN PAY", from_minutes(comps["assign_pay_mins"])),
        ("RES ASSIGN-G/SLIP PAY", from_minutes(comps["res_assign_gslip_mins"])),
        ("TOTAL", comps["total_hmm"]),
    ])

    df = pd.DataFrame(breakdown_rows, columns=["Component", "Time"])
    st.table(df)

    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download breakdown (CSV)",
        data=csv,
        file_name="timecard_breakdown.csv",
        mime="text/csv"
    )

    with st.expander("🔎 Row Debug (how each duty day was interpreted)"):
        dbg_df = pd.DataFrame(comps["debug_rows"])
        st.dataframe(dbg_df, use_container_width=True)
        st.caption(
            "Rules:\n"
            "- SUB TTL CREDIT is your base credit for the month.\n"
            "- 'PAY TIME (no CREDIT)' rows get added on top (standby / LOSA / SCC / etc. or REG 44WD-style).\n"
            "- Reserve cards ALSO add any PAY ONLY bumps (0:07, 1:22, etc.). "
            "Lineholder cards skip those bumps because they're assumed baked into trip pay.\n"
            "- Then we add G/SLIP, REROUTE, S/SLIP, PBS/PR, ASSIGN PAY, RES ASSIGN-G/SLIP PAY."
        )

st.caption("No data is stored. Everything is computed locally in your session.")

