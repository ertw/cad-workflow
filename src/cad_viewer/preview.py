"""Thin lifecycle wrapper around the released standalone OCP viewer."""
from contextlib import contextmanager
from pathlib import Path
import json
import shutil
import threading
import time
import urllib.request


def workspace_files(workspace=None, build_path=None):
    """Resolve all project discovery relative to the selected workspace."""
    from .web import BuildFiles
    from .catalog import project_workspace
    workspace = project_workspace(workspace if workspace is not None else
                                  Path(__file__).resolve().parents[3] / 'projects')
    return workspace, BuildFiles(workspace / 'cyclone/out/builds', build_path)


def latest_build(workspace, build_path=None):
    candidate = Path(build_path) if build_path else workspace / 'cyclone/out/latest-successful'
    if not candidate.exists():
        raise RuntimeError('No successful assembly build is available.')
    return candidate.resolve()


@contextmanager
def server(port=3939, build_path=None, workspace=None):
    from ocp_viewer.server import Viewer, _NotAProbe
    from ocp_viewer.server.pages import missing_assets
    from ocp_viewer.server.sockets import handle
    from ocp_viewer_core.logo import logo
    from websockets.sync.server import serve
    from .web import respond
    from .catalog import ProjectCatalog
    import logging
    if missing_assets():
        raise RuntimeError(f"Viewer assets missing: {missing_assets()}; run just setup")
    viewer = Viewer({'host': '127.0.0.1', 'port': port})
    viewer.backend.load_model(logo)
    workspace, files = workspace_files(workspace, build_path)
    catalog = None if build_path else ProjectCatalog(workspace, files)
    logging.getLogger('websockets.server').addFilter(_NotAProbe())
    try:
        service = serve(lambda ws: handle(viewer, ws), viewer.host, viewer.port,
                        process_request=lambda connection, request: respond(viewer, request, files, catalog),
                        compression=None, max_size=None, open_timeout=120)
    except OSError as exc:
        raise RuntimeError(f"Loopback port {port} unavailable; choose --port. No process was stopped.") from exc
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    try:
        yield viewer
    finally:
        if viewer.browser is not None:
            viewer.browser.close()
        service.shutdown()
        thread.join(timeout=5)


def run(port=3939, build_path=None, workspace=None):
    """Serve catalog pages and the explicit legacy live/pinned assembly stream."""
    from ocp_viewer import show, Collapse, Camera
    from .assembly import viewer_assembly
    workspace, _ = workspace_files(workspace, build_path)
    selected = latest_build(workspace, build_path) if build_path else None
    shape = None
    with server(port, build_path=selected, workspace=workspace) as viewer:
        print(f'CAD workspace: http://127.0.0.1:{port}\nCtrl-C stops this foreground viewer.', flush=True)
        previous = None
        while True:
            # Catalog pages fetch independent scenes; only explicit live/pinned
            # pages register for the legacy workspace stream.
            current = viewer.browser
            if current is not None:
                try:
                    candidate = latest_build(workspace, build_path)
                except RuntimeError:
                    candidate = None
                changed = candidate != selected
                if candidate is not None and (shape is None or changed):
                    shape, label = viewer_assembly(candidate)
                    selected = candidate
                if shape is not None and (current is not previous or changed):
                    show(shape, names=[label], port=port, collapse=Collapse.NONE,
                         reset_camera=Camera.RESET, theme='light', tree_width=430,
                         grid=(False, False, False), progress='')
            previous = current
            time.sleep(0.25)


def verify_assets(port):
    from ocp_viewer.server.pages import COPIED_IN
    results = {}
    for asset in ['', *['static/' + a for a in COPIED_IN]]:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/{asset}', timeout=10) as response:
            data = response.read()
            if response.status != 200 or len(data) < 100:
                raise RuntimeError(f"Viewer asset failed: {asset}")
            results[asset or '/'] = {'bytes': len(data), 'type': response.headers['Content-Type']}
    return results


def browser_check(shape, name, directory, port=3939, build_path=None):
    """Real Chromium + software WebGL; assert a model reached the actual UI."""
    from playwright.sync_api import sync_playwright
    from ocp_viewer import show, Collapse, Camera
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with server(port, build_path=build_path) as viewer, sync_playwright() as playwright:
        assets = verify_assets(port)
        browser = playwright.chromium.launch(executable_path=shutil.which('chromium'), headless=True,
            args=['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        page = browser.new_page(viewport={'width': 1400, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(f'http://127.0.0.1:{port}/?live=1', wait_until='networkidle')
        for _ in range(100):
            if viewer.browser is not None:
                break
            page.wait_for_timeout(100)
        if viewer.browser is None:
            raise RuntimeError('Browser never registered with OCP viewer')
        show(shape, names=[name], port=port, axes=True, grid=(False, False, False), progress=None,
             theme='light', tree_width=430, collapse=Collapse.NONE, reset_camera=Camera.RESET)
        try:
            page.get_by_text(name, exact=False).first.wait_for(timeout=20000)
        except Exception:
            page.screenshot(path=str(directory / 'browser-failure.png'))
            (directory / 'browser-failure.txt').write_text(page.locator('body').inner_text() + '\n' + str(errors), encoding='utf-8')
            raise
        page.wait_for_timeout(1500)
        canvas = page.locator('canvas').first
        if not canvas.is_visible():
            raise RuntimeError('Viewer canvas is not visible')
        page.screenshot(path=str(directory / 'browser.png'))
        result = {'assets': assets, 'browser': browser.version, 'model_name_visible': name,
                  'canvas_visible': True, 'page_errors': errors, 'software_webgl': True}
        if build_path is not None:
            from .browser_download_check import browser_download_check
            def reload_model():
                show(shape, names=[name], port=port, axes=True, grid=(False, False, False), progress=None,
                     theme='light', tree_width=430, collapse=Collapse.NONE, reset_camera=Camera.RESET)
            try:
                result['downloads'] = browser_download_check(page, build_path, reload_model=reload_model)
            except Exception:
                page.screenshot(path=str(directory / 'browser-failure.png'))
                (directory / 'browser-failure.txt').write_text(page.locator('body').inner_text() + '\n' + str(errors), encoding='utf-8')
                raise
            page.screenshot(path=str(directory / 'browser.png'))
        browser.close()
        if errors:
            raise RuntimeError(f'Browser JavaScript errors: {errors}')
        return result
