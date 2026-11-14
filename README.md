# AiHead-Cockpit

AiHead-Cockpit launches native [Herdr](https://herdr.dev/) and provides a small command-pane menu for [AiHead-Orchestrator](https://github.com/pank85/AiHead-Orchestrator). Install Herdr separately and ensure `herdr`, `herdr-master`, and `herdm` are on PATH. Install this Python package with `pip install aiheaddev`. Skill installation is separate and optional: `aihead --install-skills`.

## Start

Run `aihead` (or `aiheaddev`) to launch or attach native Herdr; `aihead --session NAME` selects a Herdr session. The former Rust cockpit and its demo are not the product screen.

## Herdr menu binding

Add the following to your Herdr `config.toml`, choosing an unused key if necessary. This project does not edit that file. Herdr 0.9.5 documents `type = "pane"` as a temporary zoomed pane that closes when the command exits.

```toml
[[keys.command]]
key = "prefix+alt+a"
type = "pane"
command = "aihead --menu"
description = "AiHead menu"
```

Run `herdr config check` and `herdr server reload-config` after editing. The command must be on the Herdr server's PATH. Use up/down to select an action, Enter to open it, or q to quit. The menu starts AiHead, New Master, New Ergo, Orchestrate, and AI Council using `herdr-master`; Ergo, Orchestrate, and Council ask for an objective. Settings runs `herdm aihead-setup`. The Orchestrator setup TUI owns squad and per-skill settings and the existing `.herdr-master.toml` config API; install an Orchestrator version that provides that TUI. Older versions may provide a different setup command instead. No separate settings store is used.

Herdr closes workspaces with its built-in `prefix+shift+d` and `confirm_close` behavior. This integration does not add a right-click menu item.

## Development

Install project dependencies and run `pytest`. Source and wheels do not need to build the old Rust cockpit. No Textual interface is used.

## License

MIT; see [LICENSE](LICENSE).
