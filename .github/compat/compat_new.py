"""Pinned additional consumers, native tests and bounded real integration paths.

No import hooks or compatibility shims. Run each ID in its own process, selecting
CadQuery with the caller's PYTHONPATH. Consumer files remain unchanged.
"""

import faulthandler
import json
import math
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import traceback
import xml.etree.ElementTree as ET
import zipfile


def provenance():
    # Check only after the consumer naturally imported CQ, never preload it.
    module = sys.modules.get("cadquery")
    assert module is not None, "consumer did not import cadquery"
    actual = Path(module.__file__).resolve()
    expected = Path(os.environ["COMPAT_CQ_ROOT"]).resolve()
    assert actual.is_relative_to(expected), (actual, expected)
    print(json.dumps({"cadquery_source": str(actual)}), flush=True)


def solid(value):
    import cadquery as cq

    shape = value.val() if isinstance(value, cq.Workplane) else value
    assert isinstance(shape, cq.Shape), type(shape)
    assert shape.isValid() and shape.Solids()
    assert math.isfinite(shape.Volume()) and shape.Volume() > 0
    return shape


def step_roundtrip(value, output):
    import cadquery as cq

    shape = solid(value)
    cq.exporters.export(shape, str(output))
    assert output.stat().st_size > 100
    restored = solid(cq.importers.importStep(str(output)))
    assert math.isclose(shape.Volume(), restored.Volume(), rel_tol=1e-5)


def native(source, paths, *, child_only=False):
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    report_path = Path(os.environ.get("COMPAT_RESULT_DIR", str(Path.cwd()))) / (
        os.environ.get("CQ_COMPAT_VARIANT", "unknown") + "-native-tests.xml")
    bootstrap = f"""import json, os, sys, pytest
from pathlib import Path
code = pytest.main(sys.argv[1:])
cq = sys.modules.get('cadquery')
actual = str(Path(cq.__file__).resolve()) if cq is not None else None
expected = Path(os.environ['COMPAT_CQ_ROOT']).resolve()
if actual is not None:
    assert Path(actual).is_relative_to(expected), actual
elif not {child_only!r} and code == 0:
    raise AssertionError('Native suite did not load CadQuery in its pytest process')
record = {{
    'pytest_parent_cadquery_source': actual,
    'expected_cadquery_root': str(expected),
    'child_provenance': 'not directly observed; unmodified workers inherit PYTHONPATH',
    'pythonpath': os.environ.get('PYTHONPATH'),
}}
output = Path(os.environ['COMPAT_RESULT_DIR'])
variant = os.environ['CQ_COMPAT_VARIANT']
(output / (variant + '-native-provenance.json')).write_text(json.dumps(record, indent=2) + '\\n')
print(json.dumps(record), flush=True)
sys.exit(code)
"""
    result = subprocess.run(
        [sys.executable, "-c", bootstrap, "-ra", "--junitxml=" + str(report_path), *paths],
        cwd=source, env=env, timeout=560,
    )
    assert result.returncode == 0, f"native pytest exit {result.returncode}"
    report = ET.parse(report_path)
    suites = report.findall(".//testsuite")
    counts = {key: sum(int(s.attrib.get(key, 0)) for s in suites)
              for key in ["tests", "failures", "errors", "skipped"]}
    assert counts["tests"] > 0, counts
    return {"native": counts, "selection": paths}


def cli(source):
    return native(source, ["tests", "--ignore=tests/test_freecad.py"], child_only=True)


def pydantic(source):
    return native(source, ["tests"])


def tcqt(source):
    from tcqt import Assembly, Workplane

    provenance()
    part = Workplane("XY").box(5, 6, 7)
    solid(part)
    assembly = Assembly(name="two-parts")
    blue = assembly.add_material("0000FF")
    green = assembly.add_material("00FF00")
    assembly.add(part, name="blue", material=blue)
    assembly.add(part.translate((8, 0, 0)), name="green", material=green)
    ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
    for index in range(2):
        output = Path.cwd() / f"assembly-{index}.3mf"
        assembly.export(str(output))
        with zipfile.ZipFile(output) as archive:
            assert archive.testzip() is None
            model = ET.fromstring(archive.read("3D/3dmodel.model"))
            assert len(model.findall(".//m:triangle", ns)) > 0
            assert len(model.findall(".//m:vertex", ns)) > 0
            settings = ET.fromstring(archive.read("Metadata/model_settings.config"))
            extruders = {m.attrib["value"] for m in settings.iter("metadata")
                         if m.attrib.get("key") == "extruder"}
            assert {"1", "2"} <= extruders, extruders
    output = Path.cwd() / "assembly.step"
    assembly.export(str(output))
    assert output.stat().st_size > 100
    return {"scenario": "own Workplane, two-material repeated 3MF and delegated STEP"}


