"""Reusable SquadEditor widget for Ergo pipeline squad topology and role editing."""

from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple, Union

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Input, Label, Select, Static, Tree

from herdr_master.core.catalog import CatalogError, worker_route_note
from herdr_master.core.models import EffortLevel, HarnessBinding, HarnessKind, Squad
from herdr_master.pipeline.squad import validate_squad
from aiheaddev.ui.tui.state import (
    binding_launch_preview,
    catalog_worker_template,
    effort_select_options,
    harness_select_options,
    normalize_worker_order,
    scout_summary,
    squad_source_line,
    validate_binding_for_editor,
)


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


class SquadEditor(Widget):
    """Reusable Textual widget for editing Ergo multi-harness Squad topologies.

    Displays a 2-column interface:
    - Left: Interactive Tree showing Root, Scout, Architect, ordered Worker Pool, Reviewer.
    - Right: Role/Worker configuration editor, effort controls, and worker pool management (+/-/↑/↓).

    Workers deliberately omit effort (the Architect decides worker effort per task).
    Deep-copies input squad on initialization and exposes public snapshot/validation methods.
    """

    DEFAULT_CSS = """
    SquadEditor {
        height: auto;
        layout: vertical;
    }
    SquadEditor.top-section {
        height: 12;
        border: solid #334155;
        layout: horizontal;
    }
    .squad-summary-bar {
        height: auto;
        min-height: 1;
        margin-bottom: 0;
    }
    .squad-editor-layout {
        height: 100%;
        min-height: 10;
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
    SquadEditor.narrow .left-tree-col {
        width: 22;
    }
    SquadEditor.stacked-options .left-tree-col {
        width: 18;
    }
    SquadEditor.stacked-options .options-grid {
        layout: vertical;
    }
    SquadEditor.stacked-options .option-column {
        width: 100%;
        margin-left: 0;
    }
    """

    class Changed(Message):
        """Posted when squad configuration is edited."""

        def __init__(self, squad: Squad) -> None:
            super().__init__()
            self.squad = squad

    def __init__(
        self,
        squad: Squad,
        *,
        sources: Optional[Dict[str, str]] = None,
        mode: str = "herdr",
        native_harness: Optional[str] = None,
        config_error: Optional[str] = None,
        include_summary: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.squad = squad.model_copy(deep=True)
        self.sources = dict(sources or {})
        self.mode = mode
        self.native_harness = native_harness
        self.config_error = config_error
        self._include_summary = include_summary
        self.status_text = ""

        self._selected_target: Union[str, Tuple[str, int]] = "scout"
        self._is_updating_fields = False
        self._rebuilding_tree = False

        scout = self.squad.scout
        effort_value = scout.effort.value if scout.effort else "none"
        effort_options = effort_select_options(scout.kind.value, scout.model, mode=mode)
        if effort_value not in {item[1] for item in effort_options}:
            effort_value = "none"
        template = catalog_worker_template(0)

        with _ensure_active_app():
            self._summary = Static("", id="squad-summary", classes="modal-subtitle squad-summary-bar")
            self._tree = Tree("👑 Herdr-Master Root", id="squad-tree", classes="squad-tree")
            self._editor_heading = Label("Scout Configuration", classes="editor-heading")
            self._harness_select = Select(
                harness_select_options(mode=mode),
                value=scout.kind.value,
                allow_blank=False,
                id="squad-harness-select",
            )
            self._model_input = Input(
                value=scout.model,
                placeholder="Explicit model. Blank input is rejected.",
                id="squad-model-input",
            )
            self._effort_label = Label("Reasoning Effort:", classes="field-label")
            self._effort_select = Select(
                effort_options,
                value=effort_value,
                allow_blank=False,
                id="squad-effort-select",
            )
            self._effort_note = Label("", classes="field-label", id="squad-effort-note")
            self._role_label_header = Label("Role / Worker Name:", classes="field-label")
            self._label_input = Input(
                value=scout.label or "",
                placeholder="Optional worker label",
                id="squad-label-input",
            )
            self._desc_label = Label("Capability Info (for Architect):", classes="field-label")
            self._description_input = Input(
                value="",
                placeholder=template.description or "Capability note for the Architect",
                id="squad-description-input",
            )
            self._status_label = Static("", id="squad-status", classes="status-msg")

    @property
    def summary_widget(self) -> Static:
        return self._summary

    def compose(self) -> ComposeResult:
        if self._include_summary:
            yield self._summary
            with Horizontal(classes="top-section squad-editor-layout"):
                with Vertical(classes="left-tree-col"):
                    yield Label("Squad Topology:", classes="col-header")
                    yield self._tree

                with Vertical(classes="right-editor-col"):
                    with VerticalScroll(classes="editor-fields"):
                        yield self._editor_heading

                        with Horizontal(classes="options-grid"):
                            with Vertical(classes="option-column"):
                                yield Label("Harness Kind:", classes="field-label")
                                yield self._harness_select
                                yield self._effort_label
                                yield self._effort_select
                            with Vertical(classes="option-column option-column-secondary"):
                                yield Label("Model:", classes="field-label")
                                yield self._model_input
                                yield self._role_label_header
                                yield self._label_input
                        yield self._effort_note

                        yield self._desc_label
                        yield self._description_input

                    with Horizontal(id="worker-pool-bar", classes="worker-pool-buttons"):
                        yield Button("+ Add", id="btn-add-worker", variant="success")
                        yield Button("− Remove", id="btn-remove-worker", variant="error")
                        yield Button("↑ Up", id="btn-up-worker")
                        yield Button("↓ Down", id="btn-down-worker")

                    yield self._status_label
        else:
            with Vertical(classes="left-tree-col"):
                yield Label("Squad Topology:", classes="col-header")
                yield self._tree

            with Vertical(classes="right-editor-col"):
                with VerticalScroll(classes="editor-fields"):
                    yield self._editor_heading

                    with Horizontal(classes="options-grid"):
                        with Vertical(classes="option-column"):
                            yield Label("Harness Kind:", classes="field-label")
                            yield self._harness_select
                            yield self._effort_label
                            yield self._effort_select
                        with Vertical(classes="option-column option-column-secondary"):
                            yield Label("Model:", classes="field-label")
                            yield self._model_input
                            yield self._role_label_header
                            yield self._label_input
                    yield self._effort_note

                    yield self._desc_label
                    yield self._description_input

                with Horizontal(id="worker-pool-bar", classes="worker-pool-buttons"):
                    yield Button("+ Add", id="btn-add-worker", variant="success")
                    yield Button("− Remove", id="btn-remove-worker", variant="error")
                    yield Button("↑ Up", id="btn-up-worker")
                    yield Button("↓ Down", id="btn-down-worker")

                yield self._status_label

    def on_mount(self) -> None:
        self._rebuild_tree()
        self.load_agent_into_editor("scout")
        self._refresh_summary()

    def set_status(self, text: str) -> None:
        """Update status line."""
        self.status_text = text
        self._status_label.update(text)

    def _editor_locked(self) -> bool:
        return self._selected_target in ("root", "pool_overview")

    def _set_editor_enabled(self, enabled: bool) -> None:
        self._harness_select.disabled = not enabled
        self._model_input.disabled = not enabled
        self._effort_select.disabled = not enabled
        self._label_input.disabled = not enabled
        self._description_input.disabled = not enabled

    def _refresh_summary(self) -> None:
        if self.config_error:
            self._summary.update(self.config_error)
        else:
            self._summary.update(
                f"{squad_source_line(self.sources, self.mode)} · {scout_summary(self.squad)}"
            )

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

    def _rebuild_tree(self) -> None:
        """Reconstruct the interactive squad tree on the left."""
        self._rebuilding_tree = True
        try:
            self._tree.clear()
            self._tree.root.label = "👑 [bold cyan]Herdr-Master Root[/bold cyan]"
            self._tree.root.data = {"type": "root"}

            scout_sub = f"{self.squad.scout.kind.value}:{self.squad.scout.model}"
            self._tree.root.add_leaf(
                f"● [bold]Scout[/bold] [dim]({scout_sub})[/dim]",
                data={"type": "role", "role": "scout"},
            )

            arch_sub = f"{self.squad.architect.kind.value}:{self.squad.architect.model}"
            self._tree.root.add_leaf(
                f"● [bold]Architect[/bold] [dim]({arch_sub})[/dim]",
                data={"type": "role", "role": "architect"},
            )

            pool_node = self._tree.root.add(
                f"📦 [bold]Worker Pool[/bold] [dim]({len(self.squad.worker_pool)} coders)[/dim]",
                data={"type": "pool"},
            )
            pool_node.expand()

            for idx, worker in enumerate(self.squad.worker_pool):
                w_sub = f"{worker.kind.value}:{worker.model}"
                w_name = worker.label or f"worker-{idx + 1}"
                state = "" if worker.enabled else " [red]disabled[/red]"
                desc_text = (
                    f" • [italic dim]{worker.description[:26]}...[/italic dim]"
                    if worker.description
                    else ""
                )
                pool_node.add_leaf(
                    f"↳ [bold]{w_name}[/bold] [dim]({w_sub})[/dim]{state}{desc_text}",
                    data={"type": "worker", "index": idx},
                )

            rev_sub = f"{self.squad.reviewer.kind.value}:{self.squad.reviewer.model}"
            self._tree.root.add_leaf(
                f"● [bold]Reviewer[/bold] [dim]({rev_sub})[/dim]",
                data={"type": "role", "role": "reviewer"},
            )
            self._tree.root.expand()
        finally:
            self._rebuilding_tree = False

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if self._rebuilding_tree or self._is_updating_fields:
            return
        data = event.node.data or {}
        node_type = data.get("type", "root")
        if node_type == "role":
            self.load_agent_into_editor(data.get("role", "scout"))
        elif node_type == "worker":
            self.load_agent_into_editor(("worker", data.get("index", 0)))
        elif node_type == "pool":
            self.load_agent_into_editor("pool_overview")
        else:
            self.load_agent_into_editor("root")

    def load_agent_into_editor(self, target: Union[str, Tuple[str, int]]) -> None:
        """Populate the editor for selected target role or worker."""
        self._selected_target = target
        self._is_updating_fields = True
        try:
            pool_bar = self.query_one("#worker-pool-bar", Horizontal)
        except Exception:
            pool_bar = None
        try:
            with self.prevent(Select.Changed), self.prevent(Input.Changed):
                if target in ("root", "pool_overview"):
                    self._set_editor_enabled(False)
                    self._effort_label.display = False
                    self._effort_select.display = False
                    self._effort_note.display = True
                    self._desc_label.display = False
                    self._description_input.display = False
                    if pool_bar:
                        pool_bar.display = target == "pool_overview"
                    if target == "root":
                        self._editor_heading.update("👑 Herdr-Master Squad Overview (read-only)")
                        self._effort_note.update(
                            "Root overview does not edit Scout. Select a role to change it."
                        )
                    else:
                        self._editor_heading.update("📦 Worker Pool Overview (read-only)")
                        self._effort_note.update(
                            "Pool overview does not edit Scout or a worker. Select a worker."
                        )
                    return

                self._set_editor_enabled(True)
                if target in ("scout", "architect", "reviewer"):
                    self._effort_label.display = True
                    self._effort_select.display = True
                    self._effort_note.display = False
                    self._desc_label.display = False
                    self._description_input.display = False
                    self._role_label_header.update("Role Identifier:")
                    if pool_bar:
                        pool_bar.display = False
                    binding = getattr(self.squad, target)
                    titles = {
                        "scout": "Scout Role Configuration",
                        "architect": "Architect Role Configuration",
                        "reviewer": "Reviewer Role Configuration",
                    }
                    self._editor_heading.update(titles[target])
                    self._populate_fields(binding)
                    return

                self._effort_label.display = False
                self._effort_select.display = False
                self._effort_note.display = True
                self._desc_label.display = True
                self._description_input.display = True
                self._role_label_header.update("Worker Identifier / Name:")
                if pool_bar:
                    pool_bar.display = True
                if isinstance(target, tuple) and target[0] == "worker":
                    idx = target[1]
                    if 0 <= idx < len(self.squad.worker_pool):
                        worker = self.squad.worker_pool[idx]
                        w_name = worker.label or f"worker-{idx + 1}"
                        self._editor_heading.update(f"Worker Pool: {w_name} (Position #{idx + 1})")
                        self._populate_fields(worker)
                        self._refresh_worker_note(worker)
        finally:
            self._is_updating_fields = False
            self._refresh_summary()

    def _populate_fields(self, binding: HarnessBinding) -> None:
        self._harness_select.value = binding.kind.value
        self._model_input.value = binding.model
        current = binding.effort.value if binding.effort else "none"
        self._set_effort_options(binding.kind.value, binding.model, current)
        self._label_input.value = binding.label or ""
        self._description_input.value = binding.description or ""

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._is_updating_fields or self._editor_locked() or not self.is_mounted:
            return
        self._apply_current_editor_changes()

    def on_select_changed(self, event: Select.Changed) -> None:
        if self._is_updating_fields or self._editor_locked() or not self.is_mounted:
            return
        self._apply_current_editor_changes()

    def _parse_editor(self) -> Tuple[Optional[HarnessBinding], Optional[str]]:
        if self._editor_locked():
            return None, None
        model = self._model_input.value.strip()
        if not model:
            return None, "Model is empty. Refusing to invent a model."
        try:
            kind = HarnessKind(str(self._harness_select.value))
        except ValueError:
            return None, "Unknown harness. Refusing to substitute agy."
        label = self._label_input.value.strip() or None
        description = self._description_input.value.strip() or None
        if isinstance(self._selected_target, tuple):
            idx = self._selected_target[1]
            existing = (
                self.squad.worker_pool[idx] if 0 <= idx < len(self.squad.worker_pool) else None
            )
            binding = HarnessBinding(
                kind=kind,
                model=model,
                effort=None,
                label=label,
                description=description,
                enabled=existing.enabled if existing else True,
                order=existing.order if existing else idx,
            )
            return binding, None
        effort_text = str(self._effort_select.value)
        effort = None if effort_text == "none" else EffortLevel(effort_text)
        existing_role = getattr(self.squad, str(self._selected_target))
        binding = HarnessBinding(
            kind=kind,
            model=model,
            effort=effort,
            label=label,
            description=existing_role.description,
            enabled=existing_role.enabled,
            order=existing_role.order,
        )
        return binding, None

    def _apply_current_editor_changes(self) -> Optional[str]:
        """Write the editor values into the deep-copied squad."""
        if self._editor_locked():
            return None
        binding, error = self._parse_editor()
        if error or binding is None:
            if error:
                self.set_status(f"[red]{error}[/red]")
            return error
        target = self._selected_target
        role = "coder" if isinstance(target, tuple) else str(target)
        catalog_error = validate_binding_for_editor(
            binding,
            role=role,
            mode=self.mode,
            native_harness=self.native_harness,
        )
        if catalog_error:
            self.set_status(f"[red]{catalog_error}[/red]")
            return catalog_error
        if target == "scout":
            self.squad.scout = binding
        elif target == "architect":
            self.squad.architect = binding
        elif target == "reviewer":
            self.squad.reviewer = binding
        elif isinstance(target, tuple) and target[0] == "worker":
            idx = target[1]
            if 0 <= idx < len(self.squad.worker_pool):
                self.squad.worker_pool[idx] = binding
                self._refresh_worker_note(binding)
        self._rebuild_tree()
        self._refresh_summary()
        self.post_message(self.Changed(self.get_squad()))
        return None

    def _normalize_after_pool_edit(self) -> None:
        normalize_worker_order(self.squad)
        self._rebuild_tree()
        self.post_message(self.Changed(self.get_squad()))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id

        if btn_id == "btn-add-worker":
            idx = len(self.squad.worker_pool)
            self.squad.worker_pool.append(catalog_worker_template(idx))
            self._normalize_after_pool_edit()
            self.load_agent_into_editor(("worker", idx))
            self.set_status(f"[green]Added worker #{idx + 1} from the catalog template[/green]")

        elif btn_id == "btn-remove-worker":
            if isinstance(self._selected_target, tuple) and self._selected_target[0] == "worker":
                idx = self._selected_target[1]
                if 0 <= idx < len(self.squad.worker_pool):
                    if len(self.squad.worker_pool) == 1:
                        self.set_status("[red]The worker pool cannot be empty.[/red]")
                        return
                    self.squad.worker_pool.pop(idx)
                    self._normalize_after_pool_edit()
                    new_idx = max(0, min(idx, len(self.squad.worker_pool) - 1))
                    self.load_agent_into_editor(("worker", new_idx))
                    self.set_status("[yellow]Removed worker from pool[/yellow]")

        elif btn_id == "btn-up-worker":
            if isinstance(self._selected_target, tuple) and self._selected_target[0] == "worker":
                idx = self._selected_target[1]
                if idx > 0:
                    pool = self.squad.worker_pool
                    pool[idx - 1], pool[idx] = pool[idx], pool[idx - 1]
                    self._normalize_after_pool_edit()
                    self.load_agent_into_editor(("worker", idx - 1))

        elif btn_id == "btn-down-worker":
            if isinstance(self._selected_target, tuple) and self._selected_target[0] == "worker":
                idx = self._selected_target[1]
                if idx < len(self.squad.worker_pool) - 1:
                    pool = self.squad.worker_pool
                    pool[idx + 1], pool[idx] = pool[idx], pool[idx + 1]
                    self._normalize_after_pool_edit()
                    self.load_agent_into_editor(("worker", idx + 1))

    def get_squad(self) -> Squad:
        """Return a validated, deep-copied snapshot of the current squad."""
        normalize_worker_order(self.squad)
        return self.squad.model_copy(deep=True)

    def validate(self) -> Optional[str]:
        """Validate the full squad configuration and return an actionable error string or None."""
        if self.config_error:
            return self.config_error
        if not self._editor_locked() and not self._model_input.value.strip():
            return "Model is empty. Refusing to invent a model."
        error = self._apply_current_editor_changes()
        if error:
            return error
        normalize_worker_order(self.squad)
        errors = validate_squad(self.squad)
        if errors:
            return errors[0]
        for role in ("scout", "architect", "reviewer"):
            message = validate_binding_for_editor(
                getattr(self.squad, role),
                role=role,
                mode=self.mode,
                native_harness=self.native_harness,
            )
            if message:
                return message
        for worker in self.squad.worker_pool:
            message = validate_binding_for_editor(
                worker,
                role="coder",
                mode=self.mode,
                native_harness=self.native_harness,
            )
            if message:
                return message
        return None

    def is_valid(self) -> bool:
        """Return True if squad is currently valid."""
        return self.validate() is None

    def reload_squad(
        self,
        squad: Squad,
        *,
        sources: Optional[Dict[str, str]] = None,
        config_error: Optional[str] = None,
    ) -> None:
        """Reload squad defaults and refresh views."""
        self.squad = squad.model_copy(deep=True)
        if sources is not None:
            self.sources = dict(sources)
        self.config_error = config_error
        self._rebuild_tree()
        self.load_agent_into_editor("scout")
        self._refresh_summary()
        self.post_message(self.Changed(self.get_squad()))

    def adapt_layout(self, width: int, height: int) -> None:
        """Adapt responsive layout classes based on terminal size."""
        self.set_class(width < 100, "narrow")
        self.set_class(width < 72, "stacked-options")
        self.set_class(height < 32, "short")
