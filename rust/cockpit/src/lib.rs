//! Native, terminal-safe cockpit foundation. Rust 1.85.1 is the tested MSRV.
//! The Python bridge is the authority for target identity and mutations.

pub mod input;
pub mod layout;
pub mod output;
pub mod panel;
pub mod protocol;
pub mod shell;
pub mod terminal;
pub mod transport;

pub use panel::{ActionId, Panel, PanelContext, PanelResponse, UiAction};
pub use protocol::UiOutcome;
pub use shell::Shell;
