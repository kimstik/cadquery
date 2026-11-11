"""Pinned headless consumers: original kernel/model, real worker and node adapter.

No GUI, model-provider request or dependency repair is simulated. Machinome is
an explicit CQ/OCP upgrade experiment, not validation of its pinned deployment.
"""

import base64
import faulthandler
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import sys
import traceback
import xml.etree.ElementTree as ET

from compat_major_ai import command, worker_provenance, check_provenance


ROOT = Path.cwd()
RESULTS = Path(os.environ.get("COMPAT_RESULT_DIR", str(ROOT))).resolve()
VARIANT = os.environ.get("CQ_COMPAT_VARIANT", "unknown")


def provenance(name):
    """Observe the consumer's natural import; never preload CQ."""
    module = sys.modules.get("cadquery")
    assert module is not None, "consumer did not import cadquery"
    actual = Path(module.__file__).resolve()
    expected = Path(os.environ["COMPAT_CQ_ROOT"]).resolve()
    assert actual.is_relative_to(expected), str(actual)
    record = {"cadquery_source": str(actual), "expected_root": str(expected),
              "worker_pid": os.getpid(), "worker_python": sys.executable}
    (RESULTS / f"{VARIANT}-renewed-{name}-provenance.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"provenance": record}), flush=True)


def shape_check(shape, dimensions, volume=None):
    assert shape.isValid() and len(shape.Solids()) == 1
    assert math.isfinite(shape.Volume()) and shape.Volume() > 0
    box = shape.BoundingBox()
    actual = (box.xlen, box.ylen, box.zlen)
    assert all(math.isclose(a, b, abs_tol=1e-5)
               for a, b in zip(actual, dimensions)), actual
    if volume is not None:
        assert math.isclose(shape.Volume(), volume, rel_tol=1e-6), shape.Volume()


def native_tests(source, paths, name, import_paths=()):
    report = RESULTS / f"{VARIANT}-native-tests.xml"
    previous = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [*(str(path) for path in import_paths), previous])
    try:
        result = command([sys.executable, "-m", "pytest", "-q", "-o", "addopts=",
                          *paths, f"--junitxml={report}"], name, source, timeout=300)
    finally:
        os.environ["PYTHONPATH"] = previous
    assert report.is_file(), f"missing JUnit report: {report}"
    suites = ET.parse(report).getroot()
    cases = list(suites.iter("testcase"))
    failed = [case.attrib for case in cases
              if case.find("failure") is not None or case.find("error") is not None]
    skipped = sum(case.find("skipped") is not None for case in cases)
    summary = {"report": str(report), "tests": len(cases),
               "skipped": skipped, "failed": failed}
    print(json.dumps({"native_tests": summary}), flush=True)
    assert result.returncode == 0 and not failed and len(cases) > skipped, summary
    return summary


def freecad_kernel(source):
    sys.path.insert(0, str(source / "kernel"))
    import fc_code_kernel.executor as executor
    from fc_code_kernel.executor import introspect_params, run_script

    assert Path(executor.__file__).resolve().is_relative_to(source / "kernel")
    print(json.dumps({"consumer_source": str(Path(executor.__file__).resolve())}), flush=True)
    script = source / "examples/cq_pillow_block.py"
    declared = {item["name"]: item["default"]
                for item in introspect_params(str(script))}
    assert declared["length"] == 80.0 and declared["thickness"] == 10.0
    outcomes = []
    for name, params, dimensions in [
        ("default", {}, (80, 60, 10)),
        ("parameters", {"length": 90.0, "thickness": 12.0}, (90, 60, 12)),
    ]:
        result = run_script(path=str(script), source=None, params=params)
        (RESULTS / f"{VARIANT}-renewed-freecad-{name}.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8")
        assert result["error"] is None, result
        assert not result["stderr"], result["stderr"]
        assert len(result["objects"]) == 1, result
        item = result["objects"][0]
        assert item["name"] == "pillow_block" and item["color"] == "steelblue"
        data = base64.b64decode(item["brep_b64"], validate=True)
        assert len(data) > 100 and b"CASCADE" in data[:200]
        artifact = RESULTS / f"{VARIANT}-renewed-freecad-{name}.brep"
        artifact.write_bytes(data)
        provenance("freecad-" + name)
        # Validation follows the original example/collector/serializer path.
        import cadquery as cq
        shape = cq.Shape.importBrep(str(artifact))
        shape_check(shape, dimensions)
        solid = shape.Solids()[0]
        assert not solid.isInside((0, 0, 0)), "missing 22 mm center hole"
        assert solid.isInside((20, 0, 0)), "plate material missing"
        stock = math.prod(dimensions)
        center_hole = math.pi * 11 ** 2 * dimensions[2]
        assert 0 < shape.Volume() < stock - center_hole
        outcomes.append({"case": name, "volume": shape.Volume(),
                         "dimensions": dimensions, "brep_bytes": len(data)})
    tests = native_tests(source, ["tests/test_executor.py", "tests/test_serialize.py"],
                         "freecad-kernel", [source / "kernel"])
    return {"cases": outcomes, "native_tests": tests,
            "scope": "original CQ pillow block, parameter injection and BREP wire roundtrip; no FreeCAD GUI or environment repair"}


