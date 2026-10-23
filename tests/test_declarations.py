"""Catalog identity must survive missing output and never follow arbitrary inputs."""
import hashlib
import json
from pathlib import Path
import shutil
import sys

import pytest

from cad_catalog import CatalogError, load, validate_upstream, validator
from cad_viewer.catalog import ProjectCatalog
from cad_viewer.web import BuildFiles
from cad_viewer.build_actions import BuildActions
from cad_build import catalog_target, run_build, source_hashes


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def declare_vault(root):
    parts = {
        's4_module_1f': {'type': 'build123d', 'desc': 'S4 Module 1F', 'path': 'catalog/module.py',
                         'parameters': {key: {'type': kind, 'default': value}
                                        for key, kind, value in [('family', 'string', 'S'),
                                                                ('depth_units', 'int', 4),
                                                                ('height_units', 'int', 1)]}},
        's1f_facade': {'type': 'build123d', 'desc': 'S1F Facade', 'path': 'catalog/facade.py'},
    }
    write(root / 'partcad.json', {'parts': parts})
    write(root / 'cad-builds.json', {'version': 1, 'default': 's4_module_1f', 'targets': {
        's4_module_1f': {'object': 'parts/s4_module_1f', 'adapter': 'vault-module'},
        's1f_facade': {'object': 'parts/s1f_facade', 'adapter': 'vault-facade'},
    }})
    for name in ('module', 'facade'):
        (root / 'catalog').mkdir(exist_ok=True)
        (root / 'catalog' / f'{name}.py').write_text('# geometry wrapper\n')
    return root


def declare_cyclone(root):
    write(root / 'partcad.json', {'parts': {'chamber': {
        'type': 'build123d', 'desc': 'Chamber', 'path': 'component.py', 'parameters': {
            'component': {'type': 'string', 'default': 'chamber'},
            'preset': {'type': 'string', 'default': 'baseline.toml'}}}}, 'assemblies': {
        'baseline': {'type': 'assy', 'path': 'baseline.assy.json', 'desc': 'Baseline assembly'}}})
    (root / 'component.py').write_text('# wrapper\n')
    (root / 'presets').mkdir(exist_ok=True)
    (root / 'presets/baseline.toml').write_text('units = "mm"\n')
    write(root / 'baseline.assy.json', {'links': [{'name': 'chamber', 'part': 'chamber'}]})
    write(root / 'cad-builds.json', {'version': 1, 'default': 'baseline', 'targets': {
        'baseline': {'object': 'assemblies/baseline', 'adapter': 'cyclone-preset',
                     'preset': 'presets/baseline.toml'}}})
    return root


def index(workspace):
    return ProjectCatalog(workspace, BuildFiles(workspace / 'cyclone/out/builds'))


def test_vendored_schemas_are_valid_and_resolve_locally():
    for name in ('partcad.json', 'assy.json'):
        check = validator(name)
        check.check_schema(check.schema)
    validate_upstream({'links': [{'part': 'one', 'name': 'one'}]}, 'assy.json', 'example')


def test_declared_parts_and_options_exist_before_first_build(tmp_path):
    root = declare_vault(tmp_path / 'vault')
    cat = index(tmp_path)
    project = cat.metadata()['projects'][0]
    assert project['files'] == [] and project['default_file'] is None
    assert [part['name'] for part in project['latest_parts']] == ['s4_module_1f', 's1f_facade']
    assert {part['state'] for part in project['latest_parts']} == {'missing'}
    assert {part['review_state'] for part in project['latest_parts']} == {'unrecorded'}
    actions = BuildActions(cat)
    options = actions.options({'project': 'vault'})[2]
    assert [item['id'] for item in options['options']] == ['s4_module_1f', 's1f_facade']
    (root / '.venv/bin').mkdir(parents=True)
    (root / '.venv/bin/python').symlink_to(sys.executable)
    command = actions.command({'project': 'vault', 'configuration': 's1f_facade'}, tmp_path / 'receipt')[2]
    assert command[5] == 's1f_facade'
    assert command[-4:] == ['--catalog-target', 's1f_facade', '--catalog-object', 'parts/s1f_facade']
    with pytest.raises(ValueError, match='configuration'):
        actions.command({'project': 'vault', 'configuration': 'XL-4-1'}, tmp_path / 'receipt')


