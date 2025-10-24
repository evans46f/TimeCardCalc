def compute_components(raw: str) -> Dict[str, Any]:
    rows = parse_duty_rows(raw)
    card_type = detect_card_type(rows)

    sub_ttl_credit_mins, final_after_award_mins, sub_src = grab_sub_ttl_credit_minutes(raw)

    # TTL BANK OPTS AWARD is the difference between final_after_award and sub_ttl
    ttl_bank_opts_award_mins = max(0, final_after_award_mins - sub_ttl_credit_mins)

    pay_only_main_mins = 0
    pay_only_bump_mins = 0
    debug_rows = []

    for r in rows:
        add_main = to_minutes(r["main_pay_candidate"]) if r["main_pay_candidate"] else 0
        add_bump = to_minutes(r["bump_pay_candidate"]) if r["bump_pay_candidate"] else 0
        pay_only_main_mins += add_main
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

    reroute_pay_mins      = extract_named_bucket(raw, ["REROUTE PAY"])
    assign_pay_mins       = extract_named_bucket(raw, ["ASSIGN PAY"])
    g_slip_pay_mins       = extract_named_bucket(raw, ["G/SLIP PAY", "G SLIP PAY"])
    res_assign_gslip_mins = extract_res_assign_gslip_bucket(raw)
    bank_dep_award_mins   = extract_numeric_field(raw, r"BANK\s+DEP\s+AWARD")
    training_pay_mins     = extract_training_pay_minutes(raw)

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

        # now robustly captured:
        "ttl_bank_opts_award_mins": ttl_bank_opts_award_mins,

        "training_pay_mins": training_pay_mins,

        "debug_rows": debug_rows,
    }
