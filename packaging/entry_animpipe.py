"""Frozen entry point for the animpipe CLI.

Uses an absolute import (not the package's __main__ relative import) so it
resolves cleanly when PyInstaller runs it outside the package context.
"""
import sys

from animpipe.cli import main

if __name__ == "__main__":
    sys.exit(main())
