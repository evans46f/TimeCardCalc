import streamlit as st
from PIL import Image
import pytesseract
import pandas as pd
import re

# =========================
# Streamlit config
# =========================
st.set_page_config(
    page_title="Timecard Calculator",
    layout="wide"
)

st.title("🕘 Monthly Time Data Calculator (OCR Beta)")
st.caption("Upload a screenshot of your Monthly Time Data page. We'll read it, parse it, and total your hours — locally, using Tesseract OCR (no cloud upload).")

uploaded_file = st.file_uploader(
    "Upload screenshot (.png, .jpg, .jpeg)",
    type=["png", "jpg", "jpeg"]
)

# =========================
# Helper functions
# =========================

def hhmm_to_minutes(hhmm: str) -> int:
    """
    Convert '10:30' -> 630 minutes.
    Returns 0 if invalid or blank.
    """
    if not isinstance(hhmm, str):
        return 0
    hhmm = hhmm.strip()
    if not re.match(r"^\d{1,2}:\d{2}$", hhmm):
        return 0
    try:
        h_str, m_str = hhmm.split(":")
        h = int(h_str)
        m = int(m_str)
        return h * 60 + m
    except ValueError:
        return 0

def minutes_to_hhmm(total_minutes: int) -> str:
    """
    Convert 630 -> '10:30' with zero padding.
    """
    hours = total_minutes // 60
    mins = total_minutes % 60
    return f"{hours:02d}:{mins:02d}"

def ocr_image_to_text(pil_image: Image.Image) -> str:
    """
    Run Tesseract OCR on the screenshot and return the raw text.
    We set --psm 6 (assume a uniform block of text with columns).
    """
    custom_config = r"--psm 6"
    return pytesseract.image_to_string(pil_image, config=custom_config)

def crop_ocr_to_timecard_section(raw_text: str) -> str:
    """
    We only want to parse the actual timecard table.
    To cut down junk, we:
      - find the first line that contains 'MONTHLY TIME DATA'
      - keep everything from there downward
    If we can't find it, we just return the original text.
    """
    lines = raw_text.splitlines()

    start_index = 0
    for i, line in enumerate(lines):
        if "MONTHLY TIME DATA" in line.upper():
            start_index = i
            break

    trimmed_lines = lines[start_index:]
    return "\n".join(trimmed_lines)

def normalize_row(des: str, time_list):
    """
    Map the list of up to four hh:mm values in that row
    into BLOCK, SKED, PAY, CREDIT using two rule sets:

    Rule set A: Reserve / SCC style days
        - DES like RES, RSV, RESERVE, SCC, etc.
        - Usually no BLOCK, and SKED/PAY/CREDIT are basically duty/credit
    Rule set B: Lineholder / REG flying days
        - BLOCK, SKED, PAY, CREDIT left-to-right

    We also make CREDIT robust:
    - If CREDIT is missing, fall back to PAY.
    - For reserve, CREDIT usually equals PAY anyway.
    """
    des_clean = (des or "").upper()

    # treat these as reserve-type days
    is_reserve_day = (
        des_clean.startswith("RES") or  # RES / RESV / RESERVE
        des_clean.startswith("RSV") or  # RSV
        des_clean == "SCC"              # SCC as seen in reserve examples
    )

    # filter out None / '' so indexing works cleanly
    tvals = [t for t in time_list if t]

    if is_reserve_day:
        # Reserve logic:
        # BLOCK is generally empty
        block = ""
        # first seen -> SKED
        sked  = tvals[0] if len(tvals) > 0 else ""
        # second seen -> PAY (fallback to SKED if missing)
        pay   = tvals[1] if len(tvals) > 1 else sked
        # CREDIT is usually same as PAY unless there's a later explicit number
        cred  = tvals[-1] if len(tvals) >= 2 else sked
        return block, sked, pay, cred

    # Lineholder / REG flying logic:
    # Expected order = BLOCK, SKED, PAY, CREDIT
    block = tvals[0] if len(tvals) > 0 else ""
    sked  = tvals[1] if len(tvals) > 1 else ""
    # PAY: if missing, fall back to SKED
    pay   = tvals[2] if len(tvals) > 2 else (tvals[1] if len(tvals) > 1 else "")
    # CREDIT: if missing, fall back to PAY
    cred  = tvals[3] if len(tvals) > 3 else pay

    return block, sked, pay, cred

