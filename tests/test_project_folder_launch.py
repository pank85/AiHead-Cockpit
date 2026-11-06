"""Project selection stays local to each launch and never changes process cwd."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from textual.widgets import Input

from herdr_master.core.context import HerdrContext
from herdr_master.pipeline.squad import resolve_squad
from aiheaddev.ui.tui.app import HerdrMasterApp
from aiheaddev.ui.tui.modals.launch_ergo import LaunchErgoModal
from aiheaddev.ui.tui.modals.launch_master import LaunchMasterModal
from aiheaddev.ui.tui.modals.project_folder import (
    ProjectFolderField,
    ProjectFolderPicker,
    initial_project_folder,
    validate_project_folder,
)


def test_folder_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert validate_project_folder("~") == tmp_path.resolve()
    assert initial_project_folder(tmp_path / ".herdr-master.toml") == tmp_path.resolve()
    for value in ("", str(tmp_path / "missing")):
        with pytest.raises(ValueError):
            validate_project_folder(value)
    file = tmp_path / "not-a-folder"
    file.touch()
    with pytest.raises(ValueError):
        validate_project_folder(str(file))


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["n", "e"])
@pytest.mark.parametrize("size", [(80, 24), (110, 32)])
async def test_browse_select_and_cancel(tmp_path, key, size):
    project = tmp_path / "a project with spaces"
    project.mkdir()
    app = HerdrMasterApp(demo=True, project_path=tmp_path)
    cwd = Path.cwd()
    async with app.run_test(size=size) as pilot:
        await pilot.press(key)
        modal = app.screen
        assert isinstance(modal, (LaunchMasterModal, LaunchErgoModal))
        submit = modal.query_one("#btn-launch" if key == "n" else "#btn-start-ergo")
        assert submit.region.bottom <= size[1]
        if key == "e":
            assert submit.region.bottom <= modal.query_one(".bottom-section").content_region.bottom
        button = modal.query_one("#btn-browse-project")
        # The field must remain reachable even when the rest of the form scrolls.
        button.scroll_visible(immediate=True)
        await pilot.pause()
        await pilot.click("#btn-browse-project")
        assert isinstance(app.screen, ProjectFolderPicker)
        picker = app.screen
        picker.query_one("#folder-location", Input).value = str(project)
        await pilot.click("#folder-go")
        await pilot.pause()
        assert picker._tree.path == project
        await pilot.click("#folder-select")
        await pilot.pause()
        assert app.screen is modal
        assert modal.query_one("#project-path", Input).value == str(project)
        await pilot.click("#btn-browse-project")
        app.screen.query_one("#folder-location", Input).value = str(tmp_path)
        await pilot.click("#folder-cancel")
        await pilot.pause()
        assert modal.query_one("#project-path", Input).value == str(project)
        assert Path.cwd() == cwd


@pytest.mark.asyncio
async def test_picker_tree_selects_directories_not_files(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    (tmp_path / "file.txt").touch()
    app = HerdrMasterApp(demo=True)
    async with app.run_test(size=(80, 24)) as pilot:
        picker = ProjectFolderPicker(tmp_path)
        app.push_screen(picker)
        await pilot.pause()
        children = picker._tree.root.children
        assert [node.data.path for node in children] == [child]
        picker._tree.select_node(children[0])
        await pilot.pause()
        assert picker._path.value == str(child)
        await pilot.click("#folder-select")
        assert app.screen is not picker


@pytest.mark.asyncio
@pytest.mark.parametrize("key,submit", [("n", "#btn-launch"), ("e", "#btn-start-ergo")])
async def test_invalid_folder_blocks_launch(tmp_path, key, submit):
    app = HerdrMasterApp(demo=False, project_path=tmp_path)
    with (
        patch("aiheaddev.ui.tui.app.launch_master", new_callable=AsyncMock) as master,
        patch("aiheaddev.ui.tui.app.launch_ergo", new_callable=AsyncMock) as ergo,
    ):
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.press(key)
            modal = app.screen
            modal.query_one("#project-path", Input).value = str(tmp_path / "missing")
            await pilot.pause()
            await pilot.click(submit)
            assert app.screen is modal
            assert "not a directory" in modal.status_text
            master.assert_not_awaited()
            ergo.assert_not_awaited()


@pytest.mark.asyncio
async def test_selected_project_loads_and_saves_its_own_squad(tmp_path):
    original = tmp_path / "original"
    selected = tmp_path / "selected"
    original.mkdir()
    selected.mkdir()
    config = selected / ".herdr-master.toml"
    config.write_text('[models.default_squad]\narchitect = "pi:xai/grok-4.7:high"\n')
    original_squad = resolve_squad(original).squad
    app = HerdrMasterApp(demo=False, project_path=original)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.press("e")
        modal = app.screen
        modal.query_one("#project-path", Input).value = str(selected)
        await pilot.pause()
        assert modal.project_path == selected
        assert modal.squad == resolve_squad(selected).squad
        assert modal.squad.architect.model == "xai/grok-4.7"
        assert modal.sources["architect"] == "project"
        await pilot.click("#btn-save-default")
        assert "Saved default" in modal.status_text
        assert not (original / ".herdr-master.toml").exists()
        saved = resolve_squad(selected).squad
        for role in ("scout", "architect", "reviewer"):
            for field in ("kind", "model", "effort"):
                assert getattr(getattr(saved, role), field) == getattr(
                    getattr(modal.squad, role), field
                )
        assert app.app_state.squad == original_squad
        assert app.project_path == original


@pytest.mark.asyncio
@pytest.mark.parametrize("key,submit", [("n", "#btn-launch"), ("e", "#btn-start-ergo")])
async def test_two_projects_launch_from_one_cockpit(tmp_path, key, submit):
    projects = [tmp_path / "project one", tmp_path / "project two"]
    for project in projects:
        project.mkdir()
    (projects[1] / ".herdr-master.toml").write_text(
        '[models.default_squad]\narchitect = "pi:xai/grok-4.7:high"\n'
    )
    ctx = HerdrContext(
        is_inside_herdr=False,
        session_name="control",
        socket_path=tmp_path / "herdr.sock",
    )
    app = HerdrMasterApp(demo=False, project_path=tmp_path)
    cwd = Path.cwd()
    results = [
        {"session": "control", "workspace_id": f"w{i}", "pane_id": f"w{i}:p1"}
        for i in range(2)
    ]
    launch_name = "launch_master" if key == "n" else "launch_ergo"
    with (
        patch("aiheaddev.ui.tui.app.resolve_launch_context", return_value=ctx),
        patch.object(HerdrMasterApp, "_schedule_inventory_refresh"),
        patch(f"aiheaddev.ui.tui.app.{launch_name}", new_callable=AsyncMock) as launch,
    ):
        launch.side_effect = results
        async with app.run_test(size=(110, 40)) as pilot:
            for project in projects:
                await pilot.press(key)
                app.screen.query_one("#project-path", Input).value = str(project)
                await pilot.pause()
                await pilot.click(submit)
                await pilot.pause()
                assert app.screen is app.screen_stack[0]
                assert app.return_value is None
                assert app.is_running
                assert not app._launch_pending
            assert [call.kwargs["cwd"] for call in launch.await_args_list] == [
                str(path) for path in projects
            ]
            assert all(call.kwargs["fresh_workspace"] for call in launch.await_args_list)
            assert all(call.kwargs["focus"] is False for call in launch.await_args_list)
            if key == "e":
                for call, project in zip(launch.await_args_list, projects, strict=True):
                    assert call.kwargs["squad"] == resolve_squad(project).squad
                # Reopening must not pair the last project's squad with the initial folder.
                await pilot.press("e")
                assert app.screen.squad == resolve_squad(tmp_path).squad
            assert Path.cwd() == cwd


@pytest.mark.asyncio
@pytest.mark.parametrize("key,submit", [("n", "#btn-launch"), ("e", "#btn-start-ergo")])
async def test_invalid_startup_project_can_switch_to_valid_project(tmp_path, key, submit):
    bad = tmp_path / "bad"
    good = tmp_path / "good"
    bad.mkdir()
    good.mkdir()
    (bad / ".herdr-master.toml").write_text('[models.default_squad]\nscout = "nope"\n')
    app = HerdrMasterApp(demo=False, project_path=bad)
    assert app.config_error
    ctx = HerdrContext(
        is_inside_herdr=True, session_name="control", socket_path=tmp_path / "herdr.sock"
    )
    launch_name = "launch_master" if key == "n" else "launch_ergo"
    with (
        patch("aiheaddev.ui.tui.app.resolve_launch_context", return_value=ctx),
        patch.object(HerdrMasterApp, "_schedule_inventory_refresh"),
        patch(f"aiheaddev.ui.tui.app.{launch_name}", new_callable=AsyncMock) as launch,
    ):
        launch.return_value = {"session": "control", "workspace_id": "w1", "pane_id": "w1:p1"}
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.press(key)
            app.screen.query_one("#project-path", Input).value = str(good)
            await pilot.pause()
            await pilot.click(submit)
            await pilot.pause()
            launch.assert_awaited_once()
            assert launch.await_args.kwargs["cwd"] == str(good)


def test_initial_project_folder_fallback_to_default_folder(tmp_path):
    from herdr_master.core.config import HerdrMasterConfig, ProjectsConfig

    default_dir = tmp_path / "configured_default"
    default_dir.mkdir()
    cfg = HerdrMasterConfig(projects=ProjectsConfig(default_folder=str(default_dir)))

    assert initial_project_folder(None, config=cfg) == default_dir.resolve()
    # Explicit project path takes precedence over default folder
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    assert initial_project_folder(explicit, config=cfg) == explicit.resolve()


@pytest.mark.asyncio
async def test_project_folder_field_favorite_selection(tmp_path):
    from textual.app import App, ComposeResult
    from textual.widgets import Select

    from herdr_master.core.config import FavoriteProject

    core_dir = tmp_path / "core_module"
    web_dir = tmp_path / "web_module"
    core_dir.mkdir()
    web_dir.mkdir()

    favs = [
        FavoriteProject(name="core", path=str(core_dir)),
        FavoriteProject(name="web", path=str(web_dir)),
    ]

    field = ProjectFolderField(favorites=favs)
    events = []

    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield field

        def on_project_folder_field_changed(self, event: ProjectFolderField.Changed) -> None:
            events.append(field.selected_path())

    app = Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        select = field.query_one("#select-favorite-folder", Select)
        assert select.display is True
        assert field.query_one("#project-path", Input).value != ""

        # Select favorite by name
        field.select_favorite("web")
        await pilot.pause()
        assert field.selected_path() == web_dir.resolve()
        assert field.query_one("#project-path", Input).value == str(web_dir.resolve())
        assert len(events) >= 1
        assert events[-1] == web_dir.resolve()

        # Selecting another favorite
        field.select_favorite("core")
        await pilot.pause()
        assert field.selected_path() == core_dir.resolve()
        assert events[-1] == core_dir.resolve()

        # Direct typing still works
        field.query_one("#project-path", Input).value = str(web_dir.resolve())
        await pilot.pause()
        assert field.selected_path() == web_dir.resolve()


@pytest.mark.asyncio
async def test_project_folder_field_no_favorites_hidden():
    from textual.app import App, ComposeResult
    from textual.widgets import Select

    field = ProjectFolderField(favorites=[])

    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield field

    app = Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        select = field.query_one("#select-favorite-folder", Select)
        assert select.display is False

