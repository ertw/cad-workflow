"""Revision-pinned native fillets, with checked immutable exports on apply."""
from collections import OrderedDict
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
from threading import Lock
import time

from .web import BuildFiles, PART_NAME


SELECTION = re.compile(r'(.+)/(edges/edges|faces/faces)_(\d+)')
MODEL_NOTE = 'Model coordinates. Select print orientation and supports before printing.'


def capabilities(entry):
    result = {'supported': entry.kind == 'assembly' and entry.units == 'mm'}
    if not result['supported']:
        result['reason'] = 'Fillet requires a saved native solid in millimeters. This file is a mesh.'
    elif entry.build_id:
        files = BuildFiles(entry.path.parent, entry.path)
        path, manifest = files.build(entry.build_id)
        if manifest.get('operation') == 'operation.json':
            result['operation'] = json.loads(files.artifact(path, manifest, 'operation.json'))
    return result


def _base_signature(entry):
    files = BuildFiles(entry.path.parent, entry.path)
    path, manifest = files.build(entry.build_id)
    description_bytes = files.artifact(path, manifest, 'viewer.json')
    description = json.loads(description_bytes)
    checksums = {'viewer.json': hashlib.sha256(description_bytes).hexdigest()}
    for component in description['components']:
        relative = component['step']
        # Artifact lookup verifies confinement and content before any CAD import.
        checksums[relative] = hashlib.sha256(files.artifact(path, manifest, relative)).hexdigest()
    return {'manifest': hashlib.sha256((path / 'manifest.json').read_bytes()).hexdigest(),
            'artifacts': checksums}


def _selected_solid(mapping, reference):
    """Resolve the exact tessellator index in its original native component.

    OCP's display mapping and tessellation use the same get_edges/get_faces
    enumeration. Browser bounds and picked coordinates are never used to infer
    topology, and exploded/zoomed display transforms cannot change selection.
    """
    from build123d import Edge, Shape
    from ocp_tessellate.ocp_utils import get_edges, get_faces

    geometry_path = reference.get('geometry_path')
    match = SELECTION.fullmatch(geometry_path) if isinstance(geometry_path, str) else None
    if not match:
        raise ValueError('Select an edge or a face in the displayed native model.')
    component_id, kind, index = match.groups()
    topology = 'edge' if kind.startswith('edges') else 'face'
    if reference.get('topology') != topology:
        raise ValueError('Selection topology does not match its geometry path.')

    def leaves(group):
        for item in group.get('parts', []):
            if 'parts' in item:
                yield from leaves(item)
            else:
                yield item

    component = next((item for item in leaves(mapping) if item['id'] == component_id), None)
    if component is None or not isinstance(component.get('shape'), dict):
        raise ValueError('Selected component is not present in this revision.')
    native = component['shape']['obj']
    if component.get('loc') is not None:
        native = native.Moved(component['loc'])
    wrapped = Shape.cast(native)
    solids = wrapped.solids()
    if len(solids) != 1 or not wrapped.is_valid:
        raise ValueError('Fillet requires a component containing one valid solid.')
    items = list(get_edges(native) if topology == 'edge' else get_faces(native))
    index = int(index)
    if index >= len(items):
        raise ValueError('Selected geometry is not present in this revision.')
    edges = [items[index]] if topology == 'edge' else list(get_edges(items[index]))
    if not edges:
        raise ValueError('The selected face has no boundary edges.')
    # STEP imports may contain an extra COMPOUND node beneath the recorded
    # assembly component. Keep the actual component name in exported files.
    prefix = mapping['id'].rstrip('/') + '/'
    if not component_id.startswith(prefix):
        raise ValueError('Selected component is outside this assembly.')
    name = component_id[len(prefix):].split('/', 1)[0]
    if not PART_NAME.fullmatch(name):
        raise ValueError('Selected component has an unsupported name.')
    return solids[0], [Edge(edge) for edge in edges], name


def _validate_solid(shape):
    if (not shape.is_valid or len(shape.solids()) != 1 or len(shape.shells()) != 1
            or not math.isfinite(shape.volume) or shape.volume <= 0):
        raise ValueError('Fillet did not produce one valid solid with a closed boundary.')


