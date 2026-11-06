"""Workspace inventory, compact interactions, and exact-socket navigation."""

import asyncio
import socket
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch

import pytest

from herdr_master.herdr.client import HerdrClient
from herdr_master.herdr.discovery import SessionInventory, discover_clients
from herdr_master.herdr.snapshot import SessionSnapshot
from aiheaddev.ui.tui.app import HerdrMasterApp
from aiheaddev.ui.tui.widgets.workspaces import (
    WorkspaceBrowser,
    WorkspaceTarget,
    workspace_agents,
)


def inventory(path=Path("/session/herdr.sock"), name="one"):
    return SessionInventory(
        session_name=name,
        socket_path=path,
        snapshot=SessionSnapshot(
            protocol=22,
            focused_workspace_id="w1",
            workspaces=[{"workspace_id": "w1", "label": "Project", "agent_status": "working"}],
            tabs=[{"tab_id": "t1", "workspace_id": "w1", "label": "Implement"}],
            panes=[
                {"pane_id": "p1", "tab_id": "t1", "agent": "pi", "agent_status": "working"},
                {"pane_id": "p2", "tab_id": "t1"},
            ],
            agents=[{"pane_id": "p1", "agent": "pi", "name": "Builder"}],
        ),
    )


def test_discovery_uses_only_existing_sockets_and_preserves_primary(tmp_path):
    paths = [tmp_path / "herdr.sock", tmp_path / "sessions" / "other" / "herdr.sock"]
    sockets = []
    try:
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            sock = socket.socket(socket.AF_UNIX)
            sock.bind(str(path))
            sockets.append(sock)
        fake = tmp_path / "sessions" / "not-a-socket" / "herdr.sock"
        fake.parent.mkdir()
        fake.write_text("not a socket")
        primary = HerdrClient(session="current", socket_path=paths[0])
        clients = discover_clients(primary, tmp_path)
        assert clients[0] is primary
        assert [(client.session, client.socket_path) for client in clients] == [
            ("current", paths[0]),
            ("other", paths[1]),
        ]
    finally:
        for sock in sockets:
            sock.close()


def test_inventory_merges_panes_agents_and_tab_ownership():
    agents = workspace_agents(inventory(), "w1")
    assert len(agents) == 2
    assert agents[0].name == "Builder"
    assert agents[0].agent_status == "working"
    assert agents[0].tab_id == "t1"
    assert agents[1].agent_status == "terminal"
    assert not workspace_agents(inventory(), "absent")


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (110, 32)])
async def test_workspace_expansion_click_navigation_and_refresh_preserve_cursor(size):
    app = HerdrMasterApp(demo=True)
    app.demo = False
    app.app_state.inventory_demo = False
    sess = inventory()
    app.app_state.live_sessions = [sess]
    app._refresh_live_snapshot = AsyncMock()

    client = Mock(
        focus_workspace=AsyncMock(),
        focus_tab=AsyncMock(),
        focus_pane=AsyncMock(),
    )
    context = Mock(is_inside_herdr=True, socket_path=sess.socket_path)

    with (
        patch("aiheaddev.ui.tui.app.HerdrClient", return_value=client),
        patch("aiheaddev.ui.tui.app.resolve_herdr_context", return_value=context),
    ):
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            browser = app.query_one(WorkspaceBrowser)
            tree = browser._tree
            workspace = tree.root.children[0]
            assert not workspace.is_expanded
            assert "running" in workspace.label.plain

            # Clicking on row expands it without navigating
            await pilot.click("#workspace-tree", offset=(12, 0))
            await pilot.pause()
            assert workspace.is_expanded
            client.focus_workspace.assert_not_called()

            # Select workspace row and press 'o' -> opens workspace only
            await pilot.press("o")
            await pilot.pause()
            client.focus_workspace.assert_awaited_once_with("w1")
            client.focus_tab.assert_not_called()
            client.focus_pane.assert_not_called()
            client.focus_workspace.reset_mock()

            agent = workspace.children[0]
            assert "Builder" in agent.label.plain

            # Clicking agent leaf selects it without navigating
            await pilot.click("#workspace-tree", offset=(12, 1))
            await pilot.pause()
            assert browser._selected_target.pane_id == "p1"
            client.focus_pane.assert_not_called()

            # Explicit Open via button for agent -> workspace -> tab -> pane
            await pilot.click("#btn-open-herdr")
            await pilot.pause()
            assert client.focus_workspace.await_args == call("w1")
            assert client.focus_tab.await_args == call("t1")
            assert client.focus_pane.await_args == call("p1")
            client.focus_workspace.reset_mock()
            client.focus_tab.reset_mock()
            client.focus_pane.reset_mock()

            # Select terminal shell pane (p2) and open via shortcut 'o'
            terminal_pane = workspace.children[1]
            tree.select_node(terminal_pane)
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            assert client.focus_workspace.await_args == call("w1")
            assert client.focus_tab.await_args == call("t1")
            assert client.focus_pane.await_args == call("p2")

            # Refresh preserves expansion, cursor, and node identity
            browser.update_state(app.app_state)
            assert tree.root.children[0] is workspace
            assert workspace.is_expanded
            assert tree.cursor_node is terminal_pane
            assert tree.region.bottom <= size[1] - 1
            # In no-pane mode tree height is >=10; with p2 selected at 80x24 it enters
            # stacked live mode (>=4)
            assert tree.size.height >= (4 if size == (80, 24) else 10)
            assert browser.display


