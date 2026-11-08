"""FastAPI service: metric catalog with lineage, structured query, natural language ask, explain, health, metrics."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .compiler import QueryError
from .engine import Engine
from .model import HOME as ROOT
from .nl import ANCHOR, categorical_values, make_interpreter
from .obs import Metrics, get_logger, log_event
from .warehouse import Scale, build

STATIC = Path(__file__).parent / "static"


def default_engine() -> Engine:
    db = Path(os.environ.get("METRICLAYER_DB") or ROOT / "data" / "warehouse.duckdb")
    if not db.exists():
        orders = int(os.environ.get("METRICLAYER_ORDERS", 20000))
        scale = Scale() if orders == 20000 else Scale(customers=max(100, orders // 5), sellers=max(10, orders // 130),
                                                      products=max(50, orders // 16), orders=orders)
        build(db, scale)
    return Engine.open(db)


def create_app(engine: Engine | None = None, interpreter=None, claude_client=None) -> FastAPI:
    engine = engine or default_engine()
    app = FastAPI(title="Metric Layer", version="1.0.0")
    app.state.engine = engine
    app.state.values = categorical_values(engine)
    app.state.interpreters = {"offline": interpreter or make_interpreter(engine, "offline")}
    default = os.environ.get("METRICLAYER_NL", "offline")
    if claude_client is not None or default == "claude":
        app.state.interpreters["claude"] = make_interpreter(engine, "claude", client=claude_client)
    app.state.default_provider = default if default in app.state.interpreters else "offline"
    metrics = Metrics()
    logger = get_logger()

    def gauges() -> None:
        s = engine.cache_stats()
        metrics.set_gauge("metriclayer_cache_entries", "Result cache entries.", [({}, s["entries"])])
        metrics.set_gauge("metriclayer_cache_hits_total", "Result cache hits.", [({}, s["hits"])])
        metrics.set_gauge("metriclayer_cache_misses_total", "Result cache misses.", [({}, s["misses"])])
        metrics.set_gauge("metriclayer_preaggregation_hits_total", "Queries served from a pre-aggregation table.",
                          [({"table": k}, v) for k, v in sorted(s["preaggregation_hits"].items())] or [({}, 0)])
        metrics.set_gauge("metriclayer_metrics_defined", "Metrics in the semantic model.",
                          [({}, len(engine.layer.metrics))])

    @app.middleware("http")
    async def observe(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        t0 = time.perf_counter()
        response = await call_next(request)
        dt = time.perf_counter() - t0
        route = getattr(request.scope.get("route"), "path", "unmatched")
        metrics.observe_request(route, response.status_code, dt)
        response.headers["x-request-id"] = rid
        if not request.url.path.startswith("/static"):
            log_event(logger, "request", request_id=rid, method=request.method, path=request.url.path, route=route,
                      status=response.status_code, ms=round(dt * 1000, 1))
        return response

    @app.exception_handler(QueryError)
    async def query_error(_request: Request, exc: QueryError):
        metrics.inc("query", f"error_{exc.code}")
        return JSONResponse(status_code=422, content={"error": exc.to_dict()})

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "metrics": len(engine.layer.metrics), "data_version": engine.version}

    @app.get("/metrics", response_class=PlainTextResponse)
    def prom():
        gauges()
        return metrics.render()

    @app.get("/api/metrics")
    def list_metrics():
        L = engine.layer
        out = []
        for m in L.metrics.values():
            out.append({"name": m.name, "label": m.label, "type": m.type, "description": m.description,
                        "owner": m.owner, "format": m.format, "synonyms": m.synonyms,
                        "dimensions": L.dimensions_for(m.name), "lineage": L.lineage(m.name)})
        return {"model": L.name, "metrics": out}

    @app.get("/api/dimensions")
    def list_dimensions():
        L = engine.layer
        dims = [{"name": q, "type": d.type, "model": d.model, "table": L.models[d.model].table,
                 "description": d.description} for q, d in L.dimensions.items() if d.type == "categorical"]
        return {"dimensions": dims,
                "grains": ["day", "week", "month", "quarter", "year"],
                "values": app.state.values}

    @app.post("/api/query")
    def query(body: Annotated[dict, Body()]):
        res = engine.run(body)
        metrics.inc("query", "cache_hit" if res.cache_hit else "cache_miss")
        return res.to_dict()

    @app.post("/api/explain")
    def explain(body: Annotated[dict, Body()]):
        return engine.compile(body).explain()

    @app.post("/api/ask")
    def ask(body: Annotated[dict, Body()]):
        question = str(body.get("question", "")).strip()
        if not question:
            raise HTTPException(422, "question is required")
        provider = body.get("provider") or app.state.default_provider
        if provider not in app.state.interpreters:
            raise HTTPException(422, f"provider {provider!r} is not configured")
        interp = app.state.interpreters[provider].interpret(question)
        metrics.inc("ask", interp.status)
        out = {"question": question, "interpretation": interp.to_dict(), "result": None}
        if interp.status == "ok":
            out["result"] = engine.run(interp.query).to_dict()
        return out

    @app.get("/api/cache")
    def cache():
        return engine.cache_stats()

    @app.get("/api/info")
    def info():
        return {"anchor_date": ANCHOR.isoformat(), "data_version": engine.version,
                "providers": sorted(app.state.interpreters)}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def serve(host: str | None = None, port: int | None = None) -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run(create_app(), host=host or os.environ.get("HOST", "127.0.0.1"),
                port=int(port or os.environ.get("PORT", 8840)), log_level="warning")
