"""Semantic model: load the YAML, validate it, and resolve join paths.

Dimension names are `<entity>__<dimension>` (for example `customer__country`), where the entity is the primary
entity of the semantic model that owns the dimension. Models without a primary entity use the model name.
`metric_time` is the shared time dimension: each measure maps it to its own agg_time_dimension.
"""

from __future__ import annotations

import os
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import yaml

AGGS = {"sum", "count", "count_distinct", "average", "min", "max"}
ADDITIVE = {"sum", "count"}
METRIC_TYPES = {"simple", "ratio", "derived", "cumulative"}
GRAINS = ["day", "week", "month", "quarter", "year"]
OPS = {"=", "!=", "in", "not_in"}
IDENT = re.compile(r"^[a-z][a-z0-9_]*$")
HOME = Path(os.environ.get("METRICLAYER_HOME") or Path(__file__).resolve().parents[2])
DEFAULT_PATH = HOME / "semantic" / "marketplace.yml"


class ModelError(ValueError):
    """The YAML does not describe a valid semantic model. `problems` lists every issue found."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Filter:
    dimension: str
    op: str
    value: str | tuple[str, ...]

    @classmethod
    def parse(cls, d: dict) -> Filter:
        value = d.get("value")
        if isinstance(value, list):
            value = tuple(str(v) for v in value)
        elif value is not None:
            value = str(value)
        return cls(str(d.get("dimension", "")), str(d.get("op", "=")), value)

    def to_dict(self) -> dict:
        v = list(self.value) if isinstance(self.value, tuple) else self.value
        return {"dimension": self.dimension, "op": self.op, "value": v}


@dataclass
class Entity:
    name: str
    type: str  # primary | foreign
    expr: str


@dataclass
class Dimension:
    name: str
    type: str  # categorical | time
    expr: str
    model: str
    qualified: str
    description: str = ""
    synonyms: list[str] = field(default_factory=list)


@dataclass
class Measure:
    name: str
    agg: str
    expr: str
    model: str
    description: str = ""


@dataclass
class SemanticModel:
    name: str
    table: str
    description: str
    owner: str
    entities: list[Entity]
    dimensions: list[Dimension]
    measures: list[Measure]
    agg_time_dimension: str | None

    @property
    def primary(self) -> str | None:
        return next((e.name for e in self.entities if e.type == "primary"), None)

    @property
    def prefix(self) -> str:
        return self.primary or self.name


@dataclass(frozen=True)
class MeasureRef:
    measure: str
    filters: tuple[Filter, ...] = ()


@dataclass
class Metric:
    name: str
    type: str
    label: str
    description: str
    owner: str
    format: str = "number"
    measure: str | None = None
    filters: tuple[Filter, ...] = ()
    numerator: MeasureRef | None = None
    denominator: MeasureRef | None = None
    expr: str | None = None
    inputs: list[str] = field(default_factory=list)
    window_days: int | None = None
    synonyms: list[str] = field(default_factory=list)

    def measure_refs(self) -> list[MeasureRef]:
        if self.type in ("simple", "cumulative"):
            return [MeasureRef(self.measure, self.filters)]
        if self.type == "ratio":
            return [self.numerator, self.denominator]
        return []


@dataclass(frozen=True)
class JoinStep:
    """One many_to_one join: from_model.foreign key = to_model.primary key."""

    from_model: str
    to_model: str
    entity: str
    from_expr: str
    to_expr: str
    cardinality: str = "many_to_one"


@dataclass
class PreAggregation:
    name: str
    model: str
    measures: list[str]
    dimensions: list[str]
    grain: str

    @property
    def table(self) -> str:
        return f"preagg_{self.name}"


def _filters(raw, where: str, problems: list[str]) -> tuple[Filter, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        problems.append(f"{where}: filter must be a list")
        return ()
    out = []
    for f in raw:
        if not isinstance(f, dict) or "dimension" not in f or "value" not in f:
            problems.append(f"{where}: each filter needs dimension, op, value")
            continue
        out.append(Filter.parse(f))
    return tuple(out)


def _window(raw, where: str, problems: list[str]) -> int | None:
    if raw is None:
        return None
    m = re.fullmatch(r"\s*(\d+)\s*(day|days|week|weeks)\s*", str(raw))
    if not m:
        problems.append(f"{where}: window must look like '28 days' or '4 weeks', got {raw!r}")
        return None
    n = int(m.group(1))
    return n * 7 if m.group(2).startswith("week") else n


class SemanticLayer:
    """The validated model plus the join graph."""

    def __init__(self, raw: dict):
        problems: list[str] = []
        if not isinstance(raw, dict):
            raise ModelError(["model file must be a mapping"])
        self.name = raw.get("name", "model")
        self.description = raw.get("description", "")
        self.models: dict[str, SemanticModel] = {}
        self.dimensions: dict[str, Dimension] = {}
        self.measures: dict[str, Measure] = {}
        self.metrics: dict[str, Metric] = {}
        self.primary_of: dict[str, str] = {}
        self.ambiguous_terms: dict[str, list[str]] = raw.get("ambiguous_terms") or {}
        self.value_synonyms: dict[str, list[str]] = raw.get("value_synonyms") or {}

        for sm in raw.get("semantic_models") or []:
            self._add_model(sm, problems)
        if not self.models:
            problems.append("no semantic_models defined")
        for m in self.models.values():
            for e in m.entities:
                if e.type == "foreign" and e.name not in self.primary_of:
                    problems.append(f"model {m.name}: foreign entity {e.name!r} has no model where it is primary")
            if m.agg_time_dimension:
                d = self._resolve_time_dim(m)
                if d is None:
                    problems.append(f"model {m.name}: agg_time_dimension {m.agg_time_dimension!r} is not a time "
                                    "dimension reachable from this model")
        for mt in raw.get("metrics") or []:
            self._add_metric(mt, problems)
        self._check_metrics(problems)
        self.preaggregations = [self._preagg(p, problems) for p in raw.get("preaggregations") or []]
        if problems:
            raise ModelError(problems)

    # ---------- loading ----------
    def _add_model(self, sm: dict, problems: list[str]) -> None:
        name = sm.get("name", "")
        if not IDENT.match(str(name)):
            problems.append(f"semantic model name {name!r} is not a lowercase identifier")
            return
        if name in self.models:
            problems.append(f"duplicate semantic model {name!r}")
            return
        for key in ("table", "owner", "description"):
            if not sm.get(key):
                problems.append(f"model {name}: missing {key}")
        entities = []
        for e in sm.get("entities") or []:
            if e.get("type") not in ("primary", "foreign"):
                problems.append(f"model {name}: entity {e.get('name')!r} type must be primary or foreign")
                continue
            entities.append(Entity(e["name"], e["type"], e.get("expr", e["name"])))
        prim = [e for e in entities if e.type == "primary"]
        if len(prim) > 1:
            problems.append(f"model {name}: more than one primary entity")
        model = SemanticModel(name, sm.get("table", name), sm.get("description", ""), sm.get("owner", ""), entities,
                              [], [], sm.get("agg_time_dimension"))
        if prim:
            if prim[0].name in self.primary_of:
                problems.append(f"entity {prim[0].name!r} is primary in both {self.primary_of[prim[0].name]} and "
                                f"{name}")
            self.primary_of[prim[0].name] = name
        for d in sm.get("dimensions") or []:
            if d.get("type") not in ("categorical", "time"):
                problems.append(f"model {name}: dimension {d.get('name')!r} type must be categorical or time")
                continue
            q = f"{model.prefix}__{d['name']}"
            if q in self.dimensions:
                problems.append(f"duplicate dimension {q}")
                continue
            dim = Dimension(d["name"], d["type"], d.get("expr", d["name"]), name, q, d.get("description", ""),
                            list(d.get("synonyms") or []))
            model.dimensions.append(dim)
            self.dimensions[q] = dim
        for m in sm.get("measures") or []:
            if m.get("agg") not in AGGS:
                problems.append(f"model {name}: measure {m.get('name')!r} has unknown agg {m.get('agg')!r} "
                                f"(use one of {sorted(AGGS)})")
                continue
            if m["name"] in self.measures:
                problems.append(f"duplicate measure {m['name']!r}")
                continue
            meas = Measure(m["name"], m["agg"], str(m.get("expr", m["name"])), name, m.get("description", ""))
            model.measures.append(meas)
            self.measures[meas.name] = meas
        self.models[name] = model

    def _measure_ref(self, raw, where: str, problems: list[str]) -> MeasureRef | None:
        if not isinstance(raw, dict) or "measure" not in raw:
            problems.append(f"{where}: needs a measure")
            return None
        return MeasureRef(raw["measure"], _filters(raw.get("filter"), where, problems))

    def _add_metric(self, mt: dict, problems: list[str]) -> None:
        name = mt.get("name", "")
        where = f"metric {name}"
        if not IDENT.match(str(name)):
            problems.append(f"metric name {name!r} is not a lowercase identifier")
            return
        if name in self.metrics:
            problems.append(f"duplicate metric {name!r}")
            return
        typ = mt.get("type")
        if typ not in METRIC_TYPES:
            problems.append(f"{where}: type must be one of {sorted(METRIC_TYPES)}, got {typ!r}")
            return
        for key in ("description", "owner"):
            if not mt.get(key):
                problems.append(f"{where}: missing {key}")
        metric = Metric(name, typ, mt.get("label", name), mt.get("description", ""), mt.get("owner", ""),
                        mt.get("format", "number"), synonyms=list(mt.get("synonyms") or []))
        if typ in ("simple", "cumulative"):
            metric.measure = mt.get("measure")
            metric.filters = _filters(mt.get("filter"), where, problems)
            if typ == "cumulative":
                metric.window_days = _window(mt.get("window"), where, problems)
        elif typ == "ratio":
            metric.numerator = self._measure_ref(mt.get("numerator"), f"{where} numerator", problems)
            metric.denominator = self._measure_ref(mt.get("denominator"), f"{where} denominator", problems)
        else:
            metric.expr = str(mt.get("expr", ""))
            tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|[-+*/()]|\S", metric.expr)
            bad = [t for t in tokens if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|[-+*/()]", t)]
            if not metric.expr or bad:
                problems.append(f"{where}: expr may only use metric names, numbers, + - * / and parentheses")
            metric.inputs = [t for t in tokens if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t)]
        self.metrics[name] = metric

    def _check_metrics(self, problems: list[str]) -> None:
        for m in self.metrics.values():
            for ref in m.measure_refs():
                if ref is None:
                    continue
                if ref.measure not in self.measures:
                    problems.append(f"metric {m.name}: unknown measure {ref.measure!r}")
                    continue
                model = self.measures[ref.measure].model
                for f in ref.filters:
                    self._check_filter(f, model, f"metric {m.name}", problems)
                if m.type == "cumulative" and not self.models[model].agg_time_dimension:
                    problems.append(f"metric {m.name}: cumulative metrics need a measure with an agg_time_dimension")
            if m.type == "derived":
                for i in m.inputs:
                    if i not in self.metrics:
                        problems.append(f"metric {m.name}: expr references unknown metric {i!r}")
        for m in self.metrics.values():
            if m.type == "derived" and self._cycle(m.name, set()):
                problems.append(f"metric {m.name}: derived metrics form a cycle")

    def _check_filter(self, f: Filter, model: str, where: str, problems: list[str]) -> None:
        if f.op not in OPS:
            problems.append(f"{where}: filter op {f.op!r} not in {sorted(OPS)}")
        if f.dimension not in self.dimensions:
            problems.append(f"{where}: filter dimension {f.dimension!r} is unknown")
        elif self.join_path(model, self.dimensions[f.dimension].model) is None:
            problems.append(f"{where}: filter dimension {f.dimension} is not reachable from {model}")

    def _cycle(self, name: str, seen: set[str]) -> bool:
        if name in seen:
            return True
        m = self.metrics.get(name)
        if m is None or m.type != "derived":
            return False
        return any(self._cycle(i, seen | {name}) for i in m.inputs)

    def _preagg(self, p: dict, problems: list[str]) -> PreAggregation:
        pa = PreAggregation(p["name"], p["model"], list(p["measures"]), list(p["dimensions"]), p.get("grain", "day"))
        for meas in pa.measures:
            ms = self.measures.get(meas)
            if ms is None or ms.model != pa.model or ms.agg not in ADDITIVE:
                problems.append(f"preaggregation {pa.name}: {meas!r} must be an additive measure of {pa.model}")
        for d in pa.dimensions:
            if d not in self.dimensions or self.join_path(pa.model, self.dimensions[d].model) is None:
                problems.append(f"preaggregation {pa.name}: dimension {d!r} not reachable from {pa.model}")
        if pa.grain != "day":
            problems.append(f"preaggregation {pa.name}: only day grain is supported (coarser grains roll up from it)")
        return pa

    # ---------- graph ----------
    def edges_from(self, model: str) -> list[JoinStep]:
        out = []
        for e in self.models[model].entities:
            if e.type == "foreign" and e.name in self.primary_of:
                target = self.models[self.primary_of[e.name]]
                pk = next(x for x in target.entities if x.type == "primary")
                out.append(JoinStep(model, target.name, e.name, e.expr, pk.expr))
        return out

    def join_path(self, start: str, target: str) -> list[JoinStep] | None:
        """Shortest chain of many_to_one joins from start to target (BFS), or None if unreachable."""
        if start == target:
            return []
        seen, queue = {start}, deque([(start, [])])
        while queue:
            node, path = queue.popleft()
            for step in self.edges_from(node):
                if step.to_model in seen:
                    continue
                if step.to_model == target:
                    return path + [step]
                seen.add(step.to_model)
                queue.append((step.to_model, path + [step]))
        return None

    def reachable_models(self, start: str) -> set[str]:
        return {m for m in self.models if self.join_path(start, m) is not None}

    def _resolve_time_dim(self, model: SemanticModel) -> Dimension | None:
        name = model.agg_time_dimension
        if not name:
            return None
        dim = self.dimensions.get(name) or self.dimensions.get(f"{model.prefix}__{name}")
        if dim is None or dim.type != "time" or self.join_path(model.name, dim.model) is None:
            return None
        return dim

    def time_dimension(self, model: str) -> Dimension | None:
        return self._resolve_time_dim(self.models[model])

    # ---------- metric helpers ----------
    def leaf_metrics(self, name: str) -> list[Metric]:
        """Simple, ratio, and cumulative metrics a metric depends on (itself if not derived)."""
        m = self.metrics[name]
        if m.type != "derived":
            return [m]
        out: list[Metric] = []
        for i in m.inputs:
            for leaf in self.leaf_metrics(i):
                if leaf not in out:
                    out.append(leaf)
        return out

    def metric_models(self, name: str) -> list[str]:
        out: list[str] = []
        for leaf in self.leaf_metrics(name):
            for ref in leaf.measure_refs():
                mod = self.measures[ref.measure].model
                if mod not in out:
                    out.append(mod)
        return out

    def dimensions_for(self, metric: str) -> list[str]:
        """Categorical dimensions reachable from every model the metric reads."""
        models = self.metric_models(metric)
        out = []
        for q, d in self.dimensions.items():
            if d.type == "categorical" and all(self.join_path(m, d.model) is not None for m in models):
                out.append(q)
        return out

    def lineage(self, metric: str) -> dict:
        m = self.metrics[metric]
        measures, tables = [], []
        for leaf in self.leaf_metrics(metric):
            for ref in leaf.measure_refs():
                meas = self.measures[ref.measure]
                model = self.models[meas.model]
                measures.append({"measure": meas.name, "agg": meas.agg, "expr": meas.expr, "model": model.name,
                                 "table": model.table, "filters": [f.to_dict() for f in ref.filters]})
                if model.table not in tables:
                    tables.append(model.table)
                for f in ref.filters:
                    t = self.models[self.dimensions[f.dimension].model].table
                    if t not in tables:
                        tables.append(t)
        return {"metric": metric, "type": m.type, "depends_on": m.inputs if m.type == "derived" else [],
                "measures": measures, "tables": tables}


def load_model(path: Path | str | None = None) -> SemanticLayer:
    p = Path(path) if path else DEFAULT_PATH
    return SemanticLayer(yaml.safe_load(p.read_text()))
