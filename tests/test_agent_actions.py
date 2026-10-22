import json
from pathlib import Path
import subprocess
from threading import RLock
from types import SimpleNamespace

import pytest
from cad_viewer.agent_actions import AgentActions


@pytest.fixture
def agent(tmp_path):
    calls = []
    def resolve(*args):
        calls.append(args)
        return SimpleNamespace(root=tmp_path / 'out/build')
    catalog = SimpleNamespace(_lock=RLock(), resolve=resolve, _project_roots={'test': tmp_path})
    request = {'project': 'test', 'message': 'Round this edge', 'reference': {
        'schema': 'cad-geometry-reference/v1', 'project': 'test', 'file': 'model',
        'revision': 'revision1', 'topology': 'edge', 'geometry_path': '/part/edges/2',
        'edge_endpoints': [[0, 0, 0], [1, 0, 0]],
    }}
    return AgentActions(catalog), request, tmp_path, calls


def test_agent_receives_reference_in_project_root(agent, monkeypatch):
    actions, request, root, calls = agent
    def run(args, **kw):
        assert kw['cwd'] == root  # Not the immutable build directory.
        assert args[args.index('--sandbox') + 1] == 'workspace-write'
        assert request['message'] in kw['input']
        assert '/part/edges/2' in kw['input']
        assert 'Do not stage, commit, or push' in kw['input']
        Path(args[args.index('--output-last-message') + 1]).write_text('Rounded the edge.')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, 'run', run)
    result = actions.execute(request)
    assert result['reply'] == 'Rounded the edge.'
    assert result['reference'] == request['reference']
    assert calls == [('test', 'model', 'revision1')]


@pytest.mark.parametrize('change', [lambda r: r.update(message=' '),
    lambda r: r.update(project='other'), lambda r: r.update(reference={}),
    lambda r: r['reference'].update(revision=None)])
def test_invalid_request_never_launches(agent, monkeypatch, change):
    actions, request, _, _ = agent
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('Unexpected agent launch'))
    change(request)
    with pytest.raises(ValueError):
        actions.execute(request)


def test_busy_and_timeout_release_lock(agent, monkeypatch):
    actions, request, _, _ = agent
    actions.lock.acquire()
    with pytest.raises(ValueError, match='already running'):
        actions.execute(request)
    actions.lock.release()
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired('codex', 1800)
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(ValueError, match='Files may have changed'):
        actions.execute(request)
    assert not actions.lock.locked()


def test_stale_reference_never_launches(agent, monkeypatch):
    actions, request, _, _ = agent
    def resolve(*args):
        raise ValueError('File changed')
    actions.catalog.resolve = resolve
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('Unexpected agent launch'))
    with pytest.raises(ValueError, match='File changed'):
        actions.execute(request)