@pytest.mark.asyncio
async def test_refresh_surfaces_offline_sessions_and_keeps_last_snapshot():
    app = HerdrMasterApp(demo=True)
    app.demo = False
    app._update_all_widgets = Mock()
    first, second = inventory(), inventory(Path("/other/herdr.sock"), "two")
    app.app_state.live_sessions = [first, second]
    primary = HerdrClient(session=first.session_name, socket_path=first.socket_path)
    other = HerdrClient(session=second.session_name, socket_path=second.socket_path)
    primary.get_snapshot = AsyncMock(return_value=first.snapshot)
    other.get_snapshot = AsyncMock(side_effect=ConnectionError("Session disappeared"))
    app.client = primary
    with patch("aiheaddev.ui.tui.app.discover_clients", return_value=[primary, other]):
        await app._refresh_live_snapshot()
    assert app.app_state.socket_connected
    assert app.app_state.live_sessions[1].error == "Session disappeared"
    assert app.app_state.live_sessions[1].snapshot == second.snapshot
    assert app.app_state.workspace_label == "Project"
    assert not app._inventory_pending


@pytest.mark.asyncio
async def test_refresh_does_not_overlap():
    app = HerdrMasterApp(demo=True)
    app.demo = False
    app.client = Mock()
    app._inventory_pending = True
    with patch("aiheaddev.ui.tui.app.discover_clients") as discover:
        await app._refresh_live_snapshot()
        discover.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inside,same_socket,attach", [(True, True, False), (True, False, True), (False, False, True)]
)
async def test_agent_navigation_uses_exact_socket_and_focuses_pane(inside, same_socket, attach):
    app = HerdrMasterApp(demo=True)
    app.demo = False
    session = inventory()
    app.app_state.live_sessions = [session]
    app.notify = Mock()
    app.exit = Mock()
    client = Mock()
    client.focus_workspace = AsyncMock()
    client.focus_tab = AsyncMock()
    client.focus_pane = AsyncMock()
    context = Mock(
        is_inside_herdr=inside,
        socket_path=session.socket_path if same_socket else Path("/elsewhere"),
    )
    target_context = Mock(
        socket_path=session.socket_path,
    )

    def mock_resolve_herdr_context(explicit_session=None):
        if explicit_session is not None:
            return target_context
        return context

    with (
        patch("aiheaddev.ui.tui.app.HerdrClient", return_value=client) as factory,
        patch(
            "aiheaddev.ui.tui.app.resolve_herdr_context", side_effect=mock_resolve_herdr_context
        ),
    ):
        await app._open_in_herdr(WorkspaceTarget("one", session.socket_path, "w1", "t1", "p1"))
    factory.assert_called_once_with(session="one", socket_path=session.socket_path, timeout=2.0)
    assert client.mock_calls == [
        call.focus_workspace("w1"),
        call.focus_tab("t1"),
        call.focus_pane("p1"),
    ]
    if attach:
        app.exit.assert_called_once_with(result={"action": "attach", "session": "one"})
    else:
        app.exit.assert_not_called()


@pytest.mark.asyncio
async def test_workspace_open_focuses_only_workspace():
    app = HerdrMasterApp(demo=True)
    app.demo = False
    session = inventory()
    app.app_state.live_sessions = [session]
    app.notify = Mock()
    app.exit = Mock()
    client = Mock()
    client.focus_workspace = AsyncMock()
    client.focus_tab = AsyncMock()
    client.focus_pane = AsyncMock()
    context = Mock(
        is_inside_herdr=True,
        socket_path=session.socket_path,
    )
    with (
        patch("aiheaddev.ui.tui.app.HerdrClient", return_value=client) as factory,
        patch("aiheaddev.ui.tui.app.resolve_herdr_context", return_value=context),
    ):
        await app._open_in_herdr(WorkspaceTarget("one", session.socket_path, "w1", None, None))
    factory.assert_called_once_with(session="one", socket_path=session.socket_path, timeout=2.0)
    client.focus_workspace.assert_awaited_once_with("w1")
    client.focus_tab.assert_not_called()
    client.focus_pane.assert_not_called()
    app.exit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    ["offline", "closed", "demo", "rpc-error", "empty-ws", "unsafe-socket", "absent-workspace"],
)
async def test_navigation_safely_handles_unavailable_targets(scenario):
    app = HerdrMasterApp(demo=True)
    app.demo = scenario == "demo"
    session = inventory()
    if scenario == "offline":
        session.error = "offline"
    elif scenario == "empty-ws":
        session.snapshot.panes.clear()
        session.snapshot.agents.clear()
    app.app_state.live_sessions = [session]
    app.notify = Mock()
    app.exit = Mock()
    client = Mock(focus_workspace=AsyncMock(side_effect=RuntimeError("pane gone")))
    context = Mock(
        is_inside_herdr=False,
        socket_path=None,
    )
    resolved_target = Mock(
        socket_path=Path("/different/sock") if scenario == "unsafe-socket" else session.socket_path,
    )

    def mock_resolve_herdr_context(explicit_session=None):
        if explicit_session is not None:
            return resolved_target
        return context

    ws_id = "w-absent" if scenario == "absent-workspace" else "w1"
    pane_id = "absent" if scenario == "closed" else "p1"

    with (
        patch("aiheaddev.ui.tui.app.HerdrClient", return_value=client) as factory,
        patch(
            "aiheaddev.ui.tui.app.resolve_herdr_context", side_effect=mock_resolve_herdr_context
        ),
    ):
        await app._open_in_herdr(
            WorkspaceTarget(
                "one", session.socket_path, ws_id, "t1", pane_id
            )
        )
    app.exit.assert_not_called()
    app.notify.assert_called_once()
    assert not app._navigation_pending
    if scenario != "rpc-error":
        factory.assert_not_called()


@pytest.mark.asyncio
async def test_typing_o_in_modal_does_not_navigate(tmp_path: Path):
    from aiheaddev.ui.tui.modals.launch_master import LaunchMasterModal

    app = HerdrMasterApp(demo=False, project_path=tmp_path)
    session = inventory()
    app.app_state.live_sessions = [session]
    app._open_in_herdr = AsyncMock()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("n")
        assert isinstance(app.screen, LaunchMasterModal)
        # Type 'o' into input field inside the modal
        await pilot.press("o")
        await pilot.pause()
        # Open in Herdr must not be triggered while modal is active
        app._open_in_herdr.assert_not_called()
        assert isinstance(app.screen, LaunchMasterModal)


