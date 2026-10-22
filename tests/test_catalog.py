"""Catalog boundaries, configuration selection, and selected-file revisions."""
import hashlib
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import time

import pytest

from cad_viewer.catalog import ProjectCatalog
from cad_viewer.web import BuildFiles


def digest(data):
    return hashlib.sha256(data).hexdigest()


def make_build(workspace, hour, configuration, source=b'baseline', passed=True):
    build_id = f'20261010T{hour:02}0000000000Z-{str(hour) * 12}'
    path = workspace / 'cyclone/out/builds' / build_id
    path.mkdir(parents=True)
    viewer = json.dumps({'components': []}).encode()
    (path / 'viewer.json').write_bytes(viewer)
    manifest = {'build_id': build_id, 'passed': passed, 'units': 'mm',
                'status': 'Prototype', 'parts': {}, 'configuration_hash': configuration,
                'input_file_hash': digest(source), 'checksums': {'viewer.json': digest(viewer)}}
    (path / 'manifest.json').write_text(json.dumps(manifest))
    return path


def make_export(project, output=None, **changes):
    output = output or project / 'out'
    output.mkdir(parents=True, exist_ok=True)
    artifacts = {'drawer.stl': b'print-oriented', 'drawer_model.stl': b'model-space',
                 'drawer.step': b'step-data'}
    for name, data in artifacts.items():
        (output / name).write_bytes(data)
    manifest = {'cad_valid': True, 'solids': 1, 'stl_watertight': True,
                'stl_winding_consistent': True, 'stl_volume_mm3': 123., 'units': 'mm',
                'print_orientation': 'Closed back on Z=0; rotated +90 degrees about X.',
                'files': {name: digest(data) for name, data in artifacts.items()}}
    manifest.update(changes)
    (output / 'build.json').write_text(json.dumps(manifest))
    return output


@pytest.fixture
def workspace(tmp_path):
    make_export(tmp_path / 'vault')
    (tmp_path / 'vault/pyproject.toml').write_text('[project]\nname = "vault"\n')
    return tmp_path


def catalog(workspace, pinned=None):
    return ProjectCatalog(workspace, BuildFiles(workspace / 'cyclone/out/builds', pinned))


def project_files(index, project_id='vault'):
    return next(project['files'] for project in index.metadata()['projects']
                if project['id'] == project_id)


def test_projects_keep_multiple_files_and_duplicate_filenames_distinct(workspace):
    make_export(workspace / 'vault', output=workspace / 'vault/out/tall')
    make_build(workspace, 1, 'configuration-a')
    index = catalog(workspace)
    assert [p['id'] for p in index.metadata()['projects']] == ['cyclone', 'vault']
    files = project_files(index)
    assert len(files) == 4
    assert len({file['id'] for file in files}) == 4
    assert {file['group'] for file in files} == {'Built files', 'Built files / tall'}
    first = files[0]
    entry = index.resolve('vault', first['id'], first['revision'])
    assert entry.source_kind == 'stl'
    assert entry.file_id == entry.component_name == first['id']
    assert 'rotated +90' in entry.print_note
    assert index.download('vault', first['id'], first['revision'], 'stl') == (
        b'print-oriented', 'drawer.stl', 'model/stl')


def test_listing_never_reads_meshes(workspace, monkeypatch):
    read_bytes = Path.read_bytes

    def no_mesh_read(path):
        assert path.suffix.lower() != '.stl', 'Listing should use recorded export checksums'
        return read_bytes(path)

    monkeypatch.setattr(Path, 'read_bytes', no_mesh_read)
    assert len(project_files(catalog(workspace))) == 2


def test_reference_library_is_never_read_or_traversed(workspace, monkeypatch):
    reference = workspace / 'vault/original_files/Drawers/Reference.stl'
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b'reference-only')
    scandir = __import__('os').scandir
    stat = Path.stat
    read_bytes = Path.read_bytes

    def no_reference_scan(path):
        assert 'original_files' not in Path(path).parts
        return scandir(path)

    def no_reference_stat(path, *args, **kwargs):
        assert 'original_files' not in path.parts
        return stat(path, *args, **kwargs)

    def no_reference_read(path):
        assert 'original_files' not in path.parts
        return read_bytes(path)

    monkeypatch.setattr('os.scandir', no_reference_scan)
    monkeypatch.setattr(Path, 'stat', no_reference_stat)
    monkeypatch.setattr(Path, 'read_bytes', no_reference_read)
    files = project_files(catalog(workspace))
    assert {file['name'] for file in files} == {'drawer.stl', 'drawer_model.stl'}


