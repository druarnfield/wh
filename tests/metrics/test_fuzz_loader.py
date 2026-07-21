"""The loader is the firewall: every input either loads to Models or
raises SemanticsError — never a crash, never a silent partial load. And
generated SQL always parses, whatever a context value contains."""

import duckdb
import pytest
import yaml
from hypothesis import given, note, strategies as st

from wh.errors import SemanticsError, WhError
from wh.metrics.compiler import compile_slice
from wh.metrics.loader import load_definitions

from strategies import Case, MEASURES, SliceArgs, build_model, render_yaml, seed, to_wh_context, cases

_keys = st.text(min_size=0, max_size=20)
_scalars = st.one_of(st.none(), st.booleans(), st.integers(),
                     st.floats(allow_nan=False), st.text(max_size=20))
_docs = st.recursive(
    _scalars,
    lambda c: st.one_of(st.dictionaries(_keys, c, max_size=4),
                        st.lists(c, max_size=4)),
    max_leaves=25,
)


@pytest.mark.fuzz
@given(doc=_docs)
def test_loader_is_total_on_arbitrary_yaml(doc, tmp_path_factory):
    d = tmp_path_factory.mktemp("fuzz")
    try:
        (d / "f.yml").write_text(yaml.safe_dump(doc, allow_unicode=True))
    except yaml.YAMLError:
        return                              # not representable — fine
    try:
        models = load_definitions(d, fiscal_year_start=7)
        assert isinstance(models, dict)
    except SemanticsError:
        pass                                # the only acceptable failure


@pytest.mark.fuzz
@given(case=cases(), data=st.data())
def test_loader_is_total_under_structure_aware_mutation(case, data, tmp_path_factory):
    """Take a VALID definition, break it one edit at a time: the result
    must load or raise SemanticsError — silent acceptance of a broken
    definition is the failure mode that matters."""
    doc = yaml.safe_load(render_yaml(case))
    path = []
    node = doc
    while isinstance(node, dict) and node and data.draw(st.booleans()):
        key = data.draw(st.sampled_from(sorted(node)))
        path.append(key)
        node = node[key]
    mutation = data.draw(st.sampled_from(["typo_key", "wrong_type", "delete"]))
    target = doc
    for key in path[:-1]:
        target = target[key]
    key = path[-1] if path else data.draw(st.sampled_from(sorted(doc)))
    if mutation == "typo_key":
        target[key + "x"] = target.pop(key)
    elif mutation == "wrong_type":
        target[key] = [target[key]]
    else:
        del target[key]
    d = tmp_path_factory.mktemp("mut")
    (d / "f.yml").write_text(yaml.safe_dump(doc))
    note(yaml.safe_dump(doc))
    try:
        load_definitions(d, fiscal_year_start=case.fys)
    except SemanticsError:
        pass


@pytest.mark.fuzz
@given(value=st.text(max_size=40))
def test_context_values_never_break_the_generated_sql(value):
    """Any string context value: SQL parses, executes, and the output
    column set is exactly the declared namespace — quotes, unicode,
    SQL-looking text included."""
    from datetime import date
    case = Case(
        rows=({"d": date(2026, 1, 1), "k": "U1", "c": value, "v": 1, "fk": "K1"},),
        dim_rows=(("K1", "North"), ("K2", "South"), ("K3", "North")),
        snapshot=False, fys=7, measures=dict(MEASURES),
    )
    con = duckdb.connect()
    seed(con, case)
    compiled = compile_slice(
        build_model(case), ["n"],
        ctx=to_wh_context(SliceArgs(measures=("n",), by=(),
                                    ctx={"c": ("eq", value)}, time=None,
                                    grain=None)),
    )
    res = con.execute(compiled.sql)
    assert [d[0] for d in res.description] == ["n"]
    assert res.fetchall() == [(1,)]
