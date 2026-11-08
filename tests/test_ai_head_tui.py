"""Tests for TUI integration of SettingsModal, LaunchCouncilModal, and persistent interactive AiHead."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.text import Text

from herdr_master.core.config import AiHeadConfig, HerdrMasterConfig
from herdr_master.core.models import HarnessBinding, HarnessKind, Squad
from herdr_master.herdr.discovery import SessionInventory
from herdr_master.herdr.snapshot import AgentSnapshot, PaneSnapshot, SessionSnapshot, TabSnapshot, WorkspaceSnapshot
from aiheaddev.ui.tui.app import HerdrMasterApp
from aiheaddev.ui.tui.modals.launch_council import CouncilLaunchParams, LaunchCouncilModal
from aiheaddev.ui.tui.modals.settings import SettingsModal
from aiheaddev.ui.tui.state import AppState
from aiheaddev.ui.tui.widgets.quick_launch import QuickLaunchBar
from aiheaddev.ui.tui.widgets.workspaces import WorkspaceBrowser, WorkspaceTarget


@pytest.mark.asyncio
async def test_settings_modal_pushed_from_button_and_shortcuts():
    """Verify SettingsModal is pushed via #btn-settings, 's', and 'c'."""
    app = HerdrMasterApp(demo=True)
    async with app.run_test(size=(110, 40)) as pilot:
        # Click #btn-settings
        await pilot.click("#btn-settings")
        assert isinstance(pilot.app.screen, SettingsModal)
        await pilot.click("#btn-cancel")
        assert not isinstance(pilot.app.screen, SettingsModal)

        # Press 's'
        await pilot.press("s")
        assert isinstance(pilot.app.screen, SettingsModal)
        await pilot.click("#btn-cancel")
        assert not isinstance(pilot.app.screen, SettingsModal)

        # Press 'c'
        await pilot.press("c")
        assert isinstance(pilot.app.screen, SettingsModal)
        await pilot.click("#btn-cancel")
        assert not isinstance(pilot.app.screen, SettingsModal)


@pytest.mark.asyncio
async def test_council_modal_pushed_from_quick_launch():
    """Verify clicking #btn-new-council pushes LaunchCouncilModal."""
    app = HerdrMasterApp(demo=True)
    async with app.run_test(size=(110, 40)) as pilot:
        toolbar = pilot.app.query_one(QuickLaunchBar)
        assert toolbar is not None
        btn = toolbar.query_one("#btn-new-council")
        assert btn is not None
        assert btn.label.plain == "AI Council"

        await pilot.click("#btn-new-council")
        assert isinstance(pilot.app.screen, LaunchCouncilModal)
        await pilot.click("#btn-cancel")
        assert not isinstance(pilot.app.screen, LaunchCouncilModal)


@pytest.mark.asyncio
async def test_saving_settings_never_starts_pipeline():
    """Verify that saving settings updates app_state.config and squad but never starts a pipeline."""
    app = HerdrMasterApp(demo=True)
    async with app.run_test(size=(110, 40)) as pilot:
        initial_pipeline_run = app.app_state.pipeline_run
        assert not app._launch_pending

        new_config = HerdrMasterConfig()
        new_config.ai_head.enabled = False
        new_squad = Squad()

        await pilot.press("s")
        assert isinstance(pilot.app.screen, SettingsModal)
        screen = pilot.app.screen
        screen.dismiss((new_config, new_squad))
        await pilot.pause()

        assert app.app_state.config == new_config
        assert app.app_state.squad == new_squad
        assert not app._launch_pending
        # Pipeline run remains unchanged (no new run created or launched)
        assert app.app_state.pipeline_run == initial_pipeline_run


@pytest.mark.asyncio
async def test_saving_settings_with_ai_head_enabled_does_not_start_agent(tmp_path):
    """Verify that saving settings with ai_head.enabled=True does NOT call ensure_herdr_ai_head or start agents."""
    app = HerdrMasterApp(demo=False, project_path=tmp_path)
    app.client = MagicMock()
    app.session_name = "test-session"
    app.app_state.session_name = "test-session"
    app.app_state.workspace_id = "ws-test"

    new_config = HerdrMasterConfig()
    new_config.ai_head.enabled = True
    new_squad = Squad()

    with patch("aiheaddev.ui.tui.app.ensure_herdr_ai_head", new_callable=AsyncMock) as mock_ensure, \
         patch.object(app, "_context_for_launch") as mock_ctx:

        mock_ctx.return_value = MagicMock(session_name="test-session", socket_path="/tmp/sock")

        async with app.run_test(size=(110, 40)) as pilot:
            # Reset any call from initial on_mount
            mock_ensure.reset_mock()

            await pilot.press("s")
            assert isinstance(pilot.app.screen, SettingsModal)
            screen = pilot.app.screen
            screen.dismiss((new_config, new_squad))
            await pilot.pause()
            await asyncio.sleep(0.05)

            assert app.app_state.config == new_config
            assert app.app_state.config.ai_head.enabled is True
            assert app.app_state.squad == new_squad
            # ensure_herdr_ai_head must NOT be called when saving settings
            mock_ensure.assert_not_called()


