"""Bounded pinned downstream routes, without source edits or host/cloud bypasses."""

import ast
import faulthandler
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import traceback
import xml.etree.ElementTree as ET
import zipfile


def provenance(source, package, require_cq=True):
    consumer_file = Path(sys.modules[package].__file__).resolve()
    assert consumer_file.is_relative_to(source), (consumer_file, source)
    cq = sys.modules.get("cadquery")
    actual = Path(cq.__file__).resolve() if cq is not None else None
    if require_cq:
        assert actual is not None, "Selected geometry route did not load CadQuery"
    if actual is not None:
        expected = Path(os.environ["COMPAT_CQ_ROOT"]).resolve()
        assert actual.is_relative_to(expected), (actual, expected)
    record = {"consumer_source": str(consumer_file),
              "cadquery_source": str(actual) if actual is not None else None}
    print(json.dumps(record), flush=True)
    return record


def valid_shape(shape):
    assert shape.isValid(), "Invalid consumer geometry"
    assert shape.Solids(), "No consumer solid"
    assert math.isfinite(shape.Volume()) and shape.Volume() > 0
    return shape


def step_roundtrip(shape, path):
    cq = sys.modules["cadquery"]
    valid_shape(shape)
    cq.exporters.export(shape, str(path))
    assert path.stat().st_size > 100
    text = path.read_text(errors="replace")
    assert "ISO-10303-21;" in text and "END-ISO-10303-21;" in text
    restored = valid_shape(cq.importers.importStep(str(path)).val())
    assert math.isclose(shape.Volume(), restored.Volume(), rel_tol=1e-5)
    return str(path)


def simpleviewer(source):
    # This consumer is deliberately CQ-lazy. Geometry fixtures are distinct
    # from its natural startup statement, which must not force a CQ import.
    import cadquery_simpleviewer
    from cadquery_simpleviewer.adapters import get_adapter
    from cadquery_simpleviewer.viewer import _build_traces
    import cadquery as cq

    provenance(source, "cadquery_simpleviewer")
    box = cq.Workplane("XY").box(5, 3, 2)
    edge = cq.Edge.makeLine(cq.Vector(0, 0, 0), cq.Vector(5, 0, 0))
    wire = cq.Wire.makePolygon(
        [cq.Vector(-2, -1, 0), cq.Vector(2, -1, 0),
         cq.Vector(2, 1, 0), cq.Vector(-2, 1, 0)], close=True,
    )
    traces, xs, ys, zs, local_axes = _build_traces(
        [box, edge, wire], None, None, 1.0, 0.1, None, None,
    )
    assert len(traces) == 6 and local_axes == [1, 2, 3]
    assert [trace.type for trace in traces] == [
        "mesh3d", "mesh3d", "mesh3d", "mesh3d", "scatter3d", "scatter3d",
    ]
    assert traces[4].mode == "lines" and traces[5].mode == "lines"
    assert all(math.isfinite(float(v)) for values in (xs, ys, zs) for v in values)
    assert min(xs) <= -2.5 and max(xs) >= 5
    mesh = traces[0]
    assert len(mesh.x) == len(mesh.y) == len(mesh.z) > 0
    assert len(mesh.i) == len(mesh.j) == len(mesh.k) > 0
    assert all(0 <= int(i) < len(mesh.x)
               for indexes in (mesh.i, mesh.j, mesh.k) for i in indexes)
    pending = cq.Workplane("XY").rect(4, 2)
    adapter = get_adapter(pending)
    assert adapter.__name__ == "cadquery_simpleviewer.adapters.cadquery_adapter"
    assert adapter.is_pending_wire(pending)
    pending_traces, *_ = _build_traces([pending], None, None, 1.0, 0.1, None, None)
    assert len(pending_traces) == 1 and pending_traces[0].type == "scatter3d"
    location = cq.Location((1, 2, 3))
    location_traces, *_, location_axes = _build_traces(
        [location], None, None, 1.0, 0.1, None, None,
    )
    assert len(location_traces) == 3 and location_axes == [0, 1, 2]
    assert all(t.type == "mesh3d" and t.opacity == 1.0 for t in location_traces)
    assert all(math.isclose(float(t.x[0]), 1.0) for t in location_traces)
    output = Path.cwd() / "simpleviewer-traces.json"
    import plotly.graph_objects as go
    payload = go.Figure(data=traces + pending_traces + location_traces).to_json()
    output.write_text(payload)
    assert len(json.loads(output.read_text())["data"]) == 10
    return {"scope": "Headless own adapter/traces only; no browser/widget/native-suite claim",
            "trace_count": 10, "mesh_triangles": len(mesh.i), "json": str(output)}


