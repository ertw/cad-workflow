"""Project page and build-pinned downloads on the viewer's loopback server."""
from http import HTTPStatus
import hashlib
import json
import mimetypes
from pathlib import Path
import re
from urllib.parse import parse_qs, quote, unquote, urlsplit

from ocp_viewer.server.pages import _response, respond as viewer_response


STATIC = Path(__file__).with_name('static')
BUILD_ID = re.compile(r'\d{8}T\d{12}Z-[a-f0-9]{12}')
PART_NAME = re.compile(r'[a-zA-Z0-9_-]+')


class BuildFiles:
    """Only successful builds and their recorded print artifacts are public."""

    def __init__(self, builds, build_path=None):
        self.builds = Path(builds).resolve()
        self.pinned = Path(build_path).resolve() if build_path else None

    def build(self, build_id):
        if not BUILD_ID.fullmatch(build_id):
            raise FileNotFoundError('Unknown build')
        path = self.builds / build_id
        if self.pinned and self.pinned.name == build_id:
            path = self.pinned
        elif path.resolve().parent != self.builds:
            raise FileNotFoundError('Unknown build')
        manifest_path = path / 'manifest.json'
        if not manifest_path.resolve().is_relative_to(path.resolve()):
            raise FileNotFoundError('Unknown build')
        manifest = json.loads(manifest_path.read_text())
        if manifest.get('passed') is not True or manifest.get('build_id') != build_id:
            raise FileNotFoundError('Build is not complete')
        return path, manifest

    @staticmethod
    def artifact(path, manifest, relative):
        digest = manifest.get('checksums', {}).get(relative)
        file = path / relative
        if not digest or not file.resolve().is_relative_to(path.resolve()) or not file.is_file():
            raise FileNotFoundError('Unknown artifact')
        data = file.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Artifact checksum failed; rebuild this revision')
        return data

    def metadata(self, build_id):
        path, manifest = self.build(build_id)
        description = json.loads(self.artifact(path, manifest, 'viewer.json'))
        assembly = {item['name'] for item in description['components']}
        parts = []
        for name, part in manifest['parts'].items():
            if not PART_NAME.fullmatch(name):
                continue
            downloads = {}
            for fmt in ('3mf', 'stl'):
                relative = f'{fmt}/{name}.{fmt}'
                if relative in manifest['checksums']:
                    downloads[fmt] = f'/downloads/{build_id}/{relative}'
            parts.append({
                'name': name, 'kind': part['kind'], 'in_assembly': name in assembly,
                'print_note': part['print_note'], 'print_bbox_mm': part['print_bbox_mm'],
                'downloads': downloads,
            })
        return {'build_id': build_id, 'units': manifest['units'],
                'status': manifest['status'], 'parts': parts}

    def download(self, build_id, fmt, filename):
        if fmt not in ('3mf', 'stl') or not filename.endswith('.' + fmt):
            raise FileNotFoundError('Unknown format')
        name = filename[:-(len(fmt) + 1)]
        if not PART_NAME.fullmatch(name):
            raise FileNotFoundError('Unknown part')
        path, manifest = self.build(build_id)
        if name not in manifest['parts']:
            raise FileNotFoundError('Unknown part')
        return self.artifact(path, manifest, f'{fmt}/{filename}')