def queryabolt(source):
    collected = []
    runpy.run_path(str(source / "examples/simple.py"),
                   init_globals={"show_object": lambda obj, **kwargs: collected.append(obj)})
    provenance()
    assert len(collected) == 1
    step_roundtrip(collected[0], Path.cwd() / "fasteners.step")
    return {"scenario": "complete original simple.py fastener example and STEP roundtrip"}


def enclosure(source):
    collected = []
    runpy.run_path(str(source / "examples/01_panel_with_button.py"),
                   init_globals={"show_object": lambda obj, **kwargs: collected.append(obj)})
    provenance()
    assert len(collected) == 1
    assembly = collected[0]
    shape = solid(assembly.toCompound())
    assembly.export(str(Path.cwd() / "panel.step"))
    assert (Path.cwd() / "panel.step").stat().st_size > 100
    return {"scenario": "original two-button panel example and own assembly STEP",
            "solids": len(shape.Solids())}


def studio_child(source, kind):
    import importlib
    import cadquery as cq
    from cq_studio.reloader import Reloader

    provenance()
    # server.py imports CQ at module line 22, before constructing Reloader in
    # Server.__init__. Its preceding build123d import is deliberately not
    # instantiated here: this is the CQ model/reloader backend, not the viewer.
    # Match Server.__init__'s real default prefix exclusions. Do not manufacture
    # CQ-editor's cleanup policy or forcibly retain/preload lazy CQ modules.
    class Logger:
        def trace(self, *args, **kwargs):
            pass

    excluded = [Path(prefix) / sys.platlibdir /
                f"python{sys.version_info.major}.{sys.version_info.minor}"
                for prefix in {sys.prefix, sys.base_prefix}]
    reloader = Reloader(Logger(), excluded)
    work = Path.cwd()
    model_name = "studio_compat_model"
    body = "import cadquery as cq\nresult = cq.Workplane().box(2, 3, 4)\n"
    if kind == "dxf":
        body += ("cq.exporters.export(cq.Workplane().rect(4, 5), 'studio.dxf')\n"
                 "restored = cq.importers.importDXF('studio.dxf').wires().toPending().extrude(1)\n")
    elif kind == "vtp":
        body += "cq.exporters.export(result, 'studio.vtp')\n"
    (work / (model_name + ".py")).write_text(body)
    sys.path.insert(0, str(work))
    for index in range(3):
        importlib.invalidate_caches()
        model = importlib.import_module(model_name)
        shape = solid(model.result)
        assert math.isclose(shape.Volume(), 24, rel_tol=1e-6)
        if kind == "dxf":
            assert math.isclose(solid(model.restored).Volume(), 20, rel_tol=1e-6)
        if kind == "vtp":
            tree = ET.parse(work / "studio.vtp")
            assert tree.getroot().attrib["type"] == "PolyData"
            piece = tree.find(".//Piece")
            assert piece is not None and int(piece.attrib["NumberOfPoints"]) > 0
        del model
        reloader.unload()
        assert model_name not in sys.modules
        print(json.dumps({"kind": kind, "cycle": index + 1, "status": "passed"}), flush=True)


def studio(source):
    results = {}
    for kind in ["geometry", "dxf", "vtp"]:
        with tempfile.TemporaryDirectory(prefix="cq-studio-") as directory:
            result = subprocess.run(
                [sys.executable, "-X", "faulthandler", str(Path(__file__).resolve()),
                 "studio-child", str(source), kind], cwd=directory, timeout=160,
            )
            results[kind] = result.returncode
    assert all(code == 0 for code in results.values()), results
    return {"scenario": "own Reloader with Server prefix exclusions, three real cycles per path",
            "paths": results, "not_covered": "YACV/build123d viewer and Server event loop"}


def mcp(source):
    # These original tests import CQ only inside script_runner subprocesses.
    # Do not preload CQ in pytest or modify their script strings to manufacture proof.
    return native(source, ["tests/test_script_runner.py"], child_only=True)


SCENARIOS = {52: cli, 53: pydantic, 54: tcqt, 55: queryabolt,
             56: enclosure, 57: studio, 58: mcp}


if __name__ == "__main__":
    faulthandler.enable()
    if sys.argv[1] == "studio-child":
        studio_child(Path(sys.argv[2]).resolve(), sys.argv[3])
    else:
        project_id = int(sys.argv[1])
        source = Path(sys.argv[2]).resolve()
        sys.path[:0] = [str(source), str(source / "src")]
        try:
            detail = SCENARIOS[project_id](source)
            print(json.dumps({"id": project_id, "status": "passed", **detail}))
        except Exception:
            traceback.print_exc()
            sys.exit(1)
