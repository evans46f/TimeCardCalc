import re
from typing import Dict, Any, List
import streamlit as st
import pandas as pd

# ======================================================
# Utilities
# ======================================================

def to_minutes(s: str) -> int:
    """H:MM -> total mins. Bad/blank -> 0."""
    if not isinstance(s, str):
        return 0
    m = re.match(r"^(\d{1,3}):([0-5]\d)$", s.strip())
    if not m:
        return 0
    return int(m.group(1)) * 60 + int(m.group(2))

def from_minutes(mins: int) -> str:
    """total mins -> H:MM."""
    mins = max(0, int(mins))
    h = mins // 60
    m = mins % 60
    return f"{h}:{m:02d}"

def clean(text: str) -> str:
    """Normalize whitespace / nbsp."""
    return (text or "").replace("\u00A0", " ")


# ======================================================
# Card type detection
# ======================================================

def detect_card_type(raw: str) -> str:
    """
    Decide RESERVE vs LINEHOLDER by looking at actual duty rows,
    not summary text.

    Logic:
    - If we see any row like "05MAR   RES ..."
      (DDMMM then RES) => RESERVE
    - Else if we see any row like "01JUN REG ..."
      => LINEHOLDER
    - If mixed, call it RESERVE.
    - Else default RESERVE.
    """
    t = clean(raw).upper()

    saw_res_row = re.search(r"\b\d{2}[A-Z]{3}\s+RES\b", t) is not None
    saw_reg_row = re.search(r"\b\d{2}[A-Z]{3}\s+REG\b", t) is not None

    if saw_res_row and not saw_reg_row:
        return "RESERVE"
    if saw_reg_row and not saw_res_row:
        return "LINEHOLDER"
    if saw_res_row and saw_reg_row:
        return "RESERVE"

    return "RESERVE"


# ======================================================
# Shared extractors (summary blocks, etc)
# ======================================================

def extract_named_bucket(raw: str, label_regexes: List[str]) -> int:
    """
    Grab H:MM after things like:
      REROUTE PAY:
      ASSIGN PAY:
      G/SLIP PAY :
      RES ASSIGN-G/SLIP PAY:
      BANK DEP AWARD
      TTL BANK OPTS AWARD
    """
    t = clean(raw)
    for lbl in label_regexes:
        pat = re.compile(
            lbl + r"\s*:?\s*([0-9]{1,3}:[0-5]\d)",
            flags=re.I,
        )
        m = pat.search(t)
        if m:
            return to_minutes(m.group(1))
    return 0

def extract_reroute_pay(raw: str) -> int:
    return extract_named_bucket(raw, [r"REROUTE\s+PAY"])

def extract_assign_pay(raw: str) -> int:
    return extract_named_bucket(raw, [r"ASSIGN\s+PAY"])

def extract_res_assign_gslip_pay(raw: str) -> int:
    return extract_named_bucket(raw, [r"RES\s+ASSIGN[-]?\s*G/\s*SLIP\s+PAY"])

def extract_gslip_pay(raw: str) -> int:
    return extract_named_bucket(raw, [r"G/\s*SLIP\s+PAY"])

def extract_bank_dep_award(raw: str) -> int:
    return extract_named_bucket(raw, [r"BANK\s+DEP\s+AWARD"])

def extract_ttl_bank_opts_award(raw: str) -> int:
    return extract_named_bucket(raw, [r"TTL\s+BANK\s+OPTS\s+AWARD"])


# ======================================================
# RESERVE HELPERS
# ======================================================

def extract_sub_ttl_credit(raw: str) -> int:
    """
    SUB TTL CREDIT for reserve.
    Example reserve block:
      ... SUB TTL CREDIT ... 56:20 ...
      10:30 + 45:50 + 0:00 = 56:20 - 0:00 + 0:00 = 56:20
    We want the first '= H:MM' (56:20 in example).
    """
    t = clean(raw)

    # direct "SUB TTL CREDIT"
    m = re.search(
        r"SUB\s+TTL\s+(?:CREDIT\s*)?[:=]?\s*([0-9]{1,3}:[0-5]\d)",
        t,
        flags=re.I,
    )
    if m:
        return to_minutes(m.group(1))

    # fallback: first '= H:MM' before the '-'
    m2 = re.search(
        r"=\s*([0-9]{1,3}:[0-5]\d)\s*-\s*[0-9]{1,3}:[0-5]\d\s*\+\s*[0-9]{1,3}:[0-5]\d\s*=\s*[0-9]{1,3}:[0-5]\d",
        t,
        flags=re.I,
    )
    if m2:
        return to_minutes(m2.group(1))

    return 0


