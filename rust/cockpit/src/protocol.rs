//! Strict protocol-1 DTOs and bounded newline-delimited JSON codec.
use serde::{
    de::{self, DeserializeSeed, MapAccess, SeqAccess, Visitor},
    Deserialize, Deserializer, Serialize,
};
use serde_json::{Map, Number, Value};
use std::{
    collections::{HashMap, HashSet},
    fmt,
    io::Write,
};

pub const PROTOCOL: u64 = 1;
pub const MAX_MESSAGE_BYTES: usize = 2 * 1024 * 1024;
pub const MAX_TEXT_BYTES: usize = 256 * 1024;
pub const MAX_ITEMS: usize = 4096;
pub const MAX_INTEGER: u64 = (1 << 53) - 1;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProtocolError(pub String);
impl fmt::Display for ProtocolError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for ProtocolError {}
fn error(s: &str) -> ProtocolError {
    ProtocolError(s.to_owned())
}
fn identity(s: &str) -> bool {
    !s.trim().is_empty() && s.chars().count() <= 4096 && s.len() <= MAX_TEXT_BYTES
}
fn text(s: &str) -> bool {
    s.len() <= MAX_TEXT_BYTES
}
// Match Python str.splitlines(), including CRLF as one boundary and Unicode separators.
fn frame_line_count(s: &str) -> usize {
    let mut chars = s.chars().peekable();
    let mut count = 0;
    let mut trailing = false;
    while let Some(c) = chars.next() {
        if matches!(
            c,
            '\n' | '\r'
                | '\u{000b}'
                | '\u{000c}'
                | '\u{001c}'
                | '\u{001d}'
                | '\u{001e}'
                | '\u{0085}'
                | '\u{2028}'
                | '\u{2029}'
        ) {
            if c == '\r' && chars.peek() == Some(&'\n') {
                chars.next();
            }
            count += 1;
            trailing = false;
        } else {
            trailing = true;
        }
    }
    count + usize::from(trailing)
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(untagged)]
pub enum RequestId {
    Integer(u64),
    String(String),
}
impl RequestId {
    fn valid(&self) -> bool {
        match self {
            Self::Integer(n) => *n <= MAX_INTEGER,
            Self::String(s) => identity(s),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Method {
    #[serde(rename = "initialize")]
    Initialize,
    #[serde(rename = "catalog.get")]
    CatalogGet,
    #[serde(rename = "project.open")]
    ProjectOpen,
    #[serde(rename = "folders.list")]
    FoldersList,
    #[serde(rename = "folders.test")]
    FoldersTest,
    #[serde(rename = "settings.validate")]
    SettingsValidate,
    #[serde(rename = "settings.save")]
    SettingsSave,
    #[serde(rename = "inventory.refresh")]
    InventoryRefresh,
    #[serde(rename = "selection.set")]
    SelectionSet,
    #[serde(rename = "reply.submit")]
    ReplySubmit,
    #[serde(rename = "terminal.input")]
    TerminalInput,
    #[serde(rename = "launch.master")]
    LaunchMaster,
    #[serde(rename = "launch.ergo")]
    LaunchErgo,
    #[serde(rename = "launch.orchestration")]
    LaunchOrchestration,
    #[serde(rename = "launch.council")]
    LaunchCouncil,
    #[serde(rename = "aihead.open")]
    AiHeadOpen,
    #[serde(rename = "workspace.delete")]
    WorkspaceDelete,
    #[serde(rename = "logs.get")]
    LogsGet,
    #[serde(rename = "shutdown")]
    Shutdown,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub id: RequestId,
    pub method: Method,
    pub params: Map<String, Value>,
}
impl Request {
    pub fn new(
        id: RequestId,
        method: Method,
        params: Map<String, Value>,
    ) -> Result<Self, ProtocolError> {
        let request = Self { id, method, params };
        request.validate()?;
        Ok(request)
    }
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if !self.id.valid() {
            return Err(error("invalid request id"));
        }
        bounded(&Value::Object(self.params.clone()), 0)?;
        use Method::*;
        // Types: i identity, s text, b boolean, n integer, o object, a identity array.
        // '?' marks optional nullable, '*' optional non-null; unmarked fields are required.
        let spec: &[(&str, &str)] = match self.method {
            Initialize => &[
                ("protocol", "n"),
                ("capabilities", "a*"),
                ("session", "i?"),
                ("demo", "b*"),
            ],
            CatalogGet | InventoryRefresh | Shutdown => &[],
            ProjectOpen => &[("path", "i")],
            FoldersList | FoldersTest => &[("path", "i"), ("project", "i?")],
            SettingsValidate => &[
                ("token", "i"),
                ("revision", "n"),
                ("sections", "o*"),
                ("discard", "b*"),
            ],
            SettingsSave => &[("token", "i"), ("revision", "n"), ("sections", "o")],
            SelectionSet => &[("target", "o")],
            ReplySubmit => &[("lease", "o"), ("text", "s"), ("notify", "b")],
            TerminalInput => &[("lease", "o"), ("text", "s*"), ("keys", "a*")],
            LaunchMaster => &[
                ("project_path", "i"),
                ("session", "i?"),
                ("name", "i"),
                ("harness", "i"),
                ("model", "i"),
                ("effort", "i?"),
                ("task", "s?"),
                ("ai_head", "b?"),
            ],
            LaunchErgo => &[
                ("project_path", "i"),
                ("session", "i?"),
                ("intent", "s"),
                ("label", "s"),
                ("squad", "o?"),
                ("customize_first", "b"),
                ("start_pipeline", "b"),
                ("save_default", "b"),
                ("ai_head", "b?"),
            ],
            LaunchOrchestration => &[
                ("project_path", "i"),
                ("session", "i?"),
                ("intent", "s"),
                ("label", "s"),
                ("harness", "i"),
                ("scout_model", "i"),
                ("architect_model", "i"),
                ("reviewer_model", "i"),
                ("worker_models", "a"),
                ("scout_effort", "i?"),
                ("architect_effort", "i?"),
                ("reviewer_effort", "i?"),
                ("dangerously_skip_permissions", "b"),
                ("ai_head", "b?"),
            ],
            LaunchCouncil => &[
                ("project_path", "i"),
                ("session", "i?"),
                ("idea", "s"),
                ("label", "s"),
                ("config", "o"),
                ("customize_roster", "b"),
            ],
            AiHeadOpen => &[("project_path", "i"), ("session", "i?")],
            WorkspaceDelete => &[("target", "o"), ("confirmed", "b")],
            LogsGet => &[("limit", "n*")],
        };
        if self
            .params
            .keys()
            .any(|k| !spec.iter().any(|(name, _)| *name == k))
        {
            return Err(error("unknown parameter"));
        }
        for (name, ty) in spec {
            let optional = ty.ends_with('?') || ty.ends_with('*');
            let Some(v) = self.params.get(*name) else {
                if optional {
                    continue;
                }
                return Err(error("missing parameter"));
            };
            if v.is_null() && ty.ends_with('?') {
                continue;
            }
            let valid = match ty.as_bytes()[0] {
                b'i' => v.as_str().is_some_and(identity),
                b's' => v.as_str().is_some_and(text),
                b'b' => v.is_boolean(),
                b'n' => v.as_u64().is_some_and(|n| n <= MAX_INTEGER),
                b'o' => v.is_object(),
                b'a' => v
                    .as_array()
                    .is_some_and(|a| a.iter().all(|x| x.as_str().is_some_and(identity))),
                _ => false,
            };
            if !valid {
                return Err(error("invalid parameter"));
            }
        }
        match self.method {
            Initialize if self.params.get("protocol").and_then(Value::as_u64) != Some(PROTOCOL) => {
                return Err(error("incompatible protocol version"))
            }
            SelectionSet | WorkspaceDelete => {
                let target: Target = from_value(self.params["target"].clone())?;
                target.validate(false)?;
                if self.method == WorkspaceDelete
                    && (target.tab_id.is_some()
                        || target.pane_id.is_some()
                        || self.params.get("confirmed") != Some(&Value::Bool(true)))
                {
                    return Err(error("delete requires confirmed workspace-only target"));
                }
            }
            ReplySubmit | TerminalInput => {
                let lease: Lease = from_value(self.params["lease"].clone())?;
                lease.validate()?;
            }
            _ => {}
        }
        if self.method == SettingsValidate
            && (self.params.get("discard") == Some(&Value::Bool(true)))
                == self.params.contains_key("sections")
        {
            return Err(error("supply sections or discard"));
        }
        if self.method == TerminalInput
            && self.params.contains_key("text") == self.params.contains_key("keys")
        {
            return Err(error("supply text or keys"));
        }
        if self.method == LaunchOrchestration
            && self.params["worker_models"]
                .as_array()
                .is_some_and(Vec::is_empty)
        {
            return Err(error("worker models required"));
        }
        if self.method == LogsGet
            && self
                .params
                .get("limit")
                .is_some_and(|v| !(1..=1000).contains(&v.as_u64().unwrap_or(0)))
        {
            return Err(error("invalid log limit"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Target {
    pub socket_path: String,
    pub workspace_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tab_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pane_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub terminal_id: Option<String>,
}
impl Target {
    pub fn validate(&self, live: bool) -> Result<(), ProtocolError> {
        if !identity(&self.socket_path)
            || !identity(&self.workspace_id)
            || [&self.tab_id, &self.pane_id, &self.terminal_id]
                .into_iter()
                .flatten()
                .any(|s| !identity(s))
            || (self.terminal_id.is_some() && self.pane_id.is_none())
            || (live && (self.pane_id.is_none() || self.terminal_id.is_none()))
        {
            return Err(error("invalid target"));
        }
        Ok(())
    }
}
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Lease {
    pub target: Target,
    pub generation: u64,
    pub terminal_id: String,
}
impl Lease {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        self.target.validate(true)?;
        if self.generation > MAX_INTEGER
            || self.target.terminal_id.as_deref() != Some(&self.terminal_id)
        {
            return Err(error("invalid lease"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Readiness {
    pub can_reply: bool,
    pub explanation: String,
    pub state: String,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LayoutState {
    Available,
    LayoutUnavailable,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaneRect {
    pub pane_id: String,
    pub x: u64,
    pub y: u64,
    pub width: u64,
    pub height: u64,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Layout {
    pub state: LayoutState,
    pub rects: Vec<PaneRect>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub focused_pane_id: Option<String>,
}
impl Layout {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.rects.len() > MAX_ITEMS
            || (self.state == LayoutState::LayoutUnavailable && !self.rects.is_empty())
            || self.focused_pane_id.as_ref().is_some_and(|s| !identity(s))
        {
            return Err(error("invalid layout"));
        }
        let mut seen = HashSet::new();
        for r in &self.rects {
            if !identity(&r.pane_id)
                || !seen.insert(&r.pane_id)
                || r.width == 0
                || r.height == 0
                || [r.x, r.y, r.width, r.height]
                    .iter()
                    .any(|n| *n > MAX_INTEGER)
                || r.x.checked_add(r.width).is_none()
                || r.y.checked_add(r.height).is_none()
            {
                return Err(error("invalid layout rect"));
            }
        }
        Ok(())
    }
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Pane {
    pub pane_id: String,
    #[serde(default)]
    pub terminal_id: Option<String>,
    pub label: String,
    pub state: String,
    #[serde(default)]
    pub role: Option<String>,
    #[serde(default)]
    pub readiness: Option<Readiness>,
    pub metadata: Map<String, Value>,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Agent {
    #[serde(default)]
    pub pane_id: Option<String>,
    pub agent: String,
    #[serde(default)]
    pub role: Option<String>,
    pub state: String,
    #[serde(default)]
    pub readiness: Option<Readiness>,
    pub metadata: Map<String, Value>,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Tab {
    pub tab_id: String,
    pub label: String,
    pub panes: Vec<Pane>,
    pub layout: Layout,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Workspace {
    pub workspace_id: String,
    pub label: String,
    pub tabs: Vec<Tab>,
    pub agents: Vec<Agent>,
    pub metadata: Map<String, Value>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Connection {
    Connected,
    Offline,
    Error,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Session {
    pub socket_path: String,
    #[serde(default)]
    pub session: Option<String>,
    pub connection: Connection,
    #[serde(default)]
    pub error: Option<String>,
    pub workspaces: Vec<Workspace>,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Inventory {
    pub sessions: Vec<Session>,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaneFrame {
    pub target: Target,
    pub lease: Option<Lease>,
    pub text: String,
    pub readiness: Readiness,
    pub layout: Layout,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Accepted,
    Rejected,
    Uncertain,
    Pending,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReplyResult {
    pub request_id: RequestId,
    pub outcome: Outcome,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub lease: Option<Lease>,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OperationStatus {
    pub request_id: RequestId,
    pub outcome: Outcome,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub phase: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub status: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target: Option<Target>,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "event", deny_unknown_fields)]
pub enum BridgeEvent {
    #[serde(rename = "inventory")]
    Inventory { revision: u64, payload: Inventory },
    #[serde(rename = "pane_frame")]
    PaneFrame { revision: u64, payload: PaneFrame },
    #[serde(rename = "reply_result")]
    ReplyResult { revision: u64, payload: ReplyResult },
    #[serde(rename = "operation_status")]
    OperationStatus {
        revision: u64,
        payload: OperationStatus,
    },
}
impl BridgeEvent {
    pub fn revision(&self) -> u64 {
        match self {
            Self::Inventory { revision, .. }
            | Self::PaneFrame { revision, .. }
            | Self::ReplyResult { revision, .. }
            | Self::OperationStatus { revision, .. } => *revision,
        }
    }
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.revision() > MAX_INTEGER {
            return Err(error("invalid revision"));
        }
        fn ready(r: &Readiness) -> bool {
            identity(&r.state) && text(&r.explanation)
        }
        match self {
            Self::Inventory { payload, .. } => {
                for s in &payload.sessions {
                    if !identity(&s.socket_path) || s.session.as_ref().is_some_and(|x| !identity(x))
                    {
                        return Err(error("invalid session"));
                    }
                    for w in &s.workspaces {
                        if !identity(&w.workspace_id) {
                            return Err(error("invalid workspace"));
                        }
                        for a in &w.agents {
                            if !identity(&a.state)
                                || a.pane_id.as_ref().is_some_and(|x| !identity(x))
                                || a.readiness.as_ref().is_some_and(|r| !ready(r))
                            {
                                return Err(error("invalid agent"));
                            }
                        }
                        for t in &w.tabs {
                            if !identity(&t.tab_id) {
                                return Err(error("invalid tab"));
                            }
                            t.layout.validate()?;
                            let mut ids = HashSet::new();
                            for p in &t.panes {
                                if !identity(&p.pane_id)
                                    || !identity(&p.state)
                                    || p.terminal_id.as_ref().is_some_and(|x| !identity(x))
                                    || p.readiness.as_ref().is_some_and(|r| !ready(r))
                                    || !ids.insert(&p.pane_id)
                                {
                                    return Err(error("invalid pane"));
                                }
                            }
                            if t.layout.rects.iter().any(|r| !ids.contains(&r.pane_id))
                                || t.layout
                                    .focused_pane_id
                                    .as_ref()
                                    .is_some_and(|id| !ids.contains(id))
                            {
                                return Err(error("foreign layout pane"));
                            }
                        }
                    }
                }
            }
            Self::PaneFrame { payload: p, .. } => {
                p.target.validate(false)?;
                if p.target.pane_id.is_none()
                    || p.text.len() > MAX_TEXT_BYTES
                    || frame_line_count(&p.text) > 1000
                    || !ready(&p.readiness)
                {
                    return Err(error("invalid pane frame"));
                }
                p.layout.validate()?;
                if let Some(lease) = &p.lease {
                    lease.validate()?;
                    if lease.target != p.target {
                        return Err(error("frame lease mismatch"));
                    }
                }
            }
            Self::ReplyResult { payload: p, .. } => {
                if !p.request_id.valid() {
                    return Err(error("invalid result id"));
                }
                if let Some(l) = &p.lease {
                    l.validate()?;
                }
            }
            Self::OperationStatus { payload: p, .. } => {
                if !p.request_id.valid() {
                    return Err(error("invalid result id"));
                }
                if let Some(t) = &p.target {
                    t.validate(false)?;
                }
            }
        }
        Ok(())
    }
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BridgeError {
    pub code: String,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub field_errors: Option<HashMap<String, String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub outcome: Option<Outcome>,
}
#[derive(Debug, Clone, PartialEq)]
pub struct Response {
    pub id: RequestId,
    pub result: Result<Value, BridgeError>,
}
#[derive(Debug, Clone, PartialEq)]
pub enum Incoming {
    Event(Box<BridgeEvent>),
    Response(Response),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum UiOutcome {
    Quit,
    Attach {
        session: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        workspace_id: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        tab_id: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        pane_id: Option<String>,
    },
    Workspace {
        workspace_id: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        session: Option<String>,
    },
}
impl UiOutcome {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        let valid = match self {
            Self::Quit => true,
            Self::Attach {
                session,
                workspace_id,
                tab_id,
                pane_id,
            } => {
                identity(session)
                    && [workspace_id, tab_id, pane_id]
                        .into_iter()
                        .flatten()
                        .all(|s| identity(s))
            }
            Self::Workspace {
                workspace_id,
                session,
            } => identity(workspace_id) && session.as_ref().is_none_or(|s| identity(s)),
        };
        if valid {
            Ok(())
        } else {
            Err(error("invalid UI outcome"))
        }
    }
    /// The caller supplies a dedicated inherited pipe, never terminal stdout.
    pub fn write_to(&self, mut pipe: impl Write) -> Result<(), ProtocolError> {
        self.validate()?;
        let data = encode(self)?;
        pipe.write_all(&data)
            .map_err(|_| error("outcome pipe failed"))
    }
}

// A recursive visitor detects duplicate keys, including inside arbitrary metadata.
struct Unique;
impl<'de> DeserializeSeed<'de> for Unique {
    type Value = Value;
    fn deserialize<D: Deserializer<'de>>(self, d: D) -> Result<Value, D::Error> {
        d.deserialize_any(UniqueVisitor)
    }
}
struct UniqueVisitor;
impl<'de> Visitor<'de> for UniqueVisitor {
    type Value = Value;
    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("JSON without duplicate keys")
    }
    fn visit_bool<E: de::Error>(self, v: bool) -> Result<Value, E> {
        Ok(Value::Bool(v))
    }
    fn visit_i64<E: de::Error>(self, v: i64) -> Result<Value, E> {
        Ok(Value::Number(v.into()))
    }
    fn visit_u64<E: de::Error>(self, v: u64) -> Result<Value, E> {
        Ok(Value::Number(v.into()))
    }
    fn visit_f64<E: de::Error>(self, v: f64) -> Result<Value, E> {
        Number::from_f64(v)
            .map(Value::Number)
            .ok_or_else(|| E::custom("non-finite number"))
    }
    fn visit_str<E: de::Error>(self, v: &str) -> Result<Value, E> {
        Ok(Value::String(v.to_owned()))
    }
    fn visit_string<E: de::Error>(self, v: String) -> Result<Value, E> {
        Ok(Value::String(v))
    }
    fn visit_none<E: de::Error>(self) -> Result<Value, E> {
        Ok(Value::Null)
    }
    fn visit_unit<E: de::Error>(self) -> Result<Value, E> {
        Ok(Value::Null)
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Value, A::Error> {
        let mut out = Vec::new();
        while let Some(v) = seq.next_element_seed(Unique)? {
            if out.len() >= MAX_ITEMS {
                return Err(de::Error::custom("array limit"));
            }
            out.push(v);
        }
        Ok(Value::Array(out))
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Value, A::Error> {
        let mut out = Map::new();
        while let Some(k) = map.next_key::<String>()? {
            if out.len() >= MAX_ITEMS || out.contains_key(&k) {
                return Err(de::Error::custom("duplicate key or object limit"));
            }
            let v = map.next_value_seed(Unique)?;
            out.insert(k, v);
        }
        Ok(Value::Object(out))
    }
}
fn bounded(v: &Value, depth: usize) -> Result<(), ProtocolError> {
    if depth > 32 {
        return Err(error("structure nesting limit"));
    }
    match v {
        Value::String(s) if !text(s) => Err(error("string limit")),
        Value::Array(a) => {
            if a.len() > MAX_ITEMS {
                return Err(error("array limit"));
            }
            for x in a {
                bounded(x, depth + 1)?;
            }
            Ok(())
        }
        Value::Object(m) => {
            if m.len() > MAX_ITEMS || m.keys().any(|k| k.chars().count() > 4096) {
                return Err(error("object limit"));
            }
            for x in m.values() {
                bounded(x, depth + 1)?;
            }
            Ok(())
        }
        _ => Ok(()),
    }
}
fn from_value<T: for<'de> Deserialize<'de>>(v: Value) -> Result<T, ProtocolError> {
    serde_json::from_value(v).map_err(|_| error("invalid protocol fields"))
}
pub fn decode_value(line: &[u8]) -> Result<Value, ProtocolError> {
    if line.len() > MAX_MESSAGE_BYTES {
        return Err(error("message exceeds 2 MiB"));
    }
    if !line.ends_with(b"\n") || line[..line.len() - 1].contains(&b'\n') {
        return Err(error("expected one newline-terminated message"));
    }
    let mut de = serde_json::Deserializer::from_slice(&line[..line.len() - 1]);
    let value = Unique
        .deserialize(&mut de)
        .map_err(|_| error("invalid UTF-8 JSON or duplicate key"))?;
    de.end().map_err(|_| error("trailing data"))?;
    if !value.is_object() {
        return Err(error("envelope must be an object"));
    }
    bounded(&value, 0)?;
    Ok(value)
}
pub fn encode<T: Serialize>(value: &T) -> Result<Vec<u8>, ProtocolError> {
    let v = serde_json::to_value(value).map_err(|_| error("invalid JSON"))?;
    if !v.is_object() {
        return Err(error("envelope must be an object"));
    }
    bounded(&v, 0)?;
    let mut bytes = serde_json::to_vec(&v).map_err(|_| error("invalid JSON"))?;
    bytes.push(b'\n');
    if bytes.len() > MAX_MESSAGE_BYTES {
        return Err(error("message exceeds 2 MiB"));
    }
    Ok(bytes)
}
pub fn decode_request(line: &[u8]) -> Result<Request, ProtocolError> {
    let r: Request = from_value(decode_value(line)?)?;
    r.validate()?;
    Ok(r)
}
pub fn decode_incoming(line: &[u8]) -> Result<Incoming, ProtocolError> {
    let mut v = decode_value(line)?;
    let obj = v.as_object_mut().ok_or_else(|| error("invalid envelope"))?;
    if obj.contains_key("event") {
        let e: BridgeEvent = from_value(v)?;
        e.validate()?;
        return Ok(Incoming::Event(Box::new(e)));
    }
    if obj.len() != 2
        || !obj.contains_key("id")
        || (obj.contains_key("result") == obj.contains_key("error"))
    {
        return Err(error("invalid response envelope"));
    }
    let id: RequestId = from_value(obj.remove("id").ok_or_else(|| error("missing id"))?)?;
    if !id.valid() {
        return Err(error("invalid response id"));
    }
    let result = if let Some(value) = obj.remove("result") {
        Ok(value)
    } else {
        let e: BridgeError =
            from_value(obj.remove("error").ok_or_else(|| error("missing error"))?)?;
        if !identity(&e.code) || !text(&e.message) || e.outcome == Some(Outcome::Pending) {
            return Err(error("invalid response error"));
        }
        Err(e)
    };
    Ok(Incoming::Response(Response { id, result }))
}
pub fn decode_outcome(line: &[u8]) -> Result<UiOutcome, ProtocolError> {
    let v = decode_value(line)?;
    let obj = v.as_object().ok_or_else(|| error("invalid outcome"))?;
    let allowed: &[&str] = match obj.get("type").and_then(Value::as_str) {
        Some("quit") => &["type"],
        Some("attach") => &["type", "session", "workspace_id", "tab_id", "pane_id"],
        Some("workspace") => &["type", "workspace_id", "session"],
        _ => return Err(error("invalid outcome")),
    };
    if obj.keys().any(|key| !allowed.contains(&key.as_str())) {
        return Err(error("invalid outcome field"));
    }
    let o: UiOutcome = from_value(v)?;
    o.validate()?;
    Ok(o)
}

#[cfg(test)]
mod tests {
    use super::*;
    const FIXTURE: &str = include_str!("../../../tests/fixtures/rust_bridge_v1.json");
    fn line(v: &Value) -> Vec<u8> {
        encode(v).unwrap()
    }
    #[test]
    fn shared_golden_roundtrip() {
        let f: Value = serde_json::from_str(FIXTURE).unwrap();
        let k = "request";
        let r = decode_request(&line(&f[k])).unwrap();
        assert_eq!(serde_json::to_value(r).unwrap(), f[k]);
        for k in ["response", "error", "event"] {
            assert!(decode_incoming(&line(&f[k])).is_ok());
        }
        for v in f["events"].as_array().unwrap() {
            let Incoming::Event(e) = decode_incoming(&line(v)).unwrap() else {
                panic!()
            };
            assert_eq!(serde_json::to_value(e).unwrap(), *v);
        }
        for v in f["launch_requests"].as_array().unwrap() {
            assert!(decode_request(&line(v)).is_ok());
        }
        for v in f["outcomes"].as_array().unwrap() {
            assert!(decode_outcome(&line(v)).is_ok());
        }
    }
    #[test]
    fn bounded_nested_data_and_python_compatible_frame_line_limits() {
        let raw = |value: &Value| {
            let mut bytes = serde_json::to_vec(value).unwrap();
            bytes.push(b'\n');
            bytes
        };
        let nested = format!("{{\"nested\":{}0{}}}\n", "[".repeat(34), "]".repeat(34));
        assert!(decode_value(nested.as_bytes()).is_err());
        assert!(decode_value(&raw(&serde_json::json!({"nested":vec![0; MAX_ITEMS + 1]}))).is_err());
        assert!(decode_value(&raw(
            &serde_json::json!({"text":"x".repeat(MAX_TEXT_BYTES + 1)})
        ))
        .is_err());
        let f: Value = serde_json::from_str(FIXTURE).unwrap();
        for boundary in ["\n", "\r", "\r\n", "\u{85}", "\u{2028}", "\u{b}"] {
            let mut frame = f["events"][1].clone();
            frame["payload"]["text"] = Value::String(boundary.repeat(1000));
            assert!(decode_incoming(&raw(&frame)).is_ok(), "1000 {boundary:?}");
            frame["payload"]["text"] = Value::String(boundary.repeat(1001));
            assert!(decode_incoming(&raw(&frame)).is_err(), "1001 {boundary:?}");
        }
        let mut mismatch = f["events"][1].clone();
        mismatch["payload"]["lease"]["generation"] = serde_json::json!(MAX_INTEGER + 1);
        assert!(decode_incoming(&raw(&mismatch)).is_err());
        mismatch = f["events"][1].clone();
        mismatch["payload"]["lease"]["target"]["terminal_id"] = serde_json::json!("other");
        assert!(decode_incoming(&raw(&mismatch)).is_err());
    }
    #[test]
    fn private_outcome_pipe_has_only_typed_json() {
        let mut bytes = Vec::new();
        UiOutcome::Attach {
            session: "sess".into(),
            workspace_id: Some("w".into()),
            tab_id: None,
            pane_id: None,
        }
        .write_to(&mut bytes)
        .unwrap();
        assert_eq!(
            decode_outcome(&bytes).unwrap(),
            UiOutcome::Attach {
                session: "sess".into(),
                workspace_id: Some("w".into()),
                tab_id: None,
                pane_id: None
            }
        );
        assert!(UiOutcome::Workspace {
            workspace_id: " ".into(),
            session: None
        }
        .write_to(&mut Vec::new())
        .is_err());
        assert!(
            decode_outcome(b"{\"type\":\"attach\",\"session\":\"s\",\"args\":[\"sh\"]}\n").is_err()
        );
    }
    #[test]
    fn fail_closed_on_invalid_data() {
        for v in [
            b"{\"x\":1,\"x\":2}\n".as_slice(),
            b"{\"id\":1}".as_slice(),
            b"{\"a\":NaN}\n".as_slice(),
            b"{\"a\":\"\xff\"}\n".as_slice(),
        ] {
            assert!(decode_value(v).is_err());
        }
        assert!(decode_value(&vec![b'a'; MAX_MESSAGE_BYTES + 1]).is_err());
        assert!(
            decode_request(b"{\"id\":1,\"method\":\"arbitrary.exec\",\"params\":{}}\n").is_err()
        );
        assert!(decode_request(
            b"{\"id\":1,\"method\":\"initialize\",\"params\":{\"protocol\":2}}\n"
        )
        .is_err());
        assert!(decode_outcome(b"{\"type\":\"quit\",\"executable\":\"sh\"}\n").is_err());
    }
}
