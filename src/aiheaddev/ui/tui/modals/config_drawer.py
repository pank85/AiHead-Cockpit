"""Configuration drawer modal for editing role bindings and the worker pool."""

from pathlib import Path
from typing import Optional, Tuple

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Select, Static

from herdr_master.core.catalog import CatalogError, worker_route_note
from herdr_master.core.models import EffortLevel, HarnessBinding, HarnessKind, Squad
from herdr_master.pipeline.squad import save_squad_as_default, validate_squad
from aiheaddev.ui.tui.state import (
    catalog_worker_template,
    effort_select_options,
    harness_select_options,
    normalize_worker_order,
    project_config_file,
    validate_binding_for_editor,
)


class ConfigDrawer(ModalScreen[Optional[Tuple[Squad, bool]]]):
    """Drawer for role bindings. Cancellation discards the deep copy."""

    DEFAULT_CSS = """
    ConfigDrawer {
        align: right top;
        background: rgba(11, 15, 25, 0.85);
    }
    .drawer-panel {
        width: 65;
        height: 100%;
        background: #161E2E;
        border-left: thick #38BDF8;
        padding: 1 2;
        color: #F8FAFC;
    }
    .drawer-title {
        color: #38BDF8;
        text-style: bold;
        margin-bottom: 1;
    }
    Select {
        height: 3;
        max-height: 3;
    }
    .field-label {
        color: #94A3B8;
        margin-bottom: 0;
    }
    .pool-list {
        height: 8;
        margin: 1 0;
        background: #111827;
        border: solid #334155;
        color: #F8FAFC;
    }
    .button-bar {
        margin-top: 1;
        align: center middle;
    }
    .button-bar Button {
        margin: 0 1;
        min-width: 12;
    }
    #btn-save {
        background: #0284C7;
        color: #F8FAFC;
        text-style: bold;
    }
    #btn-save:hover {
        background: #0369A1;
    }
    #btn-cancel {
        background: #334155;
        color: #F8FAFC;
    }
    #btn-cancel:hover {
        background: #475569;
    }
    .error-label {
        color: #F59E0B;
        margin: 1 0;
        height: auto;
        max-height: 3;
    }
    """

    def __init__(
        self,
        squad: Squad,
        target: Optional[str] = None,
        *,
        mode: str = "herdr",
        native_harness: Optional[str] = None,
        project_path: Optional[Path] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.squad = squad.model_copy(deep=True)
        self.target = target or "scout"
        self.mode = mode
        self.native_harness = native_harness
        self.project_path = project_path
        self.status_text = ""
        self._syncing = False

        role_value = "pool" if "pool" in self.target or "coder" in self.target else self.target
        if role_value not in {"scout", "architect", "reviewer", "pool"}:
            role_value = "scout"
        self._role_select = Select(
            [
                ("Scout", "scout"),
                ("Architect", "architect"),
                ("Reviewer", "reviewer"),
                ("Worker Pool", "pool"),
            ],
            value=role_value,
            allow_blank=False,
        )
        initial = self.squad.scout
        self._harness_select = Select(
            harness_select_options(mode=mode),
            value=initial.kind.value,
            allow_blank=False,
        )
        template = catalog_worker_template(0)
        self._model_input = Input(
            placeholder=(
                f"Explicit model. Blank input is rejected. Catalog scout is {initial.model}"
            ),
        )
        self._effort_label = Label("Effort Level:", classes="field-label")
        self._effort_select = Select(
            effort_select_options(initial.kind.value, initial.model, mode=mode),
            value="none",
            allow_blank=False,
        )
        self._effort_note = Label("", classes="field-label")
        self._role_label = Label("Identifier / Label:", classes="field-label")
        self._label_input = Input(placeholder="Optional capability label")
        self._desc_label = Label("Capability Info (for Architect):", classes="field-label")
        self._desc_input = Input(
            placeholder=template.description or "Capability note for the Architect",
        )
        self._error_label = Static("", id="drawer-status", classes="error-label")
        self._pool_option_list = OptionList(classes="pool-list")
        self._selected_pool_index = 0

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="drawer-panel"):
            yield Static("⚙️ Squad & Model Configuration", classes="drawer-title")

            yield Label("Target Role / Section:", classes="field-label")
            yield self._role_select

            with Vertical(id="pool-mgmt-container"):
                yield Label("Worker Pool Entries:", classes="field-label")
                yield self._pool_option_list
                with Horizontal():
                    yield Button("Add", id="btn-add-worker", variant="success")
                    yield Button("Remove", id="btn-remove-worker", variant="error")
                    yield Button("Up", id="btn-up-worker")
                    yield Button("Down", id="btn-down-worker")

            yield Label("Harness Kind:", classes="field-label")
            yield self._harness_select

            yield Label("Model:", classes="field-label")
            yield self._model_input

            yield self._effort_label
            yield self._effort_select
            yield self._effort_note

            yield self._role_label
            yield self._label_input

            yield self._desc_label
            yield self._desc_input

            yield Button("Apply Changes", id="btn-apply-binding", variant="primary")
            yield self._error_label

            with Horizontal(classes="button-bar"):
                yield Button("Save Run", id="btn-save-run", variant="success")
                yield Button("Save Default", id="btn-save-default", variant="warning")
                yield Button("Cancel", id="btn-cancel", variant="default")

    def on_mount(self) -> None:
        self._refresh_pool_list()
        self._load_current_target_fields()

    def _set_status(self, text: str) -> None:
        self.status_text = text
        self._error_label.update(text)

    def _refresh_pool_list(self) -> None:
        self._pool_option_list.clear_options()
        for idx, worker in enumerate(self.squad.worker_pool):
            label = worker.label or f"worker_{idx + 1}"
            state = "" if worker.enabled else " disabled"
            desc = f" — {worker.description[:26]}..." if worker.description else ""
            self._pool_option_list.add_option(
                f"[{idx + 1}] {worker.to_slug(include_effort=False)} ({label}){state}{desc}"
            )
        if self.squad.worker_pool:
            idx = min(self._selected_pool_index, len(self.squad.worker_pool) - 1)
            self._pool_option_list.highlighted = idx

    def _refresh_worker_note(self, binding: HarnessBinding) -> None:
        try:
            note = worker_route_note(
                binding.kind.value,
                binding.model,
                mode=self.mode,
                native_harness=self.native_harness,
            )
        except CatalogError as exc:
            note = str(exc)
        self._effort_note.update("Reasoning effort is chosen per task by the Architect. " + note)

    def _set_effort_options(self, kind: str, model: str, current: Optional[str]) -> None:
        options = effort_select_options(kind, model, mode=self.mode)
        values = {item[1] for item in options}
        selected = current if current in values else "none"
        if selected not in values:
            selected = options[0][1]
        self._effort_select.set_options(options)
        self._effort_select.value = selected

    def _load_current_target_fields(self) -> None:
        mode = str(self._role_select.value)
        pool_container = self.query_one("#pool-mgmt-container")
        self._syncing = True
        try:
            with self.prevent(Select.Changed), self.prevent(Input.Changed):
                if mode in ("scout", "architect", "reviewer"):
                    pool_container.display = False
                    self._effort_label.display = True
                    self._effort_select.display = True
                    self._effort_note.display = False
                    self._desc_label.display = False
                    self._desc_input.display = False
                    self._role_label.update("Role Identifier:")
                    self._populate_fields(getattr(self.squad, mode))
                else:
                    pool_container.display = True
                    self._effort_label.display = False
                    self._effort_select.display = False
                    self._effort_note.display = True
                    self._desc_label.display = True
                    self._desc_input.display = True
                    self._role_label.update("Worker Identifier / Label:")
                    if self.squad.worker_pool and self._selected_pool_index < len(
                        self.squad.worker_pool
                    ):
                        worker = self.squad.worker_pool[self._selected_pool_index]
                        self._populate_fields(worker)
                        self._refresh_worker_note(worker)
        finally:
            self._syncing = False

    def _populate_fields(self, binding: HarnessBinding) -> None:
        self._harness_select.value = binding.kind.value
        self._model_input.value = binding.model
        current = binding.effort.value if binding.effort else "none"
        self._set_effort_options(binding.kind.value, binding.model, current)
        self._label_input.value = binding.label or ""
        self._desc_input.value = binding.description or ""

    def on_select_changed(self, event: Select.Changed) -> None:
        if self._syncing:
            return
        if event.select == self._role_select:
            self._load_current_target_fields()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._selected_pool_index = event.option_index
        self._load_current_target_fields()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-apply-binding":
            self._apply_current_fields()
        elif bid == "btn-add-worker":
            idx = len(self.squad.worker_pool)
            self.squad.worker_pool.append(catalog_worker_template(idx))
            normalize_worker_order(self.squad)
            self._selected_pool_index = len(self.squad.worker_pool) - 1
            self._refresh_pool_list()
            self._load_current_target_fields()
        elif bid == "btn-remove-worker":
            pool_len = len(self.squad.worker_pool)
            if pool_len > 1 and self._selected_pool_index < pool_len:
                self.squad.worker_pool.pop(self._selected_pool_index)
                normalize_worker_order(self.squad)
                self._selected_pool_index = max(0, self._selected_pool_index - 1)
                self._refresh_pool_list()
                self._load_current_target_fields()
        elif bid == "btn-up-worker":
            idx = self._selected_pool_index
            if idx > 0:
                pool = self.squad.worker_pool
                pool[idx - 1], pool[idx] = pool[idx], pool[idx - 1]
                normalize_worker_order(self.squad)
                self._selected_pool_index -= 1
                self._refresh_pool_list()
        elif bid == "btn-down-worker":
            idx = self._selected_pool_index
            if idx < len(self.squad.worker_pool) - 1:
                pool = self.squad.worker_pool
                pool[idx + 1], pool[idx] = pool[idx], pool[idx + 1]
                normalize_worker_order(self.squad)
                self._selected_pool_index += 1
                self._refresh_pool_list()
        elif bid == "btn-save-run":
            self._save_and_dismiss(is_default=False)
        elif bid == "btn-save-default":
            self._save_and_dismiss(is_default=True)
        elif bid == "btn-cancel":
            self.dismiss(None)

    def _apply_current_fields(self) -> bool:
        mode = str(self._role_select.value)
        model = self._model_input.value.strip()
        if not model:
            self._set_status("Model is empty. Refusing to invent a model.")
            return False
        try:
            kind = HarnessKind(str(self._harness_select.value))
        except ValueError:
            self._set_status("Unknown harness. Refusing to substitute agy.")
            return False
        effort_text = str(self._effort_select.value)
        effort = None if effort_text == "none" else EffortLevel(effort_text)
        label = self._label_input.value.strip() or None
        desc = self._desc_input.value.strip() or None
        if mode == "pool":
            pool = self.squad.worker_pool
            if not pool or self._selected_pool_index >= len(pool):
                self._set_status("Select a worker before applying.")
                return False
            existing = pool[self._selected_pool_index]
            updated = HarnessBinding(
                kind=kind,
                model=model,
                effort=None,
                label=label,
                description=desc,
                enabled=existing.enabled,
                order=self._selected_pool_index,
            )
            role = "coder"
        else:
            existing = getattr(self.squad, mode)
            updated = HarnessBinding(
                kind=kind,
                model=model,
                effort=effort,
                label=label,
                description=existing.description,
                enabled=existing.enabled,
                order=existing.order,
            )
            role = mode
        message = validate_binding_for_editor(
            updated,
            role=role,
            mode=self.mode,
            native_harness=self.native_harness,
        )
        if message:
            self._set_status(message)
            return False
        if mode == "pool":
            self.squad.worker_pool[self._selected_pool_index] = updated
            self._refresh_pool_list()
            self._refresh_worker_note(updated)
        else:
            setattr(self.squad, mode, updated)
        normalize_worker_order(self.squad)
        self._set_status("Applied successfully")
        return True

    def _save_and_dismiss(self, is_default: bool) -> None:
        if not self._apply_current_fields():
            return
        normalize_worker_order(self.squad)
        errors = validate_squad(self.squad)
        if errors:
            self._set_status(errors[0])
            return
        for role in ("scout", "architect", "reviewer"):
            message = validate_binding_for_editor(
                getattr(self.squad, role),
                role=role,
                mode=self.mode,
                native_harness=self.native_harness,
            )
            if message:
                self._set_status(message)
                return
        for worker in self.squad.worker_pool:
            message = validate_binding_for_editor(
                worker,
                role="coder",
                mode=self.mode,
                native_harness=self.native_harness,
            )
            if message:
                self._set_status(message)
                return
        if is_default:
            try:
                save_squad_as_default(self.squad, project_config_file(self.project_path))
            except Exception as exc:
                self._set_status(f"Failed to save default: {exc}")
                return
        self.dismiss((self.squad.model_copy(deep=True), is_default))
