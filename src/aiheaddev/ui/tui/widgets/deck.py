"""Center Deck hosting the WorkspaceBrowser and LivePaneView."""

from textual.app import ComposeResult
from textual.containers import Vertical

from aiheaddev.ui.tui.live_pane import PaneFrame, ReplyResult
from aiheaddev.ui.tui.state import AppState
from aiheaddev.ui.tui.widgets.live_output import LivePaneView
from aiheaddev.ui.tui.widgets.workspaces import WorkspaceBrowser


class CenterDeck(Vertical):
    """Host container for WorkspaceBrowser and LivePaneView."""

    DEFAULT_CSS = """
    CenterDeck {
        width: 1fr;
        height: 1fr;
        background: #0B0F19;
    }
    CenterDeck.-full WorkspaceBrowser {
        width: 1fr;
        height: 1fr;
    }
    CenterDeck.-full LivePaneView {
        display: none;
    }
    CenterDeck.-horizontal {
        layout: horizontal;
    }
    CenterDeck.-horizontal WorkspaceBrowser {
        width: 50;
        min-width: 48;
        height: 100%;
    }
    CenterDeck.-horizontal LivePaneView {
        width: 1fr;
        height: 100%;
        display: block;
    }
    CenterDeck.-vertical {
        layout: vertical;
    }
    CenterDeck.-vertical WorkspaceBrowser {
        width: 100%;
        height: 12;
    }
    CenterDeck.-vertical LivePaneView {
        width: 100%;
        height: 1fr;
        display: block;
    }
    """

    def __init__(self, app_state: AppState, **kwargs) -> None:
        super().__init__(**kwargs)
        self.app_state = app_state
        self._workspace_view = WorkspaceBrowser(app_state)
        self._live_pane_view = LivePaneView(id="live-pane")

    def compose(self) -> ComposeResult:
        yield self._workspace_view
        yield self._live_pane_view

    def on_mount(self) -> None:
        self._check_layout()

    def on_resize(self, event: object) -> None:
        self._check_layout()

    def on_live_pane_view_draft_cancelled(
        self, event: LivePaneView.DraftCancelled
    ) -> None:
        self._check_layout()

    def on_workspace_browser_pane_activated(
        self, event: WorkspaceBrowser.PaneActivated
    ) -> None:
        self.focus_live_input()

    def focus_live_input(self) -> None:
        self._live_pane_view.focus_input()

    def update_state(self, app_state: AppState) -> None:
        self.app_state = app_state
        self._workspace_view.update_state(app_state)
        self._check_layout()

    def update_live_frame(self, frame: PaneFrame) -> None:
        self._live_pane_view.update_frame(frame)
        self._check_layout()

    def apply_reply_result(self, result: ReplyResult) -> None:
        self._live_pane_view.apply_reply_result(result)
        self._check_layout()

    def _check_layout(self) -> None:
        has_active = self._live_pane_view.has_active_pane
        has_draft = self._live_pane_view.has_retained_draft
        show_live = has_active or has_draft

        width = (
            self.size.width
            if self.size.width > 0
            else (self.app.size.width if self.app else 80)
        )

        if not show_live:
            self.remove_class("-horizontal", "-vertical")
            self.add_class("-full")
        else:
            self.remove_class("-full")
            if width >= 110:
                self.remove_class("-vertical")
                self.add_class("-horizontal")
            else:
                self.remove_class("-horizontal")
                self.add_class("-vertical")
