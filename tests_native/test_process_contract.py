"""Offline PTY and inherited outcome-descriptor lifecycle acceptance checks."""
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import termios
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests_native/fixtures/bridge_stub.py"
BINARY = Path(os.environ["COCKPIT_TEST_BINARY"])
PYTHON = ROOT / ".venv/bin/python"


class ProcessContract(unittest.TestCase):
    def test_ctrl_c_restores_terminal(self):
        """A Ctrl+C keyboard interrupt must leave the PTY usable."""
        master, slave = pty.openpty()
        before = termios.tcgetattr(slave)
        read_fd, write_fd = os.pipe()
        env = os.environ.copy()
        env["COCKPIT_BRIDGE_SCENARIO"] = "idle"
        process = subprocess.Popen(
            [str(BINARY), "--python", str(FIXTURE), "--outcome-fd", str(write_fd)],
            stdin=slave, stdout=slave, stderr=slave, pass_fds=(write_fd,),
            env=env, cwd=env["COCKPIT_TEST_CWD"], start_new_session=True,
        )
        os.close(write_fd)
        output = b""
        try:
            deadline = time.monotonic() + 5
            while b"\x1b[?1049h" not in output and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    output += os.read(master, 65536)
            self.assertIn(b"\x1b[?1049h", output)
            os.write(master, b"\x03")
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.fail("Ctrl+C did not exit the native UI or restore the terminal")
            while select.select([master], [], [], 0)[0]:
                output += os.read(master, 65536)
            self.assertIn(b"\x1b[?1049l", output)
            self.assertIn(b"\x1b[?25h", output)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(json.loads(os.read(read_fd, 65536)), {"type": "quit"})
            self.assertEqual(termios.tcgetattr(slave), before)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            os.close(read_fd)
            os.close(master)
            os.close(slave)

    def test_disconnected_crash_and_malformed_restore_terminal(self):
        for scenario in ("crash", "malformed"):
            with self.subTest(scenario=scenario):
                master, slave = pty.openpty()
                before = termios.tcgetattr(slave)
                read_fd, write_fd = os.pipe()
                env = os.environ.copy()
                env["COCKPIT_BRIDGE_SCENARIO"] = scenario
                process = subprocess.Popen(
                    [str(BINARY), "--python", str(FIXTURE), "--outcome-fd", str(write_fd)],
                    stdin=slave, stdout=slave, stderr=slave, pass_fds=(write_fd,),
                    env=env, cwd=env["COCKPIT_TEST_CWD"], start_new_session=True,
                )
                os.close(write_fd)
                output = b""
                try:
                    deadline = time.monotonic() + 5
                    while b"\x1b[?1049h" not in output and time.monotonic() < deadline:
                        if select.select([master], [], [], 0.1)[0]:
                            output += os.read(master, 65536)
                    self.assertIn(b"\x1b[?1049h", output)
                    time.sleep(0.4)
                    os.write(master, b"q")
                    self.assertEqual(process.wait(timeout=5), 0)
                    while select.select([master], [], [], 0)[0]:
                        output += os.read(master, 65536)
                    self.assertIn(b"\x1b[?1049l", output)
                    self.assertIn(b"\x1b[?25h", output)
                    self.assertEqual(json.loads(os.read(read_fd, 65536)), {"type": "quit"})
                    self.assertEqual(termios.tcgetattr(slave), before)
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    os.close(read_fd)
                    os.close(master)
                    os.close(slave)

    def test_descendant_does_not_delay_wrapper(self):
        """The real Python wrapper must return even if a descendant holds the pipe."""
        master, slave = pty.openpty()
        before = termios.tcgetattr(slave)
        env = os.environ.copy()
        env.update(COCKPIT_BRIDGE_SCENARIO="descendant", PYTHONPATH=str(ROOT / "src"))
        marker = Path(env["COCKPIT_TEST_CWD"]) / "descendant.pid"
        marker.unlink(missing_ok=True)
        env["COCKPIT_DESCENDANT_MARKER"] = str(marker)
        code = ("import sys; from pathlib import Path; "
                "from aiheaddev.native import RustCockpit; "
                "sys.executable=sys.argv[1]; "
                "print('WRAPPER_RESULT', repr(RustCockpit()._run(Path(sys.argv[2]))), flush=True)")
        process = subprocess.Popen(
            [str(PYTHON), "-B", "-c", code, str(FIXTURE), str(BINARY)],
            stdin=slave, stdout=slave, stderr=slave, env=env,
            cwd=env["COCKPIT_TEST_CWD"], start_new_session=True,
        )
        output = b""
        try:
            deadline = time.monotonic() + 5
            while (b"\x1b[?1049h" not in output or not marker.exists()) and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    output += os.read(master, 65536)
            self.assertIn(b"\x1b[?1049h", output, "native UI did not enter alternate screen")
            self.assertTrue(marker.exists(), "bridge descendant did not start")
            os.write(master, b"q")
            self.assertEqual(process.wait(timeout=5), 0)
            while select.select([master], [], [], 0)[0]:
                output += os.read(master, 65536)
            self.assertIn(b"WRAPPER_RESULT None", output)
            self.assertIn(b"\x1b[?1049l", output)
            self.assertEqual(termios.tcgetattr(slave), before)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            os.close(master)
            os.close(slave)
            marker.unlink(missing_ok=True)
