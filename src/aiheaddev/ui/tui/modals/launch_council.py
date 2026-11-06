"""Launch modal for AI Council deliberation rounds."""

from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Tuple

from pydantic import BaseModel
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, TextArea

from herdr_master.core.config import CouncilConfig, HerdrMasterConfig, load_project_config
from herdr_master.core.models import Squad
from aiheaddev.ui.tui.modals.project_folder import ProjectFolderField


class CouncilLaunchParams(BaseModel):
    """Launch parameters for AI Council run."""

    intent: str
    project_folder: Path
    workspace_label: str
    council_config: CouncilConfig


class LaunchCouncilModal(ModalScreen[Optional[CouncilLaunchParams]]):
    """Modal screen to configure and launch an AI Council deliberation run."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    LaunchCouncilModal {
        align: center middle;
        background: rgba(11, 15, 25, 0.85);
    }
    .council-shell {
        width: 92%;
        height: 92%;
        background: #161E2E;
        border: thick #4F46E5;
        padding: 0 1;
        color: #F8FAFC;
    }
    .council-title {
        height: 1;
        color: #818CF8;
        text-style: bold;
    }
    .council-scroll {
        height: 1fr;
        padding: 1 0;
    }
    .section-label {
        color: #94A3B8;
        text-style: bold;
        margin-top: 1;
        height: 1;
    }
    .field-row {
        height: 3;
        margin-bottom: 1;
    }
    .field-row Label {
        width: 18;
        padding-top: 1;
        color: #94A3B8;
    }
    .field-row Input {
        width: 1fr;
    }
    .field-row Select {
        width: 26;
    }
    #council-roster-summary {
        background: #111827;
        padding: 0 1;
        border: solid #334155;
        height: 5;
        margin-bottom: 1;
        color: #F8FAFC;
    }
    #council-idea-input {
        height: 8;
        margin-bottom: 1;
        background: #1E293B;
        border: solid #334155;
        color: #F8FAFC;
    }
    #council-idea-input:focus {
        border: solid #818CF8;
    }
    .status-bar {
        height: 1;
        color: #F59E0B;
    }
    .footer-row {
        height: 3;
        margin-top: 1;
    }
    .footer-row Button {
        margin-right: 2;
        min-width: 14;
    }
    #btn-launch {
        background: #4F46E5;
        color: #F8FAFC;
        text-style: bold;
    }
    #btn-launch:hover {
        background: #4338CA;
    }
    #btn-cancel {
        background: #334155;
        color: #F8FAFC;
    }
    #btn-cancel:hover {
        background: #475569;
    }
    """

    def __init__(
        self,
        config: Optional[HerdrMasterConfig] = None,
        project_path: Optional[Path] = None,
        council_config: Optional[CouncilConfig] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.project_path = project_path
        self.config = config or load_project_config(project_path)
        self.council_config = council_config or self.config.council.model_copy(deep=True)

        self._project_folder = ProjectFolderField(
            project_path=self.project_path,
            config=self.config,
        )
        self._default_label = f"council-{datetime.now().strftime('%Y%m%d-%H%M')}"

    def compose(self) -> ComposeResult:
        with Vertical(classes="council-shell"):
            yield Label("🏛 Launch AI Council Deliberation", classes="council-title")
            with VerticalScroll(classes="council-scroll"):
                yield self._project_folder

                yield Label("Workspace Label:", classes="section-label")
                with Horizontal(classes="field-row"):
                    yield Label("Label:")
                    yield Input(value=self._default_label, id="council-label-input")

                yield Label("Deliberation Roster:", classes="section-label")
                yield Static(self._format_roster(), id="council-roster-summary")
                with Horizontal(classes="field-row"):
                    yield Button("Customize Roster in Settings…", id="btn-open-settings")

                yield Label("Feature Objective / Idea Specification:", classes="section-label")
                yield TextArea(
                    "",
                    soft_wrap=True,
                    tab_behavior="focus",
                    show_line_numbers=False,
                    id="council-idea-input",
                )

            yield Static("", id="council-status", classes="status-bar")
            with Horizontal(classes="footer-row"):
                yield Button("Launch Council", id="btn-launch", variant="primary")
                yield Button("Cancel", id="btn-cancel")

    def _format_roster(self) -> str:
        coord = self.council_config.coordinator
        scout = self.council_config.scout
        enabled_members = [m for m in self.council_config.members if m.enabled]
        member_str = ", ".join(f"{m.label} ({m.kind.value}:{m.model})" for m in enabled_members)
        return (
            f"Coordinator: {coord.kind.value}:{coord.model} | Scout: {scout.kind.value}:{scout.model}\n"
            f"Active Members ({len(enabled_members)}): {member_str}"
        )

    def _refresh_roster(self) -> None:
        self.query_one("#council-roster-summary", Static).update(self._format_roster())

    def _set_status(self, text: str) -> None:
        self.query_one("#council-status", Static).update(text)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_project_folder_field_changed(self, event: ProjectFolderField.Changed) -> None:
        event.stop()
        self._set_status("")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "btn-cancel":
            self.dismiss(None)
            return

        if btn_id == "btn-open-settings":
            event.stop()
            from aiheaddev.ui.tui.modals.settings import SettingsModal

            def on_settings_dismissed(result: Optional[Tuple[HerdrMasterConfig, Squad]]) -> None:
                if result is not None:
                    new_cfg, _ = result
                    self.config = new_cfg
                    self.council_config = new_cfg.council.model_copy(deep=True)
                    self._refresh_roster()

            self.app.push_screen(
                SettingsModal(config=self.config, project_path=self.project_path),
                on_settings_dismissed,
            )
            return

        if btn_id == "btn-launch":
            idea = self.query_one("#council-idea-input", TextArea).text.strip()
            if not idea:
                self._set_status("Please provide a specification or idea for the Council.")
                return

            try:
                folder = self._project_folder.selected_path()
            except Exception as exc:
                self._set_status(str(exc))
                return

            label = self.query_one("#council-label-input", Input).value.strip()
            if not label:
                label = self._default_label

            params = CouncilLaunchParams(
                intent=idea,
                project_folder=folder,
                workspace_label=label,
                council_config=self.council_config,
            )
            self.dismiss(params)
