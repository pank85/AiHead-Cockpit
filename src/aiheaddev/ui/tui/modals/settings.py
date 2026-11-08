"""Five-tab Settings Modal for Project defaults, Squads, Council, and AiHead."""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TabPane,
    Tabs,
    TextArea,
)

from herdr_master.core.config import (
    DEFAULT_AI_HEAD_SOUL,
    AiHeadConfig,
    CouncilConfig,
    CouncilMember,
    FavoriteProject,
    HerdrMasterConfig,
    find_project_config_path,
    is_folder_available,
    load_project_config,
    resolve_folder_path,
    save_project_config,
    squad_to_project_config,
)
from herdr_master.core.models import EffortLevel, HarnessBinding, HarnessKind, Squad
from herdr_master.pipeline.squad import resolve_squad
from aiheaddev.ui.tui.modals.project_folder import ProjectFolderPicker
from aiheaddev.ui.tui.state import harness_select_options
from aiheaddev.ui.tui.widgets.orchestration_editor import OrchestrationEditor
from aiheaddev.ui.tui.widgets.squad_editor import SquadEditor

COUNCIL_EFFORT_OPTIONS = [
    ("Unset", "none"),
    ("Low", "low"),
    ("Medium", "medium"),
    ("High", "high"),
    ("Max", "max"),
]


