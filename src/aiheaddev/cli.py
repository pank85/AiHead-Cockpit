"""AiHeadDev command-line entry point."""

import argparse
import os
import sys
from pathlib import Path

from rich.console import Console

from aiheaddev.native import NativeError, RustCockpit

console = Console()


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for aiheaddev."""
    parser = argparse.ArgumentParser(
        prog="aihead",
        description="Open native Herdr or run an AiHead command.",
    )
    parser.add_argument(
        "--session",
        dest="session",
        default=None,
        help="Herdr session name to bind to (e.g. herdr-master)",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        default=False,
        help="Launch the AiHead-Cockpit TUI",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        default=False,
        help="Run the cockpit TUI in demo mode with pre-populated data",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        default=False,
        help="Open the interactive settings TUI",
    )
    parser.add_argument(
        "--config",
        dest="config",
        type=Path,
        default=None,
        help="Optional path to .herdr-master.toml configuration file",
    )
    parser.add_argument(
        "--herdr-master",
        dest="herdr_master",
        action="store_true",
        default=False,
        help="Start Herdr Master in a new Herdr workspace",
    )
    parser.add_argument(
        "--ergo",
        default=None,
        metavar="OBJECTIVE",
        help="Start Ergo in a new Herdr workspace",
    )
    parser.add_argument(
        "--orchestration",
        default=None,
        metavar="OBJECTIVE",
        help="Start Orchestration in a new Herdr workspace",
    )
    parser.add_argument(
        "--council",
        default=None,
        metavar="OBJECTIVE",
        help="Start Council in a new Herdr workspace",
    )
    parser.add_argument(
        "--menu", action="store_true", help="Open the AiHead menu in a Herdr command pane"
    )
    parser.add_argument(
        "--install-skills",
        dest="install_skills",
        action="store_true",
        default=False,
        help="Install/link Herdr-Master and AiHead standalone skills across AI harnesses (agy, pi, claude, opencode)",
    )
    return parser


def _run_cockpit(session: str | None, demo: bool) -> None:
    tui_app = RustCockpit(session=session, demo=demo)
    try:
        result = tui_app.run()
    except NativeError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(exc.code)
    if isinstance(result, dict) and result.get("action") == "attach":
        target_session = result.get("session", session)
        console.print(
            f"[yellow]Attaching to isolated Herdr session '{target_session}'...[/yellow]"
        )
        os.execvp("herdr", ["herdr", "--session", str(target_session)])
    elif isinstance(result, dict) and result.get("action") == "done":
        ws_id = result.get("workspace_id")
        console.print(f"[green]✓[/green] Active in workspace [cyan]{ws_id}[/cyan]")


def _run_setup(config: Path | None) -> None:
    try:
        import typer
        from herdr_master.cli.aihead_setup import run_skill_setup
    except ImportError as exc:
        console.print(f"[red]herdr-master is required for --setup: {exc}[/red]")
        sys.exit(1)

    target = config.expanduser() if config is not None else None
    try:
        run_skill_setup(config=target)
    except typer.Exit as exc:
        sys.exit(exc.exit_code)


def _run_skill_flag(
    skill: str,
    intent: str | None,
    session: str | None,
    config: Path | None,
) -> None:
    try:
        import typer
        from herdr_master.cli.main import _launch_skill
    except ImportError as exc:
        console.print(f"[red]herdr-master is required for --{skill}: {exc}[/red]")
        sys.exit(1)

    target = config.expanduser() if config is not None else None
    if target is not None and not target.exists():
        console.print(f"[red]Configuration file not found: {config}[/red]")
        sys.exit(1)
    try:
        _launch_skill(skill, intent, session, None, target, None, None, None)
    except typer.Exit as exc:
        sys.exit(exc.exit_code)


def install_skills_entrypoint() -> int:
    """Install and link Herdr-Master and AiHead standalone skills across AI harnesses."""
    try:
        from herdr_master.cli.skills import install_skills

        install_skills(force=True)
        return 0
    except ImportError:
        console.print(
            "[red]Error: 'herdr-master' is not installed or available in this environment.[/red]\n"
            "Please install it using: [cyan]pip install herdr-master[/cyan] or run [cyan]./install.sh[/cyan]"
        )
        return 1


def main(argv: list[str] | None = None) -> None:
    """Run the aiheaddev CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    skills = [
        (name, value)
        for name, value in (
            ("ergo", args.ergo),
            ("orchestration", args.orchestration),
            ("council", args.council),
        )
        if value is not None
    ]
    if args.herdr_master:
        skills.append(("herdr-master", None))

    active_modes = (
        int(args.install_skills)
        + int(args.menu)
        + int(args.tui)
        + int(args.setup)
        + len(skills)
    )
    if active_modes > 1:
        parser.error("Choose only one launch mode.")
    if args.demo and not args.tui:
        parser.error("--demo can only be used with --tui")
    if args.config is not None and not (args.setup or skills):
        parser.error("--config can only be used with --setup or a skill launch flag")

    if args.install_skills:
        code = install_skills_entrypoint()
        if code != 0:
            sys.exit(code)
        return

    if args.menu:
        if args.session:
            parser.error("--session cannot be combined with --menu")
        from aiheaddev.menu import run_menu

        run_menu()
        return

    if args.tui:
        _run_cockpit(session=args.session, demo=args.demo)
        return

    if args.setup:
        _run_setup(config=args.config)
        return

    if skills:
        skill_name, objective = skills[0]
        if skill_name in ("ergo", "orchestration", "council") and not (objective or "").strip():
            parser.error(f"--{skill_name} requires a non-empty objective")
        _run_skill_flag(
            skill=skill_name,
            intent=objective,
            session=args.session,
            config=args.config,
        )
        return

    command = ["herdr"]
    if args.session:
        command.extend(["--session", args.session])
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        console.print(f"[red]Cannot start Herdr: {exc}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
