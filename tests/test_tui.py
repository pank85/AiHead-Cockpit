"""Unit and Pilot tests for Herdr-Master Textual TUI Cockpit."""

from pathlib import Path

import pytest
from textual.widgets import Tree

from aiheaddev.ui.tui.app import HerdrMasterApp
from aiheaddev.ui.tui.modals.launch_ergo import LaunchErgoModal
from aiheaddev.ui.tui.modals.launch_master import LaunchMasterModal
from aiheaddev.ui.tui.modals.settings import SettingsModal
from aiheaddev.ui.tui.widgets.deck import CenterDeck
from aiheaddev.ui.tui.widgets.footer import ActionFooter
from aiheaddev.ui.tui.widgets.header_bar import HeaderBar
from aiheaddev.ui.tui.widgets.hierarchy import HierarchyTree
from aiheaddev.ui.tui.widgets.quick_launch import QuickLaunchBar
from aiheaddev.ui.tui.widgets.workspaces import WorkspaceBrowser


@pytest.mark.asyncio
async def test_tui_app_mount_and_components():
    """Verify full-width cockpit mounts exactly one workspace browser and no obsolete views."""
    app = HerdrMasterApp(demo=True)
    async with app.run_test() as pilot:
        # Check core widgets exist
        assert pilot.app.query_one(HeaderBar) is not None
        assert pilot.app.query_one(CenterDeck) is not None
        assert pilot.app.query_one(ActionFooter) is not None
        assert pilot.app.query_one(WorkspaceBrowser) is not None
        assert len(pilot.app.query(WorkspaceBrowser)) == 1

        # CenterDeck has no tab bar or obsolete view switching
        deck = pilot.app.query_one(CenterDeck)
        assert not hasattr(deck, "_tab_bar")
        assert not hasattr(deck, "switch_mode")

        # Verify HierarchyTree is NOT on the first page
        assert len(pilot.app.query(HierarchyTree)) == 0

        # Open in Herdr button exists and is keyboard-focusable
        btn = pilot.app.query_one("#btn-open-herdr")
        assert btn is not None


@pytest.mark.asyncio
async def test_deck_mode_switching_keys():
    """Verify obsolete action methods, bindings, and footer hints are removed."""
    app = HerdrMasterApp(demo=True)
    async with app.run_test() as pilot:
        # Assert obsolete action methods are absent from HerdrMasterApp
        obsolete_actions = [
            "action_deck_mode",
            "action_prompt_agent",
            "action_review_worker",
            "action_squash_land",
            "action_kill_worker",
        ]
        for action in obsolete_actions:
            assert not hasattr(pilot.app, action)

        # Assert removed keys are absent from app bindings
        bound_keys = {b.key for b in pilot.app.BINDINGS}
        for key in ("0", "1", "2", "3", "4", "p", "r", "m", "k"):
            assert key not in bound_keys

        # Assert footer does not show obsolete hints
        footer_text = str(pilot.app.query_one(ActionFooter).render())
        for hint in ("Views", "Prompt", "Review", "Squash", "Kill"):
            assert hint not in footer_text

        # Press removed keys on main screen: verify no state mutation or view switches occur
        before_state = pilot.app.app_state.model_dump()
        for key in ("0", "1", "2", "3", "4", "p", "r", "m", "k"):
            await pilot.press(key)
            await pilot.pause()
        assert pilot.app.app_state.model_dump() == before_state
        assert pilot.app.is_running


@pytest.mark.asyncio
async def test_settings_modal_open_and_tree():
    """Verify pressing 'c' or 's' opens SettingsModal with squad tree."""
    app = HerdrMasterApp(demo=True)
    async with app.run_test(size=(110, 45)) as pilot:
        await pilot.press("c")
        # Top screen should now be SettingsModal
        assert isinstance(pilot.app.screen, SettingsModal)

        # Check that the squad tree exists inside SquadEditor
        tree = pilot.app.screen.query_one("#squad-tree", Tree)
        assert tree is not None
        assert "Herdr-Master Root" in str(tree.root.label)

        # Dismiss with Cancel button
        await pilot.click("#btn-cancel")
        assert not isinstance(pilot.app.screen, SettingsModal)

        # Also test pressing 's'
        await pilot.press("s")
        assert isinstance(pilot.app.screen, SettingsModal)
        await pilot.click("#btn-cancel")
        assert not isinstance(pilot.app.screen, SettingsModal)


