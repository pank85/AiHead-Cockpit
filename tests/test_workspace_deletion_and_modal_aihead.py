"""Tests for Workspace Deletion flow and AiHead option on all launch modals except Council."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Select

from herdr_master.herdr.client import HerdrClient
from herdr_master.herdr.discovery import SessionInventory
from herdr_master.herdr.snapshot import (
    AgentSnapshot,
    PaneSnapshot,
    SessionSnapshot,
    TabSnapshot,
    WorkspaceSnapshot,
)
from herdr_master.pipeline.squad import resolve_squad
from aiheaddev.ui.tui.app import HerdrMasterApp
from aiheaddev.ui.tui.modals.confirm import ConfirmModal
from aiheaddev.ui.tui.modals.launch_council import LaunchCouncilModal
from aiheaddev.ui.tui.modals.launch_ergo import LaunchErgoModal
from aiheaddev.ui.tui.modals.launch_master import LaunchMasterModal
from aiheaddev.ui.tui.modals.launch_orchestration import LaunchOrchestrationModal
from aiheaddev.ui.tui.state import AppState
from aiheaddev.ui.tui.widgets.workspaces import WorkspaceBrowser, WorkspaceTarget


@pytest.mark.asyncio
async def test_ai_head_option_present_on_all_modals_except_council(tmp_path: Path):
    """Verify AiHead override Select exists on Ergo, Orchestration, Master; absent on Council."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    squad = resolve_squad(project_dir).squad

    # 1. LaunchErgoModal
    class ErgoTestApp(App):
        def on_mount(self) -> None:
            self.modal = LaunchErgoModal(
                squad=squad,
                project_path=project_dir,
            )
            self.push_screen(self.modal)

    async with ErgoTestApp().run_test(size=(110, 45)) as pilot:
        await pilot.pause()
        modal = pilot.app.modal
        sel = modal.query_one("#ergo-aihead-override", Select)
        assert sel.value == "inherit"
        assert [opt[1] for opt in sel._options] == ["inherit", "enable", "disable"]

    # 2. LaunchOrchestrationModal
    class OrchTestApp(App):
        def on_mount(self) -> None:
            self.modal = LaunchOrchestrationModal(
                project_path=project_dir,
            )
            self.push_screen(self.modal)

    async with OrchTestApp().run_test(size=(110, 45)) as pilot:
        await pilot.pause()
        modal = pilot.app.modal
        sel = modal.query_one("#orch-aihead-override", Select)
        assert sel.value == "inherit"
        assert [opt[1] for opt in sel._options] == ["inherit", "enable", "disable"]

    # 3. LaunchMasterModal
    class MasterTestApp(App):
        def on_mount(self) -> None:
            self.modal = LaunchMasterModal(
                project_path=project_dir,
            )
            self.push_screen(self.modal)

    async with MasterTestApp().run_test(size=(110, 45)) as pilot:
        await pilot.pause()
        modal = pilot.app.modal
        sel = modal.query_one("#master-aihead-override", Select)
        assert sel.value == "inherit"
        assert [opt[1] for opt in sel._options] == ["inherit", "enable", "disable"]

    # 4. LaunchCouncilModal (explicitly NO AiHead selector)
    class CouncilTestApp(App):
        def on_mount(self) -> None:
            self.modal = LaunchCouncilModal(
                project_path=project_dir,
            )
            self.push_screen(self.modal)

    async with CouncilTestApp().run_test(size=(110, 45)) as pilot:
        await pilot.pause()
        modal = pilot.app.modal
        assert not modal.query("#council-aihead-override")


@pytest.mark.asyncio
async def test_workspace_browser_delete_button_and_request(tmp_path: Path):
    """Verify delete button in WorkspaceBrowser emits DeleteRequested."""
    app_state = AppState()
    browser = WorkspaceBrowser(app_state=app_state)

    class BrowserApp(App):
        def compose(self) -> ComposeResult:
            yield browser

    async with BrowserApp().run_test(size=(90, 30)):
        del_btn = browser.query_one("#btn-delete-workspace")
        assert del_btn is not None

        # When no target selected
        assert browser._selected_target is None
        browser.request_delete()  # Should notify, not crash


def test_live_client_for_socket_delegation(tmp_path: Path):
    """Verify _live_client_for_socket delegates to self._client_factory."""
    mock_client = MagicMock(spec=HerdrClient)
    mock_factory = MagicMock(return_value=mock_client)
    app = HerdrMasterApp(demo=False, project_path=tmp_path, live_client_factory=mock_factory)

    sock = Path("/tmp/herdr-test.sock")
    res = app._live_client_for_socket("session-foo", sock)
    assert res is mock_client
    mock_factory.assert_called_once_with("session-foo", sock)

    # Test fallback with swapped argument signature
    swapped_factory = MagicMock(side_effect=[TypeError("wrong args"), mock_client])
    app2 = HerdrMasterApp(demo=False, project_path=tmp_path, live_client_factory=swapped_factory)
    res2 = app2._live_client_for_socket("session-bar", sock)
    assert res2 is mock_client

    # Test default factory produces HerdrClient
    app_default = HerdrMasterApp(demo=False, project_path=tmp_path)
    client = app_default._live_client_for_socket("default-sess", sock)
    assert isinstance(client, HerdrClient)
    assert client.session == "default-sess"
    assert client.socket_path == sock


