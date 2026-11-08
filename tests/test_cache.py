"""Result cache keys, hits, eviction, data versions, and pre-aggregation routing."""

from metriclayer.engine import Engine, ResultCache, cache_key
from metriclayer.warehouse import Scale, build

from .conftest import tiny_connection


def test_cache_key_depends_on_sql_and_version():
    assert cache_key("SELECT 1", "v1") == cache_key("SELECT 1", "v1")
    assert cache_key("SELECT 1", "v1") != cache_key("SELECT 1", "v2")
    assert cache_key("SELECT 1", "v1") != cache_key("SELECT 2", "v1")


def test_second_run_hits_cache(tiny):
    q = {"metrics": ["orders"], "grain": "month"}
    a, b = tiny.run(q), tiny.run(q)
    assert (a.cache_hit, b.cache_hit) == (False, True) and a.rows == b.rows
    assert tiny.cache.stats()["hits"] == 1


def test_equivalent_queries_share_an_entry(tiny):
    tiny.run({"metrics": ["orders"], "group_by": ["order__device"], "grain": "month"})
    assert tiny.run({"metrics": ["orders"], "group_by": ["metric_time__month", "order__device"]}).cache_hit


def test_lru_eviction():
    c = ResultCache(max_entries=2)
    c.put("a", [[1]])
    c.put("b", [[2]])
    c.get("a")
    c.put("c", [[3]])
    assert c.get("b") is None and c.get("a") == [[1]] and len(c.items) == 2


def test_new_data_version_misses(tmp_path, layer):
    q = {"metrics": ["orders"]}
    build(tmp_path / "a.duckdb", Scale(customers=50, sellers=5, products=20, orders=200), seed=1)
    build(tmp_path / "b.duckdb", Scale(customers=50, sellers=5, products=20, orders=200), seed=2)
    ea, eb = Engine.open(tmp_path / "a.duckdb", layer), Engine.open(tmp_path / "b.duckdb", layer)
    assert ea.version != eb.version
    assert cache_key(ea.compile(q).sql, ea.version) != cache_key(eb.compile(q).sql, eb.version)


def test_preaggregation_used_and_counted(tiny):
    r = tiny.run({"metrics": ["gross_revenue"], "group_by": ["product__category"], "grain": "month"})
    assert r.preaggs_used == ["preagg_daily_item_revenue"]
    assert "FROM preagg_daily_item_revenue" in r.compiled.sql
    assert tiny.cache_stats()["preaggregation_hits"] == {"preagg_daily_item_revenue": 1}


def test_preaggregation_skipped_when_dimension_missing(tiny):
    r = tiny.run({"metrics": ["gross_revenue"], "group_by": ["customer__segment"]})
    assert r.preaggs_used == []


def test_preaggregation_skipped_for_non_additive(tiny):
    assert tiny.run({"metrics": ["avg_unit_price"]}).preaggs_used == []


def test_usage_stats_track_metric_and_grain(tiny):
    for _ in range(3):
        tiny.run({"metrics": ["aov"], "grain": "week"}, use_cache=False)
    assert tiny.cache_stats()["most_used"][0] == {"metric": "aov", "grain": "week", "queries": 3}


def test_engine_without_preaggs(layer):
    e = Engine(tiny_connection(), layer, use_preaggs=False)
    assert e.built_preaggs == [] and e.run({"metrics": ["gross_revenue"]}).preaggs_used == []
