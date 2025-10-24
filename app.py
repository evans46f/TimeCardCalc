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
    """Normalize NBSPs and weird whitespace."""
    return (text or "").replace("\u00A0", " ")

# ======================================================
# Summary line parsing
# ======================================================

def grab_sub_ttl_credit_minutes(raw: str) -> Tuple[int, str]:
    """
    Get the final period credit.
    We take the LAST '= H:MM' on any math line, e.g.
      17:51 + 39:43 + 0:00 = 57:34 - 0:00 + 0:00 = 57:34
      68:34 + 0:00 + 0:00 = 68:34 - 0:00 + 3:26 = 72:00
    -> 57:34 or 72:00.
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

def extract_numeric_field(raw: str, left_label_regex: str) -> int:
    """
    Extract a single H:MM for a 'bank award' style field.

    We scan for:
      BANK DEP ... AWARD ... H:MM
      TTL BANK ... OPTS AWARD ... H:MM

    This is loose: it finds the first H:MM within ~40 chars after the label.
    """
    t = nbps(raw)
    pat = re.compile(
        left_label_regex + r".{0,40}?(\d{1,3}:[0-5]\d)",
        flags=re.I | re.S,
    )
    m = pat.search(t)
    if m:
        return to_minutes(m.group(1))
    return 0

# ======================================================
# Named pay bucket parsing
# ======================================================

def _label_to_regex(lbl: str) -> str:
    """
    Turn 'G/SLIP PAY' into flexible 'G/SLIP\s+PAY'.
    We escape each token and re-join with \s+ for loose spacing.
    """
    parts = re.split(r"\s+", lbl.strip())
    esc = [re.escape(p) for p in parts]
    return r"\s+".join(esc)

def extract_named_bucket(text: str, labels: List[str]) -> int:
    """
    Extract values like:
      G/SLIP PAY : 10:30
      ASSIGN PAY: 0:00
      REROUTE PAY: 1:22
    """
    t = nbps(text)
    for lbl in labels:
        pattern = _label_to_regex(lbl) + r"\s*:\s*(\d{1,3}:[0-5]\d)"
        m = re.search(pattern, t, flags=re.I)
        if m:
            return to_minutes(m.group(1))
    return 0

def extract_res_assign_gslip_bucket(text: str) -> int:
    """
    Parse 'RES ASSIGN-G/SLIP PAY:' (space or hyphen flexible).
    """
    t = nbps(text)
    m = re.search(
        r"RES\s+ASSIGN[-\s]+G/\s*SLIP\s+PAY\s*:\s*(\d{1,3}:[0-5]\d)",
        t,
        flags=re.I,
    )
    if m:
        return to_minutes(m.group(1))
    return 0

# ======================================================
# Duty row parsing logic
# ======================================================

RES_FALLBACK_ELIGIBLE_CODES = {
    # reserve "coverage"/standby-style buckets that should stack on top
    "SCC",
    "PVEL",
    "LOSA",
    "ADJ-RRPY",
    "ADJ",       # safety net: if ADJ shows without -RRPY
    "ADJ/RRPY",  # being extra safe about formatting variants
}

def parse_duty_rows(raw: str) -> List[Dict[str, Any]]:
    """
    Parse each duty day row and identify:
    - has_credit_block:
        a clear pairing-style credit block, e.g. '10:30 10:30 10:30 ...'
        or '7:21 7:21 7:21 ...'
        That time is already baked into SUB TTL CREDIT.
    - bump_pay_candidate:
        the little add-on, like 0:07 / 1:22 / 0:13 / 3:23 / 3:38.
        We detect it if the final time < the previous time.
        This feeds ADDTL PAY ONLY COLUMN.
    - main_pay_candidate:
        PAY TIME ONLY (PAY NO CREDIT).
        Rules:
          • If nbr has 'RRPY' in it (RRPY, ADJ-RRPY, etc.), take the last time.
          • If row ends with [big_time, then smaller bump], we can take big_time
            UNLESS that big_time is:
              - part of a 3x repeated credit block, OR
              - on a TRANS row (we don't bill TRANS base time).
          • Reserve fallback:
              If duty == RES, no credit block, and nbr is in RES_FALLBACK_ELIGIBLE_CODES
              (SCC, PVEL, LOSA, ADJ-RRPY...), take the last time.
              This prevents us from accidentally counting normal pairing credit like 10:30 10:30.
    """
    t = nbps(raw)

    seg_re = re.compile(
        r"(?P<date>\d{2}[A-Z]{3})\s+"
        r"(?P<duty>(RES|REG))\s+"
        r"(?P<nbr>[A-Z0-9/-]+)"
        r"(?P<tail>.*?)(?="
            r"\d{2}[A-Z]{3}\s+(RES|REG)\b|"
            r"RES\s+OTHER\s+SUB\s+TTL|"
            r"CREDIT\s+APPLICABLE|"
            r"END OF DISPLAY|$"
        ")",
        re.I | re.S,
    )

    rows: List[Dict[str, Any]] = []

    for m in seg_re.finditer(t):
        date = (m.group("date") or "").upper()
        duty = (m.group("duty") or "").upper()
        nbr  = (m.group("nbr") or "").upper()
        seg_full  = (m.group(0) or "")
        tail_text = (m.group("tail") or "")

        # all H:MM tokens on this row
        times = re.findall(r"\b\d{1,3}:[0-5]\d\b", seg_full)

        # does row mention TRANS?
        has_trans = "TRANS" in tail_text.upper()

        # detect credit block: same time repeated >=3 times before a bump
        has_credit_block = False
        repeated_credit_value = None
        if len(times) >= 4:
            before_last = times[:-1]
            last3 = before_last[-3:]
            if len(last3) == 3 and len(set(last3)) == 1:
                has_credit_block = True
                repeated_credit_value = last3[0]

        # bump pay candidate (ADDTL PAY ONLY COLUMN)
        bump_pay_candidate = None
        if len(times) >= 2:
            prev_time = times[-2]
            last_time = times[-1]
            if to_minutes(last_time) < to_minutes(prev_time):
                bump_pay_candidate = last_time  # 0:07, 1:22, 0:13, 3:23, etc.

        # main pay candidate (PAY TIME ONLY / PAY NO CREDIT)
        main_pay_candidate = None

        # Case 1: 'RRPY' in pairing code means it's standalone pay (ADJ-RRPY, RRPY)
        if "RRPY" in nbr:
            if times:
                main_pay_candidate = times[-1]

        # Case 2: row ends [big_time, smaller bump]
        # e.g. "10:30 10:30 3:23", "10:49 0:13"
        elif len(times) >= 2:
            prev_time = times[-2]
            last_time = times[-1]
            prev_m = to_minutes(prev_time)
            last_m = to_minutes(last_time)

            if last_m < prev_m:
                # Include prev_time unless it's TRANS or clearly triple-credit.
                occurrences_prev_before_last = [t for t in times[:-1] if t == prev_time]
                repeated_block = (
                    has_credit_block
                    and repeated_credit_value == prev_time
                    and len(occurrences_prev_before_last) >= 3
                )

                if (not has_trans) and (not repeated_block):
                    main_pay_candidate = prev_time

        # Case 3 (reserve fallback logic):
        # Only for RES rows:
        # - no credit block
        # - nbr looks like SCC / PVEL / LOSA / ADJ-RRPY etc.
        # - take the last time token
        #
        # This keeps SCC 1:00, PVEL 10:00, LOSA 10:00,
        # but *doesn't* grab generic pairing codes like 0991 10:30 10:30.
        if main_pay_candidate is None:
            if duty == "RES" and times and not has_credit_block:
                if nbr in RES_FALLBACK_ELIGIBLE_CODES:
                    main_pay_candidate = times[-1]

        rows.append({
            "date": date,
            "duty": duty,
            "nbr": nbr,
            "times": times,
            "has_credit_block": has_credit_block,
            "has_trans": has_trans,
            "main_pay_candidate": main_pay_candidate,
            "bump_pay_candidate": bump_pay_candidate,
            "raw": seg_full.strip(),
        })

    return rows

def detect_card_type(rows: List[Dict[str, Any]]) -> str:
    """
    Decide card type (affects which buckets get summed):
      - If we saw any REG rows and NO RES rows → LINEHOLDER
      - Otherwise → RESERVE
    """
    saw_res = any(r["duty"] == "RES" for r in rows)
    saw_reg = any(r["duty"] == "REG" for r in rows)
    if saw_reg and not saw_res:
        return "LINEHOLDER"
    return "RESERVE"

# ======================================================
# Component calculation
# ======================================================

def compute_components(raw: str) -> Dict[str, Any]:
    """
    Gather all buckets needed by either card type.
    """
    rows = parse_duty_rows(raw)
    card_type = detect_card_type(rows)

    sub_ttl_credit_mins, sub_src = grab_sub_ttl_credit_minutes(raw)

    # PAY TIME ONLY (PAY NO CREDIT)
    pay_only_main_mins = 0
    # ADDTL PAY ONLY COLUMN
    pay_only_bump_mins = 0

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
            "Added to PAY TIME ONLY": from_minutes(add_main) if add_main else "",
            "Added to ADDTL PAY ONLY": from_minutes(add_bump) if add_bump else "",
            "Raw Row Snippet": r["raw"][:200],
        })

    # Named/bottom buckets
    reroute_pay_mins      = extract_named_bucket(raw, ["REROUTE PAY"])
    assign_pay_mins       = extract_named_bucket(raw, ["ASSIGN PAY"])
    g_slip_pay_mins       = extract_named_bucket(raw, ["G/SLIP PAY", "G SLIP PAY", "G - SLIP PAY"])
    res_assign_gslip_mins = extract_res_assign_gslip_bucket(raw)

    # Bank awards
    bank_dep_award_mins       = extract_numeric_field(raw, r"BANK\s+DEP\s+AWARD")
    ttl_bank_opts_award_mins  = extract_numeric_field(raw, r"TTL\s+BANK\s+OPTS?\s+AWARD")

    return {
        "card_type": card_type,

        "sub_ttl_credit_mins": sub_ttl_credit_mins,
        "sub_ttl_src": sub_src,

        "pay_only_main_mins": pay_only_main_mins,     # PAY TIME ONLY
        "pay_only_bump_mins": pay_only_bump_mins,     # ADDTL PAY ONLY COLUMN

        "reroute_pay_mins": reroute_pay_mins,
        "assign_pay_mins": assign_pay_mins,
        "g_slip_pay_mins": g_slip_pay_mins,
        "res_assign_gslip_mins": res_assign_gslip_mins,

        "bank_dep_award_mins": bank_dep_award_mins,
        "ttl_bank_opts_award_mins": ttl_bank_opts_award_mins,

        "debug_rows": debug_rows,
    }

# ======================================================
# Totals logic (branch: RESERVE vs LINEHOLDER)
# ======================================================

def compute_totals(raw: str) -> Dict[str, Any]:
    comps = compute_components(raw)
    ct = comps["card_type"]

    # Common buckets to both:
    base_mins = (
        comps["sub_ttl_credit_mins"]
        + comps["pay_only_main_mins"]
        + comps["pay_only_bump_mins"]
        + comps["reroute_pay_mins"]
        + comps["assign_pay_mins"]
        + comps["bank_dep_award_mins"]
        + comps["ttl_bank_opts_award_mins"]
    )

    if ct == "RESERVE":
        total_mins = (
            base_mins
            + comps["res_assign_gslip_mins"]
            # reserve DOES NOT add G/SLIP PAY
        )
    else:
        total_mins = (
            base_mins
            + comps["g_slip_pay_mins"]
            # lineholder DOES NOT add RES ASSIGN-G/SLIP PAY
        )

    comps["total_mins"] = total_mins
    comps["total_hmm"] = from_minutes(total_mins)
    comps["total_decimal"] = round(total_mins / 60.0, 2)

    return comps

# ======================================================
# Streamlit UI
# ======================================================

st.set_page_config(page_title="Timecard Pay Calculator", layout="wide")

st.title("🧮 Timecard Pay Calculator")
st.caption("Auto-detects RESERVE vs LINEHOLDER. Applies the right contract math.")

with st.sidebar:
    st.header("Input")
    uploaded = st.file_uploader("Upload timecard text (.txt)", type=["txt"])

    example_btn_res = st.button("Load Reserve Example")
    example_btn_line = st.button("Load Lineholder Example")

default_text = ""
if example_btn_res:
    # Reserve example (John Oct). Expected ~103:56 after this fix.
    default_text = (
        "MONTHLY TIME DATA 10/24/25 10:16:32 "
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
        "BANK DEP AWARD 0:00 TTL BANK OPTS AWARD 0:00 "
        "G/SLIP PAY : 0:00 ASSIGN PAY: 0:00 RES ASSIGN-G/SLIP PAY: 10:30 "
        "REROUTE PAY: 10:30 END OF DISPLAY"
    )

if example_btn_line:
    # Lineholder example (Christophe June). Expected ~109:27.
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
        "BANK DEP AWARD 0:00 TTL BANK OPTS AWARD 3:26 "
        "G/SLIP PAY : 10:30 REROUTE PAY: 0:00 ASSIGN PAY: 0:00 "
        "RES ASSIGN-G/SLIP PAY: 0:00 END OF DISPLAY"
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
    c3.metric("Card Type", comps["card_type"])
    c4.metric("SUB TTL Source", comps["sub_ttl_src"])

    breakdown_rows = [
        ("SUB TTL CREDIT", from_minutes(comps["sub_ttl_credit_mins"])),
        ("PAY TIME ONLY (PAY NO CREDIT)", from_minutes(comps["pay_only_main_mins"])),
        ("ADDTL PAY ONLY COLUMN", from_minutes(comps["pay_only_bump_mins"])),
        ("REROUTE PAY", from_minutes(comps["reroute_pay_mins"])),
        ("ASSIGN PAY", from_minutes(comps["assign_pay_mins"])),
        ("RES ASSIGN-G/SLIP PAY", from_minutes(comps["res_assign_gslip_mins"])),
        ("G/SLIP PAY", from_minutes(comps["g_slip_pay_mins"])),
        ("BANK DEP AWARD", from_minutes(comps["bank_dep_award_mins"])),
        ("TTL BANK OPTS AWARD", from_minutes(comps["ttl_bank_opts_award_mins"])),
        ("TOTAL (Applied Formula Above)", comps["total_hmm"]),
    ]
    df = pd.DataFrame(breakdown_rows, columns=["Component", "Time"])
    st.table(df)

    with st.expander("Row Debug (what each duty day contributed)"):
        dbg = pd.DataFrame(comps["debug_rows"])
        st.dataframe(dbg, use_container_width=True)
        st.caption(
            "Reserve total = SUB TTL CREDIT + PAY TIME ONLY + ADDTL PAY ONLY + "
            "REROUTE PAY + ASSIGN PAY + RES ASSIGN-G/SLIP PAY + BANK DEP AWARD + TTL BANK OPTS AWARD. "
            "Lineholder total = SUB TTL CREDIT + PAY TIME ONLY + ADDTL PAY ONLY + "
            "REROUTE PAY + ASSIGN PAY + G/SLIP PAY + BANK DEP AWARD + TTL BANK OPTS AWARD."
        )

st.caption("All math runs locally. No data stored.")
