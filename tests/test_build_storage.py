"""Storage contracts shared by both CAD projects (no CAD dependencies)."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from cad_build import Build, verify_files


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'src').mkdir()
        self.source = self.root / 'src/model.py'
        self.source.write_text('original source')

    def cleanup(self):
        for path in Path(self.temporary.name).rglob('*'):
            if not path.is_symlink():
                path.chmod(0o755 if path.is_dir() else 0o644)
        self.temporary.cleanup()

    def build(self, promote=True):
        with Build(self.root, 'configuration', promote=promote) as state:
            (state.stage / 'mesh.stl').write_bytes(b'mesh')
            return state.complete({'passed': True, 'files': {'mesh.stl': hashlib.sha256(b'mesh').hexdigest()}})

    def test_immutable_individual_builds_and_snapshots(self):
        first = self.build()
        second = self.build()
        self.assertNotEqual(first, second)
        self.assertEqual((self.root / 'out/latest-successful').resolve(), second)
        manifest = verify_files(first)
        self.assertEqual(manifest['schema'], 'cad-build-v1')
        self.assertEqual((first / 'source/src/model.py').read_text(), 'original source')
        self.assertTrue((first / 'source/_build_system/__init__.py').is_file())
        self.assertEqual(first.stat().st_mode & 0o222, 0)
        self.assertEqual((first / 'mesh.stl').stat().st_mode & 0o222, 0)

    def test_failure_preserves_latest_and_keeps_diagnostics(self):
        first = self.build()
        with self.assertRaisesRegex(ValueError, 'geometry'):
            with Build(self.root, 'configuration') as state:
                raise ValueError('invalid geometry')
        self.assertEqual((self.root / 'out/latest-successful').resolve(), first)
        self.assertTrue((self.root / 'out/failed' / state.id / 'failure.json').is_file())
        self.assertFalse(state.destination.exists())

    def test_source_and_input_changes_prevent_publication(self):
        first = self.build()
        config = self.root / 'input.toml'
        config.write_text('first')
        for change in (self.source, config):
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                with Build(self.root, 'configuration', inputs={'input.toml': config}) as state:
                    change.write_text('changed')
                    state.complete({'passed': True})
        self.assertEqual((self.root / 'out/latest-successful').resolve(), first)

    def test_unpromoted_build_and_checksum_failure(self):
        first = self.build()
        second = self.build(promote=False)
        self.assertEqual((self.root / 'out/latest-successful').resolve(), first)
        artifact = second / 'mesh.stl'
        artifact.chmod(0o644)
        artifact.write_bytes(b'corrupt')
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            verify_files(second)

    def test_incomplete_and_failed_validation_cannot_publish(self):
        with self.assertRaisesRegex(RuntimeError, 'without completing'):
            with Build(self.root, 'configuration'):
                pass
        with self.assertRaisesRegex(ValueError, 'validation'):
            with Build(self.root, 'configuration') as state:
                state.complete({'passed': False})
        self.assertFalse((self.root / 'out/latest-successful').exists())


if __name__ == '__main__':
    unittest.main()
