//! Herdr-inspired read-only shell; product behavior is delegated to panel hooks.
use crate::{
    input::{self, Focus, TerminalInput},
    layout::{self, MappedLayout},
    output::{self, Scroll},
    panel::{ActionId, Panel, PanelContext, RequestIds, UiAction},
    protocol::{
        BridgeEvent, Incoming, Inventory, LayoutState, Lease, Method, Outcome, PaneFrame, Request,
        RequestId, Target, MAX_TEXT_BYTES,
    },
    transport::{Transport, TransportEvent},
};
use crossterm::event::{Event, KeyCode, KeyEventKind, KeyModifiers, MouseButton, MouseEventKind};
use ratatui::{
    layout::Rect,
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Clear, Paragraph},
    Frame,
};
use serde_json::{json, Map, Value};
use std::collections::{HashMap, HashSet, VecDeque};
use unicode_width::UnicodeWidthStr;

const MAX_DRAFTS: usize = 64;
const MAX_FRAMES: usize = 128;
const MAX_TARGET_STATES: usize = 128;
const MAX_PENDING: usize = 64;
#[derive(Debug, Clone)]
struct FrameEntry {
    revision: u64,
    frame: PaneFrame,
}
#[derive(Debug, Clone)]
struct Pending {
    target: Target,
    lease: Lease,
    reply_text: Option<String>,
}
#[derive(Debug, Clone)]
enum Hit {
    Select(Target),
    Tab(Target),
    Pane(Target),
    Action(ActionId),
    More,
}
#[derive(Debug, Clone)]
struct HitRegion {
    area: Rect,
    hit: Hit,
}
#[derive(Debug, Clone)]
struct Row {
    target: Option<Target>,
    title: String,
    detail: String,
    state: String,
    agent: bool,
}

