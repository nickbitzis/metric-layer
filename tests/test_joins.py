"""Join path resolution over many_to_one edges."""


def hops(path):
    return [(s.from_model, s.to_model) for s in path]


def test_two_hop_path(layer):
    assert hops(layer.join_path("order_items", "customers")) == [("order_items", "orders"), ("orders", "customers")]


def test_path_through_products_to_sellers(layer):
    assert hops(layer.join_path("order_items", "sellers")) == [("order_items", "products"), ("products", "sellers")]


def test_same_model_is_empty_path(layer):
    assert layer.join_path("orders", "orders") == []


def test_one_to_many_is_never_followed(layer):
    # customers -> orders would repeat customer rows once per order.
    assert layer.join_path("customers", "orders") is None
    assert layer.join_path("orders", "order_items") is None


def test_refunds_cannot_reach_sellers(layer):
    assert layer.join_path("refunds", "sellers") is None


def test_join_steps_carry_keys_and_cardinality(layer):
    step = layer.join_path("sessions", "customers")[0]
    assert (step.from_expr, step.to_expr, step.cardinality) == ("customer_id", "customer_id", "many_to_one")


def test_dimensions_for_ratio_is_intersection(layer):
    dims = layer.dimensions_for("refund_rate")
    assert "customer__country" in dims
    assert "seller__region" not in dims and "refund__reason" not in dims


def test_roas_has_no_shared_categorical_dimension(layer):
    assert layer.dimensions_for("roas") == []


def test_lineage_lists_tables(layer):
    lin = layer.lineage("net_revenue")
    assert lin["depends_on"] == ["gross_revenue", "refund_amount"]
    assert set(lin["tables"]) == {"order_items", "orders", "refunds"}


def test_time_dimension_via_join(layer):
    assert layer.time_dimension("order_items").qualified == "order__ordered_at"
    assert layer.time_dimension("marketing").qualified == "marketing__spend_date"
