"""Exercise native fillet preview and immutable export with real browser input."""
import hashlib
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from .browser_tools_check import edge_target, select_tool
from .preview import server


def _box_build(root):
    from build123d import Box, export_step, export_stl
    from cad_build import run_build

    (root / 'src').mkdir(parents=True)
    (root / 'src/model.py').write_text('from build123d import Box\nshape = Box(20, 20, 20)\n')

    def generate(state):
        shape = Box(20, 20, 20)
        (state.stage / 'parts').mkdir()
        (state.stage / 'stl').mkdir()
        export_step(shape, state.stage / 'parts/box.step')
        export_stl(shape, state.stage / 'stl/box.stl')
        return ({'passed': True, 'units': 'mm', 'status': 'Browser fixture',
                 'name': 'Box', 'parts': {'box': {
                     'kind': 'printed', 'print_note': 'Model orientation.',
                     'print_bbox_mm': [20, 20, 20],
                 }}},
                {'components': [{'name': 'box', 'kind': 'printed', 'step': 'parts/box.step'}]})

    return run_build(root, 'browser-fillet-box', generate, parameters={})


def _pick_edge(page):
    """Locate a rendered edge, then capture it with a real canvas click."""
    edge = edge_target(page)
    page.mouse.click(edge['x'], edge['y'])
    page.wait_for_function("!document.querySelector('#fillet-preview').disabled")


