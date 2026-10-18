#!/usr/bin/env python3
"""Generate config/cloud-contract.json: every Cloud call the box makes.

The Cloud repository vendors the generated file and checks each entry against
its real router. Source scanning finds every device path literal in the Cloud
client modules. Each literal must belong to exactly one entry in SPEC below,
so a new Cloud call cannot ship without a contract entry.

    python3 scripts/dev/cloud_contract.py          # rewrite the manifest
    python3 scripts/dev/cloud_contract.py --check  # fail when it is stale
"""

import argparse
import ast
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MANIFEST = Path("config") / "cloud-contract.json"
API_BASE = "/cloud-api/v1"
SCANNED = {
    "messagebox/cloud_device.py": "CloudDeviceClient",
    "messagebox/cloud_events.py": None,
}

# Patterns copied from the client validators that read each value.
ID = "^[A-Za-z0-9_-]{16,128}$"
RUNTIME_ID = "^[A-Za-z0-9:._-]{1,240}$"
MESSAGE_ID = "^[A-Za-z0-9:._-]{1,240}$"
SHA256 = "^[0-9a-f]{64}$"
PHONE = "^[1-9][0-9]{6,14}$"
TIME = {"type": "number", "min": 1_700_000_000, "max": 4_102_444_799}
INT_TIME = {"type": "integer", "min": 1_700_000_000, "max": 4_102_444_799}
VOICE_STATES = ["accepted", "canceled", "deleted", "delivered", "expired", "failed", "held_for_review",
                "queued", "read", "uncertain", "waiting_for_reply"]
INBOX_KINDS = ["audio", "card_prompt", "delete_message", "install_update", "listened", "mute",
               "nfc_cancel", "nfc_enroll", "nfc_unpair", "preview_ringtone", "queue_hold", "quiet_now",
               "reaction", "set_update_mode", "settings", "voice_preview"]
ACK_STATES = ["applied", "expired", "failed", "played", "received", "rejected"]


def string(pattern=None, **extra):
    return {"type": "string", **({"pattern": pattern} if pattern else {}), **extra}


def optional(spec):
    return {**spec, "optional": True}


def json_request(body):
    return {"content_type": "application/json", "body": body}


def json_response(*variants, status=(200,)):
    """The box accepts a response that matches any one variant."""
    return {"content": "json", "status": list(status), "variants": [{"fields": fields} for fields in variants]}