@pytest.mark.parametrize('marker', ['pyproject.toml', 'out'])
def test_project_without_completed_exports_remains_visible(tmp_path, marker):
    project = tmp_path / 'vault'
    project.mkdir()
    if marker == 'out':
        (project / marker).mkdir()
        (project / marker / 'incomplete.stl').write_bytes(b'in-progress')
    else:
        (project / marker).write_text('[project]\nname = "vault"\n')
    assert catalog(tmp_path).metadata()['projects'] == [
        {'id': 'vault', 'name': 'Vault', 'files': [], 'default_file': None}]


def test_latest_success_per_configuration_and_historical_revision(workspace):
    old = make_build(workspace, 1, 'configuration-a')
    latest = make_build(workspace, 2, 'configuration-a')
    other = make_build(workspace, 3, 'configuration-b', source=b'small')
    make_build(workspace, 4, 'configuration-a', passed=False)
    presets = workspace / 'cyclone/presets'
    presets.mkdir()
    (presets / 'baseline.toml').write_bytes(b'baseline')
    (presets / 'small.toml').write_bytes(b'small')
    index = catalog(workspace)
    files = project_files(index, 'cyclone')
    assert {file['revision'] for file in files} == {latest.name, other.name}
    assert {file['name'] for file in files} == {'Baseline', 'Small'}
    baseline = next(file for file in files if file['name'] == 'Baseline')
    entry = index.resolve('cyclone', baseline['id'], old.name)
    assert entry.source_kind == 'cyclone' and entry.path == old
    assert entry.build_id == old.name
    pinned = project_files(catalog(workspace, old), 'cyclone')
    assert {file['revision'] for file in pinned} == {old.name, other.name}
    project = catalog(workspace, old).metadata()['projects'][0]
    assert project['default_file'] == baseline['id']


def test_rebuild_cannot_silently_change_selected_download(workspace):
    index = catalog(workspace)
    file = project_files(index)[0]
    entry = index.resolve('vault', file['id'], file['revision'])
    entry.path.write_bytes(b'changed-mesh')
    with pytest.raises(ValueError, match='changed'):
        entry.read_bytes()
    with pytest.raises(ValueError, match='checksum'):
        index.download('vault', file['id'], file['revision'], 'stl')
    manifest_path = entry.path.parent / 'build.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['files'][entry.path.name] = digest(b'changed-mesh')
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='changed'):
        index.download('vault', file['id'], file['revision'], 'stl')
    replacement = project_files(index)[0]
    assert replacement['id'] == file['id']
    assert replacement['revision'] != file['revision']
    assert index.download('vault', file['id'], replacement['revision'], 'stl')[0] == b'changed-mesh'


def test_export_change_during_read_is_rejected(workspace, monkeypatch):
    index = catalog(workspace)
    file = project_files(index)[0]
    entry = index.resolve('vault', file['id'])
    read_bytes = Path.read_bytes

    def changing_read(path):
        data = read_bytes(path)
        path.write_bytes(b'changed-after-reading')
        return data

    monkeypatch.setattr(Path, 'read_bytes', changing_read)
    with pytest.raises(ValueError, match='while it was being read'):
        entry.read_bytes()


def test_only_declared_confined_exports_are_discovered(workspace):
    outside = workspace / 'private.stl'
    outside.write_bytes(b'private')
    output = workspace / 'vault/out'
    (output / 'escape.stl').symlink_to(outside)
    (output / 'directory-link').symlink_to(workspace, target_is_directory=True)
    (workspace / 'vault/unlisted.stl').write_bytes(b'unlisted')
    (output / 'unlisted.stl').write_bytes(b'unlisted')
    manifest_path = output / 'build.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['files'].update({'escape.stl': digest(b'private'),
                              '../../private.stl': digest(b'private')})
    manifest_path.write_text(json.dumps(manifest))
    other = workspace / 'other-project'
    other.mkdir()
    (other / 'out').symlink_to(output, target_is_directory=True)
    files = project_files(catalog(workspace))
    assert len(files) == 2
    assert {file['name'] for file in files} == {'drawer.stl', 'drawer_model.stl'}
    assert all(project['id'] != 'other-project' for project in catalog(workspace).metadata()['projects'])


