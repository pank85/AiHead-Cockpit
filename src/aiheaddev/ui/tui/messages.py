"""Textual Message events for Herdr-Master TUI decoupled interactions."""

from typing import Optional

from textual.message import Message

from herdr_master.core.models import Squad


class TargetSelected(Message):
    """Fired when an agent, role, or worker row is selected in the tree or view."""

    def __init__(self, target_id: str, kind: str = "worker") -> None:
        super().__init__()
        self.target_id = target_id
        self.kind = kind  # "role", "worker", "pool_row"


class DeckModeChanged(Message):
    """Fired when switching center deck mode (1: Output, 2: Diff, 3: Pipeline, 4: Mailbox)."""

    def __init__(self, mode: int) -> None:
        super().__init__()
        self.mode = mode


class ConfigDrawerRequested(Message):
    """Fired to open the configuration drawer modal."""

    def __init__(self, target: Optional[str] = None) -> None:
        super().__init__()
        self.target = target


class SquadSaved(Message):
    """Fired when the squad configuration has been updated."""

    def __init__(self, squad: Squad, is_default: bool = False) -> None:
        super().__init__()
        self.squad = squad
        self.is_default = is_default


class PromptWorkerRequested(Message):
    """Fired when the operator requests prompting an agent."""

    def __init__(self, worker_id: str) -> None:
        super().__init__()
        self.worker_id = worker_id


class ReviewWorkerRequested(Message):
    """Fired when the operator requests a review pass."""

    def __init__(self, worker_id: str) -> None:
        super().__init__()
        self.worker_id = worker_id


class SquashLandRequested(Message):
    """Fired when the operator requests squash landing to develop."""

    pass


class KillWorkerRequested(Message):
    """Fired when the operator requests terminating a worker tab."""

    def __init__(self, worker_id: str) -> None:
        super().__init__()
        self.worker_id = worker_id
