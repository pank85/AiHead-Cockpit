"""Modal screen to launch a new Herdr Master session."""

from pathlib import Path
from typing import Optional

from pydantic import BaseModel
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from herdr_master.core.catalog import CatalogError
from herdr_master.core.models import EffortLevel, HarnessKind
from herdr_master.pipeline.launcher import resolve_coordinator_binding
from herdr_master.pipeline.squad import resolve_squad
from aiheaddev.ui.tui.modals.project_folder import ProjectFolderField
from aiheaddev.ui.tui.state import (
    compatibility_preset_note,
    coordinator_route_text,
    effort_select_options,
    harness_select_options,
)


class MasterLaunchParams(BaseModel):
    name: str
    harness: HarnessKind
    model: str
    initial_task: Optional[str] = None
    effort: Optional[EffortLevel] = None
    project_path: Optional[Path] = None
    ai_head_override: Optional[bool] = None


class LaunchMasterModal(ModalScreen[Optional[MasterLaunchParams]]):
    """Modal to configure and launch a new Herdr Master orchestrator.

    Starts on the catalog Scout medium binding. Changing harness is an explicit
    override and requires a model. A typed model is not replaced by a preset.
    """

    DEFAULT_CSS = """
    LaunchMasterModal {
        align: center middle;
        background: rgba(11, 15, 25, 0.85);
    }
    .modal-box {
        width: 72;
        height: 100%;
        max-height: 100%;
        background: #161E2E;
        border: thick #38BDF8;
        padding: 0 1;
        color: #F8FAFC;
    }
    .modal-fields {
        height: 1fr;
    }
    .modal-title {
        color: #38BDF8;
        text-style: bold;
        margin-bottom: 1;
    }
    .modal-desc {
        color: #94A3B8;
        margin-bottom: 1;
    }
    .field-label {
        color: #94A3B8;
        margin-top: 0;
        height: 1;
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
        background: #0284C7;
        color: #F8FAFC;
        text-style: bold;
    }
    #btn-launch:hover {
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

    def __init__(
        self,
        default_name: str = "master-coordinator",
        *,
        config_error: Optional[str] = None,
        project_path: Optional[Path] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.config_error = config_error
        self._project_folder = ProjectFolderField(project_path)
        kind, model, effort = resolve_coordinator_binding()
        self.default_name = default_name
        self._product_kind = kind.value
        self._product_model = model
        self._product_effort = effort.value if effort else "none"
        self._programmatic_model = model
        self._syncing = False
        self._submitting = False
        self.status_text = ""
        effort_options = effort_select_options(kind.value, model)
        self._name_input = Input(value=default_name, placeholder="e.g. master-coordinator")
        self._harness_select = Select(
            harness_select_options(),
            value=kind.value,
            allow_blank=False,
            id="master-harness",
        )
        self._model_input = Input(
            value=model,
            placeholder="Explicit model. Required when the harness is not Scout.",
            id="master-model",
        )
        self._effort_select = Select(
            effort_options,
            value=self._product_effort
            if self._product_effort in {item[1] for item in effort_options}
            else "none",
            allow_blank=False,
            id="master-effort",
        )
        self._task_input = Input(placeholder="Optional initial task to dispatch immediately")
        self._aihead_select = Select(
            [
                ("Inherit (Config)", "inherit"),
                ("Enable AiHead", "enable"),
                ("Disable AiHead", "disable"),
            ],
            value="inherit",
            allow_blank=False,
            id="master-aihead-override",
        )
        self._preview = Static("", id="master-preview", classes="status-msg")
        self._status = Static("", id="master-status", classes="status-msg")

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            with VerticalScroll(classes="modal-fields"):
                yield Static("👑 Launch New Herdr Master — Multiple Jobs", classes="modal-title")
                yield Static(
                    "Starts on the catalog Scout binding. A different harness is an explicit "
                    "override and needs a model. " + compatibility_preset_note(),
                    classes="modal-desc",
                )

                yield self._project_folder

                yield Label("Master Agent Name:", classes="field-label")
                yield self._name_input

                yield Label("Harness Kind:", classes="field-label")
                yield self._harness_select

                yield Label("Model:", classes="field-label")
                yield self._model_input

                yield Label("Reasoning Effort:", classes="field-label")
                yield self._effort_select

                yield Label("Initial Task / Job (optional):", classes="field-label")
                yield self._task_input

                yield Label("AiHead Decision:", classes="field-label")
                yield self._aihead_select

            yield self._preview
            yield self._status
            with Horizontal(classes="button-bar"):
                yield Button("Launch Master", id="btn-launch", variant="primary")
                yield Button("Cancel", id="btn-cancel", variant="default")

    def on_mount(self) -> None:
        if self.config_error:
            self._set_status(self.config_error)
        self._refresh_preview()

    def on_project_folder_field_changed(self, event: ProjectFolderField.Changed) -> None:
        event.stop()
        self.config_error = None
        self._set_status("")

    def _model_is_custom(self) -> bool:
        return self._model_input.value != self._programmatic_model

    def _set_model(self, value: str) -> None:
        self._syncing = True
        self._programmatic_model = value
        try:
            with self.prevent(Input.Changed):
                self._model_input.value = value
        finally:
            self._syncing = False

    def _set_effort(self, value: str) -> None:
        options = effort_select_options(
            str(self._harness_select.value),
            self._model_input.value,
        )
        values = {item[1] for item in options}
        selected = value if value in values else "none"
        if selected not in values:
            selected = options[0][1]
        self._syncing = True
        try:
            with self.prevent(Select.Changed):
                self._effort_select.set_options(options)
                self._effort_select.value = selected
        finally:
            self._syncing = False

    def _set_status(self, text: str) -> None:
        self.status_text = text
        self._status.update(text)

    def _resolved_request(self) -> tuple[HarnessKind, str, Optional[EffortLevel]]:
        model = self._model_input.value.strip()
        effort = str(self._effort_select.value)
        return resolve_coordinator_binding(
            str(self._harness_select.value),
            model or None,
            None if effort == "none" else effort,
        )

    def _refresh_preview(self) -> None:
        try:
            kind, model, effort = self._resolved_request()
        except CatalogError as exc:
            self._preview.update(str(exc))
            return
        effort_text = effort.value if effort else None
        self._preview.update(coordinator_route_text(kind.value, model, effort_text))

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._syncing or event.input is not self._model_input:
            return
        self._set_effort(str(self._effort_select.value))
        self._refresh_preview()

    def on_select_changed(self, event: Select.Changed) -> None:
        if self._syncing:
            return
        if event.select == self._harness_select:
            kind = str(event.value)
            if not self._model_is_custom():
                if kind == self._product_kind:
                    self._set_model(self._product_model)
                    self._set_effort(self._product_effort)
                else:
                    self._set_model("")
                    self._set_effort("none")
            else:
                self._set_effort(str(self._effort_select.value))
        self._refresh_preview()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        if event.button.id != "btn-launch" or self._submitting:
            return
        name = self._name_input.value.strip() or self.default_name
        try:
            project_path = self._project_folder.selected_path()
            resolve_squad(project_path)
            kind, model, effort = self._resolved_request()
        except (ValueError, OSError, CatalogError) as exc:
            self._set_status(str(exc))
            return
        self._submitting = True
        aihead_val = str(self._aihead_select.value)
        ai_head_override = True if aihead_val == "enable" else (False if aihead_val == "disable" else None)
        self.dismiss(
            MasterLaunchParams(
                name=name,
                harness=kind,
                model=model,
                initial_task=self._task_input.value.strip() or None,
                effort=effort,
                project_path=project_path,
                ai_head_override=ai_head_override,
            )
        )