def test_symlink_swap_after_selection_is_rejected(workspace):
    index = catalog(workspace)
    file = project_files(index)[0]
    entry = index.resolve('vault', file['id'])
    secret = workspace / 'secret.stl'
    secret.write_bytes(b'secret')
    entry.path.unlink()
    entry.path.symlink_to(secret)
    with pytest.raises(FileNotFoundError):
        entry.read_bytes()


@pytest.mark.parametrize('project_id,file_id,revision,format', [
    ('../vault', 'unknown', 'revision', 'stl'),
    ('vault', '../../private.stl', 'revision', 'stl'),
    ('vault', 'unknown', 'revision', 'step'),
    ('vault', 'unknown', '', 'stl'),
])
def test_requests_never_resolve_paths(workspace, project_id, file_id, revision, format):
    with pytest.raises(FileNotFoundError):
        catalog(workspace).download(project_id, file_id, revision, format)


def test_completed_exports_keep_model_and_print_orientation_separate(workspace):
    index = catalog(workspace)
    files = project_files(index)
    assert len(files) == 2
    built = [index.resolve('vault', file['id']) for file in files if file['group'] == 'Built files']
    assert {entry.name for entry in built} == {'drawer.stl', 'drawer_model.stl'}
    printable = next(entry for entry in built if entry.name == 'drawer.stl')
    model = next(entry for entry in built if entry.name == 'drawer_model.stl')
    assert printable.revision == digest(b'print-oriented')
    assert 'rotated +90' in printable.print_note
    assert 'Model-space' in model.print_note
    assert printable.read_bytes() == b'print-oriented'


def test_nested_configurations_keep_same_named_exports_distinct(workspace):
    nested = workspace / 'vault/out/configurations/tall'
    make_export(workspace / 'vault', output=nested)
    hidden = workspace / 'vault/out/.staging'
    make_export(hidden)
    index = catalog(workspace)
    files = project_files(index)
    assert len(files) == 4
    built = [file for file in files if file['name'] == 'drawer.stl']
    assert len({file['id'] for file in built}) == 2
    assert {file['group'] for file in built} == {'Built files', 'Built files / configurations/tall'}
    assert all(index.download('vault', file['id'], file['revision'], 'stl')[0] == b'print-oriented'
               for file in built)


def test_concurrent_requests_observe_complete_catalog_snapshots(workspace, monkeypatch):
    index = catalog(workspace)
    file = project_files(index)[0]
    scan = index._scan
    start = Barrier(4)
    active = maximum = 0

    def slow_scan():
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            time.sleep(.01)
            return scan()
        finally:
            active -= 1

    def request(number):
        start.wait(timeout=5)
        if number % 2:
            return index.resolve('vault', file['id']).id == file['id']
        return len(index.metadata()['projects']) == 1

    monkeypatch.setattr(index, '_scan', slow_scan)
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert all(executor.map(request, range(4)))
    assert maximum == 1


@pytest.mark.parametrize('failure', [
    {'cad_valid': False}, {'solids': 0}, {'stl_watertight': False},
    {'stl_winding_consistent': False}, {'stl_volume_mm3': -1},
])
def test_failed_exports_leave_project_visible_and_empty(workspace, failure):
    make_export(workspace / 'vault', **failure)
    assert project_files(catalog(workspace)) == []


def test_export_checksums_and_manifest_confinement_are_enforced(workspace):
    output = make_export(workspace / 'vault')
    index = catalog(workspace)
    file = next(file for file in project_files(index) if file['name'] == 'drawer.stl')
    (output / 'drawer.stl').write_bytes(b'corrupt-export')
    with pytest.raises(ValueError, match='checksum'):
        index.download('vault', file['id'], file['revision'], 'stl')
    manifest = output / 'build.json'
    external = workspace / 'outside-build.json'
    external.write_bytes(manifest.read_bytes())
    manifest.unlink()
    manifest.symlink_to(external)
    assert project_files(index) == []
