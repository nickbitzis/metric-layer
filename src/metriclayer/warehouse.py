"""Synthetic warehouse for Larkspur Market, a made-up two-sided marketplace.

Eight tables in DuckDB, generated from a fixed seed. The grain traps are deliberate:
- order_items is finer than orders (1 to 5 items per order), so joining them repeats order-level values.
- refunds are partial and an order can have up to 3 of them, so joining refunds to items multiplies both sides.
- sessions.customer_id is NULL for anonymous visits, so an inner join to customers drops 40% of sessions.
"""

from __future__ import annotations

import csv
import hashlib
import random
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

START = date(2025, 1, 1)
END = date(2026, 7, 1)  # exclusive; data covers 18 months
COUNTRIES = [("US", 0.42), ("GB", 0.16), ("DE", 0.14), ("FR", 0.1), ("CA", 0.1), ("AU", 0.08)]
ACQ = [("organic", 0.35), ("paid_search", 0.25), ("social", 0.18), ("email", 0.12), ("referral", 0.1)]
SEGMENTS = [("consumer", 0.8), ("business", 0.2)]
DEVICES = [("web", 0.45), ("ios", 0.33), ("android", 0.22)]
CATEGORIES = {"electronics": 140, "home": 55, "fashion": 40, "toys": 28, "books": 16, "beauty": 24}
REGIONS = [("NA", 0.45), ("EU", 0.4), ("APAC", 0.15)]
TRAFFIC = [("organic", 0.38), ("paid_search", 0.24), ("social", 0.16), ("email", 0.1), ("direct", 0.12)]
SPEND_CHANNELS = {"paid_search": 900.0, "social": 520.0, "email": 80.0, "affiliate": 240.0}
REASONS = [("damaged", 0.3), ("not_as_described", 0.25), ("late_delivery", 0.2), ("changed_mind", 0.25)]
SELLER_WORDS = ["Copper", "Juniper", "Northgate", "Willow", "Saltmarsh", "Ember", "Quarry", "Bramble", "Tidewater",
                "Hollow", "Granite", "Meadow", "Lantern", "Orchard", "Foxglove"]
SELLER_KINDS = ["Goods", "Supply", "Works", "Trading", "Outfitters", "Studio", "House", "Co"]


@dataclass(frozen=True)
class Scale:
    customers: int = 4000
    sellers: int = 150
    products: int = 1200
    orders: int = 20000

    @classmethod
    def small(cls) -> Scale:
        return cls(customers=400, sellers=30, products=150, orders=2000)


DEFAULT_SCALE = Scale()


_CUM: dict[int, tuple[list[str], list[float]]] = {}


def _pick(rng: random.Random, options: list[tuple[str, float]]) -> str:
    if id(options) not in _CUM:
        names, total, cum = [o for o, _ in options], 0.0, []
        for _, w in options:
            total += w
            cum.append(total)
        _CUM[id(options)] = (names, cum)
    names, cum = _CUM[id(options)]
    return rng.choices(names, cum_weights=cum)[0]


def _ts(rng: random.Random, day: date) -> datetime:
    return datetime(day.year, day.month, day.day) + timedelta(seconds=rng.randint(0, 86399))


def _season(day: date) -> float:
    """Order volume multiplier: growth over time plus a November and December peak."""
    months = (day.year - START.year) * 12 + day.month - 1
    peak = {11: 1.35, 12: 1.6, 1: 0.85}.get(day.month, 1.0)
    return (1 + 0.035 * months) * peak


