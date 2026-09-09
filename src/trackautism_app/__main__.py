"""Package entry point."""

from __future__ import annotations

from pathlib import Path
import runpy


def main() -> None:
    project_root = Path(__file__).resolve().parents[2]
    runpy.run_path(str(project_root / "main.py"), run_name="__main__")


if __name__ == "__main__":
    main()
