"""Comprehensive unit tests for exact-target live-pane controller and reply boundary."""

from __future__ import annotations

import asyncio
import json
import unittest.mock
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from protocol22_fixtures import agent_prompted, pane_read_record, snapshot_identity

from herdr_master.herdr.client import HerdrClient
from herdr_master.herdr.discovery import SessionInventory
from herdr_master.herdr.snapshot import (
    AgentSnapshot,
    PaneSnapshot,
    SessionSnapshot,
    TabSnapshot,
    WorkspaceSnapshot,
)
from herdr_master.herdr.socket import HerdrConnectionError, HerdrRPCError
from aiheaddev.ui.tui.live_pane import (
    LivePaneController,
    LiveTargetError,
    PaneFrame,
    PaneLease,
    ReplyRequest,
    bound_output,
    resolve_live_pane,
)
from aiheaddev.core.workspace_targets import WorkspaceTarget


class FakeClient:
    """Offline fake HerdrClient for deterministic testing."""

    def __init__(self, session: str = "default", socket_path: Path | None = None) -> None:
        self.session = session
        self.socket_path = socket_path or Path("/tmp/herdr.sock")
        self.snapshot_data: Optional[SessionSnapshot] = None
        self.snapshot_delay: float = 0.0
        self.snapshot_error: Optional[Exception] = None

        self.read_pane_data: Dict[str, Any] = {"text": "hello\nworld"}
        self.read_pane_delay: float = 0.0
        self.read_pane_error: Optional[Exception] = None

        self.prompt_result: Optional[Dict[str, Any]] = None
        self.prompt_delay: float = 0.0
        self.prompt_error: Optional[Exception] = None

        self.snapshot_calls: List[Dict[str, Any]] = []
        self.read_pane_calls: List[Dict[str, Any]] = []
        self.prompt_agent_calls: List[Dict[str, Any]] = []

        # Trap any forbidden calls
        self.focus_workspace_called = False
        self.focus_tab_called = False
        self.focus_pane_called = False
        self.send_keys_called = False
        self.attach_called = False
        self.launch_called = False

    async def get_snapshot(self) -> SessionSnapshot:
        self.snapshot_calls.append({"time": asyncio.get_running_loop().time()})
        if self.snapshot_delay > 0:
            await asyncio.sleep(self.snapshot_delay)
        if self.snapshot_error:
            raise self.snapshot_error
        if self.snapshot_data is not None:
            return self.snapshot_data
        return self._default_snapshot()

    @staticmethod
    def _default_snapshot() -> SessionSnapshot:
        return SessionSnapshot(
            protocol=22,
            workspaces=[WorkspaceSnapshot(workspace_id="ws-1", label="WS 1")],
            tabs=[TabSnapshot(tab_id="tab-1", workspace_id="ws-1")],
            panes=[
                PaneSnapshot(
                    pane_id="p-1",
                    workspace_id="ws-1",
                    tab_id="tab-1",
                    agent="agent-1",
                    terminal_id="term-1",
                    agent_status="idle",
                )
            ],
            agents=[
                AgentSnapshot(
                    pane_id="p-1",
                    workspace_id="ws-1",
                    tab_id="tab-1",
                    agent="agent-1",
                    interactive_ready=True,
                    agent_status="idle",
                )
            ],
        )

    async def read_pane(
        self, pane_id: str, format: str = "ansi", lines: Optional[int] = None
    ) -> Dict[str, Any]:
        self.read_pane_calls.append(
            {
                "pane_id": pane_id,
                "format": format,
                "lines": lines,
                "time": asyncio.get_running_loop().time(),
            }
        )
        if self.read_pane_delay > 0:
            await asyncio.sleep(self.read_pane_delay)
        if self.read_pane_error:
            raise self.read_pane_error
        ws, tab, _ = snapshot_identity(self.snapshot_data or self._default_snapshot(), pane_id)
        record = pane_read_record(pane_id, ws, tab, "")
        record.pop("text")
        return {**record, **self.read_pane_data}

    async def prompt_agent(
        self,
        target: str,
        prompt: str,
        wait: Optional[Any] = None,
        timeout_ms: Optional[int] = None,
        *,
        max_attempts: int = 20,
    ) -> Dict[str, Any]:
        self.prompt_agent_calls.append(
            {
                "target": target,
                "prompt": prompt,
                "wait": wait,
                "timeout_ms": timeout_ms,
                "max_attempts": max_attempts,
            }
        )
        if self.prompt_delay > 0:
            await asyncio.sleep(self.prompt_delay)
        if self.prompt_error:
            raise self.prompt_error
        if self.prompt_result is not None:
            return self.prompt_result
        snapshot = self.snapshot_data or self._default_snapshot()
        ws, tab, terminal = snapshot_identity(snapshot, target)
        return agent_prompted(target, ws, tab, terminal)

    async def focus_workspace(self, *args: Any, **kwargs: Any) -> None:
        self.focus_workspace_called = True

    async def focus_tab(self, *args: Any, **kwargs: Any) -> None:
        self.focus_tab_called = True

    async def focus_pane(self, *args: Any, **kwargs: Any) -> None:
        self.focus_pane_called = True

    async def send_keys(self, *args: Any, **kwargs: Any) -> None:
        self.send_keys_called = True

    async def attach(self, *args: Any, **kwargs: Any) -> None:
        self.attach_called = True

    async def launch(self, *args: Any, **kwargs: Any) -> None:
        self.launch_called = True


