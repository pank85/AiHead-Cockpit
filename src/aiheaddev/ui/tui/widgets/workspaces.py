"""Live, clickable workspace/agent control panel."""

from pathlib import Path
from typing import Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Click, Key
from textual.message import Message
from textual.widgets import Button, Static, Tree

from aiheaddev.core.workspace_targets import (
    CANONICAL_ERGO_NAME, CANONICAL_MASTER_NAME, WorkspaceTarget,
    _is_agent_bearing_pane, _is_verified_owning_tab, _normalize_name,
    known_master_panes_for_workspace, safe_display, workspace_agents, workspace_roles,
)
from aiheaddev.ui.tui.state import AppState
from aiheaddev.ui.tui.widgets.quick_launch import QuickLaunchBar


BADGES = {
    "working": ("● running", "green"),
    "blocked": ("! blocked", "yellow"),
    "idle": ("○ idle", "cyan"),
    "done": ("✓ done", "dim"),
    "unknown": ("? unknown", "dim"),
    "terminal": ("› terminal", "dim"),
}


def status_label(status: Optional[str]) -> Text:
    label, style = BADGES.get(status or "unknown", BADGES["unknown"])
    return Text(label, style=style)


ROLE_STYLES = {
    "Master": "bold cyan",
    "Ergo": "bold magenta",
}


OPEN_BOUNDARY_HELP = (
    "Open switches to Herdr; another-session/outside-Herdr attach replaces this cockpit. "
    "Run `herdm tui` to return."
)
OPEN_BOUNDARY_HELP_NARROW = (
    "Attach replaces cockpit.\nRun `herdm tui` to return."
)


