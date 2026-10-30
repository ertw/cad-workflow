"""Exercise the embedded viewer with real Chromium input and file downloads."""

import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlparse
import zipfile

from .browser_tools_check import select_tool


def _selected(page, name):
    page.wait_for_function(
        """name => {
            const heading = document.querySelector('#selected-component');
            const link = document.querySelector('#download-3mf');
            return heading?.textContent.trim() === name && link?.href &&
                !link.hidden && link.getClientRects().length > 0;
        }""",
        arg=name,
        timeout=15000,
    )


def _cleared(page):
    page.wait_for_function(
        """() => ['#download-3mf', '#download-stl'].every(selector => {
            const link = document.querySelector(selector);
            return link && (!link.getAttribute('href') || link.hidden ||
                link.getClientRects().length === 0);
        })""",
        timeout=15000,
    )


def _download(page, build_path, manifest, name, extension='3mf'):
    """Follow the actual browser link and match bytes to the immutable artifact."""
    relative = f'{extension}/{name}.{extension}'
    expected = manifest['checksums'][relative]
    source = build_path / relative
    if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
        raise RuntimeError(f'Immutable artifact checksum failed: {relative}')
    link = page.locator(f'#download-{extension}')
    href = link.get_attribute('href')
    path = unquote(urlparse(href).path)
    if manifest['build_id'] not in path.split('/'):
        raise RuntimeError(f'Download is not pinned to the displayed build: {href}')
    with page.expect_download(timeout=30000) as pending:
        link.click()
    download = pending.value
    failure = download.failure()
    if failure:
        raise RuntimeError(f'Browser download failed: {failure}')
    actual = Path(download.path())
    if hashlib.sha256(actual.read_bytes()).hexdigest() != expected:
        raise RuntimeError(f'Download differs from the displayed build: {name}.{extension}')
    if not download.suggested_filename.endswith(f'.{extension}'):
        raise RuntimeError(f'Wrong download filename: {download.suggested_filename}')
    if extension == '3mf':
        with zipfile.ZipFile(actual) as archive:
            if archive.testzip() or not any(n.endswith('.model') for n in archive.namelist()):
                raise RuntimeError(f'Download is not a valid 3MF package: {name}')
    return {'component': name, 'format': extension, 'sha256': expected,
            'url': href, 'filename': download.suggested_filename, 'bytes': actual.stat().st_size}


def _canvas_target(page, names):
    """Locate a visible face using OCP's GPU picker; never synthesize a selection.

    The picker is used only to obtain a coordinate and its independently observed
    owner. A subsequent Playwright mouse double-click must drive the UI itself.
    This avoids fragile hard-coded pixels when geometry or camera bounds change.
    """
    page.locator('#cad_viewer canvas').first.scroll_into_view_if_needed()
    result = page.evaluate(
        r"""names => {
            const viewer = window.cadViewer;
            const canvas = viewer.renderer.domElement;
            const rect = canvas.getBoundingClientRect();
            const fractions = [0.5, 0.4, 0.6, 0.3, 0.7, 0.2, 0.8, 0.1, 0.9];
            for (const fy of fractions) for (const fx of fractions) {
                const x = rect.width * fx, y = rect.height * fy;
                const hit = viewer.idPicker.pickAt(x, y);
                if (!hit || hit.info.topo !== 'face') continue;
                const path = hit.info.solidPath ??
                    hit.info.path.replace(/\/(faces|edges|vertices)\/[^/]+$/, '');
                const owner = names.find(name => path.split('/').includes(name));
                if (owner) return {
                    x: rect.left + x, y: rect.top + y,
                    component: owner, topology: hit.info.topo, path,
                };
            }
            return null;
        }""",
        names,
    )
    if result is None:
        raise RuntimeError('No printable component face is pickable on the rendered canvas')
    return result