class FakeSubscription:
    """Offline fake HerdrSubscription streaming controlled events."""

    def __init__(
        self,
        socket_path: Path,
        subscriptions: List[Dict[str, Any]],
        timeout: float = 2.0,
    ) -> None:
        self.socket_path = socket_path
        self.subscriptions = subscriptions
        self.timeout = timeout
        self.connected = False
        self.closed = False
        self.connect_error: Optional[Exception] = None
        self._queue: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue()

    async def connect(self) -> Dict[str, Any]:
        if self.connect_error:
            raise self.connect_error
        self.connected = True
        return {"id": "sub-test", "result": {"type": "subscription_started"}}

    async def events(self):
        while True:
            item = await self._queue.get()
            if item is None:
                break
            yield item

    def feed_event(self, event: Dict[str, Any]) -> None:
        self._queue.put_nowait(event)

    def feed_eof(self) -> None:
        self._queue.put_nowait(None)

    async def close(self) -> None:
        self.closed = True
        self.feed_eof()


def make_inventory(
    session_name: str = "default",
    socket_path: Path = Path("/tmp/herdr.sock"),
    workspaces: Optional[List[WorkspaceSnapshot]] = None,
    tabs: Optional[List[TabSnapshot]] = None,
    panes: Optional[List[PaneSnapshot]] = None,
    agents: Optional[List[AgentSnapshot]] = None,
    error: Optional[str] = None,
) -> SessionInventory:
    if error is not None:
        return SessionInventory(session_name=session_name, socket_path=socket_path, error=error)
    snapshot = SessionSnapshot(
        protocol=22,
        workspaces=workspaces or [WorkspaceSnapshot(workspace_id="ws-1", label="WS 1")],
        tabs=tabs or [TabSnapshot(tab_id="tab-1", workspace_id="ws-1")],
        panes=panes
        or [
            PaneSnapshot(
                pane_id="p-1",
                workspace_id="ws-1",
                tab_id="tab-1",
                agent="agent-1",
                terminal_id="term-1",
                agent_status="idle",
            )
        ],
        agents=agents
        or [
            AgentSnapshot(
                pane_id="p-1",
                workspace_id="ws-1",
                tab_id="tab-1",
                agent="agent-1",
                interactive_ready=True,
                agent_status="idle",
            )
        ],
    )
    return SessionInventory(session_name=session_name, socket_path=socket_path, snapshot=snapshot)


# -------------------------------------------------------------------------
# Test 1: Validation Rules (resolve_live_pane)
# -------------------------------------------------------------------------


