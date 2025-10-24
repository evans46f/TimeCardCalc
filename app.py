import re
from typing import List, Tuple, Dict, Any
import streamlit as st
import pandas as pd

# ============================================
# Helper Functions
# ============================================

def to_minutes(s: str) -> int:
    """Convert H:MM string to total minutes."""
    if not isinstance(s, str):
        return 0
    m = re.match(r"^(\d{1,3}):([0-5]\d)$", s.strip())
    if not m:
        return 0
    return int(m.group(1)) * 60 + int(m.group(2))

def from_minutes(mins: int) -> str:
    """Convert total minutes to H:MM format."""
    mins = max(0, int(mins))
    h, m = divmod(mins, 60)
    return f"{h}:{m:02d}"

def nbps(text: str) -> str:
    """Normalize NBSP characters."""
    return (text or "").replace("\u00A0", " ")

def extract_named_bucket(text: str, labels: List[str]) -> int:
    """Find standard pay categories like REROUTE PAY, ASSIGN PAY, etc."""
    t = nbps(text)
    for lbl in labels:
        pattern = re.sub(r"\s+", r"\\s+", lbl)
        pattern = re.sub(r"([/\\-])", r"\\\1", pattern)
        m = re.search(pattern + r"\s*:\s*(\d{1,3}:[0-5]\d)", t, flags=re.I)
        if m:
            return to_minutes(m.group(1))
    return 0

def extract_res_assign_gslip_bucket(text: str) -> int:
    """Special parser for RES ASSIGN-G/SLIP PAY with flexible hyphen/spaces."""
    t = nbps(text)
    m = re.search(
        r"RES\s+ASSIGN[-\s]+G/\s*SLIP\s+PAY\s*:\s*(\d{1,3}:[0-5]\d)",
        t,
        flags=re.I
    )
    if m:
        return to_minutes(m.group(1))
    return 0

def grab_sub_ttl_credit_minutes(raw: str) -> Tuple[int, str]:
    """Get the SUB TTL CREDIT value from the math line."""
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
    """Parse each duty day row (RES or REG)."""
    t = nbps(raw)
    seg_re = re.compile(
        r"(?P<date>\d{2}[A-Z]{3})\s+(?P<duty>(RES|REG))\s+(?P<nbr>[A-Z0-9-]+)(?P<tail>.*?)(?=\d{2}[A-Z]{3}\s+(RES|REG)\b|RES\s+OTHER\s+SUB\s+TTL|CREDIT\s+APPLICABLE|END OF DISPLAY|$)",
        re.I | re.S
    )
    rows: List[Dict[str, Any]] = []

    for m in seg_re.finditer(t):
        date = (m.group("date") or "").upper()
        duty = (m.group("duty") or "").upper()
        nbr = (m.group("nbr") or "").upper()
        seg = (m.group(0) or "")
        times = re.findall(r"\b\d{1,3}:[0-5]\d\b", seg)

        pay_only_time = times[-1] if len(times) >= 5 else None

        has_credit = False
        if len(times) >= 3:
            last3 = times[-3:]
            if len(set(last3)) == 1:
                has_credit = True
            if len(times) >= 4:
                last4 = times[-4:-1]
                if len(set(last4)) == 1:
                    has_credit = True

        pay_time_candidate = times[-1] if not has_credit and len(times) > 0 else None

        rows.append({
            "date": date,
            "duty": duty,
            "nbr": nbr,
            "times": times,
            "has_credit": has_credit,
            "pay_time_candidate": pay_time_candidate,
            "pay_only_time": pay_only_time,
            "raw": seg.strip()
        })
    return rows

def detect_card_type(rows: List[Dict[str, Any]]) -> str:
    """Detect if this is a LINEHOLDER or RESERVE card."""
    saw_res = any(r["duty"] == "RES" for r in rows)
    saw_reg = any(r["duty"] == "REG" for r in rows)
    if saw_reg and not saw_res:
        return "LINEHOLDER"
    return "RESERVE"

