"""Bounded real downstream entrypoints. Does not patch or preload consumers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap


SOURCE = Path(sys.argv[2]).resolve()
RESULT = Path(os.environ["COMPAT_RESULT_DIR"]).resolve()
VARIANT = os.environ["CQ_COMPAT_VARIANT"]
RESULT.mkdir(parents=True, exist_ok=True)


def provenance():
    cq = sys.modules.get("cadquery")
    assert cq is not None, "consumer did not load CadQuery"
    path = Path(cq.__file__).resolve()
    path.relative_to(Path(os.environ["COMPAT_CQ_ROOT"]).resolve())
    (RESULT / (VARIANT + "-consumer-provenance.json")).write_text(
        json.dumps({"cadquery": str(path)}) + "\n"
    )


def soma():
    sys.path.insert(0, str(SOURCE / "python"))
    from soma.build import run_model
    from soma.cli import _export_one

    provenance()
    model = Path.cwd() / "soma-model"
    model.mkdir()
    (model / "model.py").write_text(
        "import cadquery as cq\nfrom soma import add_part\n"
        "add_part('body', cq.Workplane('XY').box(4, 5, 6))\n"
    )
    for i in range(2):
        parts = run_model(model, "model.py")
        assert len(parts) == 1
        shape = parts[0]["shape"]
        assert abs(shape.Volume() - 120) < 1e-6
        for fmt in ("step", "dxf"):
            path = model / (str(i) + "." + fmt)
            _export_one(shape, fmt, path)
            assert path.stat().st_size > 100
    import ezdxf
    import cadquery as cq
    assert len(ezdxf.readfile(model / "1.dxf").modelspace()) > 0
    assert abs(cq.importers.importStep(str(model / "1.step")).val().Volume() - 120) < 1e-5
    print("SOMA: repeated original run_model, registry, own STEP/DXF exporters and readback passed")


def repl():
    sys.path.insert(0, str(SOURCE / "src"))
    import cq_repl.main as app

    provenance()
    work = Path.cwd() / "repl-model"
    work.mkdir()
    sys.path.insert(0, str(work))
    (work / "cqcompat_model.py").write_text(
        "from pathlib import Path\nimport cadquery as cq\n"
        "p = Path('executions.txt')\n"
        "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n"
        "result = cq.Workplane('XY').box(4, 5, 6)\nresult.label = 'body'\n"
        "cq.exporters.export(result, 'model.vtp', exportType='VTP')\n"
        "cq.exporters.export(cq.Workplane('XY').rect(4, 5), 'model.dxf', exportType='DXF')\n"
    )
    old_stdin = sys.stdin
    read_fd, write_fd = os.pipe()
    callback = app.replTimerCallback()
    app.render_window.AddRenderer(app.renderer)
    app.render_window.SetOffScreenRendering(1)
    try:
        sys.stdin = os.fdopen(read_fd, "r")
        for _ in range(2):
            for line in ("import cqcompat_model as model\n", "show_object(model.result)\n"):
                os.write(write_fd, line.encode())
                callback.execute(None, None)
            assert "body" in app.display_objects
            assert app.renderer.GetActors().GetNumberOfItems() > 0
        assert Path("executions.txt").read_text() == "2"
        assert app.model.result.val().Volume() > 0
        assert Path("model.vtp").stat().st_size > 100
        import ezdxf
        assert len(ezdxf.readfile("model.dxf").modelspace()) > 0
    finally:
        os.close(write_fd)
        sys.stdin.close()
        sys.stdin = old_stdin
        app.render_window.Finalize()
    print("cq-repl: original stdin callback, module eviction/reimport, repeated DXF/VTP and viewer updates passed")


def partspec():
    sys.path.insert(0, str(SOURCE / "src"))
    from partspec.engines.pycad import PyCADSource, build, invalidate_model_modules
    from partspec.backend import BuildError

    model = Path.cwd() / "partspec-model"
    model.mkdir()
    path = model / "model.py"
    path.write_text(
        "from pathlib import Path\nimport cadquery as cq\n"
        "def make_part():\n"
        "    out = Path(__file__).parent\n"
        "    shape = cq.Workplane('XY').box(4, 5, 6)\n"
        "    cq.exporters.export(shape, str(out / 'model.vtp'), exportType='VTP')\n"
        "    cq.exporters.export(cq.Workplane('XY').rect(4, 5), str(out / 'model.dxf'), exportType='DXF')\n"
        "    return shape\n"
    )
    for _ in range(2):
        invalidate_model_modules(path)
        shape = build(PyCADSource(path=path, engine="cadquery"), timeout_s=90)
        assert not isinstance(shape, BuildError), repr(shape)
        assert abs(shape.volume - 120) < 1e-6
        provenance()
    import ezdxf
    assert len(ezdxf.readfile(model / "model.dxf").modelspace()) > 0
    assert (model / "model.vtp").stat().st_size > 100
    print("partspec: original PyCAD build/adopt and invalidate_model_modules, repeated DXF/VTP passed")


def faktory():
    # Match the project's native renderer import layout. No CadQuery import here.
    sys.path[:0] = [str(SOURCE), str(SOURCE / "renderer")]
    import pytest

    code = pytest.main([
        "-q", str(SOURCE / "renderer/tests/test_worker.py"),
        "--junitxml=" + str(RESULT / (VARIANT + "-native-tests.xml")),
        "-p", "no:cacheprovider",
    ])
    if "cadquery" in sys.modules:
        provenance()
    elif code == 0:
        raise AssertionError("native worker tests passed without loading CadQuery")
    raise SystemExit(code)


def partcad():
    wrappers = SOURCE / "src/partcad/wrappers"
    sys.path.insert(0, str(wrappers))
    import ezdxf

    path = Path.cwd() / "drawing.dxf"
    doc = ezdxf.new()
    doc.modelspace().add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)
    doc.saveas(path)
    request = {"path": str(path), "include": [], "exclude": [], "tolerance": 1e-6}
    for i in range(2):
        proof = RESULT / (VARIANT + "-partcad-wrapper-" + str(i) + ".json")
        bootstrap = textwrap.dedent("""
            import json, os, runpy, sys
            from pathlib import Path
            source, drawing, proof = sys.argv[1:]
            sys.argv = [source, drawing]
            try:
                runpy.run_path(source, run_name='__main__')
            finally:
                cq = sys.modules.get('cadquery')
                if cq is not None:
                    path = Path(cq.__file__).resolve()
                    path.relative_to(Path(os.environ['COMPAT_CQ_ROOT']).resolve())
                    Path(proof).write_text(json.dumps({'cadquery': str(path)}) + '\\n')
        """)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(wrappers), env.get("PYTHONPATH", "")])
        run = subprocess.run(
            [sys.executable, "-c", bootstrap, str(wrappers / "wrapper_import_dxf.py"), str(path), str(proof)],
            input=json.dumps(request).encode(), capture_output=True, env=env, timeout=120,
        )
        (RESULT / (VARIANT + "-partcad-wrapper-" + str(i) + ".stdout")).write_bytes(run.stdout)
        (RESULT / (VARIANT + "-partcad-wrapper-" + str(i) + ".stderr")).write_bytes(run.stderr)
        assert run.returncode == 0, run.stderr.decode(errors="replace")
        assert proof.is_file(), "wrapper did not establish CadQuery provenance"
        import ocp_serialize
        response = ocp_serialize.deserialize(run.stdout)
        assert response["success"], response
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(response["shape"], props)
        assert abs(props.Mass() - 100) < 1e-6, props.Mass()
    print("PartCAD: two fresh original DXF wrapper protocol requests and decoded face-area checks passed; not zygote lifecycle coverage")


if __name__ == "__main__":
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    {"72": partcad, "73": soma, "74": repl, "76": partspec, "77": faktory}[sys.argv[1]]()
