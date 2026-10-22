# Shared CAD build storage

`cad_build` is a standard-library-only library used by Cyclone and Vault. Keep
`cad-tooling/` alongside `projects/`, which contains `cyclone/` and `vault/`. The package lives at
`cad-tooling/src/cad_build`, alongside `cad_viewer`. Each CAD project's `_build.py`
loads this package; no extra package installation
or viewer runtime is needed to build geometry.

Projects own parameter validation, CAD construction, export, and geometry checks.
The shared `Build` transaction owns source/input snapshots, Git provenance,
checksums, staging, immutable storage, failure diagnostics, and atomic promotion.

```python
with Build(project_root, configuration_hash, inputs={'input.toml': config}) as build:
    # Write exports into build.stage and run project-specific validation.
    destination = build.complete({'passed': True, 'units': 'mm', ...})
```

Successful builds live in `out/builds/<UTC-start-time>-<content-hash>/` and use
`manifest.json` with schema `cad-build-v1`. `out/latest-successful` is an atomically
replaced symlink. Failed builds remain under `out/failed/` and never replace it.
Source or input changes during construction prevent publication. Source snapshots
include the shared build library, project code/tests, and dependency/environment
files. Full artifact checksums are available through `verify_files`.

Git provenance records the project's starting HEAD, full message, commit date,
and dirty status. Repositories without a commit or builds made without Git get
null provenance and appear under Unlinked builds. Shared-library source is
recorded separately in the snapshot; a project commit alone is not a complete
identifier for all build inputs.

Vault additionally maintains `out/latest/<size>` for size-specific render and
validation commands. `--output` chooses a build storage root. Post-build reviews
and comparisons are written outside immutable builds. Legacy outputs remain
readable; no existing snapshots are rewritten or assigned guessed commits.

Run the storage tests without CAD dependencies:

```sh
cd cad-tooling
PYTHONPATH=src python -m unittest discover -s tests -p test_build_storage.py -v
```

The viewer's `just check` also runs these storage tests alongside catalog and scene
checks. No CAD or viewer imports are used by `cad_build`.
