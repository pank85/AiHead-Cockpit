"""Pytest conftest enforcing hardened test fence and isolation contract.

Fails closed before test collection if the ui_sandbox entrypoint context is absent.
Precreates default .herdr-master.toml configuration via normal serializer before constructors,
sets cwd there, and verifies discovery resolves to the test-local file.
Provides frozen fixture contracts: ui_project and ui_inherited_project.
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import pytest

from herdr_master.core.config import (
    HerdrMasterConfig,
    find_project_config_path,
    save_project_config,
)
from tests.ui_sandbox import require_guard

# Fail closed before collection if entrypoint context is absent
require_guard()


def pytest_configure(config: pytest.Config) -> None:
    """Verify live sandbox guard is active before pytest runs."""
    require_guard()


@pytest.fixture(autouse=True)
def _isolate_ui_test_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Autouse setup creating default project config at tmp_path before test constructors.

    Changes cwd there and asserts that normal production discovery resolves to that file.
    """
    require_guard()

    config_path = tmp_path / ".herdr-master.toml"
    default_cfg = HerdrMasterConfig()
    save_project_config(default_cfg, path=config_path)

    monkeypatch.chdir(tmp_path)

    discovered_tmp = find_project_config_path(tmp_path)
    assert discovered_tmp is not None and discovered_tmp.resolve() == config_path.resolve(), (
        f"Discovery for tmp_path resolved to {discovered_tmp}, expected {config_path}"
    )

    discovered_cwd = find_project_config_path()
    assert discovered_cwd is not None and discovered_cwd.resolve() == config_path.resolve(), (
        f"Discovery for cwd resolved to {discovered_cwd}, expected {config_path}"
    )

    yield


@pytest.fixture
def ui_project(tmp_path: Path) -> Path:
    """Frozen fixture contract: returns seeded tmp_path with .herdr-master.toml."""
    config_path = tmp_path / ".herdr-master.toml"
    if not config_path.exists():
        save_project_config(HerdrMasterConfig(), path=config_path)
    return tmp_path


@pytest.fixture
def ui_inherited_project(tmp_path: Path) -> Tuple[Path, Path, Path]:
    """Frozen fixture contract: returns (parent, child, config_path) inside work."""
    parent = tmp_path / "inherited_parent"
    child = parent / "inherited_child"
    parent.mkdir(parents=True, exist_ok=True)
    child.mkdir(parents=True, exist_ok=True)

    config_path = parent / ".herdr-master.toml"
    save_project_config(HerdrMasterConfig(), path=config_path)

    discovered = find_project_config_path(child)
    assert discovered is not None and discovered.resolve() == config_path.resolve(), (
        f"Discovery for child resolved to {discovered}, expected {config_path}"
    )

    return (parent, child, config_path)
