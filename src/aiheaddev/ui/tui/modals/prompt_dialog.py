"""Prompt dialog modal for sending instructions to a worker or role."""

from typing import Optional

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class PromptDialog(ModalScreen[Optional[str]]):
    """Modal dialog allowing operator to prompt an active agent pane."""

    DEFAULT_CSS = """
    PromptDialog {
        align: center middle;
        background: rgba(11, 15, 25, 0.85);
    }
    .prompt-box {
        width: 70;
        height: auto;
        background: #161E2E;
        border: thick #A855F7;
        padding: 1 2;
        color: #F8FAFC;
    }
    .prompt-title {
        color: #A855F7;
        text-style: bold;
        margin-bottom: 1;
    }
    .prompt-buttons {
        margin-top: 1;
        align: right middle;
    }
    .prompt-buttons Button {
        margin-left: 1;
        min-width: 12;
    }
    #btn-send {
        background: #059669;
        color: #F8FAFC;
        text-style: bold;
    }
    #btn-send:hover {
        background: #047857;
    }
    #btn-cancel {
        background: #334155;
        color: #F8FAFC;
    }
    #btn-cancel:hover {
        background: #475569;
    }
    """

    def __init__(self, target_name: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.target_name = target_name
        self._input = Input(placeholder="Enter prompt instructions...")

    def compose(self) -> ComposeResult:
        with Vertical(classes="prompt-box"):
            yield Static(f"Prompt Agent: [bold]{self.target_name}[/bold]", classes="prompt-title")
            yield self._input
            with Horizontal(classes="prompt-buttons"):
                yield Button("Send Prompt", id="btn-send", variant="success")
                yield Button("Cancel", id="btn-cancel", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-send":
            self.dismiss(self._input.value.strip())
        else:
            self.dismiss(None)
