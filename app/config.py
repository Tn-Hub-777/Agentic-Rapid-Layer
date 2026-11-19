"""Central settings. Everything comes from environment variables (see .env.example)."""
from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader so we don't need python-dotenv. Real env vars win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        os.environ.setdefault(k.strip(), v)


_load_dotenv()


class Settings:
    """Read lazily so tests can monkeypatch the environment."""

    @property
    def llm_provider(self) -> str:
        return os.getenv("LLM_PROVIDER", "mock").lower()

    @property
    def llm_model(self) -> str:
        # Used when a real provider is selected.  The mock provider ignores this.
        return os.getenv("LLM_MODEL", "gemini-3.8-flash")

    @property
    def fallback_provider(self) -> str:
        return os.getenv("FALLBACK_PROVIDER", "").lower()

    @property
    def fallback_model(self) -> str:
        return os.getenv("FALLBACK_MODEL", "")

    @property
    def data_dir(self) -> Path:
        p = Path(os.getenv("DATA_DIR", "./data")).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def checkpoint_path(self) -> Path:
        return self.data_dir / "checkpoints.db"

    @property
    def slicer_mode(self) -> str:
        return os.getenv("SLICER", "auto").lower()

    @property
    def prusa_path(self) -> str:
        return os.getenv("PRUSA_SLICER_PATH", "prusa-slicer")

    @property
    def payment_provider(self) -> str:
        return os.getenv("PAYMENT_PROVIDER", "mock").lower()

    @property
    def payment_secret(self) -> str:
        return os.getenv("PAYMENT_SECRET", "change-me-dev-secret")

    @property
    def printer_driver(self) -> str:
        return os.getenv("PRINTER_DRIVER", "simulated").lower()

    @property
    def admin_token(self) -> str:
        return os.getenv("ADMIN_TOKEN", "dev-admin-token")

    # Upload safety limits
    max_upload_mb = 50
    allowed_extensions = (".stl", ".obj", ".3mf")


settings = Settings()
