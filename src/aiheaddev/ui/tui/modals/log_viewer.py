"""Log Viewer / Error Drawer modal for inspecting runtime diagnostics."""

import logging
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, RichLog, Select, Static

from aiheaddev.core.logging import get_recent_logs


class LogViewerModal(ModalScreen[None]):
    """Modal screen displaying recent logs and diagnostics with level filtering."""

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close"),
        Binding("q", "dismiss_modal", "Close"),
        Binding("r", "refresh_logs", "Refresh"),
    ]

    DEFAULT_CSS = """
    LogViewerModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }
    #log-dialog {
        width: 85%;
        height: 80%;
        background: #0B0F19;
        border: solid #334155;
        padding: 1 2;
    }
    #log-header {
        height: 3;
        margin-bottom: 1;
        border-bottom: solid #1E293B;
    }
    #log-title {
        text-style: bold;
        color: #F8FAFC;
        height: 2;
        content-align: left middle;
    }
    #log-controls {
        height: 3;
        align: right middle;
    }
    #log-content {
        height: 1fr;
        background: #020617;
        border: solid #1E293B;
        padding: 1;
        overflow-y: auto;
    }
    #log-footer {
        height: 3;
        margin-top: 1;
        align: right middle;
    }
    #btn-log-refresh {
        margin-right: 1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._min_level = logging.DEBUG

    def compose(self) -> ComposeResult:
        with Vertical(id="log-dialog"):
            with Horizontal(id="log-header"):
                yield Static("📋 Cockpit Runtime & Diagnostic Logs", id="log-title")
                with Horizontal(id="log-controls"):
                    yield Select(
                        [
                            ("All Logs (DEBUG+)", logging.DEBUG),
                            ("Info & Higher (INFO+)", logging.INFO),
                            ("Warnings & Errors (WARN+)", logging.WARNING),
                            ("Errors Only (ERROR)", logging.ERROR),
                        ],
                        value=logging.DEBUG,
                        id="select-log-level",
                        allow_blank=False,
                    )
            yield RichLog(id="log-content", highlight=True, markup=True)
            with Horizontal(id="log-footer"):
                yield Button("Refresh", id="btn-log-refresh", variant="primary")
                yield Button("Close (Esc)", id="btn-log-close", variant="default")

    def on_mount(self) -> None:
        self._update_log_display()

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def action_refresh_logs(self) -> None:
        self._update_log_display()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-log-close":
            self.action_dismiss_modal()
        elif event.button.id == "btn-log-refresh":
            self.action_refresh_logs()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "select-log-level" and event.value is not None:
            self._min_level = int(event.value)
            self._update_log_display()

    def _update_log_display(self) -> None:
        rich_log = self.query_one("#log-content", RichLog)
        rich_log.clear()
        lines = get_recent_logs(limit=300, min_level=self._min_level)
        if not lines:
            rich_log.write("[dim]No log events recorded matching the selected filter.[/dim]")
            return

        for line in lines:
            if " [ERROR] " in line or "Traceback" in line:
                rich_log.write(f"[bold red]{line}[/bold red]")
            elif " [WARNING] " in line:
                rich_log.write(f"[yellow]{line}[/yellow]")
            elif " [INFO] " in line:
                rich_log.write(f"[cyan]{line}[/cyan]")
            else:
                rich_log.write(f"[dim]{line}[/dim]")