@pytest.mark.asyncio
async def test_focus_pane_protocol_contract():
    client = HerdrClient(socket_path="/unused/herdr.sock")
    client._rpc = AsyncMock(return_value={"result": {"ok": True}})
    assert await client.focus_pane("p1") == {"ok": True}
    client._rpc.assert_awaited_once_with("pane.focus", {"pane_id": "p1"})


@pytest.mark.asyncio
async def test_refresh_bounds_unresponsive_socket():
    app = HerdrMasterApp(demo=True)
    app.demo = False
    app._update_all_widgets = Mock()
    app.client = HerdrClient(session="one", socket_path="/session/herdr.sock")

    async def slow_snapshot():
        await asyncio.sleep(10)

    app.client.get_snapshot = slow_snapshot
    with patch("aiheaddev.ui.tui.app.discover_clients", return_value=[app.client]):
        await asyncio.wait_for(app._refresh_live_snapshot(), timeout=3)
    assert app.app_state.live_sessions[0].error == "TimeoutError"
    assert not app.app_state.socket_connected


@pytest.mark.asyncio
async def test_selected_pane_removal_invalidates_open_until_deliberate_reselection():
    app = HerdrMasterApp(demo=True)
    app.demo = False
    app.app_state.inventory_demo = False
    app._refresh_live_snapshot = AsyncMock()
    app.exit = Mock()
    app.notify = Mock()

    sess = inventory()
    app.app_state.live_sessions = [sess]
    client = Mock(focus_workspace=AsyncMock(), focus_tab=AsyncMock(), focus_pane=AsyncMock())
    context = Mock(is_inside_herdr=True, socket_path=sess.socket_path)

    with (
        patch("aiheaddev.ui.tui.app.HerdrClient", return_value=client),
        patch("aiheaddev.ui.tui.app.resolve_herdr_context", return_value=context),
    ):
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            browser = app.query_one(WorkspaceBrowser)
            tree = browser._tree
            ws_node = tree.root.children[0]
            ws_node.expand()
            await pilot.pause()
            assert len(ws_node.children) == 2

            # Select last pane (p2)
            p2_node = ws_node.children[1]
            tree.select_node(p2_node)
            await pilot.pause()
            assert browser._selected_target is not None
            assert browser._selected_target.pane_id == "p2"
            assert not browser._selected_target_disappeared
            btn = browser.query_one("#btn-open-herdr")
            assert not btn.disabled

            # Remove p2 from snapshot
            sess.snapshot.panes = [p for p in sess.snapshot.panes if p.pane_id != "p2"]
            sess.snapshot.agents = [a for a in sess.snapshot.agents if a.pane_id != "p2"]
            browser.update_state(app.app_state)
            await pilot.pause()

            # Selection invalidated; cursor fallback to p1 does not authorize it
            assert browser._selected_target is None
            assert browser._selected_target_disappeared
            assert btn.disabled

            # Button click and shortcut 'o' must not trigger navigation
            await pilot.click("#btn-open-herdr")
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            client.focus_workspace.assert_not_called()
            client.focus_tab.assert_not_called()
            client.focus_pane.assert_not_called()
            app.exit.assert_not_called()

            # Repeated refresh does NOT resurrect authorization
            browser.update_state(app.app_state)
            await pilot.pause()
            assert browser._selected_target is None
            assert browser._selected_target_disappeared
            assert btn.disabled
            await pilot.press("o")
            await pilot.pause()
            client.focus_workspace.assert_not_called()

            # Shift+Tab to Settings button (unrelated control input)
            await pilot.press("shift+tab")
            await pilot.pause()
            assert app.screen.focused.id == "btn-settings"

            # Navigation keys on Settings must NOT clear the latch
            for key in ("left", "down", "up", "right"):
                await pilot.press(key)
                await pilot.pause()
                assert browser._selected_target is None
                assert browser._selected_target_disappeared
                assert btn.disabled

            # Shortcut 'o' while Settings focused must not trigger navigation
            await pilot.press("o")
            await pilot.pause()
            client.focus_workspace.assert_not_called()
            app.exit.assert_not_called()

            # Tab back to tree: tree gains focus, but latch still holds until deliberate input
            await pilot.press("tab")
            await pilot.pause()
            assert tree.has_focus
            assert browser._selected_target is None
            assert browser._selected_target_disappeared
            assert btn.disabled

            # Deliberate keyboard arrow reselection restores open:
            # Up arrow moves to workspace node
            await pilot.press("up")
            await pilot.pause()
            assert browser._selected_target is not None
            assert browser._selected_target.workspace_id == "w1"
            assert browser._selected_target.pane_id is None
            assert not browser._selected_target_disappeared
            assert not btn.disabled

            # Down arrow moves back to p1 node
            await pilot.press("down")
            await pilot.pause()
            assert browser._selected_target is not None
            assert browser._selected_target.pane_id == "p1"
            assert not browser._selected_target_disappeared
            assert not btn.disabled

            await pilot.press("o")
            await pilot.pause()
            assert client.focus_workspace.await_args == call("w1")
            assert client.focus_tab.await_args == call("t1")
            assert client.focus_pane.await_args == call("p1")


