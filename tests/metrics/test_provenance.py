"""Provenance: content hashes over semantic projections; numbers explain
themselves."""

import json
import re
from datetime import date

import pytest

from wh.metrics.compiler import compile_slice
from wh.metrics.context_ops import context
from wh.metrics.provenance import measure_hash, model_hash
from wh.metrics.timegrain import period_end


def variant(make_defs, design_yaml, old, new):
    yaml = design_yaml["removals"].replace(old, new)
    return make_defs(design_yaml["dims"], yaml)["removals"]


# --- Task 1: hashes ---


def test_measure_hash_ignores_serializer_and_formatting_noise(con, make_defs, design_yaml):
    base = variant(make_defs, design_yaml, "count(*)", "count(*)")
    noisy = variant(make_defs, design_yaml, "expr: count(*)", "expr: COUNT( * )")
    h1 = measure_hash(con, base, base.measures["removals"])
    h2 = measure_hash(con, noisy, noisy.measures["removals"])
    assert h1 == h2
    assert re.fullmatch(r"[0-9a-f]{64}", h1)


@pytest.mark.parametrize(
    "old,new",
    [
        ("expr: count(*)", "expr: count(DISTINCT ur)"),                # expr
        ("removal_reason <> 'ADMIN'", "removal_reason <> 'admin'"),    # where literal
        ("fact: main.waitlist_removals", "fact: main.removals2"),      # fact ref
    ],
)
def test_measure_hash_changes_when_the_definition_changes(
    con, make_defs, design_yaml, old, new
):
    base = variant(make_defs, design_yaml, "x", "x")
    changed = variant(make_defs, design_yaml, old, new)
    assert measure_hash(con, base, base.measures["removals"]) != measure_hash(
        con, changed, changed.measures["removals"]
    )


def test_time_agg_is_hash_relevant_but_description_is_not(con, make_defs, design_yaml):
    base = variant(make_defs, design_yaml, "x", "x")
    agg = variant(
        make_defs, design_yaml,
        "      expr: count(*)\n", "      expr: count(*)\n      time_agg: avg\n",
    )
    desc = variant(
        make_defs, design_yaml,
        "Clinically meaningful removals", "Renamed prose only",
    )
    h = measure_hash(con, base, base.measures["removals"])
    assert h != measure_hash(con, agg, agg.measures["removals"])
    assert h == measure_hash(con, desc, desc.measures["removals"])


# --- Task 2: as-at + truncation plumbing ---


@pytest.mark.parametrize(
    "d,grain,expected",
    [
        (date(2026, 7, 5), "month", date(2026, 7, 31)),
        (date(2026, 7, 8), "week", date(2026, 7, 12)),      # Wed -> Sunday
        (date(2026, 3, 10), "fy", date(2026, 6, 30)),
        (date(2025, 11, 2), "fy_quarter", date(2025, 12, 31)),
        (date(2026, 2, 1), "quarter", date(2026, 3, 31)),
        (date(2026, 7, 5), "day", date(2026, 7, 5)),
    ],
)
def test_period_end(d, grain, expected):
    assert period_end(d, grain, 7) == expected


def test_asat_queries_exposed_per_lane(con, defs):
    c = compile_slice(
        defs["waitlist"], ["patients_waiting"], grain="month",
        ctx=context(time=("2026-06-01", "2026-06-30")), compare=["prior"],
    )
    assert set(c.asat_queries) == {"base", "prior"}
    assert con.execute(c.asat_queries["base"]).fetchall() == [
        (date(2026, 6, 1), date(2026, 6, 26)),
    ]
    assert con.execute(c.asat_queries["prior"]).fetchall() == []   # no May data


def test_flow_models_have_no_asat(defs):
    c = compile_slice(defs["removals"], ["removals"], grain="month")
    assert c.asat_queries is None


def test_model_hash_covers_config_not_measures(defs, make_defs, design_yaml):
    base = defs["waitlist"]
    flipped = make_defs(
        design_yaml["dims"],
        design_yaml["waitlist"].replace("  snapshot: true\n", ""),
    )
    # stripping snapshot: true makes time_agg defaults sum — keep measures
    # comparable by hashing config only
    assert model_hash(base) != model_hash(flipped["waitlist"])
    fys1 = make_defs(design_yaml["dims"], design_yaml["removals"])["removals"]
    fys9 = make_defs(
        design_yaml["dims"], design_yaml["removals"], fiscal_year_start=9
    )["removals"]
    assert model_hash(fys1) != model_hash(fys9)
    assert re.fullmatch(r"[0-9a-f]{64}", model_hash(base))
