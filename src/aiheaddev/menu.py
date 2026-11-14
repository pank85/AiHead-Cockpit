"""Keyboard-selectable menu for Herdr's temporary command pane."""

import curses
import subprocess


OPTIONS = (
    ("AiHead", ("herdr-master", "head")),
    ("New Master", ("herdr-master", "master", "start")),
    ("New Ergo", ("herdr-master", "ergo")),
    ("Orchestrate", ("herdr-master", "orchestration")),
    ("AI Council", ("herdr-master", "council")),
    ("Settings", ("herdm", "aihead-setup")),
)


def select_action(screen):
    """Return the selected action index, or None on quit."""
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    selected = 0
    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        if height > 1 and width > 1:
            screen.addnstr(0, 0, "AiHead  •  ↑/↓ select  Enter open  q quit", width - 1)
        rows = max(0, height - 2) if width > 1 else 0
        start = max(0, selected - rows + 1)
        for line, index in enumerate(range(start, min(len(OPTIONS), start + rows)), 1):
            label = ("> " if index == selected else "  ") + OPTIONS[index][0]
            screen.addnstr(line, 0, label, max(0, width - 1),
                           curses.A_REVERSE if index == selected else curses.A_NORMAL)
        screen.refresh()
        key = screen.getch()
        if key in (ord("q"), ord("Q"), 27):
            return None
        if key == curses.KEY_UP:
            selected = (selected - 1) % len(OPTIONS)
        elif key == curses.KEY_DOWN:
            selected = (selected + 1) % len(OPTIONS)
        elif key in (10, 13, curses.KEY_ENTER):
            return selected


def run_menu() -> None:
    try:
        choice = curses.wrapper(select_action)
        if choice is None:
            return
        command = list(OPTIONS[choice][1])
        if choice in (2, 3, 4):
            intent = input("Objective (empty to cancel): ").strip()
            if not intent:
                return
            command.append(intent)
        subprocess.run(command, check=True)
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
    except (OSError, curses.error, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}")
