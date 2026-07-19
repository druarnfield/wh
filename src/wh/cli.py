"""wh CLI: `wh validate`, `wh mirror`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import find_config, load_config
from .errors import WhError
from .workspace import Workspace


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wh", description="DuckDB warehouse tools")
    sub = p.add_subparsers(dest="command", required=True)

    pv = sub.add_parser("validate", help="parse the config and exit")
    pv.add_argument("--config", type=Path, help="path to wh.yaml (default: discover)")

    pm = sub.add_parser("mirror", help="refresh the local mirror")
    pm.add_argument("--config", type=Path, help="path to wh.yaml (default: discover)")
    pm.add_argument("--only", action="append", metavar="TABLE",
                    help="refresh only these tables (others carried over)")
    pm.add_argument("--keep-staging", action="store_true")

    args = p.parse_args(argv)
    try:
        cfg = load_config(args.config or find_config())
        if args.command == "validate":
            print(f"OK: {len(cfg.tables)} tables -> {cfg.duckdb_path}")
            from .semantics import validate_semantics

            summary = validate_semantics(cfg)
            if summary:
                print(summary)
            return 0
        Workspace(cfg).mirror(only=args.only, keep_staging=args.keep_staging)
        return 0
    except WhError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
