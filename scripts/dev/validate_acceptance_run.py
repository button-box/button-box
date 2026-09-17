#!/usr/bin/env python3
"""Validate a sanitized per-unit Button Box acceptance run."""

import argparse
import json
import re
import sys
from pathlib import Path

PASS = "Passed"
ALLOWED = {PASS, "Failed", "Blocked", "Not run", "Inconclusive", "Not fitted"}
CASE_RE = re.compile(r"^\| (BB-[A-Z]+-[0-9]+) \| ([A-Z+]+) \|", re.MULTILINE)


def validate(run, matrix_text, expected_matrix_revision):
    errors = []
    matrix_rows = CASE_RE.findall(matrix_text)
    matrix = dict(matrix_rows)
    if not matrix:
        errors.append("matrix has no acceptance cases")
    if len(matrix) != len(matrix_rows):
        errors.append("matrix contains duplicate cases")
    for field in ("run_id", "unit_id", "matrix_revision", "deployed_code_revision",
                  "tested_code_revision", "unit_configuration_revision",
                  "tested_configuration_revision", "started_at", "tester", "cases"):
        if not run.get(field):
            errors.append(f"missing {field}")
    if run.get("matrix_revision") != expected_matrix_revision:
        errors.append("stale matrix revision")
    if run.get("deployed_code_revision") != run.get("tested_code_revision"):
        errors.append("stale code revision")
    if run.get("unit_configuration_revision") != run.get("tested_configuration_revision"):
        errors.append("stale configuration revision")

    rows = run.get("cases") if isinstance(run.get("cases"), list) else []
    by_id = {}
    for row in rows:
        case_id = row.get("id") if isinstance(row, dict) else None
        if case_id in by_id:
            errors.append(f"duplicate case {case_id}")
        elif case_id:
            by_id[case_id] = row
    for case_id, required in matrix.items():
        row = by_id.get(case_id)
        if not row:
            errors.append(f"missing case {case_id}")
            continue
        status = row.get("status")
        if status not in ALLOWED:
            errors.append(f"{case_id}: invalid status")
            continue
        if status == "Not fitted" and not (
            case_id.startswith("BB-NFC-") and run.get("features", {}).get("nfc") is False
        ):
            errors.append(f"{case_id}: Not fitted is not applicable")
        elif status != PASS and status != "Not fitted":
            errors.append(f"{case_id}: {status}")
        if status == PASS:
            observed = set(row.get("evidence", []))
            missing = set(required.split("+")) - observed
            if missing:
                errors.append(f"{case_id}: missing evidence {','.join(sorted(missing))}")
            if not row.get("evidence_ref"):
                errors.append(f"{case_id}: missing evidence_ref")
    unknown = sorted(set(by_id) - set(matrix))
    if unknown:
        errors.append("unknown cases " + ",".join(unknown))
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--matrix", type=Path, default=Path("docs/box-acceptance.md"))
    parser.add_argument("--expected-matrix-revision", required=True)
    args = parser.parse_args(argv)
    run = json.loads(args.run.read_text(encoding="utf-8"))
    errors = validate(run, args.matrix.read_text(encoding="utf-8"), args.expected_matrix_revision)
    if errors:
        print("NOT READY")
        for error in errors:
            print(f"- {error}")
        return 1
    print("READY FOR DELIVERY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
