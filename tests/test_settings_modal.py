"""Unit and interaction tests for 5-tab SettingsModal."""

from pathlib import Path
from typing import Optional, Tuple

import pytest
from rich.console import Console
from textual.app import App, ComposeResult
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TextArea,
)

from herdr_master.core.config import (
    AiHeadConfig,
    CouncilConfig,
    FavoriteProject,
    HerdrMasterConfig,
    load_project_config,
    save_project_config,
)
from herdr_master.core.models import EffortLevel, HarnessKind, Squad
from herdr_master.pipeline.squad import resolve_squad
from aiheaddev.ui.tui.modals.settings import SettingsModal


class SettingsHostApp(App[Optional[Tuple[HerdrMasterConfig, Squad]]]):
    """Test host application for SettingsModal."""

    def __init__(
        self,
        config: Optional[HerdrMasterConfig] = None,
        squad: Optional[Squad] = None,
        project_path: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self.modal = SettingsModal(
            config=config,
            squad=squad,
            project_path=project_path,
        )
        self.result: Optional[Tuple[HerdrMasterConfig, Squad]] = None

    def on_mount(self) -> None:
        def on_dismiss(result: Optional[Tuple[HerdrMasterConfig, Squad]]) -> None:
            self.result = result

        self.push_screen(self.modal, on_dismiss)


@pytest.mark.asyncio
async def test_settings_modal_mount_and_tabs_order(tmp_path):
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()
    app = SettingsHostApp(project_path=project_dir)

    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        modal = app.modal
        assert app.screen is modal

        tabbed = modal.query_one(TabbedContent)
        # Verify 5 tabs in exact requested order
        tab_ids = [pane.id for pane in tabbed.query("TabPane")]
        assert tab_ids == [
            "tab-projects",
            "tab-ergo",
            "tab-orchestration",
            "tab-council",
            "tab-aihead",
        ]

        # Switch across all tabs
        for tid in tab_ids:
            tabbed.active = tid
            await pilot.pause()
            assert tabbed.active == tid


@pytest.mark.asyncio
async def test_settings_modal_favorites_management(tmp_path):
    project_dir = tmp_path / "test_project"
    fav_dir = tmp_path / "fav_workspace"
    project_dir.mkdir()
    fav_dir.mkdir()

    app = SettingsHostApp(project_path=project_dir)

    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        modal = app.modal

        # Tab 1: Projects & Favorites
        name_input = modal.query_one("#favorite-name-input", Input)
        path_input = modal.query_one("#favorite-path-input", Input)

        name_input.value = "my-fav"
        path_input.value = str(fav_dir)

        # Test path button
        modal.query_one("#btn-test-favorite").scroll_visible(immediate=True)
        await pilot.click("#btn-test-favorite")
        status = modal.query_one("#favorite-test-status", Static).content
        assert "Available" in str(status)

        # Add favorite
        modal.query_one("#btn-add-favorite").scroll_visible(immediate=True)
        await pilot.click("#btn-add-favorite")
        assert len(modal._favorites) == 1
        assert modal._favorites[0].name == "my-fav"
        assert modal._favorites[0].path == str(fav_dir)

        # Select row and edit favorite
        table = modal.query_one("#favorites-table", DataTable)
        assert table.row_count == 1
        modal._selected_favorite_idx = 0

        name_input.value = "renamed-fav"
        modal.query_one("#btn-edit-favorite").scroll_visible(immediate=True)
        await pilot.click("#btn-edit-favorite")
        assert modal._favorites[0].name == "renamed-fav"

        # Remove favorite
        modal.query_one("#btn-remove-favorite").scroll_visible(immediate=True)
        await pilot.click("#btn-remove-favorite")
        assert len(modal._favorites) == 0
        assert table.row_count == 0


@pytest.mark.asyncio
async def test_settings_modal_council_validation(tmp_path):
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()
    app = SettingsHostApp(project_path=project_dir)

    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        modal = app.modal

        tabbed = modal.query_one(TabbedContent)
        tabbed.active = "tab-council"
        await pilot.pause()

        # By default, council has 2 members enabled
        assert len(modal._council_members) >= 2
        warning = modal.query_one("#council-warning", Static).content
        assert "valid" in str(warning).lower()

        # Disable all members to trigger warning and block save
        for m in modal._council_members:
            m.enabled = False
        modal._refresh_council_warning()
        warning = modal.query_one("#council-warning", Static).content
        assert "Warning" in str(warning)

        # Try to save - must block
        modal.query_one("#btn-save").scroll_visible(immediate=True)
        await pilot.click("#btn-save")
        await pilot.pause()
        assert app.screen is modal  # Not dismissed
        assert "Council requires at least 2 enabled members" in str(
            modal.query_one("#settings-status", Static).content
        )

        # Re-enable members
        for m in modal._council_members:
            m.enabled = True
        modal._refresh_council_warning()
        warning = modal.query_one("#council-warning", Static).content
        assert "valid" in str(warning).lower()


@pytest.mark.asyncio
async def test_settings_modal_aihead_editing_and_save(tmp_path):
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()
    app = SettingsHostApp(project_path=project_dir)

    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        modal = app.modal

        tabbed = modal.query_one(TabbedContent)
        tabbed.active = "tab-aihead"
        await pilot.pause()

        # Enable AiHead
        modal.query_one("#aihead-enabled", Checkbox).value = True
        modal.query_one("#aihead-soul-input", TextArea).text = "Custom UX Architect Soul"
        modal.query_one("#aihead-harness", Select).value = "claude"
        modal.query_one("#aihead-model", Input).value = "claude-sonnet-4-6"
        modal.query_one("#aihead-effort", Select).value = "high"
        modal.query_one("#aihead-dangerously-skip-permissions", Checkbox).value = False

        # Tab 1: set default folder
        tabbed.active = "tab-projects"
        await pilot.pause()
        modal.query_one("#default-folder-input", Input).value = str(project_dir)

        # Save settings
        modal.query_one("#btn-save").scroll_visible(immediate=True)
        await pilot.click("#btn-save")
        await pilot.pause()

        # Verify dismissed with (config, squad)
        assert app.result is not None
        saved_cfg, saved_squad = app.result
        assert saved_cfg.projects.default_folder == str(project_dir)
        assert saved_cfg.ai_head.enabled is True
        assert saved_cfg.ai_head.soul == "Custom UX Architect Soul"
        assert saved_cfg.ai_head.binding.kind.value == "claude"
        assert saved_cfg.ai_head.binding.model == "claude-sonnet-4-6"
        assert saved_cfg.ai_head.binding.effort == EffortLevel.HIGH
        assert saved_cfg.ai_head.dangerously_skip_permissions is False

        # Verify persisted on disk
        persisted = load_project_config(modal.project_config_path)
        assert persisted.ai_head.enabled is True
        assert persisted.ai_head.soul == "Custom UX Architect Soul"
        assert persisted.ai_head.binding.effort == EffortLevel.HIGH
        assert persisted.ai_head.dangerously_skip_permissions is False


@pytest.mark.asyncio
async def test_settings_modal_cancel_discards(tmp_path):
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()
    app = SettingsHostApp(project_path=project_dir)

    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        modal = app.modal

        # Mutate something in tab 1
        modal.query_one("#default-folder-input", Input).value = "/some/unsaved/path"

        # Cancel
        modal.query_one("#btn-cancel").scroll_visible(immediate=True)
        await pilot.click("#btn-cancel")
        await pilot.pause()

        assert app.result is None
        # Verify file on disk does not exist or was not modified
        config_path = project_dir / ".herdr-master.toml"
        assert not config_path.exists()


@pytest.mark.asyncio
async def test_council_tab_is_compact_and_add_member_visible_at_80x24(tmp_path):
    """Council controls (incl. Add member) fit in the modal viewport without scrolling."""
    app = SettingsHostApp(project_path=tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        modal = app.modal
        modal.query_one(TabbedContent).active = "tab-council"
        await pilot.pause()

        scroll = modal.query_one("#tab-council .settings-tab-scroll")
        screen_region = app.screen.region
        for bid in ("#btn-add-member", "#btn-edit-member", "#btn-remove-member"):
            region = modal.query_one(bid).region
            assert screen_region.contains_region(region), bid
            assert scroll.region.contains_region(region), bid
        assert scroll.scroll_offset.y == 0

        # Save Settings and Cancel buttons are compact 1-row and contained within screen
        save_btn = modal.query_one("#btn-save", Button)
        cancel_btn = modal.query_one("#btn-cancel", Button)
        assert save_btn.region.height == 1
        assert cancel_btn.region.height == 1
        assert screen_region.contains_region(save_btn.region)
        assert screen_region.contains_region(cancel_btn.region)

        # Modal shell fits within screen without vertical overflow
        shell = modal.query_one(".settings-shell")
        assert screen_region.contains_region(shell.region)
        assert shell.region.bottom <= screen_region.height

        table = modal.query_one("#council-members-table", DataTable)
        assert table.region.height <= 7

        # Coordinator / scout: harness + model + effort inline on one row each.
        for role in ("coordinator", "scout"):
            ys = {
                modal.query_one(f"#council-{role}-{part}").region.y
                for part in ("harness", "model", "effort")
            }
            assert len(ys) == 1

        # Add button still works
        before = len(modal._council_members)
        modal.query_one("#council-member-label-input", Input).value = "extra-member"
        await pilot.click("#btn-add-member")
        await pilot.pause()
        assert len(modal._council_members) == before + 1


@pytest.mark.asyncio
async def test_council_effort_saved_and_orchestration_efforts_saved(tmp_path):
    app = SettingsHostApp(project_path=tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        modal = app.modal
        modal.query_one("#council-coordinator-effort", Select).value = "high"
        editor = modal.orchestration_editor
        editor.query_one("#orch-architect-effort", Select).value = "xhigh"
        modal._validate_and_save()
        await pilot.pause()
    cfg, _ = app.result
    assert cfg.council.coordinator.effort.value == "high"
    assert cfg.council.scout.effort.value == "medium"  # default preserved
    assert cfg.orchestration.architect_effort == "xhigh"
    reloaded = load_project_config(tmp_path / ".herdr-master.toml")
    assert reloaded.orchestration.architect_effort == "xhigh"


@pytest.mark.asyncio
async def test_aihead_configuration_preservation_regression(tmp_path):
    """Regression test: saving persona edits must not wipe unexposed or extra fields.

    Seeds supported non-default values for lesson_paths, autonomy, brief_before_action,
    close_foreign_tabs, split, mode, non-default binding label, binding description,
    binding effort, and dangerously_skip_permissions. Verifies that saving persona edits
    overlays only intended fields while leaving all other fields preserved in both
    app.result and disk reload, and leaves caller config untouched.
    """
    project_dir = tmp_path / "test_preservation"
    project_dir.mkdir()

    cfg = HerdrMasterConfig()
    cfg.ai_head.lesson_paths = ["/opt/lessons/ai", "/custom/lessons"]
    cfg.ai_head.autonomy = "ask"
    cfg.ai_head.brief_before_action = False
    cfg.ai_head.close_foreign_tabs = True
    cfg.ai_head.split = False
    cfg.ai_head.mode = "cli"
    cfg.ai_head.binding.label = "custom-head-label"
    cfg.ai_head.binding.description = "custom head description"
    cfg.ai_head.binding.effort = EffortLevel.LOW
    setattr(cfg.ai_head, "dangerously_skip_permissions", False)

    orig_snapshot = cfg.ai_head.model_dump()

    app = SettingsHostApp(config=cfg, project_path=project_dir)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        modal = app.modal

        tabbed = modal.query_one(TabbedContent)
        tabbed.active = "tab-aihead"
        await pilot.pause()

        # Verify initial rendering reflects existing effort and permissions
        assert modal.query_one("#aihead-effort", Select).value == "low"
        assert modal.query_one("#aihead-dangerously-skip-permissions", Checkbox).value is False

        # Edit ordinary persona inputs while leaving the two new controls unchanged
        modal.query_one("#aihead-enabled", Checkbox).value = True
        modal.query_one("#aihead-soul-input", TextArea).text = "Updated Strategic Architect Soul"
        modal.query_one("#aihead-harness", Select).value = "opencode"
        modal.query_one("#aihead-model", Input).value = "deepseek-coder-v2"
        modal.query_one("#aihead-cwd-input", Input).value = str(project_dir / "work")

        modal.query_one("#btn-save").scroll_visible(immediate=True)
        await pilot.click("#btn-save")
        await pilot.pause()

    assert app.result is not None
    saved_cfg, _ = app.result

    # Construct expected dump with only intended edits overlaid
    expected_dump = dict(orig_snapshot)
    expected_dump["enabled"] = True
    expected_dump["soul"] = "Updated Strategic Architect Soul"
    expected_dump["cwd"] = str(project_dir / "work")
    expected_binding = dict(orig_snapshot["binding"])
    expected_binding["kind"] = HarnessKind.OPENCODE
    expected_binding["model"] = "deepseek-coder-v2"
    expected_dump["binding"] = expected_binding

    # Assert whole saved AiHead dump matches expected dump in app.result
    assert saved_cfg.ai_head.model_dump() == expected_dump

    # Assert whole reloaded AiHead dump matches expected dump from disk
    reloaded = load_project_config(modal.project_config_path)
    assert reloaded.ai_head.model_dump() == expected_dump

    # Assert caller's original configuration was not mutated
    assert cfg.ai_head.model_dump() == orig_snapshot


@pytest.mark.parametrize(
    "initial_effort, target_effort_str, expected_effort",
    [
        (None, "none", None),
        (None, "low", EffortLevel.LOW),
        (EffortLevel.LOW, "medium", EffortLevel.MEDIUM),
        (EffortLevel.MEDIUM, "high", EffortLevel.HIGH),
        (EffortLevel.HIGH, "max", EffortLevel.MAX),
        (EffortLevel.MAX, "none", None),
    ],
)
@pytest.mark.asyncio
async def test_aihead_effort_initial_selection_and_saving(
    tmp_path, initial_effort, target_effort_str, expected_effort
):
    """Cover initial selection and saving effort values across None and EffortLevel members."""
    project_dir = tmp_path / "test_effort"
    project_dir.mkdir()

    cfg = HerdrMasterConfig()
    cfg.ai_head.binding.effort = initial_effort
    save_project_config(cfg, path=project_dir / ".herdr-master.toml")

    expected_initial_widget = initial_effort.value if initial_effort is not None else "none"

    app = SettingsHostApp(config=cfg, project_path=project_dir)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        modal = app.modal

        modal.query_one(TabbedContent).active = "tab-aihead"
        await pilot.pause()

        effort_select = modal.query_one("#aihead-effort", Select)
        assert effort_select.value == expected_initial_widget

        effort_select.value = target_effort_str
        modal._validate_and_save()
        await pilot.pause()

    assert app.result is not None
    saved_cfg, _ = app.result
    assert saved_cfg.ai_head.binding.effort == expected_effort

    reloaded = load_project_config(modal.project_config_path)
    assert reloaded.ai_head.binding.effort == expected_effort


@pytest.mark.asyncio
async def test_aihead_dangerously_skip_permissions_explicit_and_absent(tmp_path):
    """Cover explicit True/False permissions, edits in both directions, and legacy absent attribute."""
    # 1. Explicit True -> toggle to False
    dir_true = tmp_path / "perms_true"
    dir_true.mkdir()
    cfg_true = HerdrMasterConfig()
    setattr(cfg_true.ai_head, "dangerously_skip_permissions", True)
    save_project_config(cfg_true, path=dir_true / ".herdr-master.toml")
    app_true = SettingsHostApp(config=cfg_true, project_path=dir_true)
    async with app_true.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        modal = app_true.modal
        modal.query_one(TabbedContent).active = "tab-aihead"
        await pilot.pause()
        cb = modal.query_one("#aihead-dangerously-skip-permissions", Checkbox)
        assert cb.value is True
        cb.value = False
        modal._validate_and_save()
        await pilot.pause()
    saved_true, _ = app_true.result
    assert saved_true.ai_head.dangerously_skip_permissions is False
    assert load_project_config(modal.project_config_path).ai_head.dangerously_skip_permissions is False

    # 2. Explicit False -> toggle to True
    dir_false = tmp_path / "perms_false"
    dir_false.mkdir()
    cfg_false = HerdrMasterConfig()
    setattr(cfg_false.ai_head, "dangerously_skip_permissions", False)
    save_project_config(cfg_false, path=dir_false / ".herdr-master.toml")
    app_false = SettingsHostApp(config=cfg_false, project_path=dir_false)
    async with app_false.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        modal = app_false.modal
        modal.query_one(TabbedContent).active = "tab-aihead"
        await pilot.pause()
        cb = modal.query_one("#aihead-dangerously-skip-permissions", Checkbox)
        assert cb.value is False
        cb.value = True
        modal._validate_and_save()
        await pilot.pause()
    saved_false, _ = app_false.result
    assert saved_false.ai_head.dangerously_skip_permissions is True
    assert load_project_config(modal.project_config_path).ai_head.dangerously_skip_permissions is True

    # 3. Genuinely absent attribute (older model simulation) -> renders checked (True) and saves True
    dir_absent_1 = tmp_path / "perms_absent_1"
    dir_absent_1.mkdir()
    cfg_absent_1 = HerdrMasterConfig()
    if hasattr(cfg_absent_1.ai_head, "dangerously_skip_permissions"):
        delattr(cfg_absent_1.ai_head, "dangerously_skip_permissions")
    assert hasattr(cfg_absent_1.ai_head, "dangerously_skip_permissions") is False
    save_project_config(cfg_absent_1, path=dir_absent_1 / ".herdr-master.toml")

    app_absent_1 = SettingsHostApp(config=cfg_absent_1, project_path=dir_absent_1)
    async with app_absent_1.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        modal = app_absent_1.modal
        modal.query_one(TabbedContent).active = "tab-aihead"
        await pilot.pause()
        cb = modal.query_one("#aihead-dangerously-skip-permissions", Checkbox)
        assert cb.value is True
        modal._validate_and_save()
        await pilot.pause()
    saved_absent_1, _ = app_absent_1.result
    assert saved_absent_1.ai_head.dangerously_skip_permissions is True
    assert load_project_config(modal.project_config_path).ai_head.dangerously_skip_permissions is True

    # 4. Genuinely absent attribute -> uncheck to False -> saves False
    dir_absent_2 = tmp_path / "perms_absent_2"
    dir_absent_2.mkdir()
    cfg_absent_2 = HerdrMasterConfig()
    if hasattr(cfg_absent_2.ai_head, "dangerously_skip_permissions"):
        delattr(cfg_absent_2.ai_head, "dangerously_skip_permissions")
    assert hasattr(cfg_absent_2.ai_head, "dangerously_skip_permissions") is False
    save_project_config(cfg_absent_2, path=dir_absent_2 / ".herdr-master.toml")

    app_absent_2 = SettingsHostApp(config=cfg_absent_2, project_path=dir_absent_2)
    async with app_absent_2.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        modal = app_absent_2.modal
        modal.query_one(TabbedContent).active = "tab-aihead"
        await pilot.pause()
        cb = modal.query_one("#aihead-dangerously-skip-permissions", Checkbox)
        assert cb.value is True
        cb.value = False
        modal._validate_and_save()
        await pilot.pause()
    saved_absent_2, _ = app_absent_2.result
    assert saved_absent_2.ai_head.dangerously_skip_permissions is False
    assert load_project_config(modal.project_config_path).ai_head.dangerously_skip_permissions is False


@pytest.mark.asyncio
async def test_aihead_persona_tab_at_80x24_scroll_labels_and_keyboard(tmp_path):
    """Verify 80x24 layout, scrolling to new controls, readable labels, and keyboard interaction."""
    app = SettingsHostApp(project_path=tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        modal = app.modal
        modal.query_one(TabbedContent).active = "tab-aihead"
        await pilot.pause()

        tab_scroll = modal.query_one("#tab-aihead .settings-tab-scroll")

        # Verify Effort / thinking label is readable and laid out with full width
        effort_label = modal.query_one("#aihead-effort-label", Label)
        assert str(effort_label.render()) == "Effort / thinking"
        assert effort_label.size.width >= len("Effort / thinking")
        console = Console(width=effort_label.size.width)
        with console.capture() as capture:
            console.print(effort_label.render())
        assert "thinking" in capture.get()

        # Verify Dangerously skip permissions checkbox label is readable
        cb = modal.query_one("#aihead-dangerously-skip-permissions", Checkbox)
        assert "Dangerously skip permissions" in str(cb.label)

        # Focus effort select and verify scrolled into view and changed
        effort_select = modal.query_one("#aihead-effort", Select)
        effort_select.focus()
        await pilot.pause()
        assert modal.focused is effort_select
        assert tab_scroll.region.contains_region(effort_select.region)
        effort_select.value = "low"
        await pilot.pause()
        assert effort_select.value == "low"

        # Focus checkbox and verify scrolled into view and toggled
        cb.focus()
        await pilot.pause()
        assert modal.focused is cb
        assert tab_scroll.region.contains_region(cb.region)
        val_before = cb.value
        await pilot.press("space")
        await pilot.pause()
        assert cb.value is not val_before

        modal._validate_and_save()
        await pilot.pause()

    cfg, _ = app.result
    assert cfg.ai_head.binding.effort == EffortLevel.LOW
    assert cfg.ai_head.dangerously_skip_permissions == (not val_before)


@pytest.mark.asyncio
async def test_aihead_cwd_tilde_expansion_on_save(tmp_path):
    """Verify ~/... in cwd input is expanded on save via model validation."""
    project_dir = tmp_path / "test_tilde"
    project_dir.mkdir()

    cfg = HerdrMasterConfig()
    save_project_config(cfg, path=project_dir / ".herdr-master.toml")

    app = SettingsHostApp(config=cfg, project_path=project_dir)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        modal = app.modal

        tabbed = modal.query_one(TabbedContent)
        tabbed.active = "tab-aihead"
        await pilot.pause()

        modal.query_one("#aihead-cwd-input", Input).value = "~/aihead-tilde-probe"

        modal.query_one("#btn-save").scroll_visible(immediate=True)
        await pilot.click("#btn-save")
        await pilot.pause()

    assert app.result is not None
    saved_cfg, _ = app.result
    expected_cwd = AiHeadConfig(cwd="~/aihead-tilde-probe").cwd
    assert saved_cfg.ai_head.cwd == expected_cwd

    reloaded = load_project_config(modal.project_config_path)
    assert reloaded.ai_head.cwd == expected_cwd


@pytest.mark.asyncio
async def test_aihead_unroutable_binding_fails_validation_and_does_not_save(tmp_path):
    """Verify unroutable binding (agy + pro) fails schema validation on save and does not dismiss or write."""
    project_dir = tmp_path / "test_unroutable"
    project_dir.mkdir()

    cfg = HerdrMasterConfig()
    save_project_config(cfg, path=project_dir / ".herdr-master.toml")
    caller_snapshot = cfg.ai_head.model_dump()
    initial_toml_content = (project_dir / ".herdr-master.toml").read_text()

    app = SettingsHostApp(config=cfg, project_path=project_dir)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        modal = app.modal

        tabbed = modal.query_one(TabbedContent)
        tabbed.active = "tab-aihead"
        await pilot.pause()

        # Non-blank soul and enabled AiHead, set unroutable agy + pro
        modal.query_one("#aihead-enabled", Checkbox).value = True
        modal.query_one("#aihead-soul-input", TextArea).text = "Custom strategic soul"
        modal.query_one("#aihead-harness", Select).value = "agy"
        modal.query_one("#aihead-model", Input).value = "pro"

        modal.query_one("#btn-save").scroll_visible(immediate=True)
        await pilot.click("#btn-save")
        await pilot.pause()

        # Assert the modal is not dismissed
        assert app.screen is modal
        assert app.result is None

        # Assert #settings-status contains "AiHead validation error"
        status_text = str(modal.query_one("#settings-status", Static).content)
        assert "AiHead validation error" in status_text

    # Assert caller's model_dump() is unchanged
    assert cfg.ai_head.model_dump() == caller_snapshot

    # Assert project toml was not written with that binding
    current_toml_content = (project_dir / ".herdr-master.toml").read_text()
    assert current_toml_content == initial_toml_content
    reloaded = load_project_config(project_dir / ".herdr-master.toml")
    assert not (reloaded.ai_head.binding.kind.value == "agy" and reloaded.ai_head.binding.model == "pro")
