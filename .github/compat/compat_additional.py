"""Source-pinned downstream factories, native tests and host-free adapters.

This is not a full FreeCAD/KiCad/Qt application or CAM simulation gate.
"""

from dataclasses import replace
import faulthandler
import importlib.util
import json
import math
import os
from pathlib import Path
import runpy
import sys
import traceback
import xml.etree.ElementTree as ET

from compat_major_ai import command
from compat_renewed_b import provenance


ROOT = Path.cwd()
RESULTS = Path(os.environ.get("COMPAT_RESULT_DIR", str(ROOT))).resolve()
VARIANT = os.environ.get("CQ_COMPAT_VARIANT", "unknown")


def load_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def artifact(name, extension):
    return RESULTS / f"{VARIANT}-additional-{name}.{extension}"


def valid_shape(shape):
    assert shape.isValid() and shape.Solids(), "invalid or empty geometry"
    assert math.isfinite(shape.Volume()) and shape.Volume() > 0
    box = shape.BoundingBox()
    assert all(math.isfinite(v) and v > 0 for v in (box.xlen, box.ylen, box.zlen))
    return box


def roundtrip(shape, name):
    import cadquery as cq
    valid_shape(shape)
    path = artifact(name, "step")
    cq.exporters.export(shape, str(path), exportType="STEP")
    assert path.is_file() and path.stat().st_size > 100
    restored = cq.importers.importStep(str(path)).val()
    valid_shape(restored)
    assert len(restored.Solids()) == len(shape.Solids())
    assert math.isclose(restored.Volume(), shape.Volume(), rel_tol=1e-6)
    return {"solids": len(shape.Solids()), "volume": shape.Volume(), "step": str(path)}


def selected_tests(source, nodes, name, expected):
    """Run original tests unchanged; observe their own CQ import afterwards."""
    report = RESULTS / f"{VARIANT}-native-tests.xml"
    record = artifact(name + "-native-provenance", "json")
    args = ["-q", "-o", "addopts=", *nodes, f"--junitxml={report}"]
    bootstrap = (
        "import json,os,pathlib,sys; import pytest; "
        f"code=pytest.main({args!r}); "
        "module=sys.modules.get('cadquery'); "
        "assert module is not None, 'native tests did not import cadquery'; "
        "actual=pathlib.Path(module.__file__).resolve(); "
        "expected=pathlib.Path(os.environ['COMPAT_CQ_ROOT']).resolve(); "
        "assert actual.is_relative_to(expected), str(actual); "
        f"pathlib.Path({str(record)!r}).write_text(json.dumps({{'cadquery_source':str(actual),'worker_pid':os.getpid()}})); "
        "sys.exit(code)"
    )
    previous = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = os.pathsep.join([str(source / "src"), previous])
    try:
        result = command([sys.executable, "-c", bootstrap], name, source, timeout=600)
    finally:
        os.environ["PYTHONPATH"] = previous
    assert report.is_file(), f"missing JUnit report: {report}"
    cases = list(ET.parse(report).getroot().iter("testcase"))
    failed = [c.attrib for c in cases if c.find("failure") is not None or c.find("error") is not None]
    skipped = [c.attrib for c in cases if c.find("skipped") is not None]
    summary = {"tests": len(cases), "failed": failed, "skipped": skipped, "report": str(report)}
    print(json.dumps({"native_tests": summary}), flush=True)
    assert result.returncode == 0 and len(cases) == expected and not failed and not skipped, summary
    assert record.is_file(), "native CQ provenance missing"
    return summary


def camlib(source):
    sys.path.insert(0, str(source / "python"))
    module = load_path("additional_camlib_example", source / "examples/cadquery_job.py")
    job = module.build_job(width=10, revision=1)
    provenance("additional-camlib")
    assert job.id == "cadquery-pocket" and job.revision == "1"
    assert len(job.setups) == 1 and len(job.setups[0].sources) == 1
    assert job.setups[0].id == "top" and len(job.setups[0].operations) == 2
    return {"scope": "unchanged build_job pocket floor adapter, CAD snapshot and Job construction only; no compile, toolpath, simulation or lifecycle claim"}


