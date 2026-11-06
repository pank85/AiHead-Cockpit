"""Action Footer widget displaying keybindings and status hints."""

from rich.text import Text
from textual.events import Resize
from textual.widgets import Static


class ActionFooter(Static):
    """Bottom footer bar indicating available keybindings with styled badge chips."""

    DEFAULT_CSS = """
    ActionFooter {
        height: 1;
        dock: bottom;
        background: #161E2E;
        color: #94A3B8;
        padding: 0 1;
        overflow: hidden hidden;
    }
    """

    def on_mount(self) -> None:
        self.render_hints(self.size.width if self.size.width > 0 else 80)

    def on_resize(self, event: Resize) -> None:
        self.render_hints(event.size.width)

    def render_hints(self, width: int) -> None:
        t = Text()
        if width >= 75:
            keys = [
                ("n", "New Master"),
                ("e", "New Ergo"),
                ("s/c", "Settings"),
                ("o", "Open"),
                ("d", "Delete WS"),
                ("q", "Quit"),
            ]
        elif width >= 55:
            keys = [
                ("n", "Master"),
                ("e", "Ergo"),
                ("s/c", "Settings"),
                ("o", "Open"),
                ("d", "Del"),
                ("q", "Quit"),
            ]
        else:
            keys = [
                ("o", "Open"),
                ("d", "Del"),
                ("q", "Quit"),
                ("n", "M"),
                ("e", "E"),
                ("s", "Set"),
            ]
        for key, label in keys:
            t.append(f"[{key}]", style="bold #0B0F19 on #38BDF8")
            t.append(f" {label} ", style="#F8FAFC on #1E293B")
            t.append(" ")
        self.update(t)