def _export_checked(shape, directory, name):
    """Export the edited native solid without changing its coordinate system."""
    from build123d import export_step, export_stl, import_step
    import trimesh

    _validate_solid(shape)
    for folder in ('step', 'stl'):
        (directory / folder).mkdir()
    step = directory / 'step' / f'{name}.step'
    if not export_step(shape, step):
        raise ValueError('STEP export failed.')
    restored = import_step(step)
    _validate_solid(restored)
    before, after = shape.bounding_box(), restored.bounding_box()
    bounds_delta = max(abs(a - b) for a, b in zip(
        (*before.min, *before.max), (*after.min, *after.max)))
    volume_delta = abs(restored.volume - shape.volume) / max(abs(shape.volume), 1.)
    if bounds_delta >= 1e-4 or volume_delta >= 1e-5:
        raise ValueError('STEP round-trip validation failed.')
    stl = directory / 'stl' / f'{name}.stl'
    if not export_stl(restored, stl, tolerance=0.01, angular_tolerance=0.1):
        raise ValueError('STL export failed.')
    mesh = trimesh.load(stl, force='mesh')
    if (not isinstance(mesh, trimesh.Trimesh) or not mesh.is_watertight
            or not mesh.is_winding_consistent or not math.isfinite(mesh.volume)
            or mesh.volume <= 0):
        raise ValueError('Print mesh must be watertight with consistent winding and positive volume.')
    return {'cad_valid': True, 'solids': 1, 'boundary_shells': 1,
            'step_roundtrip': {'passed': True, 'bbox_max_error_mm': bounds_delta,
                               'volume_relative_error': volume_delta},
            'stl_watertight': True, 'stl_winding_consistent': True,
            'stl_volume_mm3': float(mesh.volume),
            'print_bbox_mm': list(restored.bounding_box().size)}


