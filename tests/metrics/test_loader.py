import pytest

from wh.errors import SemanticsError

DIM_MIN = """\
dimensions:
  facility:
    table: main.clinic_dim
    key_column: clinic_code
    attributes:
      region: region
"""

MODEL_MIN = """\
removals:
  fact: main.removals
  time:
    column: removal_date
  measures:
    removals:
      expr: count(*)
      description: "Removal events"
"""


# --- shared dimensions (Task 3) ---


def test_shared_dims_parsed(defs):
    fac = defs["waitlist"].dims["facility"].shared
    assert fac.table == "main.clinic_dim"
    assert fac.key_column == "clinic_code"
    assert fac.attributes["region"] == "region"
    assert fac.hierarchy == ("clinic", "hospital", "district", "region")


def test_models_merge_across_files(defs):
    assert set(defs) == {"waitlist", "removals"}


def test_duplicate_shared_dim_errors(make_defs):
    with pytest.raises(SemanticsError, match="facility"):
        make_defs(DIM_MIN, DIM_MIN)


def test_duplicate_model_errors(make_defs):
    with pytest.raises(SemanticsError, match="removals"):
        make_defs(MODEL_MIN, MODEL_MIN)


def test_hierarchy_must_use_declared_attributes(make_defs):
    bad = DIM_MIN + "    hierarchy: [region, galaxy]\n"
    with pytest.raises(SemanticsError, match="galaxy"):
        make_defs(bad)


def test_shared_dim_requires_table_and_key(make_defs):
    with pytest.raises(SemanticsError, match="key_column"):
        make_defs(
            "dimensions:\n  facility:\n    table: t\n    attributes: {a: a}\n"
        )


# --- model parsing rules (Task 4) ---


def test_design_yaml_loads(defs):
    wl = defs["waitlist"]
    assert wl.snapshot is True
    assert wl.time_column == "snapshot_date"
    assert wl.cadence == "weekly"
    assert wl.fiscal_year_start == 7
    assert wl.dims["urgency"].shared is None                  # local/degenerate
    assert wl.dims["urgency"].fact_column == "urgency_category"
    assert wl.dims["doctor"].shared.table == "main.doctor_dim"
    assert wl.measures["pct_over_target"].ratio == (
        "count(*) FILTER (WHERE wait_days > target_days)",
        "count(*)",
    )
    assert wl.measures["long_waiters"].where == "wait_days > 365"


def test_measure_description_required(make_defs):
    bad = MODEL_MIN.replace('      description: "Removal events"\n', "")
    with pytest.raises(SemanticsError, match="description"):
        make_defs(bad)


def test_model_needs_fact_and_time_column(make_defs):
    with pytest.raises(SemanticsError, match="fact"):
        make_defs("m:\n  measures: {n: {expr: count(*), description: d}}\n")
    with pytest.raises(SemanticsError, match="time"):
        make_defs("m:\n  fact: t\n  measures: {n: {expr: count(*), description: d}}\n")


def test_time_agg_defaults(defs):
    assert defs["waitlist"].measures["patients_waiting"].time_agg == "last"
    assert defs["removals"].measures["removals"].time_agg == "sum"
    assert defs["waitlist"].measures["median_wait"].time_agg == "none"  # explicit wins


def test_sum_on_snapshot_model_is_a_load_error(make_defs, design_yaml):
    bad = design_yaml["waitlist"].replace(
        "      expr: count(DISTINCT ur)\n",
        "      expr: count(DISTINCT ur)\n      time_agg: sum\n",
    )
    with pytest.raises(SemanticsError, match="event-grain fact"):
        make_defs(design_yaml["dims"], bad)


def test_invalid_time_agg(make_defs):
    bad = MODEL_MIN.replace(
        "      expr: count(*)\n", "      expr: count(*)\n      time_agg: total\n"
    )
    with pytest.raises(SemanticsError, match="sum"):
        make_defs(bad)


def test_ratio_needs_num_and_den(make_defs):
    bad = MODEL_MIN.replace(
        "      expr: count(*)\n", "      ratio:\n        num: count(*)\n"
    )
    with pytest.raises(SemanticsError, match="den"):
        make_defs(bad)


def test_expr_xor_ratio(make_defs):
    bad = MODEL_MIN.replace(
        "      expr: count(*)\n",
        "      expr: count(*)\n      ratio: {num: count(*), den: count(*)}\n",
    )
    with pytest.raises(SemanticsError, match="expr"):
        make_defs(bad)
    with pytest.raises(SemanticsError, match="expr"):
        make_defs(MODEL_MIN.replace("      expr: count(*)\n", ""))


def test_authored_filter_plus_where_is_a_load_error(make_defs):
    bad = MODEL_MIN.replace(
        "      expr: count(*)\n",
        "      expr: count(*) FILTER (WHERE x > 1)\n      where: y > 2\n",
    )
    with pytest.raises(SemanticsError, match="FILTER"):
        make_defs(bad)


