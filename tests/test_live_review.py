"""Deterministic regressions for live-pane review findings 1–8."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from protocol22_fixtures import agent_prompted
from test_live_pane import FakeClient, FakeSubscription, make_inventory

from herdr_master.herdr.client import HerdrClient
from herdr_master.herdr.discovery import SessionInventory
from aiheaddev.ui.tui.live_pane import LivePaneController, ReplyRequest
from aiheaddev.core.workspace_targets import WorkspaceTarget


async def wait_until(predicate, timeout=2.0):
    """Wait for an explicit state change rather than guessing scheduler timing."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


def changed_inventory(inventory, snapshot):
    return SessionInventory(
        session_name=inventory.session_name, socket_path=inventory.socket_path, snapshot=snapshot
    )


def invalid_snapshot(snapshot, kind):
    snapshot = snapshot.model_copy(deep=True)
    if kind == "foreign-pane":
        snapshot.panes[0].workspace_id = "another-workspace"
    elif kind == "missing-tab":
        snapshot.tabs = []
    elif kind == "foreign-tab":
        snapshot.tabs[0].workspace_id = "another-workspace"
    elif kind == "foreign-agent":
        snapshot.agents[0].workspace_id = "another-workspace"
    elif kind == "conflicting-agent-tab":
        snapshot.agents[0].tab_id = "another-tab"
    elif kind == "missing-pane":
        snapshot.panes = []
    elif kind == "duplicate-workspace":
        snapshot.workspaces.append(snapshot.workspaces[0].model_copy(deep=True))
    elif kind == "duplicate-tab":
        snapshot.tabs.append(snapshot.tabs[0].model_copy(deep=True))
    elif kind == "duplicate-pane":
        snapshot.panes.append(snapshot.panes[0].model_copy(deep=True))
    elif kind == "duplicate-agent":
        snapshot.agents.append(snapshot.agents[0].model_copy(deep=True))
    elif kind == "missing-terminal":
        snapshot.panes[0].terminal_id = None
    elif kind == "reused-terminal":
        snapshot.panes[0].terminal_id = "term-replacement"
    elif kind == "contradictory-agent-terminal":
        snapshot.panes[0].terminal_id = "term-pane"
        snapshot.agents[0].terminal_id = "term-agent"
    else:
        raise AssertionError(kind)
    return snapshot


def make_controller(client, frames, *, subscription_factory=None, interval=0.01):
    subscriptions = []

    def default_factory(path, filters):
        sub = FakeSubscription(path, filters)
        subscriptions.append(sub)
        return sub

    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda session, path: client,
        subscription_factory=subscription_factory or default_factory,
        refresh_min_interval=interval,
        poll_interval=30,
        reconnect_backoff=0.01,
    )
    return controller, subscriptions


async def select_live(controller, frames, inventory):
    target = WorkspaceTarget(inventory.session_name, inventory.socket_path, "ws-1", pane_id="p-1")
    await controller.select(target, [inventory], enabled=True)
    await wait_until(lambda: frames and frames[-1].phase == "live")
    assert frames[-1].lease is not None and frames[-1].can_reply
    return target, frames[-1].lease


@pytest.mark.asyncio
async def test_reconcile_without_selection_does_not_publish_or_open_resources(tmp_path):
    inventory = make_inventory(socket_path=tmp_path / "empty.sock")
    client = FakeClient(socket_path=inventory.socket_path)
    frames = []
    controller, subs = make_controller(client, frames)
    try:
        await controller.reconcile([inventory], enabled=True)
        assert frames == [] and subs == [] and controller._watch_task is None
    finally:
        await controller.close()


class HeldPreflightClient(FakeClient):
    def __init__(self, socket_path):
        super().__init__(socket_path=socket_path)
        self.hold_next = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def get_snapshot(self):
        if self.hold_next:
            self.hold_next = False
            captured = self.snapshot_data
            self.entered.set()
            await self.release.wait()
            return captured
        return await super().get_snapshot()


