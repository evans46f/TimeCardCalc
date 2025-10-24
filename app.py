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

def clean(text: str) -> str:
    return (text or "").replace("\u00A0", " ")

# ======================================================
# Card type detection
# ======================================================

def detect_card_type(raw: str) -> str:
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
# Lineholder parsing and rules
# ======================================================

def parse_lineholder_rows(raw: str) -> List[Dict[str, Any]]:
    rows = []
    for line in clean(raw).splitlines():
        line_up = line.upper().strip()
        m = re.match(r"^(\d{2}[A-Z]{3})\s+REG\s+(\S+)\s+(.*)$", line_up)
        if not m:
            continue
        date, nbr, rest = m.groups()
        times = re.findall(r"\b\d{1,3}:[0-5]\d\b", line_up)
        has_trans = "TRANS" in line_up
        rows.append({
            "date": date, "nbr": nbr, "times": times,
            "has_trans": has_trans, "raw": line.strip()
        })
    return rows

def calc_pay_time_only_lineholder(rows: List[Dict[str, Any]]) -> int:
    total = 0
    for r in rows:
        times = r["times"]
        # RRPY rule
        if "RRPY" in r["nbr"] and times:
            total += to_minutes(times[-1])
            continue
        # Single-pay guarantee (e.g. 44WD 10:00)
        if (not r["has_trans"]) and len(times) == 1:
            total += to_minutes(times[0])
            continue
        # Short-day min guarantee
        if (not r["has_trans"]) and len(times) >= 3:
            prev_last_str = times[-2]
            prev_last = to_minutes(prev_last_str)
            last = to_minutes(times[-1])
            if last < prev_last:
                cnt_prev_last = times.count(prev_last_str)
                if cnt_prev_last == 2:
                    total += prev_last
                    continue
    return total

def calc_addtl_pay_only_lineholder(rows: List[Dict[str, Any]]) -> int:
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
    out = []
    for r in rows:
        times = r["times"]
        pay_only_add, addtl_only_add = "", ""
        if "RRPY" in r["nbr"] and times:
            pay_only_add = from_minutes(to_minutes(times[-1]))
        elif (not r["has_trans"]) and len(times) == 1:
            pay_only_add = from_minutes(to_minutes(times[0]))
        elif (not r["has_trans"]) and len(times) >= 3:
            prev_last_str = times[-2]
            prev_last = to_minutes(prev_last_str)
            last = to_minutes(times[-1])
            if last < prev_last:
                if times.count(prev_last_str) == 2:
                    pay_only_add = from_minutes(prev_last)
        if len(times) >= 2:
            prev_last = to_minutes(times[-2])
            last = to_minutes(times[-1])
            if last < prev_last:
                addtl_only_add = from_minutes(last)
        out.append({
            "Date": r["date"], "NBR": r["nbr"],
            "TRANS row?": "Y" if r["has_trans"] else "N",
            "All Times": ", ".join(times),
            "Counted PAY TIME ONLY": pay_only_add,
            "Counted ADDTL PAY ONLY": addtl_only_add,
            "Raw Line": r["raw"][:200],
        })
    return out

# ======================================================
# Reserve parsing and rules (simplified)
# ======================================================

def extract_named_bucket(text: str, labels: List[str]) -> int:
    t = clean(text)
    for lbl in labels:
        pattern = re.escape(lbl) + r"\s*[:=]\s*(\d{1,3}:[0-5]\d)"
        m = re.search(pattern, t, flags=re.I)
        if m:
            return to_minutes(m.group(1))
    return 0

def grab_sub_ttl_credit_minutes(raw: str) -> int:
    t = clean(raw)
    m = re.search(r"SUB\s+TTL\s+CREDIT.*?=\s*(\d{1,3}:[0-5]\d)", t, flags=re.I)
    if m:
        return to_minutes(m.group(1))
    return 0