@pytest.mark.asyncio
async def test_selected_workspace_removal_invalidates_open_until_deliberate_reselection():
    app = HerdrMasterApp(demo=True)
    app.demo = False
    app._refresh_live_snapshot = AsyncMock()
    app.app_state.inventory_demo = False
    app.exit = Mock()
    app.notify = Mock()

    sess = SessionInventory(
        session_name="one",
        socket_path=Path("/session/herdr.sock"),
        snapshot=SessionSnapshot(
            protocol=22,
            workspaces=[
                {"workspace_id": "w1", "label": "Project 1", "agent_status": "idle"},
                {"workspace_id": "w2", "label": "Project 2", "agent_status": "idle"},
            ],
            tabs=[
                {"tab_id": "t1", "workspace_id": "w1", "label": "Tab 1"},
                {"tab_id": "t2", "workspace_id": "w2", "label": "Tab 2"},
            ],
            panes=[
                {"pane_id": "p1", "tab_id": "t1"},
                {"pane_id": "p2", "tab_id": "t2"},
            ],
            agents=[],
        ),
    )
    app.app_state.live_sessions = [sess]
    client = Mock(focus_workspace=AsyncMock(), focus_tab=AsyncMock(), focus_pane=AsyncMock())
    context = Mock(is_inside_herdr=True, socket_path=sess.socket_path)

    with (
        patch("aiheaddev.ui.tui.app.HerdrClient", return_value=client),
        patch("aiheaddev.ui.tui.app.resolve_herdr_context", return_value=context),
    ):
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            browser = app.query_one(WorkspaceBrowser)
            tree = browser._tree
            assert len(tree.root.children) == 2

            # Select last workspace (w2)
            w2_node = tree.root.children[1]
            tree.select_node(w2_node)
            await pilot.pause()
            assert browser._selected_target is not None
            assert browser._selected_target.workspace_id == "w2"
            assert browser._selected_target.pane_id is None
            btn = browser.query_one("#btn-open-herdr")
            assert not btn.disabled

            # Remove w2
            sess.snapshot.workspaces = [
                w for w in sess.snapshot.workspaces if w.workspace_id != "w2"
            ]
            sess.snapshot.tabs = [t for t in sess.snapshot.tabs if t.workspace_id != "w2"]
            sess.snapshot.panes = [p for p in sess.snapshot.panes if p.pane_id != "p2"]
            browser.update_state(app.app_state)
            await pilot.pause()

            # Latch holds across queued UI events
            assert browser._selected_target is None
            assert browser._selected_target_disappeared
            assert btn.disabled

            # Button click and shortcut 'o' must not trigger navigation
            await pilot.click("#btn-open-herdr")
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            client.focus_workspace.assert_not_called()
            client.focus_tab.assert_not_called()
            client.focus_pane.assert_not_called()
            app.exit.assert_not_called()

            # Repeated refresh does NOT resurrect authorization
            browser.update_state(app.app_state)
            await pilot.pause()
            assert browser._selected_target is None
            assert browser._selected_target_disappeared
            assert btn.disabled
            await pilot.press("o")
            await pilot.pause()
            client.focus_workspace.assert_not_called()

            # Deliberately select surviving w1 via genuine keyboard Up arrow on tree
            assert tree.has_focus
            await pilot.press("up")
            await pilot.pause()
            assert browser._selected_target is not None
            assert browser._selected_target.workspace_id == "w1"
            assert browser._selected_target.pane_id is None
            assert not btn.disabled

            await pilot.press("o")
            await pilot.pause()
            client.focus_workspace.assert_awaited_once_with("w1")
            client.focus_tab.assert_not_called()
            client.focus_pane.assert_not_called()


@pytest.mark.asyncio
async def test_multiple_simultaneous_removals_invalidates_open_until_deliberate_reselection():
    app = HerdrMasterApp(demo=True)
    app.demo = False
    app.app_state.inventory_demo = False
    app._refresh_live_snapshot = AsyncMock()
    app.exit = Mock()
    app.notify = Mock()

    sess = SessionInventory(
        session_name="one",
        socket_path=Path("/session/herdr.sock"),
        snapshot=SessionSnapshot(
            protocol=22,
            workspaces=[{"workspace_id": "w1", "label": "Project"}],
            tabs=[{"tab_id": "t1", "workspace_id": "w1"}],
            panes=[
                {"pane_id": "p1", "tab_id": "t1", "agent": "pi"},
                {"pane_id": "p2", "tab_id": "t1"},
                {"pane_id": "p3", "tab_id": "t1"},
            ],
            agents=[{"pane_id": "p1", "name": "Builder"}],
        ),
    )
    app.app_state.live_sessions = [sess]
    client = Mock(focus_workspace=AsyncMock(), focus_tab=AsyncMock(), focus_pane=AsyncMock())
    context = Mock(is_inside_herdr=True, socket_path=sess.socket_path)

    with (
        patch("aiheaddev.ui.tui.app.HerdrClient", return_value=client),
        patch("aiheaddev.ui.tui.app.resolve_herdr_context", return_value=context),
    ):
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            browser = app.query_one(WorkspaceBrowser)
            tree = browser._tree
            ws_node = tree.root.children[0]
            ws_node.expand()
            await pilot.pause()
            assert len(ws_node.children) == 3

            # Select last pane (p3)
            p3_node = ws_node.children[2]
            tree.select_node(p3_node)
            await pilot.pause()
            assert browser._selected_target.pane_id == "p3"
            assert not browser._selected_target_disappeared
            btn = browser.query_one("#btn-open-herdr")
            assert not btn.disabled

            # Remove p2 and p3 simultaneously
            sess.snapshot.panes = [p for p in sess.snapshot.panes if p.pane_id == "p1"]
            browser.update_state(app.app_state)
            await pilot.pause()

            # Latch holds across queued UI events
            assert browser._selected_target is None
            assert browser._selected_target_disappeared
            assert btn.disabled

            # Try button and 'o'
            await pilot.click("#btn-open-herdr")
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            client.focus_workspace.assert_not_called()
            client.focus_pane.assert_not_called()
            app.exit.assert_not_called()

            # Repeated refresh keeps latch
            browser.update_state(app.app_state)
            await pilot.pause()
            assert browser._selected_target is None
            assert browser._selected_target_disappeared
            assert btn.disabled
            await pilot.press("o")
            await pilot.pause()
            client.focus_workspace.assert_not_called()

            # Deliberately reselect surviving p1 via genuine Enter on tree
            assert tree.has_focus
            await pilot.press("enter")
            await pilot.pause()
            assert browser._selected_target is not None
            assert browser._selected_target.pane_id == "p1"
            assert not browser._selected_target_disappeared
            assert not btn.disabled

            await pilot.press("o")
            await pilot.pause()
            assert client.focus_workspace.await_args == call("w1")
            assert client.focus_tab.await_args == call("t1")
            assert client.focus_pane.await_args == call("p1")


