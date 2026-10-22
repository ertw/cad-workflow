set positional-arguments

setup:
    uv sync --locked --no-install-project --python python3.12

view port="3939" workspace="..":
    python -m cad_viewer view --port {{port}} --workspace "{{workspace}}"

check:
    python -m pytest -q --junitxml=out/tests.xml

browser-check port="3940":
    python -m cad_viewer browser-check --port {{port}}

browser-check-safe port="3940":
    python -m cad_viewer browser-check --port {{port}} --safe-graphics
