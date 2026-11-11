"""Pinned AI/CAD consumers with fixed local model input, not model/API calls.

Consumer sources stay unchanged. Provenance is recorded in the actual generated
model/worker after its natural CadQuery import, never by preloading the library.
These bounded execution/export paths are not full-application certifications.
"""

import faulthandler
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import traceback
import xml.etree.ElementTree as ET


ROOT = Path.cwd()
RESULTS = Path(os.environ.get("COMPAT_RESULT_DIR", str(ROOT))).resolve()
VARIANT = os.environ.get("CQ_COMPAT_VARIANT", "unknown")


def command(args, name, cwd, timeout=240):
    result = subprocess.run(
        [str(value) for value in args], cwd=cwd, capture_output=True,
        text=True, timeout=timeout,
    )
    (RESULTS / f"{VARIANT}-major-{name}.log").write_text(
        result.stdout + "\n--- stderr ---\n" + result.stderr,
        encoding="utf-8",
    )
    print(json.dumps({"case": name, "returncode": result.returncode,
                      "stdout": result.stdout[-2000:],
                      "stderr": result.stderr[-2000:]}), flush=True)
    return result


def worker_provenance(name):
    output = RESULTS / f"{VARIANT}-major-{name}-provenance.json"
    # This snippet follows the model's own import or the consumer's CQGI import.
    # It neither imports CadQuery nor changes the consumer's import machinery.
    snippet = f"""
import json as _compat_json, os as _compat_os, sys as _compat_sys
from pathlib import Path as _CompatPath
_compat_cq = _compat_sys.modules.get('cadquery')
assert _compat_cq is not None, 'consumer/model did not import cadquery'
_compat_actual = _CompatPath(_compat_cq.__file__).resolve()
_compat_expected = _CompatPath(_compat_os.environ['COMPAT_CQ_ROOT']).resolve()
assert _compat_actual.is_relative_to(_compat_expected), str(_compat_actual)
_CompatPath({str(output)!r}).write_text(_compat_json.dumps({{
    'cadquery_source': str(_compat_actual),
    'expected_root': str(_compat_expected),
    'worker_pid': _compat_os.getpid(),
    'worker_python': _compat_sys.executable,
}}, indent=2) + '\\n')
"""
    return output, snippet


def check_provenance(output):
    record = json.loads(output.read_text())
    assert Path(record["cadquery_source"]).is_relative_to(
        Path(os.environ["COMPAT_CQ_ROOT"]).resolve()
    ), record
    print(json.dumps({"provenance": record}), flush=True)


def validate_files(folder, volume=None):
    # Validation runs only after the real consumer has completed its worker.
    import cadquery as cq
    import trimesh

    step = folder / "object.step"
    stl = folder / "object.stl"
    assert step.stat().st_size > 100
    assert stl.stat().st_size > 100
    assert "ISO-10303-21" in step.read_text(errors="replace")[:100]
    shape = cq.importers.importStep(str(step)).val()
    assert shape.isValid() and shape.Solids()
    assert math.isfinite(shape.Volume()) and shape.Volume() > 0
    if volume is not None:
        assert math.isclose(shape.Volume(), volume, rel_tol=1e-5), shape.Volume()
    mesh = trimesh.load(str(stl), force="mesh")
    assert len(mesh.vertices) > 0 and len(mesh.faces) > 0
    assert mesh.is_watertight and mesh.volume > 0


