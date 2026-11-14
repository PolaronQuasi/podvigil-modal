"""Modal-only command line shared by the executable and module entry point."""

import sys
from .modal_backend.cli import run_cli as modal_cli


def run_cli(argv=None):
    return modal_cli(sys.argv[1:] if argv is None else argv)
