"""Read completed assembly artifacts without importing model-generation code."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .web import BuildFiles, PART_NAME


def viewer_assembly(path, part_name=None, cutaway=False):
    """Reconstruct labeled CAD from verified viewer metadata and STEP exports.

    Only immutable artifacts used in the preview are needed: this viewer does
    not import a project's Python package or regenerate any project geometry.
    Import a private copy of verified bytes so a concurrent file replacement
    cannot change the geometry between checksum validation and the CAD import.
    """
    from build123d import Box, Pos, Color, Compound, import_step
    path = Path(path).resolve()
    files = BuildFiles(path.parent, path)
    path, manifest = files.build(path.name)
    description = json.loads(files.artifact(path, manifest, 'viewer.json'))
    if description.get('build_id', path.name) != path.name:
        raise ValueError('Assembly metadata does not match its completed build')
    children = []
    with TemporaryDirectory(prefix='cad-assembly-') as temporary:
        for entry in description['components']:
            name = entry['name']
            if not isinstance(name, str) or not PART_NAME.fullmatch(name):
                raise ValueError('Assembly contains an invalid component name')
            if part_name and name != part_name:
                continue
            if name not in manifest['parts']:
                raise ValueError('Assembly component is missing from the completed build')
            relative = entry['step']
            if (not isinstance(relative, str) or Path(relative).is_absolute()
                    or '..' in Path(relative).parts or Path(relative).suffix.lower() != '.step'):
                raise ValueError('Assembly contains an invalid STEP artifact path')
            artifact = Path(temporary) / f'{name}.step'
            artifact.write_bytes(files.artifact(path, manifest, relative))
            shape = import_step(artifact)
            if part_name:
                center = shape.bounding_box().center()
                shape = Pos(-center.X, -center.Y, -center.Z) * shape
            if cutaway:
                shape = shape.cut(Pos(0, 1000, 500) * Box(4000, 2000, 4000))
                if len(shape.solids()) == 0:
                    continue
            shape.label = name
            shape.color = Color('#444444' if entry['kind'] == 'gasket' else '#70a7bb')
            children.append(shape)
    if not children:
        raise ValueError(f'Unknown or empty part: {part_name}')
    label = manifest['build_id'] + (' cutaway' if cutaway else '')
    return Compound(label=label, children=children), label
