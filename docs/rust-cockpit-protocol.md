# Private cockpit bridge: protocol 1

This is a private, local, UTF-8 newline-delimited JSON protocol, not an exposed network API. The Rust process owns the terminal; the Python child reserves stdout for protocol messages and writes diagnostics to stderr. Every line, including its newline, is at most 2 MiB. Invalid UTF-8, duplicate keys, non-finite numbers, malformed JSON and oversized messages are errors. A receiver must bound reads before allocating the full line. Request IDs correlate responses, not exactly-once execution. Never automatically replay a mutation after an uncertain result.

## Envelopes

* Request: `{"id": 1, "method": "initialize", "params": {"protocol": 1, "capabilities": [], "session": null, "demo": false}}`. IDs are strings or integers, not booleans. Params is always an object. Initialize must reject any version other than integer 1 and negotiate capabilities explicitly.
* Success: `{"id": 1, "result": {"protocol": 1, "capabilities": []}}`.
* Failure: `{"id": 1, "error": {"code": "invalid_params", "message": "…", "field_errors": {"path": "…"}, "outcome": "uncertain"}}`. Last two fields are optional. Outcome is used when dispatch may have occurred; never replay automatically.
* Event: `{"event": "inventory", "revision": 1, "payload": {"sessions": []}}`. Revisions are opaque ordering tokens, not Herdr event sequence numbers.

The only methods are `initialize`, `catalog.get`, `project.open`, `folders.list`, `folders.test`, `settings.validate`, `settings.save`, `inventory.refresh`, `selection.set`, `reply.submit`, `terminal.input`, `launch.master`, `launch.ergo`, `launch.orchestration`, `launch.council`, `aihead.open`, `workspace.delete`, `logs.get`, `shutdown`. Unknown methods fail closed. Open in Herdr is an outcome, not a method. JSON examples for the shared codec are in `tests/fixtures/rust_bridge_v1.json`.

## Settings service

`catalog.get` returns dependency catalog `harnesses`, `native_harnesses` and `presets`; do not hard-code models. `project.open` takes a user-selected `path`; it returns `token`, `revision`, resolved `project_path`, absolute `destination`, `scope` (`new-local`, `local`, `inherited`), `sections`, `retained_metadata` and `catalog`. The five editable sections are `projects`, `squad`, `orchestration`, `council`, `ai_head`; field names and values are the dependency's model JSON representations. Omitted fields are preserved. Arrays are replacement arrays containing complete rows, including labels, descriptions, order, enabled flags and any unrecognized metadata returned in the row. Clients retain these values when editing/reordering. Return edits plus token and revision, never TOML. `settings.validate` and `settings.save` take `token`, `revision`, `sections`. Validation returns `valid`, `field_errors`, `route_previews`, the current `sections` and the next `revision` when the draft was updated. Validation errors from model construction return the original revision and `field_errors`. A stale revision or changed destination is an error. Save checks destination content and discovery, writes through the dependency to the same path and retains a draft after failure. On save failure `field_errors.revision` carries the current revision for explicit retry; `outcome: uncertain` requires checking the destination rather than blindly replaying. To cancel, send `settings.validate` with `token`, `revision`, `discard: true` and no `sections`; runtime invokes `discard(token, revision=revision)`, returns `{}` and makes no filesystem write. Drafts are also discarded on client shutdown. No user edit may change retained metadata implicitly.

The editor initializes completely unset orchestration defaults using the dependency's `DEFAULT_ORCHESTRATION_MODELS` and matching project bindings; user-entered blanks are rejected rather than repaired. To switch harnesses, validate a partial `orchestration: {harness: ...}` patch; the service retains the old per-harness fields and activates the new harness's saved values or dependency defaults. A patch may also explicitly provide new active fields. All supplied per-harness configurations receive native route validation. A successful validation increments the revision, so use that revision for subsequent patches or save.

The file fingerprint and discovery are checked around opening and immediately before saving, so an observed external change requires reload. `save_project_config(config, path=None) -> Path` has no expected-fingerprint or locking argument: the dependency performs its own write after our last check. Thus this is compare-before-write, **not atomic compare-and-swap**; a non-cooperating writer can still win the check/write race. An I/O failure may have partially written and is reported uncertain. Do not claim stronger concurrency guarantees or blindly replay.

