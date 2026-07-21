"""The generators must produce loadable, bindable, faithful specs —
otherwise every downstream layer tests garbage."""

import duckdb
import pytest
from hypothesis import given, settings

from wh.metrics.checks import bind_checks
from wh.metrics.loader import load_definitions

from strategies import build_model, cases, render_yaml, scenarios, seed


@pytest.mark.fuzz
@given(case=cases())
def test_rendered_yaml_loads_to_the_same_model(case, tmp_path_factory):
    d = tmp_path_factory.mktemp("sem")
    (d / "gen.yml").write_text(render_yaml(case))
    loaded = load_definitions(d, fiscal_year_start=case.fys)["gen"]
    assert loaded == build_model(case)


@pytest.mark.fuzz
@settings(max_examples=15)
@given(sc=scenarios())
def test_generated_cases_pass_bind_checks(sc):
    case, _args = sc
    con = duckdb.connect()
    seed(con, case)
    bind_checks(con, build_model(case))     # warnings fine, no raise