pub struct Shell {
    pub inventory: Inventory,
    pub focus: Focus,
    pub status: String,
    active: Option<Target>,
    selection_valid: bool,
    nav: usize,
    rows: Vec<Row>,
    frames: HashMap<Target, FrameEntry>,
    drafts: HashMap<Target, String>,
    scroll: HashMap<Target, Scroll>,
    pending: HashMap<RequestId, Pending>,
    terminal_queue: VecDeque<(Lease, TerminalInput)>,
    feedback: HashMap<Target, String>,
    inventory_revision: Option<u64>,
    max_revision: u64,
    selection_barrier: u64,
    request_ids: RequestIds,
    panel: Option<Box<dyn Panel>>,
    hits: Vec<HitRegion>,
    last_area: Rect,
    sidebar_overlay: bool,
    menu: bool,
    menu_index: usize,
}
impl Default for Shell {
    fn default() -> Self {
        Self::new()
    }
}
impl Shell {
    pub fn new() -> Self {
        Self {
            inventory: Inventory { sessions: vec![] },
            focus: Focus::Chrome,
            status: "Disconnected".into(),
            active: None,
            selection_valid: false,
            nav: 0,
            rows: vec![],
            frames: HashMap::new(),
            drafts: HashMap::new(),
            scroll: HashMap::new(),
            pending: HashMap::new(),
            terminal_queue: VecDeque::new(),
            feedback: HashMap::new(),
            inventory_revision: None,
            max_revision: 0,
            selection_barrier: 0,
            request_ids: RequestIds::default(),
            panel: None,
            hits: vec![],
            last_area: Rect::default(),
            sidebar_overlay: false,
            menu: false,
            menu_index: 0,
        }
    }
    pub fn active_target(&self) -> Option<&Target> {
        self.active.as_ref()
    }
    pub fn navigated_row(&self) -> usize {
        self.nav
    }
    pub fn set_panel(&mut self, panel: Box<dyn Panel>) {
        self.panel = Some(panel);
        self.menu = false;
    }
    pub fn has_panel(&self) -> bool {
        self.panel.is_some()
    }
    pub fn reserve_request_id(&self) -> RequestId {
        self.request_ids.reserve()
    }
    pub fn draft(&self, target: &Target) -> &str {
        self.drafts.get(target).map_or("", String::as_str)
    }
    pub fn scroll_state(&self, target: &Target) -> Option<&Scroll> {
        self.scroll.get(target)
    }
    pub fn frame(&self, target: &Target) -> Option<&PaneFrame> {
        self.frames.get(target).map(|f| &f.frame)
    }
    pub fn pending_count(&self) -> usize {
        self.pending.len()
    }
    pub fn task_details(&self) -> Option<&Map<String, Value>> {
        let t = self.active.as_ref()?;
        self.inventory
            .sessions
            .iter()
            .find(|s| s.socket_path == t.socket_path)?
            .workspaces
            .iter()
            .find(|w| w.workspace_id == t.workspace_id)
            .map(|w| &w.metadata)
    }
    fn context(&self) -> PanelContext {
        PanelContext {
            area: self.last_area,
            active_target: self.active.clone(),
            focus: self.focus,
            pending: !self.pending.is_empty(),
            request_ids: self.request_ids.clone(),
        }
    }
    fn request(&mut self, method: Method, params: Value) -> Option<Request> {
        let id = self.reserve_request_id();
        Request::new(id, method, params.as_object()?.clone()).ok()
    }
    fn same_scope(a: &Target, b: &Target) -> bool {
        a.socket_path == b.socket_path && a.workspace_id == b.workspace_id && a.tab_id == b.tab_id
    }
    fn workspace_target(socket: &str, w: &crate::protocol::Workspace) -> Target {
        let tab = w.tabs.first();
        let pane = tab.and_then(|t| t.panes.first());
        Target {
            socket_path: socket.into(),
            workspace_id: w.workspace_id.clone(),
            tab_id: tab.map(|t| t.tab_id.clone()),
            pane_id: pane.map(|p| p.pane_id.clone()),
            terminal_id: pane.and_then(|p| p.terminal_id.clone()),
        }
    }
    fn rebuild_rows(&mut self) {
        let mut rows = Vec::new();
        for s in &self.inventory.sessions {
            for w in &s.workspaces {
                let target = Self::workspace_target(&s.socket_path, w);
                let branch = w
                    .metadata
                    .get("branch")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                let state = match s.connection {
                    crate::protocol::Connection::Connected => "connected",
                    crate::protocol::Connection::Offline => "offline",
                    crate::protocol::Connection::Error => "error",
                };
                rows.push(Row {
                    target: Some(target.clone()),
                    title: w.label.clone(),
                    detail: branch.into(),
                    state: state.into(),
                    agent: false,
                });
                for a in &w.agents {
                    let pane = a.pane_id.as_ref().and_then(|id| {
                        let mut candidates = w.tabs.iter().flat_map(|t| {
                            t.panes
                                .iter()
                                .filter(move |p| &p.pane_id == id)
                                .map(move |p| (t, p))
                        });
                        let first = candidates.next()?;
                        if candidates.next().is_none() {
                            Some(first)
                        } else {
                            None
                        }
                    });
                    let target = pane.map(|(t, p)| Target {
                        socket_path: s.socket_path.clone(),
                        workspace_id: w.workspace_id.clone(),
                        tab_id: Some(t.tab_id.clone()),
                        pane_id: Some(p.pane_id.clone()),
                        terminal_id: p.terminal_id.clone(),
                    });
                    let tab = pane
                        .map(|(t, _)| t.label.as_str())
                        .filter(|v| !v.is_empty());
                    let title = match tab {
                        Some(t) if !w.label.is_empty() => format!("{} · {}", w.label, t),
                        Some(t) => t.to_string(),
                        None => w.label.clone(),
                    };
                    rows.push(Row {
                        target,
                        title,
                        detail: a.agent.clone(),
                        state: a.state.clone(),
                        agent: true,
                    });
                }
            }
        }
        self.rows = rows;
        self.nav = self.nav.min(self.rows.len().saturating_sub(1));
    }
    /// Independent Cockpit selection; this only requests selection.set, never Herdr focus.
    pub fn select(&mut self, target: Target) -> Vec<UiAction> {
        if target.validate(false).is_err() {
            self.status = "Invalid selection".into();
            return vec![];
        }
        let canceled = self.terminal_queue.len();
        self.terminal_queue.clear(); // queued input has not been dispatched; never retarget it
        self.selection_barrier = self.max_revision;
        self.active = Some(target.clone());
        self.selection_valid = true;
        self.focus = Focus::Chrome;
        self.sidebar_overlay = false;
        self.frames.retain(|k, _| Self::same_scope(k, &target));
        self.status = self
            .feedback
            .get(&target)
            .cloned()
            .unwrap_or_else(|| "Loading selection".into());
        if canceled > 0 {
            self.status
                .push_str(&format!("; {canceled} queued inputs not sent"));
        }
        self.request(Method::SelectionSet, json!({"target":target}))
            .map(|r| vec![UiAction::Dispatch(r)])
            .unwrap_or_default()
    }
    fn selected_lease(&self) -> Option<Lease> {
        let target = self.active.as_ref()?;
        let entry = self.frames.get(target)?;
        if entry.revision <= self.selection_barrier {
            return None;
        }
        entry.frame.lease.clone()
    }
    fn current_pending(&self) -> bool {
        self.active
            .as_ref()
            .is_some_and(|t| self.pending.values().any(|p| &p.target == t))
    }
    fn remember_feedback(&mut self, target: Target, message: String) {
        if !self.feedback.contains_key(&target) && self.feedback.len() >= MAX_TARGET_STATES {
            if let Some(old) = self
                .feedback
                .keys()
                .find(|t| Some(*t) != self.active.as_ref())
                .cloned()
            {
                self.feedback.remove(&old);
            }
        }
        self.feedback.insert(target, message);
    }
    fn active_scroll(&mut self) -> Option<&mut Scroll> {
        let target = self.active.clone()?;
        if !self.scroll.contains_key(&target) && self.scroll.len() >= MAX_TARGET_STATES {
            self.status = "Scroll state capacity reached".into();
            return None;
        }
        Some(self.scroll.entry(target).or_default())
    }
    fn queue_mutation(&mut self, input: TerminalInput, reply: bool) -> Vec<UiAction> {
        let Some(lease) = self.selected_lease() else {
            self.status = "No verified live lease; select or refresh pane".into();
            return vec![];
        };
        // The existing controller has one global in-flight pane input. Queue
        // subsequent keystrokes locally and advance only after an accepted result.
        if !self.pending.is_empty() {
            let same_terminal_input = !reply
                && self
                    .pending
                    .values()
                    .all(|p| p.reply_text.is_none() && p.lease == lease);
            if !same_terminal_input {
                self.status = "Operation pending".into();
                return vec![];
            }
            let size = |input: &TerminalInput| match input {
                TerminalInput::Text(text) => text.len(),
                TerminalInput::Keys(keys) => keys.iter().map(String::len).sum(),
                TerminalInput::ReturnToChrome => 0,
            };
            let queued_bytes: usize = self
                .terminal_queue
                .iter()
                .map(|(_, input)| size(input))
                .sum();
            if self.terminal_queue.len() >= MAX_PENDING
                || queued_bytes.saturating_add(size(&input)) > MAX_TEXT_BYTES
            {
                self.status = "Terminal input queue capacity reached".into();
            } else {
                self.terminal_queue.push_back((lease, input));
                self.status = "Terminal input queued".into();
            }
            return vec![];
        }
        if reply {
            let readiness = &self.frames[&lease.target].frame.readiness;
            if !readiness.can_reply {
                self.status = format!(
                    "Reply unavailable: {}",
                    output::plain(&readiness.explanation)
                );
                return vec![];
            }
        }
        let (method, params, reply_text) = if reply {
            let TerminalInput::Text(s) = input else {
                return vec![];
            };
            if s.is_empty() {
                return vec![];
            }
            (
                Method::ReplySubmit,
                json!({"lease":lease,"text":s,"notify":true}),
                Some(s),
            )
        } else {
            match input {
                TerminalInput::Text(s) => {
                    (Method::TerminalInput, json!({"lease":lease,"text":s}), None)
                }
                TerminalInput::Keys(keys) => (
                    Method::TerminalInput,
                    json!({"lease":lease,"keys":keys}),
                    None,
                ),
                TerminalInput::ReturnToChrome => return vec![],
            }
        };
        let Some(request) = self.request(method, params) else {
            self.status = "Invalid input".into();
            return vec![];
        };
        self.pending.insert(
            request.id.clone(),
            Pending {
                target: lease.target.clone(),
                lease,
                reply_text,
            },
        );
        self.status = "Sending…".into();
        vec![UiAction::Dispatch(request)]
    }
    /// Call when the bounded writer rejects a request before enqueueing; it was not sent.
    pub fn dispatch_failed(&mut self, id: &RequestId) {
        if self.pending.remove(id).is_some() {
            let dropped = self.terminal_queue.len();
            self.terminal_queue.clear();
            self.status = format!(
                "Not sent: bridge queue unavailable ({dropped} queued inputs also not sent)"
            );
        }
    }
    fn finish_result(
        &mut self,
        id: &RequestId,
        outcome: Outcome,
        message: &str,
        lease: Option<&Lease>,
        target: Option<&Target>,
    ) -> Vec<UiAction> {
        let Some(p) = self.pending.get(id) else {
            return vec![];
        };
        if lease.is_some_and(|l| l != &p.lease) || target.is_some_and(|t| t != &p.target) {
            return vec![];
        }
        if outcome == Outcome::Pending {
            if self.active.as_ref() == Some(&p.target) {
                self.status = output::plain(message);
            }
            return vec![];
        }
        let p = self.pending.remove(id).unwrap();
        if outcome == Outcome::Accepted {
            if let Some(text) = &p.reply_text {
                if self.drafts.get(&p.target) == Some(text) {
                    self.drafts.remove(&p.target);
                }
            }
        }
        let mut msg = if outcome == Outcome::Uncertain {
            format!(
                "Uncertain — do not retry automatically: {}",
                output::plain(message)
            )
        } else {
            output::plain(message)
        };
        let can_advance = outcome == Outcome::Accepted
            && p.reply_text.is_none()
            && self.active.as_ref() == Some(&p.target)
            && self.selected_lease().as_ref() == Some(&p.lease)
            && self
                .terminal_queue
                .front()
                .is_some_and(|(lease, _)| lease == &p.lease);
        if !self.terminal_queue.is_empty() && !can_advance {
            let dropped = self.terminal_queue.len();
            self.terminal_queue.clear();
            msg.push_str(&format!("; {dropped} queued inputs not sent"));
        }
        self.remember_feedback(p.target.clone(), msg.clone());
        if self.active.as_ref() == Some(&p.target) {
            self.status = msg;
        }
        if can_advance {
            let (_, input) = self
                .terminal_queue
                .pop_front()
                .expect("queue checked above");
            return self.queue_mutation(input, false);
        }
        vec![]
    }
    /// Apply validated messages. Mutations remain tied to request and lease identities.
    pub fn apply_incoming(&mut self, message: Incoming) -> Vec<UiAction> {
        if let Incoming::Event(e) = &message {
            if e.validate().is_err() {
                self.status = "Invalid bridge event".into();
                return vec![];
            }
        }
        let context = self.context();
        let response = self.panel.as_mut().map(|p| p.on_bridge(&message, &context));
        if let Some(r) = response.as_ref() {
            if r.close {
                self.panel = None;
            }
        }
        let mut actions = vec![];
        match message {
            Incoming::Event(e) => {
                let revision = e.revision();
                self.max_revision = self.max_revision.max(revision);
                match *e {
                    BridgeEvent::Inventory { revision, payload } => {
                        if self.inventory_revision.is_none_or(|old| revision > old) {
                            self.inventory_revision = Some(revision);
                            self.inventory = payload;
                            if let Some(a) = &self.active {
                                let consistent = self
                                    .inventory
                                    .sessions
                                    .iter()
                                    .find(|s| {
                                        s.socket_path == a.socket_path
                                            && s.connection
                                                == crate::protocol::Connection::Connected
                                    })
                                    .and_then(|s| {
                                        s.workspaces
                                            .iter()
                                            .find(|w| w.workspace_id == a.workspace_id)
                                    })
                                    .is_some_and(|w| match &a.tab_id {
                                        None => true,
                                        Some(id) => w
                                            .tabs
                                            .iter()
                                            .find(|t| &t.tab_id == id)
                                            .is_some_and(|t| match &a.pane_id {
                                                None => true,
                                                Some(id) => t
                                                    .panes
                                                    .iter()
                                                    .find(|p| &p.pane_id == id)
                                                    .is_some_and(|p| {
                                                        p.terminal_id.as_ref().is_none_or(|term| {
                                                            a.terminal_id
                                                                .as_ref()
                                                                .is_none_or(|old| old == term)
                                                        })
                                                    }),
                                            }),
                                    });
                                if !consistent {
                                    self.selection_valid = false;
                                    self.selection_barrier = self.max_revision;
                                    self.frames.clear();
                                    self.status =
                                        "Selection stale or offline — reselect before sending"
                                            .into();
                                }
                            }
                            self.rebuild_rows();
                        }
                    }
                    BridgeEvent::PaneFrame { revision, payload } => {
                        let valid_scope = self
                            .active
                            .as_ref()
                            .is_some_and(|a| Self::same_scope(a, &payload.target));
                        let upgrade = self.active.as_ref().is_some_and(|a| {
                            Self::same_scope(a, &payload.target)
                                && a.pane_id == payload.target.pane_id
                                && a.pane_id.is_some()
                                && a.terminal_id.is_none()
                                && payload.lease.is_some()
                        });
                        let selected = self.active.as_ref() == Some(&payload.target) || upgrade;
                        let valid_layout = self
                            .inventory
                            .sessions
                            .iter()
                            .find(|s| s.socket_path == payload.target.socket_path)
                            .and_then(|s| {
                                s.workspaces
                                    .iter()
                                    .find(|w| w.workspace_id == payload.target.workspace_id)
                            })
                            .and_then(|w| {
                                w.tabs
                                    .iter()
                                    .find(|t| Some(&t.tab_id) == payload.target.tab_id.as_ref())
                            })
                            .is_some_and(|t| {
                                let ids: HashSet<&str> =
                                    t.panes.iter().map(|p| p.pane_id.as_str()).collect();
                                let contains = |id: &str| ids.contains(id);
                                payload.target.pane_id.as_deref().is_some_and(contains)
                                    && payload.layout.rects.iter().all(|r| contains(&r.pane_id))
                                    && payload
                                        .layout
                                        .focused_pane_id
                                        .as_deref()
                                        .is_none_or(contains)
                            });
                        let stale = self
                            .frames
                            .get(&payload.target)
                            .is_some_and(|f| revision <= f.revision);
                        if self.selection_valid
                            && valid_scope
                            && valid_layout
                            && revision > self.selection_barrier
                            && !stale
                            && (selected || payload.lease.is_none())
                        {
                            if upgrade {
                                // Enriching the same pane with its first verified terminal ID
                                // must not orphan a draft entered while selection loaded.
                                let old = self
                                    .active
                                    .replace(payload.target.clone())
                                    .expect("upgrade has active target");
                                if !self.drafts.contains_key(&payload.target) {
                                    if let Some(draft) = self.drafts.remove(&old) {
                                        self.drafts.insert(payload.target.clone(), draft);
                                    }
                                }
                                if !self.scroll.contains_key(&payload.target) {
                                    if let Some(scroll) = self.scroll.remove(&old) {
                                        self.scroll.insert(payload.target.clone(), scroll);
                                    }
                                }
                            }
                            if selected
                                && !self.current_pending()
                                && !self.feedback.contains_key(&payload.target)
                            {
                                let available =
                                    payload.lease.is_some() && payload.readiness.can_reply;
                                let availability = if available {
                                    "Reply available"
                                } else {
                                    "Reply unavailable"
                                };
                                let explanation = output::plain(&payload.readiness.explanation);
                                self.status = if explanation.is_empty() {
                                    availability.into()
                                } else {
                                    format!("{availability}: {explanation}")
                                };
                            }
                            if selected
                                && self.frames.len() >= MAX_FRAMES
                                && !self.frames.contains_key(&payload.target)
                            {
                                if let Some(old) = self
                                    .frames
                                    .keys()
                                    .find(|key| *key != &payload.target)
                                    .cloned()
                                {
                                    self.frames.remove(&old);
                                }
                            }
                            if self.frames.len() >= MAX_FRAMES
                                && !self.frames.contains_key(&payload.target)
                            {
                                self.status = "Pane preview capacity reached".into();
                            } else {
                                self.frames.insert(
                                    payload.target.clone(),
                                    FrameEntry {
                                        revision,
                                        frame: payload,
                                    },
                                );
                            }
                        }
                    }
                    BridgeEvent::ReplyResult { payload, .. } => actions.extend(self.finish_result(
                        &payload.request_id,
                        payload.outcome,
                        &payload.message,
                        payload.lease.as_ref(),
                        None,
                    )),
                    BridgeEvent::OperationStatus { payload, .. } => {
                        actions.extend(self.finish_result(
                            &payload.request_id,
                            payload.outcome,
                            &payload.message,
                            None,
                            payload.target.as_ref(),
                        ))
                    }
                }
            }
            Incoming::Response(r) => {
                if let Err(e) = r.result {
                    let outcome = e.outcome.unwrap_or(Outcome::Rejected);
                    actions.extend(self.finish_result(&r.id, outcome, &e.message, None, None));
                }
            }
        }
        if let Some(response) = response {
            actions.extend(response.actions);
        }
        actions
    }
    pub fn bridge_lost(&mut self, reason: &str) {
        let uncertain = self.current_pending();
        let queued = self.terminal_queue.len();
        self.terminal_queue.clear();
        let targets: Vec<_> = self.pending.drain().map(|(_, p)| p.target).collect();
        for target in targets {
            self.remember_feedback(
                target,
                "Uncertain — bridge disconnected; do not retry automatically".into(),
            );
        }
        self.status = if uncertain {
            format!(
                "Uncertain — bridge disconnected; do not retry automatically: {}",
                output::plain(reason)
            )
        } else {
            format!("Bridge disconnected: {}", output::plain(reason))
        };
        if queued > 0 {
            self.status
                .push_str(&format!("; {queued} queued inputs not sent"));
        }
        self.selection_valid = false;
        self.frames.clear();
    }
    /// Start a fresh bridge revision domain while preserving drafts and uncertain feedback.
    /// The caller must initialize the new transport and explicitly reselect its target.
    pub fn reset_bridge(&mut self) {
        self.bridge_lost("restarting");
        self.inventory = Inventory { sessions: vec![] };
        self.rows.clear();
        self.nav = 0;
        self.inventory_revision = None;
        self.max_revision = 0;
        self.selection_barrier = 0;
    }
    pub fn pump<T: Transport>(&mut self, transport: &mut T) -> Vec<UiAction> {
        let mut actions = vec![];
        while let Some(event) = transport.poll() {
            match event {
                TransportEvent::Message(m) => actions.extend(self.apply_incoming(m)),
                TransportEvent::Failed(e) => self.bridge_lost(&e),
                TransportEvent::Closed => self.bridge_lost("terminated"),
            }
        }
        actions
    }
    fn change_tab(&mut self, delta: isize) -> Vec<UiAction> {
        let Some(a) = self.active.as_ref() else {
            return vec![];
        };
        let Some(w) = self
            .inventory
            .sessions
            .iter()
            .find(|s| s.socket_path == a.socket_path)
            .and_then(|s| {
                s.workspaces
                    .iter()
                    .find(|w| w.workspace_id == a.workspace_id)
            })
        else {
            return vec![];
        };
        if w.tabs.is_empty() {
            return vec![];
        }
        let index = w
            .tabs
            .iter()
            .position(|t| Some(&t.tab_id) == a.tab_id.as_ref())
            .unwrap_or(0);
        let index = (index as isize + delta).rem_euclid(w.tabs.len() as isize) as usize;
        let t = &w.tabs[index];
        let p = t.panes.first();
        let target = Target {
            socket_path: a.socket_path.clone(),
            workspace_id: a.workspace_id.clone(),
            tab_id: Some(t.tab_id.clone()),
            pane_id: p.map(|p| p.pane_id.clone()),
            terminal_id: p.and_then(|p| p.terminal_id.clone()),
        };
        self.select(target)
    }
    fn change_pane(&mut self, delta: isize) -> Vec<UiAction> {
        let Some(a) = self.active.as_ref() else {
            return vec![];
        };
        let Some(tab) = self
            .inventory
            .sessions
            .iter()
            .find(|s| s.socket_path == a.socket_path)
            .and_then(|s| {
                s.workspaces
                    .iter()
                    .find(|w| w.workspace_id == a.workspace_id)
            })
            .and_then(|w| w.tabs.iter().find(|t| Some(&t.tab_id) == a.tab_id.as_ref()))
        else {
            return vec![];
        };
        if tab.panes.is_empty() {
            return vec![];
        }
        let index = tab
            .panes
            .iter()
            .position(|p| Some(&p.pane_id) == a.pane_id.as_ref())
            .unwrap_or(0);
        let index = (index as isize + delta).rem_euclid(tab.panes.len() as isize) as usize;
        let p = &tab.panes[index];
        let target = Target {
            socket_path: a.socket_path.clone(),
            workspace_id: a.workspace_id.clone(),
            tab_id: a.tab_id.clone(),
            pane_id: Some(p.pane_id.clone()),
            terminal_id: p.terminal_id.clone(),
        };
        self.select(target)
    }
    fn activate(&mut self, id: ActionId) -> Vec<UiAction> {
        self.menu = false;
        vec![UiAction::Activate(id)]
    }
    /// Alias useful to integrations that distinguish terminal events from bridge events.
    pub fn handle_event(&mut self, event: Event) -> Vec<UiAction> {
        self.handle(event)
    }
    pub fn handle(&mut self, event: Event) -> Vec<UiAction> {
        // Reserve Ctrl+C before modals, focus handlers, or pane-input queuing.
        if let Event::Key(key) = &event {
            if input::is_quit_key(*key) {
                return if key.kind == KeyEventKind::Release {
                    vec![]
                } else {
                    self.activate(ActionId::Quit)
                };
            }
        }
        if matches!(event, Event::Resize(..)) {
            self.hits.clear();
            self.sidebar_overlay = false;
        }
        if self.panel.is_some() {
            let ctx = self.context();
            let r = self.panel.as_mut().unwrap().handle(&event, &ctx);
            if r.close {
                self.panel = None;
            }
            return r.actions;
        }
        if self.menu {
            if let Event::Key(k) = event {
                if k.kind == KeyEventKind::Release {
                    return vec![];
                }
                match k.code {
                    KeyCode::Esc | KeyCode::F(10) => self.menu = false,
                    KeyCode::Up => {
                        self.menu_index =
                            (self.menu_index + ActionId::ALL.len() - 1) % ActionId::ALL.len()
                    }
                    KeyCode::Down => self.menu_index = (self.menu_index + 1) % ActionId::ALL.len(),
                    KeyCode::Enter => return self.activate(ActionId::ALL[self.menu_index]),
                    _ => {}
                }
            }
            return vec![];
        }
        if self.focus == Focus::Terminal {
            let input = match event {
                Event::Key(k) => input::terminal_key(k),
                Event::Paste(s) => input::terminal_paste(s),
                _ => None,
            };
            return match input {
                Some(TerminalInput::ReturnToChrome) => {
                    self.focus = Focus::Chrome;
                    self.status = "Chrome focus".into();
                    vec![]
                }
                Some(i) => self.queue_mutation(i, false),
                None => vec![],
            };
        }
        if self.focus == Focus::Reply {
            let Some(target) = self.active.clone() else {
                self.focus = Focus::Chrome;
                return vec![];
            };
            match event {
                Event::Key(k) if k.kind != KeyEventKind::Release => match k.code {
                    KeyCode::Esc => self.focus = Focus::Chrome,
                    KeyCode::Enter => {
                        let text = self.draft(&target).to_owned();
                        return self.queue_mutation(TerminalInput::Text(text), true);
                    }
                    KeyCode::Backspace => {
                        if let Some(d) = self.drafts.get_mut(&target) {
                            d.pop();
                        }
                    }
                    KeyCode::Char(c)
                        if !c.is_control()
                            && !k.modifiers.intersects(
                                KeyModifiers::CONTROL | KeyModifiers::ALT | KeyModifiers::SUPER,
                            ) =>
                    {
                        if self.drafts.contains_key(&target) || self.drafts.len() < MAX_DRAFTS {
                            let d = self.drafts.entry(target).or_default();
                            if d.len() + c.len_utf8() <= MAX_TEXT_BYTES {
                                d.push(c);
                            }
                        } else {
                            self.status = "Draft capacity reached".into();
                        }
                    }
                    _ => {}
                },
                Event::Paste(s) => {
                    if (self.drafts.contains_key(&target) || self.drafts.len() < MAX_DRAFTS)
                        && self.draft(&target).len() + s.len() <= MAX_TEXT_BYTES
                    {
                        self.drafts.entry(target).or_default().push_str(&s);
                    } else {
                        self.status = "Draft capacity reached".into();
                    }
                }
                _ => {}
            }
            return vec![];
        }
        match event {
            Event::Key(k) if k.kind != KeyEventKind::Release => {
                if k.modifiers.contains(KeyModifiers::ALT) {
                    if let KeyCode::Char(c) = k.code {
                        if let Some(i) = c.to_digit(10).and_then(|i| i.checked_sub(1)) {
                            if let Some(a) = ActionId::PRIMARY.get(i as usize) {
                                return self.activate(*a);
                            }
                        }
                    }
                }
                match k.code {
                    KeyCode::F(10) => {
                        self.menu = true;
                        self.menu_index = 0;
                    }
                    KeyCode::Char('s') if layout::chrome(self.last_area).sidebar.is_none() => {
                        self.sidebar_overlay = !self.sidebar_overlay;
                        self.hits.clear();
                    }
                    KeyCode::Esc if self.sidebar_overlay => {
                        self.sidebar_overlay = false;
                        self.hits.clear();
                    }
                    KeyCode::Up | KeyCode::Char('k') => self.nav = self.nav.saturating_sub(1),
                    KeyCode::Down | KeyCode::Char('j') => {
                        self.nav = (self.nav + 1).min(self.rows.len().saturating_sub(1))
                    }
                    KeyCode::Enter => {
                        if let Some(target) =
                            self.rows.get(self.nav).and_then(|row| row.target.clone())
                        {
                            return self.select(target);
                        }
                        self.status = "Agent pane unavailable or ambiguous".into();
                    }
                    KeyCode::Char('[') => return self.change_tab(-1),
                    KeyCode::Char(']') => return self.change_tab(1),
                    KeyCode::Char('p') => return self.change_pane(-1),
                    KeyCode::Char('n') => return self.change_pane(1),
                    KeyCode::Char('r') => {
                        if self.active.is_some() {
                            self.focus = Focus::Reply;
                        }
                    }
                    KeyCode::Char('i') => {
                        if self.selected_lease().is_some() {
                            self.focus = Focus::Terminal;
                            self.status = "Terminal focus — Ctrl+] returns".into();
                        } else {
                            self.status = "No verified live lease".into();
                        }
                    }
                    KeyCode::PageUp => {
                        if let Some(scroll) = self.active_scroll() {
                            scroll.up(10);
                        }
                    }
                    KeyCode::PageDown => {
                        if let Some(scroll) = self.active_scroll() {
                            scroll.down(10);
                        }
                    }
                    KeyCode::End => {
                        if let Some(scroll) = self.active_scroll() {
                            scroll.follow();
                        }
                    }
                    KeyCode::Char('q') => return self.activate(ActionId::Quit),
                    _ => {}
                }
            }
            Event::Mouse(m) => match m.kind {
                MouseEventKind::Down(MouseButton::Left) => {
                    let hit = self
                        .hits
                        .iter()
                        .rev()
                        .find(|h| {
                            m.column >= h.area.x
                                && m.column < h.area.right()
                                && m.row >= h.area.y
                                && m.row < h.area.bottom()
                        })
                        .map(|h| h.hit.clone());
                    match hit {
                        Some(Hit::Select(t) | Hit::Tab(t) | Hit::Pane(t)) => return self.select(t),
                        Some(Hit::Action(a)) => return self.activate(a),
                        Some(Hit::More) => {
                            self.menu = true;
                            self.menu_index = 0;
                        }
                        None => {}
                    }
                }
                MouseEventKind::ScrollUp => {
                    if let Some(scroll) = self.active_scroll() {
                        scroll.up(3);
                    }
                }
                MouseEventKind::ScrollDown => {
                    if let Some(scroll) = self.active_scroll() {
                        scroll.down(3);
                    }
                }
                _ => {}
            },
            _ => {}
        }
        vec![]
    }
    fn put(frame: &mut Frame<'_>, area: Rect, text: impl Into<Line<'static>>) {
        if !area.is_empty() {
            frame.render_widget(Paragraph::new(text.into()), area);
        }
    }
    fn state_style(state: &str) -> Style {
        match state.to_ascii_lowercase().as_str() {
            "working" | "connected" | "ready" => Style::default().fg(Color::Green),
            "blocked" | "error" => Style::default().fg(Color::Yellow),
            _ => Style::default().fg(Color::DarkGray),
        }
    }
    fn row_style(&self, i: usize) -> Style {
        let active = self.active.as_ref().is_some_and(|a| {
            self.rows.get(i).is_some_and(|r| {
                r.target.as_ref().is_some_and(|target| {
                    if r.agent {
                        target == a
                    } else {
                        target.socket_path == a.socket_path && target.workspace_id == a.workspace_id
                    }
                })
            })
        });
        let s = if active {
            Style::default()
                .bg(Color::DarkGray)
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default()
        };
        if self.nav == i {
            s.add_modifier(Modifier::UNDERLINED)
        } else {
            s
        }
    }
    fn render_sidebar(&mut self, f: &mut Frame<'_>, area: Rect) {
        Self::put(f, Rect::new(area.x, area.y, area.width, 1), "spaces");
        let divider = area.height / 2 + 1;
        let agents_y = divider.saturating_add(1);
        let mut wy = 1u16;
        let mut ay = agents_y.saturating_add(1);
        let workspace_capacity = (divider.saturating_sub(1) / 2) as usize;
        let agent_capacity = (area
            .height
            .saturating_sub(1)
            .saturating_sub(ay)
            .saturating_add(1)
            / 2) as usize;
        let workspace_pos = self.rows.iter().take(self.nav).filter(|r| !r.agent).count();
        let agent_pos = self.rows.iter().take(self.nav).filter(|r| r.agent).count();
        let workspace_start = if self.rows.get(self.nav).is_some_and(|r| !r.agent) {
            workspace_pos
                .saturating_add(1)
                .saturating_sub(workspace_capacity)
        } else {
            0
        };
        let agent_start = if self.rows.get(self.nav).is_some_and(|r| r.agent) {
            agent_pos.saturating_add(1).saturating_sub(agent_capacity)
        } else {
            0
        };
        let mut workspace_index = 0usize;
        let mut agent_index = 0usize;
        for (i, row) in self.rows.iter().enumerate() {
            let index = if row.agent {
                let index = agent_index;
                agent_index += 1;
                index
            } else {
                let index = workspace_index;
                workspace_index += 1;
                index
            };
            if index
                < if row.agent {
                    agent_start
                } else {
                    workspace_start
                }
            {
                continue;
            }
            let y = if row.agent {
                let y = ay;
                ay = ay.saturating_add(2);
                y
            } else {
                let y = wy;
                wy = wy.saturating_add(2);
                y
            };
            let limit = if row.agent {
                area.height.saturating_sub(1)
            } else {
                divider.saturating_sub(1)
            };
            if y >= limit {
                continue;
            }
            let style = self.row_style(i);
            let line = Line::from(vec![
                Span::styled("● ", Self::state_style(&row.state).patch(style)),
                Span::styled(output::plain(&row.title), style),
            ]);
            let r = Rect::new(area.x, area.y + y, area.width, 1);
            f.render_widget(Paragraph::new(line).style(style), r);
            if let Some(target) = &row.target {
                self.hits.push(HitRegion {
                    area: r,
                    hit: Hit::Select(target.clone()),
                });
            }
            if y + 1 < limit {
                let r2 = Rect::new(area.x, area.y + y + 1, area.width, 1);
                Self::put(
                    f,
                    r2,
                    Line::styled(format!("  {}", output::plain(&row.detail)), style),
                );
                if let Some(target) = &row.target {
                    self.hits.push(HitRegion {
                        area: r2,
                        hit: Hit::Select(target.clone()),
                    });
                }
            }
        }
        if divider < area.height {
            Self::put(
                f,
                Rect::new(area.x, area.y + divider.saturating_sub(1), area.width, 1),
                "new                 menu",
            );
            Self::put(
                f,
                Rect::new(area.x, area.y + divider, area.width, 1),
                Line::styled(
                    "─".repeat(area.width as usize),
                    Style::default().fg(Color::DarkGray),
                ),
            );
        }
        if agents_y < area.height {
            Self::put(
                f,
                Rect::new(area.x, area.y + agents_y, area.width, 1),
                "agents           grouped",
            );
        }
        if area.height > 0 {
            Self::put(f, Rect::new(area.x, area.bottom() - 1, area.width, 1), "«");
        }
    }
    fn render_tabs(&mut self, f: &mut Frame<'_>, area: Rect) {
        let Some(a) = self.active.clone() else {
            Self::put(f, area, "Select a space");
            return;
        };
        let Some(w) = self
            .inventory
            .sessions
            .iter()
            .find(|s| s.socket_path == a.socket_path)
            .and_then(|s| {
                s.workspaces
                    .iter()
                    .find(|w| w.workspace_id == a.workspace_id)
            })
        else {
            Self::put(f, area, "Offline / selection unavailable");
            return;
        };
        let tabs = w.tabs.clone();
        let labels: Vec<_> = tabs
            .iter()
            .map(|t| format!(" {} ", output::plain(&t.label)))
            .collect();
        let widths: Vec<_> = labels
            .iter()
            .map(|label| UnicodeWidthStr::width(label.as_str()).min(u16::MAX as usize) as u16)
            .collect();
        let selected_index = tabs
            .iter()
            .position(|t| a.tab_id.as_ref() == Some(&t.tab_id))
            .unwrap_or(0);
        let mut start = 0usize;
        if widths
            .iter()
            .take(selected_index + 1)
            .map(|w| usize::from(*w))
            .sum::<usize>()
            > usize::from(area.width)
        {
            start = selected_index;
            let mut used = widths.get(start).copied().unwrap_or(0);
            let available = area.width.saturating_sub(1); // left overflow marker
            while start > 0 && used.saturating_add(widths[start - 1]) <= available {
                start -= 1;
                used += widths[start];
            }
        }
        let mut x = area.x;
        if start > 0 && x < area.right() {
            Self::put(f, Rect::new(x, area.y, 1, 1), "‹");
            x += 1;
        }
        for (index, t) in tabs.into_iter().enumerate().skip(start) {
            let remaining = area.right().saturating_sub(x);
            if remaining == 0 {
                break;
            }
            let selected = a.tab_id.as_ref() == Some(&t.tab_id);
            let width = widths[index];
            if width > remaining && !selected {
                Self::put(f, Rect::new(x, area.y, 1, 1), "…");
                break;
            }
            let displayed = width.min(remaining);
            let r = Rect::new(x, area.y, displayed, 1);
            let style = if selected {
                Style::default().add_modifier(Modifier::BOLD | Modifier::UNDERLINED)
            } else {
                Style::default()
            };
            Self::put(f, r, Line::styled(labels[index].clone(), style));
            if width > remaining {
                Self::put(
                    f,
                    Rect::new(area.right() - 1, area.y, 1, 1),
                    Line::styled("…", style),
                );
            }
            let p = t.panes.first();
            self.hits.push(HitRegion {
                area: r,
                hit: Hit::Tab(Target {
                    socket_path: a.socket_path.clone(),
                    workspace_id: a.workspace_id.clone(),
                    tab_id: Some(t.tab_id),
                    pane_id: p.map(|p| p.pane_id.clone()),
                    terminal_id: p.and_then(|p| p.terminal_id.clone()),
                }),
            });
            x += displayed;
            if width > remaining {
                break;
            }
        }
    }
    fn render_preview(&self, f: &mut Frame<'_>, area: Rect, target: &Target) {
        if area.is_empty() {
            return;
        }
        if let Some(entry) = self.frames.get(target) {
            let lines = output::sanitize(&entry.frame.text);
            let scroll = self.scroll.get(target).cloned().unwrap_or_default();
            let top = scroll
                .top_line(lines.len(), area.height as usize)
                .min(u16::MAX as usize) as u16;
            f.render_widget(Paragraph::new(lines).scroll((top, 0)), area);
        }
    }
    fn render_content(&mut self, f: &mut Frame<'_>, area: Rect) {
        let Some(a) = self.active.clone() else {
            return;
        };
        let Some(tab) = self
            .inventory
            .sessions
            .iter()
            .find(|s| s.socket_path == a.socket_path)
            .and_then(|s| {
                s.workspaces
                    .iter()
                    .find(|w| w.workspace_id == a.workspace_id)
            })
            .and_then(|w| w.tabs.iter().find(|t| Some(&t.tab_id) == a.tab_id.as_ref()))
            .cloned()
        else {
            return;
        };
        let current_layout = self
            .frames
            .get(&a)
            .map_or(&tab.layout, |entry| &entry.frame.layout);
        match layout::map_panes(current_layout, area) {
            Ok(MappedLayout::Unavailable) | Err(_) => {
                let header = Rect::new(area.x, area.y, area.width, area.height.min(1));
                let mut x = header.x;
                let hint = "Layout unavailable (n/p): ";
                let used = (UnicodeWidthStr::width(hint) as u16).min(header.width);
                Self::put(f, Rect::new(x, header.y, used, header.height), hint);
                x += used;
                for pane in &tab.panes {
                    let label = format!("[{}] ", output::plain(&pane.label));
                    let w = (UnicodeWidthStr::width(label.as_str()) as u16)
                        .min(header.right().saturating_sub(x));
                    if w == 0 {
                        break;
                    }
                    let r = Rect::new(x, header.y, w, header.height);
                    Self::put(f, r, label);
                    self.hits.push(HitRegion {
                        area: r,
                        hit: Hit::Pane(Target {
                            socket_path: a.socket_path.clone(),
                            workspace_id: a.workspace_id.clone(),
                            tab_id: a.tab_id.clone(),
                            pane_id: Some(pane.pane_id.clone()),
                            terminal_id: pane.terminal_id.clone(),
                        }),
                    });
                    x += w;
                }
                if area.height > 1 {
                    self.render_preview(
                        f,
                        Rect::new(area.x, area.y + 1, area.width, area.height - 1),
                        &a,
                    );
                }
            }
            Ok(MappedLayout::Available(mapped)) => {
                let split = mapped.len() > 1;
                for p in &mapped {
                    let Some(pane) = tab.panes.iter().find(|x| x.pane_id == p.pane_id) else {
                        continue;
                    };
                    let target = if a.pane_id.as_ref() == Some(&pane.pane_id) {
                        a.clone()
                    } else {
                        self.frames
                            .keys()
                            .find(|t| {
                                Self::same_scope(t, &a) && t.pane_id.as_ref() == Some(&pane.pane_id)
                            })
                            .cloned()
                            .unwrap_or_else(|| Target {
                                socket_path: a.socket_path.clone(),
                                workspace_id: a.workspace_id.clone(),
                                tab_id: a.tab_id.clone(),
                                pane_id: Some(pane.pane_id.clone()),
                                terminal_id: pane.terminal_id.clone(),
                            })
                    };
                    let mut inner = p.area;
                    if split && p.area.x > area.x {
                        let line = Rect::new(p.area.x, p.area.y, 1, p.area.height);
                        for y in line.y..line.bottom() {
                            Self::put(
                                f,
                                Rect::new(line.x, y, 1, 1),
                                Line::styled("│", Style::default().fg(Color::DarkGray)),
                            );
                        }
                        inner.x += 1;
                        inner.width = inner.width.saturating_sub(1);
                    }
                    if split && p.area.y > area.y {
                        Self::put(
                            f,
                            Rect::new(inner.x, inner.y, inner.width, 1),
                            Line::styled(
                                "─".repeat(inner.width as usize),
                                Style::default().fg(Color::DarkGray),
                            ),
                        );
                        inner.y += 1;
                        inner.height = inner.height.saturating_sub(1);
                    }
                    self.hits.push(HitRegion {
                        area: p.area,
                        hit: Hit::Pane(target.clone()),
                    });
                    self.render_preview(f, inner, &target);
                }
            }
        }
    }
    fn render_actions(&mut self, f: &mut Frame<'_>, area: Rect) {
        let mut x = area.x;
        let more_width = 14u16;
        for action in ActionId::PRIMARY {
            let label = format!("[{}] ", action.label());
            let width = UnicodeWidthStr::width(label.as_str()) as u16;
            if width.saturating_add(more_width) > area.right().saturating_sub(x) {
                break;
            }
            let r = Rect::new(x, area.y, width, area.height);
            Self::put(f, r, label);
            self.hits.push(HitRegion {
                area: r,
                hit: Hit::Action(action),
            });
            x += width;
        }
        if x < area.right() {
            let r = Rect::new(x, area.y, area.right() - x, area.height);
            Self::put(f, r, "[F10 Actions]");
            self.hits.push(HitRegion {
                area: r,
                hit: Hit::More,
            });
        }
    }
    pub fn render(&mut self, f: &mut Frame<'_>) {
        let area = f.area();
        self.last_area = area;
        self.hits.clear();
        let c = layout::chrome(area);
        if let Some(sidebar) = c.sidebar {
            self.render_sidebar(f, sidebar);
        }
        if let Some(sep) = c.separator {
            for y in sep.y..sep.bottom() {
                Self::put(
                    f,
                    Rect::new(sep.x, y, 1, 1),
                    Line::styled("│", Style::default().fg(Color::DarkGray)),
                );
            }
        }
        self.render_tabs(f, c.tabs);
        self.render_content(f, c.content);
        if c.sidebar.is_none() && self.sidebar_overlay {
            let overlay = Rect::new(
                area.x,
                area.y,
                area.width.min(25),
                c.status.y.saturating_sub(area.y),
            );
            self.hits
                .retain(|hit| hit.area.intersection(overlay).is_empty());
            f.render_widget(Clear, overlay);
            self.render_sidebar(f, overlay);
        }
        let status = if self.focus == Focus::Reply {
            let draft = self
                .active
                .as_ref()
                .map(|t| output::plain(self.draft(t)))
                .unwrap_or_default();
            if matches!(self.status.as_str(), "Loading selection" | "Chrome focus") {
                format!("Reply (Enter sends, Esc exits): {draft}")
            } else {
                // Important dispatch errors and uncertain outcomes must remain visible
                // even while the unchanged draft stays open for inspection.
                format!("{} | Reply: {draft}", output::plain(&self.status))
            }
        } else {
            let zoom = self
                .active
                .as_ref()
                .and_then(|a| {
                    self.inventory
                        .sessions
                        .iter()
                        .find(|s| s.socket_path == a.socket_path)
                        .and_then(|s| {
                            s.workspaces
                                .iter()
                                .find(|w| w.workspace_id == a.workspace_id)
                        })
                        .and_then(|w| w.tabs.iter().find(|t| Some(&t.tab_id) == a.tab_id.as_ref()))
                })
                .is_some_and(|t| {
                    let current_layout = self
                        .active
                        .as_ref()
                        .and_then(|a| self.frames.get(a))
                        .map_or(&t.layout, |entry| &entry.frame.layout);
                    current_layout.state == LayoutState::Available
                        && t.panes.len() > 1
                        && current_layout.rects.len() == 1
                });
            format!(
                "{}{}",
                if zoom { "[zoom] " } else { "" },
                output::plain(&self.status)
            )
        };
        let status = if c.sidebar.is_none() && self.focus == Focus::Chrome {
            format!("[s spaces] {status}")
        } else {
            status
        };
        Self::put(f, c.status, status);
        self.render_actions(f, c.actions);
        if self.menu {
            let h = (ActionId::ALL.len() as u16).min(area.height.saturating_sub(1));
            let w = 20.min(area.width);
            let r = Rect::new(
                area.right().saturating_sub(w),
                area.bottom().saturating_sub(h + 1),
                w,
                h,
            );
            f.render_widget(Clear, r);
            let start = self.menu_index.saturating_add(1).saturating_sub(h as usize);
            for (i, id) in ActionId::ALL
                .iter()
                .enumerate()
                .skip(start)
                .take(h as usize)
            {
                let style = if i == self.menu_index {
                    Style::default()
                        .bg(Color::DarkGray)
                        .add_modifier(Modifier::BOLD)
                } else {
                    Style::default()
                };
                Self::put(
                    f,
                    Rect::new(r.x, r.y + (i - start) as u16, r.width, 1),
                    Line::styled(id.label(), style),
                );
            }
        }
        let ctx = self.context();
        if let Some(panel) = self.panel.as_mut() {
            panel.render(f, area, &ctx);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::panel::{PanelContext, PanelResponse};
    use crossterm::event::{KeyEvent, MouseEvent};
    use ratatui::{backend::TestBackend, buffer::Buffer, Terminal};
    use std::sync::{Arc, Mutex};

    const FIXTURE: &str = include_str!("../../../tests/fixtures/rust_bridge_v1.json");
    fn fixture() -> Value {
        serde_json::from_str(FIXTURE).unwrap()
    }
    fn incoming(v: &Value) -> Incoming {
        let mut line = serde_json::to_vec(v).unwrap();
        line.push(b'\n');
        crate::protocol::decode_incoming(&line).unwrap()
    }
    fn key(code: KeyCode) -> Event {
        Event::Key(KeyEvent::new(code, KeyModifiers::NONE))
    }
    fn ctrl(code: KeyCode) -> Event {
        Event::Key(KeyEvent::new(code, KeyModifiers::CONTROL))
    }
    fn draw(shell: &mut Shell, width: u16, height: u16) -> Buffer {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal.draw(|frame| shell.render(frame)).unwrap();
        terminal.backend().buffer().clone()
    }
    fn row(buffer: &Buffer, y: u16) -> String {
        (0..buffer.area.width)
            .map(|x| buffer[(x, y)].symbol())
            .collect()
    }
    fn setup() -> (Shell, Target) {
        let data = fixture();
        let mut shell = Shell::new();
        shell.apply_incoming(incoming(&data["events"][0]));
        let target: Target = serde_json::from_value(data["target"].clone()).unwrap();
        assert_eq!(shell.select(target.clone()).len(), 1);
        let mut frame = data["events"][1].clone();
        frame["payload"]["layout"] = data["events"][0]["payload"]["sessions"][0]["workspaces"][0]
            ["tabs"][0]["layout"]
            .clone();
        frame["payload"]["text"] =
            Value::String("\u{1b}[31mRED\u{1b}[0m safe\u{1b}]52;c;evil\u{7}\u{202e}".into());
        shell.apply_incoming(incoming(&frame));
        (shell, target)
    }
    fn sent(actions: Vec<UiAction>) -> Request {
        match actions.as_slice() {
            [UiAction::Dispatch(request)] => request.clone(),
            _ => panic!("expected one request, got {actions:?}"),
        }
    }
    fn click(x: u16, y: u16) -> Event {
        Event::Mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: x,
            row: y,
            modifiers: KeyModifiers::NONE,
        })
    }

