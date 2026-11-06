"""HeaderBar widget displaying product branding, breadcrumbs, status, and clock."""

from datetime import datetime

from rich.text import Text
from textual.events import Resize
from textual.widgets import Static

from aiheaddev.ui.tui.state import AppState


class HeaderBar(Static):
    """Header bar at the top of the TUI Cockpit with ui-ux-pro-max slate styling."""

    DEFAULT_CSS = """
    HeaderBar {
        height: 1;
        dock: top;
        background: #161E2E;
        color: #F8FAFC;
        padding: 0 1;
        overflow: hidden hidden;
    }
    """

    def __init__(self, app_state: AppState, **kwargs) -> None:
        super().__init__(**kwargs)
        self.app_state = app_state
        self._clock_str = datetime.now().strftime("%H:%M:%S")

    def on_mount(self) -> None:
        """Start local clock timer without network I/O."""
        self.set_interval(1.0, self._tick_clock)
        self.refresh_display()

    def on_resize(self, event: Resize) -> None:
        """Re-render header pills when terminal size changes."""
        self.refresh_display()

    def _tick_clock(self) -> None:
        self._clock_str = datetime.now().strftime("%H:%M:%S")
        self.refresh_display()

    def update_state(self, app_state: AppState) -> None:
        """Update header bar state projection."""
        self.app_state = app_state
        self.refresh_display()

    def refresh_display(self) -> None:
        """Render the styled breadcrumb pills and live status indicators."""
        width = (
            self.size.width
            if self.size.width > 0
            else (self.app.size.width if self.app else 100)
        )

        left = Text()
        # Product Branding
        left.append("HERDR-MASTER ", style="bold #38BDF8")
        if width >= 70:
            left.append("0.1.0 ", style="#64748B")
        left.append("│ ", style="#334155")

        # Session Pill
        if width >= 60:
            left.append("sess: ", style="#64748B")
            left.append(f"{self.app_state.session_name} ", style="bold #F8FAFC")
            left.append("│ ", style="#334155")

        # Workspace Pill
        ws_display = self.app_state.workspace_label or self.app_state.workspace_id or "default"
        left.append("ws: ", style="#64748B")
        left.append(f"{ws_display} ", style="bold #38BDF8")
        left.append("│ ", style="#334155")

        # Target branch cleanliness
        left.append(f"{self.app_state.target_branch} ", style="#C084FC")
        branch_clean = "CLEAN" in self.app_state.branch_status.upper()
        clean_color = "#10B981" if branch_clean else "#EF4444"
        left.append(f"[{self.app_state.branch_status}] ", style=f"bold {clean_color}")

        # Active agents count (if width permits)
        if width >= 90:
            left.append("│ ", style="#334155")
            left.append("agents: ", style="#64748B")
            left.append(f"{self.app_state.active_agents_count} ", style="bold #F8FAFC")

        # Right block: Online Indicator + Clock
        right = Text()
        if self.app_state.socket_connected:
            right.append("● ONLINE", style="bold #10B981")
        else:
            right.append("✖ OFFLINE", style="bold #EF4444")

        if width >= 75:
            right.append(" │ ", style="#334155")
            right.append(f"{self._clock_str}", style="#94A3B8")

        # Combine with space padding if room exists
        padding_len = width - len(left.plain) - len(right.plain) - 2
        if padding_len > 0:
            full = Text()
            full.append_text(left)
            full.append(" " * padding_len)
            full.append_text(right)
            self.update(full)
        else:
            self.update(left)
