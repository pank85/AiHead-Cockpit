//! Convert untrusted pane bytes to safe Ratatui spans, never terminal commands.
use ratatui::{
    style::{Color, Modifier, Style},
    text::{Line, Span},
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum State {
    Text,
    Escape,
    EscapeIntermediate,
    Csi,
    String,
    StringEscape,
}
fn bidi(c: char) -> bool {
    matches!(c, '\u{061c}'|'\u{200e}'|'\u{200f}'|'\u{202a}'..='\u{202e}'|'\u{2066}'..='\u{206f}')
}
fn ansi_color(n: u16, bright: bool) -> Color {
    use Color::*;
    match (n, bright) {
        (0, false) => Black,
        (1, false) => Red,
        (2, false) => Green,
        (3, false) => Yellow,
        (4, false) => Blue,
        (5, false) => Magenta,
        (6, false) => Cyan,
        (7, false) => Gray,
        (0, true) => DarkGray,
        (1, true) => LightRed,
        (2, true) => LightGreen,
        (3, true) => LightYellow,
        (4, true) => LightBlue,
        (5, true) => LightMagenta,
        (6, true) => LightCyan,
        _ => White,
    }
}
fn sgr(current: &mut Style, params: &str) {
    if !params.chars().all(|c| c.is_ascii_digit() || c == ';') {
        return;
    }
    let codes: Vec<u16> = if params.is_empty() {
        vec![0]
    } else {
        params
            .split(';')
            .map(|s| {
                if s.is_empty() {
                    Some(0)
                } else {
                    s.parse().ok()
                }
            })
            .collect::<Option<_>>()
            .unwrap_or_default()
    };
    let mut i = 0;
    while i < codes.len() {
        let n = codes[i];
        match n {
            0 => *current = Style::default(),
            1 => *current = current.add_modifier(Modifier::BOLD),
            2 => *current = current.add_modifier(Modifier::DIM),
            3 => *current = current.add_modifier(Modifier::ITALIC),
            4 => *current = current.add_modifier(Modifier::UNDERLINED),
            7 => *current = current.add_modifier(Modifier::REVERSED),
            22 => *current = current.remove_modifier(Modifier::BOLD | Modifier::DIM),
            23 => *current = current.remove_modifier(Modifier::ITALIC),
            24 => *current = current.remove_modifier(Modifier::UNDERLINED),
            27 => *current = current.remove_modifier(Modifier::REVERSED),
            30..=37 => *current = current.fg(ansi_color(n - 30, false)),
            90..=97 => *current = current.fg(ansi_color(n - 90, true)),
            39 => *current = current.fg(Color::Reset),
            40..=47 => *current = current.bg(ansi_color(n - 40, false)),
            100..=107 => *current = current.bg(ansi_color(n - 100, true)),
            49 => *current = current.bg(Color::Reset),
            38 | 48 => {
                let color = if codes.get(i + 1) == Some(&5)
                    && codes.get(i + 2).is_some_and(|v| *v <= 255)
                {
                    i += 2;
                    Some(Color::Indexed(codes[i] as u8))
                } else if codes.get(i + 1) == Some(&2)
                    && codes
                        .get(i + 2..i + 5)
                        .is_some_and(|v| v.iter().all(|n| *n <= 255))
                {
                    let c = Color::Rgb(codes[i + 2] as u8, codes[i + 3] as u8, codes[i + 4] as u8);
                    i += 4;
                    Some(c)
                } else {
                    None
                };
                if let Some(c) = color {
                    *current = if n == 38 {
                        current.fg(c)
                    } else {
                        current.bg(c)
                    };
                }
            }
            _ => {}
        }
        i += 1;
    }
}
fn flush(buf: &mut String, spans: &mut Vec<Span<'static>>, style: Style) {
    if !buf.is_empty() {
        spans.push(Span::styled(std::mem::take(buf), style));
    }
}
/// Safe styled lines; all terminal control strings, non-SGR CSI and bidi overrides disappear.
pub fn sanitize(raw: &str) -> Vec<Line<'static>> {
    let mut lines = Vec::new();
    let mut spans = Vec::new();
    let mut buf = String::new();
    let mut state = State::Text;
    let mut params = String::new();
    let mut style = Style::default();
    for c in raw.chars() {
        match state {
            State::Text => match c {
                '\x1b' => {
                    flush(&mut buf, &mut spans, style);
                    state = State::Escape;
                }
                '\u{009b}' => {
                    flush(&mut buf, &mut spans, style);
                    params.clear();
                    state = State::Csi;
                }
                '\u{0090}' | '\u{0098}' | '\u{009d}' | '\u{009e}' | '\u{009f}' => {
                    flush(&mut buf, &mut spans, style);
                    state = State::String;
                }
                '\n' => {
                    flush(&mut buf, &mut spans, style);
                    lines.push(Line::from(std::mem::take(&mut spans)));
                }
                '\t' => buf.push_str("    "),
                _ if c.is_control() || bidi(c) => {}
                _ => buf.push(c),
            },
            State::Escape => match c {
                '[' => {
                    params.clear();
                    state = State::Csi;
                }
                ']' | 'P' | 'X' | '^' | '_' => state = State::String,
                '\x20'..='\x2f' => state = State::EscapeIntermediate,
                '\x1b' => {}
                _ => state = State::Text,
            },
            State::EscapeIntermediate => {
                if ('\x30'..='\x7e').contains(&c) {
                    state = State::Text;
                }
            }
            State::Csi => {
                if ('\x40'..='\x7e').contains(&c) {
                    if c == 'm' {
                        sgr(&mut style, &params);
                    }
                    state = State::Text;
                } else if c == '\x1b' {
                    state = State::Escape;
                } else if params.len() < 128 {
                    params.push(c);
                } else if !params.ends_with('!') {
                    /* consume to final byte, never interpret a suffix */
                    params.push('!');
                }
            }
            State::String => match c {
                '\x07' | '\u{009c}' => state = State::Text,
                '\x1b' => state = State::StringEscape,
                _ => {}
            },
            State::StringEscape => match c {
                '\\' | '\u{009c}' | '\x07' => state = State::Text,
                '\x1b' => {}
                _ => state = State::String,
            },
        }
    }
    flush(&mut buf, &mut spans, style);
    lines.push(Line::from(spans));
    lines
}
/// Sanitize arbitrary labels/status text as well as pane output.
pub fn plain(raw: &str) -> String {
    sanitize(raw)
        .into_iter()
        .map(|l| {
            l.spans
                .into_iter()
                .map(|s| s.content.into_owned())
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join(" ")
}

#[derive(Debug, Clone)]
pub struct Scroll {
    pub follow: bool,
    pub offset_from_bottom: usize,
}
impl Default for Scroll {
    fn default() -> Self {
        Self {
            follow: true,
            offset_from_bottom: 0,
        }
    }
}
impl Scroll {
    pub fn up(&mut self, n: usize) {
        self.follow = false;
        self.offset_from_bottom = self.offset_from_bottom.saturating_add(n);
    }
    pub fn down(&mut self, n: usize) {
        self.offset_from_bottom = self.offset_from_bottom.saturating_sub(n);
        if self.offset_from_bottom == 0 {
            self.follow = true;
        }
    }
    pub fn follow(&mut self) {
        self.follow = true;
        self.offset_from_bottom = 0;
    }
    pub fn top_line(&self, total: usize, height: usize) -> usize {
        total.saturating_sub(height).saturating_sub(if self.follow {
            0
        } else {
            self.offset_from_bottom
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{backend::TestBackend, widgets::Paragraph, Terminal};
    #[test]
    fn unsafe_sequences_never_reach_cells_and_sgr_survives() {
        let raw="a\x1b]52;c;secret\x07b\x1bPpayload\x1b\\c\u{009d}more\u{009c}d\x1b[2Je\u{202e}f\x1b[31;1mRED\x1b[0m!";
        let lines = sanitize(raw);
        let joined = lines
            .iter()
            .flat_map(|l| &l.spans)
            .map(|s| s.content.as_ref())
            .collect::<String>();
        assert_eq!(joined, "abcdefRED!");
        let red = lines[0]
            .spans
            .iter()
            .find(|s| s.content.contains("RED"))
            .unwrap();
        assert_eq!(red.style.fg, Some(Color::Red));
        assert!(red.style.add_modifier.contains(Modifier::BOLD));
        let mut terminal = Terminal::new(TestBackend::new(40, 2)).unwrap();
        terminal
            .draw(|f| f.render_widget(Paragraph::new(lines), f.area()))
            .unwrap();
        for cell in terminal.backend().buffer().content() {
            assert!(!cell.symbol().chars().any(|c| c.is_control() || bidi(c)));
        }
    }
    #[test]
    fn scrolling_resumes_follow() {
        let mut s = Scroll::default();
        s.up(3);
        assert!(!s.follow);
        assert_eq!(s.top_line(20, 5), 12);
        s.down(3);
        assert!(s.follow);
        assert_eq!(s.top_line(20, 5), 15);
    }
}