@pytest.mark.asyncio
async def test_unrelated_control_and_boundary_click_recovery_preserves_lost_selection_latch():
    """Verify unrelated control input and clicks preserve latch, while genuine
    boundary arrow, Enter, and tree-row click restore exact navigation targets.
    """
    app = HerdrMasterApp(demo=True)
    app.demo = False
    app.app_state.inventory_demo = False
    sess = inventory()
    app.app_state.live_sessions = [sess]
    app._refresh_live_snapshot = AsyncMock()
    app.exit = Mock()
    app.notify = Mock()

    client = Mock(focus_workspace=AsyncMock(), focus_tab=AsyncMock(), focus_pane=AsyncMock())
    context = Mock(is_inside_herdr=True, socket_path=sess.socket_path)

    with (
        patch("aiheaddev.ui.tui.app.HerdrClient", return_value=client),
        patch("aiheaddev.ui.tui.app.resolve_herdr_context", return_value=context),
    ):
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            browser = app.query_one(WorkspaceBrowser)
            tree = browser._tree
            ws_node = tree.root.children[0]
            ws_node.expand()
            await pilot.pause()

            # Select last pane p2
            p2_node = ws_node.children[1]
            tree.select_node(p2_node)
            await pilot.pause()
            assert browser._selected_target.pane_id == "p2"

            # Remove p2
            sess.snapshot.panes = [p for p in sess.snapshot.panes if p.pane_id != "p2"]
            sess.snapshot.agents = [a for a in sess.snapshot.agents if a.pane_id != "p2"]
            browser.update_state(app.app_state)
            await pilot.pause()
            assert browser._selected_target is None
            assert browser._selected_target_disappeared is True
            cursor_fallback = tree.cursor_node
            assert cursor_fallback.data.pane_id == "p1"
            assert tree.has_focus is True

            btn = browser.query_one("#btn-open-herdr")
            sel = browser.query_one("#open-selection")

            # 1. Negative: actual 'k' on tree must not authorize fallback target
            await pilot.press("k")
            await pilot.pause()
            assert tree.cursor_node is cursor_fallback
            assert browser._selected_target is None
            assert browser._selected_target_disappeared is True
            assert btn.disabled is True
            assert "Selection disappeared · reselect target" in str(sel.render())
            await pilot.press("o")
            await pilot.pause()
            client.focus_workspace.assert_not_called()
            client.focus_pane.assert_not_called()
            app.exit.assert_not_called()

            # 2. Negative: actual 'j' on tree must independently not authorize fallback target
            await pilot.press("j")
            await pilot.pause()
            assert tree.cursor_node is cursor_fallback
            assert browser._selected_target is None
            assert browser._selected_target_disappeared is True
            assert btn.disabled is True
            assert "Selection disappeared · reselect target" in str(sel.render())
            await pilot.press("o")
            await pilot.pause()
            client.focus_workspace.assert_not_called()
            client.focus_pane.assert_not_called()
            app.exit.assert_not_called()

            # 3. Subsequent polling/refresh preserves latch
            browser.update_state(app.app_state)
            await pilot.pause()
            assert tree.cursor_node is cursor_fallback
            assert browser._selected_target is None
            assert browser._selected_target_disappeared is True
            assert btn.disabled is True
            assert "Selection disappeared · reselect target" in str(sel.render())
            await pilot.press("o")
            await pilot.pause()
            client.focus_workspace.assert_not_called()
            client.focus_pane.assert_not_called()
            app.exit.assert_not_called()

            # 4. Negative: Shift+Tab -> Settings focused
            await pilot.press("shift+tab")
            await pilot.pause()
            assert app.screen.focused.id == "btn-settings"

            # Arrow keys on Settings do not clear latch
            for key in ("left", "down", "up", "right"):
                await pilot.press(key)
                await pilot.pause()
                assert browser._selected_target is None
                assert browser._selected_target_disappeared is True

            # Press o while Settings focused
            await pilot.press("o")
            await pilot.pause()
            client.focus_workspace.assert_not_called()
            client.focus_pane.assert_not_called()

            # Click outside tree does not clear latch
            await pilot.click("#open-strip")
            await pilot.pause()
            assert browser._selected_target is None
            assert browser._selected_target_disappeared is True

            # Tab back to tree: gains focus, but latch remains until deliberate input
            await pilot.press("tab")
            await pilot.pause()
            assert tree.has_focus is True
            assert browser._selected_target is None
            assert browser._selected_target_disappeared is True

            # 2. Positive: Boundary arrow (down on last row) restores selection of p1
            await pilot.press("down")
            await pilot.pause()
            assert browser._selected_target is not None
            assert browser._selected_target.pane_id == "p1"
            assert browser._selected_target_disappeared is False

            # Press o -> opens p1
            await pilot.press("o")
            await pilot.pause()
            assert client.focus_workspace.await_args == call("w1")
            assert client.focus_tab.await_args == call("t1")
            assert client.focus_pane.await_args == call("p1")
            client.focus_workspace.reset_mock()
            client.focus_tab.reset_mock()
            client.focus_pane.reset_mock()

            # 3. Positive: Enter recovery
            fresh = inventory()
            sess.snapshot = fresh.snapshot
            browser.update_state(app.app_state)
            await pilot.pause()
            tree.select_node(ws_node.children[1])
            await pilot.pause()
            assert browser._selected_target.pane_id == "p2"

            sess.snapshot.panes = [p for p in sess.snapshot.panes if p.pane_id != "p2"]
            browser.update_state(app.app_state)
            await pilot.pause()
            assert browser._selected_target is None
            assert browser._selected_target_disappeared is True

            # Press Enter on tree -> deliberately selects p1
            await pilot.press("enter")
            await pilot.pause()
            assert browser._selected_target is not None
            assert browser._selected_target.pane_id == "p1"
            assert browser._selected_target_disappeared is False

            await pilot.press("o")
            await pilot.pause()
            assert client.focus_workspace.await_args == call("w1")
            assert client.focus_tab.await_args == call("t1")
            assert client.focus_pane.await_args == call("p1")
            client.focus_workspace.reset_mock()
            client.focus_tab.reset_mock()
            client.focus_pane.reset_mock()

            # 4. Positive: Click row recovery
            fresh2 = inventory()
            sess.snapshot = fresh2.snapshot
            browser.update_state(app.app_state)
            await pilot.pause()
            tree.select_node(ws_node.children[1])
            await pilot.pause()
            assert browser._selected_target.pane_id == "p2"

            sess.snapshot.panes = [p for p in sess.snapshot.panes if p.pane_id != "p2"]
            browser.update_state(app.app_state)
            await pilot.pause()
            assert browser._selected_target is None
            assert browser._selected_target_disappeared is True

            # Click on row 1 (p1)
            await pilot.click("#workspace-tree", offset=(12, 1))
            await pilot.pause()
            assert browser._selected_target is not None
            assert browser._selected_target.pane_id == "p1"
            assert browser._selected_target_disappeared is False

            await pilot.press("o")
            await pilot.pause()
            assert client.focus_workspace.await_args == call("w1")
            assert client.focus_tab.await_args == call("t1")
            assert client.focus_pane.await_args == call("p1")


