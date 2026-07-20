"""Provenance: content hashes over semantic projections; numbers explain
themselves."""

import json
import re
from datetime import date

import pytest

from wh.metrics.provenance import measure_hash, model_hash


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