class WorkspaceBrowser(Vertical):
    """Workspace tree. Selection never focuses Herdr; Open does."""

    DEFAULT_CSS = """
    WorkspaceBrowser {
        height: 1fr;
        padding: 0 1;
        overflow-x: hidden;
        background: #161E2E;
    }
    #open-strip {
        height: 3;
        overflow: hidden hidden;
        background: #1E293B;
        padding: 0 1;
    }
    #open-strip-row {
        height: 1;
    }
    #btn-open-herdr {
        border: none;
        height: 1;
        min-height: 1;
        padding: 0 1;
        width: auto;
        max-width: 100%;
        margin: 0 1 0 0;
        background: #0284C7;
        color: #F8FAFC;
        text-style: bold;
    }
    #btn-open-herdr:hover {
        background: #0369A1;
    }
    #btn-open-herdr:focus {
        background: #0284C7;
        text-style: bold;
    }
    #btn-open-herdr:disabled {
        background: #334155;
        color: #64748B;
    }
    #btn-delete-workspace {
        border: none;
        height: 1;
        min-height: 1;
        padding: 0 1;
        width: auto;
        max-width: 100%;
        margin: 0 1 0 0;
        background: #DC2626;
        color: #F8FAFC;
        text-style: bold;
    }
    #btn-delete-workspace:hover {
        background: #B91C1C;
    }
    #btn-delete-workspace:focus {
        background: #EF4444;
        text-style: bold;
    }
    #btn-delete-workspace:disabled {
        background: #334155;
        color: #64748B;
    }
    #open-selection {
        height: 1;
        width: 1fr;
        overflow: hidden hidden;
        color: #F8FAFC;
        text-style: bold;
    }
    #open-help {
        height: 2;
        overflow: hidden hidden;
        color: #94A3B8;
    }
    #workspace-summary {
        height: 1;
        color: #94A3B8;
        overflow: hidden hidden;
        background: #161E2E;
    }
    #workspace-tree {
        height: 1fr;
        background: #111827;
        padding: 0;
        overflow-x: hidden;
        color: #F8FAFC;
    }
    #workspace-tree:focus > .tree--cursor {
        background: #1E293B;
        color: #38BDF8;
        text-style: bold;
    }
    #workspace-tree > .tree--guides {
        color: #334155;
    }
    """

    class OpenRequested(Message):
        def __init__(self, target: WorkspaceTarget) -> None:
            super().__init__()
            self.target = target

    class DeleteRequested(Message):
        """Operator requested closing/deleting a workspace."""

        def __init__(self, target: WorkspaceTarget) -> None:
            super().__init__()
            self.target = target

    class SelectionChanged(Message):
        """Accepted selection changed in the workspace tree."""

        def __init__(self, target: Optional[WorkspaceTarget]) -> None:
            super().__init__()
            self.target = target

    class PaneActivated(Message):
        """Operator explicitly activated (pressed Enter or clicked) a pane node."""

        def __init__(self, target: WorkspaceTarget) -> None:
            super().__init__()
            self.target = target

    def __init__(self, app_state: AppState, **kwargs) -> None:
        super().__init__(**kwargs)
        self.app_state = app_state
        self._summary = Static("Discovering Herdr sessions…", id="workspace-summary")
        self._tree: Tree[WorkspaceTarget] = Tree("Workspaces", id="workspace-tree")
        self._tree.show_root = False
        self._workspace_nodes = {}
        self._pane_nodes = {}
        self._selected_target: Optional[WorkspaceTarget] = None
        self._last_emitted_target: Optional[WorkspaceTarget] = None
        self._selected_target_disappeared: bool = False
        self._operator_navigating: bool = False

    def compose(self) -> ComposeResult:
        yield QuickLaunchBar()
        with Vertical(id="open-strip"):
            with Horizontal(id="open-strip-row"):
                yield Button(Text("Open in Herdr [o]"), id="btn-open-herdr")
                yield Button(Text("Delete [d]"), id="btn-delete-workspace")
                yield Static("No selection", id="open-selection")
            yield Static(OPEN_BOUNDARY_HELP, id="open-help")
        yield self._summary
        yield self._tree

    def on_mount(self) -> None:
        self.update_state(self.app_state)
        self._tree.focus()
        self._update_open_strip()

    def on_resize(self, event: object) -> None:
        self._update_open_strip()

    def _update_open_strip(self) -> None:
        """Synchronize the open button, delete button, and selection text with the active selection."""
        try:
            btn = self.query_one("#btn-open-herdr", Button)
            del_btn = self.query_one("#btn-delete-workspace", Button)
            sel_static = self.query_one("#open-selection", Static)
            help_static = self.query_one("#open-help", Static)
        except Exception:
            return

        width = self.size.width if self.size.width > 0 else 80
        if width < 55:
            help_static.update(OPEN_BOUNDARY_HELP_NARROW)
        else:
            help_static.update(OPEN_BOUNDARY_HELP)

        if self.app_state.inventory_demo:
            btn.disabled = True
            btn.tooltip = "Demo mode: live terminal navigation is disabled."
            del_btn.disabled = True
            del_btn.tooltip = "Demo mode: deletion disabled."
            sel_static.update(Text("Demo mode (offline)", style="dim"))
            return

        if self._selected_target_disappeared:
            btn.disabled = True
            btn.tooltip = "Selected target has disappeared. Select a current item."
            del_btn.disabled = True
            del_btn.tooltip = "No target selected."
            sel_static.update(Text("Selection disappeared · reselect target", style="dim"))
            return

        if self._selected_target is None:
            btn.disabled = True
            btn.tooltip = "No target selected. Select a workspace or pane to open."
            del_btn.disabled = True
            del_btn.tooltip = "No workspace selected to delete."
            sel_static.update(Text("No selection", style="dim"))
            return

        target: WorkspaceTarget = self._selected_target
        if not target.workspace_id:
            btn.disabled = True
            btn.tooltip = "Selected item has no workspace to open."
            del_btn.disabled = True
            del_btn.tooltip = "Selected item has no workspace."
            sel_static.update(Text("No workspace selected", style="dim"))
            return

        del_btn.disabled = False
        del_btn.tooltip = f"Close/Delete workspace {safe_display(target.workspace_id)}"

        inventory = next(
            (
                item
                for item in self.app_state.live_sessions
                if Path(item.socket_path) == Path(target.socket_path)
            ),
            None,
        )
        if not inventory or inventory.error or not inventory.snapshot:
            btn.disabled = True
            btn.tooltip = "Session is offline. Wait for reconnection."
            sel_static.update(Text(f"Offline · {safe_display(target.session_name)}", style="red"))
            return

        ws = next(
            (w for w in inventory.snapshot.workspaces if w.workspace_id == target.workspace_id),
            None,
        )
        if ws is None:
            btn.disabled = True
            btn.tooltip = "Workspace no longer exists."
            del_btn.disabled = True
            sel_static.update(Text("Workspace closed", style="dim"))
            return

        panes = workspace_agents(inventory, target.workspace_id)
        if target.pane_id is None:
            # Workspace target
            if not panes:
                btn.disabled = True
                btn.tooltip = "Workspace has no open panes to open."
                sel_static.update(
                    Text(
                        f"Workspace {safe_display(ws.label or ws.workspace_id)} · empty (0 panes)",
                        style="dim",
                    )
                )
            else:
                btn.disabled = False
                btn.tooltip = OPEN_BOUNDARY_HELP
                ws_title = safe_display(ws.label or ws.workspace_id)
                pane_word = "pane" if len(panes) == 1 else "panes"
                sess = safe_display(inventory.session_name)
                sel_static.update(
                    Text(
                        f"Workspace: {ws_title} ({sess}) · {len(panes)} {pane_word}",
                        style="bold white",
                    )
                )
        else:
            # Pane target
            agent_item = next((a for a in panes if a.pane_id == target.pane_id), None)
            if not agent_item:
                btn.disabled = True
                btn.tooltip = "Pane has closed. Select a current agent."
                sel_static.update(Text(f"Pane {safe_display(target.pane_id)} closed", style="dim"))
            else:
                if agent_item.tab_id:
                    tab = next(
                        (t for t in inventory.snapshot.tabs if t.tab_id == agent_item.tab_id),
                        None,
                    )
                    if tab is None or tab.workspace_id != target.workspace_id:
                        btn.disabled = True
                        btn.tooltip = "Pane tab ownership is mismatched or missing."
                        sel_static.update(
                            Text(f"Pane {safe_display(target.pane_id)} tab invalid", style="yellow")
                        )
                        return
                btn.disabled = False
                btn.tooltip = OPEN_BOUNDARY_HELP
                pane_name = safe_display(agent_item.name or agent_item.agent or "Terminal")
                ws_title = safe_display(ws.label or ws.workspace_id)
                sel_static.update(
                    Text(
                        f"Pane: {pane_name} ({safe_display(agent_item.pane_id)}) · {ws_title}",
                        style="bold white",
                    )
                )

    def request_open(self) -> None:
        """Resolve deliberate selection and post OpenRequested for an eligible live target."""
        if self.app_state.inventory_demo:
            self.notify("Demo mode: live terminal navigation is disabled.", severity="warning")
            return

        if self._selected_target_disappeared:
            self.notify(
                "Selected target disappeared. Reselect a current target.",
                severity="warning",
            )
            return

        if self._selected_target is None:
            self.notify("No target selected to open in Herdr.", severity="warning")
            return

        target = self._selected_target
        if not target.workspace_id:
            self.notify("Selected item has no workspace or terminal to open.", severity="warning")
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
            self.notify("Session is offline. Cannot open in Herdr.", severity="warning")
            return

        ws = next(
            (w for w in inventory.snapshot.workspaces if w.workspace_id == target.workspace_id),
            None,
        )
        if not ws:
            self.notify("Workspace has closed. Cannot open in Herdr.", severity="warning")
            return

        panes = workspace_agents(inventory, target.workspace_id)
        if not panes:
            self.notify("Selected workspace has no open panes to open.", severity="warning")
            return

        if target.pane_id is not None:
            pane_item = next((p for p in panes if p.pane_id == target.pane_id), None)
            if not pane_item:
                self.notify("Selected pane has closed.", severity="warning")
                return
            if pane_item.tab_id:
                tab = next(
                    (t for t in inventory.snapshot.tabs if t.tab_id == pane_item.tab_id),
                    None,
                )
                if tab is None or tab.workspace_id != target.workspace_id:
                    self.notify(
                        "Pane tab ownership is mismatched or missing. Cannot open in Herdr.",
                        severity="warning",
                    )
                    return

        self.post_message(self.OpenRequested(target))

    def on_key(self, event: Key) -> None:
        if not self._tree.has_focus:
            return
        if event.key in ("d", "x"):
            event.stop()
            self.request_delete()
            return
        if event.key in (
            "up",
            "down",
            "left",
            "right",
            "home",
            "end",
            "pageup",
            "pagedown",
        ):
            self._operator_navigating = True
            if self._selected_target_disappeared:
                self.call_after_refresh(self._reselect_current_if_navigating)

    def on_click(self, event: Click) -> None:
        if (
            getattr(event, "widget", None) is self._tree
            or getattr(event, "control", None) is self._tree
        ):
            self._operator_navigating = True

    def _notify_selection_if_changed(self) -> None:
        current = self._selected_target
        if current != self._last_emitted_target:
            self._last_emitted_target = current
            self.post_message(self.SelectionChanged(current))

    def _reselect_current_if_navigating(self) -> None:
        if (
            self._operator_navigating
            and self._selected_target_disappeared
            and self._tree.has_focus
        ):
            node = self._tree.cursor_node
            if node is not None and node.data is not None:
                self._selected_target = node.data
                self._selected_target_disappeared = False
                self._update_open_strip()
                self._notify_selection_if_changed()
        self._operator_navigating = False

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[WorkspaceTarget]) -> None:
        if self._selected_target_disappeared:
            if not self._operator_navigating or not self._tree.has_focus:
                return
            self._selected_target_disappeared = False

        self._operator_navigating = False
        if event.node is not None and event.node.data is not None:
            self._selected_target = event.node.data
        else:
            self._selected_target = None
        self._update_open_strip()
        self._notify_selection_if_changed()

    def on_tree_node_selected(self, event: Tree.NodeSelected[WorkspaceTarget]) -> None:
        event.stop()
        self._operator_navigating = False
        if event.node is not None and event.node.data is not None:
            self._selected_target = event.node.data
            self._selected_target_disappeared = False
        else:
            self._selected_target = None
            self._selected_target_disappeared = False
        self._update_open_strip()
        self._notify_selection_if_changed()
        if self._selected_target is not None and self._selected_target.pane_id is not None:
            self.post_message(self.PaneActivated(self._selected_target))

    def request_delete(self) -> None:
        """Resolve selection and post DeleteRequested for an eligible workspace."""
        if self.app_state.inventory_demo:
            self.notify("Demo mode: deletion is disabled.", severity="warning")
            return

        if self._selected_target_disappeared:
            self.notify(
                "Selected target disappeared. Reselect a current target.",
                severity="warning",
            )
            return

        if self._selected_target is None or not self._selected_target.workspace_id:
            self.notify("No workspace selected to delete.", severity="warning")
            return

        self.post_message(self.DeleteRequested(self._selected_target))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-open-herdr":
            event.stop()
            self.request_open()
        elif event.button.id == "btn-delete-workspace":
            event.stop()
            self.request_delete()

    def update_state(self, app_state: AppState) -> None:
        self.app_state = app_state
        inventories = app_state.live_sessions
        seen_workspaces = set()
        seen_panes = set()
        live_count = 0
        workspace_count = 0
        agent_count = 0
        for inventory in inventories:
            socket_key = str(inventory.socket_path)
            snapshot = inventory.snapshot
            if not inventory.error:
                live_count += 1
            if snapshot is None or not snapshot.workspaces:
                key = (socket_key, "")
                seen_workspaces.add(key)
                label = Text(
                    f"{'× offline' if inventory.error else '○ empty'} · "
                    f"{safe_display(inventory.session_name)} · "
                    f"{safe_display(inventory.error or 'No open workspaces')}",
                    style="dim",
                )
                if key not in self._workspace_nodes:
                    self._workspace_nodes[key] = self._tree.root.add_leaf(label)
                else:
                    self._workspace_nodes[key].set_label(label)
                continue
            for workspace in snapshot.workspaces:
                workspace_count += 1
                key = (socket_key, workspace.workspace_id)
                seen_workspaces.add(key)
                agents = workspace_agents(inventory, workspace.workspace_id)
                agent_count += sum(agent.agent is not None for agent in agents)
                statuses = {agent.agent_status for agent in agents} | {workspace.agent_status}
                status = next(
                    (
                        value
                        for value in ("working", "blocked", "idle", "done")
                        if value in statuses
                    ),
                    "unknown",
                )
                known_masters = known_master_panes_for_workspace(
                    getattr(app_state, "masters", None),
                    inventory.socket_path,
                    workspace.workspace_id,
                )
                roles = workspace_roles(
                    inventory, workspace.workspace_id, known_master_panes=known_masters
                )
                label = Text("× offline", style="red") if inventory.error else status_label(status)
                for role in roles:
                    role_style = "dim" if inventory.error else ROLE_STYLES.get(role, "bold cyan")
                    label.append(f" [{role}]", style=role_style)
                ws_name = safe_display(workspace.label or workspace.workspace_id)
                label.append(f"  {ws_name}" if not roles else f" {ws_name}", style="bold white")
                pane_word = "pane" if len(agents) == 1 else "panes"
                label.append(
                    f" · {safe_display(inventory.session_name)} · {len(agents)} {pane_word}",
                    style="dim",
                )
                blocked_count = sum(agent.agent_status == "blocked" for agent in agents)
                if not inventory.error and blocked_count > 0 and status != "blocked":
                    label.append(f" · ! {blocked_count} blocked", style="yellow")
                if inventory.error:
                    label.append(" · stale", style="red")
                target = WorkspaceTarget(
                    inventory.session_name, inventory.socket_path, workspace.workspace_id
                )
                if key not in self._workspace_nodes:
                    self._workspace_nodes[key] = self._tree.root.add(label, data=target)
                else:
                    self._workspace_nodes[key].set_label(label)
                    self._workspace_nodes[key].data = target
                node = self._workspace_nodes[key]
                # Reconcile in place: polling must not reset expansion, cursor, or scroll.
                for agent in agents:
                    pane_key = (*key, agent.pane_id)
                    seen_panes.add(pane_key)
                    tab = next((t for t in snapshot.tabs if t.tab_id == agent.tab_id), None)
                    is_head = bool(
                        (tab and tab.label and tab.label.strip() == "ai-head")
                        or (agent.name and agent.name.strip() == "ai-head")
                    )
                    label = (
                        Text("× stale", style="red")
                        if inventory.error
                        else status_label(agent.agent_status)
                    )
                    if is_head:
                        label.append(" [AiHead]", style="bold magenta")
                        label.append(f"  {safe_display(agent.name or 'AiHead')}", style="bold white")
                        label.append(" · ai-head", style="cyan")
                    else:
                        label.append(
                            f"  {safe_display(agent.name or agent.agent or 'Terminal')}", style="bold"
                        )
                        tab_name = tab.label if tab and tab.label else agent.tab_id or "tab"
                        label.append(f" · {safe_display(tab_name)}")
                    label.append(f" · {safe_display(agent.pane_id)}", style="dim")
                    if agent.cwd:
                        label.append(f" · {safe_display(agent.cwd)}", style="dim")
                    target = WorkspaceTarget(
                        inventory.session_name,
                        inventory.socket_path,
                        workspace.workspace_id,
                        agent.tab_id,
                        agent.pane_id,
                    )
                    if pane_key not in self._pane_nodes:
                        self._pane_nodes[pane_key] = node.add_leaf(label, data=target)
                    else:
                        self._pane_nodes[pane_key].set_label(label)
                        self._pane_nodes[pane_key].data = target
        for key in list(self._pane_nodes):
            if key not in seen_panes:
                self._pane_nodes.pop(key).remove()
        for key in list(self._workspace_nodes):
            if key not in seen_workspaces:
                self._workspace_nodes.pop(key).remove()

        if self._selected_target is not None:
            target_socket = str(self._selected_target.socket_path)
            if self._selected_target.pane_id is not None:
                pane_key = (
                    target_socket,
                    self._selected_target.workspace_id,
                    self._selected_target.pane_id,
                )
                if pane_key not in seen_panes:
                    self._selected_target = None
                    self._selected_target_disappeared = True
                    self._notify_selection_if_changed()
                else:
                    node = self._pane_nodes.get(pane_key)
                    if node is not None and node.data is not None:
                        self._selected_target = node.data
                        self._last_emitted_target = node.data
            else:
                ws_key = (target_socket, self._selected_target.workspace_id)
                if ws_key not in seen_workspaces:
                    self._selected_target = None
                    self._selected_target_disappeared = True
                    self._notify_selection_if_changed()
                else:
                    node = self._workspace_nodes.get(ws_key)
                    if node is not None and node.data is not None:
                        self._selected_target = node.data
                        self._last_emitted_target = node.data
        prefix = "DEMO · " if app_state.inventory_demo else ""
        if not inventories:
            summary = "No session data yet. Waiting for Herdr…"
        else:
            summary = (
                f"{live_count}/{len(inventories)} sessions online · {workspace_count} workspaces"
                f" · {agent_count} agents · Click row to select; [o] to open"
            )
        self._summary.update(Text(prefix + summary))
        errors = [f"{item.session_name}: {item.error}" for item in inventories if item.error]
        self._summary.tooltip = "\n".join(errors) or None
        self._operator_navigating = False
        self._update_open_strip()
