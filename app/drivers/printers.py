"""Printer drivers. SimulatedDriver is the default; OctoPrintDriver talks to a real printer (free software)."""
from __future__ import annotations

import os
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

from app.config import settings


class PrinterDriver(ABC):
    @abstractmethod
    def send_job(self, printer: dict, gcode_path: str) -> str:
        """Upload G-code and start printing. Returns a remote job id."""


class SimulatedDriver(PrinterDriver):
    def send_job(self, printer, gcode_path):
        if not Path(gcode_path).is_file():
            raise FileNotFoundError(gcode_path)
        return f"sim-{printer['id']}-{uuid.uuid4().hex[:6]}"


class OctoPrintDriver(PrinterDriver):
    """OctoPrint REST API: POST /api/files/local (print=true). API key read from env var OCTOPRINT_KEY_<ID>."""

    def send_job(self, printer, gcode_path):
        import httpx
        key = os.getenv(f"OCTOPRINT_KEY_{printer['id']}", "")
        if not printer.get("url") or not key:
            raise RuntimeError("OctoPrint URL / API key not configured for this printer")
        with open(gcode_path, "rb") as fh:
            r = httpx.post(f"{printer['url'].rstrip('/')}/api/files/local", headers={"X-Api-Key": key},
                           files={"file": (Path(gcode_path).name, fh)}, data={"print": "true"}, timeout=60)
        r.raise_for_status()
        return r.json().get("files", {}).get("local", {}).get("name", Path(gcode_path).name)


def get_driver(printer: dict) -> PrinterDriver:
    kind = printer.get("driver") or settings.printer_driver
    return OctoPrintDriver() if kind == "octoprint" else SimulatedDriver()
