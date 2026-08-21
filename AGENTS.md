# Development Guidelines

Keep changes simple, direct, and maintainable. The goal is working software that is easy to understand, not code that demonstrates cleverness.

## Approach

- Make the smallest correct change in the existing area of the codebase and avoid unrelated cleanup.
- Prefer the standard library, plain functions, and plain data structures.
- Do not add frameworks, service or repository layers, base classes, dependencies, or configuration without a concrete need.
- Keep orchestration readable. Extract code when it is reusable, independently testable, or makes a complicated invariant clearer.
- Use concise domain names and straightforward control flow.
- Do not add backward-compatibility code without a concrete need. Persisted data on deployed devices, published interfaces, and explicit requirements count; hypothetical future users do not.
- Comments should explain safety constraints, ordering, or intent rather than restating the code.

## File Placement

Put code where its purpose is obvious:

- Runtime Python belongs in `messagebox/`.
- Developer-only scripts belong in `scripts/dev/`.
- Operator commands belong in `scripts/commands/`.
- Installation helpers belong in `scripts/install/`.
- Service definitions belong in `systemd/`.
- Configuration and templates belong in `config/`.
- Tests belong in `tests/` and should follow the existing module-oriented naming.

Do not put temporary tooling or one-off scripts in production paths or the repository root. When adding a runtime file, update the explicit allowlists in `scripts/setup.sh` and `scripts/provision.sh` as needed.

## Correctness And Safety

- Preserve fail-closed recipient routing. Never silently fall back to another recipient or configuration.
- Preserve existing atomic writes, file locks, sidecars, ownership, permissions, redaction, and crash-recovery behavior.
- Keep external side effects explicit. Treat WhatsApp sends, filesystem transitions, hardware access, and systemd operations as failure boundaries.
- Keep hardware and subprocess boundaries injectable when tests need control over them.
- Do not expose credentials, card identifiers, message identifiers, phone numbers, or private command output in logs or public APIs.
- Respect the fixed Raspberry Pi paths, service users, and runtime/onboarding separation unless the task explicitly changes that architecture.

## Testing

- Add focused tests that protect changed behavior and realistic failure boundaries, including restart or idempotency when relevant.
- Prefer a few clear contract tests. Avoid trivial implementation-detail, repetitive, or coverage-only tests.
- Keep normal tests independent of credentials, network access, and physical hardware.
- Run `make check` before considering a change complete.
- State clearly when physical Raspberry Pi, audio, GPIO, NFC, Wi-Fi, or WhatsApp verification is still required.

## Documentation And Deployment

- Update operator or architecture documentation when behavior, installation, configuration, or recovery procedures change.
- After a major code or architecture change, audit the related documentation for stale claims, commands, paths, and recovery steps.
- Keep supported environment settings documented in `config/env.example`; do not scatter undocumented environment variables through the code.
- Check setup scripts, systemd units, permissions, and runtime directories whenever deployment inputs change.
