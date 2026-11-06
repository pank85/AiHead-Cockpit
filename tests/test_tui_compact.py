"""Layout regressions for the compact cockpit at everyday terminal sizes."""

import pytest
from rich.text import Text

from aiheaddev.ui.tui.app import HerdrMasterApp
from aiheaddev.ui.tui.widgets.footer import ActionFooter
from aiheaddev.ui.tui.widgets.quick_launch import QuickLaunchBar
from aiheaddev.ui.tui.widgets.workspaces import WorkspaceBrowser, WorkspaceTarget


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (110, 32)])
async def test_compact_workspace_layout(size):
    """Verify standard cockpit sizes keep toolbar geometry, Open strip, and >= 10 tree rows."""
    app = HerdrMasterApp(demo=True)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        toolbar = app.query_one(QuickLaunchBar)
        assert toolbar.size.height == 4
        descriptions = list(toolbar.query(".launch-description"))
        assert len(descriptions) == 4
        for description, (button_id, expected_text) in zip(
            descriptions,
            (
                ("#btn-new-master", "Coordinate ongoing jobs"),
                ("#btn-new-ergo", "Run one planned pipeline"),
                ("#btn-new-orchestration", "Single-harness subagents"),
                ("#btn-new-council", "Deliberate consensus plan"),
            ),
            strict=True,
        ):
            button = toolbar.query_one(button_id)
            assert description.size.height == 1
            assert description.region.y == button.region.bottom
            assert description.region.bottom <= toolbar.region.bottom
            assert expected_text in str(description.render())
        for button_id in (
            "#btn-new-master",
            "#btn-new-ergo",
            "#btn-new-orchestration",
            "#btn-new-council",
            "#btn-settings",
        ):
            button = app.query_one(button_id)
            assert button.region.y == toolbar.region.y
            assert button.region.right <= size[0]
            assert button.tooltip

        browser = app.query_one(WorkspaceBrowser)
        tree = browser._tree
        # At 80x24 and 110x32, retain at least ten visible tree rows
        assert tree.size.height >= 10
        assert tree.region.bottom <= size[1] - 1

        # Open strip and button are accessible and properly contained
        strip = browser.query_one("#open-strip")
        open_btn = browser.query_one("#btn-open-herdr")
        assert open_btn.region.right <= size[0]
        assert open_btn.region.bottom <= tree.region.y
        assert open_btn.region.y >= strip.region.y
        assert open_btn.region.bottom <= strip.region.bottom
        assert open_btn.region.x >= strip.region.x
        assert open_btn.region.right <= strip.region.right

        # Visible positive content height and literal label with [o]
        assert open_btn.content_region.height == 1
        assert open_btn.content_region.width > 0
        assert open_btn.label.plain == "Open in Herdr [o]"

        # Visible handoff/return guidance in strip
        help_static = browser.query_one("#open-help")
        help_text = str(help_static.render())
        assert "replaces this cockpit" in help_text
        assert "herdm tui" in help_text
        assert help_static.region.bottom <= strip.region.bottom

        # Footer displays real actions without overflow
        footer = app.query_one(ActionFooter)
        assert footer.region.bottom <= size[1]
        footer_text = str(footer.render())
        assert "[o]" in footer_text
        assert "[q]" in footer_text

        # Demo workspace renders both role badges
        labels = [node.label for node in tree.root.children if isinstance(node.label, Text)]
        plain_labels = [lbl.plain for lbl in labels]
        assert any("[Master]" in p and "[Ergo]" in p for p in plain_labels)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(60, 24), (40, 24)])
async def test_compact_workspace_layout_narrow(size):
    """Verify narrow terminal sizes maintain accessible selection and open controls."""
    app = HerdrMasterApp(demo=True)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        browser = app.query_one(WorkspaceBrowser)
        strip = browser.query_one("#open-strip")
        open_btn = browser.query_one("#btn-open-herdr")
        assert open_btn is not None
        assert open_btn.region.y >= strip.region.y
        assert open_btn.region.bottom <= strip.region.bottom
        assert open_btn.content_region.height == 1
        assert open_btn.content_region.width > 0
        assert open_btn.label.plain == "Open in Herdr [o]"

        help_static = browser.query_one("#open-help")
        help_rendered = str(help_static.render())
        if size[0] < 55:
            assert "Attach replaces cockpit." in help_rendered
            assert "Run `herdm tui` to return." in help_rendered
        else:
            assert "replaces this cockpit" in help_rendered
            assert "herdm tui" in help_rendered

        tree = browser._tree
        assert tree.size.height >= 4
        assert tree.region.bottom <= size[1]

        footer = app.query_one(ActionFooter)
        footer_text = str(footer.render())
        assert "[o]" in footer_text
        assert "[q]" in footer_text


