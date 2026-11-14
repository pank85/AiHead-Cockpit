"""Tests for the Herdr launcher and command menu."""
from unittest.mock import patch

import pytest

from aiheaddev.cli import main
from aiheaddev import menu


def test_launch_herdr():
    with patch("aiheaddev.cli.os.execvp") as execute:
        main([])
        execute.assert_called_once_with("herdr", ["herdr"])


def test_launch_session():
    with patch("aiheaddev.cli.os.execvp") as execute:
        main(["--session", "work"])
        execute.assert_called_once_with("herdr", ["herdr", "--session", "work"])


def test_menu_dispatch():
    with patch("aiheaddev.menu.run_menu") as run:
        main(["--menu"])
        run.assert_called_once()


class FakeScreen:
    def __init__(self, keys):
        self.keys = iter(keys)
        self.lines = []

    def keypad(self, enabled):
        assert enabled

    def erase(self):
        pass

    def getmaxyx(self):
        return (24, 80)

    def addnstr(self, y, x, text, width, *args):
        self.lines.append(text)

    def refresh(self):
        pass

    def getch(self):
        return next(self.keys)


def test_keyboard_selection_without_terminal():
    screen = FakeScreen([menu.curses.KEY_DOWN, menu.curses.KEY_DOWN,
                         menu.curses.KEY_UP, 10])
    with patch("aiheaddev.menu.curses.curs_set"):
        assert menu.select_action(screen) == 1
    for label in ("AiHead", "New Master", "New Ergo", "Orchestrate", "AI Council", "Settings"):
        assert any(label in line for line in screen.lines)
    assert "> New Master" in screen.lines


def test_keyboard_quit_and_wrap():
    with patch("aiheaddev.menu.curses.curs_set"):
        assert menu.select_action(FakeScreen([ord("q")])) is None
        assert menu.select_action(FakeScreen([menu.curses.KEY_UP, 13])) == 5


@pytest.mark.parametrize("choice,arguments", [(0, ["herdr-master", "head"]),
    (1, ["herdr-master", "master", "start"]), (2, ["herdr-master", "ergo", "objective"]),
    (3, ["herdr-master", "orchestration", "objective"]),
    (4, ["herdr-master", "council", "objective"]), (5, ["herdm", "aihead-setup"])])
def test_menu_launchers(choice, arguments):
    with patch("aiheaddev.menu.curses.wrapper", return_value=choice), \
            patch("builtins.input", return_value="objective") as prompt, patch("subprocess.run") as run:
        menu.run_menu()
    run.assert_called_once_with(arguments, check=True)
    assert prompt.call_count == (1 if choice in (2, 3, 4) else 0)


def test_quit_does_not_launch():
    with patch("aiheaddev.menu.curses.wrapper", return_value=None), patch("subprocess.run") as run:
        menu.run_menu()
    run.assert_not_called()



def test_install_skills_does_not_launch():
    with patch("aiheaddev.cli.install_skills_entrypoint", return_value=0), patch("os.execvp") as execute:
        main(["--install-skills"])
        execute.assert_not_called()
