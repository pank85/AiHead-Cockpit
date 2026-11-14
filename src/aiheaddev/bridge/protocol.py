"""Bounded, strict newline-delimited JSON envelopes for the private bridge."""
from __future__ import annotations

import json
import math
from typing import Any, BinaryIO

PROTOCOL = 1
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
METHODS = frozenset({
    "initialize", "catalog.get", "project.open", "folders.list", "folders.test",
    "settings.validate", "settings.save", "inventory.refresh", "selection.set",
    "reply.submit", "terminal.input", "launch.master", "launch.ergo",
    "launch.orchestration", "launch.council", "aihead.open", "workspace.delete",
    "logs.get", "shutdown",
})


class BridgeError(Exception):
    """A safe error suitable for serialization as a response."""
    def __init__(self, code: str, message: str, *, field_errors: dict[str, str] | None = None,
                 outcome: str | None = None) -> None:
        super().__init__(message)
        self.code, self.message = code, message
        self.field_errors, self.outcome = field_errors, outcome

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field_errors is not None:
            result["field_errors"] = self.field_errors
        if self.outcome is not None:
            result["outcome"] = self.outcome
        return result


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def decode(line: bytes) -> dict[str, Any]:
    if len(line) > MAX_MESSAGE_BYTES:
        raise BridgeError("message_too_large", "Message exceeds 2 MiB")
    if not line.endswith(b"\n") or b"\n" in line[:-1]:
        raise BridgeError("malformed_message", "Expected one newline-terminated JSON message")
    try:
        value = json.loads(line.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid number")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise BridgeError("malformed_message", "Invalid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise BridgeError("malformed_message", "Envelope must be an object")
    _bounded(value)
    return value


def encode(value: dict[str, Any]) -> bytes:
    if not isinstance(value, dict):
        raise BridgeError("malformed_message", "Envelope must be an object")
    _bounded(value)
    try:
        data = (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise BridgeError("malformed_message", "Envelope is not JSON serializable") from exc
    if len(data) > MAX_MESSAGE_BYTES:
        raise BridgeError("message_too_large", "Message exceeds 2 MiB")
    return data


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    line = stream.readline(MAX_MESSAGE_BYTES + 1)
    if not line:
        return None
    return decode(line)


def validate_request(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {"id", "method", "params"}:
        raise BridgeError("invalid_request", "Request requires id, method and params")
    if not (_identity(value["id"]) or _integer(value["id"])):
        raise BridgeError("invalid_request", "Request id must be a nonempty string or nonnegative integer")
    if not isinstance(value["method"], str) or value["method"] not in METHODS:
        raise BridgeError("unknown_method", "Method is not allowed")
    if not isinstance(value["params"], dict):
        raise BridgeError("invalid_request", "Params must be an object")
    if value["method"] == "initialize":
        version = value["params"].get("protocol")
        if type(version) is not int or version != PROTOCOL:
            raise BridgeError("incompatible_protocol", "Expected protocol version 1")
        params = value["params"]
        if set(params) - {"protocol", "capabilities", "session", "demo"}:
            raise BridgeError("invalid_params", "Unknown initialize parameter")
        if not isinstance(params.get("capabilities", []), list) or any(not isinstance(x, str) for x in params.get("capabilities", [])):
            raise BridgeError("invalid_params", "Capabilities must be an array of strings")
        if params.get("session") is not None and not isinstance(params["session"], str):
            raise BridgeError("invalid_params", "Session must be a string or null")
        if type(params.get("demo", False)) is not bool:
            raise BridgeError("invalid_params", "Demo must be a boolean")
    _validate_params(value["method"], value["params"])
    return value


def validate_target(value: Any, *, live: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"socket_path", "workspace_id", "tab_id", "pane_id", "terminal_id"}:
        raise BridgeError("invalid_params", "Invalid target")
    required = {"socket_path", "workspace_id"} | ({"pane_id", "terminal_id"} if live else set())
    if any(not isinstance(value.get(k), str) or not value[k] for k in required):
        raise BridgeError("invalid_params", "Target identity is incomplete")
    if any(v is not None and (not isinstance(v, str) or not v or len(v) > 4096) for v in value.values()):
        raise BridgeError("invalid_params", "Invalid target identity")
    if value.get("terminal_id") and not value.get("pane_id"):
        raise BridgeError("invalid_params", "A terminal target requires its pane")
    return value


def validate_lease(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"target", "generation", "terminal_id"}:
        raise BridgeError("invalid_params", "Invalid lease")
    target = validate_target(value["target"], live=True)
    if type(value["generation"]) is not int or value["generation"] < 0 or value["terminal_id"] != target["terminal_id"]:
        raise BridgeError("invalid_params", "Invalid lease generation or terminal identity")
    return value


# The codec rejects structural ambiguity; domain/catalog checks remain in the
# injectable service/runtime. Optional keys are omitted or JSON null.
def _string(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return len(value.encode("utf-8")) <= 256 * 1024
    except UnicodeError:
        return False


def _identity(value: Any) -> bool:
    return _string(value) and bool(value.strip()) and len(value) <= 4096


def _boolean(value: Any) -> bool:
    return type(value) is bool


def _integer(value: Any) -> bool:
    return type(value) is int and 0 <= value <= 2**53 - 1


def _object(value: Any) -> bool:
    return isinstance(value, dict)


def _strings(value: Any) -> bool:
    return isinstance(value, list) and len(value) <= 4096 and all(_identity(item) for item in value)


def _bounded(value: Any, depth: int = 0) -> None:
    if depth > 32:
        raise BridgeError("invalid_params", "Structure exceeds nesting limit")
    if isinstance(value, dict):
        if len(value) > 4096 or any(not isinstance(k, str) or len(k) > 4096 for k in value):
            raise BridgeError("invalid_params", "Object exceeds structural limit")
        for item in value.values(): _bounded(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        if len(value) > 4096:
            raise BridgeError("invalid_params", "Array exceeds structural limit")
        for item in value: _bounded(item, depth + 1)
    elif isinstance(value, str):
        if not _string(value): raise BridgeError("invalid_params", "String exceeds structural limit")
    elif isinstance(value, float) and not math.isfinite(value):
        raise BridgeError("invalid_params", "Number must be finite")
    elif value is not None and type(value) not in (bool, int, float):
        raise BridgeError("invalid_params", "Value is not JSON data")


# A field specification is (validator, required, nullable).
_R = lambda fn: (fn, True, False)
_O = lambda fn: (fn, False, True)
_B = lambda fn: (fn, False, False)
_PROJECT = {"project_path": _R(_identity), "session": _O(_identity)}
_ROLE_MODELS = {role + "_model": _R(_identity) for role in ("scout", "architect", "reviewer")}
_ROLE_EFFORTS = {role + "_effort": _O(_identity) for role in ("scout", "architect", "reviewer")}
_SCHEMAS = {
    "initialize": {"protocol": _R(_integer), "capabilities": _B(_strings), "session": _O(_identity), "demo": _B(_boolean)},
    "catalog.get": {}, "inventory.refresh": {}, "shutdown": {},
    "project.open": {"path": _R(_identity)},
    "folders.list": {"path": _R(_identity), "project": _O(_identity)},
    "folders.test": {"path": _R(_identity), "project": _O(_identity)},
    "settings.validate": {"token": _R(_identity), "revision": _R(_integer), "sections": _B(_object), "discard": _B(_boolean)},
    "settings.save": {"token": _R(_identity), "revision": _R(_integer), "sections": _R(_object)},
    "selection.set": {"target": _R(_object)},
    "reply.submit": {"lease": _R(_object), "text": _R(_string), "notify": _R(_boolean)},
    "terminal.input": {"lease": _R(_object), "text": _B(_string), "keys": _B(_strings)},
    "launch.master": {**_PROJECT, "name": _R(_identity), "harness": _R(_identity), "model": _R(_identity),
                      "effort": _O(_identity), "task": _O(_string), "ai_head": _O(_boolean)},
    "launch.ergo": {**_PROJECT, "intent": _R(_string), "label": _R(_string), "squad": _O(_object),
                    "customize_first": _R(_boolean), "start_pipeline": _R(_boolean),
                    "save_default": _R(_boolean), "ai_head": _O(_boolean)},
    "launch.orchestration": {**_PROJECT, "intent": _R(_string), "label": _R(_string), "harness": _R(_identity),
                             **_ROLE_MODELS, **_ROLE_EFFORTS, "worker_models": _R(_strings),
                             "dangerously_skip_permissions": _R(_boolean), "ai_head": _O(_boolean)},
    "launch.council": {**_PROJECT, "idea": _R(_string), "label": _R(_string), "config": _R(_object),
                       "customize_roster": _R(_boolean)},
    "aihead.open": {**_PROJECT},
    "workspace.delete": {"target": _R(_object), "confirmed": _R(_boolean)},
    "logs.get": {"limit": _B(_integer)},
}


def _validate_params(method: str, params: dict[str, Any]) -> None:
    _bounded(params)
    schema = _SCHEMAS[method]
    if set(params) - set(schema):
        raise BridgeError("invalid_params", "Unknown parameter")
    for key, (check, required, nullable) in schema.items():
        if key not in params:
            if required: raise BridgeError("invalid_params", f"Missing parameter: {key}")
            continue
        value = params[key]
        if value is None and nullable: continue
        if not check(value): raise BridgeError("invalid_params", f"Invalid parameter: {key}")
    if method == "settings.validate":
        if params.get("discard", False) == ("sections" in params):
            raise BridgeError("invalid_params", "Supply sections or discard the draft, but not both")
    if method == "selection.set": validate_target(params["target"])
    if method == "workspace.delete":
        target = validate_target(params["target"])
        if target.get("tab_id") or target.get("pane_id") or params["confirmed"] is not True:
            raise BridgeError("invalid_params", "Deletion requires a confirmed exact workspace target")
    if method in {"reply.submit", "terminal.input"}: validate_lease(params["lease"])
    if method == "terminal.input" and ("text" in params) == ("keys" in params):
        raise BridgeError("invalid_params", "Terminal input requires exactly one of text or keys")
    if method == "launch.orchestration" and not params["worker_models"]:
        raise BridgeError("invalid_params", "At least one worker model is required")
    if method == "logs.get" and "limit" in params and not 1 <= params["limit"] <= 1000:
        raise BridgeError("invalid_params", "Log limit must be between 1 and 1000")


def validate_response(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) not in ({"id", "result"}, {"id", "error"}):
        raise BridgeError("invalid_response", "Invalid response envelope")
    if not (_identity(value["id"]) or _integer(value["id"])):
        raise BridgeError("invalid_response", "Invalid response id")
    if "error" in value:
        error = value["error"]
        if not isinstance(error, dict) or not {"code", "message"} <= set(error) or set(error) - {"code", "message", "field_errors", "outcome"}:
            raise BridgeError("invalid_response", "Invalid error")
        if not _identity(error["code"]) or not _string(error["message"]):
            raise BridgeError("invalid_response", "Invalid error fields")
        if "outcome" in error and (not isinstance(error["outcome"], str) or error["outcome"] not in {"accepted", "rejected", "uncertain"}):
            raise BridgeError("invalid_response", "Invalid error outcome")
        if "field_errors" in error and (not isinstance(error["field_errors"], dict) or any(not _string(k) or not _string(v) for k, v in error["field_errors"].items())):
            raise BridgeError("invalid_response", "Invalid field errors")
    _bounded(value)
    return value


def _event_fields(value: Any, schema: dict[str, tuple[Any, bool, bool]]) -> None:
    if not isinstance(value, dict) or set(value) - set(schema):
        raise BridgeError("invalid_event", "Invalid event object")
    for key, (check, required, nullable) in schema.items():
        if key not in value:
            if required: raise BridgeError("invalid_event", f"Missing event field: {key}")
        elif not (value[key] is None and nullable) and not check(value[key]):
            raise BridgeError("invalid_event", f"Invalid event field: {key}")


def _readiness(value: Any) -> bool:
    _event_fields(value, {"can_reply": _R(_boolean), "explanation": _R(_string), "state": _R(_identity)})
    return True


def _layout(value: Any) -> bool:
    _event_fields(value, {"state": _R(_identity), "rects": _R(lambda v: isinstance(v, list)), "focused_pane_id": _O(_identity)})
    if value["state"] not in {"available", "layout_unavailable"}:
        raise BridgeError("invalid_event", "Invalid layout state")
    if value["state"] == "layout_unavailable" and value["rects"]:
        raise BridgeError("invalid_event", "Unavailable layout cannot contain invented rectangles")
    for rect in value["rects"]:
        _event_fields(rect, {"pane_id": _R(_identity), **{k: _R(_integer) for k in ("x", "y", "width", "height")}})
        if not rect["width"] or not rect["height"]:
            raise BridgeError("invalid_event", "Empty pane rectangle")
    return True


def _inventory_session(value: Any) -> None:
    _event_fields(value, {"socket_path": _R(_identity), "session": _O(_identity), "connection": _R(_identity),
                          "error": _O(_string), "workspaces": _R(lambda v: isinstance(v, list))})
    if value["connection"] not in {"connected", "offline", "error"}:
        raise BridgeError("invalid_event", "Invalid connection state")
    for workspace in value["workspaces"]:
        _event_fields(workspace, {"workspace_id": _R(_identity), "label": _R(_string), "tabs": _R(lambda v: isinstance(v, list)),
                                  "agents": _R(lambda v: isinstance(v, list)), "metadata": _R(_object)})
        for agent in workspace["agents"]:
            _event_fields(agent, {"pane_id": _O(_identity), "agent": _R(_string), "role": _O(_string),
                                  "state": _R(_identity), "readiness": _O(_readiness), "metadata": _R(_object)})
        for tab in workspace["tabs"]:
            _event_fields(tab, {"tab_id": _R(_identity), "label": _R(_string), "panes": _R(lambda v: isinstance(v, list)), "layout": _R(_layout)})
            pane_ids = set()
            for pane in tab["panes"]:
                _event_fields(pane, {"pane_id": _R(_identity), "terminal_id": _O(_identity), "label": _R(_string),
                                      "state": _R(_identity), "role": _O(_string), "readiness": _O(_readiness), "metadata": _R(_object)})
                if pane["pane_id"] in pane_ids: raise BridgeError("invalid_event", "Duplicate pane")
                pane_ids.add(pane["pane_id"])
            layout = tab["layout"]
            if any(rect["pane_id"] not in pane_ids for rect in layout["rects"]) or (layout.get("focused_pane_id") and layout["focused_pane_id"] not in pane_ids):
                raise BridgeError("invalid_event", "Layout contains a foreign pane")


def validate_event(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"event", "revision", "payload"}:
        raise BridgeError("invalid_event", "Invalid event envelope")
    if not isinstance(value["event"], str) or value["event"] not in {"inventory", "pane_frame", "reply_result", "operation_status"} or not _integer(value["revision"]) or not isinstance(value["payload"], dict):
        raise BridgeError("invalid_event", "Invalid event fields")
    payload = value["payload"]
    _bounded(payload)
    if value["event"] == "inventory":
        if set(payload) != {"sessions"} or not isinstance(payload["sessions"], list):
            raise BridgeError("invalid_event", "Invalid inventory")
        for session in payload["sessions"]: _inventory_session(session)
    elif value["event"] == "pane_frame":
        if set(payload) != {"target", "lease", "text", "readiness", "layout"} or not _string(payload["text"]):
            raise BridgeError("invalid_event", "Invalid pane frame")
        if len(payload["text"].encode("utf-8")) > 256 * 1024 or len(payload["text"].splitlines()) > 1000:
            raise BridgeError("invalid_event", "Pane preview exceeds limit")
        validate_target(payload["target"])
        if not payload["target"].get("pane_id"):
            raise BridgeError("invalid_event", "Pane frame requires a pane target")
        if payload["lease"] is not None:
            validate_lease(payload["lease"])
            if payload["lease"]["target"] != payload["target"]:
                raise BridgeError("invalid_event", "Pane and lease targets differ")
        _readiness(payload["readiness"])
        _layout(payload["layout"])
    else:
        required = {"request_id", "outcome", "message"}
        allowed = required | ({"phase", "status", "target"} if value["event"] == "operation_status" else {"lease"})
        if not required <= set(payload) or set(payload) - allowed or not isinstance(payload["outcome"], str) or payload["outcome"] not in {"accepted", "rejected", "uncertain", "pending"} or not _string(payload["message"]):
            raise BridgeError("invalid_event", "Invalid operation result")
        if not (_identity(payload["request_id"]) or _integer(payload["request_id"])):
            raise BridgeError("invalid_event", "Invalid request id")
        if "lease" in payload and payload["lease"] is not None: validate_lease(payload["lease"])
        if "target" in payload and payload["target"] is not None: validate_target(payload["target"])
        for key in ("phase", "status"):
            if key in payload and not _string(payload[key]): raise BridgeError("invalid_event", f"Invalid {key}")
    return value


def validate_outcome(value: Any) -> dict[str, Any]:
    """Validate the dedicated wrapper-pipe outcome, never a process command."""
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise BridgeError("invalid_outcome", "Invalid UI outcome")
    kind = value["type"]
    schemas = {
        "quit": {},
        "attach": {"session": _R(_identity), "workspace_id": _O(_identity), "tab_id": _O(_identity), "pane_id": _O(_identity)},
        "workspace": {"session": _O(_identity), "workspace_id": _R(_identity)},
    }
    if kind not in schemas: raise BridgeError("invalid_outcome", "Unknown UI outcome")
    fields = {k: v for k, v in value.items() if k != "type"}
    try: _event_fields(fields, schemas[kind])
    except BridgeError as exc: raise BridgeError("invalid_outcome", str(exc)) from exc
    return value
