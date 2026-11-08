"""Tests for Settings recoverable save, persistent destination, and feedback (F7).

Validates:
- Real Save-button handler with injected read-only OSError: no app exception,
  same mounted modal/tab, edits retained, persistent destination unchanged,
  no callback/success toast/write, exactly one attempt after idle.
- Explicit retry on second Save: exactly one additional attempt, one result
  callback/dismissal, reload exact target and verify persisted values.
- Target scopes: seeded local, new-local display, and sandboxed inherited target.
  Inherited save updates ancestor target only, creating no child override.
- Saves/reloads architect effort xhigh and retains Council effort settings.
- Cancel before save and after failed save preserves caller config/squad and bytes.
- Validation blocks save, selects invalid tab, does not write.
- Field remains present across all tabs and validation/error statuses.
- Geometry matrix (120x40, 80x24, 60x24, 40x24): containment, positive content,
  40-column keyboard inspection to end of long destination.
- Literal brackets in path and error preserved safely without markup stripping.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional, Tuple

import pytest
from rich.text import Text
from textual.app import App
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Select,
    Static,
    TabbedContent,
    TextArea,
)

from herdr_master.core.config import (
    CouncilConfig,
    FavoriteProject,
    HerdrMasterConfig,
    find_project_config_path,
    load_project_config,
    save_project_config,
)
from herdr_master.core.models import EffortLevel, Squad
from aiheaddev.ui.tui.modals.settings import SettingsModal
from tests.ui_sandbox import require_guard

require_guard()


class SettingsFeedbackHostApp(App[Optional[Tuple[HerdrMasterConfig, Squad]]]):
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
        self.dismiss_count: int = 0

    def on_mount(self) -> None:
        def on_dismiss(result: Optional[Tuple[HerdrMasterConfig, Squad]]) -> None:
            self.dismiss_count += 1
            self.result = result

        self.push_screen(self.modal, on_dismiss)


@pytest.mark.asyncio
async def test_settings_save_injected_oserror_recovery_and_explicit_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Test real Save handler with injected OSError, no auto-retry, and explicit retry success."""
    proj_dir = tmp_path / "oserror_proj"
    proj_dir.mkdir()
    config_file = proj_dir / ".herdr-master.toml"
    initial_cfg = HerdrMasterConfig()
    save_project_config(initial_cfg, path=config_file)

    call_count = 0
    fail_save = True
    real_save = save_project_config

    def patched_save(config, path=None):
        nonlocal call_count
        call_count += 1
        if fail_save:
            raise OSError(30, "Read-only file system", str(path))
        return real_save(config, path=path)

    monkeypatch.setattr(
        "aiheaddev.ui.tui.modals.settings.save_project_config", patched_save
    )

    app = SettingsFeedbackHostApp(project_path=proj_dir)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        modal = app.modal
        assert app.screen is modal

        # Tab 1: modify default folder
        folder_input = modal.query_one("#default-folder-input", Input)
        folder_input.value = "/custom/recovered/workspace"

        # Tab 5: modify AiHead soul
        tabbed = modal.query_one(TabbedContent)
        tabbed.active = "tab-aihead"
        await pilot.pause()
        soul_input = modal.query_one("#aihead-soul-input", TextArea)
        soul_input.text = "Recoverable Save Soul Prompt"

        # Check destination widget before save attempt
        dest_widget = modal.query_one("#settings-destination", TextArea)
        dest_text_before = dest_widget.text
        assert "local" in dest_text_before
        assert str(config_file) in dest_text_before

        # Click Save - fails with OSError
        btn_save = modal.query_one("#btn-save", Button)
        btn_save.scroll_visible(immediate=True)
        await pilot.click("#btn-save")
        await pilot.pause()

        # Modal must NOT be dismissed
        assert app.screen is modal
        assert app.dismiss_count == 0
        assert app.result is None

        # Verify active tab is preserved (remains tab-aihead)
        assert tabbed.active == "tab-aihead"

        # Verify edited fields are preserved
        assert soul_input.text == "Recoverable Save Soul Prompt"
        tabbed.active = "tab-projects"
        await pilot.pause()
        assert folder_input.value == "/custom/recovered/workspace"

        # Verify destination widget is unchanged
        assert dest_widget.text == dest_text_before

        # Verify status message shows readable reason first, retry/Cancel guidance, and destination
        status_widget = modal.query_one("#settings-status", Static)
        status_content = str(status_widget.content)
        assert status_content.startswith("Save failed:")
        assert "Read-only file system" in status_content
        assert "Retry with Save or press Cancel." in status_content
        assert str(config_file) in status_content

        # Verify exactly one save attempt was made
        assert call_count == 1

        # Wait idle and verify no automatic retry was scheduled
        await pilot.pause()
        await pilot.pause()
        assert call_count == 1

        # Now remove failing boundary
        fail_save = False

        # Explicitly press Save again
        await pilot.click("#btn-save")
        await pilot.pause()

        # Exactly one additional attempt was made
        assert call_count == 2

        # Modal is now dismissed with result
        assert app.dismiss_count == 1
        assert app.result is not None
        saved_cfg, saved_squad = app.result
        assert saved_cfg.projects.default_folder == "/custom/recovered/workspace"
        assert saved_cfg.ai_head.soul == "Recoverable Save Soul Prompt"

        # Verify file on disk was written with changes
        persisted = load_project_config(config_file)
        assert persisted.projects.default_folder == "/custom/recovered/workspace"
        assert persisted.ai_head.soul == "Recoverable Save Soul Prompt"