def test_resolve_live_pane_validation():
    sock = Path("/tmp/s1.sock")

    # Contradictory raw pane workspace
    inv = make_inventory(
        socket_path=sock,
        panes=[PaneSnapshot(pane_id="p-1", workspace_id="ws-2", agent="agent-1")],
    )
    with pytest.raises(LiveTargetError, match="contradicts target"):
        resolve_live_pane(
            WorkspaceTarget("s1", sock, "ws-1", pane_id="p-1"),
            [inv],
        )

    # Absent referenced tab
    inv = make_inventory(
        socket_path=sock,
        tabs=[],
        panes=[
            PaneSnapshot(pane_id="p-1", workspace_id="ws-1", tab_id="tab-missing", agent="agent-1")
        ],
        agents=[
            AgentSnapshot(pane_id="p-1", workspace_id="ws-1", tab_id="tab-missing", agent="agent-1")
        ],
    )
    with pytest.raises(LiveTargetError, match="missing tab 'tab-missing'"):
        resolve_live_pane(
            WorkspaceTarget("s1", sock, "ws-1", pane_id="p-1"),
            [inv],
        )

    # Foreign tab belonging to another workspace
    inv = make_inventory(
        socket_path=sock,
        workspaces=[WorkspaceSnapshot(workspace_id="ws-1"), WorkspaceSnapshot(workspace_id="ws-2")],
        tabs=[TabSnapshot(tab_id="tab-foreign", workspace_id="ws-2")],
        panes=[
            PaneSnapshot(pane_id="p-1", workspace_id="ws-1", tab_id="tab-foreign", agent="agent-1")
        ],
        agents=[
            AgentSnapshot(pane_id="p-1", workspace_id="ws-1", tab_id="tab-foreign", agent="agent-1")
        ],
    )
    with pytest.raises(LiveTargetError, match="foreign workspace"):
        resolve_live_pane(
            WorkspaceTarget("s1", sock, "ws-1", pane_id="p-1"),
            [inv],
        )

    # Current tab replacement (fresh snapshot establishes different tab in same workspace)
    inv = make_inventory(
        socket_path=sock,
        tabs=[
            TabSnapshot(tab_id="tab-old", workspace_id="ws-1"),
            TabSnapshot(tab_id="tab-fresh", workspace_id="ws-1"),
        ],
        panes=[
            PaneSnapshot(pane_id="p-1", workspace_id="ws-1", tab_id="tab-fresh", agent="agent-1")
        ],
        agents=[
            AgentSnapshot(pane_id="p-1", workspace_id="ws-1", tab_id="tab-fresh", agent="agent-1")
        ],
    )
    resolved = resolve_live_pane(
        WorkspaceTarget("s1", sock, "ws-1", tab_id="tab-old", pane_id="p-1"),
        [inv],
    )
    assert resolved.target.tab_id == "tab-fresh"

    # Valid tabless pane
    inv = make_inventory(
        socket_path=sock,
        panes=[PaneSnapshot(pane_id="p-1", workspace_id="ws-1", tab_id=None, agent="agent-1")],
        agents=[AgentSnapshot(pane_id="p-1", workspace_id="ws-1", tab_id=None, agent="agent-1")],
    )
    resolved = resolve_live_pane(
        WorkspaceTarget("s1", sock, "ws-1", tab_id=None, pane_id="p-1"),
        [inv],
    )
    assert resolved.target.tab_id is None
    assert resolved.target.workspace_id == "ws-1"

    # Derive missing workspace ownership only from verified owning tab
    inv = make_inventory(
        socket_path=sock,
        tabs=[TabSnapshot(tab_id="tab-1", workspace_id="ws-1")],
        panes=[PaneSnapshot(pane_id="p-1", workspace_id=None, tab_id="tab-1", agent="agent-1")],
        agents=[AgentSnapshot(pane_id="p-1", workspace_id=None, tab_id="tab-1", agent="agent-1")],
    )
    resolved = resolve_live_pane(
        WorkspaceTarget("s1", sock, "ws-1", tab_id=None, pane_id="p-1"),
        [inv],
    )
    assert resolved.target.workspace_id == "ws-1"

    # Missing workspace ownership with no tab fails
    inv = make_inventory(
        socket_path=sock,
        panes=[PaneSnapshot(pane_id="p-1", workspace_id=None, tab_id=None, agent="agent-1")],
        agents=[AgentSnapshot(pane_id="p-1", workspace_id=None, tab_id=None, agent="agent-1")],
    )
    with pytest.raises(LiveTargetError, match="has no workspace_id"):
        resolve_live_pane(
            WorkspaceTarget("s1", sock, "ws-1", pane_id="p-1"),
            [inv],
        )

    # Agent-only display entry without raw pane record is rejected
    inv = make_inventory(
        socket_path=sock,
        panes=[],
        agents=[AgentSnapshot(pane_id="p-agent-only", workspace_id="ws-1", agent="agent-1")],
    )
    with pytest.raises(LiveTargetError, match="agent-only record cannot authorize pane"):
        resolve_live_pane(
            WorkspaceTarget("s1", sock, "ws-1", pane_id="p-agent-only"),
            [inv],
        )

    # Ambiguous socket across inventories
    inv1 = make_inventory(session_name="s1", socket_path=sock)
    inv2 = make_inventory(session_name="s2", socket_path=sock)
    with pytest.raises(LiveTargetError, match="Ambiguous inventories"):
        resolve_live_pane(
            WorkspaceTarget("s1", sock, "ws-1", pane_id="p-1"),
            [inv1, inv2],
        )

    # Duplicate pane ID in snapshot
    inv = make_inventory(
        socket_path=sock,
        panes=[
            PaneSnapshot(pane_id="p-1", workspace_id="ws-1", agent="agent-1"),
            PaneSnapshot(pane_id="p-1", workspace_id="ws-1", agent="agent-2"),
        ],
    )
    with pytest.raises(LiveTargetError, match="Duplicate pane id"):
        resolve_live_pane(
            WorkspaceTarget("s1", sock, "ws-1", pane_id="p-1"),
            [inv],
        )

    # Contradictory terminal identity between pane and agent
    inv = make_inventory(
        socket_path=sock,
        panes=[
            PaneSnapshot(
                pane_id="p-1",
                workspace_id="ws-1",
                tab_id="tab-1",
                agent="agent-1",
                terminal_id="term-1",
            )
        ],
        agents=[
            AgentSnapshot(
                pane_id="p-1",
                workspace_id="ws-1",
                tab_id="tab-1",
                agent="agent-1",
                terminal_id="term-different",
            )
        ],
    )
    with pytest.raises(LiveTargetError, match="Contradictory terminal identity"):
        resolve_live_pane(
            WorkspaceTarget("s1", sock, "ws-1", pane_id="p-1"),
            [inv],
        )

    # Missing pane terminal ID when agent terminal ID is known
    inv = make_inventory(
        socket_path=sock,
        panes=[
            PaneSnapshot(
                pane_id="p-1",
                workspace_id="ws-1",
                tab_id="tab-1",
                agent="agent-1",
                terminal_id=None,
            )
        ],
        agents=[
            AgentSnapshot(
                pane_id="p-1",
                workspace_id="ws-1",
                tab_id="tab-1",
                agent="agent-1",
                terminal_id="term-agent",
            )
        ],
    )
    with pytest.raises(LiveTargetError, match="Pane terminal identity is missing"):
        resolve_live_pane(
            WorkspaceTarget("s1", sock, "ws-1", pane_id="p-1"),
            [inv],
        )


# -------------------------------------------------------------------------
# Test 2: Overlapping names/IDs across two sockets
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overlapping_names_and_ids_across_two_sockets():
    sock_a = Path("/tmp/session_a.sock")
    sock_b = Path("/tmp/session_b.sock")

    inv_a = make_inventory(session_name="alpha", socket_path=sock_a)
    inv_b = make_inventory(session_name="beta", socket_path=sock_b)

    client_a = FakeClient("alpha", sock_a)
    client_b = FakeClient("beta", sock_b)

    def factory(session: str, path: Path) -> HerdrClient:
        if path == sock_a:
            return client_a  # type: ignore
        return client_b  # type: ignore

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=factory,
        subscription_factory=lambda p, s: FakeSubscription(p, s),
        refresh_min_interval=0.01,
        poll_interval=0.1,
    )

    # Select target strictly on socket B
    target_b = WorkspaceTarget("beta", sock_b, "ws-1", pane_id="p-1")
    await controller.select(target_b, [inv_a, inv_b], enabled=True)
    await asyncio.sleep(0.02)

    # Verify client B received calls and client A was never touched
    assert len(client_b.snapshot_calls) >= 1
    assert len(client_b.read_pane_calls) >= 1
    assert len(client_a.snapshot_calls) == 0
    assert len(client_a.read_pane_calls) == 0

    await controller.close()