    #[test]
    fn observed_geometry_safe_styled_cells_and_resize() {
        let (mut shell, _) = setup();
        for (width, height) in [(140, 42), (120, 40), (80, 24), (40, 24)] {
            let b = draw(&mut shell, width, height);
            if width >= 70 {
                assert!(row(&b, 0).starts_with("spaces"));
                assert_eq!(b[(25, 0)].symbol(), "│");
                assert_eq!(b[(26, 1)].symbol(), "R", "one pane has no enclosing card");
                assert_eq!(b[(26, 1)].fg, Color::Red);
                assert_eq!(b[(2, 1)].bg, Color::DarkGray);
                assert!(b[(2, 1)].modifier.contains(Modifier::BOLD));
            } else {
                assert_eq!(b[(0, 1)].symbol(), "R");
                assert_eq!(b[(0, 1)].fg, Color::Red);
            }
            assert!(row(&b, height - 1).contains("F10"));
            let screen: String = (0..height).map(|y| row(&b, y)).collect();
            assert!(!screen.contains("evil"));
            assert!(!screen.contains('\u{1b}'));
            assert!(!screen.contains('\u{202e}'));
        }
    }

    #[test]
    fn unicode_absent_metadata_active_vs_navigated_and_tab_overflow() {
        let mut data = fixture();
        let workspace = &mut data["events"][0]["payload"]["sessions"][0]["workspaces"][0];
        workspace["label"] = json!("工作区 🦀 e\u{301} extremely long long label");
        workspace["agents"] = json!([{ "pane_id": "p1", "agent": "", "role": null, "state": "working", "metadata": {} }]);
        let mut second = workspace.clone();
        second["workspace_id"] = json!("w2");
        second["label"] = json!("other");
        data["events"][0]["payload"]["sessions"][0]["workspaces"]
            .as_array_mut()
            .unwrap()
            .push(second);
        let mut shell = Shell::new();
        shell.apply_incoming(incoming(&data["events"][0]));
        assert!(!shell.rows[1].title.contains(" ·  · "));
        assert!(shell.rows[1].title.contains("工作区"));
        assert!(shell.rows[0].detail.is_empty());
        let target: Target = serde_json::from_value(data["target"].clone()).unwrap();
        shell.select(target);
        shell.handle_event(key(KeyCode::Down));
        shell.handle_event(key(KeyCode::Down));
        assert_eq!(shell.navigated_row(), 2);
        assert_eq!(shell.row_style(0).bg, Some(Color::DarkGray));
        assert!(!shell
            .row_style(0)
            .add_modifier
            .contains(Modifier::UNDERLINED));
        assert!(shell
            .row_style(2)
            .add_modifier
            .contains(Modifier::UNDERLINED));
        assert_ne!(shell.row_style(2).bg, Some(Color::DarkGray));
        let b = draw(&mut shell, 80, 24);
        assert_eq!(b[(2, 1)].symbol(), "工");
        assert_eq!(b[(4, 1)].symbol(), "作");
        assert_eq!(b[(6, 1)].symbol(), "区");
        assert!(!row(&b, 2).contains('·'));
        shell.inventory.sessions[0].workspaces[0].tabs[0].label = "超長".repeat(30);
        shell.status = "important error".into();
        let b = draw(&mut shell, 40, 24);
        assert!(row(&b, 0).contains('…'));
        assert!(row(&b, 22).contains("important error"));
        let workspace = &mut shell.inventory.sessions[0].workspaces[0];
        let template = workspace.tabs[0].clone();
        workspace.tabs = (0..8)
            .map(|i| {
                let mut tab = template.clone();
                tab.tab_id = format!("t{i}");
                tab.label = format!("tab{i}");
                tab
            })
            .collect();
        shell.active.as_mut().unwrap().tab_id = Some("t7".into());
        let b = draw(&mut shell, 40, 24);
        assert!(row(&b, 0).contains('‹'));
        assert!(row(&b, 0).contains("tab7"));
    }

