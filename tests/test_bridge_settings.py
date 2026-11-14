import copy
from pathlib import Path

import pytest
from herdr_master.core.config import HerdrMasterConfig, save_project_config, load_project_config
from aiheaddev.bridge.protocol import BridgeError
from aiheaddev.bridge.settings import SettingsService, catalog_get, folders_list, folders_test


def test_new_project_cancel_and_folders(tmp_path, monkeypatch):
    (tmp_path / '.herdr-master.toml').unlink(missing_ok=True)
    project = tmp_path / '日本語'
    project.mkdir()
    monkeypatch.setattr('aiheaddev.bridge.settings.find_project_config_path', lambda _: None)
    service = SettingsService()
    view = service.project_open(str(project))
    assert view['scope'] == 'new-local'
    assert view['destination'] == str(project / '.herdr-master.toml')
    assert set(view['sections']) == {'projects', 'squad', 'orchestration', 'council', 'ai_head'}
    assert folders_test(str(project))['available']
    assert any(x['name'] == '日本語' for x in folders_list(str(tmp_path))['directories'])
    service.discard(view['token'])
    assert not (project / '.herdr-master.toml').exists()


def test_inherited_cancel_and_external_change(tmp_path):
    root = tmp_path / 'root'; root.mkdir()
    child = root / 'child'; child.mkdir()
    path = root / '.herdr-master.toml'
    save_project_config(HerdrMasterConfig(), path=path)
    before = path.read_bytes()
    service = SettingsService(); view = service.project_open(str(child))
    assert view['scope'] == 'inherited' and view['destination'] == str(path)
    service.discard(view['token']); assert path.read_bytes() == before
    view = service.project_open(str(child)); path.write_bytes(before + b'\n# external\n')
    with pytest.raises(BridgeError, match='changed'):
        service.save(view['token'], {}, revision=0)
    assert path.read_bytes() == before + b'\n# external\n'


def test_conflict_during_open_or_between_checks(tmp_path, monkeypatch):
    import aiheaddev.bridge.settings as settings
    path = tmp_path / '.herdr-master.toml'
    before = path.read_bytes()
    real_load = settings.load_project_config
    def racing_load(*args, **kwargs):
        cfg = real_load(*args, **kwargs)
        path.write_bytes(before + b'\n# during open\n')
        return cfg
    monkeypatch.setattr(settings, 'load_project_config', racing_load)
    with pytest.raises(BridgeError, match='changed while opening'):
        SettingsService().project_open(str(tmp_path))
    monkeypatch.setattr(settings, 'load_project_config', real_load)
    service = SettingsService(); view = service.project_open(str(tmp_path))
    real_validate = service.validate
    def racing_validate(*args, **kwargs):
        result = real_validate(*args, **kwargs)
        path.write_bytes(path.read_bytes() + b'\n# during validation\n')
        return result
    monkeypatch.setattr(service, 'validate', racing_validate)
    with pytest.raises(BridgeError, match='changed'):
        service.save(view['token'], {}, revision=0)
    assert path.read_bytes().endswith(b'# during validation\n')


def test_two_projects_and_validation(tmp_path):
    a = tmp_path / 'a'; a.mkdir()
    b = tmp_path / 'b'; b.mkdir()
    save_project_config(HerdrMasterConfig(), path=a / '.herdr-master.toml')
    save_project_config(HerdrMasterConfig(), path=b / '.herdr-master.toml')
    service = SettingsService(); one = service.project_open(str(a)); two = service.project_open(str(b))
    assert one['token'] != two['token'] and one['destination'] != two['destination']
    sections = copy.deepcopy(one['sections'])
    sections['squad']['scout']['model'] = ' '
    result = service.validate(one['token'], sections, revision=0)
    assert not result['valid'] and 'squad.scout.model' in result['field_errors']
    assert (a / '.herdr-master.toml').exists() and (b / '.herdr-master.toml').exists()


def test_council_workers_and_retry(tmp_path, monkeypatch):
    service = SettingsService(); view = service.project_open(str(tmp_path))
    token = view['token']; sections = copy.deepcopy(view['sections'])
    sections['council']['members'][1]['enabled'] = False
    sections['squad']['worker_pool'][0]['effort'] = 'high'
    result = service.validate(token, sections, revision=0)
    assert 'council' in result['field_errors']
    sections['council']['members'][1]['enabled'] = True
    sections['squad']['worker_pool'][0]['effort'] = None
    sections['ai_head']['soul'] = 'Custom persona'
    sections['projects']['default_folder'] = str(tmp_path)
    result = service.save(token, sections, revision=result['revision'])
    assert result['valid'], result
    cfg = load_project_config(tmp_path / '.herdr-master.toml')
    assert cfg.ai_head.soul == 'Custom persona'
    assert cfg.projects.default_folder == str(tmp_path)


def test_catalog_is_dependency_owned():
    assert catalog_get()['harnesses']


