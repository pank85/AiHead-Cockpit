"""Private launcher for the bundled Rust cockpit."""

import fcntl
import json
import os
from importlib import resources
import selectors
import subprocess
import sys


class NativeError(RuntimeError):
    """The native executable or its private protocol failed."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def _outcome(data: bytes):
    try:
        if len(data) > 65536 or not data.endswith(b"\n") or data.count(b"\n") != 1:
            raise ValueError("expected one bounded JSON line")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        kind = value.get("type")
        fields = {"quit": {"type"}, "attach": {"type", "session", "workspace_id", "tab_id", "pane_id"},
                  "workspace": {"type", "workspace_id", "session"}}
        if kind not in fields or not set(value) <= fields[kind]:
            raise ValueError("unknown outcome or field")
        required = {"quit": (), "attach": ("session",), "workspace": ("workspace_id",)}[kind]
        if any(k not in value for k in required):
            raise ValueError("missing outcome field")
        for key in set(value) - {"type"}:
            v = value[key]
            if v is not None and (not isinstance(v, str) or not v.strip() or any(ord(c) < 32 for c in v)):
                raise ValueError("invalid outcome identity")
            if key in required and v is None:
                raise ValueError("missing outcome identity")
        if kind == "quit":
            return None
        if kind == "attach":
            return {"action": "attach", "session": value["session"]}
        return {"action": "done", "workspace_id": value["workspace_id"]}
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise NativeError(f"Native cockpit protocol mismatch: {exc}") from exc


class RustCockpit:
    def __init__(self, session=None, demo=False):
        self.session = session
        self.demo = demo

    def run(self):
        binary = resources.files("aiheaddev").joinpath("_native", "aihead-cockpit")
        try:
            with resources.as_file(binary) as path:
                if not path.is_file() or not os.access(path, os.X_OK):
                    raise NativeError("Native cockpit binary is missing or not executable; reinstall aiheaddev.")
                return self._run(path)
        except OSError as exc:
            raise NativeError(f"Cannot launch native cockpit: {exc}; reinstall aiheaddev.") from exc

    def _run(self, path):
        read_fd, write_fd = os.pipe()
        process = None
        try:
            if write_fd < 3:
                new_fd = fcntl.fcntl(write_fd, fcntl.F_DUPFD_CLOEXEC, 3)
                os.close(write_fd)
                write_fd = new_fd
            args = [str(path), "--python", os.path.abspath(sys.executable), "--outcome-fd", str(write_fd)]
            if self.session is not None:
                args.extend(["--session", self.session])
            if self.demo:
                args.append("--demo")
            process = subprocess.Popen(args, pass_fds=(write_fd,))
            os.close(write_fd)
            write_fd = -1
            # A bridge descendant can inherit the write end. A complete outcome
            # is one bounded line, not EOF. Also stop on native exit without a
            # complete line so a leaked descendant cannot hide a failure.
            data = bytearray()
            os.set_blocking(read_fd, False)
            with selectors.DefaultSelector() as selector:
                selector.register(read_fd, selectors.EVENT_READ)
                while True:
                    exited = process.poll() is not None
                    if not selector.select(0 if exited else 0.1):
                        if exited:
                            break
                        continue
                    try:
                        chunk = os.read(read_fd, 65537 - len(data))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > 65536:
                        raise NativeError("Native cockpit protocol mismatch: outcome exceeds 65536 bytes")
                    if b"\n" in data:
                        break
            os.close(read_fd)
            read_fd = -1
            code = process.wait()
            if code:
                raise NativeError(f"Native cockpit exited with status {code}.", code if code > 0 else 128 - code)
            return _outcome(bytes(data))
        finally:
            if read_fd >= 0:
                os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait()
