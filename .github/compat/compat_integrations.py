"""Targeted original-consumer scenarios for the temporary compatibility CI."""

import importlib.util
import json
from pathlib import Path
import pickle
import subprocess
import sys
import tempfile
import traceback


BOX_SCRIPT = "import cadquery as cq\nresult = cq.Workplane('XY').box(2, 3, 4)\nshow_object(result)\n"


def load_file(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def nonempty(path):
    path = Path(path)
    assert path.is_file() and path.stat().st_size > 0, str(path)


def jupyter_pick(source):
    sys.path.insert(0, str(source))
    import cadquery as cq
    from jupyter_cadquery.tools import get_pick

    assembly = cq.Assembly(name="root").add(cq.Workplane().box(2, 3, 4), name="part")
    for _ in range(2):
        picked = get_pick(assembly, {"path": "/root", "name": "part"})
        assert picked is assembly.objects["part"]
        assert abs(picked.obj.val().Volume() - 24) < 1e-6
    return ["actual get_pick on CQ Assembly, twice; notebook/frontend not exercised"]


def freecad_cqgi(source):
    module = load_file(source / "freecad/cadquery2workbench/cadquery_model.py", "compat_freecad_cqgi")
    for _ in range(2):
        result = module.CQ_Model(BOX_SCRIPT).build()
        assert result.success, repr(result.exception)
        assert len(result.results) == 1
        assert abs(result.results[0].shape.val().Volume() - 24) < 1e-6
    return ["workbench's original CQ_Model.build twice; FreeCAD GUI/transfer not exercised"]


def gui_mesh(source):
    target = source / "assets/python/cq_process.py"
    with tempfile.TemporaryDirectory() as directory:
        script = Path(directory) / "model.py"
        script.write_text(BOX_SCRIPT)
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, str(target), "--file=" + str(script), "--outputFormat=threeJS"],
                capture_output=True, text=True, timeout=90, check=True,
            )
            data = json.loads(result.stdout)
            assert data["error"] in ("None", "")
            assert len(data["geometry"]) == 1
            mesh = data["geometry"][0]
            assert mesh["vertices"] and mesh["faces"]
    return ["original CQGI subprocess to mesh JSON twice; native GUI not exercised"]


def blendquery_mesh(source):
    # parse.py is the add-on's actual subprocess, independent of Blender's UI.
    # setup_venv creates an empty venv path but does not install packages or
    # replace this interpreter. The active comparison environment supplies CQ.
    sys.path.insert(0, str(source))
    import interop_types  # noqa: F401, needed to deserialize the original protocol

    script = "result = cq.Workplane('XY').box(2, 3, 4)"
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, str(source / "parse.py")],
            input=pickle.dumps(script), capture_output=True, timeout=120,
        )
        # Only deserialize this reviewed worker's response, never external data.
        value = pickle.loads(result.stdout)
        assert result.returncode == 0, repr(value)
        assert isinstance(value, list) and len(value) == 1
        assert value[0].vertices and value[0].faces
        assert value[0].name == "result"
    return ["original parse.py pickle IPC to tessellation twice; upgrade experiment, CQ pin 2.3.1 not honored; Blender host not exercised"]


def web_preview(source):
    import cadquery as cq

    module = load_file(source / "cadquery/Preview.py", "compat_web_preview")
    for _ in range(2):
        mesh, error = module.preview(cq.Workplane().box(2, 3, 4))
        assert error is None, error
        assert mesh["objectCount"] == 1
        assert mesh["vertices"] and mesh["faces"]
        assert len(mesh["vertices"]) % 3 == len(mesh["faces"]) % 3 == 0
    return ["original preview tessellation twice; Flask endpoints not exercised"]


def browser_stl(source):
    module = load_file(source / "backend/main.py", "compat_browser_backend")
    for script in (BOX_SCRIPT, "result = cq.Assembly().add(cq.Workplane().box(2, 3, 4), name='part')"):
        for _ in range(2):
            data = module._render_stl(script)
            assert isinstance(data, bytes) and len(data) > 84
            # The backend requests binary STL. Check the actual triangle records.
            count = int.from_bytes(data[80:84], "little")
            assert count > 0 and len(data) == 84 + 50 * count
    return ["original browser STL backend: Workplane and Assembly, each twice; HTTP not exercised"]


