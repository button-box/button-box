#!/usr/bin/env python3
"""Post-install assertions for the Raspberry Pi OS CI job.

Run as root inside the booted Pi OS container after a fresh install. Checks run
as the service users themselves, so file modes, groups and the installed Python
environments are tested the way systemd will use them.
"""
import argparse
import grp
import hashlib
import importlib.util
import json
import os
import pwd
import shlex
import subprocess
import sys
from pathlib import Path

UNIT_DIR = Path("/etc/systemd/system")
COMITUP_UNITS = ("/usr/lib/systemd/system/comitup.service", "/usr/lib/systemd/system/comitup-web.service")
UPDATE_UNITS = ("messagebox-update-recovery.service", "messagebox-update-controller.service",
                "messagebox-update-supervisor.timer", "messagebox-update-discovery.timer")
SYSTEMD_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# Third-party modules the NFC services load from venv-nfc. board and busio
# probe the hardware on import, and lgpio opens a notification FIFO in the
# working directory, so only their presence is checked.
NFC_IMPORTS = ("adafruit_pn532", "adafruit_pn532.i2c")
NFC_PRESENT = ("board", "busio", "digitalio", "lgpio")
# Release-mapped files that setup.sh deliberately does not install. Each needs
# a reason; anything else missing from a fresh install fails the job.
NOT_INSTALLED_BY_SETUP = {
    # Legacy ringtone generator: nothing imports it, setup.sh removes the old
    # top-level copy, and only the bounded updater ships the package copy.
    "messagebox/make_ringtones.py",
}

failures = []


def check(ok, message):
    print(("ok    " if ok else "FAIL  ") + message, flush=True)
    if not ok:
        failures.append(message)
    return ok


def run(command, **kwargs):
    return subprocess.run(command, text=True, capture_output=True, check=False, **kwargs)


def tail(result):
    return (result.stdout + result.stderr).strip()[-1500:]


def unit_settings(path):
    """Return the [Service] keys of a unit file, keeping repeated keys in order."""
    settings, section = {}, None
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            section = line.strip("[]")
            continue
        if section == "Service" and "=" in line:
            key, value = line.split("=", 1)
            settings.setdefault(key.strip(), []).append(value.strip())
    return settings