def run(port=3940, safe_graphics=False, output=None):
    from cad_build import verify_files
    from .fillet_actions import FilletActions

    output = Path(output or Path(__file__).resolve().parents[2] / 'out/browser/fillet-check')
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    with TemporaryDirectory(prefix='cad-fillet-browser-') as temporary:
        workspace = Path(temporary)
        original = _box_build(workspace / 'native')
        (workspace / 'empty').mkdir()
        (workspace / 'empty/pyproject.toml').write_text('[project]\nname = "empty"\n')
        original_manifest = verify_files(original)
        original_bytes = {str(path.relative_to(original)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in original.rglob('*') if path.is_file()}
        with server(port, workspace=workspace), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=shutil.which('chromium'), headless=True,
                args=['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            try:
                graphics = '&graphics=safe' if safe_graphics else ''
                page.goto(f'http://127.0.0.1:{port}/?project=native{graphics}')
                page.wait_for_function("document.querySelector('#cad_viewer').dataset.modelReady === 'true'")
                original_url = page.url
                select_tool(page, 'fillet', 'fillet')
                assert page.locator('#fillet-preview').is_disabled()
                _pick_edge(page)
                page.locator('#fillet-radius').fill('1')
                page.locator('#fillet-preview').click()
                page.wait_for_function("!document.querySelector('#fillet-apply').disabled", timeout=60000)
                assert page.url == original_url, 'An uncommitted preview changed the selected build'
                assert list((workspace / 'native/out/builds').iterdir()) == [original]
                page.screenshot(path=str(output / 'preview.png'))

                # The button must not apply a preview using a superseded radius.
                page.locator('#fillet-radius').fill('2')
                assert page.locator('#fillet-apply').is_disabled()
                page.locator('#fillet-preview').click()
                page.wait_for_function("!document.querySelector('#fillet-apply').disabled", timeout=60000)
                page.locator('#fillet-cancel').click()
                assert page.locator('#fillet-apply').is_disabled()
                assert page.url == original_url
                assert list((workspace / 'native/out/builds').iterdir()) == [original]

                # A completed response for an earlier project must not replace
                # the current preview when the user navigates during kernel work.
                prepared, release = Event(), Event()
                native_preview = FilletActions.preview

                def delayed_preview(actions, data):
                    result = native_preview(actions, data)
                    prepared.set()
                    if not release.wait(30):
                        raise RuntimeError('Browser did not release the delayed preview')
                    return result

                with patch.object(FilletActions, 'preview', delayed_preview):
                    _pick_edge(page)
                    with page.expect_websocket() as pending_socket:
                        page.locator('#fillet-preview').click()
                    socket = pending_socket.value
                    try:
                        assert prepared.wait(30), 'Native preview did not complete'
                        page.keyboard.press('Control+k')
                        page.keyboard.type('empty')
                        page.keyboard.press('Enter')
                        page.get_by_text('No files yet', exact=True).wait_for()
                    finally:
                        release.set()
                    socket.wait_for_event('framereceived', timeout=30000)
                    page.evaluate('() => new Promise(requestAnimationFrame)')
                assert 'project=empty' in page.url
                assert page.locator('#cad_viewer').get_attribute('data-model-ready') == 'false'
                assert page.locator('#fillet-apply').is_disabled()
                page.keyboard.press('Control+k')
                page.keyboard.type('native')
                page.keyboard.press('Enter')
                page.wait_for_function("document.querySelector('#cad_viewer').dataset.modelReady === 'true'")

                _pick_edge(page)
                page.locator('#fillet-preview').click()
                page.wait_for_function("!document.querySelector('#fillet-apply').disabled", timeout=60000)
                page.locator('#fillet-apply').click()
                page.wait_for_function("""url => {
                    const root = document.querySelector('#cad_viewer');
                    return root.dataset.modelReady === 'true' && location.href !== url;
                }""", arg=original_url, timeout=60000)
                builds = list((workspace / 'native/out/builds').iterdir())
                assert len(builds) == 2
                derived = next(build for build in builds if build != original)
                manifest = verify_files(derived)
                assert manifest['passed'] is True
                assert manifest['cad_valid'] and manifest['step_roundtrip']['passed']
                assert manifest['stl_watertight'] and manifest['stl_winding_consistent']
                assert 0 < manifest['stl_volume_mm3'] < 20 ** 3
                operation = json.loads((derived / manifest['operation']).read_text())
                assert operation['type'] == 'fillet' and operation['radius_mm'] == 2
                assert operation['base']['revision'] == original.name
                assert operation['selection']['component'] == 'box'
                assert operation['selection']['topology'] == 'edge'
                assert (workspace / 'native/out/latest-successful').resolve() == original
                assert manifest['build_id'] in page.url
                assert original_manifest == verify_files(original)
                assert original_bytes == {str(path.relative_to(original)): hashlib.sha256(path.read_bytes()).hexdigest()
                                          for path in original.rglob('*') if path.is_file()}
                assert page.locator('#fillet-radius').input_value() == '2'
                select_tool(page, 'export', 'export')
                page.locator('#parts-list button[data-component="box_fillet"]').click()
                for extension in ('step', 'stl'):
                    with page.expect_download() as pending:
                        page.locator(f'#download-{extension}').click()
                    actual = Path(pending.value.path()).read_bytes()
                    assert hashlib.sha256(actual).hexdigest() == manifest['checksums'][
                        f'{extension}/box_fillet.{extension}']
                page.screenshot(path=str(output / 'applied.png'))

                # Editing a saved operation starts from its original source,
                # so changing radius replaces the fillet instead of stacking it.
                select_tool(page, 'fillet', 'fillet')
                page.locator('#fillet-edit').click()
                page.wait_for_function("""revision => {
                    const root = document.querySelector('#cad_viewer');
                    return root.dataset.modelReady === 'true' && root.dataset.fileId === 'build-' + revision;
                }""", arg=original.name)
                assert page.locator('#fillet-radius').input_value() == '2'
                page.locator('#fillet-radius').fill('1.5')
                page.locator('#fillet-preview').click()
                page.wait_for_function("!document.querySelector('#fillet-apply').disabled", timeout=60000)
                page.locator('#fillet-apply').click()
                page.wait_for_function("""old => {
                    const root = document.querySelector('#cad_viewer');
                    return root.dataset.modelReady === 'true' && !old.includes(root.dataset.buildId);
                }""", arg=[original.name, derived.name], timeout=60000)
                edited = next(build for build in (workspace / 'native/out/builds').iterdir()
                              if build not in (original, derived))
                edited_manifest = verify_files(edited)
                edited_operation = json.loads((edited / edited_manifest['operation']).read_text())
                assert edited_operation['base'] == operation['base']
                assert edited_operation['radius_mm'] == 1.5
                assert (workspace / 'native/out/latest-successful').resolve() == original
                assert not errors, errors
                result = {'passed': True, 'page_errors': errors, 'safe_graphics': safe_graphics,
                          'source_build': original.name, 'fillet_build': derived.name,
                          'checks': ['native edge picking', 'fillet preview', 'radius invalidates preview',
                                     'cancel preview', 'stale preview after navigation',
                                     'immutable fillet build', 'checksummed STEP and STL downloads',
                                     'edit saved fillet radius from original source']}
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
    run()