@pytest.mark.asyncio
async def test_settings_cancel_preserves_bytes_before_and_after_failed_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Cancel before saving or after failed Save leaves config, squad, and file bytes unchanged."""
    proj_dir = tmp_path / "cancel_proj"
    proj_dir.mkdir()
    cfg_file = proj_dir / ".herdr-master.toml"
    initial_cfg = HerdrMasterConfig()
    initial_cfg.projects.default_folder = "/initial/original/folder"
    save_project_config(initial_cfg, path=cfg_file)
    original_bytes = cfg_file.read_bytes()

    # Case 1: Cancel before Save
    app1 = SettingsFeedbackHostApp(project_path=proj_dir)
    async with app1.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        modal = app1.modal
        modal.query_one("#default-folder-input", Input).value = "/mutated/unsaved/path"
        await pilot.click("#btn-cancel")
        await pilot.pause()

        assert app1.dismiss_count == 1
        assert app1.result is None
        assert cfg_file.read_bytes() == original_bytes

    # Case 2: Cancel after failed Save
    def failing_save(cfg, path=None):
        raise OSError(13, "Permission denied", str(path))

    monkeypatch.setattr(
        "aiheaddev.ui.tui.modals.settings.save_project_config", failing_save
    )

    app2 = SettingsFeedbackHostApp(project_path=proj_dir)
    async with app2.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        modal = app2.modal
        modal.query_one("#default-folder-input", Input).value = "/another/unsaved/path"

        # Attempt Save -> fails
        await pilot.click("#btn-save")
        await pilot.pause()
        assert app2.result is None
        assert "Save failed:" in str(
            modal.query_one("#settings-status", Static).content
        )

        # Cancel after failure
        await pilot.click("#btn-cancel")
        await pilot.pause()

        assert app2.dismiss_count == 1
        assert app2.result is None
        # Bytes on disk remain completely unmodified
        assert cfg_file.read_bytes() == original_bytes


@pytest.mark.asyncio
async def test_settings_validation_blocks_save_and_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Validation failure selects invalid tab, displays status, and does not attempt write."""
    proj_dir = tmp_path / "valid_test_proj"
    proj_dir.mkdir()
    cfg_file = proj_dir / ".herdr-master.toml"
    save_project_config(HerdrMasterConfig(), path=cfg_file)
    original_bytes = cfg_file.read_bytes()

    write_attempted = False

    def spy_save(cfg, path=None):
        nonlocal write_attempted
        write_attempted = True

    monkeypatch.setattr(
        "aiheaddev.ui.tui.modals.settings.save_project_config", spy_save
    )

    app = SettingsFeedbackHostApp(project_path=proj_dir)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        modal = app.modal

        # Trigger Council validation failure by disabling all members
        tabbed = modal.query_one(TabbedContent)
        tabbed.active = "tab-council"
        await pilot.pause()
        for m in modal._council_members:
            m.enabled = False
        modal._refresh_council_warning()

        # Switch to Projects tab
        tabbed.active = "tab-projects"
        await pilot.pause()

        # Click save
        await pilot.click("#btn-save")
        await pilot.pause()

        # Must select invalid tab (tab-council) and block write
        assert tabbed.active == "tab-council"
        assert not write_attempted
        assert app.result is None
        assert cfg_file.read_bytes() == original_bytes
        status = str(modal.query_one("#settings-status", Static).content)
        assert "Council requires at least 2 enabled members" in status


