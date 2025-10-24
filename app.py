import re
from typing import List, Dict, Any
import streamlit as st
import pandas as pd

# ======================================================
# Helpers
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
    h, m = divmod(mins, 60)
    return f"{h}:{m:02d}"

def clean(t: str) -> str:
    return (t or "").replace("\u00A0", " ")

# ======================================================
# Detect Card Type
# ======================================================

def detect_card_type(raw: str) -> str:
    """
    REG rows only -> LINEHOLDER
    RES rows present (or mixed) -> RESERVE
    default -> RESERVE
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
# Row Parsers
# ======================================================

def _row_regex(prefix: str) -> re.Pattern:
    """
    Build a parser for either RES or REG rows. We stop before:
    - next row of same type
    - summary blocks
    - END OF DISPLAY
    """
    return re.compile(
        rf"(?P<date>\d{{2}}[A-Z]{{3}})\s+{prefix}\s+(?P<nbr>[A-Z0-9/-]+)"
        rf"(?P<tail>.*?)(?="
        rf"\d{{2}}[A-Z]{{3}}\s+{prefix}\b|"
        r"RES\s+OTHER\s+SUB\s+TTL|"
        r"CREDIT\s+APPLICABLE|"
        r"END OF DISPLAY|$)",
        re.I | re.S,
    )

def parse_lineholder_rows(raw: str) -> List[Dict[str, Any]]:
    t = clean(raw)
    seg_re = _row_regex("REG")
    rows = []
    for m in seg_re.finditer(t):
        seg_full = m.group(0)
        times = re.findall(r"\b\d{1,3}:[0-5]\d\b", seg_full)
        rows.append({
            "date": (m.group("date") or "").upper(),
            "nbr": (m.group("nbr") or "").upper(),
            "times": times,
            "raw": seg_full.strip(),
        })
    return rows

def parse_reserve_rows(raw: str) -> List[Dict[str, Any]]:
    t = clean(raw)
    seg_re = _row_regex("RES")
    rows = []
    for m in seg_re.finditer(t):
        seg_full = m.group(0)
        times = re.findall(r"\b\d{1,3}:[0-5]\d\b", seg_full)
        rows.append({
            "date": (m.group("date") or "").upper(),
            "nbr": (m.group("nbr") or "").upper(),
            "times": times,
            "raw": seg_full.strip(),
        })
    return rows

# ======================================================
# Extractors
# ======================================================

def extract_named_bucket(text: str, labels: List[str]) -> int:
    """
    Looks for things like:
      G/SLIP PAY : 10:30
      ASSIGN PAY: 0:00
      RES ASSIGN-G/SLIP PAY: 5:15
      REROUTE PAY: 0:00
      BANK DEP AWARD 0:00
      TTL BANK OPTS AWARD 0:00
    """
    t = clean(text)
    for lbl in labels:
        # version with colon
        pat_colon = re.compile(
            re.escape(lbl) + r"\s*:\s*([0-9]{1,3}:[0-5][0-9])",
            flags=re.I,
        )
        m = pat_colon.search(t)
        if m:
            return to_minutes(m.group(1))

        # version without colon
        pat_nocolon = re.compile(
            re.escape(lbl) + r"\s+([0-9]{1,3}:[0-5][0-9])",
            flags=re.I,
        )
        m2 = pat_nocolon.search(t)
        if m2:
            return to_minutes(m2.group(1))

    return 0

def grab_sub_ttl_credit_minutes(raw: str) -> int:
    """
    We want the FINAL total credit from the guarantee math block.
    Examples:
      68:34 + 0:00 + 0:00 = 68:34 - 0:00 + 3:26 = 72:00  -> 72:00
      10:30 + 40:49 + 0:00 = 51:19 - 0:00 + 0:00 = 51:19 -> 51:19
    """
    t = clean(raw)
    eq_times = re.findall(r"=\s*([0-9]{1,3}:[0-5]\d)", t)
    if eq_times:
        return to_minutes(eq_times[-1])
    return 0

def extract_training_pay_minutes(raw: str) -> int:
    """
    Sum all 'DISTRIBUTED TRNG PAY:' lines.
    Example:
      ... DISTRIBUTED TRNG PAY:   1:52
    """
    t = clean(raw)
    total = 0
    for m in re.finditer(
        r"DISTRIBUTED\s+TRNG\s+PAY:\s+([0-9]{1,3}:[0-5][0-9])",
        t,
        flags=re.I,
    ):
        total += to_minutes(m.group(1))
    return total

# ======================================================
# Lineholder Logic
# ======================================================

def calc_pay_time_only_lineholder(rows: List[Dict[str, Any]]) -> int:
    """
    PAY TIME ONLY for lineholder = sum of rows that have exactly ONE time
    (e.g. 'REG RRPY 3:09', 'REG RRPY 5:26', single guarantee-only lines).
    """
    total = 0
    for r in rows:
        times = r["times"]
        if len(times) == 1:
            total += to_minutes(times[0])
    return total

def calc_addtl_pay_only_lineholder(rows: List[Dict[str, Any]]) -> int:
    """
    ADDTL PAY ONLY COLUMN for lineholder:
    If the last time is smaller than the one before it, add the last time.
    (captures tails like 0:13, 0:38, 3:38, 3:23...)
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

