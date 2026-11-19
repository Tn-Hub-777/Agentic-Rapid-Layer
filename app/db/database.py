"""SQLite persistence for business data (materials, stock, printers, jobs, orders, traces).

SQLite keeps the project zero-install. The SQL is plain, so moving to PostgreSQL later
only means swapping this module.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

from app.config import settings

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS materials(
  name TEXT PRIMARY KEY, density_g_cm3 REAL, price_per_g REAL);
CREATE TABLE IF NOT EXISTS stock(
  material TEXT, color TEXT, on_hand_g REAL, reserved_g REAL DEFAULT 0, reorder_level_g REAL DEFAULT 200,
  PRIMARY KEY(material, color));
CREATE TABLE IF NOT EXISTS reservations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, material TEXT, color TEXT,
  grams REAL, status TEXT);              -- RESERVED | CONSUMED | RELEASED
CREATE TABLE IF NOT EXISTS printers(
  id TEXT PRIMARY KEY, name TEXT, bed_x REAL, bed_y REAL, bed_z REAL, materials TEXT,
  loaded_material TEXT, loaded_color TEXT, status TEXT, queue_minutes REAL DEFAULT 0,
  speed_factor REAL DEFAULT 1.0, driver TEXT DEFAULT 'simulated', url TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, printer_id TEXT, gcode_path TEXT,
  est_minutes REAL, status TEXT, remote_id TEXT);
CREATE TABLE IF NOT EXISTS orders(
  id TEXT PRIMARY KEY, customer_id TEXT, status TEXT, total REAL, summary TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS trace_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT DEFAULT CURRENT_TIMESTAMP, order_id TEXT, agent TEXT,
  kind TEXT, name TEXT, duration_ms REAL, input_tokens INTEGER, output_tokens INTEGER, error TEXT);
"""

SEED_MATERIALS = [("PLA", 1.24, 1.5), ("PETG", 1.27, 1.9), ("ABS", 1.04, 1.8), ("TPU", 1.21, 3.0)]
SEED_STOCK = [  # material, color, on_hand_g, reserved_g, reorder_level_g
    ("PLA", "white", 1000, 0, 200), ("PLA", "black", 1000, 0, 200), ("PLA", "red", 500, 0, 200),
    ("PETG", "black", 1000, 0, 200), ("PETG", "white", 800, 0, 200), ("PETG", "blue", 60, 0, 200),
    ("ABS", "white", 1000, 0, 200), ("TPU", "black", 500, 0, 150),
]
SEED_PRINTERS = [  # id, name, bed xyz, materials, loaded mat, loaded color, status, queue min, speed
    ("P1", "Prusa MK4 (A)", 250, 210, 220, ["PLA", "PETG", "ABS"], "PLA", "white", "idle", 0, 1.0),
    ("P2", "Ender 3 V3", 220, 220, 250, ["PLA", "PETG"], "PETG", "black", "busy", 180, 0.8),
    ("P3", "Voron Large", 350, 350, 340, ["PLA", "PETG", "ABS", "TPU"], "ABS", "white", "idle", 60, 1.2),
    ("P4", "Bambu Mini", 180, 180, 180, ["PLA", "PETG", "TPU"], "PLA", "black", "idle", 30, 1.5),
    ("P5", "Old Anycubic", 200, 200, 200, ["PLA"], "PLA", "red", "offline", 0, 0.7),
]


@contextmanager
def connect():
    """Short-lived connection; IMMEDIATE transactions give us atomic stock updates."""
    with _lock:
        conn = sqlite3.connect(settings.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def init_db(seed: bool = True) -> None:
    with connect() as c:
        c.executescript(SCHEMA)
        if seed and c.execute("SELECT COUNT(*) FROM materials").fetchone()[0] == 0:
            c.executemany("INSERT INTO materials VALUES (?,?,?)", SEED_MATERIALS)
            c.executemany("INSERT INTO stock VALUES (?,?,?,?,?)", SEED_STOCK)
            for p in SEED_PRINTERS:
                c.execute(
                    "INSERT INTO printers(id,name,bed_x,bed_y,bed_z,materials,loaded_material,loaded_color,"
                    "status,queue_minutes,speed_factor) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (p[0], p[1], p[2], p[3], p[4], json.dumps(p[5]), p[6], p[7], p[8], p[9], p[10]),
                )


def reset_db() -> None:
    """Delete and re-seed. Used by tests and the 'Reset demo' button."""
    with connect() as c:
        for t in ("materials", "stock", "reservations", "printers", "jobs", "orders", "trace_events"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
    init_db()


def upsert_order(order_id: str, customer_id: str, status: str, total: float | None = None, summary: str = "") -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO orders(id,customer_id,status,total,summary) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
            "total=COALESCE(excluded.total,orders.total), summary=excluded.summary, updated_at=CURRENT_TIMESTAMP",
            (order_id, customer_id, status, total, summary),
        )


def rows(sql: str, args: tuple = ()) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
