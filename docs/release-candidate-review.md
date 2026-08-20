# Release candidate review

Candidate: `2026-08-20-message-box-public-candidate-01`

Source baseline: private `message-box-pi` repository, `origin/main` at
`f5c6329966e689c94dbb190a206a358beb88e1d6`.

## Completed in this pass

- Created a new repository with no imported Git history.
- Imported 63 explicitly allowlisted runtime, setup, service, configuration,
  and test files.
- Excluded 18 tracked private, stale, generated, personalized, or held paths.
- Excluded every untracked and ignored source file by construction.
- Reduced the public environment example to hardware and dashboard settings
  that have active runtime consumers.
- Ran 159 synthetic unit tests successfully.
- Ran a redacted current-tree Gitleaks scan with zero findings.
- Reviewed email-, phone-, and JID-shaped strings; imported matches are test or
  documentation examples, including reserved `555`-range phone fixtures.

## Open blockers

- Self-service recipient selection and NFC enrollment are not complete.
- No clean-card Raspberry Pi 4 physical acceptance has been performed from
  this candidate.
- Onshape enclosure URL, ownership, revision, export procedure, license, and
  validated Pi 4 STL exports are missing.
- Replacement audio with explicit redistribution rights is missing.
- Third-party license inventory, SBOM, release provenance, and vulnerability
  reporting channel are preliminary.
- Ruff, ShellCheck, shfmt, and Biome are selected but not yet pinned and wired
  into the candidate checks.
- Human editorial, PII, and embarrassing-content review is still required.
- This candidate has not been committed, pushed, or published.
