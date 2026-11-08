"""Textual Terminal User Interface (TUI) Application for Herdr-Master."""

import asyncio
from dataclasses import replace
import os
from pathlib import Path
from typing import Any, Callable, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Input, TextArea

from herdr_master.core.config import HerdrMasterConfig, load_project_config
from herdr_master.core.context import resolve_herdr_context
from herdr_master.core.models import MasterSession, PipelinePhase, PipelineRun
from herdr_master.herdr.client import HerdrClient
from herdr_master.herdr.discovery import SessionInventory, discover_clients
from herdr_master.herdr.snapshot import AgentSnapshot
from herdr_master.pipeline.ai_head import ensure_herdr_ai_head
from herdr_master.pipeline.launcher import (
    LaunchContextError,
    launch_council,
    launch_ergo,
    launch_master,
    launch_orchestration,
    resolve_launch_context,
)
from herdr_master.pipeline.squad import resolve_squad
from aiheaddev.core.logging import get_logger
from aiheaddev.ui.tui.live_pane import (
    LivePaneController,
    LiveTargetError,
    PaneFrame,
    ReplyRequest,
    ReplyResult,
    resolve_live_pane,
)
from aiheaddev.ui.tui.messages import TargetSelected
from aiheaddev.ui.tui.modals.launch_council import (
    CouncilLaunchParams,
    LaunchCouncilModal,
)
from aiheaddev.ui.tui.modals.launch_ergo import ErgoLaunchParams, LaunchErgoModal
from aiheaddev.ui.tui.modals.launch_master import LaunchMasterModal, MasterLaunchParams
from aiheaddev.ui.tui.modals.launch_orchestration import (
    LaunchOrchestrationModal,
    OrchestrationLaunchParams,
)
from aiheaddev.ui.tui.modals.project_folder import (
    initial_project_folder,
    validate_project_folder,
)
from aiheaddev.ui.tui.modals.confirm import ConfirmModal
from aiheaddev.ui.tui.modals.log_viewer import LogViewerModal
from aiheaddev.ui.tui.modals.settings import SettingsModal
from aiheaddev.ui.tui.screens.ai_head_screen import AiHeadScreen
from aiheaddev.ui.tui.state import AppState
from aiheaddev.ui.tui.widgets.deck import CenterDeck
from aiheaddev.ui.tui.widgets.footer import ActionFooter
from aiheaddev.ui.tui.widgets.header_bar import HeaderBar
from aiheaddev.ui.tui.widgets.live_output import LivePaneOutput, LivePaneView
from aiheaddev.ui.tui.widgets.quick_launch import QuickLaunchBar
from aiheaddev.ui.tui.widgets.workspaces import (
    WorkspaceBrowser,
    WorkspaceTarget,
    safe_display,
    workspace_agents,
)

logger = get_logger("cockpit")


def _result_pane(result: dict) -> Optional[str]:
    """Generic pane key. The deprecated scout_pane_id alias is not a pane."""
    pane = result.get("pane_id") or result.get("orchestrator_pane_id")
    if not pane:
        return None
    return str(pane)


