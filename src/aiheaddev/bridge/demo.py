"""Synthetic bridge backend: never discovers, writes, or contacts a live session."""
from __future__ import annotations

import copy
from typing import Any

from herdr_master.core.config import HerdrMasterConfig
from herdr_master.core.models import Squad
from .protocol import BridgeError
from .settings import catalog_get

SOCKET = "/demo/herdr.sock"
TARGET = {"socket_path": SOCKET, "workspace_id": "demo-workspace", "tab_id": "demo-tab", "pane_id": "demo-pane"}
LAYOUT = {"state": "layout_unavailable", "rects": []}


class DemoSettings:
    def __init__(self) -> None:
        self.revision = 0
        self.opened = False
        cfg = HerdrMasterConfig()
        self.sections = {"projects": cfg.projects.model_dump(mode="json"), "squad": Squad().model_dump(mode="json"),
                         "orchestration": cfg.orchestration.model_dump(mode="json"), "council": cfg.council.model_dump(mode="json"),
                         "ai_head": cfg.ai_head.model_dump(mode="json")}

    def project_open(self, path: str) -> dict[str, Any]:
        self.opened = True
        self.revision = 0
        return {"token": "demo", "revision": 0, "project_path": path, "destination": "(demo; not saved)",
                "scope": "new-local", "sections": copy.deepcopy(self.sections), "retained_metadata": {}, "catalog": catalog_get()}

    def _check(self, token: str, revision: int) -> None:
        if not self.opened or token != "demo":
            raise BridgeError("invalid_token", "Unknown demo draft")
        if revision != self.revision:
            raise BridgeError("stale_revision", "Demo draft revision changed")

    def discard(self, token: str, *, revision: int) -> None:
        self._check(token, revision)
        self.opened = False

    def validate(self, token: str, sections: dict[str, Any], *, revision: int) -> dict[str, Any]:
        self._check(token, revision)
        if set(sections) - set(self.sections):
            raise BridgeError("invalid_params", "Invalid settings sections")
        # Demo drafts are not persisted and are not claimed to pass live validation.
        self.revision += 1
        return {"valid": False, "field_errors": {"demo": "Demo settings cannot be saved"},
                "route_previews": {}, "revision": self.revision, "sections": copy.deepcopy(self.sections)}

    def save(self, token: str, sections: dict[str, Any], *, revision: int) -> dict[str, Any]:
        self._check(token, revision)
        raise BridgeError("demo_read_only", "Demo does not save settings")


class DemoRuntime:
    def __init__(self, emit, **_: Any) -> None:
        self.emit = emit

    async def refresh(self) -> None:
        self.emit("inventory", {"sessions": [{"socket_path": SOCKET, "session": "demo", "connection": "connected", "workspaces": [{
            "workspace_id": "demo-workspace", "label": "Demo workspace", "metadata": {}, "agents": [], "tabs": [{
                "tab_id": "demo-tab", "label": "Demo", "layout": LAYOUT, "panes": [{"pane_id": "demo-pane", "label": "Demo terminal",
                "state": "demo", "metadata": {}, "readiness": {"can_reply": False, "state": "demo", "explanation": "Synthetic read-only demo"}}]}]}]}]})

    async def select(self, target: dict[str, Any]) -> None:
        if any(target.get(k) != v for k, v in TARGET.items()) or target.get("terminal_id"):
            raise BridgeError("invalid_target", "Unknown demo target")
        self.emit("pane_frame", {"target": TARGET, "lease": None, "text": "AiHead Cockpit — demo\nNo live workspace was contacted.\n",
                                 "readiness": {"can_reply": False, "state": "demo", "explanation": "Synthetic read-only demo"}, "layout": LAYOUT})

    async def send(self, method: str, params: dict[str, Any]):
        raise BridgeError("demo_read_only", "Demo does not send input")

    async def launch(self, method: str, params: dict[str, Any]):
        raise BridgeError("demo_read_only", "Demo does not launch workspaces")

    async def open_aihead(self, params: dict[str, Any]):
        raise BridgeError("demo_read_only", "Demo does not create AiHead")

    async def delete(self, params: dict[str, Any]):
        raise BridgeError("demo_read_only", "Demo does not delete workspaces")

    async def close(self) -> None:
        pass