@pytest.mark.asyncio
async def test_settings_inherited_target_no_child_override(
    ui_inherited_project: Tuple[Path, Path, Path]
):
    """Inherited target updates ancestor config only; creates no child override file."""
    parent, child, config_path = ui_inherited_project
    app = SettingsFeedbackHostApp(project_path=child)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        modal = app.modal

        # Verify destination widget shows inherited scope and points to parent config_path
        dest_widget = modal.query_one("#settings-destination", TextArea)
        assert "inherited" in dest_widget.text
        assert str(config_path) in dest_widget.text

        # Change architect effort to xhigh in Orchestration Squad tab
        modal.query_one(TabbedContent).active = "tab-orchestration"
        await pilot.pause()
        orch_editor = modal.orchestration_editor
        orch_editor.query_one("#orch-architect-effort", Select).value = "xhigh"

        # Set Council coordinator effort to high in Council tab
        modal.query_one(TabbedContent).active = "tab-council"
        await pilot.pause()
        modal.query_one("#council-coordinator-effort", Select).value = "high"

        # Save settings
        await pilot.click("#btn-save")
        await pilot.pause()

        assert app.result is not None
        saved_cfg, _ = app.result
        assert saved_cfg.orchestration.architect_effort == "xhigh"
        assert saved_cfg.council.coordinator.effort == EffortLevel.HIGH

        # Verify child directory has NO .herdr-master.toml (no child override created)
        assert not (child / ".herdr-master.toml").exists()

        # Verify ancestor config_path has been updated with xhigh architect effort and high coordinator effort
        reloaded = load_project_config(config_path)
        assert reloaded.orchestration.architect_effort == "xhigh"
        assert reloaded.council.coordinator.effort == EffortLevel.HIGH


