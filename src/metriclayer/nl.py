"""Natural language to governed metric queries.

A question never becomes free-form SQL. Both interpreters produce a MetricQuery that names catalog metrics and
dimensions only; it is validated against the semantic model and compiled like any other query. When the question
is ambiguous or asks for something the model cannot answer, the interpreter returns a short clarification instead.

- OfflineMatcher (default): deterministic keyword and synonym matching. No network.
- ClaudeInterpreter: Claude with structured output whose JSON schema enumerates the catalog, so the model cannot
  name a metric or dimension that does not exist. The result still goes through the same validation.
"""

from __future__ import annotations

import calendar
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .compiler import Compiler, MetricQuery, QueryError
from .model import GRAINS, OPS, Filter, SemanticLayer

MODEL = os.environ.get("METRICLAYER_MODEL", "claude-opus-5-5")
ANCHOR = date(2026, 7, 1)  # "today" for relative ranges: the day after the last day of data
MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
GRAIN_WORDS = [
    ("day", ["daily", "per day", "by day", "each day", "day by day", "every day"]),
    ("week", ["weekly", "per week", "by week", "each week", "week over week", "wow", "every week"]),
    ("month", ["monthly", "per month", "by month", "each month", "month over month", "mom", "every month"]),
    ("quarter", ["quarterly", "per quarter", "by quarter", "each quarter", "quarter over quarter", "qoq"]),
    ("year", ["yearly", "annual", "annually", "per year", "by year", "each year", "year over year", "yoy"]),
]
TREND_WORDS = ["trend", "over time", "time series", "trending"]
NEGATIONS = {"excluding", "except", "not", "without", "exclude", "non", "outside", "minus"}
FILLER = ["show us", "give us", "tell us", "let us", "for us"]


@dataclass
class Interpretation:
    status: str  # ok | clarify
    query: MetricQuery | None = None
    clarification: str | None = None
    notes: list[str] = field(default_factory=list)
    provider: str = "offline"

    def to_dict(self) -> dict:
        return {"status": self.status, "query": self.query.to_dict() if self.query else None,
                "clarification": self.clarification, "notes": self.notes, "provider": self.provider}


def normalize(text: str) -> str:
    t = text.lower().replace("’", "'")
    t = re.sub(r"(\d+)-(day|week|month)", r"\1 \2", t)
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    t = t.replace("'s", "")
    for f in FILLER:
        t = re.sub(rf"\b{f}\b", " ", t)
    return " " + re.sub(r"\s+", " ", t).strip() + " "


def _month_end(y: int, m: int) -> date:
    return date(y + (m == 12), m % 12 + 1, 1)


def _shift_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, 1)


