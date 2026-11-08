"""Regression tests for modal cancellation, nested focus ownership, and measured geometry containment (F6)."""

import os
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Input, Select, Static, TextArea

from herdr_master.core.models import EffortLevel, HarnessKind
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
from aiheaddev.ui.tui.modals.launch_ergo import ErgoLaunchParams, LaunchErgoModal
from aiheaddev.ui.tui.modals.launch_master import LaunchMasterModal, MasterLaunchParams
from aiheaddev.ui.tui.modals.launch_orchestration import (
    LaunchOrchestrationModal,
    OrchestrationLaunchParams,
)
from aiheaddev.ui.tui.modals.project_folder import ProjectFolderPicker
from aiheaddev.ui.tui.modals.prompt_dialog import PromptDialog
from aiheaddev.ui.tui.modals.settings import SettingsModal
from aiheaddev.ui.tui.widgets.workspaces import WorkspaceTarget


class ModalHostApp(App[None]):
    """Generic host app for testing modal lifecycle, cancellation, and callbacks."""

    def __init__(self, modal_factory: Any) -> None:
        super().__init__()
        self.modal_factory = modal_factory
        self.callbacks: List[Any] = []
        self.modal: Any = None

    def on_mount(self) -> None:
        self.modal = self.modal_factory()

        def on_dismiss(result: Any) -> None:
            self.callbacks.append((result, len(self.screen_stack)))

        self.push_screen(self.modal, on_dismiss)


# ==============================================================================
# 1. Callback Matrix and Single-Dismissal Latch (Escape yields False/None, depth 2->1)
# ==============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "modal_cls,expected_cancel",
    [
        ("confirm", False),
        ("prompt", None),
        ("picker", None),
        ("master", None),
        ("ergo", None),
        ("orchestration", None),
    ],
)
async def test_modal_cancellation_callback_matrix(tmp_path: Path, modal_cls: str, expected_cancel: Any):
    """Escape yields False for ConfirmModal and None for the other modals; exactly one callback."""
    orig_cwd = os.getcwd()
    squad = resolve_squad(tmp_path).squad

    def make_modal():
        if modal_cls == "confirm":
            return ConfirmModal("Confirm Action", "Are you sure?")
        elif modal_cls == "prompt":
            return PromptDialog("worker-agent")
        elif modal_cls == "picker":
            return ProjectFolderPicker(tmp_path)
        elif modal_cls == "master":
            return LaunchMasterModal(project_path=tmp_path)
        elif modal_cls == "ergo":
            return LaunchErgoModal(squad, project_path=tmp_path)
        elif modal_cls == "orchestration":
            return LaunchOrchestrationModal(project_path=tmp_path)
        raise ValueError(f"Unknown modal class {modal_cls}")

    app = ModalHostApp(make_modal)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert len(app.screen_stack) == 2
        assert app.screen is app.modal

        # Press Escape to cancel
        await pilot.press("escape")
        await pilot.pause()

        # Stack depth transitions 2 -> 1, modal dismissed
        assert len(app.screen_stack) == 1
        assert len(app.callbacks) == 1
        result, depth_at_call = app.callbacks[0]
        assert result == expected_cancel
        assert depth_at_call == 1

        # Rapid redundant Escapes or cancel actions do not double-invoke callback
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.callbacks) == 1
        assert len(app.screen_stack) == 1

        # Cwd remains unchanged
        assert os.getcwd() == orig_cwd


# ==============================================================================
# 2. Real App Deletion Confirmation Path and PromptDialog Auxiliary
# ==============================================================================

