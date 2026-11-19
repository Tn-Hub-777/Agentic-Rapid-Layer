"""Persistence for the agent system.

* Checkpointer (SqliteSaver)  - SHORT-TERM memory: full graph state per order (thread_id = order id).
                                 Lets an order pause for approval/payment/print and resume after a restart.
* Store (SqliteStore)         - LONG-TERM memory shared across orders: customer preferences, material usage stats.
"""
from __future__ import annotations

import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.sqlite import SqliteStore

from app.config import settings


def make_checkpointer() -> SqliteSaver:
    conn = sqlite3.connect(settings.checkpoint_path, check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def make_store() -> SqliteStore:
    conn = sqlite3.connect(settings.data_dir / "memory_store.db", check_same_thread=False, isolation_level=None)
    store = SqliteStore(conn)
    store.setup()
    return store
