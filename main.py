"""Entry point for local web and local browser-launcher modes."""

from __future__ import annotations

import argparse
import multiprocessing

from trackautism_app.desktop.launcher import main as browser_main
from trackautism_app.server.web import main as web_main


def main() -> None:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="Run JTrack Insight locally.")
    parser.add_argument(
        "--mode",
        choices=("browser", "web"),
        default="browser",
        help="Launch as a browser-opening local app or as a local web server.",
    )
    args, _unknown = parser.parse_known_args()
    if args.mode == "web":
        web_main()
    else:
        browser_main()


if __name__ == "__main__":
    main()
