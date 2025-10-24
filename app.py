import re
from typing import Dict, Any, List, Tuple
import streamlit as st
import pandas as pd

# ======================================================
# Utilities
# ======================================================

def to_minutes(s: str) -> int:
    if not isinstance(s, str):
        return 0
    m = re.match(r"^(\d{1,3}):([0-5]\d)$", s.strip())
    if not m:
        return 0
    return int(m.group(1)) * 60 + int(m.group(2))

def from_minutes(mins: int) -> str:
    mins = max(0, int(mins))
    h = mins // 60
    m = mins % 60
    return f"{h}:{m:02d}"

def clean(text: str) -> str:
    return (text or "").replace("\u00A0", " ")


# ======================================================
# Card type detection
# ======================================================

def detect_card_type(raw: str) -> str:
    """
    Decide RESERVE vs LINEHOLDER by looking at the actual duty rows,
    not the summary block.

    Logic:
    - If any line matches "<DD><MMM><spaces>RES<spaces>" → RESERVE
    - Else if any line matches "<DD><MMM><spaces>REG<spaces>" → LINEHOLDER
    - Else default RESERVE
    """

    t = clean(raw).upper()

    # look for actual duty rows like "05MAR   RES" or "01JUN REG"
    saw_res_row = re.search(r"\b\d{2}[A-Z]{3}\s+RES\b", t) is not None
    saw_reg_row = re.search(r"\b\d{2}[A-Z]{3}\s+REG\b", t) is not None

    if saw_res_row and not saw_reg_row:
        return "RESERVE"
    if saw_reg_row and not saw_res_row:
        return "LINEHOLDER"

    # tie-breaker:
    # if both somehow appear (mixed month with RES and REG days),
    # we'll call that RESERVE only if there's at least one RES row.
    if saw_res_row and saw_reg_row:
        return "RESERVE"

    return "RESERVE"



# ======================================================
# Shared text extractors
# ======================================================

def extract_named_bucket(raw: str, label_regexes: List[str]) -> int:
    """
    Get H:MM after labels like:
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
# Reserve-specific helpers
# ======================================================

def extract_sub_ttl_credit(raw: str) -> int:
    """
    SUB TTL CREDIT (reserve subtotal).
    We'll try "SUB TTL CREDIT" first, else pull the first '= H:MM' before the minus.
    """
    t = clean(raw)

    m = re.search(
        r"SUB\s+TTL\s+(?:CREDIT\s*)?[:=]?\s*([0-9]{1,3}:[0-5]\d)",
        t,
        flags=re.I,
    )
    if m:
        return to_minutes(m.group(1))

    # fallback: pattern like "... = 68:34 - 0:00 + 3:26 = 72:00"
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
    Any RES row where final time repeats (SKED TIME == PAY TIME etc).
    We don't care what the NBR is.
    """
    t = clean(raw)

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
    ADDTL PAY ONLY COLUMN for RES:
    Tail bump minutes if last < previous.
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
    For visibility: each RES row + what we counted.
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
# Lineholder-specific helpers
# ======================================================

def extract_ttl_credit_final(raw: str) -> int:
    """
    TTL CREDIT (lineholder):
    We want the LAST '= H:MM' from the guarantee math block, i.e. final credit.
    Example block:
      68:34 + 0:00 + 0:00 = 68:34 - 0:00 + 3:26 = 72:00
    TTL CREDIT = 72:00
    """
    t = clean(raw)

    # grab all '= H:MM' occurrences on that line and take the last one
    m = re.search(
        r"=\s*([0-9]{1,3}:[0-5]\d)[^\n]*=\s*([0-9]{1,3}:[0-5]\d)",
        t,
        flags=re.I,
    )
    if m:
        # second capture group is the final
        return to_minutes(m.group(2))

    # fallback: last H:MM in that math line
    m2 = re.search(
        r"(\d{1,3}:[0-5]\d)[^\n]*END OF DISPLAY",
        t,
        flags=re.I,
    )
    if m2:
        return to_minutes(m2.group(1))

    return 0


