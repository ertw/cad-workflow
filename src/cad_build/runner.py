"""Uniform subprocess entry point for supported project preview builds."""
import argparse
import importlib
from pathlib import Path


ADAPTERS = {'vault': 'vault.__main__', 'cyclone': 'cyclone.workflow'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', choices=ADAPTERS)
    parser.add_argument('configuration')
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    adapter = importlib.import_module(ADAPTERS[args.project])
    destination = adapter.build_preview(args.configuration)
    args.receipt.write_text(str(destination.resolve()))


if __name__ == '__main__':
    main()
