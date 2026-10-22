"""Download routing must never substitute latest or expose unrelated files."""
import hashlib
import json
from types import SimpleNamespace

import pytest
from websockets.datastructures import Headers

from cad_viewer.web import BuildFiles, respond


@pytest.fixture
def builds(tmp_path):
    root = tmp_path / 'builds'
    root.mkdir()
    identifiers = ['20261010T120000000000Z-aaaaaaaaaaaa', '20261010T130000000000Z-bbbbbbbbbbbb']
    for i, build_id in enumerate(identifiers):
        path = root / build_id
        (path / '3mf').mkdir(parents=True)
        (path / '3mf/chamber.3mf').write_bytes(f'revision-{i}'.encode())
        (path / 'viewer.json').write_text(json.dumps({'components': [{'name': 'chamber'}]}))
        manifest = {'build_id': build_id, 'passed': True, 'units': 'mm', 'status': 'Prototype',
                    'parts': {'chamber': {'kind': 'printed', 'print_note': 'Roof on bed',
                                         'print_bbox_mm': {'size': [1, 2, 3]}}},
                    'checksums': {str(f.relative_to(path)): hashlib.sha256(f.read_bytes()).hexdigest()
                                  for f in path.rglob('*') if f.is_file()}}
        (path / 'manifest.json').write_text(json.dumps(manifest))
    (tmp_path / 'latest-successful').symlink_to(root / identifiers[1])
    return BuildFiles(root), root, identifiers


def request(files, path):
    return respond(None, SimpleNamespace(path=path, headers=Headers()), files)


def test_download_remains_pinned_to_displayed_build(builds):
    files, root, identifiers = builds
    metadata = json.loads(request(files, '/api/builds/' + identifiers[0]).body)
    part = metadata['parts'][0]
    assert part['in_assembly'] and part['print_note'] == 'Roof on bed'
    assert 'stl' not in part['downloads']  # never advertise an absent artifact
    response = request(files, part['downloads']['3mf'])
    assert response.status_code == 200
    assert response.body == b'revision-0'
    assert response.headers['Content-Type'] == 'model/3mf'
    assert response.headers['Content-Disposition'] == 'attachment; filename="chamber.3mf"'
    assert request(files, f'/downloads/{identifiers[1]}/3mf/chamber.3mf').body == b'revision-1'


@pytest.mark.parametrize('suffix', ['../manifest.json', '%2e%2e/manifest.json',
                                  '3mf/%2e%2e%2fmanifest.json', '3mf/unknown.3mf',
                                  'source/pyproject.toml', '3mf/chamber.stl'])
def test_download_rejects_unlisted_files_and_traversal(builds, suffix):
    files, _, identifiers = builds
    assert request(files, f'/downloads/{identifiers[0]}/{suffix}').status_code == 404


def test_incomplete_build_and_symlink_escape_are_rejected(builds, tmp_path):
    files, root, identifiers = builds
    path = root / identifiers[0]
    artifact = path / '3mf/chamber.3mf'
    artifact.unlink()
    external = tmp_path / 'outside.3mf'
    external.write_bytes(b'revision-0')
    artifact.symlink_to(external)
    assert request(files, f'/downloads/{identifiers[0]}/3mf/chamber.3mf').status_code == 404
    manifest = json.loads((path / 'manifest.json').read_text())
    manifest['passed'] = False
    (path / 'manifest.json').write_text(json.dumps(manifest))
    assert request(files, '/api/builds/' + identifiers[0]).status_code == 404


def test_corrupt_download_is_rejected(builds):
    files, root, identifiers = builds
    (root / identifiers[0] / '3mf/chamber.3mf').write_bytes(b'corrupt')
    assert request(files, f'/downloads/{identifiers[0]}/3mf/chamber.3mf').status_code == 409


def test_unknown_build_cannot_resolve_latest(builds):
    files, _, _ = builds
    assert request(files, '/api/builds/latest-successful').status_code == 404
    assert request(files, '/api/builds/%2e%2e/latest-successful').status_code == 404