SPEC = {
    "messagebox/cloud_device.py::CloudDeviceClient.register": [{
        "id": "register",
        "method": "POST", "path": "/device/register", "literal": "/device/register",
        "device_state": "new",
        "auth": "body_credential",
        "request": json_request({
            "device_id": string(ID),
            "credential": string("^[A-Za-z0-9_-]{43,128}$"),
            "capabilities": {"type": "object"},
            "software_version": optional({"type": "string"}),
            "software_commit": optional({"type": "string"}),
        }),
        "response": json_response(
            {"claimed": {"type": "boolean", "const": True}},
            {"claim_id": string(ID), "claim_token": string(ID), "expires_at": INT_TIME,
             "whatsapp_url": string("^https://wa\\.me/[1-9][0-9]{6,14}\\?text=",
                                    prefill_text_one_of="registration_texts")},
        ),
        "box_reads": ("claimed=true ends setup; otherwise the claim link and expiry start the setup page. "
                      "The link text must be one of registration_texts with the claim_token filled in."),
    }],
    "messagebox/cloud_device.py::CloudDeviceClient.claim": [{
        "id": "claim_status",
        "method": "GET", "path": "/device/claim", "literal": "/device/claim",
        "device_state": "unclaimed",
        "response": json_response({"claimed": {"type": "boolean"}}),
        "box_reads": "claimed=true removes the local claim and shows the box as connected.",
    }],
    "messagebox/cloud_device.py::CloudDeviceClient.setup_checkin": [{
        "id": "setup_checkin",
        "method": "POST", "path": "/device/setup-checkin", "literal": "/device/setup-checkin",
        "device_state": "unclaimed",
        "request": json_request({"device_id": string(ID)}),
        "response": json_response(
            {"claimed": {"type": "boolean", "const": True}},
            {"claimed": {"type": "boolean", "const": False},
             "pending_claim": {"type": "object", "nullable": True, "exact": True,
                               "fields": {"claim_id": string(ID), "expires_at": {"type": "integer"}}}},
        ),
        "box_reads": "pending_claim must have exactly claim_id and expires_at, or be null.",
    }],
    "messagebox/cloud_device.py::CloudDeviceClient.confirm_claim": [{
        "id": "claim_confirm",
        "method": "POST", "path": "/device/claim/confirm", "literal": "/device/claim/confirm",
        "device_state": "unclaimed",
        "request": json_request({"claim_id": string(ID)}),
        "response": json_response({"claimed": {"type": "boolean"}}),
        "box_reads": "claimed=true finishes setup; claimed=false records the button press.",
    }],
    "messagebox/cloud_device.py::CloudDeviceClient.cancel_claim": [{
        "id": "claim_cancel",
        "method": "POST", "path": "/device/claim/cancel", "literal": "/device/claim/cancel",
        "device_state": "unclaimed",
        "request": json_request({"claim_id": string(ID)}),
        "response": json_response(
            {"claim_id": string(ID, equals_request="claim_id"),
             "claimed": {"type": "boolean", "const": False}, "cancelled": {"type": "boolean", "const": True}},
            {"claim_id": string(ID, equals_request="claim_id"),
             "claimed": {"type": "boolean", "const": True}, "cancelled": {"type": "boolean", "const": False}},
        ),
        "box_reads": "The setup page shows status cancelled or claimed. Exactly one of the two flags is true.",
    }],
    "messagebox/cloud_device.py::CloudDeviceClient.heartbeat": [{
        "id": "heartbeat",
        "method": "POST", "path": "/device/heartbeat", "literal": "/device/heartbeat",
        "device_state": "claimed",
        "request": json_request({
            "version": {"type": "string"},
            "capabilities": {"type": "object"},
            "settings": {"type": "object"},
            "applied_revision": {"type": "integer"},
            "quiet_state": {"type": "object"},
            "queue": {"type": "object"},
            "last_error": {"type": "string", "nullable": True},
            "software_version": optional({"type": "string"}),
            "software_commit": optional({"type": "string"}),
            "software_update": optional({"type": "object"}),
            "remote_access": optional({"type": "object"}),
            "nfc_inventory": optional({"type": "object"}),
        }),
        "response": json_response({
            "box_id": string(RUNTIME_ID),
            "server_time": TIME,
            "account_scope": string(SHA256),
            "people": {"type": "array", "max_items": 100, "items": {"type": "object", "fields": {
                "id": string(RUNTIME_ID), "wa_id": string(PHONE),
                "display_name": {"type": "string", "max_length": 100},
                "role": {"type": "string", "enum": ["coadmin", "family", "owner"]},
            }}},
            "entitlement": {"type": "object", "fields": {
                "ingest": {"type": "boolean"}, "deliver": {"type": "boolean"}, "send": {"type": "boolean"},
                "until": optional({**TIME, "nullable": True}),
            }},
            "queue_hold": {"type": "boolean"},
            "retention_days": {"type": "integer", "enum": [7, 30, 90]},
            "desired_revision": {"type": "integer", "min": 0},
            "default_recipient_id": optional(string(RUNTIME_ID, nullable=True)),
            "connected_boxes": optional({"type": "array"}),
            "reaction_cards": optional({"type": "array", "max_items": 6}),
            "kit_cards": optional({"type": "array"}),
            "voice_features": optional({"type": "object"}),
        }),
        "box_reads": "Any invalid required field makes the box drop the heartbeat and keep its old snapshot.",
    }],
    "messagebox/cloud_device.py::CloudDeviceClient.inbox": [{
        "id": "inbox",
        "method": "GET", "path": "/device/inbox", "literal": "/device/inbox?cursor=",
        "query": {"cursor": {"type": "integer", "min": 0}},
        "device_state": "claimed",
        "response": json_response({
            "box_id": string(RUNTIME_ID),
            "server_time": TIME,
            "deleted": optional({"type": "boolean"}),
            "items": {"type": "array", "items": {"type": "object", "fields": {
                "operation_id": string(RUNTIME_ID),
                "sequence": {"type": "integer", "min": 0},
                "kind": {"type": "string", "enum": INBOX_KINDS},
                "created_at": TIME, "expires_at": TIME,
                "payload": {"type": "object"},
            }}},
        }),
        "box_reads": "One item with an unknown kind makes the box reject the whole inbox.",
    }],
    "messagebox/cloud_device.py::CloudDeviceClient.ack": [{
        "id": "ack",
        "method": "POST", "path": "/device/ack", "literal": "/device/ack",
        "device_state": "claimed",
        "request": json_request({
            "operation_id": string(RUNTIME_ID),
            "state": {"type": "string", "enum": ACK_STATES},
            "error_code": optional({"type": "string"}),
        }),
        "response": json_response({
            "operation_id": string(RUNTIME_ID, equals_request="operation_id"),
            "accepted": {"type": "boolean", "const": True},
        }),
        "errors": [{"status": 404, "code": "operation_not_found",
                    "box_reads": "The box files the ACK as gone and stops retrying it."}],
        "box_reads": "Software-update ACK states are covered by the Cloud e2e update simulator.",
    }],
    "messagebox/cloud_device.py::CloudDeviceClient.voice_line": [{
        "id": "voice_line",
        "method": "POST", "path": "/device/voice-lines", "literal": "/device/voice-lines",
        "device_state": "claimed",
        "capabilities": {"voice_lines": 1},
        "request": json_request({
            "template": {"type": "string"},
            "identity_id": optional(string(RUNTIME_ID)),
            "link_id": optional(string(RUNTIME_ID)),
        }),
        "response": json_response(
            {"use_fallback": {"type": "boolean", "const": True}},
            {"clip_id": string(SHA256), "sha256": string(SHA256),
             "content_type": {"type": "string", "const": "audio/wav"},
             "media_url": string("^https://"),
             "voice_pack": optional({"type": "string", "nullable": True})},
        ),
        "box_reads": "Any other answer makes the box play its built-in fallback sound.",
    }],
    "messagebox/cloud_device.py::CloudDeviceClient.send_reaction": [{
        "id": "reaction",
        "method": "POST", "path": "/device/reactions", "literal": "/device/reactions",
        "device_state": "claimed",
        "capabilities": {"reactions": 1},
        "request": json_request({
            "message_id": string(MESSAGE_ID),
            "operation_id": string(ID),
            "card_id": optional({"type": "string"}),
        }),
        "response": json_response({"state": {"type": "string", "enum": ["nothing_to_react_to", "sent", "uncertain"]}}),
    }],
    "messagebox/cloud_device.py::CloudDeviceClient.voice_status": [{
        "id": "voice_status",
        "method": "GET", "path": "/device/voice/status", "literal": "/device/voice/status?idempotency_key=",
        "query": {"idempotency_key": string(ID)},
        "device_state": "claimed",
        "response": json_response({
            "message_id": string(MESSAGE_ID),
            "state": {"type": "string", "enum": VOICE_STATES},
            "expires_at": INT_TIME, "server_time": INT_TIME,
            "deleted": {"type": "boolean"},
        }),
        "errors": [{"status": 404, "code": "voice_upload_not_found",
                    "box_reads": "The upload never arrived; the box may upload it again."}],
    }],
    "messagebox/cloud_device.py::CloudDeviceClient.media": [
        {"id": "media_message", "method": "GET", "path": "/device/media/{message_id}",
         "literal": "/cloud-api/v1/device/media/", "params": {"message_id": "^[A-Za-z0-9_-]{1,128}$"},
         "device_state": "claimed", "response": {"content": "bytes", "status": [200]},
         "box_reads": "The box takes this URL from an inbox audio item."},
        {"id": "media_listened", "method": "GET", "path": "/device/listened-media/{clip}",
         "literal": "/cloud-api/v1/device/listened-media/", "params": {"clip": SHA256},
         "device_state": "claimed", "response": {"content": "bytes", "status": [200]},
         "box_reads": "The box takes this URL from an inbox listened item."},
        {"id": "media_voice_line", "method": "GET", "path": "/device/voice-lines/{clip_id}",
         "literal": "/cloud-api/v1/device/voice-lines/", "params": {"clip_id": SHA256},
         "device_state": "claimed", "capabilities": {"voice_lines": 1},
         "response": {"content": "bytes", "status": [200]},
         "box_reads": "The box takes this URL from a voice line or reaction payload."},
    ],
    "messagebox/cloud_device.py::CloudDeviceClient.send_voice": [{
        "id": "voice_upload",
        "method": "POST", "path": "/device/voice", "literal": "/device/voice",
        "device_state": "claimed",
        "request": {
            "content_type": "multipart/form-data",
            "headers": {"Idempotency-Key": string(ID)},
            "body": {
                "recipient_id": string("^[A-Za-z0-9_-]{1,128}$"),
                "account_scope": string(SHA256),
                "idempotency_key": string(ID),
                "duration_seconds": {"type": "string"},
                "reply_to": optional(string(MESSAGE_ID)),
                "audio": {"type": "file", "content_type": "audio/ogg"},
            },
        },
        "response": json_response({
            "message_id": string(MESSAGE_ID),
            "state": {"type": "string", "enum": VOICE_STATES},
            "expires_at": INT_TIME, "server_time": INT_TIME,
        }, status=(200, 202)),
        "box_reads": "400, 401, 403, 404, 409, 413 and 422 are final rejections; other failures are retried.",
    }],
    "messagebox/cloud_events.py::connect_events": [{
        "id": "events",
        "method": "GET", "path": "/device/events", "literal": "/device/events",
        "device_state": "claimed",
        "optional": True,
        "request": {"headers": {"Upgrade": {"type": "string", "const": "websocket"}}},
        "response": {"content": "websocket", "status": [101]},
        "box_reads": "Work hints only. Without them the box keeps polling the inbox.",
    }],
}


