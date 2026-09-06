"""Explanations preserve the values and filtering of ordinary metric slices."""

import pytest

import wh
from wh.errors import SemanticsError
from wh.metrics.result import Slice


@pytest.mark.parametrize('snapshot', [True, False])
@pytest.mark.parametrize('compare', [[], ['prior', 'yoy'], ['fytd']])
@pytest.mark.parametrize('threshold', [None, 2, 100])
def test_explanation_matches_slice_and_ratio_arithmetic(
    make_defs, con, snapshot, compare, threshold
):
    # Flow and snapshot models both matter; fytd is deliberately flow-only.
    if snapshot and compare == ['fytd']:
        pytest.skip('wh rejects fytd for snapshots')
    model = make_defs(f'''
activity:
  fact: main.waitlist
  time: {{column: snapshot_date}}
  snapshot: {str(snapshot).lower()}
  dimensions:
    clinic: clinic_code
  measures:
    n:
      description: Source rows
      expr: count(*)
    share:
      description: Share over target
      ratio:
        num: count(*) FILTER (WHERE wait_days > target_days)
        den: count(*)
''')['activity']
    selection = Slice(model, ['n', 'share'], by=['clinic'], grain='month',
                      compare=compare, suppress=threshold,
                      ctx=wh.context(clinic=['C1', 'C3']), con=lambda: con)
    def order(rows):
        return sorted(rows, key=lambda r: (r["period"], r["clinic"]))
    normal = selection.frame(backend='pyarrow').to_pylist()
    explanation = selection.explain()
    assert order(explanation['rows']) == order(normal)
    parts = explanation['ratios']['share']['rows']
    assert len(parts) == len(normal)
    for row, part in zip(explanation['rows'], parts, strict=True):
        for suffix in ['', *['_' + c for c in compare]]:
            num, den = part['numerator' + suffix], part['denominator' + suffix]
            if row['share' + suffix] is None:
                assert num is None and den is None
            else:
                assert row['share' + suffix] == pytest.approx(num / den)
    assert explanation['provenance']['shape']['ratio_components'] is True
    # Inspection must not replace the original slice SQL or its provenance.
    assert order(selection.frame(backend='pyarrow').to_pylist()) == order(normal)
    assert '__explain' not in selection.sql


def test_explanation_keeps_global_snapshot_and_empty_denominator(defs, con):
    selection = Slice(defs['waitlist'], ['pct_over_target'],
                      ctx=wh.context(facility__clinic='Seaside Clinic',
                                     time=('2026-06-01', '2026-06-30')),
                      con=lambda: con)
    result = selection.explain()
    # C3's last row is older than the global census; never use its stale census.
    assert result['rows'] == [{'pct_over_target': None}]
    assert result['ratios']['pct_over_target']['rows'] == [
        {'numerator': 0, 'denominator': 0}]
    assert '2026-06-26' in str(result['provenance']['data']['as_at'])


def test_explanation_of_nonadditive_measure(defs, con):
    selection = Slice(defs['waitlist'], ['median_wait'], con=lambda: con)
    assert selection.explain()['rows'] == selection.frame(backend='pyarrow').to_pylist()
    assert selection.explain()['ratios'] == {}


def test_declarative_context_uses_existing_ops_and_roundtrips():
    spec = {'urgency': {'in': ['Cat 1', 'Cat 2']},
            'facility__clinic': {'not': 'X'},
            'time': {'between': ['2026-06-01', '2026-06-30']}}
    expected = wh.context(urgency=['Cat 1', 'Cat 2'],
                          facility__clinic=wh.not_('X'),
                          time=('2026-06-01', '2026-06-30'))
    assert wh.context(spec) == expected
    assert wh.context(expected.to_dict()) == expected
    assert wh.context({'time': {'last': {'n': 2, 'unit': 'month'}}}) == wh.context(
        time=wh.last(2, 'month'))
    assert wh.context({'urgency': {'all': True}}) == wh.context(urgency=wh.all())


@pytest.mark.parametrize('spec', [[], {'unit': {'oops': 1}}, {'unit': []},
    {'unit': {'all': False}}, {'time': {'last': {'n': 1.5, 'unit': 'month'}}},
    {'time': {'between': ['bad', 'date']}}, {'unit': {'eq': 'x', 'not': 'y'}},
    {1: 'x'}, {'time': {'last': {'n': 1, 'unit': 'month', 'typo': 1}}}])
def test_invalid_declarative_context_is_actionable(spec):
    with pytest.raises(SemanticsError):
        wh.context(spec)


def test_ratio_denominator_suppression_does_not_leak_components(defs, con):
    from dataclasses import replace

    model = defs['waitlist']
    measure = model.measures['pct_over_target']
    # Many cell rows but a filtered denominator below the threshold.
    measure = replace(measure, ratio=("count(*)", "count(*) FILTER (WHERE false)"))
    model = replace(model, measures={'pct_over_target': measure})
    result = Slice(model, ['pct_over_target'], suppress=2, con=lambda: con).explain()
    assert result['rows'] == [{'pct_over_target': None}]
    assert result['ratios']['pct_over_target']['rows'] == [
        {'numerator': None, 'denominator': None}]


@pytest.mark.fuzz
@pytest.mark.parametrize('with_comparisons', [False, True])
def test_explanation_generative_contract(with_comparisons):
    # Keep Hypothesis helpers in the existing wh test vocabulary, not the consumer.
    import duckdb
    from hypothesis import given
    from strategies import build_model, scenarios, seed, to_wh_context

    @given(sc=scenarios(with_compare=with_comparisons, with_suppress=True,
                        with_complete=True))
    def check(sc):
        case, args = sc
        with duckdb.connect() as connection:
            seed(connection, case)
            selection = Slice(build_model(case), list(args.measures), by=list(args.by),
                              ctx=to_wh_context(args), grain=args.grain,
                              compare=list(args.compare), suppress=args.suppress,
                              complete_periods=args.complete_periods,
                              con=lambda: connection)
            actual = selection.explain()
            ordinary = selection.frame(backend='pyarrow').to_pylist()
            # SQL guarantees period order, not order among groups in that period.
            assert sorted(map(repr, actual['rows'])) == sorted(map(repr, ordinary))
            for name, ratio in actual['ratios'].items():
                for row, part in zip(actual['rows'], ratio['rows'], strict=True):
                    for suffix in ['', *['_' + c for c in args.compare]]:
                        num = part['numerator' + suffix]
                        den = part['denominator' + suffix]
                        value = row[name + suffix]
                        if value is not None:
                            assert value == pytest.approx(num / den)
                        elif args.suppress is not None:
                            assert num is None and den is None
                        else:
                            assert den is None or den == 0
    check()


def test_component_names_do_not_overwrite_group_keys(make_defs, con):
    model = make_defs('''
activity:
  fact: main.waitlist
  time: {column: snapshot_date}
  dimensions:
    numerator: clinic_code
    urgency: urgency_category
  measures:
    share:
      description: Share over target
      ratio: {num: count(*), den: count(*)}
''')['activity']
    result = Slice(model, ['share'], by=['numerator', 'urgency'], con=lambda: con).explain()
    assert all(isinstance(row['numerator'], str) for row in result['rows'])
    # Component records align by position and never contain dimension names.
    assert all(set(row) == {'numerator', 'denominator'}
               for row in result['ratios']['share']['rows'])