    #[test]
    fn narrow_sidebar_can_be_opened_and_scrolls_to_navigated_workspace() {
        let mut data = fixture();
        let session = &mut data["events"][0]["payload"]["sessions"][0];
        let mut base = session["workspaces"][0].clone();
        base["agents"] = json!([]);
        session["workspaces"] = Value::Array(
            (0..20)
                .map(|i| {
                    let mut workspace = base.clone();
                    workspace["workspace_id"] = json!(format!("w{i}"));
                    workspace["label"] = json!(format!("workspace-{i:02}"));
                    workspace
                })
                .collect(),
        );
        let mut shell = Shell::new();
        shell.apply_incoming(incoming(&data["events"][0]));
        assert!(row(&draw(&mut shell, 40, 24), 22).contains("s spaces"));
        shell.handle_event(key(KeyCode::Char('s')));
        for _ in 0..19 {
            shell.handle_event(key(KeyCode::Down));
        }
        let buffer = draw(&mut shell, 40, 24);
        let screen: String = (0..22).map(|y| row(&buffer, y)).collect();
        assert!(screen.contains("workspace-19"));
        let request = sent(shell.handle_event(key(KeyCode::Enter)));
        assert_eq!(request.params["target"]["workspace_id"], "w19");
        assert!(!shell.sidebar_overlay);
    }
    #[test]
    fn six_actions_are_accessible_with_overflow_and_hits_rebuilt_after_resize() {
        let (mut shell, target) = setup();
        for (i, action) in ActionId::PRIMARY.iter().enumerate() {
            let event = Event::Key(KeyEvent::new(
                KeyCode::Char(char::from(b'1' + i as u8)),
                KeyModifiers::ALT,
            ));
            assert_eq!(shell.handle_event(event), vec![UiAction::Activate(*action)]);
        }
        draw(&mut shell, 40, 24);
        shell.handle_event(key(KeyCode::F(10)));
        for action in ActionId::ALL {
            assert_eq!(
                shell.handle_event(key(KeyCode::Enter)),
                vec![UiAction::Activate(action)]
            );
            shell.handle_event(key(KeyCode::F(10)));
            for _ in 0..ActionId::ALL.iter().position(|v| *v == action).unwrap() + 1 {
                shell.handle_event(key(KeyCode::Down));
            }
        }
        shell.handle_event(key(KeyCode::Esc));
        draw(&mut shell, 140, 42);
        let old_row = shell
            .hits
            .iter()
            .find(|h| matches!(h.hit, Hit::Select(_)))
            .unwrap()
            .area;
        shell.handle_event(Event::Resize(40, 24));
        assert!(shell.handle_event(click(old_row.x, old_row.y)).is_empty());
        assert_eq!(shell.active_target(), Some(&target));
        draw(&mut shell, 40, 24);
        let more = shell
            .hits
            .iter()
            .find(|h| matches!(h.hit, Hit::More))
            .unwrap()
            .area;
        shell.handle_event(click(more.x, more.y));
        assert!(shell.menu);
    }

