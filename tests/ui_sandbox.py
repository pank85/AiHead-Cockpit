#!/usr/bin/env python3
"""Hardened Test Fence and Isolation Entrypoint for AiHeadDev TUI test suite.

Sole documented entrypoint for Textual UI/UX test suites:
    .venv/bin/python -B tests/ui_sandbox.py -- <pytest arguments>

Gate invocation example:
    .venv/bin/python -B tests/ui_sandbox.py -- \\
      tests/test_ui_test_isolation.py tests/test_tui_compact.py \\
      tests/test_ergo_responsive_layout.py tests/test_council_modal.py \\
      tests/test_settings_modal.py tests/test_squad_editors.py

Architecture:
1. Pre-execution stdlib ancestor discovery and SHA-256 fingerprinting before any product imports.
2. Process-lifetime temporary container with writable work subtree and bootstrap-owned read-only canary sibling.
   Container path respects UI_SANDBOX_RUNTIME_PARENT, TMPDIR, or active .orchestration run directory.
3. Precreates valid empty local .herdr-master.toml in work subtree.
4. Installs a strict CPython audit fence (sys.addaudithook) enforcing:
   - Thread-safe reentrancy via threading.local() ensuring no race-condition bypasses across threads.
   - Canonicalized path write bounds (open flags, mkdir, rename/replace, remove/rmdir, truncate, chmod, link, symlink, dir_fd).
   - Denies external socket connection, DNS, send, and bind; preserves asyncio local socketpair.
   - Denies subprocess/system/exec/spawn launch families.
   - Rejects symlink escapes outside the work subtree.
   - Disallows /dev/null write exemption.
5. Aliases runner module in sys.modules so conftest and test suites share ONE live guard context.
6. In-process pytest execution with unrelated plugin autoload disabled and strictly managed output logs.
7. Verification of all original ancestor hashes and absence sentinels in a guaranteed finally block.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

# Module aliasing: ensure sys.modules has canonical references to this runner instance
sys.modules["tests.ui_sandbox"] = sys.modules[__name__]
sys.modules["ui_sandbox"] = sys.modules[__name__]


@dataclasses.dataclass
class AncestorSnapshot:
    """Snapshot of discoverable ancestor configuration file."""

    path: Path
    canonical_path: Path
    originally_existed: bool
    original_sha256: Optional[str] = None

    def verify_unchanged(self) -> Tuple[bool, Optional[str], Optional[str]]:
        """Verify that the ancestor file has not been created, deleted, or modified."""
        current_exists = self.canonical_path.is_file()
        if not self.originally_existed:
            return (not current_exists, None, ("EXISTS_NOW" if current_exists else None))
        if not current_exists:
            return (False, self.original_sha256, "DELETED")
        current_sha256 = hashlib.sha256(self.canonical_path.read_bytes()).hexdigest()
        return (current_sha256 == self.original_sha256, self.original_sha256, current_sha256)


@dataclasses.dataclass
class DenialRecord:
    """Record of an audit fence denial."""

    event: str
    target: str
    reason: str
    expected: bool


class SandboxGuardContext:
    """Live context holding sandbox state and tracking fence denials in a thread-safe manner."""

    def __init__(
        self,
        container_dir: Path,
        work_dir: Path,
        canary_path: Path,
        repo_root: Path,
        ancestor_records: Dict[str, AncestorSnapshot],
    ) -> None:
        self.container_dir = container_dir.resolve()
        self.work_dir = work_dir.resolve()
        self.canary_path = canary_path.resolve()
        self.repo_root = repo_root.resolve()
        self.ancestor_records = ancestor_records
        self.denials: List[DenialRecord] = []
        self._local = threading.local()
        self._lock = threading.Lock()
        self.active: bool = True

    def is_active(self) -> bool:
        return self.active

    @contextlib.contextmanager
    def expect_denial(self):
        """Context manager to mark intentional fence violations as expected on the current thread."""
        current = getattr(self._local, "expecting_count", 0)
        self._local.expecting_count = current + 1
        try:
            yield
        finally:
            self._local.expecting_count = max(0, getattr(self._local, "expecting_count", 1) - 1)

    def is_expecting_denial(self) -> bool:
        return getattr(self._local, "expecting_count", 0) > 0

    def record_denial(self, event: str, target: str, reason: str) -> None:
        with self._lock:
            self.denials.append(
                DenialRecord(
                    event=event,
                    target=target,
                    reason=reason,
                    expected=self.is_expecting_denial(),
                )
            )

    @property
    def unexpected_denials(self) -> List[DenialRecord]:
        with self._lock:
            return [d for d in self.denials if not d.expected]

    @property
    def expected_denials(self) -> List[DenialRecord]:
        with self._lock:
            return [d for d in self.denials if d.expected]


_ACTIVE_GUARD: Optional[SandboxGuardContext] = None
_GUARD_INSTALL_COUNT: int = 0


def get_active_guard() -> Optional[SandboxGuardContext]:
    """Return the active SandboxGuardContext if present."""
    return _ACTIVE_GUARD


def require_guard() -> SandboxGuardContext:
    """Check that the live sandbox guard is active; fails closed if absent."""
    guard = get_active_guard()
    if guard is None or not guard.is_active():
        raise RuntimeError(
            "Sandbox guard is not active! Tests must be run via tests/ui_sandbox.py entrypoint."
        )
    return guard


def _set_active_guard(guard: Optional[SandboxGuardContext]) -> None:
    """Set the active guard context (used by bootstrap and unit isolation tests)."""
    global _ACTIVE_GUARD
    _ACTIVE_GUARD = guard


def discover_ancestor_configs(
    original_cwd: Path,
    original_home: Path,
    repo_root: Path,
) -> Dict[str, AncestorSnapshot]:
    """Capture original cwd/home/repo ancestor .herdr-master.toml paths using stdlib only."""
    roots = [original_cwd, original_home, repo_root]
    candidate_paths: Set[Path] = set()
    for root in roots:
        r = root.resolve()
        candidate_paths.add(r / ".herdr-master.toml")
        for parent in r.parents:
            candidate_paths.add(parent / ".herdr-master.toml")

    snapshots: Dict[str, AncestorSnapshot] = {}
    for candidate in sorted(candidate_paths):
        canonical = candidate.resolve()
        key = str(canonical)
        if key in snapshots:
            continue
        existed = canonical.is_file()
        sha256_val = None
        if existed:
            sha256_val = hashlib.sha256(canonical.read_bytes()).hexdigest()
        snapshots[key] = AncestorSnapshot(
            path=candidate,
            canonical_path=canonical,
            originally_existed=existed,
            original_sha256=sha256_val,
        )
    return snapshots


def resolve_runtime_parent(repo_root: Path) -> Optional[Path]:
    """Resolve preferred runtime directory for process-lifetime sandbox container."""
    # 1. Explicit UI_SANDBOX_RUNTIME_PARENT environment variable
    env_parent = os.environ.get("UI_SANDBOX_RUNTIME_PARENT")
    if env_parent:
        p = Path(env_parent).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    # 2. Existing TMPDIR if explicitly configured
    env_tmpdir = os.environ.get("TMPDIR")
    if env_tmpdir:
        p = Path(env_tmpdir).resolve()
        if p.is_dir():
            return p

    # 3. Active .orchestration run directory under repo root
    orch_dir = repo_root / ".orchestration"
    if orch_dir.is_dir():
        run_dirs = [d for d in orch_dir.iterdir() if d.is_dir()]
        if run_dirs:
            # Pick the most recently modified run directory
            run_dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
            return run_dirs[0].resolve()

    return None


def _is_path_inside_work(
    path_or_fd: Any,
    work_dir: Path,
    dir_fd: Optional[int] = None,
) -> Tuple[bool, str]:
    """Determine whether path_or_fd resides entirely within work_dir."""
    if dir_fd is not None and dir_fd >= 0:
        try:
            base_dir_str = os.readlink(f"/proc/self/fd/{dir_fd}")
            base_dir = Path(base_dir_str)
        except OSError as e:
            return False, f"dir_fd_{dir_fd}_unresolvable: {e}"
        if isinstance(path_or_fd, (str, bytes, os.PathLike)):
            p_str = os.fsdecode(path_or_fd)
            p_path = Path(p_str)
            candidate = p_path if p_path.is_absolute() else (base_dir / p_path)
        else:
            return False, f"ambiguous_dir_fd_path_form: {type(path_or_fd)}"
    elif isinstance(path_or_fd, (str, bytes, os.PathLike)):
        p_str = os.fsdecode(path_or_fd)
        candidate = Path(p_str)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
    elif isinstance(path_or_fd, int):
        try:
            fd_target = os.readlink(f"/proc/self/fd/{path_or_fd}")
            candidate = Path(fd_target)
        except OSError as e:
            return False, f"fd_{path_or_fd}_unresolvable: {e}"
    else:
        return False, f"unsupported_path_form: {type(path_or_fd)}"

    try:
        resolved = candidate.resolve()
    except Exception as e:
        return False, f"canonicalization_error: {e}"

    if resolved == work_dir or resolved.is_relative_to(work_dir):
        return True, str(resolved)
    return False, str(resolved)


_audit_thread_local = threading.local()


def _install_audit_hook(guard: SandboxGuardContext) -> None:
    """Install the single process-lifetime CPython audit fence with thread-safe reentrancy."""
    global _GUARD_INSTALL_COUNT
    if _GUARD_INSTALL_COUNT > 0:
        return
    _GUARD_INSTALL_COUNT += 1

    def audit_hook(event: str, args: tuple) -> None:
        if getattr(_audit_thread_local, "in_hook", False):
            return
        _audit_thread_local.in_hook = True
        try:
            # 1. Subprocess and execution launch family
            if event in (
                "subprocess.Popen",
                "os.system",
                "os.posix_spawn",
                "os.posix_spawnp",
                "os.spawn",
                "os.exec",
            ):
                guard.record_denial(event, str(args[:1]), "process_launch_denied")
                raise PermissionError(
                    f"Sandbox audit fence: process launch event '{event}' denied ({args[:1]})"
                )

            # 2. Sockets, external connections, DNS, send, bind
            if event in (
                "socket.connect",
                "socket.bind",
                "socket.sendto",
                "socket.sendmsg",
                "socket.getaddrinfo",
                "socket.gethostbyname",
                "socket.gethostbyname_ex",
                "socket.gethostbyaddr",
                "socket.getnameinfo",
                "urllib.request",
                "http.client.connect",
                "http.client.send",
            ):
                guard.record_denial(event, str(args[:2]), "network_denied")
                raise PermissionError(
                    f"Sandbox audit fence: network/socket event '{event}' denied ({args[:2]})"
                )

            # 3. File write: open
            if event == "open":
                path_or_fd = args[0]
                mode = args[1] if len(args) > 1 else None
                flags = args[2] if len(args) > 2 else None
                is_write = False
                if isinstance(mode, str) and any(c in mode for c in "wax+"):
                    is_write = True
                if isinstance(flags, int):
                    acc = flags & os.O_ACCMODE
                    if acc in (os.O_WRONLY, os.O_RDWR) or bool(
                        flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND)
                    ):
                        is_write = True
                if is_write:
                    inside, path_repr = _is_path_inside_work(path_or_fd, guard.work_dir)
                    if not inside:
                        guard.record_denial(event, path_repr, "write_outside_work")
                        raise PermissionError(
                            f"Sandbox audit fence: open for write denied outside work: {path_repr}"
                        )

            # 4. File write: os.mkdir
            elif event == "os.mkdir":
                path = args[0]
                dir_fd = args[2] if len(args) > 2 else None
                inside, path_repr = _is_path_inside_work(path, guard.work_dir, dir_fd)
                if not inside:
                    guard.record_denial(event, path_repr, "mkdir_outside_work")
                    raise PermissionError(
                        f"Sandbox audit fence: mkdir denied outside work: {path_repr}"
                    )

            # 5. File write: os.rename / os.replace
            elif event in ("os.rename", "os.replace"):
                src, dst = args[0], args[1]
                src_dir_fd = args[2] if len(args) > 2 else None
                dst_dir_fd = args[3] if len(args) > 3 else None
                src_inside, src_repr = _is_path_inside_work(src, guard.work_dir, src_dir_fd)
                dst_inside, dst_repr = _is_path_inside_work(dst, guard.work_dir, dst_dir_fd)
                if not src_inside or not dst_inside:
                    target_repr = f"src={src_repr}, dst={dst_repr}"
                    guard.record_denial(event, target_repr, "rename_outside_work")
                    raise PermissionError(
                        f"Sandbox audit fence: {event} denied outside work: {target_repr}"
                    )

            # 6. File write: os.remove
            elif event == "os.remove":
                path = args[0]
                dir_fd = args[1] if len(args) > 1 else None
                inside, path_repr = _is_path_inside_work(path, guard.work_dir, dir_fd)
                if not inside:
                    guard.record_denial(event, path_repr, "remove_outside_work")
                    raise PermissionError(
                        f"Sandbox audit fence: remove denied outside work: {path_repr}"
                    )

            # 7. File write: os.rmdir
            elif event == "os.rmdir":
                path = args[0]
                dir_fd = args[1] if len(args) > 1 else None
                inside, path_repr = _is_path_inside_work(path, guard.work_dir, dir_fd)
                if not inside:
                    guard.record_denial(event, path_repr, "rmdir_outside_work")
                    raise PermissionError(
                        f"Sandbox audit fence: rmdir denied outside work: {path_repr}"
                    )

            # 8. File write: os.truncate
            elif event == "os.truncate":
                fd_or_path = args[0]
                inside, path_repr = _is_path_inside_work(fd_or_path, guard.work_dir)
                if not inside:
                    guard.record_denial(event, path_repr, "truncate_outside_work")
                    raise PermissionError(
                        f"Sandbox audit fence: truncate denied outside work: {path_repr}"
                    )

            # 9. File write: os.chmod
            elif event == "os.chmod":
                path = args[0]
                dir_fd = args[2] if len(args) > 2 else None
                inside, path_repr = _is_path_inside_work(path, guard.work_dir, dir_fd)
                if not inside:
                    guard.record_denial(event, path_repr, "chmod_outside_work")
                    raise PermissionError(
                        f"Sandbox audit fence: chmod denied outside work: {path_repr}"
                    )

            # 10. File write: os.link
            elif event == "os.link":
                src, dst = args[0], args[1]
                src_dir_fd = args[2] if len(args) > 2 else None
                dst_dir_fd = args[3] if len(args) > 3 else None
                src_inside, src_repr = _is_path_inside_work(src, guard.work_dir, src_dir_fd)
                dst_inside, dst_repr = _is_path_inside_work(dst, guard.work_dir, dst_dir_fd)
                if not src_inside or not dst_inside:
                    target_repr = f"src={src_repr}, dst={dst_repr}"
                    guard.record_denial(event, target_repr, "link_outside_work")
                    raise PermissionError(
                        f"Sandbox audit fence: link denied outside work: {target_repr}"
                    )

            # 11. Symlink: os.symlink
            elif event == "os.symlink":
                src, dst = args[0], args[1]
                dir_fd = args[2] if len(args) > 2 else None
                dst_inside, dst_repr = _is_path_inside_work(dst, guard.work_dir, dir_fd)
                if not dst_inside:
                    guard.record_denial(event, dst_repr, "symlink_dest_outside_work")
                    raise PermissionError(
                        f"Sandbox audit fence: symlink destination denied outside work: {dst_repr}"
                    )
                # Deny symlinks that escape work_dir
                if dir_fd is not None and dir_fd >= 0:
                    try:
                        dst_parent = Path(os.readlink(f"/proc/self/fd/{dir_fd}")) / Path(os.fsdecode(dst)).parent
                    except OSError:
                        dst_parent = guard.work_dir
                else:
                    d_path = Path(os.fsdecode(dst))
                    dst_parent = d_path.parent if d_path.is_absolute() else (Path.cwd() / d_path).parent

                src_path = Path(os.fsdecode(src))
                src_candidate = src_path if src_path.is_absolute() else (dst_parent / src_path)
                src_inside, src_repr = _is_path_inside_work(src_candidate, guard.work_dir)
                if not src_inside:
                    guard.record_denial(event, src_repr, "symlink_escape_denied")
                    raise PermissionError(
                        f"Sandbox audit fence: symlink target outside work denied (escape): {src_repr}"
                    )
        finally:
            _audit_thread_local.in_hook = False

    sys.addaudithook(audit_hook)


def parse_and_resolve_args(
    argv: List[str],
    original_cwd: Path,
    repo_root: Path,
) -> List[str]:
    """Parse caller CLI arguments, reject conflicting overrides, and resolve node paths."""
    if "--" in argv:
        idx = argv.index("--")
        raw_args = argv[idx + 1 :]
    else:
        raw_args = argv

    # Reject conflicting caller output/config overrides
    for a in raw_args:
        if a in ("--basetemp", "--log-file", "-c", "--config-file", "-s", "--capture"):
            raise ValueError(f"Conflicting caller output/config override rejected: {a}")
        if any(
            a.startswith(f"{opt}=")
            for opt in ("--basetemp", "--log-file", "-c", "--config-file", "--capture")
        ):
            raise ValueError(f"Conflicting caller output/config override rejected: {a}")

    # Resolve test node paths preserving :: selectors
    resolved: List[str] = []
    for arg in raw_args:
        if arg.startswith("-"):
            resolved.append(arg)
            continue
        if "::" in arg:
            path_part, selector = arg.split("::", 1)
            suffix = "::" + selector
        else:
            path_part = arg
            suffix = ""

        p = Path(path_part)
        if (original_cwd / p).exists():
            resolved.append(f"{(original_cwd / p).resolve()}{suffix}")
        elif (repo_root / p).exists():
            resolved.append(f"{(repo_root / p).resolve()}{suffix}")
        elif path_part.endswith(".py") or path_part.startswith("tests/"):
            resolved.append(f"{(repo_root / p).resolve()}{suffix}")
        else:
            resolved.append(arg)

    return resolved


def verify_and_report(
    guard: SandboxGuardContext,
    pytest_exit_code: int,
) -> int:
    """Always hash original real paths, record sentinels, write report, and enforce gate exit."""
    ancestor_checks = []
    ancestors_intact = True
    for key, snap in guard.ancestor_records.items():
        unchanged, orig_hash, cur_hash = snap.verify_unchanged()
        if not unchanged:
            ancestors_intact = False
        ancestor_checks.append(
            {
                "path": str(snap.path),
                "canonical_path": str(snap.canonical_path),
                "originally_existed": snap.originally_existed,
                "currently_exists": snap.canonical_path.is_file(),
                "original_sha256": orig_hash,
                "current_sha256": cur_hash,
                "intact": unchanged,
            }
        )

    canary_intact = guard.canary_path.is_file()
    unexpected_denials = [
        dataclasses.asdict(d) for d in guard.unexpected_denials
    ]
    expected_denials = [
        dataclasses.asdict(d) for d in guard.expected_denials
    ]

    safety_ok = ancestors_intact and canary_intact and (len(unexpected_denials) == 0)
    report = {
        "work_dir": str(guard.work_dir),
        "container_dir": str(guard.container_dir),
        "canary_path": str(guard.canary_path),
        "canary_intact": canary_intact,
        "ancestor_checks": ancestor_checks,
        "ancestors_all_intact": ancestors_intact,
        "pytest_exit_code": pytest_exit_code,
        "unexpected_denials": unexpected_denials,
        "expected_denials_count": len(expected_denials),
        "overall_safety_passed": safety_ok,
    }

    report_path = guard.work_dir / "safety_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if not ancestors_intact:
        sys.stderr.write(
            "\n[CRITICAL ERROR] Sandbox isolation failure: ancestor configuration files were mutated!\n"
        )
        return 101
    if not canary_intact:
        sys.stderr.write(
            "\n[CRITICAL ERROR] Sandbox isolation failure: canary sibling was deleted/mutated!\n"
        )
        return 102
    if len(unexpected_denials) > 0:
        sys.stderr.write(
            f"\n[ERROR] Sandbox audit fence had {len(unexpected_denials)} unexpected refusal(s)!\n"
        )
        for ud in unexpected_denials:
            sys.stderr.write(f"  - {ud['event']} on {ud['target']} ({ud['reason']})\n")
        return 103
    return pytest_exit_code


def main() -> int:
    original_cwd = Path.cwd().resolve()
    original_home = Path.home().resolve()
    repo_root = Path(__file__).resolve().parent.parent

    # 1. Parse and resolve caller arguments before changing cwd
    try:
        resolved_caller_args = parse_and_resolve_args(
            sys.argv[1:], original_cwd, repo_root
        )
    except Exception as e:
        sys.stderr.write(f"Argument error: {e}\n")
        return 2

    # 2. Capture ancestor configs before product import/collection
    ancestors = discover_ancestor_configs(original_cwd, original_home, repo_root)

    # 3. Allocate one process-lifetime temporary container with work and canary sibling
    runtime_parent = resolve_runtime_parent(repo_root)
    if runtime_parent is not None:
        container_dir = Path(
            tempfile.mkdtemp(prefix="aiheaddev_test_sandbox_", dir=str(runtime_parent))
        ).resolve()
    else:
        container_dir = Path(
            tempfile.mkdtemp(prefix="aiheaddev_test_sandbox_")
        ).resolve()

    work_dir = (container_dir / "work").resolve()
    canary_dir = (container_dir / "canary").resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    canary_dir.mkdir(parents=True, exist_ok=True)

    canary_path = canary_dir / "canary.txt"
    canary_path.write_text("bootstrap_canary_sentinel_token\n", encoding="utf-8")

    # Precreate valid empty local .herdr-master.toml in work BEFORE product imports
    (work_dir / ".herdr-master.toml").write_text("", encoding="utf-8")

    # Prepare standard subtree paths inside work
    config_dir = work_dir / ".config"
    cache_dir = work_dir / ".cache"
    data_dir = work_dir / ".local" / "share"
    state_dir = work_dir / ".local" / "state"
    tmp_dir = work_dir / "tmp"
    pytest_tmp = work_dir / "pytest-tmp"
    for d in (config_dir, cache_dir, data_dir, state_dir, tmp_dir, pytest_tmp):
        d.mkdir(parents=True, exist_ok=True)

    # Re-target environment to work subtree
    os.environ["HOME"] = str(work_dir)
    os.environ["XDG_CONFIG_HOME"] = str(config_dir)
    os.environ["XDG_CACHE_HOME"] = str(cache_dir)
    os.environ["XDG_DATA_HOME"] = str(data_dir)
    os.environ["XDG_STATE_HOME"] = str(state_dir)
    os.environ["TMPDIR"] = str(tmp_dir)
    os.environ["TMP"] = str(tmp_dir)
    os.environ["TEMP"] = str(tmp_dir)
    os.chdir(str(work_dir))
    tempfile.tempdir = None

    # Scrub HERDR_* and HERDM_* environment variables
    for k in list(os.environ.keys()):
        if k.startswith(("HERDR_", "HERDM_")):
            del os.environ[k]

    # Disable bytecode writes & unrelated plugin autoload
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    # Ensure repo worktree src is first on sys.path
    if str(repo_root / "src") not in sys.path:
        sys.path.insert(0, str(repo_root / "src"))
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    # 4. Install live guard context & Python audit fence BEFORE importing pytest
    guard = SandboxGuardContext(
        container_dir=container_dir,
        work_dir=work_dir,
        canary_path=canary_path,
        repo_root=repo_root,
        ancestor_records=ancestors,
    )
    _set_active_guard(guard)
    _install_audit_hook(guard)

    # 5. Build full pytest arguments
    log_file = work_dir / "pytest.log"
    mandatory_args = [
        "-p", "pytest_asyncio.plugin",
        "-p", "no:cacheprovider",
        "--capture=sys",
        f"--basetemp={pytest_tmp}",
        f"--log-file={log_file}",
        "-c", str(repo_root / "pyproject.toml"),
    ]
    full_pytest_args = mandatory_args + (
        resolved_caller_args if resolved_caller_args else [str(repo_root / "tests")]
    )

    exit_code = 1
    try:
        import pytest
        exit_code = pytest.main(full_pytest_args)
    except Exception as e:
        sys.stderr.write(f"\nExecution error in pytest: {e}\n")
        exit_code = 1
    finally:
        final_code = verify_and_report(guard, int(exit_code))

    return final_code


if __name__ == "__main__":
    sys.exit(main())