def clocks(source):
    from clocks.demos import get_gear_demo
    from clocks.types import GearStyle

    provenance(source, "clocks.demos")
    # Both arguments are necessary: one_gear alone still builds every style.
    model = get_gear_demo(just_style=GearStyle.ARCS, one_gear=True)
    shape = valid_shape(model.val())
    bounds = shape.BoundingBox()
    measured = [bounds.xlen, bounds.ylen, bounds.zlen]
    assert all(math.isfinite(value) and value > 0 for value in measured)
    output = step_roundtrip(shape, Path.cwd() / "clock-demo-arcs-gear.step")
    return {"scenario": "Own demo second arbor, ARCS style, one_gear=True",
            "volume": shape.Volume(), "solid_count": len(shape.Solids()),
            "bounds": measured, "step": output}


def cloudcad(source):
    sys.path.insert(0, str(source / "backend"))
    from app.models.parametric_model import ParametricModel
    from app.cad.parametric_feature_functions import create_cylinder, circular_cut

    provenance(source, "app.models.parametric_model")
    provenance(source, "app.cad.parametric_feature_functions")
    model = ParametricModel()
    model.add_parameter("radius", 3.0)
    model.add_parameter("height", 10.0)
    cylinder = model.add_feature(create_cylinder, "radius", "height")
    cut = model.add_feature(circular_cut, 1.0, 2.0)
    model.rebuild()

    def check(radius, has_cut):
        shape = valid_shape(model.get_model().val())
        assert len(shape.Solids()) == 1
        expected = math.pi * (radius ** 2 * 10.0 - (2.0 if has_cut else 0.0))
        assert math.isclose(shape.Volume(), expected, rel_tol=1e-6)
        bounds = shape.BoundingBox()
        assert math.isclose(bounds.xlen, radius * 2, abs_tol=1e-6)
        assert math.isclose(bounds.ylen, radius * 2, abs_tol=1e-6)
        assert math.isclose(bounds.zlen, 10.0, abs_tol=1e-6)
        return shape

    initial = check(3.0, True).Volume()
    model.update_parameter("radius", 4.0)
    changed = check(4.0, True).Volume()
    assert changed > initial
    model.set_feature_visibility(cut, False)
    hidden = check(4.0, False).Volume()
    assert hidden > changed
    model.set_feature_visibility(cut, True)
    model.set_feature_color(cylinder, (0.2, 0.4, 0.6))
    final = check(4.0, True)
    assert model.get_features() == [
        {"id": cylinder, "visible": True, "color": (0.2, 0.4, 0.6)},
        {"id": cut, "visible": True, "color": (0.7, 0.7, 0.7)},
    ]
    output = step_roundtrip(final, Path.cwd() / "cloudcad-parametric-cut.step")
    return {"scope": "Own local model state/rebuild only; no HTTP/server coverage",
            "initial_volume": initial, "updated_volume": changed,
            "hidden_cut_volume": hidden, "step": output}


