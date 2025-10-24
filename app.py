import re
from typing import List, Tuple, Dict, Any
import streamlit as st
import pandas as pd

# ======================================================
# Basic helpers
# ======================================================

def to_minutes(s: str) -> int:
    """Convert H:MM string to total minutes."""
    if not isinstance(s, str):
        return 0
    m = re.match(r"^(\d{1,3}):([0-5]\d)$", s.strip())
    if not m:
        return 0
    return int(m.group(1)) * 60 + int(m.group(2))

def from_minutes(mins: int) -> str:
    """Convert total minutes to H:MM string."""
    mins = max(0, int(mins))
    h, m = divmod(mins, 60)
    return f"{h}:{m:02d}"

def nbps(text: str) -> str:
    """Normalize NBSPs and similar weird whitespace."""
    return (text or "").replace("\u00A0", " ")

# ======================================================
# Parsing helpers for summary lines
# ======================================================

def grab_sub_ttl_credit_minutes(raw: str) -> Tuple[int, str]:
    """
    Get the SUB TTL CREDIT / final period credit from the math block.
    We take the LAST '= H:MM' found on any math-ish line.
    Example line:
      68:34 + 0:00 + 0:00 = 68:34 - 0:00 + 3:26 = 72:00
    -> returns 72:00 (4320 min)
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

# ======================================================
# Parsing helpers for named pay buckets at the bottom
# ======================================================

def extract_named_bucket(text: str, labels: List[str]) -> int:
    """
    Extract values like:
      REROUTE PAY: 10:30
      ASSIGN PAY: 0:00
      G/SLIP PAY : 10:30
    Whitespace and punctuation (/,-) are made flexible.
    """
    t = nbps(text)
    for lbl in labels:
        # make interior whitespace flexible
        pattern = re.sub(r"\s+", r"\\s+", lbl)
        # escape /, \, -
        pattern = re.sub(r"([/\\-])", r"\\\1", pattern)
        m = re.search(pattern + r"\s*:\s*(\d{1,3}:[0-5]\d)", t, flags=re.I)
        if m:
            return to_minutes(m.group(1))
    return 0

def extract_res_assign_gslip_bucket(text: str) -> int:
    """
    Special-case parser for 'RES ASSIGN-G/SLIP PAY:' or 'RES ASSIGN G/SLIP PAY:'.
    Very flexible around the hyphen and the space after G/.
    """
    t = nbps(text)
    m = re.search(
        r"RES\s+ASSIGN[-\s]+G/\s*SLIP\s+PAY\s*:\s*(\d{1,3}:[0-5]\d)",
        t,
        flags=re.I
    )
    if m:
        return to_minutes(m.group(1))
    return 0

# ======================================================
# Duty row parsing logic
# ======================================================

def parse_duty_rows(raw: str) -> List[Dict[str, Any]]:
    """
    Parse each duty day row like:
      06OCT RES SCC 1:00 1:00
      05JUN REG 3210 7:24 10:30 10:30 10:30
      01JUN REG 3554 6:30 TRANS TRANS 10:49 0:13
      28JUN REG 0451 1:35 10:30 10:30 3:23

    For each row we extract:
    - date (e.g. 06OCT)
    - duty (RES or REG)
    - nbr  (pairing/code, e.g. SCC, 3210, RRPY)
    - times (all H:MM tokens in the row)
    - has_credit (True if it looks like a pairing with built-in credit)
    - pay_time_candidate (if row has NO credit, the big standalone pay time)
    - pay_only_candidate (if row ends in an "extra bump" value -> PAY ONLY)

    We'll detect credit and pay-only with heuristics.
    """
    t = nbps(raw)

    seg_re = re.compile(
        r"(?P<date>\d{2}[A-Z]{3})\s+"
        r"(?P<duty>(RES|REG))\s+"
        r"(?P<nbr>[A-Z0-9-]+)"
        r"(?P<tail>.*?)(?="
            r"\d{2}[A-Z]{3}\s+(RES|REG)\b|"
            r"RES\s+OTHER\s+SUB\s+TTL|"
            r"CREDIT\s+APPLICABLE|"
            r"END OF DISPLAY|$"
        ")",
        re.I | re.S
    )

    rows: List[Dict[str, Any]] = []

    for m in seg_re.finditer(t):
        date = (m.group("date") or "").upper()
        duty = (m.group("duty") or "").upper()
        nbr  = (m.group("nbr") or "").upper()
        seg  = (m.group(0) or "")

        # Grab all H:MM-like tokens
        times = re.findall(r"\b\d{1,3}:[0-5]\d\b", seg)

        # ---------------------------------
        # Detect if this row has credit
        # ---------------------------------
        # We consider it "credit" if near the end you see repeating times
        # like "10:30 10:30 10:30" or "15:45 15:45 15:45".
        has_credit = False
        if len(times) >= 3:
            last3 = times[-3:]
            if len(set(last3)) == 1:
                has_credit = True
        if len(times) >= 4 and not has_credit:
            # e.g. "... 15:45 15:45 15:45 0:38"
            last4_main = times[-4:-1]
            if len(set(last4_main)) == 1:
                has_credit = True

        # ---------------------------------
        # PAY TIME (no CREDIT)
        # ---------------------------------
        # If the row does NOT appear to have a credit block,
        # we treat the last time token as "pay_time_candidate".
        # Ex: "RRPY 3:09" or "RRPY 5:26".
        pay_time_candidate = None
        if not has_credit and times:
            pay_time_candidate = times[-1]

        # ---------------------------------
        # PAY ONLY bump detection (NEW RULE)
        # ---------------------------------
        # Previously: we only counted the last time if there were >=5 tokens.
        # Now: If the row has >=2 time tokens, and the LAST time is strictly
        # smaller than the previous time, we treat that LAST time as a PAY ONLY bump.
        #
        # Why: this catches things like:
        #  - "... 10:49 0:13"
        #  - "... 10:30 10:30 3:23"
        #  - "... 15:45 15:45 15:45 0:38"
        pay_only_candidate = None
        if len(times) >= 2:
            last_time = times[-1]
            prev_time = times[-2]

            # Convert both to minutes so we can compare
            last_m  = to_minutes(last_time)
            prev_m  = to_minutes(prev_time)

            # If the last value is strictly less than the previous,
            # we consider it an incremental pay-only bump.
            if last_m < prev_m:
                pay_only_candidate = last_time

        rows.append({
            "date": date,
            "duty": duty,
            "nbr": nbr,
            "times": times,
            "has_credit": has_credit,
            "pay_time_candidate": pay_time_candidate,
            "pay_only_candidate": pay_only_candidate,
            "raw": seg.strip(),
        })

    return rows

def detect_card_type(rows: List[Dict[str, Any]]) -> str:
    """
    We still expose what kind of card we THINK this is (lineholder vs reserve),
    just for UI/debug clarity. (It no longer changes the math.)
    Rule:
      - If we saw REG rows and no RES rows -> LINEHOLDER
      - Otherwise -> RESERVE
    """
    saw_res = any(r["duty"] == "RES" for r in rows)
    saw_reg = any(r["duty"] == "REG" for r in rows)
    if saw_reg and not saw_res:
        return "LINEHOLDER"
    return "RESERVE"

# ======================================================
# Component calculations
# ======================================================

def compute_components(raw: str) -> Dict[str, Any]:
    """
    Compute everything we add to get final pay.

    Formula (universal):
      TOTAL PAY =
          SUB TTL CREDIT
        + PAY TIME rows with NO CREDIT
        + PAY ONLY bumps
        + G/SLIP PAY
        + REROUTE PAY
        + S/SLIP PAY
        + PBS/PR PAY
        + ASSIGN PAY
        + RES ASSIGN-G/SLIP PAY
    """
    rows = parse_duty_rows(raw)
    card_type = detect_card_type(rows)

    sub_ttl_credit_mins, sub_src = grab_sub_ttl_credit_minutes(raw)

    pay_time_no_credit_mins = 0  # RRPY-style standalone pay time
    pay_only_mins = 0           # tail bumps like 0:13, 3:38, 3:23, etc.

    debug_rows = []
    for r in rows:
        add_pay_time = 0
        add_pay_only = 0

        if r["pay_time_candidate"] and not r["has_credit"]:
            # ex: REG RRPY 3:09 (no CREDIT block)
            add_pay_time = to_minutes(r["pay_time_candidate"])
            pay_time_no_credit_mins += add_pay_time

        if r["pay_only_candidate"]:
            # ex: 10:49 0:13  -> add 0:13
            # ex: ...10:30 10:30 3:23 -> add 3:23
            add_pay_only = to_minutes(r["pay_only_candidate"])
            pay_only_mins += add_pay_only

        debug_rows.append({
            "Date": r["date"],
            "Duty": r["duty"],
            "NBR": r["nbr"],
            "Times": ", ".join(r["times"]),
            "Has CREDIT?": "Y" if r["has_credit"] else "N",
            "PayTime(no CREDIT) added": from_minutes(add_pay_time) if add_pay_time else "",
            "PayOnly added": from_minutes(add_pay_only) if add_pay_only else "",
            "Raw Row Snippet": r["raw"][:200],
        })

    # Named buckets (always added)
    g_slip_pay_mins       = extract_named_bucket(raw, ["G/SLIP PAY", "G SLIP PAY", "G - SLIP PAY"])
    reroute_pay_mins      = extract_named_bucket(raw, ["REROUTE PAY"])
    s_slip_pay_mins       = extract_named_bucket(raw, ["S/SLIP PAY", "S SLIP PAY", "S - SLIP PAY"])
    pbs_pr_pay_mins       = extract_named_bucket(raw, ["PBS/PR PAY", "PBS PR PAY"])
    assign_pay_mins       = extract_named_bucket(raw, ["ASSIGN PAY"])
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
        "debug_rows": debug_rows,
    }

def compute_totals(raw: str) -> Dict[str, Any]:
    """
    Final math:
      TOTAL PAY =
          SUB TTL CREDIT
        + PAY TIME rows with NO CREDIT
        + PAY ONLY bumps
        + all named bottom-line buckets
    """
    comps = compute_components(raw)

    total_mins = (
        comps["sub_ttl_credit_mins"]
        + comps["pay_time_no_credit_mins"]
        + comps["pay_only_mins"]
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

# ======================================================
# Streamlit UI
# ======================================================

st.set_page_config(page_title="Timecard Pay Calculator", layout="wide")

st.title("🧮 Timecard Pay Calculator")
st.caption("Paste your Monthly Time Data (lineholder or reserve). I total pay using your contract math.")

with st.sidebar:
    st.header("Input")
    uploaded = st.file_uploader("Upload timecard text (.txt)", type=["txt"])

    example_btn_res = st.button("Load Reserve Example")
    example_btn_line = st.button("Load Lineholder Example")

default_text = ""
if example_btn_res:
    # Reserve-style with reroute + RES ASSIGN-G/SLIP, expected total ~103:56 under these rules
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
        'REROUTE PAY: 10:30 END OF DISPLAY'
    )

if example_btn_line:
    # Lineholder-style with TRANS / RRPY / guarantee / g-slip
    # Should produce ~98:57 under the finalized rules.
    default_text = (
        "MONTHLY TIME DATA 10/24/25 08:38:49 "
        "BID PERIOD: 02JUN25 - 01JUL25 ATL 73N B INIT LOT: 0059 "
        "NAME: BOYES,CHRISTOPHE EMP NBR:0759386 "
        "01JUN REG 3554 6:30 TRANS TRANS 10:49 0:13 "
        "05JUN REG 3210 7:24 10:30 10:30 10:30 "
        "09JUN REG 3191 6:52 10:30 10:30 10:30 "
        "17JUN REG 0889 2:20 10:30 10:30 10:30 "
        "18JUN REG RRPY 3:09 "
        "23JUN REG C428 15:01 15:45 15:45 15:45 0:38 "
        "26JUN REG 0608 5:16 10:30 10:30 10:30 3:38 "
        "27JUN REG RRPY 5:26 "
        "28JUN REG 0451 1:35 10:30 10:30 3:23 "
        "68:34 + 0:00 + 0:00 = 68:34 - 0:00 + 3:26 = 72:00 "
        "G/SLIP PAY : 10:30 REROUTE PAY: 0:00 END OF DISPLAY"
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
    c3.metric("Detected Card Type", comps["card_type"])
    c4.metric("SUB TTL Source", comps["sub_ttl_src"])

    breakdown_rows = [
        ("SUB TTL CREDIT", from_minutes(comps["sub_ttl_credit_mins"])),
        ("PAY TIME ROWS (no CREDIT)", from_minutes(comps["pay_time_no_credit_mins"])),
        ("PAY ONLY bumps", from_minutes(comps["pay_only_mins"])),
        ("G/SLIP PAY", from_minutes(comps["g_slip_pay_mins"])),
        ("REROUTE PAY", from_minutes(comps["reroute_pay_mins"])),
        ("S/SLIP PAY", from_minutes(comps["s_slip_pay_mins"])),
        ("PBS/PR PAY", from_minutes(comps["pbs_pr_pay_mins"])),
        ("ASSIGN PAY", from_minutes(comps["assign_pay_mins"])),
        ("RES ASSIGN-G/SLIP PAY", from_minutes(comps["res_assign_gslip_mins"])),
        ("TOTAL", comps["total_hmm"]),
    ]

    df = pd.DataFrame(breakdown_rows, columns=["Component", "Time"])
    st.table(df)

    with st.expander("Row Debug (what each duty day contributed)"):
        dbg = pd.DataFrame(comps["debug_rows"])
        st.dataframe(dbg, use_container_width=True)
        st.caption(
            "Math = SUB TTL CREDIT "
            "+ PAY TIME rows with no CREDIT "
            "+ PAY ONLY bumps "
            "+ G/SLIP / REROUTE / S-SLIP / PBS/PR / ASSIGN / RES ASSIGN-G/SLIP."
        )

st.caption("No data stored. All calculations are done locally in your browser session.")