def opencode_exports(source):
    import cadquery as cq

    module = load_file(source / ".opencode/plugins/opencode-cad/cad/cad_runner.py", "compat_opencode_runner")
    with tempfile.TemporaryDirectory() as directory:
        for _ in range(2):
            paths = module.make_exports(cq.Workplane().box(2, 3, 4).val(), cq, Path(directory))
            assert {Path(p).suffix for p in paths} == {".step", ".stl"}
            for path in paths:
                nonempty(path)
            model = cq.importers.importStep(next(p for p in paths if p.endswith(".step")))
            assert abs(model.val().Volume() - 24) < 1e-6
    return ["original make_exports STEP/STL twice and STEP volume roundtrip; CLI rendering not exercised"]


def sphinx_exports(source):
    sys.path.insert(0, str(source))
    from sphinxcontrib.cadquery.cqgi import Cqgi, SvgExporter, VtkJsonExporter

    for _ in range(2):
        result = Cqgi.cqgi_parse(BOX_SCRIPT)
        svg = SvgExporter(result, "result")()
        assert "<svg" in svg
        vtk = json.loads(VtkJsonExporter(result, "result")())
        assert isinstance(vtk, list) and len(vtk) == 1
        assert vtk[0], "empty VTK assembly entry"
    return ["original CQGI SVG and VTK JSON exporters twice; Sphinx directive rendering not exercised"]


def contrib_exports(source):
    module = load_file(source / "mcp-server/cadquery_core.py", "compat_contrib_core")
    with tempfile.TemporaryDirectory() as directory:
        for _ in range(2):
            for image_format in ("svg", "png"):
                result = module.handle_render(BOX_SCRIPT, width=64, height=64, image_format=image_format)
                assert result.error is None, result.error
                assert len(result.images) == 1 and result.images[0]
                if image_format == "svg":
                    assert b"<svg" in result.images[0]
                else:
                    assert result.images[0].startswith(b"\x89PNG\r\n\x1a\n")
            cases = [
                (BOX_SCRIPT, "box.step"),
                (BOX_SCRIPT, "box.stl"),
                ("import cadquery as cq\nresult = cq.Workplane('XY').rect(2,3)\nshow_object(result)", "outline.dxf"),
                ("import cadquery as cq\nresult = cq.Assembly().add(cq.Workplane().box(2,3,4), name='part')\nshow_object(result)", "assembly.step"),
            ]
            for script, name in cases:
                path = Path(directory) / name
                result = module.handle_export(script, str(path))
                assert result.error is None, result.error
                nonempty(path)
    return ["original core SVG/PNG plus STEP/STL/DXF and Assembly STEP twice; MCP transport not exercised"]


def tessellate_tests(source):
    # Reuse original project assertions, not a replacement converter implementation.
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--override-ini=addopts=",
         "tests/test_cadquery.py::TestWorkplane::test_box",
         "tests/test_cadquery.py::TestWorkplane::test_boxes",
         "tests/test_cadquery.py::TestWorkplane::test_faces",
         "tests/test_cadquery.py::TestVector::test_vector"],
        cwd=source, text=True, capture_output=True, timeout=180,
    )
    print(result.stdout)
    print(result.stderr, file=sys.stderr)
    assert result.returncode == 0, f"original CQ converter tests exited {result.returncode}"
    assert "4 passed" in result.stdout, "selected tests did not all execute"
    return ["four original converter tests: Workplane, compound, faces, vector"]


SCENARIOS = {
    12: jupyter_pick,
    14: freecad_cqgi,
    15: gui_mesh,
    16: blendquery_mesh,
    18: web_preview,
    19: browser_stl,
    40: opencode_exports,
    44: sphinx_exports,
    49: contrib_exports,
    51: tessellate_tests,
}


def main():
    entry_id = int(sys.argv[1])
    source = Path(sys.argv[2]).resolve()
    try:
        observations = SCENARIOS[entry_id](source)
    except Exception as error:
        traceback.print_exc()
        print(json.dumps({"id": entry_id, "status": "failed", "error": f"{type(error).__name__}: {error}"}))
        return 1
    print(json.dumps({"id": entry_id, "status": "passed", "scenarios": observations}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