def test_effective_additivity(defs, make_defs):
    m = defs["waitlist"].measures
    assert m["patients_waiting"].additive is False        # DISTINCT
    assert m["median_wait"].additive is False             # median()
    assert m["pct_over_target"].additive is False         # ratio
    assert m["long_waiters"].additive is True             # plain count(*)
    explicit = MODEL_MIN.replace(
        "      expr: count(*)\n", "      expr: count(*)\n      additive: false\n"
    )
    assert make_defs(explicit)["removals"].measures["removals"].additive is False


def test_dim_mapping_form_only_accepts_shared(make_defs):
    bad = MODEL_MIN.replace(
        "  measures:\n",
        "  dimensions:\n    thing: {table: t, key_column: k}\n  measures:\n",
    )
    with pytest.raises(SemanticsError, match="shared"):
        make_defs(bad)


def test_model_without_measures_errors(make_defs):
    with pytest.raises(SemanticsError, match="measure"):
        make_defs("m:\n  fact: t\n  time:\n    column: c\n")


def test_measure_names_cannot_smuggle_sql(make_defs):
    """Names are spliced unquoted into SELECT; a crafted one would inject a
    grouping column past GROUP BY ALL and silently change the result grain."""
    inj = MODEL_MIN.replace("    removals:\n", "    'n, wait_days AS smuggled':\n")
    with pytest.raises(SemanticsError, match="name"):
        make_defs(inj)


@pytest.mark.parametrize("bad", ["fact", "period", "time", "__asat", "with space"])
def test_dim_names_cannot_collide_with_compiler_aliases(make_defs, bad):
    yaml = (
        f"m:\n  fact: t\n  time:\n    column: c\n"
        f"  dimensions:\n    '{bad}': x\n"
        f"  measures:\n    n:\n      expr: count(*)\n      description: d\n"
    )
    with pytest.raises(SemanticsError, match="name"):
        make_defs(yaml)


def test_fact_table_must_be_a_plain_identifier(make_defs):
    bad = MODEL_MIN.replace("fact: main.removals", "fact: main.removals; DROP TABLE x")
    with pytest.raises(SemanticsError, match="fact table"):
        make_defs(bad)


# --- strict keys (hardening Task 1) ---


def test_unknown_model_key_errors_with_hint(make_defs):
    with pytest.raises(SemanticsError, match="snapshot"):
        make_defs("""\
census:
  fact: main.f
  snapsot: true
  time: {column: d}
  measures:
    n: {description: n, expr: "count(*)"}
""")


def test_unknown_measure_key_errors(make_defs):
    with pytest.raises(SemanticsError, match="unknown key"):
        make_defs("""\
census:
  fact: main.f
  time: {column: d}
  measures:
    n: {description: n, expr: "count(*)", wear: "1=1"}
""")


def test_unknown_time_key_errors(make_defs):
    with pytest.raises(SemanticsError, match="cadence"):
        make_defs("""\
census:
  fact: main.f
  time: {column: d, cadense: daily}
  measures:
    n: {description: n, expr: "count(*)"}
""")


def test_unknown_shared_dim_key_errors(make_defs):
    with pytest.raises(SemanticsError, match="unknown key"):
        make_defs("""\
dimensions:
  facility:
    table: main.dim
    key_colunm: code
    attributes: {name: label}
""")


def test_unknown_ratio_key_errors(make_defs):
    with pytest.raises(SemanticsError, match="unknown key"):
        make_defs("""\
census:
  fact: main.f
  time: {column: d}
  measures:
    pct:
      description: p
      ratio: {num: "count(*)", denum: "count(*)"}
""")


# --- explicit local/shared dim linking (hardening Task 2) ---

from fixtures_data import DIMS_YAML

LOCAL_VS_SHARED_MODEL = """\
events:
  fact: main.events
  time: {column: d}
  dimensions:
    facility: {shared: clinic_code}
    urgency: urgency_code
  measures:
    n: {description: n, expr: "count(*)"}
"""


def test_bare_dimension_is_always_local(make_defs):
    defs = make_defs(DIMS_YAML, LOCAL_VS_SHARED_MODEL)
    assert defs["events"].dims["urgency"].shared is None
    assert defs["events"].dims["urgency"].fact_column == "urgency_code"


def test_shared_reference_is_explicit(make_defs):
    defs = make_defs(DIMS_YAML, LOCAL_VS_SHARED_MODEL)
    ref = defs["events"].dims["facility"]
    assert ref.shared is not None and ref.shared.table == "main.clinic_dim"
    assert ref.fact_column == "clinic_code"


def test_bare_name_colliding_with_shared_dim_errors(make_defs):
    with pytest.raises(SemanticsError, match="shared"):
        make_defs(DIMS_YAML, """\
events:
  fact: main.events
  time: {column: d}
  dimensions:
    facility: clinic_code
  measures:
    n: {description: n, expr: "count(*)"}
""")


def test_shared_reference_without_declaration_errors(make_defs):
    with pytest.raises(SemanticsError, match="doesn't exist"):
        make_defs("""\
events:
  fact: main.events
  time: {column: d}
  dimensions:
    facility: {shared: clinic_code}
  measures:
    n: {description: n, expr: "count(*)"}
""")
