# CAD viewer

This directory owns the shared web preview for the sibling CAD projects. The Python server, project catalog, scene conversion, browser assets, and viewer checks live here; project geometry and generated exports remain in their project directories.

## Run locally

The pinned Nix environment and Python lock are independent of Cyclone's environment. From this directory:

```sh
nix develop path:. -c just setup
nix develop path:. -c just view
```

The server listens on `127.0.0.1:3939` and discovers projects in the parent `cad` directory. Use another free port or workspace explicitly:

```sh
nix develop path:. -c just view 3940 /path/to/cad
```

The underlying command is `python -m cad_viewer view --port 3939 --workspace ..`. `--build-dir` selects a single immutable Cyclone build for the legacy streamed preview. Starting the viewer does not build or alter any project's geometry, move Vault files, or modify host services. Reference files under `original_files` are excluded from discovery.

## Projects and configurations

Choose a project, then a file or configuration. The URL retains that selection, and separate tabs can show different models. A project without successful exports appears with an empty-state message.

For Cyclone, **Baseline · full assembly** previews the complete assembly. Configurations represent resolved parameter sets, not Git commits. The catalog uses the newest successful build for each parameter set. Named presets are visible by default; older unnamed sets are dated and available under **Show previous designs**. Build snapshots still record the source used for each export.

For other projects, successful `out/**/build.json` manifests declare the exported STL files. Original exports are read without modification. Mesh reduction, when necessary for preview performance, affects only the displayed scene.

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

Cyclone's existing viewer commands delegate to this application for compatibility. The deployed service launcher can continue using that forwarding entry point during the transition. This extraction does not change NixOS, nginx, authentication, or the public listener configuration.
