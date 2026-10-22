"""Real-browser regression checks for independent project/file previews."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import patch
from tempfile import TemporaryDirectory

from playwright.sync_api import sync_playwright

from .preview import server


def ready(page, project, file):
    page.wait_for_function("""([project, file]) => {
        const root = document.querySelector('#cad_viewer');
        return root.dataset.modelReady === 'true' && root.dataset.projectId === project
            && root.dataset.fileId === file;
    }""", arg=[project, file], timeout=60000)


def run(port=3940, safe_graphics=False):
    import trimesh
    output = Path(__file__).resolve().parents[2] / 'out/browser/catalog-check'
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    graphics_query = '&graphics=safe' if safe_graphics else ''
    with TemporaryDirectory(prefix='cad-catalog-') as temporary:
        workspace = Path(temporary)
        (workspace / 'unfinished').mkdir()
        (workspace / 'unfinished/pyproject.toml').write_text('[project]\nname = "unfinished"\n')
        for project, scale in [('alpha', 1), ('beta', 2)]:
            for index, group in enumerate(['short', 'tall']):
                path = workspace / project / 'out' / group / 'module.stl'
                path.parent.mkdir(parents=True)
                trimesh.creation.box(extents=[10 * scale, 20, 10 * (index + 1)]).export(path)
                (path.parent / 'build.json').write_text(json.dumps({
                    'cad_valid': True, 'solids': 1, 'stl_watertight': True,
                    'stl_winding_consistent': True, 'stl_volume_mm3': 2000 * scale * (index + 1),
                    'units': 'mm', 'print_orientation': 'Flat base on the print bed.',
                    'files': {'module.stl': hashlib.sha256(path.read_bytes()).hexdigest()},
                }))
        with server(port, workspace=workspace), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=shutil.which('chromium'), headless=True,
                args=['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
            context = browser.new_context(viewport={'width': 1440, 'height': 1000})
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            base = f'http://127.0.0.1:{port}'
            catalog = context.request.get(base + '/api/projects').json()
            projects = {project['id']: project for project in catalog['projects']}
            assert set(projects) == {'alpha', 'beta', 'unfinished'}, 'Projects escaped the selected workspace'
            first, second = projects['alpha']['files']
            beta = projects['beta']['files'][0]
            try:
                page.goto(f'{base}/?project=alpha&file={first["id"]}{graphics_query}')
                ready(page, 'alpha', first['id'])
                if safe_graphics:
                    assert page.evaluate('cadViewer.renderer.getPixelRatio()') == 1
                    assert page.evaluate('cadViewer.renderer.getContext().getContextAttributes().antialias') is False
                assert page.locator('#cad_viewer canvas').first.is_visible()
                assert page.locator('#selected-component').inner_text() == 'module.stl'
                # Hover alone must not capture. A click retains the GPU-picked geometry.
                context.grant_permissions(['clipboard-read', 'clipboard-write'])
                assert page.locator('#copy-reference').is_disabled()
                canvas = page.locator('#cad_viewer canvas').first.bounding_box()
                page.mouse.move(canvas['x'] + canvas['width'] * .55,
                                canvas['y'] + canvas['height'] * .5)
                page.wait_for_function('Boolean(cadViewer.lastObject)')
                assert page.locator('#copy-reference').is_disabled()
                page.mouse.click(canvas['x'] + canvas['width'] * .55,
                                 canvas['y'] + canvas['height'] * .5)
                page.wait_for_function("!document.querySelector('#copy-reference').disabled")
                hovered = page.evaluate('cadViewer.lastObject.backendId')
                page.keyboard.press('Alt+c')
                page.wait_for_function("document.querySelector('#reference-status').textContent.startsWith('Reference copied')")
                copied = page.evaluate('navigator.clipboard.readText()')
                reference = json.loads(copied.split('\n', 1)[1].split('\nGeometry paths', 1)[0])
                assert reference['geometry_path'] == hovered
                assert reference['revision'] == first['revision']
                assert reference['project'] == 'alpha'
                assert reference['world_bounds'] and reference['camera']
                page.locator('#copy-reference').click()
                assert page.evaluate('navigator.clipboard.readText()') == copied
                # Clipboard denial exposes selectable text instead of losing the reference.
                page.evaluate("() => { navigator.clipboard.writeText = async () => { throw new Error('denied'); }; }")
                page.locator('#copy-reference').click()
                page.locator('#reference-text').wait_for(state='visible')
                assert page.locator('#reference-text').input_value() == copied

                # Find a real GPU-pickable edge, then click its exact pixel.
                edge = page.evaluate("""() => {
                    const rect = cadViewer.renderer.domElement.getBoundingClientRect();
                    for (let y = 10; y < rect.height - 10; y += 5) {
                        for (let x = 10; x < rect.width - 10; x += 5) {
                            const hit = cadViewer.idPicker.pickAt(x, y);
                            if (hit?.info.topo === 'edge') return {x: rect.x + x, y: rect.y + y, path: hit.info.path};
                        }
                    }
                    return null;
                }""")
                assert edge, 'No pickable edge in the box fixture'
                page.mouse.click(edge['x'], edge['y'])
                page.wait_for_function("document.querySelector('#reference-status').textContent.startsWith('Captured edge:')")
                page.locator('#copy-reference').click()
                edge_reference = json.loads(page.locator('#reference-text').input_value().split('\n', 1)[1].split('\nGeometry paths', 1)[0])
                assert edge_reference['geometry_path'] == edge['path']
                assert edge_reference['edge_endpoints'] and edge_reference['world_bounds']
                # Pointer movement and orbiting must not overwrite the locked reference.
                page.mouse.move(canvas['x'] + canvas['width'] * .55, canvas['y'] + canvas['height'] * .5)
                page.locator('#copy-reference').click()
                assert edge['path'] in page.locator('#reference-text').input_value()
                received = []
                real_run = subprocess.run
                def fake_agent(args, **kwargs):
                    if 'exec' not in args or '--output-last-message' not in args:
                        return real_run(args, **kwargs)
                    received.append(kwargs)
                    Path(args[args.index('--output-last-message') + 1]).write_text('Test agent received the selected edge.')
                    return SimpleNamespace(returncode=0)
                with patch('cad_viewer.agent_actions.subprocess.run', side_effect=fake_agent):
                    page.locator('#agent-message').fill('Round only this edge')
                    page.locator('#send-agent').click()
                    page.wait_for_function("document.querySelector('#agent-status').textContent.startsWith('Codex replied')")
                assert len(received) == 1
                assert received[0]['cwd'] == workspace / 'alpha'
                assert edge['path'] in received[0]['input']
                assert 'Round only this edge' in received[0]['input']
                assert page.locator('#agent-reply').inner_text() == 'Test agent received the selected edge.'
                assert page.locator('#agent-message').input_value() == 'Round only this edge'

                with page.expect_download() as download:
                    page.locator('#download-stl').click()
                actual = Path(download.value.path()).read_bytes()
                original = workspace / 'alpha/out/short/module.stl'
                assert hashlib.sha256(actual).digest() == hashlib.sha256(original.read_bytes()).digest()
                assert download.value.suggested_filename == 'module.stl'
                page.locator('#file-search').fill('tall')
                assert page.locator('.file-button').count() == 1
                page.locator('.file-button').click()
                ready(page, 'alpha', second['id'])
                assert page.locator('#copy-reference').is_disabled()
                assert page.locator('#reference-text').input_value() == ''
                second_url = page.url
                page.go_back()
                ready(page, 'alpha', first['id'])
                page.go_forward()
                ready(page, 'alpha', second['id'])
                page.reload()
                ready(page, 'alpha', second['id'])
                assert page.url == second_url
                other = context.new_page()
                other.on('pageerror', lambda error: errors.append(str(error)))
                other.goto(f'{base}/?project=beta&file={beta["id"]}{graphics_query}')
                ready(other, 'beta', beta['id'])
                page.locator('#project-select').select_option('beta')
                ready(page, 'beta', beta['id'])
                page.locator('#project-select').select_option('alpha')
                ready(page, 'alpha', first['id'])
                ready(other, 'beta', beta['id'])
                # Rapid consecutive clicks must finish on the last requested file.
                page.evaluate("""ids => {
                    document.querySelector(`[data-file-id="${ids[1]}"]`).click();
                    document.querySelector(`[data-file-id="${ids[0]}"]`).click();
                }""", [first['id'], second['id']])
                ready(page, 'alpha', first['id'])
                page.route('**/scene?*', lambda route: route.fulfill(status=409, body='{}'))
                page.locator(f'[data-file-id="{second["id"]}"]').click()
                page.locator('#scene-state[data-state="error"]').wait_for()
                assert page.locator('#cad_viewer').get_attribute('data-model-ready') == 'false'
                assert page.locator('#download-stl').is_hidden()
                assert not page.locator('#download-stl').get_attribute('href')
                page.unroute('**/scene?*')
                page.locator('#retry-scene').click()
                ready(page, 'alpha', second['id'])
                page.goto(f'{base}/?project=alpha&file=missing{graphics_query}')
                page.get_by_text('File not found', exact=True).wait_for()
                assert page.locator('#download-stl').is_hidden()
                page.locator(f'[data-file-id="{first["id"]}"]').click()
                ready(page, 'alpha', first['id'])
                page.locator('#project-select').select_option('unfinished')
                page.get_by_text('No files yet', exact=True).wait_for()
                assert page.locator('#download-stl').is_hidden()
                assert page.locator('#cad_viewer').get_attribute('data-model-ready') == 'false'
                page.locator('#project-select').select_option('alpha')
                ready(page, 'alpha', first['id'])
                page.screenshot(path=str(output / 'desktop.png'))
                page.set_viewport_size({'width': 390, 'height': 844})
                page.screenshot(path=str(output / 'mobile.png'), full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.evaluate("cadViewer.renderer.getContext().getExtension('WEBGL_lose_context').loseContext()")
                page.get_by_text('3D graphics stopped', exact=True).wait_for()
                assert page.locator('#cad_viewer').get_attribute('data-model-ready') == 'false'
                assert page.locator('#download-stl').is_hidden()
                page.locator(f'[data-file-id="{second["id"]}"]').click()
                assert page.locator('#scene-state-title').inner_text() == '3D graphics stopped'
                # Exercise commit history using deterministic catalog metadata.
                history_page = context.new_page()
                history_page.on('pageerror', lambda error: errors.append(str(error)))
                builds = []
                for index, commit in enumerate(['a', 'b', 'b', None]):
                    builds.append({'id': f'build-{index}', 'name': 'Baseline', 'kind': 'assembly',
                        'group': 'Configurations', 'revision': f'202610{10 + index}T120000000000Z-test',
                        'git': {'commit': commit * 40, 'message': f'Commit {commit}\n\nDetails',
                                'committed_at': f'2026-10-{10 if commit == "a" else 11}T12:00:00Z',
                                'dirty': index == 2} if commit else None})
                builds.reverse()
                history_page.route('**/api/projects', lambda route: route.fulfill(json={'projects': [
                    {'id': 'cyclone', 'name': 'Cyclone', 'default_file': 'build-3', 'files': builds}]}))
                history_page.route('**/scene?*', lambda route: route.fulfill(status=409, body='{}'))
                history_page.goto(base + '/?project=cyclone')
                history_page.locator('.file-group summary').first.wait_for()
                headings = history_page.locator('.file-group summary').all_text_contents()
                assert headings[0].startswith('Commit b') and headings[1].startswith('Commit a')
                assert headings[2] == 'Unlinked builds'
                newest = history_page.locator('details.file-group').first
                newest.locator('summary').click()
                assert newest.locator('.file-button').all_text_contents()[0].endswith('Uncommitted changes')
                newest.locator('.file-button').first.click()
                assert 'file=build-2' in history_page.url
                history_page.locator('#file-search').fill('Commit a')
                assert history_page.locator('.file-button').count() == 1
                assert history_page.locator('.file-button').is_visible()
                history_page.locator('#file-search').fill('')
                history_page.screenshot(path=str(output / 'commit-history.png'), full_page=True)
                history_page.close()
                assert not errors, errors
                result = {'passed': True, 'browser': browser.version, 'page_errors': errors,
                          'safe_graphics': safe_graphics,
                          'checks': ['export download bytes', 'group search', 'history', 'reload',
                                     'project switching', 'independent tabs', 'rapid selection',
                                     'failure clearing', 'retry', 'unknown link', 'empty project', 'mobile width',
                                     'graphics context loss stops retries', 'commit groups and build order',
                                     'unlinked builds', 'commit search and selection',
                                     'click reference shortcut and button', 'edge click and agent message', 'clipboard fallback',
                                     'reference cleared on file change']}
                (output / 'report.json').write_text(json.dumps(result, indent=2) + '\n')
                print(json.dumps(result, indent=2))
            except Exception:
                page.screenshot(path=str(output / 'failure.png'), full_page=True)
                (output / 'failure.txt').write_text(page.locator('body').inner_text() + '\n' + str(errors))
                raise
            finally:
                browser.close()
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=3940)
    parser.add_argument('--safe-graphics', action='store_true')
    args = parser.parse_args()
    run(args.port, args.safe_graphics)