`folders.test` and `folders.list` accept an explicit user-chosen path (and optional project base); return the expanded resolved path and availability. List returns directory names and paths only, bounded to 4096. Errors are field feedback. These methods do not perform shell completion or change the process cwd.

## Method payloads

All payloads are objects; no unknown fields are accepted. The validator in `src/aiheaddev/bridge/protocol.py` is authoritative. Required fields are listed without `?`; `?` means optional and nullable unless called non-null below. IDs and paths are nonempty strings of at most 4096 characters; general text is at most 256 KiB UTF-8. A JSON object or array contains at most 4096 elements, nesting is at most 32, and the total line remains at most 2 MiB. Unknown keys, malformed nested target/lease, non-finite numbers and structurally oversized values fail closed. Request IDs and revisions are nonnegative integers no larger than 2^53−1 or, for IDs only, nonempty strings. Optional nullable session preserves null/omitted (inherit) versus an explicit name. Optional `capabilities`, `demo`, `sections`, `discard`, `limit`, `text`, and `keys` are non-null when present. No method accepts an executable or an arbitrary RPC name.

| Method | Request params | Result / completion |
| --- | --- | --- |
| `initialize` | `protocol` integer 1; `capabilities`? string array, `session`? identity, `demo`? boolean | `{protocol: 1, capabilities: [string]}`; omitted capabilities/demo mean []/false |
| `catalog.get` | `{}` | catalog descriptor object |
| `project.open` | `path` | settings draft object described above |
| `folders.list`, `folders.test` | `path`, optional `project` | resolved path and availability, with directories for list |
| `settings.validate` | `token` identity, `revision` integer, exactly one of `sections` object or `discard: true` | validation result or `{}` for discard |
| `settings.save` | `token`, `revision`, `sections` object | validation result or `{valid: true, revision, destination}`; draft retained on failure |
| `inventory.refresh` | `{}` | acknowledgement; updated inventory event |
| `selection.set` | `target` | acknowledgement; new frames follow |
| `reply.submit` | `lease`, `text` string, `notify` boolean | acknowledgement; correlated `reply_result` |
| `terminal.input` | `lease`, exactly one of `text` string or `keys` array of nonempty strings | acknowledgement; correlated operation result |
| `launch.master` | `project_path`, `session`?, `name`, `harness`, `model` identities; `effort`? identity, `task`? string, `ai_head`? boolean | operation status and successful workspace outcome |
| `launch.ergo` | `project_path`, `session`?, `intent`, `label` strings, `squad`? dependency Squad object, `customize_first`, `start_pipeline`, `save_default` booleans, `ai_head`? boolean | operation status and successful workspace outcome |
| `launch.orchestration` | `project_path`, `session`?, `intent`, `label`, `harness`, `scout_model`, `architect_model`, `reviewer_model`, `worker_models` nonempty string array; `scout_effort`?, `architect_effort`?, `reviewer_effort`? identities; `dangerously_skip_permissions` boolean; `ai_head`? boolean | operation status and successful workspace outcome |
| `launch.council` | `project_path`, `session`?, `idea`, `label` strings, `config` dependency CouncilConfig object, `customize_roster` boolean | operation status and successful workspace outcome; AI Head disabled |
| `aihead.open` | `project_path`, `session`? | operation status and selected existing/new AI Head |
| `workspace.delete` | exact workspace-only `target`, `confirmed: true` | operation status |
| `logs.get` | `limit`? integer 1–1000 | bounded log records |
| `shutdown` | `{}` | acknowledgement then close |

For launches, `ai_head` is null/inherit, true/enable, false/disable. The codec validates DTO structure; runtime must also validate dependency Squad/CouncilConfig, catalog routes, folders, readiness, session/socket ownership and business rules before dispatch. The runtime must not infer an operation from selection. Role model/effort names are exactly as listed, and worker models remain an ordered array until the dependency dispatch joins them with ` | `. A successful asynchronous mutation response is `{pending: true}`; its correlated event carries the terminal outcome. Read-only refresh/selection acknowledgements are `{}`. `logs.get` returns `{records: [{level, message, timestamp}]}` with strings, bounded and redacted; `folders.test` returns `{path, available}` and `folders.list` adds `directories: [{name, path}]`. A false/failed request uses the error envelope, not a fake success.

