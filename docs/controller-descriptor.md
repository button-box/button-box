# Retained controller proof

Signed controller proof authenticates admission to the inactive slot.
The seven fixed unit files do not change. Neither transport calls Cloud for this proof.

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
| `selector_protocol` | Positive protocol integer. The builder emits 1. |
| `state_format` | Positive protocol integer. The builder emits 1. |
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

Admission holds `trust_update.trust_lock`, finishes `recover_trust`, and verifies
against current release keys. A removed key cannot admit a new descriptor.

The descriptor uses selector protocol 1 and shared state format 1. There are no
helper versions, recovery-file descriptors or per-release unit changes.
Candidate `UPDATER_VERSION` is `1.3.0`. Initial slot A remains `1.1.0` until a
successful trial commits B. The slot publisher enforces a persistent sequence,
digest and version floor and runs both retained controllers' real state parsers.
Boot selection uses the independent local `GOOD` records, with no release keys.

Secret-key custody, recovery copies, emergency approval thresholds and
operational rotation remain open decisions. Tests use
throwaway keys. No production release was signed.

See [controller slots](controller-recovery.md) for publication, boot selection,
failure tests and native/Pi acceptance limits.
