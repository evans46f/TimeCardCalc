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
    Get the base period credit (SUB TTL CREDIT / TTL CREDIT BEFORE AWARD).
    We take the LAST '= H:MM' on the guarantee math line.
    e.g. "68:34 ... = 68:34 - 0:00 + 3:26 = 72:00"
    We take 68:34, not 72:00 (72:00 already includes award).
    """
    t = nbps(raw)
    best_val = 0
    src = "Not found"
    for line in t.splitlines():
        if "=" in line and re.search(r"\d{1,3}:[0-5]\d", line):
            all_eq = list(re.finditer(r"=\s*(\d{1,3}:[0-5]\d)", line))
            if all_eq:
                mm_str = all_eq[0].group(1)  # first subtotal after '='
                best_val = to_minutes(mm_str)
                src = "Equation subtotal line"
    return best_val, src


def extract_numeric_field(raw: str, left_label_regex: str) -> int:
    """
    Extract a single H:MM for 'bank award' style fields like:
      BANK DEP ... AWARD ... 0:00
      TTL BANK ... OPTS AWARD ... 3:26
    We'll match the label, allow ~40 chars, then grab the first H:MM after.
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


def extract_training_pay_minutes(raw: str) -> int:
    """
    Sum any 'DISTRIBUTED TRNG PAY:' lines.
      ... DISTRIBUTED TRNG PAY:   1:00
      ... DISTRIBUTED TRNG PAY:   1:29
    -> 1:00 + 1:29
    """
    t = nbps(raw)
    total = 0
    for m in re.finditer(
        r"DISTRIBUTED\s+TRNG\s+PAY:\s+(\d{1,3}:[0-5]\d)",
        t,
        flags=re.I,
    ):
        total += to_minutes(m.group(1))
    return total


# ======================================================
# Named pay buckets (bottom section)
# ======================================================

def _label_to_regex(lbl: str) -> str:
    """
    Turn 'G/SLIP PAY' into 'G\/SLIP\s+PAY' etc.
    We escape each word and join with \s+ to allow flexible spacing.
    """
    parts = re.split(r"\s+", lbl.strip())
    esc = [re.escape(p) for p in parts]
    return r"\s+".join(esc)