class FilletActions:
    """A bounded preview cache; only Apply writes a new immutable build."""

    def __init__(self, catalog, lock=None):
        self.catalog = catalog
        self.lock = lock if lock is not None else Lock()
        self.previews = OrderedDict()

    def preview(self, data):
        from build123d import Compound
        from ocp_tessellate.ocp_utils import serialize
        from .assembly import viewer_assembly
        from .scenes import _CAD_LOCK, native_scene

        reference = data.get('reference')
        if (not isinstance(reference, dict)
                or not all(isinstance(data.get(key), str) and data[key]
                           for key in ('project', 'file', 'revision'))
                or any(reference.get(key) != data[key] for key in ('project', 'file', 'revision'))):
            raise ValueError('Select an edge or face in the current project revision.')
        radius = data.get('radius')
        if (isinstance(radius, bool) or not isinstance(radius, (int, float))
                or not math.isfinite(radius) or radius <= 0 or radius > 10000):
            raise ValueError('Enter a finite radius greater than zero and no more than 10,000 mm.')
        entry = self.catalog.resolve(data['project'], data['file'], data['revision'])
        capability = capabilities(entry)
        if not capability['supported']:
            raise ValueError(capability['reason'])
        signature = _base_signature(entry)
        with _CAD_LOCK:
            assembly, label = viewer_assembly(entry.path)
            _, mapping = native_scene(assembly, label, capture_topology=True)
            solid, edges, name = _selected_solid(mapping, reference)
            edited = solid.fillet(float(radius), edges)
            _validate_solid(edited)
            edited.label = name + '_fillet'
            edited.color = solid.color
            model, _ = native_scene(Compound(children=[edited], label=label), label)
            brep = serialize(edited.wrapped)
        # Recheck every input after kernel work; a replaced artifact cannot
        # acquire a preview token belonging to the earlier native geometry.
        entry = self.catalog.resolve(data['project'], data['file'], data['revision'])
        if _base_signature(entry) != signature:
            raise ValueError('Source revision changed during preview. Reload the model.')
        operation = {'schema': 'cad-fillet/v1', 'type': 'fillet',
                     'base': {key: data[key] for key in ('project', 'file', 'revision')},
                     'selection': {'geometry_path': reference['geometry_path'],
                                   'topology': reference['topology'], 'component': name},
                     'radius_mm': float(radius), 'edges': len(edges), 'source': signature}
        model.update(project_id=entry.project_id, file_id=entry.id, revision=entry.revision)
        now = time.monotonic()
        self.previews = OrderedDict((key, value) for key, value in self.previews.items()
                                    if now - value['created'] < 1800)
        # Keep native bytes, never live OCCT state, and bound retained memory.
        if len(brep) > 64 * 1024 * 1024:
            raise ValueError('Fillet preview is too large to retain.')
        while self.previews and (len(self.previews) >= 8 or
                sum(len(item['brep']) for item in self.previews.values()) + len(brep) > 128 * 1024 * 1024):
            self.previews.popitem(last=False)
        preview_id = secrets.token_urlsafe(24)
        self.previews[preview_id] = {'created': now, 'brep': brep, 'operation': operation,
                                     'name': edited.label}
        return {'preview_id': preview_id, 'model': model, 'operation': operation}

    def apply(self, data):
        from build123d import Shape
        from cad_build import run_build, write_json
        from ocp_tessellate.ocp_utils import deserialize
        from .scenes import _CAD_LOCK

        token = data.get('preview_id')
        if not isinstance(token, str) or token not in self.previews:
            raise ValueError('Preview this fillet before applying it.')
        preview = self.previews[token]
        if time.monotonic() - preview['created'] >= 1800:
            self.previews.pop(token)
            raise ValueError('Fillet preview expired. Preview it again.')
        operation = preview['operation']
        base = operation['base']
        entry = self.catalog.resolve(base['project'], base['file'], base['revision'])
        if _base_signature(entry) != operation['source']:
            raise ValueError('Source revision changed. Reload the model and preview again.')
        with self.catalog._lock:
            root = self.catalog._project_roots[base['project']]
        name = preview['name']
        digest = hashlib.sha256(json.dumps(operation, sort_keys=True).encode()).hexdigest()

        def generate(state):
            with _CAD_LOCK:
                shape = Shape.cast(deserialize(preview['brep']))
                checks = _export_checked(shape, state.stage, name)
            # A concurrent export replacement during validation must not publish.
            current = self.catalog.resolve(base['project'], base['file'], base['revision'])
            if _base_signature(current) != operation['source']:
                raise ValueError('Source revision changed during export. No build was published.')
            write_json(state.stage / 'operation.json', operation)
            return ({'passed': True, 'project': base['project'], 'units': 'mm',
                     'name': f'{operation["selection"]["component"]} — Fillet {operation["radius_mm"]:g} mm',
                     'status': 'Edited CAD solid', 'operation': 'operation.json', **checks,
                     'parts': {name: {'kind': 'model', 'print_note': MODEL_NOTE,
                                      'print_bbox_mm': checks['print_bbox_mm']}}},
                    {'components': [{'name': name, 'kind': 'model', 'step': f'step/{name}.step'}]})

        # The existing build pipeline snapshots sources, gates publication,
        # freezes checksums, and records failures without advancing pointers.
        destination = run_build(root, digest, generate, parameters=operation,
                                inputs={'tool/fillet_actions.py': Path(__file__)}, promote=False)
        self.previews.pop(token)
        return {'project': base['project'], 'file': 'build-' + destination.name,
                'revision': destination.name, 'operation': operation}

    def execute(self, data):
        if not self.lock.acquire(blocking=False):
            raise ValueError('A build, fillet, or agent request is already running. Wait for it to finish.')
        try:
            if data.get('action') == 'preview':
                return self.preview(data)
            if data.get('action') == 'apply':
                return self.apply(data)
            raise ValueError('Unknown fillet action.')
        finally:
            self.lock.release()

    def handle(self, socket):
        try:
            raw = socket.recv(timeout=15)
            if len(raw) > 80000:
                raise ValueError('Fillet request is too large.')
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError('Expected a fillet request.')
            socket.send(json.dumps({'result': self.execute(data)}, allow_nan=False))
        except (ValueError, OSError, KeyError, RuntimeError) as error:
            socket.send(json.dumps({'error': str(error)}))
