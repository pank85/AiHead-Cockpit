"""Tests for the Herdr launcher, unified aihead flags, and command menu."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from aiheaddev import menu
from aiheaddev.cli import main


def test_launch_herdr_does_not_start_cockpit():
    with (
        patch("aiheaddev.cli.RustCockpit") as cockpit,
        patch("aiheaddev.cli.os.execvp") as execute,
    ):
        main([])
    cockpit.assert_not_called()
    execute.assert_called_once_with("herdr", ["herdr"])


def test_launch_session():
    with (
        patch("aiheaddev.cli.RustCockpit") as cockpit,
        patch("aiheaddev.cli.os.execvp") as execute,
    ):
        main(["--session", "work"])
    cockpit.assert_not_called()
    execute.assert_called_once_with("herdr", ["herdr", "--session", "work"])


def test_tui_starts_cockpit():
    with patch("aiheaddev.cli.RustCockpit") as cockpit:
        cockpit.return_value.run.return_value = None
        main(["--tui", "--session", "work"])
    cockpit.assert_called_once_with(session="work", demo=False)
    cockpit.return_value.run.assert_called_once_with()


def test_setup_accepts_absolute_config_and_invokes_settings_tui(tmp_path):
    cfg = (tmp_path / ".herdr-master.toml").resolve()
    calls = []
    fake_module = SimpleNamespace(run_skill_setup=lambda *, config=None: calls.append(config))
    with patch.dict("sys.modules", {"herdr_master.cli.aihead_setup": fake_module}):
        main(["--setup", "--config", str(cfg)])
    assert calls == [cfg]


def test_setup_without_config_invokes_settings_tui():
    calls = []
    fake_module = SimpleNamespace(run_skill_setup=lambda *, config=None: calls.append(config))
    with patch.dict("sys.modules", {"herdr_master.cli.aihead_setup": fake_module}):
        main(["--setup"])
    assert calls == [None]


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["--herdr-master"], ("herdr-master", None, None)),
        (["--ergo", "fix bug"], ("ergo", "fix bug", None)),
        (["--orchestration", "ship feature"], ("orchestration", "ship feature", None)),
        (["--council", "review arch"], ("council", "review arch", "work")),
    ],
)
def test_skill_flags_launch_workspace_skill(argv, expected):
    calls = []

    def fake_launch_skill(skill, intent, session, cwd, config, harness, model, effort):
        calls.append((skill, intent, session))

    full_argv = argv + (["--session", "work"] if expected[2] else [])
    fake_module = SimpleNamespace(_launch_skill=fake_launch_skill)
    with patch.dict("sys.modules", {"herdr_master.cli.main": fake_module}):
        main(full_argv)
    assert calls == [expected]


@pytest.mark.parametrize(
    "argv",
    [
        ["--herdr-master", "--no-attach"],
        ["--ergo", "task", "--no-attach"],
        ["--tui", "--setup"],
        ["--ergo", "task", "--council", "plan"],
        ["--ergo", "   "],
    ],
)
def test_invalid_or_disallowed_flags_fail(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code != 0


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
    (1, ["aihead", "--herdr-master"]), (2, ["aihead", "--ergo", "objective"]),
    (3, ["aihead", "--orchestration", "objective"]),
    (4, ["aihead", "--council", "objective"]), (5, ["aihead", "--setup"])])
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
