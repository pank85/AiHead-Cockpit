//! Product panels are supplied by the binary, not mocked by the shell.
use crate::{
    input::Focus,
    protocol::{Incoming, Request, RequestId, Target, UiOutcome, MAX_INTEGER},
};
use crossterm::event::Event;
use ratatui::{layout::Rect, Frame};
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Arc,
};

/// Shared ID namespace for shell and modal-originated requests.
#[derive(Debug, Clone)]
pub struct RequestIds(Arc<AtomicU64>);
impl Default for RequestIds {
    fn default() -> Self {
        Self(Arc::new(AtomicU64::new(1)))
    }
}
impl RequestIds {
    pub fn reserve(&self) -> RequestId {
        let id = self
            .0
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |current| {
                Some(if current >= MAX_INTEGER {
                    1
                } else {
                    current + 1
                })
            })
            .expect("request ID update is infallible");
        RequestId::Integer(id)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ActionId {
    AiHead,
    NewMaster,
    NewErgo,
    Orchestrate,
    AiCouncil,
    Settings,
    Logs,
    TaskDetails,
    OpenInHerdr,
    Delete,
    Quit,
}
impl ActionId {
    pub const PRIMARY: [Self; 6] = [
        Self::AiHead,
        Self::NewMaster,
        Self::NewErgo,
        Self::Orchestrate,
        Self::AiCouncil,
        Self::Settings,
    ];
    pub const ALL: [Self; 11] = [
        Self::AiHead,
        Self::NewMaster,
        Self::NewErgo,
        Self::Orchestrate,
        Self::AiCouncil,
        Self::Settings,
        Self::Logs,
        Self::TaskDetails,
        Self::OpenInHerdr,
        Self::Delete,
        Self::Quit,
    ];
    pub fn label(self) -> &'static str {
        match self {
            Self::AiHead => "AiHead",
            Self::NewMaster => "New Master",
            Self::NewErgo => "New Ergo",
            Self::Orchestrate => "Orchestrate",
            Self::AiCouncil => "AI Council",
            Self::Settings => "Settings",
            Self::Logs => "Logs",
            Self::TaskDetails => "Task Details",
            Self::OpenInHerdr => "Open",
            Self::Delete => "Delete",
            Self::Quit => "Quit",
        }
    }
}
#[derive(Debug, Clone, PartialEq)]
pub enum UiAction {
    Dispatch(Request),
    Activate(ActionId),
    Finish(UiOutcome),
}
#[derive(Debug, Clone)]
pub struct PanelContext {
    pub area: Rect,
    pub active_target: Option<Target>,
    pub focus: Focus,
    pub pending: bool,
    /// Allocate unique request IDs from the shell's namespace for panel dispatches.
    pub request_ids: RequestIds,
}
#[derive(Debug, Default)]
pub struct PanelResponse {
    pub consumed: bool,
    pub close: bool,
    pub actions: Vec<UiAction>,
}
impl PanelResponse {
    pub fn consumed() -> Self {
        Self {
            consumed: true,
            ..Self::default()
        }
    }
    pub fn close() -> Self {
        Self {
            consumed: true,
            close: true,
            actions: vec![],
        }
    }
}
/// Modal receives every event before terminal/chrome. The owner can return typed
/// requests/outcomes; the shell never invents forms, catalogs, or launch defaults.
pub trait Panel {
    fn render(&mut self, frame: &mut Frame<'_>, area: Rect, context: &PanelContext);
    fn handle(&mut self, event: &Event, context: &PanelContext) -> PanelResponse;
    /// Correlated replies/events remain available to product panels.
    fn on_bridge(&mut self, _message: &Incoming, _context: &PanelContext) -> PanelResponse {
        PanelResponse::default()
    }
}
