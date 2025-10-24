import re
from typing import List, Tuple, Dict, Any
import streamlit as st
import pandas as pd

# ======================================================
# Basic helpers
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

def nbps(text: str) -> str:
    return (text or "").replace("\u00A0", " ")

# ======================================================
# Summary line parsing
# ======================================================

def grab_sub_ttl_credit_minutes(raw: str) -> Tuple[int, str]:
    t = nbps(raw)
    best_val = 0
    src = "Not found"
    for line in t.splitlines():
        if "=" in line and re.search(r"\d{1,3}:[0-5]\d", line):
            all_eq = list(re.finditer(r"=\s*(\d{1,3}:[0-5]\d)", line))
            if all_eq:
                mm_str = all_eq[0].group(1)
                best_val = to_minutes(mm_str)
                src = "Equation subtotal line"
    return best_val, src

# ======================================================
# Numeric field extractors
# ======================================================

def extract_numeric_field(raw: str, left_label_regex: str) -> int:
    t = nbps(raw)
    pat_direct = re.compile(
        left_label_regex + r".{0,60}?(\d{1,3}:[0-5]\d)",
        flags=re.I | re.S,
    )
    m = pat_direct.search(t)
    if m:
        return to_minutes(m.group(1))
    lines = t.splitlines()
    for i, line in enumerate(lines):
        if re.search(left_label_regex, line, flags=re.I):
            for j in range(i, min(i+3, len(lines))):
                mm = re.search(r"(\d{1,3}:[0-5]\d)", lines[j])
                if mm:
                    return to_minutes(mm.group(1))
    return 0

def extract_ttl_bank_opts_award_minutes(raw: str) -> int:
    t = nbps(raw)
    pat_label = r"TTL\s+BANK\s+OPTS?\s+AWARD"
    pat_direct = re.compile(
        pat_label + r".{0,60}?(\d{1,3}:[0-5]\d)",
        flags=re.I | re.S,
    )
    m = pat_direct.search(t)
    if m:
        return to_minutes(m.group(1))
    lines = t.splitlines()
    for i, line in enumerate(lines):
        if re.search(pat_label, line, flags=re.I):
            for j in range(i, min(i+3, len(lines))):
                mm = re.search(r"(\d{1,3}:[0-5]\d)", lines[j])
                if mm:
                    return to_minutes(mm.group(1))
    math_pat = re.compile(
        r"=\s*\d{1,3}:[0-5]\d\s*-\s*\d{1,3}:[0-5]\d\s*\+\s*(\d{1,3}:[0-5]\d)\s*=\s*\d{1,3}:[0-5]\d",
        flags=re.I,
    )
    m2 = math_pat.search(t)
    if m2:
        return to_minutes(m2.group(1))
    return 0

def extract_training_pay_minutes(raw: str) -> int:
    t = nbps(raw)
    total = 0
    for m in re.finditer(r"DISTRIBUTED\s+TRNG\s+PAY:\s+(\d{1,3}:[0-5]\d)", t, flags=re.I):
        total += to_minutes(m.group(1))
    return total

# ======================================================
# Named pay buckets
# ======================================================

def extract_named_bucket(text: str, labels: List[str]) -> int:
    t = nbps(text)
    for lbl in labels:
        pattern = re.sub(r"\s+", r"\\s+", re.escape(lbl)) + r"\s*:\s*(\d{1,3}:[0-5]\d)"
        m = re.search(pattern, t, flags=re.I)
        if m:
            return to_minutes(m.group(1))
    return 0

def extract_res_assign_gslip_bucket(text: str) -> int:
    t = nbps(text)
    m = re.search(r"RES\s+ASSIGN[-\s]+G/\s*SLIP\s+PAY\s*:\s*(\d{1,3}:[0-5]\d)", t, flags=re.I)
    if m:
        return to_minutes(m.group(1))
    return 0

# ======================================================
# Duty row parsing
# ======================================================