def generate(scale: Scale = DEFAULT_SCALE, seed: int = 7) -> dict[str, tuple[list[str], list[tuple]]]:
    rng = random.Random(seed)
    days = [START + timedelta(days=i) for i in range((END - START).days)]
    weights = [_season(d) for d in days]
    cum_days, total = [], 0.0
    for w in weights:
        total += w
        cum_days.append(total)

    customers = []
    for cid in range(1, scale.customers + 1):
        early = START - timedelta(days=rng.randint(1, 600))
        signup = rng.choice(days[: len(days) - 30]) if rng.random() > 0.25 else early
        customers.append((cid, signup, _pick(rng, COUNTRIES), _pick(rng, ACQ), _pick(rng, SEGMENTS)))
    signup_of = {c[0]: c[1] for c in customers}

    sellers = []
    for sid in range(1, scale.sellers + 1):
        name = f"{rng.choice(SELLER_WORDS)} {rng.choice(SELLER_KINDS)} {sid}"
        sellers.append((sid, name, _pick(rng, REGIONS), "pro" if rng.random() < 0.3 else "standard"))

    products = []
    for pid in range(1, scale.products + 1):
        cat = rng.choice(list(CATEGORIES))
        price = round(CATEGORIES[cat] * rng.lognormvariate(0, 0.5), 2)
        products.append((pid, rng.randint(1, scale.sellers), cat, price))

    orders, items, refunds, sessions = [], [], [], []
    order_days = rng.choices(days, weights=weights, k=scale.orders)
    order_days.sort()
    item_id = refund_id = session_id = 0
    active = [c[0] for c in customers]
    for oid, day in enumerate(order_days, start=1):
        cid = rng.choice(active)
        if signup_of[cid] > day:
            cid = rng.choice(active[: max(1, len(active) // 3)])
        ts = _ts(rng, day)
        r = rng.random()
        status = "cancelled" if r < 0.06 else ("pending" if day > END - timedelta(days=10) and r < 0.5 else "completed")
        device = _pick(rng, DEVICES)
        shipping = rng.choice([0.0, 0.0, 4.99, 7.99, 12.5])
        orders.append((oid, cid, ts, status, device, shipping))
        order_total = 0.0
        for _ in range(rng.choices([1, 2, 3, 4, 5], weights=[40, 28, 17, 10, 5])[0]):
            item_id += 1
            pid, _, _, price = rng.choice(products)
            qty = rng.choices([1, 2, 3], weights=[78, 17, 5])[0]
            unit = round(price * rng.uniform(0.9, 1.1), 2)
            discount = round(unit * qty * rng.choice([0, 0, 0, 0.1, 0.2]), 2)
            items.append((item_id, oid, pid, qty, unit, discount))
            order_total += unit * qty - discount
        if status == "completed" and rng.random() < 0.09:
            for _ in range(rng.choices([1, 2, 3], weights=[75, 20, 5])[0]):
                refund_id += 1
                when = ts + timedelta(days=rng.randint(2, 40), seconds=rng.randint(0, 86399))
                if when.date() >= END:
                    when = datetime(END.year, END.month, END.day) - timedelta(hours=rng.randint(1, 200))
                    when = max(when, ts + timedelta(hours=1))
                amount = round(order_total * rng.uniform(0.1, 0.45), 2)
                refunds.append((refund_id, oid, when, amount, _pick(rng, REASONS)))
        session_id += 1
        started = ts - timedelta(minutes=rng.randint(2, 50))
        sessions.append((session_id, cid, started, device, _pick(rng, TRAFFIC), True))

    # Non-converting sessions: about 4 per order, 40% anonymous (customer_id NULL).
    for _ in range(scale.orders * 4):
        session_id += 1
        day = rng.choices(days, cum_weights=cum_days)[0]
        cid = None if rng.random() < 0.4 else rng.randint(1, scale.customers)
        sessions.append((session_id, cid, _ts(rng, day), _pick(rng, DEVICES), _pick(rng, TRAFFIC), False))
    sessions.sort(key=lambda s: s[2])
    sessions = [(i, *s[1:]) for i, s in enumerate(sessions, start=1)]

    size = scale.orders / 20000
    spend = []
    for day in days:
        for ch, base in SPEND_CHANNELS.items():
            spend.append((day, ch, round(base * size * _season(day) * rng.uniform(0.7, 1.3), 2)))

    return {
        "customers": (["customer_id", "signup_date", "country", "acquisition_channel", "segment"], customers),
        "sellers": (["seller_id", "seller_name", "region", "tier"], sellers),
        "products": (["product_id", "seller_id", "category", "list_price"], products),
        "orders": (["order_id", "customer_id", "ordered_at", "status", "device", "shipping_fee"], orders),
        "order_items": (
            ["order_item_id", "order_id", "product_id", "quantity", "unit_price", "discount_amount"], items),
        "refunds": (["refund_id", "order_id", "refunded_at", "amount", "reason"], refunds),
        "sessions": (["session_id", "customer_id", "started_at", "device", "traffic_source", "converted"], sessions),
        "marketing_spend": (["spend_date", "channel", "amount"], spend),
    }


TYPES = {
    "customers": "customer_id INTEGER, signup_date DATE, country VARCHAR, acquisition_channel VARCHAR, segment VARCHAR",
    "sellers": "seller_id INTEGER, seller_name VARCHAR, region VARCHAR, tier VARCHAR",
    "products": "product_id INTEGER, seller_id INTEGER, category VARCHAR, list_price DECIMAL(12,2)",
    "orders": "order_id INTEGER, customer_id INTEGER, ordered_at TIMESTAMP, status VARCHAR, device VARCHAR, "
    "shipping_fee DECIMAL(12,2)",
    "order_items": "order_item_id INTEGER, order_id INTEGER, product_id INTEGER, quantity INTEGER, "
    "unit_price DECIMAL(12,2), discount_amount DECIMAL(12,2)",
    "refunds": "refund_id INTEGER, order_id INTEGER, refunded_at TIMESTAMP, amount DECIMAL(12,2), reason VARCHAR",
    "sessions": "session_id INTEGER, customer_id INTEGER, started_at TIMESTAMP, device VARCHAR, "
    "traffic_source VARCHAR, converted BOOLEAN",
    "marketing_spend": "spend_date DATE, channel VARCHAR, amount DECIMAL(12,2)",
}


def load(con: duckdb.DuckDBPyConnection, tables: dict[str, tuple[list[str], list[tuple]]], seed: int) -> str:
    """Load tables through CSV (fast and exact), write a _meta row, and return the data version."""
    digest = hashlib.sha256(f"seed={seed}".encode())
    with tempfile.TemporaryDirectory() as tmp:
        for name, (cols, rows) in tables.items():
            path = Path(tmp) / f"{name}.csv"
            with path.open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(cols)
                w.writerows(rows)
            con.execute(f"DROP TABLE IF EXISTS {name}")
            con.execute(f"CREATE TABLE {name} ({TYPES[name]})")
            con.execute(f"COPY {name} FROM '{path.as_posix()}' (HEADER, NULLSTR '')")
            digest.update(f"{name}:{len(rows)}".encode())
    version = digest.hexdigest()[:12]
    con.execute("DROP TABLE IF EXISTS _meta")
    con.execute("CREATE TABLE _meta (data_version VARCHAR, seed INTEGER, built_at TIMESTAMP)")
    con.execute("INSERT INTO _meta VALUES (?, ?, now())", [version, seed])
    return version


def build(path: Path | str, scale: Scale = DEFAULT_SCALE, seed: int = 7) -> str:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
    con = duckdb.connect(str(path))
    try:
        return load(con, generate(scale, seed), seed)
    finally:
        con.close()


def data_version(con: duckdb.DuckDBPyConnection) -> str:
    try:
        return con.execute("SELECT data_version FROM _meta").fetchone()[0]
    except duckdb.Error:
        return "unknown"