def _device_literals(node):
    """Yield (literal, method) for each device path string in a function body."""
    methods = {}
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            verb = next((kw.value.value for kw in call.keywords if kw.arg == "method"
                         and isinstance(kw.value, ast.Constant)), None)
            for child in ast.walk(call):
                if verb and isinstance(child, ast.Constant):
                    methods.setdefault(id(child), verb)
    for child in ast.walk(node):
        if (isinstance(child, ast.Constant) and isinstance(child.value, str)
                and child.value.startswith(("/device/", API_BASE + "/device/"))
                and child.value not in {"/device/", API_BASE + "/device/"}):  # path guards, not calls
            yield child.value, methods.get(id(child), "GET")


def scan(repo=REPO):
    """Map 'file::qualname' to the sorted (literal, method) pairs found in it."""
    found = {}
    for relative, owner in SCANNED.items():
        tree = ast.parse((repo / relative).read_text(encoding="utf-8"))
        scopes = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == owner:
                scopes += [(f"{owner}.{item.name}", item) for item in node.body
                           if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scopes.append((node.name, node))
        for name, node in scopes:
            literals = sorted(set(_device_literals(node)))
            if literals:
                found[f"{relative}::{name}"] = literals
    return found


def registration_texts(repo=REPO):
    """The WhatsApp prefill texts the setup page accepts, from cloud_claim._registration_texts."""
    tree = ast.parse((repo / "messagebox" / "cloud_claim.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_registration_texts":
            returned = next(child.value for child in ast.walk(node) if isinstance(child, ast.Return))
            texts = []
            for item in returned.elts:
                if not (isinstance(item, ast.BinOp) and isinstance(item.op, ast.Add)
                        and isinstance(item.left, ast.Constant) and isinstance(item.right, ast.Name)):
                    raise ValueError("_registration_texts must return 'text ' + token values")
                texts.append(item.left.value + "{claim_token}")
            return texts
    raise ValueError("_registration_texts() was not found")


def advertised_capabilities(repo=REPO):
    tree = ast.parse((repo / "messagebox" / "cloud_device.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "capabilities":
            for child in ast.walk(node):
                if isinstance(child, ast.Dict):
                    return {key.value: value.value for key, value in zip(child.keys, child.values)
                            if isinstance(key, ast.Constant) and isinstance(value, ast.Constant)}
    raise ValueError("capabilities() was not found")


def build(repo=REPO):
    found = scan(repo)
    errors = []
    for source in sorted(set(found) - set(SPEC)):
        errors.append(f"{source} calls {found[source]} but has no entry in scripts/dev/cloud_contract.py SPEC")
    for source in sorted(set(SPEC) - set(found)):
        errors.append(f"SPEC lists {source}, but that function makes no device call")
    capabilities = advertised_capabilities(repo)
    entries = []
    for source in sorted(set(found) & set(SPEC)):
        declared = sorted((entry["literal"], entry["method"]) for entry in SPEC[source])
        if declared != found[source]:
            errors.append(f"{source}: source calls {found[source]}, SPEC declares {declared}")
        for entry in SPEC[source]:
            for key, value in entry.get("capabilities", {}).items():
                if capabilities.get(key) != value:
                    errors.append(f"{entry['id']}: capabilities() does not advertise {key}={value!r}")
            entries.append({key: value for key, value in entry.items() if key != "literal"} | {"source": source})
    ids = [entry["id"] for entry in entries]
    if len(ids) != len(set(ids)):
        errors.append("entry ids are not unique")
    if errors:
        raise ValueError("\n".join(errors))
    entries.sort(key=lambda entry: (entry["path"], entry["method"]))
    return {
        "schema": 1,
        "description": "Every Cloud request the box client makes. Generated by scripts/dev/cloud_contract.py.",
        "api_base": API_BASE,
        "common": {
            "auth": "Authorization: Bearer <device credential> unless an entry says otherwise",
            "json_success_status": 200,
            "error_body": {"error": {"code": "string"}},
            "rate_limited": {"status": 429, "header": "Retry-After"},
            "unknown_response_fields": "ignored",
        },
        "registration_texts": registration_texts(repo),
        "entries": entries,
    }


def render(document):
    return json.dumps(document, indent=2, sort_keys=False) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the committed manifest is stale")
    parser.add_argument("--repo", type=Path, default=REPO)
    args = parser.parse_args(argv)
    try:
        expected = render(build(args.repo))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    path = args.repo / MANIFEST
    if args.check:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current != expected:
            print(f"{MANIFEST} is stale. Run: python3 scripts/dev/cloud_contract.py", file=sys.stderr)
            return 1
        return 0
    path.write_text(expected, encoding="utf-8")
    print(f"wrote {MANIFEST} ({len(json.loads(expected)['entries'])} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
