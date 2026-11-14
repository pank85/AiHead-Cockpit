"""Contract tests for the Herdr workspace plugin."""
from pathlib import Path
from unittest.mock import patch

import pytest

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from aiheaddev import menu

ROOT = Path(__file__).resolve().parents[1]


def test_workspace_actions_and_panes():
    with (ROOT / "herdr-plugin.toml").open("rb") as file:
        manifest = tomllib.load(file)
    expected = {
        "head": ("AiHead", ["herdr-master", "head"]),
        "master": ("New Master", ["aihead", "--herdr-master"]),
        "ergo": ("New Ergo", ["python3", "src/aiheaddev/menu.py", "ergo"]),
        "orchestration": ("Orchestrate", ["python3", "src/aiheaddev/menu.py", "orchestration"]),
        "council": ("AI Council", ["python3", "src/aiheaddev/menu.py", "council"]),
        "settings": ("Settings", ["aihead", "--setup"]),
    }
    actions = {action["id"]: action for action in manifest["actions"]}
    panes = {pane["id"]: pane for pane in manifest["panes"]}
    assert len(actions) == len(manifest["actions"]) == len(expected)
    assert len(panes) == len(manifest["panes"]) == len(expected)
    assert actions.keys() == panes.keys() == expected.keys()
    for action_id, (title, command) in expected.items():
        action, pane = actions[action_id], panes[action_id]
        assert action["title"] == pane["title"] == title
        assert action["contexts"] == ["workspace"]
        assert action["command"] == [
            "herdr", "plugin", "pane", "open", "--plugin", manifest["id"],
            "--entrypoint", action_id, "--placement", "zoomed", "--focus",
        ]
        assert pane["command"] == command
        assert pane["placement"] == "zoomed"
        assert action["platforms"] == pane["platforms"] == manifest["platforms"]


@pytest.mark.parametrize("choice,command", [
    (2, ["aihead", "--ergo"]),
    (3, ["aihead", "--orchestration"]),
    (4, ["aihead", "--council"]),
])
def test_preselected_action_prompts_only_for_objective(choice, command):
    with patch("aiheaddev.menu.curses.wrapper") as wrapper, \
         patch("builtins.input", return_value="  build a thing  "), \
         patch("aiheaddev.menu.subprocess.run") as run:
        menu.run_menu(choice)
    wrapper.assert_not_called()
    run.assert_called_once_with(command + ["build a thing"], check=True)


@pytest.mark.parametrize("choice", [2, 3, 4])
def test_empty_objective_cancels(choice):
    with patch("builtins.input", return_value="  "), \
         patch("aiheaddev.menu.subprocess.run") as run:
        menu.run_menu(choice)
    run.assert_not_called()