@pytest.mark.asyncio
async def test_revoked_watcher_cannot_dispatch_a_held_valid_preflight(tmp_path):
    """Finding 1: stale is an epoch revocation, not only a display state."""
    inventory = make_inventory(socket_path=tmp_path / "held.sock")
    client = HeldPreflightClient(inventory.socket_path)
    client.snapshot_data = inventory.snapshot
    frames = []
    controller, _ = make_controller(client, frames)
    try:
        _, lease = await select_live(controller, frames, inventory)
        client.hold_next = True
        pending = asyncio.create_task(controller.submit(ReplyRequest(lease, "original draft")))
        await asyncio.wait_for(client.entered.wait(), 2)
        client.read_pane_error = OSError("watch read failed")
        controller._dirty_event.set()
        await wait_until(lambda: frames[-1].phase == "stale")
        assert frames[-1].lease is None and controller._active_lease is None
        client.release.set()
        assert (await pending).outcome == "rejected"
        assert client.prompt_agent_calls == []
        client.read_pane_error = None
        await controller.reconcile([inventory], enabled=True)
        await wait_until(lambda: frames[-1].phase == "live")
        assert frames[-1].lease != lease
        assert (await controller.submit(ReplyRequest(lease, "old draft"))).outcome == "rejected"
        assert client.prompt_agent_calls == []
    finally:
        client.release.set()
        await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("disqualification", ["not-ready", "terminal"])
async def test_delayed_preflight_rejected_on_newer_watcher_evidence(tmp_path, disqualification):
    """Finding 2: preflight cannot dispatch if watcher observes not-ready or terminal."""
    inventory = make_inventory(socket_path=tmp_path / "gated_preflight.sock")
    client = HeldPreflightClient(inventory.socket_path)
    client.snapshot_data = inventory.snapshot
    frames = []
    controller, _ = make_controller(client, frames)
    try:
        _, lease = await select_live(controller, frames, inventory)
        client.hold_next = True
        pending = asyncio.create_task(controller.submit(ReplyRequest(lease, "original draft")))
        await asyncio.wait_for(client.entered.wait(), 2)
        # Watcher observes disqualifying evidence while preflight is held
        updated_snap = inventory.snapshot.model_copy(deep=True)
        if disqualification == "not-ready":
            updated_snap.agents[0].interactive_ready = False
            updated_snap.agents[0].agent_status = "blocked"
        else:
            updated_snap.panes[0].agent = None
            updated_snap.agents.clear()
        client.snapshot_data = updated_snap
        controller._dirty_event.set()
        if disqualification == "not-ready":
            await wait_until(lambda: frames[-1].agent_status == "blocked")
        else:
            await wait_until(lambda: frames[-1].agent_status == "terminal")
        # Release the held preflight snapshot (which still contains older valid evidence)
        client.release.set()
        res = await pending
        assert res.outcome == "rejected"
        assert "eligibility changed" in res.message.lower()
        assert client.prompt_agent_calls == []
    finally:
        client.release.set()
        await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["reused-terminal", "missing-terminal", "foreign-pane", "contradictory-agent-terminal"],
)
async def test_contradictory_preflight_revokes_lease_even_if_old_snapshot_returns(tmp_path, kind):
    """Findings 1–2: contradictory or missing evidence cannot revive the old draft."""
    inventory = make_inventory(socket_path=tmp_path / "preflight.sock")
    client = FakeClient(socket_path=inventory.socket_path)
    client.snapshot_data = inventory.snapshot
    frames = []
    controller, _ = make_controller(client, frames)
    try:
        _, lease = await select_live(controller, frames, inventory)
        client.snapshot_data = invalid_snapshot(inventory.snapshot, kind)
        result = await controller.submit(ReplyRequest(lease, "do not send"))
        assert result.outcome == "rejected"
        assert controller._active_lease is None
        assert frames[-1].lease is None and not frames[-1].can_reply
        client.snapshot_data = inventory.snapshot
        assert (await controller.submit(ReplyRequest(lease, "old draft"))).outcome == "rejected"
        assert client.prompt_agent_calls == []
        await controller.reconcile([inventory], enabled=True)
        await wait_until(lambda: frames[-1].phase == "live")
        assert frames[-1].lease != lease
    finally:
        await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["periodic", "reconcile"])