@pytest.mark.asyncio
async def test_launch_ergo_modal_worker_dynamic_effort_and_description():
    """Verify worker pool hides effort select, shows effort note, and exposes description."""
    app = HerdrMasterApp(demo=True)
    async with app.run_test(size=(110, 45)) as pilot:
        await pilot.press("e")
        modal = pilot.app.screen
        assert isinstance(modal, LaunchErgoModal)

        # Initially Scout is selected: effort select visible, description hidden
        assert modal._effort_select.display is True
        assert modal._effort_note.display is False
        assert modal._description_input.display is False

        # Select Worker 0: effort select hidden, dynamic effort note visible, description visible
        modal._load_agent_into_editor(("worker", 0))
        assert modal._effort_select.display is False
        assert modal._effort_note.display is True
        assert modal._description_input.display is True
        assert "Simple, mechanical, well-scoped jobs" in modal._description_input.value

        # Select Scout again: returns to standard role editor
        modal._load_agent_into_editor("scout")
        assert modal._effort_select.display is True
        assert modal._effort_note.display is False
        assert modal._description_input.display is False

        await pilot.click("#btn-cancel")


@pytest.mark.asyncio
async def test_launch_master_modal_and_quick_launch():
    """Verify clicking #btn-new-master or pressing 'n' opens LaunchMasterModal."""
    app = HerdrMasterApp(demo=True)
    async with app.run_test(size=(110, 45)) as pilot:
        quick_bar = pilot.app.query_one(QuickLaunchBar)
        assert quick_bar is not None

        # Press 'n' to open LaunchMasterModal
        await pilot.press("n")
        assert isinstance(pilot.app.screen, LaunchMasterModal)
        await pilot.click("#btn-cancel")
        assert not isinstance(pilot.app.screen, LaunchMasterModal)

        # Click button on QuickLaunchBar
        await pilot.click("#btn-new-master")
        assert isinstance(pilot.app.screen, LaunchMasterModal)
        await pilot.click("#btn-cancel")
        assert not isinstance(pilot.app.screen, LaunchMasterModal)


@pytest.mark.asyncio
async def test_launch_ergo_modal_and_quick_launch():
    """Verify clicking #btn-new-ergo or pressing 'e' opens LaunchErgoModal, and #btn-settings opens SettingsModal."""
    app = HerdrMasterApp(demo=True)
    async with app.run_test(size=(110, 45)) as pilot:
        # Press 'e' to open LaunchErgoModal
        await pilot.press("e")
        assert isinstance(pilot.app.screen, LaunchErgoModal)
        await pilot.click("#btn-cancel")
        assert not isinstance(pilot.app.screen, LaunchErgoModal)

        # Click New Ergo button on QuickLaunchBar
        await pilot.click("#btn-new-ergo")
        assert isinstance(pilot.app.screen, LaunchErgoModal)
        await pilot.click("#btn-cancel")
        assert not isinstance(pilot.app.screen, LaunchErgoModal)

        # Click Settings button on QuickLaunchBar
        await pilot.click("#btn-settings")
        assert isinstance(pilot.app.screen, SettingsModal)

        # Verify bottom section widgets exist
        assert pilot.app.screen.query_one("#btn-save") is not None
        assert pilot.app.screen.query_one("#btn-cancel") is not None

        await pilot.click("#btn-cancel")
        assert not isinstance(pilot.app.screen, SettingsModal)