    struct Modal(Arc<Mutex<usize>>);
    impl Panel for Modal {
        fn render(&mut self, _: &mut Frame<'_>, _: Rect, _: &PanelContext) {}
        fn handle(&mut self, _: &Event, _: &PanelContext) -> PanelResponse {
            *self.0.lock().unwrap() += 1;
            PanelResponse::close()
        }
    }
    struct RequestingPanel;
    impl Panel for RequestingPanel {
        fn render(&mut self, _: &mut Frame<'_>, _: Rect, _: &PanelContext) {}
        fn handle(&mut self, _: &Event, context: &PanelContext) -> PanelResponse {
            let request = Request::new(
                context.request_ids.reserve(),
                Method::CatalogGet,
                Map::new(),
            )
            .unwrap();
            PanelResponse {
                consumed: true,
                close: false,
                actions: vec![UiAction::Dispatch(request)],
            }
        }
    }
    #[test]
    fn panel_and_shell_share_request_id_namespace() {
        let (mut shell, _) = setup();
        let before = shell.reserve_request_id();
        shell.set_panel(Box::new(RequestingPanel));
        let request = sent(shell.handle_event(key(KeyCode::Enter)));
        let after = shell.reserve_request_id();
        assert_ne!(request.id, before);
        assert_ne!(request.id, after);
        assert_eq!(request.method, Method::CatalogGet);
    }
    #[test]
    fn ctrl_c_quits_before_all_focus_and_overlay_routing() {
        let quit_keys = [
            KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL),
            KeyEvent::new(
                KeyCode::Char('C'),
                KeyModifiers::CONTROL | KeyModifiers::SHIFT,
            ),
            KeyEvent::new(KeyCode::Char('\u{3}'), KeyModifiers::NONE),
            KeyEvent::new_with_kind(
                KeyCode::Char('c'),
                KeyModifiers::CONTROL,
                KeyEventKind::Repeat,
            ),
        ];
        for focus in [Focus::Chrome, Focus::Reply, Focus::Terminal] {
            for overlay in 0..3 {
                for quit_key in quit_keys {
                    let (mut shell, target) = setup();
                    shell.focus = focus;
                    if focus == Focus::Terminal {
                        let _ = sent(shell.handle_event(key(KeyCode::Char('x'))));
                        assert!(shell.handle_event(key(KeyCode::Char('y'))).is_empty());
                        assert_eq!(shell.terminal_queue.len(), 1);
                    }
                    let hits = Arc::new(Mutex::new(0));
                    if overlay == 1 {
                        shell.menu = true;
                    } else if overlay == 2 {
                        shell.set_panel(Box::new(Modal(hits.clone())));
                    }
                    let pending = shell.pending_count();
                    let queued = shell.terminal_queue.len();
                    assert_eq!(
                        shell.handle_event(Event::Key(quit_key)),
                        vec![UiAction::Activate(ActionId::Quit)],
                        "focus={focus:?}, overlay={overlay}, key={quit_key:?}"
                    );
                    assert_eq!(shell.pending_count(), pending);
                    assert_eq!(shell.terminal_queue.len(), queued);
                    assert!(shell.draft(&target).is_empty());
                    assert_eq!(*hits.lock().unwrap(), 0, "panel received quit key");
                }
            }
        }
    }