def extract_pay_time_only_minutes_list_reserve(raw: str) -> List[int]:
    """
    PAY TIME ONLY (PAY NO CREDIT) for RES:
    - For each RES duty row, if the last time repeats somewhere else
      on that same row (SKED==PAY or PAY==CREDIT style),
      count that last time.
    """
    t = clean(raw)

    # match each RES row (date + RES + NBR ...)
    row_re = re.compile(
        r"(?P<date>\d{2}[A-Z]{3})\s+RES\s+(?P<nbr>[A-Z0-9/]+).*?(?=\n\d{2}[A-Z]{3}\s+RES|\n\s*\n|END OF DISPLAY|CREDIT\s+GUAR|$)",
        flags=re.I | re.S,
    )

    chunks = []
    for m in row_re.finditer(t):
        seg = m.group(0)
        times = re.findall(r"\b\d{1,3}:[0-5]\d\b", seg)
        if not times:
            continue
        last_val = times[-1]
        if times.count(last_val) >= 2:
            chunks.append(to_minutes(last_val))

    return chunks


def extract_addtl_pay_only_tail_reserve(raw: str) -> int:
    """
    ADDTL PAY ONLY COLUMN (reserve):
    - If last < previous, count last (tail bump).
    """
    t = clean(raw)

    row_re = re.compile(
        r"(?P<date>\d{2}[A-Z]{3})\s+RES\s+(?P<nbr>[A-Z0-9/]+).*?(?=\n\d{2}[A-Z]{3}\s+RES|\n\s*\n|END OF DISPLAY|CREDIT\s+GUAR|$)",
        flags=re.I | re.S,
    )

    bump_total = 0
    for m in row_re.finditer(t):
        seg = m.group(0)
        times = re.findall(r"\b\d{1,3}:[0-5]\d\b", seg)
        if len(times) >= 2:
            prev_last = to_minutes(times[-2])
            last = to_minutes(times[-1])
            if last < prev_last:
                bump_total += last

    return bump_total


def build_reserve_debug_rows(raw: str) -> List[Dict[str, Any]]:
    """
    Transparency per RES row.
    """
    t = clean(raw)

    row_re = re.compile(
        r"(?P<date>\d{2}[A-Z]{3})\s+RES\s+(?P<nbr>[A-Z0-9/]+)(?P<rest>.*?)(?=\n\d{2}[A-Z]{3}\s+RES|\n\s*\n|END OF DISPLAY|CREDIT\s+GUAR|$)",
        flags=re.I | re.S,
    )

    out = []
    for m in row_re.finditer(t):
        date = m.group("date").upper()
        nbr = m.group("nbr").upper()
        seg = m.group(0)
        times = re.findall(r"\b\d{1,3}:[0-5]\d\b", seg)

        pay_only_add = 0
        addtl_bump_add = 0

        if times:
            last_val = times[-1]
            if times.count(last_val) >= 2:
                pay_only_add = to_minutes(last_val)

        if len(times) >= 2:
            prev_last = to_minutes(times[-2])
            last = to_minutes(times[-1])
            if last < prev_last:
                addtl_bump_add = last

        out.append({
            "Date": date,
            "NBR": nbr,
            "All Times": ", ".join(times),
            "Counted PAY TIME ONLY": from_minutes(pay_only_add) if pay_only_add else "",
            "Counted ADDTL PAY ONLY": from_minutes(addtl_bump_add) if addtl_bump_add else "",
            "Raw Snip": seg.strip()[:200],
        })

    return out


