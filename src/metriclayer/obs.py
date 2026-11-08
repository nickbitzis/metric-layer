"""Structured JSON logs and Prometheus metrics, without extra dependencies."""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections import defaultdict
from datetime import UTC, datetime

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        return json.dumps(entry, default=str)


def get_logger(stream=None) -> logging.Logger:
    logger = logging.getLogger("metriclayer")
    if not logger.handlers:
        handler = logging.StreamHandler(stream or sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def log_event(logger: logging.Logger, msg: str, **fields) -> None:
    logger.info(msg, extra={"fields": fields})


def _labels(d: dict) -> str:
    return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(d.items())) + "}" if d else ""


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests: dict[tuple[str, str], int] = defaultdict(int)
        self.counters: dict[tuple[str, str], int] = defaultdict(int)  # (name, label value)
        self.gauges: dict[str, tuple[str, list[tuple[dict, float]]]] = {}
        self.buckets = [0] * len(LATENCY_BUCKETS)
        self.lat_sum = 0.0
        self.lat_count = 0

    def observe_request(self, route: str, status: int, seconds: float) -> None:
        with self.lock:
            self.requests[(route, str(status))] += 1
            for i, b in enumerate(LATENCY_BUCKETS):
                if seconds <= b:
                    self.buckets[i] += 1
            self.lat_sum += seconds
            self.lat_count += 1

    def inc(self, name: str, label: str) -> None:
        with self.lock:
            self.counters[(name, label)] += 1

    def set_gauge(self, name: str, help_text: str, samples: list[tuple[dict, float]]) -> None:
        with self.lock:
            self.gauges[name] = (help_text, samples)

    def render(self) -> str:
        out: list[str] = []
        with self.lock:
            out += [
                "# HELP metriclayer_http_requests_total HTTP requests by route and status.",
                "# TYPE metriclayer_http_requests_total counter",
            ]
            for (r, s), v in sorted(self.requests.items()):
                out.append(f"metriclayer_http_requests_total{_labels({'route': r, 'status': s})} {v}")
            out += [
                "# HELP metriclayer_events_total Engine events (queries, asks, cache, pre-aggregations).",
                "# TYPE metriclayer_events_total counter",
            ]
            for (n, lab), v in sorted(self.counters.items()):
                out.append(f"metriclayer_events_total{_labels({'event': n, 'outcome': lab})} {v}")
            for name, (help_text, samples) in sorted(self.gauges.items()):
                out += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
                out += [f"{name}{_labels(lab)} {val}" for lab, val in samples]
            out += [
                "# HELP metriclayer_http_request_duration_seconds Request latency.",
                "# TYPE metriclayer_http_request_duration_seconds histogram",
            ]
            for b, c in zip(LATENCY_BUCKETS, self.buckets, strict=True):
                out.append(f'metriclayer_http_request_duration_seconds_bucket{{le="{b}"}} {c}')
            out.append(f'metriclayer_http_request_duration_seconds_bucket{{le="+Inf"}} {self.lat_count}')
            out.append(f"metriclayer_http_request_duration_seconds_sum {self.lat_sum:.6f}")
            out.append(f"metriclayer_http_request_duration_seconds_count {self.lat_count}")
        return "\n".join(out) + "\n"