async def test_known_terminal_missing_revokes_on_refresh_and_reconcile(tmp_path, path):
    """Finding 2: a known ID is never overwritten by missing evidence."""
    inventory = make_inventory(socket_path=tmp_path / "terminal.sock")
    client = FakeClient(socket_path=inventory.socket_path)
    client.snapshot_data = inventory.snapshot
    frames = []
    controller, _ = make_controller(client, frames)
    try:
        _, lease = await select_live(controller, frames, inventory)
        missing = invalid_snapshot(inventory.snapshot, "missing-terminal")
        if path == "periodic":
            client.snapshot_data = missing
            controller._dirty_event.set()
        else:
            await controller.reconcile([changed_inventory(inventory, missing)], enabled=True)
        await wait_until(lambda: frames[-1].lease is None)
        assert frames[-1].phase in ("stale", "unavailable")
        assert controller._active_terminal_id == "term-1"
        assert (await controller.submit(ReplyRequest(lease, "old"))).outcome == "rejected"
        assert client.prompt_agent_calls == []
    finally:
        await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["initial", "periodic"])
@pytest.mark.parametrize(
    "kind",
    ["foreign-pane", "missing-tab", "foreign-tab", "foreign-agent", "conflicting-agent-tab",
     "missing-pane", "duplicate-workspace", "duplicate-tab", "duplicate-pane", "duplicate-agent"],
)
async def test_every_fresh_snapshot_verifies_ownership_before_read(tmp_path, when, kind):
    """Finding 3: no read or live frame for foreign, missing, or duplicate ownership."""
    inventory = make_inventory(socket_path=tmp_path / "ownership.sock")
    client = FakeClient(socket_path=inventory.socket_path)
    client.snapshot_data = inventory.snapshot
    frames = []
    controller, _ = make_controller(client, frames)
    try:
        target = WorkspaceTarget(
            inventory.session_name, inventory.socket_path, "ws-1", pane_id="p-1"
        )
        if when == "initial":
            client.snapshot_data = invalid_snapshot(inventory.snapshot, kind)
            await controller.select(target, [inventory], enabled=True)
            before = 0
        else:
            await select_live(controller, frames, inventory)
            before = len(client.read_pane_calls)
            client.snapshot_data = invalid_snapshot(inventory.snapshot, kind)
            client.read_pane_data = {"text": "FOREIGN OUTPUT"}
            controller._dirty_event.set()
        await wait_until(lambda: frames[-1].phase == "unavailable")
        assert len(client.read_pane_calls) == before
        assert frames[-1].lease is None and not frames[-1].can_reply
        assert all("FOREIGN OUTPUT" not in frame.text_ansi for frame in frames)
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_fresh_snapshot_controls_status_and_reply_readiness(tmp_path):
    inventory = make_inventory(socket_path=tmp_path / "readiness.sock")
    client = FakeClient(socket_path=inventory.socket_path)
    client.snapshot_data = inventory.snapshot
    frames = []
    controller, _ = make_controller(client, frames)
    try:
        await select_live(controller, frames, inventory)
        changed = inventory.snapshot.model_copy(deep=True)
        changed.agents[0].interactive_ready = False
        changed.agents[0].agent_status = "blocked"
        client.snapshot_data = changed
        controller._dirty_event.set()
        await wait_until(lambda: frames[-1].agent_status == "blocked")
        assert frames[-1].phase == "live" and not frames[-1].can_reply
    finally:
        await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("handoff", ["initial", "reconnect"])
