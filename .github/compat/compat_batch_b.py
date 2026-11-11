"""Six pinned downstream probes. Consumer sources are never patched.

Terrain examples are executed unchanged, with only their output directories
provided. Dependency pin substitutions belong to the manifest, not these probes.
"""

import faulthandler
import json
import os
from pathlib import Path
import runpy
import struct
import sys
import traceback

from compat_new import native, provenance, solid, step_roundtrip


def stl_bytes(data):
    assert len(data) > 84, len(data)
    if data[:5].lower() == b"solid":
        assert b"facet normal" in data and b"endsolid" in data
    else:
        triangles = struct.unpack_from("<I", data, 80)[0]
        assert triangles > 0 and len(data) == 84 + triangles * 50


def example(source, script, result_name, output):
    (Path.cwd() / "stl").mkdir(exist_ok=True)
    namespace = runpy.run_path(str(source / script))
    provenance()
    shape = solid(namespace[result_name])
    stl_bytes((Path.cwd() / output).read_bytes())
    step_roundtrip(shape, Path.cwd() / "roundtrip.step")
    return {"scenario": "unchanged original example, valid solid, STL and STEP roundtrip",
            "example": script, "volume": shape.Volume()}


def fantasy(source):
    return example(source, "example/arch/basic_arch.py", "ex_arch",
                   "stl/arch_basic_arch.stl")


def dome(source):
    return example(source, "example/make_hexagon.py", "hexagon",
                   "dome_make_hexagon.stl")


def gothic(source):
    return example(source, "example/catwalk.py", "result", "stl/catwalk.stl")


def gears(source):
    return native(source, ["tests"])


def gears_focused(source):
    # The full suite remains a separate timeout result, not a passing suite.
    return native(source, [
        "tests/test_core.py",
        "tests/test_rack_cutter.py::TestCuttersAreCompatible",
        "tests/test_rack_cutter.py::TestFindCompatibleCutterGroups",
        "tests/test_rack_cutter.py::TestCreateRackCutter::test_spur_produces_workplane",
        "tests/test_rack_cutter.py::TestCreateRackCutter::test_b_override_changes_volume",
        "tests/test_rack_cutter.py::TestCreateRackCutterForGroup",
        "tests/test_api.py::TestBuildParametricGear::test_returns_parametric_gear[spur]",
    ])


def vendor(source):
    from vendor_models.waveshare import Ddsm115

    provenance()
    model = Ddsm115()
    assert model.path.is_file()
    assert "ISO-10303-21" in model.step()
    shape = solid(model.cq_object())
    step_roundtrip(shape, Path.cwd() / "motor.step")
    return {"scenario": "own bundled Waveshare DDSM115 STEP importer and STEP roundtrip",
            "resource": str(model.path_relative), "solids": len(shape.Solids())}


def fastapi(source):
    # Exercise the actual ASGI endpoint, not an imitation of its model function.
    from fastapi.testclient import TestClient
    import app

    provenance()
    payload = {"mounts": [[20.0, 25.0]], "angle": 60.0, "screw_size": 2.4,
               "edge_padding": 5.0, "padding": 10.0, "thickness": 3.0}
    with TestClient(app.app) as client:
        sizes = []
        for index in range(2):
            response = client.post("/backend/stand", json=payload)
            assert response.status_code == 200, response.text
            assert "filename=" in response.headers["content-disposition"]
            stl_bytes(response.content)
            (Path.cwd() / f"stand-{index}.stl").write_bytes(response.content)
            sizes.append(len(response.content))
        assert client.get("/models/does-not-exist").status_code == 404
    return {"scenario": "actual FastAPI POST model/streaming-STL route twice and unknown GET",
            "stl_sizes": sizes,
            "not_covered": "browser/template rendering, deployment and cached-model download"}


SCENARIOS = {66: fantasy, 67: dome, 68: gothic, 69: gears, 70: vendor, 71: fastapi,
             108: gears_focused}


if __name__ == "__main__":
    faulthandler.enable()
    project_id = int(sys.argv[1])
    source = Path(sys.argv[2]).resolve()
    # Keep the selected CQ checkout ahead of the consumer, matching run.py.
    sys.path[1:1] = [str(source), str(source / "src")]
    try:
        detail = SCENARIOS[project_id](source)
        print(json.dumps({"id": project_id, "status": "passed", **detail}), flush=True)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