def test_retry_preserves_metadata_and_order(tmp_path, monkeypatch):
    import aiheaddev.bridge.settings as settings
    path = tmp_path / '.herdr-master.toml'
    config = load_project_config(path)
    config.general.default_session = 'retain-this-session'
    config.ai_head.lesson_paths = ['~/untouched-lessons']
    save_project_config(config, path=path)
    before = path.read_bytes()
    service = SettingsService(); view = service.project_open(str(tmp_path))
    sections = copy.deepcopy(view['sections'])
    sections['ai_head']['soul'] = 'edited persona'
    sections['projects']['favorites'] = [{'name': '日本語', 'path': str(tmp_path)}]
    workers = sections['squad']['worker_pool']
    workers.reverse()
    for i, worker in enumerate(workers): worker['order'] = i
    workers[0]['enabled'] = False
    members = sections['council']['members']
    members.reverse()
    for i, member in enumerate(members): member['order'] = i
    real_save = settings.save_project_config
    def fail(*args, **kwargs): raise OSError('synthetic write failure')
    monkeypatch.setattr(settings, 'save_project_config', fail)
    with pytest.raises(BridgeError, match='synthetic write failure') as failure:
        service.save(view['token'], sections, revision=0)
    assert path.read_bytes() == before
    revision = int(failure.value.field_errors['revision'])
    monkeypatch.setattr(settings, 'save_project_config', real_save)
    result = service.save(view['token'], {}, revision=revision)
    assert result['valid'], result
    loaded = load_project_config(path)
    assert loaded.general.default_session == 'retain-this-session'
    assert loaded.ai_head.lesson_paths == ['~/untouched-lessons']
    assert loaded.ai_head.soul == 'edited persona'
    assert loaded.projects.favorites[0].name == '日本語'
    assert [m.label for m in loaded.council.members] == [m['label'] for m in members]
    reopened = service.project_open(str(tmp_path))['sections']['squad']['worker_pool']
    assert [w['label'] for w in reopened] == [w['label'] for w in workers]
    assert reopened[0]['enabled'] is False


def test_worker_effort_and_orchestration_worker_validation(tmp_path):
    service = SettingsService(); view = service.project_open(str(tmp_path))
    sections = copy.deepcopy(view['sections'])
    sections['squad']['worker_pool'][0]['effort'] = 'high'
    result = service.validate(view['token'], sections, revision=0)
    assert 'squad.worker_pool.0.effort' in result['field_errors']
    sections['squad']['worker_pool'][0]['effort'] = None
    sections['orchestration']['worker_models'] = ['model:high']
    result = service.validate(view['token'], sections, revision=result['revision'])
    assert 'orchestration.worker_models' in result['field_errors']


def test_arbitrary_model_and_per_harness_restoration(tmp_path):
    service = SettingsService(); view = service.project_open(str(tmp_path))
    sections = copy.deepcopy(view['sections'])
    sections['squad']['architect']['model'] = 'anthropic/my-custom-model'
    sections['orchestration']['harnesses']['pi'] = {
        'scout_model': 'anthropic/custom-scout', 'architect_model': 'anthropic/custom-architect',
        'reviewer_model': 'anthropic/custom-reviewer', 'worker_models': ['anthropic/custom-worker'],
        'scout_effort': None, 'architect_effort': None, 'reviewer_effort': None,
    }
    result = service.save(view['token'], sections, revision=0)
    assert result['valid'], result
    reopened = service.project_open(str(tmp_path))
    assert reopened['sections']['squad']['architect']['model'] == 'anthropic/my-custom-model'
    assert reopened['sections']['orchestration']['harnesses']['pi']['worker_models'] == ['anthropic/custom-worker']


def test_unknown_toml_data_is_preserved(tmp_path):
    path = tmp_path / '.herdr-master.toml'
    path.write_text(path.read_text() + '\n[custom_extension]\nvalue = "keep me"\n', encoding='utf-8')
    service = SettingsService(); view = service.project_open(str(tmp_path))
    result = service.save(view['token'], {'ai_head': {'soul': 'changed'}}, revision=0)
    assert result['valid']
    assert '[custom_extension]' in path.read_text(encoding='utf-8')
    assert 'keep me' in path.read_text(encoding='utf-8')


def test_switch_harness_restores_and_validates_each_config(tmp_path):
    service = SettingsService(); view = service.project_open(str(tmp_path)); token = view['token']
    agy = view['sections']['orchestration']['scout_model']
    alternate = view['sections']['orchestration']['architect_model']
    result = service.validate(token, {'orchestration': {'scout_model': alternate}}, revision=0)
    assert result['valid'], result
    result = service.validate(token, {'orchestration': {'harness': 'pi'}}, revision=result['revision'])
    assert result['valid'], result
    assert result['sections']['orchestration']['harness'] == 'pi'
    assert result['sections']['orchestration']['worker_models']
    result = service.validate(token, {'orchestration': {'scout_model': 'anthropic/custom-pi'}}, revision=result['revision'])
    assert result['valid'], result
    result = service.validate(token, {'orchestration': {'harness': 'agy'}}, revision=result['revision'])
    assert result['valid'], result
    saved = service.save(token, {}, revision=result['revision'])
    assert saved['valid'], saved
    reopened = service.project_open(str(tmp_path))['sections']['orchestration']
    assert agy and reopened['scout_model'] == alternate
    assert reopened['harnesses']['pi']['scout_model'] == 'anthropic/custom-pi'
    switched = service.validate(token, {'orchestration': {'harness': 'pi'}}, revision=saved['revision'])
    assert switched['valid'], switched
    invalid = service.validate(token, {'orchestration': {'scout_model': '', 'worker_models': []}}, revision=switched['revision'])
    assert not invalid['valid'] and 'orchestration.scout_model' in invalid['field_errors']
    assert 'orchestration.worker_models' in invalid['field_errors']


