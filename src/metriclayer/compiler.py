"""Metric queries and the SQL compiler.

How fan-out is avoided: every measure is aggregated in its own CTE, from its own table, at the query grain.
A CTE only joins along many_to_one paths (to look up dimensions), so the measure's rows are never repeated.
The CTEs are then joined to each other on the group-by keys, where each side has exactly one row per key.
Ratios are computed after aggregation (sum of numerator over sum of denominator), and derived metrics combine
already-aggregated metrics. Cumulative metrics join a spine of period ends to the raw rows in a window.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import date

from .model import ADDITIVE, GRAINS, OPS, Filter, JoinStep, MeasureRef, Metric, PreAggregation, SemanticLayer

KEYWORDS = {"case", "when", "then", "else", "end", "and", "or", "not", "null", "is", "true", "false", "in", "as",
            "cast", "double", "integer", "distinct"}
AGG_SQL = {"sum": "SUM({})", "count": "COUNT({})", "count_distinct": "COUNT(DISTINCT {})", "average": "AVG({})",
           "min": "MIN({})", "max": "MAX({})"}
COUNTABLE = {"sum", "count", "count_distinct"}
MAX_METRICS = 8
MAX_DIMS = 4


class QueryError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


@dataclass
class MetricQuery:
    metrics: list[str]
    group_by: list[str] = field(default_factory=list)
    grain: str | None = None
    filters: list[Filter] = field(default_factory=list)
    time_range: tuple[str, str] | None = None  # [start, end) as ISO dates
    limit: int = 5000

    @classmethod
    def from_dict(cls, d: dict) -> MetricQuery:
        if not isinstance(d, dict):
            raise QueryError("invalid_query", "query must be a JSON object")
        metrics = d.get("metrics") or []
        if isinstance(metrics, str):
            metrics = [metrics]
        tr = d.get("time_range")
        if isinstance(tr, dict):
            tr = (tr.get("start"), tr.get("end")) if tr.get("start") or tr.get("end") else None
        elif isinstance(tr, list | tuple):
            tr = tuple(tr) if tr else None
        filters = []
        for f in d.get("filters") or []:
            if not isinstance(f, dict):
                raise QueryError("invalid_filter", "each filter must be an object with dimension, op, value")
            filters.append(Filter.parse(f))
        return cls(list(metrics), list(d.get("group_by") or []), d.get("grain") or None, filters, tr,
                   int(d.get("limit") or 5000))

    def to_dict(self) -> dict:
        return {"metrics": list(self.metrics), "group_by": list(self.group_by), "grain": self.grain,
                "filters": [f.to_dict() for f in self.filters],
                "time_range": {"start": self.time_range[0], "end": self.time_range[1]} if self.time_range else None}

    def key(self) -> tuple:
        """Order-insensitive identity, used to compare an interpretation with a gold query."""
        fs = tuple(sorted((f.dimension, f.op, tuple(sorted(f.value)) if isinstance(f.value, tuple) else f.value)
                          for f in self.filters))
        return (tuple(sorted(self.metrics)), tuple(sorted(self.group_by)), self.grain, fs, self.time_range)


@dataclass
class CTE:
    name: str
    kind: str  # aggregate | cumulative
    model: str
    table: str
    source: str  # the table read, or the pre-aggregation table
    measures: list[str]
    filters: list[dict]
    joins: list[dict]
    sql: str


@dataclass
class Compiled:
    sql: str
    columns: list[str]
    dims: list[str]
    metrics: list[str]
    ctes: list[CTE]
    query: MetricQuery
    preaggs_used: list[str]

    def explain(self) -> dict:
        return {
            "sql": self.sql,
            "columns": self.columns,
            "ctes": [c.__dict__ for c in self.ctes],
            "preaggregations_used": self.preaggs_used,
            "strategy": "Each measure is aggregated at the query grain in its own CTE, joining only many_to_one "
                        "paths, then CTEs are joined on the group-by keys.",
        }


def lit(v: str) -> str:
    """A SQL string literal with quotes escaped. Values never reach SQL any other way."""
    return "'" + str(v).replace("'", "''") + "'"


def qualify(expr: str, alias: str) -> str:
    """Prefix bare column names in a measure or dimension expression with the table alias."""
    out = []
    for m in re.finditer(r"'[^']*'|[A-Za-z_][A-Za-z0-9_]*(?:\s*\()?|\S|\s+", expr):
        tok = m.group(0)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", tok) and tok.lower() not in KEYWORDS:
            out.append(f"{alias}.{tok}")
        else:
            out.append(tok)
    return "".join(out)


def _check_date(s, label: str) -> str:
    try:
        return date.fromisoformat(str(s)).isoformat()
    except ValueError:
        raise QueryError("invalid_time_range", f"time_range {label} {s!r} is not a YYYY-MM-DD date") from None


def _suggest(name: str, options) -> str:
    close = difflib.get_close_matches(name, list(options), n=2, cutoff=0.5)
    return f" Did you mean {' or '.join(close)}?" if close else ""


def _values(f: Filter) -> set[str]:
    return set(f.value) if isinstance(f.value, tuple) else {f.value}


def _contradicts(q: Filter, metric: Filter) -> bool:
    """True when a query filter selects only rows the metric's own filter already excludes."""
    if q.op not in ("=", "in"):
        return False
    wanted = _values(q)
    if metric.op in ("=", "in"):
        return not (wanted & _values(metric))
    return wanted <= _values(metric)


