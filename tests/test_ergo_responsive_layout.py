"""The Ergo dialog prioritizes a multiline prompt without clipping its controls."""

from unittest.mock import AsyncMock, patch

import pytest
from textual.widgets import TextArea

from aiheaddev.ui.tui.app import HerdrMasterApp
from aiheaddev.ui.tui.modals.launch_ergo import LaunchErgoModal


def assert_inside(widget, container):
    assert container.content_region.contains_region(widget.region), (
        widget.region,
        container.content_region,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(60, 24), (80, 24), (110, 32), (180, 45)])
async def test_responsive_editor_and_right_hand_actions(size):
    app = HerdrMasterApp(demo=True)
    async with app.run_test(size=size) as pilot:
        await pilot.press("e")
        modal = app.screen
        assert isinstance(modal, LaunchErgoModal)
        prompt = modal.query_one("#ergo-intent", TextArea)
        bottom = modal.query_one(".bottom-section")
        assert prompt.region.height >= 4
        assert_inside(prompt, bottom)
        assert_inside(modal._project_folder, bottom)
        for button_id in ("#btn-start-ergo", "#btn-save-default", "#btn-cancel"):
            button = modal.query_one(button_id)
            assert button.region.x >= prompt.region.right
            assert_inside(button, bottom)
            assert button.region.right <= size[0]
            assert button.region.bottom <= size[1]
        columns = list(modal.query(".option-column"))
        if size[0] >= 72:
            assert columns[0].region.y == columns[1].region.y
            assert columns[0].region.right < columns[1].region.x
            assert columns[0].region.height == columns[1].region.height == 8
        else:
            assert columns[1].region.y >= columns[0].region.bottom
        if size[1] >= 32:
            assert modal.query_one(".top-section").region.height <= 12


@pytest.mark.asyncio
async def test_resize_preserves_prompt_squad_and_project(tmp_path):
    app = HerdrMasterApp(demo=True, project_path=tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("e")
        modal = app.screen
        prompt = modal.query_one("#ergo-intent", TextArea)
        prompt.load_text("First task\nSecond task\nKeep all these details.")
        modal._load_agent_into_editor(("worker", 0))
        modal._description_input.value = "Custom worker capability"
        await pilot.pause()
        before = modal.squad.model_copy(deep=True)
        short_height = prompt.region.height
        await pilot.resize_terminal(180, 45)
        await pilot.pause()
        assert prompt.region.height > short_height
        assert modal.query_one(".top-section").region.height <= 16
        assert not modal.has_class("short")
        await pilot.resize_terminal(60, 24)
        await pilot.pause()
        assert modal.has_class("stacked-options")
        assert modal.has_class("short")
        assert modal.squad == before
        assert modal.project_path == tmp_path
        assert prompt.text == "First task\nSecond task\nKeep all these details."
        for selector in ("#btn-start-ergo", "#btn-save-default", "#btn-cancel"):
            assert_inside(modal.query_one(selector), modal.query_one(".bottom-section"))
        # Hidden options remain reachable through the settings scroller.
        modal._description_input.scroll_visible(immediate=True)
        await pilot.pause()
        await pilot.pause()
        assert_inside(modal._description_input, modal.query_one(".editor-fields"))


@pytest.mark.asyncio
async def test_multiline_prompt_enters_and_submits_without_global_shortcuts(tmp_path):
    app = HerdrMasterApp(demo=True, project_path=tmp_path)
    results = []
    async with app.run_test(size=(110, 32)) as pilot:
        modal = LaunchErgoModal(app.app_state.squad, project_path=tmp_path)
        app.push_screen(modal, results.append)
        await pilot.pause()
        await pilot.click("#ergo-intent")
        await pilot.press("q", "u", "e", "s", "t", "enter", "n", "e", "x", "t")
        assert app.screen is modal
        assert modal._intent_input.text == "quest\nnext"
        await pilot.press("tab")
        assert app.focused is modal.query_one("#btn-start-ergo")
        await pilot.click("#btn-start-ergo")
        await pilot.pause()
        assert len(results) == 1
        assert results[0].intent == "quest\nnext"
        assert results[0].project_path == tmp_path


@pytest.mark.asyncio
async def test_multiline_prompt_reaches_launcher(tmp_path):
    from herdr_master.core.context import HerdrContext

    app = HerdrMasterApp(demo=False, project_path=tmp_path)
    ctx = HerdrContext(
        is_inside_herdr=True,
        session_name="control",
        socket_path=tmp_path / "herdr.sock",
    )
    with (
        patch("aiheaddev.ui.tui.app.resolve_launch_context", return_value=ctx),
        patch.object(HerdrMasterApp, "_schedule_inventory_refresh"),
        patch("aiheaddev.ui.tui.app.launch_ergo", new_callable=AsyncMock) as launch,
    ):
        launch.return_value = {"session": "control", "workspace_id": "w1", "pane_id": "w1:p1"}
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("e")
            app.screen.query_one("#ergo-intent", TextArea).load_text("Line one\n\nLine three")
            await pilot.click("#btn-start-ergo")
            await pilot.pause()
        launch.assert_awaited_once()
        assert launch.await_args.kwargs["intent"] == "Line one\n\nLine three"