    #[test]
    fn ctrl_c_release_is_consumed_without_quitting_or_reaching_panel() {
        let (mut shell, _) = setup();
        let hits = Arc::new(Mutex::new(0));
        shell.set_panel(Box::new(Modal(hits.clone())));
        for (code, modifiers) in [
            (KeyCode::Char('c'), KeyModifiers::CONTROL),
            (KeyCode::Char('\u{3}'), KeyModifiers::NONE),
        ] {
            let release = KeyEvent::new_with_kind(code, modifiers, KeyEventKind::Release);
            assert!(shell.handle_event(Event::Key(release)).is_empty());
        }
        assert_eq!(*hits.lock().unwrap(), 0);
        assert!(shell.has_panel());
    }

    #[test]
    fn modal_terminal_focus_precedence_paste_keys_and_ctrl_bracket() {
        let (mut shell, _) = setup();
        shell.handle_event(key(KeyCode::Char('i')));
        assert_eq!(shell.focus, Focus::Terminal);
        let hits = Arc::new(Mutex::new(0));
        shell.set_panel(Box::new(Modal(hits.clone())));
        assert!(shell.handle_event(ctrl(KeyCode::Char(']'))).is_empty());
        assert_eq!(*hits.lock().unwrap(), 1);
        assert_eq!(shell.focus, Focus::Terminal);
        let req = sent(shell.handle_event(Event::Paste("héllo\n世界".into())));
        assert_eq!(req.method, Method::TerminalInput);
        assert_eq!(req.params["text"], "héllo\n世界");
        shell.dispatch_failed(&req.id);
        let req = sent(shell.handle_event(key(KeyCode::Left)));
        assert_eq!(req.params["keys"], json!(["left"]));
        shell.dispatch_failed(&req.id);
        assert!(shell.handle_event(ctrl(KeyCode::Char(']'))).is_empty());
        assert_eq!(shell.focus, Focus::Chrome);
        assert_eq!(shell.pending_count(), 0);
    }

