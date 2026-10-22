"""Discover previewable projects without exposing an arbitrary file browser."""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from threading import RLock


SAFE_ID = re.compile(r'[A-Za-z0-9_-]+')
SHA256 = re.compile(r'[a-f0-9]{64}')


def _provenance(manifest):
    value = manifest.get('git')
    if (isinstance(value, dict)
            and re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', str(value.get('commit', '')))
            and isinstance(value.get('message'), str)
            and isinstance(value.get('committed_at'), str)
            and isinstance(value.get('dirty'), bool)):
        return value
    return None


def project_workspace(workspace):
    """Accept either a project directory collection or its enclosing workspace."""
    root = Path(workspace).resolve()
    projects = root / 'projects'
    return projects if projects.is_dir() else root


def _identity(value):
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def _fingerprint(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _confined(path, root):
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise FileNotFoundError('Unknown project file')
        return resolved
    except (OSError, RuntimeError) as error:
        raise FileNotFoundError('Unknown project file') from error


@dataclass(frozen=True)
class CatalogEntry:
    project_id: str
    id: str
    name: str
    group: str
    kind: str
    revision: str
    path: Path
    root: Path
    git: dict | None = field(default=None)
    build_id: str | None = None
    print_note: str = 'Build export; print orientation is not recorded.'
    status: str = 'Completed CAD build'
    units: str = 'mm'
    checksum: str | None = field(default=None, repr=False)
    fingerprint: tuple | None = field(default=None, repr=False)

    @property
    def file_id(self):
        return self.id

    @property
    def label(self):
        return self.name

    @property
    def source_kind(self):
        return 'cyclone' if self.kind == 'assembly' else self.kind

    @property
    def source_path(self):
        return self.path

    @property
    def component_name(self):
        return self.id

    def metadata(self):
        return {key: getattr(self, key) for key in
                ('id', 'name', 'group', 'kind', 'revision', 'git')}

    def read_bytes(self):
        """Check the selected revision both before and after reading an STL.

        Listing uses the successful build manifest's content checksum without
        reading every mesh. Reading verifies that checksum and the filesystem
        fingerprint captured when the entry was selected.
        """
        if self.kind != 'stl':
            raise FileNotFoundError('Assembly files use build-pinned artifacts')
        path = _confined(self.path, self.root)
        before = _fingerprint(path)
        if self.fingerprint is not None and before != self.fingerprint:
            raise ValueError('File changed; refresh the project before downloading')
        data = path.read_bytes()
        if _confined(self.path, self.root) != path or _fingerprint(path) != before:
            raise ValueError('File changed while it was being read')
        if self.checksum and hashlib.sha256(data).hexdigest() != self.checksum:
            raise ValueError('Artifact checksum failed; rebuild this revision')
        return data


class ProjectCatalog:
    """Index Cyclone configurations and completed generated mesh exports.

    Only direct workspace children with project metadata or an output directory
    are projects. Generated files declared by a successful ``out/**/build.json``
    are discoverable; no API parameter is interpreted as a filesystem path.
    """

    def __init__(self, workspace, build_files):
        self.workspace = project_workspace(workspace)
        self.build_files = build_files
        self._entries = {}
        self._revisions = {}
        self._project_names = {}
        self._project_roots = {}
        self._defaults = {}
        self._lock = RLock()

    def metadata(self):
        with self._lock:
            self._scan()
            return self._metadata()

    def _metadata(self):
        projects = []
        for project_id, name in sorted(self._project_names.items(),
                                       key=lambda item: (item[0] != 'cyclone', item[1].casefold())):
            files = [entry.metadata() for entry in self._entries.values()
                     if entry.project_id == project_id]
            history = project_id in {'cyclone', 'vault'} and bool(files)
            if history:
                files.sort(key=lambda item: item['name'].casefold())
                files.sort(key=lambda item: item['revision'] if re.match(r'\d{8}T', item['revision']) else '', reverse=True)
            else:
                files.sort(key=lambda item: (item['group'].casefold(), item['name'].casefold(), item['id']))
            project = {'id': project_id, 'name': name, 'files': files,
                       'default_file': self._defaults.get(project_id, files[0]['id'] if files else None)}
            if history:
                project['build_history'] = True
            projects.append(project)
        return {'projects': projects}

    def resolve(self, project_id, file_id, revision=None):
        with self._lock:
            self._scan()
            entry = self._entries.get((project_id, file_id))
            if entry is None:
                raise FileNotFoundError('Unknown project file')
            if revision is not None:
                pinned = self._revisions.get((project_id, file_id, revision))
                if pinned is None:
                    raise ValueError('File changed; refresh the project before downloading')
                entry = pinned
            return entry

    def working_tree(self, project_id):
        """Read current changes independently of saved build provenance."""
        with self._lock:
            if project_id not in self._project_names:
                self._scan()
            if project_id not in self._project_names:
                raise FileNotFoundError('Unknown project')
            root = self._project_roots[project_id]
        try:
            result = subprocess.run(
                ['git', '-C', str(root), 'status', '--porcelain=v1', '-z',
                 '--untracked-files=normal', '--ignore-submodules=none', '--', '.'],
                capture_output=True, timeout=5, check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return {'state': 'unavailable'}
        return {'state': 'dirty' if result.stdout else 'clean'}

    def download(self, project_id, file_id, revision, format, filename=None):
        if not revision or format not in ('stl', '3mf'):
            raise FileNotFoundError('Unknown download')
        entry = self.resolve(project_id, file_id, revision)
        if entry.kind == 'assembly' and filename is not None:
            from .web import BuildFiles
            files = BuildFiles(entry.path.parent, entry.path)
            return files.download(entry.build_id, format, filename), filename, 'model/' + format
        if filename is not None or format != 'stl':
            raise FileNotFoundError('Unknown download')
        return entry.read_bytes(), entry.path.name, 'model/stl'

    def _record(self, entry):
        key = (entry.project_id, entry.id)
        self._entries[key] = entry
        self._revisions[(*key, entry.revision)] = entry

    def _scan(self):
        self._project_roots = {}
        self._entries = {}
        self._revisions = {}
        self._project_names = {}
        self._defaults = {}
        self._cyclone()
        if not self.workspace.is_dir():
            return
        for project in sorted(self.workspace.iterdir()):
            if (project.name in {'viewer', 'cad-tooling'} or project.resolve() == Path(__file__).resolve().parents[2]
                    or project.name.startswith('.') or not project.is_dir()
                    or project.resolve().parent != self.workspace):
                continue
            project_id = project.name if SAFE_ID.fullmatch(project.name) else 'project-' + _identity(project.name)
            boundary = project.resolve()
            output = project / 'out'
            has_output = output.is_dir() and output.resolve().is_relative_to(boundary)
            try:
                _confined(project / 'pyproject.toml', boundary)
                has_metadata = True
            except OSError:
                has_metadata = False
            if not (has_metadata or has_output):
                continue
            self._project_roots[project_id] = boundary
            self._project_names.setdefault(project_id, project.name.replace('_', ' ').title())
            if project_id != 'cyclone':
                for entry in self._exports(project, project_id):
                    self._record(entry)

    def _cyclone(self):
        files = self.build_files
        paths = list(files.builds.iterdir()) if files.builds.is_dir() else []
        if files.pinned and files.pinned not in paths:
            paths.append(files.pinned)
        presets = {}
        project_root = files.builds.parent.parent
        for preset in sorted((project_root / 'presets').glob('*.toml')):
            try:
                data = _confined(preset, project_root.resolve()).read_bytes()
                presets[hashlib.sha256(data).hexdigest()] = preset.stem
            except (OSError, ValueError):
                continue
        # Each successful build remains independently selectable.
        paths.sort(key=lambda path: (path == files.pinned, path.name))
        default_path = files.pinned or (files.builds.parent / 'latest-successful').resolve()
        for candidate in paths:
            try:
                path, manifest = files.build(candidate.name)
                _confined(path / 'manifest.json', path.resolve())
                metadata = files.metadata(candidate.name)
                configuration = manifest.get('configuration_hash', manifest.get('input_file_hash', 'default'))
                name = presets.get(manifest.get('input_file_hash'))
                name = name.replace('_', ' ').replace('-', ' ').title() if name else (
                    'Assembly' if configuration == 'default' else f'Configuration {configuration[:8]}')
                provenance = _provenance(manifest)
                entry = CatalogEntry('cyclone', 'build-' + candidate.name,
                                     name, 'Configurations', 'assembly', candidate.name, path,
                                     path.resolve(), git=provenance, build_id=candidate.name,
                                     print_note='Part downloads use their recorded print orientation.',
                                     status=metadata['status'], units=metadata['units'])
                self._record(entry)
                self._project_names['cyclone'] = 'Cyclone'
                self._project_roots['cyclone'] = project_root
                if path.resolve() == default_path:
                    self._defaults['cyclone'] = entry.id
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def _exports(self, project, project_id):
        root = project / 'out'
        if not root.is_dir() or not root.resolve().is_relative_to(project.resolve()):
            return
        boundary = root.resolve()
        for directory, children, filenames in os.walk(boundary, followlinks=False):
            children[:] = sorted(name for name in children
                                 if not name.startswith('.') and name not in {'cache', 'source', 'review', 'reviews', 'failed'}
                                 and not (Path(directory) / name).is_symlink())
            manifest_name = 'manifest.json' if 'manifest.json' in filenames else 'build.json'
            if manifest_name not in filenames:
                continue
            output = Path(directory)
            if output.resolve().is_relative_to(boundary):
                yield from self._export_files(output, boundary, project_id, manifest_name)

    def _export_files(self, output, boundary, project_id, manifest_name='build.json'):
        try:
            # A configuration's manifest can only declare its own artifacts.
            output_boundary = output.resolve()
            manifest = json.loads(_confined(output / manifest_name, output_boundary).read_text())
            build_id = None
            if manifest_name == 'manifest.json':
                build_id = manifest.get('build_id')
                if (manifest.get('schema') != 'cad-build-v1' or manifest.get('passed') is not True
                        or not isinstance(build_id, str)
                        or not re.fullmatch(r'\d{8}T\d{12}Z-[a-f0-9]{12}', build_id)
                        or output.name != build_id):
                    return
            if build_id and manifest.get('viewer') == 'viewer.json':
                from .web import BuildFiles
                files = BuildFiles(output.parent, output)
                metadata = files.metadata(build_id)
                file_id = 'build-' + build_id
                if output.resolve() == (boundary / 'latest-successful').resolve():
                    self._defaults[project_id] = file_id
                yield CatalogEntry(project_id, file_id, manifest.get('name', 'Assembly'),
                                   'Configurations', 'assembly', build_id, output, output_boundary,
                                   git=_provenance(manifest), build_id=build_id,
                                   print_note=manifest.get('print_orientation', ''),
                                   status=metadata['status'], units=metadata['units'])
                return
            if not (manifest.get('cad_valid') is True and manifest.get('solids') == 1
                    and manifest.get('stl_watertight') is True
                    and manifest.get('stl_winding_consistent') is True
                    and manifest.get('stl_volume_mm3', 0) > 0):
                return
            for relative, checksum in sorted(manifest.get('files', {}).items()):
                path = output / relative
                if build_id and manifest.get('checksums', {}).get(relative) != checksum:
                    continue
                if (path.suffix.lower() != '.stl' or not isinstance(checksum, str)
                        or not SHA256.fullmatch(checksum) or Path(relative).is_absolute()
                        or '..' in Path(relative).parts):
                    continue
                try:
                    resolved = _confined(path, output_boundary)
                    fingerprint = _fingerprint(resolved)
                except OSError:
                    continue
                is_model = path.stem.endswith('_model')
                note = ('Model-space STL; not transformed to the print orientation.' if is_model else
                        manifest.get('print_orientation', 'Build export; print orientation is not recorded.'))
                configuration = output.relative_to(boundary).as_posix()
                group = (manifest.get('name') or 'Built files') if build_id else (
                    'Built files' if configuration == '.' else 'Built files / ' + configuration)
                file_id = 'export-' + _identity(path.relative_to(boundary).as_posix())
                if not is_model:
                    if output.resolve() == (boundary / 'latest-successful').resolve():
                        self._defaults[project_id] = file_id
                    else:
                        self._defaults.setdefault(project_id, file_id)
                yield CatalogEntry(project_id, file_id, path.name,
                                   group, 'stl', build_id or checksum, path, output_boundary,
                                   git=_provenance(manifest) if build_id else None, build_id=build_id,
                                   print_note=note, status='Completed CAD build',
                                   units=manifest.get('units', 'mm'), checksum=checksum,
                                   fingerprint=fingerprint)
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            return
