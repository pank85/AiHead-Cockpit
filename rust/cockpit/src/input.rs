//! Explicit terminal-focus encoding; these values go to the bridge, never to stdout.
use crate::protocol::MAX_TEXT_BYTES;
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Focus {
    Chrome,
    Reply,
    Terminal,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TerminalInput {
    Text(String),
    Keys(Vec<String>),
    ReturnToChrome,
}
/// Ctrl+C is reserved for quitting the native UI, including its raw byte form.
pub(crate) fn is_quit_key(key: KeyEvent) -> bool {
    match key.code {
        KeyCode::Char('\u{3}') => true,
        KeyCode::Char(c) => {
            key.modifiers.contains(KeyModifiers::CONTROL) && c.eq_ignore_ascii_case(&'c')
        }
        _ => false,
    }
}
pub fn terminal_key(key: KeyEvent) -> Option<TerminalInput> {
    if key.kind == KeyEventKind::Release || is_quit_key(key) {
        return None;
    }
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    if (ctrl && key.code == KeyCode::Char(']')) || key.code == KeyCode::Char('\u{1d}') {
        return Some(TerminalInput::ReturnToChrome);
    }
    let name = match key.code {
        KeyCode::Esc => Some("esc"),
        KeyCode::Enter => Some("enter"),
        KeyCode::Backspace => Some("backspace"),
        KeyCode::Tab if key.modifiers.contains(KeyModifiers::SHIFT) => Some("backtab"),
        KeyCode::Tab => Some("tab"),
        KeyCode::BackTab => Some("backtab"),
        KeyCode::Up => Some("up"),
        KeyCode::Down => Some("down"),
        KeyCode::Left => Some("left"),
        KeyCode::Right => Some("right"),
        KeyCode::Home => Some("home"),
        KeyCode::End => Some("end"),
        KeyCode::PageUp => Some("pageup"),
        KeyCode::PageDown => Some("pagedown"),
        KeyCode::Delete => Some("delete"),
        KeyCode::Char(c) if ctrl => match c.to_ascii_lowercase() {
            'd' => Some("ctrl+d"),
            'z' => Some("ctrl+z"),
            'l' => Some("ctrl+l"),
            'u' => Some("ctrl+u"),
            'w' => Some("ctrl+w"),
            'a' => Some("ctrl+a"),
            'e' => Some("ctrl+e"),
            'k' => Some("ctrl+k"),
            'r' => Some("ctrl+r"),
            _ => None,
        },
        KeyCode::Char(c)
            if !c.is_control()
                && !key.modifiers.intersects(
                    KeyModifiers::CONTROL | KeyModifiers::ALT | KeyModifiers::SUPER,
                ) =>
        {
            return Some(TerminalInput::Text(c.to_string()))
        }
        _ => None,
    };
    name.map(|s| TerminalInput::Keys(vec![s.to_owned()]))
}
pub fn terminal_paste(text: String) -> Option<TerminalInput> {
    if !text.is_empty() && text.len() <= MAX_TEXT_BYTES {
        Some(TerminalInput::Text(text))
    } else {
        None
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn encodes_keys_paste_and_reserved_escape() {
        let k = |c, m| KeyEvent::new(c, m);
        assert_eq!(
            terminal_key(k(KeyCode::Char(']'), KeyModifiers::CONTROL)),
            Some(TerminalInput::ReturnToChrome)
        );
        assert_eq!(
            terminal_key(k(KeyCode::Esc, KeyModifiers::NONE)),
            Some(TerminalInput::Keys(vec!["esc".into()]))
        );
        assert_eq!(
            terminal_key(k(KeyCode::Char('中'), KeyModifiers::NONE)),
            Some(TerminalInput::Text("中".into()))
        );
        for key in [
            k(KeyCode::Char('c'), KeyModifiers::CONTROL),
            k(
                KeyCode::Char('C'),
                KeyModifiers::CONTROL | KeyModifiers::SHIFT,
            ),
            k(KeyCode::Char('\u{3}'), KeyModifiers::NONE),
        ] {
            assert_eq!(terminal_key(key), None, "quit keys must not reach a pane");
        }
        assert_eq!(
            terminal_key(k(KeyCode::Char('d'), KeyModifiers::CONTROL)),
            Some(TerminalInput::Keys(vec!["ctrl+d".into()]))
        );
        assert_eq!(
            terminal_key(k(KeyCode::Char('c'), KeyModifiers::NONE)),
            Some(TerminalInput::Text("c".into()))
        );
        assert_eq!(
            terminal_paste("hello\n".into()),
            Some(TerminalInput::Text("hello\n".into()))
        );
        assert!(terminal_paste("x".repeat(MAX_TEXT_BYTES + 1)).is_none());
    }
}
