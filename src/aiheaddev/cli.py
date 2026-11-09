"""AiHeadDev command-line entry point."""

import argparse
import os
import sys
from typing import Optional

from rich.console import Console

from aiheaddev.ui.tui.app import HerdrMasterApp

console = Console()


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for aiheaddev."""
    parser = argparse.ArgumentParser(
        prog="aiheaddev",
        description="Launch the AiHeadDev interactive terminal management cockpit.",
    )
    parser.add_argument(
        "--session",
        dest="session",
        default=None,
        help="Herdr session name to bind to (e.g. herdr-master)",
    )
    parser.add_argument(
        "--demo",
        dest="demo",
        action="store_true",
        default=False,
        help="Run TUI in demo mode with pre-populated data",
    )
    parser.add_argument(
        "--install-skills",
        dest="install_skills",
        action="store_true",
        default=False,
        help="Install/link Herdr-Master and AiHead standalone skills across AI harnesses (agy, pi, claude, opencode)",
    )
    return parser


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


def main(argv: Optional[list[str]] = None) -> None:
    """Run the aiheaddev CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.install_skills:
        code = install_skills_entrypoint()
        if code != 0:
            sys.exit(code)
        return

    tui_app = HerdrMasterApp(session=args.session, demo=args.demo)
    result = tui_app.run()
    if isinstance(result, dict) and result.get("action") == "attach":
        target_session = result.get("session", args.session)
        console.print(
            f"[yellow]Attaching to isolated Herdr session '{target_session}'...[/yellow]"
        )
        os.execvp("herdr", ["herdr", "--session", str(target_session)])
    elif isinstance(result, dict) and result.get("action") == "done":
        ws_id = result.get("workspace_id")
        console.print(f"[green]✓[/green] Active in workspace [cyan]{ws_id}[/cyan]")


if __name__ == "__main__":
    main()