@pytest.mark.asyncio
async def test_compact_workspace_resize_wide_narrow_wide():
    """Verify wide -> narrow -> wide resizing maintains layout stability and controls."""
    app = HerdrMasterApp(demo=True)
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        browser = app.query_one(WorkspaceBrowser)
        assert browser._tree.size.height >= 10
        btn = browser.query_one("#btn-open-herdr")
        assert btn.content_region.height == 1
        assert btn.label.plain == "Open in Herdr [o]"
        help_static = browser.query_one("#open-help")
        assert "replaces this cockpit" in str(help_static.render())

        # Resize to narrow (40x24)
        await pilot.resize_terminal(40, 24)
        await pilot.pause()
        assert browser._tree.size.height >= 4
        assert btn.content_region.height == 1
        assert "Attach replaces cockpit." in str(help_static.render())

        # Resize back to wide (110x32)
        await pilot.resize_terminal(110, 32)
        await pilot.pause()
        assert browser._tree.size.height >= 10
        assert btn.content_region.height == 1
        assert "replaces this cockpit" in str(help_static.render())
        assert btn.region.right <= 110


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (110, 32), (60, 24), (40, 24)])
async def test_compact_workspace_enabled_and_disabled_button_rendering(size):
    """Inspect actual button visible content height in enabled and disabled states across sizes."""
    from pathlib import Path

    from textual.geometry import Region

    from herdr_master.herdr.discovery import SessionInventory
    from herdr_master.herdr.snapshot import SessionSnapshot

    app = HerdrMasterApp(demo=True)
    app.app_state.inventory_demo = False
    sess = SessionInventory(
        session_name="prod",
        socket_path=Path("/tmp/prod.sock"),
        snapshot=SessionSnapshot(
            protocol=22,
            workspaces=[{"workspace_id": "w1", "label": "Main"}],
            tabs=[{"tab_id": "t1", "workspace_id": "w1"}],
            panes=[{"pane_id": "p1", "tab_id": "t1", "agent": "pi"}],
            agents=[{"pane_id": "p1", "name": "Worker"}],
        ),
    )
    app.app_state.live_sessions = [sess]
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        browser = app.query_one(WorkspaceBrowser)
        btn = browser.query_one("#btn-open-herdr")
        sel = browser.query_one("#open-selection")
        help_static = browser.query_one("#open-help")
        row = browser.query_one("#open-strip-row")

        # Disabled initially before deliberate selection
        assert btn.disabled
        assert btn.content_region.height == 1
        assert btn.region.right <= row.region.right
        btn_lines = [
            line.text
            for line in btn.render_lines(
                Region(0, 0, btn.content_region.width, btn.content_region.height)
            )
        ]
        assert "Open in Herdr [o]" in "".join(btn_lines)
        assert "No selection" in str(sel.render())

        # replacement/return guidance visible without clipping
        help_lines = [
            line.text
            for line in help_static.render_lines(
                Region(0, 0, help_static.content_region.width, help_static.content_region.height)
            )
        ]
        assert len(help_lines) == 2
        for hl in help_lines:
            assert len(hl) <= help_static.content_region.width
        joined_help = " ".join(line.strip() for line in help_lines)
        if size[0] < 55:
            assert "Attach replaces cockpit." in help_lines[0]
            assert "Run `herdm tui` to return." in help_lines[1]
        else:
            assert "Open switches to Herdr" in help_lines[0]
            assert "Run `herdm tui` to return." in joined_help

        # Expand and deliberately select pane p1
        tree = browser._tree
        ws_node = tree.root.children[0]
        ws_node.expand()
        await pilot.pause()
        p1_node = ws_node.children[0]
        tree.select_node(p1_node)
        await pilot.pause()

        # Enabled after deliberate selection
        assert not btn.disabled
        assert btn.content_region.height == 1
        assert btn.region.right <= row.region.right
        btn_lines_enabled = [
            line.text
            for line in btn.render_lines(
                Region(0, 0, btn.content_region.width, btn.content_region.height)
            )
        ]
        assert "Open in Herdr [o]" in "".join(btn_lines_enabled)
        assert "Worker" in str(sel.render())

        # Resize transition test
        target_size = (40, 24) if size[0] >= 55 else (80, 24)
        await pilot.resize_terminal(*target_size)
        await pilot.pause()
        assert btn.region.right <= row.region.right
        help_lines_resized = [
            line.text
            for line in help_static.render_lines(
                Region(0, 0, help_static.content_region.width, help_static.content_region.height)
            )
        ]
        assert len(help_lines_resized) == 2
        for hl in help_lines_resized:
            assert len(hl) <= help_static.content_region.width
        if target_size[0] < 55:
            assert "Attach replaces cockpit." in help_lines_resized[0]
        else:
            assert "Open switches to Herdr" in help_lines_resized[0]