def parse_timecard_lines(ocr_text: str) -> pd.DataFrame:
    """
    Take OCR text for the relevant section only and extract daily entries.

    Expected shapes:
      05OCT  REG 3324   8:50  10:30 10:30 10:30
      06OCT  RES SCC          1:00  1:00

    We'll:
    - Break text into lines
    - Try to match each line with regex
    - Use normalize_row() to map times into BLOCK/SKED/PAY/CREDIT
    """

    # Split into lines and strip blanks
    lines = [ln.strip() for ln in ocr_text.split("\n") if ln.strip()]

    rows = []

    # Regex:
    # DATE: 2 digits + 3 letters (like 05OCT)
    # DES:  word like REG, RES, RSV, etc.
    # NBR:  alphanumeric trip/code like 3324, SCC, 0991
    # Up to four time groups (hh:mm)
    row_pattern = re.compile(
        r"^(?P<DATE>\d{2}[A-Z]{3})\s+"
        r"(?P<DES>[A-Z]+)\s+"
        r"(?P<NBR>[A-Z0-9]+)"
        r"(?:\s+(?P<T1>\d{1,2}:\d{2}))?"
        r"(?:\s+(?P<T2>\d{1,2}:\d{2}))?"
        r"(?:\s+(?P<T3>\d{1,2}:\d{2}))?"
        r"(?:\s+(?P<T4>\d{1,2}:\d{2}))?"
        r"$"
    )

    for ln in lines:
        m = row_pattern.match(ln)
        if not m:
            continue

        g = m.groupdict()

        t_values = [g.get("T1"), g.get("T2"), g.get("T3"), g.get("T4")]
        block, sked, pay, cred = normalize_row(g["DES"], t_values)

        rows.append({
            "DATE": g["DATE"],
            "DES": g["DES"],
            "NBR": g["NBR"],
            "BLOCK": block,
            "SKED": sked,
            "PAY": pay,
            "CREDIT": cred
        })

    if not rows:
        return pd.DataFrame(columns=["DATE","DES","NBR","BLOCK","SKED","PAY","CREDIT"])

    df = pd.DataFrame(rows)

    # Drop duplicates (OCR can sometimes double-read)
    df = df.drop_duplicates()

    # Filter out accidental header captures like "DATE DES NBR BLOCK ..."
    df = df[~df["DATE"].str.contains("DATE", na=False)]

    df = df.reset_index(drop=True)

    return df

def compute_totals(df: pd.DataFrame) -> dict:
    """
    Calculate total monthly credit/pay.
    We prioritize CREDIT. If CREDIT is blank for a row,
    we fall back to PAY for that row.
    """
    if df.empty:
        return {
            "total_minutes": 0,
            "total_str": "00:00",
            "detail_df": df.assign(
                CREDIT_EFFECTIVE=[],
                CREDIT_MIN=[]
            )
        }

    df_work = df.copy()

    effective_credit_col = []
    for _, r in df_work.iterrows():
        if r["CREDIT"]:
            effective_credit_col.append(r["CREDIT"])
        elif r["PAY"]:
            effective_credit_col.append(r["PAY"])
        else:
            effective_credit_col.append("")

    df_work["CREDIT_EFFECTIVE"] = effective_credit_col
    df_work["CREDIT_MIN"] = df_work["CREDIT_EFFECTIVE"].apply(hhmm_to_minutes)

    total_minutes = int(df_work["CREDIT_MIN"].sum())
    total_str = minutes_to_hhmm(total_minutes)

    return {
        "total_minutes": total_minutes,
        "total_str": total_str,
        "detail_df": df_work
    }

# =========================
# Main App Logic
# =========================

if uploaded_file is None:
    st.info("No file uploaded yet.")
else:
    # Open the uploaded screenshot
    img = Image.open(uploaded_file)

    # OCR
    with st.spinner("Reading your timecard..."):
        raw_text_full = ocr_image_to_text(img)

    # Trim OCR text so we only start at MONTHLY TIME DATA
    raw_text = crop_ocr_to_timecard_section(raw_text_full)

    # Parse OCR text -> structured rows
    df = parse_timecard_lines(raw_text)

    # Compute totals for CREDIT/PAY
    totals = compute_totals(df)

    # Layout: summary on left, table on right
    left_col, right_col = st.columns([1, 2])

    with left_col:
        st.subheader("Total Credit This Bid Period")
        st.metric(
            label="Total CREDIT / PAY Time",
            value=totals["total_str"]
        )
        st.caption("Summed from CREDIT. If CREDIT was blank on a row, PAY was used instead.")

    with right_col:
        st.subheader("Daily Detail")
        st.dataframe(
            df[["DATE", "DES", "NBR", "BLOCK", "SKED", "PAY", "CREDIT"]],
            use_container_width=True,
            hide_index=True
        )

    # Debug / advanced info so we can keep iterating
    with st.expander("Advanced / Debug"):
        st.write("🔎 Raw OCR text (full):")
        st.text(raw_text_full)

        st.write("🔎 Trimmed OCR text (starting at 'MONTHLY TIME DATA'):")
        st.text(raw_text)

        st.write("Parsed DataFrame with computed minutes:")
        st.dataframe(
            totals["detail_df"][[
                "DATE", "DES", "NBR",
                "BLOCK", "SKED", "PAY", "CREDIT",
                "CREDIT_EFFECTIVE", "CREDIT_MIN"
            ]],
            use_container_width=True
        )

        csv_data = df.to_csv(index=False)
        st.download_button(
            "Download parsed CSV",
            csv_data,
            file_name="timecard_parsed.csv",
            mime="text/csv"
        )
