#!/usr/bin/env python3
"""Export a checkout's fleet settings contract without importing runtime modules."""

import argparse
import ast
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import wave
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ContractError(ValueError):
    pass


def read_source(repo, module):
    return ast.parse((repo / "messagebox" / f"{module}.py").read_text(encoding="utf-8"))


def literal(node, constants):
    """Evaluate only source constants and the few pure constructors they use."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return constants[node.id]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values = [literal(value, constants) for value in node.elts]
        return set(values) if isinstance(node, ast.Set) else values
    if isinstance(node, ast.Dict):
        return {literal(key, constants): literal(value, constants)
                for key, value in zip(node.keys, node.values)}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
        return literal(node.left, constants) - literal(node.right, constants)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in {"frozenset", "list", "bool"}:
            constructor = {"frozenset": frozenset, "list": list, "bool": bool}[node.func.id]
            return constructor(literal(node.args[0], constants))
        if node.func.id == "installed_voice_packs" and not node.args:
            return constants["installed_voice_packs"]()
    raise ContractError(f"unsupported expression: {ast.unparse(node)}")


def source_constants(tree, initial=None):
    result = dict(initial or {})
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                result[node.targets[0].id] = literal(node.value, result)
            except (KeyError, ContractError):
                continue
    return result


def isolated_functions(tree, names, namespace):
    # Execute only named helper definitions, never module imports or startup code.
    definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    missing = names - {node.name for node in definitions}
    if missing:
        raise ContractError(f"missing helpers: {', '.join(sorted(missing))}")
    module = ast.Module(body=definitions, type_ignores=[])
    exec(compile(module, "<settings-contract-helpers>", "exec"), namespace)


def settings_namespace(tree):
    constants = source_constants(tree)
    namespace = {**constants, "copy": copy, "os": os, "Path": Path, "re": re,
                 "ZoneInfo": ZoneInfo, "ZoneInfoNotFoundError": ZoneInfoNotFoundError,
                 "SettingsError": ValueError}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "_TIME"
                                               for target in node.targets):
            namespace["_TIME"] = re.compile(ast.literal_eval(node.value.args[0]))
    names = {"defaults", "validate", "_env_flag", "_env_int"}
    # Include persisted-setting migrations, including legacy recording modes,
    # while describing older checkouts that do not yet have those helpers.
    names.update(node.name for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name.startswith("normalize_"))
    isolated_functions(tree, names, namespace)
    return namespace


def setting_key(node, aliases):
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "document":
        return node.slice.value if isinstance(node.slice, ast.Constant) else None
    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    return None


def describe_keys(tree, namespace, baseline):
    validate = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "validate")
    aliases = {}
    for node in ast.walk(validate):
        if isinstance(node, ast.Assign):
            key = setting_key(node.value, aliases)
            if key:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        aliases[target.id] = key
    parents = {child: node for node in ast.walk(validate) for child in ast.iter_child_nodes(node)}
    descriptions = {}
    kinds = {bool: "bool", int: "int", str: "string", dict: "object"}
    for key in sorted(namespace["_ROOT_KEYS"]):
        if key not in baseline or type(baseline[key]) not in kinds:
            raise ContractError(f"cannot describe setting {key!r}: no supported default type")
        descriptions[key] = {"type": kinds[type(baseline[key])]}
    for node in ast.walk(validate):
        if not isinstance(node, ast.Compare):
            continue
        terms = [node.left, *node.comparators]
        for left, operator, right in zip(terms, node.ops, terms[1:]):
            key = setting_key(left, aliases)
            if key and isinstance(operator, (ast.NotIn, ast.Eq, ast.NotEq)):
                try:
                    values = literal(right, namespace)
                except (KeyError, ContractError):
                    continue
                values = list(values) if isinstance(values, (dict, list, set, frozenset)) else [values]
                if values and all(type(value) in {str, int, bool} for value in values):
                    descriptions[key] = {"type": "enum", "values": sorted(values)}
            # Comparisons in rejection guards describe the complement; `not`
            # guards (such as `not 0 <= volume <= 100`) describe accepted ranges.
            ancestor, accepted = node, False
            while ancestor in parents and not isinstance(parents[ancestor], ast.If):
                ancestor = parents[ancestor]
                if isinstance(ancestor, ast.UnaryOp) and isinstance(ancestor.op, ast.Not):
                    accepted = not accepted
            for value_node, limit_node, reversed_order in ((left, right, False), (right, left, True)):
                key = setting_key(value_node, aliases)
                if (isinstance(value_node, ast.Call) and isinstance(value_node.func, ast.Name)
                        and value_node.func.id == "len"):
                    key = setting_key(value_node.args[0], aliases)
                if not key or not isinstance(limit_node, ast.Constant) or type(limit_node.value) is not int:
                    continue
                operators = {ast.Lt: "lt", ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge"}
                relation = operators.get(type(operator))
                if relation is None:
                    continue
                if reversed_order:
                    relation = {"lt": "gt", "le": "ge", "gt": "lt", "ge": "le"}[relation]
                if not accepted:
                    relation = {"lt": "ge", "le": "gt", "gt": "le", "ge": "lt"}[relation]
                bound = "min" if relation in {"gt", "ge"} else "max"
                offset = 1 if relation == "gt" else -1 if relation == "lt" else 0
                descriptions[key][bound] = limit_node.value + offset
    if "voice_pack" in descriptions:
        if "VOICE_PACKS" not in namespace:
            raise ContractError("cannot describe voice_pack: missing VOICE_PACKS")
        descriptions["voice_pack"] = {"type": "enum", "values": list(namespace["VOICE_PACKS"])}
    if "ringtone_id" in descriptions and "LEGACY_RINGTONES" in namespace:
        descriptions["ringtone_id"]["values"] = sorted(set(descriptions["ringtone_id"]["values"]) | namespace["LEGACY_RINGTONES"])
    # Fail closed on newly introduced validation that the descriptor cannot represent.
    for key, description in descriptions.items():
        if description["type"] == "int" and "min" not in description:
            raise ContractError(f"cannot describe integer limits for {key!r}; extend the exporter")
    return descriptions


def export_contract(repo):
    tree = read_source(repo, "settings")
    namespace = settings_namespace(tree)
    baseline = namespace["validate"](namespace["defaults"]({}))
    try:
        namespace["validate"]({**baseline, "future_contract_key": True})
    except ValueError:
        unknown_keys = "reject"
    else:
        unknown_keys = "ignore"
    device = read_source(repo, "cloud_device")
    constants = source_constants(device, namespace)
    constants["nfc"] = False
    capability_function = next(node for node in device.body if isinstance(node, ast.FunctionDef) and node.name == "capabilities")
    if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
           and node.func.id == "installed_voice_packs" for node in ast.walk(capability_function)):
        sound = read_source(repo, "sound_pack")
        sound_namespace = {**source_constants(sound, namespace), "Path": Path, "json": json,
                           "hashlib": hashlib, "wave": wave, "SOUND_DIR": repo / "sounds"}
        isolated_functions(sound, {"_validate_pack", "installed_voice_packs"}, sound_namespace)
        constants["installed_voice_packs"] = sound_namespace["installed_voice_packs"]
    returns = [node for node in ast.walk(capability_function) if isinstance(node, ast.Return)]
    if len(returns) != 1:
        raise ContractError("cannot describe capabilities: expected one return")
    capabilities = literal(returns[0].value, constants)
    runtime = read_source(repo, "cloud_runtime")
    command_sets = []
    for node in ast.walk(runtime):
        if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Call)
                and isinstance(node.left.func, ast.Attribute) and node.left.func.attr == "get"
                and node.left.args and isinstance(node.left.args[0], ast.Constant)
                and node.left.args[0].value == "kind" and isinstance(node.ops[0], ast.NotIn)
                and isinstance(node.comparators[0], ast.Set)):
            command_sets.append(literal(node.comparators[0], {}))
    if len(command_sets) != 1:
        raise ContractError("cannot describe command kinds: expected one inbox allowlist")
    build = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], text=True).strip()
    return {"build": build, "settings_version": namespace["SCHEMA_VERSION"], "unknown_keys": unknown_keys,
            "keys": describe_keys(tree, namespace, baseline), "command_kinds": sorted(command_sets[0]),
            "capabilities": capabilities}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    arguments = parser.parse_args()
    try:
        contract = export_contract(arguments.repo.resolve())
    except (ContractError, OSError, ValueError, KeyError, StopIteration, TypeError, NameError, subprocess.CalledProcessError) as exc:
        print(f"settings contract export failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(contract, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