def parse_time_range(t: str, anchor: date = ANCHOR) -> tuple[tuple[str, str] | None, list[tuple[int, int]], str]:
    """Return ((start, end) or None, consumed spans, note). `t` is normalized text."""

    def out(start: date, end: date, m: re.Match, note: str):
        return (start.isoformat(), end.isoformat()), [m.span()], note

    def year_of(month: int, y: str | None) -> int:
        if y:
            return int(y)
        return anchor.year if month < anchor.month else anchor.year - 1

    mon = "|".join(sorted(MONTHS, key=len, reverse=True))
    m = re.search(rf" between ({mon}) (\d{{4}} )?and ({mon}) (\d{{4}}) ", t)
    if m:
        y2 = int(m.group(4))
        y1 = int(m.group(2)) if m.group(2) else y2
        return out(date(y1, MONTHS[m.group(1)], 1), _month_end(y2, MONTHS[m.group(3)]), m, "between months")
    m = re.search(r" (?:last|past|previous) (\d+) (day|days|week|weeks|month|months) ", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit.startswith("day"):
            start = anchor - timedelta(days=n)
        elif unit.startswith("week"):
            start = anchor - timedelta(days=7 * n)
        else:
            start = _shift_months(anchor, -n)
        return out(start, anchor, m, f"last {n} {unit}")
    m = re.search(r" (?:last|past|previous) (month|quarter|year|week) ", t)
    if m:
        unit = m.group(1)
        if unit == "month":
            return out(_shift_months(anchor, -1), anchor, m, "last month")
        if unit == "quarter":
            q_start = date(anchor.year, 3 * ((anchor.month - 1) // 3) + 1, 1)
            return out(_shift_months(q_start, -3), q_start, m, "last quarter")
        if unit == "year":
            return out(date(anchor.year - 1, 1, 1), date(anchor.year, 1, 1), m, "last year")
        monday = anchor - timedelta(days=anchor.weekday())
        return out(monday - timedelta(days=7), monday, m, "last week")
    m = re.search(r" (?:this year|year to date|ytd) ", t)
    if m:
        return out(date(anchor.year, 1, 1), anchor, m, "year to date")
    m = re.search(r" (?:q([1-4])|(first|second|third|fourth) quarter)(?: of)? (\d{4}) ", t)
    if m:
        q = int(m.group(1)) if m.group(1) else ["first", "second", "third", "fourth"].index(m.group(2)) + 1
        y = int(m.group(3))
        start = date(y, 3 * q - 2, 1)
        return out(start, _shift_months(start, 3), m, f"Q{q} {y}")
    m = re.search(r" (?:h([12])|(first|second) half)(?: of)? (\d{4}) ", t)
    if m:
        h = int(m.group(1)) if m.group(1) else (1 if m.group(2) == "first" else 2)
        y = int(m.group(3))
        start = date(y, 1 if h == 1 else 7, 1)
        return out(start, _shift_months(start, 6), m, f"H{h} {y}")
    m = re.search(rf" since ({mon})(?: (\d{{4}}))? ", t)
    if m:
        mo = MONTHS[m.group(1)]
        return out(date(year_of(mo, m.group(2)), mo, 1), anchor, m, "since month")
    m = re.search(rf" ({mon}) (\d{{4}}) ", t) or re.search(rf" (?:in|during|for) ({mon})() ", t)
    if m:
        mo = MONTHS[m.group(1)]
        y = year_of(mo, m.group(2) or None)
        return out(date(y, mo, 1), _month_end(y, mo), m, "month")
    m = re.search(r" (?:in |during |for |of )?(20\d\d) ", t)
    if m:
        y = int(m.group(1))
        return out(date(y, 1, 1), date(y + 1, 1, 1), m, f"year {y}")
    return None, [], ""


class OfflineMatcher:
    name = "offline"

    def __init__(self, layer: SemanticLayer, values: dict[str, list[str]], anchor: date = ANCHOR):
        self.layer, self.anchor, self.values = layer, anchor, values
        self.compiler = Compiler(layer)
        self.metric_phrases: list[tuple[str, str]] = []
        for m in layer.metrics.values():
            for p in {m.name.replace("_", " "), m.label.lower(), *[s.lower() for s in m.synonyms]}:
                self.metric_phrases.append((p, m.name))
        self.metric_phrases.sort(key=lambda x: -len(x[0]))
        self.dim_phrases: dict[str, list[str]] = {}
        for q, d in layer.dimensions.items():
            if d.type != "categorical":
                continue
            for p in {d.name.replace("_", " "), *[s.lower() for s in d.synonyms]}:
                self.dim_phrases.setdefault(p, []).append(q)
        self.value_phrases: dict[str, list[tuple[str, str]]] = {}
        syn = {v: [s.lower() for s in ss] for v, ss in layer.value_synonyms.items()}
        for dim, vals in values.items():
            for v in vals:
                phrases = set(syn.get(v, []))
                raw = v.lower().replace("_", " ")
                if len(raw) > 2:
                    phrases.add(raw)
                for p in phrases:
                    self.value_phrases.setdefault(p, []).append((dim, v))

    @staticmethod
    def _find(text: str, phrase: str, taken: list[tuple[int, int]]):
        for m in re.finditer(rf"(?<= ){re.escape(phrase)}(?= )", text):
            s, e = m.span()
            if not any(s < b and a < e for a, b in taken):
                return s, e
        return None

    def _models(self, metrics: list[str]) -> list[str]:
        out: list[str] = []
        for m in metrics:
            out += [x for x in self.layer.metric_models(m) if x not in out]
        return out

    def _pick_dim(self, cands: list[str], metrics: list[str]) -> tuple[list[str], str]:
        """Narrow candidate dimensions by reachability, then by the metric's own model."""
        models = self._models(metrics)
        reach = [d for d in cands if all(self.layer.join_path(m, self.layer.dimensions[d].model) is not None
                                         for m in models)]
        if len(reach) == 1:
            return reach, ""
        if not reach:
            return cands[:1] if len(cands) == 1 else [], "unreachable"
        own = [d for d in reach if self.layer.dimensions[d].model in models]
        if len(own) == 1:
            return own, ""
        return reach, "ambiguous"

    def interpret(self, question: str) -> Interpretation:
        t = normalize(question)
        notes: list[str] = []
        time_range, taken, tnote = parse_time_range(t, self.anchor)
        if time_range:
            notes.append(f"time range {time_range[0]} to {time_range[1]} ({tnote})")
        # One pass over every phrase, longest first, so "business buyers" beats "buyers" and
        # "traffic source" beats "traffic". Ties go to metrics, then dimensions, then values.
        phrases = ([(p, 0, "metric", m) for p, m in self.metric_phrases]
                   + [(p, 1, "dim", p) for p in self.dim_phrases]
                   + [(p, 2, "value", p) for p in self.value_phrases])
        phrases.sort(key=lambda x: (-len(x[0]), x[1]))
        metrics: list[str] = []
        dim_hits: list[str] = []
        value_hits: list[tuple[str, tuple[int, int]]] = []
        for phrase, _, kind, payload in phrases:
            span = self._find(t, phrase, taken)
            if not span:
                continue
            taken.append(span)
            if kind == "metric":
                if payload not in metrics:
                    metrics.append(payload)
                    notes.append(f"'{phrase}' -> metric {payload}")
            elif kind == "dim":
                dim_hits.append(payload)
            else:
                value_hits.append((payload, span))
        for term, options in self.layer.ambiguous_terms.items():
            if self._find(t, term, taken) and not any(o in metrics for o in options):
                labels = " or ".join(self.layer.metrics[o].label.lower() for o in options)
                return Interpretation("clarify", clarification=f"'{term}' could mean {labels}. Which one?",
                                      notes=notes)
        if not metrics:
            sample = ", ".join(sorted(self.layer.metrics)[:6])
            return Interpretation("clarify", clarification=f"I could not match a metric in the catalog. Try one of: "
                                  f"{sample}, ...", notes=notes)
        grain = None
        for g, words in GRAIN_WORDS:
            for w in words:
                span = self._find(t, w, taken)
                if span and grain is None:
                    grain = g
                    taken.append(span)
                    notes.append(f"'{w}' -> grain {g}")
        if grain is None and any(self._find(t, w, taken) for w in TREND_WORDS):
            grain = "month"
            notes.append("trend -> grain month")
        if grain is None and any(self.layer.metrics[m].type == "cumulative" for m in metrics):
            grain = "month"
            notes.append("cumulative metric -> grain month")
        found: dict[tuple[str, str], list[str]] = {}
        for phrase, span in sorted(value_hits, key=lambda x: x[1]):
            cands = self.value_phrases[phrase]
            dims, why = self._pick_dim([d for d, _ in cands], metrics)
            if why == "ambiguous":
                opts = " or ".join(sorted(dims))
                return Interpretation("clarify", clarification=f"'{phrase}' could be a value of {opts}. Which one?",
                                      notes=notes)
            if not dims:
                return Interpretation("clarify", clarification=f"'{phrase}' is not available for "
                                      f"{', '.join(metrics)}.", notes=notes)
            dim = dims[0]
            value = next(v for d, v in cands if d == dim)
            before = t[: span[0]].split()[-3:]
            op = "!=" if NEGATIONS & set(before) else "="
            found.setdefault((dim, op), []).append(value)
            notes.append(f"'{phrase}' -> filter {dim} {op} {value}")
        compare = bool(re.search(r" (?:vs|versus|compared to|compared with) ", t))
        filters, group_by = [], []
        for (dim, op), vals in found.items():
            vals = sorted(set(vals))
            if len(vals) == 1:
                filters.append(Filter(dim, op, vals[0]))
            else:
                filters.append(Filter(dim, "in" if op == "=" else "not_in", tuple(vals)))
                if compare and op == "=":
                    group_by.append(dim)
                    notes.append(f"'vs' -> group by {dim}")
        for phrase in dim_hits:
            dims, why = self._pick_dim(self.dim_phrases[phrase], metrics)
            if why == "ambiguous":
                return Interpretation("clarify", clarification=f"'{phrase}' could mean {' or '.join(sorted(dims))}. "
                                      "Which one?", notes=notes)
            if not dims:
                return Interpretation("clarify", clarification=f"{', '.join(metrics)} cannot be broken down by "
                                      f"'{phrase}'.", notes=notes)
            if dims[0] not in group_by:
                group_by.append(dims[0])
                notes.append(f"'{phrase}' -> group by {dims[0]}")
        q = MetricQuery(metrics, group_by, grain, filters, time_range)
        try:
            q = self.compiler.validate(q)
        except QueryError as e:
            return Interpretation("clarify", clarification=e.message, notes=notes)
        return Interpretation("ok", q, notes=notes)


SYSTEM = """You map analytics questions about Larkspur Market, an online marketplace, to a governed metric query.
Use only the metrics, dimensions, and values in the catalog. Never write SQL.
Rules:
- status "clarify" with a one-sentence question when the question is ambiguous (for example "revenue" could be
  gross or net), names something not in the catalog, or asks for a dimension the metric cannot be broken down by.
  Otherwise status "ok" and an empty clarification.
- group_by lists categorical dimensions. Put "metric_time" in group_by and set grain when the question asks for a
  trend or a time grain; otherwise grain "none".
- time_start and time_end are YYYY-MM-DD, end exclusive, or empty strings for all time. Today is {anchor}; data
  covers 2025-01-01 to {last}.
- filters use stored values from the catalog (for example country "US", not "United States").
"""


def output_schema(layer: SemanticLayer) -> dict:
    dims = sorted(q for q, d in layer.dimensions.items() if d.type == "categorical")
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["ok", "clarify"]},
            "clarification": {"type": "string"},
            "metrics": {"type": "array", "items": {"type": "string", "enum": sorted(layer.metrics)}},
            "group_by": {"type": "array", "items": {"type": "string", "enum": ["metric_time", *dims]}},
            "grain": {"type": "string", "enum": ["none", *GRAINS]},
            "filters": {"type": "array", "items": {
                "type": "object",
                "properties": {"dimension": {"type": "string", "enum": dims},
                               "op": {"type": "string", "enum": sorted(OPS)},
                               "values": {"type": "array", "items": {"type": "string"}}},
                "required": ["dimension", "op", "values"], "additionalProperties": False}},
            "time_start": {"type": "string"},
            "time_end": {"type": "string"},
        },
        "required": ["status", "clarification", "metrics", "group_by", "grain", "filters", "time_start", "time_end"],
        "additionalProperties": False,
    }