@pytest.mark.asyncio
async def test_pane_navigation_rejects_foreign_workspace_tab():
    app = HerdrMasterApp(demo=True)
    app.demo = False
    sess = SessionInventory(
        session_name="one",
        socket_path=Path("/session/herdr.sock"),
        snapshot=SessionSnapshot(
            protocol=22,
            workspaces=[
                {"workspace_id": "w1", "label": "Project 1"},
                {"workspace_id": "w2", "label": "Project 2"},
            ],
            tabs=[
                {"tab_id": "t-foreign", "workspace_id": "w2", "label": "Foreign Tab"},
            ],
            panes=[
                {"pane_id": "p1", "workspace_id": "w1", "tab_id": "t-foreign"},
            ],
            agents=[],
        ),
    )
    app.app_state.live_sessions = [sess]
    app.notify = Mock()
    app.exit = Mock()
    client = Mock(
        focus_workspace=AsyncMock(),
        focus_tab=AsyncMock(),
        focus_pane=AsyncMock(),
    )
    with patch("aiheaddev.ui.tui.app.HerdrClient", return_value=client):
        await app._open_in_herdr(
            WorkspaceTarget("one", Path("/session/herdr.sock"), "w1", "t-foreign", "p1")
        )
    client.focus_workspace.assert_not_called()
    client.focus_tab.assert_not_called()
    client.focus_pane.assert_not_called()
    app.exit.assert_not_called()
    app.notify.assert_called_once()
    assert "belonging to another workspace" in app.notify.call_args[0][0]


@pytest.mark.asyncio
async def test_pane_navigation_rejects_missing_referenced_tab():
    app = HerdrMasterApp(demo=True)
    app.demo = False
    sess = SessionInventory(
        session_name="one",
        socket_path=Path("/session/herdr.sock"),
        snapshot=SessionSnapshot(
            protocol=22,
            workspaces=[{"workspace_id": "w1", "label": "Project 1"}],
            tabs=[],
            panes=[{"pane_id": "p1", "workspace_id": "w1", "tab_id": "t-missing"}],
            agents=[],
        ),
    )
    app.app_state.live_sessions = [sess]
    app.notify = Mock()
    app.exit = Mock()
    client = Mock(
        focus_workspace=AsyncMock(),
        focus_tab=AsyncMock(),
        focus_pane=AsyncMock(),
    )
    with patch("aiheaddev.ui.tui.app.HerdrClient", return_value=client):
        await app._open_in_herdr(
            WorkspaceTarget("one", Path("/session/herdr.sock"), "w1", "t-missing", "p1")
        )
    client.focus_workspace.assert_not_called()
    client.focus_tab.assert_not_called()
    client.focus_pane.assert_not_called()
    app.exit.assert_not_called()
    app.notify.assert_called_once()
    assert "missing tab" in app.notify.call_args[0][0]


@pytest.mark.asyncio
async def test_pane_navigation_uses_live_tab_when_cached_target_is_stale():
    app = HerdrMasterApp(demo=True)
    app.demo = False
    sess = SessionInventory(
        session_name="one",
        socket_path=Path("/session/herdr.sock"),
        snapshot=SessionSnapshot(
            protocol=22,
            workspaces=[{"workspace_id": "w1", "label": "Project 1"}],
            tabs=[{"tab_id": "t-new", "workspace_id": "w1", "label": "New Tab"}],
            panes=[{"pane_id": "p1", "workspace_id": "w1", "tab_id": "t-new"}],
            agents=[],
        ),
    )
    app.app_state.live_sessions = [sess]
    app.notify = Mock()
    app.exit = Mock()
    client = Mock(
        focus_workspace=AsyncMock(),
        focus_tab=AsyncMock(),
        focus_pane=AsyncMock(),
    )
    context = Mock(is_inside_herdr=True, socket_path=Path("/session/herdr.sock"))
    with (
        patch("aiheaddev.ui.tui.app.HerdrClient", return_value=client),
        patch("aiheaddev.ui.tui.app.resolve_herdr_context", return_value=context),
    ):
        await app._open_in_herdr(
            WorkspaceTarget("one", Path("/session/herdr.sock"), "w1", "t-old", "p1")
        )
    client.focus_workspace.assert_awaited_once_with("w1")
    client.focus_tab.assert_awaited_once_with("t-new")
    client.focus_pane.assert_awaited_once_with("p1")


