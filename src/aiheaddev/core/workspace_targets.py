"""Pure workspace identity, display and inventory selection helpers."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unicodedata import category

from herdr_master.core.models import MasterSession
from herdr_master.herdr.discovery import SessionInventory
from herdr_master.herdr.snapshot import AgentSnapshot, SessionSnapshot, TabSnapshot


def safe_display(value: object) -> str:
    """Represent untrusted identifiers literally, including invisible controls."""
    result = []
    for char in str(value):
        code = ord(char)
        if char == "\\":
            result.append("\\\\")
        elif category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp"):
            if code <= 0xff:
                result.append(f"\\x{code:02x}")
            elif code <= 0xffff:
                result.append(f"\\u{code:04x}")
            else:
                result.append(f"\\U{code:08x}")
        else:
            result.append(char)
    return "".join(result)


@dataclass(frozen=True)
class WorkspaceTarget:
    session_name: str
    socket_path: Path
    workspace_id: str
    tab_id: Optional[str] = None
    pane_id: Optional[str] = None



def workspace_agents(inventory: SessionInventory, workspace_id: str) -> list[AgentSnapshot]:
    """Merge pane metadata with agents; include ordinary terminals and deduplicate."""
    snapshot = inventory.snapshot
    if snapshot is None:
        return []
    tab_workspaces = {tab.tab_id: tab.workspace_id for tab in snapshot.tabs}
    agents = {}
    for pane in snapshot.panes:
        workspace = pane.workspace_id or tab_workspaces.get(pane.tab_id)
        if workspace == workspace_id:
            agents[pane.pane_id] = AgentSnapshot(
                pane_id=pane.pane_id,
                workspace_id=workspace,
                tab_id=pane.tab_id,
                agent=pane.agent,
                agent_status=pane.agent_status if pane.agent else "terminal",
                cwd=pane.cwd,
                focused=pane.focused,
            )
    for agent in snapshot.agents:
        previous = agents.get(agent.pane_id)
        workspace = (
            agent.workspace_id
            or tab_workspaces.get(agent.tab_id)
            or (previous.workspace_id if previous else None)
        )
        if workspace == workspace_id:
            values = agent.model_dump(exclude_none=True)
            values["workspace_id"] = workspace
            agents[agent.pane_id] = (
                previous.model_copy(update=values) if previous else agent.model_copy(update=values)
            )
    return list(agents.values())


CANONICAL_MASTER_NAME = "master-coordinator"
CANONICAL_ERGO_NAME = "ergo-orchestrator"


def _normalize_name(value: Optional[str]) -> str:
    if not value:
        return ""
    return " ".join(value.strip().split()).lower()


def _is_agent_bearing_pane(item: AgentSnapshot, snapshot: SessionSnapshot) -> bool:
    """Return True if the snapshot entry represents an active or configured agent,
    not an ordinary terminal shell pane.
    """
    if item.agent and item.agent.strip():
        return True
    if any(a.pane_id == item.pane_id for a in snapshot.agents):
        return True
    return False


def _is_verified_owning_tab(
    item: AgentSnapshot,
    tab: Optional[TabSnapshot],
    workspace_id: str,
    snapshot: SessionSnapshot,
) -> bool:
    """Return True only if tab is a verified owning tab in workspace_id for item."""
    if tab is None or not item.tab_id:
        return False
    if tab.tab_id != item.tab_id:
        return False
    if tab.workspace_id != workspace_id:
        return False
    if item.workspace_id and item.workspace_id != workspace_id:
        return False

    for pane in snapshot.panes:
        if pane.pane_id == item.pane_id:
            if pane.workspace_id and pane.workspace_id != workspace_id:
                return False
            if pane.tab_id and pane.tab_id != item.tab_id:
                return False
            break

    return True


def workspace_roles(
    inventory: SessionInventory,
    workspace_id: str,
    *,
    known_master_panes: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Pure, snapshot-derived role computation for a workspace.

    Returns (), ('Master',), ('Ergo',), or ('Master', 'Ergo') in that exact order.
    """
    snapshot = inventory.snapshot
    if snapshot is None:
        return ()

    if not any(ws.workspace_id == workspace_id for ws in snapshot.workspaces):
        return ()

    agents = workspace_agents(inventory, workspace_id)
    if not agents:
        return ()

    tab_by_id = {tab.tab_id: tab for tab in snapshot.tabs}

    has_master = False
    has_ergo = False

    for item in agents:
        if not _is_agent_bearing_pane(item, snapshot):
            continue

        if item.pane_id in known_master_panes:
            has_master = True

        normalized_name = _normalize_name(item.name)
        if normalized_name == CANONICAL_MASTER_NAME:
            has_master = True
        elif normalized_name == CANONICAL_ERGO_NAME:
            has_ergo = True
        elif not normalized_name:
            # Unnamed agent: check canonical owning-tab label if verified in same workspace
            tab = tab_by_id.get(item.tab_id) if item.tab_id else None
            if _is_verified_owning_tab(item, tab, workspace_id, snapshot):
                tab_label = _normalize_name(tab.label if tab else None)
                if tab_label == CANONICAL_MASTER_NAME:
                    has_master = True
                elif tab_label == CANONICAL_ERGO_NAME:
                    has_ergo = True

    roles: list[str] = []
    if has_master:
        roles.append("Master")
    if has_ergo:
        roles.append("Ergo")
    return tuple(roles)


def known_master_panes_for_workspace(
    masters: Optional[dict[str, MasterSession]],
    socket_path: Path | str,
    workspace_id: str,
) -> frozenset[str]:
    """Collect pane IDs of custom-named Masters matching socket and workspace."""
    if not masters:
        return frozenset()

    panes: set[str] = set()
    target_socket_str = str(socket_path)
    target_path = Path(socket_path)

    for master in masters.values():
        if not master.metadata:
            continue
        meta_socket = master.metadata.get("socket_path")
        if not meta_socket or not str(meta_socket).strip():
            continue
        meta_str = str(meta_socket)
        if meta_str != target_socket_str and Path(meta_str) != target_path:
            continue
        if master.workspace_id != workspace_id:
            continue
        if master.pane_id:
            panes.add(master.pane_id)

    return frozenset(panes)