## Target and lifecycle semantics

A target is `{"socket_path":"…","workspace_id":"…","tab_id":"…","pane_id":"…","terminal_id":"…"}`; the first two are required; a live lease additionally requires `pane_id` and `terminal_id` (`tab_id` remains optional). A lease is `{"target": target, "generation": 2, "terminal_id": "…"}` and both terminal IDs must match. IDs are identities; labels never select an endpoint. Runtime must compare the lease with its current controller lease and actual verified socket/terminal, never rely on UI `can_reply`.

### Event payload DTOs

All events are `{event, revision, payload}`. Strings below are UTF-8; `?` is optional/nullable. Empty arrays/objects are explicit. Additional arbitrary backend details belong only in the `metadata` objects, which remain bounded JSON; never place secrets, prompts or pane contents there.

- `inventory`: `{sessions: [Session]}`. `Session` is `{socket_path: identity, session?: identity, connection: "connected"|"offline"|"error", error?: string, workspaces: [Workspace]}`. `Workspace` is `{workspace_id: identity, label: string, tabs: [Tab], agents: [Agent], metadata: object}`. `Tab` is `{tab_id: identity, label: string, panes: [Pane], layout: Layout}`. `Pane` is `{pane_id: identity, terminal_id?: identity, label: string, state: identity, role?: string, readiness?: Readiness, metadata: object}`. `Agent` is `{pane_id?: identity, agent: string, role?: string, state: identity, readiness?: Readiness, metadata: object}`. State strings come from the dependency and are not inferred from labels. Duplicate pane IDs within a tab and foreign layout pane IDs are rejected.
- `Readiness` is `{can_reply: boolean, explanation: string, state: identity}`. `can_reply` is display information only, not authority to send.
- `Layout` is `{state: "available"|"layout_unavailable", rects: [Rect], focused_pane_id?: identity}`. `Rect` is `{pane_id: identity, x: integer, y: integer, width: positive integer, height: positive integer}` in the authoritative layout coordinate space. Unavailable means an empty `rects` array, never fabricated geometry. Runtime verifies the socket and ownership of workspace/tab/pane IDs before constructing a layout.
- `pane_frame`: `{target: pane Target, lease: Lease|null, text: string, readiness: Readiness, layout: Layout}`. Each visible pane has a bounded read-only frame; only the selected pane may carry a live lease and the target must exactly equal the lease target. A pane without a lease may lack `terminal_id`. Text is capped at 1000 lines and 256 KiB. The Rust renderer sanitizes untrusted terminal controls. Stale reads and target/lease mismatches are discarded, never rerouted.
- `reply_result`: `{request_id: request ID, outcome: "accepted"|"rejected"|"uncertain"|"pending", message: string, lease?: Lease|null}`.
- `operation_status`: `{request_id: request ID, outcome: "accepted"|"rejected"|"uncertain"|"pending", message: string, phase?: string, status?: string, target?: Target|null}`. The request ID correlates the operation but does not claim server-side exactly-once execution. A disconnect after dispatch is uncertain.

Inventory events retain all discovered sessions, socket identity, online/offline/error status and actual workspace/tab/pane/agent identities. Other visible-pane previews are read-only, with bounded concurrent reads and stale reads cancelled. Output and inventory updates may be coalesced; mutation results must not be dropped. Shared complete examples are in `tests/fixtures/rust_bridge_v1.json`.

Demo uses synthetic data only, with no discovery, save, live sends or file logging. Shutdown releases only the bridge's own clients and controllers, never workspaces or agents. The final private outcome is exactly `{type:"quit"}`, `{type:"attach", session: identity, workspace_id?: identity, tab_id?: identity, pane_id?: identity}`, or `{type:"workspace", workspace_id: identity, session?: identity}`. Extra keys, executables and argument lists are rejected. Restore terminal state before the wrapper handles the outcome.