def _hint(layer: SemanticLayer, f: Filter) -> str:
    for m in layer.metrics.values():
        if m.type == "simple" and any(mf.dimension == f.dimension and mf.op in ("=", "in")
                                      and _values(f) <= _values(mf) for mf in m.filters):
            return f" Did you mean the metric {m.name}?"
    return ""


class Compiler:
    def __init__(self, layer: SemanticLayer, preaggs: list[PreAggregation] | None = None):
        self.layer = layer
        self.preaggs = preaggs or []

    # ---------- validation ----------
    def validate(self, q: MetricQuery) -> MetricQuery:
        L = self.layer
        if not q.metrics:
            raise QueryError("no_metrics", "pick at least one metric")
        if len(q.metrics) > MAX_METRICS:
            raise QueryError("too_many_metrics", f"at most {MAX_METRICS} metrics per query")
        for m in q.metrics:
            if m not in L.metrics:
                raise QueryError("unknown_metric", f"unknown metric {m!r}.{_suggest(m, L.metrics)}")
        if q.grain is not None and q.grain not in GRAINS:
            raise QueryError("invalid_grain", f"grain {q.grain!r} is not one of {', '.join(GRAINS)}")
        group_by = []
        for d in q.group_by:
            if d.startswith("metric_time"):
                if "__" in d and q.grain is None:
                    q.grain = d.split("__", 1)[1]
                    if q.grain not in GRAINS:
                        raise QueryError("invalid_grain", f"grain {q.grain!r} is not one of {', '.join(GRAINS)}")
                continue
            if d not in L.dimensions:
                raise QueryError("unknown_dimension", f"unknown dimension {d!r}.{_suggest(d, L.dimensions)}")
            if L.dimensions[d].type != "categorical":
                raise QueryError("time_dimension_in_group_by",
                                 f"{d} is a time dimension; group by metric_time with a grain instead")
            if d not in group_by:
                group_by.append(d)
        if len(group_by) > MAX_DIMS:
            raise QueryError("too_many_dimensions", f"at most {MAX_DIMS} dimensions per query")
        has_time = q.grain is not None or any(d.startswith("metric_time") for d in q.group_by)
        if has_time and q.grain is None:
            q.grain = "day"
        q.group_by = (["metric_time"] if has_time else []) + group_by
        for f in q.filters:
            if f.op not in OPS:
                raise QueryError("invalid_filter", f"filter op {f.op!r} is not one of {', '.join(sorted(OPS))}")
            if f.dimension not in L.dimensions:
                raise QueryError("unknown_dimension",
                                 f"unknown filter dimension {f.dimension!r}.{_suggest(f.dimension, L.dimensions)}")
            if L.dimensions[f.dimension].type != "categorical":
                raise QueryError("invalid_filter", f"{f.dimension} is a time dimension; use time_range instead")
            if f.op in ("in", "not_in") and not isinstance(f.value, tuple):
                f2 = Filter(f.dimension, f.op, (f.value,) if f.value is not None else ())
                q.filters[q.filters.index(f)] = f2
            elif f.op in ("=", "!=") and isinstance(f.value, tuple):
                raise QueryError("invalid_filter", f"op {f.op} takes one value; use in or not_in for a list")
            if f.value is None or f.value == ():
                raise QueryError("invalid_filter", f"filter on {f.dimension} has no value")
        if q.time_range is not None:
            if len(q.time_range) != 2:
                raise QueryError("invalid_time_range", "time_range needs start and end")
            start = _check_date(q.time_range[0], "start")
            end = _check_date(q.time_range[1], "end")
            if end <= start:
                raise QueryError("invalid_time_range", f"time_range end {end} must be after start {start}")
            q.time_range = (start, end)
        for name in q.metrics:
            for leaf in L.leaf_metrics(name):
                for ref in leaf.measure_refs():
                    model = L.measures[ref.measure].model
                    for d in [d for d in q.group_by if d != "metric_time"] + [f.dimension for f in q.filters]:
                        if L.join_path(model, L.dimensions[d].model) is None:
                            via = f" (via {leaf.name})" if leaf.name != name else ""
                            raise QueryError(
                                "unreachable_dimension",
                                f"dimension {d} is not reachable from {name}{via}: its measure {ref.measure} lives "
                                f"in {model}, and only many-to-one joins are followed so measures never fan out. "
                                f"Dimensions available for {name}: {', '.join(L.dimensions_for(name)) or 'none'}.")
                    for f in q.filters:
                        for mf in ref.filters:
                            if f.dimension == mf.dimension and _contradicts(f, mf):
                                raise QueryError(
                                    "contradicts_metric_filter",
                                    f"{name} already applies {mf.dimension} {mf.op} {mf.value}, so the filter "
                                    f"{f.dimension} {f.op} {f.value} would always return nothing.{_hint(L, f)}")
                    if leaf.type == "cumulative" and "metric_time" not in q.group_by:
                        raise QueryError("cumulative_needs_time",
                                         f"{leaf.name} is cumulative, so the query needs metric_time with a grain")
        return q

    # ---------- pieces ----------
    def _dim_expr(self, dim: str) -> str:
        d = self.layer.dimensions[dim]
        return qualify(d.expr, d.model)

    def _filter_sql(self, f: Filter, col: str | None = None) -> str:
        c = col or self._dim_expr(f.dimension)
        if f.op == "=":
            return f"{c} = {lit(f.value)}"
        if f.op == "!=":
            return f"{c} IS DISTINCT FROM {lit(f.value)}"
        vals = ", ".join(lit(v) for v in f.value)
        if f.op == "in":
            return f"{c} IN ({vals})"
        return f"({c} NOT IN ({vals}) OR {c} IS NULL)"

    def _joins(self, model: str, dims: list[str]) -> list[JoinStep]:
        steps: list[JoinStep] = []
        for d in dims:
            for s in self.layer.join_path(model, self.layer.dimensions[d].model) or []:
                if s not in steps:
                    steps.append(s)
        return steps

    def _from(self, model: str, steps: list[JoinStep]) -> str:
        m = self.layer.models[model]
        lines = [f"FROM {m.table} AS {m.name}"]
        for s in steps:
            t = self.layer.models[s.to_model]
            lines.append(f"LEFT JOIN {t.table} AS {t.name} ON {s.from_model}.{s.from_expr} = {t.name}.{s.to_expr}")
        return "\n    ".join(lines)

    def _time_expr(self, model: str) -> str:
        td = self.layer.time_dimension(model)
        return qualify(td.expr, td.model)

    def _agg(self, measure: str) -> str:
        ms = self.layer.measures[measure]
        return AGG_SQL[ms.agg].format(qualify(ms.expr, ms.model))

    def _preagg_for(self, model: str, measures: list[str], needed: list[str],
                    cumulative: bool) -> PreAggregation | None:
        if cumulative:
            return None
        for pa in self.preaggs:
            if (pa.model == model and all(m in pa.measures for m in measures)
                    and all(d in pa.dimensions for d in needed)):
                return pa
        return None

    def aggregate_cte(self, name: str, model: str, refs: list[MeasureRef], q: MetricQuery,
                      metric_filters: tuple[Filter, ...]) -> CTE:
        L = self.layer
        dims = [d for d in q.group_by if d != "metric_time"]
        filters = list(q.filters) + list(metric_filters)
        measures = [r.measure for r in refs]
        needed = dims + [f.dimension for f in filters]
        pa = self._preagg_for(model, measures, needed, False)
        select, group = [], []
        if pa is not None:
            if "metric_time" in q.group_by:
                select.append(f"CAST(date_trunc('{q.grain}', metric_time__day) AS DATE) AS metric_time__{q.grain}")
            select += [f"{d} AS {d}" for d in dims]
            group = list(range(1, len(select) + 1))
            agg = [f"SUM({m}) AS {m}" for m in measures]
            where = [self._filter_sql(f, col=f.dimension) for f in filters]
            if q.time_range:
                where += [f"metric_time__day >= DATE {lit(q.time_range[0])}",
                          f"metric_time__day < DATE {lit(q.time_range[1])}"]
            from_sql, joins, source = f"FROM {pa.table}", [], pa.table
        else:
            time_dims = [L.time_dimension(model).qualified] if (q.time_range or "metric_time" in q.group_by) else []
            steps = self._joins(model, needed + time_dims)
            if "metric_time" in q.group_by:
                select.append(f"CAST(date_trunc('{q.grain}', {self._time_expr(model)}) AS DATE) "
                              f"AS metric_time__{q.grain}")
            select += [f"{self._dim_expr(d)} AS {d}" for d in dims]
            group = list(range(1, len(select) + 1))
            agg = [f"{self._agg(m)} AS {m}" for m in measures]
            where = [self._filter_sql(f) for f in filters]
            if q.time_range:
                t = self._time_expr(model)
                where += [f"{t} >= TIMESTAMP {lit(q.time_range[0])}", f"{t} < TIMESTAMP {lit(q.time_range[1])}"]
            from_sql, source = self._from(model, steps), L.models[model].table
            joins = [{"from": s.from_model, "to": s.to_model, "on": f"{s.from_model}.{s.from_expr} = "
                      f"{s.to_model}.{s.to_expr}", "cardinality": s.cardinality, "join": "LEFT JOIN"} for s in steps]
        sql = "SELECT\n      " + ",\n      ".join(select + agg) + "\n    " + from_sql
        if where:
            sql += "\n    WHERE " + "\n      AND ".join(where)
        if group:
            sql += "\n    GROUP BY " + ", ".join(str(g) for g in group)
        return CTE(name, "aggregate", model, L.models[model].table, source, measures,
                   [f.to_dict() for f in filters], joins, sql)

    def cumulative_cte(self, name: str, model: str, ref: MeasureRef, window_days: int | None,
                       q: MetricQuery) -> tuple[CTE, str]:
        L = self.layer
        g = q.grain
        dims = [d for d in q.group_by if d != "metric_time"]
        filters = list(q.filters) + list(ref.filters)
        time_dim = L.time_dimension(model).qualified
        steps = self._joins(model, dims + [f.dimension for f in filters] + [time_dim])
        t = self._time_expr(model)
        if q.time_range:
            bounds = (f"SELECT DATE {lit(q.time_range[0])} AS lo, DATE {lit(q.time_range[1])} - INTERVAL 1 DAY AS hi")
        else:
            bounds = f"SELECT min({t}) AS lo, max({t}) AS hi\n      {self._from(model, self._joins(model, [time_dim]))}"
        series = (f"generate_series(CAST(date_trunc('{g}', lo) AS TIMESTAMP), CAST(hi AS TIMESTAMP), "
                  f"INTERVAL 1 {g.upper()})")
        spine = (f"SELECT CAST(t AS DATE) AS period FROM (\n      SELECT unnest({series}) AS t\n"
                 f"      FROM ({bounds}) AS b\n    ) AS s")
        on = [f"{t} < spine.period + INTERVAL 1 {g.upper()}"]
        if window_days:
            on.append(f"{t} >= spine.period + INTERVAL 1 {g.upper()} - INTERVAL {int(window_days)} DAY")
        if q.time_range:
            on.append(f"{t} < TIMESTAMP {lit(q.time_range[1])}")
        select = [f"spine.period AS metric_time__{g}"] + [f"{self._dim_expr(d)} AS {d}" for d in dims]
        sql = ("SELECT\n      " + ",\n      ".join(select + [f"{self._agg(ref.measure)} AS {ref.measure}"])
               + "\n    " + self._from(model, steps)
               + f"\n    JOIN {name}_spine AS spine ON " + "\n      AND ".join(on))
        where = [self._filter_sql(f) for f in filters]
        if where:
            sql += "\n    WHERE " + "\n      AND ".join(where)
        sql += "\n    GROUP BY " + ", ".join(str(i) for i in range(1, len(select) + 1))
        joins = [{"from": s.from_model, "to": s.to_model, "on": f"{s.from_model}.{s.from_expr} = "
                  f"{s.to_model}.{s.to_expr}", "cardinality": s.cardinality, "join": "LEFT JOIN"} for s in steps]
        cte = CTE(name, "cumulative", model, L.models[model].table, L.models[model].table, [ref.measure],
                  [f.to_dict() for f in filters], joins, sql)
        return cte, spine

    # ---------- whole query ----------
    def compile(self, q: MetricQuery) -> Compiled:
        q = self.validate(q)
        L = self.layer
        dims = [f"metric_time__{q.grain}" if d == "metric_time" else d for d in q.group_by]
        groups: dict[tuple, list[MeasureRef]] = {}
        cumulative: dict[tuple, MeasureRef] = {}
        for name in q.metrics:
            for leaf in L.leaf_metrics(name):
                for ref in leaf.measure_refs():
                    model = L.measures[ref.measure].model
                    if leaf.type == "cumulative":
                        cumulative.setdefault((model, ref, leaf.window_days), ref)
                    else:
                        refs = groups.setdefault((model, ref.filters), [])
                        if ref not in refs:
                            refs.append(ref)
        ctes: list[CTE] = []
        spines: dict[str, str] = {}
        col_of: dict[tuple, str] = {}  # (kind, measure ref, window) -> "cte.column"
        used_names: dict[str, int] = {}

        def cte_name(base: str) -> str:
            used_names[base] = used_names.get(base, 0) + 1
            return base if used_names[base] == 1 else f"{base}_{used_names[base]}"

        for (model, filters), refs in groups.items():
            cte = self.aggregate_cte(cte_name(f"agg_{model}"), model, refs, q, filters)
            ctes.append(cte)
            for r in refs:
                col_of[("agg", r, None)] = f"{cte.name}.{r.measure}"
        for (model, ref, window), _ in cumulative.items():
            cte, spine = self.cumulative_cte(cte_name(f"cum_{model}"), model, ref, window, q)
            ctes.append(cte)
            spines[cte.name] = spine
            col_of[("cum", ref, window)] = f"{cte.name}.{ref.measure}"

        def zero_fill(ref: MeasureRef, col: str) -> str:
            # A key missing from one CTE means no rows there: zero for counts and sums, unknown for averages.
            return f"COALESCE({col}, 0)" if L.measures[ref.measure].agg in COUNTABLE and len(ctes) > 1 else col

        def leaf_expr(m: Metric) -> str:
            ref = m.measure_refs()[0]
            if m.type == "simple":
                return zero_fill(ref, col_of[("agg", ref, None)])
            if m.type == "cumulative":
                return zero_fill(ref, col_of[("cum", ref, m.window_days)])
            num, den = col_of[("agg", m.numerator, None)], col_of[("agg", m.denominator, None)]
            return f"CAST(COALESCE({num}, 0) AS DOUBLE) / NULLIF({den}, 0)"

        def metric_expr(name: str) -> str:
            m = L.metrics[name]
            if m.type != "derived":
                return leaf_expr(m)
            toks = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|[-+*/()]", m.expr)
            out, prev = [], ""
            for tok in toks:
                if tok in L.metrics:
                    inner = metric_expr(tok)
                    inner = inner if inner.startswith("COALESCE(") else f"COALESCE({inner}, 0)"
                    out.append(f"NULLIF({inner}, 0)" if prev == "/" else inner)
                else:
                    out.append(tok)
                prev = tok
            body = " ".join(out)
            return f"CAST({body} AS DOUBLE)" if "/" in toks else f"({body})"

        withs = []
        for c in ctes:
            if c.name in spines:
                withs.append(f"{c.name}_spine AS (\n    {spines[c.name]}\n  )")
            withs.append(f"{c.name} AS (\n    {c.sql}\n  )")
        exprs = [f"{metric_expr(n)} AS {n}" for n in q.metrics]
        if len(ctes) == 1:
            c = ctes[0].name
            select = [f"{c}.{d}" for d in dims] + exprs
            body = "SELECT\n  " + ",\n  ".join(select) + f"\nFROM {c}"
        elif not dims:
            body = "SELECT\n  " + ",\n  ".join(exprs) + "\nFROM " + "\nCROSS JOIN ".join(c.name for c in ctes)
        else:
            keys = "\n    UNION ".join(f"SELECT {', '.join(dims)} FROM {c.name}" for c in ctes)
            withs.append(f"keys AS (\n    {keys}\n  )")
            select = [f"keys.{d}" for d in dims] + exprs
            body = "SELECT\n  " + ",\n  ".join(select) + "\nFROM keys"
            for c in ctes:
                on = " AND ".join(f"keys.{d} IS NOT DISTINCT FROM {c.name}.{d}" for d in dims)
                body += f"\nLEFT JOIN {c.name} ON {on}"
        if dims:
            body += "\nORDER BY " + ", ".join(str(i) for i in range(1, len(dims) + 1))
        body += f"\nLIMIT {int(q.limit)}"
        sql = "WITH\n  " + ",\n  ".join(withs) + "\n" + body
        used = sorted({c.source for c in ctes if c.source.startswith("preagg_")})
        return Compiled(sql, dims + list(q.metrics), dims, list(q.metrics), ctes, q, used)

    def preagg_sql(self, pa: PreAggregation) -> str:
        q = MetricQuery(metrics=[], group_by=["metric_time", *pa.dimensions], grain=pa.grain)
        saved, self.preaggs = self.preaggs, []
        try:
            cte = self.aggregate_cte(pa.table, pa.model, [MeasureRef(m) for m in pa.measures], q, ())
        finally:
            self.preaggs = saved
        return cte.sql


def is_additive(layer: SemanticLayer, measure: str) -> bool:
    return layer.measures[measure].agg in ADDITIVE
