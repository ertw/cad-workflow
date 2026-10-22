"""Browser coverage for build progress, failure retention and automatic preview loading."""
import hashlib
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from .build_actions import BuildActions
from .preview import server


def run():
    import trimesh
    with TemporaryDirectory(prefix='build-browser-') as temporary:
        workspace = Path(temporary)
        def export(group, size):
            output = workspace / 'vault/out' / group
            output.mkdir(parents=True)
            path = output / 's4_module_1f_model.stl'
            trimesh.creation.box(extents=[10, 20, size]).export(path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            (output / 'build.json').write_text(json.dumps({
                'cad_valid': True, 'solids': 1, 'stl_watertight': True,
                'stl_winding_consistent': True, 'stl_volume_mm3': 200 * size,
                'units': 'mm', 'files': {path.name: digest},
            }))
            return digest
        export('old', 10)
        count = 0
        def execute(self, data, emit):
            nonlocal count
            count += 1
            assert data['configuration'] == 'S-4-1'
            emit({'progress': 'Generating CAD\nChecking exports\n'})
            if count == 1:
                raise ValueError('Geometry validation failed. Previous preview retained.')
            revision = export('new', 15)
            self.catalog.metadata()
            entry = next(e for e in self.catalog._entries.values() if e.revision == revision)
            return {'project': 'vault', 'file': entry.id, 'revision': entry.revision}
        with patch.object(BuildActions, 'execute', execute), server(3948, workspace=workspace), sync_playwright() as p:
            browser = p.chromium.launch(executable_path=shutil.which('chromium'), headless=True,
                args=['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto('http://127.0.0.1:3948/?project=vault')
            page.wait_for_function("document.querySelector('#cad_viewer').dataset.modelReady === 'true'")
            page.wait_for_function("!document.querySelector('#build-preview').disabled")
            old_url = page.url
            page.locator('#build-preview').click()
            page.wait_for_function("document.querySelector('#build-preview-status').textContent.includes('Geometry validation failed')")
            assert page.url == old_url
            assert page.locator('#cad_viewer').get_attribute('data-model-ready') == 'true'
            assert 'Checking exports' in page.locator('#build-output').inner_text()
            page.locator('#build-preview').click()
            page.wait_for_function("document.querySelector('#cad_viewer').dataset.modelReady === 'true' && location.href !== " + json.dumps(old_url))
            assert 'Geometry validation failed' not in page.locator('#build-preview-status').inner_text()
            assert page.locator('#build-progress').is_hidden()
            assert not errors, errors
            browser.close()
    print('Build browser checks passed: progress, failure retention, configuration, automatic preview loading.')


if __name__ == '__main__':
    run()
