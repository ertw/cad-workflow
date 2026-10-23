"""Uniform subprocess entry point for supported project preview builds."""
import argparse
import importlib
from pathlib import Path
from . import catalog_target


ADAPTERS = {'vault': 'vault.__main__', 'cyclone': 'cyclone.workflow'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', choices=ADAPTERS)
    parser.add_argument('configuration')
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--catalog-target')
    parser.add_argument('--catalog-object')
    args = parser.parse_args()
    adapter = importlib.import_module(ADAPTERS[args.project])
    token = catalog_target.set({'id': args.catalog_target, 'object': args.catalog_object}
                               if args.catalog_target and args.catalog_object else None)
    try:
        destination = adapter.build_preview(args.configuration)
    finally:
        catalog_target.reset(token)
    args.receipt.write_text(str(destination.resolve()))


if __name__ == '__main__':
    main()
