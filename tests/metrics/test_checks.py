import pytest

from wh.errors import SemanticsError
from wh.metrics.checks import bind_checks
from wh.metrics.compiler import compile_slice
from wh.metrics.context_ops import context


def test_clean_model_binds_without_warnings(con, defs):
    assert bind_checks(con, defs["removals"]) == []
    assert bind_checks(con, defs["waitlist"]) == []


def test_explain_catches_broken_definitions(con, make_defs, design_yaml):
    bad = design_yaml["removals"].replace("count(*)", "count(nonexistent)")
    model = make_defs(design_yaml["dims"], bad)["removals"]
    with pytest.raises(SemanticsError, match="removals"):
        bind_checks(con, model)


def test_intrinsic_where_may_reference_fact_columns_only(con, make_defs, design_yaml):
    dim_ref = design_yaml["removals"].replace(
        "removal_reason <> 'ADMIN'", "facility.region = 'North'"
    )
    with pytest.raises(SemanticsError, match="fact columns"):
        bind_checks(con, make_defs(design_yaml["dims"], dim_ref)["removals"])
    ghost = design_yaml["removals"].replace(
        "removal_reason <> 'ADMIN'", "ghost_col > 1"
    )
    with pytest.raises(SemanticsError, match="fact columns"):
        bind_checks(con, make_defs(design_yaml["dims"], ghost)["removals"])


def test_measure_expr_may_reference_fact_columns_only(con, make_defs, design_yaml):
    bad = design_yaml["removals"].replace(
        "expr: count(*)", "expr: count(DISTINCT facility.region)"
    )
    with pytest.raises(SemanticsError, match="fact columns"):
        bind_checks(con, make_defs(design_yaml["dims"], bad)["removals"])


def test_non_aggregate_expr_is_rejected(con, make_defs, design_yaml):
    """A per-row expr would become a grouping column under GROUP BY ALL and
    silently explode a one-row total into one row per fact row."""
    bad = design_yaml["waitlist"].replace(
        "expr: median(wait_days)", "expr: wait_days - 0"
    )
    with pytest.raises(SemanticsError, match="aggregate"):
        bind_checks(con, make_defs(design_yaml["dims"], bad)["waitlist"])


def test_duplicate_dim_key_errors_naming_the_table(con, make_defs, design_yaml):
    con.execute(
        "CREATE TABLE main.bad_dim AS FROM (VALUES "
        "('C1','A','H1','D1','North'), ('C1','B','H2','D2','South')) "
        "t(clinic_code, clinic_name, hospital_name, district, region)"
    )
    dims = design_yaml["dims"].replace("main.clinic_dim", "main.bad_dim")
    model = make_defs(dims, design_yaml["removals"])["removals"]
    with pytest.raises(SemanticsError, match="bad_dim"):
        bind_checks(con, model)


def test_orphan_keys_warn_and_group_into_a_null_row(con, defs):
    con.execute(
        "INSERT INTO main.waitlist_removals VALUES (DATE '2026-06-11','C9','D1','TREATED')"
    )
    warnings = bind_checks(con, defs["removals"])
    assert any("facility" in w and "orphan" in w for w in warnings)

    june = context(time=("2026-06-01", "2026-06-30"))
    rows = con.execute(
        compile_slice(defs["removals"], ["removals"], by=["facility.region"], ctx=june).sql
    ).fetchall()
    got = dict(rows)
    assert got[None] == 1                       # the orphan surfaces, not vanishes
    (total,) = con.execute(
        compile_slice(defs["removals"], ["removals"], ctx=june).sql
    ).fetchone()
    assert total == sum(got.values())           # totals reconcile only WITH the NULL group


ROLEPLAY_YAML = """\
dimensions:
  home_clinic:
    table: main.roleplay_dim
    key_column: code_a
    attributes: {label: label}
  treating_clinic:
    table: main.roleplay_dim
    key_column: code_b
    attributes: {tlabel: label}
roleplay:
  fact: main.roleplay_fact
  time: {column: d}
  dimensions:
    home_clinic: {shared: fk_a}
    treating_clinic: {shared: fk_b}
  measures:
    n: {description: count, expr: "count(*)"}
"""


def test_roleplaying_dims_check_each_key_column(con, make_defs):
    defs = make_defs(ROLEPLAY_YAML)
    con.execute("""
        CREATE TABLE main.roleplay_dim AS FROM (VALUES
            (1, 9, 'a'), (2, 9, 'b')
        ) t(code_a, code_b, label)
    """)
    con.execute(
        "CREATE TABLE main.roleplay_fact AS "
        "FROM (VALUES (DATE '2026-01-05', 1, 9)) t(d, fk_a, fk_b)"
    )
    with pytest.raises(SemanticsError, match="code_b"):
        bind_checks(con, defs["roleplay"])


def test_measure_subquery_on_another_table_is_rejected(con, make_defs):
    defs = make_defs("""\
events:
  fact: main.waitlist_removals
  time: {column: removal_date}
  measures:
    n:
      description: leaky
      expr: "count(*) + (SELECT count(*) FROM main.clinic_dim)"
""")
    with pytest.raises(SemanticsError, match="clinic_dim"):
        bind_checks(con, defs["events"])


def test_measure_subquery_on_the_fact_itself_is_fine(con, make_defs):
    defs = make_defs("""\
events:
  fact: main.waitlist_removals
  time: {column: removal_date}
  measures:
    share:
      description: share of all removals
      expr: "count(*) / (SELECT count(*) FROM main.waitlist_removals)"
""")
    assert bind_checks(con, defs["events"]) == []