@pytest.mark.asyncio
async def test_compact_workspace_long_text_and_scroll_retention():
    """Verify long text, role badges, stale cues, and nonzero-scroll retention
    across refresh and resize.
    """
    from pathlib import Path

    from herdr_master.herdr.discovery import SessionInventory
    from herdr_master.herdr.snapshot import SessionSnapshot

    panes = [
        {
            "pane_id": f"p{i}",
            "tab_id": f"t{i}",
            "agent": "pi" if i % 2 == 0 else None,
            "agent_status": "working" if i == 0 else "idle",
            "cwd": (
                f"/home/user/very/long/deeply/nested/directory/path/item_{i}/"
                "[yellow]nested[/yellow]"
            ),
        }
        for i in range(25)
    ]
    tabs = [
        {"tab_id": f"t{i}", "workspace_id": "w_long", "label": f"[blue]Long-Tab-Label-{i}[/blue]"}
        for i in range(25)
    ]
    agents = [
        {"pane_id": "p0", "name": "master-coordinator", "agent": "pi"},
        {"pane_id": "p1", "name": "ergo-orchestrator", "agent": "pi"},
        {"pane_id": "p2", "name": "[magenta]agent-markup[/magenta]", "agent": "pi"},
    ]
    sess = SessionInventory(
        session_name="[red]session-with-excessively-long-name-for-geometry-testing[/red]",
        socket_path=Path("/tmp/long.sock"),
        error="stale connection warning",
        snapshot=SessionSnapshot(
            protocol=22,
            workspaces=[
                {
                    "workspace_id": "w_long",
                    "label": (
                        "[green]workspace-with-extremely-long-identifier-for-overflow-testing[/green]"
                    ),
                    "agent_status": "working",
                }
            ],
            tabs=tabs,
            panes=panes,
            agents=agents,
        ),
    )
    app = HerdrMasterApp(demo=True)
    app.app_state.inventory_demo = False
    app.app_state.live_sessions = [sess]

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        browser = app.query_one(WorkspaceBrowser)
        tree = browser._tree
        ws_node = tree.root.children[0]
        # Verify literal markup-shaped strings, role badges, and stale cue rendered
        expected_sess = "[red]session-with-excessively-long-name-for-geometry-testing[/red]"
        expected_ws = (
            "[green]workspace-with-extremely-long-identifier-for-overflow-testing[/green]"
        )
        assert expected_sess in ws_node.label.plain
        assert expected_ws in ws_node.label.plain
        assert "[Master]" in ws_node.label.plain
        assert "[Ergo]" in ws_node.label.plain
        assert "stale" in ws_node.label.plain

        ws_node.expand()
        await pilot.pause()

        # Check agent name and tab label retain markup-shaped text literally
        p2_node = ws_node.children[2]
        assert "[magenta]agent-markup[/magenta]" in p2_node.label.plain
        p18_node = ws_node.children[18]
        assert "[blue]Long-Tab-Label-18[/blue]" in p18_node.label.plain
        assert "[yellow]nested[/yellow]" in p18_node.label.plain

        # Select a lower pane (p18) and require nonzero scroll before retention check
        tree.select_node(p18_node)
        tree.scroll_to_node(p18_node, animate=False)
        await pilot.pause()

        assert tree.cursor_node is p18_node
        assert browser._selected_target.pane_id == "p18"
        assert tree.scroll_y > 0
        scroll_before = tree.scroll_offset

        # Refresh state
        browser.update_state(app.app_state)
        await pilot.pause()

        # Selection and scroll retention
        assert tree.cursor_node.data.pane_id == "p18"
        assert browser._selected_target.pane_id == "p18"
        assert tree.scroll_offset == scroll_before
        assert tree.scroll_y > 0

        # Resize and verify layout stability and scroll retention
        await pilot.resize_terminal(110, 32)
        await pilot.pause()
        assert tree.cursor_node.data.pane_id == "p18"
        assert browser._selected_target.pane_id == "p18"
        assert tree.scroll_y > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(40, 24), (60, 24), (80, 24), (110, 32)])
