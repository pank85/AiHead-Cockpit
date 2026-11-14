//! Bounded off-UI-thread bridge I/O. Queue pressure never drops mutation results.
use crate::protocol::{
    self, BridgeEvent, Incoming, Method, Request, RequestId, MAX_MESSAGE_BYTES, PROTOCOL,
};
use std::{
    collections::VecDeque,
    io::{self, BufRead, BufReader, Read, Write},
    path::Path,
    process::{Child, Command, Stdio},
    sync::{
        mpsc::{self, SyncSender, TrySendError},
        Arc, Condvar, Mutex,
    },
    thread,
    time::{Duration, Instant},
};

pub const QUEUE_MESSAGES: usize = 32;
pub const QUEUE_BYTES: usize = 8 * 1024 * 1024;
const BRIDGE_SHUTDOWN_GRACE: Duration = Duration::from_secs(30);
const BRIDGE_SHUTDOWN_POLL: Duration = Duration::from_millis(10);
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransportError {
    Invalid(String),
    Full,
    Closed,
}
#[derive(Debug, Clone, PartialEq)]
pub enum TransportEvent {
    Message(Incoming),
    Failed(String),
    Closed,
}
pub trait Transport {
    fn send(&mut self, request: Request) -> Result<(), TransportError>;
    fn poll(&mut self) -> Option<TransportEvent>;
}

