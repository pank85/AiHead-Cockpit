//! Responsive, bounded panel rendering and hit regions.
use super::forms::{self, Editor, Kind, Row};
use aihead_cockpit::output;
use ratatui::{
    layout::Rect,
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, Paragraph, Wrap},
    Frame,
};
use serde_json::Value;

#[derive(Clone, Copy, Debug, Default)]
pub struct Hits {
    pub rows: Rect,
    pub footer: Rect,
}
fn safe(s: &str) -> String {
    output::plain(s)
}
pub struct FormView<'a> {
    pub title: &'a str,
    pub sub: &'a str,
    pub rows: &'a [Row],
    pub root: &'a Value,
    pub selected: usize,
    pub edit: Option<&'a Editor>,
    pub status: &'a str,
    pub action: &'a str,
}
pub fn form(frame: &mut Frame<'_>, area: Rect, view: FormView<'_>, scroll: &mut usize) -> Hits {
    frame.render_widget(Clear, area);
    frame.render_widget(
        Block::default()
            .borders(Borders::ALL)
            .title(safe(view.title))
            .border_style(Style::default().fg(Color::Cyan)),
        area,
    );
    let inner = area.inner(ratatui::layout::Margin {
        horizontal: 1,
        vertical: 1,
    });
    if inner.width == 0 || inner.height == 0 {
        return Hits::default();
    }
    let header = Rect::new(inner.x, inner.y, inner.width, 1);
    frame.render_widget(Paragraph::new(safe(view.sub)), header);
    let footer_h = inner.height.saturating_sub(1).min(4);
    let row_h = inner.height.saturating_sub(1 + footer_h);
    let row_area = Rect::new(inner.x, inner.y + 1, inner.width, row_h);
    // Two lines per field keep both field labels and values accessible at 40 columns.
    let capacity = (row_h as usize / 2).max(1);
    if row_h > 0 {
        if view.selected < *scroll {
            *scroll = view.selected;
        }
        if view.selected >= scroll.saturating_add(capacity) {
            *scroll = view.selected + 1 - capacity;
        }
        *scroll = (*scroll).min(view.rows.len().saturating_sub(1));
        let mut lines = Vec::new();
        for (idx, row) in view.rows.iter().enumerate().skip(*scroll).take(capacity) {
            let value = if let Some(e) = view.edit.filter(|e| e.path == row.path) {
                let left = e.text[..e.cursor].replace('\n', "↵");
                let right = e.text[e.cursor..].replace('\n', "↵");
                let budget = inner.width.saturating_sub(7) as usize;
                let keep = left
                    .chars()
                    .rev()
                    .take(budget.saturating_sub(2))
                    .collect::<String>()
                    .chars()
                    .rev()
                    .collect::<String>();
                format!("{keep}│{right}")
            } else if matches!(row.kind, Kind::Array) {
                "F3 add • F6 remove • F7/F8 move".into()
            } else {
                forms::get(view.root, &row.path)
                    .map(forms::display)
                    .unwrap_or_default()
            };
            let style = if idx == view.selected {
                Style::default()
                    .fg(Color::Black)
                    .bg(Color::Cyan)
                    .add_modifier(Modifier::BOLD)
            } else {
                Style::default()
            };
            let label = safe(&row.label);
            let width = inner.width.saturating_sub(3) as usize;
            let label = if label.chars().count() > width && width > 1 {
                format!(
                    "…{}",
                    label
                        .chars()
                        .rev()
                        .take(width - 1)
                        .collect::<String>()
                        .chars()
                        .rev()
                        .collect::<String>()
                )
            } else {
                label
            };
            lines.push(Line::from(Span::styled(
                format!("{} {}", if idx == view.selected { ">" } else { " " }, label),
                style,
            )));
            lines.push(Line::from(Span::styled(
                format!("  {}", safe(&value)),
                style,
            )));
        }
        frame.render_widget(Paragraph::new(lines), row_area);
    }
    let footer = Rect::new(inner.x, inner.y + 1 + row_h, inner.width, footer_h);
    if footer_h > 0 {
        let left = format!("F9 {}", view.action);
        let right = "Esc Cancel";
        let padding = (inner.width as usize).saturating_sub(left.len() + right.len());
        let lines = vec![
            Line::from(safe(view.status)),
            Line::from("↑↓ select · Enter edit · Tab next"),
            Line::from("F3+ F4 folder F5 load F6− F7/8 move"),
            Line::from(format!("{left}{}{right}", " ".repeat(padding))),
        ];
        // Do not wrap status and hide the only visible action row on narrow terminals.
        frame.render_widget(Paragraph::new(lines), footer);
    }
    Hits {
        rows: row_area,
        footer,
    }
}
pub fn notice(
    frame: &mut Frame<'_>,
    area: Rect,
    title: &str,
    text: &str,
    footer: &str,
    offset: u16,
) -> Hits {
    frame.render_widget(Clear, area);
    frame.render_widget(
        Block::default()
            .borders(Borders::ALL)
            .title(safe(title))
            .border_style(Style::default().fg(Color::Cyan)),
        area,
    );
    let inner = area.inner(ratatui::layout::Margin {
        horizontal: 1,
        vertical: 1,
    });
    let rows = Rect::new(
        inner.x,
        inner.y,
        inner.width,
        inner.height.saturating_sub(1),
    );
    if inner.height > 0 {
        frame.render_widget(
            Paragraph::new(safe(text))
                .wrap(Wrap { trim: false })
                .scroll((offset, 0)),
            rows,
        );
    }
    let foot = Rect::new(
        inner.x,
        inner.y + inner.height.saturating_sub(1),
        inner.width,
        u16::from(inner.height > 0),
    );
    frame.render_widget(Paragraph::new(safe(footer)), foot);
    Hits { rows, footer: foot }
}
pub fn folder(frame: &mut Frame<'_>, area: Rect, folder: &super::folders::FolderBrowser) -> Hits {
    frame.render_widget(Clear, area);
    frame.render_widget(
        Block::default()
            .borders(Borders::ALL)
            .title("Choose folder")
            .border_style(Style::default().fg(Color::Cyan)),
        area,
    );
    let inner = area.inner(ratatui::layout::Margin {
        horizontal: 1,
        vertical: 1,
    });
    if inner.width == 0 || inner.height == 0 {
        return Hits::default();
    }
    frame.render_widget(
        Paragraph::new(safe(&format!(
            "Path: {}{}",
            folder.path.text,
            if folder.editing { " │" } else { "" }
        ))),
        Rect::new(inner.x, inner.y, inner.width, 1),
    );
    let rows = Rect::new(
        inner.x,
        inner.y + 1,
        inner.width,
        inner.height.saturating_sub(3),
    );
    let start = folder
        .selected
        .saturating_sub((rows.height as usize).saturating_sub(1));
    let lines: Vec<_> = folder
        .directories
        .iter()
        .enumerate()
        .skip(start)
        .take(rows.height as usize)
        .map(|(i, (name, _))| {
            Line::from(safe(&format!(
                "{} {}",
                if i == folder.selected { ">" } else { " " },
                name
            )))
        })
        .collect();
    frame.render_widget(Paragraph::new(lines), rows);
    if inner.height >= 3 {
        frame.render_widget(
            Paragraph::new(safe(&folder.status)),
            Rect::new(inner.x, inner.y + inner.height - 2, inner.width, 1),
        );
    }
    let footer = Rect::new(inner.x, inner.y + inner.height - 1, inner.width, 1);
    let left = "F9 choose";
    let right = "Esc cancel";
    let padding = (inner.width as usize).saturating_sub(left.len() + right.len());
    frame.render_widget(
        Paragraph::new(format!("{left}{}{right}", " ".repeat(padding))),
        footer,
    );
    Hits { rows, footer }
}
pub fn contains(r: Rect, x: u16, y: u16) -> bool {
    x >= r.x && y >= r.y && x < r.x.saturating_add(r.width) && y < r.y.saturating_add(r.height)
}
