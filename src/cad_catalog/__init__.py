"""Declarative JSON catalogs; no geometry imports or PartCAD runtime required."""
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

ID = re.compile(r'[A-Za-z0-9_-]+')
VENDOR = Path(__file__).parent / 'vendor' / 'partcad'


class CatalogError(ValueError):
    pass


def local_file(root, name):
    if not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts:
        raise CatalogError(f'Expected a local project path: {name!r}')
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise CatalogError(f'Missing or external catalog source: {name}')
    return path


def read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise CatalogError(f'{path.name}: duplicate key {key!r}')
            result[key] = value
        return result
    def invalid(value):
        raise CatalogError(f'{path.name}: non-finite number {value}')
    try:
        return json.loads(path.read_text(), object_pairs_hook=pairs, parse_constant=invalid)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CatalogError(f'{path.name}: {error}') from error


@lru_cache(maxsize=2)
def validator(name):
    from jsonschema import Draft7Validator
    from referencing import Registry, Resource
    # Preserve the upstream files byte-for-byte. This release repeats some
    # schema keys; use its normal JSON last-key semantics only for vendor data.
    schemas = {n: json.loads((VENDOR / n).read_text()) for n in ('partcad.json', 'assy.json')}
    registry = Registry().with_resources((n, Resource.from_contents(s)) for n, s in schemas.items())
    return Draft7Validator(schemas[name], registry=registry)


def validate_upstream(data, schema, filename):
    error = next(validator(schema).iter_errors(data), None)
    if error:
        location = '.'.join(map(str, error.absolute_path)) or '<root>'
        raise CatalogError(f'{filename}: {location}: {error.message}')


def only(data, allowed, location):
    if not isinstance(data, dict):
        raise CatalogError(f'{location}: expected an object')
    unsupported = set(data) - set(allowed)
    if unsupported:
        raise CatalogError(f'{location}: unsupported fields: {", ".join(sorted(unsupported))}')


def identifiers(items, location):
    if not isinstance(items, dict) or any(not ID.fullmatch(k) for k in items):
        raise CatalogError(f'{location}: names must contain only letters, numbers, _ and -')


@dataclass(frozen=True)
class Target:
    id: str
    object: str
    label: str
    adapter: str
    configuration: str
    components: tuple[str, ...]
    parameters: dict
    input_hash: str | None = None

    def matches(self, manifest, parameters, names):
        recorded = manifest.get('catalog_target')
        if recorded is not None:
            return recorded == {'id': self.id, 'object': self.object}
        if self.adapter == 'cyclone-preset':
            return manifest.get('input_file_hash') == self.input_hash
        if not set(self.components).intersection(names):
            return False
        if self.adapter == 'vault-facade':
            return parameters.get('part') == 's1f_facade' or 's1f_facade' in names
        source = parameters or manifest.get('module', {})
        return all(source.get(key) == value for key, value in self.parameters.items())


@dataclass(frozen=True)
class Declaration:
    parts: dict
    targets: tuple[Target, ...]
    default: str
    revision: str

    def target(self, target_id):
        target = next((t for t in self.targets if t.id == target_id), None)
        if target is None:
            raise CatalogError('Choose a declared build configuration.')
        return target


