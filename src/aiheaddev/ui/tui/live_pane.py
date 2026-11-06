"""Exact-target live-pane controller, validation, and reply boundary for Herdr Protocol 22.

This module provides a UI-independent controller for exactly one selected Herdr pane.
It handles exact socket-bound resolution, bounded snapshots, streaming event subscriptions,
burst coalescing, degraded polling fallback, selection epochs, and at-most-once prompt injection.

Security & Integrity Invariants:
- Never routes by display name or falls back to the primary socket.
- Rejects offline inventories, missing panes, duplicate raw IDs, and foreign tabs.
- Derives missing workspace ownership strictly from verified owning tabs.
- Enforces monotonically increasing selection epochs; late frames and completions are discarded.
- Restricts prompt injection to live, verified agents with single-attempt (max_attempts=1) RPCs.
- Synchronously guards against duplicate sends and rejects whitespace-only input.
- Distinguishes accepted (RPC succeeded), rejected (preflight/RPC error), and uncertain
  (timeout/transport failure/cancellation after dispatch) reply outcomes without automatic replay.
- Does not log output text or operator reply content.
- Never invokes focus, attach, launch, or send_keys.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Sequence,
    Tuple,
)

from herdr_master.herdr.client import HerdrClient
from herdr_master.herdr.discovery import SessionInventory
from herdr_master.herdr.protocol import parse_pane_read, parse_prompt_result
from herdr_master.herdr.snapshot import TabSnapshot
from herdr_master.herdr.socket import HerdrConnectionError, HerdrRPCError
from herdr_master.herdr.subscribe import HerdrSubscription
from aiheaddev.ui.tui.widgets.workspaces import WorkspaceTarget

logger = logging.getLogger(__name__)

PaneKey = Tuple[str, str, str]  # (str(socket_path), workspace_id, pane_id)
PanePhase = Literal["empty", "loading", "live", "degraded", "stale", "unavailable"]


@dataclass(frozen=True)
class PaneLease:
    """Unique authorization lease pinned to an exact pane identity and selection epoch."""

    key: PaneKey
    generation: int
    terminal_id: Optional[str]


@dataclass(frozen=True)
class ResolvedPane:
    """Verified pane resolution against fresh session inventory."""

    target: WorkspaceTarget
    terminal_id: Optional[str]
    is_agent: bool
    interactive_ready: Optional[bool]
    agent_status: str


@dataclass(frozen=True)
class PaneFrame:
    """Published output and status snapshot delivered to the UI layer."""

    lease: Optional[PaneLease]
    target: Optional[WorkspaceTarget]
    phase: PanePhase
    agent_status: str
    text_ansi: str
    error: Optional[str]
    can_reply: bool
    # Unlike can_reply, this is not disabled merely because a send is in flight.
    reply_eligible: Optional[bool] = None


@dataclass(frozen=True)
class ReplyRequest:
    """Operator request to send ordinary text to a verified agent pane."""

    lease: PaneLease
    text: str
    notify: bool = True


@dataclass(frozen=True)
class TerminalInput:
    """Direct terminal text or keystroke forwarding request for a live pane."""

    lease: PaneLease
    text: Optional[str] = None
    keys: Optional[List[str]] = None


@dataclass(frozen=True)
class ReplyResult:
    """Outcome of an at-most-once reply attempt."""

    lease: PaneLease
    outcome: Literal["accepted", "rejected", "uncertain"]
    message: str


class LiveTargetError(ValueError):
    """Raised when an inventory or target cannot be verified safely."""


class _ObsoleteWatcher(Exception):
    """An awaited operation belongs to a revoked or superseded lease."""


def bound_output(text: str, max_lines: int = 1000, max_bytes: int = 256 * 1024) -> str:
    """Bound terminal output to at most max_lines and max_bytes UTF-8.

    Keeps the latest lines and latest UTF-8 bytes without logging contents.
    """
    if not text:
        return ""

    lines = text.splitlines(keepends=True)
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        text = "".join(lines)

    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        sliced = encoded[-max_bytes:]
        text = sliced.decode("utf-8", errors="ignore")
    return text


def resolve_live_pane(
    target: WorkspaceTarget,
    inventories: Sequence[SessionInventory],
    *,
    require_agent: bool = False,
) -> ResolvedPane:
    """Resolve and verify exact socket/workspace/pane against fresh session inventories.

    Enforces strict ownership, rejects foreign/contradictory tabs and duplicate IDs.
    Derives missing workspace ownership strictly from verified owning tabs.
    Ignores cached tabs when fresh records establish a verified tab in the same workspace.
    """
    if not target.pane_id:
        raise LiveTargetError("Target has no pane_id")

    # Match exact socket path - never fallback to primary or route by name
    matching_inventories = [
        inv for inv in inventories if Path(inv.socket_path) == Path(target.socket_path)
    ]
    if not matching_inventories:
        raise LiveTargetError(f"Session socket '{target.socket_path}' not found in inventories")
    if len(matching_inventories) > 1:
        raise LiveTargetError(f"Ambiguous inventories for socket path '{target.socket_path}'")

    inventory = matching_inventories[0]
    if inventory.error is not None or inventory.snapshot is None:
        error_msg = inventory.error or "Snapshot unavailable"
        raise LiveTargetError(f"Session for socket '{target.socket_path}' is offline: {error_msg}")

    snapshot = inventory.snapshot

    # Validate workspace existence and uniqueness
    matching_workspaces = [w for w in snapshot.workspaces if w.workspace_id == target.workspace_id]
    if not matching_workspaces:
        raise LiveTargetError(f"Workspace '{target.workspace_id}' not found in snapshot")
    if len(matching_workspaces) > 1:
        raise LiveTargetError(f"Duplicate workspace id '{target.workspace_id}' in snapshot")

    # Require raw pane record for live view authorization
    matching_panes = [p for p in snapshot.panes if p.pane_id == target.pane_id]
    if not matching_panes:
        raise LiveTargetError(
            f"Raw pane '{target.pane_id}' not found; agent-only record cannot authorize pane"
        )
    if len(matching_panes) > 1:
        raise LiveTargetError(f"Duplicate pane id '{target.pane_id}' in snapshot.panes")
    raw_pane = matching_panes[0]

    # Check raw agent record if present
    matching_agents = [a for a in snapshot.agents if a.pane_id == target.pane_id]
    if len(matching_agents) > 1:
        raise LiveTargetError(f"Duplicate agent id '{target.pane_id}' in snapshot.agents")
    raw_agent = matching_agents[0] if matching_agents else None

    # Check contradictions between raw pane and raw agent
    if raw_pane.workspace_id and raw_pane.workspace_id != target.workspace_id:
        raise LiveTargetError(
            f"Raw pane workspace '{raw_pane.workspace_id}' "
            f"contradicts target '{target.workspace_id}'"
        )
    if raw_agent and raw_agent.workspace_id and raw_agent.workspace_id != target.workspace_id:
        raise LiveTargetError(
            f"Raw agent workspace '{raw_agent.workspace_id}' "
            f"contradicts target '{target.workspace_id}'"
        )
    if raw_agent and raw_pane.tab_id and raw_agent.tab_id and raw_pane.tab_id != raw_agent.tab_id:
        raise LiveTargetError(
            f"Contradictory tab between pane ({raw_pane.tab_id}) and agent ({raw_agent.tab_id})"
        )
    if raw_agent and raw_agent.terminal_id is not None:
        if raw_pane.terminal_id is None:
            raise LiveTargetError("Pane terminal identity is missing while agent identity is known")
        if raw_pane.terminal_id != raw_agent.terminal_id:
            raise LiveTargetError("Contradictory terminal identity between pane and agent")

    # Determine effective tab
    effective_tab_id = raw_pane.tab_id or (raw_agent.tab_id if raw_agent else None)
    owning_tab: Optional[TabSnapshot] = None
    if effective_tab_id is not None:
        matching_tabs = [t for t in snapshot.tabs if t.tab_id == effective_tab_id]
        if not matching_tabs:
            raise LiveTargetError(f"Pane references missing tab '{effective_tab_id}'")
        if len(matching_tabs) > 1:
            raise LiveTargetError(f"Duplicate tab id '{effective_tab_id}' in snapshot.tabs")
        owning_tab = matching_tabs[0]
        if owning_tab.workspace_id and owning_tab.workspace_id != target.workspace_id:
            raise LiveTargetError(
                f"Tab '{effective_tab_id}' belongs to foreign workspace '{owning_tab.workspace_id}'"
            )

    # Missing workspace ownership derivation
    if raw_pane.workspace_id is None:
        if owning_tab is None or owning_tab.workspace_id != target.workspace_id:
            raise LiveTargetError(
                f"Raw pane has no workspace_id and cannot derive from verified tab in "
                f"'{target.workspace_id}'"
            )

    # Determine agent nature and status
    is_agent = bool((raw_pane.agent and raw_pane.agent.strip()) or (raw_agent is not None))
    if require_agent and not is_agent:
        raise LiveTargetError(f"Pane '{target.pane_id}' is a terminal, not an agent")

    interactive_ready = raw_agent.interactive_ready if raw_agent else None
    if is_agent:
        raw_status = (
            raw_agent.agent_status
            if raw_agent and raw_agent.agent_status
            else raw_pane.agent_status
        )
        if raw_status in {"idle", "working", "blocked", "done", "unknown"}:
            agent_status = raw_status
        else:
            agent_status = "unknown"
    else:
        agent_status = "terminal"

    resolved_target = WorkspaceTarget(
        session_name=inventory.session_name,
        socket_path=inventory.socket_path,
        workspace_id=target.workspace_id,
        tab_id=effective_tab_id,
        pane_id=target.pane_id,
    )

    return ResolvedPane(
        target=resolved_target,
        terminal_id=raw_pane.terminal_id,
        is_agent=is_agent,
        interactive_ready=interactive_ready,
        agent_status=agent_status,
    )


class LivePaneController:
    """UI-independent controller for exactly one selected Herdr pane.

    Manages bounded snapshot polling, Protocol 22 event subscriptions, selection
    epochs, and safe at-most-once prompt injection.
    """

    def __init__(
        self,
        *,
        publish: Callable[[PaneFrame], None],
        client_factory: Optional[Callable[[str, Path], HerdrClient]] = None,
        subscription_factory: Optional[Callable[[Path, List[Dict[str, Any]]], Any]] = None,
        refresh_min_interval: float = 0.25,
        poll_interval: float = 3.0,
        rpc_timeout: float = 2.0,
        send_timeout: float = 10.0,
        reconnect_backoff: float = 1.0,
    ) -> None:
        self._publish_callback = publish
        self._client_factory = client_factory
        self._subscription_factory = subscription_factory
        self._refresh_min_interval = refresh_min_interval
        self._poll_interval = poll_interval
        self._rpc_timeout = rpc_timeout
        self._send_timeout = send_timeout
        self._reconnect_backoff = reconnect_backoff

        self._closed = False
        self._enabled = False
        self._generation = 0
        # Every applied eligibility observation fences older pending preflights.
        self._eligibility_revision = 0
        self._active_key: Optional[PaneKey] = None
        self._active_lease: Optional[PaneLease] = None
        self._active_target: Optional[WorkspaceTarget] = None
        self._active_terminal_id: Optional[str] = None
        self._is_agent = False
        self._interactive_ready: Optional[bool] = None
        self._last_agent_status = "unknown"
        self._last_text_ansi = ""
        self._current_phase: PanePhase = "empty"

        self._send_in_flight = False
        self._watch_task: Optional[asyncio.Task[None]] = None
        self._lifecycle_lock = asyncio.Lock()
        self._dirty_event: Optional[asyncio.Event] = None
        self._active_subscription: Any = None
        self._last_refresh_key: Optional[PaneKey] = None
        self._last_refresh_attempt = 0.0

    def _get_client(self, session_name: str, socket_path: Path) -> HerdrClient:
        if self._client_factory is not None:
            return self._client_factory(session_name, Path(socket_path))
        return HerdrClient(
            session=session_name,
            socket_path=Path(socket_path),
            timeout=self._rpc_timeout,
        )

    def _create_subscription(
        self, socket_path: Path, subscriptions: List[Dict[str, Any]]
    ) -> Any:
        if self._subscription_factory is not None:
            return self._subscription_factory(Path(socket_path), subscriptions)
        return HerdrSubscription(
            socket_path=Path(socket_path),
            subscriptions=subscriptions,
            timeout=self._rpc_timeout,
        )

    def _reply_eligible(self, phase: PanePhase) -> bool:
        return (
            not self._closed
            and self._enabled
            and self._active_lease is not None
            and phase in ("live", "degraded")
            and self._is_agent
            and self._interactive_ready is not False
        )

    def _compute_can_reply(self, phase: PanePhase) -> bool:
        return self._reply_eligible(phase) and not self._send_in_flight

    def _observe_eligibility(self, resolved: ResolvedPane) -> None:
        """Apply fresh evidence before any subsequent awaited pane read."""
        was_eligible = self._reply_eligible(self._current_phase)
        self._eligibility_revision += 1
        self._active_target = resolved.target
        self._is_agent = resolved.is_agent
        self._interactive_ready = resolved.interactive_ready
        self._last_agent_status = resolved.agent_status
        if was_eligible and not self._reply_eligible(self._current_phase):
            # Notify the app even during an in-flight send, whose can_reply was
            # already false for an unrelated reason.
            self._publish_current_state()

    @property
    def _desired_target(self) -> Optional[WorkspaceTarget]:
        return self._active_target

    @_desired_target.setter
    def _desired_target(self, value: Optional[WorkspaceTarget]) -> None:
        self._active_target = value

    @property
    def _epoch(self) -> int:
        return self._generation

    @_epoch.setter
    def _epoch(self, value: int) -> None:
        self._generation = value

    @property
    def _phase(self) -> PanePhase:
        return self._current_phase

    @_phase.setter
    def _phase(self, value: PanePhase) -> None:
        self._current_phase = value

    def _publish_frame(
        self,
        generation: int,
        lease: Optional[PaneLease],
        target: Optional[WorkspaceTarget],
        phase: PanePhase,
        agent_status: str,
        text_ansi: str,
        error: Optional[str],
        can_reply: bool,
    ) -> None:
        if self._closed:
            return
        if generation != self._generation:
            # Stale epoch frame discarded
            return
        self._current_phase = phase
        frame = PaneFrame(
            lease=lease,
            target=target,
            phase=phase,
            agent_status=agent_status,
            text_ansi=text_ansi,
            error=error,
            can_reply=can_reply,
            reply_eligible=lease is not None and lease == self._active_lease
            and self._reply_eligible(phase),
        )
        self._publish_callback(frame)

    def _publish_current_state(self) -> None:
        if self._closed:
            return
        can_reply = self._compute_can_reply(self._current_phase)
        frame = PaneFrame(
            lease=self._active_lease,
            target=self._active_target,
            phase=self._current_phase,
            agent_status=self._last_agent_status,
            text_ansi=self._last_text_ansi,
            error=None if self._current_phase in ("live", "degraded") else "Unavailable or error",
            can_reply=can_reply,
            reply_eligible=self._reply_eligible(self._current_phase),
        )
        self._publish_callback(frame)

    def _lease_is_current(self, lease: PaneLease) -> bool:
        return (
            not self._closed
            and self._generation == lease.generation
            and self._active_lease == lease
        )

    def _invalidate_lease(self, lease: PaneLease, *, phase: PanePhase, error: str) -> None:
        """Revoke authorization synchronously before publishing loss of trust."""
        if not self._lease_is_current(lease):
            return
        self._generation += 1
        self._eligibility_revision += 1
        self._active_lease = None
        if self._dirty_event is not None:
            self._dirty_event.set()
        self._publish_frame(
            generation=self._generation,
            lease=None,
            target=self._active_target,
            phase=phase,
            agent_status=self._last_agent_status,
            text_ansi=self._last_text_ansi,
            error=error,
            can_reply=False,
        )

    async def _verified_read(
        self, client: HerdrClient, lease: PaneLease, target: WorkspaceTarget
    ) -> PaneLease:
        """Resolve fresh ownership/incarnation before a bounded pane read."""
        snapshot = await asyncio.wait_for(client.get_snapshot(), timeout=self._rpc_timeout)
        if not self._lease_is_current(lease):
            raise _ObsoleteWatcher()
        inventory = SessionInventory(
            session_name=target.session_name,
            socket_path=Path(target.socket_path),
            snapshot=snapshot,
        )
        resolved = resolve_live_pane(target, [inventory])
        if resolved.target.tab_id is None:
            raise LiveTargetError("Verified pane tab identity is missing")
        known_terminal = (
            lease.terminal_id if lease.terminal_id is not None else self._active_terminal_id
        )
        if known_terminal is not None and resolved.terminal_id != known_terminal:
            raise LiveTargetError("Known terminal incarnation changed or is missing")
        upgraded = False
        if resolved.terminal_id is not None:
            # Preserve identity and fence old requests before the awaited read.
            self._active_terminal_id = resolved.terminal_id
            if lease.terminal_id is None:
                lease = PaneLease(lease.key, lease.generation, resolved.terminal_id)
                self._active_lease = lease
                upgraded = True
        self._observe_eligibility(resolved)
        if upgraded:
            self._publish_current_state()
        observation = self._eligibility_revision

        result = await asyncio.wait_for(
            client.read_pane(lease.key[2], format="ansi", lines=1000),
            timeout=self._rpc_timeout,
        )
        if not self._lease_is_current(lease):
            raise _ObsoleteWatcher()
        read = parse_pane_read(result)
        if (
            read.pane_id != lease.key[2]
            or read.workspace_id != lease.key[1]
            or read.tab_id != resolved.target.tab_id
            or read.source != "recent"
            or read.format != "ansi"
        ):
            raise LiveTargetError("pane.read response contradicts verified target")
        if observation != self._eligibility_revision:
            # Another verified observation won while the read was pending.
            # Never apply this older output under its newer target metadata.
            if self._dirty_event is not None:
                self._dirty_event.set()
            return lease
        self._last_text_ansi = bound_output(read.text)
        return lease

    async def _cancel_watcher(self) -> None:
        task = self._watch_task
        if task is not None:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                if asyncio.current_task() and asyncio.current_task().cancelling():
                    raise
            except Exception:
                pass
            if self._watch_task is not task:
                return
            self._watch_task = None

        subscription = self._active_subscription
        if subscription is not None:
            try:
                await subscription.close()
            except Exception:
                pass
            if self._active_subscription is subscription:
                self._active_subscription = None

    async def select(
        self,
        target: Optional[WorkspaceTarget],
        inventories: Sequence[SessionInventory],
        *,
        enabled: bool,
    ) -> None:
        """Select a target pane or clear selection, serializing watcher replacement."""
        async with self._lifecycle_lock:
            await self._select(target, inventories, enabled=enabled)

    async def _select(
        self,
        target: Optional[WorkspaceTarget],
        inventories: Sequence[SessionInventory],
        *,
        enabled: bool,
    ) -> None:
        """Apply a selection while holding the lifecycle lock."""
        if self._closed:
            return
        self._enabled = enabled

        if not enabled or target is None or target.pane_id is None:
            if self._active_lease is not None:
                self._invalidate_lease(
                    self._active_lease, phase="empty", error="Selection cleared"
                )
            self._active_key = None
            self._active_target = target
            self._active_terminal_id = None
            self._is_agent = False
            self._interactive_ready = None
            self._last_agent_status = "unknown"
            self._last_text_ansi = ""
            await self._cancel_watcher()
            if self._closed or self._active_key is not None:
                return
            self._publish_frame(
                generation=self._generation,
                lease=None,
                target=target,
                phase="empty",
                agent_status="unknown",
                text_ansi="",
                error=None,
                can_reply=False,
            )
            return

        key: PaneKey = (str(target.socket_path), target.workspace_id, target.pane_id)
        if self._active_key != key:
            if self._active_lease is not None:
                self._invalidate_lease(
                    self._active_lease, phase="stale", error="Selection changed"
                )
            # Clear the old key's output even when the new key is offline.
            self._active_key = key
            self._active_target = target
            self._active_terminal_id = None
            self._is_agent = False
            self._interactive_ready = None
            self._last_agent_status = "unknown"
            self._last_text_ansi = ""

        try:
            resolved = resolve_live_pane(target, inventories)
        except LiveTargetError as exc:
            matching_inv = next(
                (inv for inv in inventories if Path(inv.socket_path) == Path(target.socket_path)),
                None,
            )
            offline = (
                matching_inv is None
                or matching_inv.error is not None
                or matching_inv.snapshot is None
            )
            phase: PanePhase = "stale" if offline else "unavailable"
            if self._active_lease is not None:
                self._invalidate_lease(self._active_lease, phase=phase, error=str(exc))
            await self._cancel_watcher()
            if self._closed or self._active_key != key:
                return
            self._active_target = target
            self._publish_frame(
                generation=self._generation,
                lease=None,
                target=target,
                phase=phase,
                agent_status=self._last_agent_status,
                text_ansi=self._last_text_ansi,
                error=str(exc),
                can_reply=False,
            )
            return

        known_terminal = self._active_terminal_id
        if known_terminal is not None and resolved.terminal_id != known_terminal:
            error = "Known terminal incarnation changed or is missing"
            if self._active_lease is not None:
                self._invalidate_lease(self._active_lease, phase="stale", error=error)
            await self._cancel_watcher()
            if self._closed or self._active_key != key:
                return
            if resolved.terminal_id is None:
                # Do not downgrade a previously known incarnation to an unknown one.
                self._publish_frame(
                    generation=self._generation,
                    lease=None,
                    target=target,
                    phase="stale",
                    agent_status=self._last_agent_status,
                    text_ansi=self._last_text_ansi,
                    error=error,
                    can_reply=False,
                )
                return
            # A different, verified incarnation needs a new lease and fresh output.
            self._last_text_ansi = ""

        if (
            self._active_lease is not None
            and self._active_lease.terminal_id is None
            and resolved.terminal_id is not None
        ):
            # New incarnation evidence must be reflected in a new UI lease.
            self._invalidate_lease(
                self._active_lease, phase="stale", error="Terminal identity became known"
            )
            await self._cancel_watcher()
            if self._closed or self._active_key != key:
                return

        if (
            self._active_key == key
            and self._active_lease is not None
            and self._current_phase in ("live", "degraded", "loading")
        ):
            if resolved.terminal_id is not None:
                self._active_terminal_id = resolved.terminal_id
            self._observe_eligibility(resolved)
            if self._dirty_event is not None:
                self._dirty_event.set()
            return

        # Stop and await even a stale watcher before replacing shared resources.
        await self._cancel_watcher()
        if self._closed or self._active_key != key:
            return
        self._generation += 1
        gen = self._generation
        lease = PaneLease(key=key, generation=gen, terminal_id=resolved.terminal_id)
        self._active_lease = lease
        self._active_terminal_id = resolved.terminal_id
        self._observe_eligibility(resolved)
        self._publish_frame(
            generation=gen,
            lease=lease,
            target=resolved.target,
            phase="loading",
            agent_status=resolved.agent_status,
            text_ansi=self._last_text_ansi,
            error=None,
            can_reply=False,
        )
        self._dirty_event = asyncio.Event()
        self._watch_task = asyncio.create_task(
            self._run_watcher(lease, resolved.target, gen, self._dirty_event)
        )

    async def reconcile(
        self,
        inventories: Sequence[SessionInventory],
        *,
        enabled: bool,
    ) -> None:
        """Recheck the selected key, retiring an old watcher before recovery."""
        async with self._lifecycle_lock:
            if self._active_target is None:
                return
            await self._select(self._active_target, inventories, enabled=enabled)

    def _is_matching_event(
        self, event: Any, expected_pane_id: str, expected_workspace_id: Optional[str]
    ) -> bool:
        if not isinstance(event, dict):
            return False
        event_type = event.get("event") or event.get("type")
        if event_type not in ("pane.scroll_changed", "pane.agent_status_changed"):
            return False

        payload = event.get("data")
        if not isinstance(payload, dict):
            payload = event.get("params")
        if not isinstance(payload, dict):
            payload = event

        ev_pane = payload.get("pane_id")
        if ev_pane != expected_pane_id:
            return False

        ev_ws = payload.get("workspace_id")
        if (
            ev_ws is not None
            and expected_workspace_id is not None
            and ev_ws != expected_workspace_id
        ):
            return False

        return True

    def _extract_event_status(self, event: Any) -> Optional[str]:
        if not isinstance(event, dict):
            return None
        payload = event.get("data")
        if not isinstance(payload, dict):
            payload = event.get("params")
        if not isinstance(payload, dict):
            payload = event
        status = payload.get("agent_status")
        if status in {"idle", "working", "blocked", "done", "unknown"}:
            return str(status)
        return None

    async def _run_watcher(
        self,
        lease: PaneLease,
        target: WorkspaceTarget,
        generation: int,
        dirty_event: asyncio.Event,
    ) -> None:
        """Watch a single lease; every read follows fresh exact-target validation."""
        pane_id = target.pane_id
        if not pane_id:
            return

        client = self._get_client(target.session_name, Path(target.socket_path))
        subscriptions = [
            {"type": "pane.scroll_changed", "pane_id": pane_id},
            {"type": "pane.agent_status_changed", "pane_id": pane_id},
        ]
        sub: Any = None
        listener_task: Optional[asyncio.Task[None]] = None
        backoff = self._reconnect_backoff
        loop = asyncio.get_running_loop()
        next_sub_retry_time = 0.0

        async def close_subscription(subscription: Any) -> None:
            if subscription is None:
                return
            try:
                await subscription.close()
            except Exception:
                pass
            if self._active_subscription is subscription:
                self._active_subscription = None

        async def listen_events(subscription: Any) -> None:
            try:
                async for event in subscription.events():
                    if not self._lease_is_current(lease):
                        break
                    if self._is_matching_event(event, pane_id, target.workspace_id):
                        # Event payloads invalidate state; only a verified snapshot
                        # can supply current ownership, readiness, or status.
                        dirty_event.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                dirty_event.set()

        async def refresh(*, after_ack: bool = False, coalesce: bool = False) -> None:
            nonlocal lease
            # Retain the attempt time across same-key watcher recovery. The one
            # exception is the mandatory immediate post-ACK synchronization.
            if not after_ack and self._last_refresh_key == lease.key:
                delay = self._refresh_min_interval - (loop.time() - self._last_refresh_attempt)
                if delay > 0:
                    await asyncio.sleep(delay)
            if not self._lease_is_current(lease):
                raise _ObsoleteWatcher()
            if coalesce:
                dirty_event.clear()
            self._last_refresh_key = lease.key
            self._last_refresh_attempt = loop.time()
            lease = await self._verified_read(client, lease, target)

        def publish(phase: PanePhase) -> None:
            if not self._lease_is_current(lease):
                return
            self._publish_frame(
                generation=generation,
                lease=lease,
                target=self._active_target,
                phase=phase,
                agent_status=self._last_agent_status,
                text_ansi=self._last_text_ansi,
                error=None,
                can_reply=self._compute_can_reply(phase),
            )

        def revoke(exc: Exception) -> None:
            phase: PanePhase = "unavailable" if isinstance(exc, LiveTargetError) else "stale"
            # A snapshot can upgrade this watcher's lease with newly learned
            # terminal identity before its read fails. Revoke that upgraded
            # lease, but never touch a replacement watcher or selection.
            current = self._active_lease
            if (
                current is not None
                and current.generation == generation
                and current.key == lease.key
            ):
                self._invalidate_lease(current, phase=phase, error=str(exc))

        try:
            try:
                await refresh()
            except _ObsoleteWatcher:
                return
            except Exception as exc:
                revoke(exc)
                return

            # Subscription-connect failure alone is degraded, not proof that
            # the pane is offline. An acknowledged handoff, however, must
            # resnapshot and reread successfully before it can become live.
            try:
                sub = self._create_subscription(target.socket_path, subscriptions)
                self._active_subscription = sub
                await asyncio.wait_for(sub.connect(), timeout=self._rpc_timeout)
                if not self._lease_is_current(lease):
                    raise _ObsoleteWatcher()
            except _ObsoleteWatcher:
                return
            except Exception:
                await close_subscription(sub)
                sub = None
                if not self._lease_is_current(lease):
                    return
                phase: PanePhase = "degraded"
                next_sub_retry_time = loop.time() + backoff
            else:
                try:
                    await refresh(after_ack=True)
                except _ObsoleteWatcher:
                    return
                except Exception as exc:
                    revoke(exc)
                    return
                phase = "live"
                listener_task = asyncio.create_task(listen_events(sub))

            publish(phase)

            while self._lease_is_current(lease):
                now = loop.time()
                # Schedule from the preceding attempt, not from completion of
                # its snapshot/read. A slow RPC may overrun, but never overlap.
                until_poll = max(0.0, self._last_refresh_attempt + self._poll_interval - now)
                until_retry = max(0.0, next_sub_retry_time - now) if sub is None else float("inf")
                try:
                    await asyncio.wait_for(
                        dirty_event.wait(), timeout=min(until_poll, until_retry)
                    )
                except asyncio.TimeoutError:
                    pass
                if not self._lease_is_current(lease):
                    break

                if sub is not None and listener_task is not None and listener_task.done():
                    await close_subscription(sub)
                    sub = None
                    listener_task = None
                    if not self._lease_is_current(lease):
                        break
                    phase = "degraded"
                    backoff = self._reconnect_backoff
                    next_sub_retry_time = loop.time() + backoff
                    publish(phase)

                if sub is None and loop.time() >= next_sub_retry_time:
                    try:
                        sub = self._create_subscription(target.socket_path, subscriptions)
                        self._active_subscription = sub
                        await asyncio.wait_for(sub.connect(), timeout=self._rpc_timeout)
                        if not self._lease_is_current(lease):
                            raise _ObsoleteWatcher()
                    except _ObsoleteWatcher:
                        return
                    except Exception:
                        await close_subscription(sub)
                        sub = None
                        if not self._lease_is_current(lease):
                            break
                        backoff = min(backoff * 2.0, 5.0)
                        next_sub_retry_time = loop.time() + backoff
                    else:
                        # A fresh snapshot AND valid bounded reread close the
                        # acknowledgement gap on every reconnect.
                        try:
                            await refresh(after_ack=True)
                        except _ObsoleteWatcher:
                            return
                        except Exception as exc:
                            revoke(exc)
                            return
                        phase = "live"
                        backoff = self._reconnect_backoff
                        listener_task = asyncio.create_task(listen_events(sub))
                        publish(phase)
                        continue

                # A subscription retry alone need not trigger an early poll.
                if (
                    not dirty_event.is_set()
                    and loop.time() < self._last_refresh_attempt + self._poll_interval
                ):
                    continue
                # One dirty bit absorbs all events received before this attempt;
                # new events during the await request at most one later attempt.
                try:
                    await refresh(coalesce=True)
                except _ObsoleteWatcher:
                    break
                except Exception as exc:
                    revoke(exc)
                    break
                publish(phase)
        except asyncio.CancelledError:
            raise
        finally:
            if listener_task is not None and not listener_task.done():
                listener_task.cancel()
                try:
                    await listener_task
                except (asyncio.CancelledError, Exception):
                    pass
            await close_subscription(sub)

    async def submit(self, request: ReplyRequest) -> ReplyResult:
        """Submit an ordinary-text prompt to a live, verified agent pane.

        Ensures at-most-once delivery with preflight revalidation, single-attempt dispatch,
        and strict outcome classifications (accepted, rejected, uncertain).
        """
        # 1. Synchronously claim one-send guard before first await
        if self._send_in_flight:
            return ReplyResult(
                lease=request.lease,
                outcome="rejected",
                message="A send is already in flight",
            )
        self._send_in_flight = True

        try:
            self._publish_current_state()

            # Reject empty or whitespace-only input
            if not request.text or not request.text.strip():
                return ReplyResult(
                    lease=request.lease,
                    outcome="rejected",
                    message="Cannot send empty or whitespace-only message",
                )

            if self._closed:
                return ReplyResult(
                    lease=request.lease,
                    outcome="rejected",
                    message="Controller is closed",
                )

            if not self._enabled:
                return ReplyResult(
                    lease=request.lease,
                    outcome="rejected",
                    message="Live pane input is disabled in demo mode",
                )

            # Check that request lease is current
            if self._active_lease is None or self._active_lease != request.lease:
                return ReplyResult(
                    lease=request.lease,
                    outcome="rejected",
                    message="Lease is no longer active",
                )
            if self._generation != request.lease.generation:
                return ReplyResult(
                    lease=request.lease,
                    outcome="rejected",
                    message="Lease generation is obsolete",
                )

            if not self._is_agent:
                return ReplyResult(
                    lease=request.lease,
                    outcome="rejected",
                    message="Target is a terminal, not an agent",
                )

            if not self._reply_eligible(self._current_phase):
                return ReplyResult(
                    lease=request.lease,
                    outcome="rejected",
                    message="Target is not currently eligible for input",
                )

            # Preflight fresh snapshot from exact socket (two-second deadline).
            # Older results must never overwrite later watcher/inventory evidence.
            eligibility_revision = self._eligibility_revision
            socket_str, ws_id, pane_id = request.lease.key
            session_name = self._active_target.session_name if self._active_target else "default"
            client = self._get_client(session_name, Path(socket_str))

            try:
                snapshot = await asyncio.wait_for(
                    client.get_snapshot(), timeout=self._rpc_timeout
                )
            except Exception as exc:
                self._invalidate_lease(
                    request.lease, phase="stale", error=f"Preflight snapshot check failed: {exc}"
                )
                return ReplyResult(
                    lease=request.lease,
                    outcome="rejected",
                    message=f"Preflight snapshot check failed: {exc}",
                )

            if (
                not self._lease_is_current(request.lease)
                or eligibility_revision != self._eligibility_revision
            ):
                return ReplyResult(
                    lease=request.lease,
                    outcome="rejected",
                    message="Target eligibility changed during preflight",
                )

            # Revalidate ownership, agent presence/readiness, and known terminal_id
            temp_inventory = SessionInventory(
                session_name=session_name,
                socket_path=Path(socket_str),
                snapshot=snapshot,
            )
            preflight_target = WorkspaceTarget(
                session_name=session_name,
                socket_path=Path(socket_str),
                workspace_id=ws_id,
                pane_id=pane_id,
            )

            try:
                resolved = resolve_live_pane(
                    preflight_target, [temp_inventory], require_agent=True
                )
            except LiveTargetError as exc:
                self._invalidate_lease(
                    request.lease, phase="unavailable", error=f"Preflight validation failed: {exc}"
                )
                return ReplyResult(
                    lease=request.lease,
                    outcome="rejected",
                    message=f"Preflight validation failed: {exc}",
                )

            known_terminal = (
                request.lease.terminal_id
                if request.lease.terminal_id is not None
                else self._active_terminal_id
            )
            if known_terminal is not None and resolved.terminal_id != known_terminal:
                message = (
                    f"Terminal incarnation changed from '{known_terminal}' "
                    f"to '{resolved.terminal_id}'"
                )
                self._invalidate_lease(request.lease, phase="stale", error=message)
                return ReplyResult(lease=request.lease, outcome="rejected", message=message)

            if request.lease.terminal_id is None and resolved.terminal_id is not None:
                self._active_terminal_id = resolved.terminal_id
                message = "Terminal identity became known; acquire a new lease before sending"
                self._invalidate_lease(request.lease, phase="stale", error=message)
                return ReplyResult(lease=request.lease, outcome="rejected", message=message)

            if resolved.target.tab_id is None:
                self._invalidate_lease(
                    request.lease,
                    phase="unavailable",
                    error="Verified pane tab identity is missing",
                )
                return ReplyResult(
                    lease=request.lease, outcome="rejected", message="Verified pane tab is missing"
                )
            if resolved.terminal_id is not None:
                self._active_terminal_id = resolved.terminal_id
            self._observe_eligibility(resolved)
            if resolved.interactive_ready is False:
                return ReplyResult(
                    lease=request.lease,
                    outcome="rejected",
                    message="Agent is not interactively ready",
                )

            # Recheck authorization and epoch immediately before dispatch.
            if (
                not self._lease_is_current(request.lease)
                or not self._enabled
                or self._current_phase not in ("live", "degraded")
                or not self._is_agent
                or self._interactive_ready is False
            ):
                return ReplyResult(
                    lease=request.lease,
                    outcome="rejected",
                    message="Target changed before dispatch",
                )

            # Dispatch prompt_agent with wait omitted and max_attempts=1
            try:
                dispatch_coro = client.prompt_agent(
                    target=pane_id,
                    prompt=request.text,
                    max_attempts=1,
                )
                result = await asyncio.wait_for(dispatch_coro, timeout=self._send_timeout)
                agent = parse_prompt_result(result)
                if (
                    agent.pane_id != pane_id
                    or agent.workspace_id != ws_id
                    or agent.tab_id != resolved.target.tab_id
                    or (known_terminal is not None and agent.terminal_id != known_terminal)
                ):
                    raise HerdrConnectionError("agent.prompt response contradicts verified target")
                return ReplyResult(
                    lease=request.lease,
                    outcome="accepted",
                    message="Prompt accepted",
                )
            except HerdrRPCError as exc:
                # Explicit server rejection (agent_busy, agent_not_ready, etc.)
                return ReplyResult(
                    lease=request.lease,
                    outcome="rejected",
                    message=f"RPC rejected: {exc.message}",
                )
            except asyncio.TimeoutError:
                return ReplyResult(
                    lease=request.lease,
                    outcome="uncertain",
                    message="Send timed out. Inspect output before manually sending again.",
                )
            except (HerdrConnectionError, OSError) as exc:
                return ReplyResult(
                    lease=request.lease,
                    outcome="uncertain",
                    message=(
                        f"Transport error ({exc}). Inspect output before manually sending again."
                    ),
                )
            except asyncio.CancelledError:
                return ReplyResult(
                    lease=request.lease,
                    outcome="uncertain",
                    message=(
                        "Cancelled after dispatch. Inspect output before manually sending again."
                    ),
                )
            except Exception as exc:
                return ReplyResult(
                    lease=request.lease,
                    outcome="uncertain",
                    message=f"Send failure ({exc}). Inspect output before manually sending again.",
                )
        finally:
            self._send_in_flight = False
            self._publish_current_state()

    def _nudge_dirty(self) -> None:
        if not self._closed and self._dirty_event is not None:
            self._last_refresh_attempt = 0.0
            self._dirty_event.set()

    async def send_terminal_input(
        self,
        lease: PaneLease,
        text: Optional[str] = None,
        keys: Optional[List[str]] = None,
    ) -> bool:
        """Forward raw terminal text or keystrokes directly to a live pane.

        Supports all live panes (shells, idle agents, busy agents, prompt blocks)
        without requiring agent identity or interactive readiness.
        """
        if self._closed or not self._enabled:
            return False
        if (
            self._active_lease is None
            or self._active_lease.key != lease.key
            or self._active_lease.generation != lease.generation
            or self._epoch != lease.generation
            or (
                lease.terminal_id is not None
                and self._active_lease.terminal_id != lease.terminal_id
            )
        ):
            return False
        if self._phase not in ("live", "degraded"):
            return False
        if not text and not keys:
            return False

        session_name = (
            self._desired_target.session_name
            if self._desired_target and self._desired_target.session_name
            else "default"
        )
        socket_path = Path(lease.key[0])
        pane_id = lease.key[2]

        if self._client_factory is not None:
            try:
                client = self._client_factory(session_name, socket_path)
            except TypeError:
                client = self._client_factory(socket_path, session_name)  # type: ignore[call-arg]
        else:
            client = self._get_client(session_name, socket_path)

        text_arg = text if text else None
        keys_arg = keys if keys else None

        try:
            if isinstance(client, HerdrClient):
                if text_arg is not None:
                    await asyncio.wait_for(
                        client.pane_send_text(pane_id=pane_id, text=text_arg),
                        timeout=self._send_timeout,
                    )
                if text_arg is not None and keys_arg is not None:
                    await asyncio.sleep(0.02)
                if keys_arg is not None:
                    await asyncio.wait_for(
                        client.pane_send_keys(pane_id=pane_id, keys=keys_arg),
                        timeout=self._send_timeout,
                    )
            elif hasattr(client, "pane_send_input") and callable(client.pane_send_input):
                res = client.pane_send_input(pane_id=pane_id, text=text_arg, keys=keys_arg)
                if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                    await asyncio.wait_for(res, timeout=self._send_timeout)
            elif hasattr(client, "pane_send_text") or hasattr(client, "pane_send_keys"):
                if text_arg and callable(getattr(client, "pane_send_text", None)):
                    res = client.pane_send_text(pane_id=pane_id, text=text_arg)
                    if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                        await asyncio.wait_for(res, timeout=self._send_timeout)
                if keys_arg and callable(getattr(client, "pane_send_keys", None)):
                    res = client.pane_send_keys(pane_id=pane_id, keys=keys_arg)
                    if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                        await asyncio.wait_for(res, timeout=self._send_timeout)
            elif hasattr(client, "_rpc") and callable(client._rpc):
                params: Dict[str, Any] = {"pane_id": pane_id}
                if text_arg is not None:
                    params["text"] = text_arg
                if keys_arg is not None:
                    params["keys"] = [keys_arg] if isinstance(keys_arg, str) else list(keys_arg)
                res = client._rpc("pane.send_input", params)
                if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                    await asyncio.wait_for(res, timeout=self._send_timeout)
            else:
                return False
        except (HerdrRPCError, HerdrConnectionError, asyncio.TimeoutError, OSError) as exc:
            logger.warning("Failed to send terminal input to pane %s: %s", pane_id, exc)
            return False
        except Exception as exc:
            logger.warning("Unexpected error sending terminal input to pane %s: %s", pane_id, exc)
            return False

        self._nudge_dirty()
        try:
            loop = asyncio.get_running_loop()
            loop.call_later(0.08, self._nudge_dirty)
            loop.call_later(0.25, self._nudge_dirty)
        except RuntimeError:
            pass
        return True

    async def close(self) -> None:
        """Close the controller idempotently and suppress further publishes."""
        if self._closed:
            return
        self._closed = True
        async with self._lifecycle_lock:
            await self._cancel_watcher()
            self._active_lease = None
            self._active_key = None
