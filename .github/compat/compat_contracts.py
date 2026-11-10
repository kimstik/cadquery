"""Probe a finite set of observable import contracts in fresh CI processes.

This is not a proof about arbitrary downstream programs. Each case states its
scope and expected base/PR behavior. Former incidental exports and reflection
are observable old behavior, not a claim that CadQuery documented those names.
A known difference still fails the old-behavior check. Only the startup case
has deliberately different expectations, because lazy loading is the change
under test. No import hooks, module proxies or advance CadQuery imports run in
this driver.

Sandbox coverage stays in compat_paperclip.py, which invokes the real worker.
This fixture does not invent a different sandbox and call it Paperclip.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time


BASE = "cb26a271db07a8ad21285253698452034665ec68"
HEAD = "02a0aa33de73ead142bbac02ee474b8892585082"
MARKER = "CONTRACT_RESULT "
NAMES = ("exportDXF", "exportDXFProjection", "DxfDocument", "exportVTP")


def case(name, scope, source, code, *, expected_pr="pass", reason=""):
    return {
        "name": name,
        "scope": scope,
        "source": source,
        "expected_base": "pass",
        "expected_pr": expected_pr,
        "reason": reason,
        "code": textwrap.dedent(code).strip() + "\n",
    }


def cases():
    result = [
        case(
            "startup-loading", "intentional-load-timing", "cadquery/__init__.py",
            """
            import cadquery
            loaded = {name.split('.')[0] for name in sys.modules}
            observed = {name: name in loaded for name in ('ezdxf', 'vtkmodules')}
            assert observed == dict.fromkeys(observed, variant == 'base'), observed
            """,
            reason="Same import succeeds; absence of eager ezdxf/VTK is intentional on PR.",
        ),
        case(
            "exporter-public-names", "retained-reexports",
            "cadquery/occ_impl/exporters/__init__.py",
            """
            import cadquery as cq
            from cadquery.occ_impl.exporters import (
                exportDXF, exportDXFProjection, DxfDocument, exportVTP,
            )
            from cadquery.occ_impl.exporters import dxf, vtk
            assert exportDXF is dxf.exportDXF
            assert exportDXFProjection is dxf.exportDXFProjection
            assert DxfDocument is dxf.DxfDocument
            assert exportVTP is vtk.exportVTP
            try:
                cq.exporters._not_a_cadquery_exporter
            except AttributeError:
                pass
            else:
                raise AssertionError('Unknown attribute did not raise AttributeError')
            observed = {'names': ['exportDXF', 'exportDXFProjection', 'DxfDocument', 'exportVTP']}
            """,
        ),
        case(
            "submodules-from-import", "python-submodule-import",
            "cadquery/occ_impl/{exporters,importers}/__init__.py",
            """
            import cadquery as cq
            from cadquery.occ_impl.exporters import dxf, vtk, assembly
            from cadquery.occ_impl.importers import dxf as importer_dxf
            assert cq.exporters.dxf is dxf
            assert cq.exporters.vtk is vtk
            assert cq.exporters.assembly is assembly
            assert cq.importers.dxf is importer_dxf
            observed = {'module_names': [m.__name__ for m in (dxf, vtk, assembly, importer_dxf)]}
            """,
        ),
        case(
            "submodules-explicit-import", "python-submodule-import",
            "cadquery/occ_impl/{exporters,importers}/__init__.py",
            """
            import cadquery.occ_impl.importers.dxf as importer_dxf
            import cadquery.occ_impl.exporters.dxf as exporter_dxf
            import cadquery.occ_impl.exporters.vtk as exporter_vtk
            import cadquery as cq
            assert cq.importers.dxf is importer_dxf
            assert cq.exporters.dxf is exporter_dxf
            assert cq.exporters.vtk is exporter_vtk
            observed = {'explicit_submodule_imports': True}
            """,
        ),
        case(
            "exporter-submodules-attribute", "retained-reexports",
            "cadquery/occ_impl/exporters/__init__.py:151-158",
            """
            import cadquery as cq
            assert callable(cq.exporters.dxf.exportDXF)
            assert callable(cq.exporters.vtk.exportVTP)
            assert callable(cq.exporters.assembly.exportGLTF)
            observed = {'plain_attribute_access': True}
            """,
        ),
        case(
            "importer-dxf-attribute", "former-implicit-submodule",
            "cadquery/occ_impl/importers/__init__.py",
            """
            import cadquery as cq
            assert cq.importers.dxf.__name__ == 'cadquery.occ_impl.importers.dxf'
            observed = {'plain_attribute_access': True}
            """,
            expected_pr="fail",
            reason="Old eager import bound importers.dxf; the PR does not bind it on startup.",
        ),
    ]
    for name in ("exportGLTF", "exportAssembly", "exportCAF", "exportVTKJS", "exportVRML"):
        result.append(case(
            "assembly-import-" + name, "former-incidental-export",
            "cadquery/assembly.py:base module imports; PR Assembly.export local imports",
            f"""
            from cadquery.assembly import {name}
            from cadquery.occ_impl.exporters.assembly import {name} as implementation
            assert {name} is implementation
            observed = {{'name': '{name}', 'same_implementation': True}}
            """,
            expected_pr="fail",
            reason="Name was observable at the old path but is not documented there; maintainer policy required.",
        ))
    result.extend([
        case(
            "exporter-dir", "former-reflection-behavior",
            "cadquery/occ_impl/exporters/__init__.py",
            f"""
            import cadquery as cq
            names = {NAMES!r}
            missing = sorted(set(names) - set(dir(cq.exporters)))
            observed = {{'missing_from_dir': missing}}
            assert not missing, observed
            """,
            expected_pr="fail",
            reason="PEP 562 attributes without __dir__ need not appear in dir().",
        ),
        case(
            "exporter-star-import", "former-reflection-behavior",
            "cadquery/occ_impl/exporters/__init__.py",
            f"""
            from cadquery.occ_impl.exporters import *
            missing = [name for name in {NAMES!r} if name not in globals()]
            observed = {{'missing_from_star': missing}}
            assert not missing, observed
            """,
            expected_pr="fail",
            reason="No __all__: star import reads module globals, not all lazy attributes.",
        ),
        case(
            "annotations-shape", "runtime-annotation-resolution",
            "cadquery/occ_impl/shapes.py:Shape.toVtkPolyData",
            """
            import cadquery as cq
            from typing import get_type_hints
            hints = get_type_hints(cq.Shape.toVtkPolyData)
            assert hints['return'].__name__ == 'vtkPolyData', hints
            observed = {'return_type': hints['return'].__name__}
            """,
            expected_pr="fail",
            reason="TYPE_CHECKING imports do not populate runtime globals used by get_type_hints.",
        ),
        case(
            "annotations-vtk-assy", "runtime-annotation-resolution",
            "cadquery/occ_impl/assembly.py:toVTKAssy",
            """
            from typing import get_type_hints, get_args
            from cadquery.occ_impl.assembly import toVTKAssy
            hints = get_type_hints(toVTKAssy)
            assert get_args(hints['return'])[0].__name__ == 'vtkProp3D', hints
            observed = {'return_type': str(hints['return'])}
            """,
            expected_pr="fail",
            reason="Quoted return annotation needs vtkProp3D in function globals at evaluation time.",
        ),
        case(
            "annotations-vtk-renderer", "runtime-annotation-resolution",
            "cadquery/occ_impl/assembly.py:toVTK",
            """
            from typing import get_type_hints
            from cadquery.occ_impl.assembly import toVTK
            hints = get_type_hints(toVTK)
            assert hints['return'].__name__ == 'vtkRenderer', hints
            observed = {'return_type': hints['return'].__name__}
            """,
            expected_pr="fail",
            reason="Quoted return annotation needs vtkRenderer in function globals at evaluation time.",
        ),
        case(
            "dxf-first-and-repeat", "functional-first-repeat",
            "cadquery/occ_impl/{exporters,importers}/__init__.py:DXF branches",
            """
            import cadquery as cq
            profile = cq.Workplane('XY').rect(10, 8)
            areas = []
            for index in range(2):
                path = f'rectangle-{index}.dxf'
                cq.exporters.export(profile, path, 'DXF')
                imported = cq.importers.importDXF(path)
                area = sum(face.Area() for face in imported.faces().vals())
                assert abs(area - 80) < 1e-6, area
                areas.append(area)
            observed = {'imported_areas': areas}
            """,
        ),
        case(
            "vtp-first-and-repeat", "functional-first-repeat",
            "cadquery/occ_impl/exporters/__init__.py:VTP branch; shapes.py:toVtkPolyData",
            """
            import cadquery as cq
            box = cq.Workplane('XY').box(1, 2, 3)
            counts = []
            for index in range(2):
                path = f'box-{index}.vtp'
                cq.exporters.export(box, path, 'VTP')
                # Read only after the exporter has had its first opportunity to load VTK.
                from vtkmodules.vtkIOXML import vtkXMLPolyDataReader
                reader = vtkXMLPolyDataReader()
                reader.SetFileName(path)
                reader.Update()
                poly = reader.GetOutput()
                count = (poly.GetNumberOfPoints(), poly.GetNumberOfCells())
                assert min(count) > 0, count
                counts.append(count)
            assert counts[0] == counts[1], counts
            observed = {'mesh_counts': counts}
            """,
        ),
        case(
            "exporter-package-reload", "explicit-package-reload",
            "cadquery/occ_impl/exporters/__init__.py",
            """
            import cadquery as cq
            from importlib import reload
            before = (cq.exporters.exportDXF, cq.exporters.exportVTP)
            reload(cq.exporters)
            after = (cq.exporters.exportDXF, cq.exporters.exportVTP)
            assert all(a is b for a, b in zip(before, after))
            observed = {'function_identities_retained': True}
            """,
        ),
        case(
            "dxf-cleanup-identity", "consumer-lifecycle-model",
            "CQ-editor cq_editor/widgets/debugger.py:module_manager lifecycle",
            """
            import cadquery as cq
            # Model only the cleanup rule: keep the startup set, remove additions.
            # This is not a general promise to survive arbitrary sys.modules edits.
            before = set(sys.modules)
            old = cq.exporters.dxf.DxfDocument
            removed = sorted(set(sys.modules) - before)
            for name in removed:
                sys.modules.pop(name, None)
            new = cq.exporters.DxfDocument
            observed = {'removed_modules': removed, 'same_class': old is new}
            assert old is new, 'Cleanup produced a second DxfDocument class'
            """,
            expected_pr="fail",
            reason="Reduced reproduction of observed CQ-editor lifecycle, not a new consumer certification.",
        ),
        case(
            "vtk-assy-first-and-repeat", "functional-first-repeat",
            "cadquery/occ_impl/assembly.py:toVTKAssy",
            """
            import cadquery as cq
            from cadquery.occ_impl.assembly import toVTKAssy
            assy = cq.Assembly(cq.Workplane('XY').box(1, 2, 3))
            counts = []
            for _ in range(2):
                actors = toVTKAssy(assy)
                assert len(actors) == 2, len(actors)
                cells = [a.GetMapper().GetInput().GetNumberOfCells() for a in actors]
                assert min(cells) > 0, cells
                counts.append(cells)
            assert counts[0] == counts[1], counts
            observed = {'actor_cells': counts}
            """,
        ),
    ])
    return result


def run_case(spec, output, variant):
    folder = output / spec["name"]
    folder.mkdir(parents=True, exist_ok=True)
    prefix = f"""import json as _contract_json, os, sys
