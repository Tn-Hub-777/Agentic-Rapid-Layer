"""Deterministic mesh analysis (trimesh). The LLM never does geometry."""
from __future__ import annotations

import os
import re

import trimesh

from app.config import settings

# Largest build volume in the fleet is checked by the scheduler; here we only grade printability.
MIN_FEATURE_MM = 5.0


def sanitize_filename(name: str) -> str:
    name = os.path.basename(name)
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:100]


def analyze_model(file_path: str) -> dict:
    """Validate and analyse an STL/OBJ/3MF file. Returns a plain dict (JSON-safe)."""
    result: dict = {"valid": False, "file": os.path.basename(file_path), "warnings": []}
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in settings.allowed_extensions:
        result["error"] = f"Unsupported format '{ext}'. Allowed: {', '.join(settings.allowed_extensions)}"
        return result
    if not os.path.isfile(file_path):
        result["error"] = "File not found"
        return result
    size_mb = os.path.getsize(file_path) / 1_048_576
    result["file_size_mb"] = round(size_mb, 3)
    if size_mb > settings.max_upload_mb:
        result["error"] = f"File too large ({size_mb:.1f} MB > {settings.max_upload_mb} MB)"
        return result
    try:
        mesh = trimesh.load(file_path, force="mesh")
    except Exception as exc:  # corrupted / hostile file: report, never crash the worker
        result["error"] = f"Could not parse file: {exc}"
        return result
    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        result["error"] = "File contains no printable triangle mesh"
        return result

    dims = (mesh.bounds[1] - mesh.bounds[0]).tolist()
    watertight = bool(mesh.is_watertight)
    winding_ok = bool(mesh.is_winding_consistent)
    volume_mm3 = abs(float(mesh.volume)) if watertight else abs(float(mesh.convex_hull.volume)) * 0.6
    if not watertight:
        result["warnings"].append("Mesh is not watertight (holes/open surfaces); volume is approximate.")
    if not winding_ok:
        result["warnings"].append("Inconsistent face winding (flipped normals).")
    if min(dims) < MIN_FEATURE_MM:
        result["warnings"].append(f"Very thin model ({min(dims):.1f} mm); fine features may not print.")
    degenerate = int((mesh.area_faces < 1e-9).sum())
    if degenerate:
        result["warnings"].append(f"{degenerate} degenerate faces found.")

    printability = "good"
    if result["warnings"]:
        printability = "fair"
    if not watertight and not winding_ok:
        printability = "poor"

    result.update(
        valid=True,
        format=ext.lstrip(".").upper(),
        dimensions_mm={"x": round(dims[0], 2), "y": round(dims[1], 2), "z": round(dims[2], 2)},
        volume_cm3=round(volume_mm3 / 1000, 3),
        surface_area_mm2=round(float(mesh.area), 1),
        face_count=int(len(mesh.faces)),
        mesh_valid=watertight and winding_ok,
        watertight=watertight,
        printability=printability,
    )
    return result