def browser_download_check(page, build_path, reload_model=None):
    """Check tree/list/canvas selection, immutable downloads, resize and reload.

    ``reload_model`` optionally sends the model through the real OCP socket again,
    allowing the caller (which owns the shape and server) to verify revision reset.
    Leave a canvas-selected component displayed for the caller's final screenshot.
    """
    build_path = Path(build_path).resolve()
    manifest = json.loads((build_path / 'manifest.json').read_text())
    description = json.loads((build_path / 'viewer.json').read_text())
    names = [entry['name'] for entry in description['components']]
    root = page.locator('#cad_viewer')
    page.wait_for_function(
        """id => {
            const root = document.querySelector('#cad_viewer');
            return root?.dataset.modelReady === 'true' && root.dataset.buildId === id;
        }""",
        arg=manifest['build_id'],
        timeout=20000,
    )
    if page.locator('iframe').count():
        raise RuntimeError('CAD viewer must be directly embedded, without an iframe')
    select_tool(page, 'export', 'export')
    _cleared(page)

    # Use an actually rendered native tree label so --part and cutaway views also
    # work. Parts outside the current assembly can remain available in the list.
    tree = page.locator('.tcv_cad_tree_container .tv-node-label')
    labels = tree.evaluate_all("""elements => elements.map(element =>
        [...element.childNodes].filter(node => node.nodeType === Node.TEXT_NODE)
            .map(node => node.textContent).join('').trim())""")
    available = [name for name in names if name in labels]
    if not available:
        raise RuntimeError('No component is visible in the native CAD tree')
    name = available[0]
    # Attribute matching prevents overlapping names selecting the wrong part.
    list_button = page.locator(f'#parts-list button[data-component="{name}"]')
    list_button.click()
    _selected(page, name)
    if list_button.get_attribute('aria-pressed') != 'true':
        raise RuntimeError('Parts list did not retain the selected component')
    downloads = [_download(page, build_path, manifest, name)]
    downloads.append(_download(page, build_path, manifest, name, 'stl'))
    coupons = [part for part in manifest['parts'] if part not in names]
    if coupons:
        coupon = coupons[0]
        page.locator(f'#parts-list button[data-component="{coupon}"]').click()
        _selected(page, coupon)
        downloads.append(_download(page, build_path, manifest, coupon))

    # Clear with the page's public control, then select through OCP's own tree.
    page.locator('#clear-selection').click()
    _cleared(page)
    tree_label = tree.filter(has_text=re.compile(r'^' + re.escape(name) + r'(?:\s+⚈)?$'))
    tree_label.click()
    _selected(page, name)
    downloads.append(_download(page, build_path, manifest, name))
    tree_label.click()
    _cleared(page)

    target = _canvas_target(page, names)
    page.mouse.dblclick(target['x'], target['y'])
    _selected(page, target['component'])
    downloads.append(_download(page, build_path, manifest, target['component']))
    # Repeating OCP's native pick toggles it off; stale downloads must disappear.
    page.mouse.dblclick(target['x'], target['y'])
    _cleared(page)

    # The native Select tool reports topological faces rather than whole parts.
    # Its owning component must still download correctly, and Escape must clear
    # the panel even when upstream clears its selection without a final event.
    page.locator('.tcv_button_select').click()
    face_target = _canvas_target(page, names)
    page.mouse.move(face_target['x'], face_target['y'])
    page.wait_for_function(
        """name => window.cadViewer.lastObject?.backendId.split('/').includes(name)""",
        arg=face_target['component'],
        timeout=10000,
    )
    page.mouse.click(face_target['x'], face_target['y'])
    _selected(page, face_target['component'])
    downloads.append(_download(page, build_path, manifest, face_target['component']))
    page.keyboard.press('Escape')
    _cleared(page)
    page.locator('.tcv_button_select').click()

    old_viewport = page.viewport_size
    original_width = page.locator('#cad_viewer canvas').first.bounding_box()['width']
    page.set_viewport_size({'width': 1050, 'height': 850})
    page.wait_for_function(
        """width => {
            const rect = document.querySelector('#cad_viewer canvas').getBoundingClientRect();
            return rect.width > 100 && Math.abs(rect.width - width) > 20;
        }""",
        arg=original_width,
        timeout=10000,
    )
    resized_width = page.locator('#cad_viewer canvas').first.bounding_box()['width']
    target = _canvas_target(page, names)
    page.mouse.dblclick(target['x'], target['y'])
    _selected(page, target['component'])
    downloads.append(_download(page, build_path, manifest, target['component']))
    page.set_viewport_size(old_viewport)
    page.wait_for_function(
        """width => Math.abs(document.querySelector('#cad_viewer canvas')
            .getBoundingClientRect().width - width) < 2""",
        arg=original_width,
        timeout=10000,
    )

    reloaded = False
    if reload_model is not None:
        revision = root.get_attribute('data-model-revision')
        if revision is None:
            raise RuntimeError('Viewer does not expose its rendered model revision')
        reload_model()
        page.wait_for_function(
            """revision => {
                const root = document.querySelector('#cad_viewer');
                return root.dataset.modelReady === 'true' &&
                    root.dataset.modelRevision !== revision;
            }""",
            arg=revision,
            timeout=30000,
        )
        _cleared(page)
        reloaded = True
    else:
        page.locator('#clear-selection').click()
        _cleared(page)

    target = _canvas_target(page, names)
    page.mouse.dblclick(target['x'], target['y'])
    _selected(page, target['component'])
    return {'direct_embed': True, 'build_id': manifest['build_id'],
            'parts_list_selection': name, 'native_tree_selection': name,
            'canvas_pick': target, 'face_selection': face_target, 'selection_clear': True,
            'resize': {'before': original_width, 'after': resized_width},
            'reload_clears_selection': reloaded, 'downloads': downloads}