# -------------------------------------------------------------------------
# Test 3: Second pane selected in a multi-pane workspace
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_pane_selected_in_multi_pane_workspace():
    sock = Path("/tmp/multi.sock")
    inv = make_inventory(
        socket_path=sock,
        panes=[
            PaneSnapshot(pane_id="p-1", workspace_id="ws-1", tab_id="tab-1", agent="agent-1"),
            PaneSnapshot(pane_id="p-2", workspace_id="ws-1", tab_id="tab-1", agent="agent-2"),
        ],
        agents=[
            AgentSnapshot(pane_id="p-1", workspace_id="ws-1", agent="agent-1"),
            AgentSnapshot(pane_id="p-2", workspace_id="ws-1", agent="agent-2"),
        ],
    )
    client = FakeClient("multi", sock)
    client.snapshot_data = inv.snapshot

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,  # type: ignore
        subscription_factory=lambda p, s: FakeSubscription(p, s),
        refresh_min_interval=0.01,
        poll_interval=0.1,
    )

    target_p2 = WorkspaceTarget("multi", sock, "ws-1", pane_id="p-2")
    await controller.select(target_p2, [inv], enabled=True)
    await asyncio.sleep(0.02)

    # Assert read_pane was explicitly dispatched for p-2, never p-1
    assert any(call["pane_id"] == "p-2" for call in client.read_pane_calls)
    assert not any(call["pane_id"] == "p-1" for call in client.read_pane_calls)
    assert frames[-1].target.pane_id == "p-2"

    await controller.close()


# -------------------------------------------------------------------------
# Test 4: Workspace-only selection emits no pane calls
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_only_selection_emits_no_pane_calls():
    sock = Path("/tmp/ws_only.sock")
    inv = make_inventory(socket_path=sock)
    client = FakeClient("ws_only", sock)

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,  # type: ignore
        subscription_factory=lambda p, s: FakeSubscription(p, s),
    )

    ws_target = WorkspaceTarget("ws_only", sock, "ws-1", pane_id=None)
    await controller.select(ws_target, [inv], enabled=True)
    await asyncio.sleep(0.01)

    assert len(client.snapshot_calls) == 0
    assert len(client.read_pane_calls) == 0
    assert frames[-1].phase == "empty"
    assert frames[-1].lease is None
    assert frames[-1].can_reply is False

    await controller.close()


# -------------------------------------------------------------------------
# Test 5: Terminal read-only mode
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_read_only_mode():
    sock = Path("/tmp/term.sock")
    # Shell pane: no agent record
    inv = make_inventory(
        socket_path=sock,
        panes=[
            PaneSnapshot(
                pane_id="p-shell",
                workspace_id="ws-1",
                tab_id="tab-1",
                agent=None,
                agent_status=None,
            )
        ],
        agents=[],
    )
    client = FakeClient("term", sock)
    client.snapshot_data = inv.snapshot
    client.read_pane_data = {"text": "bash$ ls\nfile.txt"}

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,  # type: ignore
        subscription_factory=lambda p, s: FakeSubscription(p, s),
        refresh_min_interval=0.01,
        poll_interval=0.1,
    )

    target = WorkspaceTarget("term", sock, "ws-1", pane_id="p-shell")
    await controller.select(target, [inv], enabled=True)
    await asyncio.sleep(0.02)

    latest = frames[-1]
    assert latest.phase in ("live", "degraded")
    assert latest.agent_status == "terminal"
    assert "file.txt" in latest.text_ansi
    assert latest.can_reply is False

    # Attempting to submit to terminal is rejected
    req = ReplyRequest(lease=latest.lease, text="echo test")
    res = await controller.submit(req)
    assert res.outcome == "rejected"
    assert "terminal" in res.message

    await controller.close()


# -------------------------------------------------------------------------
# Test 6: Known terminal reuse invalidates lease
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_known_terminal_reuse():
    sock = Path("/tmp/reuse.sock")
    inv1 = make_inventory(
        socket_path=sock,
        panes=[
            PaneSnapshot(
                pane_id="p-1",
                workspace_id="ws-1",
                agent="agent-1",
                terminal_id="term-old",
            )
        ],
    )
    client = FakeClient("reuse", sock)
    client.snapshot_data = inv1.snapshot

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,  # type: ignore
        subscription_factory=lambda p, s: FakeSubscription(p, s),
        refresh_min_interval=0.01,
        poll_interval=0.05,
    )

    target = WorkspaceTarget("reuse", sock, "ws-1", pane_id="p-1")
    await controller.select(target, [inv1], enabled=True)
    await asyncio.sleep(0.02)

    lease1 = controller._active_lease
    assert lease1 is not None
    assert lease1.terminal_id == "term-old"
    gen1 = lease1.generation

    # Reconcile with updated snapshot showing new terminal_id (incarnation change)
    inv2 = make_inventory(
        socket_path=sock,
        panes=[
            PaneSnapshot(
                pane_id="p-1",
                workspace_id="ws-1",
                agent="agent-1",
                terminal_id="term-reused",
            )
        ],
    )
    client.snapshot_data = inv2.snapshot
    await controller.reconcile([inv2], enabled=True)
    await asyncio.sleep(0.02)

    lease2 = controller._active_lease
    assert lease2 is not None
    assert lease2.terminal_id == "term-reused"
    assert lease2.generation > gen1

    # Submitting with old lease is rejected
    stale_req = ReplyRequest(lease=lease1, text="should reject")
    res = await controller.submit(stale_req)
    assert res.outcome == "rejected"

    await controller.close()


