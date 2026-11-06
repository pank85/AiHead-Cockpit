"""Pilot unit and integration tests for responsive live view and safe operator interaction."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from protocol22_fixtures import agent_prompted, pane_read_record, snapshot_identity
from rich.text import Text

from herdr_master.herdr.discovery import SessionInventory
from herdr_master.herdr.snapshot import SessionSnapshot
from aiheaddev.ui.tui.app import HerdrMasterApp
from aiheaddev.ui.tui.live_pane import (
    LivePaneController,
    PaneFrame,
    PaneLease,
    ReplyResult,
)
from aiheaddev.ui.tui.widgets.deck import CenterDeck
from aiheaddev.ui.tui.widgets.live_output import LivePaneOutput, LivePaneView, sanitize_ansi
from aiheaddev.ui.tui.widgets.workspaces import WorkspaceBrowser, WorkspaceTarget, safe_display


def make_inventory(
    socket_path: Path,
    session_name: str = "test-session",
    error: str | None = None,
    panes: list[dict] | None = None,
    agents: list[dict] | None = None,
) -> SessionInventory:
    if panes is None:
        panes = [
            {"pane_id": "p1", "tab_id": "t1", "agent": "worker", "agent_status": "working"},
            {"pane_id": "p2", "tab_id": "t1", "agent": None},  # terminal
        ]
    if agents is None:
        agents = [
            {
                "pane_id": "p1",
                "name": "WorkerAgent",
                "agent_status": "working",
                "interactive_ready": True,
            },
        ]
    snap = SessionSnapshot(
        protocol=22,
        workspaces=[{"workspace_id": "w1", "label": "Workspace 1", "agent_status": "working"}],
        tabs=[{"tab_id": "t1", "workspace_id": "w1", "label": "Tab 1"}],
        panes=panes,
        agents=agents,
    )
    return SessionInventory(
        session_name=session_name,
        socket_path=socket_path,
        error=error,
        snapshot=snap if error is None else None,
    )


def test_identifier_controls_are_visible_and_unambiguous():
    value = "literal\\x1b" + chr(0xE0001) + "a" + chr(0xD800) + chr(0x2028)
    assert safe_display(value) == r"literal\\x1b\U000e0001a\ud800\u2028"


def test_sanitize_ansi_controls():
    """Verify unsafe sequences are stripped while SGR color formatting is preserved."""
    raw = (
        "\x1b]0;Evil Window Title\x07"  # OSC
        "\x1bP123;evil_dcs\x1b\\"       # DCS
        "\x1b[2J\x1b[H"                 # Cursor / clear
        "\x1b[32;1mGreen Bold\x1b[0m"   # SGR formatting
        "\r\nNext Line\r"               # Newline variations
        "\x1b[?25h"                     # Cursor visibility sequence
    )
    sanitized = sanitize_ansi(raw)
    assert "Evil Window Title" not in sanitized
    assert "evil_dcs" not in sanitized
    assert "\x1b[2J" not in sanitized
    assert "\x1b[H" not in sanitized
    assert "\x1b[32;1mGreen Bold\x1b[0m" in sanitized
    assert "\nNext Line\n" in sanitized

    # Convert to rich Text
    rich_text = Text.from_ansi(sanitized)
    assert "Green Bold" in rich_text.plain
    assert "Next Line" in rich_text.plain
    # Ensure markup square brackets are treated literally
    bracket_raw = "[bold]Literal brackets[/bold] and \x1b[31mRed\x1b[0m"
    bracket_sanitized = sanitize_ansi(bracket_raw)
    rich_brackets = Text.from_ansi(bracket_sanitized)
    assert "[bold]Literal brackets[/bold]" in rich_brackets.plain


@pytest.mark.asyncio
async def test_workspace_selection_without_auto_following_child():
    """Verify workspace-only selection emits no pane RPCs and keeps landing view."""
    app = HerdrMasterApp(demo=True)
    app.app_state.inventory_demo = False
    sock = Path("/tmp/w_only.sock")
    inv = make_inventory(sock)
    app.app_state.live_sessions = [inv]

    published_frames: list[PaneFrame] = []
    fake_controller = LivePaneController(
        publish=lambda f: published_frames.append(f),
        client_factory=Mock(),
    )
    app._live_controller = fake_controller

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        app.demo = False  # Avoid starting real socket discovery during mount.
        deck = app.query_one(CenterDeck)
        browser = app.query_one(WorkspaceBrowser)
        live_pane = app.query_one(LivePaneView)

        # Select the workspace row (pane_id=None)
        ws_node = browser._tree.root.children[0]
        browser._tree.select_node(ws_node)
        await pilot.pause()

        assert browser._selected_target is not None
        assert browser._selected_target.pane_id is None
        assert not live_pane.has_active_pane
        assert live_pane.display is False
        assert deck.has_class("-full")

        # Zero pane reads or sends occurred
        fake_controller._client_factory.assert_not_called()


@pytest.mark.asyncio
async def test_selecting_second_pane_and_different_socket_with_same_ids():
    """Verify selecting second pane and different socket with identical IDs isolates targets."""
    app = HerdrMasterApp(demo=True)
    app.app_state.inventory_demo = False

    sock1 = Path("/tmp/sess1.sock")
    sock2 = Path("/tmp/sess2.sock")
    inv1 = make_inventory(sock1, session_name="session-1")
    inv2 = make_inventory(sock2, session_name="session-2")
    app.app_state.live_sessions = [inv1, inv2]

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        deck = app.query_one(CenterDeck)
        live_pane = app.query_one(LivePaneView)

        # 1. Deliver frame from socket 1 pane p1
        t1 = WorkspaceTarget("session-1", sock1, "w1", "t1", "p1")
        l1 = PaneLease((str(sock1), "w1", "p1"), 1, "term-1")
        f1 = PaneFrame(
            lease=l1,
            target=t1,
            phase="live",
            agent_status="working",
            text_ansi="Socket 1 Output",
            error=None,
            can_reply=True,
        )
        deck.update_live_frame(f1)
        await pilot.pause()

        assert live_pane.has_active_pane
        id_text = str(live_pane.query_one("#live-pane-identity").render())
        assert "session-1" in id_text
        out_text = live_pane.query_one("#live-pane-output").render().plain
        assert "Socket 1 Output" in out_text

        # 2. Deliver frame from socket 2 pane p1 (same workspace/pane ID, different socket)
        t2 = WorkspaceTarget("session-2", sock2, "w1", "t1", "p1")
        l2 = PaneLease((str(sock2), "w1", "p1"), 2, "term-2")
        f2 = PaneFrame(
            lease=l2,
            target=t2,
            phase="live",
            agent_status="idle",
            text_ansi="Socket 2 Output",
            error=None,
            can_reply=True,
        )
        deck.update_live_frame(f2)
        await pilot.pause()

        id_text2 = str(live_pane.query_one("#live-pane-identity").render())
        assert "session-2" in id_text2
        out_text2 = live_pane.query_one("#live-pane-output").render().plain
        assert "Socket 2 Output" in out_text2
        assert "Socket 1 Output" not in out_text2


@pytest.mark.asyncio
async def test_output_switch_causes_no_navigation_or_input_until_explicit():
    """Verify real controller selection reads only from its offline client."""
    app = HerdrMasterApp(demo=True)
    sock = Path("/tmp/nav_test.sock")
    inv = make_inventory(sock)
    clients, live_events, _ = install_event_controller(app, [inv])
    client = clients[str(sock)]
    first_live = live_events.setdefault((str(sock), "p1"), asyncio.Event())

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        app.demo = False
        browser = app.query_one(WorkspaceBrowser)
        await select_pane(pilot, browser, 0, first_live)
        assert app.query_one(LivePaneView)._current_frame.phase == "live"
        client.focus_workspace.assert_not_called()
        client.focus_tab.assert_not_called()
        client.focus_pane.assert_not_called()
        client.send_keys.assert_not_called()
        assert client.prompts == []


@pytest.mark.asyncio
async def test_stale_disappeared_selection_and_no_fallback():
    """Verify vanished target displays stale/unavailable and last labeled output."""
    app = HerdrMasterApp(demo=True)
    sock = Path("/tmp/stale.sock")
    t1 = WorkspaceTarget("session", sock, "w1", "t1", "p1")
    l1 = PaneLease((str(sock), "w1", "p1"), 1, "term-1")

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        deck = app.query_one(CenterDeck)
        live_pane = app.query_one(LivePaneView)

        # Frame 1: Live output
        deck.update_live_frame(
            PaneFrame(
                lease=l1,
                target=t1,
                phase="live",
                agent_status="working",
                text_ansi="Last Known Output\n",
                error=None,
                can_reply=True,
            )
        )
        await pilot.pause()
        assert live_pane.has_active_pane

        # Frame 2: Target became stale/offline
        deck.update_live_frame(
            PaneFrame(
                lease=None,
                target=t1,
                phase="stale",
                agent_status="unknown",
                text_ansi="Last Known Output\n",
                error="Session offline",
                can_reply=False,
            )
        )
        await pilot.pause()

        state_text = str(live_pane.query_one("#live-pane-state").render())
        assert "stale" in state_text
        out_text = live_pane.query_one("#live-pane-output").render().plain
        assert "Last Known Output" in out_text
        assert live_pane.query_one("#btn-reply").disabled


@pytest.mark.asyncio
async def test_draft_pinning_across_selection_removal_and_cancellation():
    """Verify unsent draft text is pinned to original target and warns on target change."""
    app = HerdrMasterApp(demo=True)
    sock = Path("/tmp/pinning.sock")
    t1 = WorkspaceTarget("session", sock, "w1", "t1", "p1")
    l1 = PaneLease((str(sock), "w1", "p1"), 1, "term-1")

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        deck = app.query_one(CenterDeck)
        live_pane = app.query_one(LivePaneView)

        # Publish live frame and start a draft
        deck.update_live_frame(
            PaneFrame(
                lease=l1,
                target=t1,
                phase="live",
                agent_status="working",
                text_ansi="Hello\n",
                error=None,
                can_reply=True,
            )
        )
        await pilot.pause()

        await pilot.click("#btn-reply")
        await pilot.pause()

        ta = live_pane.query_one("#reply-text")
        ta.text = "My custom prompt for worker 1"
        await pilot.pause()

        btn_send = live_pane.query_one("#btn-send-reply")
        assert not btn_send.disabled

        # Target changes to a different pane p2
        t2 = WorkspaceTarget("session", sock, "w1", "t1", "p2")
        l2 = PaneLease((str(sock), "w1", "p2"), 2, "term-2")
        deck.update_live_frame(
            PaneFrame(
                lease=l2,
                target=t2,
                phase="live",
                agent_status="idle",
                text_ansi="Pane 2 output\n",
                error=None,
                can_reply=True,
            )
        )
        await pilot.pause()

        # Invalidation occurs: text is preserved, Send is disabled, warning is displayed
        assert ta.text == "My custom prompt for worker 1"
        assert btn_send.disabled
        state_text = str(live_pane.query_one("#live-pane-state").render())
        assert "target changed—cancel this draft to reply elsewhere" in state_text
        id_text = str(live_pane.query_one("#live-pane-identity").render())
        assert "p1" in id_text

        # Even if the original IDs reappear, Send stays disabled
        deck.update_live_frame(
            PaneFrame(
                lease=l1,
                target=t1,
                phase="live",
                agent_status="working",
                text_ansi="Hello again\n",
                error=None,
                can_reply=True,
            )
        )
        await pilot.pause()
        assert btn_send.disabled

        # Cancel clears the draft and returns controls to healthy pane
        await pilot.click("#btn-cancel-reply")
        await pilot.pause()

        assert not live_pane.has_retained_draft
        assert not live_pane.has_class("-reply-active")
        btn_reply = live_pane.query_one("#btn-reply")
        assert not btn_reply.disabled


@pytest.mark.asyncio
async def test_submission_feedback_double_send_and_stale_completion():
    """Verify accepted/rejected/uncertain submission feedback and stale lease exclusion."""
    app = HerdrMasterApp(demo=True)
    sock = Path("/tmp/sub.sock")
    t1 = WorkspaceTarget("session", sock, "w1", "t1", "p1")
    l1 = PaneLease((str(sock), "w1", "p1"), 1, "term-1")

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        deck = app.query_one(CenterDeck)
        live_pane = app.query_one(LivePaneView)

        deck.update_live_frame(
            PaneFrame(
                lease=l1,
                target=t1,
                phase="live",
                agent_status="working",
                text_ansi="Ready\n",
                error=None,
                can_reply=True,
            )
        )
        await pilot.pause()

        await pilot.click("#btn-reply")
        ta = live_pane.query_one("#reply-text")
        ta.text = "Hello Agent"
        await pilot.pause()

        # 1. Rejected outcome: retains draft text with explicit feedback
        deck.apply_reply_result(ReplyResult(lease=l1, outcome="rejected", message="Agent busy"))
        await pilot.pause()

        assert ta.text == "Hello Agent"
        state_text = str(live_pane.query_one("#live-pane-state").render())
        assert "Rejected: Agent busy" in state_text

        # 2. Uncertain outcome: retains draft text with explicit feedback
        deck.apply_reply_result(
            ReplyResult(lease=l1, outcome="uncertain", message="Socket timeout")
        )
        await pilot.pause()

        assert ta.text == "Hello Agent"
        state_text2 = str(live_pane.query_one("#live-pane-state").render())
        assert "Uncertain: Socket timeout" in state_text2

        # 3. Stale completion from an old lease does not affect current draft
        old_lease = PaneLease((str(sock), "w1", "p0"), 0, "term-0")
        deck.apply_reply_result(
            ReplyResult(lease=old_lease, outcome="accepted", message="Old accepted")
        )
        await pilot.pause()
        assert ta.text == "Hello Agent"
        assert live_pane.has_retained_draft

        # 4. Accepted outcome for current lease clears draft
        deck.apply_reply_result(
            ReplyResult(lease=l1, outcome="accepted", message="Prompt accepted")
        )
        await pilot.pause()
        assert not live_pane.has_retained_draft
        assert not live_pane.has_class("-reply-active")


@pytest.mark.asyncio
async def test_multiline_and_shortcut_typing_safety():
    """Verify typing letter shortcuts and Enter into composer does not trigger cockpit actions."""
    app = HerdrMasterApp(demo=True)
    sock = Path("/tmp/typing.sock")
    t1 = WorkspaceTarget("session", sock, "w1", "t1", "p1")
    l1 = PaneLease((str(sock), "w1", "p1"), 1, "term-1")

    app.action_new_master = Mock()
    app.action_new_ergo = Mock()
    app.action_open_settings = Mock()
    app.action_open_in_herdr = Mock()

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        deck = app.query_one(CenterDeck)
        live_pane = app.query_one(LivePaneView)

        deck.update_live_frame(
            PaneFrame(
                lease=l1,
                target=t1,
                phase="live",
                agent_status="working",
                text_ansi="Output\n",
                error=None,
                can_reply=True,
            )
        )
        await pilot.pause()

        await pilot.click("#btn-reply")
        await pilot.pause()

        ta = live_pane.query_one("#reply-text")
        ta.focus()
        await pilot.pause()

        # App and removed historical letter/digit bindings must remain ordinary text.
        for char in "new open quit 01234prmkd":
            await pilot.press(char)
        await pilot.press("enter")
        await pilot.pause()

        assert "new open quit 01234prmkd\n" in ta.text
        app.action_new_master.assert_not_called()
        app.action_new_ergo.assert_not_called()
        app.action_open_settings.assert_not_called()
        app.action_open_in_herdr.assert_not_called()
        assert app.return_value is None


@pytest.mark.asyncio
async def test_terminal_and_demo_mode_disabled_input():
    """Verify terminal shell panes and demo mode prevent prompt submission."""
    app = HerdrMasterApp(demo=True)
    sock = Path("/tmp/term.sock")

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        deck = app.query_one(CenterDeck)
        live_pane = app.query_one(LivePaneView)

        # Terminal shell pane
        t_term = WorkspaceTarget("session", sock, "w1", "t1", "p2")
        deck.update_live_frame(
            PaneFrame(
                lease=None,
                target=t_term,
                phase="live",
                agent_status="terminal",
                text_ansi="bash$ ls\n",
                error=None,
                can_reply=False,
            )
        )
        await pilot.pause()

        btn_reply = live_pane.query_one("#btn-reply")
        assert btn_reply.disabled
        state_text = str(live_pane.query_one("#live-pane-state").render())
        assert "terminal" in state_text


@pytest.mark.asyncio
async def test_literal_markup_and_control_sequence_rendering():
    """Verify terminal output containing raw Rich markup and ANSI escape codes renders literally."""
    app = HerdrMasterApp(demo=True)
    sock = Path("/tmp/markup.sock")
    t1 = WorkspaceTarget("session", sock, "w1", "t1", "p1")
    l1 = PaneLease((str(sock), "w1", "p1"), 1, "term-1")

    raw_output = (
        "[bold red]This is not markup[/bold red]\n"
        "\x1b[32mANSI Green Text\x1b[0m\n"
        "\x1b]0;Title Injection\x07Clean Line\n"
    )

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        deck = app.query_one(CenterDeck)
        live_pane = app.query_one(LivePaneView)

        deck.update_live_frame(
            PaneFrame(
                lease=l1,
                target=t1,
                phase="live",
                agent_status="working",
                text_ansi=raw_output,
                error=None,
                can_reply=True,
            )
        )
        await pilot.pause()

        out_text = live_pane.query_one("#live-pane-output").render().plain
        assert "[bold red]This is not markup[/bold red]" in out_text
        assert "ANSI Green Text" in out_text
        assert "Clean Line" in out_text
        assert "Title Injection" not in out_text


@pytest.mark.asyncio
async def test_pause_and_follow_output_behavior():
    """Verify scrolling up pauses output on snapshot and follow action resumes bottom tracking."""
    app = HerdrMasterApp(demo=True)
    sock = Path("/tmp/scroll.sock")
    t1 = WorkspaceTarget("session", sock, "w1", "t1", "p1")
    l1 = PaneLease((str(sock), "w1", "p1"), 1, "term-1")

    initial_lines = "\n".join(f"Line {i}" for i in range(50))

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        deck = app.query_one(CenterDeck)
        live_pane = app.query_one(LivePaneView)
        output = live_pane.query_one(LivePaneOutput)
        btn_follow = live_pane.query_one("#btn-follow-output")

        deck.update_live_frame(
            PaneFrame(
                lease=l1,
                target=t1,
                phase="live",
                agent_status="working",
                text_ansi=initial_lines,
                error=None,
                can_reply=True,
            )
        )
        await pilot.pause()

        assert not output.is_paused
        assert btn_follow.disabled

        # Scroll up triggers pause
        output.scroll_up()
        await pilot.pause()

        assert output.is_paused
        assert not btn_follow.disabled
        state_text = str(live_pane.query_one("#live-pane-state").render())
        assert "[Paused]" in state_text

        # Frame arrives while paused: rendered output remains on snapshot
        newer_lines = initial_lines + "\nLine 51 NEW ARRIVAL\n"
        deck.update_live_frame(
            PaneFrame(
                lease=l1,
                target=t1,
                phase="live",
                agent_status="working",
                text_ansi=newer_lines,
                error=None,
                can_reply=True,
            )
        )
        await pilot.pause()

        assert output.is_paused
        assert "Line 51 NEW ARRIVAL" not in output.render().plain

        # Click Follow button resumes at newest bottom
        await pilot.click("#btn-follow-output")
        await pilot.pause()

        assert not output.is_paused
        assert btn_follow.disabled
        assert "Line 51 NEW ARRIVAL" in output.render().plain


@pytest.mark.asyncio
async def test_negative_waiting_inference_limitation():
    """Verify blocked/idle/working panes never fabricate a confirmed-waiting badge."""
    app = HerdrMasterApp(demo=True)
    sock = Path("/tmp/limitation.sock")
    t1 = WorkspaceTarget("session", sock, "w1", "t1", "p1")
    l1 = PaneLease((str(sock), "w1", "p1"), 1, "term-1")

    # Output posing a question
    question_output = "Agent asks: What branch should I push to? (Waiting for input)\n"

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        deck = app.query_one(CenterDeck)
        live_pane = app.query_one(LivePaneView)

        # Frame reported as blocked by server
        deck.update_live_frame(
            PaneFrame(
                lease=l1,
                target=t1,
                phase="live",
                agent_status="blocked",
                text_ansi=question_output,
                error=None,
                can_reply=True,
            )
        )
        await pilot.pause()

        state_widget = live_pane.query_one("#live-pane-state")
        rendered_state = str(state_widget.render())
        # Truthful blocked description
        assert "! blocked (reason unknown—inspect output)" in rendered_state
        assert "waiting for you" not in rendered_state
        # Tooltip explanation
        assert (
            "Protocol 22 does not confirm whether user input is required"
            in str(state_widget.tooltip)
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\x00\x01\x07\x7f\x80\x9c\u202eb", "ab"),
        ("a\x1b]0;secret", "a"),
        ("a\x1bPsecret", "a"),
        ("a\x9dsecret\x9cb", "ab"),
        ("a\x90secret\x9cb", "ab"),
        ("a\x1b[?25lb\x9b2Jc", "abc"),
        ("a\x1b[31mred\x1b[0mz", "a\x1b[31mred\x1b[0mz"),
        ("a\x1b[31", "a"),
        ("a\x1b", "a"),
    ],
)
def test_unsafe_incomplete_and_c1_terminal_controls_are_stripped(raw, expected):
    assert sanitize_ansi(raw) == expected


@pytest.mark.asyncio
async def test_empty_snapshot_pause_requires_explicit_follow_and_ansi_deduplicates():
    app = HerdrMasterApp(demo=True)
    target = WorkspaceTarget("s", Path("/tmp/scroll.sock"), "w", "t", "p")
    lease = PaneLease((str(target.socket_path), "w", "p"), 1, "terminal")
    initial = "\x1b[31m" + "\n".join(f"line {n}" for n in range(80)) + "\x1b[0m"
    frame = PaneFrame(lease, target, "live", "working", initial, None, True)
    async with app.run_test(size=(80, 24)) as pilot:
        deck = app.query_one(CenterDeck)
        view = app.query_one(LivePaneView)
        output = view.query_one(LivePaneOutput)
        deck.update_live_frame(frame)
        await pilot.pause()
        assert output.scroll_y == output.max_scroll_y > 0
        output.scroll_up(animate=False, immediate=True)
        await pilot.pause()
        assert output.is_paused
        old_text, old_y = output.text.plain, output.scroll_y
        deck.update_live_frame(replace(frame, text_ansi=""))
        await pilot.pause()
        assert output.text.plain == old_text
        assert output.scroll_y == old_y
        output.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        assert output.is_paused  # Reaching the bottom is not consent to resume.
        await pilot.click("#btn-follow-output")
        await pilot.pause()
        assert not output.is_paused
        assert output.text.plain == ""  # Empty is a complete new snapshot.
        deck.update_live_frame(frame)
        await pilot.pause()
        position = output.scroll_y
        deck.update_live_frame(frame)
        await pilot.pause()
        assert output.scroll_y == position
        assert output.text.spans  # SGR formatting survived.


@pytest.mark.asyncio
async def test_invalid_draft_never_labels_new_output_as_original_and_cancel_restores_it():
    app = HerdrMasterApp(demo=True)
    a = WorkspaceTarget("s", Path("/tmp/a.sock"), "workspace-a", "t", "pane-a")
    b = WorkspaceTarget("s", Path("/tmp/b.sock"), "workspace-b", "t", "pane-b")
    la = PaneLease((str(a.socket_path), a.workspace_id, a.pane_id), 1, "a")
    lb = PaneLease((str(b.socket_path), b.workspace_id, b.pane_id), 2, "b")
    fa = PaneFrame(la, a, "live", "idle", "ORIGINAL", None, True)
    fb = PaneFrame(lb, b, "live", "working", "NEW TARGET CONTENT", None, True)
    async with app.run_test(size=(80, 24)) as pilot:
        deck = app.query_one(CenterDeck)
        view = app.query_one(LivePaneView)
        deck.update_live_frame(fa)
        await pilot.pause()
        await pilot.click("#btn-reply")
        view._reply_text.text = "private draft"
        await pilot.pause()
        deck.update_live_frame(fb)
        await pilot.pause()
        assert "Original target" in str(view._identity.render())
        assert str(a.socket_path) in str(view._identity.tooltip)
        assert "NEW TARGET CONTENT" not in view._output.text.plain
        assert view._reply_text.text == "private draft"
        assert view._btn_send.disabled
        await pilot.click("#btn-cancel-reply")
        await pilot.pause()
        assert "NEW TARGET CONTENT" in view._output.text.plain
        assert "workspace-b" in str(view._identity.render())


@pytest.mark.asyncio
async def test_direct_send_checks_readiness_and_stale_completion_cannot_unlock_send():
    app = HerdrMasterApp(demo=True)
    target = WorkspaceTarget("s", Path("/tmp/guard.sock"), "w", "t", "p")
    lease = PaneLease((str(target.socket_path), "w", "p"), 1, "one")
    other = replace(lease, generation=2)
    frame = PaneFrame(lease, target, "live", "idle", "output", None, True)
    async with app.run_test(size=(80, 24)) as pilot:
        view = app.query_one(LivePaneView)
        app.query_one(CenterDeck).update_live_frame(frame)
        await pilot.pause()
        await pilot.click("#btn-reply")
        view._reply_text.text = "hello"
        view.update_frame(replace(frame, can_reply=False))
        view.send_draft()
        assert not view._send_in_flight
        view._send_in_flight = True
        view.apply_reply_result(ReplyResult(other, "accepted", "late"))
        assert view._send_in_flight
        assert view._reply_text.text == "hello"
        view.cancel_draft()  # A pending send cannot be presented as retracted.
        assert view._reply_text.text == "hello"


class EventClient:
    """Offline RPC fake with independently gated preflight and dispatch."""

    def __init__(self, inventory):
        self.inventory = inventory
        self.snapshot_gate = None
        self.preflight_entered = asyncio.Event()
        self.dispatch_gate = None
        self.dispatch_entered = asyncio.Event()
        self.prompts = []
        self.focus_workspace = AsyncMock()
        self.focus_tab = AsyncMock()
        self.focus_pane = AsyncMock()
        self.send_keys = AsyncMock()
        self.attach = AsyncMock()
        self.launch = AsyncMock()

    async def get_snapshot(self):
        gate, self.snapshot_gate = self.snapshot_gate, None
        if gate is not None:
            self.preflight_entered.set()
            await gate.wait()
        return self.inventory.snapshot

    async def read_pane(self, pane_id, *, format, lines):
        assert format == "ansi" and lines == 1000
        ws, tab, _ = snapshot_identity(self.inventory.snapshot, pane_id)
        return pane_read_record(pane_id, ws, tab, f"{self.inventory.socket_path} / {pane_id}")

    async def prompt_agent(self, **kwargs):
        self.prompts.append(kwargs)
        self.dispatch_entered.set()
        if self.dispatch_gate is not None:
            await self.dispatch_gate.wait()
        pane_id = kwargs["target"]
        ws, tab, terminal = snapshot_identity(self.inventory.snapshot, pane_id)
        return agent_prompted(pane_id, ws, tab, terminal)


class EventSubscription:
    def __init__(self, close_gate=None):
        self.close_gate = close_gate
        self.close_entered = asyncio.Event()
        self.stop = asyncio.Event()

    async def connect(self):
        return {"result": {"type": "subscription_started"}}

    async def events(self):
        await self.stop.wait()
        if False:
            yield None

    async def close(self):
        self.close_entered.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.stop.set()


def make_two_agent_inventory(path, name="session"):
    return make_inventory(
        path,
        name,
        panes=[
            {"pane_id": pane, "tab_id": "t1", "agent": "worker", "terminal_id": pane + "-term"}
            for pane in ("p1", "p2")
        ],
        agents=[
            {"pane_id": pane, "agent": "worker", "interactive_ready": True}
            for pane in ("p1", "p2")
        ],
    )


def install_event_controller(app, inventories, close_gate=None):
    app.app_state.live_sessions = inventories
    app.app_state.inventory_demo = False
    clients = {str(inv.socket_path): EventClient(inv) for inv in inventories}
    live_events = {}
    subscriptions = []

    def publish(frame):
        app._on_live_pane_frame(frame)
        if frame.phase == "live" and frame.target:
            key = (str(frame.target.socket_path), frame.target.pane_id)
            live_events.setdefault(key, asyncio.Event()).set()

    def subscription_factory(path, filters):
        subscription = EventSubscription(close_gate)
        subscriptions.append(subscription)
        return subscription

    app._live_controller = LivePaneController(
        publish=publish,
        client_factory=lambda session, path: clients[str(path)],
        subscription_factory=subscription_factory,
        poll_interval=30,
    )
    app.notify = Mock()
    return clients, live_events, subscriptions


async def select_pane(pilot, browser, index, event):
    workspace = browser._tree.root.children[0]
    workspace.expand()
    browser._tree.select_node(workspace.children[index])
    await asyncio.wait_for(event.wait(), timeout=2)
    await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_at", ["preflight", "dispatch"])
async def test_real_controller_selection_change_cancels_authorization_and_names_outcome(cancel_at):
    app = HerdrMasterApp(demo=True)
    inv = make_two_agent_inventory(Path("/tmp/integration-a.sock"))
    clients, live_events, _ = install_event_controller(app, [inv])
    client = clients[str(inv.socket_path)]
    first_live = live_events.setdefault((str(inv.socket_path), "p1"), asyncio.Event())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.demo = False
        browser = app.query_one(WorkspaceBrowser)
        view = app.query_one(LivePaneView)
        await select_pane(pilot, browser, 0, first_live)
        await pilot.click("#btn-reply")
        view._reply_text.text = "first line\nsecond line"
        await pilot.pause()
        if cancel_at == "preflight":
            client.snapshot_gate = asyncio.Event()
            entered = client.preflight_entered
        else:
            client.dispatch_gate = asyncio.Event()
            entered = client.dispatch_entered
        await pilot.click("#btn-send-reply")
        await asyncio.wait_for(entered.wait(), timeout=2)
        browser._tree.select_node(browser._tree.root.children[0].children[1])
        await pilot.pause()
        assert view._draft_invalid and view._btn_send.disabled
        assert view._reply_text.text == "first line\nsecond line"
        assert not view._send_in_flight
        messages = [call.args[0] for call in app.notify.call_args_list]
        assert any(str(inv.socket_path) in msg and "workspace: w1" in msg and "pane: p1" in msg
                   and ("Rejected" if cancel_at == "preflight" else "Uncertain") in msg
                   for msg in messages)
        if cancel_at == "preflight":
            assert client.prompts == []
        else:
            assert client.prompts == [{"target": "p1", "prompt": "first line\nsecond line",
                                       "max_attempts": 1}]
        for forbidden in (client.focus_workspace, client.focus_tab, client.focus_pane,
                          client.send_keys, client.attach, client.launch):
            forbidden.assert_not_called()


@pytest.mark.asyncio
async def test_rapid_a_b_a_discards_late_frames_and_serializes_slow_watcher_teardown():
    app = HerdrMasterApp(demo=True)
    inv = make_two_agent_inventory(Path("/tmp/switch.sock"))
    gate = asyncio.Event()
    clients, live_events, subscriptions = install_event_controller(app, [inv], gate)
    del clients
    first_live = live_events.setdefault((str(inv.socket_path), "p1"), asyncio.Event())
    async with app.run_test(size=(80, 24)) as pilot:
        try:
            await pilot.pause()
            app.demo = False
            browser = app.query_one(WorkspaceBrowser)
            view = app.query_one(LivePaneView)
            await select_pane(pilot, browser, 0, first_live)
            old_frame = app._last_live_frame
            nodes = browser._tree.root.children[0].children
            browser._tree.select_node(nodes[1])
            await asyncio.wait_for(subscriptions[0].close_entered.wait(), timeout=2)
            await pilot.pause()
            assert view._current_frame.target.pane_id == "p2"
            browser._tree.select_node(nodes[0])
            await pilot.pause()
            app._on_live_pane_frame(old_frame)
            assert view._current_frame.phase == "loading"
            gate.set()
            await pilot.pause()
            await pilot.pause()
            assert app._live_controller._active_target.pane_id == "p1"
            assert view._current_frame.target.pane_id == "p1"
            assert view._current_frame.lease != old_frame.lease
        finally:
            gate.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["offline", "incarnation", "not-ready"])
async def test_inventory_invalidation_blocks_pending_preflight_before_reconcile(change):
    app = HerdrMasterApp(demo=True)
    inv = make_two_agent_inventory(Path("/tmp/inventory-guard.sock"))
    clients, live_events, _ = install_event_controller(app, [inv])
    client = clients[str(inv.socket_path)]
    first_live = live_events.setdefault((str(inv.socket_path), "p1"), asyncio.Event())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.demo = False
        browser = app.query_one(WorkspaceBrowser)
        view = app.query_one(LivePaneView)
        await select_pane(pilot, browser, 0, first_live)
        await pilot.click("#btn-reply")
        view._reply_text.text = "do not send"
        await pilot.pause()
        client.snapshot_gate = asyncio.Event()
        await pilot.click("#btn-send-reply")
        await asyncio.wait_for(client.preflight_entered.wait(), timeout=2)
        updated = inv.model_copy(deep=True) if hasattr(inv, "model_copy") else None
        if updated is None:
            import copy
            updated = copy.deepcopy(inv)
        if change == "offline":
            updated.error = "offline"
        elif change == "incarnation":
            updated.snapshot.panes[0].terminal_id = "replacement"
        else:
            updated.snapshot.agents[0].interactive_ready = False
        app.app_state.live_sessions = [updated]
        app._guard_live_inventory()
        await pilot.pause()
        assert client.prompts == []
        assert view._reply_text.text == "do not send"
        assert view._btn_send.disabled
        if change != "not-ready":
            assert view._draft_invalid
        assert any("Rejected" in call.args[0] for call in app.notify.call_args_list)


@pytest.mark.asyncio
async def test_keyboard_target_details_identify_original_draft_at_narrow_width():
    app = HerdrMasterApp(demo=True)
    target = WorkspaceTarget(
        "session-[red]", Path("/tmp/" + "long-socket-" * 5 + "[literal].sock"),
        "workspace-" + "long-" * 8, "tab-original", "pane-original",
    )
    lease = PaneLease((str(target.socket_path), target.workspace_id, target.pane_id), 4, "term-id")
    frame = PaneFrame(lease, target, "live", "blocked", "original", None, True)
    other = WorkspaceTarget("other", Path("/tmp/other.sock"), "workspace-b", "tb", "pb")
    other_lease = PaneLease((str(other.socket_path), other.workspace_id, other.pane_id), 5, "tb")
    async with app.run_test(size=(40, 24)) as pilot:
        deck = app.query_one(CenterDeck)
        view = app.query_one(LivePaneView)
        deck.update_live_frame(frame)
        await pilot.pause()
        await pilot.click("#btn-reply")
        view._reply_text.text = "original draft"
        deck.update_live_frame(PaneFrame(other_lease, other, "live", "idle", "other", None, True))
        await pilot.pause()
        assert view._draft_invalid
        assert view._btn_send.disabled
        # Tab navigation must expose the full identity without relying on hover.
        view._reply_text.focus()
        for _ in range(8):
            if app.focused is view._btn_target:
                break
            await pilot.press("tab")
        assert app.focused is view._btn_target
        await pilot.press("enter")
        await pilot.pause()
        from aiheaddev.ui.tui.widgets.live_output import TargetDetails

        assert isinstance(app.screen, TargetDetails)
        assert app.focused.id == "target-details-scroll"
        details = app.screen.query_one("#target-details-text").render().plain
        assert "Original draft target" in details
        assert str(target.socket_path) in details
        assert target.workspace_id in details
        assert "pane-original" in details and "term-id" in details
        assert "workspace-b" not in details
        assert "Status legend" in details
        assert "Protocol 22 does not confirm whether user input is required" in details
        await pilot.press("q")  # No app-wide quit while a modal is active.
        assert isinstance(app.screen, TargetDetails)
        await pilot.press("escape")
        await pilot.pause()
        assert view._reply_text.text == "original draft"
        assert view._draft_invalid


@pytest.mark.asyncio
async def test_small_state_line_exposes_stale_before_long_blocked_explanation():
    app = HerdrMasterApp(demo=True)
    target = WorkspaceTarget("s", Path("/tmp/stale.sock"), "w", "t", "p")
    async with app.run_test(size=(40, 24)) as pilot:
        app.query_one(CenterDeck).update_live_frame(
            PaneFrame(None, target, "stale", "blocked", "", "Target vanished", False)
        )
        await pilot.pause()
        line = app.query_one(LivePaneView)._state_bar.render_line(0).text
        assert "stale" in line
        assert "waiting" not in line.lower()


@pytest.mark.asyncio
async def test_real_controller_routes_identical_ids_to_exact_second_socket():
    app = HerdrMasterApp(demo=True)
    first = make_two_agent_inventory(Path("/tmp/socket-first.sock"), "same-session")
    second = make_two_agent_inventory(Path("/tmp/socket-second.sock"), "same-session")
    clients, live_events, subscriptions = install_event_controller(app, [first, second])
    one = live_events.setdefault((str(first.socket_path), "p1"), asyncio.Event())
    two = live_events.setdefault((str(second.socket_path), "p1"), asyncio.Event())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.demo = False
        browser = app.query_one(WorkspaceBrowser)
        nodes = browser._tree.root.children
        first_ws = next(node for node in nodes if node.data.socket_path == first.socket_path)
        second_ws = next(node for node in nodes if node.data.socket_path == second.socket_path)
        first_ws.expand()
        browser._tree.select_node(first_ws.children[0])
        await asyncio.wait_for(one.wait(), 2)
        second_ws.expand()
        browser._tree.select_node(second_ws.children[0])
        await asyncio.wait_for(two.wait(), 2)
        await pilot.pause()
        view = app.query_one(LivePaneView)
        assert str(second.socket_path) in view._output.text.plain
        assert subscriptions[0].close_entered.is_set()
        await pilot.click("#btn-reply")
        view._reply_text.text = "only second"
        await pilot.pause()
        await pilot.click("#btn-send-reply")
        await asyncio.wait_for(clients[str(second.socket_path)].dispatch_entered.wait(), 2)
        await pilot.pause()
        assert clients[str(first.socket_path)].prompts == []
        assert clients[str(second.socket_path)].prompts == [
            {"target": "p1", "prompt": "only second", "max_attempts": 1}
        ]
        assert any("Accepted" in call.args[0] and str(second.socket_path) in call.args[0]
                   for call in app.notify.call_args_list)
        for client in clients.values():
            for forbidden in (client.focus_workspace, client.focus_tab, client.focus_pane,
                              client.send_keys, client.attach, client.launch):
                forbidden.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_serializes_with_slow_selection_teardown():
    app = HerdrMasterApp(demo=True)
    inv = make_two_agent_inventory(Path("/tmp/shutdown.sock"))
    gate = asyncio.Event()
    _, live_events, subscriptions = install_event_controller(app, [inv], gate)
    first = live_events.setdefault((str(inv.socket_path), "p1"), asyncio.Event())
    async with app.run_test(size=(80, 24)) as pilot:
        try:
            await pilot.pause()
            app.demo = False
            browser = app.query_one(WorkspaceBrowser)
            await select_pane(pilot, browser, 0, first)
            browser._tree.select_node(browser._tree.root.children[0].children[1])
            await asyncio.wait_for(subscriptions[0].close_entered.wait(), 2)
            close_task = asyncio.create_task(app._close_live_controller())
            await asyncio.sleep(0)
            assert app._live_closed and not close_task.done()
            gate.set()
            await asyncio.wait_for(close_task, 2)
            assert app._live_controller._closed
            assert app._live_controller._active_lease is None
            assert app._live_controller._watch_task is None
        finally:
            gate.set()


@pytest.mark.asyncio
async def test_inventory_update_supersedes_selection_during_old_watcher_teardown():
    import copy

    app = HerdrMasterApp(demo=True)
    inv = make_two_agent_inventory(Path("/tmp/selection-inventory-race.sock"))
    gate = asyncio.Event()
    clients, live_events, subscriptions = install_event_controller(app, [inv], gate)
    first = live_events.setdefault((str(inv.socket_path), "p1"), asyncio.Event())
    frames = []
    update = app._deck.update_live_frame

    def capture(frame):
        frames.append(frame)
        update(frame)

    app._deck.update_live_frame = capture
    async with app.run_test(size=(80, 24)) as pilot:
        try:
            await pilot.pause()
            app.demo = False
            browser = app.query_one(WorkspaceBrowser)
            await select_pane(pilot, browser, 0, first)
            browser._tree.select_node(browser._tree.root.children[0].children[1])
            await asyncio.wait_for(subscriptions[0].close_entered.wait(), 2)
            newer = copy.deepcopy(inv)
            newer.snapshot.panes = [pane for pane in newer.snapshot.panes if pane.pane_id != "p2"]
            newer.snapshot.agents = [
                agent for agent in newer.snapshot.agents if agent.pane_id != "p2"
            ]
            app.app_state.live_sessions = [newer]
            reconcile = asyncio.create_task(app._reconcile_live_inventory())
            await asyncio.sleep(0)
            assert not app._live_ready
            gate.set()
            await asyncio.wait_for(reconcile, 2)
            await pilot.pause()
            view = app.query_one(LivePaneView)
            assert view._current_frame.target.pane_id == "p2"
            assert view._current_frame.phase == "unavailable"
            assert view._btn_reply.disabled
            assert not any(frame.target and frame.target.pane_id == "p2" and frame.can_reply
                           for frame in frames)
            assert clients[str(inv.socket_path)].prompts == []
        finally:
            gate.set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "output"),
    [
        ("idle", "What branch should I use?"),
        ("done", "Old approval request: may I continue?"),
        ("working", "Ready for input? (historical output)"),
        ("unknown", "Please approve"),
    ],
)
async def test_questions_and_readiness_do_not_imply_waiting(status, output):
    app = HerdrMasterApp(demo=True)
    target = WorkspaceTarget("s", Path("/tmp/not-waiting.sock"), "w", "t", "p")
    lease = PaneLease((str(target.socket_path), "w", "p"), 1, "terminal")
    async with app.run_test(size=(80, 24)) as pilot:
        app.query_one(CenterDeck).update_live_frame(
            PaneFrame(lease, target, "live", status, output, None, True)
        )
        await pilot.pause()
        view = app.query_one(LivePaneView)
        state = view._state_bar.render().plain.lower()
        assert "waiting" not in state
        assert "input need unconfirmed" in state
        assert view._output.text.plain == output


@pytest.mark.asyncio
async def test_tree_and_identity_treat_names_as_literal_and_escape_controls():
    app = HerdrMasterApp(demo=True)
    esc, bel = chr(27), chr(7)
    inv = make_two_agent_inventory(Path("/tmp/literal.sock"), "[red]session" + esc + "[0m")
    inv.snapshot.workspaces[0].label = "[bold]workspace" + esc + "[2J"
    inv.snapshot.agents[0].name = "[blue]agent" + bel
    app.app_state.inventory_demo = False
    app.app_state.live_sessions = [inv]
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        browser = app.query_one(WorkspaceBrowser)
        workspace = browser._tree.root.children[0]
        workspace_label = str(workspace.label)
        pane_label = str(workspace.children[0].label)
        assert "[red]session" in workspace_label and "[bold]workspace" in workspace_label
        assert "[blue]agent" in pane_label
        assert r"\x1b" in workspace_label and r"\x07" in pane_label
        assert esc not in workspace_label and bel not in pane_label
        target = WorkspaceTarget(inv.session_name, inv.socket_path, "w1", "t1", "p1")
        lease = PaneLease((str(inv.socket_path), "w1", "p1"), 1, "terminal")
        app.query_one(CenterDeck).update_live_frame(
            PaneFrame(lease, target, "live", "idle", "", "[red]error" + esc + "[2J", True)
        )
        await pilot.pause()
        view = app.query_one(LivePaneView)
        assert r"[red]session\x1b[0m" in view._identity.render().plain
        assert r"[red]error\x1b[2J" in view._state_bar.render().plain


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["accepted", "rejected", "uncertain"])
async def test_invalid_original_draft_keeps_delivery_outcome_visible(outcome):
    app = HerdrMasterApp(demo=True)
    a = WorkspaceTarget("s", Path("/tmp/outcome-a.sock"), "w", "t", "a")
    b = WorkspaceTarget("s", Path("/tmp/outcome-b.sock"), "w", "t", "b")
    la = PaneLease((str(a.socket_path), "w", "a"), 1, "a")
    lb = PaneLease((str(b.socket_path), "w", "b"), 2, "b")
    async with app.run_test(size=(40, 24)) as pilot:
        deck = app.query_one(CenterDeck)
        view = app.query_one(LivePaneView)
        deck.update_live_frame(PaneFrame(la, a, "live", "idle", "", None, True))
        await pilot.pause()
        await pilot.click("#btn-reply")
        view._reply_text.text = "preserve this original draft"
        deck.update_live_frame(PaneFrame(lb, b, "live", "idle", "", None, True))
        view.apply_reply_result(ReplyResult(la, outcome, "delivery detail"))
        await pilot.pause()
        line = view._state_bar.render_line(0).text.lower()
        assert outcome in line and "target changed" in line
        assert view._reply_text.text == "preserve this original draft"
        assert view._btn_send.disabled
        if outcome != "accepted":
            assert "delivery detail" in str(view._state_bar.tooltip)


@pytest.mark.asyncio
async def test_completion_feedback_does_not_follow_a_new_target():
    app = HerdrMasterApp(demo=True)
    a = WorkspaceTarget("s", Path("/tmp/feedback-a.sock"), "w", "t", "a")
    b = WorkspaceTarget("s", Path("/tmp/feedback-b.sock"), "w", "t", "b")
    la = PaneLease((str(a.socket_path), "w", "a"), 1, "a")
    lb = PaneLease((str(b.socket_path), "w", "b"), 2, "b")
    async with app.run_test(size=(40, 24)) as pilot:
        deck = app.query_one(CenterDeck)
        view = app.query_one(LivePaneView)
        deck.update_live_frame(PaneFrame(la, a, "live", "idle", "", None, True))
        await pilot.pause()
        await pilot.click("#btn-reply")
        view._reply_text.text = "hello"
        view.apply_reply_result(ReplyResult(la, "accepted", "ok"))
        assert "Prompt accepted" in view._state_bar.render().plain
        deck.update_live_frame(PaneFrame(lb, b, "stale", "blocked", "", "offline", False))
        await pilot.pause()
        assert "Prompt accepted" not in view._state_bar.render().plain
        assert "stale" in view._state_bar.render_line(0).text