@pytest.mark.asyncio
async def test_pane_navigation_allows_tabless_pane_without_inventing_tab():
    app = HerdrMasterApp(demo=True)
    app.demo = False
    sess = SessionInventory(
        session_name="one",
        socket_path=Path("/session/herdr.sock"),
        snapshot=SessionSnapshot(
            protocol=22,
            workspaces=[{"workspace_id": "w1", "label": "Project 1"}],
            tabs=[{"tab_id": "t1", "workspace_id": "w1"}],
            panes=[{"pane_id": "p-notab", "workspace_id": "w1", "tab_id": None}],
            agents=[],
        ),
    )
    app.app_state.live_sessions = [sess]
    app.notify = Mock()
    app.exit = Mock()
    client = Mock(
        focus_workspace=AsyncMock(),
        focus_tab=AsyncMock(),
        focus_pane=AsyncMock(),
    )
    context = Mock(is_inside_herdr=True, socket_path=Path("/session/herdr.sock"))
    with (
        patch("aiheaddev.ui.tui.app.HerdrClient", return_value=client),
        patch("aiheaddev.ui.tui.app.resolve_herdr_context", return_value=context),
    ):
        await app._open_in_herdr(
            WorkspaceTarget("one", Path("/session/herdr.sock"), "w1", None, "p-notab")
        )
    client.focus_workspace.assert_awaited_once_with("w1")
    client.focus_tab.assert_not_called()
    client.focus_pane.assert_awaited_once_with("p-notab")


@pytest.mark.asyncio
async def test_pane_navigation_rejects_contradictory_pane_workspace():
    app = HerdrMasterApp(demo=True)
    app.demo = False
    sess = SessionInventory(
        session_name="one",
        socket_path=Path("/session/herdr.sock"),
        snapshot=SessionSnapshot(
            protocol=22,
            workspaces=[{"workspace_id": "w1"}, {"workspace_id": "w2"}],
            tabs=[{"tab_id": "t1", "workspace_id": "w1"}],
            panes=[{"pane_id": "p1", "workspace_id": "w2", "tab_id": "t1"}],
            agents=[{"pane_id": "p1", "workspace_id": "w1", "tab_id": "t1"}],
        ),
    )
    app.app_state.live_sessions = [sess]
    app.notify = Mock()
    app.exit = Mock()
    client = Mock(focus_workspace=AsyncMock(), focus_tab=AsyncMock(), focus_pane=AsyncMock())
    with patch("aiheaddev.ui.tui.app.HerdrClient", return_value=client):
        await app._open_in_herdr(
            WorkspaceTarget("one", Path("/session/herdr.sock"), "w1", "t1", "p1")
        )
    client.focus_workspace.assert_not_called()
    app.exit.assert_not_called()
    app.notify.assert_called_once()
    assert "contradictory" in app.notify.call_args[0][0]


@pytest.mark.asyncio
async def test_exact_socket_targeting_with_overlapping_ids_and_names():
    app = HerdrMasterApp(demo=True)
    app.demo = False
    sock_a = Path("/tmp/herdr-a.sock")
    sock_b = Path("/tmp/herdr-b.sock")
    sess_a = inventory(sock_a, name="prod")
    sess_b = inventory(sock_b, name="prod")
    app.app_state.live_sessions = [sess_a, sess_b]
    app.notify = Mock()
    app.exit = Mock()
    client = Mock(focus_workspace=AsyncMock(), focus_tab=AsyncMock(), focus_pane=AsyncMock())
    context = Mock(is_inside_herdr=True, socket_path=sock_b)
    with (
        patch("aiheaddev.ui.tui.app.HerdrClient", return_value=client) as factory,
        patch("aiheaddev.ui.tui.app.resolve_herdr_context", return_value=context),
    ):
        await app._open_in_herdr(
            WorkspaceTarget("prod", sock_b, "w1", "t1", "p1")
        )
    factory.assert_called_once_with(session="prod", socket_path=sock_b, timeout=2.0)
    client.focus_workspace.assert_awaited_once_with("w1")


@pytest.mark.asyncio
async def test_renamed_live_session_navigation():
    app = HerdrMasterApp(demo=True)
    app.demo = False
    sess = inventory(Path("/session/herdr.sock"), name="renamed-session")
    app.app_state.live_sessions = [sess]
    app.notify = Mock()
    app.exit = Mock()
    client = Mock(focus_workspace=AsyncMock(), focus_tab=AsyncMock(), focus_pane=AsyncMock())
    context = Mock(is_inside_herdr=True, socket_path=Path("/session/herdr.sock"))
    with (
        patch("aiheaddev.ui.tui.app.HerdrClient", return_value=client) as factory,
        patch("aiheaddev.ui.tui.app.resolve_herdr_context", return_value=context),
    ):
        await app._open_in_herdr(
            WorkspaceTarget("old-session", Path("/session/herdr.sock"), "w1", "t1", "p1")
        )
    factory.assert_called_once_with(
        session="renamed-session",
        socket_path=Path("/session/herdr.sock"),
        timeout=2.0,
    )


@pytest.mark.asyncio
async def test_navigation_timeout_releases_guard_and_does_not_attach():
    app = HerdrMasterApp(demo=True)
    app.demo = False
    sess = inventory()
    app.app_state.live_sessions = [sess]
    app.notify = Mock()
    app.exit = Mock()
    client = Mock(focus_workspace=AsyncMock(side_effect=asyncio.TimeoutError("timeout")))
    context = Mock(is_inside_herdr=True, socket_path=sess.socket_path)
    with (
        patch("aiheaddev.ui.tui.app.HerdrClient", return_value=client),
        patch("aiheaddev.ui.tui.app.resolve_herdr_context", return_value=context),
    ):
        await app._open_in_herdr(
            WorkspaceTarget("one", sess.socket_path, "w1", "t1", "p1")
        )
    assert not app._navigation_pending
    app.exit.assert_not_called()
    app.notify.assert_called_once()
    assert "Could not open in Herdr" in app.notify.call_args[0][0]


