//! Small keyboard-first JSON-backed form primitives. Values remain in the server DTO;
//! edits never reconstruct a dependency model or discard unrecognised row metadata.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use serde_json::{json, Value};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Part {
    Key(String),
    Index(usize),
}
pub type Path = Vec<Part>;

pub fn key(s: &str) -> Part {
    Part::Key(s.to_owned())
}
pub fn get<'a>(root: &'a Value, path: &[Part]) -> Option<&'a Value> {
    path.iter().try_fold(root, |v, p| match p {
        Part::Key(k) => v.get(k),
        Part::Index(i) => v.get(*i),
    })
}
pub fn get_mut<'a>(root: &'a mut Value, path: &[Part]) -> Option<&'a mut Value> {
    path.iter().try_fold(root, |v, p| match p {
        Part::Key(k) => v.get_mut(k),
        Part::Index(i) => v.get_mut(*i),
    })
}
pub fn path_name(path: &[Part]) -> String {
    let skip = match path.first() {
        Some(Part::Key(k)) if k == "sections" => 2,
        Some(Part::Key(k)) if k == "form" => 1,
        _ => 0,
    };
    let tail = if skip < path.len() {
        &path[skip..]
    } else {
        &path[path.len().saturating_sub(1)..]
    };
    tail.iter()
        .map(|p| match p {
            Part::Key(s) => s.clone(),
            Part::Index(i) => (i + 1).to_string(),
        })
        .collect::<Vec<_>>()
        .join(" / ")
}
#[derive(Clone, Debug, PartialEq)]
pub enum Kind {
    Text {
        multiline: bool,
        nullable: bool,
        numeric: bool,
    },
    Bool,
    Choice(Vec<Value>),
    Array,
}
#[derive(Clone, Debug)]
pub struct Row {
    pub path: Path,
    pub label: String,
    pub kind: Kind,
}

