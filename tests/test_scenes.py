"""Saved-file previews keep geometry and cached browser selections independent."""
from dataclasses import replace
import base64
import hashlib
import json

import numpy as np
import pytest
import trimesh

from cad_viewer.catalog import CatalogEntry
from cad_viewer import scenes


def entry_for(tmp_path, mesh, name='mesh'):
    path = tmp_path / f'{name}.stl'
    data = mesh.export(file_type='stl')
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    return CatalogEntry('vault', name, name.title(), 'Parts', 'stl', digest,
                        path, tmp_path, checksum=digest)


def array(value):
    return np.frombuffer(base64.b64decode(value['buffer']), dtype=value['dtype'])


def test_mesh_scene_has_exact_geometry_and_safe_component_name(tmp_path):
    entry = entry_for(tmp_path, trimesh.creation.box(extents=(12, 20, 30)), 'safe-id')
    message = json.loads(scenes.scene_for(entry))
    shape = message['data']['shapes']['parts'][0]
    assert shape['id'] == '/Group/safe-id'
    assert shape['name'] == 'safe-id'
    assert message['project_id'] == 'vault'
    assert message['file_id'] == 'safe-id'
    assert message['revision'] == entry.revision
    instance = message['data']['instances'][0]
    vertices = array(instance['vertices']).reshape(-1, 3)
    assert np.ptp(vertices, axis=0) == pytest.approx((12, 20, 30))
    assert len(array(instance['triangles'])) == 36
    # Six coplanar diagonals are omitted; the twelve actual box edges remain.
    assert len(array(instance['edges'])) == 12 * 6
    assert shape['state'] == [1, 1]
    assert np.isfinite(array(instance['normals'])).all()
    normals = array(instance['normals']).reshape(-1, 3, 3)[:, 0]
    centers = vertices.reshape(-1, 3, 3).mean(axis=1)
    assert (np.sum(normals * centers, axis=1) > 0).all()
    assert message['preview'] == {'simplified': False, 'source_triangles': 12, 'triangles': 12}
    assert message['config']['resetCamera'] == 'reset'
    assert entry.read_bytes() == entry.path.read_bytes()


def test_large_mesh_is_reduced_only_for_display(tmp_path, monkeypatch):
    monkeypatch.setattr(scenes, 'MAX_PREVIEW_TRIANGLES', 500)
    entry = entry_for(tmp_path, trimesh.creation.icosphere(subdivisions=3), 'sphere')
    original = entry.path.read_bytes()
    message = json.loads(scenes.SceneCache().scene(entry))
    assert message['preview']['simplified'] is True
    assert message['preview']['source_triangles'] == 1280
    assert 0 < message['preview']['triangles'] <= 500
    vertices = array(message['data']['instances'][0]['vertices']).reshape(-1, 3)
    assert np.ptp(vertices, axis=0) == pytest.approx((2, 2, 2), abs=.2)
    assert entry.read_bytes() == original


def test_non_finite_mesh_rejected():
    triangles = np.zeros((1, 3, 3), dtype=np.float32)
    triangles[0, 0, 0] = np.nan
    with pytest.raises(ValueError, match='non-finite'):
        scenes._mesh_bounds(triangles)


def test_cache_keeps_selections_and_revisions_independent(tmp_path, monkeypatch):
    first = entry_for(tmp_path, trimesh.creation.box(), 'first')
    second = replace(first, id='second')
    new_revision = replace(first, revision='next')
    renders = []

    def render(entry):
        renders.append((entry.file_id, entry.revision))
        return f'{entry.file_id}:{entry.revision}'.encode()

    monkeypatch.setattr(scenes, '_render', render)
    cache = scenes.SceneCache(max_entries=2, max_bytes=200)
    result = cache.scene(first)
    assert cache.scene(second) != result
    assert cache.scene(first) == result
    assert len(renders) == 2
    assert cache.scene(new_revision) != result
    cache.scene(second)
    assert len(renders) == 4
    assert len(cache._entries) == 2
    assert cache._bytes <= 200


def test_oversize_scene_is_not_retained(tmp_path, monkeypatch):
    entry = entry_for(tmp_path, trimesh.creation.box())
    monkeypatch.setattr(scenes, '_render', lambda entry: b'12345')
    cache = scenes.SceneCache(max_bytes=4)
    assert cache.scene(entry) == b'12345'
    assert not cache._entries
    assert cache._bytes == 0


def test_cad_failure_is_reported_as_invalid_scene(tmp_path, monkeypatch):
    entry = entry_for(tmp_path, trimesh.creation.box())

    def fail(entry):
        raise RuntimeError('Kernel could not import saved geometry')

    monkeypatch.setattr(scenes, '_render', fail)
    cache = scenes.SceneCache()
    with pytest.raises(ValueError, match='geometry is unavailable or invalid'):
        cache.scene(entry)
    assert not cache._entries


def test_cad_conversion_uses_local_transport(tmp_path, monkeypatch):
    from build123d import Box
    from cad_viewer import assembly
    monkeypatch.setattr(assembly, 'viewer_assembly', lambda path: (Box(2, 3, 4), 'assembly'))
    entry = CatalogEntry('cyclone', 'baseline', 'Baseline', 'Configurations', 'assembly',
                         'build-id', tmp_path, tmp_path, build_id='build-id')
    message = json.loads(scenes.SceneCache().scene(entry))
    assert message['build_id'] == 'build-id'
    assert message['count'] == 1
    assert message['data']['shapes']['bb']['xmax'] == pytest.approx(1)
    assert message['config']['resetCamera'] == 'reset'
