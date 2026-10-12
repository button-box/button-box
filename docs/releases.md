# Signed public releases

Release tooling builds program files and bundled sounds from an existing tag.
It does not install software on a Pi. Runtime discovery, anti-downgrade state,
physical approval, transaction health, and recovery are separate updater work.
The wacli runtime is unchanged by these tools.

## Identity and ordering

Stable tags are `v0.3.0`, `v0.3.1`, and so on. Canary tags are
`v0.3.0-canary.1`, `v0.3.0-canary.2`, and so on; their GitHub releases must be
prereleases. Tags must already exist and must never move after release. Branches,
raw commits, earlier tag formats, and dirty source checkouts are refused.

Allocate a positive integer `release_sequence` strictly greater than every
previously signed sequence across **both** channels, including withdrawn releases
and signed candidates that left the signing computer. Reserve it in the existing
release record before signing; never reuse it. Tag sorting and version-string
comparison do not establish update order. The offline human signer reviews the
sequence history; the build script validates the integer but cannot establish
global monotonicity without that history. Devices must persist their accepted
sequence floor outside application rollback.

The schema-1 manifest uses product `button-box`, the exact tag in `tag` and
`version`, the full 40-character commit, sequence, `stable` or `canary` channel,
`min_updater_version` (major.minor.patch), `supported_os`, `supported_arch`,
`supported_models`, and the existing `files` source/destination/SHA-256 mapping.
The declared target is Raspberry Pi OS Lite 64-bit based on Debian 13
(`raspios-trixie`, `aarch64`), with `Raspberry Pi 3 Model A Plus` and
`Raspberry Pi 4 Model B` model identifiers. Inclusion is a packaging contract;
Pi 3 A+ memory, timing and hardware acceptance remains required before deployment
or advertising physical support.

The packaged `VERSION` and `release.json.version` are generated as the exact tag,
even when the source's development VERSION differs. The source checkout remains
unchanged. `release.json` also contains commit and manifest SHA-256. The existing
bounded updater records installed identity separately; this tooling does not
change heartbeat protocol `version` or install VERSION on existing boxes.

## Build and review

Use a reviewed, clean checkout containing these tools and an existing release
tag containing `scripts/release/payload.txt`. Python 3.9+, Git, gzip, make, uv,
and Bun are needed; use the repository's pinned check tools. Create a tag only
through the separately authorized release process. The builder creates no tags,
commits, pushes, or releases. For example, with an already existing tag:

```sh
python3 scripts/release/build.py v0.3.0-canary.1 \
  --release-sequence 1 --min-updater-version 1.0.0 \
  --output /tmp/button-box-v0.3.0-canary.1
```

Choose the real sequence and minimum controller version; the example values are
not approval of an updater version. Output must be a new directory outside the
source checkout. Both annotated and lightweight tags are supported. The tag's
object and peeled commit are checked throughout the build, including after both
builds. Each build uses a separate clean detached clone, runs both
`MSGBOX_TEST_TRANSPORT=cloud make check` and
`MSGBOX_TEST_TRANSPORT=wacli make check`, and rejects changes made by checks.
Ignored tool caches are never included.

`scripts/release/payload.txt` lists each permitted tracked regular file explicitly,
without directory wildcards. It includes the installed-file mapping, the bounded
updater and manifest generator needed by the legacy operator path, license and QR
provenance. No setup/provisioning scripts, Git metadata, device configuration,
user state, logs, test reports, or arbitrary migrations are packaged. New installed
files must be added deliberately to this list. New public keys must also be added
explicitly when a bridge/bootstrap release needs to carry them.

The tar archive has sorted file entries, uid/gid zero, empty owner/group names,
0644/0755 modes determined from tracked Git modes, and the commit timestamp for
all entries. It uses USTAR and `gzip -n -9` without filename or wall-clock gzip
metadata. Generated identity files have mode 0644. Two independent builds must
match every artifact hash before any output is copied. Repeat builds with the
same tag, sequence, minimum version, Python and gzip toolchain produce identical
bytes; compression across different gzip implementations is not promised.

The output contains exactly:

- `button-box-<tag>.tar.gz`
- `release-manifest.json` (also embedded byte-for-byte in the tarball)
- `SHA256SUMS`: two sorted `64 lowercase hex digits`, two spaces, basename,
  newline entries, covering the tarball and external manifest

Keep build logs private unless they have received the same publication review.
A deterministic archive is not proof that its content is safe to share.

## Offline human signing

Use a dedicated **password-encrypted minisign key on an offline signing
computer**. The human signer generates it outside the repository, with encrypted
copies on two removable drives and the passphrase stored separately. Never put a
secret key or passphrase in this repository, release assets, shell environment,
CI, GitHub secrets, an agent environment, or a Pi. A USB key attached to an online
computer does not provide offline custody. This tooling generates no keys and
cannot prove the computer is offline.

