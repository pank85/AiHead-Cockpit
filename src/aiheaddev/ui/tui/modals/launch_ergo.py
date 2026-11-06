"""Modal screen for Squad configuration and Ergo Pipeline launching."""

from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, Union

from pydantic import BaseModel
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, TextArea

from herdr_master.core.models import Squad
from herdr_master.pipeline.squad import resolve_squad, save_squad_as_default
from aiheaddev.ui.tui.modals.project_folder import ProjectFolderField, initial_project_folder
from aiheaddev.ui.tui.state import (
    binding_launch_preview,
    project_config_file,
)
from aiheaddev.ui.tui.widgets.squad_editor import SquadEditor


class ErgoLaunchParams(BaseModel):
    intent: str
    workspace_label: str
    squad: Optional[Squad] = None
    start_pipeline: bool = True
    customize_squad_first: bool = False
    project_path: Optional[Path] = None
    ai_head_override: Optional[bool] = None


class LaunchErgoModal(ModalScreen[Optional[ErgoLaunchParams]]):
    """2-column Squad Settings & Ergo Pipeline Launcher.

    Consumes reusable SquadEditor for topology and role editing.
    Cancellation discards the deep copy and does not write app state.
    """

    DEFAULT_CSS = """
    LaunchErgoModal {
        align: center middle;
        background: rgba(11, 15, 25, 0.85);
    }
    .modal-shell {
        width: 96%;
        height: 100%;
        max-height: 100%;
        background: #161E2E;
        border: thick #A855F7;
        padding: 0 1;
        color: #F8FAFC;
    }
    .modal-header {
        height: 2;
        max-height: 2;
    }
    .modal-title {
        color: #A855F7;
        text-style: bold;
    }
    .modal-subtitle {
        color: #94A3B8;
    }
    .top-section {
        height: 12;
        border: solid #334155;
    }
    .left-tree-col {
        width: 32;
        height: 100%;
        border-right: solid #334155;
        background: #111827;
    }
    .col-header {
        color: #94A3B8;
        background: #1E293B;
        text-style: bold;
    }
    .squad-tree {
        height: 1fr;
        background: #111827;
        color: #F8FAFC;
    }
    .right-editor-col {
        width: 1fr;
        height: 100%;
        padding: 0 1;
        background: #161E2E;
    }
    .editor-fields {
        height: 1fr;
    }
    .editor-heading {
        height: 1;
        color: #38BDF8;
        text-style: bold;
    }
    .field-label {
        height: 1;
        color: #94A3B8;
    }
    .options-grid {
        height: auto;
    }
    .option-column {
        width: 1fr;
        height: auto;
    }
    .option-column-secondary {
        margin-left: 1;
    }
    .option-column SelectCurrent > Static#label {
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    .editor-fields > .field-label {
        height: auto;
        max-height: 2;
    }
    .worker-pool-buttons {
        height: 1;
    }
    .worker-pool-buttons Button {
        width: 1fr;
        min-width: 4;
        height: 1;
        min-height: 1;
        border: none;
        margin-right: 1;
        background: #334155;
        color: #F8FAFC;
    }
    .worker-pool-buttons Button:hover {
        background: #475569;
    }
    .status-msg {
        height: 1;
        max-height: 2;
        color: #F59E0B;
    }
    .bottom-section {
        height: 1fr;
        border: solid #334155;
        background: #161E2E;
        padding: 0 1;
    }
    .project-row {
        height: 4;
    }
    .project-row ProjectFolderField {
        width: 1fr;
    }
    .ws-col {
        width: 22;
        margin-left: 1;
    }
    .aihead-col {
        width: 22;
        margin-left: 1;
    }
    .aihead-col Select {
        width: 100%;
    }
    .prompt-row {
        height: 1fr;
    }
    .prompt-col {
        width: 1fr;
        height: 1fr;
    }
    #ergo-intent {
        height: 1fr;
        min-height: 3;
        border: solid #334155;
        background: #1E293B;
        color: #F8FAFC;
    }
    #ergo-intent:focus {
        border: solid #A855F7;
    }
    .action-button-bar {
        width: 18;
        height: 1fr;
        margin-left: 1;
    }
    .action-button-bar Button {
        width: 100%;
        min-width: 10;
        margin: 0;
    }
    #btn-launch {
        background: #059669;
        color: #F8FAFC;
        text-style: bold;
    }
    #btn-launch:hover {
        background: #047857;
    }
    #btn-cancel {
        background: #334155;
        color: #F8FAFC;
    }
    #btn-cancel:hover {
        background: #475569;
    }
    LaunchErgoModal.narrow .left-tree-col {
        width: 22;
    }
    LaunchErgoModal.narrow .ws-col {
        width: 20;
    }
    LaunchErgoModal.narrow .action-button-bar {
        width: 14;
    }
    LaunchErgoModal.short .action-button-bar Button {
        height: 1;
        min-height: 1;
        border: none;
    }
    LaunchErgoModal.stacked-options .left-tree-col {
        width: 18;
    }
    LaunchErgoModal.stacked-options .options-grid {
        layout: vertical;
    }
    LaunchErgoModal.stacked-options .option-column {
        width: 100%;
        margin-left: 0;
    }
    LaunchErgoModal.stacked-options .ws-col {
        width: 16;
    }
    """

    def __init__(
        self,
        squad: Squad,
        *,
        sources: Optional[dict] = None,
        mode: str = "herdr",
        native_harness: Optional[str] = None,
        project_path: Optional[Path] = None,
        config_error: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.mode = mode
        self.native_harness = native_harness
        self.project_path = initial_project_folder(project_path)
        self.config_error = config_error
        self._submitting = False
        ts = datetime.now().strftime("%Y%m%d-%H%M")
        self.default_label = f"ergo-{ts}"

        self.squad_editor = SquadEditor(
            squad,
            sources=sources,
            mode=mode,
            native_harness=native_harness,
            config_error=config_error,
            include_summary=False,
            classes="top-section",
        )

        self._project_folder = ProjectFolderField(self.project_path)
        self._project_folder.tooltip = (
            "Changing folders reloads that project's squad defaults and discards unsaved edits."
        )

        self._preview = Static("", id="launch-preview", classes="status-msg")
        self._intent_input = TextArea(
            "",
            soft_wrap=True,
            tab_behavior="focus",
            show_line_numbers=False,
            id="ergo-intent",
        )
        self._intent_input.tooltip = (
            "Describe the job across multiple lines. Enter adds a line; Tab moves to the actions."
        )
        self._ws_input = Input(value=self.default_label, placeholder="Workspace label")
        self._aihead_select = Select(
            [
                ("Inherit (Config)", "inherit"),
                ("Enable AiHead", "enable"),
                ("Disable AiHead", "disable"),
            ],
            value="inherit",
            allow_blank=False,
            id="ergo-aihead-override",
        )

    @property
    def squad(self) -> Squad:
        return self.squad_editor.squad

    @squad.setter
    def squad(self, value: Squad) -> None:
        self.squad_editor.reload_squad(value)

    @property
    def sources(self) -> dict:
        return self.squad_editor.sources

    @sources.setter
    def sources(self, value: dict) -> None:
        self.squad_editor.sources = dict(value)

    @property
    def status_text(self) -> str:
        return self.squad_editor.status_text

    @status_text.setter
    def status_text(self, value: str) -> None:
        self.squad_editor.status_text = value

    @property
    def _summary(self) -> Static:
        return self.squad_editor.summary_widget

    @property
    def _status_label(self) -> Static:
        return self.squad_editor._status_label

    @property
    def _description_input(self):
        return self.squad_editor._description_input

    @property
    def _harness_select(self):
        return self.squad_editor._harness_select

    @property
    def _model_input(self):
        return self.squad_editor._model_input

    @property
    def _effort_select(self):
        return self.squad_editor._effort_select

    @property
    def _effort_note(self):
        return self.squad_editor._effort_note

    def _load_agent_into_editor(self, target: Union[str, Tuple[str, int]]) -> None:
        self.squad_editor.load_agent_into_editor(target)

    def _apply_current_editor_changes(self) -> Optional[str]:
        return self.squad_editor._apply_current_editor_changes()

    def _set_status(self, text: str) -> None:
        self.squad_editor.set_status(text)

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-shell"):
            with Vertical(classes="modal-header"):
                yield Static("⚙️ Squad Settings & Ergo Pipeline Launcher", classes="modal-title")
                yield self.squad_editor.summary_widget

            yield self.squad_editor

            with Vertical(classes="bottom-section"):
                with Horizontal(classes="project-row"):
                    yield self._project_folder
                    with Vertical(classes="ws-col"):
                        yield Label("Workspace label:", classes="field-label")
                        yield self._ws_input
                    with Vertical(classes="aihead-col"):
                        yield Label("AiHead Decision:", classes="field-label")
                        yield self._aihead_select
                yield self._preview
                with Horizontal(classes="prompt-row"):
                    with Vertical(classes="prompt-col"):
                        yield Label("Feature objective / prompt:", classes="field-label")
                        yield self._intent_input
                    with Vertical(classes="action-button-bar"):
                        yield Button("Start Ergo", id="btn-start-ergo", variant="success")
                        yield Button("Save Default", id="btn-save-default", variant="warning")
                        yield Button("Cancel", id="btn-cancel", variant="default")

    def on_mount(self) -> None:
        self._adapt_layout(self.size.width, self.size.height)
        self._refresh_preview()

    def on_resize(self, event: Resize) -> None:
        if self.is_mounted:
            self._adapt_layout(event.size.width, event.size.height)

    def _adapt_layout(self, width: int, height: int) -> None:
        self.set_class(width < 100, "narrow")
        self.set_class(width < 72, "stacked-options")
        self.set_class(height < 32, "short")
        self.squad_editor.adapt_layout(width, height)
        preferred = 16 if isinstance(self.squad_editor._selected_target, tuple) else 12
        try:
            self.query_one(".top-section").styles.height = min(preferred, max(8, height - 20))
        except Exception:
            pass

    def _refresh_preview(self) -> None:
        squad = self.squad_editor.get_squad()
        self._preview.update(
            binding_launch_preview(
                squad.scout,
                role="scout",
                mode=self.mode,
                native_harness=self.native_harness,
            )
        )

    def on_squad_editor_changed(self, event: SquadEditor.Changed) -> None:
        self._refresh_preview()

    def _sync_project_folder(self) -> None:
        """Load the selected project's defaults without changing the cockpit's project."""
        try:
            path = self._project_folder.selected_path()
            if path == self.project_path and not self.config_error:
                return
            resolved = resolve_squad(path, mode=self.mode, native_harness=self.native_harness)
        except Exception as exc:
            self.config_error = (
                f"Invalid project squad settings: {exc}. "
                "Fix .herdr-master.toml before launching. "
                "Refusing to launch product defaults."
            )
            self._set_status(f"Project folder/settings error: {exc}")
            self.squad_editor.config_error = self.config_error
            self.squad_editor._refresh_summary()
            return
        self.project_path = path
        self.config_error = None
        self.squad_editor.reload_squad(
            resolved.squad,
            sources=resolved.sources,
            config_error=None,
        )
        self._refresh_preview()
        self._set_status("Loaded selected project's squad defaults.")

    def on_project_folder_field_changed(self, event: ProjectFolderField.Changed) -> None:
        event.stop()
        self._sync_project_folder()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id

        if btn_id == "btn-save-default":
            self._sync_project_folder()
            error = self.squad_editor.validate()
            if error:
                self._set_status(f"[red]Validation Error: {error}[/red]")
                return
            try:
                path = save_squad_as_default(
                    self.squad_editor.get_squad(),
                    project_config_file(self.project_path),
                )
            except Exception as exc:
                self._set_status(f"[red]Error saving default: {exc}[/red]")
                return
            self._set_status(f"[bold green]Saved default: {path}[/bold green]")

        elif btn_id == "btn-start-ergo":
            if self._submitting:
                return
            self._sync_project_folder()
            error = self.squad_editor.validate()
            if error:
                self._set_status(f"[red]Validation Error: {error}[/red]")
                return
            self._submitting = True
            aihead_val = str(self._aihead_select.value)
            ai_head_override = True if aihead_val == "enable" else (False if aihead_val == "disable" else None)
            self.dismiss(
                ErgoLaunchParams(
                    intent=self._intent_input.text.strip() or "Execute standard Ergo pipeline",
                    workspace_label=self._ws_input.value.strip() or self.default_label,
                    squad=self.squad_editor.get_squad(),
                    start_pipeline=True,
                    project_path=self._project_folder.selected_path(),
                    ai_head_override=ai_head_override,
                )
            )

        elif btn_id == "btn-cancel":
            self.dismiss(None)


ErgoSettingsModal = LaunchErgoModal