def skill(source):
    outcomes = []
    for kind in ["geometry", "no-stl", "script-error"]:
        folder = ROOT / ("skill-" + kind)
        folder.mkdir()
        record, stamp = worker_provenance("skill-" + kind)
        body = "import cadquery as cq\n" + stamp
        if kind == "geometry":
            body += (
                "from gridfinity import GridfinityBin\n"
                "result = GridfinityBin(1, 1, 2, stacking_lip=False).build()\n"
                "assert result.val().isValid() and result.val().Volume() > 0\n"
                "cq.exporters.export(result, 'object.step')\n"
                "cq.exporters.export(result, 'object.stl')\n"
            )
        elif kind == "script-error":
            body += "raise ValueError('fixed local invalid model')\n"
        else:
            body += "result = cq.Workplane().box(2, 3, 4)\n"
        model = folder / "model.py"
        model.write_text(body)
        result = command(
            [sys.executable, source / "run_cadquery_model.py", model,
             "--strict", "--timeout", "120"], "skill-" + kind, folder,
        )
        report = json.loads(result.stdout)
        check_provenance(record)
        if kind == "geometry":
            assert result.returncode == 0 and report["success"], report
            assert report["watertight"] is True
            assert len(report["stls"]) == 1 and len(report["steps"]) == 1
            assert len(report["bboxes"]) == 1
            assert all(value > 0 for value in report["bboxes"][0])
            validate_files(folder)
        else:
            assert result.returncode == 1 and not report["success"], report
            message = "No STL files produced" if kind == "no-stl" else "fixed local invalid model"
            assert message in report["stderr"], report
        outcomes.append(kind)

    # Entire original geometry test module, without rendering/model downloads.
    report = RESULTS / f"{VARIANT}-native-tests.xml"
    result = command(
        [sys.executable, "-m", "pytest", "tests/test_gridfinity.py", "-ra",
         "--junitxml=" + str(report)], "skill-native-gridfinity", source,
        timeout=1100,
    )
    assert result.returncode == 0, "original gridfinity tests failed"
    tree = ET.parse(report)
    counts = {key: sum(int(s.attrib.get(key, 0)) for s in tree.findall(".//testsuite"))
              for key in ["tests", "failures", "errors", "skipped"]}
    assert counts["tests"] > 0 and counts["skipped"] == 0, counts
    return {"cases": outcomes, "native": counts,
            "scope": "own strict subprocess wrapper and full gridfinity test module; no preview renderer"}


def codeverse(source):
    outcomes = []
    for kind in ["workplane", "assembly", "missing-result"]:
        folder = ROOT / ("codeverse-" + kind)
        folder.mkdir()
        record, stamp = worker_provenance("codeverse-" + kind)
        body = "import cadquery as cq\n" + stamp
        body += "part = cq.Workplane().box(2, 3, 4)\n"
        if kind == "workplane":
            body += "result = part\n"
        elif kind == "assembly":
            body += (
                "result = cq.Assembly(name='Sample')\n"
                "result.add(part, name='Body', color=cq.Color('red'))\n"
                "result.add(part.translate((5, 0, 0)), name='Cap', color=cq.Color('blue'))\n"
            )
        model = folder / "model.py"
        model.write_text(body)
        output = folder / "out"
        wrapper = source / "harness/codeverse3d/languages/wrappers/run_cq.py"
        result = command(
            [sys.executable, wrapper, "--script", model, "--out", output],
            "codeverse-" + kind, folder,
        )
        # The real wrapper returns zero even for model errors. Its report is
        # the authoritative status, rather than the subprocess exit alone.
        assert result.returncode == 0
        report = json.loads((output / "build.json").read_text())
        check_provenance(record)
        if kind == "missing-result":
            assert not report["ok"] and report["error_type"] == "MissingResult", report
        else:
            assert report["ok"] and not report["error_type"], report
            assert {"glb", "step", "stl"} <= set(report["exported"]), report
            census = json.loads((output / "census.json").read_text())
            expected = 2 if kind == "assembly" else 1
            assert len(census["parts"]) == expected, census
            assert all(p["n_solids"] == 1 and p["tri_count"] > 0 and p["volume_m3"] > 0
                       for p in census["parts"]), census
            glb = (output / "object.glb").read_bytes()
            assert glb[:4] == b"glTF" and struct.unpack_from("<I", glb, 4)[0] == 2
            assert struct.unpack_from("<I", glb, 8)[0] == len(glb)
            validate_files(output, volume=24 * expected)
        outcomes.append(kind)
    return {"cases": outcomes,
            "scope": "original standalone CQ wrapper, GLB/STEP/STL and error reports; not full Python>=3.13 harness"}