@pytest.mark.asyncio
async def test_ai_head_tab_in_workspace_browser():
    """Verify WorkspaceBrowser badges and labels persistent ai-head tab with [AiHead]."""
    snap = SessionSnapshot(
        protocol=22,
        focused_workspace_id="ws-head",
        workspaces=[WorkspaceSnapshot(workspace_id="ws-head", label="main-repo", agent_status="working")],
        tabs=[
            TabSnapshot(tab_id="tab-head", workspace_id="ws-head", label="ai-head"),
            TabSnapshot(tab_id="tab-shell", workspace_id="ws-head", label="Terminal"),
        ],
        panes=[
            PaneSnapshot(pane_id="pane-head", tab_id="tab-head", workspace_id="ws-head", agent="agy", agent_status="working"),
            PaneSnapshot(pane_id="pane-shell", tab_id="tab-shell", workspace_id="ws-head"),
        ],
        agents=[
            AgentSnapshot(
                pane_id="pane-head",
                workspace_id="ws-head",
                tab_id="tab-head",
                agent="agy",
                name="ai-head",
                agent_status="working",
                cwd="/test/repo",
            )
        ],
    )
    inv = SessionInventory(session_name="test-session", socket_path=Path("/tmp/herdr.sock"), snapshot=snap)
    app_state = AppState(
        session_name="test-session",
        live_sessions=[inv],
        workspace_id="ws-head",
        workspace_label="main-repo",
    )

    app = HerdrMasterApp(demo=True)
    async with app.run_test(size=(110, 40)) as pilot:
        active_browser = app.query_one(WorkspaceBrowser)
        active_browser.update_state(app_state)
        await pilot.pause()

        tree = active_browser._tree
        found_head_node = False
        target_found: WorkspaceTarget | None = None

        for ws_node in tree.root.children:
            for pane_node in ws_node.children:
                label_text = pane_node.label.plain if isinstance(pane_node.label, Text) else str(pane_node.label)
                if "[AiHead]" in label_text:
                    found_head_node = True
                    target_found = pane_node.data
                    break

        assert found_head_node, "Expected an [AiHead] badged node in the tree"
        assert target_found is not None
        assert target_found.pane_id == "pane-head"
        assert target_found.workspace_id == "ws-head"


@pytest.mark.asyncio
async def test_ensure_ai_head_if_enabled_demo_mode_skipped():
    """Verify _ensure_ai_head_if_enabled does nothing in demo mode."""
    app = HerdrMasterApp(demo=True)
    with patch("aiheaddev.ui.tui.app.ensure_herdr_ai_head", new_callable=AsyncMock) as mock_ensure:
        app._ensure_ai_head_if_enabled()
        await asyncio.sleep(0.05)
        mock_ensure.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_ai_head_if_enabled_live_mode():
    """Verify _ensure_ai_head_if_enabled calls ensure_herdr_ai_head when enabled in non-demo mode."""
    cfg = HerdrMasterConfig()
    cfg.ai_head.enabled = True
    cfg.ai_head.binding = HarnessBinding(kind=HarnessKind.AGY, model="claude-3-5-sonnet")

    app = HerdrMasterApp(demo=False)
    app.client = MagicMock()
    app.app_state.config = cfg
    app.app_state.workspace_id = "ws-target"

    with patch("aiheaddev.ui.tui.app.ensure_herdr_ai_head", new_callable=AsyncMock) as mock_ensure:
        mock_ensure.return_value = {
            "workspace_id": "ws-target",
            "tab_id": "tab-head",
            "pane_id": "pane-head",
            "reused": True,
            "enabled": True,
            "label": "ai-head",
        }
        with patch.object(app, "_schedule_inventory_refresh") as mock_refresh:
            app._ensure_ai_head_if_enabled()
            # Allow background worker to complete
            await asyncio.sleep(0.1)

            mock_ensure.assert_awaited_once()
            _, kwargs = mock_ensure.call_args
            assert kwargs["workspace_id"] == "ws-target"
            assert kwargs["head_config"].enabled is True
            mock_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_ensure_ai_head_reconciles_default_ws_01_from_live_snapshot():
    """Verify that when app_state.workspace_id has default 'ws-01' or None,
    _ensure_ai_head_if_enabled queries the snapshot and uses the live workspace."""
    cfg = HerdrMasterConfig()
    cfg.ai_head.enabled = True
    cfg.ai_head.binding = HarnessBinding(kind=HarnessKind.AGY, model="claude-3-5-sonnet")

    app = HerdrMasterApp(demo=False)
    snap = SessionSnapshot(
        protocol=22,
        focused_workspace_id="ws-real-herdr",
        workspaces=[WorkspaceSnapshot(workspace_id="ws-real-herdr", label="live")],
        tabs=[],
        panes=[],
    )
    mock_client = MagicMock()
    mock_client.get_snapshot = AsyncMock(return_value=snap)
    app.client = mock_client
    app.app_state.config = cfg
    # Default state has workspace_id as None or ws-01
    app.app_state.workspace_id = "ws-01"

    with patch("aiheaddev.ui.tui.app.ensure_herdr_ai_head", new_callable=AsyncMock) as mock_ensure:
        mock_ensure.return_value = {
            "workspace_id": "ws-real-herdr",
            "tab_id": "tab-head",
            "pane_id": "pane-head",
            "reused": True,
            "enabled": True,
            "label": "ai-head",
        }
        with patch.object(app, "_schedule_inventory_refresh"):
            app._ensure_ai_head_if_enabled()
            await asyncio.sleep(0.1)

            mock_ensure.assert_awaited_once()
            _, kwargs = mock_ensure.call_args
            assert kwargs["workspace_id"] == "ws-real-herdr"
            assert app.app_state.workspace_id == "ws-real-herdr"



