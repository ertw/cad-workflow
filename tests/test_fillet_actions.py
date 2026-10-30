"""Native selection and immutable fillet publication without project constructors."""
import hashlib
import json

from build123d import Box, Pos, Rot, export_step, export_stl, import_step
import pytest

from cad_viewer.catalog import ProjectCatalog
from cad_viewer.fillet_actions import FilletActions
from cad_viewer.scenes import scene_for
from cad_viewer.web import BuildFiles, _file_metadata


@pytest.fixture
def native(tmp_path):
    root = tmp_path / 'vault'
    build_id = '20261010T120000000000Z-aaaaaaaaaaaa'
    path = root / 'out/builds' / build_id
    (path / 'step').mkdir(parents=True)
    (path / 'stl').mkdir()
    shape = Pos(30, 8, 14) * Rot(0, 0, 30) * Box(12, 16, 20)
    export_step(shape, path / 'step/box.step')
    export_stl(shape, path / 'stl/box.stl')
    (root / 'pyproject.toml').write_text('[project]\nname="fixture"\n')
    (path / 'viewer.json').write_text(json.dumps({
        'components': [{'name': 'box', 'kind': 'model', 'step': 'step/box.step'}]}))
    manifest = {'schema': 'cad-build-v1', 'build_id': build_id, 'passed': True,
                'viewer': 'viewer.json', 'units': 'mm', 'status': 'Fixture',
                'parts': {'box': {'kind': 'model', 'print_note': 'Fixture coordinates',
                                  'print_bbox_mm': list(shape.bounding_box().size)}},
                'checksums': {file.relative_to(path).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
                              for file in path.rglob('*') if file.is_file()}}
    (path / 'manifest.json').write_text(json.dumps(manifest))
    (root / 'out/latest-successful').symlink_to(path)
    files = BuildFiles(tmp_path / 'cyclone/out/builds')
    catalog = ProjectCatalog(tmp_path, files)
    entry = catalog.resolve('vault', 'build-' + build_id, build_id)
    model = json.loads(scene_for(entry))
    leaf = model['data']['shapes']
    while leaf.get('parts'):
        leaf = leaf['parts'][0]
    reference = {'project': 'vault', 'file': entry.id, 'revision': build_id,
                 'geometry_path': leaf['id'] + '/edges/edges_0', 'topology': 'edge',
                 'world_bounds': {'min': [999, 999, 999], 'max': [1000, 1000, 1000]}}
    data = {'action': 'preview', 'project': 'vault', 'file': entry.id,
            'revision': build_id, 'reference': reference, 'radius': 1.0}
    return FilletActions(catalog), data, path, shape, files


def test_preview_and_apply_exact_native_selection_preserves_source(native):
    actions, data, source, original, files = native
    before = {file.relative_to(source): file.read_bytes() for file in source.rglob('*') if file.is_file()}
    preview = actions.execute(data)
    assert preview['operation']['edges'] == 1
    assert preview['operation']['base']['revision'] == source.name
    assert set(source.parent.iterdir()) == {source}
    result = actions.execute({'action': 'apply', 'preview_id': preview['preview_id']})
    entry = actions.catalog.resolve(result['project'], result['file'], result['revision'])
    assert entry.path != source
    assert (source.parent.parent / 'latest-successful').resolve() == source
    assert before == {file.relative_to(source): file.read_bytes() for file in source.rglob('*') if file.is_file()}
    edited = import_step(entry.path / 'step/box_fillet.step')
    assert edited.is_valid and len(edited.solids()) == 1
    assert 0 < edited.volume < original.volume
    # Displayed rotation and translation survive native edge selection/export.
    assert tuple(edited.bounding_box().center()) == pytest.approx((30, 8, 14), abs=1)
    assert tuple(edited.bounding_box().size) == pytest.approx(tuple(original.bounding_box().size), abs=1)
    assert edited.bounding_box().min.Z == pytest.approx(4)
    assert edited.bounding_box().max.Z == pytest.approx(24)
    manifest = json.loads((entry.path / 'manifest.json').read_text())
    assert manifest['step_roundtrip']['passed'] and manifest['stl_watertight']
    for relative, digest in manifest['checksums'].items():
        assert hashlib.sha256((entry.path / relative).read_bytes()).hexdigest() == digest
    metadata = _file_metadata(entry, files)
    assert metadata['fillet']['operation'] == preview['operation']
    assert set(metadata['parts'][0]['downloads']) == {'step', 'stl'}
    for fmt in ('step', 'stl'):
        content, filename, mime = actions.catalog.download('vault', entry.id, entry.revision,
                                                         fmt, f'box_fillet.{fmt}')
        assert content == (entry.path / fmt / filename).read_bytes()
        assert mime == 'model/' + fmt
    with pytest.raises(ValueError, match='Preview this fillet'):
        actions.execute({'action': 'apply', 'preview_id': preview['preview_id']})


