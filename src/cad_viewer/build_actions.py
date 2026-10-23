"""Build known project configurations and stream output to the requesting viewer."""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import time
from threading import Lock


class BuildActions:
    def __init__(self, catalog, lock=None):
        self.catalog = catalog
        self.lock = lock if lock is not None else Lock()

    def options(self, data):
        project = data.get('project')
        if project not in ('vault', 'cyclone'):
            raise ValueError('Preview builds are supported for Vault and Cyclone.')
        with self.catalog._lock:
            self.catalog._scan()
            root = self.catalog._project_roots.get(project)
            if root is None:
                raise ValueError('Unknown project.')
            entry = self.catalog.resolve(project, data['file'], data.get('revision')) if data.get('file') else None
        from cad_catalog import load
        declaration = load(root)
        options = [{'id': target.id, 'label': target.label} for target in declaration.targets]
        default = declaration.default
        return root, entry, {'options': options, 'default': default}

    def command(self, data, receipt):
        root, entry, options = self.options(data)
        configuration = data.get('configuration')
        if configuration not in [p['id'] for p in options['options']]:
            raise ValueError('Choose a build configuration.')
        from cad_catalog import load
        target = load(root).target(configuration)
        expected = 'vault' if target.adapter.startswith('vault-') else 'cyclone'
        if data['project'] != expected:
            raise ValueError('Build adapter does not match this project.')
        python = root / '.venv/bin/python'
        if not python.is_file():
            raise ValueError('Project environment missing. Run the project setup command first.')
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join([str(root / 'src'), str(Path(__file__).resolve().parents[1])]), 'PYTHONUNBUFFERED': '1',
               'MPLCONFIGDIR': str(root / '.cache/matplotlib')}
        return root, entry, [str(python), '-u', '-m', 'cad_build.runner', data['project'],
                             target.configuration, '--receipt', str(receipt),
                             '--catalog-target', target.id, '--catalog-object', target.object], env

    def result(self, project, root, entry, receipt):
        destination = Path(receipt.read_text()).resolve()
        if destination.parent != (root / 'out/builds').resolve():
            raise ValueError('Build returned an unexpected output directory.')
        manifest = json.loads((destination / 'manifest.json').read_text())
        if manifest.get('passed') is not True or manifest.get('build_id') != destination.name:
            raise ValueError('Build validation did not pass.')
        with self.catalog._lock:
            self.catalog._scan()
            entries = [e for e in self.catalog._entries.values()
                       if e.project_id == project and e.build_id == destination.name]
        if not entries:
            raise ValueError('Build completed but no preview was published.')
        selected = next((e for e in entries if entry and e.path.name == entry.path.name), entries[0])
        return {'project': project, 'file': selected.id, 'revision': selected.revision}

    def execute(self, data, emit):
        if not self.lock.acquire(blocking=False):
            raise ValueError('A build or agent request is already running. Wait for it to finish.')
        try:
            with tempfile.TemporaryDirectory(prefix='viewer-build-') as directory:
                receipt = Path(directory) / 'result.txt'
                root, entry, command, env = self.command(data, receipt)
                emit({'progress': 'Starting build and validation…\n'})
                with subprocess.Popen(command, cwd=root, env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, start_new_session=True) as process:
                    try:
                        with selectors.DefaultSelector() as selector:
                            selector.register(process.stdout, selectors.EVENT_READ)
                            deadline = time.monotonic() + 1800
                            while selector.get_map():
                                if time.monotonic() > deadline:
                                    raise ValueError('Build timed out after 30 minutes. Previous preview retained.')
                                for key, _ in selector.select(timeout=1):
                                    chunk = os.read(key.fileobj.fileno(), 4096)
                                    if chunk:
                                        emit({'progress': chunk.decode(errors='replace')})
                                    else:
                                        selector.unregister(key.fileobj)
                            code = process.wait(timeout=max(1, deadline - time.monotonic()))
                        if code:
                            raise ValueError(f'Build failed (exit {code}). See the build output. Previous preview retained.')
                    finally:
                        if process.poll() is None:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
                return self.result(data['project'], root, entry, receipt)
        finally:
            self.lock.release()

    def handle(self, socket):
        def emit(value):
            socket.send(json.dumps(value))
        try:
            raw = socket.recv(timeout=15)
            if len(raw) > 10000:
                raise ValueError('Build request is too large.')
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError('Expected a build request.')
            if data.get('action') == 'options':
                emit({'result': self.options(data)[2]})
            elif data.get('action') == 'build':
                emit({'result': self.execute(data, emit)})
            else:
                raise ValueError('Unknown build action.')
        except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
            emit({'error': str(error)})