def test_project_scene_keeps_downloads_on_requested_revision(builds, monkeypatch):
    from cad_viewer.catalog import ProjectCatalog
    from cad_viewer import scenes
    files, root, identifiers = builds
    catalog = ProjectCatalog(root.parent, files)
    file = next(item for item in catalog.metadata()['projects'][0]['files']
                if item['revision'] == identifiers[0])
    monkeypatch.setattr(scenes, 'scene_for', lambda entry: json.dumps({
        'type': 'data', 'build_id': entry.build_id,
        'data': {'shapes': {'parts': []}},
    }).encode())
    url = f'/api/projects/cyclone/files/{file["id"]}/scene?revision={identifiers[0]}'
    response = respond(None, SimpleNamespace(path=url, headers=Headers()), files, catalog)
    assert response.status_code == 200
    result = json.loads(response.body)
    assert result['model']['build_id'] == result['metadata']['build_id'] == identifiers[0]
    assert result['metadata']['project_id'] == 'cyclone'
    assert result['metadata']['file_id'] == file['id']
    download = result['metadata']['parts'][0]['downloads']['3mf']
    assert request(files, download).body == b'revision-0'


def test_project_scene_requires_revision_and_rejects_unknown_files(builds):
    from cad_viewer.catalog import ProjectCatalog
    files, root, _ = builds
    catalog = ProjectCatalog(root.parent, files)
    file = catalog.metadata()['projects'][0]['files'][0]
    base = f'/api/projects/cyclone/files/{file["id"]}/scene'
    for url, status in [(base, 409), (base + '?revision=missing', 409),
                        ('/api/projects/vault/files/missing/scene?revision=missing', 404),
                        ('/api/projects/cyclone/files/%2e%2e/scene?revision=missing', 404)]:
        response = respond(None, SimpleNamespace(path=url, headers=Headers()), files, catalog)
        assert response.status_code == status


def test_manifest_symlink_escape_is_rejected(builds, tmp_path):
    files, root, identifiers = builds
    manifest = root / identifiers[0] / 'manifest.json'
    outside = tmp_path / 'outside.json'
    outside.write_bytes(manifest.read_bytes())
    manifest.unlink()
    manifest.symlink_to(outside)
    assert request(files, '/api/builds/' + identifiers[0]).status_code == 404


def test_current_project_working_tree(tmp_path):
    import subprocess
    from cad_viewer.catalog import ProjectCatalog

    project = tmp_path / 'example'
    project.mkdir()
    (project / 'pyproject.toml').write_text('')
    files = BuildFiles(tmp_path / 'absent')
    catalog = ProjectCatalog(tmp_path, files)

    def status(project_id='example'):
        return respond(None, SimpleNamespace(
            path=f'/api/projects/{project_id}/working-tree', headers=Headers()), files, catalog)

    def git(*args):
        subprocess.run(['git', '-C', str(project), *args], check=True, capture_output=True)

    assert json.loads(status().body)['state'] == 'unavailable'
    assert status('unknown').status_code == 404
    git('init')
    assert json.loads(status().body)['state'] == 'dirty'  # untracked
    git('add', '.')
    assert json.loads(status().body)['state'] == 'dirty'  # staged, unborn HEAD
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-m', 'Initial')
    assert json.loads(status().body)['state'] == 'clean'
    (project / 'pyproject.toml').write_text('# modified')
    assert json.loads(status().body)['state'] == 'dirty'
    git('checkout', '--', 'pyproject.toml')
    (project / '.git/info/exclude').write_text('ignored\n')
    (project / 'ignored').write_text('ignored')
    assert json.loads(status().body)['state'] == 'clean'
    (project / 'pyproject.toml').unlink()
    assert json.loads(status().body)['state'] == 'dirty'
    assert status().headers['Cache-Control'] == 'no-store'
