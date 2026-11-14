# AiHead-Cockpit

AiHead-Cockpit provides the `aihead` CLI wrapper around native [Herdr](https://herdr.dev/), the AiHead-Cockpit TUI (`aihead --tui`), and a command-pane menu for [AiHead-Orchestrator](https://github.com/pank85/AiHead-Orchestrator). Install Herdr separately and ensure `herdr`, `herdr-master`, and `herdm` are on PATH. Install this Python package with `pip install aiheaddev`. Skill installation is separate and optional: `aihead --install-skills`.

## Commands

- `aihead` (or `aihead --session NAME`): opens native Herdr (does not start the cockpit).
- `aihead --tui`: starts the AiHead-Cockpit TUI.
- `aihead --setup [--config /absolute/path/to/.herdr-master.toml]`: opens the interactive settings TUI. The settings screen covers `aihead`, `ergo`, `orchestration`, `council`, `herdr-master`, and the `worker-pool` (worker pool).
- `aihead --herdr-master`: starts Herdr Master in a new Herdr workspace and switches to it when Herdr can switch, or starts it in a terminal when switching is unavailable.
- `aihead --ergo <objective>`: starts Ergo in a new Herdr workspace and switches to it when Herdr can switch, or starts it in a terminal when switching is unavailable.
- `aihead --orchestration <objective>`: starts Orchestration in a new Herdr workspace and switches to it when Herdr can switch, or starts it in a terminal when switching is unavailable.
- `aihead --council <objective>`: starts Council in a new Herdr workspace and switches to it when Herdr can switch, or starts it in a terminal when switching is unavailable.

## Herdr workspace actions

Link this checkout with `herdr plugin link /absolute/path/to/AiHead-Cockpit`. The
workspace context menu then offers AiHead, New Master, New Ergo, Orchestrate,
AI Council, and Settings. These actions open zoomed plugin panes; New Master runs
`aihead --herdr-master`, Settings runs `aihead --setup`, and Ergo, Orchestrate,
and Council use the menu's objective prompt before calling `aihead --ergo`,
`aihead --orchestration`, and `aihead --council`. The plugin requires Herdr
0.9.5, Python 3, and the installed `aihead` and `herdr-master` commands on PATH.
Keep the linked checkout in place while using the plugin.

## Herdr menu binding

Add the following to your Herdr `config.toml`, choosing an unused key if necessary. This project does not edit that file. Herdr 0.9.5 documents `type = "pane"` as a temporary zoomed pane that closes when the command exits.

```toml
[[keys.command]]
key = "prefix+alt+a"
type = "pane"
command = "aihead --menu"
description = "AiHead menu"
```

Run `herdr config check` and `herdr server reload-config` after editing. The command must be on the Herdr server's PATH. Use up/down to select an action, Enter to open it, or q to quit. The menu starts AiHead (`herdr-master head`), New Master (`aihead --herdr-master`), New Ergo (`aihead --ergo`), Orchestrate (`aihead --orchestration`), AI Council (`aihead --council`), and Settings (`aihead --setup`); Ergo, Orchestrate, and Council ask for an objective. The Orchestrator settings TUI covers `aihead`, `ergo`, `orchestration`, `council`, `herdr-master`, and the worker pool and writes to `.herdr-master.toml`. No separate settings store is used.

Herdr closes workspaces with its built-in `prefix+shift+d` and `confirm_close` behavior.

## Development

Install project dependencies and run `pytest`. Check Rust bridge changes with `cargo test` inside `rust/cockpit`.

## License

MIT; see [LICENSE](LICENSE).