fn allowed_efforts(v: &Value, out: &mut Vec<Value>) {
    match v {
        Value::Object(map) => {
            for (k, x) in map {
                if k == "allowed_efforts" || k == "efforts" {
                    if let Some(a) = x.as_array() {
                        for item in a {
                            if item.is_string() && !out.contains(item) {
                                out.push(item.clone());
                            }
                        }
                    }
                } else {
                    allowed_efforts(x, out);
                }
            }
        }
        Value::Array(a) => {
            for x in a {
                allowed_efforts(x, out);
            }
        }
        _ => {}
    }
}
fn choices(path: &[Part], catalog: &Value) -> Option<Vec<Value>> {
    let name = match path.last()? {
        Part::Key(k) => k.as_str(),
        Part::Index(_) => return None,
    };
    if name == "ai_head" && path.iter().any(|p| *p == key("form")) {
        return Some(vec![Value::Null, json!(true), json!(false)]);
    }
    if name == "kind" || name == "harness" {
        let native = path.iter().any(|p| *p == key("orchestration"))
            || path.iter().any(|p| *p == key("native_harness"));
        let list = if native {
            catalog.get("native_harnesses")
        } else {
            catalog.get("harnesses")
        }?
        .as_array()?;
        let result: Vec<_> = list
            .iter()
            .filter_map(|v| v.as_str().or_else(|| v.get("kind").and_then(Value::as_str)))
            .map(|s| json!(s))
            .collect();
        return (!result.is_empty()).then_some(result);
    }
    if name == "effort" || name.ends_with("_effort") {
        let mut result = vec![Value::Null];
        allowed_efforts(catalog, &mut result);
        if result.len() > 1 {
            return Some(result);
        }
    }
    None
}
/// Flatten a selected DTO subtree into an entirely scrollable list.
pub fn rows(root: &Value, prefix: &[Part], catalog: &Value) -> Vec<Row> {
    fn walk(v: &Value, path: &mut Path, out: &mut Vec<Row>, catalog: &Value) {
        match v {
            Value::Object(map) => {
                for (k, child) in map {
                    path.push(key(k));
                    walk(child, path, out, catalog);
                    path.pop();
                }
            }
            Value::Array(values) => {
                out.push(Row {
                    path: path.clone(),
                    label: format!("{} [{}]  + / − / move", path_name(path), values.len()),
                    kind: Kind::Array,
                });
                for (i, child) in values.iter().enumerate() {
                    path.push(Part::Index(i));
                    walk(child, path, out, catalog);
                    path.pop();
                }
            }
            Value::Bool(_) => out.push(Row {
                path: path.clone(),
                label: path_name(path),
                kind: choices(path, catalog).map_or(Kind::Bool, Kind::Choice),
            }),
            Value::String(_) | Value::Null => {
                let label = path_name(path);
                let multiline = label.ends_with("soul")
                    || label.ends_with("intent")
                    || label.ends_with("idea")
                    || label.ends_with("task")
                    || label.ends_with("description");
                let kind = choices(path, catalog).map_or(
                    Kind::Text {
                        multiline,
                        nullable: v.is_null()
                            || label.ends_with("effort")
                            || label.ends_with("cwd")
                            || label.ends_with("default_folder"),
                        numeric: false,
                    },
                    Kind::Choice,
                );
                out.push(Row {
                    path: path.clone(),
                    label,
                    kind,
                });
            }
            Value::Number(_) => out.push(Row {
                path: path.clone(),
                label: path_name(path),
                kind: Kind::Text {
                    multiline: false,
                    nullable: false,
                    numeric: true,
                },
            }),
        }
    }
    let mut out = Vec::new();
    if let Some(v) = get(root, prefix) {
        walk(v, &mut prefix.to_vec(), &mut out, catalog);
    }
    out
}
pub fn display(v: &Value) -> String {
    match v {
        Value::Null => "(inherit / unset)".into(),
        Value::Bool(b) => if *b { "on" } else { "off" }.into(),
        Value::String(s) => s.replace('\n', " ↵ "),
        _ => v.to_string(),
    }
}
pub fn cycle(root: &mut Value, row: &Row, backwards: bool) {
    if let Some(v) = get_mut(root, &row.path) {
        match &row.kind {
            Kind::Bool => {
                if let Some(b) = v.as_bool() {
                    *v = json!(!b);
                }
            }
            Kind::Choice(options) if !options.is_empty() => {
                let pos = options.iter().position(|x| x == v).unwrap_or(if backwards {
                    0
                } else {
                    options.len() - 1
                });
                let next = if backwards {
                    (pos + options.len() - 1) % options.len()
                } else {
                    (pos + 1) % options.len()
                };
                *v = options[next].clone();
            }
            _ => {}
        }
    }
}
#[derive(Clone, Debug)]
pub struct Editor {
    pub path: Path,
    pub text: String,
    pub cursor: usize,
    pub multiline: bool,
    pub nullable: bool,
    pub numeric: bool,
}
impl Editor {
    pub fn new(row: &Row, root: &Value) -> Option<Self> {
        let Kind::Text {
            multiline,
            nullable,
            numeric,
        } = row.kind
        else {
            return None;
        };
        let value = get(root, &row.path)?;
        let text = if numeric {
            value.to_string()
        } else {
            value.as_str().unwrap_or("").to_owned()
        };
        let cursor = text.len();
        Some(Self {
            path: row.path.clone(),
            text,
            cursor,
            multiline,
            nullable,
            numeric,
        })
    }
    pub fn insert(&mut self, text: &str) {
        let filtered: String = text
            .chars()
            .filter(|c| !c.is_control() || (*c == '\n' && self.multiline) || *c == '\t')
            .collect();
        if self.text.len().saturating_add(filtered.len())
            <= aihead_cockpit::protocol::MAX_TEXT_BYTES
        {
            self.text.insert_str(self.cursor, &filtered);
            self.cursor += filtered.len();
        }
    }
    pub fn key(&mut self, k: KeyEvent) {
        match k.code {
            KeyCode::Left => {
                if self.cursor > 0 {
                    self.cursor = self.text[..self.cursor]
                        .char_indices()
                        .next_back()
                        .map_or(0, |(i, _)| i);
                }
            }
            KeyCode::Right => {
                if let Some(c) = self.text[self.cursor..].chars().next() {
                    self.cursor += c.len_utf8();
                }
            }
            KeyCode::Up | KeyCode::Down if self.multiline => {
                let start = self.text[..self.cursor].rfind('\n').map_or(0, |i| i + 1);
                let column = self.text[start..self.cursor].chars().count();
                let target = if k.code == KeyCode::Up && start > 0 {
                    let end = start - 1;
                    Some((self.text[..end].rfind('\n').map_or(0, |i| i + 1), end))
                } else if k.code == KeyCode::Down {
                    self.text[self.cursor..].find('\n').map(|n| {
                        let next = self.cursor + n + 1;
                        let end = next
                            + self.text[next..]
                                .find('\n')
                                .unwrap_or(self.text.len() - next);
                        (next, end)
                    })
                } else {
                    None
                };
                if let Some((line_start, line_end)) = target {
                    self.cursor = line_start
                        + self.text[line_start..line_end]
                            .char_indices()
                            .nth(column)
                            .map_or(line_end - line_start, |(i, _)| i);
                }
            }
            KeyCode::Home => {
                self.cursor = self.text[..self.cursor].rfind('\n').map_or(0, |i| i + 1)
            }
            KeyCode::End => {
                self.cursor += self.text[self.cursor..]
                    .find('\n')
                    .unwrap_or(self.text.len() - self.cursor)
            }
            KeyCode::Backspace if self.cursor > 0 => {
                let start = self.text[..self.cursor]
                    .char_indices()
                    .next_back()
                    .map_or(0, |(i, _)| i);
                self.text.drain(start..self.cursor);
                self.cursor = start;
            }
            KeyCode::Delete if self.cursor < self.text.len() => {
                let end = self.cursor
                    + self.text[self.cursor..]
                        .chars()
                        .next()
                        .map_or(0, char::len_utf8);
                self.text.drain(self.cursor..end);
            }
            KeyCode::Enter if self.multiline && !k.modifiers.contains(KeyModifiers::CONTROL) => {
                self.insert("\n")
            }
            KeyCode::Char(c)
                if !k
                    .modifiers
                    .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
            {
                self.insert(&c.to_string())
            }
            _ => {}
        }
    }
    pub fn commit(self, root: &mut Value) -> bool {
        let value = if self.numeric {
            match serde_json::from_str::<Value>(&self.text) {
                Ok(v @ Value::Number(_)) => v,
                _ => return false,
            }
        } else if self.nullable && self.text.is_empty() {
            Value::Null
        } else {
            json!(self.text)
        };
        if let Some(v) = get_mut(root, &self.path) {
            *v = value;
            true
        } else {
            false
        }
    }
}
/// Locate the closest containing array and (where applicable) the selected row index.
pub fn array_location(root: &Value, path: &[Part]) -> Option<(Path, Option<usize>)> {
    for n in (0..=path.len()).rev() {
        if get(root, &path[..n]).is_some_and(Value::is_array) {
            let index = match path.get(n) {
                Some(Part::Index(i)) => Some(*i),
                _ => None,
            };
            return Some((path[..n].to_vec(), index));
        }
    }
    None
}
pub fn edit_array(root: &mut Value, path: &[Part], operation: i8) -> bool {
    let Some((array, selected)) = array_location(root, path) else {
        return false;
    };
    // Empty favorites and model lists have explicit blank rows. For empty bindings,
    // use the server-provided role as a complete template, not a model catalog.
    let template = if array.last() == Some(&key("favorites")) {
        Some(json!({"name":"", "path":""}))
    } else if array.last() == Some(&key("worker_models")) {
        Some(json!(""))
    } else if array.last() == Some(&key("worker_pool")) {
        get(root, &[key("sections"), key("squad"), key("scout")]).cloned()
    } else if array.last() == Some(&key("members")) {
        get(root, &[key("sections"), key("council"), key("scout")]).cloned()
    } else if array.last() == Some(&key("lesson_paths")) {
        Some(json!(""))
    } else {
        None
    };
    let Some(values) = get_mut(root, &array).and_then(Value::as_array_mut) else {
        return false;
    };
    match operation {
        1 => {
            let Some(mut item) = selected
                .and_then(|i| values.get(i))
                .or_else(|| values.first())
                .cloned()
                .or(template)
            else {
                return false;
            };
            if let Some(obj) = item.as_object_mut() {
                for k in ["model", "label", "name", "path", "description"] {
                    if obj.contains_key(k) {
                        obj.insert(k.into(), json!(""));
                    }
                }
                if obj.contains_key("enabled") {
                    obj.insert("enabled".into(), json!(true));
                }
                if array.last() == Some(&key("worker_pool")) && obj.contains_key("effort") {
                    obj.insert("effort".into(), Value::Null);
                }
            } else if item.is_string() {
                item = json!("");
            }
            values.push(item);
        }
        -1 => {
            let Some(i) = selected.filter(|i| *i < values.len()) else {
                return false;
            };
            values.remove(i);
        }
        -2 => {
            let Some(i) = selected.filter(|i| *i > 0 && *i < values.len()) else {
                return false;
            };
            values.swap(i, i - 1);
        }
        2 => {
            let Some(i) = selected.filter(|i| i + 1 < values.len()) else {
                return false;
            };
            values.swap(i, i + 1);
        }
        _ => return false,
    }
    for (i, v) in values.iter_mut().enumerate() {
        if let Some(obj) = v.as_object_mut() {
            if obj.contains_key("order") {
                obj.insert("order".into(), json!(i));
            }
        }
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unicode_cursor_edit_and_paste() {
        let mut root = json!({"form":{"intent":"hé界"}});
        let row = rows(&root, &[key("form")], &json!({})).remove(0);
        let mut edit = Editor::new(&row, &root).unwrap();
        edit.key(KeyEvent::new(KeyCode::Left, KeyModifiers::NONE));
        edit.key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE));
        edit.insert("🙂\nline\u{1b}");
        edit.commit(&mut root);
        assert_eq!(root["form"]["intent"], "h🙂\nline界");
    }
    #[test]
    fn row_order_and_metadata_survive() {
        let mut root = json!({"sections":{"squad":{"worker_pool":[{"model":"one","order":0,"extra":"x"},{"model":"two","order":1,"extra":"y"}]}}});
        let p = vec![
            key("sections"),
            key("squad"),
            key("worker_pool"),
            Part::Index(1),
            key("model"),
        ];
        assert!(edit_array(&mut root, &p, -2));
        assert_eq!(root["sections"]["squad"]["worker_pool"][0]["extra"], "y");
        assert_eq!(root["sections"]["squad"]["worker_pool"][0]["order"], 0);
    }
    #[test]
    fn choices_come_from_catalog() {
        let root = json!({"form":{"harness":"one","ai_head":null}});
        let cat = json!({"harnesses":[{"kind":"one"},{"kind":"new"}]});
        let r = rows(&root, &[key("form")], &cat);
        assert!(r
            .iter()
            .any(|r| matches!(&r.kind,Kind::Choice(v) if v.contains(&json!("new")))));
    }
    #[test]
    fn numeric_validation_and_multiline_vertical_cursor() {
        let mut root = json!({"form":{"count":3,"intent":"é界\n🙂x"}});
        let all = rows(&root, &[key("form")], &json!({}));
        let count = all.iter().find(|r| r.label == "count").unwrap();
        let mut e = Editor::new(count, &root).unwrap();
        e.text = "not-a-number".into();
        assert!(!e.commit(&mut root));
        assert_eq!(root["form"]["count"], 3);
        let mut e = Editor::new(count, &root).unwrap();
        e.text = "12".into();
        assert!(e.commit(&mut root));
        assert_eq!(root["form"]["count"], 12);
        let intent = all.iter().find(|r| r.label == "intent").unwrap();
        let mut e = Editor::new(intent, &root).unwrap();
        e.key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE));
        e.insert("Z");
        assert!(e.commit(&mut root));
        assert_eq!(root["form"]["intent"], "é界Z\n🙂x");
    }
    #[test]
    fn list_add_remove_and_order_keep_foreign_metadata() {
        let mut root = json!({"sections":{"squad":{"scout":{"kind":"x","model":"scout"},"worker_pool":[{"kind":"x","model":"a","order":0,"enabled":false,"opaque":42}]}}});
        let list = vec![key("sections"), key("squad"), key("worker_pool")];
        assert!(edit_array(&mut root, &list, 1));
        assert_eq!(root["sections"]["squad"]["worker_pool"][1]["opaque"], 42);
        assert_eq!(root["sections"]["squad"]["worker_pool"][1]["enabled"], true);
        let mut child = list.clone();
        child.push(Part::Index(1));
        child.push(key("model"));
        assert!(edit_array(&mut root, &child, -2));
        assert_eq!(root["sections"]["squad"]["worker_pool"][0]["order"], 0);
        assert!(edit_array(&mut root, &child, -1));
        assert_eq!(
            root["sections"]["squad"]["worker_pool"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
    }
}
