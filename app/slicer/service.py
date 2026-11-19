"""Slicer abstraction: real PrusaSlicer CLI when available, physics-based estimator otherwise.

Security: the slicer is launched with a fixed argv list (no shell), a timeout, and never with
any text produced by an LLM. In production run it inside a container (see docs).
"""
from __future__ import annotations

import math
import re
import shutil
import subprocess
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

import trimesh

from app.config import settings
from app.db.database import rows


def _density(material: str) -> float:
    r = rows("SELECT density_g_cm3 FROM materials WHERE name=?", (material,))
    return r[0]["density_g_cm3"] if r else 1.24


class SlicerService(ABC):
    name = "base"

    @abstractmethod
    def slice(self, file_path: str, material: str, infill: int, layer_h: float, walls: int) -> dict: ...


class EstimatorSlicer(SlicerService):
    """Fast approximation: shell volume (walls) + infill volume. Writes a stub G-code file."""
    name = "estimator"

    def slice(self, file_path, material, infill, layer_h, walls):
        mesh = trimesh.load(file_path, force="mesh")
        vol = abs(float(mesh.volume)) if mesh.is_watertight else abs(float(mesh.convex_hull.volume)) * 0.6
        shell = min(vol, float(mesh.area) * walls * 0.45)          # 0.45 mm line width
        solid = shell + (vol - shell) * infill / 100.0
        weight = solid / 1000.0 * _density(material)
        z = float(mesh.bounds[1][2] - mesh.bounds[0][2])
        layers = max(1, math.ceil(z / layer_h))
        flow = 3.0 + 25.0 * layer_h                                 # mm3/s, thicker layers flow faster
        minutes = solid / flow / 60.0 * 1.25 + layers * 0.05 + 3    # +25% travel, per-layer overhead, warm-up
        out = Path(settings.data_dir) / "gcode" / f"{uuid.uuid4().hex[:10]}.gcode"
        out.parent.mkdir(exist_ok=True)
        out.write_text(
            f"; SIMULATED G-CODE (estimator slicer)\n; source={Path(file_path).name}\n"
            f"; material={material} infill={infill}% layer={layer_h} walls={walls}\n"
            f"; filament used [g] = {weight:.2f}\n; layers = {layers}\nG28\nM104 S200\nM84\n")
        return dict(weight_g=round(weight, 2), print_minutes=round(minutes, 1), layer_count=layers,
                    gcode_path=str(out), source=self.name, warnings=[])


class PrusaSlicerService(SlicerService):
    """Runs the real PrusaSlicer CLI (free, open source) as an isolated subprocess."""
    name = "prusaslicer"

    def __init__(self, binary: str):
        self.binary = binary

    def slice(self, file_path, material, infill, layer_h, walls):
        out = Path(settings.data_dir) / "gcode" / f"{uuid.uuid4().hex[:10]}.gcode"
        out.parent.mkdir(exist_ok=True)
        cmd = [self.binary, "--export-gcode", "--layer-height", str(layer_h), "--fill-density", f"{infill}%",
               "--perimeters", str(walls), "--output", str(out), file_path]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, shell=False)
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(f"PrusaSlicer failed: {proc.stderr[-300:]}")
        text = out.read_text(errors="ignore")
        grams = re.search(r"filament used \[g\]\s*=\s*([\d.]+)", text)
        t = re.search(r"estimated printing time \(normal mode\)\s*=\s*(.+)", text)
        minutes = 0.0
        if t:
            for val, unit in re.findall(r"(\d+)([dhms])", t.group(1)):
                minutes += int(val) * {"d": 1440, "h": 60, "m": 1, "s": 1 / 60}[unit]
        layers = len(re.findall(r";LAYER_CHANGE", text))
        if not grams:
            raise RuntimeError("Could not read filament usage from G-code")
        return dict(weight_g=float(grams.group(1)), print_minutes=round(minutes, 1), layer_count=layers,
                    gcode_path=str(out), source=self.name, warnings=[])


def get_slicer() -> SlicerService:
    mode = settings.slicer_mode
    if mode == "auto" and shutil.which(settings.prusa_path):
        return PrusaSlicerService(settings.prusa_path)
    return EstimatorSlicer()


def slice_model(file_path: str, material: str, infill: int, layer_h: float, walls: int) -> dict:
    slicer = get_slicer()
    try:
        return slicer.slice(file_path, material, infill, layer_h, walls)
    except Exception as exc:                                    # fall back, but tell the customer
        if isinstance(slicer, EstimatorSlicer):
            raise
        res = EstimatorSlicer().slice(file_path, material, infill, layer_h, walls)
        res["warnings"].append(f"Real slicer failed ({exc}); used estimator instead.")
        return res
