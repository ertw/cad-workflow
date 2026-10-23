"""Check project catalogs without loading CAD geometry."""
import argparse
from pathlib import Path

from . import CatalogError, load


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('projects', nargs='+', type=Path)
    args = parser.parse_args()
    failed = False
    for root in args.projects:
        try:
            declaration = load(root)
            print(f'{root}: {len(declaration.parts)} parts, {len(declaration.targets)} build targets')
        except (CatalogError, OSError) as error:
            print(f'{root}: {error}')
            failed = True
    raise SystemExit(1 if failed else 0)


if __name__ == '__main__':
    main()
