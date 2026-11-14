"""Exercise the build hook with injected build tools, never real Cargo."""
import importlib.util
from pathlib import Path
import shutil
import sys
import tarfile
import types
import zipfile

import pytest
from unittest.mock import patch


def load_hook():
    module = types.ModuleType("hatchling.builders.hooks.plugin.interface")
    module.BuildHookInterface = type("BuildHookInterface", (), {})
    spec = importlib.util.spec_from_file_location("test_hatch_build", Path(__file__).resolve().parents[1] / "hatch_build.py")
    result = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {module.__name__: module}):
        spec.loader.exec_module(result)
    return result


def test_wheel_and_editable_hook(tmp_path):
    hook_module = load_hook()
    hook = hook_module.CustomBuildHook()
    hook.root = str(tmp_path)
    hook.target_name = "wheel"
    calls = []
    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        binary = tmp_path / 'build/cargo/release/aihead-cockpit'
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text('binary')
        binary.chmod(0o755)
    with patch.object(hook_module.subprocess, 'run', side_effect=fake_run):
        data = {}
        hook.initialize('standard', data)
        assert data['pure_python'] is False and data['infer_tag'] is True
        assert list(data['force_include'].values()) == ['aiheaddev/_native/aihead-cockpit']
        assert '--locked' in calls[0][0]
        hook.initialize('editable', {})
    assert (tmp_path / 'src/aiheaddev/_native/aihead-cockpit').is_file()


def test_sdist_does_not_build(tmp_path):
    module = load_hook()
    hook = module.CustomBuildHook()
    hook.root, hook.target_name = str(tmp_path), 'sdist'
    with patch.object(module.subprocess, 'run', side_effect=AssertionError('Cargo called')):
        data = {}
        hook.initialize('standard', data)
    assert not data.get('force_include')  # Never bypass sdist selection by forcing directories.


def test_distribution_allowlists_exclude_worktree_data():
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / 'pyproject.toml').read_text())['tool']['hatch']['build']['targets']
    assert set(config['sdist']['include']) == {
        '/pyproject.toml', '/hatch_build.py', '/README.md', '/LICENSE',
        '/src/aiheaddev/**/*.py', '/rust/cockpit/Cargo.toml',
        '/rust/cockpit/Cargo.lock', '/rust/cockpit/src/**/*.rs',
        '/tests/fixtures/rust_bridge_v1.json',
    }
    assert set(config['wheel']['include']) == {'/src/aiheaddev/**/*.py'}
    assert not config['wheel'].get('packages')  # packages bypasses include patterns
    assert {'/.orchestration', '/.herdr-master.toml', '/.venv', '/build'} <= set(config['sdist']['exclude'])


def test_real_hatchling_archives_exclude_synthetic_user_data(tmp_path, monkeypatch):
    pytest.importorskip('hatchling')
    from hatchling.builders.sdist import SdistBuilder
    from hatchling.builders.wheel import WheelBuilder
    root = Path(__file__).resolve().parents[1]
    project, out = tmp_path / 'project', tmp_path / 'out'
    project.mkdir()
    out.mkdir()
    for name in ('pyproject.toml', 'hatch_build.py', 'README.md', 'LICENSE'):
        shutil.copy2(root / name, project / name)
    for name in ('src/aiheaddev', 'rust/cockpit/src'):
        shutil.copytree(root / name, project / name, ignore=shutil.ignore_patterns('__pycache__', '_native'))
    for name in ('Cargo.toml', 'Cargo.lock'):
        shutil.copy2(root / 'rust/cockpit' / name, project / 'rust/cockpit' / name)
    fixture = project / 'tests/fixtures/rust_bridge_v1.json'
    fixture.parent.mkdir(parents=True)
    shutil.copy2(root / 'tests/fixtures/rust_bridge_v1.json', fixture)
    for name in ('.herdr-master.toml', '.orchestration/agent/handoff.md',
                 'build/secret', '.venv/secret', 'src/aiheaddev/user-data.toml',
                 'rust/cockpit/src/handoff.md'):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('private-sentinel')
    def fake_cargo(argv, **kwargs):
        binary = project / 'build/cargo/release/aihead-cockpit'
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b'fake-native-binary')
        binary.chmod(0o755)
    monkeypatch.setattr('subprocess.run', fake_cargo)
    sdist = next(SdistBuilder(str(project)).build(directory=str(out)))
    wheel = next(WheelBuilder(str(project)).build(directory=str(out)))
    with tarfile.open(sdist) as archive:
        source_names = archive.getnames()
        assert any(n.endswith('/rust/cockpit/Cargo.lock') for n in source_names)
        assert any(n.endswith('/src/aiheaddev/cli.py') for n in source_names)
        assert all(b'private-sentinel' not in archive.extractfile(m).read() for m in archive.getmembers() if m.isfile())
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
        assert 'aiheaddev/cli.py' in wheel_names
        assert 'aiheaddev/_native/aihead-cockpit' in wheel_names
        assert all(b'private-sentinel' not in archive.read(n) for n in wheel_names)
    for names in (source_names, wheel_names):
        assert all(not any(part in n.split('/') for part in ('.orchestration', '.herdr-master.toml', '.venv')) for n in names)