def compute_reserve_totals(raw: str) -> Dict[str, Any]:
    """
    Reserve total = sum of:
      SUB TTL CREDIT
      PAY TIME ONLY (PAY NO CREDIT)
      ADDTL PAY ONLY COLUMN
      REROUTE PAY
      ASSIGN PAY
      RES ASSIGN-G/SLIP PAY
      BANK DEP AWARD
      TTL BANK OPTS AWARD
    """
    sub_ttl_credit_mins = extract_sub_ttl_credit(raw)
    pay_time_only_mins = sum(extract_pay_time_only_minutes_list_reserve(raw))
    addtl_pay_only_mins = extract_addtl_pay_only_tail_reserve(raw)

    reroute_pay_mins = extract_reroute_pay(raw)
    assign_pay_mins = extract_assign_pay(raw)
    res_assign_gslip_mins = extract_res_assign_gslip_pay(raw)
    bank_dep_award_mins = extract_bank_dep_award(raw)
    ttl_bank_opts_award_mins = extract_ttl_bank_opts_award(raw)

    total_mins = (
        sub_ttl_credit_mins
        + pay_time_only_mins
        + addtl_pay_only_mins
        + reroute_pay_mins
        + assign_pay_mins
        + res_assign_gslip_mins
        + bank_dep_award_mins
        + ttl_bank_opts_award_mins
    )

    breakdown = [
        ("SUB TTL CREDIT", from_minutes(sub_ttl_credit_mins)),
        ("PAY TIME ONLY (PAY NO CREDIT)", from_minutes(pay_time_only_mins)),
        ("ADDTL PAY ONLY COLUMN", from_minutes(addtl_pay_only_mins)),
        ("REROUTE PAY", from_minutes(reroute_pay_mins)),
        ("ASSIGN PAY", from_minutes(assign_pay_mins)),
        ("RES ASSIGN-G/SLIP PAY", from_minutes(res_assign_gslip_mins)),
        ("BANK DEP AWARD", from_minutes(bank_dep_award_mins)),
        ("TTL BANK OPTS AWARD", from_minutes(ttl_bank_opts_award_mins)),
        ("TOTAL", from_minutes(total_mins)),
    ]

    debug_rows = build_reserve_debug_rows(raw)

    return {
        "card_type": "RESERVE",
        "total_mins": total_mins,
        "total_hmm": from_minutes(total_mins),
        "total_decimal": round(total_mins / 60.0, 2),
        "breakdown_rows": breakdown,
        "debug_rows": debug_rows,
    }


# ======================================================
# LINEHOLDER HELPERS
# ======================================================

def extract_ttl_credit_final(raw: str) -> int:
    """
    TTL CREDIT for lineholder.
    We want final total credit after bank/opts fill.
    Example:
      68:34 - 0:00 + 3:26 = 72:00
    -> 72:00
    We'll take the LAST '= H:MM' group on that guarantee math line.
    """
    t = clean(raw)

    # pull all '= H:MM', take the last
    eq_times = re.findall(r"=\s*([0-9]{1,3}:[0-5]\d)", t)
    if eq_times:
        return to_minutes(eq_times[-1])

    return 0


def parse_lineholder_rows(raw: str) -> List[Dict[str, Any]]:
    """
    Parse each REG duty line individually.
    We do NOT try to consume multi-line blocks; we just read each line.
    """
    rows = []
    for line in clean(raw).splitlines():
        line_up = line.upper().strip()

        # must start with "DDMMM REG ..."
        m = re.match(r"^(\d{2}[A-Z]{3})\s+REG\s+(\S+)\s+(.*)$", line_up)
        if not m:
            continue

        date = m.group(1)
        nbr = m.group(2)
        rest = m.group(3)

        times = re.findall(r"\b\d{1,3}:[0-5]\d\b", line_up)
        has_trans = "TRANS" in line_up

        rows.append({
            "date": date,
            "nbr": nbr,
            "times": times,
            "has_trans": has_trans,
            "raw": line.strip(),
        })

    return rows


def calc_pay_time_only_lineholder(rows: List[Dict[str, Any]]) -> int:
    """
    PAY TIME ONLY (PAY NO CREDIT) for lineholder:
    - RRPY rows: add the time (ex: 3:09, 5:26)
    - Non-TRANS rows with a bump tail:
        e.g. "1:35 10:30 10:30 3:23"
        last < prev_last, so add prev_last (10:30)
    - We do NOT count TRANS rows in this bucket.
      (01JUN has TRANS so 10:49 doesn't count here)
    """
    total = 0

    for r in rows:
        times = r["times"]

        # RRPY rule
        if "RRPY" in r["nbr"] and times:
            total += to_minutes(times[-1])
            continue

        # bump-tail rule (non-TRANS only)
        if (not r["has_trans"]) and len(times) >= 3:
            prev_last = to_minutes(times[-2])
            last = to_minutes(times[-1])
            if last < prev_last:
                total += prev_last
                continue

    return total