def test_face_selection_fillets_native_boundary_edges(native):
    actions, data, _, _, _ = native
    data['reference']['geometry_path'] = data['reference']['geometry_path'].replace('/edges/edges_0', '/faces/faces_0')
    data['reference']['topology'] = 'face'
    preview = actions.execute(data)
    assert preview['operation']['edges'] == 4


@pytest.mark.parametrize('change', [
    {'radius': 0}, {'radius': -1}, {'radius': float('nan')}, {'radius': True},
    {'radius': 10001}, {'revision': 'wrong'},
])
def test_bad_parameters_and_revision_rejected(native, change):
    actions, data, source, _, _ = native
    data.update(change)
    with pytest.raises(ValueError):
        actions.execute(data)
    assert set(source.parent.iterdir()) == {source}
    assert not actions.previews


@pytest.mark.parametrize('path,topology', [
    ('/different/edges/edges_0', 'edge'), ('/edges/edges_999999', 'edge'),
    ('/edges/edges_0', 'face'), ('/vertices/vertices_0', 'vertex'),
])
def test_selection_must_exist_in_exact_native_mapping(native, path, topology):
    actions, data, _, _, _ = native
    prefix = data['reference']['geometry_path'].rsplit('/edges/', 1)[0]
    data['reference'].update(geometry_path=prefix + path, topology=topology)
    with pytest.raises(ValueError):
        actions.execute(data)


def test_apply_rechecks_source_even_if_manifest_checksum_was_replaced(native):
    actions, data, source, _, _ = native
    preview = actions.execute(data)
    step = source / 'step/box.step'
    export_step(Box(5, 5, 5), step)
    path = source / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['checksums']['step/box.step'] = hashlib.sha256(step.read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='Source revision changed'):
        actions.execute({'action': 'apply', 'preview_id': preview['preview_id']})
    assert set(source.parent.iterdir()) == {source}


def test_failed_export_never_publishes_or_changes_latest(native, monkeypatch):
    from cad_viewer import fillet_actions
    actions, data, source, _, _ = native
    preview = actions.execute(data)
    def fail(*args):
        raise ValueError('Print mesh must be watertight')
    monkeypatch.setattr(fillet_actions, '_export_checked', fail)
    with pytest.raises(ValueError, match='watertight'):
        actions.execute({'action': 'apply', 'preview_id': preview['preview_id']})
    assert set(source.parent.iterdir()) == {source}
    assert (source.parent.parent / 'latest-successful').resolve() == source
    assert list((source.parent.parent / 'failed').glob('*/failure.json'))


def test_expired_preview_and_shared_build_lock(native):
    actions, data, source, _, _ = native
    preview = actions.execute(data)
    actions.previews[preview['preview_id']]['created'] -= 1801
    with pytest.raises(ValueError, match='expired'):
        actions.execute({'action': 'apply', 'preview_id': preview['preview_id']})
    actions.lock.acquire()
    with pytest.raises(ValueError, match='already running'):
        actions.execute(data)
    actions.lock.release()
    assert set(source.parent.iterdir()) == {source}


def test_stl_is_not_mapped_to_native_edges(native, monkeypatch):
    from dataclasses import replace
    actions, data, _, _, _ = native
    entry = actions.catalog.resolve(data['project'], data['file'], data['revision'])
    monkeypatch.setattr(actions.catalog, 'resolve', lambda *args: replace(entry, kind='stl'))
    with pytest.raises(ValueError, match='native solid'):
        actions.execute(data)
