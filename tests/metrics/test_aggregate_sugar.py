"""Aggregate shorthand must behave like its explicit SQL definition."""

from dataclasses import replace

import duckdb
import pytest
import wh
import yaml
from wh.errors import SemanticsError
from wh.metrics.provenance import measure_hash
from wh.metrics.result import Slice


def model(make_defs, expr, **measure_options):
    return make_defs(yaml.safe_dump({'activity': {
        'fact': 'main.activity', 'time': {'column': 'd'},
        'dimensions': {'unit': 'unit'},
        'measures': {'value': {'description': 'Value', 'expr': expr,
                               **measure_options}},
    }}))['activity']


@pytest.mark.parametrize('values, expected', [
    ([2, 3], 5), ([0, 0], 0), ([2, None], None), ([None], None), ([], None),
])
@pytest.mark.parametrize('nulls', ['propagate', 'ignore'])
def test_sum_null_policy_and_empty_population(make_defs, values, expected, nulls):
    definition = model(make_defs, {'sum': 'x', 'nulls': nulls})
    with duckdb.connect() as con:
        con.execute('CREATE TABLE activity(d DATE, unit VARCHAR, x INTEGER)')
        if values:
            con.executemany("INSERT INTO activity VALUES ('2026-07-01', 'A', ?)",
                            [(v,) for v in values])
        result = Slice(definition, ['value'], con=lambda: con).frame(
            backend='pyarrow').to_pylist()
    if nulls == 'ignore':
        present = [v for v in values if v is not None]
        expected = sum(present) if present else None
    assert result == [{'value': expected}]


def test_sum_defaults_to_sql_null_handling(make_defs):
    assert model(make_defs, {'sum': 'x'}).measures['value'].expr == 'sum(x)'


@pytest.mark.parametrize('snapshot', [False, True])
@pytest.mark.parametrize('compare', [[], ['prior', 'yoy']])
@pytest.mark.parametrize('by', [[], ['unit']])
def test_sugar_and_explicit_sql_match_values_explanations_and_hashes(
    make_defs, snapshot, compare, by,
):
    definition = model(make_defs, {'sum': 'x + y', 'nulls': 'propagate'})
    definition = replace(definition, snapshot=snapshot, measures={
        name: replace(m, time_agg='last' if snapshot else 'sum')
        for name, m in definition.measures.items()
    })
    raw = replace(definition, measures={'value': replace(
        definition.measures['value'],
        expr='case when count(x + y) = count(*) then sum(x + y) end',
    )})
    with duckdb.connect() as con:
        con.execute("""CREATE TABLE activity AS SELECT * FROM (VALUES
            (DATE '2025-07-01', 'A', 2, 3),
            (DATE '2026-06-01', 'A', 4, 5),
            (DATE '2026-07-01', 'A', 6, 7),
            (DATE '2026-07-01', 'B', NULL, 8)
        ) t(d, unit, x, y)""")
        def evaluate(m):
            return Slice(m, ['value'], by=by, grain='month', compare=compare,
                         ctx=wh.context(time=('2026-06-01', '2026-07-31')),
                         con=lambda: con).explain()
        sugar_result, raw_result = evaluate(definition), evaluate(raw)
        assert sorted(map(repr, sugar_result['rows'])) == sorted(
            map(repr, raw_result['rows']))
        assert measure_hash(con, definition, definition.measures['value']) == (
            measure_hash(con, raw, raw.measures['value']))


@pytest.mark.parametrize('predicate_location', ['measure', 'expression'])
def test_filtered_sum_only_checks_inputs_inside_its_population(
    make_defs, predicate_location,
):
    spec = {'sum': 'x', 'nulls': 'propagate'}
    options = {}
    (spec if predicate_location == 'expression' else options)['where'] = "unit = 'A'"
    definition = model(make_defs, spec, **options)
    with duckdb.connect() as con:
        con.execute("""CREATE TABLE activity AS SELECT * FROM (VALUES
            (DATE '2026-07-01', 'A', 2),
            (DATE '2026-07-01', 'B', NULL)
        ) t(d, unit, x)""")
        selection = Slice(definition, ['value'], con=lambda: con)
        assert selection.frame(backend='pyarrow').to_pylist() == [{'value': 2}]
        con.execute("INSERT INTO activity VALUES ('2026-07-01', 'A', NULL)")
        assert selection.frame(backend='pyarrow').to_pylist() == [{'value': None}]


def test_ratio_components_accept_independent_sum_policies_and_filters(make_defs):
    definition = model(make_defs, None, ratio={
        'num': {'sum': 'x', 'nulls': 'propagate', 'where': "unit = 'A'"},
        'den': {'sum': 'y', 'nulls': 'propagate'},
    })
    with duckdb.connect() as con:
        con.execute("""CREATE TABLE activity AS SELECT * FROM (VALUES
            (DATE '2026-07-01', 'A', 2, 5),
            (DATE '2026-07-01', 'B', NULL, 5)
        ) t(d, unit, x, y)""")
        result = Slice(definition, ['value'], con=lambda: con).explain()
        assert result['rows'] == [{'value': 0.2}]
        assert result['ratios']['value']['rows'] == [
            {'numerator': 2, 'denominator': 10}]
        con.execute("UPDATE activity SET y = NULL WHERE unit = 'B'")
        result = Slice(definition, ['value'], con=lambda: con).explain()
        assert result['rows'] == [{'value': None}]


@pytest.mark.parametrize('expr', [
    {'sum': 'x', 'nulls': 'guess'}, {'sum': 'x', 'nulls': None},
    {'sum': 'x', 'nulls': []}, {'sum': 'x', 'typo': True},
    {'sum': ''}, {'sum': None}, {'sum': ['x']}, {'avg': 'x'},
    {'sum': 'x', 'where': False}, {'sum': 'x', 'where': ''},
    {'sum': 'DISTINCT x'},
])
def test_invalid_shorthand_is_a_load_error(make_defs, expr):
    with pytest.raises(SemanticsError, match='measure.*value'):
        model(make_defs, expr)


def test_conflicting_predicates_are_rejected(make_defs):
    with pytest.raises(SemanticsError, match='where'):
        model(make_defs, {'sum': 'x', 'where': 'x > 0'}, where='x < 0')