@pytest.mark.asyncio
async def test_app_delete_workspace_flow_completed_workspace(tmp_path: Path):
    """Test full delete workspace confirmation and Herdr client close_workspace execution."""
    mock_client = AsyncMock(spec=HerdrClient)
    mock_client.close_workspace = AsyncMock(return_value={"status": "ok"})
    mock_factory = MagicMock(return_value=mock_client)
    app = HerdrMasterApp(demo=False, project_path=tmp_path, live_client_factory=mock_factory)
    app.client = mock_client
    app._schedule_inventory_refresh = MagicMock()

    # Setup live session inventory with a completed workspace
    sock_path = Path("/tmp/test-herdr.sock")
    snapshot = SessionSnapshot(
        protocol=22,
        workspaces=[WorkspaceSnapshot(workspace_id="ws-completed", label="Completed Task")],
        tabs=[TabSnapshot(tab_id="tab-1", workspace_id="ws-completed")],
        panes=[PaneSnapshot(pane_id="pane-1", tab_id="tab-1", workspace_id="ws-completed")],
        agents=[
            AgentSnapshot(
                pane_id="pane-1",
                tab_id="tab-1",
                workspace_id="ws-completed",
                agent_status="done",
            )
        ],
    )
    inv = SessionInventory(session_name="test-session", socket_path=sock_path, snapshot=snapshot)
    app.app_state.live_sessions = [inv]

    async with app.run_test(size=(110, 35)) as pilot:
        target = WorkspaceTarget(
            socket_path=sock_path,
            session_name="test-session",
            workspace_id="ws-completed",
        )

        # Trigger delete workspace flow
        await app._delete_workspace_flow(target)
        await pilot.pause()

        # ConfirmModal should be shown
        confirm_modal = pilot.app.screen
        assert isinstance(confirm_modal, ConfirmModal)
        assert "Delete Completed Workspace" in confirm_modal.title_text

        # Click Confirm
        await pilot.click("#btn-confirm")
        await pilot.pause()

        # Client close_workspace should have been called
        mock_factory.assert_called_once_with("test-session", sock_path)
        mock_client.close_workspace.assert_awaited_once_with("ws-completed")
        assert app._schedule_inventory_refresh.call_count >= 1


@pytest.mark.asyncio
async def test_app_delete_workspace_flow_active_workspace(tmp_path: Path):
    """Test delete workspace flow with running agents warns and closes workspace."""
    mock_client = AsyncMock(spec=HerdrClient)
    mock_client.close_workspace = AsyncMock(return_value={"status": "ok"})
    mock_factory = MagicMock(return_value=mock_client)
    app = HerdrMasterApp(demo=False, project_path=tmp_path, live_client_factory=mock_factory)
    app.client = mock_client
    app._schedule_inventory_refresh = MagicMock()

    sock_path = Path("/tmp/test-herdr.sock")
    snapshot = SessionSnapshot(
        protocol=22,
        workspaces=[WorkspaceSnapshot(workspace_id="ws-active", label="Active Task")],
        tabs=[TabSnapshot(tab_id="tab-1", workspace_id="ws-active")],
        panes=[PaneSnapshot(pane_id="pane-1", tab_id="tab-1", workspace_id="ws-active")],
        agents=[
            AgentSnapshot(
                pane_id="pane-1",
                tab_id="tab-1",
                workspace_id="ws-active",
                agent_status="working",
            )
        ],
    )
    inv = SessionInventory(session_name="test-session", socket_path=sock_path, snapshot=snapshot)
    app.app_state.live_sessions = [inv]

    async with app.run_test(size=(110, 35)) as pilot:
        target = WorkspaceTarget(
            socket_path=sock_path,
            session_name="test-session",
            workspace_id="ws-active",
        )

        await app._delete_workspace_flow(target)
        await pilot.pause()

        confirm_modal = pilot.app.screen
        assert isinstance(confirm_modal, ConfirmModal)
        assert "Close Active Workspace" in confirm_modal.title_text

        await pilot.click("#btn-confirm")
        await pilot.pause()

        mock_factory.assert_called_once_with("test-session", sock_path)
        mock_client.close_workspace.assert_awaited_once_with("ws-active")
        assert app._schedule_inventory_refresh.call_count >= 1