from pathlib import Path
variant = {variant!r}
import cadquery
root = Path(os.environ['COMPAT_CQ_ROOT']).resolve()
assert Path(cadquery.__file__).resolve().is_relative_to(root), cadquery.__file__
observed = {{}}
"""
    suffix = f"finally:\n    print({MARKER!r} + _contract_json.dumps(observed, sort_keys=True))\n"
    code = prefix + "try:\n" + textwrap.indent(spec["code"], "    ") + suffix
    record = {key: value for key, value in spec.items() if key != "code"}
    record["script"] = code
    record["status"] = "fail"
    record["expected"] = spec["expected_base" if variant == "base" else "expected_pr"]
    start = time.monotonic()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1")
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "faulthandler", "-u", "-c", code],
            cwd=folder, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        (folder / "stdout.log").write_text(proc.stdout, encoding="utf-8")
        (folder / "stderr.log").write_text(proc.stderr, encoding="utf-8")
        record["exit_code"] = proc.returncode
        markers = [line[len(MARKER):] for line in proc.stdout.splitlines() if line.startswith(MARKER)]
        if markers:
            record["observed"] = json.loads(markers[-1])
        if proc.returncode == 0 and markers:
            record["status"] = "pass"
        if record["status"] != "pass":
            print(f"--- {spec['name']} stdout ---\n{proc.stdout}", flush=True)
            print(f"--- {spec['name']} stderr ---\n{proc.stderr}", flush=True)
    except subprocess.TimeoutExpired as error:
        record["exit_code"] = 124
        record["error"] = "TIMEOUT"
        for name, data in [("stdout", error.stdout), ("stderr", error.stderr)]:
            text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else (data or "")
            (folder / (name + ".log")).write_text(text, encoding="utf-8")
            print(text, flush=True)
    record["matches_prediction"] = record["status"] == record["expected"]
    record["elapsed_seconds"] = round(time.monotonic() - start, 3)
    (folder / "result.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record), flush=True)
    return record


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Contract execution is CI-only; use --manifest for static inspection")
    variant = os.environ["CQ_COMPAT_VARIANT"]
    assert variant in ("base", "pr"), variant
    assert "COMPAT_CQ_ROOT" in os.environ
    output = Path(os.environ.get("COMPAT_RESULT_DIR", ".")) / ("contracts-" + variant)
    output.mkdir(parents=True, exist_ok=True)
    records = [run_case(spec, output, variant) for spec in cases()]
    failed = [r["name"] for r in records if r["status"] != "pass"]
    summary = {
        "base_revision": BASE, "pr_revision": HEAD, "variant": variant,
        "scope": f"{len(records)} finite contracts; no whole-project or all-path proof",
        "cases": records, "failed": failed,
        "prediction_mismatches": [r["name"] for r in records if not r["matches_prediction"]],
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"variant": variant, "passed": len(records) - len(failed),
                      "failed": failed, "prediction_mismatches": summary["prediction_mismatches"]}), flush=True)
    # Expected failures remain failures of old behavior. Predictions never turn them green.
    return bool(failed)


if __name__ == "__main__":
    if "--manifest" in sys.argv:
        print(json.dumps(cases(), indent=2))
    else:
        sys.exit(main())
