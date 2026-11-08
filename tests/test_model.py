"""YAML validation: the default model loads, and each kind of mistake is reported."""

import copy

import pytest
import yaml

from metriclayer.model import DEFAULT_PATH, ModelError, SemanticLayer

RAW = yaml.safe_load(DEFAULT_PATH.read_text())


def broken(mutate):
    raw = copy.deepcopy(RAW)
    mutate(raw)
    with pytest.raises(ModelError) as e:
        SemanticLayer(raw)
    return " | ".join(e.value.problems)


def model(raw, name):
    return next(m for m in raw["semantic_models"] if m["name"] == name)


def metric(raw, name):
    return next(m for m in raw["metrics"] if m["name"] == name)


def test_default_model_loads(layer):
    assert len(layer.models) == 8
    assert len(layer.metrics) == 21
    assert {m.type for m in layer.metrics.values()} == {"simple", "ratio", "derived", "cumulative"}
    assert "customer__country" in layer.dimensions and "marketing__channel" in layer.dimensions


def test_every_metric_has_owner_and_description(layer):
    for m in layer.metrics.values():
        assert m.owner and m.description, m.name


def test_window_parsed_in_days(layer):
    assert layer.metrics["active_buyers_28d"].window_days == 28
    assert layer.metrics["total_customers"].window_days is None


def test_unknown_agg():
    msg = broken(lambda r: model(r, "orders")["measures"][0].update(agg="median"))
    assert "unknown agg 'median'" in msg


def test_unknown_measure_in_metric():
    assert "unknown measure 'nope'" in broken(lambda r: metric(r, "gross_revenue").update(measure="nope"))


def test_foreign_entity_without_primary():
    msg = broken(lambda r: model(r, "orders")["entities"].append({"name": "warehouse", "type": "foreign"}))
    assert "foreign entity 'warehouse' has no model where it is primary" in msg


def test_entity_primary_twice():
    msg = broken(lambda r: model(r, "sessions")["entities"].append({"name": "order", "type": "primary",
                                                                    "expr": "session_id"}))
    assert "more than one primary entity" in msg


def test_derived_cycle():
    def mutate(r):
        metric(r, "net_revenue")["expr"] = "gross_revenue - roas"
        metric(r, "roas")["expr"] = "net_revenue / marketing_spend"
    assert "cycle" in broken(mutate)


def test_derived_expr_rejects_sql():
    msg = broken(lambda r: metric(r, "net_revenue").update(expr="gross_revenue; DROP TABLE orders"))
    assert "expr may only use metric names" in msg


def test_metric_filter_on_unreachable_dimension():
    msg = broken(lambda r: metric(r, "refund_amount").update(
        filter=[{"dimension": "product__category", "op": "=", "value": "toys"}]))
    assert "not reachable from refunds" in msg


def test_bad_window():
    assert "window must look like" in broken(lambda r: metric(r, "active_buyers_28d").update(window="a month"))


def test_missing_owner():
    assert "missing owner" in broken(lambda r: metric(r, "orders").pop("owner"))


def test_preaggregation_needs_additive_measures():
    msg = broken(lambda r: r["preaggregations"][0]["measures"].append("unit_price_avg"))
    assert "must be an additive measure" in msg


def test_agg_time_dimension_must_be_time():
    msg = broken(lambda r: model(r, "orders").update(agg_time_dimension="status"))
    assert "agg_time_dimension 'status' is not a time dimension" in msg


def test_all_problems_reported_at_once():
    def mutate(r):
        metric(r, "orders")["type"] = "weird"
        model(r, "orders")["measures"][0]["agg"] = "median"
    e = broken(mutate)
    assert "weird" in e and "median" in e
