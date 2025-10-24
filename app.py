def parse_duty_rows(raw: str) -> List[Dict[str, Any]]:
    """
    Parse each duty-day row and extract:
      - main_pay_candidate  -> PAY TIME ONLY (PAY NO CREDIT)
      - bump_pay_candidate  -> ADDTL PAY ONLY COLUMN
      - metadata flags (credit block, trans)

    MAIN PAY LOGIC (PAY TIME ONLY):

      Rule A: If NBR includes 'RRPY' (or 'ADJ-RRPY', etc.),
              then the last H:MM on that row is PAY TIME ONLY.
              (Works for RES and REG.)

      Rule B: Otherwise, RES rows can generate PAY TIME ONLY if ALL are true:
        - there's NO pairing credit block (10:30 10:30 10:30 ...)
        - ALL the H:MM times on that row are identical
          (e.g. "15:00 15:00", "1:00 1:00", "6:33 6:33", "32:05 32:05")
        - NBR is NOT "SICK"
        - NBR is NOT "TOFF"
        - the row does NOT contain the word "TRANS"
        This covers SCC / LOSA / PVEL / 20WD / 4F1C / VAC / etc.
        We EXCLUDE SICK, TOFF, TRANS because those shouldn't stack.

      Rule C: For LINEHOLDER-style REG rows:
        If the row is REG,
        AND it's basically a pure standalone pay value (like "10:00" by itself),
        AND there's exactly one time on that row,
        AND it's not a TRANS row,
        then count that one time as PAY TIME ONLY.
        This captures things like "07OCT REG 44WD 10:00".

      We do NOT grab pairing-credit values like 15:45 or 26:15; those are already
      baked into SUB TTL CREDIT or handled elsewhere.

    BUMP PAY LOGIC (ADDTL PAY ONLY COLUMN):
      We take the last time on the row as bump pay ONLY IF:
        - there are at least 2 times on the row
        - the last time is strictly smaller than the one before it
        - AND the last time is not equal to the first time on the row
          (so we don't treat "8:03 ... 8:03" as add-on pay).
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

        # Pull all H:MM in this row
        times = re.findall(r"\b\d{1,3}:[0-5]\d\b", seg_full)

        # Does the row mention TRANS?
        has_trans = "TRANS" in tail_text.upper()

        # Detect a 'pairing credit block':
        # ... 10:30 10:30 10:30 0:07
        has_credit_block = False
        if len(times) >= 4:
            before_last = times[:-1]
            last3 = before_last[-3:]
            if len(last3) == 3 and len(set(last3)) == 1:
                has_credit_block = True

        # bump pay candidate (ADDTL PAY ONLY COLUMN):
        bump_pay_candidate = None
        if len(times) >= 2:
            prev_time = times[-2]
            last_time = times[-1]
            prev_m = to_minutes(prev_time)
            last_m = to_minutes(last_time)
            # only count if smaller and not just repeating the first time
            if last_m < prev_m and last_time != times[0]:
                bump_pay_candidate = last_time

        # main pay candidate (PAY TIME ONLY):
        main_pay_candidate = None

        # Rule A: any RRPY variant => last time is pay
        if "RRPY" in nbr:
            if times:
                main_pay_candidate = times[-1]

        # Rule B: RES flat-pay style rows (SCC/LOSA/etc.), with exclusions
        if main_pay_candidate is None:
            if duty == "RES" and times and not has_credit_block:
                unique_times = set(times)
                if (
                    len(unique_times) == 1
                    and nbr not in ("SICK", "TOFF")
                    and not has_trans
                ):
                    main_pay_candidate = times[-1]

        # Rule C: REG single-pay rows (lineholder add-on like "10:00")
        if main_pay_candidate is None:
            if duty == "REG" and times and not has_trans:
                # only if it's literally a single time value on that line
                if len(times) == 1:
                    main_pay_candidate = times[0]

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
