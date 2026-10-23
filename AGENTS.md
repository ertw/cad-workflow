# CAD tooling

- Shared tooling lives here; project geometry and exports live in `../projects/`. Keep `cad_build` standard-library-only and builds independent of the viewer/GPU.
- Completed builds are immutable. Never edit published artifacts or repoint latest-successful to a failed build.
- Use `nix develop path:.`; install locked dependencies explicitly with `just setup`. Do not install dependencies during shell startup.
- For code changes, run `nix develop path:. -c just check`; for viewer behavior, also run `just browser-check` and `just browser-check-safe` inside the development shell.
- HTML/CSS/JS changes need a browser refresh; catalog/export changes need a file-list refresh. Loaded viewer Python changes need a service restart. Project geometry changes need a new build.
- As `ertw`, run `systemctl restart cad-viewer.service` without root or sudo; it is a system service, so omit `--user`. Wait for active builds/agent tasks to finish first: they currently depend on the viewer process.
- Bind local servers to `127.0.0.1`; use another port if occupied. Keep NixOS, nginx, and authentication changes outside routine tooling edits.
