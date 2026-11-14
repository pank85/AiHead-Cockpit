#!/usr/bin/env python3
"""Explicit, synthetic-environment native and distribution acceptance runner."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = "14d1b988ca6fb21f56834e0de6739254831c698f8fa174c9a12a7158d281add2"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", required=True)
    parser.parse_args()
    config_paths = [p / ".herdr-master.toml" for p in (ROOT, *ROOT.parents)]
    before = {str(p): digest(p) for p in config_paths}
    if before[str(ROOT / ".herdr-master.toml")] != EXPECTED:
        raise RuntimeError("approved config hash does not match")
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("BLOCKED: uv is required for isolated builds and installs")
    python = ROOT / ".venv/bin/python"
    if not python.is_file():
        raise RuntimeError(f"BLOCKED: missing {python}")
    cargo = shutil.which("cargo")
    if not cargo:
        raise RuntimeError("BLOCKED: cargo is required")
    dependency = Path(os.environ.get("COCKPIT_HERDR_SOURCE", "/home/pank/projects/AiHead-Orchestrator"))
    if not (dependency / "pyproject.toml").is_file():
        raise RuntimeError(f"BLOCKED: authorized herdr-master source missing: {dependency}")
    base = ROOT / ".orchestration"
    base.mkdir(exist_ok=True)
    # Python's Unix-socket tests require short paths (<108 bytes).
    with tempfile.TemporaryDirectory(prefix="t6-accept-", dir=base) as tmp, tempfile.TemporaryDirectory(prefix="t6-", dir="/tmp") as sandbox:
        tmp = Path(tmp)
        for part in ("home", "cwd", "tmp", "home/config", "home/cache", "home/data", "home/state", "runtime", "sandbox"):
            (tmp / part).mkdir(parents=True, exist_ok=True)
        env = {"HOME": str(tmp / "home"), "XDG_CONFIG_HOME": str(tmp / "home/config"),
               "XDG_CACHE_HOME": str(tmp / "home/cache"), "XDG_DATA_HOME": str(tmp / "home/data"),
               "XDG_STATE_HOME": str(tmp / "home/state"), "XDG_RUNTIME_DIR": str(tmp / "runtime"), "TMPDIR": str(tmp / "tmp"),
               "PATH": os.environ.get("PATH", ""), "TERM": "xterm-256color", "PYTHONDONTWRITEBYTECODE": "1",
               "COCKPIT_TEST_CWD": str(tmp / "cwd"), "COCKPIT_TEST_ROOT": str(tmp),
               "COCKPIT_HERDR_SOURCE": str(dependency), "COCKPIT_UV": uv,
               "UI_SANDBOX_RUNTIME_PARENT": sandbox}
        for name in ("RUSTUP_HOME", "CARGO_HOME", "CARGO_TARGET_DIR"):
            if name in os.environ:
                env[name] = os.environ[name]
        for path in (tmp / ".herdr-master.toml", tmp / "cwd/.herdr-master.toml"):
            path.write_text('# synthetic canary: do not modify\n', encoding='utf-8')
            before[str(path)] = digest(path)
        print(f"Synthetic root: {tmp}\nHOME: {env['HOME']}\nXDG: {env['XDG_CONFIG_HOME']}\ncwd: {env['COCKPIT_TEST_CWD']}\nTMPDIR: {env['TMPDIR']}\nHerdr source (copy only): {dependency}\nuv: {uv}\ncargo: {cargo}\nPython sandbox: {sandbox}", flush=True)
        def run(args, cwd=ROOT):
            print("+", *map(str, args), f"[cwd={cwd}]", flush=True)
            subprocess.run([str(a) for a in args], cwd=cwd, env=env, check=True)
        try:
            manifest = "rust/cockpit/Cargo.toml"
            run([cargo, "fmt", "--manifest-path", manifest, "--all", "--", "--check"])
            run([cargo, "test", "--locked", "--manifest-path", manifest, "--all-targets"])
            run([cargo, "clippy", "--locked", "--manifest-path", manifest, "--all-targets", "--", "-D", "warnings"])
            # The guarded suite's real Hatchling regression must not silently skip.
            run([uv, "pip", "install", "--python", python, "--target", tmp / "test-deps", "hatchling"])
            env["PYTHONPATH"] = str(tmp / "test-deps")
            run([python, "-B", "tests/ui_sandbox.py", "--", "tests"])
            env.pop("PYTHONPATH")
            run([cargo, "build", "--locked", "--manifest-path", manifest, "--bin", "aihead-cockpit"])
            target = Path(env.get("CARGO_TARGET_DIR", str(ROOT / "rust/cockpit/target")))
            env["COCKPIT_TEST_BINARY"] = str(target.resolve() / "debug/aihead-cockpit")
            run([python, "-B", "-m", "unittest", "discover", "-s", "tests_native", "-p", "test_*.py", "-v"])
        finally:
            after = {path: digest(Path(path)) for path in before}
            if after != before:
                raise RuntimeError(f"config canary changed: before={before}, after={after}")
    print("All native and distribution acceptance gates passed.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        print(f"BLOCKED/FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
