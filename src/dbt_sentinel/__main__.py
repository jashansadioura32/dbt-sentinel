"""Enables `python -m dbt_sentinel`, which is how the README and CI invoke it."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