def compute_components(raw: str) -> Dict[str, Any]:
    """Compute all pay components for a timecard."""
    rows = parse_duty_rows(raw)
    card_type = detect_card_type(rows)
    sub_ttl_credit_mins, sub_src = grab_sub_ttl_credit_minutes(raw)
    pay_time_no_credit_mins = 0
    pay_only_mins = 0
    debug_rows = []

    for r in rows:
        add_pay_time = 0
        add_pay_only = 0
        if not r["has_credit"] and r["pay_time_candidate"]:
            add_pay_time = to_minutes(r["pay_time_candidate"])
            pay_time_no_credit_mins += add_pay_time
        if r["pay_only_time"]:
            if card_type == "RESERVE":
                add_pay_only = to_minutes(r["pay_only_time"])
                pay_only_mins += add_pay_only
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

    # Named pay categories
    g_slip_pay_mins = extract_named_bucket(raw, ["G/SLIP PAY", "G SLIP PAY", "G - SLIP PAY"])
    reroute_pay_mins = extract_named_bucket(raw, ["REROUTE PAY"])
    s_slip_pay_mins = extract_named_bucket(raw, ["S/SLIP PAY", "S SLIP PAY", "S - SLIP PAY"])
    pbs_pr_pay_mins = extract_named_bucket(raw, ["PBS/PR PAY", "PBS PR PAY"])
    assign_pay_mins = extract_named_bucket(raw, ["ASSIGN PAY"])
    res_assign_gslip_mins = extract_res_assign_gslip_bucket(raw)

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
    """Combine all pay components into the final total."""
    comps = compute_components(raw)
    total_mins = (
        comps["sub_ttl_credit_mins"]
        + comps["pay_time_no_credit_mins"]
        + (comps["pay_only_mins"] if comps["card_type"] == "RESERVE" else 0)
        + comps["g_slip_pay_mins"]
        + comps["reroute_pay_mins"]
        + comps["s_slip_pay_mins"]
        + comps["pbs_pr_pay_mins"]
        + comps["assign_pay_mins"]
        + comps["res_assign_gslip_mins"]
    )
    comps["total_hmm"] = from_minutes(total_mins)
    comps["total_decimal"] = round(total_mins / 60.0, 2)
    return comps

# ============================================
# Streamlit UI
# ============================================

st.set_page_config(page_title="Timecard Pay Calculator", layout="wide")
st.title("🧮 Timecard Pay Calculator")
st.caption("Paste your Monthly Time Data (lineholder or reserve).")

with st.sidebar:
    st.header("Input")
    uploaded = st.file_uploader("Upload timecard text (.txt)", type=["txt"])
    example_btn_res = st.button("Load Reserve Example")
    example_btn_line = st.button("Load Lineholder Example")

default_text = ""
if example_btn_res:
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
        "REROUTE PAY: 10:30 END OF DISPLAY"
    )

if example_btn_line:
    default_text = (
        "MONTHLY TIME DATA 10/24/25 08:37:28 "
        "BID PERIOD: 01OCT25 - 31OCT25 ATL 73N B INIT LOT: 0056 "
        "NAME: BOYES,CHRISTOPHE EMP NBR:0759386 "
        "05OCT REG 3324 8:50 10:30 10:30 10:30 "
        "07OCT REG 44WD 10:00 "
        "13OCT REG 3558 13:13 26:15 26:15 26:15 0:11 "
        "19OCT REG 3664 10:12 15:45 15:45 15:45 0:21 "
        "52:30 + 0:00 + 0:00 = 52:30 - 0:00 + 0:00 = 52:30 END OF DISPLAY"
    )

text_value = ""
if uploaded is not None:
    text_value = uploaded.read().decode("utf-8", errors="ignore")

text_area = st.text_area("Paste your timecard text:", value=(text_value or default_text), height=260)

colA, colB = st.columns([1, 1])
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
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Pay (H:MM)", comps["total_hmm"])
    c2.metric("Total Pay (Decimal)", f"{comps['total_decimal']:.2f}")
    c3.metric("Card Type", comps["card_type"])
    c4.metric("SUB TTL Source", comps["sub_ttl_src"])

    breakdown_rows = [
        ("SUB TTL CREDIT", from_minutes(comps["sub_ttl_credit_mins"])),
        ("PAY TIME ROWS (no CREDIT)", from_minutes(comps["pay_time_no_credit_mins"])),
        ("PAY ONLY bumps", from_minutes(comps["pay_only_mins"]) if comps["card_type"] == "RESERVE" else "— (ignored for lineholder)"),
        ("G/SLIP PAY", from_minutes(comps["g_slip_pay_mins"])),
        ("REROUTE PAY", from_minutes(comps["reroute_pay_mins"])),
        ("S/SLIP PAY", from_minutes(comps["s_slip_pay_mins"])),
        ("PBS/PR PAY", from_minutes(comps["pbs_pr_pay_mins"])),
        ("ASSIGN PAY", from_minutes(comps["assign_pay_mins"])),
        ("RES ASSIGN-G/SLIP PAY", from_minutes(comps["res_assign_gslip_mins"])),
        ("TOTAL", comps["total_hmm"])
    ]
    df = pd.DataFrame(breakdown_rows, columns=["Component", "Time"])
    st.table(df)

    with st.expander("Row Debug (per duty day parsing)"):
        dbg = pd.DataFrame(comps["debug_rows"])
        st.dataframe(dbg, use_container_width=True)
        st.caption(
            "- SUB TTL CREDIT: base period credit\n"
            "- PAY TIME (no CREDIT): SCC, LOSA, PVEL, etc.\n"
            "- PAY ONLY bumps: added for reserves only\n"
            "- All named PAY fields always included"
        )

st.caption("No data stored. All calculations are done locally in your session.")
