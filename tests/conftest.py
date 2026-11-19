import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
EX = ROOT / "examples"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Fresh data dir + seeded DB + mock LLM for every test."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("SLICER", "estimator")
    monkeypatch.setenv("PAYMENT_PROVIDER", "mock")
    from app.db.database import init_db
    init_db()
    return tmp_path


@pytest.fixture
def svc():
    from app.service import PrintService
    return PrintService()


@pytest.fixture
def example():
    return lambda name: str(EX / name)