# -------------------------------------------------------------------------
# Test 7: Missing terminal ID limitation
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_terminal_id_limitation():
    sock = Path("/tmp/no_term_id.sock")
    inv = make_inventory(
        socket_path=sock,
        panes=[
            PaneSnapshot(
                pane_id="p-1",
                workspace_id="ws-1",
                agent="agent-1",
                terminal_id=None,
            )
        ],
    )
    client = FakeClient("no_term_id", sock)
    client.snapshot_data = inv.snapshot

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,  # type: ignore
        subscription_factory=lambda p, s: FakeSubscription(p, s),
        refresh_min_interval=0.01,
        poll_interval=0.1,
    )

    target = WorkspaceTarget("no_term_id", sock, "ws-1", pane_id="p-1")
    await controller.select(target, [inv], enabled=True)
    await asyncio.sleep(0.02)

    lease = controller._active_lease
    assert lease is not None
    assert lease.terminal_id is None

    # Normal submission succeeds without terminal incarnation checks
    res = await controller.submit(ReplyRequest(lease=lease, text="hello"))
    assert res.outcome == "accepted"

    await controller.close()


# -------------------------------------------------------------------------
# Test 8: Rapidly switched/removed panes and ignored late read completions
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rapidly_switched_removed_panes_and_ignored_late_read_completions():
    sock = Path("/tmp/rapid.sock")
    inv = make_inventory(
        socket_path=sock,
        panes=[
            PaneSnapshot(pane_id="p-1", workspace_id="ws-1", tab_id="tab-1", agent="agent-1"),
            PaneSnapshot(pane_id="p-2", workspace_id="ws-1", tab_id="tab-1", agent="agent-2"),
        ],
        agents=[
            AgentSnapshot(pane_id="p-1", workspace_id="ws-1", agent="agent-1"),
            AgentSnapshot(pane_id="p-2", workspace_id="ws-1", agent="agent-2"),
        ],
    )
    client = FakeClient("rapid", sock)
    client.snapshot_data = inv.snapshot
    # Delay initial read so we can switch targets before it finishes
    client.read_pane_delay = 0.05

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,  # type: ignore
        subscription_factory=lambda p, s: FakeSubscription(p, s),
        refresh_min_interval=0.01,
        poll_interval=0.1,
    )

    t1 = WorkspaceTarget("rapid", sock, "ws-1", pane_id="p-1")
    t2 = WorkspaceTarget("rapid", sock, "ws-1", pane_id="p-2")

    # Select p-1, then quickly select p-2
    await controller.select(t1, [inv], enabled=True)
    await asyncio.sleep(0.01)
    await controller.select(t2, [inv], enabled=True)

    # Wait for reads to complete
    await asyncio.sleep(0.08)

    # The active lease must strictly be p-2, and late frames for p-1 must be discarded
    active_lease = controller._active_lease
    assert active_lease is not None
    assert active_lease.key[2] == "p-2"

    # Last frame published must belong to p-2
    assert frames[-1].target.pane_id == "p-2"

    await controller.close()


# -------------------------------------------------------------------------
# Test 9: Output Bounding (empty, huge, repeated)
# -------------------------------------------------------------------------


def test_bound_output_helper():
    assert bound_output("") == ""
    assert bound_output("abc\n") == "abc\n"

    # Line bounding: > 1000 lines
    lines = [f"line {i}\n" for i in range(1500)]
    huge_lines = "".join(lines)
    bounded = bound_output(huge_lines, max_lines=1000)
    assert len(bounded.splitlines()) == 1000
    assert bounded.startswith("line 500\n")
    assert bounded.endswith("line 1499\n")

    # Byte bounding: > 256 KiB
    huge_text = "x" * (300 * 1024)
    bounded_bytes = bound_output(huge_text, max_bytes=256 * 1024)
    assert len(bounded_bytes.encode("utf-8")) == 256 * 1024


@pytest.mark.asyncio
async def test_empty_huge_repeated_output():
    sock = Path("/tmp/output.sock")
    inv = make_inventory(socket_path=sock)
    client = FakeClient("output", sock)

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,  # type: ignore
        subscription_factory=lambda p, s: FakeSubscription(p, s),
        refresh_min_interval=0.01,
        poll_interval=0.05,
    )

    # Empty output
    client.read_pane_data = {"text": ""}
    target = WorkspaceTarget("output", sock, "ws-1", pane_id="p-1")
    await controller.select(target, [inv], enabled=True)
    await asyncio.sleep(0.02)
    assert frames[-1].text_ansi == ""

    # Invalid non-string text
    client.read_pane_data = {"text": 12345}
    controller._dirty_event.set()
    await asyncio.sleep(0.02)
    assert frames[-1].phase == "stale"
    assert "Invalid pane.read record" in frames[-1].error

    # A stale lease is revoked; recovery requires a new verified lease.
    previous_generation = controller._generation
    client.read_pane_data = {"text": "A\n" * 1500}
    await controller.reconcile([inv], enabled=True)
    await asyncio.sleep(0.02)
    assert controller._active_lease is not None
    assert controller._active_lease.generation > previous_generation
    assert len(frames[-1].text_ansi.splitlines()) == 1000

    await controller.close()


# -------------------------------------------------------------------------
# Test 10: Malformed and unrelated events are ignored
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_unrelated_events():
    sock = Path("/tmp/events.sock")
    inv = make_inventory(socket_path=sock)
    client = FakeClient("events", sock)
    fake_sub = FakeSubscription(sock, [])

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,  # type: ignore
        subscription_factory=lambda p, s: fake_sub,
        refresh_min_interval=0.01,
        poll_interval=0.5,
    )

    target = WorkspaceTarget("events", sock, "ws-1", pane_id="p-1")
    await controller.select(target, [inv], enabled=True)
    await asyncio.sleep(0.02)

    initial_read_count = len(client.read_pane_calls)

    # 1. Non-dict frame
    fake_sub.feed_event("not a dict")  # type: ignore
    # 2. Unrelated event
    fake_sub.feed_event({"event": "server.heartbeat", "data": {}})
    # 3. Event for different pane
    fake_sub.feed_event({"event": "pane.scroll_changed", "data": {"pane_id": "p-other"}})
    # 4. Event for foreign workspace
    fake_sub.feed_event(
        {
            "event": "pane.scroll_changed",
            "data": {"pane_id": "p-1", "workspace_id": "ws-other"},
        }
    )

    await asyncio.sleep(0.05)
    # Read calls must NOT have increased
    assert len(client.read_pane_calls) == initial_read_count

    # Now feed matching event
    fake_sub.feed_event(
        {
            "event": "pane.scroll_changed",
            "data": {"pane_id": "p-1", "workspace_id": "ws-1"},
        }
    )
    await asyncio.sleep(0.05)
    assert len(client.read_pane_calls) > initial_read_count

    await controller.close()