def environment_file(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def service_environment(settings):
    # Probes must not leave bytecode in the program tree.
    env = {"PATH": SYSTEMD_PATH, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    for value in settings.get("EnvironmentFile", []):
        optional = value.startswith("-")
        path = value.lstrip("-")
        if Path(path).is_file():
            env.update(environment_file(path))
        elif not optional:
            check(False, f"required EnvironmentFile {path} exists")
    for value in settings.get("Environment", []):
        for item in shlex.split(value):
            key, _, assigned = item.partition("=")
            env[key] = assigned.replace("%%", "%")
    return env


def as_unit_user(settings, argv, cwd="/"):
    """Run argv with the unit's user, group, supplementary groups and environment."""
    user = settings.get("User", ["root"])[-1]
    command = ["runuser", "-u", user]
    group = settings.get("Group", [None])[-1]
    supplementary = [name for value in settings.get("SupplementaryGroups", []) for name in value.split()]
    if group or supplementary:
        command += ["-g", group or user]
        for name in supplementary:
            command += ["-G", name]
    env = service_environment(settings)
    command += ["--", "env", "-i", *[f"{key}={value}" for key, value in env.items()], *argv]
    return run(command, cwd=cwd)


IMPORT_MODULE = "import importlib, sys; importlib.import_module(sys.argv[1])"
# A script's own directory comes first on sys.path when Python runs it.
LOAD_SCRIPT = ("import importlib.util, os, sys; sys.path.insert(0, os.path.dirname(sys.argv[1])); "
               "spec = importlib.util.spec_from_file_location('ci_probe', sys.argv[1]); "
               "spec.loader.exec_module(importlib.util.module_from_spec(spec))")


def exec_start(settings):
    for value in settings.get("ExecStart", []):
        argv = shlex.split(value.lstrip("-+!@:"))
        if argv:
            return argv
    return None


def check_service_imports(unit):
    settings = unit_settings(unit)
    argv = exec_start(settings)
    if argv is None:
        return
    working = settings.get("WorkingDirectory", ["/"])[-1].lstrip("-")
    cwd = working if Path(working).is_dir() else "/"
    program = argv[0]
    probes = []
    if Path(program).name.startswith("python"):
        arguments = [item for item in argv[1:] if item != "-u"]
        if arguments[:1] == ["-m"]:
            probes.append((program, IMPORT_MODULE, arguments[1]))
        else:
            probes.append((program, LOAD_SCRIPT, arguments[0]))
    elif Path(program).name == "gunicorn":
        interpreter = Path(program).read_text().splitlines()[0].removeprefix("#!").split()[0]
        probes.append((interpreter, IMPORT_MODULE, "gunicorn"))
        probes.append((interpreter, IMPORT_MODULE, argv[-1].split(":")[0]))
    else:
        result = as_unit_user(settings, ["test", "-x", program], cwd=cwd)
        check(result.returncode == 0, f"{unit.name}: {settings.get('User', ['root'])[-1]} can execute {program}")
        shell = Path(program).read_text().splitlines()[0].removeprefix("#!").split()[0]
        result = run([shell, "-n", program])
        check(result.returncode == 0, f"{unit.name}: {program} parses")
        return
    for interpreter, code, target in probes:
        result = as_unit_user(settings, [interpreter, "-c", code, target], cwd=cwd)
        check(result.returncode == 0,
              f"{unit.name}: {settings.get('User', ['root'])[-1]} imports {target} with {interpreter}"
              + ("" if result.returncode == 0 else "\n" + tail(result)))


WALK = r"""
import json, os, sys
bad = []
def visit(path):
    if os.path.islink(path) and not os.path.exists(path):
        bad.append(["broken link", path]); return
    if os.path.isdir(path):
        need, kind = os.R_OK | os.X_OK, "directory"
    else:
        mode = os.stat(path).st_mode
        need, kind = (os.R_OK | os.X_OK, "program") if mode & 0o111 else (os.R_OK, "file")
    if not os.access(path, need):
        bad.append([f"unreadable {kind}", path])
for root in sys.argv[1:]:
    if not os.path.lexists(root):
        bad.append(["missing", root]); continue
    visit(root)
    if not os.path.isdir(root):
        continue
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda error: bad.append(["unlistable", error.filename])):
        # Bytecode caches are optional: Python compiles from source when it
        # cannot read them.
        dirnames[:] = [name for name in dirnames if name != "__pycache__"]
        for name in dirnames + filenames:
            visit(os.path.join(dirpath, name))
print(json.dumps(bad))
"""


def check_readable(user, roots):
    result = run(["runuser", "-u", user, "--", "/usr/bin/python3", "-c", WALK, *roots])
    if not check(result.returncode == 0, f"{user}: readability walk ran" + ("" if result.returncode == 0 else "\n" + tail(result))):
        return
    bad = json.loads(result.stdout)
    detail = "\n".join(f"      {kind}: {path}" for kind, path in bad[:25])
    check(not bad, f"{user} can read every file it needs under {', '.join(roots)}"
          + (f" ({len(bad)} problems)\n{detail}" if bad else ""))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_payload(source, mode):
    spec = importlib.util.spec_from_file_location("release_manifest", source / "scripts/dev/release-manifest.py")
    manifest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manifest)
    mapping = manifest.installed_paths(source)
    payload = {line for line in (source / "scripts/release/payload.txt").read_text().splitlines()
               if line and not line.startswith("#")}
    outside = sorted(set(mapping) - payload)
    check(not outside, "every installed source is in scripts/release/payload.txt" + (f": {outside}" if outside else ""))

    # The fixed updater units are installed by update-bootstrap.sh, which only
    # Cloud boxes run. setup.sh does not install them on self-hosted boxes.
    bootstrap_only = {src for src in mapping if src.startswith("systemd/messagebox-update-")}
    missing, changed = [], []
    for src, installed in sorted(mapping.items()):
        if mode == "wacli" and src in bootstrap_only:
            check(not os.path.lexists(installed), f"self-hosted box has no {installed}")
            continue
        if src in NOT_INSTALLED_BY_SETUP:
            continue
        if not os.path.isfile(installed):
            missing.append(installed)
        elif sha256(installed) != sha256(source / src):
            changed.append(installed)
    check(not missing, f"all {len(mapping)} release-mapped files are installed" + (f"; missing {missing}" if missing else ""))
    check(not changed, "installed files match their sources" + (f"; differ: {changed}" if changed else ""))

    # Nothing outside the release mapping may appear in the program tree.
    app = Path(os.path.realpath("/opt/messagebox"))
    expected = {os.path.realpath(path) for path in mapping.values() if path.startswith("/opt/messagebox/")}
    generated = {str(app / name) for name in ("release.json", "VERSION", "release-manifest.json")}
    extra = []
    for path in sorted(app.rglob("*")):
        # Root services may compile bytecode next to the sources at run time.
        parts = path.relative_to(app).parts
        if "venv-nfc" in parts or "__pycache__" in parts or path.is_dir():
            continue
        if str(path) not in expected and str(path) not in generated:
            extra.append(str(path))
    check(not extra, "the program tree has no files outside the release mapping" + (f": {extra[:25]}" if extra else ""))
    units = {path for path in mapping.values() if path.startswith(str(UNIT_DIR) + "/")}
    unmapped = sorted(str(path) for path in UNIT_DIR.glob("messagebox*")
                      if path.is_file() and not path.is_symlink() and str(path) not in units)
    check(not unmapped, "every installed messagebox unit is in the release mapping" + (f": {unmapped}" if unmapped else ""))


INSTALL_LOG = Path("/var/log/button-box-install.log")
FAKE_SETUP_LINK = "https://button.box/s/CI2345"  # fake-cloud.py


def check_installer():
    """Extra checks for the one-command installer (scripts/install/install.sh)."""
    info = INSTALL_LOG.stat() if INSTALL_LOG.exists() else None
    check(info is not None and info.st_uid == 0 and info.st_mode & 0o777 == 0o600,
          f"{INSTALL_LOG} exists and only root can read it")
    log = INSTALL_LOG.read_text(errors="replace") if info else ""
    check(log.count("Button Box is installed.") == 2, "both installer runs finished")
    check("Signed updater is installed; checking its services" in log, "the second run repaired instead of reinstalling")
    check(FAKE_SETUP_LINK in log, "the installer printed the Cloud setup link")
    identity = Path("/var/lib/messagebox-cloud/device.json")
    if check(identity.is_file(), "the box registered and has a device identity"):
        meta = identity.stat()
        check(meta.st_uid == pwd.getpwnam("messagebox").pw_uid
              and meta.st_gid == grp.getgrnam("messagebox-settings").gr_gid and meta.st_mode & 0o777 == 0o660,
              "the device identity belongs to messagebox:messagebox-settings with mode 0660")
        credential = json.loads(identity.read_text())["credential"]
        check(credential not in log, "the install log has no device credential")
    check("ci-claim-token" not in log, "the install log has no claim token")
    release = json.loads(Path("/opt/messagebox/release.json").read_text())
    check(release.get("manifest_sha256") is not None, f"installed identity comes from the signed release ({release.get('version')})")
    policy = json.loads(Path("/var/lib/button-box-update/policy.json").read_text())
    check(policy.get("mode") == "auto" and policy.get("channel") == "stable", "updates are auto on the stable channel")
    floor = json.loads(Path("/var/lib/button-box-update/sequence.json").read_text())
    check(floor == 1, f"the update floor is the installed release's sequence (1, found {floor})")
    check(Path("/opt/messagebox/VERSION").read_text().strip() == release.get("version"), "VERSION matches release.json")
    for unit in ("messagebox-onboarding-complete.path", "messagebox-onboarding-voice.path"):
        check(run(["systemctl", "is-enabled", unit]).stdout.strip() == "enabled", f"{unit} is enabled")
    result = run(["messageboxctl", "doctor"])
    print("      doctor:\n" + "\n".join("        " + line for line in result.stdout.splitlines()))
    check(result.returncode == 0, "messageboxctl doctor reports no failures")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("wacli", "cloud"), required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--installer", action="store_true", help="also check the one-command installer")
    args = parser.parse_args()

    env = environment_file("/etc/messagebox/env")
    check(env.get("MSGBOX_TRANSPORT") == args.mode, f"/etc/messagebox/env selects MSGBOX_TRANSPORT={args.mode}")

    # Real audio detection against the fake USB speaker and microphone.
    result = run(["systemctl", "start", "messagebox-audio-detect.service"])
    check(result.returncode == 0 and Path("/run/messagebox-audio/audio.env").is_file(),
          "messagebox-audio-detect.service writes /run/messagebox-audio/audio.env" + ("" if result.returncode == 0 else "\n" + tail(result)))

    units = sorted(path for pattern in ("messagebox*.service", "messagebox*.path", "messagebox*.timer", "messagebox*.target")
                   for path in UNIT_DIR.glob(pattern) if path.is_file())
    result = run(["systemd-analyze", "verify", "--man=no", *map(str, units), *COMITUP_UNITS])
    check(result.returncode == 0, f"systemd-analyze verify accepts {len(units) + len(COMITUP_UNITS)} unit files"
          + ("" if result.returncode == 0 else "\n" + tail(result)))
    if result.returncode == 0 and result.stderr.strip():
        print("      verify notes:\n" + result.stderr.strip()[-1500:])

    venv = os.path.realpath("/opt/messagebox/venv-nfc")
    check_readable("messagebox", ["/opt/messagebox/", venv, "/usr/lib/messagebox", "/usr/share/messagebox",
                                  "/etc/messagebox", "/var/lib/messagebox", "/var/lib/messagebox-cloud",
                                  "/usr/local/bin/wacli"])
    check_readable("messagebox-onboarding", ["/opt/messagebox/", "/usr/share/messagebox",
                                             "/var/lib/messagebox-onboarding"])
    if args.mode == "cloud":
        check_readable("messagebox-update", ["/usr/lib/messagebox-update", "/var/cache/button-box-update"])

    for unit in units:
        if unit.suffix == ".service":
            check_service_imports(unit)
    nfc = unit_settings(UNIT_DIR / "messagebox-nfc.service")
    python = exec_start(nfc)[0]
    for module in NFC_IMPORTS:
        result = as_unit_user(nfc, [python, "-c", IMPORT_MODULE, module], cwd="/opt/messagebox")
        check(result.returncode == 0, f"messagebox imports {module} from venv-nfc" + ("" if result.returncode == 0 else "\n" + tail(result)))
    code = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)"
    for module in NFC_PRESENT:
        result = as_unit_user(nfc, [python, "-c", code, module], cwd="/opt/messagebox")
        check(result.returncode == 0, f"messagebox finds {module} in venv-nfc")

    check_payload(args.source, args.mode)

    if args.mode == "wacli":
        check(not os.path.islink("/opt/messagebox"), "self-hosted box keeps the setup.sh layout")
        result = run(["/usr/local/bin/wacli", "--version"])
        check(result.returncode == 0 and result.stdout.startswith("wacli "), "wacli is installed")
    else:
        target = os.readlink("/opt/messagebox") if os.path.islink("/opt/messagebox") else ""
        check(target.startswith("/opt/messagebox-releases/"), "Cloud box runs from a versioned release")
        check(Path("/etc/button-box-update/installed").is_file(), "updater bootstrap completed")
        for unit in UPDATE_UNITS:
            enabled = run(["systemctl", "is-enabled", unit]).stdout.strip()
            failed = run(["systemctl", "is-failed", unit]).stdout.strip()
            check(enabled == "enabled" and failed != "failed", f"{unit} is enabled and not failed ({enabled}, {failed})")

    if args.installer:
        check_installer()

    # dpkg is the source of truth; `apt-mark showhold` sometimes lists nothing
    # right after the Tailscale repository is added.
    status = run(["dpkg-query", "-W", "-f=${Status}", "tailscale"])
    check(status.stdout.strip() == "hold ok installed",
          f"tailscale is installed and held (dpkg: {status.stdout.strip() or tail(status)!r})")
    result = run(["systemctl", "list-units", "--failed", "--plain", "--no-legend", "messagebox*", "comitup*"])
    check(not result.stdout.strip(), "no Button Box or Comitup unit failed" + (f"\n{result.stdout}" if result.stdout.strip() else ""))

    print(f"\n{len(failures)} failed checks" if failures else "\nAll install checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