async def test_compact_layout_with_live_pane_and_composer(size):
    """Verify geometry across terminal sizes with live view and composer active.

    At 40x24, 60x24, and 80x24: stacked browser (12 rows, >=4 tree rows), readable
    output rows (>=3), visible Send/Cancel/Follow controls, and Open/footer controls.
    At 110x32: horizontal split (browser width >=48, tree rows >=10).
    """
    from pathlib import Path

    from herdr_master.herdr.discovery import SessionInventory
    from herdr_master.herdr.snapshot import SessionSnapshot
    from aiheaddev.ui.tui.live_pane import PaneFrame, PaneLease
    from aiheaddev.ui.tui.widgets.deck import CenterDeck
    from aiheaddev.ui.tui.widgets.live_output import LivePaneView

    app = HerdrMasterApp(demo=True)
    app.app_state.inventory_demo = False
    sock = Path("/tmp/compact.sock")
    sess = SessionInventory(
        session_name="demo-session",
        socket_path=sock,
        snapshot=SessionSnapshot(
            protocol=22,
            workspaces=[{"workspace_id": "w1", "label": "Main"}],
            tabs=[{"tab_id": "t1", "workspace_id": "w1"}],
            panes=[{"pane_id": "p1", "tab_id": "t1", "agent": "pi"}],
            agents=[{"pane_id": "p1", "name": "Agent", "agent_status": "working"}],
        ),
    )
    app.app_state.live_sessions = [sess]

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        deck = app.query_one(CenterDeck)
        browser = app.query_one(WorkspaceBrowser)
        live_pane = app.query_one(LivePaneView)

        # 1. No pane selected initially: full-width landing view
        assert not live_pane.has_active_pane
        assert live_pane.display is False
        assert browser._tree.size.height >= 10

        # A long identity must not alter the compact geometry.
        target = WorkspaceTarget(
            "demo-session-with-a-very-long-name", sock, "workspace-with-a-long-id",
            "tab-with-a-long-id", "pane-with-a-long-id",
        )
        lease = PaneLease((str(sock), target.workspace_id, target.pane_id), 1, "term-1")
        frame = PaneFrame(
            lease=lease,
            target=target,
            phase="live",
            agent_status="working",
            text_ansi="Line 1\nLine 2\nLine 3\nLine 4\nLine 5\n",
            error=None,
            can_reply=True,
        )
        deck.update_live_frame(frame)
        await pilot.pause()

        assert live_pane.display is True
        tree = browser._tree
        output = live_pane.query_one("#live-pane-output")

        if size[0] >= 110:
            assert deck.has_class("-horizontal")
            assert browser.region.width >= 48
            assert tree.size.height >= 10
        else:
            assert deck.has_class("-vertical")
            assert browser.region.height == 12
            assert tree.size.height >= 4

        assert output.content_region.height >= 3

        # 3. Reveal composer by clicking Reply
        btn_reply = live_pane.query_one("#btn-reply")
        assert not btn_reply.disabled
        await pilot.click("#btn-reply")
        await pilot.pause()

        assert live_pane.has_class("-reply-active")
        ta = live_pane.query_one("#reply-text")
        assert ta.display is True
        assert ta.region.height == 2
        assert app.focused is ta
        assert "pane-with-a-long-id" in live_pane.query_one("#live-pane-identity").render().plain

        btn_send = live_pane.query_one("#btn-send-reply")
        btn_cancel = live_pane.query_one("#btn-cancel-reply")
        btn_follow = live_pane.query_one("#btn-follow-output")
        btn_target = live_pane.query_one("#btn-target-details")

        assert btn_send.display is True
        assert btn_cancel.display is True
        assert btn_follow.display is True
        assert btn_target.display is True

        # All buttons fit within the available terminal width
        assert btn_send.region.right <= size[0]
        assert btn_cancel.region.right <= size[0]
        assert btn_follow.region.right <= size[0]
        assert btn_target.region.right <= live_pane.content_region.right
        for button in (btn_send, btn_cancel, btn_follow, btn_target):
            assert button.content_region.width >= len(str(button.label))
            assert button.region.bottom <= live_pane.region.bottom

        # In stacked live mode with composer, check minimum readable heights
        if size[0] < 110:
            assert tree.size.height >= 4
            assert output.content_region.height >= 3

        # Open in Herdr button and footer remain visible and properly bounded
        open_btn = browser.query_one("#btn-open-herdr")
        assert open_btn.region.right <= size[0]
        footer = app.query_one(ActionFooter)
        assert footer.region.bottom <= size[1]