@pytest.mark.asyncio
async def test_council_launch_flow(tmp_path):
    """Verify action_new_council executes launch_council and updates app_state.pipeline_run."""
    app = HerdrMasterApp(demo=False, project_path=tmp_path)
    mock_client = MagicMock()
    app.client = mock_client
    app.session_name = "test-session"
    app.app_state.session_name = "test-session"

    cfg = HerdrMasterConfig()
    params = CouncilLaunchParams(
        intent="Deliberate on design",
        project_folder=tmp_path,
        workspace_label="council-20261106",
        council_config=cfg.council,
        ai_head_override=True,
    )

    with patch("aiheaddev.ui.tui.app.launch_council", new_callable=AsyncMock) as mock_launch, \
         patch.object(app, "_context_for_launch") as mock_ctx, \
         patch.object(app, "_exit_launch") as mock_exit:

        mock_ctx.return_value = MagicMock(session_name="test-session", socket_path="/tmp/sock")
        mock_launch.return_value = {
            "session": "test-session",
            "socket": "/tmp/sock",
            "workspace_id": "ws-council",
            "pane_id": "pane-coord",
            "agent_name": "council-coordinator",
        }

        async with app.run_test(size=(110, 40)) as pilot:
            # Click AI Council button on toolbar
            await pilot.click("#btn-new-council")
            modal = pilot.app.screen
            assert isinstance(modal, LaunchCouncilModal)
            modal.dismiss(params)

            # Allow background launch worker to execute
            await pilot.pause()
            await asyncio.sleep(0.1)

            mock_launch.assert_awaited_once()
            assert app.app_state.pipeline_run is not None
            assert app.app_state.pipeline_run.run_id == "council-council-20261106"
            assert app.app_state.pipeline_run.metadata["mode"] == "council"
            assert app.app_state.pipeline_run.metadata["pane_id"] == "pane-coord"
            mock_exit.assert_called_once()


@pytest.mark.asyncio
async def test_tui_handles_busy_ai_head_gracefully():
    """Verify that when ensure_herdr_ai_head returns busy=True, app_state.ai_head_busy is recorded."""
    cfg = HerdrMasterConfig()
    cfg.ai_head.enabled = True
    cfg.ai_head.binding = HarnessBinding(kind=HarnessKind.AGY, model="claude-3-5-sonnet")

    app = HerdrMasterApp(demo=False)
    app.client = MagicMock()
    app.app_state.config = cfg
    app.app_state.workspace_id = "ws-target"

    head_info = {
        "workspace_id": "ws-target",
        "tab_id": "tab-head",
        "pane_id": "pane-head",
        "reused": True,
        "enabled": True,
        "label": "ai-head",
        "busy": True,
        "reconciled": False,
        "model": "previous-model",
    }

    with patch("aiheaddev.ui.tui.app.ensure_herdr_ai_head", new_callable=AsyncMock) as mock_ensure:
        mock_ensure.return_value = head_info
        with patch.object(app, "_schedule_inventory_refresh"):
            app._ensure_ai_head_if_enabled()
            await asyncio.sleep(0.1)

            assert app.app_state.ai_head_info == head_info
            assert app.app_state.ai_head_busy is True


