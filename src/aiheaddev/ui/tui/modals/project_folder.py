"""Reusable project-folder input and keyboard-accessible directory picker."""

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, List, Optional

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DirectoryTree, Input, Label, Select, Static


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


def initial_project_folder(
    project_path: Optional[Path] = None,
    config: Optional[Any] = None,
) -> Path:
    if project_path is not None:
        path = Path(project_path).expanduser()
        return (path.parent if path.suffix == ".toml" else path).resolve()

    if config is None:
        try:
            from herdr_master.core.config import load_project_config

            config = load_project_config()
        except Exception:
            config = None

    if config is not None and getattr(config, "projects", None) is not None:
        default_folder = getattr(config.projects, "default_folder", None)
        if default_folder and str(default_folder).strip():
            try:
                from herdr_master.core.config import resolve_effective_folder

                return resolve_effective_folder(None, config=config)
            except Exception:
                pass

    return Path.cwd().resolve()


def validate_project_folder(value: str) -> Path:
    if not value.strip():
        raise ValueError("Choose a project folder.")
    path = Path(value.strip()).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"Project folder does not exist or is not a directory: {path}")
    return path


class FolderTree(DirectoryTree):
    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [path for path in paths if path.is_dir()]


class ProjectFolderPicker(ModalScreen[Optional[Path]]):
    """Select a directory, never a file; browsing does not change process cwd."""

    DEFAULT_CSS = """
    ProjectFolderPicker { align: center middle; background: rgba(11, 15, 25, 0.85); }
    ProjectFolderPicker > Vertical {
        width: 90%; height: 90%; border: thick #38BDF8; background: #161E2E; padding: 0 1; color: #F8FAFC;
    }
    ProjectFolderPicker Label { height: 1; color: #94A3B8; }
    ProjectFolderPicker DirectoryTree { height: 1fr; background: #111827; color: #F8FAFC; }
    ProjectFolderPicker DirectoryTree:focus > .tree--cursor { background: #1E293B; color: #38BDF8; }
    ProjectFolderPicker Horizontal { height: 3; margin-top: 1; }
    ProjectFolderPicker Button { margin-right: 1; min-width: 10; background: #334155; color: #F8FAFC; }
    ProjectFolderPicker Button:hover { background: #475569; }
    ProjectFolderPicker #btn-select-folder { background: #0284C7; color: #F8FAFC; text-style: bold; }
    ProjectFolderPicker #btn-select-folder:hover { background: #0369A1; }
    #folder-error { height: 2; color: #F59E0B; }
    """

    def __init__(self, folder: Path) -> None:
        super().__init__()
        self.folder = folder if folder.is_dir() else Path.home()
        with _ensure_active_app():
            self._path = Input(value=str(self.folder), id="folder-location")
            self._tree = FolderTree(self.folder, id="folder-tree")
            self._error = Static("", id="folder-error", markup=False)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Choose project folder (select a directory, then Use Folder)")
            yield self._path
            with Horizontal():
                yield Button("Go", id="folder-go")
                yield Button("Up", id="folder-up")
                yield Button("Home", id="folder-home")
            yield self._tree
            yield self._error
            with Horizontal():
                yield Button("Use Folder", id="folder-select", variant="primary")
                yield Button("Cancel", id="folder-cancel")

    def _go(self, value: str) -> None:
        try:
            self.folder = validate_project_folder(value)
            self._tree.path = self.folder
            self._path.value = str(self.folder)
            self._error.update("")
        except (ValueError, OSError) as exc:
            self._error.update(str(exc))

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        event.stop()
        self._path.value = str(event.path)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._go(self._path.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        action = event.button.id
        if action == "folder-cancel":
            self.dismiss(None)
        elif action == "folder-go":
            self._go(self._path.value)
        elif action == "folder-up":
            try:
                self._go(str(validate_project_folder(self._path.value).parent))
            except (ValueError, OSError) as exc:
                self._error.update(str(exc))
        elif action == "folder-home":
            self._go(str(Path.home()))
        elif action == "folder-select":
            try:
                folder = validate_project_folder(self._path.value)
            except (ValueError, OSError) as exc:
                self._error.update(str(exc))
                return
            self.dismiss(folder)


class ProjectFolderField(Vertical):
    DEFAULT_CSS = """
    ProjectFolderField { height: 4; }
    ProjectFolderField Label { height: 1; }
    ProjectFolderField Horizontal { height: 3; }
    #select-favorite-folder { width: 22; min-width: 16; margin-right: 1; }
    ProjectFolderField Input { width: 1fr; }
    ProjectFolderField Button { width: 12; min-width: 10; margin-left: 1; }
    """

    class Changed(Message):
        pass

    def __init__(
        self,
        project_path: Optional[Path] = None,
        config: Optional[Any] = None,
        favorites: Optional[List[Any]] = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._favorites: List[Any] = list(favorites) if favorites is not None else []
        if favorites is None and self._config is None:
            try:
                from herdr_master.core.config import load_project_config

                self._config = load_project_config(project_path)
            except Exception:
                self._config = None

        if favorites is None and self._config is not None:
            projects_cfg = getattr(self._config, "projects", None)
            if projects_cfg is not None:
                self._favorites = list(getattr(projects_cfg, "favorites", []) or [])

        init_folder = initial_project_folder(project_path, config=self._config)
        with _ensure_active_app():
            self._input = Input(value=str(init_folder), id="project-path")
            self._input.tooltip = "Working directory for this launch. Type a path or browse folders."

            options = [(fav.name, fav.name) for fav in self._favorites]
            self._select = Select(
                options=options,
                prompt="Favorites…",
                id="select-favorite-folder",
                allow_blank=True,
            )
            if not self._favorites:
                self._select.display = False

    def compose(self) -> ComposeResult:
        yield Label("Project folder:")
        with Horizontal():
            yield self._select
            yield self._input
            yield Button("Browse…", id="btn-browse-project")

    def selected_path(self) -> Path:
        return validate_project_folder(self._input.value)

    def select_favorite(self, name_or_path: str) -> None:
        for fav in self._favorites:
            if fav.name == name_or_path or str(fav.path) == name_or_path:
                self._select.value = fav.name
                from herdr_master.core.config import resolve_folder_path

                self._input.value = str(resolve_folder_path(fav.path))
                self.post_message(self.Changed())
                return

    def set_favorites(self, favorites: List[Any]) -> None:
        self._favorites = list(favorites)
        options = [(fav.name, fav.name) for fav in self._favorites]
        self._select.set_options(options)
        self._select.display = bool(self._favorites)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "select-favorite-folder":
            event.stop()
            if event.value != Select.BLANK and event.value is not None:
                for fav in self._favorites:
                    if fav.name == event.value:
                        from herdr_master.core.config import resolve_folder_path

                        self._input.value = str(resolve_folder_path(fav.path))
                        self.post_message(self.Changed())
                        break

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "project-path":
            event.stop()
            self.post_message(self.Changed())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        try:
            folder = self.selected_path()
        except (ValueError, OSError):
            folder = Path.home()

        def selected(path: Optional[Path]) -> None:
            if path is not None:
                self._input.value = str(path)
                self.post_message(self.Changed())

        self.app.push_screen(ProjectFolderPicker(folder), selected)
