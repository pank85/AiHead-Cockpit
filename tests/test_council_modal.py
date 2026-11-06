"""Unit and interaction tests for LaunchCouncilModal."""

from pathlib import Path
from typing import Optional

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, Select, Static, TextArea

from herdr_master.core.config import CouncilConfig, HerdrMasterConfig
from aiheaddev.ui.tui.modals.launch_council import CouncilLaunchParams, LaunchCouncilModal
from aiheaddev.ui.tui.modals.settings import SettingsModal


class CouncilHostApp(App[Optional[CouncilLaunchParams]]):
    """Test host application for LaunchCouncilModal."""

    def __init__(
        self,
        config: Optional[HerdrMasterConfig] = None,
        project_path: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self.modal = LaunchCouncilModal(
            config=config,
            project_path=project_path,
        )
        self.result: Optional[CouncilLaunchParams] = None

    def on_mount(self) -> None:
        def on_dismiss(result: Optional[CouncilLaunchParams]) -> None:
            self.result = result

        self.push_screen(self.modal, on_dismiss)


@pytest.mark.asyncio
async def test_council_modal_mount_and_fields(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    app = CouncilHostApp(project_path=project_dir)

    async with app.run_test(size=(110, 35)) as pilot:
        await pilot.pause()
        modal = app.modal
        assert app.screen is modal

        # Check fields exist
        assert modal.query_one("#council-label-input", Input).value.startswith("council-")
        assert modal.query_one("#council-idea-input", TextArea).text == ""
        roster_summary = modal.query_one("#council-roster-summary", Static).content
        assert "Coordinator" in str(roster_summary)
        assert "Scout" in str(roster_summary)
        assert "Active Members" in str(roster_summary)
        # AI Council explicitly does not have AiHead override
        assert not modal.query("#council-aihead-override")


@pytest.mark.asyncio
async def test_council_modal_empty_idea_validation(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    app = CouncilHostApp(project_path=project_dir)

    async with app.run_test(size=(110, 35)) as pilot:
        await pilot.pause()
        modal = app.modal

        # Leave idea empty and try to launch
        await pilot.click("#btn-launch")
        await pilot.pause()

        assert app.screen is modal  # Not dismissed
        status = modal.query_one("#council-status", Static).content
        assert "provide a specification" in str(status).lower()


@pytest.mark.asyncio
async def test_council_modal_successful_launch(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    app = CouncilHostApp(project_path=project_dir)

    async with app.run_test(size=(110, 35)) as pilot:
        await pilot.pause()
        modal = app.modal

        # Fill in idea and options
        modal.query_one("#council-idea-input", TextArea).text = (
            "Design decentralized pubsub message bus architecture"
        )
        modal.query_one("#council-label-input", Input).value = "council-pubsub-v1"

        await pilot.click("#btn-launch")
        await pilot.pause()

        assert app.result is not None
        params = app.result
        assert isinstance(params, CouncilLaunchParams)
        assert params.intent == "Design decentralized pubsub message bus architecture"
        assert params.workspace_label == "council-pubsub-v1"
        assert params.project_folder == project_dir.resolve()
        assert isinstance(params.council_config, CouncilConfig)
        assert len(params.council_config.members) >= 2


@pytest.mark.asyncio
async def test_council_modal_cancel(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    app = CouncilHostApp(project_path=project_dir)

    async with app.run_test(size=(110, 35)) as pilot:
        await pilot.pause()
        await pilot.click("#btn-cancel")
        await pilot.pause()

        assert app.result is None


@pytest.mark.asyncio
async def test_council_modal_open_settings(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    app = CouncilHostApp(project_path=project_dir)

    async with app.run_test(size=(110, 35)) as pilot:
        await pilot.pause()
        modal = app.modal

        await pilot.click("#btn-open-settings")
        await pilot.pause()

        assert isinstance(app.screen, SettingsModal)
