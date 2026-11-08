"""Correctness suite (compiled vs hand-written SQL), naive join examples, and the natural language evaluation."""

from __future__ import annotations

import math
from pathlib import Path

import yaml

from .compiler import MetricQuery, QueryError
from .model import HOME as ROOT

REFERENCE = ROOT / "eval" / "reference_queries.yml"
NL_EVAL = ROOT / "eval" / "nl_eval.yml"


def _norm(v):
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, int | float) and not isinstance(v, bool):
        return round(float(v), 6)
    return v


def _key(row):
    return tuple((v is None, str(v)) for v in row)


def same_rows(a: list[list], b: list[list], tol: float = 1e-6) -> bool:
    """Equal as multisets of rows, with a relative tolerance on numbers."""
    if len(a) != len(b):
        return False
    for ra, rb in zip(sorted(a, key=_key), sorted(b, key=_key), strict=True):
        if len(ra) != len(rb):
            return False
        for x, y in zip(ra, rb, strict=True):
            x, y = _norm(x), _norm(y)
            if isinstance(x, float) and isinstance(y, float):
                if abs(x - y) > tol * max(1.0, abs(x), abs(y)):
                    return False
            elif x != y:
                return False
    return True


def _rows_sorted(rows):
    return sorted(rows, key=_key)


def run_reference(engine, path: Path = REFERENCE) -> list[dict]:
    cases = yaml.safe_load(Path(path).read_text())
    out = []
    for c in cases:
        ref = engine.sql(c["sql"])
        res = {"name": c["name"], "rows": len(ref)}
        for label, use_pre in (("compiled", True), ("compiled_raw", False)):
            r = engine.run(c["query"], use_cache=False, use_preaggs=use_pre)
            res[label] = same_rows(r.rows, ref)
            if use_pre:
                res["preagg"] = bool(r.preaggs_used)
        res["match"] = res["compiled"] and res["compiled_raw"]
        out.append(res)
    return out


NAIVE = [
    {
        "name": "Net revenue, all time",
        "why": ("orders JOIN order_items JOIN refunds: each refund repeats once per line item, "
                "each item once per refund"),
        "naive_sql": """SELECT SUM(oi.quantity * oi.unit_price - oi.discount_amount) - SUM(COALESCE(r.amount, 0))
FROM orders o
JOIN order_items oi ON oi.order_id = o.order_id
LEFT JOIN refunds r ON r.order_id = o.order_id
WHERE o.status <> 'cancelled'""",
        "query": {"metrics": ["net_revenue"]},
    },
    {
        "name": "Refunds issued, all time",
        "why": "same join, refund side only: a refund on a 4-item order is counted 4 times",
        "naive_sql": """SELECT SUM(r.amount)
FROM orders o
JOIN order_items oi ON oi.order_id = o.order_id
JOIN refunds r ON r.order_id = o.order_id""",
        "query": {"metrics": ["refund_amount"]},
    },
    {
        "name": "Average order value",
        "why": "AVG over joined item rows averages line items, not orders",
        "naive_sql": """SELECT AVG(oi.quantity * oi.unit_price - oi.discount_amount)
FROM orders o JOIN order_items oi ON oi.order_id = o.order_id
WHERE o.status <> 'cancelled'""",
        "query": {"metrics": ["aov"]},
    },
    {
        "name": "Sessions, all time",
        "why": "INNER JOIN to customers to get country drops anonymous sessions (customer_id is NULL)",
        "naive_sql": """SELECT COUNT(*) FROM sessions s JOIN customers c ON c.customer_id = s.customer_id""",
        "query": {"metrics": ["sessions"]},
    },
    {
        "name": "Conversion rate",
        "why": "average of per-customer conversion rates (an average of ratios) that also skips anonymous sessions",
        "naive_sql": """SELECT AVG(rate) FROM (
  SELECT customer_id, AVG(CASE WHEN converted THEN 1.0 ELSE 0 END) AS rate
  FROM sessions WHERE customer_id IS NOT NULL GROUP BY 1)""",
        "query": {"metrics": ["conversion_rate"]},
    },
]