def load(root):
    """Read a strict, literal subset of PartCAD's schema, serialized as JSON.

    Execution bindings live in cad-builds.json, outside the upstream schema.
    Files are validated before use; unsupported PartCAD behavior is never ignored.
    """
    root = Path(root).resolve()
    catalog_path = local_file(root, 'partcad.json')
    binding_path = local_file(root, 'cad-builds.json')
    data = read_json(catalog_path)
    validate_upstream(data, 'partcad.json', 'partcad.json')
    only(data, ('name', 'desc', 'private', 'parts', 'assemblies'), 'partcad.json')
    parts, assemblies = data.get('parts', {}), data.get('assemblies', {})
    identifiers(parts, 'parts'); identifiers(assemblies, 'assemblies')
    parameters = {}
    documents = [catalog_path, binding_path]
    for name, part in parts.items():
        only(part, ('type', 'desc', 'path', 'parameters'), f'parts.{name}')
        if part.get('type') != 'build123d':
            raise CatalogError(f'parts.{name}: only build123d descriptions are supported')
        local_file(root, part.get('path'))
        parameters[name] = {}
        for key, value in part.get('parameters', {}).items():
            only(value, ('type', 'default'), f'parts.{name}.parameters.{key}')
            kind = {'string': str, 'int': int, 'float': float, 'bool': bool}.get(value.get('type'))
            default = value.get('default')
            if kind is None or type(default) is not kind:
                raise CatalogError(f'parts.{name}.parameters.{key}: expected a literal {value.get("type")} default')
            parameters[name][key] = default
    members = {}
    for name, assembly in assemblies.items():
        only(assembly, ('type', 'desc', 'path'), f'assemblies.{name}')
        if assembly.get('type') != 'assy':
            raise CatalogError(f'assemblies.{name}: only assy descriptions are supported')
        path = local_file(root, assembly.get('path'))
        document = read_json(path)
        validate_upstream(document, 'assy.json', path.name)
        only(document, ('links',), path.name)
        names, components = set(), []
        for node in document.get('links', []):
            only(node, ('name', 'part'), path.name)
            if node.get('part') not in parts:
                raise CatalogError(f'{path.name}: unknown local part {node.get("part")!r}')
            occurrence = node.get('name', node['part'])
            if not ID.fullmatch(occurrence) or occurrence in names:
                raise CatalogError(f'{path.name}: duplicate or invalid occurrence {occurrence!r}')
            names.add(occurrence); components.append(node['part'])
        if not components:
            raise CatalogError(f'{path.name}: assembly must have parts')
        members[name] = tuple(dict.fromkeys(components))
        documents.append(path)
    bindings = read_json(binding_path)
    only(bindings, ('version', 'default', 'targets'), 'cad-builds.json')
    if type(bindings.get('version')) is not int or bindings['version'] != 1:
        raise CatalogError('cad-builds.json: expected version 1')
    identifiers(bindings.get('targets'), 'targets')
    targets = []
    for name, spec in bindings['targets'].items():
        only(spec, ('object', 'adapter', 'preset'), f'targets.{name}')
        reference, adapter = spec.get('object'), spec.get('adapter')
        group, _, object_id = str(reference).partition('/')
        objects = parts if group == 'parts' else assemblies if group == 'assemblies' else {}
        if object_id not in objects:
            raise CatalogError(f'targets.{name}: unknown object {reference!r}')
        components = (object_id,) if group == 'parts' else members[object_id]
        values = parameters.get(object_id, {}) if group == 'parts' else {}
        input_hash = None
        if adapter == 'vault-module' and group == 'parts':
            if set(values) != {'family', 'depth_units', 'height_units'}:
                raise CatalogError(f'targets.{name}: module needs family, depth_units, height_units')
            family, depth, height = (values[k] for k in ('family', 'depth_units', 'height_units'))
            if (family not in ('S', 'M', 'L', 'XL') or type(depth) is not int or type(height) is not int
                    or not 3 <= depth <= 100 or not 1 <= height <= 100):
                raise CatalogError(f'targets.{name}: unsupported module dimensions')
            if object_id != f'{family.lower()}{depth}_module_{height}f':
                raise CatalogError(f'targets.{name}: part identity must match module dimensions')
            configuration = f'{family}-{depth}-{height}'
        elif adapter == 'vault-facade' and group == 'parts':
            if object_id != 's1f_facade' or values:
                raise CatalogError(f'targets.{name}: facade adapter supports only s1f_facade')
            configuration = 's1f_facade'
        elif adapter == 'cyclone-preset' and group == 'assemblies':
            preset = local_file(root, spec.get('preset'))
            if preset.parent != root / 'presets' or preset.suffix != '.toml':
                raise CatalogError(f'targets.{name}: preset must be presets/*.toml')
            configuration = preset.name
            input_hash = hashlib.sha256(preset.read_bytes()).hexdigest()
            for component in components:
                if parameters[component] != {'component': component, 'preset': configuration}:
                    raise CatalogError(f'targets.{name}: component {component} must use this preset and identity')
        else:
            raise CatalogError(f'targets.{name}: unsupported adapter {adapter!r} for {group}')
        if adapter != 'cyclone-preset' and 'preset' in spec:
            raise CatalogError(f'targets.{name}: preset is only supported for cyclone-preset')
        targets.append(Target(name, reference, objects[object_id].get('desc', object_id), adapter,
                              configuration, components, values, input_hash))
    if bindings.get('default') not in bindings['targets']:
        raise CatalogError('cad-builds.json: default must name a declared target')
    bound = {part for target in targets for part in target.components}
    if bound != set(parts):
        raise CatalogError('Every declared part must belong to a build target')
    revision = hashlib.sha256(b''.join(p.read_bytes() for p in documents)).hexdigest()
    return Declaration(parts, tuple(targets), bindings['default'], revision)
