"""Keyboard and layout coverage for the viewer's tool drawer."""


def edge_target(page):
    """Find a real GPU-pickable edge; callers select it with browser input."""
    edge = page.evaluate("""() => {
        const rect = cadViewer.renderer.domElement.getBoundingClientRect();
        for (let y = 10; y < rect.height - 10; y += 5) {
            for (let x = 10; x < rect.width - 10; x += 5) {
                const hit = cadViewer.idPicker.pickAt(x, y);
                if (hit?.info.topo === 'edge') return {
                    x: rect.x + x, y: rect.y + y, path: hit.info.path,
                };
            }
        }
        return null;
    }""")
    assert edge, 'No pickable edge in the box fixture'
    return edge


def select_tool(page, name, tool_id):
    """Activate a tool through the same keyboard controls as the viewer user."""
    page.keyboard.press('Control+k')
    page.keyboard.type(name)
    page.keyboard.press('Enter')
    page.locator(f'#tool-{tool_id}').wait_for(state='visible')
    assert page.locator('#tool-results').is_hidden()


def _selected_text(search):
    return search.evaluate('(input) => input.value.slice(input.selectionStart, input.selectionEnd)')


def check_tool_launcher(page, projects, output):
    """Exercise command recall, fuzzy matching and project navigation on desktop."""
    search = page.locator('#tool-search')
    results = page.locator('#tool-results')
    content = page.locator('#tool-content')
    assert page.locator('.app-header').count() == 0
    assert search.is_visible()
    assert search.evaluate('(input) => document.activeElement === input')
    assert page.locator('#tool-export').is_visible()
    assert page.locator('#tool-agent-reference').is_hidden()
    assert page.locator('#tool-fillet').is_hidden()

    # A collapsed drawer must expand using the requested macOS shortcut too.
    page.locator('#tool-toggle').click()
    assert not search.is_visible()
    page.keyboard.press('Meta+k')
    results.wait_for(state='visible')
    assert search.evaluate('(input) => document.activeElement === input')
    assert _selected_text(search) == 'Export'
    expected = {'tool:export', 'tool:agent-reference', 'tool:fillet'}
    expected.update(f'project:{project}' for project in projects)
    assert set(results.locator('[data-command-id]').evaluate_all(
        '(options) => options.map(option => option.dataset.commandId)')) == expected

    # The popup overlays content; opening, filtering and closing do not move it.
    opened_top = content.bounding_box()['y']
    page.screenshot(path=str(output / 'tools-desktop.png'))
    page.keyboard.press('ArrowDown')
    page.keyboard.press('Enter')
    page.locator('#tool-agent-reference').wait_for(state='visible')
    assert abs(content.bounding_box()['y'] - opened_top) < 1
    page.keyboard.press('Control+k')
    assert _selected_text(search) == 'Agent reference'
    page.keyboard.press('ArrowUp')
    page.keyboard.press('Enter')
    page.locator('#tool-export').wait_for(state='visible')

    # Real typing replaces the selected remembered name, then fuzzy matches it.
    select_tool(page, 'fllt', 'fillet')
    page.keyboard.press('Control+k')
    assert _selected_text(search) == 'Fillet'
    page.keyboard.press('Enter')
    assert page.locator('#tool-fillet').is_visible()
    select_tool(page, 'agref', 'agent-reference')
    page.keyboard.press('Control+k')
    assert _selected_text(search) == 'Agent reference'
    page.keyboard.type('zzzz-no-tool')
    assert search.input_value() == 'zzzz-no-tool'
    assert results.locator('[data-command-id]').count() == 0
    page.keyboard.press('Enter')
    assert page.locator('#tool-agent-reference').is_visible()
    page.keyboard.press('Escape')
    assert results.is_hidden()
    assert search.input_value() == 'Agent reference'
    assert abs(content.bounding_box()['y'] - opened_top) < 1

    # Projects use the same command list and maintain the active tool.
    for query, project in [('bta', 'beta'), ('alha', 'alpha')]:
        page.keyboard.press('Control+k')
        page.keyboard.type(query)
        page.keyboard.press('Enter')
        page.wait_for_function("""project => {
            const root = document.querySelector('#cad_viewer');
            return root.dataset.projectId === project && root.dataset.modelReady === 'true';
        }""", arg=project)
        assert page.locator('#tool-agent-reference').is_visible()
        assert f'project={project}' in page.url
    select_tool(page, 'export', 'export')


def check_mobile_tool_launcher(page, output):
    """The search and result choices remain reachable at a narrow viewport."""
    page.keyboard.press('Control+k')
    search = page.locator('#tool-search')
    results = page.locator('#tool-results')
    results.wait_for(state='visible')
    assert search.is_visible()
    assert search.evaluate('(input) => document.activeElement === input')
    page.keyboard.type('fllt')
    option = results.locator('[data-command-id="tool:fillet"]')
    option.wait_for(state='visible')
    assert option.bounding_box()['width'] <= page.viewport_size['width']
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(output / 'tools-mobile.png'), full_page=True)
    option.click()
    assert page.locator('#tool-fillet').is_visible()
    select_tool(page, 'export', 'export')