@pytest.mark.parametrize("kind", ["reused-terminal", "foreign-pane", "malformed-read"])
async def test_acknowledged_handoff_resnapshots_and_requires_valid_read(tmp_path, handoff, kind):
    """Finding 4: a replacement or malformed reread cannot enter live after ACK."""
    inventory = make_inventory(socket_path=tmp_path / "handoff.sock")
    client = FakeClient(socket_path=inventory.socket_path)
    client.snapshot_data = inventory.snapshot
    client.read_pane_data = {"text": "ORIGINAL"}
    frames, subs = [], []

    class ChangingSubscription(FakeSubscription):
        async def connect(self):
            response = await super().connect()
            if len(subs) == (1 if handoff == "initial" else 2):
                self.reads_before_mutation = len(client.read_pane_calls)
                if kind == "malformed-read":
                    client.read_pane_data = {"text": None}
                else:
                    client.snapshot_data = invalid_snapshot(inventory.snapshot, kind)
                    client.read_pane_data = {"text": "REPLACEMENT OUTPUT"}
            return response

    def factory(path, filters):
        sub = ChangingSubscription(path, filters)
        subs.append(sub)
        return sub

    controller, _ = make_controller(client, frames, subscription_factory=factory)
    try:
        target = WorkspaceTarget(
            inventory.session_name, inventory.socket_path, "ws-1", pane_id="p-1"
        )
        await controller.select(target, [inventory], enabled=True)
        if handoff == "reconnect":
            await wait_until(lambda: frames[-1].phase == "live")
            start = len(frames)
            subs[0].feed_eof()
        else:
            start = 0
        await wait_until(lambda: frames[-1].phase in ("stale", "unavailable"))
        assert len(subs) == (1 if handoff == "initial" else 2)
        assert frames[-1].lease is None and not frames[-1].can_reply
        assert not any(frame.phase == "live" for frame in frames[start:])
        assert not any("REPLACEMENT OUTPUT" in frame.text_ansi for frame in frames)
        # Only a verified ownership may reach the corresponding pane read.
        assert len(client.read_pane_calls) == subs[-1].reads_before_mutation + (
            1 if kind == "malformed-read" else 0
        )
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_old_watcher_is_awaited_before_recovery_and_cannot_clear_new_resources(tmp_path):
    """Finding 7: a cancellation-resistant late read cannot sabotage a new lease."""
    inventory = make_inventory(socket_path=tmp_path / "old-watcher.sock")

    class SlowOldClient(FakeClient):
        def __init__(self):
            super().__init__(socket_path=inventory.socket_path)
            self.block = False
            self.entered = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.release = asyncio.Event()

        async def read_pane(self, *args, **kwargs):
            if self.block:
                self.block = False
                self.entered.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    await self.release.wait()
            return await super().read_pane(*args, **kwargs)

    client = SlowOldClient()
    client.snapshot_data = inventory.snapshot
    frames = []
    controller, subs = make_controller(client, frames)
    try:
        _, lease = await select_live(controller, frames, inventory)
        old_task, old_sub = controller._watch_task, subs[0]
        client.block = True
        controller._dirty_event.set()
        await asyncio.wait_for(client.entered.wait(), 2)
        client.snapshot_data = invalid_snapshot(inventory.snapshot, "reused-terminal")
        assert (await controller.submit(ReplyRequest(lease, "old"))).outcome == "rejected"
        client.snapshot_data = inventory.snapshot
        frame_count = len(frames)
        recovery = asyncio.create_task(controller.reconcile([inventory], enabled=True))
        await asyncio.wait_for(client.cancelled.wait(), 2)
        concurrent_recovery = asyncio.create_task(controller.reconcile([inventory], enabled=True))
        await asyncio.sleep(0)
        assert not recovery.done() and not concurrent_recovery.done()
        assert controller._watch_task is old_task
        assert controller._active_lease is None and controller._active_subscription is old_sub
        client.release.set()
        await asyncio.gather(recovery, concurrent_recovery)
        await wait_until(lambda: frames[-1].phase == "live")
        assert sum(frame.phase == "loading" for frame in frames[frame_count:]) == 1
        assert old_sub.closed and len(subs) == 2
        assert controller._active_subscription is subs[1]
        assert controller._active_lease == frames[-1].lease != lease
        assert not subs[1].closed and client.prompt_agent_calls == []
    finally:
        client.release.set()
        await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["snapshot", "read"])
