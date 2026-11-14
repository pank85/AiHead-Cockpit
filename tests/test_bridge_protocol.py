import io
import json
from pathlib import Path

import pytest

from aiheaddev.bridge.protocol import (BridgeError, MAX_MESSAGE_BYTES, decode, encode,
                                        read_message, validate_event, validate_lease, validate_outcome, validate_request, validate_response)


def test_envelopes_and_allowlist():
    request = {"id": 1, "method": "initialize", "params": {"protocol": 1}}
    assert validate_request(decode(encode(request))) == request
    with pytest.raises(BridgeError, match="version 1"):
        validate_request({**request, "params": {"protocol": 2}})
    with pytest.raises(BridgeError, match="not allowed"):
        validate_request({**request, "method": "exec"})
    assert read_message(io.BytesIO()) is None


def test_malformed_and_oversize():
    for value in (b'{}', b'[]\n', b'{"a":1,"a":2}\n', b'{"x":NaN}\n', b'\xff\n'):
        with pytest.raises(BridgeError): decode(value)
    with pytest.raises(BridgeError): decode(b' ' * MAX_MESSAGE_BYTES + b'\n')
    with pytest.raises(BridgeError): encode({"x": "a" * MAX_MESSAGE_BYTES})


def test_shared_fixture():
    fixture = json.loads((Path(__file__).parent / 'fixtures' / 'rust_bridge_v1.json').read_text(encoding='utf-8'))
    assert validate_request(decode(encode(fixture['request']))) == fixture['request']
    assert validate_lease(fixture['lease']) == fixture['lease']
    for key in ('response', 'error', 'event'):
        assert decode(encode(fixture[key])) == fixture[key]
    assert validate_response(fixture['response']) == fixture['response']
    assert validate_response(fixture['error']) == fixture['error']
    assert validate_event(fixture['event']) == fixture['event']
    for event in fixture['events']: assert validate_event(event) == event
    for request in fixture['launch_requests']: assert validate_request(request) == request
    for outcome in fixture['outcomes']: assert validate_outcome(outcome) == outcome


def test_lease_identity():
    target = {"socket_path": "/tmp/s", "workspace_id": "w", "tab_id": "t", "pane_id": "p", "terminal_id": "term"}
    lease = {"target": target, "terminal_id": "term", "generation": 1}
    assert validate_lease(lease) == lease
    with pytest.raises(BridgeError): validate_lease({**lease, "terminal_id": "other"})


def test_method_specific_dtos_and_adversarial_values():
    target = {"socket_path": "/s", "workspace_id": "w", "tab_id": "t", "pane_id": "p", "terminal_id": "term"}
    lease = {"target": target, "generation": 2, "terminal_id": "term"}
    project = {"project_path": "/p", "session": None}
    cases = {
        "catalog.get": {}, "project.open": {"path": "/p"}, "folders.list": {"path": "/p"},
        "folders.test": {"path": "/p"}, "settings.validate": {"token": "t", "revision": 0, "sections": {}},
        "settings.save": {"token": "t", "revision": 0, "sections": {}}, "inventory.refresh": {},
        "selection.set": {"target": target}, "reply.submit": {"lease": lease, "text": "hello", "notify": True},
        "terminal.input": {"lease": lease, "text": "hello"},
        "launch.master": {**project, "name": "name", "harness": "pi", "model": "model", "ai_head": None},
        "launch.ergo": {**project, "intent": "idea", "label": "", "squad": None, "customize_first": False, "start_pipeline": True, "save_default": False},
        "launch.orchestration": {**project, "intent": "idea", "label": "", "harness": "pi", "scout_model": "s", "architect_model": "a", "reviewer_model": "r", "worker_models": ["w"], "dangerously_skip_permissions": False},
        "launch.council": {**project, "idea": "idea", "label": "", "config": {}, "customize_roster": False},
        "aihead.open": project, "workspace.delete": {"target": {"socket_path": "/s", "workspace_id": "w"}, "confirmed": True},
        "logs.get": {"limit": 100}, "shutdown": {},
    }
    for method, params in cases.items():
        request = {"id": 1, "method": method, "params": params}
        assert validate_request(request) == request
        with pytest.raises(BridgeError): validate_request({**request, "params": {**params, "unexpected": True}})
    invalid = [
        ("settings.save", {"token": "t", "revision": True, "sections": {}}),
        ("settings.save", {"token": "t", "revision": 0, "sections": {"a": [0] * 4097}}),
        ("workspace.delete", {"target": target, "confirmed": True}),
        ("workspace.delete", {"target": {"socket_path": "/s", "workspace_id": "w"}, "confirmed": False}),
        ("terminal.input", {"lease": lease, "text": "x", "keys": ["Enter"]}),
        ("terminal.input", {"lease": lease}),
        ("logs.get", {"limit": 1001}),
        ("project.open", {"path": ""}),
        ("launch.orchestration", {**cases["launch.orchestration"], "worker_models": []}),
        ("launch.master", {**cases["launch.master"], "ai_head": 1}),
    ]
    for method, params in invalid:
        with pytest.raises(BridgeError): validate_request({"id": 1, "method": method, "params": params})


def test_event_and_response_validation():
    target = {"socket_path": "/s", "workspace_id": "w", "tab_id": "t", "pane_id": "p", "terminal_id": "term"}
    lease = {"target": target, "generation": 2, "terminal_id": "term"}
    event = {"event": "pane_frame", "revision": 1, "payload": {"target": target, "lease": lease, "text": "hello", "readiness": {"can_reply": True, "explanation": "ready", "state": "ready"}, "layout": {"state": "layout_unavailable", "rects": []}}}
    assert validate_event(event) == event
    with pytest.raises(BridgeError): validate_event({**event, "payload": {**event['payload'], "text": "x\n" * 1001}})
    with pytest.raises(BridgeError): validate_event({**event, "payload": {**event['payload'], "lease": {**lease, "terminal_id": "other"}}})
    with pytest.raises(BridgeError): validate_event({**event, "event": []})
    with pytest.raises(BridgeError): validate_response({"id": 1, "error": {"code": "x", "message": "x", "outcome": "maybe"}})
    with pytest.raises(BridgeError): validate_response({"id": True, "result": {}})
    with pytest.raises(BridgeError): validate_outcome({"type": "attach", "session": "x", "args": ["/bin/sh"]})
    assert validate_outcome({"type": "attach", "session": "x", "pane_id": "p"})['pane_id'] == 'p'
