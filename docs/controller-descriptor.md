# Retained controller proof

This change prepares signed controller proof. It does not promote a controller.
The fixed units do not change. Neither transport calls Cloud for this proof.

## Files and format

The builder generates two files inside the application payload:

| File | Data |
|---|---|
| `messagebox/controller_descriptor.py` | One Python dictionary literal. |
| `messagebox/controller_signature.py` | One Python string literal containing the detached minisign signature. |

These are data files. Parse them; never import or execute them. Both files use
mode 0644. Their `.py` extension lets the frozen field workers retain them.
The installed-file manifest covers both files. The application signature also
covers them through the archive hash.

The descriptor has these exact schema-1 fields:

| Field | Meaning |
|---|---|
| `schema` | Integer 1. |
| `product` | `button-box-controller`. |
| `controller_version` | Target bundle version. The builder emits `1.3.0`. |
| `controller_sequence` | Reserved positive integer below 2^63. Independent of the application sequence. |
| `min_application_version` | Minimum application tag, including an optional canary suffix. |
| `min_launcher_protocol` | Positive protocol integer. The builder emits 1. |
| `min_helper_protocol` | Positive protocol integer. The builder emits 1. |
| `supported_os` | `['raspios-trixie']`. |
| `supported_arch` | `['aarch64']`. |
| `supported_models` | The sorted Pi 3 A+ and Pi 4 model names from `release_verify.py`. |
| `capabilities` | Exactly `signed_descriptor` and `self_upgrade`, both Boolean. |
| `modules` | Exactly the six records below, in this order. |
| `unit_changes` | Empty list. This version authorizes no controller integration changes. |

Each module record has exactly `path`, `size`, `mode`, and `sha256`.
`size` is a positive byte count. `mode` is integer 420, which is octal 0644.
`sha256` is 64 lowercase hexadecimal digits.

| Payload source | Retained module path |
|---|---|
| `scripts/install/bounded_update.py` | `updater/bounded_update.py` |
| `scripts/install/health-probe.py` | `updater/health-probe.py` |
| `scripts/install/release_verify.py` | `updater/release_verify.py` |
| `scripts/install/trust_update.py` | `updater/trust_update.py` |
| `scripts/install/update_controller.py` | `updater/update_controller.py` |
| `scripts/install/update_discovery.py` | `updater/update_discovery.py` |

The builder hashes the exact payload sources. The old worker copies those
sources into the release's `updater/` directory with mode 0644. It does not
replace `/usr/lib/messagebox-update`.

The descriptor contains no Git commit or signature hash. Its SHA-256 identifies
the controller bundle without a reference to the enclosing application commit.
The signature authenticates the exact descriptor bytes, including whitespace.

## Limits and rejection

The descriptor limit is 16 KiB. The signature literal limit is 4 KiB; its decoded
ASCII signature is at most 2 KiB. Each module is at most 2 MiB.

The parser accepts only dictionaries, lists, strings, integers and Booleans.
It limits data to 512 nodes and depth 8. Duplicate keys, unknown fields,
assignments, expressions, calls and imports are rejected. No candidate code runs.

Verification rejects a missing signature, wrong key, changed descriptor,
changed module, wrong hash, wrong size, wrong mode, missing module or extra module
record. It also rejects incompatible application versions, platforms and sequence
floors. An equal sequence is permitted when rechecking an accepted retained proof.
Admission of a new promotion needs its own strict sequence check in later work.

Files are read through no-follow file descriptors. The selected root, its
descendant directories and files must belong to the verifying user and exclude
group/other write permission. Files must be regular and have one link.
The reader checks size and metadata again after reading. It rejects symlinks,
hardlinks, special files and unsafe parents. Root selection remains the caller's
responsibility.

## Build and release checks

Build on the offline signing computer. Use an existing reviewed tag and a clean
checkout. Reserve both sequence numbers before signing. Example values below
do not allocate a real sequence or authorize a release.

```sh
python3 scripts/release/build.py v0.3.13-canary.1 \
  --release-sequence 13 --min-updater-version 1.1.0 \
  --controller-sequence 1 --min-application-version v0.3.13-canary.1 \
  --controller-secret-key /media/offline-signing/release.key \
  --controller-public-key config/release-keys/button-box-release-2026.pub \
  --output /tmp/button-box-v0.3.13-canary.1
```

