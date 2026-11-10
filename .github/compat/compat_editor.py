"""Exercise CQ-editor's actual render/reload lifecycle in fresh processes.

Install the pinned CQ-editor source plus its dependencies first. This runner
uses its real Debugger, not a copy of reload_cq or module_manager. It needs Qt
but does not create an OpenGL viewer. Run the editor's own GUI tests separately.
"""

import argparse
import faulthandler
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback


CASES = (
    "geometry",
    "geometry-reload-cq",
    "dxf-roundtrip",
    "vtp-export",
    "dxf-module-identity",
)


def exercise(case, cleanup):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    import cadquery as cq
    from cq_editor.widgets.debugger import Debugger

    class ScriptDebugger(Debugger):
        def get_current_script(self):
            return self.script_buffer

        def get_current_script_path(self):
            return None

    debugger = ScriptDebugger(None)
    debugger.preferences["Reload CQ"] = case == "geometry-reload-cq"
    debugger.preferences["Reload imported modules"] = cleanup
    debugger.preferences["Add script dir to path"] = False
    debugger.preferences["Change working dir to script dir"] = False
    rendered = []
    errors = []
    debugger.sigRendered.connect(rendered.append)
    debugger.sigTraceback.connect(
        lambda exc_info, script: errors.append(exc_info) if exc_info else None
    )

    startup_modules = {
        name: name in sys.modules
        for name in (
            "cadquery.occ_impl.importers.dxf",
            "cadquery.occ_impl.exporters.dxf",
            "ezdxf",
            "vtkmodules",
        )
    }
    with tempfile.TemporaryDirectory(prefix="cq-editor-compat-") as directory:
        output = Path(directory) / ("model.vtp" if case == "vtp-export" else "model.dxf")
        script = (
            'import cadquery as cq\n'
            'result = cq.Workplane("XY").box(1, 2, 3)\n'
            'assert result.val().isValid()\n'
            'assert abs(result.val().Volume() - 6) < 1e-7\n'
        )
        if case == "dxf-roundtrip":
            script += (
                'section = cq.Workplane("XY").rect(2, 3)\n'
                f'cq.exporters.export(section, {str(output)!r}, exportType="DXF")\n'
                f'imported = cq.importers.importDXF({str(output)!r})\n'
                'assert abs(imported.val().Area() - 6) < 1e-7\n'
            )
        elif case == "dxf-module-identity":
            script += (
                'section = cq.Workplane("XY").rect(2, 3)\n'
                f'cq.exporters.export(section, {str(output)!r}, exportType="DXF")\n'
            )
        elif case == "vtp-export":
            script += f'cq.exporters.export(result, {str(output)!r}, exportType="VTP")\n'
        script += 'show_object(result, name="result")\n'
        debugger.script_buffer = script

        for iteration in (1, 2):
            before = len(rendered)
            print(json.dumps({"case": case, "phase": "render-start", "iteration": iteration}), flush=True)
            debugger.render()
            print(json.dumps({"case": case, "phase": "render-return", "iteration": iteration}), flush=True)
            if errors:
                raise AssertionError(
                    f"render {iteration} failed:\n"
                    + "".join(traceback.format_exception(*errors[-1]))
                )
            assert len(rendered) == before + 1, "Debugger emitted no render result"
            assert set(rendered[-1]) == {"result"}, rendered[-1]
            assert rendered[-1]["result"].shape.val().isValid()
            if case not in ("geometry", "geometry-reload-cq"):
                assert output.is_file() and output.stat().st_size > 0, output
                output.unlink()
            if case == "dxf-module-identity":
                via_module = cq.exporters.dxf.DxfDocument
                via_package = cq.exporters.DxfDocument
                assert via_module is via_package, (
                    "module_manager left a stale exporters.dxf package attribute: "
                    "its DxfDocument differs from exporters.DxfDocument"
                )

    print(json.dumps({"case": case, "phase": "checks-complete"}), flush=True)
    return {"startup_modules": startup_modules, "renders": len(rendered)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Pinned CQ-editor checkout")
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--no-cleanup", action="store_true")
    args = parser.parse_args()
    if args.case:
        faulthandler.enable()
        sys.path.insert(0, str(args.source.resolve()))
        try:
            details = exercise(args.case, not args.no_cleanup)
        except Exception:
            print(json.dumps({"case": args.case, "status": "fail", "error": traceback.format_exc()}))
            return 1
        print(json.dumps({"case": args.case, "status": "pass", **details}))
        return 0

    failed = False
    for case in CASES:
        command = [sys.executable, "-u", __file__, str(args.source.resolve()), "--case", case]
        if args.no_cleanup:
            command.append("--no-cleanup")
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired as exc:
            failed = True
            print(json.dumps({"case": case, "status": "timeout", "error": str(exc)}), flush=True)
            continue
        print(completed.stdout, end="", flush=True)
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr, flush=True)
        if completed.returncode:
            failed = True
            print(json.dumps({"case": case, "exit_code": completed.returncode}), flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
