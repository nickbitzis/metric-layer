"""FastAPI end to end with the test client."""

import json
import logging

import pytest
from fastapi.testclient import TestClient

from metriclayer.app import create_app
from metriclayer.obs import JsonFormatter

from .test_nl import FakeClient, answer


@pytest.fixture(scope="module")
def client(engine):
    fake = FakeClient(answer(metrics=["orders"], group_by=["order__device"]))
    return TestClient(create_app(engine, claude_client=fake))


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["metrics"] == 21


def test_index_and_static(client):
    assert "Metric Layer" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_metrics_catalog_with_lineage(client):
    ms = {m["name"]: m for m in client.get("/api/metrics").json()["metrics"]}
    assert ms["net_revenue"]["lineage"]["tables"] == ["order_items", "orders", "refunds"]
    assert "seller__region" not in ms["refund_rate"]["dimensions"]


def test_dimensions_with_values(client):
    body = client.get("/api/dimensions").json()
    assert "US" in body["values"]["customer__country"]


def test_query(client):
    r = client.post("/api/query", json={"metrics": ["gross_revenue", "aov"], "group_by": ["customer__country"]})
    body = r.json()
    assert r.status_code == 200 and body["columns"] == ["customer__country", "gross_revenue", "aov"]
    assert len(body["rows"]) == 6 and "WITH" in body["sql"]
    assert r.headers["x-request-id"]


@pytest.mark.parametrize("q,code", [
    ({"metrics": ["nope"]}, "unknown_metric"),
    ({"metrics": ["net_revenue"], "group_by": ["seller__region"]}, "unreachable_dimension"),
    ({"metrics": []}, "no_metrics"),
])
def test_query_errors_are_422(client, q, code):
    r = client.post("/api/query", json=q)
    assert r.status_code == 422 and r.json()["error"]["code"] == code


def test_explain_shows_join_path(client):
    ex = client.post("/api/explain", json={"metrics": ["refund_amount"], "group_by": ["customer__country"]}).json()
    joins = ex["ctes"][0]["joins"]
    assert [(j["from"], j["to"]) for j in joins] == [("refunds", "orders"), ("orders", "customers")]


def test_ask_offline(client):
    body = client.post("/api/ask", json={"question": "conversion rate by traffic source"}).json()
    assert body["interpretation"]["status"] == "ok"
    assert body["result"]["columns"] == ["session__traffic_source", "conversion_rate"]


def test_ask_clarify_has_no_result(client):
    body = client.post("/api/ask", json={"question": "revenue by country"}).json()
    assert body["interpretation"]["status"] == "clarify" and body["result"] is None


def test_ask_claude_provider(client):
    body = client.post("/api/ask", json={"question": "orders by device", "provider": "claude"}).json()
    assert body["interpretation"]["provider"] == "claude" and body["result"]["row_count"] == 3


def test_ask_validation(client):
    assert client.post("/api/ask", json={"question": ""}).status_code == 422
    assert client.post("/api/ask", json={"question": "orders", "provider": "other"}).status_code == 422


def test_cache_and_prometheus(client):
    q = {"metrics": ["sessions"], "grain": "month"}
    client.post("/api/query", json=q)
    assert client.post("/api/query", json=q).json()["cache_hit"] is True
    stats = client.get("/api/cache").json()
    assert stats["hits"] >= 1 and "preagg_daily_item_revenue" in stats["preaggregation_rows"]
    text = client.get("/metrics").text
    assert "metriclayer_http_requests_total" in text and "metriclayer_cache_hits_total" in text


def test_json_log_format():
    rec = logging.LogRecord("metriclayer", logging.INFO, __file__, 1, "request", None, None)
    rec.fields = {"route": "/healthz", "status": 200}
    out = json.loads(JsonFormatter().format(rec))
    assert out["msg"] == "request" and out["route"] == "/healthz" and out["level"] == "info"