def extract_named_bucket(text: str, labels: List[str]) -> int:
    """
    Extract values like:
      G/SLIP PAY : 10:30
      ASSIGN PAY: 0:00
      REROUTE PAY: 6:32
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
    Parse 'RES ASSIGN-G/SLIP PAY:' with flexible spacing / hyphen.
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
# Duty row parsing
# ======================================================

def parse_duty_rows(raw: str) -> List[Dict[str, Any]]:
    """
    Parse each duty-day row and extract:
      - main_pay_candidate  -> PAY TIME ONLY (PAY NO CREDIT)
      - bump_pay_candidate  -> ADDTL PAY ONLY COLUMN
      - debug flags

    PAY TIME ONLY rules:

      Rule A: If NBR includes 'RRPY' (RRPY, ADJ-RRPY, etc.),
              then the LAST H:MM on that row counts as PAY TIME ONLY.
              Applies to BOTH RES and REG.
              e.g. "ADJ-RRPY 1:53 1:53", "RRPY 3:09", "RRPY 5:26"

      Rule B: RES flat-pay guarantee style:
        duty == RES
        not has_credit_block
        all times on that row are IDENTICAL
        NBR not in {"SICK","TOFF"}
        row doesn't contain "TRANS"
        → take that time as PAY TIME ONLY
        (covers SCC, LOSA, PVEL, 20WD, VAC, PVEL, 4F1C, etc.)
        We EXCLUDE SICK, TOFF, TRANS because you said don't stack those.

      Rule C: REG single-pay guarantee style:
        duty == REG
        not has_trans
        len(times) == 1
        → that single time is PAY TIME ONLY
        (covers things like "44WD 10:00")

      Rule D: REG partial-credit-with-tail style (your 28JUN case):
        duty == REG
        not has_trans
        not has_credit_block  (so NOT the classic 10:30 10:30 10:30 3:38 block)
        len(times) >= 3
        the row has a "tail" we already classify as bump_pay_candidate
        → treat the SECOND-TO-LAST time as PAY TIME ONLY
        (ex: "1:35 10:30 10:30 3:23" -> PAY TIME ONLY = 10:30, bump = 3:23)

    ADDTL PAY ONLY rules (bump pay):
      If len(times) >= 2 and
         last_time < prev_time (in minutes) and
         last_time != first_time on the row:
        → last_time is ADDTL PAY ONLY for that row
      (covers 0:13, 0:38, 3:38, 3:23, etc.)

    We do NOT also treat normal pairing block credit (like
    10:30 10:30 10:30 0:07) as PAY TIME ONLY, because it's already in subtotal.
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

        # All H:MM tokens on this row:
        times = re.findall(r"\b\d{1,3}:[0-5]\d\b", seg_full)

        # Is this row marked TRANS?
        has_trans = "TRANS" in tail_text.upper()

        # pairing credit block?
        # e.g. "10:30 10:30 10:30 0:07" (triple repeat before final tail)
        has_credit_block = False
        if len(times) >= 4:
            before_last = times[:-1]
            last3 = before_last[-3:]
            if len(last3) == 3 and len(set(last3)) == 1:
                has_credit_block = True

        # bump pay candidate (ADDTL PAY ONLY COLUMN)
        bump_pay_candidate = None
        if len(times) >= 2:
            prev_time = times[-2]
            last_time = times[-1]
            prev_m = to_minutes(prev_time)
            last_m = to_minutes(last_time)
            # only count real little tails (like 0:13, 0:38, 3:38, 3:23)
            if last_m < prev_m and last_time != times[0]:
                bump_pay_candidate = last_time

        # PAY TIME ONLY candidate
        main_pay_candidate = None

        # Rule A: any RRPY => last time is PAY TIME ONLY
        if "RRPY" in nbr and times:
            main_pay_candidate = times[-1]

        # Rule B: RES flat-pay (SCC/LOSA/PVEL/etc.), excluding SICK/TOFF/TRANS
        if main_pay_candidate is None:
            if duty == "RES" and times and not has_credit_block:
                unique_times = set(times)
                if (
                    len(unique_times) == 1
                    and nbr not in ("SICK", "TOFF")
                    and not has_trans
                ):
                    main_pay_candidate = times[-1]

        # Rule C: REG single-pay (like "44WD 10:00")
        if main_pay_candidate is None:
            if duty == "REG" and times and not has_trans:
                if len(times) == 1:
                    main_pay_candidate = times[0]

        # Rule D: REG partial-credit-with-tail ("... 10:30 10:30 3:23")
        # conditions:
        #   - REG
        #   - not has_trans
        #   - not has_credit_block (so it's not a 3x repeat trip credit)
        #   - len(times) >= 3
        #   - there's a bump tail
        #   - then second-to-last time is PAY TIME ONLY
        if main_pay_candidate is None:
            if (
                duty == "REG"
                and times
                and not has_trans
                and not has_credit_block
                and len(times) >= 3
                and bump_pay_candidate is not None
            ):
                # second-to-last time
                main_pay_candidate = times[-2]

        rows.append({
            "date": date,
            "duty": duty,
            "nbr": nbr,
            "times": times,
            "has_credit_block": has_credit_block,
            "has_trans": has_trans,
            "main_pay_candidate": main_pay_candidate,   # PAY TIME ONLY
            "bump_pay_candidate": bump_pay_candidate,   # ADDTL PAY ONLY
            "raw": seg_full.strip(),
        })

    return rows


# ======================================================
# Card type detection
# ======================================================

