"""Focused Textual Pilot regression tests for reusing existing AiHead instances and tabs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aiheaddev.ui.tui.app import HerdrMasterApp
from aiheaddev.ui.tui.modals.settings import SettingsModal
from aiheaddev.ui.tui.screens.ai_head_screen import AiHeadScreen
from aiheaddev.ui.tui.widgets.quick_launch import QuickLaunchBar
from aiheaddev.ui.tui.widgets.workspaces import WorkspaceBrowser, WorkspaceTarget
from herdr_master.herdr.discovery import SessionInventory
from herdr_master.herdr.snapshot import (
    AgentSnapshot,
    PaneSnapshot,
    SessionSnapshot,
    TabSnapshot,
    WorkspaceSnapshot,
)


def _build_snapshot_with_live_head(
    ws_id: str = "ws-head-dedicated",
    tab_id: str = "tab-head-existing",
    pane_id: str = "pane-head-existing",
    agent_status: str = "ready",
) -> SessionSnapshot:
    return SessionSnapshot(
        protocol=22,
        focused_workspace_id=ws_id,
        workspaces=[
            WorkspaceSnapshot(workspace_id="ws-main", label="main-repo"),
            WorkspaceSnapshot(workspace_id=ws_id, label="ai-head"),
        ],
        tabs=[
            TabSnapshot(tab_id="tab-code", workspace_id="ws-main", label="Terminal"),
            TabSnapshot(tab_id=tab_id, workspace_id=ws_id, label="ai-head"),
        ],
        panes=[
            PaneSnapshot(pane_id="pane-code", tab_id="tab-code", workspace_id="ws-main"),
            PaneSnapshot(
                pane_id=pane_id,
                tab_id=tab_id,
                workspace_id=ws_id,
                agent="agy",
                agent_status=agent_status,
            ),
        ],
        agents=[
            AgentSnapshot(
                pane_id=pane_id,
                workspace_id=ws_id,
                tab_id=tab_id,
                agent="agy",
                name="ai-head",
                agent_status=agent_status,
                cwd="/test/repo",
            )
        ],
    )


@pytest.mark.asyncio
async def test_reuse_existing_screen_instance_from_stack(tmp_path: Path):
    """If AiHeadScreen is already in the screen stack, pop back to that same instance without pushing another."""
    client = MagicMock()
    client.socket_path = "/tmp/herdr.sock"
    client.session = "test-session"
    client.get_snapshot = AsyncMock(return_value=None)

    app = HerdrMasterApp(demo=False, session="test-session", project_path=tmp_path)
    app.app_state.workspace_id = "ws-main"

    with patch("aiheaddev.ui.tui.app.HerdrClient", return_value=client), \
         patch("aiheaddev.ui.tui.app.ensure_herdr_ai_head", new_callable=AsyncMock) as mock_ensure:
        mock_ensure.return_value = {
            "workspace_id": "ws-aihead",
            "tab_id": "tab-1",
            "pane_id": "pane-1",
            "status": "ready",
            "busy": False,
        }

        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            # 1. Genuine first launch pushes AiHeadScreen
            await pilot.press("h")
            await pilot.pause()

            head_screen = app.screen
            assert isinstance(head_screen, AiHeadScreen)
            assert len(app.screen_stack) == 2
            assert mock_ensure.call_count == 1

            # 2. Push SettingsModal on top of AiHeadScreen
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, SettingsModal)
            assert len(app.screen_stack) == 3

            # 3. Trigger action_launch_aihead while SettingsModal is active
            app.action_launch_aihead()
            await pilot.pause()

            # Must pop back to the exact same AiHeadScreen instance
            assert app.screen is head_screen
            assert len(app.screen_stack) == 2
            # Must NOT call ensure_herdr_ai_head again
            assert mock_ensure.call_count == 1


@pytest.mark.asyncio
async def test_reuse_existing_live_tab_without_create_tab_or_restart(tmp_path: Path):
    """If workspace already has a live tab labeled ai-head, focus and show it without create_tab or restart."""
    snap = _build_snapshot_with_live_head(
        ws_id="ws-head-dedicated",
        tab_id="tab-head-existing",
        pane_id="pane-head-existing",
        agent_status="ready",
    )

    client = MagicMock()
    client.socket_path = "/tmp/herdr.sock"
    client.session = "test-session"
    client.get_snapshot = AsyncMock(return_value=snap)
    client.create_tab = AsyncMock()
    client.start_agent = AsyncMock()
    client.stop_agent = AsyncMock()
    client.prompt_agent = AsyncMock()
    client.create_workspace = AsyncMock()

    app = HerdrMasterApp(demo=False, session="test-session", project_path=tmp_path)
    app.app_state.workspace_id = "ws-main"

    with patch("aiheaddev.ui.tui.app.HerdrClient", return_value=client), \
         patch("aiheaddev.ui.tui.app.ensure_herdr_ai_head", new_callable=AsyncMock) as mock_ensure:
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            assert not isinstance(app.screen, AiHeadScreen)

            # Trigger launch from QuickLaunchBar button
            await pilot.click("#btn-new-aihead")
            await pilot.pause()

            # AiHeadScreen is shown
            assert isinstance(app.screen, AiHeadScreen)
            assert len(app.screen_stack) == 2

            # Verified: existing tab and pane were reused
            assert app._ai_head_confirmed_target is not None
            assert app._ai_head_confirmed_target.tab_id == "tab-head-existing"
            assert app._ai_head_confirmed_target.pane_id == "pane-head-existing"
            assert app._ai_head_confirmed_target.workspace_id == "ws-head-dedicated"

            # Verified: NO create_tab, NO agent lifecycle calls, NO ensure_herdr_ai_head restart
            client.create_tab.assert_not_awaited()
            client.start_agent.assert_not_awaited()
            client.stop_agent.assert_not_awaited()
            client.prompt_agent.assert_not_awaited()
            mock_ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_genuine_first_launch_creates_own_workspace_when_none_exists(tmp_path: Path):
    """When no AiHead exists, ensure path creates its own dedicated workspace and ensures session."""
    snap = SessionSnapshot(
        protocol=22,
        focused_workspace_id="ws-main",
        workspaces=[WorkspaceSnapshot(workspace_id="ws-main", label="main-repo")],
        tabs=[TabSnapshot(tab_id="tab-main", workspace_id="ws-main", label="Terminal")],
        panes=[PaneSnapshot(pane_id="pane-main", tab_id="tab-main", workspace_id="ws-main")],
        agents=[],
    )

    client = MagicMock()
    client.socket_path = "/tmp/herdr.sock"
    client.session = "test-session"
    client.get_snapshot = AsyncMock(return_value=snap)
    client.create_workspace = AsyncMock(return_value={"workspace_id": "ws-aihead-dedicated"})

    app = HerdrMasterApp(demo=False, session="test-session", project_path=tmp_path)
    app.app_state.workspace_id = "ws-main"

    with patch("aiheaddev.ui.tui.app.HerdrClient", return_value=client), \
         patch("aiheaddev.ui.tui.app.ensure_herdr_ai_head", new_callable=AsyncMock) as mock_ensure:
        mock_ensure.return_value = {
            "workspace_id": "ws-aihead-dedicated",
            "tab_id": "tab-new-head",
            "pane_id": "pane-new-head",
            "status": "ready",
            "busy": False,
        }

        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            # Launch via keybinding 'h'
            await pilot.press("h")
            await pilot.pause()

            # Dedicated workspace was created for new AiHead
            client.create_workspace.assert_awaited_once_with(
                label="ai-head", cwd=str(tmp_path.resolve())
            )
            # ensure_herdr_ai_head was called with the dedicated workspace
            mock_ensure.assert_awaited_once()
            call_kwargs = mock_ensure.call_args.kwargs
            assert call_kwargs["workspace_id"] == "ws-aihead-dedicated"

            # Screen is showing the target
            assert isinstance(app.screen, AiHeadScreen)
            assert app._ai_head_confirmed_target is not None
            assert app._ai_head_confirmed_target.workspace_id == "ws-aihead-dedicated"
            assert app._ai_head_confirmed_target.pane_id == "pane-new-head"