def saved_export(root, part, hour, family='S', target=None):
    stamp = f'20261022T{hour:02d}0000000000Z-aaaaaaaaaaaa'
    path = root / 'out/builds' / stamp
    path.mkdir(parents=True)
    data = b'preview'
    (path / f'{part}.stl').write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    manifest = {'schema': 'cad-build-v1', 'build_id': stamp, 'passed': True,
                'cad_valid': True, 'solids': 1, 'stl_watertight': True,
                'stl_winding_consistent': True, 'stl_volume_mm3': 1,
                'source_files': source_hashes(root), 'name': part,
                'files': {f'{part}.stl': digest}, 'checksums': {f'{part}.stl': digest}}
    if target:
        manifest['catalog_target'] = {'id': target, 'object': f'parts/{target}'}
    write(path / 'manifest.json', manifest)
    write(path / 'resolved-parameters.json', {'family': family, 'depth_units': 4, 'height_units': 1, 'part': part})
    return path


def test_unregistered_exports_are_history_only_and_output_is_disposable(tmp_path):
    root = declare_vault(tmp_path / 'vault')
    regular = saved_export(root, 's4_module_1f', 1)
    saved_export(root, 'xl4_module_1f', 2, family='XL')
    cat = index(tmp_path)
    project = cat.metadata()['projects'][0]
    assert len(project['files']) == 2
    assert len(project['latest_parts']) == 2
    assert all(p['name'] != 'xl4_module_1f' for p in project['latest_parts'])
    assert next(f for f in project['files'] if f['id'] == project['default_file'])['revision'] == regular.name
    assert project['latest_parts'][0]['state'] == 'current'
    (root / 'catalog/module.py').write_text('# changed generator\n')
    assert cat.metadata()['projects'][0]['latest_parts'][0]['state'] == 'changed'
    shutil.rmtree(root / 'out')
    project = cat.metadata()['projects'][0]
    assert len(project['latest_parts']) == 2 and project['files'] == []
    assert project['default_file'] is None


def test_wrong_or_missing_configuration_is_not_attached_by_part_name(tmp_path):
    root = declare_vault(tmp_path / 'vault')
    saved_export(root, 's4_module_1f', 1, family='XL')
    cat = index(tmp_path)
    assert cat.metadata()['projects'][0]['latest_parts'][0]['state'] == 'missing'


def test_invalid_catalog_is_visible_and_does_not_fall_back_to_discovery(tmp_path):
    root = declare_vault(tmp_path / 'vault')
    saved_export(root, 'xl4_module_1f', 1, family='XL')
    (root / 'partcad.json').write_text('{broken')
    project = index(tmp_path).metadata()['projects'][0]
    assert project['catalog_error'].startswith('partcad.json:')
    assert project['latest_parts'] == [] and project['default_file'] is None
    assert len(project['files']) == 1


@pytest.mark.parametrize('mutate', [
    lambda d: d.update(interfaces={}),
    lambda d: d['parts']['s4_module_1f'].update(path='../outside.py'),
    lambda d: d['parts']['s4_module_1f'].update(metadata={'reviewed': True}),
    lambda d: d['parts']['s4_module_1f']['parameters']['depth_units'].update(default=True),
])
def test_unsupported_or_invalid_descriptions_are_rejected(tmp_path, mutate):
    root = declare_vault(tmp_path / 'vault')
    data = json.loads((root / 'partcad.json').read_text()); mutate(data)
    write(root / 'partcad.json', data)
    with pytest.raises(CatalogError):
        load(root)


def test_duplicate_keys_external_sources_and_unknown_targets_are_rejected(tmp_path):
    root = declare_vault(tmp_path / 'vault')
    (root / 'partcad.json').write_text('{"parts":{},"parts":{}}')
    with pytest.raises(CatalogError, match='duplicate'):
        load(root)
    declare_vault(root)
    outside = tmp_path / 'outside.py'; outside.write_text('# outside')
    path = root / 'catalog/module.py'; path.unlink(); path.symlink_to(outside)
    with pytest.raises(CatalogError, match='external'):
        load(root)


def test_cyclone_assembly_has_explicit_members_and_preset(tmp_path):
    root = declare_cyclone(tmp_path / 'cyclone')
    declaration = load(root)
    assert declaration.target('baseline').components == ('chamber',)
    assert declaration.target('baseline').configuration == 'baseline.toml'
    assert index(tmp_path).metadata()['projects'][0]['latest_parts'][0]['name'] == 'chamber'
    write(root / 'baseline.assy.json', {'links': [{'part': 'unknown'}]})
    with pytest.raises(CatalogError, match='unknown local part'):
        load(root)


