"""Unit and interaction tests for 5-tab SettingsModal."""

from pathlib import Path
from typing import Optional, Tuple

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static, TabbedContent, TextArea

from herdr_master.core.config import (
    CouncilConfig,
    FavoriteProject,
    HerdrMasterConfig,
    load_project_config,
)
from herdr_master.core.models import Squad
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

        # Verify persisted on disk
        persisted = load_project_config(modal.project_config_path)
        assert persisted.ai_head.enabled is True
        assert persisted.ai_head.soul == "Custom UX Architect Soul"


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
