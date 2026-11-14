//! Project-scoped folder browsing. The bridge alone expands and tests paths.
use super::forms::{Editor, Path};
use aihead_cockpit::protocol::{Method, Request, RequestId};
use serde_json::{json, Value};

#[derive(Debug)]
pub struct FolderBrowser {
    pub destination: Path,
    pub path: Editor,
    pub directories: Vec<(String, String)>,
    pub selected: usize,
    pub editing: bool,
    pub status: String,
    pub verified_path: Option<String>,
    favorites: Vec<(String, String)>,
    favorite_index: Option<usize>,
}
impl FolderBrowser {
    pub fn new(destination: Path, initial: &str) -> Self {
        Self {
            destination,
            path: Editor {
                path: vec![],
                text: initial.into(),
                cursor: initial.len(),
                multiline: false,
                nullable: false,
                numeric: false,
            },
            directories: vec![],
            selected: 0,
            editing: false,
            status: "F8 favorites · Tab path · Enter opens".into(),
            verified_path: None,
            favorites: Vec::new(),
            favorite_index: None,
        }
    }
    pub fn request(&self, id: RequestId, project: Option<&str>) -> Result<Request, String> {
        let mut params = json!({"path":self.path.text}).as_object().unwrap().clone();
        if let Some(p) = project {
            params.insert("project".into(), json!(p));
        }
        Request::new(id, Method::FoldersList, params).map_err(|e| e.to_string())
    }
    pub fn apply(&mut self, result: &Value) -> Result<(), String> {
        if !result
            .get("available")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            return Err("Folder is not available".into());
        }
        let path = result
            .get("path")
            .and_then(Value::as_str)
            .ok_or("Folder response missing path")?;
        let entries = result
            .get("directories")
            .and_then(Value::as_array)
            .ok_or("Folder response missing directories")?;
        let mut dirs = Vec::new();
        for d in entries {
            if let (Some(name), Some(path)) = (
                d.get("name").and_then(Value::as_str),
                d.get("path").and_then(Value::as_str),
            ) {
                dirs.push((name.to_owned(), path.to_owned()));
            }
        }
        self.path.text = path.into();
        self.path.cursor = self.path.text.len();
        self.verified_path = Some(path.into());
        self.directories = dirs;
        self.selected = 0;
        self.status = "F8 favorites · Tab path · Enter opens".into();
        Ok(())
    }
    pub fn set_favorites(&mut self, entries: &Value) {
        self.favorites = entries
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|v| {
                Some((
                    v.get("name")?.as_str()?.to_owned(),
                    v.get("path")?.as_str()?.to_owned(),
                ))
            })
            .filter(|(_, p)| !p.is_empty())
            .collect();
        self.favorite_index = None;
    }
    pub fn next_favorite(&mut self) -> bool {
        if self.favorites.is_empty() {
            self.status = "No favorites in this project's settings".into();
            return false;
        }
        let i = self
            .favorite_index
            .map_or(0, |i| (i + 1) % self.favorites.len());
        self.favorite_index = Some(i);
        self.path.text = self.favorites[i].1.clone();
        self.path.cursor = self.path.text.len();
        self.status = format!("Loading favorite: {}", self.favorites[i].0);
        true
    }
    pub fn descend(&mut self) -> bool {
        let Some((_, path)) = self.directories.get(self.selected) else {
            return false;
        };
        self.path.text = path.clone();
        self.path.cursor = self.path.text.len();
        true
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn folder_selection_is_explicit_and_scoped() {
        let mut b = FolderBrowser::new(vec![], ".");
        let r = b.request(RequestId::Integer(1), Some("/project")).unwrap();
        assert_eq!(r.params["project"], "/project");
        b.apply(&json!({"path":"/project","available":true,"directories":[{"name":"子","path":"/project/子"}]})).unwrap();
        assert!(b.descend());
        assert_eq!(b.path.text, "/project/子");
    }
}