@pytest.mark.parametrize('field,value', [('scout_model', ''), ('architect_model', ''), ('reviewer_model', ''),
                                       ('worker_models', []), ('worker_models', ['']), ('worker_models', ['foo:high'])])
def test_required_orchestration_fields_never_repaired(tmp_path, field, value):
    service = SettingsService(); view = service.project_open(str(tmp_path))
    result = service.validate(view['token'], {'orchestration': {field: value}}, revision=0)
    assert not result['valid']
    assert any(key.startswith('orchestration.' + field) for key in result['field_errors'])


def test_inactive_harness_route_is_checked(tmp_path):
    service = SettingsService(); view = service.project_open(str(tmp_path))
    result = service.validate(view['token'], {'orchestration': {'harnesses': {'pi': {
        'scout_model': 'anthropic/example', 'architect_model': '', 'reviewer_model': 'anthropic/example',
        'worker_models': ['anthropic/example'], 'scout_effort': None, 'architect_effort': None, 'reviewer_effort': None,
    }}}}, revision=0)
    assert not result['valid'] and 'orchestration.harnesses.pi.architect_model' in result['field_errors']


def test_nested_unknown_toml_retained(tmp_path):
    import tomllib
    path = tmp_path / '.herdr-master.toml'
    save_project_config(HerdrMasterConfig(), path=path)
    text = path.read_text(encoding='utf-8')
    assert '[general]' in text and '[ai_head]' in text
    text = text.replace('[general]', '[general]\ncustom_general = "preserve"', 1)
    text = text.replace('[ai_head]', '[ai_head]\ncustom_persona_data = { x = 3 }', 1)
    text = text.replace('[[council.members]]', '[[council.members]]\ncustom_member = "keep"', 1)
    text = text.replace('[orchestration.harnesses.agy]', '[orchestration.harnesses.agy]\ncustom_harness_data = "keep"', 1)
    path.write_text(text, encoding='utf-8')
    service = SettingsService(); view = service.project_open(str(tmp_path))
    members = copy.deepcopy(view['sections']['council']['members'])
    original_label = members[0]['label']
    members.reverse()
    for i, member in enumerate(members): member['order'] = i
    assert service.save(view['token'], {'ai_head': {'soul': 'edited'}, 'council': {'members': members}}, revision=0)['valid']
    result = tomllib.loads(path.read_text(encoding='utf-8'))
    assert result['general']['custom_general'] == 'preserve'
    assert result['ai_head']['custom_persona_data'] == {'x': 3}
    assert result['orchestration']['harnesses']['agy']['custom_harness_data'] == 'keep'
    assert next(m for m in result['council']['members'] if m['label'] == original_label)['custom_member'] == 'keep'


def test_real_no_ancestor_discovery_in_synthetic_home(tmp_path):
    # The ordinary sandbox may itself be nested under a real ancestor config.
    # A targeted run sets UI_SANDBOX_RUNTIME_PARENT=/tmp; never modify an ancestor.
    home = Path.home()
    if any((parent / '.herdr-master.toml').is_file() for parent in home.parents):
        pytest.skip('requires sandbox runtime with no ancestor project config')
    home_config = home / '.herdr-master.toml'
    local_config = tmp_path / '.herdr-master.toml'
    home_bytes, local_bytes = home_config.read_bytes(), local_config.read_bytes()
    project = tmp_path / 'new-local-東京'; project.mkdir()
    try:
        home_config.unlink(); local_config.unlink()
        from herdr_master.core.config import find_project_config_path
        assert find_project_config_path(project) is None
        service = SettingsService(); view = service.project_open(str(project))
        assert view['scope'] == 'new-local' and view['destination'] == str(project / '.herdr-master.toml')
        assert service.save(view['token'], {}, revision=0)['valid']
        assert (project / '.herdr-master.toml').is_file()
    finally:
        home_config.write_bytes(home_bytes); local_config.write_bytes(local_bytes)


def test_import_does_not_load_textual_or_emit_stdout(monkeypatch, capsys):
    import builtins
    import importlib
    import aiheaddev.bridge.protocol as protocol
    import aiheaddev.bridge.settings as settings
    calls = []
    original = builtins.__import__
    def tracked(name, *args, **kwargs):
        if name == 'textual' or name.startswith('textual.'):
            calls.append(name)
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', tracked)
    importlib.reload(protocol); importlib.reload(settings)
    assert calls == []
    assert capsys.readouterr().out == ''
