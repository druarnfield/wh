#!/usr/bin/env bash
# The emitted SQL means what DuckDB says it means — run the metrics suite
# against the pinned engine and the latest release before upgrading.
set -euo pipefail
cd "$(dirname "$0")/.."
pinned=$(uv run python -c 'import duckdb; print(duckdb.__version__)')
for spec in "duckdb==${pinned}" "duckdb"; do
    echo "=== ${spec}"
    uv run --with "${spec}" -- pytest tests/metrics -q
done
