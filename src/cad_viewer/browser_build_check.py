"""Browser coverage for declared parts, first builds and failure retention."""
import hashlib
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from .build_actions import BuildActions
from .preview import server


def run(screenshot=None):
    import trimesh
    with TemporaryDirectory(prefix='build-browser-') as temporary:
        workspace = Path(temporary)
        root = workspace / 'vault'
        root.mkdir()
        (root / 'module.py').write_text('# fixture\n')
        (root / 'facade.py').write_text('# fixture\n')
        (root / 'partcad.json').write_text(json.dumps({'parts': {
            's4_module_1f': {'type': 'build123d', 'desc': 'S4 Module 1F', 'path': 'module.py',
                'parameters': {'family': {'type': 'string', 'default': 'S'},
                               'depth_units': {'type': 'int', 'default': 4},
                               'height_units': {'type': 'int', 'default': 1}}},
            's1f_facade': {'type': 'build123d', 'desc': 'S1F Facade', 'path': 'facade.py'}}}))
        (root / 'cad-builds.json').write_text(json.dumps({'version': 1, 'default': 's4_module_1f', 'targets': {
            's4_module_1f': {'object': 'parts/s4_module_1f', 'adapter': 'vault-module'},
            's1f_facade': {'object': 'parts/s1f_facade', 'adapter': 'vault-facade'}}}))

        def export(group, slug, size):
            output = root / 'out' / group
            output.mkdir(parents=True)
            path = output / f'{slug}.stl'
            trimesh.creation.box(extents=[10, 20, size]).export(path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            (output / 'build.json').write_text(json.dumps({
                'cad_valid': True, 'solids': 1, 'stl_watertight': True,
                'stl_winding_consistent': True, 'stl_volume_mm3': 200 * size,
                'module': {'family': 'XL' if slug.startswith('xl') else 'S', 'depth_units': 4, 'height_units': 1},
                'units': 'mm', 'files': {path.name: digest},
            }))
            return digest

        # An experimental export must not select an undeclared build target.
        export('experimental', 'xl4_module_1f', 30)
        count = 0

        def execute(self, data, emit):
            nonlocal count
            count += 1
            assert data['configuration'] == ('s1f_facade' if count <= 2 else 's4_module_1f')
            emit({'progress': 'Generating CAD\nChecking exports\n'})
            if count in (1, 4):
                raise ValueError('Geometry validation failed. Previous preview retained.')
            revision = export(f'build-{count}', data['configuration'], 10 + count)
            self.catalog.metadata()
            entry = next(e for e in self.catalog._entries.values() if e.revision == revision)
            return {'project': 'vault', 'file': entry.id, 'revision': entry.revision}

        with patch.object(BuildActions, 'execute', execute), server(3948, workspace=workspace), sync_playwright() as p:
            browser = p.chromium.launch(executable_path=shutil.which('chromium'), headless=True,
                args=['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
            page = browser.new_page(viewport={'width': 1440, 'height': 1100})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto('http://127.0.0.1:3948/?project=vault')
            page.wait_for_function("!document.querySelector('#build-preview').disabled")
            assert page.locator('#latest-parts-list li').count() == 2
            assert page.locator('#cad_viewer').get_attribute('data-model-ready') == 'false'
            assert page.locator('#build-configuration option').all_text_contents() == [
                'Choose configuration', 'S4 Module 1F', 'S1F Facade']
            page.get_by_role('button', name='S1F Facade: No matching build. Select build configuration').click()
            page.wait_for_function("document.querySelector('#build-configuration').value === 's1f_facade'")
            page.locator('#build-preview').click()
            page.wait_for_function("document.querySelector('#build-preview-status').textContent.includes('Geometry validation failed')")
            assert page.locator('#cad_viewer').get_attribute('data-model-ready') == 'false'
            page.locator('#build-preview').click()
            page.wait_for_function("document.querySelector('#cad_viewer').dataset.modelReady === 'true'")
            page.wait_for_function("!document.querySelector('#build-preview').disabled")
            assert 'Geometry validation failed' not in page.locator('#build-preview-status').inner_text()
            page.locator('#build-configuration').select_option('s4_module_1f')
            previous = page.url
            page.locator('#build-preview').click()
            page.wait_for_function("document.querySelector('#cad_viewer').dataset.modelReady === 'true' && location.href !== " + json.dumps(previous))
            page.wait_for_function("!document.querySelector('#build-preview').disabled")
            previous = page.url
            page.locator('#build-preview').click()
            page.wait_for_function("document.querySelector('#build-preview-status').textContent.includes('Geometry validation failed')")
            assert page.url == previous
            assert page.locator('#cad_viewer').get_attribute('data-model-ready') == 'true'
            assert 'Checking exports' in page.locator('#build-output').inner_text()
            assert page.locator('#build-progress').is_hidden()
            assert page.locator('#latest-parts-list li').count() == 2
            assert not errors, errors
            if screenshot:
                page.screenshot(path=str(screenshot), full_page=True)
            browser.close()
    print('Build browser checks passed: declared catalog, missing previews, facade selection, first builds, progress and failure retention.')


if __name__ == '__main__':
    run()
