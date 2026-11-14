"""Headless, socket-identity-scoped adapter for the existing Herdr controller.

No Textual widgets or application objects cross this boundary. Selection is read-only;
only explicitly requested mutations dispatch to Herdr, and none are retried here.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError
from herdr_master.core.catalog import ensure_herdr_routable, native_binding_error
from herdr_master.core.config import CouncilConfig, load_project_config
from herdr_master.core.models import Squad
from herdr_master.herdr.client import HerdrClient
from herdr_master.herdr.discovery import SessionInventory, discover_clients
from herdr_master.herdr.protocol import parse_pane_read
from herdr_master.pipeline.ai_head import DEAD_AGENT_STATUSES, _resolve_agent_liveness, ensure_herdr_ai_head
from herdr_master.pipeline.launcher import (
    launch_council, launch_ergo, launch_master, launch_orchestration, resolve_launch_context,
)
from herdr_master.pipeline.squad import describe_agent, resolve_squad

from aiheaddev.core.workspace_targets import WorkspaceTarget, workspace_agents
from aiheaddev.ui.tui.live_pane import (
    LivePaneController, LiveTargetError, PaneFrame, PaneLease, ReplyRequest, TerminalInput,
    bound_output, resolve_live_pane,
)
from .protocol import BridgeError
from .settings import SettingsService, folders_test

Emit = Callable[[str, dict[str, Any]], None]
RPC_TIMEOUT = 2.0
POLL_INTERVAL = 3.0
MAX_CONCURRENT_READS = 4
MAX_PREVIEWS = 64
UNAVAILABLE_LAYOUT = {"state": "layout_unavailable", "rects": []}
LIMITATION = ("Protocol 22 does not confirm whether user input is required. "
              "Native permission dialogs require Open in Herdr.")


def _wire_target(target: WorkspaceTarget, terminal_id: str | None = None) -> dict[str, str]:
    result = {"socket_path": str(target.socket_path), "workspace_id": target.workspace_id}
    if target.tab_id is not None:
        result["tab_id"] = target.tab_id
    if target.pane_id is not None:
        result["pane_id"] = target.pane_id
    if terminal_id is not None:
        result["terminal_id"] = terminal_id
    return result


def _wire_lease(target: WorkspaceTarget, lease: PaneLease | None) -> dict[str, Any] | None:
    if lease is None or not lease.terminal_id or not target.pane_id:
        return None
    if lease.key != (str(target.socket_path), target.workspace_id, target.pane_id):
        return None
    return {"target": _wire_target(target, lease.terminal_id),
            "generation": lease.generation, "terminal_id": lease.terminal_id}


def _readiness(can_reply: bool, state: str, explanation: str = LIMITATION) -> dict[str, Any]:
    return {"can_reply": bool(can_reply), "state": state or "unknown", "explanation": explanation}


def _layout(raw: Any, workspace_id: str, tab_id: str, pane_ids: set[str]) -> dict[str, Any]:
    """Validate authoritative ownership and rectangles; never synthesize geometry."""
    if not isinstance(raw, dict) or raw.get("workspace_id") != workspace_id or raw.get("tab_id") != tab_id:
        raise LiveTargetError("Layout ownership mismatch")
    panes = raw.get("panes")
    if not isinstance(panes, list) or not panes or len(panes) > MAX_PREVIEWS:
        raise LiveTargetError("Invalid layout panes")
    rects, seen, focused = [], set(), None
    for item in panes:
        if not isinstance(item, dict):
            raise LiveTargetError("Invalid layout pane")
        pane_id = item.get("pane_id")
        rect = item.get("rect")
        if not isinstance(pane_id, str) or pane_id not in pane_ids or pane_id in seen or not isinstance(rect, dict):
            raise LiveTargetError("Foreign or duplicate layout pane")
        seen.add(pane_id)
        values = [rect.get(k) for k in ("x", "y", "width", "height")]
        if any(type(x) is not int or x < 0 or x > 2**31 - 1 for x in values) or not values[2] or not values[3]:
            raise LiveTargetError("Invalid layout rectangle")
        rects.append(dict(zip(("pane_id", "x", "y", "width", "height"), [pane_id, *values])))
        if item.get("focused") is True:
            if focused is not None:
                raise LiveTargetError("Multiple focused layout panes")
            focused = pane_id
    explicit_focused = raw.get("focused_pane_id")
    if explicit_focused is not None:
        if explicit_focused not in seen or (focused is not None and focused != explicit_focused):
            raise LiveTargetError("Foreign focused layout pane")
        focused = explicit_focused
    result: dict[str, Any] = {"state": "available", "rects": rects}
    if focused is not None:
        result["focused_pane_id"] = focused
    return result


class LiveRuntime:
    """One client-owned controller, exact socket inventory, and bounded previews."""

    def __init__(self, emit: Emit, *, session: str | None = None,
                 client_factory: Callable[..., Any] = HerdrClient,
                 discover: Callable[..., Any] = discover_clients,
                 context_resolver: Callable[..., Any] = resolve_launch_context,
                 controller_factory: Callable[..., Any] = LivePaneController,
                 poll_interval: float = POLL_INTERVAL) -> None:
        self.emit, self.session = emit, session
        self.client_factory, self.discover, self.context_resolver = client_factory, discover, context_resolver
        self.poll_interval = poll_interval
        self.clients: dict[str, Any] = {}
        self.inventories: list[SessionInventory] = []
        self.selected: WorkspaceTarget | None = None
        self.layout: dict[str, Any] = dict(UNAVAILABLE_LAYOUT)
        self._epoch = 0
        self._inventory_revision = 0
        self._preview_task: asyncio.Task | None = None
        self._refresh_lock = asyncio.Lock()
        self._read_limit = asyncio.Semaphore(MAX_CONCURRENT_READS)
        self._closed = False
        self._mutation_busy = False
        self._last_frame: PaneFrame | None = None
        self.controller = controller_factory(publish=self._frame, client_factory=self._client_for, strict_tab_identity=True)

    def _client_for(self, session: str, socket_path: Path) -> Any:
        key = str(socket_path)
        client = self.clients.get(key)
        if client is None:
            # This is only called after exact inventory resolution.
            if not any(str(i.socket_path) == key and not i.error and i.snapshot is not None for i in self.inventories):
                raise LiveTargetError("Socket is not connected")
            client = self.client_factory(session=session, socket_path=Path(socket_path))
            self.clients[key] = client
        return client

    async def _context(self, session: str | None) -> tuple[Any, Any]:
        ctx = await asyncio.to_thread(self.context_resolver, session)
        key = str(ctx.socket_path)
        client = self.clients.get(key)
        if client is None or getattr(client, "session", None) != ctx.session_name:
            client = self.client_factory(session=ctx.session_name, socket_path=Path(ctx.socket_path))
            self.clients[key] = client
        return ctx, client

    async def refresh(self) -> None:
        if self._refresh_lock.locked():
            return
        async with self._refresh_lock:
            previous = {str(i.socket_path): i for i in self.inventories}
            try:
                _, primary = await self._context(self.session)
                discovered = await asyncio.to_thread(self.discover, primary)
            except Exception:
                discovered = []
            unique: dict[str, Any] = {}
            duplicates: set[str] = set()
            for client in discovered[:4096]:
                key = str(client.socket_path)
                if key in unique:
                    duplicates.add(key)
                else:
                    unique[key] = client
            for key in duplicates:
                unique.pop(key, None)
            sem = asyncio.Semaphore(8)
            async def fetch(key: str, client: Any) -> SessionInventory:
                inv = SessionInventory(session_name=client.session or "default", socket_path=Path(key))
                try:
                    async with sem:
                        inv.snapshot = await asyncio.wait_for(client.get_snapshot(), RPC_TIMEOUT)
                    self.clients[key] = client
                except Exception:
                    inv.error = "Snapshot unavailable"
                    inv.snapshot = previous[key].snapshot if key in previous else None
                return inv
            current = await asyncio.gather(*(fetch(k, c) for k, c in unique.items()))
            for key, old in previous.items():
                if key not in unique:
                    current.append(SessionInventory(session_name=old.session_name, socket_path=old.socket_path,
                                                    snapshot=old.snapshot, error="Session offline"))
            self.inventories = current
            if self.selected is not None:
                await self.controller.reconcile(self.inventories, enabled=True)
            await self._emit_inventory()

    async def _emit_inventory(self) -> None:
        self._inventory_revision += 1
        revision, epoch = self._inventory_revision, self._epoch
        payload = await asyncio.to_thread(self._inventory_payload, list(self.inventories), self.selected, self.layout)
        if not self._closed and revision == self._inventory_revision and epoch == self._epoch:
            self.emit("inventory", payload)

    @staticmethod
    def _inventory_payload(inventories: list[SessionInventory], selected: WorkspaceTarget | None,
                           current_layout: dict[str, Any]) -> dict[str, Any]:
        sessions = []
        for inv in inventories[:4096]:
            session: dict[str, Any] = {"socket_path": str(inv.socket_path), "session": inv.session_name,
                                       "connection": "offline" if inv.error else "connected", "workspaces": []}
            if inv.error:
                session["error"] = "Session offline or snapshot unavailable"
            snap = inv.snapshot
            if snap is not None:
                # Duplicate IDs are never offered as actionable identities.
                ws_counts = {w.workspace_id: sum(x.workspace_id == w.workspace_id for x in snap.workspaces) for w in snap.workspaces}
                tab_counts = {t.tab_id: sum(x.tab_id == t.tab_id for x in snap.tabs) for t in snap.tabs}
                pane_counts = {p.pane_id: sum(x.pane_id == p.pane_id for x in snap.panes) for p in snap.panes}
                for ws in snap.workspaces[:4096]:
                    if ws_counts[ws.workspace_id] != 1:
                        continue
                    workspace = {"workspace_id": ws.workspace_id, "label": ws.label or ws.workspace_id,
                                 "tabs": [], "agents": [], "metadata": {}}
                    for tab in snap.tabs[:4096]:
                        if tab.workspace_id != ws.workspace_id or tab_counts[tab.tab_id] != 1:
                            continue
                        panes = []
                        for pane in snap.panes[:4096]:
                            if pane.tab_id != tab.tab_id or pane_counts[pane.pane_id] != 1:
                                continue
                            target = WorkspaceTarget(inv.session_name, Path(inv.socket_path), ws.workspace_id, tab.tab_id, pane.pane_id)
                            try:
                                resolved = resolve_live_pane(target, [SessionInventory(session_name=inv.session_name, socket_path=inv.socket_path, snapshot=snap)])
                                if resolved.target.tab_id != tab.tab_id:
                                    continue
                            except LiveTargetError:
                                continue
                            row: dict[str, Any] = {"pane_id": pane.pane_id, "label": pane.terminal_title_stripped or pane.pane_id,
                                                   "state": resolved.agent_status, "metadata": {}}
                            if pane.terminal_id:
                                row["terminal_id"] = pane.terminal_id
                            if resolved.is_agent:
                                row["readiness"] = _readiness(False, resolved.agent_status, "Select a pane to establish a live lease. " + LIMITATION)
                            panes.append(row)
                        layout = dict(UNAVAILABLE_LAYOUT)
                        if selected and str(selected.socket_path) == str(inv.socket_path) and selected.workspace_id == ws.workspace_id and selected.tab_id == tab.tab_id:
                            layout = current_layout
                        workspace["tabs"].append({"tab_id": tab.tab_id, "label": tab.label or tab.tab_id, "panes": panes, "layout": layout})
                    for agent in workspace_agents(inv, ws.workspace_id)[:4096]:
                        if not agent.agent:
                            continue
                        workspace["agents"].append({"pane_id": agent.pane_id, "agent": agent.agent,
                            "role": agent.name, "state": agent.agent_status or "unknown", "metadata": {}})
                    session["workspaces"].append(workspace)
            sessions.append(session)
        return {"sessions": sessions}

    def _inventory_for(self, target: WorkspaceTarget) -> SessionInventory:
        matches = [i for i in self.inventories if str(i.socket_path) == str(target.socket_path)]
        if len(matches) != 1 or matches[0].error or matches[0].snapshot is None:
            raise LiveTargetError("Exact session socket is offline or ambiguous")
        return matches[0]

    async def select(self, wire: dict[str, Any]) -> None:
        self._epoch += 1
        epoch = self._epoch
        task, self._preview_task = self._preview_task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.layout = dict(UNAVAILABLE_LAYOUT)
        self.selected = None
        self._last_frame = None
        await self.controller.select(None, self.inventories, enabled=True)
        invs = [i for i in self.inventories if str(i.socket_path) == wire["socket_path"]]
        if len(invs) != 1:
            raise BridgeError("invalid_target", "Exact socket is absent or ambiguous")
        target = WorkspaceTarget(invs[0].session_name, Path(wire["socket_path"]), wire["workspace_id"], wire.get("tab_id"), wire.get("pane_id"))
        try:
            inv = self._inventory_for(target)
            if target.pane_id:
                resolved = resolve_live_pane(target, [inv])
                if (target.tab_id is not None and target.tab_id != resolved.target.tab_id) or (wire.get("terminal_id") is not None and wire["terminal_id"] != resolved.terminal_id):
                    raise LiveTargetError("Selected pane identity does not match snapshot")
                target = resolved.target
            else:
                snap = inv.snapshot
                if sum(w.workspace_id == target.workspace_id for w in snap.workspaces) != 1:
                    raise LiveTargetError("Workspace missing or ambiguous")
                if target.tab_id and sum(t.tab_id == target.tab_id and t.workspace_id == target.workspace_id for t in snap.tabs) != 1:
                    raise LiveTargetError("Tab missing or foreign")
        except LiveTargetError as exc:
            self.selected = None
            await self.controller.select(None, self.inventories, enabled=True)
            raise BridgeError("invalid_target", str(exc)) from exc
        self.selected = target
        self._last_frame = None
        await self.controller.select(target, self.inventories, enabled=True)
        if epoch == self._epoch and target.tab_id:
            self._preview_task = asyncio.create_task(self._preview_loop(target, epoch))

    def _frame(self, frame: PaneFrame) -> None:
        target = frame.target
        selected = self.selected
        if not target or not selected or not target.pane_id:
            return
        if (str(target.socket_path), target.workspace_id, target.tab_id, target.pane_id) != (str(selected.socket_path), selected.workspace_id, selected.tab_id, selected.pane_id):
            return
        self._last_frame = frame
        lease = _wire_lease(target, frame.lease)
        self.emit("pane_frame", {"target": lease["target"] if lease else _wire_target(target), "lease": lease,
            "text": bound_output(frame.text_ansi), "readiness": _readiness(frame.can_reply and lease is not None,
                frame.agent_status, frame.error or LIMITATION), "layout": self.layout})

    async def _get_layout(self, target: WorkspaceTarget, inv: SessionInventory, panes: list[Any]) -> dict[str, Any]:
        pane_ids = {p.pane_id for p in panes}
        raw = next((x for x in getattr(inv.snapshot, "layouts", []) if isinstance(x, dict) and x.get("workspace_id") == target.workspace_id and x.get("tab_id") == target.tab_id), None)
        if raw is None:
            if not target.pane_id:
                return dict(UNAVAILABLE_LAYOUT)
            try:
                client = self._client_for(inv.session_name, Path(inv.socket_path))
                response = await asyncio.wait_for(client._rpc("pane.layout", {"pane_id": target.pane_id}), RPC_TIMEOUT)
                # HerdrClient._rpc returns the full JSON-RPC envelope, unlike
                # read_pane which unwraps its own result.
                raw = response.get("result") if isinstance(response, dict) else None
            except Exception:
                return dict(UNAVAILABLE_LAYOUT)
        try:
            return _layout(raw, target.workspace_id, target.tab_id or "", pane_ids)
        except LiveTargetError:
            return dict(UNAVAILABLE_LAYOUT)

    async def _preview_loop(self, target: WorkspaceTarget, epoch: int) -> None:
        try:
            while not self._closed and epoch == self._epoch:
                try:
                    inv = self._inventory_for(target)
                    snap = inv.snapshot
                    candidates = [p for p in snap.panes if p.tab_id == target.tab_id and (p.workspace_id is None or p.workspace_id == target.workspace_id)][:MAX_PREVIEWS]
                    panes = []
                    for pane in candidates:
                        candidate = WorkspaceTarget(inv.session_name, Path(inv.socket_path), target.workspace_id, target.tab_id, pane.pane_id)
                        try:
                            resolve_live_pane(candidate, [inv], strict_tab_identity=True)
                        except LiveTargetError:
                            continue
                        panes.append(pane)
                    layout = await self._get_layout(target, inv, panes)
                    if epoch != self._epoch:
                        return
                    self.layout = layout
                    await self._emit_inventory()
                    if epoch != self._epoch:
                        return
                    if self._last_frame:
                        self._frame(self._last_frame)
                    await asyncio.gather(*(self._preview(inv, target, p, epoch) for p in panes if p.pane_id != target.pane_id))
                except Exception:
                    pass
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            raise

    async def _preview(self, inv: SessionInventory, selected: WorkspaceTarget, pane: Any, epoch: int) -> None:
        async with self._read_limit:
            target = WorkspaceTarget(inv.session_name, Path(inv.socket_path), selected.workspace_id, selected.tab_id, pane.pane_id)
            try:
                client = self._client_for(inv.session_name, Path(inv.socket_path))
                snap = await asyncio.wait_for(client.get_snapshot(), RPC_TIMEOUT)
                fresh = SessionInventory(session_name=inv.session_name, socket_path=inv.socket_path, snapshot=snap)
                resolved = resolve_live_pane(target, [fresh])
                if resolved.target.tab_id != selected.tab_id or (pane.terminal_id and resolved.terminal_id != pane.terminal_id):
                    return
                raw = await asyncio.wait_for(client.read_pane(pane.pane_id, format="ansi", lines=1000), RPC_TIMEOUT)
                read = parse_pane_read(raw)
                if (epoch != self._epoch or read.pane_id != pane.pane_id or read.workspace_id != selected.workspace_id
                        or read.tab_id != selected.tab_id or read.source != "recent" or read.format != "ansi"):
                    return
                self.emit("pane_frame", {"target": _wire_target(resolved.target, resolved.terminal_id), "lease": None,
                    "text": bound_output(read.text), "readiness": _readiness(False, resolved.agent_status, "Read-only preview. " + LIMITATION),
                    "layout": self.layout})
            except Exception:
                return

    def _lease(self, wire: dict[str, Any]) -> PaneLease:
        target = self.selected
        frame = self._last_frame
        if not target or not frame or not frame.lease or _wire_lease(target, frame.lease) != wire:
            raise BridgeError("stale_lease", "Selected pane lease is no longer current")
        return frame.lease

    async def send(self, method: str, params: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None]:
        lease = self._lease(params["lease"])
        try:
            self._inventory_for(self.selected)
        except LiveTargetError as exc:
            raise BridgeError("stale_lease", "Selected session is offline") from exc
        if method == "reply.submit":
            result = await self.controller.submit(ReplyRequest(lease, params["text"], params["notify"]))
        else:
            result = await self.controller.submit_terminal_input(TerminalInput(lease, params.get("text"), params.get("keys")))
        return result.outcome, result.message, params["lease"]

    async def delete(self, params: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None]:
        wire = params["target"]
        if params.get("confirmed") is not True or any(wire.get(k) is not None for k in ("tab_id", "pane_id", "terminal_id")):
            raise BridgeError("invalid_target", "Deletion requires confirmation and an exact workspace")
        target = WorkspaceTarget("", Path(wire["socket_path"]), wire["workspace_id"])
        try:
            inv = self._inventory_for(target)
            client = self._client_for(inv.session_name, Path(inv.socket_path))
            snap = await asyncio.wait_for(client.get_snapshot(), RPC_TIMEOUT)
            if sum(w.workspace_id == target.workspace_id for w in snap.workspaces) != 1:
                raise LiveTargetError("Workspace missing or ambiguous")
        except (LiveTargetError, asyncio.TimeoutError) as exc:
            raise BridgeError("invalid_target", "Cannot verify workspace") from exc
        try:
            await client.close_workspace(target.workspace_id)
        except Exception:
            return "uncertain", "Workspace deletion may have been dispatched; do not retry automatically", wire
        return "accepted", "Workspace deletion requested", wire

    async def launch(self, method: str, params: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None]:
        """Validate all settings before one dispatch; failures after dispatch are uncertain."""
        if self._mutation_busy:
            raise BridgeError("operation_in_progress", "A launch or AiHead operation is already in progress")
        self._mutation_busy = True
        try:
            folder = await asyncio.to_thread(folders_test, params["project_path"])
            if not folder["available"]:
                raise BridgeError("folder_error", "Project folder is not available")
            cwd = folder["path"]
            try:
                resolved = (await asyncio.to_thread(resolve_squad, Path(cwd))
                            if method in ("launch.master", "launch.ergo") else None)
                if method == "launch.master":
                    ensure_herdr_routable(params["harness"], params["model"], params.get("effort"), role="master", require_effort=False)
                elif method == "launch.ergo":
                    squad = Squad.model_validate(params["squad"]) if params.get("squad") is not None else resolved.squad
                    for role in ("scout", "architect", "reviewer"):
                        describe_agent(getattr(squad, role), mode="herdr", role=role)
                    for worker in squad.worker_pool:
                        if worker.effort is not None:
                            raise ValueError("Worker effort must be unset")
                        ensure_herdr_routable(worker.kind.value, worker.model, None, role="coder", require_effort=False)
                    if params["customize_first"] and params.get("squad") is None:
                        raise ValueError("Customized squad is required")
                elif method == "launch.council":
                    council = CouncilConfig.model_validate(params["config"])
                    if sum(bool(x.enabled) for x in council.members) < 2:
                        raise ValueError("At least two council members must be enabled")
                    for role, binding in [("master", council.coordinator), ("scout", council.scout), *(("reviewer", m) for m in council.members)]:
                        describe_agent(binding, mode="herdr", role=role)
                elif method == "launch.orchestration":
                    for role in ("scout", "architect", "reviewer"):
                        error = native_binding_error(params["harness"], params[role + "_model"], role=role, native_harness=params["harness"])
                        if error:
                            raise ValueError(error)
                    for model in params["worker_models"]:
                        if ":" in model and model.rsplit(":", 1)[-1].lower() in {"low", "medium", "high", "xhigh", "max"}:
                            raise ValueError("Worker model must not specify effort")
                        error = native_binding_error(params["harness"], model, role="coder", native_harness=params["harness"])
                        if error:
                            raise ValueError(error)
            except (ValueError, ValidationError) as exc:
                raise BridgeError("invalid_launch", "Invalid launcher settings", field_errors={"settings": str(exc)[:1000]}) from exc
            if method == "launch.ergo" and params["save_default"]:
                service = SettingsService()
                draft = await asyncio.to_thread(service.project_open, cwd)
                saved = await asyncio.to_thread(service.save, draft["token"], {"squad": squad.model_dump(mode="json")}, revision=draft["revision"])
                if not saved.get("valid"):
                    raise BridgeError("invalid_launch", "Squad default was not saved", field_errors=saved.get("field_errors", {}))
            if method == "launch.ergo" and not params["start_pipeline"]:
                return "accepted", "Squad settings validated; pipeline not started", None
            ctx, client = await self._context(params.get("session"))
            common = dict(client=client, cwd=cwd, session=ctx.session_name, fresh_workspace=True, focus=False)
            if method == "launch.master":
                call, kwargs = launch_master, dict(name=params["name"], harness=params["harness"], model=params["model"], initial_task=params.get("task"), effort=params.get("effort"), ai_head=params.get("ai_head"))
            elif method == "launch.ergo":
                call, kwargs = launch_ergo, dict(intent=params["intent"], workspace_label=params["label"], squad=squad, ai_head=params.get("ai_head"))
            elif method == "launch.orchestration":
                call, kwargs = launch_orchestration, dict(intent=params["intent"], workspace_label=params["label"], harness=params["harness"], scout_model=params["scout_model"], architect_model=params["architect_model"], coder_model=" | ".join(params["worker_models"]), reviewer_model=params["reviewer_model"], scout_effort=params.get("scout_effort"), architect_effort=params.get("architect_effort"), reviewer_effort=params.get("reviewer_effort"), ai_head=params.get("ai_head"), dangerously_skip_permissions=params["dangerously_skip_permissions"])
            elif method == "launch.council":
                call, kwargs = launch_council, dict(intent=params["idea"], workspace_label=params["label"], council=council, ai_head=False)
            else:
                raise BridgeError("unknown_method", "Unsupported launcher")
            try:
                result = await call(**common, **kwargs)
            except Exception:
                return "uncertain", "Launch may have been dispatched; inspect Herdr before retrying", None
            workspace = result.get("workspace_id") if isinstance(result, dict) else None
            if (not isinstance(workspace, str) or not workspace
                    or (result.get("session") is not None and result["session"] != ctx.session_name)
                    or (result.get("socket_path") is not None and str(result["socket_path"]) != str(ctx.socket_path))):
                return "uncertain", "Launch returned an invalid identity; inspect Herdr before retrying", None
            target = {"socket_path": str(ctx.socket_path), "workspace_id": workspace}
            for key in ("tab_id", "pane_id"):
                if isinstance(result.get(key), str) and result[key]:
                    target[key] = result[key]
            return "accepted", "Workspace launched", target
        finally:
            self._mutation_busy = False

    async def open_aihead(self, params: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None]:
        if self._mutation_busy:
            raise BridgeError("operation_in_progress", "A launch or AiHead operation is already in progress")
        self._mutation_busy = True
        try:
            folder = await asyncio.to_thread(folders_test, params["project_path"])
            if not folder["available"]:
                raise BridgeError("folder_error", "Project folder is not available")
            ctx, client = await self._context(params.get("session"))
            try:
                snap = await asyncio.wait_for(client.get_snapshot(), RPC_TIMEOUT)
            except Exception as exc:
                raise BridgeError("session_offline", "Cannot verify existing AiHead") from exc
            # Exact socket and workspace identities are resolved in a fresh snapshot.
            for tab in snap.tabs:
                if (tab.label or "").strip() != "ai-head" or not tab.workspace_id:
                    continue
                for pane in snap.panes:
                    if pane.tab_id != tab.tab_id:
                        continue
                    alive, agent = _resolve_agent_liveness(pane, list(snap.agents))
                    if not alive and agent is not None and (agent.agent_status or "").strip().lower() not in DEAD_AGENT_STATUSES:
                        alive = True
                    if alive:
                        inv = SessionInventory(session_name=ctx.session_name, socket_path=Path(ctx.socket_path), snapshot=snap)
                        candidate = WorkspaceTarget(ctx.session_name, Path(ctx.socket_path), tab.workspace_id, tab.tab_id, pane.pane_id)
                        try:
                            resolved = resolve_live_pane(candidate, [inv])
                            if resolved.target.tab_id != tab.tab_id:
                                continue
                        except LiveTargetError:
                            continue
                        return "accepted", "Existing AiHead reused", _wire_target(resolved.target, resolved.terminal_id)
            config = await asyncio.to_thread(load_project_config, Path(folder["path"]))
            origin = str(Path(config.ai_head.cwd).expanduser().resolve()) if config.ai_head.cwd else folder["path"]
            named = [w for w in snap.workspaces if (w.label or "").strip() == "ai-head"]
            if len(named) > 1:
                raise BridgeError("invalid_target", "AiHead workspace is ambiguous")
            workspace = named[0].workspace_id if named else None
            if not workspace:
                try:
                    created = await client.create_workspace(label="ai-head", cwd=origin)
                    workspace = created.get("workspace_id") or created.get("id")
                except Exception:
                    return "uncertain", "AiHead workspace creation may have been dispatched; inspect Herdr before retrying", None
                if not isinstance(workspace, str) or not workspace:
                    return "uncertain", "AiHead workspace creation returned no identity; inspect Herdr before retrying", None
            try:
                result = await ensure_herdr_ai_head(client=client, workspace_id=workspace, head_config=config.ai_head, origin_cwd=origin, force=True)
            except Exception:
                return "uncertain", "AiHead creation may have been dispatched; inspect Herdr before retrying", None
            if (not isinstance(result, dict) or not isinstance(result.get("pane_id"), str) or not result["pane_id"]
                    or (result.get("workspace_id") is not None and result["workspace_id"] != workspace)):
                return "uncertain", "AiHead result has an invalid identity; inspect Herdr before retrying", None
            target = {"socket_path": str(ctx.socket_path), "workspace_id": workspace, "pane_id": result["pane_id"]}
            if result.get("tab_id"):
                target["tab_id"] = result["tab_id"]
            return "accepted", "AiHead ready", target
        finally:
            self._mutation_busy = False

    async def close(self) -> None:
        self._closed = True
        self._epoch += 1
        if self._preview_task:
            self._preview_task.cancel()
            await asyncio.gather(self._preview_task, return_exceptions=True)
        await self.controller.close()


def logs(limit: int = 200) -> dict[str, Any]:
    # Other components may log prompts or secrets. Expose bounded metadata only.
    from aiheaddev.core.logging import _MEMORY_BUFFER
    return {"records": [{"level": str(r.levelname), "message": "[redacted]", "timestamp": datetime.fromtimestamp(r.created, timezone.utc).isoformat()}
                        for r in list(_MEMORY_BUFFER)[-limit:]]}
