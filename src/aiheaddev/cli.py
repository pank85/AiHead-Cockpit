"""AiHeadDev command-line entry point."""

import argparse
import os
import sys
from typing import Optional

from rich.console import Console

console = Console()


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for aiheaddev."""
    parser = argparse.ArgumentParser(
        prog="aiheaddev",
        description="Open native Herdr or the AiHead command menu.",
    )
    parser.add_argument(
        "--session",
        dest="session",
        default=None,
        help="Herdr session name to bind to (e.g. herdr-master)",
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

    if args.menu:
        if args.session:
            parser.error("--session cannot be combined with --menu")
        from aiheaddev.menu import run_menu
        run_menu()
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