def compute_reserve_total(raw: str) -> Dict[str, Any]:
    sub_ttl = grab_sub_ttl_credit_minutes(raw)
    pay_time_lines = re.findall(r"\bRES\s+\S+\s+(?:\S+\s+){0,6}?(\d{1,3}:[0-5]\d)\s+\1\b", clean(raw))
    pay_time_only = sum(to_minutes(x) for x in pay_time_lines)
    res_assign_gslip = extract_named_bucket(raw, ["RES ASSIGN-G/SLIP PAY"])
    reroute = extract_named_bucket(raw, ["REROUTE PAY"])
    assign = extract_named_bucket(raw, ["ASSIGN PAY"])
    bank_dep = extract_named_bucket(raw, ["BANK DEP AWARD"])
    ttl_bank = extract_named_bucket(raw, ["TTL BANK OPTS AWARD"])
    total = sub_ttl + pay_time_only + reroute + assign + res_assign_gslip + bank_dep + ttl_bank
    return {
        "TTL CREDIT": from_minutes(sub_ttl),
        "PAY TIME ONLY (PAY NO CREDIT)": from_minutes(pay_time_only),
        "REROUTE PAY": from_minutes(reroute),
        "ASSIGN PAY": from_minutes(assign),
        "RES ASSIGN-G/SLIP PAY": from_minutes(res_assign_gslip),
        "BANK DEP AWARD": from_minutes(bank_dep),
        "TTL BANK OPTS AWARD": from_minutes(ttl_bank),
        "TOTAL": from_minutes(total),
    }

# ======================================================
# Compute totals (combined)
# ======================================================

def compute_totals(raw: str) -> Dict[str, Any]:
    card_type = detect_card_type(raw)
    if card_type == "LINEHOLDER":
        rows = parse_lineholder_rows(raw)
        ttl_credit = grab_sub_ttl_credit_minutes(raw)
        pay_only = calc_pay_time_only_lineholder(rows)
        addtl_only = calc_addtl_pay_only_lineholder(rows)
        reroute = extract_named_bucket(raw, ["REROUTE PAY"])
        assign = extract_named_bucket(raw, ["ASSIGN PAY"])
        gslip = extract_named_bucket(raw, ["G/SLIP PAY"])
        total = ttl_credit + pay_only + addtl_only + reroute + assign + gslip
        return {
            "card_type": "LINEHOLDER",
            "TTL CREDIT": from_minutes(ttl_credit),
            "PAY TIME ONLY (PAY NO CREDIT)": from_minutes(pay_only),
            "ADDTL PAY ONLY COLUMN": from_minutes(addtl_only),
            "REROUTE PAY": from_minutes(reroute),
            "ASSIGN PAY": from_minutes(assign),
            "G/SLIP PAY": from_minutes(gslip),
            "TOTAL": from_minutes(total),
            "debug_rows": build_lineholder_debug_rows(rows),
        }
    else:
        data = compute_reserve_total(raw)
        data["card_type"] = "RESERVE"
        return data

# ======================================================
# Streamlit UI
# ======================================================

st.set_page_config(page_title="Timecard Pay Calculator", layout="wide")
st.title("🧮 Timecard Pay Calculator")
st.caption("Auto-detects RESERVE vs LINEHOLDER and applies the correct pay rules.")

st.text_area("Paste your timecard text here:", key="timecard_text", height=260)

colA, colB = st.columns([1, 1])
calc_btn = colA.button("Calculate", type="primary")
clear_btn = colB.button("Clear", on_click=lambda: st.session_state.update(timecard_text="", calc=False))

if "calc" not in st.session_state:
    st.session_state["calc"] = False

if calc_btn:
    st.session_state["calc"] = True

if st.session_state["calc"]:
    raw = st.session_state["timecard_text"]
    result = compute_totals(raw)
    st.subheader("Results")
    c1, c2 = st.columns([1, 1])
    c1.metric("Card Type", result["card_type"])
    c2.metric("TOTAL PAY (H:MM)", result["TOTAL"])
    rows = [(k, v) for k, v in result.items() if k not in ("card_type", "debug_rows", "TOTAL")]
    df = pd.DataFrame(rows, columns=["Component", "Time"])
    st.table(df)
    if result.get("debug_rows"):
        with st.expander("Row Debug"):
            st.dataframe(pd.DataFrame(result["debug_rows"]), use_container_width=True)

st.caption("All math runs locally. No data stored.")
