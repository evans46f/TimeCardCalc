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
      27JUN REG RRPY 5:26

    For each row we extract:
    - date, duty (RES/REG), nbr
    - times = all H:MM tokens
    - has_credit: does this row clearly show pairing credit (trip-style credit)?
                  heuristic: repeated same time 3+ times near end
    - has_trans: row text contains 'TRANS'
    - main_pay_candidate: base pay for this day that is NOT already in SUB TTL CREDIT
      Examples:
        * RRPY 3:09
        * RRPY 5:26
        * ...10:30 10:30 3:23 -> 10:30
        * We SKIP 10:49 on TRANS rows, and we SKIP 15:45 if it's repeated 3x
    - bump_pay_candidate: last add-on bump (0:13, 0:38, 3:38, 3:23)
                          Defined as: if last time is strictly smaller than previous time

    This matches the breakdown you gave:
      Pay only        = main_pay_candidate sum
      ADDTL PAY ONLY  = bump_pay_candidate sum
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
        tail_text = (m.group("tail") or "")

        # all H:MM-ish tokens
        times = re.findall(r"\b\d{1,3}:[0-5]\d\b", seg)

        # detect TRANS explicitly
        has_trans = "TRANS" in tail_text.upper()

        # detect "has_credit" = repeated same number 3+ times before a small bump
        # e.g. "15:45 15:45 15:45 0:38", "10:30 10:30 10:30 3:38"
        has_credit = False
        repeated_credit_value = None
        if len(times) >= 4:
            # look at everything except final bump
            before_last = times[:-1]
            # check last up to 3 tokens before final
            last4_main = before_last[-3:]
            if len(last4_main) == 3 and len(set(last4_main)) == 1:
                has_credit = True
                repeated_credit_value = last4_main[0]

        # next: detect bump_pay_candidate (ADDTL PAY ONLY)
        # rule: last time < previous time means the last time is a bump
        bump_pay_candidate = None
        if len(times) >= 2:
            prev_time = times[-2]
            last_time = times[-1]
            if to_minutes(last_time) < to_minutes(prev_time):
                bump_pay_candidate = last_time

        # detect main_pay_candidate ("Pay only" bucket)
        # There are two big sources:
        # - RRPY rows (standalone pay, like "RRPY 3:09")
        # - rows that end with main_time then a smaller bump (e.g. "10:30 10:30 3:23")
        #   BUT:
        #   * if main_time is repeated 3+ times (trip/credit rows) -> skip, already in credit
        #   * if it's a TRANS row -> skip main_time completely (you told me not to count 10:49)
        main_pay_candidate = None

        # Case 1: RRPY row (always take its time)
        if "RRPY" in nbr:
            # Usually these are like "RRPY 3:09" (single time)
            if times:
                main_pay_candidate = times[-1]

        # Case 2: non-RRPY rows with a "main then bump" shape
        elif len(times) >= 2:
            prev_time = times[-2]
            last_time = times[-1]
            prev_m = to_minutes(prev_time)
            last_m = to_minutes(last_time)

            if last_m < prev_m:
                # We have main pay (prev_time) and bump pay (last_time)
                # We may or may not include prev_time.
                # We include prev_time as main pay UNLESS:
                #  - It's a TRANS row, or
                #  - That prev_time is part of a 3x repeat block (like 15:45 15:45 15:45 ...)
                #    meaning it's already baked into credit guarantee.
                if not has_trans:
                    # count how many times prev_time appeared BEFORE the last one
                    occurrences_prev_before_last = [t for t in times[:-1] if t == prev_time]
                    if not (has_credit and repeated_credit_value == prev_time and len(occurrences_prev_before_last) >= 3):
                        # Special handling: has_credit will only be True if repeated >=3,
                        # so essentially:
                        # - If prev_time shows up 3+ times, skip it, it's already in credit.
                        # - Else include it.
                        #
                        # BUT NOTE: if has_credit==True we *know* prev_time repeated at least 3x,
                        # so that path won't fire anyway.
                        # We'll also add an extra explicit guard:
                        if len(occurrences_prev_before_last) < 3:
                            main_pay_candidate = prev_time

        rows.append({
            "date": date,
            "duty": duty,
            "nbr": nbr,
            "times": times,
            "has_credit_block": has_credit,
            "has_trans": has_trans,
            "main_pay_candidate": main_pay_candidate,  # goes to "Pay only"
            "bump_pay_candidate": bump_pay_candidate,  # goes to "ADDTL PAY ONLY"
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

    Formula you just finalized:
      TOTAL PAY =
          SUB TTL CREDIT
        + SUM(main_pay_candidate)         (aka "Pay only": RRPY, 10:30 from partial day, etc.)
        + SUM(bump_pay_candidate)         (aka "ADDTL PAY ONLY": 0:13, 3:38, etc.)
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

    pay_only_main_mins = 0       # sum(main_pay_candidate)
    pay_only_bump_mins = 0       # sum(bump_pay_candidate)

    debug_rows = []
    for r in rows:
        add_main = 0
        add_bump = 0

        if r["main_pay_candidate"]:
            add_main = to_minutes(r["main_pay_candidate"])
            pay_only_main_mins += add_main

        if r["bump_pay_candidate"]:
            add_bump = to_minutes(r["bump_pay_candidate"])
            pay_only_bump_mins += add_bump

        debug_rows.append({
            "Date": r["date"],
            "Duty": r["duty"],
            "NBR": r["nbr"],
            "Times": ", ".join(r["times"]),
            "Has credit block (3x repeat)?": "Y" if r["has_credit_block"] else "N",
            "TRANS Row?": "Y" if r["has_trans"] else "N",
            "Main Pay added (Pay only)": from_minutes(add_main) if add_main else "",
            "Bump Pay added (ADDTL PAY ONLY)": from_minutes(add_bump) if add_bump else "",
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

        "pay_only_main_mins": pay_only_main_mins,   # bucket: Pay only
        "pay_only_bump_mins": pay_only_bump_mins,   # bucket: ADDTL PAY ONLY

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
        + PAY ONLY (main_pay_candidate)
        + ADDTL PAY ONLY (bump_pay_candidate)
        + G/SLIP PAY
        + REROUTE PAY
        + S/SLIP PAY
        + PBS/PR PAY
        + ASSIGN PAY
        + RES ASSIGN-G/SLIP PAY
    """
    comps = compute_components(raw)

    total_mins = (
        comps["sub_ttl_credit_mins"]
        + comps["pay_only_main_mins"]
        + comps["pay_only_bump_mins"]
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
st.caption("Paste your Monthly Time Data. I total pay using the exact buckets you defined (SUB TTL CREDIT + Pay only + ADDTL PAY ONLY + named pays).")

with st.sidebar:
    st.header("Input")
    uploaded = st.file_uploader("Upload timecard text (.txt)", type=["txt"])

    example_btn_res = st.button("Load Reserve Example")
    example_btn_line = st.button("Load Lineholder Example")

default_text = ""
if example_btn_res:
    # Reserve-style example, should give ~103:56 under this logic.
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
    # Lineholder-style example (the June card),
    # should return ~109:27 after applying the final logic.
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
    c1.metric("TOTAL PAY (H:MM)", comps["total_hmm"])
    c2.metric("TOTAL PAY (Decimal)", f"{comps['total_decimal']:.2f}")
    c3.metric("Detected Card Type", comps["card_type"])
    c4.metric("SUB TTL Source", comps["sub_ttl_src"])

    breakdown_rows = [
        ("SUB TTL CREDIT", from_minutes(comps["sub_ttl_credit_mins"])),
        ("PAY ONLY (main pay)", from_minutes(comps["pay_only_main_mins"])),
        ("ADDTL PAY ONLY (bumps)", from_minutes(comps["pay_only_bump_mins"])),
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
            "+ PAY ONLY (main pay rows like RRPY, partial 10:30 days not baked into credit) "
            "+ ADDTL PAY ONLY (0:13/3:38/etc) "
            "+ G/SLIP / REROUTE / S-SLIP / PBS/PR / ASSIGN / RES ASSIGN-G/SLIP."
        )

st.caption("No data stored. All calculations are done locally in your browser session.")
