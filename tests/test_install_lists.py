"""Every module the Cloud runtime imports must be installed by every path.

A box set up with scripts/setup.sh or scripts/provision.sh must be able to
import messagebox.cloud_runtime. Release payloads are checked elsewhere; this
test checks the two direct install lists against the real import graph.
"""

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "messagebox"


def _module_imports(path):
    """Return the messagebox module files (relative to messagebox/) imported by path."""
    found = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = ".".join(path.relative_to(ROOT).with_suffix("").parts[:-node.level])
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module or ""
            names = [module] + [f"{module}.{alias.name}" for alias in node.names]
        for name in names:
            if not name.startswith("messagebox"):
                continue
            parts = name.split(".")[1:]
            # The longest prefix that is a real file or package wins.
            for end in range(len(parts), 0, -1):
                candidate = PACKAGE.joinpath(*parts[:end])
                if candidate.with_suffix(".py").is_file():
                    found.add(str(candidate.with_suffix(".py").relative_to(PACKAGE)))
                    break
                if (candidate / "__init__.py").is_file():
                    found.add(str((candidate / "__init__.py").relative_to(PACKAGE)))
                    break
    return found


def runtime_closure(entry="cloud_runtime.py"):
    seen, todo = {"__init__.py"}, [entry]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        todo.extend(sorted(_module_imports(PACKAGE / name) - seen))
    return seen


def setup_list():
    text = (ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")
    match = re.search(r'^PACKAGE_PYTHON="([^"]*)"', text, re.M)
    if not match:
        raise AssertionError("PACKAGE_PYTHON not found in scripts/setup.sh")
    return set(match.group(1).split())


def provision_list():
    text = (ROOT / "scripts" / "provision.sh").read_text(encoding="utf-8")
    return set(re.findall(r'"\$REPO_DIR/\./messagebox/([^"]+\.py)"', text))


class CloudRuntimeInstallListTest(unittest.TestCase):
    def test_closure_finds_the_cloud_runtime_imports(self):
        closure = runtime_closure()
        for name in ("cloud_runtime.py", "cloud_device.py", "cloud_quiet.py", "remote_access.py"):
            self.assertIn(name, closure)

    def test_setup_installs_every_cloud_runtime_module(self):
        missing = sorted(runtime_closure() - setup_list())
        self.assertEqual(missing, [], f"scripts/setup.sh PACKAGE_PYTHON is missing {missing}")

    def test_provision_copies_every_cloud_runtime_module(self):
        missing = sorted(runtime_closure() - provision_list())
        self.assertEqual(missing, [], f"scripts/provision.sh is missing {missing}")


if __name__ == "__main__":
    unittest.main()