@pytest.mark.asyncio
async def test_settings_target_scope_display_variants(
    ui_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Exercise seeded local and new-local target display scopes."""
    # Seeded local
    app_local = SettingsFeedbackHostApp(project_path=ui_project)
    async with app_local.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        dest_widget = app_local.modal.query_one("#settings-destination", TextArea)
        assert "Scope: local" in dest_widget.text
        assert str(ui_project / ".herdr-master.toml") in dest_widget.text

    # New-local: directory without existing config or ancestor config
    new_dir = tmp_path / "new_standalone_dir"
    new_dir.mkdir()
    monkeypatch.setattr(
        "aiheaddev.ui.tui.modals.settings.find_project_config_path",
        lambda p: None,
    )
    app_new = SettingsFeedbackHostApp(project_path=new_dir)
    async with app_new.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        dest_widget = app_new.modal.query_one("#settings-destination", TextArea)
        assert "Scope: new-local" in dest_widget.text
        assert str(new_dir / ".herdr-master.toml") in dest_widget.text


@pytest.mark.asyncio
async def test_settings_destination_presence_across_tabs_and_statuses(
    ui_project: Path,
):
    """Destination field remains present and identical across all five tabs and status messages."""
    app = SettingsFeedbackHostApp(project_path=ui_project)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        modal = app.modal
        dest = modal.query_one("#settings-destination", TextArea)
        expected_text = dest.text
        assert "Scope: local" in expected_text

        tabbed = modal.query_one(TabbedContent)
        for tid in [
            "tab-projects",
            "tab-ergo",
            "tab-orchestration",
            "tab-council",
            "tab-aihead",
        ]:
            tabbed.active = tid
            await pilot.pause()
            curr_dest = modal.query_one("#settings-destination", TextArea)
            assert curr_dest.text == expected_text
            assert curr_dest.region.height == 2

        # Invalidate council to produce validation status
        tabbed.active = "tab-council"
        await pilot.pause()
        for m in modal._council_members:
            m.enabled = False
        modal._refresh_council_warning()

        await pilot.click("#btn-save")
        await pilot.pause()

        # Destination persists unchanged while status is active
        assert modal.query_one("#settings-destination", TextArea).text == expected_text
        assert "Council requires at least 2 enabled members" in str(
            modal.query_one("#settings-status", Static).content
        )


@pytest.mark.asyncio
async def test_settings_destination_literal_brackets_in_path_and_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Literal brackets in target paths and error reasons are preserved without markup stripping."""
    proj_dir = tmp_path / "bracket_[demo]_dir"
    proj_dir.mkdir()
    cfg_file = proj_dir / ".herdr-master.toml"
    save_project_config(HerdrMasterConfig(), path=cfg_file)

    def bracket_err_save(cfg, path=None):
        raise OSError(13, "Permission denied", f"{path}/[restricted_tag]")

    monkeypatch.setattr(
        "aiheaddev.ui.tui.modals.settings.save_project_config", bracket_err_save
    )

    app = SettingsFeedbackHostApp(project_path=proj_dir)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        modal = app.modal

        # Verify destination text preserves [demo] brackets literally
        dest = modal.query_one("#settings-destination", TextArea)
        assert "[demo]" in dest.text

        await pilot.click("#btn-save")
        await pilot.pause()

        status_widget = modal.query_one("#settings-status", Static)
        status_text = str(status_widget.content)
        # Readable reason first
        assert status_text.startswith("Save failed:")
        assert "[restricted_tag]" in status_text
        assert "Retry with Save or press Cancel." in status_text

        # Verify rendered content preserves brackets and does not parse as Rich markup tags
        rendered_str = str(status_widget.render())
        assert "[restricted_tag]" in rendered_str


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 40), (80, 24), (60, 24), (40, 24)])
async def test_settings_layout_containment_and_keyboard_action_across_geometries(
    ui_project: Path, size: Tuple[int, int]
):
    """All 4 geometries (120x40, 80x24, 60x24, 40x24) maintain positive layout, containment, and activation."""
    app = SettingsFeedbackHostApp(project_path=ui_project)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        modal = app.modal
        screen_r = app.screen.region
        shell = modal.query_one(".settings-shell")
        shell_r = shell.region

        # Shell region bounds
        assert shell_r.x >= 0 and shell_r.y >= 0
        assert shell_r.width > 0 and shell_r.height > 0
        assert screen_r.contains_region(shell_r)

        # Destination widget region bounds
        dest = modal.query_one("#settings-destination", TextArea)
        dest_r = dest.region
        assert dest_r.x >= 0 and dest_r.y >= 0
        assert dest_r.width > 0 and dest_r.height == 2
        assert shell_r.contains_region(dest_r)

        # Tabbed content region bounds
        tabbed = modal.query_one(TabbedContent)
        assert tabbed.region.width > 0 and tabbed.region.height > 0
        assert shell_r.contains_region(tabbed.region)

        # Action buttons Save and Cancel
        btn_save = modal.query_one("#btn-save", Button)
        btn_cancel = modal.query_one("#btn-cancel", Button)
        assert btn_save.region.width > 0 and btn_save.region.height > 0
        assert btn_cancel.region.width > 0 and btn_cancel.region.height > 0
        assert shell_r.contains_region(btn_save.region)
        assert shell_r.contains_region(btn_cancel.region)
        assert screen_r.contains_region(btn_save.region)
        assert screen_r.contains_region(btn_cancel.region)

        # Rendered labels
        assert "Save Settings" in str(btn_save.label)
        assert "Cancel" in str(btn_cancel.label)

        # Keyboard activation: focus Cancel button and press Enter
        btn_cancel.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.result is None
        assert app.dismiss_count == 1


@pytest.mark.asyncio
async def test_settings_40_columns_horizontal_inspection(tmp_path: Path):
    """At 40 columns, initial tab focus is preserved and destination is keyboard reachable and inspectable."""
    deep_dir = (
        tmp_path
        / "deep"
        / "nested"
        / "directory"
        / "structure"
        / "that"
        / "is"
        / "quite"
        / "long"
        / "for"
        / "testing"
        / "forty"
        / "columns"
    )
    deep_dir.mkdir(parents=True)
    cfg_file = deep_dir / ".herdr-master.toml"
    save_project_config(HerdrMasterConfig(), path=cfg_file)

    app = SettingsFeedbackHostApp(project_path=deep_dir)
    async with app.run_test(size=(40, 24)) as pilot:
        await pilot.pause()
        modal = app.modal

        # Tab strip initially focused
        assert app.focused.__class__.__name__ in ("ContentTabs", "Tabs")

        dest = modal.query_one("#settings-destination", TextArea)
        # Reach via keyboard (Shift+Tab from ContentTabs reaches destination)
        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.focused is dest

        # Line 0 is scope, line 1 is destination path
        assert dest.scroll_offset.x == 0
        # Navigate to line 1 and press end to scroll horizontally to end of long destination
        await pilot.press("down")
        await pilot.press("end")
        await pilot.pause()

        # Destination line has scrolled horizontally beyond 40 columns
        assert dest.cursor_location[0] == 1
        assert dest.cursor_location[1] > 40
        assert dest.scroll_offset.x > 0
