"""AppState store for Herdr-Master TUI.

Domain state stays usable without project I/O. Catalog helpers below only read
the in-memory descriptor so live widgets do not keep a second model table.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from herdr_master.core.catalog import (
    CatalogError,
    compatibility_presets,
    describe_herdr_agent,
    ensure_herdr_routable,
    get_harness,
    supported_harnesses,
    worker_capability,
    worker_route_note,
)
from herdr_master.core.config import HerdrMasterConfig
from herdr_master.core.models import (
    EffortLevel,
    HarnessBinding,
    MasterSession,
    PipelinePhase,
    PipelineRun,
    Squad,
    TaskNode,
    WorkerSession,
    WorkerState,
)
from herdr_master.herdr.discovery import SessionInventory
from herdr_master.herdr.snapshot import (
    AgentSnapshot,
    SessionSnapshot,
    TabSnapshot,
    WorkspaceSnapshot,
)
from herdr_master.pipeline.squad import describe_agent


def harness_select_options(*, mode: str = "herdr") -> List[Tuple[str, str]]:
    """Harness labels and values from the catalog, not a hand-written table."""
    options: List[Tuple[str, str]] = []
    for kind in supported_harnesses(mode=mode):
        spec = get_harness(kind)
        options.append((f"{spec.label} ({spec.kind})", spec.kind))
    return options


def effort_select_options(
    kind: Optional[str] = None,
    model: Optional[str] = None,
    *,
    mode: str = "herdr",
    include_unset: bool = True,
) -> List[Tuple[str, str]]:
    """Efforts the catalog allows for this harness and model."""
    efforts: List[str] = []
    text = model.strip() if isinstance(model, str) else ""
    if kind and text:
        try:
            payload = worker_capability(kind, text, mode=mode)
            efforts = list(payload.get("allowed_efforts") or [])
        except CatalogError:
            efforts = []
    if not efforts and kind:
        try:
            spec = get_harness(kind)
            if spec.herdr.effort_flag or spec.herdr.prompt_effort:
                efforts = ["low", "medium", "high", "max"]
        except CatalogError:
            efforts = []
    options: List[Tuple[str, str]] = []
    if include_unset:
        options.append(("Unset", "none"))
    for effort in efforts:
        options.append((effort.capitalize(), effort))
    if not options:
        options.append(("Unset", "none"))
    return options


def catalog_worker_template(index: int) -> HarnessBinding:
    """New worker from the catalog pool template. Effort stays unset."""
    template = Squad().worker_pool[0]
    return template.model_copy(
        update={
            "label": f"worker-{index + 1}",
            "effort": None,
            "enabled": True,
            "order": index,
        }
    )


def normalize_worker_order(squad: Squad) -> None:
    for idx, worker in enumerate(squad.worker_pool):
        worker.order = idx


def project_config_file(project_path: Optional[Path]) -> Optional[Path]:
    """Return the project toml path. A directory means that directory's file."""
    if project_path is None:
        return None
    path = Path(project_path)
    if path.suffix == ".toml":
        return path
    return path / ".herdr-master.toml"


def compatibility_preset_note() -> str:
    presets = compatibility_presets()
    if any(item.get("default") for item in presets):
        return "A compatibility preset is marked default; that is a catalog bug."
    return "Compatibility presets are not the product default."


def squad_source_line(sources: Optional[Dict[str, str]], mode: str) -> str:
    if not sources:
        return f"{mode} · catalog"
    if set(sources.values()) == {"product_default"}:
        return f"{mode} · product_default"
    ordered = [
        f"{key}={sources[key]}"
        for key in ("scout", "architect", "reviewer", "workers")
        if key in sources
    ]
    return f"{mode} · " + " ".join(ordered)


def scout_summary(squad: Squad) -> str:
    effort = squad.scout.effort.value if squad.scout.effort else "unset"
    return f"scout {squad.scout.kind.value}:{squad.scout.model}:{effort}"


def binding_launch_preview(
    binding: HarnessBinding,
    *,
    role: str,
    mode: str = "herdr",
    native_harness: Optional[str] = None,
) -> str:
    """Canonical route text. Workers keep effort unset."""
    if role in {"worker", "coder"}:
        try:
            note = worker_route_note(
                binding.kind.value,
                binding.model,
                mode=mode,
                native_harness=native_harness,
            )
        except CatalogError as exc:
            return str(exc)
        return f"worker effort unset · {note}"
    try:
        described = describe_agent(
            binding,
            mode=mode,  # type: ignore[arg-type]
            role=role,
            native_harness=native_harness,
        )
    except CatalogError as exc:
        return str(exc)
    argv = described.get("argv") or []
    kind = described.get("kind") or binding.kind.value
    if argv:
        return f"{kind} " + " ".join(str(part) for part in argv)
    return f"{kind} {described.get('tool') or role}".strip()


