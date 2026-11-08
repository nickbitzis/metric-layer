"""Command line: build the warehouse, serve, run the correctness suite and the NL eval, compile a query."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .app import ROOT, default_engine, serve
from .compiler import Compiler, MetricQuery
from .engine import Engine
from .evaluate import markdown, naive_examples, run_nl_eval, run_reference
from .model import load_model
from .nl import make_interpreter
from .warehouse import Scale, build


def _engine(db: str | None) -> Engine:
    if db:
        os.environ["METRICLAYER_DB"] = db
    return default_engine()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="metriclayer")
    ap.add_argument("--db", help="DuckDB path (default data/warehouse.duckdb)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="generate the synthetic warehouse")
    b.add_argument("--orders", type=int, default=20000)
    b.add_argument("--seed", type=int, default=7)
    s = sub.add_parser("serve")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    v = sub.add_parser("verify", help="compiled vs reference SQL, plus naive join examples")
    v.add_argument("--summary", help="append a markdown table to this file (for $GITHUB_STEP_SUMMARY)")
    n = sub.add_parser("nl-eval", help="natural language evaluation")
    n.add_argument("--provider", default="offline", choices=["offline", "claude"])
    n.add_argument("--summary")
    n.add_argument("--show-misses", action="store_true")
    r = sub.add_parser("report", help="write docs/results.md and docs/results.json")
    r.add_argument("--out", default=str(ROOT / "docs"))
    c = sub.add_parser("compile", help="print the SQL for a metric query given as JSON")
    c.add_argument("query")
    a = ap.parse_args(argv)

    if a.cmd == "build":
        db = Path(a.db or ROOT / "data" / "warehouse.duckdb")
        scale = Scale() if a.orders == 20000 else Scale(customers=max(100, a.orders // 5),
                                                        sellers=max(10, a.orders // 130),
                                                        products=max(50, a.orders // 16), orders=a.orders)
        print(f"built {db} data_version={build(db, scale, a.seed)}")
        return 0
    if a.cmd == "serve":
        if a.db:
            os.environ["METRICLAYER_DB"] = a.db
        serve(a.host, a.port)
        return 0
    if a.cmd == "compile":  # no database needed: compiling only reads the semantic model
        layer = load_model()
        print(Compiler(layer, layer.preaggregations).compile(MetricQuery.from_dict(json.loads(a.query))).sql)
        return 0
    eng = _engine(a.db)
    if a.cmd == "verify":
        ref = run_reference(eng)
        naive = naive_examples(eng)
        md = markdown(ref, naive, [])
        md = md[: md.index("## Natural language evaluation")]
        print(md)
        if a.summary:
            with open(a.summary, "a") as f:
                f.write(md)
        bad = [r["name"] for r in ref if not r["match"]]
        if bad:
            print(f"MISMATCH: {bad}", file=sys.stderr)
            return 1
        return 0
    if a.cmd == "nl-eval":
        res = run_nl_eval(eng, make_interpreter(eng, a.provider))
        md = markdown([], [], [res])
        md = md[md.index("## Natural language evaluation"):]
        print(md)
        if a.show_misses:
            for row in res["rows"]:
                if not row["execution"]:
                    print(f"miss: {row['question']} -> {row['predicted']} {row.get('interpreted') or ''} "
                          f"{row['clarification'] or ''}")
        if a.summary:
            with open(a.summary, "a") as f:
                f.write(md)
        return 0
    if a.cmd == "report":
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        ref, naive = run_reference(eng), naive_examples(eng)
        nl = run_nl_eval(eng, make_interpreter(eng, "offline"))
        (out / "results.md").write_text(markdown(ref, naive, [nl]))
        (out / "results.json").write_text(json.dumps({"reference": ref, "naive": naive, "nl": nl}, indent=1))
        print(f"wrote {out / 'results.md'}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
