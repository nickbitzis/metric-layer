import duckdb
import pytest

from metriclayer.engine import Engine
from metriclayer.model import load_model
from metriclayer.warehouse import TYPES, Scale, build

TINY = {
    "customers": ["(1, DATE '2025-01-01', 'US', 'organic', 'consumer')",
                  "(2, DATE '2025-01-01', 'DE', 'email', 'business')"],
    "sellers": ["(1, 'Alder Goods 1', 'NA', 'pro')", "(2, 'Birch Supply 2', 'EU', 'standard')"],
    "products": ["(1, 1, 'toys', 10)", "(2, 2, 'books', 20)"],
    "orders": ["(1, 1, TIMESTAMP '2025-01-10 10:00', 'completed', 'web', 5)",
               "(2, 1, TIMESTAMP '2025-01-20 10:00', 'completed', 'ios', 0)",
               "(3, 2, TIMESTAMP '2025-02-05 10:00', 'completed', 'web', 5)",
               "(4, 2, TIMESTAMP '2025-02-06 10:00', 'cancelled', 'web', 0)"],
    # order 1: 20 + 20 + 10 = 50, order 2: 20, order 3: 25 + 20 = 45, order 4 (cancelled): 10
    "order_items": ["(1, 1, 1, 2, 10, 0)", "(2, 1, 2, 1, 20, 0)", "(3, 1, 1, 1, 10, 0)", "(4, 2, 2, 1, 20, 0)",
                    "(5, 3, 1, 3, 10, 5)", "(6, 3, 2, 1, 20, 0)", "(7, 4, 1, 1, 10, 0)"],
    "refunds": ["(1, 1, TIMESTAMP '2025-01-15 12:00', 10, 'damaged')",
                "(2, 1, TIMESTAMP '2025-02-02 12:00', 5, 'late_delivery')",
                "(3, 3, TIMESTAMP '2025-02-10 12:00', 9, 'damaged')"],
    "sessions": ["(1, 1, TIMESTAMP '2025-01-10 09:50', 'web', 'organic', true)",
                 "(2, NULL, TIMESTAMP '2025-01-11 08:00', 'web', 'social', false)",
                 "(3, NULL, TIMESTAMP '2025-01-12 08:00', 'ios', 'organic', false)",
                 "(4, 2, TIMESTAMP '2025-02-05 09:00', 'web', 'email', true)",
                 "(5, 1, TIMESTAMP '2025-01-20 09:00', 'ios', 'direct', true)"],
    "marketing_spend": ["(DATE '2025-01-01', 'paid_search', 50)", "(DATE '2025-01-15', 'social', 20)",
                        "(DATE '2025-02-01', 'paid_search', 30)"],
}


def tiny_connection():
    con = duckdb.connect()
    for name, rows in TINY.items():
        con.execute(f"CREATE TABLE {name} ({TYPES[name]})")
        con.execute(f"INSERT INTO {name} VALUES {', '.join(rows)}")
    con.execute("CREATE TABLE _meta (data_version VARCHAR, seed INTEGER, built_at TIMESTAMP)")
    con.execute("INSERT INTO _meta VALUES ('tiny-v1', 0, now())")
    return con


@pytest.fixture(scope="session")
def layer():
    return load_model()


@pytest.fixture
def tiny(layer):
    return Engine(tiny_connection(), layer)


@pytest.fixture(scope="session")
def small_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("wh") / "small.duckdb"
    build(path, Scale.small(), seed=7)
    return path


@pytest.fixture(scope="session")
def engine(small_db, layer):
    return Engine.open(small_db, layer)


def value(engine, query, **kw):
    return engine.run(query, use_cache=False, **kw).rows
