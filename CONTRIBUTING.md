# Contributing to Button Box

Button Box is an open-source, screen-free voice messaging device for families.
Contributions are welcome — bug reports, docs fixes, features, and hardware
notes. This file describes the bar for getting a change merged.

## The one rule

**Nothing lands in `main` that isn't tested.** `main` is the branch people
build real boxes from. CI runs `make check` on every PR, and anything that
touches hardware behavior must also be validated on a real box before merge.

## Pull requests

1. Fork the repo, create a branch, open a PR against `main`.
2. Keep PRs small and focused. A 1,300-line PR covering five features will be
   sent back and asked to split — small PRs get reviewed in days, big ones
   stall for weeks.
3. **Tests:** behavior changes need tests. CI must be green before merge,
   no exceptions.
4. **Hardware-touching changes** (button handling, audio path, NFC, install
   scripts, power behavior): say how it was tested on a real box in the PR
   description — box model, software version, what you ran, what happened.
   If you can't test on hardware, say so; a maintainer will add the
   `needs-hardware-test` label and run it on the bench. It merges after that.
5. **Docs-only PRs** (README, guides, comments): fast lane — review and merge,
   no hardware needed.

One maintainer review is required before merge, and review comments must be
resolved. Draft PRs are fine for early feedback — mark them ready when they're
actually ready.

## Issues

Good bug reports get fixed fastest. Include:

- Box model (e.g. Pi 3 A+, Pi 4) and software version/commit
- What you expected vs. what happened
- Steps to reproduce
- Relevant logs (`journalctl`, app output — trim to the interesting part)

Feature requests: describe the problem you're solving, not just the solution.
"If we had X, then Y would work" beats "add X".

## Triage

Issues and PRs are triaged weekly. If yours goes quiet for more than a week,
a polite ping is welcome — things slip, pings don't offend.

## Code of conduct

Be kind. We're building something for families — act like it. Assume good
faith, disagree on the merits, and remember there's a human on the other side
of every issue.