@pytest.mark.asyncio
async def test_app_deletion_confirmation_escape_never_closes_workspace(tmp_path: Path):
    """In real app deletion flow, Escape dismisses ConfirmModal and never calls close_workspace."""
    mock_client = AsyncMock(spec=HerdrClient)
    mock_client.close_workspace = AsyncMock(return_value={"status": "ok"})
    mock_factory = MagicMock(return_value=mock_client)
    app = HerdrMasterApp(demo=False, project_path=tmp_path, live_client_factory=mock_factory)
    app.client = mock_client
    app._schedule_inventory_refresh = MagicMock()

    sock_path = tmp_path / "test-deletion.sock"
    snapshot = SessionSnapshot(
        protocol=22,
        workspaces=[WorkspaceSnapshot(workspace_id="ws-delete-target", label="Delete Target")],
        tabs=[TabSnapshot(tab_id="tab-1", workspace_id="ws-delete-target")],
        panes=[PaneSnapshot(pane_id="pane-1", tab_id="tab-1", workspace_id="ws-delete-target")],
        agents=[
            AgentSnapshot(
                pane_id="pane-1",
                tab_id="tab-1",
                workspace_id="ws-delete-target",
                agent_status="completed",
            )
        ],
    )
    inv = SessionInventory(session_name="test-session", socket_path=sock_path, snapshot=snapshot)
    app.app_state.live_sessions = [inv]

    async with app.run_test(size=(110, 35)) as pilot:
        target = WorkspaceTarget(
            socket_path=sock_path,
            session_name="test-session",
            workspace_id="ws-delete-target",
        )
        await app._delete_workspace_flow(target)
        await pilot.pause()

        # ConfirmModal should be top of stack
        confirm_modal = app.screen
        assert isinstance(confirm_modal, ConfirmModal)
        assert len(app.screen_stack) == 2

        # Press Escape
        await pilot.press("escape")
        await pilot.pause()

        # Dismissed, returned to main screen, mock close_workspace never called
        assert not isinstance(app.screen, ConfirmModal)
        assert len(app.screen_stack) == 1
        mock_client.close_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_prompt_dialog_auxiliary_escape(tmp_path: Path):
    """PromptDialog typed instructions are discarded and dismissed with None on Escape."""
    callbacks = []

    class PromptHostApp(App[None]):
        def on_mount(self) -> None:
            dialog = PromptDialog("worker-lead")
            self.push_screen(dialog, lambda res: callbacks.append(res))

    app = PromptHostApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, PromptDialog)
        prompt = app.screen
        prompt._input.value = "do not execute dangerous command"
        await pilot.pause()

        await pilot.press("escape")
        await pilot.pause()

        assert len(app.screen_stack) == 1
        assert callbacks == [None]


# ==============================================================================
# 3. Expanded Select Matrix (First Escape Closes Dropdown, Second Dismisses Modal)
# ==============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("select_id", ["#master-harness", "#master-effort", "#master-aihead-override"])
async def test_master_select_expanded_two_step_escape(tmp_path: Path, select_id: str):
    """Actual Master harness/effort/aihead Select expanded: first Escape closes dropdown only, second dismisses."""
    callbacks = []

    class MasterHostApp(App[None]):
        def on_mount(self) -> None:
            modal = LaunchMasterModal(project_path=tmp_path)
            self.push_screen(modal, lambda res: callbacks.append(res))

    app = MasterHostApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert len(app.screen_stack) == 2
        modal = app.screen
        select = modal.query_one(select_id, Select)

        select.focus()
        await pilot.pause()
        assert app.focused == select

        # Expand Select
        await pilot.press("enter")
        await pilot.pause()
        assert select.expanded is True

        # First Escape: closes dropdown only
        await pilot.press("escape")
        await pilot.pause()
        assert select.expanded is False
        assert len(app.screen_stack) == 2
        assert len(callbacks) == 0

        # Second Escape: dismisses modal
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert callbacks == [None]


