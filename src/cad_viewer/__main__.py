"""Run the standalone CAD workspace viewer and its browser regression check."""
import argparse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    view = commands.add_parser('view', help='Serve the workspace on loopback')
    view.add_argument('--port', type=int, default=3939)
    view.add_argument('--workspace', help='Directory containing CAD project directories')
    view.add_argument('--build-dir', help='Pin the legacy viewer to a completed assembly build')
    browser = commands.add_parser('browser-check', help='Exercise project/file selection in Chromium')
    browser.add_argument('--port', type=int, default=3940)
    browser.add_argument('--safe-graphics', action='store_true', help='Exercise the Safari graphics profile')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('--port must be between 1024 and 65535')
    try:
        if args.command == 'view':
            from .preview import run
            run(args.port, build_path=args.build_dir, workspace=args.workspace)
        else:
            from .browser_catalog_check import run
            run(args.port, safe_graphics=args.safe_graphics)
    except KeyboardInterrupt:
        print('\nViewer stopped.')


if __name__ == '__main__':
    main()