def parse_lineholder_rows(raw: str):
    """
    Break out all REG rows.
    We'll capture:
      - list of all times
      - whether row says TRANS
      - pairing NBR (to detect RRPY)
    """
    t = clean(raw)

    row_re = re.compile(
        r"(?P<date>\d{2}[A-Z]{3})\s+REG\s+(?P<nbr>[A-Z0-9/]+)(?P<rest>.*?)(?=\n\d{2}[A-Z]{3}\s+REG|\n\s*\n|END OF DISPLAY|CREDIT\s+APPLICABLE|$)",
        flags=re.I | re.S,
    )

    rows = []
    for m in row_re.finditer(t):
        date = m.group("date").upper()
        nbr = m.group("nbr").upper()
        seg = m.group(0)

        times = re.findall(r"\b\d{1,3}:[0-5]\d\b", seg)
        has_trans = "TRANS" in seg.upper()

        rows.append({
            "date": date,
            "nbr": nbr,
            "times": times,
            "has_trans": has_trans,
            "raw": seg.strip(),
        })

    return rows


def calc_pay_time_only_lineholder(rows: List[Dict[str, Any]]) -> int:
    """
    PAY TIME ONLY (PAY NO CREDIT) for lineholder:
    - If NBR contains RRPY and there's a single duty value like "3:09", "5:26" => include it.
    - If row has 3+ times and also has an extra tail bump (ex: 1:35 10:30 10:30 3:23),
      we include the second-to-last time as guarantee pay (10:30 in example).
      We do NOT require TRANS = False here for that pattern if it's clearly that structure.
      We DO ignore pure pairing-credit block rows like 10:30 10:30 10:30 (no tail).
    """
    total = 0

    for r in rows:
        times = r["times"]
        nbr = r["nbr"]

        # RRPY rule
        if "RRPY" in nbr and times:
            # take the last time (this covers 3:09, 5:26)
            total += to_minutes(times[-1])
            continue

        # bump-tail rule like 28JUN:
        # pattern: [..., X, X, Y] where Y < X
        # we add that X (the second-to-last)
        if len(times) >= 3:
            prev_last = to_minutes(times[-2])
            last = to_minutes(times[-1])
            if last < prev_last:
                total += prev_last
                continue

    return total


def calc_addtl_pay_only_lineholder(rows: List[Dict[str, Any]]) -> int:
    """
    ADDTL PAY ONLY COLUMN for lineholder:
    - Look for tail bump minutes on REG rows:
      if last < second-to-last, add last.
    - This includes TRANS rows too.
    Example:
      01JUN ... 10:49 0:13  -> +0:13
      23JUN ... 15:45 15:45 15:45 0:38 -> +0:38
      26JUN ... 10:30 10:30 10:30 3:38 -> +3:38
      28JUN ... 10:30 10:30 3:23 -> +3:23
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
    Show how we decided for each REG row.
    """
    out = []
    for r in rows:
        times = r["times"]
        pay_only_add = 0
        addtl_only_add = 0

        # PAY TIME ONLY logic recap
        if "RRPY" in r["nbr"] and times:
            pay_only_add = to_minutes(times[-1])
        elif len(times) >= 3:
            prev_last = to_minutes(times[-2])
            last = to_minutes(times[-1])
            if last < prev_last:
                pay_only_add = prev_last

        # ADDTL PAY ONLY logic recap
        if len(times) >= 2:
            prev_last = to_minutes(times[-2])
            last = to_minutes(times[-1])
            if last < prev_last:
                addtl_only_add = last

        out.append({
            "Date": r["date"],
            "NBR": r["nbr"],
            "TRANS row?": "Y" if r["has_trans"] else "N",
            "All Times": ", ".join(times),
            "Counted PAY TIME ONLY": from_minutes(pay_only_add) if pay_only_add else "",
            "Counted ADDTL PAY ONLY": from_minutes(addtl_only_add) if addtl_only_add else "",
            "Raw Snip": r["raw"][:200],
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
st.caption("Detects RES vs REG, applies the right rules for that type.")

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

 G/SLIP PAY : 10:30 ASSIGN PAY: 0:00
 REROUTE PAY: 0:00

 END OF DISPLAY
        """.strip()

# init session state
if "timecard_text" not in st.session_state:
    st.session_state["timecard_text"] = ""
if "calc" not in st.session_state:
    st.session_state["calc"] = False

# input
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