@pytest.mark.asyncio
async def test_launch_master_submits_to_app_state():
    """Verify launching a master correctly stores MasterSession in app_state.masters."""
    from herdr_master.core.models import HarnessKind
    from aiheaddev.ui.tui.modals.launch_master import MasterLaunchParams

    app = HerdrMasterApp(demo=True)
    async with app.run_test(size=(110, 45)) as pilot:
        await pilot.press("n")
        modal = pilot.app.screen
        modal.dismiss(
            MasterLaunchParams(
                name="test-coord",
                harness=HarnessKind.AGY,
                model="gemini-3.8-flash-high",
            )
        )
        await pilot.pause()
        assert "master-test-coord" in pilot.app.app_state.masters
        master = pilot.app.app_state.masters["master-test-coord"]
        assert master.harness == HarnessKind.AGY


@pytest.mark.asyncio
async def test_tui_launch_ergo_outside_herdr_stays_open():
    """The multi-project cockpit stays open after launching, even outside Herdr."""
    from unittest.mock import AsyncMock, patch

    from herdr_master.core.context import HerdrContext
    from aiheaddev.ui.tui.modals.launch_ergo import ErgoLaunchParams

    mock_client = AsyncMock()
    mock_client.session = "default"

    fake_ctx = HerdrContext(
        is_inside_herdr=False,
        session_name="default",
        socket_path=Path("/tmp/herdr.sock"),
    )
    fake_res = {
        "session": "ergo-worker",
        "workspace_id": "w_test",
        "pane_id": "w_test:p1",
        "orchestrator_pane_id": "w_test:p1",
        "scout_pane_id": "w_test:p1",
        "tab_id": "w_test:t1",
    }

    app = HerdrMasterApp(demo=False)
    app.client = mock_client

    with (
        patch("aiheaddev.ui.tui.app.resolve_launch_context", return_value=fake_ctx),
        patch("aiheaddev.ui.tui.app.launch_ergo", new_callable=AsyncMock, return_value=fake_res),
    ):
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("e")
            modal = pilot.app.screen
            modal.dismiss(
                ErgoLaunchParams(
                    intent="Deploy service",
                    workspace_label="worker",
                    start_pipeline=True,
                )
            )
            await pilot.pause()
            assert app.return_value is None
            assert app.is_running


