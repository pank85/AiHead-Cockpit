"""Offline bridge safety and launcher boundary tests (no Herdr processes or sockets)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from herdr_master.core.config import CouncilConfig
from herdr_master.core.models import Squad
from herdr_master.herdr.discovery import SessionInventory
from herdr_master.herdr.snapshot import PaneSnapshot, TabSnapshot

from aiheaddev.bridge import runtime as r
from aiheaddev.bridge.protocol import BridgeError, validate_event
from aiheaddev.core.workspace_targets import WorkspaceTarget
from aiheaddev.ui.tui.live_pane import LivePaneController
from test_live_pane import FakeClient, FakeSubscription


class Client(FakeClient):
    def __init__(self, session="s", socket_path=Path("/fake/a")):
        super().__init__(session, socket_path)
        self.deleted = []
        self.layout_data = None
        self.layout_requests = []
    async def _rpc(self, method, params):
        self.layout_requests.append((method, params))
        if self.layout_data is None:
            raise RuntimeError("unsupported")
        return {'result': self.layout_data}
    async def close_workspace(self, workspace_id, close_group=False):
        self.deleted.append((workspace_id, close_group))


def controller(**kwargs):
    return LivePaneController(**kwargs, subscription_factory=lambda path, subs: FakeSubscription(path, subs),
                              poll_interval=0.05, refresh_min_interval=0.001, rpc_timeout=0.2)


def make_runtime(clients=None):
    events = []
    clients = clients or [Client()]
    by_path = {str(c.socket_path): c for c in clients}
    def factory(session, socket_path):
        return by_path[str(socket_path)]
    rt = r.LiveRuntime(lambda e, p: events.append((e, p)), client_factory=factory,
        discover=lambda primary: clients, context_resolver=lambda session: SimpleNamespace(session_name=session or "s", socket_path=clients[0].socket_path),
        controller_factory=controller, poll_interval=0.05)
    return rt, events


def target(client, **updates):
    value = {"socket_path": str(client.socket_path), "workspace_id": "ws-1", "tab_id": "tab-1", "pane_id": "p-1"}
    value.update(updates)
    return value


@pytest.mark.asyncio
async def test_overlapping_socket_ids_offline_reconnect_and_no_implicit_mutations():
    a, b = Client(socket_path=Path('/fake/a')), Client(socket_path=Path('/fake/b'))
    b.read_pane_data = {"text": "different socket"}
    rt, events = make_runtime([a, b])
    await rt.refresh()
    await rt.select(target(b))
    await asyncio.sleep(.06)
    frames = [p for e, p in events if e == "pane_frame"]
    assert frames and all(p['target']['socket_path'] == '/fake/b' for p in frames)
    assert any('different socket' in p['text'] for p in frames)
    for e, payload in events:
        validate_event({'event': e, 'revision': 1, 'payload': payload})
    b.snapshot_error = RuntimeError('offline')
    await rt.refresh()
    assert next(i for i in rt.inventories if str(i.socket_path) == '/fake/b').error
    with pytest.raises(BridgeError):
        await rt.select(target(b))
    b.snapshot_error = None
    await rt.refresh()
    assert not next(i for i in rt.inventories if str(i.socket_path) == '/fake/b').error
    await rt.close()
    for c in (a, b):
        assert not (c.focus_workspace_called or c.focus_tab_called or c.focus_pane_called or c.send_keys_called or c.attach_called or c.launch_called)
        assert c.deleted == []


@pytest.mark.asyncio
async def test_stale_terminal_lease_and_at_most_once_uncertain_send():
    c = Client()
    rt, events = make_runtime([c])
    await rt.refresh(); await rt.select(target(c)); await asyncio.sleep(.05)
    lease = next(p['lease'] for e,p in reversed(events) if e == 'pane_frame' and p['lease'] and p['readiness']['can_reply'])
    c.prompt_error = TimeoutError('ambiguous')
    outcome, _, _ = await rt.send('reply.submit', {'lease': lease, 'text': 'hello', 'notify': True})
    assert outcome == 'uncertain'
    assert len(c.prompt_agent_calls) == 1 and c.prompt_agent_calls[0]['max_attempts'] == 1
    c.snapshot_data = c._default_snapshot()
    c.snapshot_data.panes[0].terminal_id = 'replacement'
    await rt.refresh(); await asyncio.sleep(.05)
    with pytest.raises(BridgeError):
        await rt.send('reply.submit', {'lease': lease, 'text': 'never', 'notify': True})
    assert len(c.prompt_agent_calls) == 1
    await rt.close()


@pytest.mark.parametrize('bad', [
    {'workspace_id':'other', 'tab_id':'tab-1', 'panes':[{'pane_id':'p-1','rect':{'x':0,'y':0,'width':1,'height':1}}]},
    {'workspace_id':'ws-1', 'tab_id':'tab-1', 'panes':[{'pane_id':'foreign','rect':{'x':0,'y':0,'width':1,'height':1}}]},
    {'workspace_id':'ws-1', 'tab_id':'tab-1', 'panes':[{'pane_id':'p-1','rect':{'x':0,'y':0,'width':0,'height':1}}]},
    {'workspace_id':'ws-1', 'tab_id':'tab-1', 'panes':[{'pane_id':'p-1','rect':{'x':True,'y':0,'width':1,'height':1}}]},
])
def test_layout_rejects_foreign_and_malformed_rectangles(bad):
    with pytest.raises(r.LiveTargetError):
        r._layout(bad, 'ws-1', 'tab-1', {'p-1'})


@pytest.mark.asyncio
async def test_layout_rpc_exact_selected_and_unsupported():
    c = Client(); rt, events = make_runtime([c])
    await rt.refresh(); await rt.select(target(c)); await asyncio.sleep(.04)
    assert c.layout_requests[0] == ('pane.layout', {'pane_id': 'p-1'})
    assert rt.layout == {'state':'layout_unavailable','rects':[]}
    c.layout_data = {'workspace_id':'ws-1','tab_id':'tab-1','panes':[{'pane_id':'p-1','rect':{'x':2,'y':3,'width':10,'height':5},'focused':True}]}
    await asyncio.sleep(.08)
    assert rt.layout == {'state':'available','rects':[{'pane_id':'p-1','x':2,'y':3,'width':10,'height':5}], 'focused_pane_id':'p-1'}
    await rt.close()


@pytest.mark.asyncio
async def test_tab_change_cancels_slow_sibling_reads():
    c = Client(); snap = c._default_snapshot()
    snap.panes.append(PaneSnapshot(pane_id='sibling', workspace_id='ws-1', tab_id='tab-1', terminal_id='sibling-term'))
    snap.tabs.append(TabSnapshot(tab_id='tab-2', workspace_id='ws-1'))
    snap.panes.append(PaneSnapshot(pane_id='p-2', workspace_id='ws-1', tab_id='tab-2', terminal_id='term-2'))
    c.snapshot_data = snap
    c.read_pane_delay = .2
    rt, events = make_runtime([c]); await rt.refresh(); await rt.select(target(c)); await asyncio.sleep(.04)
    await rt.select(target(c, tab_id='tab-2', pane_id='p-2'))
    index = len(events)
    await asyncio.sleep(.24)
    assert not any(e == 'pane_frame' and p['target']['pane_id'] == 'sibling' for e,p in events[index:])
    await rt.close()


@pytest.mark.asyncio
async def test_confirmed_delete_exact_workspace_and_shutdown_leaves_workspaces():
    c = Client(); rt, _ = make_runtime([c]); await rt.refresh()
    with pytest.raises(BridgeError):
        await rt.delete({'target': {'socket_path':str(c.socket_path),'workspace_id':'ws-1'}, 'confirmed':False})
    with pytest.raises(BridgeError):
        await rt.delete({'target': target(c), 'confirmed':True})
    outcome, _, _ = await rt.delete({'target': {'socket_path':str(c.socket_path),'workspace_id':'ws-1'}, 'confirmed':True})
    assert outcome == 'accepted' and c.deleted == [('ws-1', False)]
    await rt.close(); assert len(c.deleted) == 1


def launch_setup(monkeypatch):
    c = Client(); rt, _ = make_runtime([c]); calls = {}
    monkeypatch.setattr(r, 'folders_test', lambda path: {'path':'/fake/project','available':True})
    monkeypatch.setattr(r, 'resolve_squad', lambda path: SimpleNamespace(squad=Squad()))
    monkeypatch.setattr(r, 'ensure_herdr_routable', lambda *a, **kw: None)
    monkeypatch.setattr(r, 'describe_agent', lambda *a, **kw: {})
    monkeypatch.setattr(r, 'native_binding_error', lambda *a, **kw: None)
    for name in ('launch_master','launch_ergo','launch_orchestration','launch_council'):
        fn = AsyncMock(return_value={'workspace_id':'new-ws','session':'s','pane_id':'new-pane'})
        monkeypatch.setattr(r, name, fn); calls[name] = fn
    return c, rt, calls


@pytest.mark.asyncio
@pytest.mark.parametrize('override', [None, True, False])
@pytest.mark.parametrize('kind', ['master','ergo','orchestration'])
async def test_launch_exact_kwargs_and_tristate(monkeypatch, override, kind):
    c, rt, calls = launch_setup(monkeypatch)
    common = {'project_path':'/fake/project', 'session':None, 'ai_head':override}
    if kind == 'master':
        params = {**common, 'name':'Master','harness':'pi','model':'model','effort':'high','task':'goal'}
        expected = {'name':'Master','harness':'pi','model':'model','initial_task':'goal','effort':'high','ai_head':override}
    elif kind == 'ergo':
        squad = Squad().model_dump(mode='json')
        params = {**common, 'intent':'goal','label':'Work','squad':squad,'customize_first':True,'start_pipeline':True,'save_default':False}
        expected = {'intent':'goal','workspace_label':'Work','squad':Squad.model_validate(squad),'ai_head':override}
    else:
        params = {**common,'intent':'goal','label':'Work','harness':'pi','scout_model':'s','architect_model':'a','reviewer_model':'r',
                  'worker_models':['w1','w2'],'scout_effort':'high','architect_effort':None,'reviewer_effort':'max','dangerously_skip_permissions':True}
        expected = {'intent':'goal','workspace_label':'Work','harness':'pi','scout_model':'s','architect_model':'a','reviewer_model':'r',
                    'coder_model':'w1 | w2','scout_effort':'high','architect_effort':None,'reviewer_effort':'max','ai_head':override,'dangerously_skip_permissions':True}
    result = await rt.launch('launch.' + kind, params)
    assert result[0] == 'accepted'
    calls['launch_' + kind].assert_awaited_once_with(client=c, cwd='/fake/project', session='s', fresh_workspace=True, focus=False, **expected)
    await rt.close()


@pytest.mark.asyncio
async def test_ergo_save_default_is_explicit_and_precedes_dispatch(monkeypatch):
    c, rt, calls = launch_setup(monkeypatch)
    events = []
    class Settings:
        def project_open(self, path):
            events.append(('open', path))
            return {'token':'draft', 'revision':7}
        def save(self, token, sections, *, revision):
            events.append(('save', token, sections, revision))
            return {'valid':True}
    monkeypatch.setattr(r, 'SettingsService', Settings)
    async def launched(**kwargs):
        events.append(('launch',))
        return {'workspace_id':'new-ws', 'session':'s'}
    calls['launch_ergo'].side_effect = launched
    squad = Squad().model_dump(mode='json')
    params = {'project_path':'/fake/project', 'intent':'goal', 'label':'Work', 'squad':squad,
              'customize_first':True, 'save_default':True, 'start_pipeline':True}
    assert (await rt.launch('launch.ergo', params))[0] == 'accepted'
    assert events == [('open', '/fake/project'), ('save', 'draft', {'squad':squad}, 7), ('launch',)]
    events.clear(); params.update(save_default=False, start_pipeline=False)
    assert (await rt.launch('launch.ergo', params))[0] == 'accepted'
    assert events == [] and calls['launch_ergo'].await_count == 1
    await rt.close()


@pytest.mark.asyncio
async def test_launch_result_with_foreign_session_is_uncertain(monkeypatch):
    c, rt, calls = launch_setup(monkeypatch)
    calls['launch_master'].return_value = {'workspace_id':'new-ws', 'session':'other'}
    outcome, _, target = await rt.launch('launch.master', {'project_path':'/fake/project',
        'name':'n', 'harness':'pi', 'model':'m'})
    assert outcome == 'uncertain' and target is None
    calls['launch_master'].assert_awaited_once()
    await rt.close()


@pytest.mark.asyncio
async def test_council_false_no_start_and_duplicate_guard(monkeypatch):
    c, rt, calls = launch_setup(monkeypatch)
    params = {'project_path':'/fake/project','session':'s','idea':'idea','label':'Council','config':CouncilConfig().model_dump(mode='json'),'customize_roster':True}
    outcome, _, _ = await rt.launch('launch.council', params)
    assert outcome == 'accepted'
    calls['launch_council'].assert_awaited_once_with(client=c, cwd='/fake/project', session='s', fresh_workspace=True, focus=False,
        intent='idea', workspace_label='Council', council=CouncilConfig.model_validate(params['config']), ai_head=False)
    event = asyncio.Event()
    async def blocked(**kw):
        await event.wait(); return {'workspace_id':'new'}
    calls['launch_master'].side_effect = blocked
    master = {'project_path':'/fake/project','name':'n','harness':'pi','model':'m'}
    task = asyncio.create_task(rt.launch('launch.master', master)); await asyncio.sleep(.02)
    with pytest.raises(BridgeError): await rt.launch('launch.master', master)
    event.set(); await task
    ergo = {'project_path':'/fake/project','intent':'x','label':'x','squad':None,'customize_first':False,'start_pipeline':False,'save_default':False}
    assert (await rt.launch('launch.ergo', ergo))[0] == 'accepted'
    calls['launch_ergo'].assert_not_awaited()
    await rt.close()


@pytest.mark.asyncio
async def test_fresh_preflight_rejects_tab_replacement_without_send():
    c = Client(); rt, events = make_runtime([c])
    await rt.refresh(); await rt.select(target(c)); await asyncio.sleep(.05)
    lease = next(p['lease'] for e,p in reversed(events) if e == 'pane_frame' and p['lease'] and p['readiness']['can_reply'])
    fresh = c._default_snapshot()
    fresh.tabs[0].tab_id = 'new-tab'
    fresh.panes[0].tab_id = 'new-tab'
    fresh.agents[0].tab_id = 'new-tab'
    c.snapshot_data = fresh
    outcome, _, _ = await rt.send('reply.submit', {'lease':lease, 'text':'do not send', 'notify':True})
    assert outcome == 'rejected'
    assert c.prompt_agent_calls == []
    await rt.close()


@pytest.mark.asyncio
async def test_direct_input_one_attempt_uncertain_and_tab_fenced():
    c = Client(); c.pane_send_input = AsyncMock(return_value={'type':'input_sent','pane_id':'p-1'})
    rt, events = make_runtime([c])
    await rt.refresh(); await rt.select(target(c)); await asyncio.sleep(.05)
    lease = next(p['lease'] for e,p in reversed(events) if e == 'pane_frame' and p['lease'])
    outcome, _, _ = await rt.send('terminal.input', {'lease':lease, 'keys':['Enter']})
    assert outcome == 'accepted'
    c.pane_send_input.assert_awaited_once_with(pane_id='p-1', text=None, keys=['Enter'])
    c.pane_send_input.side_effect = TimeoutError('after dispatch')
    outcome, _, _ = await rt.send('terminal.input', {'lease':lease, 'text':'raw'})
    assert outcome == 'uncertain' and c.pane_send_input.await_count == 2
    c.pane_send_input.side_effect = None; c.pane_send_input.return_value = {}
    outcome, _, _ = await rt.send('terminal.input', {'lease':lease, 'text':'ambiguous'})
    assert outcome == 'uncertain' and c.pane_send_input.await_count == 3
    fresh = c._default_snapshot(); fresh.tabs[0].tab_id = 'new-tab'
    fresh.panes[0].tab_id = 'new-tab'; fresh.agents[0].tab_id = 'new-tab'
    c.snapshot_data = fresh
    outcome, _, _ = await rt.send('terminal.input', {'lease':lease, 'text':'never'})
    assert outcome == 'rejected' and c.pane_send_input.await_count == 3
    await rt.close()


@pytest.mark.asyncio
async def test_sibling_reads_bounded_and_discovery_does_not_block_loop():
    import time
    class CountingClient(Client):
        active = 0
        maximum = 0
        async def read_pane(self, pane_id, format='ansi', lines=None):
            if pane_id == 'p-1':
                return await super().read_pane(pane_id, format, lines)
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            try:
                await asyncio.sleep(.04)
                return await super().read_pane(pane_id, format, lines)
            finally:
                self.active -= 1
    c = CountingClient(); snap = c._default_snapshot()
    for i in range(12):
        snap.panes.append(PaneSnapshot(pane_id=f'sibling-{i}', workspace_id='ws-1', tab_id='tab-1', terminal_id=f'term-{i}'))
    c.snapshot_data = snap
    rt, events = make_runtime([c])
    rt.discover = lambda primary: (time.sleep(.1) or [c])
    refreshing = asyncio.create_task(rt.refresh())
    await asyncio.sleep(.02)
    assert not refreshing.done(), 'blocking discovery must run off the event loop'
    await refreshing; await rt.select(target(c)); await asyncio.sleep(.22)
    assert c.maximum == r.MAX_CONCURRENT_READS
    frames = [p for e,p in events if e == 'pane_frame' and p['target']['pane_id'].startswith('sibling-')]
    assert len(frames) >= 8 and all(p['lease'] is None for p in frames)
    await rt.close()


@pytest.mark.asyncio
async def test_snapshot_polling_is_bounded_and_overlapping_refresh_is_coalesced():
    counts = {'active':0, 'maximum':0, 'calls':0}
    class SlowClient(Client):
        async def get_snapshot(self):
            counts['active'] += 1
            counts['maximum'] = max(counts['maximum'], counts['active'])
            counts['calls'] += 1
            try:
                await asyncio.sleep(.03)
                return await super().get_snapshot()
            finally:
                counts['active'] -= 1
    clients = [SlowClient(f'session-{i}', Path(f'/fake/socket-{i}')) for i in range(18)]
    rt, events = make_runtime(clients)
    first = asyncio.create_task(rt.refresh())
    await asyncio.sleep(.01)
    await rt.refresh()
    await first
    assert counts == {'active':0, 'maximum':8, 'calls':18}
    assert len(rt.inventories) == 18
    await rt.close()


@pytest.mark.asyncio
async def test_aihead_reuses_existing_and_rejects_duplicate_creation(monkeypatch):
    c, rt, _ = launch_setup(monkeypatch)
    snap = c._default_snapshot(); snap.tabs[0].label = 'ai-head'; c.snapshot_data = snap
    ensure = AsyncMock(return_value={'workspace_id':'ws-1','tab_id':'tab-1','pane_id':'p-1'})
    monkeypatch.setattr(r, 'ensure_herdr_ai_head', ensure)
    result = await rt.open_aihead({'project_path':'/fake/project'})
    assert result[0] == 'accepted' and result[2] == {'socket_path':'/fake/a','workspace_id':'ws-1','tab_id':'tab-1','pane_id':'p-1','terminal_id':'term-1'}
    ensure.assert_not_awaited()
    snap.tabs[0].label = 'ordinary'; snap.workspaces[0].label = 'ai-head'
    monkeypatch.setattr(r, 'load_project_config', lambda _: SimpleNamespace(ai_head=SimpleNamespace(cwd=None)))
    started, finish = asyncio.Event(), asyncio.Event()
    async def create(**kwargs):
        started.set(); await finish.wait()
        return {'workspace_id':'ws-1','tab_id':'new-tab','pane_id':'new-pane'}
    ensure.side_effect = create
    first = asyncio.create_task(rt.open_aihead({'project_path':'/fake/project'}))
    await started.wait()
    with pytest.raises(BridgeError) as exc:
        await rt.open_aihead({'project_path':'/fake/project'})
    assert exc.value.code == 'operation_in_progress'
    finish.set(); result = await first
    assert result[0] == 'accepted' and result[2]['pane_id'] == 'new-pane'
    assert ensure.await_count == 1
    ensure.side_effect = None
    ensure.return_value = {'workspace_id':'foreign', 'pane_id':'new-pane'}
    outcome, _, target = await rt.open_aihead({'project_path':'/fake/project'})
    assert outcome == 'uncertain' and target is None
    assert not c.focus_workspace_called and not c.focus_tab_called and not c.focus_pane_called
    await rt.close()