# -------------------------------------------------------------------------
# Test 11: Burst coalescing and no concurrent refreshes
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_burst_coalescing_and_no_concurrent_refreshes():
    sock = Path("/tmp/coalesce.sock")
    inv = make_inventory(socket_path=sock)
    client = FakeClient("coalesce", sock)
    fake_sub = FakeSubscription(sock, [])

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,  # type: ignore
        subscription_factory=lambda p, s: fake_sub,
        refresh_min_interval=0.05,  # 50ms min interval
        poll_interval=1.0,
    )

    target = WorkspaceTarget("coalesce", sock, "ws-1", pane_id="p-1")
    await controller.select(target, [inv], enabled=True)
    await asyncio.sleep(0.02)

    count_before = len(client.read_pane_calls)

    # Fire 10 events rapidly within 10ms
    for i in range(10):
        fake_sub.feed_event(
            {
                "event": "pane.scroll_changed",
                "data": {"pane_id": "p-1", "workspace_id": "ws-1", "seq": i},
            }
        )

    # Wait 80ms (enough for exactly one coalesced refresh interval)
    await asyncio.sleep(0.08)

    # Should have executed only 1 additional refresh
    count_after = len(client.read_pane_calls)
    assert count_after - count_before <= 2

    await controller.close()


# -------------------------------------------------------------------------
# Test 12: Coalesced acknowledgement events with real subscription helper
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coalesced_acknowledgement_events_real_subscription_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    sock_path = tmp_path / "mock_sub.sock"
    sock_path.touch()
    reader = asyncio.StreamReader()
    writer = unittest.mock.MagicMock()
    writer.closed = False

    def close_writer():
        writer.closed = True

    writer.close.side_effect = close_writer
    writer.wait_closed = unittest.mock.AsyncMock()
    writer.drain = unittest.mock.AsyncMock()
    writer.write = unittest.mock.MagicMock()

    async def _fake_open_unix_connection(path: str, **kwargs: Any):
        return reader, writer

    monkeypatch.setattr(asyncio, "open_unix_connection", _fake_open_unix_connection)
    monkeypatch.setattr(
        "herdr_master.herdr.subscribe.uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="1"))
    )

    inv = make_inventory(socket_path=sock_path)
    client = FakeClient("real_sub", sock_path)
    # Server snapshot reflects new agent status after transition
    client.snapshot_data = make_inventory(
        socket_path=sock_path,
        panes=[
            PaneSnapshot(
                pane_id="p-1",
                workspace_id="ws-1",
                tab_id="tab-1",
                agent="agent-1",
                terminal_id="term-1",
                agent_status="working",
            )
        ],
        agents=[
            AgentSnapshot(
                pane_id="p-1",
                workspace_id="ws-1",
                agent="agent-1",
                agent_status="working",
            )
        ],
    ).snapshot

    # Pre-feed acknowledgement + complete event into stream reader
    ack = {"id": "sub-1", "result": {"type": "subscription_started"}}
    ev = {
        "event": "pane.agent_status_changed",
        "data": {"pane_id": "p-1", "workspace_id": "ws-1", "agent_status": "working"},
    }
    reader.feed_data(
        json.dumps(ack).encode("utf-8") + b"\n" + json.dumps(ev).encode("utf-8") + b"\n"
    )

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,  # type: ignore
        refresh_min_interval=0.01,
        poll_interval=0.1,
    )

    target = WorkspaceTarget("real_sub", sock_path, "ws-1", pane_id="p-1")
    await controller.select(target, [inv], enabled=True)
    await asyncio.sleep(0.03)

    assert frames[-1].phase == "live"
    assert frames[-1].agent_status == "working"

    reader.feed_eof()
    await controller.close()


# -------------------------------------------------------------------------
# Test 13: Reconnect EOF/error fallback to degraded and resync
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_eof_error_fallback_resync():
    sock = Path("/tmp/reconnect.sock")
    inv = make_inventory(socket_path=sock)
    client = FakeClient("reconnect", sock)

    sub1 = FakeSubscription(sock, [])
    sub2 = FakeSubscription(sock, [])
    subs = [sub1, sub2]

    def sub_factory(p: Path, s: List[Dict[str, Any]]) -> Any:
        if subs:
            return subs.pop(0)
        return FakeSubscription(p, s)

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,  # type: ignore
        subscription_factory=sub_factory,
        refresh_min_interval=0.01,
        poll_interval=0.05,
        reconnect_backoff=0.02,
    )

    target = WorkspaceTarget("reconnect", sock, "ws-1", pane_id="p-1")
    await controller.select(target, [inv], enabled=True)
    await asyncio.sleep(0.02)

    assert frames[-1].phase == "live"

    # Feed EOF to disconnect stream
    sub1.feed_eof()
    await asyncio.sleep(0.03)

    # Enters degraded state while polling
    degraded_frames = [f for f in frames if f.phase == "degraded"]
    assert len(degraded_frames) >= 1

    # Reconnect triggers on sub2, resyncs and returns to live
    await asyncio.sleep(0.06)
    assert frames[-1].phase == "live"

    await controller.close()


