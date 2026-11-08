"""Compiled SQL vs hand-written reference SQL on the generated warehouse, with and without pre-aggregation."""

import pytest
import yaml

from metriclayer.evaluate import REFERENCE, naive_examples, run_reference, same_rows

CASES = yaml.safe_load(REFERENCE.read_text())


@pytest.mark.parametrize("case", CASES, ids=[c["name"][:40] for c in CASES])
def test_compiled_matches_reference(engine, case):
    ref = engine.sql(case["sql"])
    assert ref, "reference returned no rows"
    for pre in (True, False):
        assert same_rows(engine.run(case["query"], use_cache=False, use_preaggs=pre).rows, ref), pre


def test_suite_summary_all_match(engine):
    res = run_reference(engine)
    assert all(r["match"] for r in res)
    assert any(r["preagg"] for r in res) and not all(r["preagg"] for r in res)


def test_naive_examples_are_wrong(engine):
    for n in naive_examples(engine):
        assert abs(n["error_pct"]) > 1, n["name"]


def test_same_rows_tolerance_and_order():
    assert same_rows([[1, 2.0000000001], ["a", None]], [["a", None], [1, 2.0]])
    assert not same_rows([[1, 2.0]], [[1, 2.1]])
    assert not same_rows([[1]], [[1], [1]])
