"""Reject incomplete, duplicated or self-relabelled offline control reports."""
from collections import Counter

from ._expected import (EXPECTED_ANSWERS, EXPECTED_CONTROL_IDS,
    EXPECTED_FIXTURE_SHA256, EXPECTED_FROZEN_CASE_IDS,
    EXPECTED_FROZEN_EXPECTATION_COUNT, EXPECTED_STEP_COUNT)


def same_typed_value(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_typed_value(left[k], right[k]) for k in left)
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(same_typed_value(a, b) for a, b in zip(left, right))
    return left == right


def validate_control_report(report):
    rows = report.get("reports", [])
    ids = Counter(row.get("id") for row in rows)
    expected = set(EXPECTED_CONTROL_IDS)
    actual = set(ids)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected, key=str)
    duplicates = sorted((key for key, count in ids.items() if count != 1), key=str)
    wrong = []
    for row in rows:
        id = row.get("id")
        if id not in EXPECTED_ANSWERS:
            continue
        answer = EXPECTED_ANSWERS[id]
        if (row.get("passed") is not True or
                not same_typed_value(row.get("expected"), answer) or
                not same_typed_value(row.get("actual"), answer)):
            wrong.append(id)
    cases = report.get("cases", [])
    case_ids = Counter(case.get("id") for case in cases)
    missing_cases = sorted(set(EXPECTED_FROZEN_CASE_IDS) - set(case_ids))
    unexpected_cases = sorted(set(case_ids) - set(EXPECTED_FROZEN_CASE_IDS), key=str)
    bad_cases = sorted((case.get("id") for case in cases
                        if case.get("status") != "PASS" or case.get("failures")), key=str)
    wrong_counts = []
    counts = {"case_count": len(EXPECTED_FROZEN_CASE_IDS), "pass_count": len(EXPECTED_FROZEN_CASE_IDS),
              "fail_count": 0, "steps_executed": EXPECTED_STEP_COUNT,
              "frozen_expectation_count": EXPECTED_FROZEN_EXPECTATION_COUNT}
    for key, value in counts.items():
        if type(report.get(key)) is not int or report.get(key) != value:
            wrong_counts.append(key)
    if len(rows) != len(EXPECTED_CONTROL_IDS):
        wrong_counts.append("report_count")
    if any(count != 1 for count in case_ids.values()):
        wrong_counts.append("duplicate_case_ids")
    fixture_matched = report.get("sources_sha256", {}).get("fixtures.json") == EXPECTED_FIXTURE_SHA256
    classification = report.get("classification") == "OFFLINE_SYNTHETIC_ONLY"
    accepted = not (missing or unexpected or duplicates or wrong or missing_cases or
                    unexpected_cases or bad_cases or wrong_counts) and fixture_matched and classification
    return {"accepted": accepted, "expected_report_count": len(EXPECTED_CONTROL_IDS),
            "observed_report_count": len(rows), "missing_control_ids": missing,
            "unexpected_control_ids": unexpected, "duplicate_control_ids": duplicates,
            "wrong_answers": wrong, "missing_case_ids": missing_cases,
            "unexpected_case_ids": unexpected_cases, "bad_case_ids": bad_cases,
            "wrong_counts": wrong_counts, "fixture_sha256_matched": fixture_matched}
