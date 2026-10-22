{
  description = "Shared CAD builds and preview (project-local, x86_64-linux)";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/02e08985a27c65ffd33d434eeb2e660a2e4dc84d";
  outputs = { self, nixpkgs }: let
    system = "x86_64-linux";
    pkgs = nixpkgs.legacyPackages.${system};
  in {
    devShells.${system}.default = pkgs.mkShell {
      packages = with pkgs; [ python312 git uv just patchelf curl cairo fontconfig dejavu_fonts chromium ];
      # Supply native wheel libraries in this shell without host configuration.
      LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib pkgs.zlib pkgs.expat pkgs.libGL pkgs.libx11 pkgs.libxext pkgs.libxrender pkgs.libice pkgs.libsm pkgs.cairo pkgs.fontconfig pkgs.freetype ];
      UV_PYTHON_DOWNLOADS = "never";
      UV_NO_MANAGED_PYTHON = "1";
      PLAYWRIGHT_NODEJS_PATH = "${pkgs.nodejs}/bin/node";
      UV_CACHE_DIR = "./.cache/uv";
      PYTHONPATH = "./src";
      PYTHONDONTWRITEBYTECODE = "1";
      shellHook = ''
        export PATH="$PWD/.venv/bin:$PATH"
        export FONTCONFIG_FILE="${pkgs.fontconfig.out}/etc/fonts/fonts.conf"
        echo "CAD tooling: run just setup once; just view serves ../projects on loopback."
      '';
    };
  };
}
