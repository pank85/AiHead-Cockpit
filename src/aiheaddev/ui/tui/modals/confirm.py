"""Confirmation modal dialog for destructive or landing operations."""

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ConfirmModal(ModalScreen[bool]):
    """Modal dialog prompting the operator for confirmation."""

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
        background: rgba(11, 15, 25, 0.85);
    }
    .confirm-dialog {
        width: 60;
        height: auto;
        background: #161E2E;
        border: thick #38BDF8;
        padding: 1 2;
        color: #F8FAFC;
    }
    .confirm-title {
        text-align: center;
        text-style: bold;
        color: #38BDF8;
        margin-bottom: 1;
    }
    .confirm-buttons {
        align: center middle;
        margin-top: 1;
    }
    .confirm-buttons Button {
        margin: 0 1;
        min-width: 12;
    }
    #btn-confirm {
        background: #0284C7;
        color: #F8FAFC;
        text-style: bold;
    }
    #btn-confirm:hover {
        background: #0369A1;
    }
    #btn-cancel {
        background: #334155;
        color: #F8FAFC;
    }
    #btn-cancel:hover {
        background: #475569;
    }
    """

    def __init__(self, title: str, message: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.title_text = title
        self.message_text = message

    def compose(self) -> ComposeResult:
        with Vertical(classes="confirm-dialog"):
            yield Static(self.title_text, classes="confirm-title")
            yield Static(self.message_text)
            with Horizontal(classes="confirm-buttons"):
                yield Button("Confirm", id="btn-confirm", variant="primary")
                yield Button("Cancel", id="btn-cancel", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-confirm":
            self.dismiss(True)
        else:
            self.dismiss(False)
