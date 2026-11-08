"""Dedicated full-screen single-page view for the persistent interactive AiHead session."""

from pathlib import Path
from typing import Any, Optional

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Static

from aiheaddev.ui.tui.live_pane import PaneFrame
from aiheaddev.ui.tui.widgets.live_output import LivePaneView


class AiHeadScreen(Screen[None]):
    """Dedicated single-page view for AiHead terminal orchestrator, separate from workspace tree."""

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back to Workspaces"),
        Binding("h", "dismiss_screen", "Back to Workspaces"),
        Binding("s", "open_settings", "Settings"),
        Binding("L", "view_logs", "Logs"),
        Binding("q", "quit_app", "Quit"),
    ]

    DEFAULT_CSS = """
    AiHeadScreen {
        background: #0B0F19;
        color: #F8FAFC;
    }
    #aihead-screen-header {
        height: 3;
        background: #161E2E;
        padding: 0 1;
        border-bottom: solid #334155;
        align: left middle;
    }
    #btn-aihead-back {
        background: #0284C7;
        color: #FFFFFF;
        text-style: bold;
        min-width: 22;
        height: 2;
        border: none;
        margin-right: 1;
    }
    #btn-aihead-back:hover {
        background: #0369A1;
    }
    #aihead-screen-title {
        text-style: bold;
        color: #FBBF24;
        width: auto;
        padding: 0 1;
    }
    #aihead-screen-meta {
        margin-left: 1;
        color: #94A3B8;
        width: 1fr;
        padding: 0;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    #aihead-screen-actions {
        width: auto;
        align: right middle;
    }
    #aihead-screen-actions Button {
        margin-left: 1;
        height: 2;
        border: none;
    }
    #aihead-screen-body {
        height: 1fr;
        width: 100%;
        background: #0B0F19;
        padding: 0;
    }
    #aihead-screen-body LivePaneView {
        height: 100%;
        width: 100%;
        padding: 0;
        background: #0B0F19;
    }
    #aihead-screen-footer {
        height: 3;
        dock: bottom;
        background: #161E2E;
        color: #94A3B8;
        padding: 0 1;
        align: left middle;
        border-top: solid #334155;
    }
    #btn-aihead-back-footer {
        background: #0284C7;
        color: #FFFFFF;
        text-style: bold;
        margin-right: 2;
        min-width: 22;
        height: 2;
        border: none;
    }
    #btn-aihead-back-footer:hover {
        background: #0369A1;
    }
    #aihead-footer-hints {
        width: 1fr;
        padding: 0;
    }

    /* Narrow responsive breakpoint (<100 columns) */
    AiHeadScreen.-narrow #aihead-screen-header {
        height: 3;
        padding: 0 1;
    }
    AiHeadScreen.-narrow #btn-aihead-back {
        min-width: 0;
        margin-right: 1;
        padding: 0 1;
        height: 2;
    }
    AiHeadScreen.-narrow #aihead-screen-title {
        display: none;
    }
    AiHeadScreen.-narrow #aihead-screen-meta {
        display: none;
    }
    AiHeadScreen.-narrow #aihead-screen-actions {
        width: 1fr;
        align: right middle;
    }
    AiHeadScreen.-narrow #aihead-screen-actions Button {
        min-width: 0;
        margin-left: 1;
        padding: 0 1;
        height: 2;
    }
    AiHeadScreen.-narrow #btn-aihead-back-footer {
        min-width: 0;
        margin-right: 1;
        padding: 0 1;
        height: 2;
    }
    AiHeadScreen.-narrow #aihead-footer-hints {
        display: none;
    }
    """

    def __init__(
        self,
        model: Optional[str] = None,
        harness: Optional[str] = None,
        cwd: Optional[str] = None,
        status: str = "ready",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.model = model or "gpt-6.1-sol"
        self.harness = harness or "opencode"
        self.cwd = cwd or str(Path.home())
        self.status = status
        self._live_pane_view = LivePaneView(id="aihead-fullscreen-live-pane")
        self._pending_frame: Optional[PaneFrame] = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="aihead-screen-header"):
            yield Button("← Back to Workspaces", id="btn-aihead-back", variant="primary")
            yield Static("👑 AiHead Terminal Orchestrator", id="aihead-screen-title")
            meta_text = f"· [{self.harness}] {self.model} · 📁 {self.cwd} · ● {self.status}"
            yield Static(meta_text, id="aihead-screen-meta")
            with Horizontal(id="aihead-screen-actions"):
                yield Button("⚙ Settings", id="btn-aihead-settings")
                yield Button("📋 Logs", id="btn-aihead-logs")
        with Vertical(id="aihead-screen-body"):
            yield self._live_pane_view
        with Horizontal(id="aihead-screen-footer"):
            yield Button("← Back to Workspaces", id="btn-aihead-back-footer", variant="primary")
            yield Static(
                " [Esc/h] Back to Workspaces   [s] Settings   [L] Diagnostic Logs   [q] Quit",
                id="aihead-footer-hints",
            )

    def _update_geometry(self, width: int) -> None:
        is_narrow = width < 100
        if is_narrow:
            self.add_class("-narrow")
        else:
            self.remove_class("-narrow")

        try:
            btn_back = self.query_one("#btn-aihead-back", Button)
            btn_back_footer = self.query_one("#btn-aihead-back-footer", Button)
            btn_settings = self.query_one("#btn-aihead-settings", Button)
            btn_logs = self.query_one("#btn-aihead-logs", Button)

            if is_narrow:
                btn_back.label = "← Back"
                btn_back_footer.label = "← Back"
                btn_settings.label = "Settings"
                btn_logs.label = "Logs"
            else:
                btn_back.label = "← Back to Workspaces"
                btn_back_footer.label = "← Back to Workspaces"
                btn_settings.label = "⚙ Settings"
                btn_logs.label = "📋 Logs"
        except Exception:
            pass

    def on_mount(self) -> None:
        width = self.size.width if self.size.width > 0 else (self.app.size.width if self.app else 120)
        self._update_geometry(width)
        if self._pending_frame is not None:
            self._live_pane_view.update_frame(self._pending_frame)
        self._live_pane_view.focus_input()

    def on_resize(self, event: events.Resize) -> None:
        self._update_geometry(event.size.width)

    def on_live_pane_view_focus_return_requested(
        self, event: LivePaneView.FocusReturnRequested
    ) -> None:
        event.stop()
        try:
            self.query_one("#btn-aihead-back").focus()
        except Exception:
            pass

    def update_live_frame(self, frame: PaneFrame) -> None:
        """Update live stream from background PTY controller."""
        self._pending_frame = frame
        if self._live_pane_view is not None:
            self._live_pane_view.update_frame(frame)

    def update_status(self, status: str) -> None:
        """Update header metadata status indicator."""
        self.status = status
        try:
            meta = self.query_one("#aihead-screen-meta", Static)
            meta.update(f"· [{self.harness}] {self.model} · 📁 {self.cwd} · ● {self.status}")
        except Exception:
            pass

    def action_dismiss_screen(self) -> None:
        """Return to main Cockpit workspace tree view."""
        if self.app.screen is self:
            self.app.pop_screen()
            if hasattr(self.app, "_focus_cockpit_tree"):
                self.app._focus_cockpit_tree()

    def action_open_settings(self) -> None:
        """Open settings modal from AiHead screen."""
        if hasattr(self.app, "action_open_settings"):
            self.app.action_open_settings()

    def action_view_logs(self) -> None:
        """Open diagnostic log viewer from AiHead screen."""
        if hasattr(self.app, "action_view_logs"):
            self.app.action_view_logs()

    def action_quit_app(self) -> None:
        self.app.exit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in ("btn-aihead-back", "btn-aihead-back-footer"):
            self.action_dismiss_screen()
        elif event.button.id == "btn-aihead-settings":
            self.action_open_settings()
        elif event.button.id == "btn-aihead-logs":
            self.action_view_logs()