def catalog_context(layer: SemanticLayer, values: dict[str, list[str]]) -> str:
    metrics = [{"name": m.name, "type": m.type, "description": m.description, "synonyms": m.synonyms,
                "dimensions": layer.dimensions_for(m.name)} for m in layer.metrics.values()]
    dims = [{"name": q, "description": d.description, "values": values.get(q, [])}
            for q, d in layer.dimensions.items() if d.type == "categorical"]
    return json.dumps({"metrics": metrics, "dimensions": dims, "grains": GRAINS}, indent=1)


class ClaudeInterpreter:
    name = "claude"

    def __init__(self, layer: SemanticLayer, values: dict[str, list[str]], client=None, model: str = MODEL,
                 anchor: date = ANCHOR):
        if client is None:  # pragma: no cover - needs the optional extra and a key
            try:
                import anthropic  # lazy: the app runs without the optional extra
            except ImportError as exc:
                raise RuntimeError("Install the Claude extra first: uv sync --extra claude") from exc
            client = anthropic.Anthropic()
        self.layer, self.values, self.client, self.model, self.anchor = layer, values, client, model, anchor
        self.compiler = Compiler(layer)
        self.schema = output_schema(layer)
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    def _call(self, question: str) -> dict:
        system = SYSTEM.format(anchor=self.anchor.isoformat(), last=(self.anchor - timedelta(days=1)).isoformat())
        prompt = f"Catalog:\n{catalog_context(self.layer, self.values)}\n\nQuestion: {question}"
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": self.schema}},
        )
        u = getattr(resp, "usage", None)
        self.usage["calls"] += 1
        self.usage["input_tokens"] += getattr(u, "input_tokens", 0) or 0
        self.usage["output_tokens"] += getattr(u, "output_tokens", 0) or 0
        if getattr(resp, "stop_reason", None) == "refusal":
            raise ValueError("the model declined the request")
        body = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return json.loads(body)

    def interpret(self, question: str) -> Interpretation:
        try:
            out = self._call(question)
        except (ValueError, json.JSONDecodeError) as e:
            return Interpretation("clarify", clarification=f"I could not interpret that ({e}). Please rephrase.",
                                  provider=self.name)
        return self.from_output(out)

    def from_output(self, out: dict) -> Interpretation:
        """Validate the model's JSON against the semantic model. Nothing unvalidated reaches the compiler."""
        if not isinstance(out, dict) or out.get("status") not in ("ok", "clarify"):
            return Interpretation("clarify", clarification="The interpreter returned an invalid answer.",
                                  provider=self.name)
        if out["status"] == "clarify":
            return Interpretation("clarify", clarification=out.get("clarification") or "Can you be more specific?",
                                  provider=self.name)
        filters = []
        for f in out.get("filters") or []:
            vals = [str(v) for v in f.get("values") or []]
            op = f.get("op", "=")
            if op in ("=", "!=") and len(vals) == 1:
                filters.append({"dimension": f.get("dimension"), "op": op, "value": vals[0]})
            elif op in ("=", "!="):
                filters.append({"dimension": f.get("dimension"), "op": "in" if op == "=" else "not_in",
                                "value": vals})
            else:
                filters.append({"dimension": f.get("dimension"), "op": op, "value": vals})
        tr = (out.get("time_start"), out.get("time_end")) if out.get("time_start") and out.get("time_end") else None
        grain = out.get("grain") if out.get("grain") not in (None, "none") else None
        try:
            q = MetricQuery.from_dict({"metrics": out.get("metrics") or [], "group_by": out.get("group_by") or [],
                                       "grain": grain, "filters": filters, "time_range": tr})
            q = self.compiler.validate(q)
        except QueryError as e:
            return Interpretation("clarify", clarification=f"That query is not valid for the model: {e.message}",
                                  provider=self.name)
        return Interpretation("ok", q, notes=["interpreted by Claude, validated against the semantic model"],
                              provider=self.name)


def categorical_values(engine) -> dict[str, list[str]]:
    """Distinct values for each categorical dimension with at most 30 values."""
    out = {}
    for q, d in engine.layer.dimensions.items():
        if d.type != "categorical":
            continue
        table = engine.layer.models[d.model].table
        rows = engine.sql(f"SELECT DISTINCT {d.expr} FROM {table} WHERE {d.expr} IS NOT NULL ORDER BY 1 LIMIT 31")
        if len(rows) <= 30:
            out[q] = [str(r[0]) for r in rows]
    return out


def make_interpreter(engine, kind: str | None = None, client=None):
    kind = kind or os.environ.get("METRICLAYER_NL", "offline")
    values = categorical_values(engine)
    if kind == "claude":
        return ClaudeInterpreter(engine.layer, values, client=client)
    return OfflineMatcher(engine.layer, values)
