"""Tests for aiheaddev CLI entry point."""

from unittest.mock import MagicMock, patch

from aiheaddev.cli import build_parser, main


def test_parser_defaults():
    parser = build_parser()
    args = parser.parse_args([])
    assert args.session is None
    assert args.demo is False
    assert args.install_skills is False


def test_parser_custom_args():
    parser = build_parser()
    args = parser.parse_args(["--session", "my-sess", "--demo", "--install-skills"])
    assert args.session == "my-sess"
    assert args.demo is True
    assert args.install_skills is True


def test_main_install_skills():
    with patch("aiheaddev.cli.install_skills_entrypoint", return_value=0) as mock_install, patch(
        "aiheaddev.cli.HerdrMasterApp"
    ) as mock_app_cls:
        main(["--install-skills"])
        mock_install.assert_called_once()
        mock_app_cls.assert_not_called()


def test_install_scripts_exist_and_executable():
    import os
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    install_sh = repo_root / "install.sh"
    scripts_install_sh = repo_root / "scripts" / "install.sh"

    assert install_sh.is_file(), "install.sh must exist at repo root"
    assert os.access(install_sh, os.X_OK), "install.sh must be executable"

    assert scripts_install_sh.is_file(), "scripts/install.sh must exist"
    assert os.access(scripts_install_sh, os.X_OK), "scripts/install.sh must be executable"


def test_main_normal_exit():
    with patch("aiheaddev.cli.HerdrMasterApp") as mock_app_cls:
        mock_app = MagicMock()
        mock_app.run.return_value = None
        mock_app_cls.return_value = mock_app

        main(["--session", "test-sess"])

        mock_app_cls.assert_called_once_with(session="test-sess", demo=False)
        mock_app.run.assert_called_once()


def test_main_attach_action():
    with patch("aiheaddev.cli.HerdrMasterApp") as mock_app_cls, patch("os.execvp") as mock_execvp:
        mock_app = MagicMock()
        mock_app.run.return_value = {"action": "attach", "session": "remote-sess"}
        mock_app_cls.return_value = mock_app

        main(["--session", "orig-sess"])

        mock_execvp.assert_called_once_with("herdr", ["herdr", "--session", "remote-sess"])


def test_main_done_action():
    with patch("aiheaddev.cli.HerdrMasterApp") as mock_app_cls, patch("aiheaddev.cli.console.print") as mock_print:
        mock_app = MagicMock()
        mock_app.run.return_value = {"action": "done", "workspace_id": "wX"}
        mock_app_cls.return_value = mock_app

        main([])

        assert any("wX" in str(arg) for call in mock_print.call_args_list for arg in call[0])
