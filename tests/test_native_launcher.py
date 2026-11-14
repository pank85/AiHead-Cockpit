"""Offline native wrapper tests; no child processes are spawned."""
import os
import sys
import threading
from unittest.mock import patch

import pytest

from aiheaddev.native import NativeError, RustCockpit, _outcome


def test_outcomes_and_invalid_messages():
    assert _outcome(b'{"type":"quit"}\n') is None
    assert _outcome(b'{"type":"attach","session":"s"}\n') == {"action": "attach", "session": "s"}
    assert _outcome(b'{"type":"workspace","workspace_id":"w"}\n') == {"action": "done", "workspace_id": "w"}
    for data in (b'', b'{}\n', b'{"type":"quit","x":1}\n', b'{"type":"attach","session":null}\n', b'{"type":"quit"}\n{}\n'):
        with pytest.raises(NativeError, match="protocol mismatch"):
            _outcome(data)


def test_launch_fd_args_and_cleanup(tmp_path):
    executable = tmp_path / "native"
    executable.write_text("fixture")
    executable.chmod(0o755)
    calls = []

    class Child:
        def wait(self): return 0
        def poll(self): return 0

    def spawn(argv, *, pass_fds):
        calls.append((argv, pass_fds))
        os.write(pass_fds[0], b'{"type":"quit"}\n')
        return Child()

    with patch("aiheaddev.native.subprocess.Popen", side_effect=spawn):
        assert RustCockpit(None, True)._run(executable) is None
    args, fds = calls[0]
    assert args == [str(executable), "--python", os.path.abspath(sys.executable), "--outcome-fd", str(fds[0]), "--demo"]
    assert fds[0] >= 3
    with pytest.raises(OSError):
        os.fstat(fds[0])


@pytest.mark.parametrize('payload,exit_code', [(b'{"type":"quit"}\n', None), (b'{"type":', 0)])
def test_inherited_writer_does_not_block_completion(tmp_path, payload, exit_code):
    leaked = []
    class Child:
        waited = False
        def wait(self):
            self.waited = True
            return 0
        def poll(self):
            return 0 if self.waited else exit_code
        def terminate(self):
            self.waited = True
    def spawn(argv, *, pass_fds):
        leaked.append(os.dup(pass_fds[0]))
        os.write(pass_fds[0], payload)
        return Child()
    # A safety timer prevents a broken EOF-waiting implementation from hanging
    # the test suite, while requiring completion before the descendant closes.
    timer = None
    def start_timer(argv, *, pass_fds):
        nonlocal timer
        child = spawn(argv, pass_fds=pass_fds)
        timer = threading.Timer(2, lambda: os.close(leaked.pop()))
        timer.start()
        return child
    try:
        with patch('aiheaddev.native.subprocess.Popen', side_effect=start_timer):
            if exit_code is None:
                assert RustCockpit()._run(tmp_path / 'native') is None
            else:
                with pytest.raises(NativeError, match='protocol mismatch'):
                    RustCockpit()._run(tmp_path / 'native')
        assert timer.is_alive(), 'waited for leaked writer to close'
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()
        if leaked:
            os.close(leaked.pop())


def test_child_failure_code(tmp_path):
    class Child:
        def wait(self): return 17
        def poll(self): return 17
    with patch("aiheaddev.native.subprocess.Popen", return_value=Child()):
        with pytest.raises(NativeError) as error:
            RustCockpit("s")._run(tmp_path / "executable")
    assert error.value.code == 17


def test_launch_error_is_clear_and_closes_pipe(tmp_path):
    executable = tmp_path / 'broken'
    executable.write_text('not an executable image')
    executable.chmod(0o755)
    with patch('aiheaddev.native.resources.files') as files, \
         patch('aiheaddev.native.subprocess.Popen', side_effect=OSError('Exec format error')):
        files.return_value.joinpath.return_value = executable
        with pytest.raises(NativeError, match='Exec format error'):
            RustCockpit().run()


def test_missing_binary():
    with patch("aiheaddev.native.resources.files") as files:
        files.return_value.joinpath.return_value = __import__('pathlib').Path('/nonexistent/aihead-cockpit')
        with pytest.raises(NativeError, match="missing"):
            RustCockpit().run()
