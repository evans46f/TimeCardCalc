import streamlit as st
from PIL import Image
import pytesseract
import pandas as pd
import re

# =========================
# Streamlit config / Header
# =========================
st.set_page_config(
    page_title="Timecard Calculator",
    layout="wide"
)

st.title("🕘 Monthly Time Data Calculator (OCR Beta)")
st.caption("Upload a screenshot of your Monthly Time Data page. We'll OCR it, parse it, and total your credit — locally using Tesseract (no external API).")

uploaded_file = st.file_uploader(
    "Upload screenshot (.png, .jpg, .jpeg)",
    type=["png", "jpg", "jpeg"]
)

# =========================
# Helper functions
# =========================

def hhmm_to_minutes(val: str) -> int:
    """
    Convert '10:30' -> 630.
    Convert '10'   -> 600.
    Convert ''     -> 0.
    """
    if not isinstance(val, str):
        return 0
    val = val.strip()
    if val == "":
        return 0

    # HH:MM form
    m = re.match(r"^(\d{1,2}):(\d{2})$", val)
    if m:
        h = int(m.group(1))
        mins = int(m.group(2))
        return h * 60 + mins

    # Just hours like "10" or "1"
    m2 = re.match(r"^(\d{1,2})$", val)
    if m2:
        h = int(m2.group(1))
        return h * 60

    return 0

def minutes_to_hhmm(total_minutes: int) -> str:
    hours = total_minutes // 60
    mins = total_minutes % 60
    return f"{hours:02d}:{mins:02d}"

def ocr_image_to_text(pil_image: Image.Image) -> str:
    """
    Run Tesseract OCR on the screenshot and return the raw text.
    """
    custom_config = r"--psm 6"
    return pytesseract.image_to_string(pil_image, config=custom_config)

