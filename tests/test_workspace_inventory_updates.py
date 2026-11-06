"""Inventory reconciliation must distinguish servers and update role badges."""

from pathlib import Path

import pytest

from herdr_master.core.models import HarnessKind, MasterSession
from herdr_master.herdr.discovery import SessionInventory
from herdr_master.herdr.snapshot import AgentSnapshot, SessionSnapshot
from aiheaddev.ui.tui.app import HerdrMasterApp
from aiheaddev.ui.tui.widgets.workspaces import WorkspaceBrowser


def session(name: str, *, agent_name: str | None = None, second_agent_name: str | None = None):
    agents = [
        {
            "pane_id": "p1",
            "workspace_id": "w1",
            "agent": "pi",
            "name": agent_name,
            "agent_status": "working",
        }
    ]
    if second_agent_name:
        agents.append(
            {
                "pane_id": "p2",
                "workspace_id": "w1",
                "agent": "agy",
                "name": second_agent_name,
                "agent_status": "working",
            }
        )
    return SessionInventory(
        session_name=name,
        socket_path=Path(f"/{name}/herdr.sock"),
        snapshot=SessionSnapshot(
            protocol=22,
            workspaces=[{"workspace_id": "w1", "label": name}],
            agents=agents,
        ),
    )


@pytest.mark.asyncio
async def test_same_ids_across_sessions_and_removed_panes():
    app = HerdrMasterApp(demo=True)
    first = session("one", agent_name="master-coordinator")
    second = session("two", agent_name="ergo-orchestrator")
    app.app_state.live_sessions = [first, second]
    async with app.run_test() as pilot:
        await pilot.pause()
        browser = app.query_one(WorkspaceBrowser)
        nodes = browser._tree.root.children
        assert len(nodes) == 2
        node0, node1 = nodes[0], nodes[1]
        assert node0.children[0].data.socket_path != node1.children[0].data.socket_path

        # Verify initial role badges are socket-scoped
        assert "[Master]" in node0.label.plain
        assert "[Ergo]" not in node0.label.plain
        assert "[Ergo]" in node1.label.plain
        assert "[Master]" not in node1.label.plain

        node0.expand()
        browser._tree.cursor_line = 0
        cursor_before = browser._tree.cursor_node

        # Remove panes/workspaces
        first.snapshot.agents.clear()
        second.snapshot.workspaces.clear()
        second.snapshot.agents.clear()
        browser.update_state(app.app_state)

        # In-place reconciliation preserves node instance, expansion, and cursor
        assert browser._tree.root.children[0] is node0
        assert node0.is_expanded
        assert browser._tree.cursor_node is cursor_before
        assert not node0.children
        assert len(browser._pane_nodes) == 0
        assert len(browser._tree.root.children) == 2

        # Role badge removed when agent disappeared
        assert "[Master]" not in node0.label.plain
        assert "empty" in browser._tree.root.children[1].label.plain


@pytest.mark.asyncio
async def test_disconnected_snapshot_is_visibly_stale_then_recovers():
    app = HerdrMasterApp(demo=True)
    first = session("one", agent_name="master-coordinator", second_agent_name="ergo-orchestrator")
    app.app_state.live_sessions = [first]
    async with app.run_test() as pilot:
        await pilot.pause()
        browser = app.query_one(WorkspaceBrowser)
        node = browser._tree.root.children[0]
        node.expand()

        # Initial fresh state
        assert "● running [Master] [Ergo]" in node.label.plain
        spans_fresh = {node.label.plain[s.start : s.end]: s.style for s in node.label.spans}
        assert spans_fresh.get(" [Master]") == "bold cyan"
        assert spans_fresh.get(" [Ergo]") == "bold magenta"

        # Disconnect -> visibly offline/stale, role badges become dimmed
        first.error = "Connection refused"
        browser.update_state(app.app_state)

        # Node identity and expansion preserved
        assert browser._tree.root.children[0] is node
        assert node.is_expanded
        assert node.label.plain.startswith("× offline [Master] [Ergo]")
        assert node.label.plain.endswith("· stale")
        assert "stale" in node.children[0].label.plain
        assert "Connection refused" in browser._summary.tooltip

        spans_offline = {node.label.plain[s.start : s.end]: s.style for s in node.label.spans}
        assert spans_offline.get(" [Master]") == "dim"
        assert spans_offline.get(" [Ergo]") == "dim"

        # Recover -> fresh role colors restored
        first.error = None
        browser.update_state(app.app_state)

        assert browser._tree.root.children[0] is node
        assert node.is_expanded
        assert node.label.plain.startswith("● running [Master] [Ergo]")
        assert "running" in node.children[0].label.plain
        assert browser._summary.tooltip is None

        spans_recovered = {node.label.plain[s.start : s.end]: s.style for s in node.label.spans}
        assert spans_recovered.get(" [Master]") == "bold cyan"
        assert spans_recovered.get(" [Ergo]") == "bold magenta"