def parse_duty_rows(raw: str) -> List[Dict[str, Any]]:
    t = nbps(raw)
    seg_re = re.compile(
        r"(?P<date>\d{2}[A-Z]{3})\s+(?P<duty>(RES|REG))\s+(?P<nbr>[A-Z0-9/-]+)(?P<tail>.*?)(?=\d{2}[A-Z]{3}\s+(RES|REG)\b|RES\s+OTHER|CREDIT\s+APPLICABLE|END OF DISPLAY|$)",
        re.I | re.S,
    )

    rows = []
    for m in seg_re.finditer(t):
        date = m.group("date").upper()
        duty = m.group("duty").upper()
        nbr  = m.group("nbr").upper()
        seg_full  = m.group(0)
        tail_text = m.group("tail")
        times = re.findall(r"\b\d{1,3}:[0-5]\d\b", seg_full)
        has_trans = "TRANS" in tail_text.upper()
        has_credit_block = False
        if len(times) >= 4:
            before_last = times[:-1]
            last3 = before_last[-3:]
            if len(last3) == 3 and len(set(last3)) == 1:
                has_credit_block = True
        bump_pay_candidate = None
        if len(times) >= 2:
            prev_time, last_time = times[-2], times[-1]
            if to_minutes(last_time) < to_minutes(prev_time) and last_time != times[0]:
                bump_pay_candidate = last_time
        main_pay_candidate = None
        if "RRPY" in nbr and times:
            main_pay_candidate = times[-1]
        if main_pay_candidate is None and duty == "RES" and times and not has_credit_block:
            if len(set(times)) == 1 and nbr not in ("SICK", "TOFF") and not has_trans:
                main_pay_candidate = times[-1]
        if main_pay_candidate is None and duty == "REG" and times and not has_trans and len(times) == 1:
            main_pay_candidate = times[0]
        # Rule D
        if main_pay_candidate is None:
            if duty == "REG" and not has_trans and not has_credit_block and len(times) >= 3 and bump_pay_candidate:
                main_pay_candidate = times[-2]
        rows.append({
            "date": date, "duty": duty, "nbr": nbr, "times": times,
            "has_credit_block": has_credit_block, "has_trans": has_trans,
            "main_pay_candidate": main_pay_candidate,
            "bump_pay_candidate": bump_pay_candidate, "raw": seg_full.strip()
        })
    return rows

# ======================================================
# Card type
# ======================================================

def detect_card_type(rows: List[Dict[str, Any]]) -> str:
    saw_res = any(r["duty"] == "RES" for r in rows)
    saw_reg = any(r["duty"] == "REG" for r in rows)
    return "LINEHOLDER" if (saw_reg and not saw_res) else "RESERVE"

# ======================================================
# Components + totals
# ======================================================

def compute_components(raw: str) -> Dict[str, Any]:
    rows = parse_duty_rows(raw)
    card_type = detect_card_type(rows)
    sub_ttl_credit_mins, sub_src = grab_sub_ttl_credit_minutes(raw)

    pay_only_main_mins = pay_only_bump_mins = 0
    debug_rows = []
    for r in rows:
        add_main = to_minutes(r["main_pay_candidate"]) if r["main_pay_candidate"] else 0
        add_bump = to_minutes(r["bump_pay_candidate"]) if r["bump_pay_candidate"] else 0
        pay_only_main_mins += add_main
        pay_only_bump_mins += add_bump
        debug_rows.append({
            "Date": r["date"], "Duty": r["duty"], "NBR": r["nbr"],
            "Times": ", ".join(r["times"]),
            "TRANS row?": "Y" if r["has_trans"] else "N",
            "Has credit block (3x repeat)?": "Y" if r["has_credit_block"] else "N",
            "Added to PAY TIME ONLY": from_minutes(add_main) if add_main else "",
            "Added to ADDTL PAY ONLY": from_minutes(add_bump) if add_bump else "",
            "Raw Row Snippet": r["raw"][:200],
        })

    reroute_pay_mins      = extract_named_bucket(raw, ["REROUTE PAY"])
    assign_pay_mins       = extract_named_bucket(raw, ["ASSIGN PAY"])
    g_slip_pay_mins       = extract_named_bucket(raw, ["G/SLIP PAY", "G SLIP PAY"])
    res_assign_gslip_mins = extract_res_assign_gslip_bucket(raw)
    bank_dep_award_mins   = extract_numeric_field(raw, r"BANK\s+DEP\s+AWARD")
    ttl_bank_opts_award_mins = extract_ttl_bank_opts_award_minutes(raw)
    training_pay_mins     = extract_training_pay_minutes(raw)

    return {
        "card_type": card_type, "sub_ttl_credit_mins": sub_ttl_credit_mins,
        "sub_ttl_src": sub_src, "pay_only_main_mins": pay_only_main_mins,
        "pay_only_bump_mins": pay_only_bump_mins, "reroute_pay_mins": reroute_pay_mins,
        "assign_pay_mins": assign_pay_mins, "g_slip_pay_mins": g_slip_pay_mins,
        "res_assign_gslip_mins": res_assign_gslip_mins,
        "bank_dep_award_mins": bank_dep_award_mins,
        "ttl_bank_opts_award_mins": ttl_bank_opts_award_mins,
        "training_pay_mins": training_pay_mins, "debug_rows": debug_rows
    }

