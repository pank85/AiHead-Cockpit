//! Responsive chrome and proportional, non-overlapping authoritative pane geometry.
use crate::protocol::{Layout, LayoutState, PaneRect};
use ratatui::layout::Rect;
use std::{
    cmp::Reverse,
    collections::{BTreeMap, BinaryHeap},
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Chrome {
    pub sidebar: Option<Rect>,
    pub separator: Option<Rect>,
    pub tabs: Rect,
    pub content: Rect,
    pub status: Rect,
    pub actions: Rect,
}
pub fn chrome(area: Rect) -> Chrome {
    let actions_h = u16::from(area.height > 0);
    let status_h = u16::from(area.height > 1);
    let body_h = area.height.saturating_sub(actions_h + status_h);
    let sidebar_width = if area.width >= 70 {
        25
    } else if area.width >= 50 {
        18
    } else {
        0
    };
    let sidebar = (sidebar_width > 0 && body_h > 0).then_some(Rect::new(
        area.x,
        area.y,
        sidebar_width,
        body_h,
    ));
    let separator =
        sidebar.map(|_| Rect::new(area.x.saturating_add(sidebar_width), area.y, 1, body_h));
    let left = if sidebar.is_some() {
        sidebar_width + 1
    } else {
        0
    };
    let main = Rect::new(
        area.x.saturating_add(left),
        area.y,
        area.width.saturating_sub(left),
        body_h,
    );
    let tab_h = u16::from(main.height > 0);
    Chrome {
        sidebar,
        separator,
        tabs: Rect::new(main.x, main.y, main.width, tab_h),
        content: Rect::new(
            main.x,
            main.y.saturating_add(tab_h),
            main.width,
            main.height.saturating_sub(tab_h),
        ),
        status: Rect::new(area.x, area.y.saturating_add(body_h), area.width, status_h),
        actions: Rect::new(
            area.x,
            area.y.saturating_add(body_h + status_h),
            area.width,
            actions_h,
        ),
    }
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MappedPane {
    pub pane_id: String,
    pub area: Rect,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MappedLayout {
    Unavailable,
    Available(Vec<MappedPane>),
}
/// Sweep the x axis. At each x, already-active non-overlapping y intervals
/// can only collide with the immediate predecessor or successor. This keeps
/// hostile but valid 4096-pane layouts O(n log n) on the rendering thread.
fn overlaps_any(rects: &[PaneRect]) -> bool {
    let mut sorted: Vec<_> = rects.iter().enumerate().collect();
    sorted.sort_unstable_by_key(|(i, r)| (r.x, r.y, *i));
    let mut expires = BinaryHeap::<Reverse<(u64, u64, usize)>>::new();
    let mut active = BTreeMap::<(u64, usize), u64>::new();
    for (i, r) in sorted {
        while let Some(&Reverse((end_x, y, index))) = expires.peek() {
            if end_x > r.x {
                break;
            }
            expires.pop();
            active.remove(&(y, index));
        }
        if active
            .range(..=(r.y, usize::MAX))
            .next_back()
            .is_some_and(|(_, &end_y)| end_y > r.y)
            || active
                .range((r.y, 0)..)
                .next()
                .is_some_and(|(&(start_y, _), _)| start_y < r.y + r.height)
        {
            return true;
        }
        active.insert((r.y, i), r.y + r.height);
        expires.push(Reverse((r.x + r.width, r.y, i)));
    }
    false
}
fn scale(v: u64, min: u64, extent: u64, size: u16) -> u16 {
    (((v - min) as u128 * size as u128) / (extent as u128)) as u16
}
/// Reject malformed/overlapping layouts rather than guessing. Shared source boundaries
/// use the same integer transform, so resize never creates overlapping pane rectangles.
pub fn map_panes(layout: &Layout, area: Rect) -> Result<MappedLayout, &'static str> {
    layout.validate().map_err(|_| "invalid layout")?;
    if layout.state == LayoutState::LayoutUnavailable {
        return Ok(MappedLayout::Unavailable);
    }
    let rs = &layout.rects;
    if rs.is_empty() {
        return Ok(MappedLayout::Available(Vec::new()));
    }
    if overlaps_any(rs) {
        return Err("overlapping pane rectangles");
    }
    let min_x = rs.iter().map(|r| r.x).min().unwrap();
    let min_y = rs.iter().map(|r| r.y).min().unwrap();
    let max_x = rs.iter().map(|r| r.x + r.width).max().unwrap();
    let max_y = rs.iter().map(|r| r.y + r.height).max().unwrap();
    let mut out = Vec::with_capacity(rs.len());
    for r in rs {
        let x = scale(r.x, min_x, max_x - min_x, area.width);
        let right = scale(r.x + r.width, min_x, max_x - min_x, area.width);
        let y = scale(r.y, min_y, max_y - min_y, area.height);
        let bottom = scale(r.y + r.height, min_y, max_y - min_y, area.height);
        if right > x && bottom > y {
            out.push(MappedPane {
                pane_id: r.pane_id.clone(),
                area: Rect::new(
                    area.x.saturating_add(x),
                    area.y.saturating_add(y),
                    right - x,
                    bottom - y,
                ),
            });
        }
    }
    Ok(MappedLayout::Available(out))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn p(id: &str, x: u64, y: u64, w: u64, h: u64) -> PaneRect {
        PaneRect {
            pane_id: id.into(),
            x,
            y,
            width: w,
            height: h,
        }
    }
    #[test]
    fn sidebar_and_resizes() {
        for (w, h) in [(140, 42), (120, 40), (80, 24), (40, 24)] {
            let c = chrome(Rect::new(0, 0, w, h));
            assert_eq!(c.actions.y, h - 1);
            assert!(c.tabs.right() <= w);
            if w >= 70 {
                assert_eq!(c.sidebar.unwrap().width, 25);
                assert_eq!(c.content.x, 26)
            } else {
                assert!(c.sidebar.is_none())
            }
        }
    }
    #[test]
    fn sweep_matches_exhaustive_overlap_check() {
        let mut state = 7u64;
        let mut next = || {
            state = state.wrapping_mul(6364136223846793005).wrapping_add(1);
            (state >> 32) % 20
        };
        for _ in 0..300 {
            let rects: Vec<_> = (0..12)
                .map(|i| {
                    p(
                        &i.to_string(),
                        next(),
                        next(),
                        next() % 5 + 1,
                        next() % 5 + 1,
                    )
                })
                .collect();
            let brute = rects.iter().enumerate().any(|(i, a)| {
                rects.iter().skip(i + 1).any(|b| {
                    a.x < b.x + b.width
                        && b.x < a.x + a.width
                        && a.y < b.y + b.height
                        && b.y < a.y + a.height
                })
            });
            assert_eq!(overlaps_any(&rects), brute);
        }
        let large: Vec<_> = (0..4096).map(|i| p(&i.to_string(), 0, i, 10, 1)).collect();
        assert!(!overlaps_any(&large));
    }
    #[test]
    fn mapping_nested_horizontal_vertical_and_unavailable() {
        let l = Layout {
            state: LayoutState::Available,
            rects: vec![
                p("a", 0, 0, 50, 100),
                p("b", 50, 0, 50, 50),
                p("c", 50, 50, 50, 50),
            ],
            focused_pane_id: Some("b".into()),
        };
        for (w, h) in [(100, 30), (17, 9), (2, 2)] {
            let MappedLayout::Available(v) = map_panes(&l, Rect::new(2, 3, w, h)).unwrap() else {
                panic!()
            };
            for (i, a) in v.iter().enumerate() {
                for b in v.iter().skip(i + 1) {
                    assert!(a.area.intersection(b.area).is_empty());
                }
            }
        }
        let mut bad = l.clone();
        bad.rects[1].x = 49;
        assert!(map_panes(&bad, Rect::new(0, 0, 80, 24)).is_err());
        let unavailable = Layout {
            state: LayoutState::LayoutUnavailable,
            rects: vec![],
            focused_pane_id: None,
        };
        assert_eq!(
            map_panes(&unavailable, Rect::new(0, 0, 20, 10)).unwrap(),
            MappedLayout::Unavailable
        );
        let zoom = Layout {
            state: LayoutState::Available,
            rects: vec![p("b", 4, 2, 10, 9)],
            focused_pane_id: Some("b".into()),
        };
        let MappedLayout::Available(v) = map_panes(&zoom, Rect::new(1, 1, 20, 10)).unwrap() else {
            panic!()
        };
        assert_eq!(v[0].area, Rect::new(1, 1, 20, 10));
    }
}
