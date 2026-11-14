"""python -m podvigil uses the same Modal-only CLI as podvigil-modal."""

from .cli import run_cli

if __name__ == "__main__":
    raise SystemExit(run_cli())
