"""Comprehensive regression test suite for AiHead exact-target presentation, navigation, and responsive chrome.

Covers Findings F1-F5, F8 as specified in Task 3:
- Test Group A: Settings navigation, stack guard narrowing, and focus guards (F1).
- Test Group B: Pending fullscreen, exact identity binding, and ensure edge cases (F2).
- Test Group C: Covered state publication, readiness eligibility, and inventory guards (F3).
- Test Group D: Two-step Escape, focus-return routing, raw Escape, and Tab forwarding (F4).
- Test Group E: Responsive geometry, narrow class, and reversible resize breakpoints (F5).
- Test Group F8: Discoverability, contextual fallback notification order, and tooltips (F8).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from textual.widgets import Button, Input, TextArea

from aiheaddev.ui.tui.app import HerdrMasterApp
from aiheaddev.ui.tui.live_pane import PaneFrame, PaneLease
from aiheaddev.ui.tui.modals.log_viewer import LogViewerModal
from aiheaddev.ui.tui.modals.settings import SettingsModal
from aiheaddev.ui.tui.screens.ai_head_screen import AiHeadScreen
from aiheaddev.ui.tui.widgets.live_output import FocusReturnRequested, LivePaneOutput, LivePaneView
from aiheaddev.ui.tui.widgets.workspaces import WorkspaceBrowser, WorkspaceTarget
from herdr_master.core.config import HerdrMasterConfig
from herdr_master.core.models import Squad
from herdr_master.herdr.discovery import SessionInventory
from herdr_master.herdr.snapshot import (
    AgentSnapshot,
    PaneSnapshot,
    SessionSnapshot,
    TabSnapshot,
    WorkspaceSnapshot,
)


def _make_inventory(
    socket_path: str = "/tmp/herdr.sock",
    ws_id: str = "ws-head",
    tab_id: str = "tab-head",
    pane_id: str = "pane-head",
    agent_status: str = "ready",
    is_agent: bool = True,
) -> SessionInventory:
    snap = SessionSnapshot(
        protocol=22,
        focused_workspace_id=ws_id,
        workspaces=[WorkspaceSnapshot(workspace_id=ws_id, label="main-repo")],
        tabs=[TabSnapshot(tab_id=tab_id, workspace_id=ws_id, label="ai-head")],
        panes=[PaneSnapshot(pane_id=pane_id, tab_id=tab_id, workspace_id=ws_id, agent="agy" if is_agent else None)],
        agents=[
            AgentSnapshot(
                pane_id=pane_id,
                workspace_id=ws_id,
                tab_id=tab_id,
                agent="agy",
                name="ai-head",
                agent_status=agent_status,
                cwd="/test/repo",
            )
        ]
        if is_agent
        else [],
    )
    return SessionInventory(session_name="test-session", socket_path=Path(socket_path), snapshot=snap)


def _create_app(tmp_path: Path) -> HerdrMasterApp:
    app = HerdrMasterApp(demo=False, project_path=tmp_path)
    client = MagicMock()
    client.socket_path = "/tmp/herdr.sock"
    client.session = "test-session"
    app.client = client
    app.session_name = "test-session"
    app.app_state.session_name = "test-session"
    app.app_state.workspace_id = "ws-head"
    return app


# =============================================================================
# Test Group A: Settings Navigation & Focus Guards (F1)
# =============================================================================


@pytest.mark.asyncio
async def test_a_settings_open_from_aihead_screen_and_return_to_same_instance(tmp_path: Path):
    """F1: Settings button Enter and 's' from Back open SettingsModal; dismiss returns to same screen."""
    app = _create_app(tmp_path)

    ensure_result = {
        "workspace_id": "ws-head",
        "tab_id": "tab-head",
        "pane_id": "pane-head",
        "status": "ready",
        "busy": False,
    }

    with patch("aiheaddev.ui.tui.app.ensure_herdr_ai_head", new_callable=AsyncMock) as mock_ensure:
        mock_ensure.return_value = ensure_result

        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            app.action_launch_aihead()
            await pilot.pause()

            head_screen = app.screen
            assert isinstance(head_screen, AiHeadScreen)
            assert len(app.screen_stack) == 2

            # Focus the Back button and press 's'
            head_screen.query_one("#btn-aihead-back").focus()
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()

            # SettingsModal must be open
            assert isinstance(app.screen, SettingsModal)
            assert len(app.screen_stack) == 3

            # Press Escape / Cancel to dismiss Settings
            await pilot.press("escape")
            await pilot.pause()

            # Must return cleanly to the exact same mounted AiHeadScreen instance
            assert app.screen is head_screen
            assert len(app.screen_stack) == 2
            assert mock_ensure.call_count == 1  # No extra ensure call!

            # Now test activating Settings button via click
            await pilot.click("#btn-aihead-settings")
            await pilot.pause()

            assert isinstance(app.screen, SettingsModal)
            assert len(app.screen_stack) == 3

            # Test duplicate action_open_settings does nothing while modal is open
            app.action_open_settings()
            await pilot.pause()
            assert len(app.screen_stack) == 3

            # Simulate successful save dismissal
            settings_modal = app.screen
            new_config = HerdrMasterConfig()
            new_squad = Squad(members=[])
            settings_modal.dismiss((new_config, new_squad))
            await pilot.pause()

            # Returns to identical mounted fullscreen object
            assert app.screen is head_screen
            assert len(app.screen_stack) == 2
            assert mock_ensure.call_count == 1  # Still no extra ensure!


@pytest.mark.asyncio
async def test_a_settings_shortcut_guarded_when_text_or_output_focused(tmp_path: Path):
    """F1: Letters typed in prompt input are text; Settings shortcut is guarded."""
    app = _create_app(tmp_path)

    with patch("aiheaddev.ui.tui.app.ensure_herdr_ai_head", new_callable=AsyncMock) as mock_ensure:
        mock_ensure.return_value = {
            "workspace_id": "ws-head",
            "tab_id": "tab-head",
            "pane_id": "pane-head",
            "status": "ready",
            "busy": False,
        }
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            app.action_launch_aihead()
            await pilot.pause()

            head_screen = app.screen
            assert isinstance(head_screen, AiHeadScreen)

            sock_path = Path(getattr(app.client, "socket_path", "/tmp/herdr.sock"))
            head_target = WorkspaceTarget(
                session_name="test-session",
                socket_path=sock_path,
                workspace_id="ws-head",
                tab_id="tab-head",
                pane_id="pane-head",
            )
            lease = PaneLease(key=(str(sock_path), "ws-head", "pane-head"), generation=1, terminal_id="term-1")
            live_frame = PaneFrame(
                lease=lease,
                target=head_target,
                phase="live",
                agent_status="ready",
                text_ansi="aihead prompt",
                error=None,
                can_reply=True,
                reply_eligible=True,
            )

            app._publish_live_frame(live_frame)
            await pilot.pause()

            prompt_input = head_screen.query_one("#prompt-input", Input)
            prompt_input.focus()
            await pilot.pause()
            assert app.focused is prompt_input

            # Typing 's' into prompt input should type 's', not open SettingsModal
            await pilot.press("s")
            await pilot.pause()

            assert isinstance(app.screen, AiHeadScreen)
            assert prompt_input.value == "s"

            # Focusing LivePaneOutput also guards against action_open_settings
            output_widget = head_screen.query_one("#live-pane-output", LivePaneOutput)
            output_widget.focus()
            await pilot.pause()
            assert app.focused is output_widget

            app.action_open_settings()
            await pilot.pause()
            assert isinstance(app.screen, AiHeadScreen)


# =============================================================================
# Test Group B: Pending Presentation & Exact-Target Binding (F2)
# =============================================================================


@pytest.mark.asyncio
async def test_b_pending_fullscreen_isolates_worker_and_exact_target_binding(tmp_path: Path):
    """F2: Unrelated worker frame is not leaked; fullscreen presents truthful pending state."""
    app = _create_app(tmp_path)

    worker_target = WorkspaceTarget(
        session_name="test-session",
        socket_path=Path("/tmp/worker.sock"),
        workspace_id="ws-worker",
        tab_id="tab-worker",
        pane_id="pane-worker",
    )
    worker_lease = PaneLease(
        key=("/tmp/worker.sock", "ws-worker", "pane-worker"), generation=1, terminal_id="term-w"
    )
    worker_frame = PaneFrame(
        lease=worker_lease,
        target=worker_target,
        phase="live",
        agent_status="working",
        text_ansi="WORKER ACTIVE PROCESS OUTPUT",
        error=None,
        can_reply=True,
        reply_eligible=True,
    )
    app._last_live_frame = worker_frame

    ensure_started = asyncio.Event()
    ensure_release = asyncio.Event()

    async def _mock_held_ensure(*args, **kwargs):
        ensure_started.set()
        await ensure_release.wait()
        return {
            "workspace_id": "ws-head",
            "tab_id": "tab-head",
            "pane_id": "pane-head",
            "status": "ready",
            "busy": False,
        }

    with patch("aiheaddev.ui.tui.app.ensure_herdr_ai_head", side_effect=_mock_held_ensure) as mock_ensure:
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()

            # Launch fullscreen AiHead
            app.action_launch_aihead()
            await ensure_started.wait()
            await pilot.pause()

            # Fullscreen screen is mounted while ensure is held
            head_screen = app.screen
            assert isinstance(head_screen, AiHeadScreen)

            # Assert: NO worker output, enabled input, or ready metadata while held!
            live_view = head_screen._live_pane_view
            assert live_view._current_frame is not None
            assert live_view._current_frame.phase == "loading"
            assert live_view._current_frame.text_ansi == ""
            assert live_view._current_frame.lease is None
            assert live_view._prompt_input.disabled is True
            assert head_screen.status != "ready"
            assert head_screen.status == "pending"

            # Repeating action while ensure is pending must not duplicate ensure or leak worker
            app.action_launch_aihead()
            await pilot.pause()
            assert mock_ensure.call_count == 1

            # 3. Release ensure result
            ensure_release.set()
            await pilot.pause()
            await pilot.pause()

            # Target is now bound, but until authoritative frame arrives, still loading/disabled!
            assert app._ai_head_confirmed_target is not None
            assert app._ai_head_confirmed_target.pane_id == "pane-head"
            assert live_view._prompt_input.disabled is True

            # 4. Attempt to publish worker frame -> MUST BE REJECTED
            app._publish_live_frame(worker_frame)
            await pilot.pause()
            assert live_view._current_frame.text_ansi != "WORKER ACTIVE PROCESS OUTPUT"

            # Attempt to publish frame with same workspace/pane but different socket -> REJECTED
            cross_socket_target = WorkspaceTarget(
                session_name="test-session",
                socket_path=Path("/tmp/other.sock"),
                workspace_id="ws-head",
                tab_id="tab-head",
                pane_id="pane-head",
            )
            cross_socket_frame = PaneFrame(
                lease=PaneLease(key=("/tmp/other.sock", "ws-head", "pane-head"), generation=1, terminal_id="term-x"),
                target=cross_socket_target,
                phase="live",
                agent_status="ready",
                text_ansi="CROSS SOCKET OUTPUT",
                error=None,
                can_reply=True,
                reply_eligible=True,
            )
            app._publish_live_frame(cross_socket_frame)
            await pilot.pause()
            assert live_view._current_frame.text_ansi != "CROSS SOCKET OUTPUT"

            # 5. Publish matching authoritative head frame -> ACCEPTED
            sock_path = Path(getattr(app.client, "socket_path", "/tmp/herdr.sock"))
            head_target = WorkspaceTarget(
                session_name="test-session",
                socket_path=sock_path,
                workspace_id="ws-head",
                tab_id="tab-head",
                pane_id="pane-head",
            )
            head_lease = PaneLease(
                key=(str(sock_path), "ws-head", "pane-head"), generation=2, terminal_id="term-head"
            )
            head_frame = PaneFrame(
                lease=head_lease,
                target=head_target,
                phase="live",
                agent_status="ready",
                text_ansi="👑 AIHEAD AUTHORITATIVE READY",
                error=None,
                can_reply=True,
                reply_eligible=True,
            )
            app._publish_live_frame(head_frame)
            await pilot.pause()

            assert live_view._current_frame.lease == head_lease
            assert live_view._current_frame.text_ansi == "👑 AIHEAD AUTHORITATIVE READY"
            assert live_view._prompt_input.disabled is False


@pytest.mark.asyncio
async def test_b_ensure_failure_and_unusable_result(tmp_path: Path):
    """F2: Ensure exception and unusable result leave disabled actionable error with Back."""
    app = _create_app(tmp_path)

    with patch("aiheaddev.ui.tui.app.ensure_herdr_ai_head", new_callable=AsyncMock) as mock_ensure:
        # Test 1: Exception during ensure
        mock_ensure.side_effect = RuntimeError("Herdr RPC failed")
        with patch.object(app, "notify", wraps=app.notify) as spy_notify1:
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                app.action_launch_aihead()
                await pilot.pause()
                await pilot.pause()

                head_screen = app.screen
                assert isinstance(head_screen, AiHeadScreen)
                assert head_screen.status == "error"
                live_view = head_screen._live_pane_view
                assert live_view._current_frame.phase == "unavailable"
                assert live_view._current_frame.can_reply is False
                assert live_view._prompt_input.disabled is True
                assert "Back [Esc]" in (live_view._current_frame.error or "")

                # Press Escape to return
                await pilot.press("escape")
                await pilot.pause()
                assert not isinstance(app.screen, AiHeadScreen)

                # Exception must not announce success
                notified1 = [call.args[0] for call in spy_notify1.call_args_list if call.args]
                assert not any("AiHead tab ready." in str(msg) for msg in notified1)

        # Test 2: Unusable result (missing pane_id)
        mock_ensure.side_effect = None
        mock_ensure.return_value = {"workspace_id": "ws-head", "status": "incomplete"}
        app2 = _create_app(tmp_path)
        with patch.object(app2, "notify", wraps=app2.notify) as spy_notify2:
            async with app2.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                app2.action_launch_aihead()
                await pilot.pause()
                await pilot.pause()

                head_screen = app2.screen
                assert isinstance(head_screen, AiHeadScreen)
                assert head_screen.status == "error"
                live_view = head_screen._live_pane_view
                assert live_view._current_frame.phase == "unavailable"
                assert live_view._prompt_input.disabled is True
                assert "missing pane" in (live_view._current_frame.error or "")

                # Unusable result must NOT notify "AiHead tab ready."
                notified2 = [call.args[0] for call in spy_notify2.call_args_list if call.args]
                assert not any("AiHead tab ready." in str(msg) for msg in notified2)


@pytest.mark.asyncio
async def test_b_mount_ensure_holds_and_foreground_launch_claims_focus(tmp_path: Path):
    """Defect 1 regression: Foreground launch does not get dropped behind in-flight mount ensure."""
    # Enable AiHead in project config before constructing the app
    (tmp_path / ".herdr-master.toml").write_text("[ai_head]\nenabled = true\n")
    app = _create_app(tmp_path)

    worker_target = WorkspaceTarget(
        session_name="test-session",
        socket_path=Path("/tmp/worker.sock"),
        workspace_id="ws-worker",
        tab_id="tab-worker",
        pane_id="pane-worker",
    )
    worker_lease = PaneLease(
        key=("/tmp/worker.sock", "ws-worker", "pane-worker"), generation=1, terminal_id="term-w"
    )
    worker_frame = PaneFrame(
        lease=worker_lease,
        target=worker_target,
        phase="live",
        agent_status="working",
        text_ansi="WORKER ACTIVE PROCESS OUTPUT",
        error=None,
        can_reply=True,
        reply_eligible=True,
    )
    app._last_live_frame = worker_frame

    ensure_started = asyncio.Event()
    ensure_release = asyncio.Event()

    async def _mock_held_ensure(*args, **kwargs):
        ensure_started.set()
        await ensure_release.wait()
        return {
            "workspace_id": "ws-head",
            "tab_id": "tab-head",
            "pane_id": "pane-head",
            "status": "ready",
            "busy": False,
        }

    with patch("aiheaddev.ui.tui.app.ensure_herdr_ai_head", side_effect=_mock_held_ensure) as mock_ensure:
        async with app.run_test(size=(120, 45)) as pilot:
            # 1. Wait until mount ensure has started (focus=False)
            await ensure_started.wait()
            await pilot.pause()
            assert mock_ensure.call_count == 1

            # 2. Foreground launch while ensure is held
            app.action_launch_aihead()
            await pilot.pause()

            # Assert: mock was not called a second time
            assert mock_ensure.call_count == 1

            # Fullscreen pending screen is mounted
            head_screen = app.screen
            assert isinstance(head_screen, AiHeadScreen)
            live_view = head_screen._live_pane_view
            assert live_view._current_frame.phase == "loading"
            assert live_view._current_frame.text_ansi == ""
            assert live_view._prompt_input.disabled is True

            # 3. Release held ensure with usable pane
            ensure_release.set()
            await pilot.pause()
            await pilot.pause()

            # Assert: _ai_head_confirmed_target.pane_id is bound
            assert app._ai_head_confirmed_target is not None
            assert app._ai_head_confirmed_target.pane_id == "pane-head"

            # 4. Deliver matching authoritative head frame
            sock_path = Path(getattr(app.client, "socket_path", "/tmp/herdr.sock"))
            head_target = WorkspaceTarget(
                session_name="test-session",
                socket_path=sock_path,
                workspace_id="ws-head",
                tab_id="tab-head",
                pane_id="pane-head",
            )
            head_lease = PaneLease(
                key=(str(sock_path), "ws-head", "pane-head"), generation=2, terminal_id="term-head"
            )
            head_frame = PaneFrame(
                lease=head_lease,
                target=head_target,
                phase="live",
                agent_status="ready",
                text_ansi="👑 AIHEAD AUTHORITATIVE READY",
                error=None,
                can_reply=True,
                reply_eligible=True,
            )
            app._publish_live_frame(head_frame)
            await pilot.pause()

            # Assert: frame is visible on the AiHead screen
            assert live_view._current_frame.lease == head_lease
            assert live_view._current_frame.text_ansi == "👑 AIHEAD AUTHORITATIVE READY"
            assert live_view._prompt_input.disabled is False

            # Assert: worker-keyed frame remains absent
            app._publish_live_frame(worker_frame)
            await pilot.pause()
            assert live_view._current_frame.text_ansi != "WORKER ACTIVE PROCESS OUTPUT"
            assert live_view._current_frame.text_ansi == "👑 AIHEAD AUTHORITATIVE READY"


@pytest.mark.asyncio
async def test_b_force_launch_with_unusable_result_no_success_notification(tmp_path: Path):
    """Defect 2 regression: Force-launch with missing pane_id shows error and does NOT notify success."""
    app = _create_app(tmp_path)

    async def _mock_unusable_ensure(*args, **kwargs):
        return {
            "workspace_id": "ws-head",
            "tab_id": "tab-head",
            # missing pane_id
            "status": "incomplete",
            "busy": False,
        }

    with patch("aiheaddev.ui.tui.app.ensure_herdr_ai_head", side_effect=_mock_unusable_ensure), \
         patch.object(app, "notify", wraps=app.notify) as spy_notify:
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()

            # Force launch AiHead
            app.action_launch_aihead()
            await pilot.pause()
            await pilot.pause()

            head_screen = app.screen
            assert isinstance(head_screen, AiHeadScreen)
            assert head_screen.status == "error"

            live_view = head_screen._live_pane_view
            assert live_view._current_frame.phase == "unavailable"
            assert live_view._prompt_input.disabled is True
            assert "missing pane" in (live_view._current_frame.error or "")

            # Assert: target was not bound
            assert app._ai_head_confirmed_target is None

            # Assert: "AiHead tab ready." was NOT notified
            notified = [call.args[0] for call in spy_notify.call_args_list if call.args]
            assert not any("AiHead tab ready." in str(msg) for msg in notified)

    # When usable result is returned, "AiHead tab ready." IS notified
    app2 = _create_app(tmp_path)
    async def _mock_usable_ensure(*args, **kwargs):
        return {
            "workspace_id": "ws-head",
            "tab_id": "tab-head",
            "pane_id": "pane-head",
            "status": "ready",
            "busy": False,
        }
    with patch("aiheaddev.ui.tui.app.ensure_herdr_ai_head", side_effect=_mock_usable_ensure), \
         patch.object(app2, "notify", wraps=app2.notify) as spy_notify2:
        async with app2.run_test(size=(120, 45)) as pilot2:
            await pilot2.pause()
            app2.action_launch_aihead()
            await pilot2.pause()
            await pilot2.pause()

            assert app2._ai_head_confirmed_target is not None
            notified2 = [call.args[0] for call in spy_notify2.call_args_list if call.args]
            assert any("AiHead tab ready." in str(msg) for msg in notified2)


# =============================================================================
# Test Group C: Covered State & Readiness Invariants (F3)
# =============================================================================


@pytest.mark.asyncio
async def test_c_covered_screen_under_modals_receives_publications(tmp_path: Path):
    """F3: Under LogViewerModal/SettingsModal, accepted frames and inventory guards reach AiHeadScreen."""
    app = _create_app(tmp_path)

    with patch("aiheaddev.ui.tui.app.ensure_herdr_ai_head", new_callable=AsyncMock) as mock_ensure:
        mock_ensure.return_value = {
            "workspace_id": "ws-head",
            "tab_id": "tab-head",
            "pane_id": "pane-head",
            "status": "ready",
            "busy": False,
        }
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            app.action_launch_aihead()
            await pilot.pause()

            head_screen = app.screen
            assert isinstance(head_screen, AiHeadScreen)

            sock_path = Path(getattr(app.client, "socket_path", "/tmp/herdr.sock"))
            head_target = WorkspaceTarget(
                session_name="test-session",
                socket_path=sock_path,
                workspace_id="ws-head",
                tab_id="tab-head",
                pane_id="pane-head",
            )
            lease = PaneLease(key=(str(sock_path), "ws-head", "pane-head"), generation=1, terminal_id="term-1")
            frame1 = PaneFrame(
                lease=lease,
                target=head_target,
                phase="live",
                agent_status="ready",
                text_ansi="Initial live",
                error=None,
                can_reply=True,
                reply_eligible=True,
            )

            app._publish_live_frame(frame1)
            await pilot.pause()

            # 1. Cover with LogViewerModal
            app.action_view_logs()
            await pilot.pause()
            assert isinstance(app.screen, LogViewerModal)
            assert len(app.screen_stack) == 3

            # Deliver updated frame while covered
            frame2 = replace(frame1, text_ansi="Updated while covered by Logs")
            app._publish_live_frame(frame2)
            await pilot.pause()

            # Modal is still on top, but underlying screen already received the update!
            assert isinstance(app.screen, LogViewerModal)
            assert head_screen._live_pane_view._current_frame.text_ansi == "Updated while covered by Logs"

            # Dismiss logs modal
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is head_screen

            # 2. Cover with SettingsModal
            head_screen.query_one("#btn-aihead-back").focus()
            await pilot.pause()
            app.action_open_settings()
            await pilot.pause()
            assert isinstance(app.screen, SettingsModal)

            # Test inventory guard with reply_eligible=False while covered
            app.app_state.live_sessions = [_make_inventory(socket_path=str(sock_path), is_agent=False)]
            app._guard_live_inventory()
            await pilot.pause()

            # Underlying screen received the not-ready update!
            assert head_screen._live_pane_view._current_frame.reply_eligible is False

            # Dismiss settings modal
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is head_screen

            # Input agrees after dismissal: prompt is disabled!
            assert head_screen._live_pane_view._prompt_input.disabled is True

            # Calling focus_input() on explicitly ineligible frame MUST NOT unlock input!
            head_screen._live_pane_view.focus_input()
            await pilot.pause()
            assert head_screen._live_pane_view._prompt_input.disabled is True

            # Subsequent valid matching frame restores normal state
            restored_frame = replace(frame1, reply_eligible=True, text_ansi="Restored live")
            app._live_inventory_blocked = False
            app._publish_live_frame(restored_frame)
            await pilot.pause()
            assert head_screen._live_pane_view._prompt_input.disabled is False


@pytest.mark.asyncio
async def test_c_reply_eligible_none_vs_false_and_can_reply_in_flight():
    """F3: None preserves unknown semantics; can_reply=False alone does not disable input."""
    app = HerdrMasterApp(demo=True)
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        view = app.query_one(LivePaneView)

        target = WorkspaceTarget(
            session_name="test", socket_path=Path("/tmp/s.sock"), workspace_id="w1", pane_id="p1"
        )
        lease = PaneLease(key=("/tmp/s.sock", "w1", "p1"), generation=1, terminal_id="t1")

        # Frame with reply_eligible=None and can_reply=False (e.g. in-flight send without readiness loss)
        frame_unknown = PaneFrame(
            lease=lease,
            target=target,
            phase="live",
            agent_status="working",
            text_ansi="Working...",
            error=None,
            can_reply=False,
            reply_eligible=None,
        )
        view.update_frame(frame_unknown)
        await pilot.pause()
        # When reply_eligible is None and no send is in flight, prompt is NOT disabled
        assert view._prompt_input.disabled is False

        # Frame with explicit reply_eligible=False
        frame_ineligible = replace(frame_unknown, reply_eligible=False)
        view.update_frame(frame_ineligible)
        await pilot.pause()
        assert view._prompt_input.disabled is True

        # focus_input must not unlock it
        view.focus_input()
        await pilot.pause()
        assert view._prompt_input.disabled is True


# =============================================================================
# Test Group D: Keyboard Routing, Focus Return & Raw Forwarding (F4)
# =============================================================================


@pytest.mark.asyncio
async def test_d_keyboard_two_step_escape_and_ctrl_bracket(tmp_path: Path):
    """F4: Prompt Escape/Ctrl+] focuses Back; second Escape pops screen; draft unchanged."""
    app = _create_app(tmp_path)

    with patch("aiheaddev.ui.tui.app.ensure_herdr_ai_head", new_callable=AsyncMock) as mock_ensure:
        mock_ensure.return_value = {
            "workspace_id": "ws-head",
            "tab_id": "tab-head",
            "pane_id": "pane-head",
            "status": "ready",
            "busy": False,
        }
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            app.action_launch_aihead()
            await pilot.pause()

            head_screen = app.screen
            assert isinstance(head_screen, AiHeadScreen)

            sock_path = Path(getattr(app.client, "socket_path", "/tmp/herdr.sock"))
            head_target = WorkspaceTarget(
                session_name="test-session",
                socket_path=sock_path,
                workspace_id="ws-head",
                tab_id="tab-head",
                pane_id="pane-head",
            )
            lease = PaneLease(key=(str(sock_path), "ws-head", "pane-head"), generation=1, terminal_id="term-1")
            live_frame = PaneFrame(
                lease=lease,
                target=head_target,
                phase="live",
                agent_status="ready",
                text_ansi="aihead prompt",
                error=None,
                can_reply=True,
                reply_eligible=True,
            )

            app._publish_live_frame(live_frame)
            await pilot.pause()

            prompt_input = head_screen.query_one("#prompt-input", Input)
            prompt_input.focus()
            await pilot.pause()

            # Set a non-empty draft
            prompt_input.value = "important draft command"
            await pilot.pause()

            # STEP 1: First Escape in prompt
            await pilot.press("escape")
            await pilot.pause()

            # Must stay on AiHeadScreen (depth 2), focus header Back button, keep draft, send nothing
            assert len(app.screen_stack) == 2
            assert isinstance(app.screen, AiHeadScreen)
            assert app.focused is head_screen.query_one("#btn-aihead-back")
            assert prompt_input.value == "important draft command"

            # STEP 2: Second Escape from Back button pops fullscreen to Cockpit
            await pilot.press("escape")
            await pilot.pause()

            assert len(app.screen_stack) == 1
            assert not isinstance(app.screen, AiHeadScreen)
            assert app.focused.id == "workspace-tree"

            # Test Ctrl+] aliases in prompt: ctrl+right_square_bracket, ctrl+close_bracket
            for key in ("ctrl+right_square_bracket", "ctrl+close_bracket"):
                app.action_launch_aihead()
                await pilot.pause()
                head = app.screen
                app._publish_live_frame(live_frame)
                await pilot.pause()

                head._live_pane_view.focus_input()
                await pilot.pause()

                await pilot.press(key)
                await pilot.pause()
                assert app.focused is head.query_one("#btn-aihead-back")

                await pilot.press("escape")
                await pilot.pause()


@pytest.mark.asyncio
async def test_d_output_ctrl_bracket_and_raw_escape_forwarding(tmp_path: Path):
    """F4: Output Ctrl+] returns focus; raw output Escape forwards ['esc']; empty Tab forwards ['tab']."""
    app = _create_app(tmp_path)
    with patch("aiheaddev.ui.tui.app.ensure_herdr_ai_head", new_callable=AsyncMock):
        async with app.run_test(size=(110, 32)) as pilot:
            await pilot.pause()
            view = app.query_one(LivePaneView)

            sock_path = Path(getattr(app.client, "socket_path", "/tmp/herdr.sock"))
            target = WorkspaceTarget(
                session_name="test", socket_path=sock_path, workspace_id="w1", pane_id="p1"
            )
            lease = PaneLease(key=(str(sock_path), "w1", "p1"), generation=1, terminal_id="t1")
            frame = PaneFrame(
                lease=lease,
                target=target,
                phase="live",
                agent_status="ready",
                text_ansi="Terminal output",
                error=None,
                can_reply=True,
                reply_eligible=True,
            )
            view.update_frame(frame)
            await pilot.pause()

            output = view._output
            forwarded_keys: list[tuple[str | None, list[str] | None]] = []
            output._on_terminal_input = lambda text, keys: forwarded_keys.append((text, keys))

            # Raw Escape in output forwards ['esc'] without focus return
            raw_esc_event = MagicMock()
            raw_esc_event.key = "escape"
            output.on_key(raw_esc_event)
            assert raw_esc_event.stop.called
            assert forwarded_keys == [(None, ["esc"])]

            # Ctrl+] in output posts FocusReturnRequested
            with patch.object(output, "post_message", wraps=output.post_message) as mock_post:
                ctrl_bracket_event = MagicMock()
                ctrl_bracket_event.key = "ctrl+]"
                output.on_key(ctrl_bracket_event)
                assert ctrl_bracket_event.stop.called
                assert mock_post.called
                msg = mock_post.call_args[0][0]
                assert isinstance(msg, FocusReturnRequested)
                assert msg.origin is output

            # Empty prompt Tab and Shift+Tab forward terminal keys
            with patch.object(view, "_handle_terminal_input") as mock_input:
                view._prompt_input.value = ""
                view._prompt_input.has_focus = True

                tab_event = MagicMock()
                tab_event.key = "tab"
                view.on_key(tab_event)
                assert tab_event.stop.called
                mock_input.assert_called_with(None, ["tab"])

                shift_tab_event = MagicMock()
                shift_tab_event.key = "shift+tab"
                view.on_key(shift_tab_event)
                assert shift_tab_event.stop.called
                mock_input.assert_called_with(None, ["backtab"])


# =============================================================================
# Test Group E: Responsive Geometry & Chrome (F5)
# =============================================================================


@pytest.mark.asyncio
async def test_e_responsive_geometry_at_all_dimensions_and_roundtrip(tmp_path: Path):
    """F5: Narrow breakpoint (<100) compacts chrome; resize 80->40->80->120 restores wide."""
    app = _create_app(tmp_path)

    with patch("aiheaddev.ui.tui.app.ensure_herdr_ai_head", new_callable=AsyncMock) as mock_ensure:
        mock_ensure.return_value = {
            "workspace_id": "ws-head",
            "tab_id": "tab-head",
            "pane_id": "pane-head",
            "status": "ready",
            "busy": False,
        }
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_launch_aihead()
            await pilot.pause()

            head_screen = app.screen
            assert isinstance(head_screen, AiHeadScreen)

            btn_back = head_screen.query_one("#btn-aihead-back", Button)
            btn_footer = head_screen.query_one("#btn-aihead-back-footer", Button)
            btn_settings = head_screen.query_one("#btn-aihead-settings", Button)
            btn_logs = head_screen.query_one("#btn-aihead-logs", Button)

            # 1. At 120x40 (wide)
            assert "-narrow" not in head_screen.classes
            assert str(btn_back.label) == "← Back to Workspaces"
            assert str(btn_footer.label) == "← Back to Workspaces"
            assert str(btn_settings.label) == "⚙ Settings"
            assert str(btn_logs.label) == "📋 Logs"

            # 2. At 80x24 (narrow)
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert "-narrow" in head_screen.classes
            assert str(btn_back.label) == "← Back"
            assert str(btn_footer.label) == "← Back"
            assert str(btn_settings.label) == "Settings"
            assert str(btn_logs.label) == "Logs"

            # 3. At 60x24 (narrow)
            await pilot.resize_terminal(60, 24)
            await pilot.pause()
            assert "-narrow" in head_screen.classes
            assert str(btn_back.label) == "← Back"

            # 4. At 40x24 (extreme narrow)
            await pilot.resize_terminal(40, 24)
            await pilot.pause()
            assert "-narrow" in head_screen.classes
            assert str(btn_back.label) == "← Back"
            assert str(btn_settings.label) == "Settings"
            assert str(btn_logs.label) == "Logs"

            # 5. Roundtrip resize back: 40 -> 80 -> 120x40
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert "-narrow" in head_screen.classes

            await pilot.resize_terminal(120, 40)
            await pilot.pause()
            # Wide class state and wide labels restored!
            assert "-narrow" not in head_screen.classes
            assert str(btn_back.label) == "← Back to Workspaces"
            assert str(btn_footer.label) == "← Back to Workspaces"
            assert str(btn_settings.label) == "⚙ Settings"
            assert str(btn_logs.label) == "📋 Logs"


# =============================================================================
# Test Group F8: Discoverability & Fallback Order (F8)
# =============================================================================


@pytest.mark.asyncio
async def test_f8_order_and_tooltip_discoverability(tmp_path: Path):
    """F8: No-selection notification precedes fallback; tooltip explains contextual 'o'."""
    app = _create_app(tmp_path)

    events_record: list[str] = []

    def _mock_notify(msg, **kwargs):
        events_record.append(f"notify: {msg}")

    def _mock_new_orchestration():
        events_record.append("action: new_orchestration")

    app.notify = _mock_notify
    app.action_new_orchestration = _mock_new_orchestration

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        browser = app.query_one(WorkspaceBrowser)

        # 1. Check tooltip on #btn-new-orchestration explains contextual 'o'
        btn_orch = app.query_one("#btn-new-orchestration", Button)
        tooltip_text = str(btn_orch.tooltip)
        assert "Open" in tooltip_text or "selected" in tooltip_text
        assert "Orchestration" in tooltip_text

        # 2. No selection -> notification precedes fallback in append-only record
        browser._selected_target = None
        browser._selected_target_disappeared = False
        app.action_open_in_herdr()
        await pilot.pause()

        assert len(events_record) == 2
        assert events_record[0].startswith("notify: No target selected")
        assert events_record[1] == "action: new_orchestration"

        # 3. Disappeared selection never starts fallback
        events_record.clear()
        browser._selected_target_disappeared = True
        with patch.object(browser, "request_open") as mock_open:
            app.action_open_in_herdr()
            await pilot.pause()
            assert "action: new_orchestration" not in events_record
            mock_open.assert_called_once()

        # 4. Valid selected Open routes once to request_open without fallback
        events_record.clear()
        browser._selected_target_disappeared = False
        browser._selected_target = WorkspaceTarget(
            session_name="test", socket_path=Path("/tmp/s.sock"), workspace_id="ws-1"
        )
        with patch.object(browser, "request_open") as mock_open:
            app.action_open_in_herdr()
            await pilot.pause()
            assert "action: new_orchestration" not in events_record
            mock_open.assert_called_once()

        # 5. When text widget has focus, action_open_in_herdr does neither
        events_record.clear()
        browser._selected_target = None
        input_widget = app.query_one("#prompt-input", Input)
        input_widget.disabled = False
        input_widget.focus()
        await pilot.pause()

        app.action_open_in_herdr()
        await pilot.pause()
        assert len(events_record) == 0

        # When modal is stacked, action_open_in_herdr does neither
        events_record.clear()
        app.push_screen(LogViewerModal())
        await pilot.pause()

        app.action_open_in_herdr()
        await pilot.pause()
        assert len(events_record) == 0
