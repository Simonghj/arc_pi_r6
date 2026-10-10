"""Coding harness entrypoint."""
from __future__ import annotations

import sys

from arc_pi.cli import run_generation


def main(argv: list[str] | None = None) -> int:
    return run_generation(argv, prog="main.py")


if __name__ == "__main__":
    sys.exit(main())

