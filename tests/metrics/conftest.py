"""Fixtures for the metrics layer: the design-doc YAML, a loader helper,
a seeded in-memory mirror, and the parse-tree SQL equality helper."""

import duckdb
import pytest

from wh.metrics.loader import load_definitions

from fixtures_data import DIMS_YAML, REMOVALS_YAML, SEED_SQL, WAITLIST_YAML


@pytest.fixture
def make_defs(tmp_path):
    """Write YAML documents as separate files and load them."""

    def _make(*yamls, fiscal_year_start=7):
        d = tmp_path / "semantics"
        d.mkdir(exist_ok=True)
        for existing in d.glob("*.yml"):
            existing.unlink()
        for i, text in enumerate(yamls):
            (d / f"f{i}.yml").write_text(text)
        return load_definitions(d, fiscal_year_start=fiscal_year_start)

    return _make


@pytest.fixture
def defs(make_defs):
    """The design-doc example: shared dims + waitlist (snapshot) + removals."""
    return make_defs(DIMS_YAML, WAITLIST_YAML, REMOVALS_YAML)


@pytest.fixture
def design_yaml():
    return {"dims": DIMS_YAML, "waitlist": WAITLIST_YAML, "removals": REMOVALS_YAML}


@pytest.fixture
def con():
    """In-memory mirror. June 2026 weekly waitlist snapshots (05/12/19/26);
    clinic C3's feed lags — absent from the June-final 06-26 snapshot."""
    c = duckdb.connect()
    c.execute(SEED_SQL)
    yield c
    c.close()
