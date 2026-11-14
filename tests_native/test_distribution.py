"""Real isolated Hatchling artifacts and CLI smoke, never a live Herdr session."""
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(os.environ["COCKPIT_TEST_ROOT"])
UV = os.environ["COCKPIT_UV"]
HERDR = Path(os.environ["COCKPIT_HERDR_SOURCE"])


def copy_inputs(source, dest, names):
    dest.mkdir()
    for name in names:
        src, dst = source / name, dest / name
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_native", "target", ".git"), symlinks=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def command(*args, cwd=None, env=None, timeout=600):
    print("+", *map(str, args), f"[cwd={cwd or TMP}]", flush=True)
    result = subprocess.run([str(x) for x in args], cwd=cwd or TMP, env=env or os.environ.copy(),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {args}\n{result.stdout}")
    return result


class Distribution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = TMP / "distribution"
        cls.workspace.mkdir()
        cls.herdr = cls.workspace / "herdr-source"
        cls.source = cls.workspace / "cockpit-source"
        copy_inputs(HERDR, cls.herdr, ["pyproject.toml", "README.md", "LICENSE", "src", "skills"])
        copy_inputs(ROOT, cls.source, ["pyproject.toml", "hatch_build.py", "README.md", "LICENSE", "src", "rust/cockpit", "tests/fixtures"])
        cls.out = cls.workspace / "artifacts"
        cls.out.mkdir()
        command(UV, "build", "--wheel", "--out-dir", cls.out, cls.herdr)
        command(UV, "build", "--wheel", "--sdist", "--out-dir", cls.out, cls.source)
        cls.herdr_wheel = next(cls.out.glob("herdr_master-*.whl"))
        cls.wheel = next(cls.out.glob("aiheaddev-*.whl"))
        cls.sdist = next(cls.out.glob("aiheaddev-*.tar.gz"))

    def test_artifacts(self):
        self.assertNotIn("-none-any.whl", self.wheel.name)
        with zipfile.ZipFile(self.wheel) as z:
            names = z.namelist()
            binary = next(n for n in names if n.endswith("aiheaddev/_native/aihead-cockpit"))
            self.assertTrue((z.getinfo(binary).external_attr >> 16) & 0o111)
            self.assertIn("aiheaddev/bridge/__main__.py", names)
        with tarfile.open(self.sdist) as tar:
            names = tar.getnames()
            for suffix in ("rust/cockpit/Cargo.toml", "rust/cockpit/Cargo.lock", "rust/cockpit/src/main.rs", "tests/fixtures/rust_bridge_v1.json"):
                self.assertTrue(any(n.endswith("/" + suffix) for n in names), suffix)
            self.assertFalse(any("/.orchestration/" in n or n.endswith("/.herdr-master.toml") for n in names))
        # The actual worktree, not just the clean copy, must also exclude private inputs.
        actual = self.workspace / "actual"
        actual.mkdir()
        command(UV, "build", "--sdist", "--out-dir", actual, ROOT)
        with tarfile.open(next(actual.glob("*.tar.gz"))) as tar:
            names = tar.getnames()
            self.assertFalse(any("/.orchestration/" in n or n.endswith("/.herdr-master.toml") for n in names))

    def test_installations_and_commands(self):
        for mode, artifact in (("wheel", self.wheel), ("sdist", self.sdist), ("source", self.source), ("editable", self.source)):
            with self.subTest(mode=mode):
                venv = self.workspace / ("venv-" + mode)
                command(UV, "venv", "--python", sys.executable, venv)
                python = venv / "bin/python"
                command(UV, "pip", "install", "--python", python, self.herdr_wheel)
                opts = ["--editable"] if mode == "editable" else []
                command(UV, "pip", "install", "--python", python, *opts, artifact)
                env = os.environ.copy()
                env["PATH"] = "/usr/bin:/bin"  # No Cargo at runtime.
                env.pop("PYTHONPATH", None)
                env["HOME"] = str(self.workspace / ("home-" + mode))
                Path(env["HOME"]).mkdir()
                for var, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"), ("XDG_DATA_HOME", "data"), ("XDG_STATE_HOME", "state")):
                    env[var] = str(Path(env["HOME"]) / sub)
                for name in ("aiheaddev", "aihead"):
                    exe = venv / "bin" / name
                    help_text = command(exe, "--help", cwd=TMP / "cwd", env=env).stdout
                    self.assertIn("--demo", help_text)
                    self.assertIn("--install-skills", help_text)
                    result = command(exe, "--install-skills", cwd=TMP / "cwd", env=env)
                    self.assertIn("Installing", result.stdout)
                    self._demo(exe, env)
                skill = Path(env["HOME"]) / ".pi/agent/skills/aihead/SKILL.md"
                self.assertTrue(skill.is_file(), f"missing installed skill: {skill}")

    def _demo(self, exe, env):
        master, slave = pty.openpty()
        proc = subprocess.Popen([str(exe), "--demo"], stdin=slave, stdout=slave, stderr=slave,
                                cwd=TMP / "cwd", env=env, start_new_session=True)
        output = b""
        try:
            end = time.monotonic() + 20
            while b"\x1b[?1049h" not in output and time.monotonic() < end:
                if select.select([master], [], [], 0.1)[0]:
                    output += os.read(master, 65536)
                if proc.poll() is not None:
                    break
            self.assertIn(b"\x1b[?1049h", output, output[-2000:])
            os.write(master, b"q")
            self.assertEqual(proc.wait(timeout=10), 0)
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            os.close(master)
            os.close(slave)
