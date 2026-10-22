"""The workspace viewer must run without importing any CAD project's code."""
import os
from pathlib import Path
import subprocess
import sys

from cad_viewer.catalog import ProjectCatalog
from cad_viewer.preview import workspace_files


def test_selected_workspace_controls_assembly_build_directory(tmp_path):
    workspace, files = workspace_files(tmp_path)
    assert workspace == tmp_path.resolve()
    assert files.builds == tmp_path / 'cyclone/out/builds'
    assert files.pinned is None
    assert ProjectCatalog(workspace, files).metadata() == {'projects': []}


def test_default_workspace_is_the_projects_directory():
    from cad_viewer import preview
    workspace, files = workspace_files()
    assert workspace == Path(preview.__file__).resolve().parents[3] / 'projects'
    assert files.builds == workspace / 'cyclone/out/builds'


def test_viewer_application_is_never_a_cad_project(tmp_path):
    application = tmp_path / 'cad-tooling'
    application.mkdir()
    (application / 'pyproject.toml').write_text('[project]\nname = "cad-workspace-viewer"\n')
    (application / 'out').mkdir()
    project = tmp_path / 'unfinished'
    project.mkdir()
    (project / 'pyproject.toml').write_text('[project]\nname = "unfinished"\n')
    workspace, files = workspace_files(tmp_path)
    assert [project['id'] for project in ProjectCatalog(workspace, files).metadata()['projects']] == ['unfinished']


def test_all_viewer_modules_import_without_cyclone(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'src'
    script = '''
import importlib
import importlib.abc
import sys
class NoProjectImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'cyclone' or fullname.startswith('cyclone.'):
            raise AssertionError('Viewer imported a CAD project: ' + fullname)
sys.meta_path.insert(0, NoProjectImports())
for name in ['assembly', 'catalog', 'web', 'scenes', 'preview', '__main__',
             'browser_catalog_check', 'browser_download_check']:
    importlib.import_module('cad_viewer.' + name)
assert not any(name == 'cyclone' or name.startswith('cyclone.') for name in sys.modules)
'''
    environment = dict(os.environ, PYTHONPATH=str(source))
    result = subprocess.run([sys.executable, '-c', script], cwd=tmp_path,
                            env=environment, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_workspace_root_discovers_nested_projects(tmp_path):
    projects = tmp_path / 'projects'
    project = projects / 'unfinished'
    project.mkdir(parents=True)
    (project / 'pyproject.toml').write_text('[project]\nname = "unfinished"\n')
    workspace, files = workspace_files(tmp_path)
    assert workspace == projects
    assert files.builds == projects / 'cyclone/out/builds'
    assert [p['id'] for p in ProjectCatalog(tmp_path, files).metadata()['projects']] == ['unfinished']
