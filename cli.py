"""
Command-line interface for the jirastats package.

Preferred invocation patterns:
- Installed as a package: run the console script `jirastats`
- From source without installation: python -m jirastats.cli

This keeps the CLI thin and delegates to the existing main.main() workflow.
"""
from typing import Optional


def main(argv: Optional[list] = None) -> int:
    # Defer imports so unit tests that stub heavy deps can run fast
    from main import main as run
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