#[derive(Default)]
struct QueueState {
    queue: VecDeque<(Incoming, usize)>,
    bytes: usize,
    closed: bool,
    terminal: Option<TransportEvent>,
    terminal_read: bool,
}
struct Queue {
    state: Mutex<QueueState>,
    space: Condvar,
}
impl Queue {
    fn new() -> Self {
        Self {
            state: Mutex::new(QueueState::default()),
            space: Condvar::new(),
        }
    }
    fn droppable(message: &Incoming) -> bool {
        matches!(message, Incoming::Event(e) if matches!(e.as_ref(), BridgeEvent::Inventory { .. } | BridgeEvent::PaneFrame { .. }))
    }
    fn same_snapshot(a: &Incoming, b: &Incoming) -> bool {
        match (a, b) {
            (Incoming::Event(a), Incoming::Event(b)) => match (a.as_ref(), b.as_ref()) {
                (BridgeEvent::Inventory { .. }, BridgeEvent::Inventory { .. }) => true,
                (
                    BridgeEvent::PaneFrame { payload: a, .. },
                    BridgeEvent::PaneFrame { payload: b, .. },
                ) => a.target == b.target,
                _ => false,
            },
            _ => false,
        }
    }
    fn push(&self, message: Incoming, size: usize) {
        let mut s = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(i) = s
            .queue
            .iter()
            .position(|(old, _)| Self::same_snapshot(old, &message))
        {
            let old_revision = match &s.queue[i].0 {
                Incoming::Event(e) => e.revision(),
                _ => 0,
            };
            let new_revision = match &message {
                Incoming::Event(e) => e.revision(),
                _ => 0,
            };
            if old_revision >= new_revision {
                return;
            }
            let (_, old_size) = s.queue.remove(i).unwrap();
            s.bytes -= old_size;
        }
        while !s.closed
            && (s.queue.len() >= QUEUE_MESSAGES || s.bytes.saturating_add(size) > QUEUE_BYTES)
        {
            if let Some(i) = s.queue.iter().position(|(m, _)| Self::droppable(m)) {
                let (_, n) = s.queue.remove(i).unwrap();
                s.bytes -= n;
            } else {
                s = self.space.wait(s).unwrap_or_else(|e| e.into_inner());
            }
        }
        if !s.closed {
            s.bytes += size;
            s.queue.push_back((message, size));
        }
    }
    fn end(&self, event: TransportEvent) {
        let mut s = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if s.terminal.is_none() {
            s.terminal = Some(event);
        }
        s.closed = true;
        self.space.notify_all();
    }
    fn pop(&self) -> Option<TransportEvent> {
        let mut s = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if let Some((v, n)) = s.queue.pop_front() {
            s.bytes -= n;
            self.space.notify_one();
            return Some(TransportEvent::Message(v));
        }
        if !s.terminal_read && s.terminal.is_some() {
            s.terminal_read = true;
            return s.terminal.clone();
        }
        None
    }
}
/// Read at most one bounded line without first allocating an attacker-sized line.
pub fn read_bounded_line(reader: &mut impl BufRead) -> io::Result<Option<Vec<u8>>> {
    let mut line = Vec::new();
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return if line.is_empty() {
                Ok(None)
            } else {
                Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "unterminated bridge message",
                ))
            };
        }
        let n = available
            .iter()
            .position(|b| *b == b'\n')
            .map_or(available.len(), |i| i + 1);
        if line.len().saturating_add(n) > MAX_MESSAGE_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "bridge message exceeds 2 MiB",
            ));
        }
        let terminated = available[n - 1] == b'\n';
        line.extend_from_slice(&available[..n]);
        reader.consume(n);
        if terminated {
            return Ok(Some(line));
        }
    }
}
enum Handshake {
    New,
    Waiting {
        id: RequestId,
        capabilities: Vec<String>,
    },
    Ready,
    Failed,
}
pub struct BridgeTransport {
    sender: Option<SyncSender<Vec<u8>>>,
    incoming: Arc<Queue>,
    handshake: Handshake,
}
impl BridgeTransport {
    /// Streams may be fake in tests; neither stream is the terminal output.
    pub fn from_io<R: Read + Send + 'static, W: Write + Send + 'static>(
        reader: R,
        mut writer: W,
    ) -> Self {
        let incoming = Arc::new(Queue::new());
        let queue = incoming.clone();
        thread::spawn(move || {
            let mut reader = BufReader::new(reader);
            loop {
                match read_bounded_line(&mut reader) {
                    Ok(Some(line)) => match protocol::decode_incoming(&line) {
                        Ok(message) => queue.push(message, line.len()),
                        Err(_) => {
                            queue.end(TransportEvent::Failed("Malformed bridge message".into()));
                            break;
                        }
                    },
                    Ok(None) => {
                        queue.end(TransportEvent::Closed);
                        break;
                    }
                    Err(e) => {
                        queue.end(TransportEvent::Failed(format!(
                            "Bridge read failed: {}",
                            e.kind()
                        )));
                        break;
                    }
                }
            }
        });
        let (sender, receiver) = mpsc::sync_channel::<Vec<u8>>(QUEUE_MESSAGES);
        let queue = incoming.clone();
        thread::spawn(move || {
            while let Ok(bytes) = receiver.recv() {
                if writer
                    .write_all(&bytes)
                    .and_then(|_| writer.flush())
                    .is_err()
                {
                    queue.end(TransportEvent::Failed(
                        "Bridge write failed; dispatched outcomes may be uncertain".into(),
                    ));
                    break;
                }
            }
        });
        Self {
            sender: Some(sender),
            incoming,
            handshake: Handshake::New,
        }
    }
    pub fn is_ready(&self) -> bool {
        matches!(self.handshake, Handshake::Ready)
    }
    pub fn close(&mut self) {
        self.sender.take();
        self.incoming.end(TransportEvent::Closed);
    }
}
impl Transport for BridgeTransport {
    fn send(&mut self, request: Request) -> Result<(), TransportError> {
        request
            .validate()
            .map_err(|e| TransportError::Invalid(e.to_string()))?;
        match self.handshake {
            Handshake::New if request.method == Method::Initialize => {}
            Handshake::Ready if request.method != Method::Initialize => {}
            Handshake::Failed => return Err(TransportError::Closed),
            _ => {
                return Err(TransportError::Invalid(
                    "bridge initialization not complete".into(),
                ))
            }
        }
        let bytes =
            protocol::encode(&request).map_err(|e| TransportError::Invalid(e.to_string()))?;
        let sender = self.sender.as_ref().ok_or(TransportError::Closed)?;
        if self
            .incoming
            .state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .closed
        {
            return Err(TransportError::Closed);
        }
        sender.try_send(bytes).map_err(|e| match e {
            TrySendError::Full(_) => TransportError::Full,
            TrySendError::Disconnected(_) => TransportError::Closed,
        })?;
        if request.method == Method::Initialize {
            let capabilities = request
                .params
                .get("capabilities")
                .and_then(serde_json::Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(str::to_owned))
                        .collect()
                })
                .unwrap_or_default();
            self.handshake = Handshake::Waiting {
                id: request.id,
                capabilities,
            };
        }
        Ok(())
    }
    fn poll(&mut self) -> Option<TransportEvent> {
        if matches!(self.handshake, Handshake::Failed) {
            return None;
        }
        let event = self.incoming.pop()?;
        if let Handshake::Waiting { id, capabilities } = &self.handshake {
            let valid = match &event {
                TransportEvent::Message(Incoming::Response(r)) if &r.id == id => r
                    .result
                    .as_ref()
                    .ok()
                    .and_then(|v| v.as_object())
                    .is_some_and(|o| {
                        o.len() == 2
                            && o.get("protocol").and_then(serde_json::Value::as_u64)
                                == Some(PROTOCOL)
                            && o.get("capabilities")
                                .and_then(serde_json::Value::as_array)
                                .is_some_and(|a| {
                                    a.iter().all(|v| {
                                        v.as_str()
                                            .is_some_and(|s| capabilities.iter().any(|c| c == s))
                                    })
                                })
                    }),
                _ => false,
            };
            if !valid {
                self.handshake = Handshake::Failed;
                self.incoming.end(TransportEvent::Closed);
                return Some(TransportEvent::Failed(
                    "Bridge initialization failed or protocol is incompatible".into(),
                ));
            }
            self.handshake = Handshake::Ready;
        } else if matches!(self.handshake, Handshake::New)
            && matches!(event, TransportEvent::Message(_))
        {
            self.handshake = Handshake::Failed;
            self.incoming.end(TransportEvent::Closed);
            return Some(TransportEvent::Failed(
                "Bridge sent data before initialization".into(),
            ));
        }
        Some(event)
    }
}
impl Drop for BridgeTransport {
    fn drop(&mut self) {
        self.close();
    }
}