def _head_app(agent_status, info=None, busy=True):
    snap = SessionSnapshot(
        protocol=22,
        focused_workspace_id="ws-head",
        workspaces=[WorkspaceSnapshot(workspace_id="ws-head", label="main-repo")],
        agents=[
            AgentSnapshot(
                pane_id="pane-head", workspace_id="ws-head", agent="agy",
                name="ai-head", agent_status=agent_status,
            )
        ],
    )
    client = MagicMock()
    client.socket_path = "/tmp/herdr.sock"
    client.session = "test-session"
    client.get_snapshot = AsyncMock(return_value=snap)
    cfg = HerdrMasterConfig()
    cfg.ai_head.enabled = True

    app = HerdrMasterApp(demo=False)
    app.client = client
    app.app_state.config = cfg
    app.app_state.workspace_id = "ws-head"
    app.app_state.ai_head_busy = busy
    app.app_state.ai_head_info = info or {
        "pane_id": "pane-head", "busy": busy, "status": "busy", "reconciled": False,
    }
    return app, client


async def _refresh(app, client, ensure_mock):
    with patch("aiheaddev.ui.tui.app.discover_clients", return_value=[client]), \
         patch("aiheaddev.ui.tui.app.ensure_herdr_ai_head", ensure_mock), \
         patch.object(app, "_update_all_widgets"), \
         patch.object(app, "_reconcile_live_inventory", new_callable=AsyncMock), \
         patch.object(app, "_schedule_inventory_refresh"), \
         patch.object(app, "notify") as notify:
        await app._refresh_live_snapshot()
        await asyncio.sleep(0.1)
    return notify


@pytest.mark.asyncio
async def test_busy_to_idle_applies_deferred_config_via_ensure():
    app, client = _head_app("idle")
    ensure = AsyncMock(return_value={
        "pane_id": "pane-head", "busy": False, "status": "ready", "reconciled": True,
    })
    await _refresh(app, client, ensure)
    ensure.assert_awaited_once()
    assert app.app_state.ai_head_busy is False
    assert app.app_state.ai_head_info["busy"] is False
    assert app.app_state.ai_head_info["status"] == "ready"
    assert app.app_state.ai_head_info["reconciled"] is True


@pytest.mark.asyncio
async def test_idle_snapshot_alone_never_marks_ready_without_ensure_result():
    app, client = _head_app("idle")
    ensure = AsyncMock(return_value={
        "pane_id": "pane-head", "busy": False, "status": "pending", "reconciled": False,
    })
    await _refresh(app, client, ensure)
    assert app.app_state.ai_head_info["status"] == "pending"
    assert app.app_state.ai_head_info["reconciled"] is False


@pytest.mark.asyncio
async def test_ensure_still_busy_stays_busy():
    app, client = _head_app("idle")
    ensure = AsyncMock(return_value={
        "pane_id": "pane-head", "busy": True, "status": "busy", "reconciled": False,
    })
    await _refresh(app, client, ensure)
    ensure.assert_awaited_once()
    assert app.app_state.ai_head_busy is True
    assert app.app_state.ai_head_info["status"] == "busy"
    assert app.app_state.ai_head_info["reconciled"] is False


@pytest.mark.asyncio
async def test_ensure_raises_remains_pending_and_notifies():
    app, client = _head_app("idle")
    ensure = AsyncMock(side_effect=RuntimeError("boom"))
    notify = await _refresh(app, client, ensure)
    assert app.app_state.ai_head_info["status"] == "pending"
    assert app.app_state.ai_head_info["reconciled"] is False
    notify.assert_called_once()
    assert app._ai_head_ensure_pending is False


@pytest.mark.asyncio
async def test_stopped_status_not_treated_as_ready():
    app, client = _head_app("stopped")
    ensure = AsyncMock()
    await _refresh(app, client, ensure)
    ensure.assert_not_awaited()
    assert app.app_state.ai_head_info["status"] == "busy"
    assert app.app_state.ai_head_info["reconciled"] is False
    assert app.app_state.ai_head_busy is True


@pytest.mark.asyncio
async def test_working_snapshot_sets_busy_only():
    app, client = _head_app(
        "working",
        info={"pane_id": "pane-head", "busy": False, "status": "ready", "reconciled": True},
        busy=False,
    )
    ensure = AsyncMock()
    await _refresh(app, client, ensure)
    ensure.assert_not_awaited()
    assert app.app_state.ai_head_busy is True
    assert app.app_state.ai_head_info["busy"] is True
