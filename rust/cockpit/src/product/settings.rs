//! Revisioned settings drafts; all serialization remains dependency-owned.
use aihead_cockpit::protocol::{BridgeError, Method, Outcome, Request, RequestId};
use serde_json::{json, Value};

#[derive(Clone, Debug)]
pub struct Draft {
    pub token: String,
    pub revision: u64,
    pub project: String,
    pub destination: String,
    pub scope: String,
    pub catalog: Value,
    pub previews: Value,
    pub errors: Value,
    pub uncertain: bool,
}
impl Draft {
    pub fn open(value: &Value) -> Result<(Self, Value), String> {
        let string = |k: &str| {
            value
                .get(k)
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
                .ok_or_else(|| format!("Project response missing {k}"))
        };
        let revision = value
            .get("revision")
            .and_then(Value::as_u64)
            .ok_or("Project response missing revision")?;
        let sections = value
            .get("sections")
            .filter(|v| v.is_object())
            .ok_or("Project response missing sections")?
            .clone();
        for name in ["projects", "squad", "orchestration", "council", "ai_head"] {
            if !sections.get(name).is_some_and(Value::is_object) {
                return Err(format!("Project response missing {name}"));
            }
        }
        Ok((
            Self {
                token: string("token")?,
                revision,
                project: string("project_path")?,
                destination: string("destination")?,
                scope: string("scope")?,
                catalog: value.get("catalog").cloned().unwrap_or_else(|| json!({})),
                previews: json!({}),
                errors: json!({}),
                uncertain: false,
            },
            sections,
        ))
    }
    pub fn request(
        &self,
        id: RequestId,
        method: Method,
        sections: Option<Value>,
    ) -> Result<Request, String> {
        if self.uncertain && method == Method::SettingsSave {
            return Err(
                "Save outcome is uncertain. Inspect the destination and reload before saving."
                    .into(),
            );
        }
        let params = if let Some(sections) = sections {
            json!({"token":self.token,"revision":self.revision,"sections":sections})
        } else {
            json!({"token":self.token,"revision":self.revision,"discard":true})
        };
        Request::new(id, method, params.as_object().unwrap().clone()).map_err(|e| e.to_string())
    }
    pub fn apply(&mut self, root: &mut Value, result: &Value) -> bool {
        if let Some(r) = result.get("revision").and_then(Value::as_u64) {
            self.revision = r;
        }
        if let Some(v) = result.get("sections").filter(|v| v.is_object()) {
            root["sections"] = v.clone();
        }
        self.errors = result
            .get("field_errors")
            .cloned()
            .unwrap_or_else(|| json!({}));
        self.previews = result
            .get("route_previews")
            .cloned()
            .unwrap_or_else(|| json!({}));
        result
            .get("valid")
            .and_then(Value::as_bool)
            .unwrap_or(false)
    }
    pub fn fail(&mut self, error: &BridgeError) -> String {
        if let Some(rev) = error
            .field_errors
            .as_ref()
            .and_then(|e| e.get("revision"))
            .and_then(|s| s.parse().ok())
        {
            self.revision = rev;
        }
        self.uncertain |= error.outcome == Some(Outcome::Uncertain)
            || error.code == "concurrent_modification"
            || error.code == "stale_revision";
        if let Some(fields) = &error.field_errors {
            self.errors = json!(fields);
        }
        format!(
            "{}{}",
            error.message,
            if self.uncertain {
                " — inspect destination/reload; do not retry automatically"
            } else {
                ""
            }
        )
    }
}
pub fn feedback(value: &Value) -> String {
    value
        .as_object()
        .map(|m| {
            m.iter()
                .map(|(k, v)| {
                    format!(
                        "{k}: {}",
                        v.as_str().map_or_else(|| v.to_string(), str::to_owned)
                    )
                })
                .collect::<Vec<_>>()
                .join("; ")
        })
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> Value {
        json!({"token":"a", "revision":0,"project_path":"/p","destination":"/parent/.herdr-master.toml","scope":"inherited","catalog":{},"sections":{"projects":{},"squad":{"secret":"keep"},"orchestration":{},"council":{},"ai_head":{}}})
    }
    #[test]
    fn inherited_destination_and_unknown_values_preserved() {
        let (d, sections) = Draft::open(&fixture()).unwrap();
        let request = d
            .request(
                RequestId::Integer(2),
                Method::SettingsSave,
                Some(sections.clone()),
            )
            .unwrap();
        assert_eq!(d.scope, "inherited");
        assert_eq!(d.destination, "/parent/.herdr-master.toml");
        assert_eq!(request.params["sections"]["squad"]["secret"], "keep");
    }
    #[test]
    fn cancel_is_discard_never_save() {
        let (d, _) = Draft::open(&fixture()).unwrap();
        let req = d
            .request(RequestId::Integer(1), Method::SettingsValidate, None)
            .unwrap();
        assert_eq!(req.params["discard"], true);
        assert!(!req.params.contains_key("sections"));
    }
    #[test]
    fn uncertain_save_cannot_replay() {
        let (mut d, _) = Draft::open(&fixture()).unwrap();
        let error = BridgeError {
            code: "save_failed".into(),
            message: "disk".into(),
            field_errors: Some([("revision".into(), "2".into())].into()),
            outcome: Some(Outcome::Uncertain),
        };
        d.fail(&error);
        assert_eq!(d.revision, 2);
        assert!(d
            .request(RequestId::Integer(3), Method::SettingsSave, Some(json!({})))
            .is_err());
    }
}
