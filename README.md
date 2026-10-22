# CAD tooling

This repository owns the shared build storage and web preview for the CAD projects in `../projects/`.

`src/cad_build` is a standard-library-only package for immutable builds and provenance.
`src/cad_viewer` displays those builds; building geometry does not import or require the viewer. The Python server, project catalog, scene conversion, browser assets, and viewer checks live here; project geometry and generated exports remain in their project directories.

![CAD viewer showing the Cyclone baseline assembly, project and configuration selectors, and downloadable parts.](docs/images/cad-viewer.png)

The workspace layout is:

```text
cad/
  cad-tooling/       # this repository: cad_build and cad_viewer
  projects/
    cyclone/
    vault/
```

## Run locally

The pinned Nix environment and Python lock are independent of Cyclone's environment. From this directory:

```sh
nix develop path:. -c just setup
nix develop path:. -c just view
```

The server listens on `127.0.0.1:3939` and discovers designs in `../projects/`. Use another free port or workspace explicitly:

```sh
nix develop path:. -c just view 3940 /path/to/cad
```

The underlying command is `python -m cad_viewer view --port 3939 --workspace ../projects`. `--build-dir` selects a single immutable Cyclone build for the legacy streamed preview. Starting the viewer does not build or alter any project's geometry, move Vault files, or modify host services. Reference files under `original_files` are excluded from discovery.

## Projects and configurations

Choose a project, then a file or configuration. The URL retains that selection, and separate tabs can show different models. A project without successful exports appears with an empty-state message.

For Cyclone and Vault, every successful build is listed under an expandable Git commit message, with the newest commit dates first and newest builds first within each group. Build rows show their configuration, build start time, and whether the checkout had uncommitted changes. New builds record the starting commit, full commit message, commit date, and working-tree status in their manifest. Builds without valid recorded provenance appear under **Unlinked builds**; timestamps are never used to guess a commit. Source snapshots remain the authoritative record for modified checkouts.

The shared [build library](docs/builds.md) writes `cad-build-v1` manifests for both projects. The viewer also reads legacy successful `out/**/build.json` exports, including older Vault files, without rewriting them. Original exports are read without modification. Mesh reduction, when necessary for preview performance, affects only the displayed scene.

Safari uses reduced graphics settings to limit GPU memory use. A lost WebGL context stops automatic recovery and displays an explicit reload message. The `?graphics=safe` query option enables the same profile in other browsers.

## Checks

```sh
nix develop path:. -c just check
nix develop path:. -c just browser-check
nix develop path:. -c just browser-check-safe
```

The browser checks use the pinned Chromium executable and temporary exported fixtures, covering project/file selection, separate tabs, history, downloads, empty/error states, and graphics-context loss. They do not rebuild Cyclone geometry. Check reports and screenshots are written under `out/`.

The extracted environment retains the existing dependency versions: Python 3.12, build123d 0.13.0, cadquery-ocp-novtk 8.0.1.1.0, ocp-viewer 1.1.3, ocp-viewer-core 1.0.13, trimesh 4.12.2, numpy 2.5.3, websockets 17.2, pytest 9.0.2, and Playwright 1.58.0. Shell entry never installs dependencies; `just setup` installs the lock explicitly.

## Existing deployment

Cyclone's existing viewer commands delegate to this application for compatibility. Existing service launchers need to reference the new `projects/cyclone` or `cad-tooling` paths. This extraction does not change NixOS, nginx, authentication, or the public listener configuration.
