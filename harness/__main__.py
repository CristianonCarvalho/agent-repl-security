"""Permite `uv run python -m harness ...`: delega à CLI e usa o código de saída dela."""

import sys

from harness.cli import main

if __name__ == "__main__":
    sys.exit(main())