@pytest.mark.asyncio
async def test_reconciliation_individual_role_disappearance_and_custom_master():
    app = HerdrMasterApp(demo=True)
    first = session("one", agent_name="master-coordinator", second_agent_name="ergo-orchestrator")
    app.app_state.live_sessions = [first]

    async with app.run_test() as pilot:
        await pilot.pause()
        browser = app.query_one(WorkspaceBrowser)
        node = browser._tree.root.children[0]
        node.expand()
        assert "● running [Master] [Ergo]" in node.label.plain

        # Drop only the Ergo agent (p2)
        first.snapshot.agents = [a for a in first.snapshot.agents if a.pane_id != "p2"]
        browser.update_state(app.app_state)

        # In-place node identity preserved, only Ergo badge removed
        assert browser._tree.root.children[0] is node
        assert node.is_expanded
        assert "[Master]" in node.label.plain
        assert "[Ergo]" not in node.label.plain

        # Now rename remaining master agent to custom name (no automatic role inference)
        first.snapshot.agents[0].name = "custom-agent-name"
        browser.update_state(app.app_state)
        assert "[Master]" not in node.label.plain

        # Dynamically register custom MasterSession with exact socket and workspace match
        app.app_state.masters = {
            "m1": MasterSession(
                session_id="master-custom",
                workspace_id="w1",
                pane_id="p1",
                harness=HarnessKind.PI,
                metadata={"socket_path": str(first.socket_path)},
            )
        }
        browser.update_state(app.app_state)
        # Custom master badge now established
        assert "[Master]" in node.label.plain

        # Unregister master
        app.app_state.masters.clear()
        browser.update_state(app.app_state)
        assert "[Master]" not in node.label.plain


@pytest.mark.asyncio
async def test_reconciliation_preserves_nonzero_scroll_through_badge_changes_and_stale_recovery():
    """Verify nonzero scroll offset is retained through badge changes, stale and recovery."""
    app = HerdrMasterApp(demo=True)
    workspaces = [{"workspace_id": f"w{i}", "label": f"project-{i}"} for i in range(35)]
    agents = [
        AgentSnapshot(
            pane_id="p-master",
            workspace_id="w15",
            name="master-coordinator",
            agent="pi",
            agent_status="working",
        )
    ]
    inv = SessionInventory(
        session_name="scroll-sess",
        socket_path=Path("/scroll/herdr.sock"),
        snapshot=SessionSnapshot(
            protocol=22,
            workspaces=workspaces,
            agents=agents,
        ),
    )
    app.app_state.live_sessions = [inv]

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        browser = app.query_one(WorkspaceBrowser)
        tree = browser._tree
        assert tree.max_scroll_y >= 10

        # Scroll down to a nonzero offset
        tree.scroll_to(y=10, animate=False)
        await pilot.pause()
        assert tree.scroll_y == 10

        node15 = browser._workspace_nodes[(str(inv.socket_path), "w15")]
        assert "[Master]" in node15.label.plain
        assert "[Ergo]" not in node15.label.plain
        assert tree.scroll_y == 10
        # 1. Badge change: add Ergo agent
        inv.snapshot.agents.append(
            AgentSnapshot(
                pane_id="p-ergo",
                workspace_id="w15",
                name="ergo-orchestrator",
                agent="agy",
                agent_status="working",
            )
        )
        browser.update_state(app.app_state)
        assert "[Master]" in node15.label.plain
        assert "[Ergo]" in node15.label.plain
        assert browser._workspace_nodes[(str(inv.socket_path), "w15")] is node15
        assert tree.scroll_y == 10

        # 2. Offline / stale transition
        inv.error = "Connection lost"
        browser.update_state(app.app_state)
        assert node15.label.plain.startswith("× offline [Master] [Ergo]")
        assert node15.label.plain.endswith("· stale")
        assert tree.scroll_y == 10

        # 3. Recovery transition
        inv.error = None
        browser.update_state(app.app_state)
        assert node15.label.plain.startswith("● running [Master] [Ergo]")
        assert tree.scroll_y == 10

        # 4. Badge change: remove Ergo agent
        inv.snapshot.agents = [a for a in inv.snapshot.agents if a.pane_id != "p-ergo"]
        browser.update_state(app.app_state)
        assert "[Master]" in node15.label.plain
        assert "[Ergo]" not in node15.label.plain
        assert tree.scroll_y == 10
