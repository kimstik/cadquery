"""Pinned worker, mechanical part and mold-engine comparison scenarios.

Consumers load CadQuery naturally; the parent supplies the paired source path.
No consumer edits, uv source resolution, GUI setup or dependency monkeypatches.
"""

import faulthandler
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import traceback
import xml.etree.ElementTree as ET


def provenance(source, package):
    consumer = sys.modules[package]
    consumer_file = Path(consumer.__file__).resolve()
    assert consumer_file.is_relative_to(source), (consumer_file, source)
    cq = sys.modules.get("cadquery")
    assert cq is not None, "Consumer did not load CadQuery"
    actual = Path(cq.__file__).resolve()
    expected = Path(os.environ["COMPAT_CQ_ROOT"]).resolve()
    assert actual.is_relative_to(expected), (actual, expected)
    record = {"consumer_source": str(consumer_file), "cadquery_source": str(actual)}
    print(json.dumps(record), flush=True)
    return record


def native(source, selection, package, expected_tests, plugins=()):
    report = Path(os.environ["COMPAT_RESULT_DIR"]) / (
        os.environ["CQ_COMPAT_VARIANT"] + "-native-tests.xml"
    )
    bootstrap = """import json, os, sys, pytest
from pathlib import Path
code = pytest.main(sys.argv[1:])
cq = sys.modules.get('cadquery')
consumer = sys.modules.get(os.environ['COMPAT_CONSUMER_PACKAGE'])
actual = str(Path(cq.__file__).resolve()) if cq is not None else None
consumer_file = str(Path(consumer.__file__).resolve()) if consumer is not None else None
expected = Path(os.environ['COMPAT_CQ_ROOT']).resolve()
if actual is not None:
    assert Path(actual).is_relative_to(expected), (actual, expected)
if consumer_file is not None:
    assert Path(consumer_file).is_relative_to(Path.cwd().resolve()), consumer_file
record = {'cadquery_source': actual, 'consumer_source': consumer_file,
          'expected_cadquery_root': str(expected)}
folder = Path(os.environ['COMPAT_RESULT_DIR'])
(folder / (os.environ['CQ_COMPAT_VARIANT'] + '-native-provenance.json')).write_text(json.dumps(record, indent=2) + '\\n')
print(json.dumps(record), flush=True)
if code == 0:
    assert actual is not None, 'Native tests did not load CadQuery'
    assert consumer_file is not None, 'Native tests did not load consumer module'
sys.exit(code)
"""
    args = [sys.executable, "-X", "faulthandler", "-c", bootstrap,
            "-ra", "--junitxml=" + str(report)]
    for plugin in plugins:
        args.extend(["-p", plugin])
    args.extend(selection)
    result = subprocess.run(
        args, cwd=source, timeout=700,
        env=dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                 COMPAT_CONSUMER_PACKAGE=package),
    )
    assert result.returncode == 0, f"Native pytest exited {result.returncode}"
    root = ET.parse(report).getroot()
    counts = {key: sum(int(s.get(key, 0)) for s in root.iter("testsuite"))
              for key in ("tests", "failures", "errors", "skipped")}
    assert counts == {"tests": expected_tests, "failures": 0, "errors": 0, "skipped": 0}, counts
    cases = [case.attrib for case in root.iter("testcase")]
    return {"selection": selection, "native_tests": counts, "native_cases": cases}


def valid_solid(shape):
    assert shape.isValid(), "Invalid geometry"
    assert len(shape.Solids()) == 1, "Expected one solid"
    volume = shape.Volume()
    assert math.isfinite(volume) and volume > 0, volume
    return shape


def step_roundtrip(shape, destination):
    # Consumer import and provenance check must precede this helper.
    cq = sys.modules["cadquery"]
    valid_solid(shape)
    cq.exporters.export(shape, str(destination))
    assert destination.stat().st_size > 100, destination
    text = destination.read_text(errors="replace")
    assert "ISO-10303-21;" in text and "END-ISO-10303-21;" in text
    restored = valid_solid(cq.importers.importStep(str(destination)).val())
    assert math.isclose(shape.Volume(), restored.Volume(), rel_tol=1e-5)
    return str(destination)