@pytest.mark.asyncio
async def test_ergo_and_orchestration_select_expanded_two_step_escape(tmp_path: Path):
    """Ergo and Orchestration modals also honor 2-step escape for expanded Selects."""
    squad = resolve_squad(tmp_path).squad

    # Ergo
    ergo_callbacks = []

    class ErgoHostApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(LaunchErgoModal(squad, project_path=tmp_path), lambda r: ergo_callbacks.append(r))

    app_ergo = ErgoHostApp()
    async with app_ergo.run_test(size=(110, 35)) as pilot:
        await pilot.pause()
        modal = app_ergo.screen
        select = modal.query_one("#ergo-aihead-override", Select)
        select.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert select.expanded is True

        await pilot.press("escape")
        await pilot.pause()
        assert select.expanded is False
        assert len(app_ergo.screen_stack) == 2
        assert len(ergo_callbacks) == 0

        await pilot.press("escape")
        await pilot.pause()
        assert len(app_ergo.screen_stack) == 1
        assert ergo_callbacks == [None]

    # Orchestration
    orch_callbacks = []

    class OrchHostApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(LaunchOrchestrationModal(project_path=tmp_path), lambda r: orch_callbacks.append(r))

    app_orch = OrchHostApp()
    async with app_orch.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        modal = app_orch.screen
        select = modal.query_one("#orch-aihead-override", Select)
        select.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert select.expanded is True

        await pilot.press("escape")
        await pilot.pause()
        assert select.expanded is False
        assert len(app_orch.screen_stack) == 2
        assert len(orch_callbacks) == 0

        await pilot.press("escape")
        await pilot.pause()
        assert len(app_orch.screen_stack) == 1
        assert orch_callbacks == [None]


@pytest.mark.asyncio
async def test_council_and_settings_escape_regression_controls(tmp_path: Path):
    """Existing Council and Settings modals remain regression controls and cancel cleanly on Escape."""
    # Council
    council_callbacks = []

    class CouncilHostApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(LaunchCouncilModal(project_path=tmp_path), lambda r: council_callbacks.append(r))

    app_c = CouncilHostApp()
    async with app_c.run_test(size=(110, 35)) as pilot:
        await pilot.pause()
        assert isinstance(app_c.screen, LaunchCouncilModal)
        await pilot.press("escape")
        await pilot.pause()
        assert len(app_c.screen_stack) == 1
        assert council_callbacks == [None]

    # Settings
    settings_callbacks = []

    class SettingsHostApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(SettingsModal(project_path=tmp_path), lambda r: settings_callbacks.append(r))

    app_s = SettingsHostApp()
    async with app_s.run_test(size=(110, 35)) as pilot:
        await pilot.pause()
        assert isinstance(app_s.screen, SettingsModal)
        await pilot.press("escape")
        await pilot.pause()
        assert len(app_s.screen_stack) == 1
        assert settings_callbacks == [None]


# ==============================================================================
# 4. Picker Matrix (Nested Picker Cancellation from Input & Tree, Focus & State Restored)
# ==============================================================================