def agentcad(source):
    from agentcad.runners import cadquery as runner

    outcomes = []
    for index in range(2):
        name = "agentcad-" + str(index)
        record, stamp = worker_provenance(name)
        model = (
            "length = 2.0\n"
            "part = cq.Workplane().box(length, 3, 4)\n"
            "show_object(part, name='Body')\n" + stamp
        )
        result = runner.execute(model, params={"length": 2.0 + index})
        # execute() imports CQ itself, before its preamble builds the model.
        # A baseline helper-import failure is retained, never bypassed.
        assert result.success, (result.exception, result.traceback)
        check_provenance(record)
        assert len(result.parts) == 1 and result.parts[0]["name"] == "Body"
        folder = ROOT / name
        folder.mkdir()
        runner.export_step(result.native_shape, str(folder / "object.step"))
        runner.export_stl(result.native_shape, str(folder / "object.stl"))
        validate_files(folder, volume=(2 + index) * 12)
        outcomes.append(name)
    record, stamp = worker_provenance("agentcad-missing-output")
    result = runner.execute("part = cq.Workplane().box(2, 3, 4)\n" + stamp)
    check_provenance(record)
    assert not result.success and result.status == "execution_error", result
    assert result.exception
    outcomes.append("missing-output")
    return {"cases": outcomes,
            "scope": "real CQGI runner, parameter override, exports and missing-output contract; no daemon or LLM"}


def cadgenbench(source):
    # Documented separate-backend interpreter option. No probe/import injected
    # into the parent and no altered subprocess implementation.
    os.environ["CADGENBENCH_CADQUERY_PYTHON"] = sys.executable
    from cadgenbench.baseline.agent import execute_code

    folder = ROOT / "cadgenbench-worker"
    folder.mkdir()
    outcomes = []
    for index in range(2):
        record, stamp = worker_provenance("cadgenbench-" + str(index))
        model = (
            "import cadquery as cq\n" + stamp +
            "result = cq.Workplane().box(2, 3, 4)\n"
            "assert result.val().isValid() and result.val().Volume() > 0\n"
            "cq.exporters.export(result, 'object.step')\n"
            "cq.exporters.export(result, 'object.stl')\n"
        )
        result = execute_code(model, folder, backend="cadquery", script_index=index)
        print(json.dumps({"case": index, "result": result.__dict__}), flush=True)
        assert result.success, result.stderr
        check_provenance(record)
        assert {"object.step", "object.stl"} <= set(result.files_produced), result
        assert all(result.files_produced[name] > 100 for name in ["object.step", "object.stl"])
        assert not (folder / f"_script_{index}.py").exists()
        validate_files(folder, volume=24)
        outcomes.append("export-" + str(index))
    record, stamp = worker_provenance("cadgenbench-invalid")
    result = execute_code(
        "import cadquery as cq\n" + stamp + "raise ValueError('fixed local invalid model')\n",
        folder, backend="cadquery", script_index=2,
    )
    check_provenance(record)
    assert not result.success and "fixed local invalid model" in result.stderr
    assert not result.files_produced, result.files_produced
    outcomes.append("model-error")
    return {"cases": outcomes,
            "scope": "actual selected CQ subprocess backend and artifact/error protocol; no run_agent, credentials, rendering or downloads"}


SCENARIOS = {82: skill, 83: codeverse, 84: agentcad, 85: cadgenbench}


if __name__ == "__main__":
    faulthandler.enable()
    project_id = int(sys.argv[1])
    source = Path(sys.argv[2]).resolve()
    sys.path[:0] = [str(source), str(source / "src")]
    RESULTS.mkdir(parents=True, exist_ok=True)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    try:
        detail = SCENARIOS[project_id](source)
        print(json.dumps({"id": project_id, "status": "passed", **detail}))
    except Exception:
        traceback.print_exc()
        sys.exit(1)
