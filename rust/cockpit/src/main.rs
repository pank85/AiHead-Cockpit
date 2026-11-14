//! Standalone terminal executable. Typed outcomes go only to the inherited wrapper pipe.
mod product;

use aihead_cockpit::{
    panel::{ActionId, UiAction},
    protocol::{
        BridgeError, Incoming, Method, Outcome, Request, RequestId, Response, UiOutcome, PROTOCOL,
    },
    shell::Shell,
    terminal::{CrosstermControl, TerminalGuard},
    transport::{BridgeProcess, Transport, TransportEvent},
};
use crossterm::event;
use product::{Mailbox, ProductPanel};
use ratatui::{backend::CrosstermBackend, Terminal};
use serde_json::json;
use std::{
    cell::RefCell,
    collections::{HashMap, VecDeque},
    env,
    fs::File,
    io::{self, Write},
    os::{fd::FromRawFd, unix::fs::FileTypeExt},
    path::PathBuf,
    rc::Rc,
    time::Duration,
};

#[derive(Debug)]
struct Args {
    python: PathBuf,
    outcome_fd: i32,
    session: Option<String>,
    demo: bool,
}
fn parse_args<I: IntoIterator<Item = String>>(args: I) -> Result<Args, String> {
    let mut args = args.into_iter();
    let mut python = None;
    let mut fd = None;
    let mut session = None;
    let mut demo = false;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--python" if python.is_none() => {
                python = Some(PathBuf::from(
                    args.next().ok_or("--python requires a path")?,
                ))
            }
            "--outcome-fd" if fd.is_none() => {
                fd = Some(
                    args.next()
                        .ok_or("--outcome-fd requires a descriptor")?
                        .parse::<i32>()
                        .map_err(|_| "Invalid outcome descriptor")?,
                )
            }
            "--session" if session.is_none() => {
                session = Some(args.next().ok_or("--session requires a name")?)
            }
            "--demo" if !demo => demo = true,
            _ => return Err(format!("Unknown or repeated argument: {arg}")),
        }
    }
    let python = python.ok_or("--python is required")?;
    let outcome_fd = fd.ok_or("--outcome-fd is required")?;
    if !python.is_absolute() {
        return Err("--python must be absolute".into());
    }
    if outcome_fd < 3 {
        return Err("outcome descriptor must be a private pipe (fd >= 3)".into());
    }
    if session
        .as_ref()
        .is_some_and(|s: &String| s.trim().is_empty())
    {
        return Err("session cannot be empty".into());
    }
    Ok(Args {
        python,
        outcome_fd,
        session,
        demo,
    })
}
fn mutation(method: Method) -> bool {
    matches!(
        method,
        Method::SettingsSave
            | Method::LaunchMaster
            | Method::LaunchErgo
            | Method::LaunchOrchestration
            | Method::LaunchCouncil
            | Method::AiHeadOpen
            | Method::WorkspaceDelete
            | Method::ReplySubmit
            | Method::TerminalInput
    )
}
struct App {
    shell: Shell,
    mailbox: Rc<RefCell<Mailbox>>,
    outstanding: HashMap<RequestId, Method>,
    project: String,
    session: Option<String>,
    ready: bool,
    outcome: Option<UiOutcome>,
}
impl App {
    fn new(project: String, session: Option<String>) -> Self {
        Self {
            shell: Shell::new(),
            mailbox: Rc::new(RefCell::new(Mailbox::default())),
            outstanding: HashMap::new(),
            project,
            session,
            ready: false,
            outcome: None,
        }
    }
    fn synthetic(
        &mut self,
        id: RequestId,
        method: Method,
        message: &str,
        uncertain: bool,
    ) -> Vec<UiAction> {
        self.shell.dispatch_failed(&id);
        self.shell.apply_incoming(Incoming::Response(Response {
            id,
            result: Err(BridgeError {
                code: "transport_failed".into(),
                message: message.into(),
                field_errors: None,
                outcome: Some(if uncertain && mutation(method) {
                    Outcome::Uncertain
                } else {
                    Outcome::Rejected
                }),
            }),
        }))
    }
    fn actions<T: Transport>(&mut self, transport: &mut T, actions: Vec<UiAction>) {
        let mut queue: VecDeque<_> = actions.into();
        while let Some(action) = queue.pop_front() {
            match action {
                UiAction::Dispatch(req) => {
                    let id = req.id.clone();
                    let method = req.method;
                    match transport.send(req) {
                        Ok(()) => {
                            self.outstanding.insert(id, method);
                        }
                        Err(e) => {
                            self.shell.status = format!("Bridge send failed: {e:?}");
                            queue.extend(self.synthetic(
                                id,
                                method,
                                "Bridge did not accept request",
                                false,
                            ));
                        }
                    }
                }
                UiAction::Finish(outcome) => self.outcome = Some(outcome),
                UiAction::Activate(action) => {
                    if action == ActionId::Quit {
                        self.outcome = Some(UiOutcome::Quit);
                        continue;
                    }
                    if action == ActionId::OpenInHerdr {
                        let Some(target) = self.shell.active_target() else {
                            self.shell.status = "Select a workspace to open in Herdr".into();
                            continue;
                        };
                        let session = self
                            .shell
                            .inventory
                            .sessions
                            .iter()
                            .find(|s| s.socket_path == target.socket_path)
                            .and_then(|s| s.session.clone());
                        if let Some(session) = session {
                            self.outcome = Some(UiOutcome::Attach {
                                session,
                                workspace_id: Some(target.workspace_id.clone()),
                                tab_id: target.tab_id.clone(),
                                pane_id: target.pane_id.clone(),
                            });
                        } else {
                            self.shell.status =
                                "Cannot open: selected socket has no verified session identity"
                                    .into();
                        }
                        continue;
                    }
                    if !self.ready && action != ActionId::TaskDetails {
                        self.shell.status = "Bridge is disconnected; action unavailable".into();
                        continue;
                    }
                    let details = self
                        .shell
                        .task_details()
                        .cloned()
                        .map(serde_json::Value::Object);
                    let target = self.shell.active_target().cloned();
                    let (panel, initial) = ProductPanel::new(
                        action,
                        self.project.clone(),
                        self.session.clone(),
                        target,
                        details,
                        self.shell.reserve_request_id(),
                        self.mailbox.clone(),
                    );
                    self.shell.set_panel(Box::new(panel));
                    if let Some(request) = initial {
                        queue.push_back(UiAction::Dispatch(request));
                    }
                }
            }
        }
    }
    fn incoming<T: Transport>(&mut self, transport: &mut T, message: Incoming) {
        if let Incoming::Response(r) = &message {
            if r.result
                .as_ref()
                .ok()
                .and_then(|v| v.get("pending"))
                .and_then(serde_json::Value::as_bool)
                != Some(true)
            {
                self.outstanding.remove(&r.id);
            }
        }
        if let Incoming::Event(e) = &message {
            if let aihead_cockpit::protocol::BridgeEvent::OperationStatus { payload, .. } =
                e.as_ref()
            {
                if payload.outcome != Outcome::Pending {
                    self.outstanding.remove(&payload.request_id);
                }
            }
        }
        let actions = self.shell.apply_incoming(message);
        self.actions(transport, actions);
        let select = self.mailbox.borrow_mut().select.take();
        if let Some(target) = select {
            let actions = self.shell.select(target);
            self.actions(transport, actions);
        }
    }
    fn disconnected<T: Transport>(&mut self, transport: &mut T, reason: &str) {
        self.ready = false;
        self.shell.reset_bridge();
        let outstanding = std::mem::take(&mut self.outstanding);
        for (id, method) in outstanding {
            let actions = self.synthetic(id, method, reason, true);
            self.actions(transport, actions);
        }
        self.shell.status=format!("Bridge disconnected: {reason}. Dispatched changes may be uncertain; never replay automatically.");
    }
}
fn run(args: &Args) -> Result<UiOutcome, Box<dyn std::error::Error>> {
    let project = env::current_dir()?
        .to_str()
        .ok_or("Project path is not valid UTF-8")?
        .to_owned();
    let mut bridge = BridgeProcess::spawn(&args.python)?;
    let mut app = App::new(project, args.session.clone());
    let init = Request::new(
        app.shell.reserve_request_id(),
        Method::Initialize,
        json!({"protocol":PROTOCOL,"capabilities":[],"session":args.session,"demo":args.demo})
            .as_object()
            .unwrap()
            .clone(),
    )?;
    bridge
        .transport
        .send(init)
        .map_err(|e| format!("Failed to initialize bridge: {e:?}"))?;
    let mut guard = TerminalGuard::enter(CrosstermControl::new(io::stdout()))?;
    let mut terminal = Terminal::new(CrosstermBackend::new(io::stdout()))?;
    app.shell.status = "Connecting to bridge…".into();
    while app.outcome.is_none() {
        while let Some(event) = bridge.transport.poll() {
            match event {
                TransportEvent::Message(message) => {
                    let became_ready = !app.ready && bridge.transport.is_ready();
                    if became_ready {
                        app.ready = true;
                        app.shell.status = "Connected".into();
                    }
                    app.incoming(&mut bridge.transport, message);
                    if became_ready {
                        let req = Request::new(
                            app.shell.reserve_request_id(),
                            Method::InventoryRefresh,
                            Default::default(),
                        )?;
                        app.actions(&mut bridge.transport, vec![UiAction::Dispatch(req)]);
                    }
                }
                TransportEvent::Failed(reason) => app.disconnected(&mut bridge.transport, &reason),
                TransportEvent::Closed => {
                    app.disconnected(&mut bridge.transport, "connection closed")
                }
            }
        }
        terminal.draw(|frame| app.shell.render(frame))?;
        if event::poll(Duration::from_millis(40))? {
            let e = event::read()?;
            let actions = app.shell.handle_event(e);
            app.actions(&mut bridge.transport, actions);
        }
    }
    drop(terminal);
    guard.restore()?;
    Ok(app.outcome.take().unwrap_or(UiOutcome::Quit))
}
fn main() {
    let result = (|| -> Result<(), Box<dyn std::error::Error>> {
        let args = parse_args(env::args().skip(1))?;
        // SAFETY: the wrapper explicitly transfers ownership of this inherited fd;
        // it is >= 3, checked to be a pipe, and closed exactly once by File.
        let mut outcome = unsafe { File::from_raw_fd(args.outcome_fd) };
        if !outcome.metadata()?.file_type().is_fifo() {
            return Err("outcome descriptor is not a private pipe".into());
        }
        let value = run(&args)?;
        value.validate()?;
        serde_json::to_writer(&mut outcome, &value)?;
        outcome.write_all(b"\n")?;
        outcome.flush()?;
        Ok(())
    })();
    if let Err(e) = result {
        eprintln!("aihead-cockpit: {e}");
        std::process::exit(1);
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use aihead_cockpit::transport::FakeTransport;
    #[test]
    fn cli_requires_private_fd_and_absolute_python() {
        assert!(
            parse_args(["--python", "/python", "--outcome-fd", "1"].map(str::to_owned)).is_err()
        );
        assert!(
            parse_args(["--python", "relative", "--outcome-fd", "3"].map(str::to_owned)).is_err()
        );
        assert!(
            parse_args(["--python", "/python", "--outcome-fd", "3"].map(str::to_owned)).is_ok()
        );
    }
    #[test]
    fn fake_transport_receives_actions_from_panel_dispatch() {
        let mut app = App::new("/project".into(), None);
        app.ready = true;
        let mut transport = FakeTransport::default();
        app.actions(&mut transport, vec![UiAction::Activate(ActionId::Settings)]);
        assert_eq!(transport.sent.len(), 1);
        assert_eq!(transport.sent[0].method, Method::ProjectOpen);
    }
    #[test]
    fn open_never_guesses_session() {
        let mut app = App::new("/project".into(), None);
        let mut transport = FakeTransport::default();
        app.actions(
            &mut transport,
            vec![UiAction::Activate(ActionId::OpenInHerdr)],
        );
        assert!(app.outcome.is_none());
    }
    #[test]
    fn all_six_actions_dispatch_and_incoming_panel_actions_are_sent() {
        for action in ActionId::PRIMARY {
            let mut app = App::new("/project".into(), None);
            app.ready = true;
            let mut transport = FakeTransport::default();
            app.actions(&mut transport, vec![UiAction::Activate(action)]);
            assert_eq!(transport.sent.len(), 1);
            let first = transport.sent[0].clone();
            assert_eq!(first.method, Method::ProjectOpen);
            assert_eq!(first.params["path"], "/project");
            let sections = json!({"projects":{},"squad":{"scout":{"kind":"h","model":"m"}},"orchestration":{},"council":{},"ai_head":{}});
            let value = json!({"token":"t","revision":0,"project_path":"/project","destination":"/project/config","scope":"local","catalog":{},"sections":sections});
            app.incoming(
                &mut transport,
                Incoming::Response(Response {
                    id: first.id,
                    result: Ok(value),
                }),
            );
            assert_eq!(transport.sent.len(), 2);
            assert_eq!(transport.sent[1].method, Method::SettingsValidate);
            assert_eq!(transport.sent[1].params["token"], "t");
        }
    }
    #[test]
    fn cancel_sends_only_discard_and_quit_is_typed() {
        let mut app = App::new("/project".into(), None);
        app.ready = true;
        let mut t = FakeTransport::default();
        app.actions(&mut t, vec![UiAction::Activate(ActionId::Settings)]);
        let first = t.sent[0].clone();
        let sections =
            json!({"projects":{},"squad":{},"orchestration":{},"council":{},"ai_head":{}});
        let doc = json!({"token":"t","revision":0,"project_path":"/project","destination":"/project/config","scope":"local","catalog":{},"sections":sections});
        app.incoming(
            &mut t,
            Incoming::Response(Response {
                id: first.id,
                result: Ok(doc),
            }),
        );
        let validation = t.sent[1].clone();
        app.incoming(
            &mut t,
            Incoming::Response(Response {
                id: validation.id,
                result: Ok(json!({"valid":true,"revision":1,"sections":sections})),
            }),
        );
        let actions = app.shell.handle_event(crossterm::event::Event::Key(
            crossterm::event::KeyEvent::new(
                crossterm::event::KeyCode::Esc,
                crossterm::event::KeyModifiers::NONE,
            ),
        ));
        app.actions(&mut t, actions);
        let discard = t.sent.last().unwrap();
        assert_eq!(discard.method, Method::SettingsValidate);
        assert_eq!(discard.params["discard"], true);
        assert!(!t.sent.iter().any(|r| r.method == Method::SettingsSave));
        app.actions(&mut t, vec![UiAction::Activate(ActionId::Quit)]);
        assert_eq!(app.outcome, Some(UiOutcome::Quit));
    }
}