def mcp_local(source):
    # Configuration only, not a credential and not an upload target.
    os.environ["GCS_BUCKET"] = "compat-local-no-upload"
    from mcp_cadquery_server.server import validate_script, _build_export_script

    provenance(source, "mcp_cadquery_server.server", require_cq=False)
    # Read the real fixture without executing its top-level generate_3mf call.
    tree = ast.parse((source / "tests/test_server.py").read_text())
    scripts = [ast.literal_eval(node.value) for node in tree.body
               if isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Name) and target.id == "test_script"
                       for target in node.targets)]
    assert len(scripts) == 1
    script = scripts[0]
    assert 'box(10, 10, 10).faces(">Z").hole(3)' in script
    validate_script(script)
    for rejected in ("import os", "open('no-file', 'w')"):
        try:
            validate_script(rejected)
        except ValueError:
            pass
        else:
            raise AssertionError("Own validator did not reject prohibited fixture")
    output = Path.cwd() / "mcp-through-hole.3mf"
    tail = _build_export_script("result", "3MF", output.as_posix(),
                                (Path.cwd() / "unused.stl").as_posix(), {})
    # This wrapper observes the real generated child, without changing its
    # validator, exporter, CAD model or external service functions.
    observation = '''
import json, os
from pathlib import Path
actual = Path(cq.__file__).resolve()
expected = Path(os.environ['COMPAT_CQ_ROOT']).resolve()
assert actual.is_relative_to(expected), (actual, expected)
shape = result.val()
assert shape.isValid() and len(shape.Solids()) == 1
assert 0 < shape.Volume() < 1000
record = {'cadquery_source': str(actual), 'volume': shape.Volume()}
Path(os.environ['COMPAT_CHILD_PROVENANCE']).write_text(json.dumps(record))
print(json.dumps(record), flush=True)
'''
    child_source = Path.cwd() / "mcp-local-export.py"
    child_source.write_text(script + "\n" + tail + "\n" + observation)
    record_path = Path.cwd() / "mcp-child-provenance.json"
    child = subprocess.run(
        [sys.executable, "-X", "faulthandler", str(child_source)],
        cwd=Path.cwd(), timeout=180,
        env=dict(os.environ, COMPAT_CHILD_PROVENANCE=str(record_path)),
    )
    assert child.returncode == 0, child.returncode
    record = json.loads(record_path.read_text())
    assert output.stat().st_size > 100
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        members = [name for name in archive.namelist() if name.endswith(".model")]
        assert members
        triangle_count = 0
        for name in members:
            root = ET.fromstring(archive.read(name))
            for mesh in root.findall(".//{*}mesh"):
                vertices = mesh.findall("{*}vertices/{*}vertex")
                triangles = mesh.findall("{*}triangles/{*}triangle")
                assert vertices and triangles
                for vertex in vertices:
                    assert all(math.isfinite(float(vertex.get(axis))) for axis in ("x", "y", "z"))
                for triangle in triangles:
                    assert all(0 <= int(triangle.get(key)) < len(vertices)
                               for key in ("v1", "v2", "v3"))
                triangle_count += len(triangles)
        assert triangle_count > 0
    return {"scope": "Own local validator/export-template child only; no cloud/upload/MCP/PNG claim",
            "child_provenance": record, "triangles": triangle_count,
            "three_mf": str(output)}


def main():
    faulthandler.enable()
    project_id = int(sys.argv[1])
    source = Path(sys.argv[2]).resolve()
    folder = Path(os.environ["COMPAT_RESULT_DIR"])
    record_path = folder / (os.environ["CQ_COMPAT_VARIANT"] + "-scenario.json")
    if project_id == 98:
        result = {"id": 98, "status": "blocked",
                  "reason": "Own isolated child strips paired PYTHONPATH. No whitelist/resource/thread-cap bypass authorized."}
        record_path.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result), flush=True)
        return 2
    try:
        detail = {99: simpleviewer, 100: clocks, 109: cloudcad, 110: mcp_local}[project_id](source)
    except Exception:
        traceback.print_exc()
        print(json.dumps({"id": project_id, "status": "failed"}), flush=True)
        return 1
    result = {"id": project_id, "status": "passed", **detail}
    record_path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
