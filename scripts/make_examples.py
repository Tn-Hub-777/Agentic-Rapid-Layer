"""Generate sample STL files (so the demo works with no downloads)."""
from pathlib import Path

import trimesh

out = Path(__file__).resolve().parent.parent / "examples"
out.mkdir(exist_ok=True)
trimesh.creation.box(extents=(40, 30, 20)).export(out / "small_box.stl")
trimesh.creation.cylinder(radius=25, height=60, sections=64).export(out / "cylinder.stl")
trimesh.creation.icosphere(subdivisions=3, radius=35).export(out / "sphere.stl")
trimesh.creation.box(extents=(300, 120, 80)).export(out / "large_bracket.stl")   # only fits the big printer
trimesh.creation.annulus(r_min=15, r_max=30, height=10, sections=64).export(out / "ring.stl")
trimesh.creation.box(extents=(500, 50, 50)).export(out / "too_big.stl")           # fits no printer
print("examples written to", out)