class HerdrMasterApp(App):
    """Herdr-Master interactive terminal management cockpit."""

    TITLE = "HERDR-MASTER Cockpit"

    BINDINGS = [
        Binding("h", "launch_aihead", "AiHead"),
        Binding("n", "new_master", "New Master"),
        Binding("e", "new_ergo", "New Ergo"),
        Binding("s", "open_settings", "Settings"),
        Binding("c", "open_settings", "Settings"),
        Binding("l", "view_logs", "Logs"),
        Binding("o", "open_in_herdr", "Open in Herdr", priority=False),
        Binding("d", "delete_workspace", "Delete Workspace", priority=False),
        Binding("q", "quit", "Quit"),
    ]

    CSS = """
    Screen {
        background: #0B0F19;
        color: #F8FAFC;
    }
    .main-body {
        height: 1fr;
        width: 100%;
    }
    """

    def __init__(
        self,
        session: Optional[str] = None,
        demo: bool = False,
        project_path: Optional[Path] = None,
        *,
        live_controller: Optional[LivePaneController] = None,
        live_client_factory: Optional[Callable[[str, Path], HerdrClient]] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._ai_head_ensure_pending = False
        self._ai_head_focus_requested = False
        self._ai_head_confirmed_target: Optional[WorkspaceTarget] = None
        self._session_argument = session
        self.session_name = self._requested_session()
        self.socket_path: Optional[Path] = None
        self.demo = demo
        self.project_path = Path(project_path) if project_path else None
        self.config_error: Optional[str] = None
        self.squad_sources: dict[str, str] = {}
        self.squad_mode = "herdr"
        self.native_harness: Optional[str] = None
        self._launch_pending = False
        self._inventory_pending = False
        self._navigation_pending = False
        if demo:
            self.app_state = AppState.create_demo_state()
            self.squad_sources = {
                key: "product_default" for key in ("scout", "architect", "reviewer", "workers")
            }
        else:
            self.app_state = self._load_live_state()
        self._settings_squad = self.app_state.squad.model_copy(deep=True)
        self.client: Optional[HerdrClient] = None
        self._header = HeaderBar(self.app_state)
        self._deck = CenterDeck(self.app_state)
        self._footer = ActionFooter()
        self._client_factory = live_client_factory or self._default_live_client_factory
        self._live_controller: Optional[LivePaneController] = live_controller
        self._live_desired_target: Optional[WorkspaceTarget] = None
        self._live_closed = False
        self._live_revision = 0
        self._live_ready = True
        self._live_transition = False
        self._live_inventory_blocked = False
        self._live_buffered_frame: Optional[PaneFrame] = None
        self._last_live_frame: Optional[PaneFrame] = None
        self._live_lock = asyncio.Lock()
        self._live_submit_task: Optional[asyncio.Task] = None
        self._live_submit_pending = False
        if self._live_controller is None:
            self._live_controller = LivePaneController(
                publish=self._on_live_pane_frame,
                client_factory=self._client_factory,
            )

    def _default_live_client_factory(self, session_name: str, socket_path: Path) -> HerdrClient:
        return HerdrClient(session=session_name, socket_path=Path(socket_path))

    def _live_client_for_socket(self, session_name: str, socket_path: Path) -> HerdrClient:
        """Obtain a HerdrClient for the given session and socket path via client factory."""
        try:
            return self._client_factory(session_name, Path(socket_path))
        except TypeError:
            return self._client_factory(Path(socket_path), session_name)  # type: ignore[call-arg]

    @staticmethod
    def _live_key(target: Optional[WorkspaceTarget]) -> Optional[tuple[str, str, str]]:
        if target is None or target.pane_id is None:
            return None
        return (str(target.socket_path), target.workspace_id, target.pane_id)

    def _publish_live_frame(self, frame: PaneFrame) -> None:
        """Publish accepted or state-guard frame to deck and matching mounted AiHeadScreens."""
        self._last_live_frame = frame
        self._deck.update_live_frame(frame)
        frame_key = self._live_key(frame.target)
        for screen in self.screen_stack:
            if isinstance(screen, AiHeadScreen):
                if (
                    self._ai_head_confirmed_target is not None
                    and frame_key == self._live_key(self._ai_head_confirmed_target)
                ):
                    screen.update_live_frame(frame)

    def _on_live_pane_frame(self, frame: PaneFrame) -> None:
        if self._live_closed:
            return
        previous = self._last_live_frame
        if (
            previous is not None
            and previous.lease is not None
            and self._live_key(frame.target) == previous.lease.key
            and frame.lease != previous.lease
        ):
            # Revocation must interrupt a held preflight immediately; after
            # dispatch the controller classifies cancellation as uncertain.
            self._cancel_live_submit()
        if frame.lease is not None and frame.reply_eligible is False:
            # can_reply is already false for any in-flight send; this separate
            # eligibility signal distinguishes a real readiness/agent loss.
            self._cancel_live_submit()
        if self._live_transition:
            self._live_buffered_frame = frame
            return
        if (
            not self._live_ready
            or self._live_inventory_blocked
            or self._live_key(frame.target) != self._live_key(self._live_desired_target)
        ):
            return
        self._publish_live_frame(frame)

    def _cancel_live_submit(self) -> None:
        task = self._live_submit_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _guard_live_inventory(self) -> None:
        """Invalidate a draft/send before a slow controller reconciliation can yield."""
        frame = self._last_live_frame
        target = self._live_desired_target
        if not frame or not frame.lease or self._live_key(target) != frame.lease.key:
            return
        try:
            resolved = resolve_live_pane(target, self.app_state.live_sessions)
            incarnation_changed = (
                frame.lease.terminal_id is not None
                and frame.lease.terminal_id != resolved.terminal_id
            )
            if incarnation_changed:
                raise LiveTargetError("Terminal incarnation changed")
        except LiveTargetError as exc:
            self._cancel_live_submit()
            self._live_inventory_blocked = True
            unavailable = replace(
                frame, lease=None, phase="stale", error=str(exc), can_reply=False, reply_eligible=False
            )
            self._publish_live_frame(unavailable)
            return
        if not resolved.is_agent or resolved.interactive_ready is False:
            self._cancel_live_submit()
            self._live_inventory_blocked = True
            not_ready = replace(frame, can_reply=False, reply_eligible=False)
            self._publish_live_frame(not_ready)

    async def _run_live_update(
        self, revision: int, target: Optional[WorkspaceTarget], *, select: bool
    ) -> None:
        if self._live_controller is None or self._live_closed:
            return
        async with self._live_lock:
            if (
                self._live_closed
                or revision != self._live_revision
                or (not select and not self._live_ready)
            ):
                return
            self._live_transition = True
            self._live_buffered_frame = None
            try:
                if select:
                    await self._live_controller.select(
                        target, self.app_state.live_sessions, enabled=not self.demo
                    )
                else:
                    await self._live_controller.reconcile(
                        self.app_state.live_sessions, enabled=not self.demo
                    )
            finally:
                self._live_transition = False
                buffered = self._live_buffered_frame
                self._live_buffered_frame = None
                if not self._live_closed and revision == self._live_revision:
                    self._live_ready = True
                    self._live_inventory_blocked = False
                    if buffered is not None:
                        self._on_live_pane_frame(buffered)

    def _load_live_state(self) -> AppState:
        """Resolve the project squad and config. Invalid settings must not become a silent default."""
        try:
            resolved = resolve_squad(self.project_path)
        except Exception as exc:
            self.config_error = (
                f"Invalid project squad settings: {exc}. "
                "Fix .herdr-master.toml before launching. "
                "Refusing to launch product defaults."
            )
            return self._state(branch_status="INVALID CONFIG", socket_connected=False)
        self.squad_sources = dict(resolved.sources)
        self.squad_mode = resolved.mode
        self.native_harness = resolved.native_harness
        project_cfg: Optional[HerdrMasterConfig] = None
        try:
            project_cfg = load_project_config(self.project_path)
            if project_cfg and not project_cfg.ai_head.enabled and not self.project_path:
                home_cfg_file = Path.home() / ".herdr-master.toml"
                if home_cfg_file.is_file():
                    try:
                        home_cfg = load_project_config(home_cfg_file)
                        if home_cfg.ai_head.enabled:
                            project_cfg.ai_head = home_cfg.ai_head
                    except Exception:
                        pass
        except Exception:
            pass
        return self._state(squad=resolved.squad, config=project_cfg)

    def compose(self) -> ComposeResult:
        yield self._header
        with Horizontal(classes="main-body"):
            yield self._deck
        yield self._footer

    async def on_mount(self) -> None:
        """Initialize socket connection and poll live state if not in demo mode."""
        if self.config_error:
            self.notify(self.config_error, severity="error", timeout=8)
        if not self.demo:
            if self._requested_session() is None:
                self._bind_launch_client(resolve_launch_context(None))
            else:
                self.client = HerdrClient(session=self.session_name)
            self._schedule_inventory_refresh()
            self._ensure_ai_head_if_enabled()
            self.set_interval(3.0, self._schedule_inventory_refresh)

    def _ensure_ai_head_if_enabled(self, *, force: bool = False, focus: bool = False) -> None:
        """Ensure persistent ai-head tab exists if AiHead is enabled in config or force=True."""
        if self.demo or self.client is None:
            return
        cfg = getattr(self.app_state, "config", None)
        if not cfg or (not cfg.ai_head.enabled and not force):
            return
        if focus:
            self._ai_head_focus_requested = True
        if self._ai_head_ensure_pending:
            return
        self._ai_head_ensure_pending = True

        async def _run_ensure() -> None:
            try:
                logger.info(f"Ensuring AiHead tab in workspace (force={force})...")
                target_ws = getattr(self.app_state, "workspace_id", None)
                if (not target_ws or target_ws == "ws-01") and self.client is not None:
                    try:
                        snap = await self.client.get_snapshot()
                        if hasattr(snap, "workspaces"):
                            target_ws = snap.focused_workspace_id or (
                                snap.workspaces[0].workspace_id if snap.workspaces else target_ws
                            )
                    except Exception:
                        pass
                if not target_ws:
                    target_ws = "default"
                origin_cwd = (
                    str(Path(os.path.expanduser(cfg.ai_head.cwd)).resolve())
                    if (cfg and cfg.ai_head and cfg.ai_head.cwd)
                    else str((self.project_path or Path.cwd()).resolve())
                )
                head_info = await ensure_herdr_ai_head(
                    client=self.client,
                    workspace_id=target_ws,
                    head_config=cfg.ai_head,
                    origin_cwd=origin_cwd,
                    force=force,
                )
                if head_info and "workspace_id" in head_info:
                    self.app_state.workspace_id = head_info["workspace_id"]
                self.app_state.ai_head_info = head_info
                self.app_state.ai_head_busy = bool(head_info.get("busy", False))
                self._schedule_inventory_refresh()
                logger.info(f"AiHead tab successfully ensured: {head_info}")
                should_focus = focus or getattr(self, "_ai_head_focus_requested", False)
                if should_focus:
                    if head_info and head_info.get("pane_id"):
                        target = WorkspaceTarget(
                            session_name=self.session_name or "default",
                            socket_path=Path(getattr(self.client, "socket_path", "")),
                            workspace_id=head_info.get("workspace_id") or target_ws,
                            tab_id=head_info.get("tab_id"),
                            pane_id=head_info.get("pane_id"),
                        )
                        self._ai_head_confirmed_target = target
                        self.on_workspace_browser_selection_changed(
                            WorkspaceBrowser.SelectionChanged(target)
                        )
                        for scr in self.screen_stack:
                            if isinstance(scr, AiHeadScreen):
                                scr.update_status(head_info.get("status", "ready"))
                        self.notify("AiHead tab ready.", severity="information")
                    else:
                        self._ai_head_confirmed_target = None
                        err_frame = PaneFrame(
                            lease=None,
                            target=None,
                            phase="unavailable",
                            agent_status="error",
                            text_ansi="",
                            error="AiHead initialization incomplete: missing pane. Use Back [Esc] to return and retry.",
                            can_reply=False,
                            reply_eligible=False,
                        )
                        for scr in self.screen_stack:
                            if isinstance(scr, AiHeadScreen):
                                scr.update_status("error")
                                scr.update_live_frame(err_frame)
            except Exception as exc:
                logger.exception(f"Failed to initialize AiHead tab: {exc}")
                info = self.app_state.ai_head_info
                if info is not None:
                    info["status"] = "pending"
                    info["reconciled"] = False
                self._ai_head_confirmed_target = None
                err_frame = PaneFrame(
                    lease=None,
                    target=None,
                    phase="unavailable",
                    agent_status="error",
                    text_ansi="",
                    error=f"AiHead initialization failed: {exc}. Use Back [Esc] to return and retry.",
                    can_reply=False,
                    reply_eligible=False,
                )
                for scr in self.screen_stack:
                    if isinstance(scr, AiHeadScreen):
                        scr.update_status("error")
                        scr.update_live_frame(err_frame)
                self.notify(f"Could not initialize AiHead tab: {exc}", severity="warning")
            finally:
                self._ai_head_ensure_pending = False
                self._ai_head_focus_requested = False

        self.run_worker(_run_ensure(), name="ensure_ai_head")

    def _schedule_inventory_refresh(self) -> None:
        self.run_worker(self._refresh_live_snapshot(), group="workspace-inventory")

    async def _refresh_live_snapshot(self) -> None:
        """Refresh all discovered sockets without blocking terminal interaction."""
        if self.demo or self.client is None or self._inventory_pending:
            return
        self._inventory_pending = True
        try:
            previous = {item.socket_path: item for item in self.app_state.live_sessions}

            async def fetch(client: HerdrClient) -> SessionInventory:
                path = Path(client.socket_path)
                inventory = SessionInventory(
                    session_name=client.session or self.session_name or "default", socket_path=path
                )
                try:
                    inventory.snapshot = await asyncio.wait_for(client.get_snapshot(), timeout=2.0)
                except Exception as exc:
                    inventory.error = str(exc)[:140] or type(exc).__name__
                    if path in previous:
                        inventory.snapshot = previous[path].snapshot
                return inventory

            clients = await asyncio.to_thread(discover_clients, self.client)
            inventories = await asyncio.gather(*(fetch(client) for client in clients))
            self.app_state.live_sessions = list(inventories)
            self.app_state.socket_connected = any(not item.error for item in inventories)
            self.app_state.active_agents_count = sum(
                agent.agent_status in {"working", "idle", "blocked"}
                for item in inventories
                if item.snapshot and not item.error
                for agent in item.snapshot.agents
            )
            primary = next(
                (item for item in inventories if item.socket_path == Path(self.client.socket_path)),
                None,
            )
            if primary and primary.snapshot and not primary.error:
                snapshot = primary.snapshot
                workspace = next(
                    (
                        ws
                        for ws in snapshot.workspaces
                        if ws.workspace_id == snapshot.focused_workspace_id
                    ),
                    next(iter(snapshot.workspaces), None),
                )
                if workspace:
                    self.app_state.workspace_id = workspace.workspace_id
                    self.app_state.workspace_label = workspace.label or workspace.workspace_id
                head_info = self.app_state.ai_head_info
                if head_info and head_info.get("pane_id"):
                    agent = next(
                        (a for a in snapshot.agents if a.pane_id == head_info["pane_id"]), None
                    )
                    if agent is not None:
                        status = agent.agent_status
                        if status in ("working", "busy"):
                            self.app_state.ai_head_busy = True
                            head_info["busy"] = True
                        elif status in ("idle", "done"):
                            was_busy = (
                                self.app_state.ai_head_busy
                                or head_info.get("status") == "busy"
                                or head_info.get("reconciled") is False
                            )
                            if was_busy:
                                # Only ensure_herdr_ai_head may declare ready/reconciled.
                                self._ensure_ai_head_if_enabled()
            self._update_all_widgets()
            await self._reconcile_live_inventory()
        finally:
            self._inventory_pending = False

    async def _reconcile_live_inventory(self) -> None:
        self._guard_live_inventory()
        if not self._live_ready:
            # A selection can be waiting for old watcher teardown with an older
            # inventory. Supersede it and resolve the desired target again before
            # allowing its frames or a reply to become current.
            self._live_revision += 1
            await self._run_live_update(
                self._live_revision, self._live_desired_target, select=True
            )
        else:
            await self._run_live_update(
                self._live_revision, self._live_desired_target, select=False
            )

    async def _close_live_controller(self) -> None:
        """Fence pending selections before closing, including during slow teardown."""
        self._live_closed = True
        self._live_revision += 1
        self._live_ready = False
        task = self._live_submit_task
        self._cancel_live_submit()
        if task is not None and task is not asyncio.current_task():
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with self._live_lock:
            if self._live_controller is not None:
                await self._live_controller.close()

    async def on_unmount(self) -> None:
        await self._close_live_controller()

    async def action_quit(self) -> None:
        if len(self.screen_stack) > 1 or isinstance(
            self.focused, (TextArea, Input, LivePaneOutput)
        ):
            return
        await self._close_live_controller()
        self.exit()

    def on_workspace_browser_selection_changed(
        self, event: WorkspaceBrowser.SelectionChanged
    ) -> None:
        if self._live_closed:
            return
        target = event.target
        if self._live_key(target) != self._live_key(self._live_desired_target):
            self._live_revision += 1
            self._live_ready = False
            self._live_inventory_blocked = False
            self._cancel_live_submit()
            # Invalidate the old lease and output synchronously, before watcher teardown.
            frame = PaneFrame(
                lease=None,
                target=target,
                phase="loading" if self._live_key(target) else "empty",
                agent_status="unknown",
                text_ansi="",
                error=None,
                can_reply=False,
                reply_eligible=False if self._live_key(target) else None,
            )
            self._publish_live_frame(frame)
        self._live_desired_target = target
        if self._live_controller is not None:
            self.run_worker(
                self._run_live_update(self._live_revision, target, select=True),
                group="live-pane-select",
            )

    def on_live_pane_view_reply_requested(
        self, event: LivePaneView.ReplyRequested
    ) -> None:
        if self._live_submit_pending:
            return
        self._live_submit_pending = True
        self.run_worker(
            self._submit_live_pane_reply(event.request),
            group="live-pane-submit",
        )

    def on_live_pane_view_terminal_input_requested(
        self, event: LivePaneView.TerminalInputRequested
    ) -> None:
        if (
            self.demo
            or self._live_closed
            or not self._live_ready
            or self._live_controller is None
        ):
            return
        self.run_worker(
            self._live_controller.send_terminal_input(
                lease=event.lease, text=event.text, keys=event.keys
            ),
            group="live-pane-terminal-input",
            exclusive=False,
        )

    async def _submit_live_pane_reply(self, request: ReplyRequest) -> None:
        self._live_submit_task = asyncio.current_task()
        try:
            if (
                self._live_controller is None
                or self._live_closed
                or not self._live_ready
                or self._live_inventory_blocked
                or self._live_key(self._live_desired_target) != request.lease.key
                or self._last_live_frame is None
                or self._last_live_frame.lease != request.lease
                or not self._last_live_frame.can_reply
            ):
                result = ReplyResult(request.lease, "rejected", "Target changed before dispatch")
            else:
                try:
                    result = await self._live_controller.submit(request)
                except asyncio.CancelledError:
                    # The controller converts cancellation after dispatch to uncertain;
                    # only cancellation during preflight reaches this handler.
                    result = ReplyResult(
                        request.lease, "rejected", "Target changed before dispatch"
                    )
            self._deck.apply_reply_result(result)
            if request.notify:
                socket, workspace, pane = request.lease.key
                self.notify(
                    f"{result.outcome.capitalize()}: {safe_display(result.message)}\n"
                    f"Socket: {safe_display(socket)} · workspace: {safe_display(workspace)}"
                    f" · pane: {safe_display(pane)}",
                    severity="information" if result.outcome == "accepted" else "warning",
                    timeout=10,
                    markup=False,
                )
            if result.outcome == "accepted" and not self._live_closed:
                self._schedule_inventory_refresh()
        finally:
            self._live_submit_task = None
            self._live_submit_pending = False

    def action_open_in_herdr(self) -> None:
        """Open selected workspace or pane in Herdr, or trigger Orchestration if no target selected."""
        if len(self.screen_stack) > 1 or isinstance(
            self.focused, (TextArea, Input, LivePaneOutput)
        ):
            return
        try:
            browser = self.query_one(WorkspaceBrowser)
            if browser._selected_target is not None or browser._selected_target_disappeared:
                if self.demo:
                    self.notify("Demo mode: live terminal navigation is disabled.", severity="warning")
                    return
                browser.request_open()
                return
        except Exception:
            pass
        self.notify("No target selected. Opening Orchestration launcher.", severity="information")
        self.action_new_orchestration()

    def on_workspace_browser_open_requested(
        self, event: WorkspaceBrowser.OpenRequested
    ) -> None:
        event.stop()
        if not self._navigation_pending:
            self.run_worker(self._open_in_herdr(event.target), group="agent-navigation")

    async def _open_in_herdr(self, target: WorkspaceTarget) -> None:
        """Focus the exact socket/workspace/tab/pane; hand off safely to Herdr attach."""
        if self.demo:
            self.notify("Demo mode: live terminal navigation is disabled.", severity="warning")
            return
        if self._navigation_pending:
            return
        if len(self.screen_stack) > 1:
            return
        self._navigation_pending = True
        try:
            inventory = next(
                (
                    item
                    for item in self.app_state.live_sessions
                    if Path(item.socket_path) == Path(target.socket_path)
                ),
                None,
            )
            if not inventory or inventory.error or not inventory.snapshot:
                self.notify("Session is offline. Wait for it to reconnect.", severity="warning")
                return

            snapshot = inventory.snapshot
            ws = next(
                (w for w in snapshot.workspaces if w.workspace_id == target.workspace_id),
                None,
            )
            if not ws:
                self.notify("Workspace has closed or no longer exists.", severity="warning")
                return

            agents = workspace_agents(inventory, target.workspace_id)
            if not agents:
                self.notify("Workspace has no open panes to open.", severity="warning")
                return

            target_pane: Optional[AgentSnapshot] = None
            target_tab_id: Optional[str] = None
            if target.pane_id is not None:
                target_pane = next((p for p in agents if p.pane_id == target.pane_id), None)
                if not target_pane:
                    self.notify("This pane has closed. Select a current agent.", severity="warning")
                    return
                if target_pane.tab_id:
                    tab = next((t for t in snapshot.tabs if t.tab_id == target_pane.tab_id), None)
                    if tab is None:
                        self.notify(
                            f"Pane references missing tab '{target_pane.tab_id}'. "
                            "Cannot open in Herdr.",
                            severity="warning",
                        )
                        return
                    if tab.workspace_id != target.workspace_id:
                        self.notify(
                            f"Pane references tab '{tab.tab_id}' belonging to another workspace "
                            f"'{tab.workspace_id}'. Cannot open in Herdr.",
                            severity="warning",
                        )
                        return
                    target_tab_id = tab.tab_id
                else:
                    target_tab_id = None

                raw_pane = next((p for p in snapshot.panes if p.pane_id == target.pane_id), None)
                if raw_pane is not None:
                    if raw_pane.workspace_id and raw_pane.workspace_id != target.workspace_id:
                        self.notify(
                            "Pane workspace ownership is contradictory. Cannot open in Herdr.",
                            severity="warning",
                        )
                        return
                    if (
                        raw_pane.tab_id
                        and target_pane.tab_id
                        and raw_pane.tab_id != target_pane.tab_id
                    ):
                        self.notify(
                            "Pane tab ownership is contradictory. Cannot open in Herdr.",
                            severity="warning",
                        )
                        return

            context = resolve_herdr_context()
            same_socket = (
                context.is_inside_herdr
                and context.socket_path is not None
                and Path(context.socket_path) == Path(target.socket_path)
            )

            if not same_socket:
                try:
                    resolved_target_context = resolve_herdr_context(
                        explicit_session=inventory.session_name
                    )
                    target_ctx_socket = (
                        Path(resolved_target_context.socket_path)
                        if resolved_target_context.socket_path
                        else None
                    )
                except Exception:
                    target_ctx_socket = None

                if target_ctx_socket is None or target_ctx_socket != Path(inventory.socket_path):
                    self.notify(
                        f"Cannot attach to nonstandard socket for session "
                        f"'{inventory.session_name}'. Open it through its owning Herdr client.",
                        severity="warning",
                    )
                    return

            client = HerdrClient(
                session=inventory.session_name,
                socket_path=target.socket_path,
                timeout=2.0,
            )
            await client.focus_workspace(target.workspace_id)
            if target.pane_id is not None and target_pane is not None:
                if target_tab_id is not None:
                    await client.focus_tab(target_tab_id)
                await client.focus_pane(target_pane.pane_id)

            if not same_socket:
                await self._close_live_controller()
                self.exit(result={"action": "attach", "session": inventory.session_name})
            else:
                if target.pane_id is None:
                    self.notify(f"Focused workspace {ws.label or ws.workspace_id}")
                else:
                    pane_name = (
                        target_pane.name or target_pane.agent or target_pane.pane_id
                        if target_pane
                        else target.pane_id
                    )
                    self.notify(f"Focused {pane_name}")
        except Exception as exc:
            self.notify(f"Could not open in Herdr: {exc}", severity="error")
        finally:
            self._navigation_pending = False

    def action_delete_workspace(self) -> None:
        """Trigger deletion of the selected workspace."""
        if len(self.screen_stack) > 1 or isinstance(
            self.focused, (TextArea, Input, LivePaneOutput)
        ):
            return
        try:
            browser = self.query_one(WorkspaceBrowser)
            browser.request_delete()
        except Exception:
            self.notify("No workspace selected to delete.", severity="warning")

    def on_workspace_browser_delete_requested(
        self, event: WorkspaceBrowser.DeleteRequested
    ) -> None:
        event.stop()
        self.run_worker(self._delete_workspace_flow(event.target), group="workspace-deletion")

    async def _delete_workspace_flow(self, target: WorkspaceTarget) -> None:
        """Confirm and delete/close the targeted workspace."""
        if self.demo:
            self.notify("Demo mode: deletion is disabled.", severity="warning")
            return
        if len(self.screen_stack) > 1:
            return

        inventory = next(
            (
                item
                for item in self.app_state.live_sessions
                if Path(item.socket_path) == Path(target.socket_path)
            ),
            None,
        )
        if not inventory or inventory.error or not inventory.snapshot:
            self.notify("Session is offline. Cannot close workspace.", severity="warning")
            return

        ws = next(
            (w for w in inventory.snapshot.workspaces if w.workspace_id == target.workspace_id),
            None,
        )
        if not ws:
            self.notify("Workspace has already closed or no longer exists.", severity="warning")
            return

        panes = workspace_agents(inventory, target.workspace_id)
        has_running = any(a.agent_status == "working" for a in panes)
        ws_name = safe_display(ws.label or ws.workspace_id)

        if has_running:
            title = f"Close Active Workspace: {ws_name}"
            msg = (
                f"Workspace '{ws_name}' has active/running agents.\n"
                "Are you sure you want to forcibly close this workspace and its panes?"
            )
        else:
            title = f"Delete Completed Workspace: {ws_name}"
            msg = f"Close and delete workspace '{ws_name}' ({len(panes)} panes)?"

        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return

            async def _do_delete():
                try:
                    target_client = self._live_client_for_socket(
                        inventory.session_name, Path(inventory.socket_path)
                    )
                    await target_client.close_workspace(target.workspace_id)
                    self.notify(f"Closed workspace '{ws_name}'.", severity="information")
                    self._schedule_inventory_refresh()
                except Exception as exc:
                    self.notify(f"Failed to close workspace: {exc}", severity="error")

            self.run_worker(_do_delete(), group="workspace-deletion-exec")

        self.push_screen(ConfirmModal(title=title, message=msg), _on_confirm)

    def _update_all_widgets(self) -> None:
        self._header.update_state(self.app_state)
        self._deck.update_state(self.app_state)

    def _requested_session(self) -> Optional[str]:
        """Explicit session, or None when the operator omitted it.

        Blank text is omitted. Do not invent ``default`` here.
        """
        raw = self._session_argument
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        return text or None

    def _state(self, **kwargs):
        if self.session_name:
            kwargs.setdefault("session_name", self.session_name)
        return AppState(**kwargs)

    def _bind_launch_client(self, ctx) -> None:
        """Point status and launch at the resolved session socket."""
        self.session_name = ctx.session_name
        self.socket_path = Path(ctx.socket_path)
        self.app_state.session_name = ctx.session_name
        client = self.client
        if (
            client is not None
            and getattr(client, "session", None) == ctx.session_name
            and Path(getattr(client, "socket_path", "")) == self.socket_path
        ):
            return
        self.client = HerdrClient(session=ctx.session_name, socket_path=self.socket_path)

    def _context_for_launch(self):
        """Resolve omitted session as None. An explicit mismatch raises first."""
        ctx = resolve_launch_context(self._requested_session())
        self._bind_launch_client(ctx)
        return ctx

    def _begin_launch(self, worker, name: str) -> None:
        if self._launch_pending:
            worker.close()
            self.notify(
                "Launch already pending. Wait for the current launch to finish.",
                severity="warning",
            )
            return
        self._launch_pending = True
        self.run_worker(worker, name=name)

    def _exit_launch(self, ctx, session_name: str, workspace_id: Optional[str]) -> None:
        """Keep this multi-project cockpit open; agent navigation remains explicit."""
        self.notify(
            f"Launch is in workspace {workspace_id or session_name}. This cockpit stays open.",
            severity="information",
        )
        self._schedule_inventory_refresh()

    def action_toggle_focus(self) -> None:
        """Focus the CenterDeck."""
        self._deck.focus()

    def action_new_master(self) -> None:
        """Open the Launch Herdr Master dialog."""
        if len(self.screen_stack) > 1 or isinstance(
            self.focused, (TextArea, Input, LivePaneOutput)
        ):
            return

        async def _on_master_launch(params: MasterLaunchParams) -> None:
            try:
                project_path = validate_project_folder(
                    str(params.project_path or initial_project_folder(self.project_path))
                )
                resolve_squad(project_path)
                if self.demo or self.client is None:
                    self._record_demo_master(params)
                    return
                ctx = self._context_for_launch()
                result = await launch_master(
                    client=self.client,
                    name=params.name,
                    harness=params.harness,
                    model=params.model,
                    initial_task=params.initial_task,
                    session=ctx.session_name,
                    effort=params.effort,
                    cwd=str(project_path),
                    ai_head=params.ai_head_override,
                    fresh_workspace=True,
                    focus=False,
                )
                pane = _result_pane(result)
                session_name = result.get("session")
                if not pane or not session_name:
                    self.notify(
                        "Launch result missing pane_id or session. No master was recorded.",
                        severity="error",
                    )
                    return
                self._record_live_master(params, result, pane)
                self.notify(f"Master active in pane {pane}", severity="information")
                self._exit_launch(ctx, str(session_name), result.get("workspace_id"))
            except LaunchContextError as exc:
                self.notify(str(exc), severity="error")
            except Exception as exc:
                self.notify(f"Herdr launch error: {exc}", severity="error")
            finally:
                self._launch_pending = False

        def _on_modal_dismiss(params: Optional[MasterLaunchParams]) -> None:
            if params is not None:
                self._begin_launch(_on_master_launch(params), "launch_master")

        self.push_screen(
            LaunchMasterModal(config_error=self.config_error, project_path=self.project_path),
            _on_modal_dismiss,
        )

    def _record_demo_master(self, params: MasterLaunchParams) -> None:
        recorded = {}
        if self.session_name:
            recorded["herdr_session"] = self.session_name
        master = MasterSession(
            session_id=f"master-{params.name}",
            workspace_id=self.app_state.workspace_id or "default",
            **recorded,
            pane_id="p-master",
            harness=params.harness,
            role="root-orchestrator",
            metadata={
                "model": params.model,
                "effort": params.effort,
                "demo": True,
                "socket_path": str(Path("/demo/herdr.sock")),
            },
        )
        self.app_state.masters[master.session_id] = master
        self.app_state.selected_target_id = master.session_id
        self._update_all_widgets()

    def _record_live_master(self, params: MasterLaunchParams, result: dict, pane: str) -> None:
        master = MasterSession(
            session_id=f"master-{params.name}",
            herdr_session=str(result.get("session") or self.session_name),
            workspace_id=str(result.get("workspace_id") or ""),
            pane_id=pane,
            harness=params.harness,
            role="root-orchestrator",
            metadata={
                "model": result.get("model"),
                "effort": result.get("effort"),
                "pane_id": pane,
                "project_path": str(
                    params.project_path or initial_project_folder(self.project_path)
                ),
                "socket_path": str(result.get("socket") or "").strip(),
            },
        )
        self.app_state.masters[master.session_id] = master
        self.app_state.selected_target_id = master.session_id
        self._update_all_widgets()

    def action_new_ergo(self) -> None:
        """Open the 2-column Squad Settings & Ergo Pipeline Launcher."""
        if len(self.screen_stack) > 1 or isinstance(
            self.focused, (TextArea, Input, LivePaneOutput)
        ):
            return

        async def _on_ergo_launch(params: ErgoLaunchParams) -> None:
            try:
                if not params.start_pipeline:
                    if params.squad is not None:
                        self.app_state.squad = params.squad
                        self._settings_squad = params.squad.model_copy(deep=True)
                        self._update_all_widgets()
                    return
                project_path = validate_project_folder(
                    str(params.project_path or initial_project_folder(self.project_path))
                )
                resolved = resolve_squad(project_path)
                if self.demo or self.client is None:
                    self.notify("Demo mode does not launch an orchestrator.", severity="warning")
                    return
                ctx = self._context_for_launch()
                result = await launch_ergo(
                    client=self.client,
                    intent=params.intent,
                    workspace_label=params.workspace_label,
                    squad=params.squad or resolved.squad,
                    cwd=str(project_path),
                    session=ctx.session_name,
                    ai_head=params.ai_head_override,
                    fresh_workspace=True,
                    focus=False,
                )
                pane = _result_pane(result)
                session_name = result.get("session")
                if not pane or not session_name:
                    self.notify(
                        "Launch result missing pane_id. "
                        "No run was recorded and no attach was started.",
                        severity="error",
                    )
                    return
                workspace_id = result.get("workspace_id")
                self.app_state.squad = params.squad or resolved.squad
                self.app_state.workspace_label = params.workspace_label
                self.app_state.pipeline_run = PipelineRun(
                    run_id=f"ergo-{params.workspace_label}",
                    intent=params.intent,
                    squad=self.app_state.squad,
                    phase=PipelinePhase.PREFLIGHT,
                    workspace_id=str(workspace_id) if workspace_id else None,
                    metadata={
                        "pane_id": pane,
                        "orchestrator_pane_id": result.get("orchestrator_pane_id") or pane,
                        "session": session_name,
                        "agent_name": result.get("agent_name"),
                        "project_path": str(project_path),
                    },
                )
                self._update_all_widgets()
                self.notify(f"Ergo orchestrator pane {pane}", severity="information")
                self._exit_launch(ctx, str(session_name), workspace_id)
            except LaunchContextError as exc:
                self.notify(str(exc), severity="error")
            except Exception as exc:
                self.notify(f"Ergo launch error: {exc}", severity="error")
            finally:
                if params.start_pipeline:
                    self._launch_pending = False

        def _on_modal_dismiss(params: Optional[ErgoLaunchParams]) -> None:
            if params is None:
                return
            if not params.start_pipeline:
                self.run_worker(_on_ergo_launch(params), name="apply_squad")
                return
            self._begin_launch(_on_ergo_launch(params), "launch_ergo")

        self.push_screen(
            LaunchErgoModal(
                self._settings_squad,
                sources=self.squad_sources,
                mode=self.squad_mode,
                native_harness=self.native_harness,
                project_path=self.project_path,
                config_error=self.config_error,
            ),
            _on_modal_dismiss,
        )

    def action_open_settings(self) -> None:
        """Open the Settings modal."""
        if (len(self.screen_stack) > 1 and not isinstance(self.screen, AiHeadScreen)) or isinstance(
            self.focused, (TextArea, Input, LivePaneOutput)
        ):
            return

        def _on_settings_dismiss(result: Optional[tuple[HerdrMasterConfig, Any]]) -> None:
            if result is None:
                return
            config, squad = result
            self.app_state.config = config
            self.app_state.squad = squad
            self._settings_squad = squad.model_copy(deep=True)
            self._update_all_widgets()
            self._schedule_inventory_refresh()
            self.notify("Settings saved.", severity="information")

        self.push_screen(
            SettingsModal(
                config=getattr(self.app_state, "config", None),
                squad=self._settings_squad,
                project_path=self.project_path,
            ),
            _on_settings_dismiss,
        )

    def action_new_council(self) -> None:
        """Open the AI Council deliberation launch modal."""
        if len(self.screen_stack) > 1 or isinstance(
            self.focused, (TextArea, Input, LivePaneOutput)
        ):
            return

        async def _on_council_launch(params: CouncilLaunchParams) -> None:
            try:
                project_path = validate_project_folder(
                    str(params.project_folder or initial_project_folder(self.project_path))
                )
                if self.demo or self.client is None:
                    self.notify("Demo mode does not launch an orchestrator.", severity="warning")
                    return
                ctx = self._context_for_launch()
                session_name = getattr(self.app_state, "session_name", ctx.session_name)
                result = await launch_council(
                    client=self.client,
                    intent=params.intent,
                    workspace_label=params.workspace_label,
                    council=params.council_config,
                    cwd=str(project_path),
                    session=session_name,
                    ai_head=False,
                    fresh_workspace=True,
                    focus=False,
                )
                pane = _result_pane(result)
                session_name = result.get("session")
                if not pane or not session_name:
                    self.notify(
                        "Launch result missing pane_id. "
                        "No run was recorded and no attach was started.",
                        severity="error",
                    )
                    return
                workspace_id = result.get("workspace_id")
                self.app_state.workspace_label = params.workspace_label
                self.app_state.pipeline_run = PipelineRun(
                    run_id=f"council-{params.workspace_label}",
                    intent=params.intent,
                    squad=self.app_state.squad,
                    phase=PipelinePhase.PREFLIGHT,
                    workspace_id=str(workspace_id) if workspace_id else None,
                    metadata={
                        "mode": "council",
                        "pane_id": pane,
                        "session": session_name,
                        "agent_name": result.get("agent_name"),
                        "project_path": str(project_path),
                    },
                )
                self._update_all_widgets()
                self.notify(f"Council coordinator pane {pane}", severity="information")
                self._exit_launch(ctx, str(session_name), workspace_id)
            except LaunchContextError as exc:
                self.notify(str(exc), severity="error")
            except Exception as exc:
                self.notify(f"Council launch error: {exc}", severity="error")
            finally:
                self._launch_pending = False

        def _on_modal_dismiss(params: Optional[CouncilLaunchParams]) -> None:
            if params is None:
                return
            self._begin_launch(_on_council_launch(params), "launch_council")

        self.push_screen(
            LaunchCouncilModal(
                config=getattr(self.app_state, "config", None),
                project_path=self.project_path,
            ),
            _on_modal_dismiss,
        )

    def action_new_orchestration(self) -> None:
        """Open the single-harness Orchestration launch modal."""
        if len(self.screen_stack) > 1 or isinstance(
            self.focused, (TextArea, Input, LivePaneOutput)
        ):
            return

        async def _on_orchestration_launch(params: OrchestrationLaunchParams) -> None:
            try:
                project_path = validate_project_folder(
                    str(params.project_path or initial_project_folder(self.project_path))
                )
                if self.demo or self.client is None:
                    self.notify("Demo mode does not launch an orchestrator.", severity="warning")
                    return
                ctx = self._context_for_launch()
                session_name = getattr(self.app_state, "session_name", ctx.session_name)
                result = await launch_orchestration(
                    client=self.client,
                    intent=params.intent,
                    workspace_label=params.workspace_label,
                    harness=params.harness,
                    scout_model=params.scout_model,
                    architect_model=params.architect_model,
                    coder_model=params.coder_model,
                    reviewer_model=params.reviewer_model,
                    scout_effort=params.scout_effort,
                    architect_effort=params.architect_effort,
                    reviewer_effort=params.reviewer_effort,
                    cwd=str(project_path),
                    session=session_name,
                    ai_head=params.ai_head_override,
                    dangerously_skip_permissions=params.dangerously_skip_permissions,
                    fresh_workspace=True,
                    focus=False,
                )
                pane = _result_pane(result)
                session_name = result.get("session")
                if not pane or not session_name:
                    self.notify(
                        "Launch result missing pane_id. "
                        "No run was recorded and no attach was started.",
                        severity="error",
                    )
                    return
                workspace_id = result.get("workspace_id")
                squad = result["squad"]
                self.app_state.squad = squad
                self.app_state.workspace_label = params.workspace_label
                self.app_state.pipeline_run = PipelineRun(
                    run_id=f"orch-{params.workspace_label}",
                    intent=params.intent,
                    squad=squad,
                    phase=PipelinePhase.PREFLIGHT,
                    workspace_id=str(workspace_id) if workspace_id else None,
                    orchestrator_pane_id=result.get("orchestrator_pane_id") or pane,
                    metadata={
                        "mode": "native",
                        "native_harness": params.harness,
                        "dangerously_skip_permissions": params.dangerously_skip_permissions,
                        "pane_id": pane,
                        "orchestrator_pane_id": result.get("orchestrator_pane_id") or pane,
                        "session": session_name,
                        "agent_name": result.get("agent_name"),
                        "project_path": str(project_path),
                    },
                )
                self._update_all_widgets()
                self.notify(f"Orchestration pane {pane}", severity="information")
                self._exit_launch(ctx, str(session_name), workspace_id)
            except LaunchContextError as exc:
                self.notify(str(exc), severity="error")
            except Exception as exc:
                self.notify(f"Orchestration launch error: {exc}", severity="error")
            finally:
                self._launch_pending = False

        def _on_modal_dismiss(params: Optional[OrchestrationLaunchParams]) -> None:
            if params is None:
                return
            self._begin_launch(_on_orchestration_launch(params), "launch_orchestration")

        self.push_screen(
            LaunchOrchestrationModal(
                project_path=self.project_path,
                config_error=self.config_error,
            ),
            _on_modal_dismiss,
        )

    def action_launch_aihead(self) -> None:
        """Launch, ensure, and switch to the dedicated full-page AiHead screen."""
        if not isinstance(self.screen, AiHeadScreen):
            cfg = getattr(self.app_state, "config", None)
            model = cfg.ai_head.binding.model if cfg and cfg.ai_head else "gpt-6.1-sol"
            harness = (
                cfg.ai_head.binding.kind.value
                if cfg and cfg.ai_head and hasattr(cfg.ai_head.binding.kind, "value")
                else str(cfg.ai_head.binding.kind if cfg and cfg.ai_head else "opencode")
            )
            cwd = (
                cfg.ai_head.cwd
                if cfg and cfg.ai_head and cfg.ai_head.cwd
                else str(self.project_path or Path.home())
            )

            can_reuse = (
                self._ai_head_confirmed_target is not None
                and self._last_live_frame is not None
                and self._live_key(self._last_live_frame.target) == self._live_key(self._ai_head_confirmed_target)
            )

            status_val = (
                self.app_state.ai_head_info.get("status", "ready")
                if (can_reuse and self.app_state.ai_head_info)
                else "pending"
            )

            screen = AiHeadScreen(
                model=model,
                harness=harness,
                cwd=cwd,
                status=status_val,
            )

            if can_reuse and self._last_live_frame is not None:
                screen.update_live_frame(self._last_live_frame)
            else:
                pending_frame = PaneFrame(
                    lease=None,
                    target=None,
                    phase="loading",
                    agent_status="pending",
                    text_ansi="",
                    error=None,
                    can_reply=False,
                    reply_eligible=False,
                )
                screen.update_live_frame(pending_frame)

            self.push_screen(screen)

        self._ensure_ai_head_if_enabled(force=True, focus=True)

    def action_view_logs(self) -> None:
        """Open diagnostic log viewer modal."""
        self.push_screen(LogViewerModal())

    def on_quick_launch_bar_launch_ai_head_requested(
        self, event: QuickLaunchBar.LaunchAiHeadRequested
    ) -> None:
        self.action_launch_aihead()

    def on_quick_launch_bar_launch_master_requested(
        self, event: QuickLaunchBar.LaunchMasterRequested
    ) -> None:
        self.action_new_master()

    def on_quick_launch_bar_launch_ergo_requested(
        self, event: QuickLaunchBar.LaunchErgoRequested
    ) -> None:
        self.action_new_ergo()

    def on_quick_launch_bar_launch_orchestration_requested(
        self, event: QuickLaunchBar.LaunchOrchestrationRequested
    ) -> None:
        self.action_new_orchestration()

    def on_quick_launch_bar_launch_council_requested(
        self, event: QuickLaunchBar.LaunchCouncilRequested
    ) -> None:
        self.action_new_council()

    def on_quick_launch_bar_open_settings_requested(
        self, event: QuickLaunchBar.OpenSettingsRequested
    ) -> None:
        self.action_open_settings()

    def action_open_config(self) -> None:
        """Route config action directly to Settings modal."""
        self.action_open_settings()

    def on_live_pane_view_focus_return_requested(
        self, event: LivePaneView.FocusReturnRequested
    ) -> None:
        event.stop()
        self._focus_cockpit_tree()

    def _focus_cockpit_tree(self) -> None:
        def _do_focus() -> None:
            try:
                browser = self.query_one("WorkspaceBrowser")
                try:
                    browser.query_one("#workspace-tree").focus()
                except Exception:
                    browser.focus()
            except Exception:
                pass

        _do_focus()
        self.call_after_refresh(_do_focus)

    def on_target_selected(self, message: TargetSelected) -> None:
        """Update selected target across tree and deck."""
        self.app_state.selected_target_id = message.target_id
        self._deck.update_state(self.app_state)
