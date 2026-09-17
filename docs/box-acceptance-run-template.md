# Button Box acceptance run

Copy this file for each unit run. Keep private evidence in the operational system; public files contain only sanitized references.

## Run identity

- Unit inventory ID:
- Hostname (do not infer identity from it):
- Hardware revision and fitted options:
- Software commit / PR:
- Configuration revision:
- Started / completed (ISO 8601 with timezone):
- Operator:

## Results

| Case ID | Status | Evidence layer | Timestamp | Sanitized evidence reference | Defect / PR | Notes |
|---|---|---|---|---|---|---|
| BB-HOME-01 | Not run | UI+API | | | | |

Add one row for every case in [the canonical matrix](box-acceptance.md). Use only `Passed`, `Failed`, `Blocked`, `Inconclusive`, `Not run`, or `Not fitted`. Never collapse a partial layer into an unconditional pass.

## Completion accounting

- Passed at every required layer:
- Failed:
- Blocked:
- Inconclusive:
- Not run:
- Not fitted (optional hardware only):
- Acceptance decision: `Accepted` / `Not accepted`
- First unmet required case:
- Follow-up owner and next action:

The totals must equal the number of matrix cases applicable to this unit. `Accepted` requires no failed, blocked, inconclusive, or not-run required case.