@pytest.mark.asyncio
async def test_repeated_activation_while_pending_issues_only_one_navigation():
    app = HerdrMasterApp(demo=True)
    app.demo = False
    sess = inventory()
    app.app_state.live_sessions = [sess]
    block_event = asyncio.Event()
    client = Mock()

    async def slow_focus_workspace(ws_id):
        await block_event.wait()

    client.focus_workspace = AsyncMock(side_effect=slow_focus_workspace)
    client.focus_tab = AsyncMock()
    client.focus_pane = AsyncMock()
    context = Mock(is_inside_herdr=True, socket_path=sess.socket_path)
    target = WorkspaceTarget("one", sess.socket_path, "w1", "t1", "p1")

    with (
        patch("aiheaddev.ui.tui.app.HerdrClient", return_value=client),
        patch("aiheaddev.ui.tui.app.resolve_herdr_context", return_value=context),
    ):
        task1 = asyncio.create_task(app._open_in_herdr(target))
        # Yield control so task1 runs up to block_event.wait()
        await asyncio.sleep(0.01)
        assert app._navigation_pending is True

        # Second navigation while first is genuinely awaiting
        await app._open_in_herdr(target)

        # Release the event so task1 completes
        block_event.set()
        await task1

        # Exactly one focus sequence was executed
        client.focus_workspace.assert_awaited_once_with("w1")
        client.focus_tab.assert_awaited_once_with("t1")
        client.focus_pane.assert_awaited_once_with("p1")
        assert not app._navigation_pending


@pytest.mark.asyncio
async def test_enter_and_arrow_keys_do_not_focus_or_exit():
    app = HerdrMasterApp(demo=True)
    app.app_state.inventory_demo = False
    sess = inventory()
    app.app_state.live_sessions = [sess]
    app._open_in_herdr = AsyncMock()
    app.exit = Mock()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.press("down")
        await pilot.press("up")
        await pilot.press("space")
        await pilot.pause()
        app._open_in_herdr.assert_not_called()


@pytest.mark.asyncio
async def test_workspace_browser_selection_changed_emission_and_disappearance():
    """Verify SelectionChanged is emitted on deliberate selection and None on disappearance."""
    app = HerdrMasterApp(demo=True)
    app.demo = False
    app.app_state.inventory_demo = False
    sess = inventory()
    app.app_state.live_sessions = [sess]
    app._refresh_live_snapshot = AsyncMock()

    emitted_selections = []

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        browser = app.query_one(WorkspaceBrowser)

        # Hook to capture SelectionChanged messages
        original_post = browser.post_message

        def capturing_post(msg):
            if isinstance(msg, WorkspaceBrowser.SelectionChanged):
                emitted_selections.append(msg.target)
            return original_post(msg)

        browser.post_message = capturing_post

        tree = browser._tree
        ws_node = tree.root.children[0]
        ws_node.expand()
        await pilot.pause()

        # Select agent pane p1
        p1_node = ws_node.children[0]
        tree.select_node(p1_node)
        await pilot.pause()

        assert len(emitted_selections) >= 1
        last = emitted_selections[-1]
        assert last is not None
        assert last.pane_id == "p1"
        assert last.workspace_id == "w1"

        # Metadata refresh reconciles in place without auto-selecting another pane
        emitted_count_before = len(emitted_selections)
        browser.update_state(app.app_state)
        await pilot.pause()
        # No extra selection changed event emitted on healthy same-pane in-place reconciliation
        assert len(emitted_selections) == emitted_count_before
        assert browser._selected_target.pane_id == "p1"

        # Disappearance: pane p1 is removed from inventory
        empty_snap = SessionSnapshot(
            protocol=22,
            workspaces=[{"workspace_id": "w1", "label": "Project"}],
            tabs=[{"tab_id": "t1", "workspace_id": "w1"}],
            panes=[],
            agents=[],
        )
        empty_sess = SessionInventory(
            session_name="one",
            socket_path=sess.socket_path,
            snapshot=empty_snap,
        )
        app.app_state.live_sessions = [empty_sess]
        browser.update_state(app.app_state)
        await pilot.pause()

        assert browser._selected_target is None
        assert browser._selected_target_disappeared is True
        assert len(emitted_selections) > emitted_count_before
        assert emitted_selections[-1] is None


@pytest.mark.asyncio
async def test_mixed_workspace_blocked_count_suffix():
    """Verify mixed workspaces append a blocked-count suffix so running siblings
    do not conceal attention.
    """
    app = HerdrMasterApp(demo=True)
    app.app_state.inventory_demo = False

    mixed_snapshot = SessionSnapshot(
        protocol=22,
        workspaces=[{"workspace_id": "w_mixed", "label": "MixedWS", "agent_status": "working"}],
        tabs=[{"tab_id": "t1", "workspace_id": "w_mixed"}],
        panes=[
            {"pane_id": "p1", "tab_id": "t1", "agent": "worker1", "agent_status": "working"},
            {"pane_id": "p2", "tab_id": "t1", "agent": "worker2", "agent_status": "blocked"},
        ],
        agents=[
            {"pane_id": "p1", "name": "Worker 1", "agent_status": "working"},
            {"pane_id": "p2", "name": "Worker 2", "agent_status": "blocked"},
        ],
    )
    sess = SessionInventory(
        session_name="mixed-session",
        socket_path=Path("/tmp/mixed.sock"),
        snapshot=mixed_snapshot,
    )
    app.app_state.live_sessions = [sess]

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        browser = app.query_one(WorkspaceBrowser)
        tree = browser._tree
        ws_node = tree.root.children[0]
        label_plain = ws_node.label.plain

        # Primary status is running, but blocked suffix is appended
        assert "running" in label_plain
        assert "! 1 blocked" in label_plain

        # Expand to check pane badges
        ws_node.expand()
        await pilot.pause()
        p1_node = ws_node.children[0]
        p2_node = ws_node.children[1]
        assert "running" in p1_node.label.plain
        assert "blocked" in p2_node.label.plain
        assert app.return_value is None