/// Owns only the private Python bridge process, never a Herdr workspace or agent.
pub struct BridgeProcess {
    child: Child,
    pub transport: BridgeTransport,
    stderr_bytes: Arc<Mutex<usize>>,
}
impl BridgeProcess {
    pub fn spawn(interpreter: &Path) -> io::Result<Self> {
        if !interpreter.is_absolute() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "interpreter must be an absolute path",
            ));
        }
        let mut child = Command::new(interpreter)
            .args(["-m", "aiheaddev.bridge"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| io::Error::other("bridge stdin unavailable"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| io::Error::other("bridge stdout unavailable"))?;
        let mut stderr = child
            .stderr
            .take()
            .ok_or_else(|| io::Error::other("bridge stderr unavailable"))?;
        // Drain stderr to avoid deadlock, but retain only a bounded byte count: diagnostic
        // content may contain prompts/secrets and must not corrupt the terminal or logs.
        let stderr_bytes = Arc::new(Mutex::new(0usize));
        let count = stderr_bytes.clone();
        thread::spawn(move || {
            let mut buf = [0u8; 4096];
            while let Ok(n) = stderr.read(&mut buf) {
                if n == 0 {
                    break;
                }
                let mut c = count.lock().unwrap_or_else(|e| e.into_inner());
                *c = c.saturating_add(n).min(32 * 1024);
            }
        });
        Ok(Self {
            child,
            transport: BridgeTransport::from_io(stdout, stdin),
            stderr_bytes,
        })
    }
    pub fn diagnostics_available(&self) -> bool {
        *self.stderr_bytes.lock().unwrap_or_else(|e| e.into_inner()) > 0
    }
    fn shutdown(&mut self, grace: Duration) {
        // Disconnect the writer after it flushes queued requests. Its stdin then
        // reaches EOF, while the reader keeps draining the child's stdout.
        self.transport.close();
        let started = Instant::now();
        loop {
            if let Ok(Some(_)) = self.child.try_wait() {
                return;
            }
            let Some(remaining) = grace.checked_sub(started.elapsed()) else {
                break;
            };
            if remaining.is_zero() {
                break;
            }
            thread::sleep(remaining.min(BRIDGE_SHUTDOWN_POLL));
        }
        // Never send SIGKILL before the grace period has elapsed, even if
        // try_wait fails. Reap after a successful kill; if killing fails, avoid
        // an unbounded wait on a child that might still be running.
        if self.child.kill().is_ok() {
            let _ = self.child.wait();
        } else {
            let _ = self.child.try_wait();
        }
    }
}
impl Drop for BridgeProcess {
    fn drop(&mut self) {
        self.shutdown(BRIDGE_SHUTDOWN_GRACE);
    }
}