def naive_examples(engine) -> list[dict]:
    out = []
    for n in NAIVE:
        naive = float(engine.sql(n["naive_sql"])[0][0])
        correct = float(engine.run(n["query"], use_cache=False).rows[0][0])
        out.append({"name": n["name"], "why": n["why"], "naive": naive, "correct": correct,
                    "error_pct": round(100 * (naive - correct) / correct, 2), "naive_sql": n["naive_sql"]})
    return out


def run_nl_eval(engine, interpreter, path: Path = NL_EVAL) -> dict:
    cases = yaml.safe_load(Path(path).read_text())
    rows = []
    for c in cases:
        interp = interpreter.interpret(c["question"])
        gold_clarify = bool(c.get("clarify"))
        row = {"question": c["question"], "gold_clarify": gold_clarify, "predicted": interp.status,
               "exact": False, "execution": False, "clarification": interp.clarification}
        if gold_clarify:
            row["exact"] = row["execution"] = interp.status == "clarify"
        elif interp.status == "ok":
            gold = engine.compiler.validate(MetricQuery.from_dict(c["query"]))
            row["exact"] = interp.query.key() == gold.key()
            try:
                row["execution"] = row["exact"] or same_rows(engine.run(interp.query).rows, engine.run(gold).rows)
            except QueryError:
                row["execution"] = False
            row["interpreted"] = interp.query.to_dict()
        rows.append(row)
    answerable = [r for r in rows if not r["gold_clarify"]]
    clar = [r for r in rows if r["gold_clarify"]]
    n = len(rows)
    return {
        "provider": interpreter.name,
        "questions": n,
        "answerable": len(answerable),
        "needs_clarification": len(clar),
        "exact_match": round(sum(r["exact"] for r in answerable) / max(1, len(answerable)), 3),
        "execution_match": round(sum(r["execution"] for r in answerable) / max(1, len(answerable)), 3),
        "clarification_recall": round(sum(r["predicted"] == "clarify" for r in clar) / max(1, len(clar)), 3),
        "false_clarifications": sum(r["predicted"] == "clarify" for r in answerable),
        "overall_correct": round(sum(r["execution"] for r in rows) / max(1, n), 3),
        "rows": rows,
    }


def _fmt(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.2f}" if abs(v) >= 1 else f"{v:.4f}"


def markdown(ref: list[dict], naive: list[dict], nl: list[dict]) -> str:
    lines = ["## Reference SQL suite", "", "| Query | Rows | Compiled | Without pre-agg | Used pre-agg |",
             "|---|---:|:---:|:---:|:---:|"]
    for r in ref:
        lines.append(f"| {r['name']} | {r['rows']} | {'pass' if r['compiled'] else 'FAIL'} | "
                     f"{'pass' if r['compiled_raw'] else 'FAIL'} | {'yes' if r['preagg'] else 'no'} |")
    lines.append(f"\n{sum(r['match'] for r in ref)} of {len(ref)} reference queries match.\n")
    lines += ["## Naive joins vs compiled metrics", "", "| Metric | Naive SQL | Compiled | Error | Cause |",
              "|---|---:|---:|---:|---|"]
    for n in naive:
        lines.append(f"| {n['name']} | {_fmt(n['naive'])} | {_fmt(n['correct'])} | {n['error_pct']:+.1f}% "
                     f"| {n['why']} |")
    lines += ["", "## Natural language evaluation", "",
              "| Interpreter | Questions | Exact match | Execution match | Clarification recall "
              "| False clarifications |",
              "|---|---:|---:|---:|---:|---:|"]
    for e in nl:
        lines.append(f"| {e['provider']} | {e['questions']} ({e['answerable']} answerable, {e['needs_clarification']} "
                     f"need clarification) | {e['exact_match']:.1%} | {e['execution_match']:.1%} | "
                     f"{e['clarification_recall']:.1%} | {e['false_clarifications']} |")
    return "\n".join(lines) + "\n"