    #[test]
    fn rapid_terminal_input_is_serialized_bounded_and_never_replayed() {
        let (mut shell, _) = setup();
        shell.handle_event(key(KeyCode::Char('i')));
        let a = sent(shell.handle_event(key(KeyCode::Char('a'))));
        assert!(shell.handle_event(key(KeyCode::Char('b'))).is_empty());
        assert!(shell.handle_event(key(KeyCode::Left)).is_empty());
        assert_eq!(a.params["text"], "a");
        assert_eq!(shell.pending_count(), 1);
        assert_eq!(shell.terminal_queue.len(), 2);
        let result = |id: &RequestId, outcome: &str| {
            incoming(
                &json!({"event":"reply_result","revision":4,"payload":{"request_id":id,"outcome":outcome,"message":"done","lease":fixture()["lease"]}}),
            )
        };
        let b = sent(shell.apply_incoming(result(&a.id, "accepted")));
        assert_eq!(b.params["text"], "b");
        let c = sent(shell.apply_incoming(result(&b.id, "accepted")));
        assert_eq!(c.params["keys"], json!(["left"]));
        assert_ne!(a.id, b.id);
        assert_ne!(b.id, c.id);
        for _ in 0..MAX_PENDING {
            assert!(shell.handle_event(key(KeyCode::Char('x'))).is_empty());
        }
        assert_eq!(shell.terminal_queue.len(), MAX_PENDING);
        assert!(shell.handle_event(key(KeyCode::Char('z'))).is_empty());
        assert!(shell.status.contains("capacity"));
        assert!(shell.apply_incoming(result(&c.id, "uncertain")).is_empty());
        assert_eq!(shell.terminal_queue.len(), 0);
        assert!(shell.status.contains("queued inputs not sent"));
        assert_eq!(shell.pending_count(), 0);
    }
    #[test]
    fn terminal_queue_cancels_on_selection_change_or_enqueue_failure() {
        let (mut shell, target) = setup();
        shell.handle_event(key(KeyCode::Char('i')));
        let a = sent(shell.handle_event(key(KeyCode::Char('a'))));
        shell.handle_event(key(KeyCode::Char('b')));
        shell.select(target);
        assert!(shell.terminal_queue.is_empty());
        assert!(shell.apply_incoming(incoming(&json!({"event":"reply_result","revision":4,"payload":{"request_id":a.id,"outcome":"accepted","message":"done","lease":fixture()["lease"]}}))).is_empty());
        let (mut shell, _) = setup();
        shell.handle_event(key(KeyCode::Char('i')));
        let a = sent(shell.handle_event(key(KeyCode::Char('a'))));
        shell.handle_event(Event::Paste("x".repeat(MAX_TEXT_BYTES)));
        shell.handle_event(key(KeyCode::Char('z')));
        assert!(shell.status.contains("capacity"));
        shell.dispatch_failed(&a.id);
        assert!(shell.terminal_queue.is_empty());
        assert!(shell.status.contains("not sent"));
        let (mut shell, _) = setup();
        shell.handle_event(key(KeyCode::Char('i')));
        let a = sent(shell.handle_event(key(KeyCode::Char('a'))));
        shell.handle_event(key(KeyCode::Char('b')));
        assert!(shell.apply_incoming(incoming(&json!({"event":"reply_result","revision":4,"payload":{"request_id":a.id,"outcome":"rejected","message":"busy","lease":fixture()["lease"]}}))).is_empty());
        assert!(shell.terminal_queue.is_empty());
        assert!(shell.status.contains("queued inputs not sent"));
        let (mut shell, _) = setup();
        shell.handle_event(key(KeyCode::Char('i')));
        let _ = sent(shell.handle_event(key(KeyCode::Char('a'))));
        shell.handle_event(key(KeyCode::Char('b')));
        shell.bridge_lost("closed");
        assert!(shell.terminal_queue.is_empty());
        assert!(shell.status.contains("Uncertain"));
        assert!(shell.status.contains("queued inputs not sent"));
    }