def coordinator_route_text(kind: str, model: str, effort: Optional[str]) -> str:
    described = describe_herdr_agent(kind, model, effort, "master")
    argv = described.get("argv") or []
    return f"{described['kind']} " + " ".join(str(part) for part in argv)


def validate_binding_for_editor(
    binding: HarnessBinding,
    *,
    role: str,
    mode: str = "herdr",
    native_harness: Optional[str] = None,
) -> Optional[str]:
    """Return an actionable error. Never invent a model for blank input."""
    if not binding.kind or not str(binding.model).strip():
        return f"{role} requires a model. Refusing to invent one."
    try:
        if role in {"worker", "coder"}:
            ensure_herdr_routable(
                binding.kind.value,
                binding.model,
                None,
                role="coder",
                require_effort=False,
            )
            return None
        describe_agent(
            binding,
            mode=mode,  # type: ignore[arg-type]
            role=role,
            native_harness=native_harness,
        )
    except CatalogError as exc:
        return str(exc)
    return None


class AppState(BaseModel):
    """Central reactive application state store for the TUI."""

    session_name: str = "default"
    workspace_label: str = "ergo-cockpit"
    workspace_id: str = "ws-01"
    target_branch: str = "develop"
    branch_status: str = "CLEAN"
    socket_connected: bool = True
    active_agents_count: int = 0
    live_sessions: List[SessionInventory] = Field(default_factory=list)
    inventory_demo: bool = False
    selected_target_id: Optional[str] = "scout"

    squad: Squad = Field(default_factory=Squad)
    config: Optional[HerdrMasterConfig] = None
    ai_head_info: Optional[Dict[str, Any]] = None
    ai_head_busy: bool = False
    pipeline_run: Optional[PipelineRun] = None
    masters: Dict[str, MasterSession] = Field(default_factory=dict)
    workers: Dict[str, WorkerSession] = Field(default_factory=dict)

    @classmethod
    def create_demo_state(cls) -> "AppState":
        """Offline visualization. Squad models come from the catalog.

        Task text, scrollback, and diffs are labelled demo fixtures, not product defaults.
        """
        squad = Squad()
        pool = squad.worker_pool

        def _bind(index: int) -> HarnessBinding:
            return pool[index % len(pool)]

        tasks = [
            TaskNode(
                task_id="task-1",
                description="Core domain models & slug parser",
                files=["src/herdr_master/core/slug.py", "src/herdr_master/core/models.py"],
                dependency="INDEPENDENT",
                expert_persona="Protocol & Grammar Specialist",
                binding=_bind(0),
                status=WorkerState.DONE,
                worker_id="coder-1",
            ),
            TaskNode(
                task_id="task-2",
                description="Herdr Socket Client (Protocol 22)",
                files=["src/herdr_master/herdr/socket.py", "src/herdr_master/herdr/client.py"],
                dependency="INDEPENDENT",
                expert_persona="Async Networking & Protocol Engineer",
                binding=_bind(1),
                status=WorkerState.WORKING,
                worker_id="coder-2",
            ),
            TaskNode(
                task_id="task-3",
                description="Textual TUI Cockpit & Ergo pipeline view",
                files=[
                    "src/herdr_master/ui/tui/app.py",
                    "src/herdr_master/ui/tui/widgets/workspaces.py",
                ],
                dependency="DEPENDS ON: task-1",
                expert_persona="TUI & Visual Layout Architect",
                binding=_bind(0),
                status=WorkerState.SPAWNING,
                worker_id="coder-3",
            ),
        ]

        run = PipelineRun(
            run_id="run-20261103-01",
            phase=PipelinePhase.CODERS,
            intent=(
                "Implement Herdr-Master core functionality and cockpit "
                "according to GROK_IMPLEMENTATION_PLAN.md"
            ),
            squad=squad,
            tasks=tasks,
            workspace_id="ws-01",
        )

        workers = {
            "coder-1": WorkerSession(
                worker_id="coder-1",
                pane_id="ws-01:p1",
                tab_id="ws-01:t1",
                harness=pool[0].kind,
                model=pool[0].model,
                worktree_path=".herdr-worktrees/herdm-coder-1",
                branch="herdm/run-01/task-1",
                state=WorkerState.DONE,
            ),
            "coder-2": WorkerSession(
                worker_id="coder-2",
                pane_id="ws-01:p2",
                tab_id="ws-01:t2",
                harness=pool[1].kind,
                model=pool[1].model,
                effort=EffortLevel.HIGH,
                worktree_path=".herdr-worktrees/herdm-coder-2",
                branch="herdm/run-01/task-2",
                state=WorkerState.WORKING,
            ),
            "coder-3": WorkerSession(
                worker_id="coder-3",
                pane_id="ws-01:p3",
                tab_id="ws-01:t3",
                harness=pool[0].kind,
                model=pool[0].model,
                worktree_path=".herdr-worktrees/herdm-coder-3",
                branch="herdm/run-01/task-3",
                state=WorkerState.SPAWNING,
            ),
        }
        masters = {
            "master-main": MasterSession(
                session_id="master-main",
                herdr_session="demo",
                workspace_id="ws-01",
                pane_id="ws-01:p-master",
                harness=squad.scout.kind,
                role="root-orchestrator",
                metadata={"socket_path": str(Path("/demo/herdr.sock")), "demo": True},
                child_workers=["coder-1", "coder-2", "coder-3"],
            )
        }

        tabs = [
            TabSnapshot(tab_id="ws-01:t-master", workspace_id="ws-01", label="Master"),
            TabSnapshot(tab_id="ws-01:t-ergo", workspace_id="ws-01", label="Ergo"),
            TabSnapshot(tab_id="ws-01:t1", workspace_id="ws-01", label="Task 1"),
            TabSnapshot(tab_id="ws-01:t2", workspace_id="ws-01", label="Task 2"),
            TabSnapshot(tab_id="ws-01:t3", workspace_id="ws-01", label="Task 3"),
            TabSnapshot(tab_id="ws-01:t-shell", workspace_id="ws-01", label="Shell"),
        ]

        demo_agents = [
            AgentSnapshot(
                pane_id="ws-01:p-master",
                workspace_id="ws-01",
                tab_id="ws-01:t-master",
                agent=squad.scout.kind.value,
                name="master-coordinator",
                agent_status="working",
                cwd=".herdr-worktrees/master",
            ),
            AgentSnapshot(
                pane_id="ws-01:p-ergo",
                workspace_id="ws-01",
                tab_id="ws-01:t-ergo",
                agent=squad.scout.kind.value,
                name="ergo-orchestrator",
                agent_status="working",
                cwd=".herdr-worktrees/ergo",
            ),
            AgentSnapshot(
                pane_id="ws-01:p1",
                workspace_id="ws-01",
                tab_id="ws-01:t1",
                agent=pool[0].kind.value,
                name="coder-1",
                agent_status="idle",
                cwd=".herdr-worktrees/herdm-coder-1",
            ),
            AgentSnapshot(
                pane_id="ws-01:p2",
                workspace_id="ws-01",
                tab_id="ws-01:t2",
                agent=pool[1].kind.value,
                name="coder-2",
                agent_status="working",
                cwd=".herdr-worktrees/herdm-coder-2",
            ),
            AgentSnapshot(
                pane_id="ws-01:p3",
                workspace_id="ws-01",
                tab_id="ws-01:t3",
                agent=pool[0].kind.value,
                name="coder-3",
                agent_status="working",
                cwd=".herdr-worktrees/herdm-coder-3",
            ),
            AgentSnapshot(
                pane_id="ws-01:p-shell",
                workspace_id="ws-01",
                tab_id="ws-01:t-shell",
                agent=None,
                name=None,
                agent_status="terminal",
                cwd="/projects/project-demo",
            ),
        ]

        return cls(
            session_name="demo",
            workspace_label="project-demo",
            workspace_id="ws-01",
            target_branch="develop",
            branch_status="CLEAN",
            socket_connected=True,
            active_agents_count=5,
            inventory_demo=True,
            live_sessions=[
                SessionInventory(
                    session_name="demo",
                    socket_path=Path("/demo/herdr.sock"),
                    snapshot=SessionSnapshot(
                        protocol=22,
                        workspaces=[
                            WorkspaceSnapshot(
                                workspace_id="ws-01", label="project-demo", agent_status="working"
                            )
                        ],
                        tabs=tabs,
                        agents=demo_agents,
                    ),
                )
            ],
            selected_target_id="coder-2",
            squad=squad,
            pipeline_run=run,
            masters=masters,
            workers=workers,
        )