def test_recorded_target_survives_preset_edits_but_legacy_match_requires_hash(tmp_path):
    root = declare_cyclone(tmp_path / 'cyclone')
    old = load(root).target('baseline')
    (root / 'presets/baseline.toml').write_text('units="mm"\n# changed\n')
    target = load(root).target('baseline')
    assert not target.matches({'input_file_hash': old.input_hash}, {}, ('chamber',))
    assert target.matches({'catalog_target': {'id': 'baseline', 'object': 'assemblies/baseline'}}, {}, ('chamber',))


def test_declarations_are_snapshotted_and_build_identity_is_recorded(tmp_path):
    root = declare_vault(tmp_path / 'vault')
    assert {'partcad.json', 'cad-builds.json', 'catalog/module.py'} <= set(source_hashes(root))
    target = {'id': 's1f_facade', 'object': 'parts/s1f_facade'}
    token = catalog_target.set(target)
    try:
        destination = run_build(root, 'test', lambda state: ({'passed': True}, {'components': []}), parameters={})
    finally:
        catalog_target.reset(token)
    assert json.loads((destination / 'manifest.json').read_text())['catalog_target'] == target


def test_vault_box_binding_and_identity(tmp_path):
    root = declare_vault(tmp_path / 'vault')
    parts = json.loads((root / 'partcad.json').read_text())
    parts['parts']['s4_box_1f'] = {'type': 'build123d', 'desc': 'S4 Box 1F', 'path': 'catalog/box.py'}
    write(root / 'partcad.json', parts)
    (root / 'catalog/box.py').write_text('# box wrapper\n')
    bindings = json.loads((root / 'cad-builds.json').read_text())
    bindings['targets']['s4_box_1f'] = {'object': 'parts/s4_box_1f', 'adapter': 'vault-box'}
    write(root / 'cad-builds.json', bindings)
    target = load(root).target('s4_box_1f')
    assert target.configuration == 's4_box_1f'
    assert target.label == 'S4 Box 1F'
    assert target.matches({}, {'part': 's4_box_1f'}, ['s4_box_1f'])
    assert not target.matches({}, {'part': 's4_module_1f'}, ['s4_module_1f'])
    bindings['targets']['s4_box_1f']['object'] = 'parts/s1f_facade'
    write(root / 'cad-builds.json', bindings)
    with pytest.raises(CatalogError, match='box adapter supports only'):
        load(root)


@pytest.mark.parametrize('part_id', [
    's4_box_1f_printed_screw', 's1f_facade_printed_screw', 's4_facade_screw',
])
def test_vault_printed_joint_binding_and_identity(tmp_path, part_id):
    root = declare_vault(tmp_path / 'vault')
    parts = json.loads((root / 'partcad.json').read_text())
    parts['parts'][part_id] = {'type': 'build123d', 'desc': part_id, 'path': 'catalog/printed.py'}
    write(root / 'partcad.json', parts)
    (root / 'catalog/printed.py').write_text('# printed joint wrapper\n')
    bindings = json.loads((root / 'cad-builds.json').read_text())
    bindings['targets'][part_id] = {'object': f'parts/{part_id}', 'adapter': 'vault-printed-joint'}
    write(root / 'cad-builds.json', bindings)
    target = load(root).target(part_id)
    assert target.configuration == part_id
    assert target.matches({}, {'part': part_id}, [part_id])
    assert not target.matches({}, {'part': 's4_box_1f'}, ['s4_box_1f'])
    actions = BuildActions(index(tmp_path))
    assert part_id in {item['id'] for item in actions.options({'project': 'vault'})[2]['options']}
    bindings['targets'][part_id]['object'] = 'parts/s1f_facade'
    write(root / 'cad-builds.json', bindings)
    with pytest.raises(CatalogError, match='printed joint adapter supports only'):
        load(root)
    bindings['targets'][part_id]['object'] = f'parts/{part_id}'
    write(root / 'cad-builds.json', bindings)
    parts['parts'][part_id]['parameters'] = {'family': {'type': 'string', 'default': 'S'}}
    write(root / 'partcad.json', parts)
    with pytest.raises(CatalogError, match='printed joint adapter supports only'):
        load(root)