Commit only the independently verified public key under
`config/release-keys/<name>.pub` through the authorized source-review process.
Copy the reviewed tools, public pin, and three unsigned assets to the offline
computer. Independently rebuild there with the same inputs where possible, or
inspect all artifact bytes, hashes, installed mapping and release metadata.
Confirm the sequence history and compatibility before signing.

```sh
scripts/release/sign.sh /tmp/button-box-v0.3.0-canary.1 \
  --secret-key /media/offline-signing/button-box.key \
  --public-key config/release-keys/release.pub
```

The key path is a required operator argument. Minisign prompts for its password;
there is no environment or CI key interface. The wrapper refuses CI, keys inside
the repository/artifacts, unencrypted secret-key format and existing signatures.
It signs the exact `SHA256SUMS` once with `minisign -S`, then verifies it with
`minisign -V -H` and checks both hashes, archive identity and installed-file hashes.
The signature is `SHA256SUMS.minisig`. See the [minisign manual](https://jedisct1.github.io/minisign/).

Return only the four release assets to the connected release computer. Verify
again before any upload, using the trusted public pin, never a key fetched from
the candidate release:

```sh
scripts/release/verify.sh /tmp/button-box-v0.3.0-canary.1 \
  --public-key config/release-keys/release.pub
```

Authenticate SHA256SUMS before trusting its filenames or extracting anything.
Then check the tarball and manifest hashes. An unsigned checksum is insufficient.

## Manual gate and draft upload

Write purpose-made plain-English notes in a file: changes, compatibility, known
limitations, exact release and compare links. Exclude internal discussion,
private company/customer/family content, identifiers and credentials. Use
`https://github.com/button-box/button-box/releases/tag/<tag>` and
`https://github.com/button-box/button-box/compare/<previous-tag>...<tag>`.

Before every GitHub upload, follow the applicable public-output privacy policy.
Inspect the actual public destination and every outgoing surface: archive entries
and contents, binary/audio content and metadata, manifest, sums, signature comments,
notes, title/tag/commit, build logs if shared, and GitHub's automatic source archives
of the **entire tagged tree**. The payload allowlist does not sanitize GitHub's
source archives or history. Run pattern scans and semantic review, sanitize any
finding, rebuild/re-sign changed artifacts, and recheck. Stop on uncertainty or
an unreviewed surface. Draft releases are externally shared too.

```sh
scripts/release/publish.sh /tmp/button-box-v0.3.0-canary.1 \
  --public-key config/release-keys/release.pub \
  --notes /tmp/release-notes.md \
  --receipt /tmp/private-release-review.json
```

Use a private receipt location (parent directory restricted to the operator),
outside the repository and release directory. The script snapshots the four
assets and notes, verifies the signature before hashes and archive inspection,
checks the GitHub repository's actual public audience and existing remote tag
commit, and performs a bounded privacy pattern scan. It prints the archive listing,
release identity and notes, then requires the human to type
`CREATE DRAFT <tag>` in an interactive terminal after completing all reviews.
There is no skip/yes/auto-publish option. Pattern scans cannot replace semantic,
audio, or whole-source review.

The private 0600 receipt binds the destination, reviewed revision, notes and
asset hashes, and review checks. It is written before the upload; a failure leaves
an explicit pending receipt for reconciliation. The command uses
`gh release create --verify-tag --draft`, adds `--prerelease` for canaries, uploads
exactly the four assets, and reads back draft/prerelease flags and asset names.
It never creates a missing tag or automatically publishes. See the
[GitHub CLI documentation](https://cli.github.com/manual/gh_release_create).

If upload/read-back fails, inspect the existing draft and receipt before retrying;
do not create a duplicate release, overwrite assets, or publish a partial draft.
Finally, the human reviews the exact downloaded draft assets and their verified
signature/checksums, repeats public-output review if anything changed, and
publishes separately in GitHub. Enable immutable releases where available.
Physical wacli send/play/reply, family-card, quiet-hours and listened-notice checks
remain a gate before merging a release into main. Software checks do not prove
physical Pi, GPIO, audio, NFC, Wi-Fi or WhatsApp acceptance.

## A bad release

Withdraw an unsafe release from discovery through the separately authorized
release process; preserve investigation evidence and do not move its tag. Devices
may already have installed it. Publish the corrected code (or known-good program
bytes) as a **new tag with a higher release_sequence**, sign and review it normally.
Never reduce or reset the sequence floor or reuse a failed tag/sequence. The
updater may restore its journal's exact previous release during a failed local
transaction; that is separate from accepting an older network release.
