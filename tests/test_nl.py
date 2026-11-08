"""Offline matcher, time parsing, the evaluation set, and Claude structured output with a fake client."""

import json
from datetime import date
from types import SimpleNamespace

import pytest

from metriclayer.evaluate import run_nl_eval
from metriclayer.nl import ClaudeInterpreter, make_interpreter, normalize, output_schema, parse_time_range


@pytest.fixture(scope="module")
def matcher(engine):
    return make_interpreter(engine, "offline")


def tr(text):
    return parse_time_range(normalize(text), date(2026, 7, 1))[0]


@pytest.mark.parametrize("text,expected", [
    ("orders in 2025", ("2025-01-01", "2026-01-01")),
    ("orders last 90 days", ("2026-04-02", "2026-07-01")),
    ("orders over the last 6 months", ("2026-01-01", "2026-07-01")),
    ("orders last month", ("2026-06-01", "2026-07-01")),
    ("orders last quarter", ("2026-04-01", "2026-07-01")),
    ("orders last week", ("2026-06-22", "2026-06-29")),
    ("orders in Q1 2026", ("2026-01-01", "2026-04-01")),
    ("orders first half of 2025", ("2025-01-01", "2025-07-01")),
    ("orders between January and March 2026", ("2026-01-01", "2026-04-01")),
    ("orders since March", ("2026-03-01", "2026-07-01")),
    ("orders in November", ("2025-11-01", "2025-12-01")),
    ("orders year to date", ("2026-01-01", "2026-07-01")),
    ("orders by country", None),
])
def test_time_ranges(text, expected):
    assert tr(text) == expected


def test_trailing_metric_name_is_not_a_time_range(matcher):
    it = matcher.interpret("trailing 28-day active buyers weekly")
    assert it.status == "ok" and it.query.metrics == ["active_buyers_28d"] and it.query.time_range is None


def test_value_synonym_and_negation(matcher):
    q = matcher.interpret("net revenue by country excluding the United States").query
    assert [f.to_dict() for f in q.filters] == [{"dimension": "customer__country", "op": "!=", "value": "US"}]
    assert "customer__country" in q.group_by


def test_vs_groups_compared_values(matcher):
    q = matcher.interpret("orders on android vs ios").query
    assert q.filters[0].op == "in" and "order__device" in q.group_by


def test_same_word_resolves_by_metric(matcher):
    # "web" is a value of both order__device and session__device; reachability picks the right one.
    assert matcher.interpret("orders on web").query.filters[0].dimension == "order__device"
    assert matcher.interpret("sessions on web").query.filters[0].dimension == "session__device"


def test_ambiguous_term_asks(matcher):
    it = matcher.interpret("revenue by country")
    assert it.status == "clarify" and "gross revenue or net revenue" in it.clarification


def test_unknown_metric_asks(matcher):
    assert matcher.interpret("what is our churn rate").status == "clarify"


def test_unreachable_dimension_asks_with_reason(matcher):
    it = matcher.interpret("refund rate by seller region")
    assert it.status == "clarify" and "not reachable" in it.clarification


def test_eval_set_offline(engine, matcher):
    res = run_nl_eval(engine, matcher)
    assert res["questions"] >= 60
    assert res["exact_match"] >= 0.85
    assert res["execution_match"] >= res["exact_match"]
    assert res["clarification_recall"] == 1.0
    assert res["false_clarifications"] <= 3


# ---------- Claude with a fake client ----------
class FakeClient:
    def __init__(self, payload, stop_reason="end_turn"):
        self.payload, self.stop_reason, self.calls = payload, stop_reason, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=self.stop_reason,
                               usage=SimpleNamespace(input_tokens=1200, output_tokens=80))


def answer(**kw):
    base = {"status": "ok", "clarification": "", "metrics": [], "group_by": [], "grain": "none", "filters": [],
            "time_start": "", "time_end": ""}
    return {**base, **kw}


def claude(engine, payload, **kw):
    client = FakeClient(payload, **kw)
    return ClaudeInterpreter(engine.layer, {"customer__country": ["US"]}, client=client), client


def test_claude_valid_output(engine):
    interp, client = claude(engine, answer(metrics=["gross_revenue"], group_by=["metric_time", "customer__country"],
                                          grain="month", filters=[{"dimension": "order__device", "op": "=",
                                                                   "values": ["web"]}],
                                          time_start="2026-01-01", time_end="2026-07-01"))
    it = interp.interpret("monthly gmv by country on web this year")
    assert it.status == "ok" and it.provider == "claude"
    assert it.query.grain == "month" and it.query.time_range == ("2026-01-01", "2026-07-01")
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "Question: monthly gmv" in call["messages"][0]["content"]
    assert interp.usage == {"calls": 1, "input_tokens": 1200, "output_tokens": 80}


def test_schema_enumerates_catalog(layer):
    s = output_schema(layer)
    assert set(s["properties"]["metrics"]["items"]["enum"]) == set(layer.metrics)
    assert "customer__country" in s["properties"]["filters"]["items"]["properties"]["dimension"]["enum"]
    assert s["additionalProperties"] is False


def test_claude_metric_outside_catalog_is_rejected(engine):
    interp, _ = claude(engine, answer(metrics=["profit"]))
    it = interp.interpret("profit")
    assert it.status == "clarify" and "unknown metric" in it.clarification


def test_claude_unreachable_dimension_is_rejected(engine):
    interp, _ = claude(engine, answer(metrics=["refund_rate"], group_by=["seller__region"]))
    assert interp.interpret("refund rate by seller region").status == "clarify"


def test_claude_clarification_passes_through(engine):
    interp, _ = claude(engine, answer(status="clarify", clarification="Gross or net revenue?"))
    it = interp.interpret("revenue")
    assert it.status == "clarify" and it.clarification == "Gross or net revenue?"


def test_claude_multi_value_equals_becomes_in(engine):
    interp, _ = claude(engine, answer(metrics=["orders"], filters=[{"dimension": "customer__country", "op": "=",
                                                                    "values": ["US", "CA"]}]))
    f = interp.interpret("orders in US and Canada").query.filters[0]
    assert f.op == "in" and set(f.value) == {"US", "CA"}


def test_claude_bad_json_and_refusal(engine):
    interp, _ = claude(engine, "not json")
    assert interp.interpret("x").status == "clarify"
    interp, _ = claude(engine, answer(metrics=["orders"]), stop_reason="refusal")
    assert interp.interpret("x").status == "clarify"


def test_claude_invalid_shape(engine):
    interp, _ = claude(engine, {"status": "maybe"})
    assert interp.interpret("x").status == "clarify"