def cadsmith(source):
    from autofab.executor import Executor

    dataset = source / "data/dataset_v2/t2_engineering_parts.jsonl"
    row = next(json.loads(line) for line in dataset.read_text().splitlines()
               if json.loads(line)["id"] == "T2_001")
    output = ROOT / "cadsmith-worker"
    record, stamp = worker_provenance("renewed-cadsmith")
    # The checked-in model naturally imports CQ; only observation is appended.
    result = Executor(output_dir=str(output), timeout_seconds=120).execute(
        row["reference_code"] + "\n" + stamp, name=row["id"])
    print(json.dumps({"execution": result.__dict__}), flush=True)
    assert result.success, result.__dict__
    check_provenance(record)
    worker = json.loads(record.read_text())
    assert worker["worker_pid"] != os.getpid(), worker
    expected_volume = math.pi * (10 ** 2 - 5.25 ** 2) * 2
    geometry = result.geometry_json
    assert geometry["is_valid"] and geometry["num_faces"] == 4, geometry
    assert math.isclose(geometry["volume"], expected_volume, rel_tol=1e-6), geometry
    bounds = geometry["bounding_box"]
    assert all(math.isclose(bounds[key], value, abs_tol=1e-5)
               for key, value in [("xlen", 20), ("ylen", 20), ("zlen", 2)])
    for value in [result.step_path, result.stl_path]:
        assert Path(value).is_file() and Path(value).stat().st_size > 100
    import cadquery as cq
    shape = cq.importers.importStep(result.step_path).val()
    shape_check(shape, (20, 20, 2), expected_volume)
    assert not shape.Solids()[0].isInside((0, 0, 1))
    assert shape.Solids()[0].isInside((8, 0, 1))
    import trimesh
    mesh = trimesh.load(result.stl_path, force="mesh")
    assert len(mesh.vertices) > 0 and len(mesh.faces) > 0 and mesh.is_watertight
    assert math.isclose(abs(mesh.volume), expected_volume, rel_tol=0.02)
    return {"case": row["id"], "geometry": geometry,
            "scope": "original dataset washer through actual Executor subprocess, success-result protocol and STEP/STL; no generation pipeline or model-provider API"}


def machinome(source):
    project = ROOT / "machinome-project"
    project.mkdir()
    template = source / "packages/machinome/machinome/manager/templates/project/root/cadquery.py"
    model = project / "model.py"
    shutil.copyfile(template, model)
    (project / "pyproject.toml").write_text(
        '[tool.machinome]\nmodel = "model:DemoProject"\n', encoding="utf-8")
    spec = importlib.util.spec_from_file_location("renewed_machinome_model", model)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    provenance("machinome")
    node = module.DemoProject()
    rendered = node.render()
    node.validate(rendered)
    shape = node.shape_from_rendered(rendered)
    from machinome.engine import require_brep_engine
    from machinome.node.cadquery import workplane_shape
    engine = require_brep_engine("compat original DemoProject", "validate BREP adapter")
    assert engine.solid_count(shape) == 1
    expected_volume = 50 ** 3 - math.pi * 10 ** 2 * 50
    assert math.isclose(engine.solid_volume(shape), expected_volume, rel_tol=1e-6)
    lower, upper = engine.bounds(shape)
    assert all(math.isclose(v, e, abs_tol=1e-5)
               for v, e in zip((*lower, *upper), (-25, -25, 0, 25, 25, 50)))
    import cadquery as cq
    solid = cq.Shape.cast(shape).Solids()[0]
    shape_check(solid, (50, 50, 50), expected_volume)
    assert not solid.isInside((0, 0, 25)) and solid.isInside((20, 0, 25))
    brep = RESULTS / f"{VARIANT}-renewed-machinome.brep"
    stl = RESULTS / f"{VARIANT}-renewed-machinome.stl"
    engine.write_brep(shape, str(brep))
    engine.write_stl(shape, str(stl), 0.1, 0.1)
    restored = engine.read_brep(str(brep))
    assert engine.solid_count(restored) == 1
    assert math.isclose(engine.solid_volume(restored), expected_volume, rel_tol=1e-6)
    assert brep.stat().st_size > 100 and stl.stat().st_size > 100
    # Real adapter boundaries, independently from the unchanged demo's geometry.
    try:
        workplane_shape(cq.Workplane().newObject([]), engine)
    except ValueError as error:
        assert str(error) == "CadQuery render produced no shape"
    else:
        raise AssertionError("empty CQ stack accepted")
    multiple = cq.Workplane().newObject([rendered.val(), rendered.val().translate((100, 0, 0))])
    compound = workplane_shape(multiple, engine)
    assert engine.solid_count(compound) == 2
    assert math.isclose(engine.solid_volume(compound), 2 * expected_volume, rel_tol=1e-6)
    tests = native_tests(source / "packages/machinome", ["tests/test_brep_engine.py"],
                         "machinome-engine")
    return {"volume": expected_volume, "native_tests": tests,
            "scope": "unchanged DemoProject render, own node conversion and BREP engine write/read/STL; empty/multiple stack boundaries; CQ2.7/OCP<7.9 pin overrides explicit, no build123d/cq-editor/movie/studio claim"}


SCENARIOS = {95: freecad_kernel, 96: cadsmith, 97: machinome}


if __name__ == "__main__":
    faulthandler.enable()
    source = Path(sys.argv[2]).resolve()
    sys.path[:0] = [str(source), str(source / "src")]
    RESULTS.mkdir(parents=True, exist_ok=True)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    try:
        detail = SCENARIOS[int(sys.argv[1])](source)
        print(json.dumps({"id": int(sys.argv[1]), "status": "passed", **detail}))
    except Exception:
        traceback.print_exc()
        sys.exit(1)