def calc_addtl_pay_only_lineholder(rows: List[Dict[str, Any]]) -> int:
    """
    ADDTL PAY ONLY COLUMN for lineholder:
    - If last < prev_last, add last.
    - This DOES include TRANS rows.
      Examples that count: 0:13, 0:38, 3:38, 3:23
    """
    total = 0
    for r in rows:
        times = r["times"]
        if len(times) >= 2:
            prev_last = to_minutes(times[-2])
            last = to_minutes(times[-1])
            if last < prev_last:
                total += last
    return total


def build_lineholder_debug_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Show how each REG row contributed.
    """
    out = []
    for r in rows:
        times = r["times"]

        # what we counted into PAY TIME ONLY
        pay_only_add = ""
        if "RRPY" in r["nbr"] and times:
            pay_only_add = from_minutes(to_minutes(times[-1]))
        elif (not r["has_trans"]) and len(times) >= 3:
            prev_last = to_minutes(times[-2])
            last = to_minutes(times[-1])
            if last < prev_last:
                pay_only_add = from_minutes(prev_last)

        # what we counted into ADDTL PAY ONLY
        addtl_only_add = ""
        if len(times) >= 2:
            prev_last = to_minutes(times[-2])
            last = to_minutes(times[-1])
            if last < prev_last:
                addtl_only_add = from_minutes(last)

        out.append({
            "Date": r["date"],
            "NBR": r["nbr"],
            "TRANS row?": "Y" if r["has_trans"] else "N",
            "All Times": ", ".join(times),
            "Counted PAY TIME ONLY": pay_only_add,
            "Counted ADDTL PAY ONLY": addtl_only_add,
            "Raw Line": r["raw"][:200],
        })

    return out


def compute_lineholder_totals(raw: str) -> Dict[str, Any]:
    """
    Lineholder total = sum of:
      TTL CREDIT
      PAY TIME ONLY (PAY NO CREDIT)
      ADDTL PAY ONLY COLUMN
      REROUTE PAY
      ASSIGN PAY
      G/SLIP PAY
    """
    rows = parse_lineholder_rows(raw)

    ttl_credit_mins = extract_ttl_credit_final(raw)
    pay_time_only_mins = calc_pay_time_only_lineholder(rows)
    addtl_pay_only_mins = calc_addtl_pay_only_lineholder(rows)

    reroute_pay_mins = extract_reroute_pay(raw)
    assign_pay_mins = extract_assign_pay(raw)
    gslip_pay_mins = extract_gslip_pay(raw)

    total_mins = (
        ttl_credit_mins
        + pay_time_only_mins
        + addtl_pay_only_mins
        + reroute_pay_mins
        + assign_pay_mins
        + gslip_pay_mins
    )

    breakdown = [
        ("TTL CREDIT", from_minutes(ttl_credit_mins)),
        ("PAY TIME ONLY (PAY NO CREDIT)", from_minutes(pay_time_only_mins)),
        ("ADDTL PAY ONLY COLUMN", from_minutes(addtl_pay_only_mins)),
        ("REROUTE PAY", from_minutes(reroute_pay_mins)),
        ("ASSIGN PAY", from_minutes(assign_pay_mins)),
        ("G/SLIP PAY", from_minutes(gslip_pay_mins)),
        ("TOTAL", from_minutes(total_mins)),
    ]

    debug_rows = build_lineholder_debug_rows(rows)

    return {
        "card_type": "LINEHOLDER",
        "total_mins": total_mins,
        "total_hmm": from_minutes(total_mins),
        "total_decimal": round(total_mins / 60.0, 2),
        "breakdown_rows": breakdown,
        "debug_rows": debug_rows,
    }


# ======================================================
# Dispatcher
# ======================================================

def compute_totals(raw: str) -> Dict[str, Any]:
    card_type = detect_card_type(raw)
    if card_type == "RESERVE":
        return compute_reserve_totals(raw)
    else:
        return compute_lineholder_totals(raw)


# ======================================================
# Streamlit UI
# ======================================================

st.set_page_config(page_title="Timecard Pay Calculator", layout="wide")

st.title("🧮 Timecard Pay Calculator")
st.caption("Detects RES vs REG and applies the right rules for that type.")

def handle_clear():
    st.session_state["timecard_text"] = ""
    st.session_state["calc"] = False

with st.sidebar:
    st.header("Example Inputs")

    # Reserve example
    if st.button("Load Reserve Example"):
        st.session_state["timecard_text"] = """
