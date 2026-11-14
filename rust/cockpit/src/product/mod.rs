//! Native product panels. Server drafts and catalog are the only configuration authority.
mod dialogs;
mod folders;
mod forms;
mod settings;
mod workflows;

use self::{
    dialogs::Hits,
    folders::FolderBrowser,
    forms::{key, Editor, Kind, Row},
    settings::Draft,
};
use aihead_cockpit::{
    panel::{ActionId, Panel, PanelContext, PanelResponse, UiAction},
    protocol::{BridgeEvent, Incoming, Method, Outcome, Request, RequestId, Target},
};
use crossterm::event::{
    Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEventKind,
};
use ratatui::{layout::Rect, Frame};
use serde_json::{json, Value};
use std::{cell::RefCell, rc::Rc};

#[derive(Default)]
pub struct Mailbox {
    pub select: Option<Target>,
}
#[derive(Clone, Debug)]
enum Pending {
    Open,
    Folder,
    Validate,
    Preview,
    SwitchPrepare(String),
    SwitchApply,
    Save,
    Launch,
    Delete,
    Logs,
}
#[derive(Debug)]
struct Nested {
    section: &'static str,
    original: Value,
}

pub struct ProductPanel {
    action: ActionId,
    root: Value,
    draft: Option<Draft>,
    session: Option<String>,
    target: Option<Target>,
    tab: usize,
    selected: usize,
    scroll: usize,
    editor: Option<Editor>,
    folder: Option<FolderBrowser>,
    nested: Option<Nested>,
    feedback_open: bool,
    pending: Option<(RequestId, Pending)>,
    status: String,
    hits: Hits,
    finished: bool,
    mailbox: Rc<RefCell<Mailbox>>,
    notice: Option<String>,
    notice_scroll: u16,
}
const TABS: [(&str, &str); 5] = [
    ("projects", "Projects & Folders"),
    ("squad", "Ergo Squad"),
    ("orchestration", "Orchestration"),
    ("council", "AI Council"),
    ("ai_head", "AiHead Persona"),
];
impl ProductPanel {
    pub fn new(
        action: ActionId,
        project: String,
        session: Option<String>,
        target: Option<Target>,
        details: Option<Value>,
        initial_id: RequestId,
        mailbox: Rc<RefCell<Mailbox>>,
    ) -> (Self, Option<Request>) {
        let mut this = Self {
            action,
            root: json!({"form":{"project_path":project},"sections":{}}),
            draft: None,
            session,
            target,
            tab: 0,
            selected: 0,
            scroll: 0,
            editor: None,
            folder: None,
            nested: None,
            feedback_open: false,
            pending: None,
            status: "Loading project…".into(),
            hits: Hits::default(),
            finished: false,
            mailbox,
            notice: None,
            notice_scroll: 0,
        };
        let req = match action {
            ActionId::Logs => {
                this.status = "Loading logs…".into();
                this.pending = Some((initial_id.clone(), Pending::Logs));
                Request::new(
                    initial_id,
                    Method::LogsGet,
                    json!({"limit":500}).as_object().unwrap().clone(),
                )
                .ok()
            }
            ActionId::Delete => {
                this.status =
                    "Confirm deletion of exactly the selected workspace. This cannot be undone."
                        .into();
                if this.target.is_none() {
                    this.notice = Some("Select a workspace before deleting.".into());
                }
                None
            }
            ActionId::TaskDetails => {
                this.notice = Some(details.map_or_else(
                    || "Select a workspace to inspect its metadata.".into(),
                    |v| serde_json::to_string_pretty(&v).unwrap_or_default(),
                ));
                None
            }
            _ => {
                this.pending = Some((initial_id.clone(), Pending::Open));
                match workflows::open_project(
                    initial_id,
                    this.root["form"]["project_path"].as_str().unwrap_or(""),
                ) {
                    Ok(r) => Some(r),
                    Err(e) => {
                        this.pending = None;
                        this.status = e;
                        None
                    }
                }
            }
        };
        (this, req)
    }
    fn rows(&self) -> Vec<Row> {
        let Some(d) = &self.draft else { return vec![] };
        let mut catalog = d.catalog.clone();
        if self.action == ActionId::Orchestrate && self.nested.is_none() {
            if let Some(native) = catalog.get("native_harnesses").cloned() {
                catalog["harnesses"] = native;
            }
        }
        if let Some(n) = &self.nested {
            return forms::rows(&self.root, &[key("sections"), key(n.section)], &catalog);
        }
        if self.action == ActionId::Settings {
            let mut rows = forms::rows(&self.root, &[key("form"), key("project_path")], &catalog);
            rows.extend(forms::rows(
                &self.root,
                &[key("sections"), key(TABS[self.tab].0)],
                &catalog,
            ));
            rows
        } else {
            forms::rows(&self.root, &[key("form")], &catalog)
        }
    }
    fn send(&mut self, request: Result<Request, String>, pending: Pending) -> PanelResponse {
        match request {
            Ok(r) => {
                self.pending = Some((r.id.clone(), pending));
                self.status = "Waiting for bridge…".into();
                PanelResponse {
                    consumed: true,
                    close: false,
                    actions: vec![UiAction::Dispatch(r)],
                }
            }
            Err(e) => {
                self.status = e;
                PanelResponse::consumed()
            }
        }
    }
    fn discard(&self, ctx: &PanelContext) -> Vec<UiAction> {
        self.draft
            .as_ref()
            .and_then(|d| {
                d.request(ctx.request_ids.reserve(), Method::SettingsValidate, None)
                    .ok()
            })
            .map_or_else(Vec::new, |r| vec![UiAction::Dispatch(r)])
    }
    fn open_project(&mut self, ctx: &PanelContext) -> PanelResponse {
        let path = self.root["form"]["project_path"]
            .as_str()
            .unwrap_or("")
            .to_owned();
        let mut actions = self.discard(ctx);
        self.draft = None;
        self.root["sections"] = json!({});
        self.editor = None;
        self.selected = 0;
        self.scroll = 0;
        self.finished = false;
        let mut result = self.send(
            workflows::open_project(ctx.request_ids.reserve(), &path),
            Pending::Open,
        );
        actions.append(&mut result.actions);
        result.actions = actions;
        result
    }
    fn validate(&mut self, ctx: &PanelContext, pending: Pending, sections: Value) -> PanelResponse {
        let req = self
            .draft
            .as_ref()
            .ok_or_else(|| "No project loaded".to_owned())
            .and_then(|d| {
                d.request(
                    ctx.request_ids.reserve(),
                    Method::SettingsValidate,
                    Some(sections),
                )
            });
        self.send(req, pending)
    }
    fn cycle(&mut self, row: &Row, back: bool, ctx: &PanelContext) -> PanelResponse {
        let old = forms::get(&self.root, &row.path).cloned();
        forms::cycle(&mut self.root, row, back);
        let changed = forms::get(&self.root, &row.path).cloned();
        let path = &row.path;
        let orchestration_harness = (path
            == &vec![key("sections"), key("orchestration"), key("harness")]
            && self.action == ActionId::Settings)
            || (path == &vec![key("form"), key("harness")]
                && self.action == ActionId::Orchestrate
                && self.nested.is_none());
        if orchestration_harness && old != changed {
            let Some(new) = changed.and_then(|v| v.as_str().map(str::to_owned)) else {
                return PanelResponse::consumed();
            };
            if let Some(v) = forms::get_mut(&mut self.root, path) {
                *v = old.unwrap_or(Value::Null);
            }
            if self.action == ActionId::Orchestrate {
                let form = self.root["form"].clone();
                workflows::orchestration_to_sections(&form, &mut self.root["sections"]);
            }
            let sections = self.root["sections"].clone();
            return self.validate(ctx, Pending::SwitchPrepare(new), sections);
        }
        PanelResponse::consumed()
    }
    fn main_action(&mut self, ctx: &PanelContext) -> PanelResponse {
        if let Some(n) = self.nested.take() {
            if self.action == ActionId::NewErgo {
                self.root["form"]["customize_first"] = json!(true);
            }
            if self.action == ActionId::AiCouncil {
                self.root["form"]["customize_roster"] = json!(true);
            }
            self.selected = 0;
            self.scroll = 0;
            self.status = format!("{} edits applied to this launch only", n.section);
            return PanelResponse::consumed();
        }
        if self.finished {
            self.status = "Operation finished. Close this panel before starting another.".into();
            return PanelResponse::consumed();
        }
        if self.action == ActionId::Delete {
            let Some(mut target) = self.target.clone() else {
                self.status = "Select a workspace first".into();
                return PanelResponse::consumed();
            };
            target.tab_id = None;
            target.pane_id = None;
            target.terminal_id = None;
            let params = json!({"target":target,"confirmed":true})
                .as_object()
                .unwrap()
                .clone();
            return self.send(
                Request::new(ctx.request_ids.reserve(), Method::WorkspaceDelete, params)
                    .map_err(|e| e.to_string()),
                Pending::Delete,
            );
        }
        let Some(d) = &self.draft else {
            self.status = "Load a project first".into();
            return PanelResponse::consumed();
        };
        if self.root["form"]["project_path"].as_str() != Some(&d.project) {
            self.status = "Project changed: F5 loads that project's own settings".into();
            return PanelResponse::consumed();
        }
        if self.action == ActionId::Settings {
            let req = d.request(
                ctx.request_ids.reserve(),
                Method::SettingsSave,
                Some(self.root["sections"].clone()),
            );
            return self.send(req, Pending::Save);
        }
        let req = workflows::request(
            self.action,
            ctx.request_ids.reserve(),
            &self.root["form"],
            &self.root["sections"],
            &d.project,
            self.session.as_deref(),
        );
        self.send(req, Pending::Launch)
    }
    fn browse(&mut self, ctx: &PanelContext, rows: &[Row]) -> PanelResponse {
        let destination = rows
            .get(self.selected)
            .filter(|r| {
                matches!(r.kind, Kind::Text { .. })
                    && (r.label.contains("path")
                        || r.label.contains("folder")
                        || r.label.ends_with("cwd"))
            })
            .map_or_else(
                || vec![key("form"), key("project_path")],
                |r| r.path.clone(),
            );
        let initial = forms::get(&self.root, &destination)
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .or_else(|| self.draft.as_ref().map(|d| d.project.as_str()))
            .unwrap_or(".");
        let mut folder = FolderBrowser::new(destination, initial);
        folder.set_favorites(&self.root["sections"]["projects"]["favorites"]);
        self.folder = Some(folder);
        self.folder_request(ctx)
    }
    fn folder_request(&mut self, ctx: &PanelContext) -> PanelResponse {
        let req = self.folder.as_ref().unwrap().request(
            ctx.request_ids.reserve(),
            self.draft.as_ref().map(|d| d.project.as_str()),
        );
        self.send(req, Pending::Folder)
    }
    fn folder_event(&mut self, event: &Event, ctx: &PanelContext) -> PanelResponse {
        let folder = self.folder.as_mut().unwrap();
        if let Event::Paste(s) = event {
            if folder.editing {
                folder.path.insert(s);
            }
            return PanelResponse::consumed();
        }
        if let Event::Mouse(m) = event {
            match m.kind {
                MouseEventKind::Down(MouseButton::Left)
                    if dialogs::contains(self.hits.rows, m.column, m.row) =>
                {
                    let start = folder
                        .selected
                        .saturating_sub((self.hits.rows.height as usize).saturating_sub(1));
                    let selected = start + usize::from(m.row - self.hits.rows.y);
                    if selected < folder.directories.len() {
                        folder.selected = selected;
                        folder.editing = false;
                        if folder.descend() {
                            return self.folder_request(ctx);
                        }
                    }
                }
                MouseEventKind::Down(MouseButton::Left)
                    if dialogs::contains(self.hits.footer, m.column, m.row) =>
                {
                    if m.column >= self.hits.footer.x + self.hits.footer.width / 2 {
                        self.folder = None;
                        self.status = "Folder selection cancelled".into();
                    } else if folder.verified_path.as_deref() != Some(folder.path.text.as_str()) {
                        folder.status = "Press Enter to check this path first".into();
                    } else {
                        let dest = folder.destination.clone();
                        let path = folder.path.text.clone();
                        if let Some(v) = forms::get_mut(&mut self.root, &dest) {
                            *v = json!(path);
                        }
                        self.folder = None;
                        self.status = "Folder chosen. F5 loads a changed project.".into();
                    }
                }
                MouseEventKind::ScrollDown => {
                    folder.selected =
                        (folder.selected + 1).min(folder.directories.len().saturating_sub(1))
                }
                MouseEventKind::ScrollUp => folder.selected = folder.selected.saturating_sub(1),
                _ => {}
            }
            return PanelResponse::consumed();
        }
        let Event::Key(k) = event else {
            return PanelResponse::consumed();
        };
        if k.kind == KeyEventKind::Release {
            return PanelResponse::consumed();
        }
        match k.code {
            KeyCode::Esc => {
                self.folder = None;
                self.status = "Folder selection cancelled".into();
            }
            KeyCode::Tab => folder.editing = !folder.editing,
            KeyCode::F(8) => {
                folder.editing = false;
                if folder.next_favorite() {
                    return self.folder_request(ctx);
                }
            }
            KeyCode::F(9) => {
                if folder.verified_path.as_deref() != Some(folder.path.text.as_str()) {
                    folder.status = "Press Enter to check this path first".into();
                } else {
                    let dest = folder.destination.clone();
                    let path = folder.path.text.clone();
                    if let Some(v) = forms::get_mut(&mut self.root, &dest) {
                        *v = json!(path);
                    }
                    self.folder = None;
                    self.status = "Folder chosen. F5 loads a changed project.".into();
                }
            }
            KeyCode::Enter if folder.editing => {
                folder.editing = false;
                return self.folder_request(ctx);
            }
            KeyCode::Enter => {
                if folder.descend() {
                    return self.folder_request(ctx);
                }
            }
            KeyCode::Up if !folder.editing => folder.selected = folder.selected.saturating_sub(1),
            KeyCode::Down if !folder.editing => {
                folder.selected =
                    (folder.selected + 1).min(folder.directories.len().saturating_sub(1))
            }
            KeyCode::Backspace if !folder.editing => {
                if let Some(p) = std::path::Path::new(&folder.path.text)
                    .parent()
                    .and_then(|p| p.to_str())
                    .filter(|p| !p.is_empty())
                {
                    folder.path.text = p.into();
                    folder.path.cursor = folder.path.text.len();
                    return self.folder_request(ctx);
                }
            }
            _ if folder.editing => folder.path.key(*k),
            _ => {}
        }
        PanelResponse::consumed()
    }
    fn key(&mut self, k: KeyEvent, ctx: &PanelContext) -> PanelResponse {
        if k.kind == KeyEventKind::Release {
            return PanelResponse::consumed();
        }
        if self.feedback_open {
            match k.code {
                KeyCode::Esc | KeyCode::F(1) | KeyCode::Enter => {
                    self.feedback_open = false;
                    self.notice_scroll = 0;
                }
                KeyCode::Down => self.notice_scroll = self.notice_scroll.saturating_add(1),
                KeyCode::Up => self.notice_scroll = self.notice_scroll.saturating_sub(1),
                KeyCode::PageDown => self.notice_scroll = self.notice_scroll.saturating_add(10),
                KeyCode::PageUp => self.notice_scroll = self.notice_scroll.saturating_sub(10),
                _ => {}
            }
            return PanelResponse::consumed();
        }
        if let Some(edit) = self.editor.as_mut() {
            match k.code {
                KeyCode::Esc => {
                    self.editor = None;
                    self.status = "Edit cancelled".into();
                }
                KeyCode::Tab | KeyCode::BackTab => {
                    let edit = self.editor.take().unwrap();
                    if edit.clone().commit(&mut self.root) {
                        let len = self.rows().len();
                        if len > 0 {
                            self.selected = if k.code == KeyCode::Tab {
                                (self.selected + 1) % len
                            } else {
                                (self.selected + len - 1) % len
                            };
                        }
                    } else {
                        self.editor = Some(edit);
                        self.status = "Enter a valid number".into();
                    }
                }
                KeyCode::Enter
                    if !edit.multiline || k.modifiers.contains(KeyModifiers::CONTROL) =>
                {
                    let edit = self.editor.take().unwrap();
                    if edit.clone().commit(&mut self.root) {
                        self.status = "Field updated".into();
                    } else {
                        self.editor = Some(edit);
                        self.status = "Enter a valid number".into();
                    }
                }
                _ => edit.key(k),
            }
            return PanelResponse::consumed();
        }
        if self.pending.is_some() {
            self.status = "Request pending; wait for its result. No automatic retry.".into();
            return PanelResponse::consumed();
        }
        if self.notice.is_some() {
            match k.code {
                KeyCode::Esc | KeyCode::Enter => return PanelResponse::close(),
                KeyCode::Down => self.notice_scroll = self.notice_scroll.saturating_add(1),
                KeyCode::Up => self.notice_scroll = self.notice_scroll.saturating_sub(1),
                KeyCode::PageDown => self.notice_scroll = self.notice_scroll.saturating_add(10),
                KeyCode::PageUp => self.notice_scroll = self.notice_scroll.saturating_sub(10),
                _ => {}
            }
            return PanelResponse::consumed();
        }
        let rows = self.rows();
        self.selected = self.selected.min(rows.len().saturating_sub(1));
        match k.code {
            KeyCode::Esc => {
                if let Some(n) = self.nested.take() {
                    self.root["sections"][n.section] = n.original;
                    self.selected = 0;
                    self.scroll = 0;
                    self.status = "Customization cancelled; no changes saved".into();
                    return PanelResponse::consumed();
                }
                return PanelResponse {
                    consumed: true,
                    close: true,
                    actions: self.discard(ctx),
                };
            }
            KeyCode::F(1) => {
                if (self.action == ActionId::Settings || self.action == ActionId::Orchestrate)
                    && self.draft.as_ref().is_some_and(|d| {
                        self.root["form"]["project_path"].as_str() == Some(&d.project)
                    })
                {
                    if self.action == ActionId::Orchestrate {
                        let form = self.root["form"].clone();
                        workflows::orchestration_to_sections(&form, &mut self.root["sections"]);
                    }
                    return self.validate(ctx, Pending::Preview, self.root["sections"].clone());
                }
                self.feedback_open = true;
            }
            KeyCode::F(2)
                if self.nested.is_none()
                    && matches!(self.action, ActionId::NewErgo | ActionId::AiCouncil) =>
            {
                let section = if self.action == ActionId::NewErgo {
                    "squad"
                } else {
                    "council"
                };
                self.nested = Some(Nested {
                    section,
                    original: self.root["sections"][section].clone(),
                });
                self.selected = 0;
                self.scroll = 0;
                self.status = "F9 applies customization; Esc cancels it".into();
            }
            KeyCode::F(9) => return self.main_action(ctx),
            KeyCode::F(5) if self.nested.is_none() => return self.open_project(ctx),
            KeyCode::F(4) if self.draft.is_some() => return self.browse(ctx, &rows),
            KeyCode::F(n @ (3 | 6 | 7 | 8)) => {
                if let Some(row) = rows.get(self.selected) {
                    let op = match n {
                        3 => 1,
                        6 => -1,
                        7 => -2,
                        _ => 2,
                    };
                    self.status = if forms::edit_array(&mut self.root, &row.path, op) {
                        "List updated"
                    } else {
                        "Select a list or an item in a list"
                    }
                    .into();
                }
            }
            KeyCode::Char('[') | KeyCode::PageUp
                if self.action == ActionId::Settings && self.nested.is_none() =>
            {
                self.tab = (self.tab + TABS.len() - 1) % TABS.len();
                self.selected = 0;
                self.scroll = 0;
            }
            KeyCode::Char(']') | KeyCode::PageDown
                if self.action == ActionId::Settings && self.nested.is_none() =>
            {
                self.tab = (self.tab + 1) % TABS.len();
                self.selected = 0;
                self.scroll = 0;
            }
            KeyCode::Up | KeyCode::BackTab => self.selected = self.selected.saturating_sub(1),
            KeyCode::Down | KeyCode::Tab => {
                self.selected = (self.selected + 1).min(rows.len().saturating_sub(1))
            }
            KeyCode::Enter | KeyCode::Right | KeyCode::Left => {
                if let Some(row) = rows.get(self.selected) {
                    match row.kind {
                        Kind::Text { .. } if k.code == KeyCode::Enter => {
                            self.editor = Editor::new(row, &self.root);
                            self.status="Edit: Enter/Tab applies; Esc reverts; multiline Ctrl+Enter applies".into();
                        }
                        Kind::Bool | Kind::Choice(_) => {
                            return self.cycle(row, k.code == KeyCode::Left, ctx)
                        }
                        _ => {}
                    }
                }
            }
            _ => {}
        }
        PanelResponse::consumed()
    }
}
impl Panel for ProductPanel {
    fn render(&mut self, frame: &mut Frame<'_>, area: Rect, _: &PanelContext) {
        if let Some(f) = &self.folder {
            self.hits = dialogs::folder(frame, area, f);
            return;
        }
        if let Some(n) = &self.notice {
            self.hits = dialogs::notice(
                frame,
                area,
                self.action.label(),
                n,
                "↑↓ scroll · Esc close",
                self.notice_scroll,
            );
            return;
        }
        if self.feedback_open {
            let text = self.draft.as_ref().map_or_else(
                || {
                    if self.action == ActionId::Delete {
                        format!(
                            "{}\n\nExact workspace target:\n{}",
                            self.status,
                            self.target.as_ref().map_or_else(
                                || "none".into(),
                                |t| serde_json::to_string_pretty(t).unwrap_or_default()
                            )
                        )
                    } else {
                        self.status.clone()
                    }
                },
                |d| {
                    format!(
                        "{}\n\nDestination: {} ({})\n\nErrors:\n{}\n\nRoute previews:\n{}",
                        self.status,
                        d.destination,
                        d.scope,
                        settings::feedback(&d.errors),
                        serde_json::to_string_pretty(&d.previews).unwrap_or_default()
                    )
                },
            );
            self.hits = dialogs::notice(
                frame,
                area,
                "Feedback / routes",
                &text,
                "↑↓ scroll · F1/Esc returns",
                self.notice_scroll,
            );
            return;
        }
        let title = if let Some(n) = &self.nested {
            format!("Customize {}", n.section)
        } else if self.action == ActionId::Delete {
            format!(
                "Delete {}",
                self.target
                    .as_ref()
                    .map_or("workspace", |t| t.workspace_id.as_str())
            )
        } else {
            self.action.label().into()
        };
        let sub = if self.action == ActionId::Settings && self.nested.is_none() {
            format!(
                "[ ] tab {}/{}: {}",
                self.tab + 1,
                TABS.len(),
                TABS[self.tab].1
            )
        } else if self.nested.is_some() {
            "Local draft • F9 apply / Esc cancel".into()
        } else if matches!(self.action, ActionId::NewErgo | ActionId::AiCouncil) {
            "F2 customize · F1 feedback".into()
        } else if self.action == ActionId::Delete {
            "F1 verify exact workspace target".into()
        } else {
            "F1 feedback / route previews".into()
        };
        let status = self.status.clone();
        let rows = self.rows();
        self.selected = self.selected.min(rows.len().saturating_sub(1));
        let action = if self.nested.is_some() {
            "Apply"
        } else if self.action == ActionId::Settings {
            "Save"
        } else if self.action == ActionId::Delete {
            "Delete"
        } else {
            "Start"
        };
        self.hits = dialogs::form(
            frame,
            area,
            dialogs::FormView {
                title: &title,
                sub: &sub,
                rows: &rows,
                root: &self.root,
                selected: self.selected,
                edit: self.editor.as_ref(),
                status: &status,
                action,
            },
            &mut self.scroll,
        );
    }
    fn handle(&mut self, event: &Event, ctx: &PanelContext) -> PanelResponse {
        if self.folder.is_some() {
            if self.pending.is_some() {
                return PanelResponse::consumed();
            }
            return self.folder_event(event, ctx);
        }
        match event {
            Event::Key(k) => self.key(*k, ctx),
            Event::Paste(s) => {
                if let Some(e) = self.editor.as_mut() {
                    e.insert(s);
                }
                PanelResponse::consumed()
            }
            Event::Mouse(m) if m.kind == MouseEventKind::Down(MouseButton::Left) => {
                if dialogs::contains(self.hits.rows, m.column, m.row) {
                    let i = self.scroll + usize::from(m.row - self.hits.rows.y) / 2;
                    if i < self.rows().len() {
                        self.selected = i;
                        return self.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE), ctx);
                    }
                }
                if dialogs::contains(self.hits.footer, m.column, m.row)
                    && m.row == self.hits.footer.y + self.hits.footer.height.saturating_sub(1)
                {
                    let code = if self.notice.is_some()
                        || self.feedback_open
                        || m.column >= self.hits.footer.x + self.hits.footer.width / 2
                    {
                        KeyCode::Esc
                    } else {
                        KeyCode::F(9)
                    };
                    return self.key(KeyEvent::new(code, KeyModifiers::NONE), ctx);
                }
                PanelResponse::consumed()
            }
            Event::Mouse(m)
                if matches!(
                    m.kind,
                    MouseEventKind::ScrollDown | MouseEventKind::ScrollUp
                ) =>
            {
                let code = if m.kind == MouseEventKind::ScrollDown {
                    KeyCode::Down
                } else {
                    KeyCode::Up
                };
                self.key(KeyEvent::new(code, KeyModifiers::NONE), ctx)
            }
            _ => PanelResponse::consumed(),
        }
    }
    fn on_bridge(&mut self, message: &Incoming, ctx: &PanelContext) -> PanelResponse {
        if let Incoming::Event(event) = message {
            if let BridgeEvent::OperationStatus { payload, .. } = event.as_ref() {
                let Some((id, kind)) = &self.pending else {
                    return PanelResponse::default();
                };
                if *id != payload.request_id || !matches!(kind, Pending::Launch | Pending::Delete) {
                    return PanelResponse::default();
                }
                self.status = format!("{} ({:?})", payload.message, payload.outcome);
                if payload.outcome == Outcome::Pending {
                    return PanelResponse::default();
                }
                self.pending = None;
                self.finished = matches!(payload.outcome, Outcome::Accepted | Outcome::Uncertain);
                if self.action == ActionId::AiHead && payload.outcome == Outcome::Accepted {
                    self.mailbox.borrow_mut().select = payload.target.clone();
                }
                let actions = if payload.outcome == Outcome::Accepted {
                    Request::new(
                        ctx.request_ids.reserve(),
                        Method::InventoryRefresh,
                        Default::default(),
                    )
                    .ok()
                    .map_or_else(Vec::new, |r| vec![UiAction::Dispatch(r)])
                } else {
                    vec![]
                };
                return PanelResponse {
                    consumed: false,
                    close: false,
                    actions,
                };
            }
            return PanelResponse::default();
        }
        let Incoming::Response(response) = message else {
            return PanelResponse::default();
        };
        let Some((id, kind)) = self.pending.clone() else {
            return PanelResponse::default();
        };
        if id != response.id {
            return PanelResponse::default();
        }
        if matches!(kind, Pending::Launch | Pending::Delete)
            && response
                .result
                .as_ref()
                .ok()
                .and_then(|r| r.get("pending"))
                .and_then(Value::as_bool)
                == Some(true)
        {
            self.status = "Operation pending; waiting for correlated completion".into();
            return PanelResponse::default();
        }
        self.pending = None;
        let result = match &response.result {
            Ok(v) => v,
            Err(e) => {
                self.status = if let Some(d) = self.draft.as_mut() {
                    d.fail(e)
                } else {
                    e.message.clone()
                };
                if matches!(kind, Pending::Folder) {
                    if let Some(folder) = self.folder.as_mut() {
                        folder.status = self.status.clone();
                    }
                }
                if matches!(kind, Pending::Launch | Pending::Delete)
                    && e.outcome == Some(Outcome::Uncertain)
                {
                    self.finished = true;
                }
                return PanelResponse::default();
            }
        };
        match kind {
            Pending::Open => match Draft::open(result) {
                Ok((d, sections)) => {
                    let project = d.project.clone();
                    self.root["sections"] = sections;
                    self.root["form"] = if self.action == ActionId::Settings {
                        json!({"project_path":project})
                    } else {
                        workflows::form(self.action, &project, &self.root["sections"])
                    };
                    self.status = format!("Loaded {} ({})", d.destination, d.scope);
                    self.draft = Some(d);
                    let sections = self.root["sections"].clone();
                    return self.validate(ctx, Pending::Validate, sections);
                }
                Err(e) => self.status = e,
            },
            Pending::Folder => {
                if let Some(folder) = self.folder.as_mut() {
                    if let Err(e) = folder.apply(result) {
                        folder.status = e;
                    }
                }
            }
            Pending::Validate
            | Pending::Preview
            | Pending::Save
            | Pending::SwitchPrepare(_)
            | Pending::SwitchApply => {
                if let Some(d) = self.draft.as_mut() {
                    let valid = d.apply(&mut self.root, result);
                    let errors = settings::feedback(&d.errors);
                    self.status = if valid {
                        if matches!(kind, Pending::Save) {
                            format!("Saved {} ({})", d.destination, d.scope)
                        } else {
                            "Validated; F1 shows route previews".into()
                        }
                    } else {
                        format!("Validation: {errors} (F1 details)")
                    };
                    if matches!(kind, Pending::Preview) {
                        self.feedback_open = true;
                        self.notice_scroll = 0;
                    }
                    if let Pending::SwitchPrepare(new) = kind {
                        if result.get("sections").is_some() {
                            return self.validate(
                                ctx,
                                Pending::SwitchApply,
                                json!({"orchestration":{"harness":new}}),
                            );
                        }
                    } else if matches!(kind, Pending::SwitchApply)
                        && self.action == ActionId::Orchestrate
                    {
                        let sections = self.root["sections"].clone();
                        workflows::apply_orchestration(&mut self.root["form"], &sections);
                    }
                }
            }
            Pending::Logs => {
                self.notice = Some(
                    serde_json::to_string_pretty(result.get("records").unwrap_or(result))
                        .unwrap_or_default(),
                );
            }
            Pending::Launch | Pending::Delete => {
                self.status = "Unexpected operation response; inspect before retrying".into();
                self.finished = true;
            }
        }
        PanelResponse::default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use aihead_cockpit::{
        input::Focus,
        panel::RequestIds,
        protocol::{BridgeError, Response},
    };
    use crossterm::event::{MouseEvent, MouseEventKind};
    use ratatui::{backend::TestBackend, style::Color, Terminal};

    fn ctx(width: u16) -> PanelContext {
        PanelContext {
            area: Rect::new(0, 0, width, 24),
            active_target: None,
            focus: Focus::Chrome,
            pending: false,
            request_ids: RequestIds::default(),
        }
    }
    fn document(project: &str) -> Value {
        json!({"token":format!("token-{project}"),"revision":0,"project_path":project,"destination":"/parent/settings.toml","scope":"inherited",
        "catalog":{"harnesses":[{"kind":"alpha","allowed_efforts":["slow","fast"]},{"kind":"beta"}],"native_harnesses":["alpha","beta"]},
        "sections":{
          "projects":{"default_folder":null,"favorites":[{"name":"Home","path":"/one","unknown":"keep"}]},
          "squad":{"scout":{"kind":"alpha","model":"scout","effort":null},"worker_pool":[{"kind":"alpha","model":"worker","label":"W","order":0,"enabled":true,"extra":"keep"}]},
          "orchestration":{"harness":"alpha","scout_model":"s","architect_model":"a","reviewer_model":"r","worker_models":["w"],"scout_effort":null,"architect_effort":null,"reviewer_effort":null,"dangerously_skip_permissions":false,"harnesses":{"alpha":{"scout_model":"s"},"beta":{"scout_model":"other"}}},
          "council":{"scout":{"kind":"alpha","model":"judge"},"members":[{"kind":"alpha","model":"judge","label":"M","enabled":true,"order":0,"extra":"keep"}]},
          "ai_head":{"enabled":true,"soul":"hello","cwd":null,"binding":{"kind":"alpha","model":"guide"},"dangerously_skip_permissions":false}
        }})
    }
    fn response(id: RequestId, result: Result<Value, BridgeError>) -> Incoming {
        Incoming::Response(Response { id, result })
    }
    fn dispatched(r: PanelResponse) -> Request {
        match r.actions.into_iter().next().expect("dispatch") {
            UiAction::Dispatch(r) => r,
            _ => panic!("expected request"),
        }
    }
    fn keypress(panel: &mut ProductPanel, ctx: &PanelContext, code: KeyCode) -> PanelResponse {
        panel.handle(&Event::Key(KeyEvent::new(code, KeyModifiers::NONE)), ctx)
    }
    fn opened(action: ActionId, ctx: &PanelContext) -> ProductPanel {
        let (mut panel, initial) = ProductPanel::new(
            action,
            "/one".into(),
            Some("sess".into()),
            None,
            None,
            RequestId::Integer(900),
            Rc::new(RefCell::new(Mailbox::default())),
        );
        let initial = initial.unwrap();
        let doc = document("/one");
        let validate = dispatched(panel.on_bridge(&response(initial.id, Ok(doc.clone())), ctx));
        assert_eq!(validate.method, Method::SettingsValidate);
        panel.on_bridge(&response(validate.id,Ok(json!({"valid":true,"revision":1,"sections":doc["sections"],"route_previews":{"scout":"ready"}}))),ctx);
        panel
    }
    fn draw(panel: &mut ProductPanel, ctx: &PanelContext) -> Terminal<TestBackend> {
        let mut terminal =
            Terminal::new(TestBackend::new(ctx.area.width, ctx.area.height)).unwrap();
        terminal.draw(|f| panel.render(f, ctx.area, ctx)).unwrap();
        terminal
    }
    fn buffer_text(terminal: &Terminal<TestBackend>) -> String {
        let buf = terminal.backend().buffer();
        let mut out = String::new();
        for y in 0..buf.area.height {
            for x in 0..buf.area.width {
                out.push_str(buf[(x, y)].symbol());
            }
            out.push('\n');
        }
        out
    }
    #[test]
    fn all_settings_tabs_and_narrow_scroll_have_accessible_actions() {
        for width in [80, 40] {
            let c = ctx(width);
            let mut p = opened(ActionId::Settings, &c);
            for tab in 0..5 {
                assert_eq!(p.tab, tab);
                let count = p.rows().len();
                assert!(count >= 2);
                for _ in 1..count {
                    keypress(&mut p, &c, KeyCode::Down);
                    let _ = draw(&mut p, &c);
                }
                assert_eq!(p.selected, count - 1);
                let terminal = draw(&mut p, &c);
                let text = buffer_text(&terminal);
                assert!(text.contains("F9 Save"), "{width}: {text}");
                assert!(text.contains("Esc Cancel"), "{width}: {text}");
                assert!(
                    p.selected >= p.scroll
                        && p.selected < p.scroll + (p.hits.rows.height as usize / 2).max(1)
                );
                let y = p.hits.rows.y + ((p.selected - p.scroll) * 2) as u16;
                assert_eq!(
                    terminal.backend().buffer()[(p.hits.rows.x, y)].bg,
                    Color::Cyan
                );
                if tab < 4 {
                    keypress(&mut p, &c, KeyCode::Char(']'));
                }
            }
        }
    }
    #[test]
    fn edits_are_unicode_safe_and_cancel_never_saves() {
        let c = ctx(80);
        let mut p = opened(ActionId::Settings, &c);
        p.tab = 4;
        p.selected = p
            .rows()
            .iter()
            .position(|r| r.path == vec![key("sections"), key("ai_head"), key("soul")])
            .unwrap();
        keypress(&mut p, &c, KeyCode::Enter);
        p.handle(&Event::Paste("界🙂\nline".into()), &c);
        assert!(keypress(&mut p, &c, KeyCode::F(9)).actions.is_empty());
        keypress(&mut p, &c, KeyCode::Esc);
        assert_eq!(p.root["sections"]["ai_head"]["soul"], "hello");
        keypress(&mut p, &c, KeyCode::Enter);
        p.handle(&Event::Paste("界🙂\nline".into()), &c);
        p.handle(
            &Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::CONTROL)),
            &c,
        );
        assert_eq!(p.root["sections"]["ai_head"]["soul"], "hello界🙂\nline");
        let r = keypress(&mut p, &c, KeyCode::Esc);
        assert!(r.close);
        let req = dispatched(r);
        assert_eq!(req.method, Method::SettingsValidate);
        assert_eq!(req.params["discard"], true);
    }
    #[test]
    fn nested_customization_cancel_and_explicit_apply() {
        let c = ctx(80);
        let mut p = opened(ActionId::NewErgo, &c);
        let original = p.root["sections"]["squad"].clone();
        keypress(&mut p, &c, KeyCode::F(2));
        p.root["sections"]["squad"]["scout"]["model"] = json!("discarded");
        keypress(&mut p, &c, KeyCode::Esc);
        assert_eq!(p.root["sections"]["squad"], original);
        keypress(&mut p, &c, KeyCode::F(2));
        p.root["sections"]["squad"]["scout"]["model"] = json!("custom");
        keypress(&mut p, &c, KeyCode::F(9));
        p.root["form"]["save_default"] = json!(true);
        let req = dispatched(keypress(&mut p, &c, KeyCode::F(9)));
        assert_eq!(req.method, Method::LaunchErgo);
        assert_eq!(req.params["squad"]["scout"]["model"], "custom");
        assert_eq!(req.params["customize_first"], true);
        assert_eq!(req.params["save_default"], true);
        assert!(keypress(&mut p, &c, KeyCode::F(9)).actions.is_empty());
    }
    #[test]
    fn per_harness_switch_uses_revisioned_partial_patch_and_preview() {
        let c = ctx(80);
        let mut p = opened(ActionId::Settings, &c);
        p.tab = 2;
        p.root["sections"]["orchestration"]["scout_model"] = json!("edited-alpha");
        p.selected = p
            .rows()
            .iter()
            .position(|r| r.path == vec![key("sections"), key("orchestration"), key("harness")])
            .unwrap();
        let first = dispatched(keypress(&mut p, &c, KeyCode::Enter));
        assert_eq!(
            first.params["sections"]["orchestration"]["scout_model"],
            "edited-alpha"
        );
        let old = p.root["sections"].clone();
        let second = dispatched(p.on_bridge(
            &response(
                first.id,
                Ok(json!({"valid":true,"revision":2,"sections":old})),
            ),
            &c,
        ));
        assert_eq!(second.params["revision"], 2);
        assert_eq!(
            second.params["sections"],
            json!({"orchestration":{"harness":"beta"}})
        );
        let mut switched = old;
        switched["orchestration"]["harness"] = json!("beta");
        switched["orchestration"]["scout_model"] = json!("saved-beta");
        p.on_bridge(
            &response(
                second.id,
                Ok(json!({"valid":true,"revision":3,"sections":switched})),
            ),
            &c,
        );
        assert_eq!(
            p.root["sections"]["orchestration"]["scout_model"],
            "saved-beta"
        );
        let preview = dispatched(keypress(&mut p, &c, KeyCode::F(1)));
        assert_eq!(preview.params["revision"], 3);
        let sections = p.root["sections"].clone();
        p.on_bridge(&response(preview.id,Ok(json!({"valid":true,"revision":4,"sections":sections,"route_previews":{"scout":"route"}}))),&c);
        assert!(p.feedback_open);
        assert_eq!(p.draft.as_ref().unwrap().previews["scout"], "route");
    }
    #[test]
    fn save_failure_keeps_edits_and_uncertain_blocks_replay() {
        let c = ctx(80);
        let mut p = opened(ActionId::Settings, &c);
        p.root["sections"]["ai_head"]["soul"] = json!("unsaved");
        let req = dispatched(keypress(&mut p, &c, KeyCode::F(9)));
        let error = BridgeError {
            code: "io".into(),
            message: "disk".into(),
            field_errors: Some([("revision".into(), "2".into())].into()),
            outcome: Some(Outcome::Rejected),
        };
        p.on_bridge(&response(req.id, Err(error)), &c);
        assert_eq!(p.root["sections"]["ai_head"]["soul"], "unsaved");
        let retry = dispatched(keypress(&mut p, &c, KeyCode::F(9)));
        assert_eq!(retry.params["revision"], 2);
        let uncertain = BridgeError {
            code: "save_failed".into(),
            message: "unknown".into(),
            field_errors: None,
            outcome: Some(Outcome::Uncertain),
        };
        p.on_bridge(&response(retry.id, Err(uncertain)), &c);
        assert!(keypress(&mut p, &c, KeyCode::F(9)).actions.is_empty());
        assert!(p.status.contains("uncertain"));
    }
    #[test]
    fn project_switch_does_not_reuse_the_old_project_draft() {
        let c = ctx(80);
        let mut p = opened(ActionId::Settings, &c);
        p.root["sections"]["ai_head"]["soul"] = json!("private-one");
        p.root["form"]["project_path"] = json!("/two");
        assert!(keypress(&mut p, &c, KeyCode::F(9)).actions.is_empty());
        let r = keypress(&mut p, &c, KeyCode::F(5));
        assert_eq!(r.actions.len(), 2);
        let req = match r.actions.last().unwrap() {
            UiAction::Dispatch(r) => r.clone(),
            _ => panic!(),
        };
        assert_eq!(req.method, Method::ProjectOpen);
        assert_eq!(req.params["path"], "/two");
        let doc = document("/two");
        let validate = dispatched(p.on_bridge(&response(req.id, Ok(doc.clone())), &c));
        assert_ne!(
            validate.params["sections"]["ai_head"]["soul"],
            "private-one"
        );
        assert_eq!(p.draft.as_ref().unwrap().project, "/two");
    }
    #[test]
    fn delete_is_confirmed_workspace_only_and_cannot_double_send() {
        let c = ctx(40);
        let target = Target {
            socket_path: "/sock".into(),
            workspace_id: "w".into(),
            tab_id: Some("t".into()),
            pane_id: Some("p".into()),
            terminal_id: Some("term".into()),
        };
        let (mut p, _) = ProductPanel::new(
            ActionId::Delete,
            "/one".into(),
            None,
            Some(target),
            None,
            RequestId::Integer(900),
            Rc::new(RefCell::new(Mailbox::default())),
        );
        let req = dispatched(keypress(&mut p, &c, KeyCode::F(9)));
        assert_eq!(req.method, Method::WorkspaceDelete);
        assert_eq!(req.params["confirmed"], true);
        assert_eq!(
            req.params["target"],
            json!({"socket_path":"/sock","workspace_id":"w"})
        );
        assert!(keypress(&mut p, &c, KeyCode::F(9)).actions.is_empty());
    }
    #[test]
    fn council_custom_roster_round_trip_preserves_full_rows() {
        let c = ctx(80);
        let mut p = opened(ActionId::AiCouncil, &c);
        keypress(&mut p, &c, KeyCode::F(2));
        let member = vec![
            key("sections"),
            key("council"),
            key("members"),
            forms::Part::Index(0),
            key("model"),
        ];
        assert!(forms::edit_array(&mut p.root, &member, 1));
        p.root["sections"]["council"]["members"][1]["model"] = json!("second");
        keypress(&mut p, &c, KeyCode::F(9));
        let req = dispatched(keypress(&mut p, &c, KeyCode::F(9)));
        assert_eq!(req.method, Method::LaunchCouncil);
        assert_eq!(req.params["customize_roster"], true);
        assert_eq!(req.params["config"]["members"][1]["model"], "second");
        assert_eq!(req.params["config"]["members"][1]["extra"], "keep");
        assert!(!req.params.contains_key("ai_head"));
    }
    #[test]
    fn mouse_field_focus_and_footer_do_not_run_shortcuts_while_editing() {
        let c = ctx(40);
        let mut p = opened(ActionId::Settings, &c);
        let _ = draw(&mut p, &c);
        let click = Event::Mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: p.hits.rows.x,
            row: p.hits.rows.y,
            modifiers: KeyModifiers::NONE,
        });
        assert!(p.handle(&click, &c).actions.is_empty());
        assert!(p.editor.is_some());
        let footer = Event::Mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: p.hits.footer.x,
            row: p.hits.footer.y + p.hits.footer.height - 1,
            modifiers: KeyModifiers::NONE,
        });
        assert!(p.handle(&footer, &c).actions.is_empty());
        assert!(p.editor.is_some());
        keypress(&mut p, &c, KeyCode::Esc);
        assert!(p.editor.is_none());
        let r = p.handle(&footer, &c);
        assert_eq!(dispatched(r).method, Method::SettingsSave);
    }
    #[test]
    fn aihead_persona_tab_has_its_product_label() {
        assert_eq!(
            TABS.iter().find(|(key, _)| *key == "ai_head").unwrap().1,
            "AiHead Persona"
        );
    }
    #[test]
    fn mouse_cancel_label_never_triggers_save_at_either_width() {
        for width in [40, 80] {
            let c = ctx(width);
            let mut p = opened(ActionId::Settings, &c);
            let _ = draw(&mut p, &c);
            let click = Event::Mouse(MouseEvent {
                kind: MouseEventKind::Down(MouseButton::Left),
                column: p.hits.footer.x + p.hits.footer.width - 1,
                row: p.hits.footer.y + p.hits.footer.height - 1,
                modifiers: KeyModifiers::NONE,
            });
            let r = p.handle(&click, &c);
            assert!(r.close);
            let req = dispatched(r);
            assert_eq!(req.method, Method::SettingsValidate);
            assert_eq!(req.params["discard"], true);
        }
    }
    #[test]
    fn project_favorites_are_browsed_and_verified_before_selection() {
        let c = ctx(80);
        let mut p = opened(ActionId::Settings, &c);
        p.root["sections"]["projects"]["favorites"][0]["path"] = json!("/favorite");
        let r = p.key(KeyEvent::new(KeyCode::F(4), KeyModifiers::NONE), &c);
        let first = dispatched(r);
        p.on_bridge(
            &response(
                first.id,
                Ok(json!({"path":"/one","available":true,"directories":[]})),
            ),
            &c,
        );
        let req = dispatched(p.folder_event(
            &Event::Key(KeyEvent::new(KeyCode::F(8), KeyModifiers::NONE)),
            &c,
        ));
        assert_eq!(req.method, Method::FoldersList);
        assert_eq!(req.params["path"], "/favorite");
        assert_eq!(req.params["project"], "/one");
    }
    #[test]
    fn mouse_folder_browsing_and_validation() {
        let c = ctx(40);
        let mut p = opened(ActionId::Settings, &c);
        let req = dispatched(keypress(&mut p, &c, KeyCode::F(4)));
        p.on_bridge(&response(req.id,Ok(json!({"path":"/one","available":true,"directories":[{"name":"子","path":"/one/子"}]}))),&c);
        let _ = draw(&mut p, &c);
        let event = Event::Mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: p.hits.rows.x,
            row: p.hits.rows.y,
            modifiers: KeyModifiers::NONE,
        });
        let next = dispatched(p.handle(&event, &c));
        assert_eq!(next.params["path"], "/one/子");
        p.on_bridge(
            &response(
                next.id,
                Ok(json!({"path":"/one/子","available":true,"directories":[]})),
            ),
            &c,
        );
        keypress(&mut p, &c, KeyCode::F(9));
        assert!(p.folder.is_none());
        assert_eq!(p.root["form"]["project_path"], "/one/子");
    }
}
