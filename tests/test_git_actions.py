import subprocess
from pathlib import Path
from types import SimpleNamespace
from threading import RLock

import pytest
from cad_viewer.git_actions import GitActions


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        return subprocess.run(['git', '-C', str(tmp_path), *args], check=True, capture_output=True).stdout
    git('init')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.com')
    catalog = SimpleNamespace(_lock=RLock(), _scan=lambda: None, _project_roots={'test': tmp_path})
    actions = GitActions(catalog)
    return tmp_path, git, lambda action, **kw: actions.execute({'project': 'test', 'action': action, **kw})


def test_selected_files_and_edited_message(repo):
    root, git, run = repo
    (root / 'one.txt').write_text('one')
    (root / 'two.txt').write_text('two')
    state = run('stage', paths=['one.txt'])
    assert state['has_staged']
    result = run('commit', snapshot=state['snapshot'], message='My edited message')
    assert result['commit']
    assert git('log', '-1', '--format=%B').strip() == b'My edited message'
    assert git('ls-files').strip() == b'one.txt'
    assert result['files'][0]['path'] == 'two.txt'


def test_stale_index_and_bad_paths(repo):
    root, git, run = repo
    (root / 'file').write_text('one')
    with pytest.raises(ValueError):
        run('stage', paths=['../elsewhere'])
    state = run('stage', paths=['file'])
    (root / 'file').write_text('two')
    git('add', 'file')
    with pytest.raises(ValueError, match='changed'):
        run('commit', snapshot=state['snapshot'], message='stale')
    with pytest.raises(ValueError, match='message'):
        run('commit', snapshot=run('status')['snapshot'], message='  ')


def test_deletion_and_literal_filename(repo):
    root, git, run = repo
    (root / ':odd.txt').write_text('test')
    state = run('stage', paths=[':odd.txt'])
    run('commit', snapshot=state['snapshot'], message='Add')
    (root / ':odd.txt').unlink()
    state = run('stage', paths=[':odd.txt'])
    run('commit', snapshot=state['snapshot'], message='Delete')
    assert not run('status')['files']


def test_ai_draft(repo, monkeypatch):
    root, git, run = repo
    (root / 'file').write_text('one')
    state = run('stage', paths=['file'])
    original = subprocess.run
    def fake(args, **kwargs):
        if args[0] != 'codex':
            return original(args, **kwargs)
        assert 'read-only' in args
        assert '+one' in kwargs['input']
        Path(args[args.index('--output-last-message') + 1]).write_text('Add one')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, 'run', fake)
    assert run('message', snapshot=state['snapshot'])['message'] == 'Add one'
