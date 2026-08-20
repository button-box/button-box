# Promoting private development into the clean candidate

## Recommendation

Treat the private device repository as the development upstream and this clean
repository as a downstream release-integration repository. Do not merge their
Git histories, add the private repository as a permanent remote, or mirror all
branches.

Every promotion should:

1. Land and test the change in the private repository first.
2. Choose an exact new source commit.
3. Compare the previous and new source commits only across
   `release/include-paths.txt`.
4. Apply the reviewed source changes to a candidate branch, reconciling any
   public-only sanitization or documentation by hand.
5. Re-run tests, secret scanning, PII review, and license review.
6. Update `release/provenance.md` with the new source commit and candidate
   review result.
7. Credit source authors in the candidate commit message without importing the
   private history.

Example comparison in the private checkout:

```sh
git diff --name-status OLD_SOURCE_SHA NEW_SOURCE_SHA -- $(cat /path/to/candidate/release/include-paths.txt)
git diff OLD_SOURCE_SHA NEW_SOURCE_SHA -- $(cat /path/to/candidate/release/include-paths.txt)
```

Do not automatically accept a newly added upstream path. Add it to the source
manifest only after reviewing its purpose, privacy, security, licensing, and
release relevance.

Once the public repository becomes the primary development home, reverse the
relationship: develop public functionality there and keep private deployment
configuration in a separate private layer.
