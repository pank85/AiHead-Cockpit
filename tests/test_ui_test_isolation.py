"""Safety and isolation verification tests for the hardened test fence.

Verifies:
1. Guard installed once and active.
2. require_guard rejects absent context by direct unit invocation (no child process).
3. Canary write, rename, remove, and symlink escapes outside work are denied.
4. Nonexistent socket and executable attempts are denied BEFORE real I/O.
5. All original ancestor configuration snapshots remain completely unchanged.
6. Safe local and inherited sandbox configuration saves operate cleanly inside work.
7. Local socketpair operations for asyncio remain operational.
8. Thread-safe audit hook reentrancy preventing race-condition bypasses across concurrent threads.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import socket
import subprocess
import threading

import pytest

from herdr_master.core.config import (
    find_project_config_path,
    load_project_config,
    save_project_config,
)
from tests.ui_sandbox import (
    _GUARD_INSTALL_COUNT,
    _set_active_guard,
    get_active_guard,
    require_guard,
)


def test_guard_installed_once():
    """Verify that exactly one live sandbox guard context is installed and active."""
    guard = require_guard()
    assert guard is not None
    assert guard.is_active() is True
    assert guard.work_dir.is_dir()
    assert guard.canary_path.is_file()
    assert guard.canary_path.parent != guard.work_dir
    assert get_active_guard() is guard
    assert _GUARD_INSTALL_COUNT == 1


def test_require_guard_rejects_absent_context():
    """Verify require_guard fails closed when context is absent via direct unit invocation."""
    current_guard = get_active_guard()
    try:
        _set_active_guard(None)
        with pytest.raises(RuntimeError, match="Sandbox guard is not active"):
            require_guard()
    finally:
        _set_active_guard(current_guard)

    # Context restored cleanly
    assert require_guard() is current_guard


def test_canary_operations_and_symlink_escape_denied(tmp_path: Path):
    """Verify outside-work canary write, rename, remove, and symlink escapes are denied."""
    guard = require_guard()
    canary = guard.canary_path
    initial_content = canary.read_text(encoding="utf-8")

    # 1. Deny write (open with 'w' or 'a')
    with guard.expect_denial(), pytest.raises(PermissionError, match="Sandbox audit fence"):
        with open(canary, "w", encoding="utf-8") as f:
            f.write("unauthorized_write")

    with guard.expect_denial(), pytest.raises(PermissionError, match="Sandbox audit fence"):
        with open(canary, "a", encoding="utf-8") as f:
            f.write("unauthorized_append")

    # 2. Deny rename and replace
    renamed_target = canary.parent / "canary_renamed.txt"
    with guard.expect_denial(), pytest.raises(PermissionError, match="Sandbox audit fence"):
        os.rename(canary, renamed_target)

    with guard.expect_denial(), pytest.raises(PermissionError, match="Sandbox audit fence"):
        os.replace(canary, renamed_target)

    # 3. Deny remove and rmdir
    with guard.expect_denial(), pytest.raises(PermissionError, match="Sandbox audit fence"):
        os.remove(canary)

    with guard.expect_denial(), pytest.raises(PermissionError, match="Sandbox audit fence"):
        os.rmdir(canary.parent)

    # 4. Deny symlink escapes pointing outside work
    escape_link_canary = tmp_path / "link_to_canary"
    with guard.expect_denial(), pytest.raises(PermissionError, match="Sandbox audit fence"):
        os.symlink(canary, escape_link_canary)

    escape_link_etc = tmp_path / "link_to_etc"
    with guard.expect_denial(), pytest.raises(PermissionError, match="Sandbox audit fence"):
        os.symlink("/etc", escape_link_etc)

    # Verify canary remains intact and unchanged
    assert canary.is_file()
    assert canary.read_text(encoding="utf-8") == initial_content


def test_nonexistent_sandbox_socket_and_executable_denied_before_real_io():
    """Verify external sockets and executable spawns are denied by audit hook before real I/O."""
    guard = require_guard()

    # 1. External socket connect to nonexistent port (must raise PermissionError, NOT ConnectionRefusedError)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with guard.expect_denial(), pytest.raises(PermissionError, match="Sandbox audit fence") as exc_info:
            sock.connect(("127.0.0.1", 59999))
        assert not isinstance(exc_info.value, ConnectionRefusedError)
    finally:
        sock.close()

    # 2. Socket bind (must raise PermissionError)
    sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with guard.expect_denial(), pytest.raises(PermissionError, match="Sandbox audit fence"):
            sock2.bind(("127.0.0.1", 59998))
    finally:
        sock2.close()

    # 3. Subprocess launch with nonexistent executable (must raise PermissionError, NOT FileNotFoundError)
    with guard.expect_denial(), pytest.raises(PermissionError, match="Sandbox audit fence") as exc_info:
        subprocess.Popen(["/nonexistent_sandbox_binary_boundary_test_54321"])
    assert not isinstance(exc_info.value, FileNotFoundError)

    # 4. os.system command launch
    with guard.expect_denial(), pytest.raises(PermissionError, match="Sandbox audit fence"):
        os.system("echo sandbox_denied")

    # 5. os.posix_spawn launch
    with guard.expect_denial(), pytest.raises(PermissionError, match="Sandbox audit fence") as exc_info:
        os.posix_spawn(
            "/nonexistent_sandbox_binary_boundary_test_54321",
            ["/nonexistent_sandbox_binary_boundary_test_54321"],
            {},
        )
    assert not isinstance(exc_info.value, FileNotFoundError)

    # 6. Verify all captured ancestors remain completely unchanged
    for key, snap in guard.ancestor_records.items():
        unchanged, orig_hash, cur_hash = snap.verify_unchanged()
        assert unchanged is True, f"Ancestor config altered: {snap.path} ({orig_hash} != {cur_hash})"


def test_safe_local_and_inherited_sandbox_saves_work(
    ui_project: Path,
    ui_inherited_project: tuple[Path, Path, Path],
):
    """Verify that local and inherited configuration saves succeed within the sandbox boundary."""
    # 1. Local project configuration save
    assert ui_project.is_dir()
    local_cfg_path = ui_project / ".herdr-master.toml"
    assert local_cfg_path.is_file()

    local_cfg = load_project_config(local_cfg_path)
    local_cfg.orchestration.architect_effort = "xhigh"
    saved_path = save_project_config(local_cfg, path=local_cfg_path)
    assert saved_path.resolve() == local_cfg_path.resolve()

    reloaded_local = load_project_config(local_cfg_path)
    assert reloaded_local.orchestration.architect_effort == "xhigh"

    # 2. Inherited project configuration save
    parent, child, inherited_cfg_path = ui_inherited_project
    assert parent.is_dir() and child.is_dir() and inherited_cfg_path.is_file()

    discovered = find_project_config_path(child)
    assert discovered is not None and discovered.resolve() == inherited_cfg_path.resolve()

    inherited_cfg = load_project_config(inherited_cfg_path)
    inherited_cfg.council.coordinator.effort = "high"
    saved_inherited = save_project_config(inherited_cfg, path=inherited_cfg_path)
    assert saved_inherited.resolve() == inherited_cfg_path.resolve()

    reloaded_inherited = load_project_config(inherited_cfg_path)
    assert reloaded_inherited.council.coordinator.effort.value == "high"

    # Verify no override was created in the child directory
    assert not (child / ".herdr-master.toml").exists()


@pytest.mark.asyncio
async def test_asyncio_local_socketpair_allowed():
    """Verify that asyncio and local socketpair operations remain fully operational under fence."""
    reader, writer = socket.socketpair()

    async def _send_receive():
        writer.sendall(b"socketpair_ping")
        data = reader.recv(1024)
        return data

    try:
        received = await asyncio.wait_for(_send_receive(), timeout=2.0)
        assert received == b"socketpair_ping"
    finally:
        reader.close()
        writer.close()


def test_audit_hook_thread_local_reentrancy():
    """Verify concurrent threads executing simultaneous audited ops are independently protected.

    Asserts that thread-local tracking prevents reentrancy race conditions where one thread
    inside an audit hook could allow another concurrent thread to bypass the audit fence.
    Uses deterministic no-I/O operations intercepted before real OS execution.
    """
    guard = require_guard()
    num_threads = 8
    iterations_per_thread = 20
    barrier = threading.Barrier(num_threads)
    thread_errors: list[Exception] = []

    def worker(worker_id: int):
        try:
            # Synchronize start to force maximum concurrency across threads
            barrier.wait(timeout=5.0)
            for i in range(iterations_per_thread):
                # 1. Subprocess launch intercepted before OS exec
                with guard.expect_denial(), pytest.raises(
                    PermissionError, match="Sandbox audit fence"
                ):
                    subprocess.Popen([f"/nonexistent_reentrancy_race_binary_{worker_id}_{i}"])

                # 2. Socket connect intercepted before OS connect
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    with guard.expect_denial(), pytest.raises(
                        PermissionError, match="Sandbox audit fence"
                    ):
                        sock.connect(("127.0.0.1", 59900 + ((worker_id * 10 + i) % 50)))
                finally:
                    sock.close()
        except Exception as exc:
            thread_errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(t_id,), name=f"guard-worker-{t_id}")
        for t_id in range(num_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
        assert not t.is_alive(), f"Worker thread {t.name} timed out"

    assert not thread_errors, f"Thread errors occurred during reentrancy test: {thread_errors}"
    assert len(guard.unexpected_denials) == 0, "Unexpected denials recorded during multi-threaded test"
