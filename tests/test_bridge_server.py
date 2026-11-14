"""In-memory wire tests: no subprocesses, network, or user configuration."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from aiheaddev.bridge.protocol import encode, validate_event
from aiheaddev.bridge.server import BridgeServer


def test_sock_ok(tmp_path):
    """The amended fence permits only synthetic filesystem Unix sockets here."""
    import socket
    from tests.ui_sandbox import get_active_guard
    guard = get_active_guard()
    assert guard is not None and guard.is_active()
    address = str(tmp_path / 's')
    with socket.socket(socket.AF_UNIX) as server, socket.socket(socket.AF_UNIX) as client:
        server.bind(address)
        server.listen(1)
        client.connect(address)
        conn, _ = server.accept()
        conn.close()


def test_sock_fence(tmp_path):
    """Verify local allowance does not permit outside, abstract or network access."""
    import socket
    from tests.ui_sandbox import get_active_guard
    guard = get_active_guard()
    assert guard is not None and guard.is_active()
    outside = str(guard.container_dir.parent / 'outside.sock')
    escaped = str(guard.container_dir / '..' / 'outside.sock')
    with socket.socket(socket.AF_UNIX) as unix, socket.socket(socket.AF_INET) as inet:
        for call in (lambda: unix.bind(outside), lambda: unix.connect(outside),
                     lambda: unix.bind(escaped), lambda: unix.connect(escaped),
                     lambda: unix.bind('\0abstract-test'), lambda: unix.connect(b'\0abstract-test'),
                     lambda: inet.bind(('127.0.0.1', 0)), lambda: inet.connect(('127.0.0.1', 9)),
                     lambda: unix.sendto(b'x', str(tmp_path / 's')),
                     lambda: socket.getaddrinfo('localhost', 80)):
            with guard.expect_denial(), pytest.raises(PermissionError):
                call()
        if hasattr(unix, 'sendmsg'):
            with guard.expect_denial(), pytest.raises(PermissionError):
                unix.sendmsg([b'x'], [], 0, str(tmp_path / 's'))
    if socket.has_ipv6:
        with socket.socket(socket.AF_INET6) as inet6:
            for call in (lambda: inet6.bind(('::1', 0)), lambda: inet6.connect(('::1', 9))):
                with guard.expect_denial(), pytest.raises(PermissionError):
                    call()
    for event in ('urllib.request', 'http.client.connect', 'http.client.send'):
        import sys
        with guard.expect_denial(), pytest.raises(PermissionError):
            sys.audit(event, 'synthetic')


class Writer:
    def __init__(self, broken=False):
        self.lines = []
        self.broken = broken
    def write(self, data):
        if self.broken:
            raise BrokenPipeError()
        self.lines.append(data)
    async def drain(self):
        await asyncio.sleep(0)


class Runtime:
    instances = []
    def __init__(self, emit, **kwargs):
        self.emit = emit
        self.closed = False
        self.calls = []
        Runtime.instances.append(self)
    async def refresh(self):
        self.emit('inventory', {'sessions': []})
    async def select(self, target):
        self.emit('pane_frame', {'target':target, 'lease':None,'text':'hello',
            'readiness':{'can_reply':False,'state':'unknown','explanation':'read-only'},
            'layout':{'state':'layout_unavailable','rects':[]}})
    async def send(self, method, params):
        self.calls.append((method,params))
        return 'accepted','sent',params['lease']
    async def launch(self, method, params):
        self.calls.append((method,params)); await asyncio.sleep(.01)
        return 'accepted','launched',{'socket_path':'/fake/s','workspace_id':'w'}
    async def open_aihead(self, params): return 'rejected','no',None
    async def delete(self, params): return 'rejected','no',None
    async def close(self): self.closed = True


def req(id, method, **params):
    return {'id':id,'method':method,'params':params}


async def run(requests, *, broken=False, factory=Runtime):
    reader = asyncio.StreamReader(limit=2*1024*1024)
    for request in requests:
        reader.feed_data(encode(request))
    reader.feed_eof()
    writer = Writer(broken)
    await BridgeServer(reader, writer, runtime_factory=factory).serve()
    return [json.loads(x) for x in writer.lines]


@pytest.mark.asyncio
async def test_ordering_selection_unknown_and_mutation_result():
    t = {'socket_path':'/fake/s','workspace_id':'w','tab_id':'t','pane_id':'p','terminal_id':'x'}
    lease = {'target':t, 'generation':1, 'terminal_id':'x'}
    messages = await run([req(1,'initialize',protocol=1), req(2,'selection.set',target=t),
        req(3,'no.such.method'), req(4,'reply.submit',lease=lease,text='hello',notify=True), req(5,'shutdown')])
    assert messages[0] == {'id':1,'result':{'protocol':1,'capabilities':[]}}
    index = lambda predicate: next(i for i,m in enumerate(messages) if predicate(m))
    assert index(lambda m:m.get('id') == 2) < index(lambda m:m.get('event') == 'pane_frame')
    assert next(m for m in messages if m.get('id') == 3)['error']['code'] == 'unknown_method'
    assert index(lambda m:m.get('id') == 4) < index(lambda m:m.get('event') == 'reply_result')
    assert next(m for m in messages if m.get('event') == 'reply_result')['payload']['outcome'] == 'accepted'
    revisions = [m['revision'] for m in messages if 'event' in m]
    assert revisions == sorted(revisions) and len(set(revisions)) == len(revisions)
    for msg in messages:
        if 'event' in msg: validate_event(msg)
    assert Runtime.instances[-1].closed


@pytest.mark.asyncio
async def test_duplicate_launch_is_rejected_while_first_runs():
    p = {'project_path':'/fake/project','name':'n','harness':'pi','model':'m'}
    messages = await run([req(1,'initialize',protocol=1),req(2,'launch.master',**p),req(3,'launch.master',**p),req(4,'shutdown')])
    assert next(x for x in messages if x.get('id') == 2)['result'] == {'pending':True}
    assert next(x for x in messages if x.get('id') == 3)['error']['code'] == 'operation_in_progress'
    events = [x for x in messages if x.get('event') == 'operation_status']
    assert len(events) == 1 and events[0]['payload']['request_id'] == 2
    assert len(Runtime.instances[-1].calls) == 1


@pytest.mark.asyncio
async def test_malformed_input_and_broken_output_are_safe(capsys):
    reader = asyncio.StreamReader(); reader.feed_data(b'{bad json}\n'); reader.feed_eof()
    writer = Writer(); await BridgeServer(reader, writer, runtime_factory=Runtime).serve()
    assert writer.lines == []
    await run([req(1,'initialize',protocol=1),req(2,'shutdown')], broken=True)
    assert capsys.readouterr().out == ''


@pytest.mark.asyncio
async def test_broken_input_closes_runtime_without_mutating_workspaces():
    class BrokenReader:
        def __init__(self): self.reads = 0
        async def readline(self):
            self.reads += 1
            if self.reads == 1:
                return encode(req(1, 'initialize', protocol=1))
            raise ConnectionResetError('input disconnected')
    writer = Writer()
    await BridgeServer(BrokenReader(), writer, runtime_factory=Runtime).serve()
    assert Runtime.instances[-1].closed is True
    assert Runtime.instances[-1].calls == []
    assert json.loads(writer.lines[0])['id'] == 1


def test_entrypoint_reserves_stdout_and_routes_diagnostics_to_stderr(monkeypatch):
    import io
    import sys
    from types import SimpleNamespace
    from aiheaddev.bridge import __main__ as entrypoint
    wire_in, wire_out = io.BytesIO(), io.BytesIO()
    diagnostics = io.StringIO()
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=wire_in))
    monkeypatch.setattr(sys, 'stdout', SimpleNamespace(buffer=wire_out))
    monkeypatch.setattr(sys, 'stderr', diagnostics)
    async def fake_main(stdin, stdout):
        assert stdin is wire_in and stdout is wire_out
        print('integration diagnostic')
        stdout.write(encode({'id':1, 'result':{}}))
    monkeypatch.setattr(entrypoint, '_main', fake_main)
    entrypoint.main()
    assert diagnostics.getvalue() == 'integration diagnostic\n'
    assert json.loads(wire_out.getvalue()) == {'id':1, 'result':{}}


@pytest.mark.asyncio
async def test_broken_output_unblocks_open_input():
    reader = asyncio.StreamReader()
    reader.feed_data(encode(req(1, 'initialize', protocol=1)))
    await asyncio.wait_for(BridgeServer(reader, Writer(broken=True), runtime_factory=Runtime).serve(), 1)
    assert Runtime.instances[-1].closed


@pytest.mark.asyncio
async def test_bad_backend_result_still_emits_uncertain_completion():
    class BadResult(Runtime):
        async def send(self, method, params):
            return 'not-an-outcome', 'result', None
    t = {'socket_path':'/fake/s','workspace_id':'w','tab_id':'t','pane_id':'p','terminal_id':'x'}
    lease = {'target':t, 'generation':1, 'terminal_id':'x'}
    messages = await run([req(1,'initialize',protocol=1), req(2,'reply.submit',lease=lease,text='x',notify=True), req(3,'shutdown')], factory=BadResult)
    result = next(m for m in messages if m.get('event') == 'reply_result')
    assert result['payload']['outcome'] == 'uncertain' and result['payload']['request_id'] == 2


@pytest.mark.asyncio
async def test_oversized_backend_response_is_replaced_and_stream_continues(monkeypatch):
    import aiheaddev.bridge.server as s
    monkeypatch.setattr(s, 'catalog_get', lambda: {'too_large': 'x' * (3 * 1024 * 1024)})
    messages = await run([req(1, 'initialize', protocol=1), req(2, 'catalog.get'), req(3, 'shutdown')])
    assert next(m for m in messages if m.get('id') == 2)['error']['code'] == 'internal_error'
    assert next(m for m in messages if m.get('id') == 3)['result'] == {}


@pytest.mark.asyncio
async def test_demo_never_constructs_live_or_saves(monkeypatch):
    import aiheaddev.bridge.server as s
    monkeypatch.setattr(s, 'LiveRuntime', lambda *a,**k: (_ for _ in ()).throw(AssertionError('live runtime')))
    monkeypatch.setattr(s, 'folders_test', lambda *a,**k: (_ for _ in ()).throw(AssertionError('folder IO')))
    monkeypatch.setattr(s, 'folders_list', lambda *a,**k: (_ for _ in ()).throw(AssertionError('folder IO')))
    monkeypatch.setattr(s, 'logs', lambda *a,**k: (_ for _ in ()).throw(AssertionError('log IO')))
    target = {'socket_path':'/demo/herdr.sock', 'workspace_id':'demo-workspace',
              'tab_id':'demo-tab', 'pane_id':'demo-pane', 'terminal_id':'synthetic'}
    lease = {'target':target, 'generation':1, 'terminal_id':'synthetic'}
    writer = await run([req(1,'initialize',protocol=1,demo=True), req(2,'project.open',path='/synthetic'),
        req(3,'folders.test',path='/synthetic'), req(4,'logs.get'),
        req(5,'settings.save',token='demo',revision=0,sections={}),
        req(6,'terminal.input',lease=lease,text='do not send'),
        req(7,'launch.master',project_path='/synthetic',name='n',harness='pi',model='m'),
        req(8,'workspace.delete',target={'socket_path':'/demo/herdr.sock','workspace_id':'demo-workspace'},confirmed=True),
        req(9,'shutdown')], factory=lambda *a,**k: (_ for _ in ()).throw(AssertionError('discovery')))
    assert next(x for x in writer if x.get('id') == 2)['result']['token'] == 'demo'
    assert next(x for x in writer if x.get('id') == 3)['result']['available'] is False
    assert next(x for x in writer if x.get('id') == 4)['result']['records'] == []
    assert next(x for x in writer if x.get('id') == 5)['error']['code'] == 'demo_read_only'
    for request_id in (6, 7, 8):
        assert next(x for x in writer if x.get('id') == request_id)['result'] == {'pending':True}
        completion = next(x for x in writer if x.get('event') in ('reply_result', 'operation_status')
                          and x['payload']['request_id'] == request_id)
        assert completion['payload']['outcome'] == 'rejected'
