"""Task DAG widget rendering planned tasks and dependency edges."""

from rich.table import Table
from rich.text import Text
from textual.widgets import Static

from herdr_master.core.models import PipelineRun
from aiheaddev.ui.tui.theme import STATE_COLORS, STATE_ICONS


class TaskDAGWidget(Static):
    """Renders task dependency graph (DAG) and file ownership matrix."""

    DEFAULT_CSS = """
    TaskDAGWidget {
        height: auto;
        padding: 0 1;
        background: #111827;
        color: #F8FAFC;
        margin-top: 1;
        border: solid #334155;
    }
    """

    def __init__(self, run: PipelineRun | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.run = run

    def update_run(self, run: PipelineRun | None) -> None:
        self.run = run
        self.refresh_dag()

    def on_mount(self) -> None:
        self.refresh_dag()

    def refresh_dag(self) -> None:
        if not self.run or not self.run.tasks:
            self.update(Text("No active pipeline plan loaded.", style="dim italic"))
            return

        table = Table(
            title="[bold cyan]Pipeline Tasks[/bold cyan]",
            box=None,
            pad_edge=False,
            title_justify="left",
            border_style="dim",
            expand=True,
            show_header=True,
            header_style="bold magenta",
        )
        table.add_column("Task", style="bold white", ratio=1)
        table.add_column("Status", ratio=1)
        table.add_column("Dependency", ratio=1)
        table.add_column("Specialist", ratio=2)
        table.add_column("Files", style="dim", ratio=2)

        for task in self.run.tasks:
            color = STATE_COLORS.get(task.status, "white")
            icon = STATE_ICONS.get(task.status, "•")
            status_text = Text(f"{icon} {task.status.value}", style=color)

            dep_style = "green" if task.dependency == "INDEPENDENT" else "yellow"
            dep_text = Text(task.dependency, style=dep_style)

            binding_str = task.binding.to_slug() if task.binding else "Pending assignment"
            persona = f" ({task.expert_persona})" if task.expert_persona else ""
            specialist_text = Text(f"{binding_str}{persona}")

            files_str = ", ".join(task.files[:3])
            if len(task.files) > 3:
                files_str += f" (+{len(task.files) - 3} more)"

            table.add_row(task.task_id, status_text, dep_text, specialist_text, files_str)

        self.update(table)
