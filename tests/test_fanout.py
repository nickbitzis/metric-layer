"""Fan-out avoidance, ratios, cumulative windows, grains, and filters on a hand-built dataset.

Order 1 has 3 line items and 2 refunds, so any query that joins items and refunds repeats both.
Every expected number below was worked out by hand from tests/conftest.py.
"""

import pytest

from .conftest import value


@pytest.mark.parametrize("pre", [True, False])
def test_gross_and_net_revenue(tiny, pre):
    assert value(tiny, {"metrics": ["gross_revenue", "refund_amount", "net_revenue"]}, use_preaggs=pre) == \
        [[115.0, 24.0, 91.0]]


def test_naive_join_double_counts(tiny):
    naive = tiny.sql("""SELECT SUM(oi.quantity * oi.unit_price - oi.discount_amount), SUM(r.amount)
        FROM orders o JOIN order_items oi ON oi.order_id = o.order_id LEFT JOIN refunds r ON r.order_id = o.order_id
        WHERE o.status <> 'cancelled'""")[0]
    assert naive == [165.0, 63.0]  # gross +43%, refunds +163%
    assert naive[0] - naive[1] != 91.0


@pytest.mark.parametrize("pre", [True, False])
def test_net_revenue_by_country(tiny, pre):
    assert value(tiny, {"metrics": ["net_revenue"], "group_by": ["customer__country"]}, use_preaggs=pre) == \
        [["DE", 36.0], ["US", 55.0]]


def test_net_revenue_by_month_books_refunds_when_issued(tiny):
    assert value(tiny, {"metrics": ["gross_revenue", "net_revenue"], "grain": "month"}) == \
        [["2025-01-01", 70.0, 60.0], ["2025-02-01", 45.0, 31.0]]


def test_revenue_by_seller_region_three_hops(tiny):
    assert value(tiny, {"metrics": ["gross_revenue"], "group_by": ["seller__region"]}) == [["EU", 60.0], ["NA", 55.0]]


def test_aov_is_ratio_of_sums(tiny):
    [[aov]] = value(tiny, {"metrics": ["aov"]})
    assert aov == pytest.approx(115 / 3)
    item_avg = tiny.sql("""SELECT AVG(oi.quantity * oi.unit_price - oi.discount_amount)
        FROM orders o JOIN order_items oi ON oi.order_id = o.order_id WHERE o.status <> 'cancelled'""")[0][0]
    assert item_avg == pytest.approx(115 / 6)


def test_ratio_by_group_not_average_of_ratios(tiny):
    rows = value(tiny, {"metrics": ["cancellation_rate"], "group_by": ["order__device"]})
    assert rows == [["ios", 0.0], ["web", pytest.approx(1 / 3)]]
    assert value(tiny, {"metrics": ["cancellation_rate"]}) == [[0.25]]  # not the mean of 0 and 1/3


def test_sessions_keep_anonymous_visits(tiny):
    rows = value(tiny, {"metrics": ["sessions", "conversion_rate"], "group_by": ["customer__country"]})
    assert rows == [["DE", 1, 1.0], ["US", 2, 1.0], [None, 2, 0.0]]
    assert value(tiny, {"metrics": ["conversion_rate"]}) == [[0.6]]


def test_not_equal_filter_keeps_null_group(tiny):
    assert value(tiny, {"metrics": ["sessions"], "filters": [
        {"dimension": "customer__country", "op": "!=", "value": "US"}]}) == [[3]]


@pytest.mark.parametrize("op,val,expected", [("in", ["toys"], 55.0), ("not_in", ["toys"], 60.0),
                                             ("=", "books", 60.0), ("!=", "books", 55.0)])
def test_category_filters(tiny, op, val, expected):
    assert value(tiny, {"metrics": ["gross_revenue"], "filters": [
        {"dimension": "product__category", "op": op, "value": val}]}) == [[expected]]


def test_filter_on_joined_dimension_and_time_range(tiny):
    assert value(tiny, {"metrics": ["gross_revenue"], "filters": [
        {"dimension": "customer__segment", "op": "=", "value": "consumer"}],
        "time_range": {"start": "2025-01-15", "end": "2025-02-01"}}) == [[20.0]]


@pytest.mark.parametrize("grain,expected", [
    ("day", [["2025-01-10", 50.0], ["2025-01-20", 20.0], ["2025-02-05", 45.0]]),
    ("week", [["2025-01-06", 50.0], ["2025-01-20", 20.0], ["2025-02-03", 45.0]]),
    ("month", [["2025-01-01", 70.0], ["2025-02-01", 45.0]]),
    ("quarter", [["2025-01-01", 115.0]]),
    ("year", [["2025-01-01", 115.0]]),
])
def test_time_grains(tiny, grain, expected):
    assert value(tiny, {"metrics": ["gross_revenue"], "grain": grain}) == expected
    assert value(tiny, {"metrics": ["gross_revenue"], "grain": grain}, use_preaggs=False) == expected


def test_cumulative_daily_window(tiny):
    rows = dict(value(tiny, {"metrics": ["active_buyers_28d"], "grain": "day",
                             "time_range": {"start": "2025-02-01", "end": "2025-02-20"}}))
    assert rows["2025-02-01"] == 1  # customer 1 (Jan 20) is inside Jan 5 to Feb 1
    assert rows["2025-02-05"] == 2  # customer 2 orders that day
    assert rows["2025-02-17"] == 1  # customer 1 dropped out of the 28 days
    assert "2025-02-20" not in rows


def test_cumulative_monthly_window_is_28_days_not_month(tiny):
    assert value(tiny, {"metrics": ["active_buyers_28d", "buyers"], "grain": "month"}) == \
        [["2025-01-01", 1, 1], ["2025-02-01", 1, 1]]


def test_cumulative_all_time(tiny):
    assert value(tiny, {"metrics": ["total_customers", "new_customers"], "grain": "month",
                        "time_range": {"start": "2025-01-01", "end": "2025-03-01"}}) == \
        [["2025-01-01", 2, 2], ["2025-02-01", 2, 0]]


def test_roas_across_unrelated_models(tiny):
    assert value(tiny, {"metrics": ["roas"], "grain": "month"}) == [["2025-01-01", 1.0], ["2025-02-01", 1.5]]
