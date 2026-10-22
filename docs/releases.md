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
or advertising physical support. Raspberry Pi Zero 2 W is accepted by the setup
installer only; it is not a supported update model.

The packaged `VERSION` and `release.json.version` are generated as the exact tag,
even when the source's development VERSION differs. The source checkout remains
unchanged. `release.json` also contains commit and manifest SHA-256. The existing
bounded updater records installed identity separately; this tooling does not
change heartbeat protocol `version` or install VERSION on existing boxes.

## Build and review

Use a reviewed, clean checkout containing these tools and an existing release
tag containing `scripts/release/payload.txt`. Python 3.9+, Git, gzip, make, uv,
Bun and minisign are needed; use the repository's pinned check tools. This build
now signs the [retained controller descriptor](controller-descriptor.md) on the
offline signing computer. Create a tag only
through the separately authorized release process. The builder creates no tags,
commits, pushes, or releases. For example, with an already existing tag:

```sh
python3 scripts/release/build.py v0.3.0-canary.1 \
  --release-sequence 1 --min-updater-version 1.0.0 \
  --controller-sequence 1 --min-application-version v0.3.0-canary.1 \
  --controller-secret-key /media/offline-signing/release.key \
  --controller-public-key config/release-keys/button-box-release-2026.pub \
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

Controller signing arguments are mandatory. Reserve the independent controller
sequence as well as the application sequence. The builder creates two literal
data files under `messagebox/` after its clean-source checks. They contain the
descriptor and its separate signature. Both enter the installed manifest and
remain with the release after staging cleanup. The second clone must produce the
same descriptor. The builder also checks each completed archive against it.

`scripts/release/payload.txt` lists each permitted tracked regular file explicitly,
without directory wildcards. It includes the installed-file mapping, the bounded
updater and manifest generator needed by the legacy operator path, license and QR
provenance. No setup/provisioning scripts, Git metadata, device configuration,
user state, logs, test reports, or arbitrary migrations are packaged. New installed
files must be added deliberately to this list. New public keys must also be added
explicitly when a bootstrap release needs to carry them.

Each signed release chooses its bundled sound set. The controller still requires
the original shipped cues, voices, voice packs, ringtones and Cloud fallback
files, and keeps the same rules for installing program code, units and configs.
Extra WAVs may be added in the existing sound folders; optional additions may
be removed in a later release. Add every new sound to `payload.txt` and its
sound manifest. The installed-file manifest generator includes extra sounds.
The signature covers the manifest list and every byte through the hashes;
sound flexibility does not change the trust model.

WAVs must be RIFF/WAVE PCM, 48 kHz, 16-bit mono and nonempty, with complete data.
Names match `[a-z0-9][a-z0-9._-]{0,80}\.wav`. Only the existing cue, Cloud,
voice, ringtone and named voice-pack folders are supported; no arbitrary nested
folders or links. Metadata additions are limited to `manifest.json` and
`cues.json`; existing README and lamp files retain their exact mappings.
The limits are 2 MiB per sound file, 160 MiB total including sound metadata, and
2,000 sound files. The current pack already exceeds 64 MiB. Each installed
release owns its sounds, including original ringtones at their existing path,
so rollback restores the previous pack's bytes.

Set `--min-updater-version 1.3.0` for this integrated voice candidate. The fresh
bench path installs that controller before the first application transaction.
Older field boxes need the reviewed A/B bridge first, then signed inactive-slot
publication. Do not use the replaced attended fixed-controller promotion.
See [the bench procedure](rita-bench-install.md).

The tar archive has sorted file entries, uid/gid zero, empty owner/group names,
0644/0755 modes determined from tracked Git modes, and the commit timestamp for
all entries. It uses USTAR and `gzip -n -9` without filename or wall-clock gzip
metadata. Generated identity files have mode 0644. Two independent builds must
match every artifact hash before any output is copied. Repeat builds with the
same reviewed tools, tag, both sequences, minimum versions, signing key, Python
and gzip toolchain produce identical
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
computer**. The human signer generates it outside the repository. Storage,
recovery copies and passphrase custody still need a defined policy. Never put a
secret key or passphrase in this repository, release assets, shell environment,
CI, GitHub secrets, an agent environment, or a Pi. A USB key attached to an online
computer does not provide offline custody. This tooling generates no keys and
cannot prove the computer is offline.

Commit only the independently verified public key under
`config/release-keys/<name>.pub` through the authorized source-review process.
Build with the reviewed tools and public pin on the offline computer. Its three
output assets already contain a signed controller descriptor. The application
checksum file still needs its own signature. Independently rebuild with the same
inputs where possible, or inspect all artifact bytes, hashes, installed mapping
and release metadata.
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

## Emergency trust updates

Use two separately held offline password-encrypted minisign secret keys. The
**release** key signs normal `SHA256SUMS`; the **emergency** key never signs an
application release. It signs only `trust-update.json`. The reviewed emergency
public pin is `config/emergency-keys/button-box-emergency-2026.pub` (key ID
73BD56BB8F9940FC). Public pins are bootstrap-only; they are not in the release
payload. Bootstrap installs the pins in separate root-owned directories and
refuses matching key IDs. If there is no emergency pin, trust replacement is off.
Adding one to an already bootstrapped box needs attended fixed-updater maintenance;
an ordinary application release cannot install a new root of trust.

For a stolen or lost release key, generate a new release key offline. Reserve a
new positive `trust_sequence` greater than **every** previously signed trust
sequence (separate from `release_sequence`). Build a document with only the keys
that should remain trusted, including the new one, and the stolen key's uppercase
16-digit minisign ID in `revoked_key_ids`. Include **all historical revoked IDs**
in every document: offline boxes can skip earlier releases. Keep the complete
sequence/revocation record with the offline signing procedure. Never reuse a
sequence, even for a withdrawn or unpublished signed document.

```sh
python3 scripts/release/trust-update.py build \
  --output /tmp/button-box-trust-v2 --trust-sequence 2 \
  --release-key config/release-keys/new-release.pub \
  --revoked-key-id 0000000000000001
