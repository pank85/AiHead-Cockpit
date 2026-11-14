//! Launch DTO composition from the currently opened project's server draft.
use aihead_cockpit::{
    panel::ActionId,
    protocol::{Method, Request, RequestId},
};
use serde_json::{json, Map, Value};
use std::os::raw::{c_char, c_int, c_long};

// The executable is Unix-only. Use the POSIX reentrant local-time conversion so
// launch labels have the same local timezone and DST behavior as datetime.now().
// The trailing fields cover the Linux/BSD/macOS struct tm extensions; the first
// nine fields are the POSIX layout, and other Unix implementations may use less.
#[repr(C)]
struct LocalTime {
    second: c_int,
    minute: c_int,
    hour: c_int,
    day: c_int,
    month: c_int,
    year: c_int,
    weekday: c_int,
    yearday: c_int,
    daylight_saving: c_int,
    utc_offset: c_long,
    zone: *const c_char,
}

// Bind the ABI of the `time` / `localtime_r` symbols themselves. On older
// 32-bit Linux ABIs these are the legacy time32 symbols (even with newer musl);
// platforms without a time32 ABI, including riscv32 and GNU x32, use time64.
#[cfg(all(
    target_pointer_width = "32",
    any(
        all(
            target_os = "linux",
            not(target_arch = "riscv32"),
            not(all(target_env = "gnu", target_arch = "x86_64"))
        ),
        target_os = "android",
        target_os = "solaris",
        target_os = "illumos",
        target_os = "aix",
        target_os = "haiku",
        target_vendor = "apple",
        all(target_os = "freebsd", target_arch = "x86")
    )
))]
type TimeT = c_long;
#[cfg(not(all(
    target_pointer_width = "32",
    any(
        all(
            target_os = "linux",
            not(target_arch = "riscv32"),
            not(all(target_env = "gnu", target_arch = "x86_64"))
        ),
        target_os = "android",
        target_os = "solaris",
        target_os = "illumos",
        target_os = "aix",
        target_os = "haiku",
        target_vendor = "apple",
        all(target_os = "freebsd", target_arch = "x86")
    )
)))]
type TimeT = i64;

extern "C" {
    #[cfg_attr(target_os = "netbsd", link_name = "__time50")]
    fn time(timer: *mut TimeT) -> TimeT;
    #[cfg_attr(target_os = "netbsd", link_name = "__localtime_r50")]
    fn localtime_r(timer: *const TimeT, result: *mut LocalTime) -> *mut LocalTime;
}

fn local_time_at(timestamp: TimeT) -> LocalTime {
    let mut result = LocalTime {
        second: 0,
        minute: 0,
        hour: 0,
        day: 0,
        month: 0,
        year: 0,
        weekday: 0,
        yearday: 0,
        daylight_saving: 0,
        utc_offset: 0,
        zone: std::ptr::null(),
    };
    // SAFETY: `localtime_r` receives a valid time_t and correctly aligned,
    // writable storage for the Unix tm ABI. It is caller-owned, so concurrent
    // launch forms do not share the output.
    let converted = unsafe { localtime_r(&timestamp, &mut result) };
    assert!(
        !converted.is_null(),
        "cannot convert the current time to local time"
    );
    result
}

fn local_time() -> LocalTime {
    // SAFETY: POSIX `time` accepts a null pointer when only its return is needed.
    local_time_at(unsafe { time(std::ptr::null_mut()) })
}

fn label_at(prefix: &str, include_seconds: bool, now: &LocalTime) -> String {
    let base = format!(
        "{prefix}-{:04}{:02}{:02}-{:02}{:02}",
        i64::from(now.year) + 1900,
        now.month + 1,
        now.day,
        now.hour,
        now.minute
    );
    if include_seconds {
        format!("{base}{:02}", now.second)
    } else {
        base
    }
}

