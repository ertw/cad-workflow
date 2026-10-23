import json
from pathlib import Path
import sys
from threading import RLock
from types import SimpleNamespace

import pytest
from cad_viewer.build_actions import BuildActions
from cad_build import run_build, verify_files
from test_declarations import declare_vault, declare_cyclone


def test_shared_pipeline_publishes_metadata_and_aliases(tmp_path):
    def generate(state):
        (state.stage / 'model.step').write_text('test')
        return {'passed': True}, {'components': []}
    result = run_build(tmp_path, 'config', generate, parameters={'size': 2}, aliases=['latest/test'])
    manifest = verify_files(result)
    assert manifest['viewer'] == 'viewer.json'
    assert json.loads((result / 'resolved-parameters.json').read_text()) == {'size': 2}
    assert (tmp_path / 'out/latest/test').resolve() == result
    with pytest.raises(ValueError, match='validation'):
        run_build(tmp_path, 'config', lambda s: ({'passed': False}, {}), parameters={})
    assert (tmp_path / 'out/latest-successful').resolve() == result


@pytest.mark.parametrize('project', ['vault', 'cyclone'])
def test_commands_share_runner_and_preserve_configuration(tmp_path, project):
    root = tmp_path / project
    (root / '.venv/bin').mkdir(parents=True)
    (root / '.venv/bin/python').symlink_to(sys.executable)
    (declare_vault if project == 'vault' else declare_cyclone)(root)
    catalog = SimpleNamespace(_lock=RLock(), _scan=lambda: None, _project_roots={project: root})
    actions = BuildActions(catalog)
    data = {'project': project}
    options = actions.options(data)[2]
    data['configuration'] = options['default']
    _, _, command, env = actions.command(data, tmp_path / 'receipt')
    assert command[2:5] == ['-m', 'cad_build.runner', project]
    assert command[5] == ('S-4-1' if project == 'vault' else 'baseline.toml')
    assert str(root / 'src') in env['PYTHONPATH']
    data['configuration'] = '../escape'
    with pytest.raises(ValueError):
        actions.command(data, tmp_path / 'receipt')


def test_output_stream_and_failure_leave_no_result(tmp_path, monkeypatch):
    actions = BuildActions(None)
    monkeypatch.setattr(actions, 'command', lambda data, receipt: (
        tmp_path, None, [sys.executable, '-u', '-c', 'print("Generating CAD", flush=True); raise SystemExit(2)'], {}))
    messages = []
    with pytest.raises(ValueError, match='exit 2'):
        actions.execute({'project': 'vault'}, messages.append)
    assert 'Generating CAD' in ''.join(m['progress'] for m in messages)
    assert not actions.lock.locked()


def test_disconnected_browser_stops_build_and_releases_lock(tmp_path, monkeypatch):
    actions = BuildActions(None)
    monkeypatch.setattr(actions, 'command', lambda data, receipt: (
        tmp_path, None, [sys.executable, '-u', '-c', 'import time; print("Running"); time.sleep(30)'], {}))
    def emit(message):
        if 'Running' in message['progress']:
            raise RuntimeError('Disconnected')
    with pytest.raises(RuntimeError, match='Disconnected'):
        actions.execute({'project': 'vault'}, emit)
    assert not actions.lock.locked()


def test_success_resolves_only_the_returned_build(tmp_path):
    destination = tmp_path / 'out/builds/build1'
    destination.mkdir(parents=True)
    (destination / 'manifest.json').write_text(json.dumps({'passed': True, 'build_id': 'build1'}))
    receipt = tmp_path / 'receipt'
    receipt.write_text(str(destination))
    target = SimpleNamespace(project_id='vault', build_id='build1', path=destination,
                             id='new', revision='build1')
    unrelated = SimpleNamespace(project_id='vault', build_id='build2', path=destination.parent / 'build2',
                                id='other', revision='build2')
    catalog = SimpleNamespace(_lock=RLock(), _scan=lambda: None, _entries={'new': target, 'other': unrelated})
    result = BuildActions(catalog).result('vault', tmp_path, None, receipt)
    assert result == {'project': 'vault', 'file': 'new', 'revision': 'build1'}
    receipt.write_text('/tmp/outside')
    with pytest.raises(ValueError, match='unexpected'):
        BuildActions(catalog).result('vault', tmp_path, None, receipt)
