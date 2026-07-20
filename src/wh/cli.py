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

    pv = sub.add_parser(
        "validate", help="check the config and semantic models, then exit"
    )
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
            summary = _validate_metrics(cfg)
            if summary:
                print(summary)
            return 0
        Workspace(cfg).mirror(only=args.only, keep_staging=args.keep_staging)
        return 0
    except WhError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _validate_metrics(cfg) -> str | None:
    """Structure always; bind checks only when the mirror file exists."""
    d = cfg.semantics_dir
    if d is None or not d.is_dir():
        return None
    from .metrics.loader import load_definitions

    models = load_definitions(d, cfg.fiscal_year_start)
    if not models:
        return None
    n = f"{len(models)} metric model{'s' if len(models) > 1 else ''}"
    if not cfg.duckdb_path.exists():
        return f"{n} OK (structure only — no mirror file)"
    from .metrics.checks import bind_checks

    ws = Workspace(cfg)
    try:
        warnings = [w for m in models.values() for w in bind_checks(ws.con, m)]
    finally:
        ws.close()
    return "\n".join([f"{n} OK (fully bound)"] + [f"warning: {w}" for w in warnings])


if __name__ == "__main__":
    sys.exit(main())