def respond(viewer, request, files, catalog=None):
    if request.headers.get('Upgrade', '').lower() == 'websocket':
        return None
    url = urlsplit(request.path)
    path = unquote(url.path)
    try:
        if path in ('/', '/viewer'):
            return _response(HTTPStatus.OK, (STATIC / 'index.html').read_bytes(),
                             'text/html; charset=utf-8', {'Cache-Control': 'no-store'})
        if path.startswith('/app/'):
            file = (STATIC / path.removeprefix('/app/')).resolve()
            if not file.is_relative_to(STATIC.resolve()) or not file.is_file():
                raise FileNotFoundError('Unknown asset')
            return _response(HTTPStatus.OK, file.read_bytes(),
                             mimetypes.guess_type(file.name)[0] or 'application/octet-stream',
                             {'Cache-Control': 'no-cache'})
        if path == '/api/viewer-config':
            config = {key: viewer.config[key] for key in
                      ('theme', 'tree_width', 'glass', 'tools', 'up', 'control', 'modifier_keys')}
            config['max_reconnect_attempts'] = viewer.max_reconnect_attempts
            config['catalog_enabled'] = catalog is not None
            return _json(config)
        if path == '/api/projects' and catalog is not None:
            return _json(catalog.metadata())
        if path.startswith('/api/projects/') and catalog is not None:
            segments = path.strip('/').split('/')
            if len(segments) == 4 and segments[3] == 'working-tree':
                return _json(catalog.working_tree(segments[2]))
            if len(segments) < 6 or segments[3] != 'files':
                raise FileNotFoundError('Unknown project file')
            project, file_id = segments[2], segments[4]
            if len(segments) == 6 and segments[5] == 'scene':
                revisions = parse_qs(url.query).get('revision', [])
                if len(revisions) != 1:
                    raise ValueError('A file revision is required')
                entry = catalog.resolve(project, file_id, revisions[0])
                from .scenes import scene_for
                model = scene_for(entry)
                # Recheck after tessellation; a concurrent export must never
                # attach another revision's downloads to this scene.
                catalog.resolve(project, file_id, entry.revision)
                metadata = _file_metadata(entry, files)
                descriptor = {key: getattr(entry, key) for key in
                              ('id', 'name', 'group', 'kind', 'revision')}
                payload = (b'{"model":' + model + b',"metadata":' +
                           json.dumps(metadata, allow_nan=False).encode() +
                           b',"file":' + json.dumps(descriptor).encode() + b'}')
                return _response(HTTPStatus.OK, payload, 'application/json; charset=utf-8',
                                 {'Cache-Control': 'no-store'})
            if len(segments) == 8 and segments[5] == 'downloads':
                data, filename, mime = catalog.download(project, file_id, segments[6], segments[7])
                return _response(HTTPStatus.OK, data, mime, {
                    'Content-Disposition': "attachment; filename*=UTF-8''" + quote(filename, safe=''),
                    'Cache-Control': 'private, no-cache',
                    'X-Content-Type-Options': 'nosniff',
                })
            raise FileNotFoundError('Unknown project file')
        if path.startswith('/api/builds/'):
            return _json(files.metadata(path.removeprefix('/api/builds/')))
        if path.startswith('/downloads/'):
            segments = path.split('/')
            if len(segments) != 5:
                raise FileNotFoundError('Unknown download')
            _, _, build_id, fmt, filename = segments
            data = files.download(build_id, fmt, filename)
            return _response(HTTPStatus.OK, data, 'model/3mf' if fmt == '3mf' else 'model/stl', {
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Cache-Control': 'private, max-age=31536000, immutable',
                'X-Content-Type-Options': 'nosniff',
            })
        return viewer_response(viewer, request)
    except (FileNotFoundError, NotADirectoryError):
        return _json({'error': 'Build or file not found'}, HTTPStatus.NOT_FOUND)
    except (ValueError, KeyError):
        return _json({'error': 'This file revision is unavailable or invalid. Refresh the file list and try again.'},
                     HTTPStatus.CONFLICT)


def _file_metadata(entry, files):
    if entry.kind == 'assembly':
        metadata = files.metadata(entry.build_id)
    else:
        metadata = {
            'units': entry.units, 'status': entry.status,
            'parts': [{
                'name': entry.component_name, 'label': entry.name,
                'kind': 'mesh', 'in_assembly': True,
                'print_note': entry.print_note,
                'downloads': {'stl': f'/api/projects/{entry.project_id}/files/{entry.id}'
                                     f'/downloads/{entry.revision}/stl'},
            }],
        }
    return {**metadata, 'project_id': entry.project_id, 'file_id': entry.id,
            'revision': entry.revision}


def _json(data, status=HTTPStatus.OK):
    return _response(status, json.dumps(data, allow_nan=False).encode(),
                     'application/json; charset=utf-8', {'Cache-Control': 'no-store'})