def detect_card_type(rows: List[Dict[str, Any]]) -> str:
    """
    Decide whether the card is RESERVE or LINEHOLDER.

    Rule:
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
    rows = parse_duty_rows(raw)
    card_type = detect_card_type(rows)

    sub_ttl_credit_mins, sub_src = grab_sub_ttl_credit_minutes(raw)

    pay_only_main_mins = 0   # PAY TIME ONLY
    pay_only_bump_mins = 0   # ADDTL PAY ONLY

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
            "TRANS row?": "Y" if r["has_trans"] else "N",
            "Has credit block (3x repeat)?": "Y" if r["has_credit_block"] else "N",
            "Added to PAY TIME ONLY": from_minutes(add_main) if add_main else "",
            "Added to ADDTL PAY ONLY": from_minutes(add_bump) if add_bump else "",
            "Raw Row Snippet": r["raw"][:200],
        })

    # Bottom summary buckets:
    reroute_pay_mins      = extract_named_bucket(raw, ["REROUTE PAY"])
    assign_pay_mins       = extract_named_bucket(raw, ["ASSIGN PAY"])
    g_slip_pay_mins       = extract_named_bucket(raw, ["G/SLIP PAY", "G SLIP PAY", "G - SLIP PAY"])
    res_assign_gslip_mins = extract_res_assign_gslip_bucket(raw)

    bank_dep_award_mins       = extract_numeric_field(raw, r"BANK\s+DEP\s+AWARD")
    ttl_bank_opts_award_mins  = extract_numeric_field(raw, r"TTL\s+BANK\s+OPTS?\s+AWARD")

    training_pay_mins = extract_training_pay_minutes(raw)

    return {
        "card_type": card_type,

        "sub_ttl_credit_mins": sub_ttl_credit_mins,
        "sub_ttl_src": sub_src,

        "pay_only_main_mins": pay_only_main_mins,     # PAY TIME ONLY
        "pay_only_bump_mins": pay_only_bump_mins,     # ADDTL PAY ONLY

        "reroute_pay_mins": reroute_pay_mins,
        "assign_pay_mins": assign_pay_mins,
        "g_slip_pay_mins": g_slip_pay_mins,
        "res_assign_gslip_mins": res_assign_gslip_mins,

        "bank_dep_award_mins": bank_dep_award_mins,
        "ttl_bank_opts_award_mins": ttl_bank_opts_award_mins,

        "training_pay_mins": training_pay_mins,

        "debug_rows": debug_rows,
    }


# ======================================================
# Totals logic
# ======================================================

def compute_totals(raw: str) -> Dict[str, Any]:
    comps = compute_components(raw)
    ct = comps["card_type"]

    # Buckets shared by both card types:
    base_mins = (
        comps["sub_ttl_credit_mins"]
        + comps["pay_only_main_mins"]
        + comps["pay_only_bump_mins"]
        + comps["reroute_pay_mins"]
        + comps["assign_pay_mins"]
        + comps["bank_dep_award_mins"]
        + comps["ttl_bank_opts_award_mins"]
        + comps["training_pay_mins"]
    )

    # Reserve includes RES ASSIGN-G/SLIP PAY
    # Lineholder includes G/SLIP PAY
    if ct == "RESERVE":
        total_mins = base_mins + comps["res_assign_gslip_mins"]
    else:
        total_mins = base_mins + comps["g_slip_pay_mins"]

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

# Clear button callback
def handle_clear():
    st.session_state["timecard_text"] = ""
    st.session_state["calc"] = False

# Sidebar example loaders
with st.sidebar:
    st.header("Input")
    example_btn_res = st.button("Load Reserve Example")
    example_btn_line = st.button("Load Lineholder Example")

default_text = ""
if example_btn_res:
    default_text = (
        "MONTHLY TIME DATA 10/24/25 10:16:32 "
        "BID PERIOD: 01OCT25 - 31OCT25 ATL 320 B INIT LOT: 0513 "
        "TEMP IN BANK -1:08 IN BANK -1:08 ALV 77:45 "
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
        "REROUTE PAY: 10:30 "
        "END OF DISPLAY"
    )

if example_btn_line:
    default_text = (
        "MONTHLY TIME DATA 10/24/25 08:38:49 "
        "BID PERIOD: 02JUN25 - 01JUL25 ATL 73N B INIT LOT: 0059 "
        "TEMP IN BANK 0:00 IN BANK 0:00 ALV 79:49 "
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
        "END OF DISPLAY"
    )

# init session state
if "timecard_text" not in st.session_state:
    st.session_state["timecard_text"] = default_text
elif default_text:
    st.session_state["timecard_text"] = default_text

if "calc" not in st.session_state:
    st.session_state["calc"] = False

# textarea
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
    comps = compute_totals(st.session_state["timecard_text"])

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
        ("DISTRIBUTED TRNG PAY", from_minutes(comps["training_pay_mins"])),
        ("TOTAL (Applied Formula Above)", comps["total_hmm"]),
    ]
    df = pd.DataFrame(breakdown_rows, columns=["Component", "Time"])
    st.table(df)

    with st.expander("Row Debug (what each duty day contributed)"):
        dbg = pd.DataFrame(comps["debug_rows"])
        st.dataframe(dbg, use_container_width=True)
        st.caption(
            "Reserve total = SUB TTL CREDIT + PAY TIME ONLY + ADDTL PAY ONLY + "
            "REROUTE PAY + ASSIGN PAY + RES ASSIGN-G/SLIP PAY + BANK DEP AWARD + "
            "TTL BANK OPTS AWARD + DISTRIBUTED TRNG PAY. "
            "Lineholder total = SUB TTL CREDIT + PAY TIME ONLY + ADDTL PAY ONLY + "
            "REROUTE PAY + ASSIGN PAY + G/SLIP PAY + BANK DEP AWARD + "
            "TTL BANK OPTS AWARD + DISTRIBUTED TRNG PAY."
        )

st.caption("All math runs locally. No data stored.")
