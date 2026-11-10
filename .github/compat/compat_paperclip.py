"""Exercise an unchanged Paperclip worker in fresh disposable CI processes."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile


SOLID = """import cadquery as cq
result = cq.Workplane('XY').box(10, 8, 3)
assert result.val().isValid()
assert abs(result.val().Volume() - 240) < 1e-6
"""
PLANAR = """import cadquery as cq
result = cq.Workplane('XY').rect(10, 8)
assert result.val().isValid()
"""
REPEAT_DXF = PLANAR + """cq.exporters.export(result, 'first.dxf', 'DXF')
cq.exporters.export(result, 'second.dxf', 'DXF')
"""


def validate(path, fmt):
    if not path.is_file() or path.stat().st_size == 0:
        raise AssertionError(f"Missing or empty artifact: {path}")
    data = path.read_bytes()
    if fmt == "step":
        assert b"ISO-10303-21;" in data and b"END-ISO-10303-21;" in data
    elif fmt == "stl":
        facets = int.from_bytes(data[80:84], "little") if len(data) >= 84 else 0
        binary = facets > 0 and len(data) == 84 + 50 * facets
        ascii_stl = data.lstrip().startswith(b"solid") and b"facet normal" in data
        assert binary or ascii_stl, "Invalid or empty STL structure"
    elif fmt == "svg":
        assert ET.fromstring(data).tag.rsplit("}", 1)[-1] == "svg"
    elif fmt == "dxf":
        assert all(tag in data for tag in (b"SECTION", b"ENTITIES", b"EOF"))
        assert any(tag in data for tag in (b"LINE", b"POLYLINE", b"SPLINE")), "No DXF geometry"
    elif fmt == "3mf":
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            models = [name for name in archive.namelist() if name.endswith(".model")]
            assert models, "No 3MF model"
            assert any(b"triangle" in archive.read(name) for name in models)


def run_case(worker, output, name, fmt, script, expected_error=False):
    workdir = output / name
    workdir.mkdir(parents=True)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(output / "home"),
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }
    # The workflow may select an audited checkout without installing it.
    if "PYTHONPATH" in os.environ:
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    started = time.monotonic()
    record = {"case": name, "format": fmt, "status": "fail"}
    try:
        proc = subprocess.run(
            [sys.executable, "-u", str(worker)],
            input=json.dumps({"script": script, "format": fmt, "workdir": str(workdir)}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=workdir,
            env=env,
            timeout=120,
        )
        (workdir / "stdout.txt").write_text(proc.stdout, encoding="utf-8")
        (workdir / "stderr.txt").write_text(proc.stderr, encoding="utf-8")
        record["returncode"] = proc.returncode
        assert proc.returncode == 0, f"Worker exit {proc.returncode}: {proc.stderr[-2000:]}"
        # The real client parses the first stdout line, not arbitrary JSON found later.
        lines = proc.stdout.strip().splitlines()
        assert len(lines) == 1, f"Unexpected worker stdout: {proc.stdout[-2000:]}"
        reply = json.loads(lines[0])
        record["reply"] = reply
        if expected_error:
            assert reply.get("ok") is False and reply.get("error") == "script_error", reply
            assert not (workdir / f"artifact.{fmt}").exists()
        else:
            assert reply.get("ok") is True, reply
            artifact = Path(reply["artifactPath"]).resolve()
            expected = workdir / f"artifact.{fmt}"
            assert artifact == expected, (artifact, expected)
            validate(artifact, fmt)
            if name == "dxf-repeat":
                validate(workdir / "first.dxf", "dxf")
                validate(workdir / "second.dxf", "dxf")
            record["artifact_bytes"] = artifact.stat().st_size
        record["status"] = "pass"
    except subprocess.TimeoutExpired as exc:
        record["error"] = "Worker timed out after 120 seconds"
        (workdir / "stdout.txt").write_text(str(exc.stdout or ""), encoding="utf-8")
        (workdir / "stderr.txt").write_text(str(exc.stderr or ""), encoding="utf-8")
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["seconds"] = round(time.monotonic() - started, 3)
    print(json.dumps(record), flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Pinned Paperclip source checkout")
    parser.add_argument("--output", type=Path, help="New result directory")
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        parser.error("Run this worker probe only in disposable GitHub Actions CI")
    worker = (args.source / "src" / "cad_worker.py").resolve(strict=True)
    label = os.environ.get("CQ_COMPAT_VARIANT", "run")
    output = (args.output or Path(f"paperclip-{label}")).resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "home").mkdir()
    cases = [
        ("step", "step", SOLID),
        ("stl", "stl", SOLID),
        ("svg", "svg", SOLID),
        ("3mf", "3mf", SOLID),
        ("dxf", "dxf", PLANAR),
        ("dxf-repeat", "dxf", REPEAT_DXF),
        ("missing-result", "step", "import cadquery as cq\n"),
    ]
    results = [
        run_case(worker, output, name, fmt, script, name == "missing-result")
        for name, fmt, script in cases
    ]
    (output / "results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return int(any(result["status"] != "pass" for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
