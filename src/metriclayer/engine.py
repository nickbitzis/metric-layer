"""Engine: compile, run on DuckDB, cache results, and route to pre-aggregations.

Result cache key = sha256(compiled SQL + data version). Rebuilding the warehouse changes the data version, so old
entries can never be served for new data. Pre-aggregation tables are built at startup from the YAML and used when a
query's measures are additive and every dimension it needs is in the table.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb

from .compiler import Compiled, Compiler, MetricQuery
from .model import SemanticLayer, load_model
from .warehouse import data_version


def cache_key(sql: str, version: str) -> str:
    return hashlib.sha256(f"{version}\n{sql}".encode()).hexdigest()


def _jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    return v


class ResultCache:
    def __init__(self, max_entries: int = 256):
        self.max_entries = max_entries
        self.items: OrderedDict[str, list[list]] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.lock = threading.Lock()

    def get(self, key: str):
        with self.lock:
            if key in self.items:
                self.items.move_to_end(key)
                self.hits += 1
                return self.items[key]
            self.misses += 1
            return None

    def put(self, key: str, rows: list[list]) -> None:
        with self.lock:
            self.items[key] = rows
            self.items.move_to_end(key)
            while len(self.items) > self.max_entries:
                self.items.popitem(last=False)

    def clear(self) -> None:
        with self.lock:
            self.items.clear()

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {"entries": len(self.items), "hits": self.hits, "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else 0.0}


@dataclass
class Result:
    columns: list[str]
    rows: list[list]
    compiled: Compiled
    cache_hit: bool
    elapsed_ms: float
    data_version: str
    preaggs_used: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"columns": self.columns, "rows": self.rows, "sql": self.compiled.sql,
                "query": self.compiled.query.to_dict(), "cache_hit": self.cache_hit,
                "preaggregations_used": self.preaggs_used, "elapsed_ms": round(self.elapsed_ms, 2),
                "data_version": self.data_version, "row_count": len(self.rows)}


class Engine:
    def __init__(self, con: duckdb.DuckDBPyConnection, layer: SemanticLayer | None = None, use_preaggs: bool = True):
        self.con = con
        self.layer = layer or load_model()
        self.lock = threading.Lock()
        self.cache = ResultCache()
        self.usage: Counter[tuple[str, str]] = Counter()
        self.preagg_hits: Counter[str] = Counter()
        self.version = data_version(con)
        self.raw = Compiler(self.layer)
        self.built_preaggs = self.build_preaggs() if use_preaggs else []
        self.compiler = Compiler(self.layer, self.built_preaggs)

    @classmethod
    def open(cls, db: Path | str, layer: SemanticLayer | None = None, **kw) -> Engine:
        return cls(duckdb.connect(str(db)), layer, **kw)

    def build_preaggs(self):
        built = []
        with self.lock:
            for pa in self.layer.preaggregations:
                self.con.execute(f"CREATE OR REPLACE TABLE {pa.table} AS {self.raw.preagg_sql(pa)}")
                built.append(pa)
        return built

    def preagg_rows(self) -> dict[str, int]:
        return {pa.table: self.con.execute(f"SELECT count(*) FROM {pa.table}").fetchone()[0]
                for pa in self.built_preaggs}

    def compile(self, q: MetricQuery | dict, use_preaggs: bool = True) -> Compiled:
        q = MetricQuery.from_dict(q) if isinstance(q, dict) else q
        return (self.compiler if use_preaggs else self.raw).compile(q)

    def sql(self, sql: str, params=None) -> list[list]:
        with self.lock:
            return [[_jsonable(v) for v in row] for row in self.con.execute(sql, params or []).fetchall()]

    def run(self, q: MetricQuery | dict, use_cache: bool = True, use_preaggs: bool = True) -> Result:
        t0 = time.perf_counter()
        compiled = self.compile(q, use_preaggs)
        for m in compiled.metrics:
            self.usage[(m, compiled.query.grain or "none")] += 1
        key = cache_key(compiled.sql, self.version)
        rows = self.cache.get(key) if use_cache else None
        hit = rows is not None
        if not hit:
            rows = self.sql(compiled.sql)
            if use_cache:
                self.cache.put(key, rows)
        for p in compiled.preaggs_used:
            self.preagg_hits[p] += 1
        return Result(compiled.columns, rows, compiled, hit, (time.perf_counter() - t0) * 1000, self.version,
                      compiled.preaggs_used)

    def cache_stats(self) -> dict:
        top = [{"metric": m, "grain": g, "queries": n} for (m, g), n in self.usage.most_common(5)]
        return {**self.cache.stats(), "data_version": self.version, "preaggregation_hits": dict(self.preagg_hits),
                "preaggregation_rows": self.preagg_rows(), "most_used": top}
