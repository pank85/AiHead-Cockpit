"""Modal screen for single-harness orchestration launching."""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Union

from pydantic import BaseModel
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from herdr_master.core.config import OrchestrationConfig
from herdr_master.core.models import HarnessKind
from aiheaddev.ui.tui.modals.project_folder import ProjectFolderField, initial_project_folder
from aiheaddev.ui.tui.widgets.orchestration_editor import (
    DEFAULT_MODELS,
    OrchestrationEditor,
    _ensure_active_app,
)


class OrchestrationLaunchParams(BaseModel):
    """Parameters for launching a single-harness orchestration pipeline."""

    intent: str
    workspace_label: str
    harness: HarnessKind
    scout_model: str
    architect_model: str
    coder_model: str
    reviewer_model: str
    scout_effort: Optional[str] = None
    architect_effort: Optional[str] = None
    reviewer_effort: Optional[str] = None
    dangerously_skip_permissions: bool = True
    project_path: Optional[Path] = None
    ai_head_override: Optional[bool] = None


class LaunchOrchestrationModal(ModalScreen[Optional[OrchestrationLaunchParams]]):
    """Modal screen for configuring and launching a single-harness orchestration run.

    Consumes reusable OrchestrationEditor for native harness and model editing.
    Provides directory selection, intent/label specification, and permission skipping.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    LaunchOrchestrationModal {
        align: center middle;
        background: rgba(11, 15, 25, 0.85);
    }
    .modal-box {
        width: 76;
        max-width: 95%;
        height: 100%;
        max-height: 100%;
        background: #161E2E;
        border: thick #10B981;
        padding: 0 1;
        color: #F8FAFC;
    }
    .modal-fields {
        height: 1fr;
    }
    .modal-title {
        color: #10B981;
        text-style: bold;
        margin-bottom: 0;
    }
    .modal-desc {
        color: #94A3B8;
        margin-bottom: 1;
    }
    .field-label {
        color: #94A3B8;
        margin-top: 1;
        height: 1;
    }
    .model-row {
        height: auto;
        margin-top: 1;
    }
    .model-col {
        width: 1fr;
        height: auto;
    }
    .model-col-right {
        margin-left: 1;
    }
    .model-col .field-label {
        margin-top: 0;
    }
    #orch-skip-permissions {
        margin-top: 1;
        margin-bottom: 1;
        height: auto;
        color: #F8FAFC;
    }
    .status-msg {
        height: 2;
        max-height: 2;
        color: #F59E0B;
    }
    .button-bar {
        height: 3;
        align: center middle;
    }
    .button-bar Button {
        margin: 0 1;
        min-width: 14;
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
    """

    def __init__(
        self,
        *,
        project_path: Optional[Path] = None,
        config_error: Optional[str] = None,
        default_intent: str = "",
        default_harness: Optional[Union[HarnessKind, str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.project_path = initial_project_folder(project_path)
        self.config_error = config_error
        self._submitting = False

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.default_label = f"orch-{ts}"

        with _ensure_active_app():
            self._project_folder = ProjectFolderField(self.project_path)
            self._project_folder.tooltip = (
                "Project root directory for the orchestration pipeline."
            )

            self._intent_input = Input(
                value=default_intent,
                placeholder="Task or goal for single-harness pipeline…",
                id="orch-intent",
            )
            self._workspace_label_input = Input(
                value=self.default_label,
                placeholder=self.default_label,
                id="orch-workspace-label",
            )

            self.editor = OrchestrationEditor(
                project_path=self.project_path,
                default_harness=default_harness,
            )

            self._skip_permissions_checkbox = Checkbox(
                "Dangerously skip permissions (--dangerously-skip-permissions)",
                value=True,
                id="orch-skip-permissions",
            )
            self._aihead_select = Select(
                [
                    ("Inherit (Config)", "inherit"),
                    ("Enable AiHead", "enable"),
                    ("Disable AiHead", "disable"),
                ],
                value="inherit",
                allow_blank=False,
                id="orch-aihead-override",
            )

    @property
    def _harness_select(self):
        return self.editor._harness_select

    @property
    def _scout_input(self):
        return self.editor._scout_input

    @property
    def _architect_input(self):
        return self.editor._architect_input

    @property
    def _coder_input(self):
        return self.editor._coder_input

    @property
    def _reviewer_input(self):
        return self.editor._reviewer_input

    @property
    def _status_label(self):
        return self.editor._status_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            with VerticalScroll(classes="modal-fields"):
                yield Static("⚡ Launch Single-Harness Orchestration", classes="modal-title")
                yield Static(
                    "Self-contained subagent pipeline in a single harness. "
                    "Scout -> Architect -> Coders -> Reviewer.",
                    classes="modal-desc",
                )

                yield self._project_folder

                yield Label("Intent / Goal:", classes="field-label")
                yield self._intent_input

                yield Label("Workspace Label:", classes="field-label")
                yield self._workspace_label_input

                yield self.editor

                yield Label("AiHead Decision:", classes="field-label")
                yield self._aihead_select

                yield self._skip_permissions_checkbox

            with Horizontal(classes="button-bar"):
                yield Button("Launch Orchestration", id="btn-launch", variant="success")
                yield Button("Cancel", id="btn-cancel", variant="default")

    def on_mount(self) -> None:
        if self.config_error:
            self._set_status(self.config_error)

    def _set_status(self, text: str) -> None:
        self.editor.set_status(text)

    def on_project_folder_field_changed(self, event: ProjectFolderField.Changed) -> None:
        event.stop()
        self.config_error = None
        self._set_status("")
        try:
            self.project_path = self._project_folder.selected_path()
            self.editor.reload_for_project(self.project_path)
        except (ValueError, OSError) as exc:
            self._set_status(str(exc))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        if event.button.id != "btn-launch" or self._submitting:
            return

        intent = self._intent_input.value.strip()
        if not intent:
            self._set_status("Intent is required.")
            return

        try:
            project_path = self._project_folder.selected_path()
        except (ValueError, OSError) as exc:
            self._set_status(f"Invalid project folder: {exc}")
            return

        label = self._workspace_label_input.value.strip() or self.default_label

        error = self.editor.validate()
        if error:
            self._set_status(error)
            return

        orch_config = self.editor.get_config()
        skip_perms = bool(self._skip_permissions_checkbox.value)
        aihead_val = str(self._aihead_select.value)
        ai_head_override = True if aihead_val == "enable" else (False if aihead_val == "disable" else None)

        self._submitting = True
        params = OrchestrationLaunchParams(
            intent=intent,
            workspace_label=label,
            harness=orch_config.harness,
            scout_model=self.editor._scout_input.value.strip(),
            architect_model=self.editor._architect_input.value.strip(),
            coder_model=" | ".join(orch_config.worker_models),
            reviewer_model=self.editor._reviewer_input.value.strip(),
            scout_effort=orch_config.scout_effort,
            architect_effort=orch_config.architect_effort,
            reviewer_effort=orch_config.reviewer_effort,
            dangerously_skip_permissions=skip_perms,
            project_path=project_path,
            ai_head_override=ai_head_override,
        )
        self.dismiss(params)

    def action_cancel(self) -> None:
        self.dismiss(None)