@pytest.mark.asyncio
async def test_tui_launch_ergo_inside_herdr_stays_open():
    """Inside Herdr, Ergo opens a fresh workspace and the cockpit stays running."""
    from unittest.mock import AsyncMock, patch

    from herdr_master.core.context import HerdrContext
    from aiheaddev.ui.tui.modals.launch_ergo import ErgoLaunchParams

    mock_client = AsyncMock()
    mock_client.session = "default"

    fake_ctx = HerdrContext(
        is_inside_herdr=True,
        session_name="default",
        workspace_id="w9",
        pane_id="w9:p1",
        socket_path=Path("/tmp/herdr.sock"),
    )
    fake_res = {
        "session": "default",
        "workspace_id": "w-new",
        "pane_id": "w-new:p1",
        "orchestrator_pane_id": "w9:p2",
        "scout_pane_id": "w9:p2",
        "tab_id": "w9:t2",
    }

    app = HerdrMasterApp(demo=False)
    app.client = mock_client

    with (
        patch("aiheaddev.ui.tui.app.resolve_launch_context", return_value=fake_ctx),
        patch(
            "aiheaddev.ui.tui.app.launch_ergo", new_callable=AsyncMock, return_value=fake_res
        ) as launch,
    ):
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("e")
            modal = pilot.app.screen
            modal.dismiss(
                ErgoLaunchParams(
                    intent="Deploy service",
                    workspace_label="worker",
                    start_pipeline=True,
                )
            )
            await pilot.pause()
            assert app.return_value is None
            assert app.app_state.workspace_id != "w-new"
    launch.assert_awaited_once()
    assert launch.await_args.kwargs["fresh_workspace"] is True
    assert launch.await_args.kwargs["focus"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "return_socket,expected_meta",
    [
        ("/custom/herdr.sock", "/custom/herdr.sock"),
        (None, ""),
    ],
)
async def test_live_custom_master_launch_records_exact_socket_without_handoff(
    return_socket, expected_meta, tmp_path: Path
):
    from unittest.mock import AsyncMock, patch

    from herdr_master.core.context import HerdrContext
    from herdr_master.core.models import HarnessKind
    from aiheaddev.ui.tui.modals.launch_master import MasterLaunchParams

    mock_client = AsyncMock()
    mock_client.session = "default"

    fake_ctx = HerdrContext(
        is_inside_herdr=False,
        session_name="default",
        socket_path=Path("/tmp/herdr.sock"),
    )
    fake_res = {
        "session": "custom-sess",
        "workspace_id": "w-cust",
        "pane_id": "w-cust:p1",
        "model": "gemini-3.8-flash-high",
    }
    if return_socket is not None:
        fake_res["socket"] = return_socket

    app = HerdrMasterApp(demo=False, project_path=tmp_path)
    app.client = mock_client
    app._refresh_live_snapshot = AsyncMock()

    with (
        patch("aiheaddev.ui.tui.app.resolve_launch_context", return_value=fake_ctx),
        patch(
            "aiheaddev.ui.tui.app.launch_master",
            new_callable=AsyncMock,
            return_value=fake_res,
        ),
    ):
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("n")
            modal = pilot.app.screen
            assert isinstance(modal, LaunchMasterModal)
            modal.dismiss(
                MasterLaunchParams(
                    name="cust-coord",
                    harness=HarnessKind.AGY,
                    model="gemini-3.8-flash-high",
                    project_path=tmp_path,
                )
            )
            await pilot.pause()
            assert app.return_value is None
            assert app.is_running
            master = app.app_state.masters["master-cust-coord"]
            assert master.metadata["socket_path"] == expected_meta


@pytest.mark.asyncio
async def test_removed_main_screen_keys_produce_no_mocked_navigation_rpcs():
    from unittest.mock import AsyncMock

    app = HerdrMasterApp(demo=True)
    app._open_in_herdr = AsyncMock()
    async with app.run_test() as pilot:
        for key in ("0", "1", "2", "3", "4", "p", "r", "m", "k"):
            await pilot.press(key)
            await pilot.pause()
        app._open_in_herdr.assert_not_called()


@pytest.mark.asyncio
async def test_typing_shortcuts_in_launcher_and_config_inputs(tmp_path: Path):
    from unittest.mock import AsyncMock

    app = HerdrMasterApp(demo=False, project_path=tmp_path)
    app._open_in_herdr = AsyncMock()
    app._refresh_live_snapshot = AsyncMock()
    async with app.run_test(size=(110, 45)) as pilot:
        # Open LaunchMasterModal
        await pilot.press("n")
        modal = pilot.app.screen
        assert isinstance(modal, LaunchMasterModal)
        modal._name_input.focus()
        for char in ("o", "0", "1", "2", "3", "4", "p", "r", "m", "k", "d"):
            await pilot.press(char)
        await pilot.pause()
        app._open_in_herdr.assert_not_called()
        assert isinstance(pilot.app.screen, LaunchMasterModal)
        await pilot.click("#btn-cancel")
        await pilot.pause()
        assert not isinstance(pilot.app.screen, LaunchMasterModal)

        # Open LaunchErgoModal
        await pilot.press("e")
        ergo_modal = pilot.app.screen
        assert isinstance(ergo_modal, LaunchErgoModal)
        ergo_modal._ws_input.focus()
        for char in ("o", "0", "1", "2", "3", "4", "p", "r", "m", "k", "d"):
            await pilot.press(char)
        await pilot.pause()
        app._open_in_herdr.assert_not_called()
        assert isinstance(pilot.app.screen, LaunchErgoModal)
        await pilot.click("#btn-cancel")
        await pilot.pause()
        assert not isinstance(pilot.app.screen, LaunchErgoModal)
