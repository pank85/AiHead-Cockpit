"""Reusable OrchestrationEditor widget for single-harness orchestration pipelines."""

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Input, Label, Select, Static

from herdr_master.core.catalog import get_harness, native_binding_error
from herdr_master.core.config import (
    DEFAULT_ORCHESTRATION_MODELS,
    HarnessOrchestrationSettings,
    HerdrMasterConfig,
    OrchestrationConfig,
    load_project_config,
)
from herdr_master.core.models import HarnessKind
from herdr_master.core.slug import parse_binding
from aiheaddev.ui.tui.state import harness_select_options

DEFAULT_MODELS: Dict[str, Dict[str, str]] = DEFAULT_ORCHESTRATION_MODELS


@contextmanager
def _ensure_active_app():
    """Ensure an active Textual app context is present for headless widget testing."""
    from textual._context import active_app

    try:
        current = active_app.get()
        try:
            _ = current.screen
            yield
            return
        except Exception:
            pass
    except LookupError:
        pass

    class _HeadlessApp(App):
        def clear_selection(self) -> None:
            pass

    dummy = _HeadlessApp()
    token = active_app.set(dummy)
    try:
        yield
    finally:
        active_app.reset(token)


EFFORT_OPTIONS = [
    ("Unset", "none"),
    ("Low", "low"),
    ("Medium", "medium"),
    ("High", "high"),
    ("XHigh", "xhigh"),
    ("Max", "max"),
]
_EFFORT_ROLES = ("scout", "architect", "reviewer")
_EFFORT_SUFFIXES = ("low", "medium", "high", "max", "xhigh")