```

The example sequence and revoked ID are placeholders; use the reviewed history.
Repeat `--release-key` for 1–3 keys and `--revoked-key-id` for each revoked ID.
The JSON contains exactly `schema`, `product`, `trust_sequence`, `release_keys`,
`revoked_key_ids` and `created`, with a maximum of 8 KiB. Public keys are canonical
base64 lines without their untrusted comments. `created` defaults to UTC seconds;
use `--created <ISO timestamp>` to build fixed bytes. Unknown/duplicate fields,
duplicate key IDs, revoked keys in the new set and malformed dates are rejected.
Review the JSON and public-key fingerprints on the offline computer.

```sh
python3 scripts/release/trust-update.py sign /tmp/button-box-trust-v2 \
  --secret-key /media/offline-emergency/button-box-emergency.key \
  --public-key config/emergency-keys/emergency.pub

python3 scripts/release/trust-update.py verify /tmp/button-box-trust-v2 \
  --public-key config/emergency-keys/emergency.pub
```

Signing is manual with `minisign -S`; the secret path is mandatory and minisign
prompts for its password. The wrapper shares the normal signer's refusal of CI,
secrets inside the repository/artifact directory, unencrypted secret keys and
existing signatures. Verification uses `minisign -V -H` with a reviewed emergency
pin and needs no make, credentials or network. It verifies before parsing JSON.
These commands create no tags, commits, uploads or releases.

Return only `trust-update.json` and `trust-update.json.minisig` to the connected
release computer. Apply the same mandatory public-output policy, actual-audience
check, pattern scan, semantic review, sanitization/recheck and private receipt to
both assets, signature comments, notes, tag/commit and GitHub automatic source
archives before any upload. Publish manually as assets of a normal release or a
dedicated immutable `trust-v<N>` **prerelease**. A dedicated trust release must be
a prerelease so it does not displace `/latest` for boxes without emergency pins.
The normal four-asset publish wrapper does not upload trust assets. Read back and
independently verify the downloaded assets before publishing the draft.

Boxes replace release trust without a physical button press and report
`software_update.trust_sequence`. Sequence and cumulative revocation history
persist outside application rollback. Removed keys cannot authenticate a queued
candidate after replacement; revoked IDs can never return. Publish a **normal
release signed with the new release key** after publishing the trust update.
Emergency pins themselves cannot be rotated by this document.

If both secret keys are lost, boxes keep working on their existing Cloud features
but box software freezes. Recovery requires an SD-card swap with newly reviewed
pins. A box without an emergency pin also needs that recovery if its release key
is lost. Power-loss durability, ownership and concurrency need physical Pi
acceptance before rollout; local minisign/crash tests do not establish it.

## A bad release

Withdraw an unsafe release from discovery through the separately authorized
release process; preserve investigation evidence and do not move its tag. Devices
may already have installed it. Publish the corrected code (or known-good program
bytes) as a **new tag with a higher release_sequence**, sign and review it normally.
Never reduce or reset the sequence floor or reuse a failed tag/sequence. The
updater may restore its journal's exact previous release during a failed local
transaction; that is separate from accepting an older network release.


### A/B controller payload and proof

The payload contains `messagebox/controller_selector.py` and
`messagebox/controller_slots.py`, the bootstrap and selector services, the
selector timer, and the supervisor queue drop-in. The first bootstrap installs
one permanent selector and five fixed command routes. Application releases must
preserve the selector service, timer, and routes. Admission checks these before
installed-file mutation, including when the selected controller is a legacy one.

Descriptor schema 1 is bounded Python literal data, parsed without import. It
signs controller version, sequence, minimum application version, capabilities,
selector protocol 1, shared-state format 1, and the exact six controller modules
with size, mode, and SHA-256. `unit_changes` is empty. The builder derives this
data from the payload and requires its detached signature. The descriptor and
signature stay with the release for offline admission checks.

Publication verifies the current release keys and persistent sequence/version
floor. It writes only the inactive slot, consumes `TRIED`, and publishes `GOOD`
after a bounded recovery and readiness trial. Each slot's `GOOD` is independent
of the current keys. Key rotation cannot disable a retained accepted controller.
See [descriptor format](controller-descriptor.md) and
[A/B recovery](controller-recovery.md). There are no promotion helpers, recovery
key copies, directory exchanges, or two-rename fallback.
