"""Shared immutable build storage; geometry and validation belong to each project."""
from datetime import datetime, timezone
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import uuid

from .provenance import git_provenance


catalog_target = ContextVar('catalog_target', default=None)


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + '\n')


def source_paths(root):
    root = Path(root)
    paths = {str(p.relative_to(root)): p for directory in ('src', 'tests', 'catalog')
             for p in (root / directory).rglob('*')
             if p.is_file() and p.suffix in {'.py', '.html', '.js', '.css', '.json', '.c'}}
    for name in ('pyproject.toml', 'uv.lock', 'flake.nix', 'flake.lock', 'justfile',
                 'partcad.json', 'cad-builds.json'):
        if (root / name).is_file():
            paths[name] = root / name
    paths.update({f'_build_system/{p.name}': p for p in Path(__file__).parent.glob('*.py')})
    return paths


def source_hashes(root):
    return {name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in source_paths(root).items()}


def source_digest(hashes):
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


def set_latest(path, pointer):
    path, pointer = Path(path), Path(pointer)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    temporary = pointer.with_name(f'.latest-{uuid.uuid4().hex}')
    try:
        temporary.symlink_to(os.path.relpath(path, pointer.parent))
        os.replace(temporary, pointer)
    finally:
        temporary.unlink(missing_ok=True)


def freeze(path):
    for file in path.rglob('*'):
        if file.is_symlink():
            raise ValueError('Build artifacts cannot be symbolic links')
        file.chmod(0o555 if file.is_dir() else 0o444)
    path.chmod(0o555)


def verify_files(path):
    path = Path(path).resolve()
    manifest = json.loads((path / 'manifest.json').read_text())
    if manifest.get('passed') is not True:
        raise RuntimeError('Build is not successful')
    for relative, digest in manifest['checksums'].items():
        file = path / relative
        if (not file.resolve().is_relative_to(path) or not file.is_file()
                or hashlib.sha256(file.read_bytes()).hexdigest() != digest):
            raise RuntimeError(f'Artifact checksum failed: {relative}; rebuild rather than modifying a completed build')
    return manifest


class Build:
    """Stage a build and publish only after its caller's checks have passed.

    The source snapshot includes this library. Source or input changes during a
    build cause failure. Completed directories are never reused or modified.
    """
    def __init__(self, root, configuration_hash, *, output=None, inputs=None, promote=True):
        self.root = Path(root).resolve()
        self.output = Path(output) if output is not None else self.root / 'out'
        self.output = self.output.resolve()
        self.configuration_hash = configuration_hash
        self.inputs = {name: Path(path) for name, path in (inputs or {}).items()}
        self.input_contents = {name: path.read_bytes() for name, path in self.inputs.items()}
        self.git = git_provenance(self.root)
        self.contents = {name: path.read_bytes() for name, path in source_paths(self.root).items()}
        self.hashes = {name: hashlib.sha256(data).hexdigest() for name, data in self.contents.items()}
        self.source_hash = source_digest(self.hashes)
        self.content_hash = hashlib.sha256((self.source_hash + configuration_hash).encode()).hexdigest()
        self.id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + self.content_hash[:12]
        self.stage = self.output / '.building' / self.id
        self.destination = self.output / 'builds' / self.id
        self.promote = promote
        self.completed = False

    def __enter__(self):
        self.stage.mkdir(parents=True, exist_ok=False)
        return self

    def complete(self, metadata):
        if metadata.get('passed') is not True:
            raise ValueError('Project validation must pass before publishing a build')
        if (source_hashes(self.root) != self.hashes
                or any(path.read_bytes() != self.input_contents[name] for name, path in self.inputs.items())):
            raise RuntimeError('Source or inputs changed during this build; rerun with stable source. Previous success is preserved.')
        for relative, data in self.contents.items():
            target = self.stage / 'source' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for relative, data in self.input_contents.items():
            target = self.stage / relative
            if not target.resolve().is_relative_to(self.stage.resolve()):
                raise ValueError('Input snapshot must stay inside the build')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        checksums = {}
        for path in sorted(self.stage.rglob('*')):
            if path.is_symlink():
                raise ValueError('Build artifacts cannot be symbolic links')
            if path.is_file():
                checksums[str(path.relative_to(self.stage))] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = {**metadata, 'schema': 'cad-build-v1', 'build_id': self.id,
                    'git': self.git, 'configuration_hash': self.configuration_hash,
                    'content_hash': self.content_hash, 'source_hash': self.source_hash,
                    'source_files': self.hashes, 'checksums': checksums}
        write_json(self.stage / 'manifest.json', manifest)
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self.stage.rename(self.destination)
        freeze(self.destination)
        self.completed = True
        if self.promote:
            set_latest(self.destination, self.output / 'latest-successful')
        return self.destination

    def __exit__(self, kind, error, traceback):
        if not self.completed and self.stage.exists():
            write_json(self.stage / 'failure.json', {'build_id': self.id,
                       'error': str(error or 'Build exited without completing'),
                       'source_hash': self.source_hash, 'configuration_hash': self.configuration_hash})
            failed = self.output / 'failed' / self.id
            failed.parent.mkdir(parents=True, exist_ok=True)
            self.stage.rename(failed)
        if kind is None and not self.completed:
            raise RuntimeError('Build exited without completing')


def run_build(root, configuration_hash, generate, *, parameters, inputs=None,
              output=None, promote=True, aliases=()):
    """Common project pipeline: stage, generate/check, describe, freeze, publish.

    ``generate(state)`` exports and validates project geometry, then returns
    (manifest metadata, viewer description). Only validated results are published.
    """
    with Build(root, configuration_hash, inputs=inputs, output=output, promote=promote) as state:
        print(f'Build {state.id}: generating and validating CAD', flush=True)
        metadata, viewer = generate(state)
        if metadata.get('passed') is not True:
            raise ValueError('Project validation must pass before publishing a build')
        print('Writing preview metadata and publishing validated artifacts', flush=True)
        write_json(state.stage / 'resolved-parameters.json', parameters)
        write_json(state.stage / 'viewer.json', {**viewer, 'build_id': state.id})
        if catalog_target.get() is not None:
            metadata = {**metadata, 'catalog_target': catalog_target.get()}
        destination = state.complete({**metadata, 'viewer': 'viewer.json'})
        if promote:
            for alias in aliases:
                pointer = state.output / alias
                if Path(alias).is_absolute() or '..' in Path(alias).parts:
                    raise ValueError('Build alias must stay inside the output directory')
                set_latest(destination, pointer)
    print(f'Success: {destination}', flush=True)
    return destination