    #[test]
    fn restart_resets_revision_domain_but_requires_reselection() {
        let (mut shell, target) = setup();
        shell.handle_event(key(KeyCode::Char('r')));
        shell.handle_event(Event::Paste("preserve".into()));
        let _ = sent(shell.handle_event(key(KeyCode::Enter)));
        shell.reset_bridge();
        assert_eq!(shell.pending_count(), 0);
        assert_eq!(shell.draft(&target), "preserve");
        assert!(shell.feedback[&target].contains("Uncertain"));
        let data = fixture();
        shell.apply_incoming(incoming(&data["events"][0]));
        shell.apply_incoming(incoming(&data["events"][1]));
        assert!(shell.selected_lease().is_none());
        shell.select(target.clone());
        let mut fresh = data["events"][1].clone();
        fresh["revision"] = json!(4);
        shell.apply_incoming(incoming(&fresh));
        assert!(shell.selected_lease().is_some());
        assert_eq!(shell.draft(&target), "preserve");
    }

    #[test]
    fn draft_follows_first_verified_identity_of_the_same_pane() {
        let mut data = fixture();
        data["events"][0]["payload"]["sessions"][0]["workspaces"][0]["tabs"][0]["panes"][0]
            ["terminal_id"] = Value::Null;
        let mut shell = Shell::new();
        shell.apply_incoming(incoming(&data["events"][0]));
        let verified: Target = serde_json::from_value(data["target"].clone()).unwrap();
        let mut unverified = verified.clone();
        unverified.terminal_id = None;
        shell.select(unverified.clone());
        shell.handle_event(key(KeyCode::Char('r')));
        shell.handle_event(Event::Paste("draft before lease".into()));
        shell.apply_incoming(incoming(&data["events"][1]));
        assert_eq!(shell.active_target(), Some(&verified));
        assert_eq!(shell.draft(&verified), "draft before lease");
        assert_eq!(shell.draft(&unverified), "");
    }
    #[test]
    fn reply_readiness_is_explained_and_does_not_infer_permission_from_state() {
        let (mut shell, target) = setup();
        let mut frame = fixture()["events"][1].clone();
        frame["revision"] = json!(4);
        frame["payload"]["readiness"] = json!({"can_reply":false,"explanation":"Open in Herdr for permissions","state":"blocked"});
        shell.apply_incoming(incoming(&frame));
        assert!(row(&draw(&mut shell, 80, 24), 22).contains("Open in Herdr for permissions"));
        shell.handle_event(key(KeyCode::Char('r')));
        shell.handle_event(Event::Paste("preserved draft".into()));
        assert!(shell.handle_event(key(KeyCode::Enter)).is_empty());
        assert!(row(&draw(&mut shell, 80, 24), 22).contains("Open in Herdr for permissions"));
        assert_eq!(shell.draft(&target), "preserved draft");
        assert!(shell.pending.is_empty());
    }
    #[test]
    fn uncertain_send_is_visible_without_closing_reply_draft() {
        let (mut shell, target) = setup();
        shell.handle_event(key(KeyCode::Char('r')));
        shell.handle_event(Event::Paste("keep".into()));
        let req = sent(shell.handle_event(key(KeyCode::Enter)));
        shell.apply_incoming(incoming(&json!({"event":"reply_result","revision":4,"payload":{"request_id":req.id,"outcome":"uncertain","message":"lost","lease":fixture()["lease"]}})));
        assert_eq!(shell.focus, Focus::Reply);
        assert_eq!(shell.draft(&target), "keep");
        assert!(row(&draw(&mut shell, 80, 24), 22).contains("Uncertain"));
    }

    #[test]
    fn ambiguous_agent_and_foreign_layout_never_retarget() {
        let mut data = fixture();
        let workspace = &mut data["events"][0]["payload"]["sessions"][0]["workspaces"][0];
        let mut duplicate = workspace["tabs"][0].clone();
        duplicate["tab_id"] = json!("other-tab");
        workspace["tabs"].as_array_mut().unwrap().push(duplicate);
        workspace["agents"] =
            json!([{"pane_id":"p1","agent":"ambiguous","state":"working","metadata":{}}]);
        let mut shell = Shell::new();
        shell.apply_incoming(incoming(&data["events"][0]));
        assert!(shell.rows[1].target.is_none());
        shell.nav = 1;
        assert!(shell.handle_event(key(KeyCode::Enter)).is_empty());
        assert!(shell.status.contains("ambiguous"));
        let target: Target = serde_json::from_value(data["target"].clone()).unwrap();
        shell.select(target.clone());
        let mut frame = data["events"][1].clone();
        frame["payload"]["layout"] = json!({"state":"available","rects":[{"pane_id":"foreign","x":0,"y":0,"width":1,"height":1}]});
        shell.apply_incoming(incoming(&frame));
        assert!(shell.frame(&target).is_none());
    }

    #[test]
    fn target_bound_drafts_stale_and_uncertain_results() {
        let (mut shell, first) = setup();
        shell.handle_event(key(KeyCode::Char('r')));
        shell.handle_event(Event::Paste("first draft 🦀".into()));
        let req = sent(shell.handle_event(key(KeyCode::Enter)));
        let mut wrong_lease = fixture()["lease"].clone();
        wrong_lease["generation"] = json!(999);
        let result = |lease: Value| {
            incoming(
                &json!({"event":"reply_result","revision":4,"payload":{"request_id":req.id,"outcome":"accepted","message":"done","lease":lease}}),
            )
        };
        shell.apply_incoming(result(wrong_lease));
        assert_eq!(shell.pending_count(), 1);
        assert_eq!(shell.draft(&first), "first draft 🦀");
        let mut second = first.clone();
        second.workspace_id = "w2".into();
        second.pane_id = Some("p2".into());
        second.terminal_id = Some("term2".into());
        shell.select(second.clone());
        shell.handle_event(key(KeyCode::Char('r')));
        shell.handle_event(Event::Paste("second".into()));
        shell.apply_incoming(incoming(&json!({"event":"reply_result","revision":5,"payload":{"request_id":req.id,"outcome":"uncertain","message":"disconnected","lease":fixture()["lease"]}})));
        assert_eq!(shell.draft(&first), "first draft 🦀");
        assert_eq!(shell.draft(&second), "second");
        assert_eq!(shell.pending_count(), 0);
        assert!(
            !shell.status.contains("Uncertain"),
            "old target must not overwrite current feedback"
        );
        shell.select(first.clone());
        assert_eq!(
            shell.feedback[&first],
            "Uncertain — do not retry automatically: disconnected"
        );
        assert_eq!(shell.draft(&first), "first draft 🦀");
    }

    #[test]
    fn offline_or_replaced_selection_cannot_revive_from_late_frame() {
        let (mut shell, target) = setup();
        let mut data = fixture();
        data["events"][0]["revision"] = json!(4);
        data["events"][0]["payload"]["sessions"][0]["connection"] = json!("offline");
        shell.apply_incoming(incoming(&data["events"][0]));
        data["events"][1]["revision"] = json!(5);
        shell.apply_incoming(incoming(&data["events"][1]));
        assert!(shell.frame(&target).is_none());
        assert!(shell.selected_lease().is_none());
        assert!(shell.status.contains("offline"));
    }

    #[test]
    fn zoom_and_unavailable_layout_keep_pane_selection() {
        let (mut shell, first) = setup();
        let tab = &mut shell.inventory.sessions[0].workspaces[0].tabs[0];
        let mut pane = tab.panes[0].clone();
        pane.pane_id = "p2".into();
        pane.terminal_id = Some("term2".into());
        pane.label = "second".into();
        tab.panes.push(pane);
        assert!(row(&draw(&mut shell, 80, 24), 22).contains("[zoom]"));
        let mut event = fixture()["events"][1].clone();
        event["revision"] = json!(4);
        shell.apply_incoming(incoming(&event));
        assert!(row(&draw(&mut shell, 80, 24), 1).contains("Layout unavailable"));
        let req = sent(shell.handle_event(key(KeyCode::Char('n'))));
        assert_eq!(req.method, Method::SelectionSet);
        assert_eq!(
            shell.active_target().unwrap().pane_id.as_deref(),
            Some("p2")
        );
        assert_eq!(
            shell.active_target().unwrap().workspace_id,
            first.workspace_id
        );
    }
}