/// Simple bounded fake for deterministic UI and panel tests.
#[derive(Default)]
pub struct FakeTransport {
    pub sent: Vec<Request>,
    pub events: VecDeque<TransportEvent>,
}
impl Transport for FakeTransport {
    fn send(&mut self, r: Request) -> Result<(), TransportError> {
        r.validate()
            .map_err(|e| TransportError::Invalid(e.to_string()))?;
        if self.sent.len() >= QUEUE_MESSAGES {
            return Err(TransportError::Full);
        }
        self.sent.push(r);
        Ok(())
    }
    fn poll(&mut self) -> Option<TransportEvent> {
        self.events.pop_front()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::io::Cursor;
    use std::time::{Duration, Instant};

    struct ChannelReader {
        receiver: mpsc::Receiver<Vec<u8>>,
        current: Cursor<Vec<u8>>,
    }
    impl Read for ChannelReader {
        fn read(&mut self, out: &mut [u8]) -> io::Result<usize> {
            while self.current.position() as usize == self.current.get_ref().len() {
                match self.receiver.recv() {
                    Ok(bytes) => self.current = Cursor::new(bytes),
                    Err(_) => return Ok(0),
                }
            }
            self.current.read(out)
        }
    }
    fn wait(t: &mut BridgeTransport) -> TransportEvent {
        let start = Instant::now();
        loop {
            if let Some(event) = t.poll() {
                return event;
            }
            assert!(
                start.elapsed() < Duration::from_secs(2),
                "bridge reader timed out"
            );
            thread::sleep(Duration::from_millis(1));
        }
    }
    fn request(id: u64, method: Method, params: serde_json::Value) -> Request {
        Request::new(
            RequestId::Integer(id),
            method,
            params.as_object().unwrap().clone(),
        )
        .unwrap()
    }
    fn channel_transport() -> (mpsc::Sender<Vec<u8>>, BridgeTransport) {
        let (tx, rx) = mpsc::channel();
        (
            tx,
            BridgeTransport::from_io(
                ChannelReader {
                    receiver: rx,
                    current: Cursor::new(vec![]),
                },
                io::sink(),
            ),
        )
    }
    #[test]
    fn bounded_reader_rejects_oversize_and_terminated_input() {
        assert!(read_bounded_line(&mut Cursor::new(vec![b'x'; MAX_MESSAGE_BYTES + 1])).is_err());
        assert_eq!(
            read_bounded_line(&mut Cursor::new(b"partial".to_vec()))
                .unwrap_err()
                .kind(),
            io::ErrorKind::UnexpectedEof
        );
        assert_eq!(
            read_bounded_line(&mut Cursor::new(b"{}\n".to_vec())).unwrap(),
            Some(b"{}\n".to_vec())
        );
    }
    #[test]
    fn queue_coalesces_snapshots_without_losing_results() {
        let q = Queue::new();
        let inventory = |revision| {
            Incoming::Event(Box::new(BridgeEvent::Inventory {
                revision,
                payload: crate::protocol::Inventory { sessions: vec![] },
            }))
        };
        q.push(inventory(1), 10);
        q.push(inventory(2), 10);
        assert!(
            matches!(q.pop(), Some(TransportEvent::Message(Incoming::Event(e))) if matches!(*e, BridgeEvent::Inventory { revision: 2, .. }))
        );
        let result=protocol::decode_incoming(b"{\"event\":\"reply_result\",\"revision\":3,\"payload\":{\"request_id\":1,\"outcome\":\"uncertain\",\"message\":\"lost\"}}\n").unwrap();
        q.push(result.clone(), 10);
        q.push(inventory(4), 10);
        q.push(inventory(5), 10);
        assert_eq!(q.pop(), Some(TransportEvent::Message(result)));
    }
    #[test]
    fn initialization_is_required_negotiated_and_fails_closed() {
        let (tx, mut t) = channel_transport();
        let refresh = || request(2, Method::InventoryRefresh, json!({}));
        assert!(matches!(t.send(refresh()), Err(TransportError::Invalid(_))));
        t.send(request(
            1,
            Method::Initialize,
            json!({"protocol":1,"capabilities":["layout"]}),
        ))
        .unwrap();
        assert!(matches!(t.send(refresh()), Err(TransportError::Invalid(_))));
        tx.send(b"{\"id\":1,\"result\":{\"protocol\":1,\"capabilities\":[\"layout\"]}}\n".to_vec())
            .unwrap();
        assert!(matches!(
            wait(&mut t),
            TransportEvent::Message(Incoming::Response(_))
        ));
        assert!(t.is_ready());
        t.send(refresh()).unwrap();
        drop(tx);

        for bad in [
            b"{\"id\":1,\"result\":{\"protocol\":2,\"capabilities\":[]}}\n".as_slice(),
            b"{\"id\":1,\"result\":{\"protocol\":1,\"capabilities\":[\"not-requested\"]}}\n"
                .as_slice(),
            b"{\"id\":7,\"result\":{\"protocol\":1,\"capabilities\":[]}}\n".as_slice(),
        ] {
            let (tx, mut t) = channel_transport();
            t.send(request(1, Method::Initialize, json!({"protocol":1})))
                .unwrap();
            tx.send(bad.to_vec()).unwrap();
            assert!(matches!(wait(&mut t), TransportEvent::Failed(_)));
            assert!(!t.is_ready());
            assert!(matches!(t.send(refresh()), Err(TransportError::Closed)));
            assert!(t.poll().is_none());
            drop(tx);
        }
    }
    #[test]
    fn bounded_queue_backpressures_results_without_loss_and_evicts_snapshots() {
        let q = Arc::new(Queue::new());
        let result = protocol::decode_incoming(b"{\"event\":\"reply_result\",\"revision\":3,\"payload\":{\"request_id\":1,\"outcome\":\"uncertain\",\"message\":\"lost\"}}\n").unwrap();
        for _ in 0..QUEUE_MESSAGES {
            q.push(result.clone(), 10);
        }
        let (done_tx, done_rx) = mpsc::channel();
        let thread_queue = q.clone();
        let extra = result.clone();
        let producer = thread::spawn(move || {
            thread_queue.push(extra, 10);
            done_tx.send(()).unwrap();
        });
        assert!(done_rx.try_recv().is_err());
        assert_eq!(q.pop(), Some(TransportEvent::Message(result.clone())));
        done_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        producer.join().unwrap();
        for _ in 0..QUEUE_MESSAGES {
            assert_eq!(q.pop(), Some(TransportEvent::Message(result.clone())));
        }
        assert!(q.pop().is_none());
        let snapshot = |revision| {
            Incoming::Event(Box::new(BridgeEvent::Inventory {
                revision,
                payload: crate::protocol::Inventory { sessions: vec![] },
            }))
        };
        q.push(snapshot(10), QUEUE_BYTES - 100);
        q.push(snapshot(5), 1);
        q.push(result.clone(), 200);
        assert_eq!(q.pop(), Some(TransportEvent::Message(result)));
        assert!(q.pop().is_none());
    }
    #[cfg(unix)]
    #[test]
    fn bridge_process_kills_stuck_child_only_after_timeout() {
        use std::os::unix::process::ExitStatusExt;

        let mut child = Command::new("python3")
            .args([
                "-I",
                "-B",
                "-c",
                "import sys, time; sys.stdin.buffer.read(); time.sleep(30)",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .expect("python3 is required to exercise bridge shutdown");
        let stdin = child.stdin.take().unwrap();
        let stdout = child.stdout.take().unwrap();
        let mut bridge = BridgeProcess {
            child,
            transport: BridgeTransport::from_io(stdout, stdin),
            stderr_bytes: Arc::new(Mutex::new(0)),
        };
        let grace = Duration::from_millis(100);
        let started = Instant::now();
        bridge.shutdown(grace);
        let elapsed = started.elapsed();
        let status = bridge.child.try_wait().unwrap().unwrap();
        assert!(
            elapsed >= grace,
            "bridge was killed before the grace period"
        );
        assert!(
            elapsed < Duration::from_secs(5),
            "bridge shutdown was not bounded"
        );
        assert_eq!(status.signal(), Some(9));
    }
    #[cfg(unix)]
    #[test]
    fn bridge_process_drop_waits_for_in_flight_mutation() {
        // Exercise the real Python serve() finalizer with synthetic dependencies and
        // a gated mutation; this never connects to Herdr or touches user settings.
        const BRIDGE: &str = r#"
import asyncio, pathlib, sys, types
sys.path.insert(0, sys.argv[1])
root = pathlib.Path(sys.argv[2])
def stub(name, **members):
    module = types.ModuleType(name)
    module.__dict__.update(members)
    sys.modules[name] = module
stub('aiheaddev.bridge.runtime', LiveRuntime=object, logs=lambda *a: None)
stub('aiheaddev.bridge.settings', SettingsService=object, catalog_get=None,
     folders_list=None, folders_test=None)
stub('aiheaddev.bridge.demo', DemoRuntime=object, DemoSettings=object)
from aiheaddev.bridge.server import BridgeServer

def record(line):
    with (root / 'trace').open('a') as trace:
        trace.write(line + '\n')

class Runtime:
    def __init__(self, emit, **kwargs): pass
    async def refresh(self): pass
    async def launch(self, method, params):
        (root / 'started').write_text('started')
        while not (root / 'release').exists():
            await asyncio.sleep(0.005)
        record('mutation finished')
        return 'accepted', 'finished', None
    async def close(self): record('runtime closed')

class ObservedReader:
    def __init__(self, reader): self.reader = reader
    async def readline(self):
        line = await self.reader.readline()
        if not line: (root / 'eof').write_text('eof')
        return line

async def main():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=2 * 1024 * 1024)
    read_pipe, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    write_pipe, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer)
    writer = asyncio.StreamWriter(write_pipe, protocol, reader, loop)
    await BridgeServer(ObservedReader(reader), writer, runtime_factory=Runtime,
                       settings_factory=object, poll_interval=600).serve()
    record('serve returned')
    read_pipe.close()
    writer.close()
    await writer.wait_closed()

asyncio.run(main())
"#;
        struct TempDir(std::path::PathBuf);
        impl Drop for TempDir {
            fn drop(&mut self) {
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }
        let unique = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = TempDir(
            std::env::temp_dir().join(format!("aihead-transport-{}-{unique}", std::process::id())),
        );
        std::fs::create_dir(&dir.0).unwrap();
        let src = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src");
        let mut child = Command::new("python3")
            .args(["-I", "-B", "-u", "-c", BRIDGE])
            .arg(src)
            .arg(&dir.0)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .expect("python3 is required to exercise the bridge finalizer");
        let stdin = child.stdin.take().unwrap();
        let stdout = child.stdout.take().unwrap();
        let mut bridge = BridgeProcess {
            child,
            transport: BridgeTransport::from_io(stdout, stdin),
            stderr_bytes: Arc::new(Mutex::new(0)),
        };
        bridge
            .transport
            .send(request(1, Method::Initialize, json!({"protocol": 1})))
            .unwrap();
        assert!(matches!(
            wait(&mut bridge.transport),
            TransportEvent::Message(Incoming::Response(_))
        ));
        bridge
            .transport
            .send(request(
                2,
                Method::LaunchMaster,
                json!({"project_path":"/synthetic", "name":"test", "harness":"pi", "model":"fake"}),
            ))
            .unwrap();
        assert!(matches!(
            wait(&mut bridge.transport),
            TransportEvent::Message(Incoming::Response(r))
                if r.id == RequestId::Integer(2) && r.result == Ok(json!({"pending": true}))
        ));
        let started = Instant::now();
        while !dir.0.join("started").exists() {
            assert!(
                started.elapsed() < Duration::from_secs(5),
                "mutation did not start"
            );
            thread::sleep(Duration::from_millis(1));
        }

        let (done_tx, done_rx) = mpsc::channel();
        let dropper = thread::spawn(move || {
            drop(bridge);
            let _ = done_tx.send(());
        });
        let started = Instant::now();
        let mut exited_early = false;
        while !dir.0.join("eof").exists() && started.elapsed() < Duration::from_secs(3) {
            if done_rx.try_recv().is_ok() {
                exited_early = true;
                break;
            }
            thread::sleep(Duration::from_millis(1));
        }
        let saw_eof = dir.0.join("eof").exists();
        exited_early |= done_rx.try_recv().is_ok();
        let finished_before_release = dir.0.join("trace").exists();
        std::fs::write(dir.0.join("release"), "release").unwrap();
        if !exited_early {
            done_rx.recv_timeout(Duration::from_secs(3)).unwrap();
        }
        dropper.join().unwrap();
        let trace = std::fs::read_to_string(dir.0.join("trace")).unwrap_or_default();
        assert!(saw_eof, "bridge stdin was not closed before shutdown");
        assert!(
            !exited_early,
            "bridge was killed while the mutation was pending"
        );
        assert!(
            !finished_before_release,
            "mutation completed before being released"
        );
        assert_eq!(trace, "mutation finished\nruntime closed\nserve returned\n");
    }
    #[test]
    fn malformed_and_eof_are_reported() {
        let cases: &[(&[u8], TransportEvent)] = &[
            (
                b"not json\n",
                TransportEvent::Failed("Malformed bridge message".into()),
            ),
            (
                b"partial",
                TransportEvent::Failed(format!(
                    "Bridge read failed: {}",
                    io::ErrorKind::UnexpectedEof
                )),
            ),
            (b"", TransportEvent::Closed),
        ];
        for (input, expected) in cases {
            let mut t = BridgeTransport::from_io(Cursor::new(input.to_vec()), io::sink());
            // Give the reader thread time to run, but still fail if its event is lost.
            assert_eq!(&wait(&mut t), expected);
            assert_eq!(t.poll(), None);
        }
    }
}
