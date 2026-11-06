"""Small Protocol 22-shaped records for offline controller/client fakes."""


def snapshot_identity(snapshot, pane_id):
    pane = next(p for p in snapshot.panes if p.pane_id == pane_id)
    agent = next((a for a in snapshot.agents if a.pane_id == pane_id), None)
    tab_id = pane.tab_id or (agent.tab_id if agent else None)
    tab = next((t for t in snapshot.tabs if t.tab_id == tab_id), None)
    workspace_id = pane.workspace_id or (agent.workspace_id if agent else None)
    workspace_id = workspace_id or (tab.workspace_id if tab else None)
    return workspace_id, tab_id, pane.terminal_id or "unreported-terminal"


def pane_read_record(pane_id, workspace_id, tab_id, text):
    return {
        "pane_id": pane_id,
        "workspace_id": workspace_id,
        "tab_id": tab_id,
        "source": "recent",
        "format": "ansi",
        "text": text,
        "revision": 1,
        "truncated": False,
    }


def agent_prompted(pane_id="p-1", workspace_id="ws-1", tab_id="tab-1", terminal_id="term-1"):
    return {
        "type": "agent_prompted",
        "agent": {
            "terminal_id": terminal_id,
            "agent_status": "idle",
            "workspace_id": workspace_id,
            "tab_id": tab_id,
            "pane_id": pane_id,
            "focused": False,
            "revision": 1,
        },
    }
