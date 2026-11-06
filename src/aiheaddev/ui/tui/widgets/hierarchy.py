"""Hierarchy tree widget representing Master, Roles, and Worker topology."""

from rich.text import Text
from textual.widgets import Tree

from aiheaddev.ui.tui.messages import TargetSelected
from aiheaddev.ui.tui.state import AppState
from aiheaddev.ui.tui.theme import STATE_COLORS, STATE_ICONS


class HierarchyTree(Tree):
    """Hierarchy tree displaying Root master, fixed roles, and active worker pool."""

    DEFAULT_CSS = """
    HierarchyTree {
        width: 32;
        background: #111827;
        border-right: solid #334155;
        scrollbar-gutter: stable;
        color: #F8FAFC;
    }
    HierarchyTree:focus > .tree--cursor {
        background: #1E293B;
        color: #38BDF8;
        text-style: bold;
    }
    """

    def __init__(self, app_state: AppState, **kwargs) -> None:
        super().__init__("👑 Root Master", data={"id": "root", "kind": "master"}, **kwargs)
        self.app_state = app_state
        self.show_root = True

    def on_mount(self) -> None:
        self.rebuild_tree()

    def update_state(self, app_state: AppState) -> None:
        self.app_state = app_state
        self.rebuild_tree()

    def rebuild_tree(self) -> None:
        """Reconstruct the hierarchy tree from current AppState."""
        self.clear()
        self.root.label = "👑 [bold cyan]Root Master[/bold cyan]"
        self.root.data = {"id": "master", "kind": "master"}

        squad = self.app_state.squad
        run = self.app_state.pipeline_run

        # 1. Scout Role Node
        scout_label = Text()
        scout_label.append("● ", style="cyan" if run and run.phase.value == "SCOUT" else "green")
        scout_label.append("Scout", style="bold")
        scout_label.append(f" ({squad.scout.kind.value})", style="dim")
        self.root.add_leaf(scout_label, data={"id": "scout", "kind": "role"})

        # 2. Architect Role Node
        arch_label = Text()
        arch_label.append("● ", style="cyan" if run and run.phase.value == "ARCHITECT" else "green")
        arch_label.append("Architect", style="bold")
        arch_label.append(f" ({squad.architect.kind.value})", style="dim")
        self.root.add_leaf(arch_label, data={"id": "architect", "kind": "role"})

        # 3. Worker Pool Subtree
        pool_node = self.root.add(
            "📦 [bold]Worker Pool[/bold]", data={"id": "worker_pool", "kind": "pool"}
        )
        pool_node.expand()

        for worker_id, worker in self.app_state.workers.items():
            leaf_text = Text()
            color = STATE_COLORS.get(worker.state, "white")
            icon = STATE_ICONS.get(worker.state, "•")
            leaf_text.append(f"{icon} ", style=color)
            leaf_text.append(f"{worker_id} ", style="bold")
            leaf_text.append(f"[{worker.state.value}]", style=color)
            leaf_text.append(f"\n   ↳ {worker.harness.value}:{worker.model}", style="dim")
            if worker.branch:
                leaf_text.append(f" ({worker.branch.split('/')[-1]})", style="dim")
            pool_node.add_leaf(leaf_text, data={"id": worker_id, "kind": "worker"})

        # If no active workers yet, display template worker rows from pool
        if not self.app_state.workers and squad.worker_pool:
            for idx, pool_item in enumerate(squad.worker_pool):
                p_text = Text()
                p_text.append("○ ", style="dim")
                label = pool_item.label or f"worker-{idx + 1}"
                p_text.append(f"{label} ", style="dim")
                p_text.append(f"({pool_item.to_slug()})", style="dim italic")
                pool_node.add_leaf(p_text, data={"id": f"pool_{idx}", "kind": "pool_row"})

        # 4. Reviewer Role Node
        rev_label = Text()
        rev_label.append("● ", style="cyan" if run and run.phase.value == "REVIEW" else "dim")
        rev_label.append("Reviewer", style="bold")
        rev_label.append(f" ({squad.reviewer.kind.value})", style="dim")
        self.root.add_leaf(rev_label, data={"id": "reviewer", "kind": "role"})

        self.root.expand()

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Handle tree node selection and post decoupled TargetSelected message."""
        node_data = event.node.data or {}
        target_id = node_data.get("id")
        kind = node_data.get("kind", "unknown")
        if target_id:
            self.post_message(TargetSelected(target_id=target_id, kind=kind))