# -------------------------------------------------------------------------
# Test 14: Close with outstanding reads
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_with_outstanding_reads():
    sock = Path("/tmp/close_test.sock")
    inv = make_inventory(socket_path=sock)
    client = FakeClient("close_test", sock)
    client.read_pane_delay = 0.5  # Long read

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,  # type: ignore
        subscription_factory=lambda p, s: FakeSubscription(p, s),
        refresh_min_interval=0.01,
        poll_interval=0.1,
    )

    target = WorkspaceTarget("close_test", sock, "ws-1", pane_id="p-1")
    asyncio.create_task(controller.select(target, [inv], enabled=True))
    await asyncio.sleep(0.02)

    # Close while read is pending
    await controller.close()
    frames_count_at_close = len(frames)

    # Wait and verify no new frames are published
    await asyncio.sleep(0.1)
    assert len(frames) == frames_count_at_close

    # Closing again is idempotent
    await controller.close()


# -------------------------------------------------------------------------
# Test 15: Demo zero-I/O
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_zero_io():
    sock = Path("/tmp/demo.sock")
    inv = make_inventory(socket_path=sock)
    client = FakeClient("demo", sock)

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,  # type: ignore
        subscription_factory=lambda p, s: FakeSubscription(p, s),
    )

    target = WorkspaceTarget("demo", sock, "ws-1", pane_id="p-1")
    # Selection with enabled=False (demo mode)
    await controller.select(target, [inv], enabled=False)
    await asyncio.sleep(0.02)

    # Must produce empty state with zero client calls
    assert len(client.snapshot_calls) == 0
    assert len(client.read_pane_calls) == 0
    assert frames[-1].phase == "empty"
    assert frames[-1].can_reply is False

    # Submission with dummy lease is rejected
    fake_lease = PaneLease(key=(str(sock), "ws-1", "p-1"), generation=1, terminal_id="term-1")
    res = await controller.submit(ReplyRequest(lease=fake_lease, text="hello"))
    assert res.outcome == "rejected"
    assert "demo" in res.message.lower()

    await controller.close()


# -------------------------------------------------------------------------
# Test 16: Duplicate submit guard
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_submit_guard():
    sock = Path("/tmp/dup.sock")
    inv = make_inventory(socket_path=sock)
    client = FakeClient("dup", sock)
    client.snapshot_data = inv.snapshot
    client.prompt_delay = 0.05  # Slow dispatch

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,  # type: ignore
        subscription_factory=lambda p, s: FakeSubscription(p, s),
        refresh_min_interval=0.01,
        poll_interval=0.1,
    )

    target = WorkspaceTarget("dup", sock, "ws-1", pane_id="p-1")
    await controller.select(target, [inv], enabled=True)
    await asyncio.sleep(0.02)

    lease = controller._active_lease
    assert lease is not None

    req1 = ReplyRequest(lease=lease, text="first send")
    req2 = ReplyRequest(lease=lease, text="second send concurrent")

    t1 = asyncio.create_task(controller.submit(req1))
    await asyncio.sleep(0.01)  # t1 enters submit and sets _send_in_flight

    res2 = await controller.submit(req2)
    assert res2.outcome == "rejected"
    assert "in flight" in res2.message

    res1 = await t1
    assert res1.outcome == "accepted"

    await controller.close()


# -------------------------------------------------------------------------
# Test 17: Vanished/offline target before send
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vanished_offline_target_before_send():
    sock = Path("/tmp/vanish.sock")
    inv = make_inventory(socket_path=sock)
    client = FakeClient("vanish", sock)
    client.snapshot_data = inv.snapshot

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,  # type: ignore
        subscription_factory=lambda p, s: FakeSubscription(p, s),
        refresh_min_interval=0.01,
        poll_interval=0.1,
    )

    target = WorkspaceTarget("vanish", sock, "ws-1", pane_id="p-1")
    await controller.select(target, [inv], enabled=True)
    await asyncio.sleep(0.02)

    lease = controller._active_lease
    assert lease is not None

    # Pane disappears in fresh snapshot
    client.snapshot_data = SessionSnapshot(
        protocol=22,
        workspaces=[WorkspaceSnapshot(workspace_id="ws-1")],
        panes=[],
        agents=[],
    )

    res = await controller.submit(ReplyRequest(lease=lease, text="hello"))
    assert res.outcome == "rejected"
    assert "Preflight validation failed" in res.message

    await controller.close()


# -------------------------------------------------------------------------
# Test 18: Exact-socket prompt with one attempt and no wait
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_socket_pane_prompt_one_attempt_no_wait():
    sock = Path("/tmp/prompt_exact.sock")
    inv = make_inventory(socket_path=sock)
    client = FakeClient("prompt_exact", sock)
    client.snapshot_data = inv.snapshot

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,  # type: ignore
        subscription_factory=lambda p, s: FakeSubscription(p, s),
        refresh_min_interval=0.01,
        poll_interval=0.1,
    )

    target = WorkspaceTarget("prompt_exact", sock, "ws-1", pane_id="p-1")
    await controller.select(target, [inv], enabled=True)
    await asyncio.sleep(0.02)

    lease = controller._active_lease
    assert lease is not None

    res = await controller.submit(ReplyRequest(lease=lease, text="exact prompt message"))
    assert res.outcome == "accepted"

    assert len(client.prompt_agent_calls) == 1
    call = client.prompt_agent_calls[0]
    assert call["target"] == "p-1"
    assert call["prompt"] == "exact prompt message"
    assert call["max_attempts"] == 1
    assert call["wait"] is None

    # Whitespace-only rejected
    res_empty = await controller.submit(ReplyRequest(lease=lease, text="   \n  \t "))
    assert res_empty.outcome == "rejected"
    assert "whitespace" in res_empty.message

    await controller.close()


