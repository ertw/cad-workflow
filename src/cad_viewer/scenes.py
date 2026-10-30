"""Independent, immutable scenes for saved CAD files and generated meshes."""
from collections import OrderedDict
from io import BytesIO
import json
from pathlib import Path
import threading

import numpy as np
from ocp_tessellate.utils import numpy_to_buffer_json
from ocp_viewer_core.comms import Comms, Session
from ocp_viewer_core.config import Camera, Collapse, Config
from ocp_viewer_core.show import Viewer


MAX_STL_BYTES = 512 * 1024 * 1024
MAX_PREVIEW_TRIANGLES = 150_000
MESH_CHUNK = 100_000
_CAD_LOCK = threading.Lock()
_STL_RECORD = np.dtype([
    ('normal', '<f4', (3,)), ('vertices', '<f4', (3, 3)), ('attribute', '<u2'),
])


class _Capture(Comms):
    """The released viewer pipeline, with an in-memory transport per request."""

    def status(self):
        return {}

    def workspace_config(self):
        return {}

    def send_data(self, data, timeit=False):
        return data

    def __init__(self, capture_topology=False):
        self.capture_topology = capture_topology
        self.topology = None

    def send_backend(self, data, timeit=False):
        # The static preview has no shared measurement backend. Keeping these
        # OCCT objects would retain the complete CAD model after serialization.
        if self.capture_topology:
            self.topology = data['model']


def _options():
    return dict(axes=True, grid=(False, False, False), theme='light',
                tree_width=300, collapse=Collapse.NONE, reset_camera=Camera.RESET)


def _mesh_triangles(data):
    """Keep binary STL as a view over its bytes, even for multi-million faces."""
    if len(data) >= 84:
        count = int.from_bytes(data[80:84], 'little')
        if 84 + count * _STL_RECORD.itemsize == len(data):
            return np.frombuffer(data, dtype=_STL_RECORD, count=count, offset=84)['vertices']
    # ASCII STL's parser is useful for small exports and tests; large binary
    # exports never pay its memory overhead.
    import trimesh
    mesh = trimesh.load(BytesIO(data), file_type='stl', process=False)
    if not isinstance(mesh, trimesh.Trimesh):
        raise ValueError('STL does not contain a triangle mesh')
    return np.asarray(mesh.triangles, dtype=np.float32)


def _mesh_bounds(triangles):
    if not len(triangles):
        raise ValueError('STL has no triangles')
    low = np.full(3, np.inf)
    high = np.full(3, -np.inf)
    for start in range(0, len(triangles), MESH_CHUNK):
        chunk = triangles[start:start + MESH_CHUNK]
        if not np.isfinite(chunk).all():
            raise ValueError('STL contains non-finite coordinates')
        low = np.minimum(low, chunk.min(axis=(0, 1)))
        high = np.maximum(high, chunk.max(axis=(0, 1)))
    if np.max(high - low) <= 0:
        raise ValueError('STL has no extent')
    return low, high