class SettingsModal(ModalScreen[Optional[Tuple[HerdrMasterConfig, Squad]]]):
    """Native Textual 5-tab Settings configuration modal.

    Tabs in exact order:
    1. Projects & Folder Defaults (#tab-projects)
    2. Ergo Squad (#tab-ergo)
    3. Orchestration Squad (#tab-orchestration)
    4. AI Council Settings (#tab-council)
    5. AiHead Persona Settings (#tab-aihead)
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    SettingsModal {
        align: center middle;
        background: rgba(11, 15, 25, 0.85);
    }
    .settings-shell {
        width: 96%;
        height: 100%;
        max-height: 100%;
        background: #161E2E;
        border: thick #38BDF8;
        padding: 0 1;
        color: #F8FAFC;
    }
    .settings-title {
        height: 1;
        color: #38BDF8;
        text-style: bold;
    }
    #settings-destination {
        height: 2;
        min-height: 2;
        max-height: 2;
        border: none;
        padding: 0;
        background: #111827;
        color: #94A3B8;
    }
    #settings-destination:focus {
        border: none;
        background: #1E293B;
    }
    SettingsModal TabbedContent {
        height: 1fr;
    }
    SettingsModal TabbedContent > ContentSwitcher {
        height: 1fr;
    }
    .settings-tab-scroll {
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
        width: 14;
        padding-top: 1;
        color: #94A3B8;
    }
    #aihead-effort-label {
        width: 18;
    }
    .field-row Input {
        width: 1fr;
    }
    .field-row Select {
        width: 24;
    }
    .field-row Button {
        margin-left: 1;
        min-width: 10;
        background: #334155;
        color: #F8FAFC;
    }
    .field-row Button:hover {
        background: #475569;
    }
    .table-container {
        height: 7;
        margin-bottom: 1;
        background: #111827;
        border: solid #334155;
    }
    .action-row {
        height: 3;
        margin-bottom: 1;
    }
    .action-row Button {
        margin-right: 1;
        min-width: 12;
        background: #334155;
        color: #F8FAFC;
    }
    .action-row Button:hover {
        background: #475569;
    }
    .warning-text {
        color: #F59E0B;
        text-style: bold;
        height: 2;
    }
    .status-bar {
        height: 1;
        color: #F59E0B;
    }
    .footer-row {
        height: 1;
        max-height: 1;
        margin-top: 0;
    }
    .footer-row Button {
        margin-right: 2;
        min-width: 14;
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
    .compact-tab {
        padding: 0;
    }
    .compact-row {
        height: 1;
        margin-top: 0;
    }
    .compact-label {
        width: 13;
        color: #94A3B8;
    }
    .compact-harness {
        width: 14;
    }
    .compact-model {
        width: 1fr;
        margin-left: 1;
    }
    .compact-effort {
        width: 11;
        margin-left: 1;
    }
    .compact-label-input {
        width: 20;
    }
    .compact-row Button {
        margin-right: 1;
        min-width: 8;
        background: #334155;
        color: #F8FAFC;
    }
    .compact-row Button:hover {
        background: #475569;
    }
    .compact-row Checkbox {
        margin-left: 1;
        width: auto;
    }
    .compact-section {
        color: #94A3B8;
        text-style: bold;
        margin-top: 1;
        height: 1;
    }
    .compact-warning {
        color: #F59E0B;
        height: 1;
    }
    .council-table-container {
        height: 7;
        background: #111827;
        border: solid #334155;
    }
    #aihead-soul-input {
        height: 6;
        margin-bottom: 1;
        background: #1E293B;
        border: solid #334155;
        color: #F8FAFC;
    }
    #aihead-soul-input:focus {
        border: solid #38BDF8;
    }
    """

    def __init__(
        self,
        config: Optional[HerdrMasterConfig] = None,
        squad: Optional[Squad] = None,
        project_path: Optional[Path] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.project_path = project_path
        self.project_config_path = find_project_config_path(project_path) or (
            (project_path or Path.cwd()).resolve() / ".herdr-master.toml"
        )

        # Deep copy config to prevent mutation on cancellation
        base_cfg = config or load_project_config(self.project_config_path)
        self.config = base_cfg.model_copy(deep=True)

        # Deep copy squad
        base_squad = squad or resolve_squad(self.project_path).squad
        self.squad = base_squad.model_copy(deep=True)

        # Local state for Tab 1 (Favorites)
        self._favorites: List[FavoriteProject] = [
            fav.model_copy(deep=True) for fav in self.config.projects.favorites
        ]
        self._selected_favorite_idx: Optional[int] = None

        # Tab 2 widget: SquadEditor
        self.squad_editor = SquadEditor(self.squad, include_summary=True)

        # Tab 3 widget: OrchestrationEditor
        self.orchestration_editor = OrchestrationEditor(
            config=self.config.orchestration
        )

        # Local state for Tab 4 (Council)
        self._council_config = self.config.council.model_copy(deep=True)
        self._council_members: List[CouncilMember] = [
            m.model_copy(deep=True) for m in self._council_config.members
        ]
        self._selected_member_idx: Optional[int] = None

        # Local state for Tab 5 (AiHead)
        self._aihead_config = self.config.ai_head.model_copy(deep=True)

    def _compute_scope(self) -> str:
        resolved_proj = (self.project_path or Path.cwd()).resolve()
        if resolved_proj.is_file():
            resolved_proj = resolved_proj.parent
        config_parent = self.project_config_path.parent.resolve()
        if not self.project_config_path.exists():
            return "new-local"
        if config_parent == resolved_proj:
            return "local"
        return "inherited"

    def _format_destination_text(self) -> str:
        scope = self._compute_scope()
        return f"Scope: {scope}\nDestination: {self.project_config_path}"

    def compose(self) -> ComposeResult:
        with Vertical(classes="settings-shell"):
            yield Label("⚙ Herdr-Master Settings", classes="settings-title")
            yield TextArea(
                self._format_destination_text(),
                read_only=True,
                show_line_numbers=False,
                soft_wrap=False,
                id="settings-destination",
            )
            with TabbedContent():
                # Tab 1: Projects & Folder Defaults
                with TabPane("Projects & Folders", id="tab-projects"):
                    with VerticalScroll(classes="settings-tab-scroll"):
                        yield Label("Default Folder:", classes="section-label")
                        with Horizontal(classes="field-row"):
                            yield Input(
                                value=self.config.projects.default_folder or "",
                                placeholder="/path/to/default/workspace",
                                id="default-folder-input",
                            )
                            yield Button("Browse…", id="btn-browse-default-folder")

                        yield Label(
                            "Favorite Projects / Workspaces:", classes="section-label"
                        )
                        with Vertical(classes="table-container"):
                            yield DataTable(id="favorites-table", cursor_type="row")

                        yield Label("Favorite Details:", classes="section-label")
                        with Horizontal(classes="field-row"):
                            yield Label("Name:")
                            yield Input(
                                placeholder="Favorite name (e.g. backend)",
                                id="favorite-name-input",
                            )
                        with Horizontal(classes="field-row"):
                            yield Label("Path:")
                            yield Input(
                                placeholder="Absolute or ~/ path",
                                id="favorite-path-input",
                            )
                            yield Button("Browse…", id="btn-browse-favorite-path")

                        with Horizontal(classes="action-row"):
                            yield Button(
                                "Add Favorite", id="btn-add-favorite", variant="success"
                            )
                            yield Button(
                                "Update", id="btn-edit-favorite", variant="warning"
                            )
                            yield Button(
                                "Remove", id="btn-remove-favorite", variant="error"
                            )
                            yield Button("Test Path", id="btn-test-favorite")

                        yield Static("", id="favorite-test-status")

                # Tab 2: Ergo Squad
                with TabPane("Ergo Squad", id="tab-ergo"):
                    yield self.squad_editor

                # Tab 3: Orchestration Squad
                with TabPane("Orchestration Squad", id="tab-orchestration"):
                    yield self.orchestration_editor

                # Tab 4: AI Council Settings
                with TabPane("AI Council", id="tab-council"):
                    with VerticalScroll(classes="settings-tab-scroll compact-tab"):
                        for role, binding in (
                            ("coordinator", self._council_config.coordinator),
                            ("scout", self._council_config.scout),
                        ):
                            with Horizontal(classes="compact-row"):
                                yield Label(
                                    f"{role.capitalize()}:", classes="compact-label"
                                )
                                yield Select(
                                    harness_select_options(),
                                    value=binding.kind.value,
                                    compact=True,
                                    classes="compact-harness",
                                    id=f"council-{role}-harness",
                                )
                                yield Input(
                                    value=binding.model,
                                    compact=True,
                                    classes="compact-model",
                                    id=f"council-{role}-model",
                                )
                                yield Select(
                                    COUNCIL_EFFORT_OPTIONS,
                                    value=(
                                        binding.effort.value
                                        if binding.effort
                                        else "none"
                                    ),
                                    allow_blank=False,
                                    compact=True,
                                    classes="compact-effort",
                                    id=f"council-{role}-effort",
                                )

                        yield Label(
                            "Council Members (Deliberators):", classes="compact-section"
                        )
                        yield Static(
                            "", id="council-warning", classes="compact-warning"
                        )
                        with Vertical(classes="council-table-container"):
                            yield DataTable(
                                id="council-members-table", cursor_type="row"
                            )

                        with Horizontal(classes="compact-row"):
                            yield Input(
                                placeholder="Label (e.g. council-1)",
                                compact=True,
                                classes="compact-label-input",
                                id="council-member-label-input",
                            )
                            yield Select(
                                harness_select_options(),
                                value="agy",
                                compact=True,
                                classes="compact-harness",
                                id="council-member-harness-select",
                            )
                            yield Input(
                                placeholder="Model name",
                                compact=True,
                                classes="compact-model",
                                id="council-member-model-input",
                            )
                            yield Checkbox(
                                "Enabled",
                                value=True,
                                compact=True,
                                id="council-member-enabled-checkbox",
                            )
                        with Horizontal(classes="compact-row"):
                            yield Button(
                                "Add",
                                id="btn-add-member",
                                variant="success",
                                compact=True,
                            )
                            yield Button(
                                "Update",
                                id="btn-edit-member",
                                variant="warning",
                                compact=True,
                            )
                            yield Button(
                                "Remove",
                                id="btn-remove-member",
                                variant="error",
                                compact=True,
                            )
                            yield Button("Up", id="btn-member-up", compact=True)
                            yield Button("Down", id="btn-member-down", compact=True)

                # Tab 5: AiHead Persona Settings
                with TabPane("AiHead Persona", id="tab-aihead"):
                    with VerticalScroll(classes="settings-tab-scroll"):
                        yield Checkbox(
                            "Enable AiHead Autonomous Decision-Maker",
                            value=self._aihead_config.enabled,
                            id="aihead-enabled",
                        )
                        yield Label(
                            "Persona Soul / System Prompt:", classes="section-label"
                        )
                        yield TextArea(
                            self._aihead_config.soul or DEFAULT_AI_HEAD_SOUL,
                            soft_wrap=True,
                            id="aihead-soul-input",
                        )
                        with Horizontal(classes="field-row"):
                            yield Label("Harness:")
                            yield Select(
                                harness_select_options(),
                                value=self._aihead_config.binding.kind.value,
                                id="aihead-harness",
                            )
                            yield Label("Model:")
                            yield Input(
                                value=self._aihead_config.binding.model,
                                id="aihead-model",
                            )
                        with Horizontal(classes="field-row"):
                            yield Label("Effort / thinking", id="aihead-effort-label")
                            yield Select(
                                COUNCIL_EFFORT_OPTIONS,
                                value=(
                                    self._aihead_config.binding.effort.value
                                    if self._aihead_config.binding and self._aihead_config.binding.effort
                                    else "none"
                                ),
                                allow_blank=False,
                                id="aihead-effort",
                            )
                        yield Checkbox(
                            "Dangerously skip permissions",
                            value=getattr(
                                self._aihead_config, "dangerously_skip_permissions", True
                            ),
                            id="aihead-dangerously-skip-permissions",
                        )
                        yield Label(
                            "Working Folder / Directory:", classes="section-label"
                        )
                        with Horizontal(classes="field-row"):
                            yield Input(
                                value=self._aihead_config.cwd or "",
                                placeholder="Folder path or leave blank for repo root",
                                id="aihead-cwd-input",
                            )
                            yield Button("Browse…", id="btn-browse-aihead-cwd")

            yield Static("", id="settings-status", classes="status-bar")
            with Horizontal(classes="footer-row"):
                yield Button(
                    "Save Settings", id="btn-save", variant="primary", compact=True
                )
                yield Button("Cancel", id="btn-cancel", compact=True)

    def on_mount(self) -> None:
        self._setup_favorites_table()
        self._setup_council_table()
        self._refresh_council_warning()
        try:
            self.query_one(Tabs).focus()
        except Exception:
            pass

    def _set_status(self, text: Union[str, Text]) -> None:
        if isinstance(text, str):
            self.query_one("#settings-status", Static).update(Text(text))
        else:
            self.query_one("#settings-status", Static).update(text)

    # --------------------------------------------------------------------------
    # Tab 1: Favorites logic
    # --------------------------------------------------------------------------
    def _setup_favorites_table(self) -> None:
        table = self.query_one("#favorites-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Name", "Path", "Status")
        for idx, fav in enumerate(self._favorites):
            status = "✓ Available" if is_folder_available(fav.path) else "✗ Missing"
            table.add_row(fav.name, fav.path, status, key=str(idx))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if event.data_table.id == "favorites-table":
            idx = int(event.row_key.value)
            if 0 <= idx < len(self._favorites):
                self._selected_favorite_idx = idx
                fav = self._favorites[idx]
                self.query_one("#favorite-name-input", Input).value = fav.name
                self.query_one("#favorite-path-input", Input).value = fav.path
        elif event.data_table.id == "council-members-table":
            idx = int(event.row_key.value)
            if 0 <= idx < len(self._council_members):
                self._selected_member_idx = idx
                m = self._council_members[idx]
                self.query_one("#council-member-label-input", Input).value = m.label
                self.query_one("#council-member-harness-select", Select).value = (
                    m.kind.value
                )
                self.query_one("#council-member-model-input", Input).value = m.model
                self.query_one("#council-member-enabled-checkbox", Checkbox).value = (
                    m.enabled
                )

    # --------------------------------------------------------------------------
    # Tab 4: Council logic
    # --------------------------------------------------------------------------
    def _setup_council_table(self) -> None:
        table = self.query_one("#council-members-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Label", "Harness", "Model", "Enabled")
        for idx, m in enumerate(self._council_members):
            enabled_str = "Enabled" if m.enabled else "Disabled"
            table.add_row(m.label, m.kind.value, m.model, enabled_str, key=str(idx))

    def _refresh_council_warning(self) -> None:
        enabled_count = sum(1 for m in self._council_members if m.enabled)
        warning = self.query_one("#council-warning", Static)
        if enabled_count < 2:
            warning.update(
                f"⚠ Warning: AI Council requires at least 2 enabled members (currently {enabled_count} enabled)."
            )
        else:
            warning.update(f"Council roster valid ({enabled_count} members enabled).")

    # --------------------------------------------------------------------------
    # Actions
    # --------------------------------------------------------------------------
    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "btn-cancel":
            self.dismiss(None)
            return

        if btn_id == "btn-browse-default-folder":
            self._browse_folder(self.query_one("#default-folder-input", Input))
            return

        if btn_id == "btn-browse-favorite-path":
            self._browse_folder(self.query_one("#favorite-path-input", Input))
            return

        if btn_id == "btn-browse-aihead-cwd":
            self._browse_folder(self.query_one("#aihead-cwd-input", Input))
            return

        if btn_id == "btn-add-favorite":
            name = self.query_one("#favorite-name-input", Input).value.strip()
            path = self.query_one("#favorite-path-input", Input).value.strip()
            if not name or not path:
                self._set_status("Favorite name and path cannot be empty.")
                return
            if any(f.name.lower() == name.lower() for f in self._favorites):
                self._set_status(f"Favorite name '{name}' already exists.")
                return
            self._favorites.append(FavoriteProject(name=name, path=path))
            self._setup_favorites_table()
            self._set_status(f"Added favorite '{name}'.")
            return

        if btn_id == "btn-edit-favorite":
            if self._selected_favorite_idx is None:
                self._set_status("Select a favorite to update.")
                return
            name = self.query_one("#favorite-name-input", Input).value.strip()
            path = self.query_one("#favorite-path-input", Input).value.strip()
            if not name or not path:
                self._set_status("Favorite name and path cannot be empty.")
                return
            idx = self._selected_favorite_idx
            if any(
                i != idx and f.name.lower() == name.lower()
                for i, f in enumerate(self._favorites)
            ):
                self._set_status(f"Favorite name '{name}' already exists.")
                return
            self._favorites[idx] = FavoriteProject(name=name, path=path)
            self._setup_favorites_table()
            self._set_status(f"Updated favorite '{name}'.")
            return

        if btn_id == "btn-remove-favorite":
            if self._selected_favorite_idx is None:
                self._set_status("Select a favorite to remove.")
                return
            idx = self._selected_favorite_idx
            removed = self._favorites.pop(idx)
            self._selected_favorite_idx = None
            self._setup_favorites_table()
            self._set_status(f"Removed favorite '{removed.name}'.")
            return

        if btn_id == "btn-test-favorite":
            path = self.query_one("#favorite-path-input", Input).value.strip()
            status_widget = self.query_one("#favorite-test-status", Static)
            if not path:
                status_widget.update("Enter a path to test.")
                return
            available = is_folder_available(path)
            resolved = resolve_folder_path(path)
            if available:
                status_widget.update(f"✓ Available: {resolved}")
            else:
                status_widget.update(
                    f"✗ Path does not exist or is not a directory: {resolved}"
                )
            return

        # Council tab buttons
        if btn_id == "btn-add-member":
            label = self.query_one("#council-member-label-input", Input).value.strip()
            if not label:
                label = f"council-{len(self._council_members) + 1}"
            if any(m.label.lower() == label.lower() for m in self._council_members):
                self._set_status(f"Council member '{label}' already exists.")
                return
            harness = str(
                self.query_one("#council-member-harness-select", Select).value
            )
            model = (
                self.query_one("#council-member-model-input", Input).value.strip()
                or "pro"
            )
            enabled = self.query_one("#council-member-enabled-checkbox", Checkbox).value
            m = CouncilMember(
                kind=HarnessKind(harness),
                model=model,
                label=label,
                enabled=enabled,
                order=len(self._council_members),
            )
            self._council_members.append(m)
            self._setup_council_table()
            self._refresh_council_warning()
            self._set_status(f"Added Council member '{label}'.")
            return

        if btn_id == "btn-edit-member":
            if self._selected_member_idx is None:
                self._set_status("Select a council member to update.")
                return
            idx = self._selected_member_idx
            label = self.query_one("#council-member-label-input", Input).value.strip()
            if not label:
                self._set_status("Member label cannot be blank.")
                return
            if any(
                i != idx and m.label.lower() == label.lower()
                for i, m in enumerate(self._council_members)
            ):
                self._set_status(f"Council member '{label}' already exists.")
                return
            harness = str(
                self.query_one("#council-member-harness-select", Select).value
            )
            model = self.query_one("#council-member-model-input", Input).value.strip()
            if not model:
                self._set_status("Member model cannot be blank.")
                return
            enabled = self.query_one("#council-member-enabled-checkbox", Checkbox).value
            self._council_members[idx] = CouncilMember(
                kind=HarnessKind(harness),
                model=model,
                label=label,
                enabled=enabled,
                order=idx,
            )
            self._setup_council_table()
            self._refresh_council_warning()
            self._set_status(f"Updated Council member '{label}'.")
            return

        if btn_id == "btn-remove-member":
            if self._selected_member_idx is None:
                self._set_status("Select a council member to remove.")
                return
            idx = self._selected_member_idx
            removed = self._council_members.pop(idx)
            self._selected_member_idx = None
            self._setup_council_table()
            self._refresh_council_warning()
            self._set_status(f"Removed Council member '{removed.label}'.")
            return

        if btn_id == "btn-member-up":
            if self._selected_member_idx is None or self._selected_member_idx <= 0:
                return
            idx = self._selected_member_idx
            self._council_members[idx - 1], self._council_members[idx] = (
                self._council_members[idx],
                self._council_members[idx - 1],
            )
            self._selected_member_idx = idx - 1
            self._setup_council_table()
            return

        if btn_id == "btn-member-down":
            if (
                self._selected_member_idx is None
                or self._selected_member_idx >= len(self._council_members) - 1
            ):
                return
            idx = self._selected_member_idx
            self._council_members[idx + 1], self._council_members[idx] = (
                self._council_members[idx],
                self._council_members[idx + 1],
            )
            self._selected_member_idx = idx + 1
            self._setup_council_table()
            return

        if btn_id == "btn-save":
            self._validate_and_save()
            return

    def _browse_folder(self, target_input: Input) -> None:
        try:
            raw = target_input.value.strip()
            folder = Path(raw).expanduser().resolve() if raw else Path.home()
            if not folder.is_dir():
                folder = Path.home()
        except Exception:
            folder = Path.home()

        def on_selected(path: Optional[Path]) -> None:
            if path is not None:
                target_input.value = str(path)

        self.app.push_screen(ProjectFolderPicker(folder), on_selected)

    def _council_effort(self, role: str) -> Optional[EffortLevel]:
        value = str(self.query_one(f"#council-{role}-effort", Select).value)
        return EffortLevel(value) if value in {e.value for e in EffortLevel} else None

    def _validate_and_save(self) -> None:
        tabbed = self.query_one(TabbedContent)

        # Tab 1: Projects validation
        default_folder = self.query_one("#default-folder-input", Input).value.strip()

        # Tab 2: Ergo Squad validation
        ergo_err = self.squad_editor.validate()
        if ergo_err:
            self._set_status(f"Ergo Squad: {ergo_err}")
            tabbed.active = "tab-ergo"
            return
        new_squad = self.squad_editor.get_squad()

        # Tab 3: Orchestration Squad validation
        orch_err = self.orchestration_editor.validate()
        if orch_err:
            self._set_status(f"Orchestration Squad: {orch_err}")
            tabbed.active = "tab-orchestration"
            return
        new_orch = self.orchestration_editor.get_config()
        new_orch.default_harness = new_orch.harness

        # Tab 4: Council validation
        coord_harness = str(
            self.query_one("#council-coordinator-harness", Select).value
        )
        coord_model = self.query_one("#council-coordinator-model", Input).value.strip()
        scout_harness = str(self.query_one("#council-scout-harness", Select).value)
        scout_model = self.query_one("#council-scout-model", Input).value.strip()
        coord_effort = self._council_effort("coordinator")
        scout_effort = self._council_effort("scout")

        if not coord_model:
            self._set_status("Council Coordinator model cannot be blank.")
            tabbed.active = "tab-council"
            return
        if not scout_model:
            self._set_status("Council Scout model cannot be blank.")
            tabbed.active = "tab-council"
            return

        enabled_members = [m for m in self._council_members if m.enabled]
        if len(enabled_members) < 2:
            self._set_status(
                f"Council requires at least 2 enabled members (currently {len(enabled_members)} enabled)."
            )
            tabbed.active = "tab-council"
            return

        try:
            new_council = CouncilConfig(
                coordinator=HarnessBinding(
                    kind=HarnessKind(coord_harness),
                    model=coord_model,
                    effort=coord_effort,
                    label="coordinator",
                ),
                scout=HarnessBinding(
                    kind=HarnessKind(scout_harness),
                    model=scout_model,
                    effort=scout_effort,
                    label="scout",
                ),
                members=self._council_members,
            )
        except Exception as exc:
            self._set_status(f"Council validation error: {exc}")
            tabbed.active = "tab-council"
            return

        # Tab 5: AiHead validation
        aihead_enabled = self.query_one("#aihead-enabled", Checkbox).value
        aihead_soul = self.query_one("#aihead-soul-input", TextArea).text.strip()
        aihead_harness = str(self.query_one("#aihead-harness", Select).value)
        aihead_model = self.query_one("#aihead-model", Input).value.strip()
        aihead_cwd = self.query_one("#aihead-cwd-input", Input).value.strip() or None
        aihead_effort_raw = str(self.query_one("#aihead-effort", Select).value)
        aihead_effort = (
            EffortLevel(aihead_effort_raw)
            if aihead_effort_raw in {e.value for e in EffortLevel}
            else None
        )
        aihead_skip_permissions = self.query_one(
            "#aihead-dangerously-skip-permissions", Checkbox
        ).value

        if aihead_enabled:
            if not aihead_model:
                self._set_status("AiHead model cannot be blank when enabled.")
                tabbed.active = "tab-aihead"
                return
            if not aihead_soul:
                self._set_status("AiHead soul / system prompt cannot be blank.")
                tabbed.active = "tab-aihead"
                return

        try:
            new_aihead = self._aihead_config.model_copy(deep=True)
            new_aihead.enabled = aihead_enabled
            new_aihead.soul = aihead_soul or DEFAULT_AI_HEAD_SOUL
            new_aihead.cwd = aihead_cwd
            setattr(new_aihead, "dangerously_skip_permissions", aihead_skip_permissions)

            if new_aihead.binding is not None:
                new_binding = new_aihead.binding.model_copy(deep=True)
                new_binding.kind = HarnessKind(aihead_harness)
                new_binding.model = aihead_model or "pro"
                new_binding.effort = aihead_effort
            else:
                new_binding = HarnessBinding(
                    kind=HarnessKind(aihead_harness),
                    model=aihead_model or "pro",
                    effort=aihead_effort,
                    label="ai-head",
                )
            new_aihead.binding = new_binding

            new_aihead = type(new_aihead).model_validate(new_aihead.model_dump())
        except Exception as exc:
            self._set_status(f"AiHead validation error: {exc}")
            tabbed.active = "tab-aihead"
            return

        # Assemble and persist config
        self.config.projects.default_folder = default_folder or None
        self.config.projects.favorites = self._favorites
        self.config.orchestration = new_orch
        self.config.council = new_council
        self.config.ai_head = new_aihead

        self.config = squad_to_project_config(new_squad, existing_config=self.config)
        try:
            save_project_config(self.config, path=self.project_config_path)
        except OSError as exc:
            self._set_status(
                Text(
                    f"Save failed: {exc}. Retry with Save or press Cancel. Destination: {self.project_config_path}"
                )
            )
            return

        self.dismiss((self.config, new_squad))
