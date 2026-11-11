"""Native CQ bridge tests and an unchanged hardware enclosure template.

Semblage requires its Godot host and is recorded as blocked in the manifest.
No fake Godot module or replacement plugin path is used here.
"""

import faulthandler
import json
import math
from pathlib import Path
import runpy
import struct
import sys
import traceback

from compat_new import native, provenance, solid


def bridge(source):
    detail = native(source, ["-p", "pytest_pyvista.pytest_pyvista",
                             "tests/test_bridges/test_cadquery.py"])
    assert detail["native"]["tests"] == 2, detail
    assert detail["native"]["skipped"] == 0, detail
    return detail


def stl(path):
    data = path.read_bytes()
    assert len(data) > 84, path
    if data[:5].lower() == b"solid":
        assert b"facet normal" in data and b"endsolid" in data, path
    else:
        triangles = struct.unpack_from("<I", data, 80)[0]
        assert triangles > 0 and len(data) == 84 + 50 * triangles, path


def enclosure(source):
    example = (source / "skills/hardware-in-the-loop/templates/cad/examples/enclosure")
    assert (example / "parts/gasket.py").is_file()
    assert (example / "parts/gland_access.py").is_file()
    # Preserve the original __file__, HERE, cad_plugins import and parts/ lookup.
    # run_name prevents only the CLI guard; main receives its real supported --out.
    namespace = runpy.run_path(str(example / "build_enclosure.py"))
    provenance()
    output = Path.cwd() / "enclosure-output"
    code = namespace["main"](["--out", str(output)])
    assert code == 0, f"original enclosure checks failed: exit {code}"
    rows = json.loads((output / "parts.json").read_text())
    design = json.loads((output / "enclosure_cad.json").read_text())
    assert design["checks"]["pass"] is True, design["checks"]
    assert design["checks"]["problems"] == [], design["checks"]
    assert design["checks"]["plugins"]["gasket"]["pass"] is True
    parts = rows["parts"]
    assert {"base", "lid", "lid_gasket"} <= parts.keys(), parts.keys()
    assert math.isfinite(rows["totals"]["mass_g"]) and rows["totals"]["mass_g"] > 0
    import cadquery as cq

    for name, row in parts.items():
        assert row["volume_mm3"] > 0 and row["mass_g"] > 0, (name, row)
        restored = solid(cq.importers.importStep(str(output / "step" / (name + ".step"))))
        assert math.isclose(restored.Volume(), row["volume_mm3"], rel_tol=1e-3, abs_tol=0.1)
        stl(output / "stl" / (name + ".stl"))
    return {"scenario": "unchanged enclosure CLI main, original gasket/gland plugins and design checks",
            "parts": sorted(parts), "exports": "all own STEP and STL files validated",
            "not_covered": "whole engineering platform, KiCad, physical hardware, renderer and Windows setup"}


SCENARIOS = {90: bridge, 91: enclosure}


if __name__ == "__main__":
    faulthandler.enable()
    project_id = int(sys.argv[1])
    source = Path(sys.argv[2]).resolve()
    sys.path[1:1] = [str(source), str(source / "src")]
    try:
        detail = SCENARIOS[project_id](source)
        print(json.dumps({"id": project_id, "status": "passed", **detail}), flush=True)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
