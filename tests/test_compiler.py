"""Query validation and SQL shape."""

import pytest

from metriclayer.compiler import Compiler, MetricQuery, QueryError, lit, qualify


@pytest.fixture
def c(layer):
    return Compiler(layer)


def err(c, q):
    with pytest.raises(QueryError) as e:
        c.compile(MetricQuery.from_dict(q))
    return e.value


def test_unknown_metric_suggests(c):
    e = err(c, {"metrics": ["gross_revenu"]})
    assert e.code == "unknown_metric" and "Did you mean gross_revenue" in e.message


def test_unknown_dimension(c):
    e = err(c, {"metrics": ["orders"], "group_by": ["customer__contry"]})
    assert e.code == "unknown_dimension" and "customer__country" in e.message


def test_unreachable_dimension_names_the_measure(c):
    e = err(c, {"metrics": ["net_revenue"], "group_by": ["seller__region"]})
    assert e.code == "unreachable_dimension"
    assert "via refund_amount" in e.message and "refunds" in e.message


def test_unreachable_filter_dimension(c):
    e = err(c, {"metrics": ["sessions"], "filters": [{"dimension": "product__category", "op": "=", "value": "toys"}]})
    assert e.code == "unreachable_dimension"


def test_invalid_grain(c):
    assert err(c, {"metrics": ["orders"], "grain": "hour"}).code == "invalid_grain"


def test_cumulative_needs_time(c):
    assert err(c, {"metrics": ["active_buyers_28d"]}).code == "cumulative_needs_time"


@pytest.mark.parametrize("tr", [{"start": "2025-13-01", "end": "2026-01-01"},
                                {"start": "2026-01-01", "end": "2025-01-01"}])
def test_invalid_time_range(c, tr):
    assert err(c, {"metrics": ["orders"], "time_range": tr}).code == "invalid_time_range"


def test_bad_filter_op(c):
    f = {"dimension": "order__device", "op": "like", "value": "w%"}
    assert err(c, {"metrics": ["orders"], "filters": [f]}).code == "invalid_filter"


def test_equals_with_list_rejected(c):
    e = err(c, {"metrics": ["orders"], "filters": [{"dimension": "order__device", "op": "=", "value": ["web", "ios"]}]})
    assert e.code == "invalid_filter"


def test_time_dimension_in_group_by_rejected(c):
    assert err(c, {"metrics": ["orders"], "group_by": ["order__ordered_at"]}).code == "time_dimension_in_group_by"


def test_contradicting_filter_suggests_metric(c):
    e = err(c, {"metrics": ["orders"], "filters": [{"dimension": "order__status", "op": "=", "value": "cancelled"}]})
    assert e.code == "contradicts_metric_filter" and "cancelled_orders" in e.message


def test_too_many_metrics(c, layer):
    assert err(c, {"metrics": list(layer.metrics)[:9]}).code == "too_many_metrics"


def test_grain_adds_metric_time(c):
    comp = c.compile(MetricQuery.from_dict({"metrics": ["orders"], "grain": "week"}))
    assert comp.columns == ["metric_time__week", "orders"]
    assert "date_trunc('week', orders.ordered_at)" in comp.sql


def test_metric_time_suffix_sets_grain(c):
    comp = c.compile(MetricQuery.from_dict({"metrics": ["orders"], "group_by": ["metric_time__quarter"]}))
    assert comp.dims == ["metric_time__quarter"]


def test_each_model_gets_its_own_cte(c):
    comp = c.compile(MetricQuery.from_dict({"metrics": ["net_revenue", "aov"], "group_by": ["customer__country"]}))
    names = [x.name for x in comp.ctes]
    assert sorted(names) == ["agg_order_items", "agg_orders", "agg_refunds"]
    # The items CTE never joins refunds, and the refunds CTE never joins items.
    items = next(x for x in comp.ctes if x.model == "order_items")
    assert "refunds" not in items.sql
    assert all(j["cardinality"] == "many_to_one" for x in comp.ctes for j in x.joins)


def test_ratio_is_ratio_of_sums(c):
    sql = c.compile(MetricQuery.from_dict({"metrics": ["aov"]})).sql
    assert "CAST(COALESCE(agg_order_items.item_revenue, 0) AS DOUBLE) / NULLIF(agg_orders.order_rows, 0) AS aov" in sql
    assert "AVG(" not in sql


def test_derived_coalesces_inputs(c):
    sql = c.compile(MetricQuery.from_dict({"metrics": ["net_revenue"]})).sql
    assert "COALESCE(agg_order_items.item_revenue, 0) - COALESCE(agg_refunds.refund_total, 0)" in sql


def test_derived_division_guards_zero(c):
    sql = c.compile(MetricQuery.from_dict({"metrics": ["roas"], "grain": "month"})).sql
    assert "NULLIF(COALESCE(agg_marketing.spend, 0), 0)" in sql


def test_filter_values_are_escaped(c):
    sql = c.compile(MetricQuery.from_dict({"metrics": ["orders"], "filters": [
        {"dimension": "order__device", "op": "=", "value": "web' OR '1'='1"}]})).sql
    assert "orders.device = 'web'' OR ''1''=''1'" in sql


def test_not_equal_keeps_nulls(c):
    sql = c.compile(MetricQuery.from_dict({"metrics": ["sessions"], "filters": [
        {"dimension": "customer__country", "op": "!=", "value": "US"}]})).sql
    assert "customers.country IS DISTINCT FROM 'US'" in sql


def test_not_in_keeps_nulls(c):
    sql = c.compile(MetricQuery.from_dict({"metrics": ["sessions"], "filters": [
        {"dimension": "customer__country", "op": "not_in", "value": ["US", "DE"]}]})).sql
    assert "(customers.country NOT IN ('US', 'DE') OR customers.country IS NULL)" in sql


def test_joins_are_left_joins(c):
    sql = c.compile(MetricQuery.from_dict({"metrics": ["sessions"], "group_by": ["customer__country"]})).sql
    assert "LEFT JOIN customers AS customers ON sessions.customer_id = customers.customer_id" in sql
    assert " INNER JOIN " not in sql


def test_multi_cte_join_on_keys_null_safe(c):
    q = {"metrics": ["gross_revenue", "sessions"], "group_by": ["customer__country"]}
    sql = c.compile(MetricQuery.from_dict(q)).sql
    assert "keys AS (" in sql and "IS NOT DISTINCT FROM" in sql


def test_qualify_skips_keywords_and_functions():
    assert qualify("CASE WHEN converted THEN 1 ELSE 0 END", "s") == "CASE WHEN s.converted THEN 1 ELSE 0 END"
    expected = "oi.quantity * oi.unit_price - oi.discount_amount"
    assert qualify("quantity * unit_price - discount_amount", "oi") == expected
    assert qualify("COALESCE(amount, 0)", "r") == "COALESCE(r.amount, 0)"


def test_lit_escapes_quotes():
    assert lit("O'Brien") == "'O''Brien'"


def test_query_key_is_order_insensitive():
    a = MetricQuery.from_dict({"metrics": ["a", "b"], "group_by": ["x", "y"]})
    b = MetricQuery.from_dict({"metrics": ["b", "a"], "group_by": ["y", "x"]})
    assert a.key() == b.key()