def coworker(source):
    detail = native(source, ["tests/test_build.py"], "cad_coworker.worker", 4,
                    plugins=("pytest_asyncio.plugin",))
    from cad_coworker.worker import build

    provenance(source, "cad_coworker.worker")
    mesh = build(source / "examples/simple_box.py",
                 {"WIDTH": 80.0, "DEPTH": 60.0, "HEIGHT": 20.0}, "draft")
    vertices, indices, normals = mesh.vertices, mesh.indices, mesh.normals
    assert vertices and len(vertices) % 9 == 0
    assert len(indices) == len(vertices) // 3 and len(normals) == len(vertices)
    assert all(math.isfinite(value) for value in vertices + normals)
    count = len(vertices) // 3
    assert all(isinstance(i, int) and 0 <= i < count for i in indices)
    volume = 0.0
    for offset in range(0, len(indices), 3):
        a, b, c = [vertices[3 * i:3 * i + 3] for i in indices[offset:offset + 3]]
        cross = (b[1] * c[2] - b[2] * c[1],
                 b[2] * c[0] - b[0] * c[2],
                 b[0] * c[1] - b[1] * c[0])
        volume += sum(a[axis] * cross[axis] for axis in range(3)) / 6.0
    assert math.isclose(abs(volume), 80 * 60 * 20, rel_tol=1e-6), volume
    output = Path.cwd() / "coworker-mesh.json"
    payload = {"vertices": vertices, "indices": indices, "normals": normals,
               "bbox": mesh.bbox}
    output.write_text(json.dumps(payload, allow_nan=False))
    assert json.loads(output.read_text()) == payload
    return {**detail, "mesh_triangles": len(indices) // 3,
            "mesh_volume": abs(volume), "mesh_json": str(output)}


def vibe(source):
    detail = native(source, ["tests/test_technic_beam.py"],
                    "vibe_cading.lego.technic_beam", 3)
    from vibe_cading.lego.technic_beam import LegoTechnicBeam

    provenance(source, "vibe_cading.lego.technic_beam")
    shape = valid_solid(LegoTechnicBeam(length_in_studs=5).solid.val())
    bounds = shape.BoundingBox()
    measured = [bounds.xlen, bounds.ylen, bounds.zlen]
    assert all(math.isclose(actual, expected, abs_tol=0.01)
               for actual, expected in zip(measured, [40.0, 7.8, 7.8])), measured
    output = step_roundtrip(shape, Path.cwd() / "technic-beam.step")
    return {**detail, "bounds": measured, "volume": shape.Volume(), "step": output}


def moldforge(source):
    from cq_model import build_mold
    from params import MoldParams

    provenance(source, "cq_model")
    provenance(source, "params")
    params = MoldParams()
    assert params.MoldType == "Male_Mold" and params.ShapeStyle == "Custom"
    assert params.AddGuideHoles and params.AddFillet and not params.AddIndicators
    model = build_mold(params)
    shape = valid_solid(model.val())
    bounds = shape.BoundingBox()
    measured = [bounds.xlen, bounds.ylen, bounds.zlen]
    assert math.isclose(bounds.xlen, params.MoldBaseWidth, abs_tol=0.01), measured
    assert math.isclose(bounds.ylen, params.MoldLength, abs_tol=0.01), measured
    assert 0 < bounds.zlen < params.MoldLength, measured
    assert shape.Volume() < bounds.xlen * bounds.ylen * bounds.zlen, shape.Volume()
    output = step_roundtrip(shape, Path.cwd() / "male-mold-default.step")
    return {"scenario": "Unmodified build_mold(MoldParams()) factory-default Male_Mold",
            "bounds": measured, "volume": shape.Volume(), "step": output,
            "scope": "Geometry engine only, not Qt/PyVista UI. Own engine may catch fillet errors; no independent fillet-success claim."}


def main():
    faulthandler.enable()
    project_id = int(sys.argv[1])
    source = Path(sys.argv[2]).resolve()
    try:
        detail = {92: coworker, 93: vibe, 94: moldforge}[project_id](source)
    except Exception:
        traceback.print_exc()
        print(json.dumps({"id": project_id, "status": "failed"}), flush=True)
        return 1
    result = {"id": project_id, "status": "passed", **detail}
    folder = Path(os.environ["COMPAT_RESULT_DIR"])
    (folder / (os.environ["CQ_COMPAT_VARIANT"] + "-scenario.json")).write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
