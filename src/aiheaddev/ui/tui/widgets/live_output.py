"""Responsive live output viewer and pinned operator reply composer."""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

import pyte
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static, TextArea

from aiheaddev.ui.tui.live_pane import PaneFrame, PaneLease, ReplyRequest, ReplyResult
from aiheaddev.ui.tui.widgets.workspaces import WorkspaceTarget, safe_display, status_label

WAITING_LIMITATION_NOTE = (
    "Protocol 22 does not confirm whether user input is required. "
    "Ordinary text replies use agent.prompt; native permission dialogs require Open in Herdr."
)


def sanitize_ansi(raw: str) -> str:
    """Keep printable text and SGR only, including with incomplete control strings."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    clean: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        i += 1
        if char == "\x1b" and i < len(text):
            kind = text[i]
            i += 1
        elif char in "\x90\x98\x9b\x9d\x9e\x9f":
            kind = {"\x90": "P", "\x98": "X", "\x9b": "[", "\x9d": "]",
                    "\x9e": "^", "\x9f": "_"}[char]
        elif char == "\x1b":
            break
        else:
            if (char in "\n\t" or ord(char) >= 0x20) and not (
                0x7f <= ord(char) <= 0x9f
                or char in "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
                or "\u2066" <= char <= "\u2069"
            ):
                clean.append(char)
            continue

        if kind in "]PX^_":
            # OSC may also end in BEL; ST terminates every control string.
            while i < len(text):
                if text[i] == "\x9c" or (kind == "]" and text[i] == "\x07"):
                    i += 1
                    break
                if text[i:i + 2] == "\x1b\\":
                    i += 2
                    break
                i += 1
        elif kind == "[":
            start = i
            while i < len(text) and not ("@" <= text[i] <= "~"):
                i += 1
            if i < len(text):
                final = text[i]
                params = text[start:i]
                i += 1
                if final == "m" and re.fullmatch(r"[0-9;]*", params):
                    clean.append(f"\x1b[{params}m")
        else:
            # Consume ESC intermediate bytes and their final byte, if any.
            while i < len(text) and " " <= kind <= "/":
                kind = text[i]
                i += 1
    return "".join(clean)


class TargetDetails(ModalScreen[None]):
    """Keyboard-accessible full identity when the one-line header is truncated."""

    BINDINGS = [Binding("escape", "close", "Close", show=False)]
    DEFAULT_CSS = """
    TargetDetails { align: center middle; background: rgba(11, 15, 25, 0.85); }
    TargetDetails > Vertical {
        width: 90%; height: 75%; border: solid #38BDF8; background: #161E2E; padding: 0 1;
    }
    #target-details-scroll { height: 1fr; width: 100%; }
    #target-details-text { height: auto; width: 100%; color: #F8FAFC; }
    #target-details-close { height: 3; min-width: 8; background: #0284C7; color: #F8FAFC; text-style: bold; }
    #target-details-close:hover { background: #0369A1; }
    """

    def __init__(self, target: WorkspaceTarget, lease: Optional[PaneLease], original: bool):
        super().__init__()
        title = "Original draft target" if original else "Selected target"
        detail = (
            f"{title}\n\nSocket: {safe_display(target.socket_path)}\n"
            f"Session: {safe_display(target.session_name)}\n"
            f"Workspace: {safe_display(target.workspace_id)}\n"
            f"Tab: {safe_display(target.tab_id or 'unknown')}\n"
            f"Pane: {safe_display(target.pane_id or 'unknown')}"
        )
        if lease is not None:
            detail += (
                f"\nLease: {lease.generation}"
                f"\nTerminal ID: {safe_display(lease.terminal_id or 'unknown')}"
            )
        detail += (
            "\n\nStatus legend: ● running · ! blocked · ○ idle · ✓ done · ? unknown"
            " · › terminal · × stale/offline.\n"
            "Blocked: reason unknown—inspect output.\n"
            f"{WAITING_LIMITATION_NOTE}"
        )
        self._details = Static(Text(detail), id="target-details-text")

    def compose(self) -> ComposeResult:
        with Vertical():
            with VerticalScroll(id="target-details-scroll"):
                yield self._details
            yield Button("Close", id="target-details-close")

    def on_mount(self) -> None:
        self.query_one("#target-details-scroll", VerticalScroll).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "target-details-close":
            event.stop()
            self.dismiss()

    def action_close(self) -> None:
        self.dismiss()


class LivePaneOutput(VerticalScroll):
    """Scrollable container for bounded terminal output snapshots."""

    can_focus = True

    DEFAULT_CSS = """
    LivePaneOutput {
        height: 1fr;
        width: 100%;
        background: #0B0F19;
        overflow-y: scroll;
        overflow-x: hidden;
        color: #F8FAFC;
    }
    #live-pane-output-text {
        width: 100%;
        height: auto;
    }
    """

    def __init__(
        self,
        on_pause_changed: Optional[Callable[[bool], None]] = None,
        on_terminal_input: Optional[
            Callable[[Optional[str], Optional[list[str]]], None]
        ] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._static = Static("", id="live-pane-output-text")
        self._content = self._static
        self._rich_text = Text("")
        self.is_paused = False
        self._programmatic = False
        self.on_pause_changed = on_pause_changed
        self._on_terminal_input = on_terminal_input
        self._pyte_screen = pyte.HistoryScreen(120, 40, history=1000)
        self._pyte_stream = pyte.Stream(self._pyte_screen)

    @property
    def on_terminal_input(
        self,
    ) -> Optional[Callable[[Optional[str], Optional[list[str]]], None]]:
        return self._on_terminal_input

    @on_terminal_input.setter
    def on_terminal_input(
        self, value: Optional[Callable[[Optional[str], Optional[list[str]]], None]]
    ) -> None:
        self._on_terminal_input = value

    @property
    def cursor(self) -> tuple[int, int]:
        return (self._pyte_screen.cursor.x, self._pyte_screen.cursor.y)

    def compose(self) -> ComposeResult:
        yield self._content

    def _scroll_bottom_if_following(self) -> None:
        # Layout must settle before max_scroll_y is known. A later manual pause wins.
        if not self.is_paused:
            self._programmatic = True
            try:
                self.scroll_end(animate=False, immediate=True)
            finally:
                self._programmatic = False

    def _refresh_display(self) -> None:
        try:
            if self.has_focus:
                display_text = self._rich_text.copy()
                display_text.append(Text("█", style="bold cyan"))
                self._content.update(display_text)
            else:
                self._content.update(self._rich_text)
        except Exception:
            pass

    def update_snapshot(self, raw: str | Text, reset: bool = False) -> None:
        if reset:
            self._pyte_screen.reset()
        if isinstance(raw, str):
            raw_str = raw
            self._rich_text = Text.from_ansi(sanitize_ansi(raw_str))
        else:
            self._rich_text = raw
            raw_str = raw.plain

        self._pyte_screen.reset()
        if raw_str:
            try:
                self._pyte_stream.feed(raw_str.replace("\r\n", "\n").replace("\n", "\r\n"))
            except Exception:
                pass

        self._refresh_display()
        if not self.is_paused:
            self.call_after_refresh(self._scroll_bottom_if_following)

    def on_focus(self, event: events.Focus) -> None:
        self._refresh_display()

    def on_blur(self, event: events.Blur) -> None:
        self._refresh_display()

    def on_click(self, event: events.Click) -> None:
        try:
            parent_view = self.ancestors_with_self
            for ancestor in parent_view:
                if isinstance(ancestor, LivePaneView):
                    if (
                        ancestor._prompt_input.display
                        and not ancestor._prompt_input.disabled
                    ):
                        ancestor._prompt_input.focus()
                        return
                    break
        except Exception:
            pass
        self.focus()

    def on_key(self, event: events.Key) -> None:
        if event.key in ("ctrl+right_square_bracket", "ctrl+]", "ctrl+close_bracket"):
            event.stop()
            self.post_message(LivePaneView.FocusReturnRequested(origin=self))
            return

        if event.key == "escape":
            event.stop()
            if self._on_terminal_input:
                self._on_terminal_input(None, ["esc"])
            return

        key_map = {
            "enter": "enter",
            "backspace": "backspace",
            "tab": "tab",
            "shift+tab": "backtab",
            "backtab": "backtab",
            "up": "up",
            "down": "down",
            "left": "left",
            "right": "right",
            "home": "home",
            "end": "end",
            "pageup": "pageup",
            "pagedown": "pagedown",
            "delete": "delete",
            "ctrl+c": "ctrl+c",
            "ctrl+d": "ctrl+d",
            "ctrl+z": "ctrl+z",
            "ctrl+l": "ctrl+l",
            "ctrl+u": "ctrl+u",
            "ctrl+w": "ctrl+w",
            "ctrl+a": "ctrl+a",
            "ctrl+e": "ctrl+e",
            "ctrl+k": "ctrl+k",
            "ctrl+r": "ctrl+r",
        }

        if event.key in key_map:
            event.stop()
            if self._on_terminal_input:
                self._on_terminal_input(None, [key_map[event.key]])
            return

        if event.key == "space":
            event.stop()
            if self._on_terminal_input:
                self._on_terminal_input(" ", None)
            return

        if event.character and len(event.character) == 1 and event.character.isprintable():
            event.stop()
            if self._on_terminal_input:
                self._on_terminal_input(event.character, None)
            return

    def on_paste(self, event: events.Paste) -> None:
        if event.text:
            event.stop()
            if self._on_terminal_input:
                self._on_terminal_input(event.text, None)

    def follow(self) -> None:
        self.is_paused = False
        self.call_after_refresh(self._scroll_bottom_if_following)

    def reset_scroll(self) -> None:
        self.is_paused = False
        self._programmatic = True
        try:
            self.scroll_to(y=0, animate=False, immediate=True)
        finally:
            self._programmatic = False

    def render(self) -> Text:
        return self._rich_text

    @property
    def text(self) -> Text:
        return self._rich_text

    def watch_scroll_y(self, old_val: float, new_val: float) -> None:
        super().watch_scroll_y(old_val, new_val)
        if (
            not self._programmatic
            and not self.is_paused
            and self.max_scroll_y > 0
            and new_val < old_val
            and new_val < self.max_scroll_y - 0.5
        ):
            self.is_paused = True
            if self.on_pause_changed is not None:
                self.on_pause_changed(True)


class LivePaneView(Vertical):
    """Deliberately selected pane live output viewer and at-most-once reply composer."""

    DEFAULT_CSS = """
    LivePaneView {
        height: 1fr;
        padding: 0 1;
        background: #161E2E;
        overflow-x: hidden;
    }
    #live-pane-identity {
        height: 1;
        width: 100%;
        color: #F8FAFC;
        text-style: bold;
        overflow: hidden hidden;
    }
    #live-pane-state {
        height: 1;
        width: 100%;
        color: #94A3B8;
        overflow: hidden hidden;
    }
    #live-pane-output {
        height: 1fr;
        width: 100%;
        background: #0B0F19;
        overflow-y: scroll;
        overflow-x: hidden;
        color: #F8FAFC;
    }
    #reply-text {
        height: 2;
        width: 100%;
        border: solid #334155;
        background: #1E293B;
        color: #F8FAFC;
        padding: 0;
        display: none;
    }
    #prompt-input {
        height: 3;
        min-height: 3;
        border: round #334155;
        padding: 0 1;
        background: #1E293B;
        color: #F8FAFC;
        width: 100%;
        margin: 0;
    }
    #prompt-input:focus {
        border: round #38BDF8;
        background: #161E2E;
    }
    #prompt-input:disabled {
        border: round #1E293B;
        background: #111827;
        color: #64748B;
    }
    #live-pane-controls {
        height: 1;
        width: 100%;
        margin-top: 0;
    }
    #btn-reply, #btn-send-reply, #btn-cancel-reply, #btn-follow-output, #btn-target-details {
        border: none;
        height: 1;
        min-height: 1;
        padding: 0;
        width: auto;
        min-width: 6;
        margin: 0 1 0 0;
    }
    #btn-reply {
        background: #0284C7;
        color: #F8FAFC;
        text-style: bold;
    }
    #btn-reply:hover {
        background: #0369A1;
    }
    #btn-reply:focus {
        background: #0284C7;
        text-style: bold;
    }
    #btn-reply:disabled {
        background: #334155;
        color: #64748B;
    }
    #btn-send-reply {
        background: #059669;
        color: #F8FAFC;
        text-style: bold;
        display: none;
    }
    #btn-send-reply:hover {
        background: #047857;
    }
    #btn-send-reply:focus {
        background: #059669;
        text-style: bold;
    }
    #btn-send-reply:disabled {
        background: #334155;
        color: #64748B;
    }
    #btn-cancel-reply {
        background: #475569;
        color: #F8FAFC;
        display: none;
    }
    #btn-cancel-reply:hover {
        background: #64748B;
    }
    #btn-cancel-reply:focus {
        background: #475569;
        color: #F8FAFC;
        text-style: bold;
    }
    #btn-cancel-reply:disabled {
        background: #334155;
        color: #64748B;
    }
    #btn-follow-output {
        background: #334155;
        color: #F8FAFC;
    }
    #btn-follow-output:hover {
        background: #475569;
    }
    #btn-follow-output:focus {
        background: #334155;
        text-style: bold;
    }
    #btn-follow-output:disabled {
        background: #1E293B;
        color: #64748B;
    }
    #btn-target-details {
        background: #334155;
        color: #F8FAFC;
    }
    #btn-target-details:hover {
        background: #475569;
    }
    #btn-target-details:focus {
        background: #334155;
        text-style: bold;
    }
    #btn-target-details:disabled {
        background: #1E293B;
        color: #64748B;
    }

    LivePaneView.-reply-active #reply-text {
        display: block;
    }
    LivePaneView.-reply-active #prompt-input {
        display: none;
    }
    LivePaneView.-reply-active #btn-reply {
        display: none;
    }
    LivePaneView.-reply-active #btn-send-reply {
        display: block;
    }
    LivePaneView.-reply-active #btn-cancel-reply {
        display: block;
    }
    """

    class ReplyRequested(Message):
        """Operator requested ordinary text submission to a verified agent pane."""

        def __init__(self, request: ReplyRequest) -> None:
            super().__init__()
            self.request = request

    class TerminalInputRequested(Message):
        """Operator typed or pasted directly into the interactive terminal screen."""

        def __init__(
            self,
            lease: PaneLease,
            text: Optional[str] = None,
            keys: Optional[list[str]] = None,
        ) -> None:
            super().__init__()
            self.lease = lease
            self.text = text
            self.keys = keys

    class DraftCancelled(Message):
        """Operator cancelled the unsent draft."""

    class FocusReturnRequested(Message):
        """Operator requested to return keyboard focus out of output or prompt."""

        def __init__(self, origin: Any = None) -> None:
            super().__init__()
            self.origin = origin

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._identity = Static("No pane selected", id="live-pane-identity")
        self._state_bar = Static("", id="live-pane-state")
        self._output = LivePaneOutput(
            id="live-pane-output",
            on_pause_changed=self._on_pause_changed,
            on_terminal_input=self._handle_terminal_input,
        )
        self._prompt_input = Input(
            placeholder="Type prompt and press Enter to send…",
            id="prompt-input",
        )
        self._reply_text = TextArea("", id="reply-text")
        self._btn_reply = Button(Text("Reply"), id="btn-reply")
        self._btn_send = Button(Text("Send"), id="btn-send-reply")
        self._btn_cancel = Button(Text("Cancel"), id="btn-cancel-reply")
        self._btn_follow = Button(Text("Follow"), id="btn-follow-output")
        self._btn_target = Button(Text("Target"), id="btn-target-details")
        self._btn_target.tooltip = "Show the complete socket, workspace, and pane identity."

        self._current_frame: Optional[PaneFrame] = None
        self._latest_rendered_text: Optional[str] = None
        self._stashed_rich_text: Optional[Text] = None

        self._draft_lease: Optional[PaneLease] = None
        self._draft_target: Optional[WorkspaceTarget] = None
        self._draft_invalid: bool = False
        self._inline_pending_lease: Optional[PaneLease] = None
        self._send_in_flight: bool = False
        self._feedback_message: Optional[Text] = None
        self._pending_focus: bool = False

    def compose(self) -> ComposeResult:
        yield self._identity
        yield self._state_bar
        yield self._output
        yield self._prompt_input
        yield self._reply_text
        with Horizontal(id="live-pane-controls"):
            yield self._btn_reply
            yield self._btn_send
            yield self._btn_cancel
            yield self._btn_follow
            yield self._btn_target

    def on_mount(self) -> None:
        self._output.on_pause_changed = self._on_pause_changed
        self._output.on_terminal_input = self._handle_terminal_input
        self._refresh_identity()
        self._refresh_state()
        self._refresh_controls()

    @property
    def has_active_pane(self) -> bool:
        """True when the current frame targets an existing pane and is not empty."""
        return bool(
            self._current_frame is not None
            and self._current_frame.target is not None
            and self._current_frame.target.pane_id is not None
            and self._current_frame.phase != "empty"
        )

    @property
    def has_retained_draft(self) -> bool:
        """True when an unsent draft exists and must remain accessible until Cancel."""
        return self._draft_lease is not None

    def update_frame(self, frame: PaneFrame) -> None:
        """Update live view presentation with a published controller frame."""
        prev = self._current_frame
        prev_key = (
            (str(prev.target.socket_path), prev.target.workspace_id, prev.target.pane_id)
            if prev and prev.target and prev.target.pane_id
            else None
        )
        new_key = (
            (str(frame.target.socket_path), frame.target.workspace_id, frame.target.pane_id)
            if frame.target and frame.target.pane_id
            else None
        )

        pane_changed = prev_key != new_key

        if pane_changed:
            if self._draft_lease is None:
                self._feedback_message = None
            self._output.reset_scroll()
            self._latest_rendered_text = None
            self._stashed_rich_text = None
            if self._draft_lease is not None:
                self._draft_invalid = True
        else:
            if self._draft_lease is not None:
                if (
                    frame.lease != self._draft_lease
                    or frame.phase in ("stale", "unavailable")
                ):
                    self._draft_invalid = True

        self._current_frame = frame

        # Never display a different pane's output under the original draft heading.
        if self._draft_invalid:
            self._stashed_rich_text = None
            self._latest_rendered_text = None
            self._output.update_snapshot(
                Text("Draft retained for original target. Cancel to view the current pane.",
                     style="yellow"),
                reset=True,
            )
        else:
            if frame.phase == "loading" and not frame.text_ansi:
                rich = Text("Loading output…", style="dim")
                snapshot_key = None
                raw_payload: str | Text = rich
            else:
                rich = Text.from_ansi(sanitize_ansi(frame.text_ansi))
                snapshot_key = frame.text_ansi
                raw_payload = frame.text_ansi
            self._stashed_rich_text = rich
            if not self._output.is_paused and (
                snapshot_key != self._latest_rendered_text or pane_changed
            ):
                self._latest_rendered_text = snapshot_key
                self._output.update_snapshot(raw_payload, reset=pane_changed)

        self._refresh_identity()
        self._refresh_state()
        self._refresh_controls()

        if (
            frame.phase in ("empty", "stale", "unavailable")
            or not (frame.target and frame.target.pane_id)
            or frame.reply_eligible is False
        ):
            self._pending_focus = False
        elif (
            self._pending_focus
            and frame.phase in ("live", "degraded")
            and frame.lease is not None
            and not self._draft_invalid
            and frame.reply_eligible is not False
            and not self._prompt_input.disabled
        ):
            self._pending_focus = False
            self._prompt_input.focus()
            self.call_after_refresh(self._prompt_input.focus)

    def _on_pause_changed(self, is_paused: bool) -> None:
        self._refresh_state()
        self._refresh_controls()

    def _handle_terminal_input(
        self, text: Optional[str], keys: Optional[list[str]]
    ) -> None:
        if (
            self._current_frame is not None
            and self._current_frame.lease is not None
            and self._current_frame.phase in ("live", "degraded")
            and not self._draft_invalid
        ):
            self.post_message(
                self.TerminalInputRequested(
                    lease=self._current_frame.lease,
                    text=text,
                    keys=keys,
                )
            )

    def _refresh_identity(self) -> None:
        if self._draft_invalid and self._draft_target:
            t = self._draft_target
            label = Text("[Original target] ", style="bold yellow")
            label.append(f"{safe_display(t.session_name)} · {safe_display(t.workspace_id)}")
            if t.tab_id:
                label.append(f" · {safe_display(t.tab_id)}")
            if t.pane_id:
                label.append(f" · {safe_display(t.pane_id)}")
            self._identity.update(label)
            self._identity.tooltip = Text(
                f"Original draft target: {safe_display(t.socket_path)} · "
                f"{safe_display(t.workspace_id)} · {safe_display(t.pane_id)}"
            )
            return

        frame = self._current_frame
        if frame and frame.target and frame.target.pane_id:
            t = frame.target
            label = Text(
                f"{safe_display(t.session_name)} · {safe_display(t.workspace_id)}",
                style="bold white",
            )
            if t.tab_id:
                label.append(f" · {safe_display(t.tab_id)}", style="dim")
            label.append(f" · {safe_display(t.pane_id)}", style="bold")
            self._identity.update(label)
            self._identity.tooltip = Text(
                f"Socket: {safe_display(t.socket_path)} · "
                f"workspace: {safe_display(t.workspace_id)} · pane: {safe_display(t.pane_id)}"
            )
        else:
            self._identity.update(Text("No pane selected", style="dim"))
            self._identity.tooltip = None

    def _refresh_state(self) -> None:
        frame = self._current_frame
        if self._draft_invalid:
            # Keep the delivery outcome visible even when this original draft is
            # pinned to a target that has since changed, including at 40 columns.
            warning = Text()
            if self._feedback_message:
                feedback = self._feedback_message.plain
                for prefix, label in (
                    ("Uncertain:", "Uncertain"),
                    ("Rejected:", "Rejected"),
                    ("Original prompt accepted", "Accepted"),
                    ("Sending…", "Sending…"),
                ):
                    if feedback.startswith(prefix):
                        warning.append(f"{label} · ", style=self._feedback_message.style)
                        break
            warning.append(
                "! target changed—cancel this draft to reply elsewhere", style="bold red"
            )
            self._state_bar.update(warning)
            detail = self._feedback_message.plain if self._feedback_message else ""
            self._state_bar.tooltip = Text(f"{warning.plain}\n{detail}\n{WAITING_LIMITATION_NOTE}")
            return

        if not frame or frame.phase == "empty":
            self._state_bar.update(Text("○ empty · select an agent pane", style="dim"))
            self._state_bar.tooltip = WAITING_LIMITATION_NOTE
            return

        line = Text()
        if self._output.is_paused:
            line.append("[Paused] · ", style="bold magenta")
        # Lifecycle precedes feedback and status so stale/unavailable remains visible.
        if frame.phase == "live":
            line.append("● live", style="green")
        elif frame.phase == "degraded":
            line.append("! degraded (polling 3s)", style="yellow")
        elif frame.phase == "loading":
            line.append("○ loading…", style="cyan")
        elif frame.phase == "stale":
            line.append("× stale", style="red")
        elif frame.phase == "unavailable":
            line.append("× unavailable", style="red")

        if self._feedback_message:
            line.append(" · ")
            line.append_text(self._feedback_message)
        line.append(" · ")
        if frame.agent_status == "blocked":
            line.append("! blocked (reason unknown—inspect output)", style="yellow")
        else:
            line.append_text(status_label(frame.agent_status))

        if frame.error:
            line.append(f" · {safe_display(frame.error)}", style="red")
        else:
            line.append(" · input need unconfirmed", style="dim")

        self._state_bar.update(line)
        self._state_bar.tooltip = Text(f"{line.plain}\n{WAITING_LIMITATION_NOTE}")

    def _refresh_controls(self) -> None:
        self._btn_follow.disabled = not self._output.is_paused
        self._btn_target.disabled = not (
            self._draft_target
            or (self._current_frame and self._current_frame.target
                and self._current_frame.target.pane_id)
        )

        if self._current_frame is None or self._current_frame.phase == "empty":
            self._prompt_input.disabled = True
            self._prompt_input.placeholder = "No pane selected"
        elif self._draft_invalid:
            self._prompt_input.disabled = True
            self._prompt_input.placeholder = (
                "Draft retained for previous target — Cancel draft to send"
            )
        elif self._send_in_flight:
            self._prompt_input.disabled = True
            self._prompt_input.placeholder = "Sending prompt…"
        elif self._current_frame.phase == "loading":
            self._prompt_input.disabled = True
            self._prompt_input.placeholder = "Pane is loading…"
        elif self._current_frame.phase in ("stale", "unavailable"):
            self._prompt_input.disabled = True
            self._prompt_input.placeholder = (
                f"Pane {self._current_frame.phase} — cannot accept input"
            )
        elif self._current_frame.lease is None:
            self._prompt_input.disabled = True
            self._prompt_input.placeholder = "Terminal input disabled in demo mode"
        elif self._current_frame.reply_eligible is False:
            self._prompt_input.disabled = True
            self._prompt_input.placeholder = "Agent is not ready for input"
        else:
            self._prompt_input.disabled = False
            self._prompt_input.placeholder = (
                "Type command or prompt and press Enter… (Esc to exit focus)"
            )

        if self._draft_lease is not None:
            self.add_class("-reply-active")
            self._reply_text.read_only = self._send_in_flight
            self._btn_cancel.disabled = self._send_in_flight

            has_text = bool(self._reply_text.text and self._reply_text.text.strip())
            can_send = bool(
                has_text
                and not self._send_in_flight
                and not self._draft_invalid
                and self._current_frame is not None
                and self._current_frame.can_reply
                and self._current_frame.lease == self._draft_lease
            )
            self._btn_send.disabled = not can_send
            if self._draft_invalid:
                self._btn_send.tooltip = "Target changed—cancel this draft to reply elsewhere."
            elif not has_text:
                self._btn_send.tooltip = "Enter ordinary text to send."
            elif not can_send:
                self._btn_send.tooltip = "Target cannot accept replies."
            else:
                self._btn_send.tooltip = "Send ordinary text prompt (at most once)."
        else:
            self.remove_class("-reply-active")
            can_reply = bool(
                self._current_frame is not None
                and self._current_frame.can_reply
                and self._current_frame.lease is not None
                and not self._send_in_flight
            )
            self._btn_reply.disabled = not can_reply
            if can_reply:
                self._btn_reply.tooltip = "Compose an ordinary text reply for this agent."
            else:
                self._btn_reply.tooltip = (
                    "Replies only available for verified active agents "
                    "(terminal panes and demo mode are read-only)."
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-reply":
            event.stop()
            if self._current_frame and self._current_frame.lease and self._current_frame.can_reply:
                self._draft_lease = self._current_frame.lease
                self._draft_target = self._current_frame.target
                self._draft_invalid = False
                self._send_in_flight = False
                self._feedback_message = None
                self._reply_text.text = ""
                self._refresh_controls()
                self._refresh_identity()
                self._refresh_state()
                self._reply_text.focus()
        elif event.button.id == "btn-cancel-reply":
            event.stop()
            self.cancel_draft()
        elif event.button.id == "btn-send-reply":
            event.stop()
            self.send_draft()
        elif event.button.id == "btn-target-details":
            event.stop()
            target = self._draft_target or (
                self._current_frame.target if self._current_frame else None
            )
            lease = self._draft_lease or (
                self._current_frame.lease if self._current_frame else None
            )
            if target is not None and target.pane_id is not None:
                self.app.push_screen(TargetDetails(target, lease, bool(self._draft_target)))
        elif event.button.id == "btn-follow-output":
            event.stop()
            self._output.follow()
            if self._stashed_rich_text is not None:
                self._latest_rendered_text = (
                    self._current_frame.text_ansi if self._current_frame else None
                )
                payload: str | Text = (
                    self._current_frame.text_ansi
                    if self._current_frame and self._current_frame.text_ansi
                    else self._stashed_rich_text
                )
                self._output.update_snapshot(payload)
            self._refresh_state()
            self._refresh_controls()

    def cancel_draft(self) -> None:
        """Cancel and discard the unsent draft."""
        if self._send_in_flight:
            return
        self._draft_lease = None
        self._draft_target = None
        self._draft_invalid = False
        self._feedback_message = None
        self._reply_text.text = ""
        if self._current_frame is not None:
            self._latest_rendered_text = None
            self.update_frame(self._current_frame)
        self._refresh_controls()
        self._refresh_identity()
        self._refresh_state()
        self.post_message(self.DraftCancelled())

    def send_draft(self) -> None:
        """Dispatch the unsent draft text to the pinned lease."""
        if (
            not self._draft_lease
            or self._draft_invalid
            or self._send_in_flight
            or self._current_frame is None
            or not self._current_frame.can_reply
            or self._current_frame.lease != self._draft_lease
        ):
            return
        text = self._reply_text.text
        if not text or not text.strip():
            return
        self._send_in_flight = True
        self._feedback_message = Text("Sending…", style="cyan")
        self._refresh_controls()
        self._refresh_state()
        self.post_message(self.ReplyRequested(ReplyRequest(lease=self._draft_lease, text=text)))

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "reply-text":
            self._refresh_controls()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "prompt-input":
            event.stop()
            self._submit_inline_prompt()

    def _submit_inline_prompt(self) -> None:
        if (
            self._current_frame is None
            or self._current_frame.phase not in ("live", "degraded")
            or self._current_frame.lease is None
            or self._draft_invalid
        ):
            return
        text = self._prompt_input.value
        self._prompt_input.value = ""
        self.post_message(
            self.TerminalInputRequested(
                lease=self._current_frame.lease,
                text=text if text else None,
                keys=["enter"],
            )
        )

    def focus_input(self) -> None:
        """Focus the inline prompt input now, or as soon as the selected pane becomes live."""
        self._pending_focus = True
        self._refresh_controls()
        if (
            self._current_frame is not None
            and self._current_frame.phase in ("live", "degraded")
            and self._current_frame.lease is not None
            and not self._draft_invalid
            and self._current_frame.reply_eligible is not False
            and not self._prompt_input.disabled
        ):
            self._prompt_input.focus()
            self.call_after_refresh(self._prompt_input.focus)
            self._pending_focus = False

    def on_key(self, event: events.Key) -> None:
        if not self._prompt_input.has_focus:
            return
        if event.key in (
            "escape",
            "ctrl+right_square_bracket",
            "ctrl+]",
            "ctrl+close_bracket",
        ):
            event.stop()
            self.post_message(self.FocusReturnRequested(origin=self._prompt_input))
            return

        if event.key == "ctrl+c" and self._prompt_input.value != "":
            event.stop()
            self._prompt_input.value = ""
            return

        if self._prompt_input.value == "":
            empty_key_map = {
                "up": "up",
                "down": "down",
                "tab": "tab",
                "shift+tab": "backtab",
                "backtab": "backtab",
                "ctrl+c": "ctrl+c",
                "ctrl+d": "ctrl+d",
                "ctrl+z": "ctrl+z",
                "ctrl+l": "ctrl+l",
            }
            if event.key in empty_key_map:
                event.stop()
                self._handle_terminal_input(None, [empty_key_map[event.key]])

    def apply_reply_result(self, result: ReplyResult) -> None:
        """Apply submission outcome delivered from the app submission worker."""
        if result.lease != self._draft_lease and result.lease != self._inline_pending_lease:
            # Stale completion must not unlock, modify, or clear a newer send.
            return
        self._send_in_flight = False
        is_inline = result.lease == self._inline_pending_lease
        self._inline_pending_lease = None

        if result.outcome == "accepted":
            if is_inline:
                self._feedback_message = Text("Prompt accepted", style="green")
                self._refresh_controls()
                self._refresh_state()
            elif not self._draft_invalid:
                self._draft_lease = None
                self._draft_target = None
                self._draft_invalid = False
                self._reply_text.text = ""
                self._feedback_message = Text("Prompt accepted", style="green")
                self._refresh_controls()
                self._refresh_identity()
                self._refresh_state()
                self.post_message(self.DraftCancelled())
            else:
                self._feedback_message = Text("Original prompt accepted", style="green")
                self._refresh_controls()
                self._refresh_state()
        elif result.outcome == "rejected":
            self._feedback_message = Text(
                f"Rejected: {safe_display(result.message)}", style="bold red"
            )
            self._refresh_controls()
            self._refresh_state()
        elif result.outcome == "uncertain":
            self._feedback_message = Text(
                f"Uncertain: {safe_display(result.message)} (inspect output before retrying)",
                style="bold yellow",
            )
            self._refresh_controls()
            self._refresh_state()


FocusReturnRequested = LivePaneView.FocusReturnRequested