class OrchestrationEditor(Widget):
    """Reusable Textual widget for editing single-harness Orchestration configuration.

    Edits native harness selection, role models for Scout, Architect and Reviewer
    (each with an optional reasoning effort), and a dynamic list of Worker models.
    Workers deliberately omit reasoning effort (the Architect decides it).
    """

    DEFAULT_CSS = """
    OrchestrationEditor {
        height: auto;
        layout: vertical;
    }
    OrchestrationEditor .orch-row {
        height: 1;
        margin-top: 1;
    }
    OrchestrationEditor .orch-row Label {
        width: 11;
        color: #94A3B8;
    }
    OrchestrationEditor .orch-row Input {
        width: 1fr;
    }
    OrchestrationEditor .orch-row Select {
        width: 16;
        margin-left: 1;
    }
    OrchestrationEditor #orch-harness {
        width: 24;
        margin-left: 0;
    }
    OrchestrationEditor .orch-eff-label {
        width: 8;
        margin-left: 1;
    }
    OrchestrationEditor #orch-workers-header Button {
        margin-left: 1;
        min-width: 14;
        background: #334155;
        color: #F8FAFC;
    }
    OrchestrationEditor #orch-workers-header Button:hover {
        background: #475569;
    }
    OrchestrationEditor #orch-workers-box {
        height: auto;
        max-height: 5;
    }
    OrchestrationEditor .orch-worker-row {
        height: 1;
    }
    OrchestrationEditor .orch-worker-row Label {
        width: 11;
        color: #94A3B8;
    }
    OrchestrationEditor .orch-worker-row Input {
        width: 1fr;
    }
    OrchestrationEditor .orch-worker-row Button {
        margin-left: 1;
        min-width: 5;
        background: #334155;
        color: #F8FAFC;
    }
    OrchestrationEditor .orch-worker-row Button:hover {
        background: #475569;
    }
    .status-msg {
        height: 2;
        max-height: 2;
        color: #F59E0B;
    }
    """

    class Changed(Message):
        """Posted when orchestration configuration is edited."""

        def __init__(self, config: OrchestrationConfig) -> None:
            super().__init__()
            self.config = config

    def __init__(
        self,
        config: Optional[OrchestrationConfig] = None,
        *,
        project_path: Optional[Path] = None,
        default_harness: Optional[Union[HarnessKind, str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.project_path = project_path
        self._syncing = False
        self._programmatic_models: Dict[str, str] = {}
        self._worker_seq = 0
        self._worker_rows: List[Horizontal] = []
        self._worker_inputs: List[Input] = []
        self._harness_configs: Dict[str, HarnessOrchestrationSettings] = {}

        if config is not None:
            self._config = config.model_copy(deep=True)
        elif self.project_path:
            try:
                loaded = load_project_config(self.project_path)
                self._config = loaded.orchestration.model_copy(deep=True)
                if default_harness:
                    self._config.harness = HarnessKind(default_harness)
            except Exception:
                initial_harness = (
                    HarnessKind(default_harness) if default_harness else HarnessKind.AGY
                )
                self._config = OrchestrationConfig(harness=initial_harness)
        else:
            initial_harness = (
                HarnessKind(default_harness) if default_harness else HarnessKind.AGY
            )
            self._config = OrchestrationConfig(harness=initial_harness)

        self._default_harness: HarnessKind = (
            HarnessKind(default_harness)
            if default_harness
            else (self._config.default_harness or self._config.harness)
        )

        for k, v in self._config.harnesses.items():
            self._harness_configs[k] = v.model_copy(deep=True)

        active_key = self._config.harness.value
        if active_key not in self._harness_configs:
            if (
                self._config.scout_model
                or self._config.architect_model
                or self._config.worker_models
                or self._config.reviewer_model
                or self._config.scout_effort
                or self._config.architect_effort
                or self._config.reviewer_effort
            ):
                self._harness_configs[active_key] = HarnessOrchestrationSettings(
                    scout_model=self._config.scout_model,
                    architect_model=self._config.architect_model,
                    worker_models=list(self._config.worker_models),
                    reviewer_model=self._config.reviewer_model,
                    scout_effort=self._config.scout_effort,
                    architect_effort=self._config.architect_effort,
                    reviewer_effort=self._config.reviewer_effort,
                )

        options = harness_select_options(mode="native")
        initial_val = self._config.harness.value
        option_values = {val for _, val in options}
        if initial_val not in option_values and options:
            initial_val = options[0][1]

        self._current_harness = initial_val

        with _ensure_active_app():
            self._harness_select = Select(
                options,
                value=initial_val,
                allow_blank=False,
                compact=True,
                id="orch-harness",
            )
            self._scout_input = Input(
                value=self._config.scout_model or "",
                id="orch-scout-model",
                placeholder="e.g. flash_lite",
                compact=True,
            )
            self._architect_input = Input(
                value=self._config.architect_model or "",
                id="orch-architect-model",
                placeholder="e.g. pro",
                compact=True,
            )
            self._reviewer_input = Input(
                value=self._config.reviewer_model or "",
                id="orch-reviewer-model",
                placeholder="e.g. pro",
                compact=True,
            )
            self._effort_selects: Dict[str, Select] = {}
            for role in _EFFORT_ROLES:
                self._effort_selects[role] = Select(
                    EFFORT_OPTIONS,
                    value=getattr(self._config, f"{role}_effort", None) or "none",
                    allow_blank=False,
                    compact=True,
                    id=f"orch-{role}-effort",
                )
            self._add_worker_btn = Button(
                "+ Add Worker", id="orch-add-worker", variant="success", compact=True
            )
            self._remove_worker_btn = Button(
                "- Remove", id="orch-remove-worker", variant="error", compact=True
            )
            self._workers_box = VerticalScroll(id="orch-workers-box")
            self._status_label = Static("", id="orch-status", classes="status-msg")

            for model in self._config.worker_models:
                self._create_worker_row(model)

            has_explicit_models = bool(
                self._config.scout_model
                or self._config.architect_model
                or self._config.worker_models
                or self._config.reviewer_model
            )
            if not has_explicit_models:
                self._load_harness_into_ui(initial_val)
            else:
                self._save_current_to_harness(initial_val)

    # ------------------------------------------------------------------
    # Worker list
    # ------------------------------------------------------------------
    def _create_worker_row(self, model: str = "") -> Horizontal:
        self._worker_seq += 1
        seq = self._worker_seq
        with _ensure_active_app():
            inp = Input(
                value=model,
                id=f"orch-worker-model-{seq}",
                placeholder="e.g. flash",
                compact=True,
                classes="orch-worker-input",
            )
            rm = Button("✕", id=f"orch-worker-rm-{seq}", variant="error", compact=True)
            row = Horizontal(
                Label("", classes="orch-worker-label"),
                inp,
                rm,
                classes="orch-worker-row",
            )
        self._worker_inputs.append(inp)
        self._worker_rows.append(row)
        self._relabel_workers()
        return row

    def _relabel_workers(self) -> None:
        for i, row in enumerate(self._worker_rows):
            try:
                row.query_one(".orch-worker-label", Label).update(f"Worker {i + 1}:")
            except Exception:
                pass

    @property
    def worker_count(self) -> int:
        return len(self._worker_inputs)

    def add_worker(self, model: str = "") -> None:
        """Append a worker row (any number of workers is allowed)."""
        if not model.strip():
            model = self._get_default_model(str(self._harness_select.value), "coder")
        row = self._create_worker_row(model)
        if self.is_mounted:
            self._workers_box.mount(row)
            self._workers_box.scroll_end(animate=False)
            if not self._syncing:
                self._save_current_to_harness(self._current_harness)
                self._notify_changed()

    def remove_worker(self, index: Optional[int] = None) -> bool:
        """Remove a worker row (default: last). At least one worker is kept."""
        if len(self._worker_inputs) <= 1:
            self.set_status("At least one worker is required.")
            return False
        idx = len(self._worker_inputs) - 1 if index is None else index
        if not 0 <= idx < len(self._worker_inputs):
            return False
        self._worker_inputs.pop(idx)
        row = self._worker_rows.pop(idx)
        if row.is_attached:
            row.remove()
        self._relabel_workers()
        self.set_status("")
        if self.is_mounted and not self._syncing:
            self._save_current_to_harness(self._current_harness)
            self._notify_changed()
        return True

    def _worker_values(self) -> List[str]:
        return [i.value.strip() for i in self._worker_inputs]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "orch-add-worker":
            event.stop()
            self.add_worker()
        elif bid == "orch-remove-worker":
            event.stop()
            self.remove_worker()
        elif bid.startswith("orch-worker-rm-"):
            event.stop()
            for idx, row in enumerate(self._worker_rows):
                if event.button in row.children:
                    self.remove_worker(idx)
                    break

    def compose(self) -> ComposeResult:
        with Horizontal(classes="orch-row"):
            yield Label("Harness:")
            yield self._harness_select

        for role in _EFFORT_ROLES:
            with Horizontal(classes="orch-row"):
                yield Label(f"{role.capitalize()}:")
                yield getattr(self, f"_{role}_input")
                yield Label("Effort:", classes="orch-eff-label")
                yield self._effort_selects[role]

        with Horizontal(classes="orch-row", id="orch-workers-header"):
            yield Label("Workers:")
            yield self._add_worker_btn
            yield self._remove_worker_btn
        with self._workers_box:
            for row in self._worker_rows:
                yield row

        yield self._status_label

    def on_mount(self) -> None:
        self._relabel_workers()

    def set_status(self, text: str) -> None:
        """Update status line."""
        self._status_label.update(text)

    def _get_project_model(self, harness: str, role: str) -> Optional[str]:
        if not self.project_path:
            return None
        try:
            cfg = load_project_config(self.project_path)
            if cfg.models and cfg.models.default_squad:
                slug = cfg.models.default_squad.get(role)
                if slug:
                    b = parse_binding(slug)
                    if b.kind.value == harness:
                        return b.model
        except Exception:
            pass
        return None

    def _get_default_model(self, harness: str, role: str) -> str:
        project_model = self._get_project_model(harness, role)
        if project_model:
            return project_model
        return DEFAULT_MODELS.get(harness, {}).get(role, "general")

    def _save_current_to_harness(self, harness: str) -> None:
        """Capture current editor inputs into per-harness in-memory storage."""
        worker_models = [m for m in self._worker_values() if m]
        fields = dict(
            scout_model=self._scout_input.value.strip() or None,
            architect_model=self._architect_input.value.strip() or None,
            worker_models=worker_models,
            reviewer_model=self._reviewer_input.value.strip() or None,
            scout_effort=self._effort_value("scout"),
            architect_effort=self._effort_value("architect"),
            reviewer_effort=self._effort_value("reviewer"),
        )
        try:
            self._harness_configs[harness] = HarnessOrchestrationSettings(**fields)
        except Exception:
            self._harness_configs[harness] = (
                HarnessOrchestrationSettings.model_construct(**fields)
            )

    def _update_worker_rows(self, models: List[str]) -> None:
        """Update worker input rows to match the desired model list."""
        models = list(models) or [""]
        while len(self._worker_inputs) > len(models) and len(self._worker_inputs) > 1:
            idx = len(self._worker_inputs) - 1
            self._worker_inputs.pop(idx)
            row = self._worker_rows.pop(idx)
            if row.is_attached:
                row.remove()
        while len(self._worker_inputs) < len(models):
            row = self._create_worker_row("")
            if self.is_mounted:
                self._workers_box.mount(row)
        for inp, m in zip(self._worker_inputs, models):
            with self.prevent(Input.Changed):
                inp.value = m
        self._relabel_workers()

    def _load_harness_into_ui(self, harness: str) -> None:
        """Swap inputs to the saved configuration for harness, or sensible defaults."""
        saved = self._harness_configs.get(harness)
        if saved is not None:
            scout = (
                saved.scout_model
                if saved.scout_model is not None
                else self._get_default_model(harness, "scout")
            )
            arch = (
                saved.architect_model
                if saved.architect_model is not None
                else self._get_default_model(harness, "architect")
            )
            rev = (
                saved.reviewer_model
                if saved.reviewer_model is not None
                else self._get_default_model(harness, "reviewer")
            )
            workers = (
                list(saved.worker_models)
                if saved.worker_models
                else [self._get_default_model(harness, "coder")]
            )
            scout_eff = saved.scout_effort or "none"
            arch_eff = saved.architect_effort or "none"
            rev_eff = saved.reviewer_effort or "none"
        else:
            scout = self._get_default_model(harness, "scout")
            arch = self._get_default_model(harness, "architect")
            rev = self._get_default_model(harness, "reviewer")
            workers = [self._get_default_model(harness, "coder")]
            scout_eff = "none"
            arch_eff = "none"
            rev_eff = "none"

        with _ensure_active_app():
            self._syncing = True
            try:
                with self.prevent(Input.Changed):
                    self._scout_input.value = scout
                    self._architect_input.value = arch
                    self._reviewer_input.value = rev
                with self.prevent(Select.Changed):
                    self._effort_selects["scout"].value = scout_eff
                    self._effort_selects["architect"].value = arch_eff
                    self._effort_selects["reviewer"].value = rev_eff
                self._update_worker_rows(workers)
                self._save_current_to_harness(harness)
            finally:
                self._syncing = False

    def on_select_changed(self, event: Select.Changed) -> None:
        if self._syncing or not self.is_mounted:
            return
        if event.select == self._harness_select:
            new_harness = str(event.value)
            if new_harness != self._current_harness:
                self._save_current_to_harness(self._current_harness)
                self._current_harness = new_harness
                self._load_harness_into_ui(new_harness)
            self._notify_changed()
        elif event.select in self._effort_selects.values():
            self._save_current_to_harness(self._current_harness)
            self._notify_changed()

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._syncing or not self.is_mounted:
            return
        self._save_current_to_harness(self._current_harness)
        self._notify_changed()

    def _notify_changed(self) -> None:
        try:
            cfg = self.get_config()
            self.post_message(self.Changed(cfg))
        except Exception:
            pass

    def reload_for_project(self, project_path: Optional[Path]) -> None:
        """Reload project overrides when project folder changes."""
        self.project_path = project_path
        if project_path:
            try:
                cfg = load_project_config(project_path)
                self.reload_config(cfg.orchestration, project_path=project_path)
                return
            except Exception:
                pass
        harness = str(self._harness_select.value)
        self._load_harness_into_ui(harness)
        self._notify_changed()

    def reload_config(
        self,
        config: OrchestrationConfig,
        *,
        project_path: Optional[Path] = None,
    ) -> None:
        """Reload orchestration config and update inputs."""
        self.project_path = project_path
        self._config = config.model_copy(deep=True)
        self._default_harness = config.default_harness or config.harness
        self._harness_configs.clear()
        for k, v in self._config.harnesses.items():
            self._harness_configs[k] = v.model_copy(deep=True)

        active_key = self._config.harness.value
        if active_key not in self._harness_configs:
            self._harness_configs[active_key] = HarnessOrchestrationSettings(
                scout_model=self._config.scout_model,
                architect_model=self._config.architect_model,
                worker_models=list(self._config.worker_models),
                reviewer_model=self._config.reviewer_model,
                scout_effort=self._config.scout_effort,
                architect_effort=self._config.architect_effort,
                reviewer_effort=self._config.reviewer_effort,
            )

        self._current_harness = active_key
        with _ensure_active_app():
            self._syncing = True
            try:
                self._harness_select.value = active_key
                self._load_harness_into_ui(active_key)
                self.set_status("")
            finally:
                self._syncing = False
        self._notify_changed()

    def _effort_value(self, role: str) -> Optional[str]:
        val = str(self._effort_selects[role].value)
        return None if val in ("none", "", "Select.BLANK") else val

    def get_config(self) -> OrchestrationConfig:
        """Construct an OrchestrationConfig snapshot from editor fields."""
        harness_val = str(self._harness_select.value)
        try:
            harness_kind = HarnessKind(harness_val)
        except ValueError:
            harness_kind = HarnessKind.AGY

        self._save_current_to_harness(harness_val)

        worker_models = [m for m in self._worker_values() if m]
        harnesses_dump = {
            k: v.model_copy(deep=True) for k, v in self._harness_configs.items()
        }

        default_harness = self._default_harness or harness_kind

        fields: Dict[str, Any] = dict(
            harness=harness_kind,
            default_harness=default_harness,
            scout_model=self._scout_input.value.strip() or None,
            architect_model=self._architect_input.value.strip() or None,
            worker_models=worker_models,
            reviewer_model=self._reviewer_input.value.strip() or None,
            scout_effort=self._effort_value("scout"),
            architect_effort=self._effort_value("architect"),
            reviewer_effort=self._effort_value("reviewer"),
            harnesses=harnesses_dump,
        )
        try:
            return OrchestrationConfig(**fields)
        except Exception:
            return OrchestrationConfig.model_construct(**fields)

    def validate(self) -> Optional[str]:
        """Validate all fields and native compatibility. Return actionable error or None."""
        harness_val = str(self._harness_select.value)
        try:
            kind = HarnessKind(harness_val)
        except ValueError:
            return f"Unsupported harness: {harness_val}"

        spec = get_harness(kind.value)
        if not spec.supports_native:
            return f"Harness '{kind.value}' does not support native orchestration."

        scout_m = self._scout_input.value.strip()
        arch_m = self._architect_input.value.strip()
        rev_m = self._reviewer_input.value.strip()
        worker_models = [m for m in self._worker_values() if m]

        if not scout_m or not arch_m or not worker_models or not rev_m:
            return "All models (Scout, Architect, Worker, Reviewer) must be specified."
        if any(not m for m in self._worker_values()):
            return "Worker models must not be blank (fill in or remove the empty row)."

        # Check workers for prohibited effort levels
        for m in worker_models:
            if ":" in m and m.split(":")[-1].lower() in _EFFORT_SUFFIXES:
                return (
                    f"Worker model '{m}' must not specify an effort level "
                    "(the Architect decides worker effort)."
                )

        # Check native binding errors
        for role, model in (
            ("scout", scout_m),
            ("architect", arch_m),
            ("reviewer", rev_m),
        ):
            err = native_binding_error(
                kind.value, model, role=role, native_harness=kind.value
            )
            if err:
                return err

        for worker in worker_models:
            err = native_binding_error(
                kind.value, worker, role="coder", native_harness=kind.value
            )
            if err:
                return err

        return None

    def is_valid(self) -> bool:
        """Return True if orchestration configuration is currently valid."""
        return self.validate() is None
