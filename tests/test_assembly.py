"""Saved assemblies use complete, checksummed artifacts and no project code."""
import hashlib
import json
from pathlib import Path

import pytest
from build123d import Box, Pos, export_step

from cad_viewer.assembly import viewer_assembly


@pytest.fixture
def saved_assembly(tmp_path):
    path = tmp_path / '20261010T120000000000Z-aaaaaaaaaaaa'
    (path / 'parts').mkdir(parents=True)
    export_step(Pos(10, 0, 0) * Box(2, 4, 6), path / 'parts/shell.step')
    export_step(Pos(-10, 0, 0) * Box(4, 2, 1), path / 'parts/gasket.step')
    (path / 'viewer.json').write_text(json.dumps({
        'build_id': path.name,
        'components': [{'name': name, 'kind': kind, 'step': f'parts/{name}.step'}
                       for name, kind in [('shell', 'printed'), ('gasket', 'gasket')]],
    }))
    manifest = {'build_id': path.name, 'passed': True,
                'parts': {'shell': {}, 'gasket': {}},
                'checksums': {file.relative_to(path).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
                              for file in path.rglob('*') if file.is_file()}}
    (path / 'manifest.json').write_text(json.dumps(manifest))
    return path


def test_saved_assembly_preserves_parts_and_assembly_coordinates(saved_assembly):
    shape, label = viewer_assembly(saved_assembly)
    assert label == saved_assembly.name == shape.label
    assert [part.label for part in shape.children] == ['shell', 'gasket']
    assert shape.volume == pytest.approx(56)
    assert shape.children[0].bounding_box().center().X == pytest.approx(10)
    assert shape.children[1].bounding_box().center().X == pytest.approx(-10)


def test_part_selection_recenters_and_cutaway_removes_positive_y(saved_assembly):
    shape, _ = viewer_assembly(saved_assembly, part_name='shell')
    assert len(shape.children) == 1
    center = shape.bounding_box().center()
    assert (center.X, center.Y, center.Z) == pytest.approx((0, 0, 0))
    cutaway, label = viewer_assembly(saved_assembly, part_name='shell', cutaway=True)
    assert cutaway.volume == pytest.approx(shape.volume / 2)
    assert cutaway.bounding_box().max.Y == pytest.approx(0)
    assert label.endswith(' cutaway')


@pytest.mark.parametrize('relative', ['viewer.json', 'parts/shell.step'])
def test_changed_artifact_is_rejected_before_cad_import(saved_assembly, relative, monkeypatch):
    import build123d
    (saved_assembly / relative).write_bytes(b'changed')
    def unexpected_import(path):
        pytest.fail('Corrupt artifact reached the CAD importer')
    monkeypatch.setattr(build123d, 'import_step', unexpected_import)
    with pytest.raises(ValueError, match='checksum'):
        viewer_assembly(saved_assembly)


def test_incomplete_assembly_is_rejected(saved_assembly):
    manifest = saved_assembly / 'manifest.json'
    data = json.loads(manifest.read_text())
    data['passed'] = False
    manifest.write_text(json.dumps(data))
    with pytest.raises(FileNotFoundError, match='complete'):
        viewer_assembly(saved_assembly)


def test_import_uses_a_private_copy_of_verified_bytes(saved_assembly, monkeypatch):
    import build123d
    original = build123d.import_step
    imported = []
    def record_import(path):
        imported.append(Path(path))
        assert not Path(path).is_relative_to(saved_assembly)
        return original(path)
    monkeypatch.setattr(build123d, 'import_step', record_import)
    viewer_assembly(saved_assembly)
    assert len(imported) == 2
    assert all(not path.exists() for path in imported)


def test_assembly_artifact_symlink_escape_is_rejected(saved_assembly, tmp_path):
    artifact = saved_assembly / 'parts/shell.step'
    outside = tmp_path / 'outside.step'
    outside.write_bytes(artifact.read_bytes())
    artifact.unlink()
    artifact.symlink_to(outside)
    with pytest.raises(FileNotFoundError, match='artifact'):
        viewer_assembly(saved_assembly)
