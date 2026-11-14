//! One RAII owner for terminal modes. Never send untrusted pane bytes here.
use crossterm::{
    cursor::{Hide, Show},
    event::{DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use std::io::{self, Write};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Raw,
    Alternate,
    Mouse,
    Paste,
    CursorHidden,
}
pub trait TerminalControl {
    fn enable(&mut self, mode: Mode) -> io::Result<()>;
    fn disable(&mut self, mode: Mode) -> io::Result<()>;
}
pub struct CrosstermControl<W: Write> {
    writer: W,
}
impl<W: Write> CrosstermControl<W> {
    pub fn new(writer: W) -> Self {
        Self { writer }
    }
}
impl<W: Write> TerminalControl for CrosstermControl<W> {
    fn enable(&mut self, mode: Mode) -> io::Result<()> {
        match mode {
            Mode::Raw => enable_raw_mode(),
            Mode::Alternate => execute!(self.writer, EnterAlternateScreen),
            Mode::Mouse => execute!(self.writer, EnableMouseCapture),
            Mode::Paste => execute!(self.writer, EnableBracketedPaste),
            Mode::CursorHidden => execute!(self.writer, Hide),
        }
    }
    fn disable(&mut self, mode: Mode) -> io::Result<()> {
        match mode {
            Mode::Raw => disable_raw_mode(),
            Mode::Alternate => execute!(self.writer, LeaveAlternateScreen),
            Mode::Mouse => execute!(self.writer, DisableMouseCapture),
            Mode::Paste => execute!(self.writer, DisableBracketedPaste),
            Mode::CursorHidden => execute!(self.writer, Show),
        }
    }
}
const MODES: [Mode; 5] = [
    Mode::Raw,
    Mode::Alternate,
    Mode::Mouse,
    Mode::Paste,
    Mode::CursorHidden,
];
pub struct TerminalGuard<C: TerminalControl> {
    control: C,
    attempted: usize,
}
impl<C: TerminalControl> TerminalGuard<C> {
    pub fn enter(control: C) -> io::Result<Self> {
        let mut guard = Self {
            control,
            attempted: 0,
        };
        for mode in MODES {
            guard.attempted += 1;
            guard.control.enable(mode)?;
        }
        Ok(guard)
    }
    /// Attempt every cleanup even if a prior cleanup failed; subsequent Drop is inert.
    pub fn restore(&mut self) -> io::Result<()> {
        let mut first = None;
        while self.attempted > 0 {
            self.attempted -= 1;
            if let Err(e) = self.control.disable(MODES[self.attempted]) {
                if first.is_none() {
                    first = Some(e);
                }
            }
        }
        match first {
            Some(e) => Err(e),
            None => Ok(()),
        }
    }
}
impl<C: TerminalControl> Drop for TerminalGuard<C> {
    fn drop(&mut self) {
        let _ = self.restore();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::RefCell, rc::Rc};
    struct Fake {
        calls: Rc<RefCell<Vec<(bool, Mode)>>>,
        fail: Option<Mode>,
    }
    impl TerminalControl for Fake {
        fn enable(&mut self, m: Mode) -> io::Result<()> {
            self.calls.borrow_mut().push((true, m));
            if self.fail == Some(m) {
                Err(io::Error::other("injected"))
            } else {
                Ok(())
            }
        }
        fn disable(&mut self, m: Mode) -> io::Result<()> {
            self.calls.borrow_mut().push((false, m));
            Ok(())
        }
    }
    fn modes(c: &[(bool, Mode)], enabled: bool) -> Vec<Mode> {
        c.iter()
            .filter(|(e, _)| *e == enabled)
            .map(|(_, m)| *m)
            .collect()
    }
    #[test]
    fn cleanup_on_normal_error_and_panic() {
        let c = Rc::new(RefCell::new(vec![]));
        {
            let mut g = TerminalGuard::enter(Fake {
                calls: c.clone(),
                fail: None,
            })
            .unwrap();
            g.restore().unwrap();
            g.restore().unwrap();
        }
        assert_eq!(
            modes(&c.borrow(), false),
            MODES.into_iter().rev().collect::<Vec<_>>()
        );
        c.borrow_mut().clear();
        assert!(TerminalGuard::enter(Fake {
            calls: c.clone(),
            fail: Some(Mode::Paste)
        })
        .is_err());
        assert_eq!(
            modes(&c.borrow(), false),
            vec![Mode::Paste, Mode::Mouse, Mode::Alternate, Mode::Raw]
        );
        c.borrow_mut().clear();
        let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _g = TerminalGuard::enter(Fake {
                calls: c.clone(),
                fail: None,
            })
            .unwrap();
            panic!("test panic");
        }));
        assert_eq!(modes(&c.borrow(), false).len(), 5);
    }
}