def _cluster_mesh(triangles, low, high, limit):
    """Display-only vertex clustering; original print files stay untouched.

    Integer cell ids keep working memory proportional to the input bytes and
    a small regular grid. No full-size float64 trimesh or CAD solid is created.
    Centroids of occupied cells are used after choosing the preview resolution.
    """
    cells = 128
    while True:
        step = float(np.max(high - low)) / cells
        dimensions = np.rint((high - low) / step).astype(np.int32) + 1

        def cell_ids(chunk):
            xyz = np.rint((chunk.reshape(-1, 3) - low) / step).astype(np.int32)
            xyz = np.minimum(np.maximum(xyz, 0), dimensions - 1)
            return (xyz[:, 0] * dimensions[1] + xyz[:, 1]) * dimensions[2] + xyz[:, 2]

        reduced = []
        for start in range(0, len(triangles), MESH_CHUNK):
            faces = cell_ids(triangles[start:start + MESH_CHUNK]).reshape(-1, 3)
            faces = faces[(faces[:, 0] != faces[:, 1]) &
                          (faces[:, 1] != faces[:, 2]) &
                          (faces[:, 2] != faces[:, 0])]
            # Rotate without reversing winding so repeated triangle orderings
            # deduplicate without changing which side faces outward.
            if len(faces):
                offsets = np.argmin(faces, axis=1)
                faces = np.take_along_axis(faces, (offsets[:, None] + np.arange(3)) % 3, axis=1)
                reduced.append(np.unique(faces, axis=0))
        if not reduced:
            raise ValueError('Mesh has no visible surface at preview resolution')
        faces = np.unique(np.concatenate(reduced), axis=0)
        if len(faces) <= limit:
            break
        cells = max(1, cells * 3 // 4)
        if cells == 1:
            raise ValueError('Mesh cannot be reduced for preview')

    used, inverse = np.unique(faces, return_inverse=True)
    sums = np.zeros((int(np.prod(dimensions)), 3), dtype=np.float64)
    counts = np.zeros(len(sums), dtype=np.int32)
    for start in range(0, len(triangles), MESH_CHUNK):
        chunk = triangles[start:start + MESH_CHUNK]
        ids = cell_ids(chunk)
        unique, index, frequency = np.unique(ids, return_inverse=True, return_counts=True)
        points = chunk.reshape(-1, 3)
        counts[unique] += frequency.astype(np.int32)
        for coordinate in range(3):
            sums[unique, coordinate] += np.bincount(index, weights=points[:, coordinate])
    vertices = (sums[used] / counts[used, None]).astype(np.float32)
    return vertices, inverse.reshape(-1, 3).astype(np.int32)


def _feature_edges(positions, normals):
    """Expose boundaries and sharp folds without drawing every STL triangle."""
    vertices, indices = np.unique(positions, axis=0, return_inverse=True)
    faces = indices.reshape(-1, 3)
    edges = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    edges.sort(axis=1)
    unique, inverse, counts = np.unique(edges, axis=0, return_inverse=True, return_counts=True)
    summed = np.zeros((len(unique), 3), dtype=np.float32)
    np.add.at(summed, inverse, np.tile(normals, (3, 1)))
    # A cube's faces receive equal light in the initial isometric view. Its
    # actual sharp edges make that view readable without adding triangulation
    # lines to curved or coplanar surfaces. Keep nonmanifold boundaries too.
    sharp = (counts != 2) | (np.linalg.norm(summed, axis=1) < 2 * np.cos(np.deg2rad(25 / 2)))
    sharp &= unique[:, 0] != unique[:, 1]
    return vertices[unique[sharp]].astype(np.float32).ravel()


def _mesh_message(entry, data):
    triangles = _mesh_triangles(data)
    low, high = _mesh_bounds(triangles)
    original_count = len(triangles)
    simplified = original_count > MAX_PREVIEW_TRIANGLES
    if simplified:
        vertices, faces = _cluster_mesh(triangles, low, high, MAX_PREVIEW_TRIANGLES)
        # Flat shading preserves sharp print surfaces and avoids requiring a
        # costly adjacency graph for the complete source mesh.
        positions = vertices[faces].reshape(-1, 3)
    else:
        positions = np.asarray(triangles.reshape(-1, 3), dtype=np.float32)
    count = len(positions) // 3
    corners = positions.reshape(-1, 3, 3)
    normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    normals /= np.where(lengths > 0, lengths, 1)[:, None]
    edges = _feature_edges(positions, normals)
    edge_count = len(edges) // 6
    instance = {
        'vertices': positions.ravel(),
        'triangles': np.arange(len(positions), dtype=np.int32),
        'normals': np.repeat(normals, 3, axis=0).ravel(),
        'edges': edges,
        'obj_vertices': np.empty(0, dtype=np.float32),
        'face_types': np.zeros(1, dtype=np.int32),
        'edge_types': np.zeros(edge_count, dtype=np.int32),
        'triangles_per_face': np.array([count], dtype=np.int32),
        'segments_per_edge': np.ones(edge_count, dtype=np.int32),
    }
    name = entry.component_name
    shapes = {
        'version': 3, 'name': 'Group', 'id': '/Group', 'loc': None,
        'normal_len': 0,
        'bb': {f'{axis}{side}': float(point[index]) for side, point in
               (('min', low), ('max', high)) for index, axis in enumerate('xyz')},
        'parts': [{
            'id': f'/Group/{name}', 'name': name, 'type': 'shapes', 'subtype': 'solid',
            'shape': {'ref': 0}, 'state': [1, 1], 'color': '#70a7bb', 'alpha': 1.0,
            'material': None, 'normalize_uvs': True, 'texture': None, 'loc': None,
            'renderback': False, 'accuracy': None, 'bb': None,
        }],
    }
    message = Session(_Capture()).send_data({
        'type': 'data', 'data': numpy_to_buffer_json({'instances': [instance], 'shapes': shapes}),
        'config': {key: value.value if hasattr(value, 'value') else value
                   for key, value in _options().items()}, 'count': 1,
    })
    message['preview'] = {'simplified': simplified, 'source_triangles': original_count,
                          'triangles': count}
    return message


def native_scene(shape, label, *, capture_topology=False):
    """Use one conversion path for displayed geometry and exact edit selection.

    Callers hold ``_CAD_LOCK``. The optional topology lives only for the request;
    the ordinary scene cache continues to retain serialized bytes only.
    """
    capture = _Capture(capture_topology)
    message = Viewer(Config(Session(capture), ())).show(
        shape, names=[label], progress='', **_options())
    if not isinstance(message, dict):
        raise ValueError('CAD scene could not be serialized')
    return message, capture.topology


def _render(entry):
    if entry.source_kind == 'cyclone':
        from .assembly import viewer_assembly
        shape, label = viewer_assembly(entry.source_path)
        message, _ = native_scene(shape, label)
        message['build_id'] = entry.build_id
    elif entry.source_kind == 'stl':
        if Path(entry.source_path).stat().st_size > MAX_STL_BYTES:
            raise ValueError('STL exceeds the 512 MiB preview limit')
        data = entry.read_bytes()
        if len(data) > MAX_STL_BYTES:
            raise ValueError('STL exceeds the 512 MiB preview limit')
        message = _mesh_message(entry, data)
    else:
        raise ValueError(f'Unsupported preview type: {entry.source_kind}')
    message['project_id'] = entry.project_id
    message['file_id'] = entry.file_id
    message['revision'] = entry.revision
    return json.dumps(message, allow_nan=False, separators=(',', ':')).encode()


class SceneCache:
    """Bound serialized scenes by both count and bytes; never retain CAD objects."""

    def __init__(self, max_entries=2, max_bytes=64 * 1024 * 1024):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._entries = OrderedDict()
        self._bytes = 0

    def scene(self, entry):
        key = (entry.project_id, entry.file_id, entry.revision)
        # OCCT and the upstream tessellation cache share process state. Keep
        # their work serial, including between distinct SceneCache instances.
        with _CAD_LOCK:
            if key in self._entries:
                self._entries.move_to_end(key)
                return self._entries[key]
            try:
                data = _render(entry)
            except RuntimeError as error:
                # Saved build verification and OCCT imports report runtime
                # errors; the HTTP layer reports invalid revisions as 409.
                raise ValueError('Saved geometry is unavailable or invalid') from error
            if self.max_entries > 0 and len(data) <= self.max_bytes:
                while self._entries and (len(self._entries) >= self.max_entries or
                                         self._bytes + len(data) > self.max_bytes):
                    _, previous = self._entries.popitem(last=False)
                    self._bytes -= len(previous)
                self._entries[key] = data
                self._bytes += len(data)
            return data


_SCENES = SceneCache()


def scene_for(entry):
    """Return a complete OCP DATA message as UTF-8 JSON, pinned to this entry."""
    return _SCENES.scene(entry)