def compute_totals(raw: str) -> Dict[str, Any]:
    c = compute_components(raw)
    base = (c["sub_ttl_credit_mins"] + c["pay_only_main_mins"] +
            c["pay_only_bump_mins"] + c["reroute_pay_mins"] +
            c["assign_pay_mins"] + c["bank_dep_award_mins"] +
            c["ttl_bank_opts_award_mins"] + c["training_pay_mins"])
    total = base + (c["res_assign_gslip_mins"] if c["card_type"]=="RESERVE" else c["g_slip_pay_mins"])
    c["total_mins"], c["total_hmm"], c["total_decimal"] = total, from_minutes(total), round(total/60,2)
    return c

# ======================================================
# Streamlit UI
# ======================================================

st.set_page_config(page_title="Timecard Pay Calculator", layout="wide")
st.title("🧮 Timecard Pay Calculator")
st.caption("Detects Reserve vs Lineholder and applies contract rules automatically.")

def handle_clear():
    st.session_state["timecard_text"] = ""
    st.session_state["calc"] = False

with st.sidebar:
    st.header("Example Inputs")
    if st.button("Load Reserve Example"):
        st.session_state["timecard_text"] = "06OCT RES SCC 1:00 1:00 09OCT RES SCC 1:00 1:00 15OCT RES 5999 5:14 10:30 10:30 10:30 0:07 19OCT RES 0198 5:06 7:21 7:21 7:21 1:22 19OCT RES ADJ-RRPY 1:53 1:53 G/SLIP PAY : 0:00 RES ASSIGN-G/SLIP PAY: 10:30 REROUTE PAY: 10:30 END OF DISPLAY"
    if st.button("Load Lineholder Example"):
        st.session_state["timecard_text"] = "01JUN REG 3554 6:30 TRANS TRANS 10:49 0:13 05JUN REG 3210 7:24 10:30 10:30 10:30 09JUN REG 3191 6:52 10:30 10:30 10:30 17JUN REG 0889 2:20 10:30 10:30 10:30 18JUN REG RRPY 3:09 23JUN REG C428 15:01 15:45 15:45 15:45 0:38 26JUN REG 0608 5:16 10:30 10:30 10:30 3:38 27JUN REG RRPY 5:26 28JUN REG 0451 1:35 10:30 10:30 3:23 68:34 + 0:00 + 0:00 = 68:34 - 0:00 + 3:26 = 72:00 BANK DEP AWARD 0:00 TTL BANK OPTS AWARD 3:26 G/SLIP PAY : 10:30 END OF DISPLAY"

if "timecard_text" not in st.session_state:
    st.session_state["timecard_text"] = ""
if "calc" not in st.session_state:
    st.session_state["calc"] = False

st.text_area("Paste your timecard text here:", key="timecard_text", height=260)
st.divider()
col1, col2 = st.columns([1,1])
if col1.button("Calculate", type="primary"):
    st.session_state["calc"] = True
col2.button("Clear", on_click=handle_clear)

if st.session_state["calc"]:
    comps = compute_totals(st.session_state["timecard_text"])
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("TOTAL PAY (H:MM)", comps["total_hmm"])
    c2.metric("TOTAL PAY (Decimal)", f"{comps['total_decimal']:.2f}")
    c3.metric("Card Type", comps["card_type"])
    c4.metric("SUB TTL Source", comps["sub_ttl_src"])

    df = pd.DataFrame([
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
        ("TOTAL (Applied Formula Above)", comps["total_hmm"])
    ], columns=["Component","Time"])
    st.table(df)

    with st.expander("Row Debug"):
        st.dataframe(pd.DataFrame(comps["debug_rows"]), use_container_width=True)
st.caption("All calculations run locally — no data stored.")