# ======================================================
# Reserve Logic
# ======================================================

def calc_pay_time_only_reserve(rows: List[Dict[str, Any]]) -> int:
    """
    PAY TIME ONLY (PAY NO CREDIT) for Reserve.

    Rule:
    - Include any RES row whose NBR is NOT:
        * SICK
        * a numeric pairing / trip code (e.g. '0097', '5501', etc.)
      These are pay-no-credit style days like SCC, LOSA, FOSP, PJRY,
      VAC, TRVL, PRPL, RRPY, etc.

    - For included rows, add the LAST time on that row.
    - Exclude actual pairings because those hours are already in SUB TTL CREDIT.
    """
    total = 0
    for r in rows:
        code = r["nbr"].upper()
        times = r["times"]
        if not times:
            continue

        # Skip SICK
        if code == "SICK":
            continue

        # Skip numeric / pairing-style codes (start with a digit)
        if re.match(r"^\d", code):
            continue

        # Everything else counts (SCC, FOSP, PJRY, LOSA, VAC, TRVL, PRPL, RRPY, etc.)
        total += to_minutes(times[-1])

    return total

# ======================================================
# Compute Totals
# ======================================================

def compute_totals(raw: str) -> Dict[str, Any]:
    card_type = detect_card_type(raw)

    if card_type == "LINEHOLDER":
        rows = parse_lineholder_rows(raw)

        ttl_credit_mins = grab_sub_ttl_credit_minutes(raw)
        pay_only_mins = calc_pay_time_only_lineholder(rows)
        addtl_only_mins = calc_addtl_pay_only_lineholder(rows)

        gslip_mins = extract_named_bucket(raw, ["G/SLIP PAY"])
        assign_mins = extract_named_bucket(raw, ["ASSIGN PAY"])

        gslip_twice_mins = 2 * gslip_mins
        assign_twice_mins = 2 * assign_mins

        total_mins = (
            ttl_credit_mins
            + pay_only_mins
            + addtl_only_mins
            + gslip_twice_mins
            + assign_twice_mins
        )

        return {
            "card_type": "LINEHOLDER",
            "TTL CREDIT": from_minutes(ttl_credit_mins),
            "PAY TIME ONLY (single-time rows only)": from_minutes(pay_only_mins),
            "ADDTL PAY ONLY COLUMN": from_minutes(addtl_only_mins),
            "G/SLIP PAY x2": from_minutes(gslip_twice_mins),
            "ASSIGN PAY x2": from_minutes(assign_twice_mins),
            "TOTAL": from_minutes(total_mins),
        }

    else:
        # RESERVE
        rows = parse_reserve_rows(raw)

        sub_ttl_mins = grab_sub_ttl_credit_minutes(raw)
        pay_time_mins = calc_pay_time_only_reserve(rows)
        addtl_only_mins = calc_addtl_pay_only_reserve(rows)

        reroute_mins = extract_named_bucket(raw, ["REROUTE PAY"])
        assign_mins = extract_named_bucket(raw, ["ASSIGN PAY"])
        res_assign_gslip_mins = extract_named_bucket(raw, ["RES ASSIGN-G/SLIP PAY"])
        bank_dep_mins = extract_named_bucket(raw, ["BANK DEP AWARD"])
        ttl_bank_mins = extract_named_bucket(raw, ["TTL BANK OPTS AWARD"])
        training_mins = extract_training_pay_minutes(raw)

        total_mins = (
            sub_ttl_mins
            + pay_time_mins
            + addtl_only_mins
            + reroute_mins
            + assign_mins
            + res_assign_gslip_mins
            + bank_dep_mins
            + ttl_bank_mins
            + training_mins
        )

        return {
            "card_type": "RESERVE",
            "SUB TTL CREDIT": from_minutes(sub_ttl_mins),
            "PAY TIME ONLY (PAY NO CREDIT)": from_minutes(pay_time_mins),
            "ADDTL PAY ONLY COLUMN": from_minutes(addtl_only_mins),
            "REROUTE PAY": from_minutes(reroute_mins),
            "ASSIGN PAY": from_minutes(assign_mins),
            "RES ASSIGN-G/SLIP PAY": from_minutes(res_assign_gslip_mins),
            "BANK DEP AWARD": from_minutes(bank_dep_mins),
            "TTL BANK OPTS AWARD": from_minutes(ttl_bank_mins),
            "DISTRIBUTED TRNG PAY": from_minutes(training_mins),
            "TOTAL": from_minutes(total_mins),
        }