@pytest.mark.asyncio
async def test_nested_picker_cancellation_from_input_and_tree_master(tmp_path: Path):
    """Browse from populated Master form; cancel picker from location and tree separately; preserve all state."""
    app = ModalHostApp(lambda: LaunchMasterModal(project_path=tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        modal: LaunchMasterModal = app.modal

        # Populate parent fields
        modal._name_input.value = "custom-master-name"
        modal._task_input.value = "custom-task-instruction"
        modal._set_model("custom-model-alpha")
        modal._aihead_select.value = "enable"
        orig_folder = modal._project_folder._input.value
        await pilot.pause()

        # --- Subtest A: Open Browse, focus location input, press Escape ---
        browse_btn = modal.query_one("#btn-browse-project", Button)
        await pilot.click("#btn-browse-project")
        await pilot.pause()
        assert len(app.screen_stack) == 3
        assert isinstance(app.screen, ProjectFolderPicker)
        picker: ProjectFolderPicker = app.screen

        # Focus location input
        picker._path.focus()
        await pilot.pause()
        assert app.focused == picker._path

        # Press Escape
        await pilot.press("escape")
        await pilot.pause()

        # Stack returns to 2, picker closed, parent modal unchanged
        assert len(app.screen_stack) == 2
        assert app.screen is modal
        assert app.focused == browse_btn
        assert modal._name_input.value == "custom-master-name"
        assert modal._task_input.value == "custom-task-instruction"
        assert modal._model_input.value == "custom-model-alpha"
        assert modal._aihead_select.value == "enable"
        assert modal._project_folder._input.value == orig_folder

        # --- Subtest B: Open Browse, focus DirectoryTree, press Escape ---
        await pilot.click("#btn-browse-project")
        await pilot.pause()
        assert len(app.screen_stack) == 3
        picker2: ProjectFolderPicker = app.screen

        # Focus FolderTree
        picker2._tree.focus()
        await pilot.pause()
        assert app.focused == picker2._tree

        # Press Escape
        await pilot.press("escape")
        await pilot.pause()

        # Stack returns to 2, picker closed, focus restored to browse button, parent state preserved
        assert len(app.screen_stack) == 2
        assert app.screen is modal
        assert app.focused == browse_btn
        assert modal._name_input.value == "custom-master-name"
        assert modal._task_input.value == "custom-task-instruction"
        assert modal._model_input.value == "custom-model-alpha"
        assert modal._project_folder._input.value == orig_folder

        # --- Subtest C: Press Escape on parent cancels parent modal ---
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert app.callbacks == [(None, 1)]


@pytest.mark.asyncio
async def test_nested_picker_cancellation_ergo_and_orchestration(tmp_path: Path):
    """Browse from populated Ergo and Orchestration forms; tree/input escape preserves all parent state."""
    squad = resolve_squad(tmp_path).squad

    # Ergo Form
    app_ergo = ModalHostApp(lambda: LaunchErgoModal(squad, project_path=tmp_path))
    async with app_ergo.run_test(size=(110, 35)) as pilot:
        await pilot.pause()
        modal: LaunchErgoModal = app_ergo.modal
        modal._intent_input.text = "Multitier pipeline objective\nLine 2 details"
        modal._ws_input.value = "ergo-populated-ws"
        await pilot.pause()

        browse_btn = modal.query_one("#btn-browse-project", Button)
        await pilot.click("#btn-browse-project")
        await pilot.pause()
        assert len(app_ergo.screen_stack) == 3
        picker: ProjectFolderPicker = app_ergo.screen

        picker._tree.focus()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert len(app_ergo.screen_stack) == 2
        assert app_ergo.screen is modal
        assert app_ergo.focused == browse_btn
        assert modal._intent_input.text == "Multitier pipeline objective\nLine 2 details"
        assert modal._ws_input.value == "ergo-populated-ws"

        await pilot.press("escape")
        await pilot.pause()
        assert len(app_ergo.screen_stack) == 1

    # Orchestration Form
    app_orch = ModalHostApp(lambda: LaunchOrchestrationModal(project_path=tmp_path))
    async with app_orch.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        modal_orch: LaunchOrchestrationModal = app_orch.modal
        modal_orch._intent_input.value = "Single harness pipeline goal"
        modal_orch._workspace_label_input.value = "orch-populated-ws"
        await pilot.pause()

        browse_btn = modal_orch.query_one("#btn-browse-project", Button)
        await pilot.click("#btn-browse-project")
        await pilot.pause()
        assert len(app_orch.screen_stack) == 3
        picker_orch: ProjectFolderPicker = app_orch.screen

        picker_orch._path.focus()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert len(app_orch.screen_stack) == 2
        assert app_orch.screen is modal_orch
        assert app_orch.focused == browse_btn
        assert modal_orch._intent_input.value == "Single harness pipeline goal"
        assert modal_orch._workspace_label_input.value == "orch-populated-ws"

        await pilot.press("escape")
        await pilot.pause()
        assert len(app_orch.screen_stack) == 1


# ==============================================================================
# 5. Geometry Matrix Across 120x40, 80x24, 60x24, 40x24
# ==============================================================================

GEOMETRY_CASES = [(120, 40), (80, 24), (60, 24), (40, 24)]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", GEOMETRY_CASES)
async def test_confirm_modal_geometry(tmp_path: Path, size: tuple[int, int]):
    """ConfirmModal dialog containment, action button containment, readable captions, and Enter activation."""
    callbacks = []

    class ConfirmApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(
                ConfirmModal("Delete Workspace?", "Are you sure you want to delete workspace?"),
                lambda r: callbacks.append(r),
            )

    app = ConfirmApp()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        modal: ConfirmModal = app.screen
        box = modal.query_one(".confirm-dialog")
        btn_confirm = modal.query_one("#btn-confirm", Button)
        btn_cancel = modal.query_one("#btn-cancel", Button)

        # Dialog containment
        assert box.region.x >= 0
        assert box.region.right <= size[0]
        assert box.region.y >= 0
        assert box.region.bottom <= size[1]

        # Buttons containment and positive dimensions
        for btn in (btn_confirm, btn_cancel):
            assert btn.region.width > 0
            assert btn.region.height > 0
            assert btn.region.x >= 0
            assert btn.region.right <= size[0]
            assert btn.region.y >= 0
            assert btn.region.bottom <= size[1]
            assert len(str(btn.label).strip()) > 0

        # Captions preserved
        assert "Delete Workspace?" in modal.title_text
        assert "Are you sure" in modal.message_text

        # Keyboard activation: click confirm dismisses with True
        await pilot.click("#btn-confirm")
        await pilot.pause()
        assert callbacks == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", GEOMETRY_CASES)
async def test_prompt_dialog_geometry(tmp_path: Path, size: tuple[int, int]):
    """PromptDialog box containment, action button containment, readable captions, and submit activation."""
    callbacks = []

    class PromptApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(PromptDialog("worker-agent-1"), lambda r: callbacks.append(r))

    app = PromptApp()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        modal: PromptDialog = app.screen
        box = modal.query_one(".prompt-box")
        btn_send = modal.query_one("#btn-send", Button)
        btn_cancel = modal.query_one("#btn-cancel", Button)

        # Box containment
        assert box.region.x >= 0
        assert box.region.right <= size[0]
        assert box.region.y >= 0
        assert box.region.bottom <= size[1]

        # Button containment
        for btn in (btn_send, btn_cancel):
            assert btn.region.width > 0
            assert btn.region.height > 0
            assert btn.region.x >= 0
            assert btn.region.right <= size[0]
            assert btn.region.y >= 0
            assert btn.region.bottom <= size[1]
            assert len(str(btn.label).strip()) > 0

        modal._input.value = "run analysis"
        await pilot.click("#btn-send")
        await pilot.pause()
        assert callbacks == ["run analysis"]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", GEOMETRY_CASES)
async def test_launch_master_geometry(tmp_path: Path, size: tuple[int, int]):
    """LaunchMasterModal box containment, button containment, and launch activation across all geometries."""
    callbacks = []

    class MasterApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(LaunchMasterModal(project_path=tmp_path), lambda r: callbacks.append(r))

    app = MasterApp()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        modal: LaunchMasterModal = app.screen
        box = modal.query_one(".modal-box")
        btn_launch = modal.query_one("#btn-launch", Button)
        btn_cancel = modal.query_one("#btn-cancel", Button)

        # Box containment
        assert box.region.x >= 0
        assert box.region.right <= size[0]
        assert box.region.y >= 0
        assert box.region.bottom <= size[1]

        # Buttons containment and positive dimensions
        for btn in (btn_launch, btn_cancel):
            assert btn.region.width > 0
            assert btn.region.height > 0
            assert btn.region.x >= 0
            assert btn.region.right <= size[0]
            assert btn.region.y >= 0
            assert btn.region.bottom <= size[1]
            assert len(str(btn.label).strip()) > 0

        # Narrow class applied when width < 72
        assert modal.has_class("narrow") == (size[0] < 72)

        # Launch activation
        await pilot.click("#btn-launch")
        await pilot.pause()
        assert len(callbacks) == 1
        assert isinstance(callbacks[0], MasterLaunchParams)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", GEOMETRY_CASES)
async def test_launch_ergo_geometry(tmp_path: Path, size: tuple[int, int]):
    """LaunchErgoModal shell containment, action button containment, and start activation across geometries."""
    callbacks = []
    squad = resolve_squad(tmp_path).squad

    class ErgoApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(LaunchErgoModal(squad, project_path=tmp_path), lambda r: callbacks.append(r))

    app = ErgoApp()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        modal: LaunchErgoModal = app.screen
        shell = modal.query_one(".modal-shell")
        btn_start = modal.query_one("#btn-start-ergo", Button)
        btn_save = modal.query_one("#btn-save-default", Button)
        btn_cancel = modal.query_one("#btn-cancel", Button)

        # Shell containment
        assert shell.region.x >= 0
        assert shell.region.right <= size[0]
        assert shell.region.y >= 0
        assert shell.region.bottom <= size[1]

        # Buttons containment
        for btn in (btn_start, btn_save, btn_cancel):
            assert btn.region.width > 0
            assert btn.region.height > 0
            assert btn.region.x >= 0
            assert btn.region.right <= size[0]
            assert btn.region.y >= 0
            assert btn.region.bottom <= size[1]

        # Activation
        await pilot.click("#btn-start-ergo")
        await pilot.pause()
        assert len(callbacks) == 1
        assert isinstance(callbacks[0], ErgoLaunchParams)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", GEOMETRY_CASES)
async def test_launch_orchestration_geometry(tmp_path: Path, size: tuple[int, int]):
    """LaunchOrchestrationModal box containment, responsive caption, and activation across geometries."""
    callbacks = []

    class OrchApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(
                LaunchOrchestrationModal(project_path=tmp_path, default_intent="Run test pipeline"),
                lambda r: callbacks.append(r),
            )

    app = OrchApp()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        modal: LaunchOrchestrationModal = app.screen
        box = modal.query_one(".modal-box")
        btn_launch = modal.query_one("#btn-launch", Button)
        btn_cancel = modal.query_one("#btn-cancel", Button)

        # Box containment
        assert box.region.x >= 0
        assert box.region.right <= size[0]
        assert box.region.y >= 0
        assert box.region.bottom <= size[1]

        # Buttons containment
        for btn in (btn_launch, btn_cancel):
            assert btn.region.width > 0
            assert btn.region.height > 0
            assert btn.region.x >= 0
            assert btn.region.right <= size[0]
            assert btn.region.y >= 0
            assert btn.region.bottom <= size[1]

        # Caption check
        if size[0] < 72:
            assert str(btn_launch.label) == "Launch"
            assert modal.has_class("narrow")
        else:
            assert str(btn_launch.label) == "Launch Orchestration"
            assert not modal.has_class("narrow")

        # Activation
        await pilot.click("#btn-launch")
        await pilot.pause()
        assert len(callbacks) == 1
        assert isinstance(callbacks[0], OrchestrationLaunchParams)


# ==============================================================================
# 6. Resize 80 -> 40 -> 80 Retains Form State and Restores Wide Chrome
# ==============================================================================

@pytest.mark.asyncio
async def test_orchestration_resize_80_40_80(tmp_path: Path):
    """Resize 80->40->80 toggles caption, retains populated values, and text shortcuts stay text."""
    callbacks = []

    class OrchApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(
                LaunchOrchestrationModal(project_path=tmp_path, default_intent="Initial goal"),
                lambda r: callbacks.append(r),
            )

    app = OrchApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        modal: LaunchOrchestrationModal = app.screen
        btn_launch = modal.query_one("#btn-launch", Button)
        assert str(btn_launch.label) == "Launch Orchestration"
        assert not modal.has_class("narrow")

        # Populate intent and label
        modal._intent_input.value = "Refactor subsystem with subagents"
        modal._workspace_label_input.value = "orch-dynamic-ws"
        await pilot.pause()

        # Resize to 40x24 (narrow)
        await pilot.resize_terminal(40, 24)
        await pilot.pause()
        assert modal.has_class("narrow")
        assert str(btn_launch.label) == "Launch"
        assert modal._intent_input.value == "Refactor subsystem with subagents"
        assert modal._workspace_label_input.value == "orch-dynamic-ws"

        # Letters typed into input remain text (not hotkeys)
        modal._intent_input.focus()
        await pilot.pause()
        modal._intent_input.action_end()
        await pilot.press("s", "p", "a", "c", "e")
        await pilot.pause()
        assert modal._intent_input.value == "Refactor subsystem with subagentsspace"

        # Resize back to 80x24 (wide restored)
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert not modal.has_class("narrow")
        assert str(btn_launch.label) == "Launch Orchestration"
        assert modal._intent_input.value == "Refactor subsystem with subagentsspace"
        assert modal._workspace_label_input.value == "orch-dynamic-ws"

        # Launch produces params with exact populated values
        await pilot.click("#btn-launch")
        await pilot.pause()
        assert len(callbacks) == 1
        params: OrchestrationLaunchParams = callbacks[0]
        assert params.intent == "Refactor subsystem with subagentsspace"
        assert params.workspace_label == "orch-dynamic-ws"


@pytest.mark.asyncio
async def test_master_resize_80_40_80(tmp_path: Path):
    """Master modal resize 80->40->80 retains custom model, task, harness and options."""
    callbacks = []

    class MasterApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(LaunchMasterModal(project_path=tmp_path), lambda r: callbacks.append(r))

    app = MasterApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        modal: LaunchMasterModal = app.screen
        assert not modal.has_class("narrow")

        # Populate custom values
        modal._name_input.value = "master-custom-run"
        modal._task_input.value = "Initial master job description"
        modal._set_model("custom-flash-model")
        modal._aihead_select.value = "disable"
        await pilot.pause()

        # Resize to 40x24
        await pilot.resize_terminal(40, 24)
        await pilot.pause()
        assert modal.has_class("narrow")
        assert modal._name_input.value == "master-custom-run"
        assert modal._task_input.value == "Initial master job description"
        assert modal._model_input.value == "custom-flash-model"
        assert modal._aihead_select.value == "disable"

        # Resize back to 80x24
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert not modal.has_class("narrow")
        assert modal._name_input.value == "master-custom-run"
        assert modal._task_input.value == "Initial master job description"
        assert modal._model_input.value == "custom-flash-model"
        assert modal._aihead_select.value == "disable"

        # Launch and verify parameters
        await pilot.click("#btn-launch")
        await pilot.pause()
        assert len(callbacks) == 1
        params: MasterLaunchParams = callbacks[0]
        assert params.name == "master-custom-run"
        assert params.initial_task == "Initial master job description"
        assert params.model == "custom-flash-model"
        assert params.ai_head_override is False


@pytest.mark.asyncio
async def test_ergo_resize_80_40_80_and_tab_to_start(tmp_path: Path):
    """Ergo modal resize 80->40->80 retains multiline intent, and Tab-to-Start focuses start action."""
    callbacks = []
    squad = resolve_squad(tmp_path).squad

    class ErgoApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(LaunchErgoModal(squad, project_path=tmp_path), lambda r: callbacks.append(r))

    app = ErgoApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        modal: LaunchErgoModal = app.screen

        multiline_text = "Line 1: High priority bugfix\nLine 2: Edge cases covered\nLine 3: Verified"
        modal._intent_input.text = multiline_text
        modal._ws_input.value = "ergo-resize-ws"
        await pilot.pause()

        # Tab-to-Start: focus on intent TextArea, pressing Tab moves focus out
        modal._intent_input.focus()
        await pilot.pause()
        assert app.focused == modal._intent_input
        await pilot.press("tab")
        await pilot.pause()
        # Focus is now on action button bar (#btn-start-ergo)
        assert app.focused == modal.query_one("#btn-start-ergo", Button)

        # Resize to 40x24
        await pilot.resize_terminal(40, 24)
        await pilot.pause()
        assert modal._intent_input.text == multiline_text
        assert modal._ws_input.value == "ergo-resize-ws"

        # Resize back to 80x24
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert modal._intent_input.text == multiline_text
        assert modal._ws_input.value == "ergo-resize-ws"

        # Launch produces ErgoLaunchParams with exact multiline intent
        await pilot.click("#btn-start-ergo")
        await pilot.pause()
        assert len(callbacks) == 1
        params: ErgoLaunchParams = callbacks[0]
        assert params.intent == multiline_text
        assert params.workspace_label == "ergo-resize-ws"