pub fn form(action: ActionId, project: &str, sections: &Value) -> Value {
    let scout = &sections["squad"]["scout"];
    match action {
        ActionId::NewMaster => {
            json!({"project_path":project,"name":"master-coordinator","harness":scout.get("kind").cloned().unwrap_or(Value::Null),"model":scout.get("model").cloned().unwrap_or(Value::Null),"effort":scout.get("effort").cloned().unwrap_or(Value::Null),"task":"","ai_head":null})
        }
        ActionId::NewErgo => {
            json!({"project_path":project,"intent":"","label":label_at("ergo", false, &local_time()),"customize_first":false,"start_pipeline":true,"save_default":false,"ai_head":null})
        }
        ActionId::Orchestrate => {
            let config = &sections["orchestration"];
            let mut v = json!({"project_path":project,"intent":"","label":label_at("orch", true, &local_time()),"ai_head":null});
            for name in [
                "harness",
                "scout_model",
                "architect_model",
                "reviewer_model",
                "worker_models",
                "scout_effort",
                "architect_effort",
                "reviewer_effort",
                "dangerously_skip_permissions",
            ] {
                v[name] = config.get(name).cloned().unwrap_or(match name {
                    "worker_models" => json!([]),
                    "dangerously_skip_permissions" => json!(true),
                    _ => Value::Null,
                });
            }
            v
        }
        ActionId::AiCouncil => {
            json!({"project_path":project,"idea":"","label":label_at("council", false, &local_time()),"customize_roster":false})
        }
        ActionId::AiHead => json!({"project_path":project}),
        _ => json!({"project_path":project}),
    }
}
pub fn orchestration_to_sections(form: &Value, sections: &mut Value) {
    for name in [
        "harness",
        "scout_model",
        "architect_model",
        "reviewer_model",
        "worker_models",
        "scout_effort",
        "architect_effort",
        "reviewer_effort",
        "dangerously_skip_permissions",
    ] {
        if let Some(v) = form.get(name) {
            sections["orchestration"][name] = v.clone();
        }
    }
}
pub fn apply_orchestration(form: &mut Value, sections: &Value) {
    for name in [
        "harness",
        "scout_model",
        "architect_model",
        "reviewer_model",
        "worker_models",
        "scout_effort",
        "architect_effort",
        "reviewer_effort",
        "dangerously_skip_permissions",
    ] {
        if let Some(v) = sections.get("orchestration").and_then(|s| s.get(name)) {
            form[name] = v.clone();
        }
    }
}
pub fn request(
    action: ActionId,
    id: RequestId,
    form: &Value,
    sections: &Value,
    project: &str,
    session: Option<&str>,
) -> Result<Request, String> {
    if form.get("project_path").and_then(Value::as_str) != Some(project) {
        return Err("Project changed: press F5 to load its own configuration before launch".into());
    }
    let required: &[&str] = match action {
        ActionId::NewMaster => &["name", "harness", "model"],
        ActionId::Orchestrate => &[
            "harness",
            "scout_model",
            "architect_model",
            "reviewer_model",
        ],
        _ => &[],
    };
    for name in required {
        if form
            .get(*name)
            .and_then(Value::as_str)
            .is_none_or(|s| s.trim().is_empty())
        {
            return Err(format!("{name} is required"));
        }
    }
    if action == ActionId::Orchestrate
        && !form
            .get("worker_models")
            .and_then(Value::as_array)
            .is_some_and(|workers| {
                !workers.is_empty()
                    && workers
                        .iter()
                        .all(|v| v.as_str().is_some_and(|s| !s.trim().is_empty()))
            })
    {
        return Err("At least one nonblank worker_models entry is required".into());
    }
    let mut params = form.as_object().cloned().ok_or("Invalid form")?;
    if let Some(session) = session {
        params.insert("session".into(), json!(session));
    }
    let method = match action {
        ActionId::NewMaster => Method::LaunchMaster,
        ActionId::NewErgo => {
            params.insert("squad".into(), sections["squad"].clone());
            Method::LaunchErgo
        }
        ActionId::Orchestrate => Method::LaunchOrchestration,
        ActionId::AiCouncil => {
            params.insert("config".into(), sections["council"].clone());
            Method::LaunchCouncil
        }
        ActionId::AiHead => Method::AiHeadOpen,
        _ => return Err("Not a launch action".into()),
    };
    Request::new(id, method, params).map_err(|e| e.to_string())
}
pub fn open_project(id: RequestId, path: &str) -> Result<Request, String> {
    let mut params = Map::new();
    params.insert("path".into(), json!(path));
    Request::new(id, Method::ProjectOpen, params).map_err(|e| e.to_string())
}
#[cfg(test)]
mod tests {
    use super::*;
    fn sections() -> Value {
        json!({"squad":{"scout":{"kind":"from-server","model":"model-a","effort":null},"worker_pool":[]},"orchestration":{"harness":"from-server","scout_model":"s","architect_model":"a","reviewer_model":"r","worker_models":["w"],"dangerously_skip_permissions":false},"council":{"members":[]}})
    }
    #[test]
    fn launch_label_formats_and_editable_values() {
        let date = LocalTime {
            year: 124,
            month: 1,
            day: 9,
            hour: 3,
            minute: 4,
            second: 5,
            weekday: 0,
            yearday: 0,
            daylight_saving: 0,
            utc_offset: 0,
            zone: std::ptr::null(),
        };
        assert_eq!(label_at("ergo", false, &date), "ergo-20240209-0304");
        assert_eq!(label_at("orch", true, &date), "orch-20240209-030405");
        assert_eq!(label_at("council", false, &date), "council-20240209-0304");
        let sections = sections();
        for (action, prefix, stamp_len) in [
            (ActionId::NewErgo, "ergo-", 13),
            (ActionId::Orchestrate, "orch-", 15),
            (ActionId::AiCouncil, "council-", 13),
        ] {
            let mut f = form(action, "/p", &sections);
            let stamp = f["label"].as_str().unwrap().strip_prefix(prefix).unwrap();
            assert_eq!(stamp.len(), stamp_len);
            assert!(stamp.bytes().enumerate().all(|(i, b)| if i == 8 {
                b == b'-'
            } else {
                b.is_ascii_digit()
            }));
            f["label"] = json!("custom-工作区");
            let request =
                request(action, RequestId::Integer(1), &f, &sections, "/p", None).unwrap();
            assert_eq!(request.params["label"], "custom-工作区");
        }
    }
    #[test]
    fn local_clock_respects_process_timezone() {
        // TZ is inherited from the test process; no global mutation or subprocess.
        let actual = local_time_at(1_735_689_607);
        match std::env::var("TZ").as_deref() {
            Ok("UTC0") => assert_eq!(label_at("orch", true, &actual), "orch-20250101-000007"),
            Ok("EST5") => assert_eq!(label_at("orch", true, &actual), "orch-20241231-190007"),
            Ok("EST5EDT,M3.2.0,M11.1.0") => {
                assert_eq!(label_at("orch", true, &actual), "orch-20241231-190007");
                let summer = local_time_at(1_751_371_200);
                assert_eq!(label_at("orch", true, &summer), "orch-20250701-080000");
            }
            _ => {
                assert!((0..=11).contains(&actual.month));
                assert!((1..=31).contains(&actual.day));
                assert!((0..=23).contains(&actual.hour));
            }
        }
    }
    #[test]
    fn orchestration_permission_default_is_independent_of_aihead() {
        let mut s = sections();
        s["orchestration"]
            .as_object_mut()
            .unwrap()
            .remove("dangerously_skip_permissions");
        for aihead_value in [false, true] {
            s["ai_head"] = json!({"dangerously_skip_permissions": aihead_value});
            assert_eq!(
                form(ActionId::Orchestrate, "/p", &s)["dangerously_skip_permissions"],
                true
            );
        }
        for configured in [false, true] {
            s["orchestration"]["dangerously_skip_permissions"] = json!(configured);
            s["ai_head"]["dangerously_skip_permissions"] = json!(!configured);
            assert_eq!(
                form(ActionId::Orchestrate, "/p", &s)["dangerously_skip_permissions"],
                configured
            );
        }
    }
    #[test]
    fn four_forms_keep_all_flags_and_project_isolation() {
        for action in [
            ActionId::NewMaster,
            ActionId::NewErgo,
            ActionId::Orchestrate,
            ActionId::AiCouncil,
        ] {
            let f = form(action, "/one", &sections());
            assert!(request(action, RequestId::Integer(1), &f, &sections(), "/two", None).is_err());
            let req = request(
                action,
                RequestId::Integer(2),
                &f,
                &sections(),
                "/one",
                Some("session"),
            )
            .unwrap();
            assert_eq!(req.params["project_path"], "/one");
            assert_eq!(req.params["session"], "session");
            match action {
                ActionId::NewErgo => {
                    for k in [
                        "customize_first",
                        "start_pipeline",
                        "save_default",
                        "ai_head",
                        "squad",
                    ] {
                        assert!(req.params.contains_key(k));
                    }
                }
                ActionId::AiCouncil => {
                    assert!(req.params.contains_key("config"));
                    assert!(!req.params.contains_key("ai_head"));
                }
                _ => {}
            }
        }
    }
    #[test]
    fn master_model_is_project_scout_not_static_catalog() {
        let f = form(ActionId::NewMaster, "/p", &sections());
        assert_eq!(f["harness"], "from-server");
        assert_eq!(f["model"], "model-a");
    }
    #[test]
    fn launch_dtos_keep_every_value_and_tri_state() {
        let mut config = sections();
        config["squad"]["opaque"] = json!("keep");
        config["council"]["members"] = json!([{"label":"member","enabled":true,"extra":"kept"}]);
        for ai in [Value::Null, json!(true), json!(false)] {
            let mut master = form(ActionId::NewMaster, "/p", &config);
            master["name"] = json!("lead");
            master["model"] = json!("custom");
            master["effort"] = json!("fast");
            master["task"] = json!("hello\nworld");
            master["ai_head"] = ai.clone();
            let r = request(
                ActionId::NewMaster,
                RequestId::Integer(1),
                &master,
                &config,
                "/p",
                None,
            )
            .unwrap();
            assert_eq!(Value::Object(r.params), master);
            let mut ergo = form(ActionId::NewErgo, "/p", &config);
            ergo["intent"] = json!("go");
            ergo["label"] = json!("label");
            ergo["customize_first"] = json!(true);
            ergo["start_pipeline"] = json!(false);
            ergo["save_default"] = json!(true);
            ergo["ai_head"] = ai.clone();
            let r = request(
                ActionId::NewErgo,
                RequestId::Integer(2),
                &ergo,
                &config,
                "/p",
                Some("session"),
            )
            .unwrap();
            let mut expected = ergo.clone();
            expected["session"] = json!("session");
            expected["squad"] = config["squad"].clone();
            assert_eq!(Value::Object(r.params), expected);
            let mut orch = form(ActionId::Orchestrate, "/p", &config);
            orch["intent"] = json!("go");
            orch["scout_effort"] = json!("fast");
            orch["architect_effort"] = json!("slow");
            orch["reviewer_effort"] = json!("fast");
            orch["dangerously_skip_permissions"] = json!(true);
            orch["ai_head"] = ai;
            let r = request(
                ActionId::Orchestrate,
                RequestId::Integer(3),
                &orch,
                &config,
                "/p",
                None,
            )
            .unwrap();
            assert_eq!(Value::Object(r.params), orch);
        }
        let mut council = form(ActionId::AiCouncil, "/p", &config);
        council["idea"] = json!("idea");
        council["customize_roster"] = json!(true);
        let r = request(
            ActionId::AiCouncil,
            RequestId::Integer(4),
            &council,
            &config,
            "/p",
            None,
        )
        .unwrap();
        let mut expected = council;
        expected["config"] = config["council"].clone();
        assert_eq!(Value::Object(r.params), expected);
        let ai = form(ActionId::AiHead, "/p", &config);
        assert_eq!(
            request(
                ActionId::AiHead,
                RequestId::Integer(5),
                &ai,
                &config,
                "/p",
                None
            )
            .unwrap()
            .method,
            Method::AiHeadOpen
        );
    }
    #[test]
    fn blank_model_is_error_and_harness_copies_preserve_other_fields() {
        let s = sections();
        let mut f = form(ActionId::NewMaster, "/p", &s);
        f["model"] = json!("");
        assert_eq!(
            request(
                ActionId::NewMaster,
                RequestId::Integer(1),
                &f,
                &s,
                "/p",
                None
            )
            .unwrap_err(),
            "model is required"
        );
        let mut f = form(ActionId::Orchestrate, "/p", &s);
        f["scout_model"] = json!("");
        assert_eq!(
            request(
                ActionId::Orchestrate,
                RequestId::Integer(2),
                &f,
                &s,
                "/p",
                None
            )
            .unwrap_err(),
            "scout_model is required"
        );
        let mut updated = s.clone();
        f["scout_model"] = json!("alpha-edited");
        orchestration_to_sections(&f, &mut updated);
        assert_eq!(updated["orchestration"]["scout_model"], "alpha-edited");
        updated["orchestration"]["scout_model"] = json!("beta-saved");
        apply_orchestration(&mut f, &updated);
        assert_eq!(f["scout_model"], "beta-saved");
        assert_eq!(f["project_path"], "/p");
    }
}