def mesh2cad(source):
    tests = selected_tests(source, [
        "tests/test_cadquery.py::test_script_reproduces_part[plate_with_hole-0.0]",
        "tests/test_cadquery.py::test_edit_parameter_changes_part",
    ], "mesh2cad", 2)
    return {"native_tests": tests, "scope": "two original clean plate/hole reconstruction and parameter-edit tests, original distance/volume oracles; OCP<8 pin explicitly overridden"}


def absorber(source):
    tests = selected_tests(source, [
        "tests/test_geometry.py::test_absorber_build_creates_solids",
        "tests/test_geometry.py::test_absorber_export_custom_filename",
    ], "absorber", 2)
    return {"native_tests": tests, "scope": "two original block-tile and triangle STL geometry tests; zero skipped tests required"}


def stencilforge(source):
    os.environ["XDG_CONFIG_HOME"] = str(ROOT / "stencil-config")
    from stencilforge.config import StencilConfig
    from stencilforge.pipeline.cadquery import export_cadquery_stl
    from shapely.geometry import Polygon
    config = replace(StencilConfig.load_default(source), model_backend="cadquery")
    # Minimal real stencil input: aperture holes, not a substituted CQ model.
    stencil = Polygon([(0, 0), (20, 0), (20, 12), (0, 12)], holes=[
        [(3, 3), (5, 3), (5, 5), (3, 5)],
        [(10, 3), (13, 3), (13, 5), (10, 5)],
        [(3, 8), (6, 8), (6, 10), (3, 10)],
    ])
    path = artifact("stencilforge", "stl")
    export_cadquery_stl(stencil, None, None, path, config)
    provenance("additional-stencilforge")
    assert path.is_file() and path.stat().st_size > 100
    import trimesh
    mesh = trimesh.load(path, force="mesh")
    assert mesh.is_watertight and len(mesh.faces) > 0
    expected = stencil.area * config.effective_thickness_mm
    assert math.isclose(abs(mesh.volume), expected, rel_tol=0.01)
    assert all(math.isclose(a, b, abs_tol=1e-4) for a, b in zip(
        mesh.extents, (20, 12, config.effective_thickness_mm)))
    return {"volume": abs(mesh.volume), "scope": "actual exporter with default config and explicit three-aperture polygon; no Gerber parsing, locator branches, Qt or GUI claim; CQ2.7 pin override"}


def kicad_tools(source):
    from kicad_tools.modelgen.families import FAMILIES, build_body
    from kicad_tools.modelgen.cadquery_backend import build_shapes, write_step
    spec = build_body(FAMILIES["gullwing"], None,
                      {"pins": 8, "pitch": 1.27, "L": 4.9, "W": 3.9, "E": 6.0})
    shapes = build_shapes(spec)
    provenance("additional-kicad-tools")
    assert shapes and all(material for material, shape in shapes)
    import cadquery as cq
    compound = cq.Compound.makeCompound([shape.val() for material, shape in shapes])
    box = valid_shape(compound)
    extent = spec.extent()
    assert extent is not None
    expected = (extent.width, extent.height, spec.height())
    assert all(math.isclose(a, b, abs_tol=1e-5) for a, b in zip(
        (box.xlen, box.ylen, box.zmax), expected)), (box, expected)
    path = artifact("kicad-gullwing", "step")
    write_step(spec, path, ["Original eight-pin gullwing family backend gate"])
    restored = cq.importers.importStep(str(path)).val()
    valid_shape(restored)
    assert math.isclose(restored.Volume(), compound.Volume(), rel_tol=1e-6)
    assert len(restored.Solids()) == len(compound.Solids())
    return {"materials": [material for material, shape in shapes], "solids": len(compound.Solids()),
            "scope": "own gullwing family factory and real build_shapes/write_step optional backend; no KiCad host, fallback-only tests or board pipeline claim"}


def storage_grid(source):
    from dovetailstoragegrid import DovetailStorageGrid
    grid = DovetailStorageGrid()
    tray = grid.basic_tray(x=1, y=1, wall_thickness=0.8)
    provenance("additional-storage-grid")
    shape = tray.val()
    box = valid_shape(shape)
    bounds = grid.bounding_volume(1, 1).val().BoundingBox()
    for lo, hi in [("xmin", "xmax"), ("ymin", "ymax"), ("zmin", "zmax")]:
        assert getattr(box, lo) >= getattr(bounds, lo) - 1e-5
        assert getattr(box, hi) <= getattr(bounds, hi) + 1e-5
    assert shape.Volume() < grid.nominal_volume(1, 1).val().Volume()
    return {**roundtrip(shape, "storage-tray"), "scope": "own one-cell normal-print tray with four dovetails and0.8mm shell; own bounding_volume containment"}


