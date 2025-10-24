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
st.caption(
    "Upload a screenshot of your Monthly Time Data page. "
    "We'll OCR it, parse it, and total your credit — locally using Tesseract (no external API)."
)

uploaded_file = st.file_uploader(
    "Upload screenshot (.png, .jpg, .jpeg)",
    type=["png", "jpg", "jpeg"]
)

# =========================
# Helper functions
# =========================

def hhmm_to_minutes(val: str) -> int:
    """
    Convert strings like:
      '10:30' -> 630
      '10'    -> 600
      ''      -> 0
    Anything else -> 0
    """
    if not isinstance(val, str):
        return 0
    val = val.strip()
    if val == "":
        return 0

    # HH:MM
    m = re.match(r"^(\d{1,2}):(\d{2})$", val)
    if m:
        h = int(m.group(1))
        mins = int(m.group(2))
        return h * 60 + mins

    # Just hours like "10" or "1"
    m2 = re.match(r"^(\d{1,2})$", val)
    if m2:
        h = int(m2)
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
    Keep only text starting at 'MONTHLY TIME DATA'
    so we ignore page header junk.
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
    Normalize OCR noise in possible duty rows.
    Examples observed:
      @60CT -> 06OCT
      150CT -> 15OCT
      0CT   -> OCT
    Also squeeze whitespace.
    """
    # strip leading junk chars
    line = line.lstrip("@#*()[]{}<>~|`•-_=+,:;")

    # fix '0CT' -> 'OCT' where OCR used zero instead of O
    line = re.sub(r"0CT", "OCT", line, flags=re.IGNORECASE)

    # fix '@60CT' -> '06OCT', '@90CT' -> '09OCT'
    m = re.match(r"^@(\d{1,2})OCT", line, flags=re.IGNORECASE)
    if m:
        day = m.group(1)
        if len(day) == 1:
            day = "0" + day
        line = re.sub(r"^@(\d{1,2})OCT", day + "OCT", line, flags=re.IGNORECASE)

    # fix '150CT' -> '15OCT', '190CT' -> '19OCT'
    line = re.sub(r"\b(\d{1,2})0CT\b", r"\1OCT", line, flags=re.IGNORECASE)

    # squeeze whitespace
    line = re.sub(r"\s+", " ", line).strip()
    return line


def is_date_token(tok: str) -> bool:
    """
    DATE token looks like '06OCT', '6OCT', '15OCT', etc.
    = 1-2 digits + 3 letters
    """
    return re.match(r"^\d{1,2}[A-Z]{3}$", tok.upper()) is not None


def is_time_token(tok: str) -> bool:
    """
    Accept either:
      HH:MM  (10:30)
      HH     (10)
    """
    return (
        re.match(r"^\d{1,2}:\d{2}$", tok) is not None or
        re.match(r"^\d{1,2}$", tok) is not None
    )


def normalize_row(des: str, times_list):
    """
    Parse a single duty day row into structured fields.

    For RES / RSV / SCC-style days (reserve days):
      tvals[0]     ~ SKED / duty-ish number (1:00, 5:14, etc.)
      tvals[1]     ~ MAIN CREDIT (10:30, 7:21, etc.)
      tvals[2..n]  ~ often repeats of MAIN CREDIT, then tiny 'PAY ONLY'
                     add-ons like 0:07 or 1:22.

    We want:
      - BLOCK: "" for reserve days (block time isn't really used here)
      - SKED: the first time if present
      - PAY_MAIN: the main credit time for that day
      - CREDIT_MAIN: same as PAY_MAIN (this is what we sum as base credit)
      - EXTRA_PAY_LIST: ONLY the true scraps (0:07, 1:22), not repeats
                        of PAY_MAIN.

    For REG / lineholder (not in your screenshot yet), we fallback
    to normal column order: BLOCK, SKED, PAY, CREDIT_MAIN, and treat
    anything beyond that as extras, excluding duplicates.
    """
    des_clean = (des or "").upper()
    is_reserve_day = (
        des_clean.startswith("RES") or
        des_clean.startswith("RSV") or
        des_clean == "SCC"
    )

    # only keep plausible time tokens
    tvals = [t for t in times_list if t]

    if is_reserve_day:
        sked = tvals[0] if len(tvals) > 0 else ""

        if len(tvals) > 1:
            main_credit = tvals[1]
        elif len(tvals) == 1:
            main_credit = tvals[0]
        else:
            main_credit = ""

        # extras:
        # take everything after index 2 (or none if <3 tokens)
        # remove duplicates of main_credit
        # remove duplicates we've already kept
        if len(tvals) > 2:
            extras_raw = tvals[2:]
        else:
            extras_raw = []

        extras_filtered = []
        for val in extras_raw:
            if val != main_credit:
                if val not in extras_filtered:
                    extras_filtered.append(val)

        return {
            "BLOCK": "",
            "SKED": sked,
            "PAY_MAIN": main_credit,
            "CREDIT_MAIN": main_credit,
            "EXTRA_PAY_LIST": extras_filtered,
        }

    # REG / lineholder fallback
    block = tvals[0] if len(tvals) > 0 else ""
    sked  = tvals[1] if len(tvals) > 1 else ""
    pay   = tvals[2] if len(tvals) > 2 else (tvals[1] if len(tvals) > 1 else "")
    credit_main = tvals[3] if len(tvals) > 3 else pay

    extras_filtered = []
    if len(tvals) > 4:
        for val in tvals[4:]:
            if val != credit_main and val not in extras_filtered:
                extras_filtered.append(val)

    return {
        "BLOCK": block,
        "SKED": sked,
        "PAY_MAIN": pay,
        "CREDIT_MAIN": credit_main,
        "EXTRA_PAY_LIST": extras_filtered,
    }


def parse_timecard_lines(ocr_text: str) -> pd.DataFrame:
    """
    Parse the daily table section into rows:
      06OCT RES SCC 1:00 1:00
      15OCT RES 5999 5:14 10:30 10:30 10:30 0:07
      ...

    Logic:
      tokens[0] = DATE
      tokens[1] = DES
      tokens[2] = NBR (rot number / code)
      tokens[3:] = time-ish values (HH:MM or HH)
    """
    parsed_rows = []

    for raw_line in ocr_text.splitlines():
        if not raw_line.strip():
            continue

        line = clean_line(raw_line)
        tokens = line.split(" ")
        if len(tokens) < 3:
            continue

        # must start with a recognizable date (06OCT, 15OCT...)
        if not is_date_token(tokens[0]):
            continue

        date_tok = tokens[0].upper()
        des_tok  = tokens[1].upper()
        nbr_tok  = tokens[2]

        # gather time tokens
        time_tokens = [t for t in tokens[3:] if is_time_token(t)]

        mapped = normalize_row(des_tok, time_tokens)

        # flatten extras to a comma string for storage
        extra_pay_str = ",".join(mapped["EXTRA_PAY_LIST"]) if mapped["EXTRA_PAY_LIST"] else ""

        parsed_rows.append({
            "DATE": date_tok,
            "DES": des_tok,
            "NBR": nbr_tok,
            "BLOCK": mapped["BLOCK"],
            "SKED": mapped["SKED"],
            "PAY_MAIN": mapped["PAY_MAIN"],
            "CREDIT_MAIN": mapped["CREDIT_MAIN"],
            "EXTRA_PAY": extra_pay_str,
        })

    if not parsed_rows:
        return pd.DataFrame(columns=[
            "DATE","DES","NBR","BLOCK","SKED","PAY_MAIN","CREDIT_MAIN","EXTRA_PAY"
        ])

    df = pd.DataFrame(parsed_rows).drop_duplicates().reset_index(drop=True)
    return df


def sum_daylevel_minutes(df: pd.DataFrame) -> (int, int):
    """
    Returns (day_base_minutes, day_extras_minutes)

    day_base_minutes:
        sum of CREDIT_MAIN for each row
    day_extras_minutes:
        sum of EXTRA_PAY scraps (like 0:07, 1:22),
        NOT counting repeated main credit values
    """
    if df.empty:
        return 0, 0

    # base = sum of CREDIT_MAIN
    base_minutes = df["CREDIT_MAIN"].apply(hhmm_to_minutes).sum()

    # extras = sum of EXTRA_PAY tokens per row
    extras_total = 0
    for _, row in df.iterrows():
        extra_field = row.get("EXTRA_PAY", "")
        if not extra_field:
            continue
        for piece in extra_field.split(","):
            extras_total += hhmm_to_minutes(piece.strip())

    return int(base_minutes), int(extras_total)


def parse_summary_block(ocr_text: str) -> list:
    """
    ONLY pull add-on buckets we believe should stack on top of the daily credit.
    From your OCR block, the ones that matter are:
      - 'RES ASSIGN-G/SLIP PAY: 10:30'
      - 'REROUTE PAY: 10:30'

    We IGNORE:
      - CREDIT APPLICABLE TO REG G/S SLIP PAY: 57:34  (guarantee math)
      - REG G/S TRIGGER: 72:00 (threshold, not credit)
      - BANK / VAC / LOOK BACK GUAR / PBS/PR PAY / etc.
      - G/SLIP PAY: 0:00
      - ASSIGN PAY: 0:00

    We'll scan lines and if they start with one of our trusted labels,
    we'll grab that HH:MM or HH value.
    """
    adds = []

    for raw_line in ocr_text.splitlines():
        line = re.sub(r"\s+", " ", raw_line.strip())

        # RES ASSIGN-G/SLIP PAY
        if line.upper().startswith("RES ASSIGN-G/SLIP PAY"):
            m = re.search(
                r"RES ASSIGN-G/SLIP PAY:\s*([0-9]{1,2}(?::[0-9]{2})?)",
                line,
                flags=re.I
            )
            if m:
                adds.append(m.group(1))

        # REROUTE PAY
        if line.upper().startswith("REROUTE PAY"):
            m = re.search(
                r"REROUTE PAY:\s*([0-9]{1,2}(?::[0-9]{2})?)",
                line,
                flags=re.I
            )
            if m:
                adds.append(m.group(1))

    return adds


def compute_grand_totals(df: pd.DataFrame, trimmed_text: str) -> dict:
    """
    Compute:
      - day_base_minutes     (sum CREDIT_MAIN per row)
      - day_extras_minutes   (sum EXTRA_PAY scraps per row)
      - summary_minutes      (sum of trusted summary add-ons like REROUTE PAY)
      - total_minutes        = all of the above

    We also return strings for display + debug.
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
    # Load screenshot
    img = Image.open(uploaded_file)

    with st.spinner("Reading your timecard..."):
        raw_text_full = ocr_image_to_text(img)

    trimmed_text = crop_ocr_to_timecard_section(raw_text_full)

    # Parse daily rows
    df = parse_timecard_lines(trimmed_text)

    # Totals and breakdown
    totals = compute_grand_totals(df, trimmed_text)

    # --- UI layout ---
    left_col, right_col = st.columns([1, 2])

    with left_col:
        st.subheader("Total CREDIT (Composite)")
        st.metric(
            label="Grand Total Credit/Pay",
            value=totals["total_str"]
        )

        st.caption(
            "This total = daily credit + pay-only scraps + specific reroute/assign pay buckets. "
            "No bank/vacuum/trigger numbers, and no double-counted repeats."
        )

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
        st.dataframe(df, use_container_width=True)

        st.write("Summary values captured from block (whitelisted):")
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
