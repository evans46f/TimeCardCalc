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
            # NOTE: we are intentionally NOT adding res_assign_gslip_mins
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