# -------------------------------------------------------------------------
# Test 19: Not-ready and busy rejection
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_ready_busy_rejection():
    sock = Path("/tmp/busy.sock")
    inv = make_inventory(socket_path=sock)
    client = FakeClient("busy", sock)
    client.snapshot_data = inv.snapshot
    client.prompt_error = HerdrRPCError(code="agent_busy", message="Agent is currently busy")

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,  # type: ignore
        subscription_factory=lambda p, s: FakeSubscription(p, s),
        refresh_min_interval=0.01,
        poll_interval=0.1,
    )

    target = WorkspaceTarget("busy", sock, "ws-1", pane_id="p-1")
    await controller.select(target, [inv], enabled=True)
    await asyncio.sleep(0.02)

    lease = controller._active_lease
    assert lease is not None

    res = await controller.submit(ReplyRequest(lease=lease, text="hello"))
    assert res.outcome == "rejected"
    assert "Agent is currently busy" in res.message

    await controller.close()


# -------------------------------------------------------------------------
# Test 20: Timeout and uncertain delivery without replay
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_uncertain_delivery_no_replay():
    sock = Path("/tmp/timeout.sock")
    inv = make_inventory(socket_path=sock)
    client = FakeClient("timeout", sock)
    client.snapshot_data = inv.snapshot

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,  # type: ignore
        subscription_factory=lambda p, s: FakeSubscription(p, s),
        send_timeout=0.02,  # 20ms timeout
        refresh_min_interval=0.01,
        poll_interval=0.1,
    )

    target = WorkspaceTarget("timeout", sock, "ws-1", pane_id="p-1")
    await controller.select(target, [inv], enabled=True)
    await asyncio.sleep(0.02)

    lease = controller._active_lease
    assert lease is not None

    # Trigger timeout
    client.prompt_delay = 0.05
    res = await controller.submit(ReplyRequest(lease=lease, text="timeout test"))
    assert res.outcome == "uncertain"
    assert "timed out" in res.message.lower()

    # Trigger transport failure
    client.prompt_delay = 0.0
    client.prompt_error = HerdrConnectionError("Connection lost mid-RPC")
    res_conn = await controller.submit(ReplyRequest(lease=lease, text="transport fail"))
    assert res_conn.outcome == "uncertain"
    assert "transport" in res_conn.message.lower()

    await controller.close()


# -------------------------------------------------------------------------
# Test 21: Old-target completion never modifying a new target's draft
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_target_completion_never_modifying_new_target_draft():
    sock = Path("/tmp/stale_draft.sock")
    inv = make_inventory(
        socket_path=sock,
        panes=[
            PaneSnapshot(pane_id="p-1", workspace_id="ws-1", tab_id="tab-1", agent="agent-1"),
            PaneSnapshot(pane_id="p-2", workspace_id="ws-1", tab_id="tab-1", agent="agent-2"),
        ],
        agents=[
            AgentSnapshot(pane_id="p-1", workspace_id="ws-1", agent="agent-1"),
            AgentSnapshot(pane_id="p-2", workspace_id="ws-1", agent="agent-2"),
        ],
    )
    client = FakeClient("stale_draft", sock)
    client.snapshot_data = inv.snapshot
    client.prompt_delay = 0.05

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,  # type: ignore
        subscription_factory=lambda p, s: FakeSubscription(p, s),
        refresh_min_interval=0.01,
        poll_interval=0.1,
    )

    t1 = WorkspaceTarget("stale_draft", sock, "ws-1", pane_id="p-1")
    await controller.select(t1, [inv], enabled=True)
    await asyncio.sleep(0.02)

    lease_1 = controller._active_lease
    assert lease_1 is not None

    # Submit on p-1
    submit_task = asyncio.create_task(
        controller.submit(ReplyRequest(lease=lease_1, text="msg for p1"))
    )
    await asyncio.sleep(0.01)

    # In the meantime, operator selects p-2
    t2 = WorkspaceTarget("stale_draft", sock, "ws-1", pane_id="p-2")
    await controller.select(t2, [inv], enabled=True)

    result_1 = await submit_task
    # The result is strictly pinned to lease_1
    assert result_1.lease == lease_1
    assert result_1.outcome == "accepted"
    # Controller's active lease is lease_2
    assert controller._active_lease is not None
    assert controller._active_lease.key[2] == "p-2"

    await controller.close()


# -------------------------------------------------------------------------
# Test 22: Forbidden methods assertion
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_no_forbidden_methods_called():
    """Verify that focus, attach, launch, or send_keys are never called."""
    sock = Path("/tmp/forbidden.sock")
    inv = make_inventory(socket_path=sock)
    client = FakeClient("forbidden", sock)
    client.snapshot_data = inv.snapshot

    frames: List[PaneFrame] = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,  # type: ignore
        subscription_factory=lambda p, s: FakeSubscription(p, s),
        refresh_min_interval=0.01,
        poll_interval=0.1,
    )

    target = WorkspaceTarget("forbidden", sock, "ws-1", pane_id="p-1")
    await controller.select(target, [inv], enabled=True)
    await asyncio.sleep(0.02)

    lease = controller._active_lease
    assert lease is not None
    await controller.submit(ReplyRequest(lease=lease, text="test input"))
    await controller.reconcile([inv], enabled=True)
    await controller.close()

    assert not client.focus_workspace_called
    assert not client.focus_tab_called
    assert not client.focus_pane_called
    assert not client.send_keys_called
    assert not client.attach_called
    assert not client.launch_called
