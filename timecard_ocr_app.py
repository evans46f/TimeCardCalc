import streamlit as st
from PIL import Image
import pytesseract
import pandas as pd
import re

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

# ---------- helpers ----------

def hhmm_to_minutes(hhmm: str) -> int:
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
    hours = total_minutes // 60
    mins = total_minutes % 60
    return f"{hours:02d}:{mins:02d}"

def ocr_image_to_text(pil_image: Image.Image) -> str:
    custom_config = r"--psm 6"
    return pytesseract.image_to_string(pil_image, config=custom_config)

def crop_ocr_to_timecard_section(raw_text: str) -> str:
    lines = raw_text.splitlines()
    start_index = 0
    for i, line in enumerate(lines):
        if "MONTHLY TIME DATA" in line.upper():
            start_index = i
            break
    trimmed_lines = lines[start_index:]
    return "\n".join(trimmed_lines)

def clean_line(line: str) -> str:
    """
    Make OCR'd line closer to what we expect.
    - Fix @60CT → 06OCT, @90CT → 09OCT, 110CT → 11OCT, etc.
    - Fix 0CT → OCT (OCR saw zero instead of O)
    - Normalize spacing
    - Strip leading garbage symbols like '@'
    """

    # Kill leading weird symbol(s) like '@' at start of line
    line = line.lstrip("@#*()[]{}<>~|`•-_=+,:;")

    # Replace things like '0CT' with 'OCT'
    # We only do this when it's clearly the month code (OCT)
    # If we see \d{2}0CT or \d{2}OCT both mean OCT.
    # We'll normalize "0CT" => "OCT"
    line = re.sub(r"0CT", "OCT", line, flags=re.IGNORECASE)

    # Sometimes first char of day gets OCR'd as @ instead of digit
    # e.g. "@60CT" should be "06OCT"
    # We'll try patterns like:
    # ^@(\d)OCT  => 0<digit>OCT
    # ^@(\d{2})OCT => <2digits>OCT
    m = re.match(r"^@(\d{1,2})OCT", line, flags=re.IGNORECASE)
    if m:
        day = m.group(1)
        # zero pad day to 2 digits
        if len(day) == 1:
            day = "0" + day
        line = re.sub(r"^@(\d{1,2})OCT", day + "OCT", line, flags=re.IGNORECASE)

    # Another case: "110CT" → "11OCT", "150CT" → "15OCT"
    line = re.sub(r"\b(\d{1,2})0CT\b", r"\1OCT", line, flags=re.IGNORECASE)

    # Collapse multiple spaces/tabs to a single space
    line = re.sub(r"\s+", " ", line).strip()

    return line

def is_date_token(tok: str) -> bool:
    """
    We consider something a DATE token if it looks like DDMMM
    where DD is 1-2 digits, MMM is 3 letters. ex: 06OCT, 6OCT, 15OCT, 19OCT
    """
    return re.match(r"^\d{1,2}[A-Z]{3}$", tok.upper()) is not None

def is_time_token(tok: str) -> bool:
    """
    HH:MM where H or HH and MM are 2 digits.
    """
    return re.match(r"^\d{1,2}:\d{2}$", tok) is not None

def normalize_row(des: str, time_list):
    """
    Reserve vs lineholder mapping.

    Reserve-ish DES: RES, RSV, RESV, RESERVE, SCC
      - BLOCK = ""
      - SKED  = first time
      - PAY   = second time (fallback = first)
      - CREDIT = last time (fallback = first)

    Lineholder-ish:
      - BLOCK = first
      - SKED  = second
      - PAY   = third (fallback=second)
      - CREDIT= fourth (fallback=PAY)
    """
    des_clean = (des or "").upper()
    is_reserve_day = (
        des_clean.startswith("RES") or
        des_clean.startswith("RSV") or
        des_clean == "SCC"
    )

    tvals = [t for t in time_list if t]

    if is_reserve_day:
        block = ""
        sked  = tvals[0] if len(tvals) > 0 else ""
        pay   = tvals[1] if len(tvals) > 1 else sked
        cred  = tvals[-1] if len(tvals) >= 2 else sked
        return block, sked, pay, cred

    # lineholder/flying
    block = tvals[0] if len(tvals) > 0 else ""
    sked  = tvals[1] if len(tvals) > 1 else ""
    pay   = tvals[2] if len(tvals) > 2 else (tvals[1] if len(tvals) > 1 else "")
    cred  = tvals[3] if len(tvals) > 3 else pay
    return block, sked, pay, cred

def parse_timecard_lines(ocr_text: str) -> pd.DataFrame:
    """
    Token-based parser:
    1. Clean each line.
    2. Split into tokens.
    3. Expect tokens like:
        DATE DES NBR <time> <time> <time> ...
       where:
        DATE = DDMMM (e.g. 06OCT, 15OCT)
        DES  = REG/RES/etc.
        NBR  = trip#/code like 3324, SCC, 0991, LOSA, etc.
       The rest that look like HH:MM are time fields.

    We collect those time fields (could be 1, 2, 3, 4, 5...) and feed to normalize_row().
    """

    parsed_rows = []

    # go line by line
    for raw_line in ocr_text.splitlines():
        if not raw_line.strip():
            continue

        line = clean_line(raw_line)

        # skip obvious section headers / totals lines etc.
        # We only want rows that start with a DATE token like 06OCT/15OCT/etc.
        tokens = line.split(" ")

        if len(tokens) < 3:
            continue

        # tokens[0] should be DATE-like
        if not is_date_token(tokens[0]):
            continue

        # after DATE we expect DES then NBR
        # Example:
        # 06OCT RES SCC 1:00 1:00
        # DATE=06OCT DES=RES NBR=SCC times=[1:00,1:00]
        date_tok = tokens[0].upper()
        des_tok  = tokens[1].upper()
        nbr_tok  = tokens[2]

        # remaining tokens that are HH:MM
        time_tokens = [t for t in tokens[3:] if is_time_token(t)]

        block, sked, pay, cred = normalize_row(des_tok, time_tokens)

        parsed_rows.append({
            "DATE": date_tok,
            "DES": des_tok,
            "NBR": nbr_tok,
            "BLOCK": block,
            "SKED": sked,
            "PAY": pay,
            "CREDIT": cred
        })

    if not parsed_rows:
        return pd.DataFrame(columns=["DATE","DES","NBR","BLOCK","SKED","PAY","CREDIT"])

    df = pd.DataFrame(parsed_rows)

    # drop duplicates, defensive
    df = df.drop_duplicates()

    # final tidy
    df = df.reset_index(drop=True)
    return df

def compute_totals(df: pd.DataFrame) -> dict:
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

    # choose CREDIT if present else PAY
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

# ---------- main app ----------

if uploaded_file is None:
    st.info("No file uploaded yet.")
else:
    img = Image.open(uploaded_file)

    with st.spinner("Reading your timecard..."):
        raw_text_full = ocr_image_to_text(img)

    trimmed_text = crop_ocr_to_timecard_section(raw_text_full)

    df = parse_timecard_lines(trimmed_text)
    totals = compute_totals(df)

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

    with st.expander("Advanced / Debug"):
        st.write("🔎 Raw OCR text (full):")
        st.text(raw_text_full)

        st.write("🔎 Trimmed OCR text (starting at 'MONTHLY TIME DATA'):")
        st.text(trimmed_text)

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