async def test_persistent_failures_and_event_storms_are_attempt_throttled(tmp_path, failure):
    """Finding 8: failed attempts are bounded even across explicit recovery."""
    inventory = make_inventory(socket_path=tmp_path / "storm.sock")

    class CountClient(FakeClient):
        def __init__(self):
            super().__init__(socket_path=inventory.socket_path)
            self.in_flight = 0
            self.max_in_flight = 0

        async def get_snapshot(self):
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                await asyncio.sleep(0)
                return await super().get_snapshot()
            finally:
                self.in_flight -= 1

        async def read_pane(self, *args, **kwargs):
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                await asyncio.sleep(0)
                return await super().read_pane(*args, **kwargs)
            finally:
                self.in_flight -= 1

    client = CountClient()
    client.snapshot_data = inventory.snapshot
    frames = []
    controller, subs = make_controller(client, frames, interval=0.05)
    try:
        await select_live(controller, frames, inventory)
        calls = client.snapshot_calls if failure == "snapshot" else client.read_pane_calls
        start = len(calls)
        if failure == "snapshot":
            client.snapshot_error = OSError("persistent snapshot failure")
        else:
            client.read_pane_error = OSError("persistent read failure")
        for attempt in range(3):
            if attempt:
                await controller.reconcile([inventory], enabled=True)
            for _ in range(50):
                controller._dirty_event.set()
                subs[0].feed_event({"event": "pane.scroll_changed", "data": {"pane_id": "p-1"}})
            await wait_until(lambda: frames[-1].lease is None)
        times = [call["time"] for call in calls[start:]]
        assert len(times) == 3
        assert all(b - a >= 0.045 for a, b in zip(times, times[1:], strict=False))
        assert client.max_in_flight == 1
    finally:
        await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_kind", "outcome"),
    [
        ("empty", "uncertain"),
        ("wrong-id", "uncertain"),
        ("wrong-error-id", "uncertain"),
        ("missing-result", "uncertain"),
        ("malformed-error", "uncertain"),
        ("conflicting", "uncertain"),
        ("null-result", "uncertain"),
        ("empty-result", "uncertain"),
        ("wrong-tag", "uncertain"),
        ("contradictory-agent", "uncertain"),
        ("success", "accepted"),
        ("error", "rejected"),
    ],
)
async def test_real_socket_response_envelope_and_at_most_once(tmp_path, response_kind, outcome):
    """Finding 6: only a matching valid response can accept or reject a prompt."""
    socket_path = tmp_path / "prompt.sock"
    requests = []

    async def handle(reader, writer):
        request = json.loads(await reader.readline())
        requests.append(request)
        response = {
            "empty": {},
            "wrong-id": {"id": "another-request", "result": {"status": "ok"}},
            "wrong-error-id": {
                "id": "another-request", "error": {"code": "busy", "message": "busy"}
            },
            "missing-result": {"id": request["id"]},
            "malformed-error": {"id": request["id"], "error": {"message": "bad"}},
            "conflicting": {"id": request["id"], "result": {"ok": True},
                            "error": {"code": "agent_busy", "message": "busy"}},
            "null-result": {"id": request["id"], "result": None},
            "empty-result": {"id": request["id"], "result": {}},
            "wrong-tag": {"id": request["id"], "result": {"type": "wrong_tag"}},
            "contradictory-agent": {
                "id": request["id"],
                "result": agent_prompted(pane_id="wrong-pane"),
            },
            "success": {"id": request["id"], "result": agent_prompted()},
            "error": {"id": request["id"], "error": {"code": "agent_busy", "message": "busy"}},
        }[response_kind]
        writer.write(json.dumps(response).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(socket_path))
    inventory = make_inventory(socket_path=socket_path)
    fake = FakeClient(socket_path=socket_path)
    fake.snapshot_data = inventory.snapshot
    fake.prompt_agent = HerdrClient(socket_path=socket_path).prompt_agent
    frames = []
    controller, _ = make_controller(fake, frames)
    try:
        _, lease = await select_live(controller, frames, inventory)
        result = await controller.submit(ReplyRequest(lease, "only once"))
        assert result.outcome == outcome
        assert len(requests) == 1
        assert requests[0]["method"] == "agent.prompt"
        assert requests[0]["params"] == {"target": "p-1", "text": "only once"}
        if outcome == "uncertain":
            assert "Inspect output" in result.message
    finally:
        await controller.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_newly_known_terminal_id_is_exposed_in_lease_and_remains_required(tmp_path):
    inventory = make_inventory(socket_path=tmp_path / "newly-known.sock")
    unknown = inventory.model_copy(deep=True)
    unknown.snapshot.panes[0].terminal_id = None
    client = FakeClient(socket_path=inventory.socket_path)
    client.snapshot_data = unknown.snapshot
    frames = []
    controller, _ = make_controller(client, frames)
    try:
        _, original = await select_live(controller, frames, unknown)
        assert original.terminal_id is None
        client.snapshot_data = inventory.snapshot
        controller._dirty_event.set()
        await wait_until(lambda: frames[-1].lease and frames[-1].lease.terminal_id == "term-1")
        assert frames[-1].lease != original
        result = await controller.submit(ReplyRequest(original, "old identity"))
        assert result.outcome == "rejected"
        client.snapshot_data = unknown.snapshot
        controller._dirty_event.set()
        await wait_until(lambda: frames[-1].lease is None)
        assert client.prompt_agent_calls == []
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_new_identity_survives_following_failed_read_and_blocks_unknown_recovery(tmp_path):
    inventory = make_inventory(socket_path=tmp_path / "known-before-failure.sock")
    unknown = inventory.model_copy(deep=True)
    unknown.snapshot.panes[0].terminal_id = None
    client = FakeClient(socket_path=inventory.socket_path)
    client.snapshot_data = unknown.snapshot
    frames = []
    controller, _ = make_controller(client, frames)
    try:
        _, old_lease = await select_live(controller, frames, unknown)
        client.snapshot_data = inventory.snapshot
        client.read_pane_error = OSError("read failed after terminal evidence")
        controller._dirty_event.set()
        await wait_until(lambda: frames[-1].phase == "stale")
        assert controller._active_terminal_id == "term-1" and frames[-1].lease is None
        client.snapshot_data = unknown.snapshot
        client.read_pane_error = None
        await controller.reconcile([unknown], enabled=True)
        assert frames[-1].lease is None and frames[-1].phase == "stale"
        assert (await controller.submit(ReplyRequest(old_lease, "old"))).outcome == "rejected"
        assert client.prompt_agent_calls == []
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_periodic_polling_deadline_schedules_from_attempt_start(tmp_path):
    """Finding 6: polling schedules from preceding attempt start, not completion."""
    inventory = make_inventory(socket_path=tmp_path / "poll_deadline.sock")
    client = FakeClient(socket_path=inventory.socket_path)
    client.snapshot_data = inventory.snapshot
    orig_get_snapshot = client.get_snapshot

    async def slow_snapshot():
        await asyncio.sleep(0.03)
        return await orig_get_snapshot()

    client.get_snapshot = slow_snapshot
    frames = []
    controller = LivePaneController(
        publish=frames.append,
        client_factory=lambda s, p: client,
        subscription_factory=lambda p, f: FakeSubscription(p, f),
        refresh_min_interval=0.01,
        poll_interval=0.1,
    )
    try:
        await select_live(controller, frames, inventory)
        await wait_until(lambda: len(client.snapshot_calls) >= 3, timeout=2.0)
        times = [call["time"] for call in client.snapshot_calls]
        interval = times[2] - times[1]
        assert 0.09 <= interval < 0.16
    finally:
        await controller.close()