The builder checks both transports in two clean clones. It requires all six
controller sources in the tracked payload allowlist. It then generates the
descriptor, signs it once, and verifies the selected secret/public pair.
The second clone must produce identical descriptor bytes. Both archives retain
the same verified proof. Archive checks reject a mismatch before copying output.
Generated proof paths are reserved; tracked or existing files cannot replace them.

Signing reuses the existing encrypted-key policy. CI signing is refused.
The secret must remain outside the source checkout and artifacts. There is no
password argument or environment interface. Minisign requests the password.

The output still has three assets. Sign `SHA256SUMS` separately with the existing
`scripts/release/sign.sh` command. This authenticates the enclosing application.
`verify.sh`, `sign.sh`, and `publish.sh` now check the retained controller proof
and module hashes as well. Publication stops before GitHub access on a mismatch.
The public-output review remains required before any real upload or publish.

The descriptor signature is separate from the application signature. This PR
uses the current release-key set for both roles. It does not create a separate
controller-maintenance key role. Emergency keys can replace release trust;
they cannot sign controller code. The verifier resolves one current key
generation and excludes emergency key IDs, even if such a pin appears there.

## Retention and future use

After an old-worker installation, both proof files remain under the committed
release's `messagebox/` directory. The six modules remain under `updater/`.
The download and staging trees can be removed without losing this proof.

The read-only API is `release_verify.verify_retained_controller`. The caller
supplies the controlled release root, current release keys, verified application
version, platform, and sequence floor. The result contains the parsed descriptor,
its SHA-256, and the signing key ID.

A future helper must hold `trust_update.trust_lock`, finish `recover_trust`, and
verify against that current generation. It must repeat verification before
promotion. A descriptor signed by a removed key is rejected after trust rotation.
An earlier build-time check does not override later revocation.

The builder currently emits `signed_descriptor=True` and `self_upgrade=False`.
Version `1.3.0` names the target bundle. It does not identify the running fixed
controller. `UPDATER_VERSION` remains 1.1.0 in this PR. No heartbeat or active
capability changes.

The minimum application field provides a lower version bound only. It does not
prove state-format or rollback compatibility. Later work must bind integration
files, helper artifacts and rollback compatibility before any promotion uses them.
The future helper must enforce both protocol minima. This API does not attest a
running launcher or helper.
The empty `unit_changes` contract must be extended before such changes can be
authorized by a descriptor.

Key storage, recovery copies, separate controller-key roles, emergency approval
thresholds and rotation procedures are not settled by these tools. No production
secret key was used to test this change. No deployable release was signed here.

## Validation and remaining work

Checks on 21 October 2026:

| Command | Result |
|---|---|
| `python3 -m unittest discover -s tests -p test_controller_descriptor.py -v` | PASS: 13 tests, including real minisign and all four frozen workers. |
| `python3 -m unittest discover -s tests -p test_release_tooling.py -v` | PASS: 18 tests. |
| `MSGBOX_TEST_TRANSPORT=cloud make check` | PASS: syntax, lint, 1,136 Python tests and 54 JavaScript tests. |
| `MSGBOX_TEST_TRANSPORT=wacli make check` | PASS: syntax, lint, 1,136 Python tests and 54 JavaScript tests. |

The independent review found a selected-pin mismatch during key overlap.
The signer now verifies the selected pair directly. A real-minisign regression
test passes. The follow-up review found no remaining blocker in this scope.

Tests use real minisign signatures with temporary test keys. They cover changed
modules, wrong signed hashes, missing signatures, wrong keys, signed malformed
data, bounds, unsafe paths, key overlap and emergency-authorized trust replacement.

Each frozen v0.3.9–v0.3.12 worker installs the current candidate modules and both
proof files in a temporary root. The test removes downloads through the real old
worker path, then verifies the retained proof offline. The fixed controller bytes
remain unchanged. Systemd, health and the enclosing application signature process
use the PR 1 host substitutes. The descriptor signature uses real minisign.

No promotion, recovery launcher, controller exchange, new-box installation,
rollout permit, or shipment acceptance is implemented here. Live OTA, boot,
power-loss recovery, storage behavior and physical Cloud/wacli acceptance remain
unverified. This PR adds no Cloud dependency to wacli.