# ======================================================
# Streamlit UI
# ======================================================

st.set_page_config(page_title="Timecard Pay Calculator", layout="wide")
st.title("🧮 Timecard Pay Calculator")
st.caption("Auto-detects RESERVE vs LINEHOLDER and applies the correct rules for that type.")

def handle_clear():
    st.session_state["timecard_text"] = ""
    st.session_state["calc"] = False

with st.sidebar:
    st.header("Examples")

    if st.button("Load Lineholder Example"):
        st.session_state["timecard_text"] = (
            "MONTHLY TIME DATA "
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
            "G/SLIP PAY : 10:30 ASSIGN PAY: 0:00 "
            "END OF DISPLAY"
        )

    if st.button("Load Reserve Example"):
        st.session_state["timecard_text"] = (
            "02DEC RES TRVL 2:00 2:00 "
            "03DEC RES PRPL 10:00 10:00 "
            "05DEC RES SCC 1:00 1:00 "
            "08DEC RES 0427 5:15 5:15 "
            "17DEC RES 5464 4:09 5:15 5:15 5:15 0:08 "
            "19DEC RES LOSA 15:00 15:00 "
            "29DEC RES 0961 2:56 5:15 5:15 5:15 "
            "31DEC RES SCC 1:00 1:00 "
            "10:30 + 40:49 + 0:00 = 51:19 - 0:00 + 0:00 = 51:19 "
            "RES ASSIGN-G/SLIP PAY: 5:15 "
            "DISTRIBUTED TRNG PAY: 1:52 "
            "END OF DISPLAY"
        )

# init session state
if "timecard_text" not in st.session_state:
    st.session_state["timecard_text"] = ""
if "calc" not in st.session_state:
    st.session_state["calc"] = False

st.text_area("Paste your timecard text here:", key="timecard_text", height=260)

colA, colB = st.columns([1, 1])
if colA.button("Calculate", type="primary"):
    st.session_state["calc"] = True
if colB.button("Clear", on_click=handle_clear):
    pass

if st.session_state["calc"]:
    comps = compute_totals(st.session_state["timecard_text"])

    st.subheader("Results")
    c1, c2 = st.columns(2)
    c1.metric("Card Type", comps["card_type"])
    c2.metric("TOTAL PAY", comps["TOTAL"])

    df = pd.DataFrame(
        [(k, v) for k, v in comps.items() if k not in ("card_type",)],
        columns=["Component", "Time"]
    )
    st.table(df)

st.caption("All calculations run locally. No data stored.")