def crop_ocr_to_timecard_section(raw_text: str) -> str:
    """
    Keep only from 'MONTHLY TIME DATA' downward.
    """
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
    Normalize OCR noise in a row that *might* be a duty day line.
    Examples we saw in your OCR:
      @60CT -> 06OCT
      150CT -> 15OCT
      0CT   -> OCT
    Also compress spaces.
    """
    # Strip leading junk chars
    line = line.lstrip("@#*()[]{}<>~|`•-_=+,:;")

    # Normalize "0CT" -> "OCT"
    line = re.sub(r"0CT", "OCT", line, flags=re.IGNORECASE)

    # Handle things like "@60CT", "150CT"
    # Pattern: ^@(\d{1,2})OCT -> <2digit>OCT
    m = re.match(r"^@(\d{1,2})OCT", line, flags=re.IGNORECASE)
    if m:
        day = m.group(1)
        if len(day) == 1:
            day = "0" + day
        line = re.sub(r"^@(\d{1,2})OCT", day + "OCT", line, flags=re.IGNORECASE)

    # Handle "150CT" -> "15OCT", "190CT" -> "19OCT"
    line = re.sub(r"\b(\d{1,2})0CT\b", r"\1OCT", line, flags=re.IGNORECASE)

    # Squeeze multiple whitespace
    line = re.sub(r"\s+", " ", line).strip()
    return line

def is_date_token(tok: str) -> bool:
    """
    We consider DATE tokens like 06OCT, 6OCT, 15OCT, 19OCT
    (1-2 digits followed by 3 letters).
    """
    return re.match(r"^\d{1,2}[A-Z]{3}$", tok.upper()) is not None

def is_time_token(tok: str) -> bool:
    """
    Accept HH:MM or HH (hours only).
    """
    return (
        re.match(r"^\d{1,2}:\d{2}$", tok) is not None or
        re.match(r"^\d{1,2}$", tok) is not None
    )

def normalize_row(des: str, times_list):
    """
    Reserve vs lineholder mapping.

    For RES / RSV / SCC-style days:
      - BLOCK      = "" (we don't really use it for credit math)
      - SKED       = first time on the row (tvals[0] if it exists)
      - PAY / CREDIT_MAIN = second time on the row if present,
                            else first time if there's only one.
      - EXTRA_PAY  = any remaining times after that (PAY ONLY, etc.)
                     e.g. 0:07, 1:22

      We'll return CREDIT_MAIN separately from EXTRA_PAY.

    For REG/flying style (if we ever see it later):
      - BLOCK = first
      - SKED  = second
      - PAY   = third (fallback=second)
      - CREDIT_MAIN = fourth (fallback=PAY)
      - EXTRA_PAY   = anything beyond that
    """
    des_clean = (des or "").upper()
    is_reserve_day = (
        des_clean.startswith("RES") or
        des_clean.startswith("RSV") or
        des_clean == "SCC"
    )

    # Only keep non-empty times
    tvals = [t for t in times_list if t]

    if is_reserve_day:
        sked = tvals[0] if len(tvals) > 0 else ""
        # main credited pay for reserve is usually the 2nd time,
        # fallback = 1st if only 1
        if len(tvals) > 1:
            main_credit = tvals[1]
            extras = tvals[2:]  # PAY ONLY fragments etc.
        elif len(tvals) == 1:
            main_credit = tvals[0]
            extras = []
        else:
            main_credit = ""
            extras = []

        return {
            "BLOCK": "",
            "SKED": sked,
            "PAY": main_credit,
            "CREDIT_MAIN": main_credit,
            "EXTRA_PAY_LIST": extras,
        }

    # lineholder / REG (not in your sample yet, but let's keep support)
    block = tvals[0] if len(tvals) > 0 else ""
    sked  = tvals[1] if len(tvals) > 1 else ""
    pay   = tvals[2] if len(tvals) > 2 else (tvals[1] if len(tvals) > 1 else "")
    credit_main = tvals[3] if len(tvals) > 3 else pay
    extras = tvals[4:] if len(tvals) > 4 else []

    return {
        "BLOCK": block,
        "SKED": sked,
        "PAY": pay,
        "CREDIT_MAIN": credit_main,
        "EXTRA_PAY_LIST": extras,
    }

def parse_timecard_lines(ocr_text: str) -> pd.DataFrame:
    """
    Parse the daily table section into rows.
    Each row looks like:
        06OCT RES SCC 1:00 1:00
        15OCT RES 5999 5:14 10:30 10:30 10:30 0:07
    Logic:
      DATE = token[0]
      DES  = token[1]
      NBR  = token[2]
      TIMES = rest of tokens that look like HH:MM or HH
    """
    parsed_rows = []

    for raw_line in ocr_text.splitlines():
        if not raw_line.strip():
            continue

        line = clean_line(raw_line)
        tokens = line.split(" ")
        if len(tokens) < 3:
            continue

        # must start with a date-like token (06OCT, 15OCT...)
        if not is_date_token(tokens[0]):
            continue

        date_tok = tokens[0].upper()
        des_tok  = tokens[1].upper()
        nbr_tok  = tokens[2]

        # The rest that are numeric-ish time values
        time_tokens = [t for t in tokens[3:] if is_time_token(t)]

        mapped = normalize_row(des_tok, time_tokens)

        # Flatten extras into a comma-separated string just for display
        extra_pay = ",".join(mapped["EXTRA_PAY_LIST"]) if mapped["EXTRA_PAY_LIST"] else ""

        parsed_rows.append({
            "DATE": date_tok,
            "DES": des_tok,
            "NBR": nbr_tok,
            "BLOCK": mapped["BLOCK"],
            "SKED": mapped["SKED"],
            "PAY_MAIN": mapped["PAY"],
            "CREDIT_MAIN": mapped["CREDIT_MAIN"],
            "EXTRA_PAY": extra_pay,
        })

    if not parsed_rows:
        return pd.DataFrame(columns=[
            "DATE","DES","NBR","BLOCK","SKED","PAY_MAIN","CREDIT_MAIN","EXTRA_PAY"
        ])

    df = pd.DataFrame(parsed_rows)
    df = df.drop_duplicates().reset_index(drop=True)
    return df

def sum_daylevel_minutes(df: pd.DataFrame) -> (int, int):
    """
    Returns (base_minutes, extras_minutes)

    base_minutes = sum of CREDIT_MAIN across rows
    extras_minutes = sum of EXTRA_PAY tokens across rows (PAY ONLY fragments)
    """
    if df.empty:
        return 0, 0

    # base (CREDIT_MAIN)
    base_minutes = df["CREDIT_MAIN"].apply(hhmm_to_minutes).sum()

    # extras
    extras_total = 0
    for extra in df["EXTRA_PAY"]:
        if not extra:
            continue
        # "0:07,1:22" etc.
        for piece in extra.split(","):
            extras_total += hhmm_to_minutes(piece.strip())

    return int(base_minutes), int(extras_total)

def parse_summary_block(ocr_text: str) -> list:
    """
    Pull additional credit buckets from the summary block under the table.
    We look for lines like:
        "CREDIT APPLICABLE TO REG G/S SLIP PAY: 57:34"
        "RES ASSIGN-G/SLIP PAY: 10:30"
        "REROUTE PAY: 10:30"
    We'll return a list of those time values as strings.
    """
    summary_values = []

    # The summary block is usually after the daily lines, starting around
    # "RES OTHER SUB TTL..." etc. We'll just scan the entire ocr_text.
    lines = ocr_text.splitlines()
    for line in lines:
        # Normalize whitespace
        cleaned = re.sub(r"\s+", " ", line.strip())

        # Look for patterns like ": 57:34" or ": 10:30" at end of line
        m = re.search(r":\s*([0-9]{1,2}(?::[0-9]{2})?)\s*$", cleaned)
        if m:
            val = m.group(1)
            # sanity check it looks like hours or hh:mm
            if is_time_token(val):
                summary_values.append(val)

    return summary_values

def compute_grand_totals(df: pd.DataFrame, trimmed_text: str) -> dict:
    """
    Compute 3 things:
      - day_base_minutes     (sum CREDIT_MAIN per day)
      - day_extras_minutes   (sum PAY ONLY pieces per day)
      - summary_minutes      (sum items like 57:34, 10:30, etc. in summary block)

    Then:
      displayed_total_minutes = day_base + day_extras + summary

    We'll surface all three so we can show debug later.
    """
    day_base, day_extras = sum_daylevel_minutes(df)

    summary_vals = parse_summary_block(trimmed_text)
    summary_minutes = sum(hhmm_to_minutes(v) for v in summary_vals)

    total_minutes = day_base + day_extras + summary_minutes

    return {
        "day_base_minutes": day_base,
        "day_extras_minutes": day_extras,
        "summary_minutes": summary_minutes,
        "total_minutes": total_minutes,
        "total_str": minutes_to_hhmm(total_minutes),
        "summary_vals": summary_vals
    }

# =========================
# Main App Logic
# =========================

if uploaded_file is None:
    st.info("No file uploaded yet.")
else:
    img = Image.open(uploaded_file)

    with st.spinner("Reading your timecard..."):
        raw_text_full = ocr_image_to_text(img)

    trimmed_text = crop_ocr_to_timecard_section(raw_text_full)

    # Parse day rows
    df = parse_timecard_lines(trimmed_text)

    # Compute totals
    totals = compute_grand_totals(df, trimmed_text)

    # --- UI layout ---
    left_col, right_col = st.columns([1, 2])

    with left_col:
        st.subheader("Total CREDIT (Delta-style)")
        st.metric(
            label="Grand Total Credit/Pay",
            value=totals["total_str"]
        )

        st.caption(
            "Matches the full credit picture, including reserve credit, reroute pay, "
            "assignment pay, and pay-only add-ons."
        )

        # breakdown for you (pilots like receipts)
        with st.container(border=True):
            st.write("Breakdown (HH:MM):")
            st.write(f"- Day credit sum: {minutes_to_hhmm(totals['day_base_minutes'])}")
            st.write(f"- Day pay-only extras: {minutes_to_hhmm(totals['day_extras_minutes'])}")
            st.write(f"- Summary block adds: {minutes_to_hhmm(totals['summary_minutes'])}")

    with right_col:
        st.subheader("Daily Detail (Parsed)")
        st.dataframe(
            df[["DATE", "DES", "NBR", "BLOCK", "SKED", "PAY_MAIN", "CREDIT_MAIN", "EXTRA_PAY"]],
            use_container_width=True,
            hide_index=True
        )

    # Debug / Advanced
    with st.expander("Advanced / Debug"):
        st.write("🔎 Raw OCR text (full):")
        st.text(raw_text_full)

        st.write("🔎 Trimmed OCR text (from 'MONTHLY TIME DATA'):")
        st.text(trimmed_text)

        st.write("Parsed Daily DataFrame (internal):")
        st.dataframe(
            df,
            use_container_width=True
        )

        st.write("Summary values captured from block:")
        st.write(totals["summary_vals"])

        st.write("Minutes math:")
        st.json({
            "day_base_minutes": totals["day_base_minutes"],
            "day_extras_minutes": totals["day_extras_minutes"],
            "summary_minutes": totals["summary_minutes"],
            "grand_total_minutes": totals["total_minutes"],
            "grand_total_hhmm": totals["total_str"]
        })

        csv_data = df.to_csv(index=False)
        st.download_button(
            "Download parsed CSV",
            csv_data,
            file_name="timecard_parsed.csv",
            mime="text/csv"
        )