def din_enclosure(source):
    # This unchanged config already calls generate_enclosure once at its end.
    result = runpy.run_path(str(source / "dual/config.py"), run_name="__main__")
    provenance("additional-din-enclosure")
    config = result["config"]
    import cadquery as cq
    outcomes = []
    for suffix in ["assbly", "compound"]:
        path = ROOT / f"din_enclosure_{config.CONFIG_NAME}_{suffix}.step"
        assert path.is_file() and path.stat().st_size > 100
        shape = cq.importers.importStep(str(path)).val()
        valid_shape(shape)
        outcomes.append({"name": suffix, "solids": len(shape.Solids()), "volume": shape.Volume()})
    assert outcomes[0]["solids"] == outcomes[1]["solids"]
    assert math.isclose(outcomes[0]["volume"], outcomes[1]["volume"], rel_tol=1e-6)
    return {"outputs": outcomes, "scope": "unchanged dual config, complete original enclosure and both STEP exports; original show() uses config's own no-op show_object fallback, no GUI; font/native failures remain failures, separately bounded"}


def oasis(source):
    module = load_path("additional_oasis_honeycomb", source / "cadquery/honeycomb.py")
    provenance("additional-oasis")
    solids, empty = [], 0
    for location, tile in module.gen_honeycombed_circle(6, 2, 1, (0, 0, 0), 10):
        parts = tile.solids().vals()
        if not parts:
            empty += 1
            continue
        for shape in parts:
            box = valid_shape(shape)
            assert box.xmin >= -10.00001 and box.ymin >= -10.00001
            assert box.xmax <= 10.00001 and box.ymax <= 10.00001
            assert box.zmin >= -0.00001 and box.zmax <= 2.00001
            solids.append(shape)
    assert solids
    import cadquery as cq
    compound = cq.Compound.makeCompound(solids)
    assert compound.Volume() < math.pi * 10 ** 2 * 2
    return {**roundtrip(compound, "oasis-honeycomb"), "empty_intersections": empty,
            "scope": "own small honeycomb circle generator, clipped boundary tiles and STEP compound; no terrarium controller or complete blower model claim"}


def freecad_serializer(source):
    from freecad_ocp_viewer.show import get_brep
    # Inputs exercise the bridge contract, not a claimed consumer-owned model.
    import cadquery as cq
    workplane = cq.Workplane("XY").box(8, 6, 4).faces(">Z").workplane().hole(2)
    assembly = cq.Assembly(workplane, name="first").add(
        workplane.translate((12, 0, 0)), name="second")
    outcomes = []
    for name, obj, expected in [("workplane", workplane, workplane.val()),
                                ("assembly", assembly, assembly.toCompound())]:
        stream = get_brep(obj)
        provenance("additional-freecad-serializer")
        data = stream.getvalue()
        assert len(data) > 100 and b"CASCADE" in data[:200]
        path = artifact("freecad-serializer-" + name, "brep")
        path.write_bytes(data)
        restored = cq.Shape.importBrep(str(path))
        valid_shape(restored)
        assert len(restored.Solids()) == len(expected.Solids())
        assert math.isclose(restored.Volume(), expected.Volume(), rel_tol=1e-6)
        outcomes.append({"case": name, "bytes": len(data), "solids": len(restored.Solids())})
    return {"cases": outcomes, "host_status": "blocked-unprovisioned",
            "scope": "actual module-dispatch and CQ Workplane/Assembly BREP serializer only; FreeCAD/FreeCADGui/Part/PySide6 host, load_file, reload and document replacement explicitly untested"}


SCENARIOS = {111: camlib, 112: mesh2cad, 113: absorber, 114: stencilforge,
             115: kicad_tools, 116: storage_grid, 117: din_enclosure,
             118: oasis, 119: freecad_serializer}


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
