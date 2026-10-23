# Declared project catalogs

The viewer reads `partcad.json` and `cad-builds.json` from each project. The
catalog defines which parts exist; successful files under `out/` supply optional
previews and build history. Removing output never removes a catalog entry.
Undeclared exports remain in history and do not become build configurations.

Both files are plain JSON. No YAML conversion, template evaluation, PartCAD
installation, geometry import, or managed CAD runtime is involved in discovery.
The existing project Python environment generates and validates geometry.

## Description format

`partcad.json` uses a literal subset of PartCAD's package vocabulary. It is
validated against the unmodified upstream JSON Schema draft-07, then against
our narrower supported profile. PartCAD itself normally discovers a file named
`partcad.yaml`; our JSON filename and reader are a local integration, not a claim
that its CLI discovers this filename. JSON can be converted or supplied as YAML
later without changing the supported field meanings.

Supported package fields are `name`, `desc`, `private`, `parts`, and `assemblies`.
Part names are stable IDs containing letters, numbers, underscores and hyphens.
Each part has `type: "build123d"`, a local `path`, optional `desc`, and optional
`parameters`. Parameters explicitly specify `type` and a literal `default`.
Supported types are `string`, `int`, `float`, and `bool`.

Assemblies have `type: "assy"`, a local JSON `path`, and optional `desc`. Their
JSON document contains a `links` array of local `part` references with optional
unique occurrence `name`s. Parts are already in assembly coordinates. The reader
checks membership but does not reposition shapes or solve joints.

Templates, remote dependencies, aliases, arbitrary execution types, interface
mating, parameter expressions and placement transforms are not implemented in
this first profile. Unsupported fields fail explicitly, even if the broader
upstream schema accepts them. This leaves room to add named interfaces later
without pretending a description already implements mechanical compatibility.

The geometry entry scripts under each project's `catalog/` expose existing
builders through `show_object` when invoked by a compatible script runner. The
viewer never executes these scripts directly: it uses the build bindings below.
PartCAD execution itself has not been tested with our pinned CAD environment.

## Build bindings

Application behavior is kept outside upstream's schema:

```json
{
  "version": 1,
  "default": "s4_module_1f",
  "targets": {
    "s4_module_1f": {
      "object": "parts/s4_module_1f",
      "adapter": "vault-module"
    }
  }
}
```

This is a binding excerpt; every declared part must belong to a target.
`object` references `parts/<id>` or `assemblies/<id>`. The description's `desc`
is the display label. Supported adapters are:

- `vault-module`: reads `family`, `depth_units`, and `height_units` from the part's
  parameter defaults. Its ID must describe the same module size.
- `vault-facade`: builds the existing `s1f_facade` builder.
- `cyclone-preset`: builds a complete assembly using an explicit
  `"preset": "presets/baseline.toml"`. Each component declares matching
  `component` and `preset` defaults. It runs the assembly generator once, retaining
  its coupons, validation and print exports.

Only declared target IDs are accepted by the viewer's build API. The picker is
independent of the displayed historical file. Existing direct project CLI
commands and preset files remain available; presets do not automatically become
viewer build targets.

## Builds and review

New viewer builds record their target ID and object reference in the immutable
manifest. Catalog files and entry scripts are included in source snapshots and
freshness comparisons. Old Vault builds are attached only when part identity and
recorded dimensions match. Old Cyclone builds require a matching preset content
hash. An older unmatched build remains accessible in history.

A part can have no matching build, a current build, changed source, or unknown
freshness. These describe artifact availability and inputs only. The UI reports
review as unrecorded: this change does not invent approval for existing builds.
Human review records remain separate from descriptions and artifacts; a review
editor and persisted review-record format are outside this first catalog profile.

## Validation and upstream source

After the normal `just setup`, check descriptions without generating geometry:

```sh
python -m cad_catalog ../projects/vault ../projects/cyclone
```

Vendored schemas and license are under `src/cad_catalog/vendor/partcad/`.
`UPSTREAM.json` records the release, exact commit and SHA-256 of every copied file.
Version 0.8.232 is pinned at commit
`354ed5201e831b867ce0eb9637a234ea3f3ec692`.

The vendored package schema repeats some keys. It is preserved byte-for-byte and
loaded with ordinary JSON last-key semantics, matching upstream's loader. Our
project documents reject duplicate keys and non-finite numbers. Schema references
resolve locally; validation never fetches remote schemas.