MONTHLY TIME DATA           

 BID PERIOD:   02MAR25 - 31MAR25                    ATL 320 B     INIT LOT: 0509
 NAME: EVANS,JOHN                                   EMP NBR:0618143             

         DAY    ROT      BLOCK      SKED      PAY                PAY            
 DATE    DES    NBR       HRS       TIME      TIME    CREDIT     ONLY           

 05MAR   RES    4F1C                6:33      6:33
 05MAR   RES    4560      9:38     10:30     10:30     10:30
 12MAR   RES    SCC                 1:00      1:00
 21MAR   RES    0056      2:35      5:15      5:15
 25MAR   RES    LOSA               15:00     15:00

            RES      OTHER   SUB TTL   PAYBACK   BANK OPT 1    TTL   BANK OPT 1
 CREDIT     GUAR     GUAR    CREDIT    NEG BANK      AWD      CREDIT    LIMIT
  10:30 +  45:50 +   0:00 =  56:20  -   0:00   +    0:00  =  56:20     77:00

              BANK DEP      TTL BANK     G/SLIP      OUT
                AWARD      OPTS AWARD    CREDIT      BANK
                0:00           0:00        0:00     -  1:08

 G/SLIP PAY :   0:00      ASSIGN PAY:   0:00     RES ASSIGN-G/SLIP PAY:   5:15
 REROUTE PAY:   0:00                             RES LOOK BACK GUAR   :   0:00

 END OF DISPLAY
        """.strip()

    # Lineholder example
    if st.button("Load Lineholder Example"):
        st.session_state["timecard_text"] = """
MONTHLY TIME DATA           

 BID PERIOD: 02JUN25 - 01JUL25 ATL 73N B INIT LOT: 0059
 NAME: BOYES,CHRISTOPHE EMP NBR:0759386

         DAY ROT BLOCK SKED PAY PAY
 DATE DES NBR HRS TIME TIME CREDIT ONLY

 01JUN REG 3554 6:30 TRANS TRANS 10:49 0:13
 05JUN REG 3210 7:24 10:30 10:30 10:30
 09JUN REG 3191 6:52 10:30 10:30 10:30
 17JUN REG 0889 2:20 10:30 10:30 10:30
 18JUN REG RRPY 3:09
 23JUN REG C428 15:01 15:45 15:45 15:45 0:38
 26JUN REG 0608 5:16 10:30 10:30 10:30 3:38
 27JUN REG RRPY 5:26
 28JUN REG 0451 1:35 10:30 10:30 3:23

  68:34 + 0:00 + 0:00 = 68:34 - 0:00 + 3:26 = 72:00

 CREDIT APPLICABLE TO REG G/S SLIP PAY: 72:00 REG G/S TRIGGER: 72:00

 G/SLIP PAY : 10:30 ASSIGN PAY: 0:00
 REROUTE PAY: 0:00

 END OF DISPLAY
        """.strip()

# init session state
if "timecard_text" not in st.session_state:
    st.session_state["timecard_text"] = ""
if "calc" not in st.session_state:
    st.session_state["calc"] = False

# main input
st.text_area(
    "Paste your timecard text here:",
    key="timecard_text",
    height=260,
)

st.divider()
colA, colB = st.columns([1, 1])
calc_btn = colA.button("Calculate", type="primary")
clear_btn = colB.button("Clear", on_click=handle_clear)

if calc_btn:
    st.session_state["calc"] = True

if st.session_state["calc"]:
    result = compute_totals(st.session_state["timecard_text"])

    st.subheader("Results")

    c1, c2, c3 = st.columns(3)
    c1.metric("TOTAL PAY (H:MM)", result["total_hmm"])
    c2.metric("TOTAL PAY (Decimal)", f"{result['total_decimal']:.2f}")
    c3.metric("Card Type", result["card_type"])

    df = pd.DataFrame(result["breakdown_rows"], columns=["Component", "Time"])
    st.table(df)

    with st.expander("Row Debug (what each duty day contributed)"):
        dbg = pd.DataFrame(result["debug_rows"])
        st.dataframe(dbg, use_container_width=True)

st.caption("All math runs locally. No data stored.")