@pytest.mark.asyncio
async def test_compact_layout_live_pane_resize_110_40_110():
    """Verify live pane and composer maintain stability and constraints during
    110 -> 40 -> 110 resize.
    """
    from pathlib import Path

    from herdr_master.herdr.discovery import SessionInventory
    from herdr_master.herdr.snapshot import SessionSnapshot
    from aiheaddev.ui.tui.live_pane import PaneFrame, PaneLease
    from aiheaddev.ui.tui.widgets.deck import CenterDeck
    from aiheaddev.ui.tui.widgets.live_output import LivePaneView

    app = HerdrMasterApp(demo=True)
    app.app_state.inventory_demo = False
    sock = Path("/tmp/compact.sock")
    sess = SessionInventory(
        session_name="demo-session",
        socket_path=sock,
        snapshot=SessionSnapshot(
            protocol=22,
            workspaces=[{"workspace_id": "w1", "label": "Main"}],
            tabs=[{"tab_id": "t1", "workspace_id": "w1"}],
            panes=[{"pane_id": "p1", "tab_id": "t1", "agent": "pi"}],
            agents=[{"pane_id": "p1", "name": "Agent", "agent_status": "working"}],
        ),
    )
    app.app_state.live_sessions = [sess]

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        deck = app.query_one(CenterDeck)
        browser = app.query_one(WorkspaceBrowser)
        live_pane = app.query_one(LivePaneView)

        target = WorkspaceTarget("demo-session", sock, "w1", "t1", "p1")
        lease = PaneLease((str(sock), "w1", "p1"), 1, "term-1")
        frame = PaneFrame(
            lease=lease,
            target=target,
            phase="live",
            agent_status="working",
            text_ansi="Line 1\nLine 2\nLine 3\n",
            error=None,
            can_reply=True,
        )
        deck.update_live_frame(frame)
        await pilot.pause()

        await pilot.click("#btn-reply")
        await pilot.pause()
        ta = live_pane.query_one("#reply-text")
        assert app.focused is ta
        await pilot.press("x")

        assert deck.has_class("-horizontal")
        assert browser.region.width >= 48
        assert browser._tree.size.height >= 10

        # Resize to narrow 40x24
        await pilot.resize_terminal(40, 24)
        await pilot.pause()

        assert deck.has_class("-vertical")
        assert browser.region.height == 12
        assert browser._tree.size.height >= 4
        output = live_pane.query_one("#live-pane-output")
        assert output.content_region.height >= 3
        assert app.focused is ta
        assert ta.text == "x"
        assert live_pane.query_one("#btn-target-details").region.right <= 40

        # Resize back to wide 110x32
        await pilot.resize_terminal(110, 32)
        await pilot.pause()

        assert deck.has_class("-horizontal")
        assert browser.region.width >= 48
        assert browser._tree.size.height >= 10
        assert app.focused is ta
        assert ta.text == "x"
