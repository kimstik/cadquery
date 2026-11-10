"""Run complete pinned consumer suites without altering their source or imports."""

import json
import os
from pathlib import Path
import subprocess
import sys


HERE = Path(__file__).resolve().parent


def specification(project_id):
    entries = json.loads((HERE / "selftests.json").read_text())
    return next(entry for entry in entries if entry["id"] == project_id)


def run_suite(entry, source):
    import pytest

    output = Path(os.environ["COMPAT_RESULT_DIR"]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    variant = os.environ["CQ_COMPAT_VARIANT"]
    summary_path = output / f"{variant}-native-tests.json"
    junit_path = output / f"{variant}-native-tests.xml"
    observation = os.environ.get("COMPAT_OBSERVE") == "1"
    cov = None
    if observation:
        import coverage

        cov = coverage.Coverage(
            branch=True,
            source=[str(source), str(Path(os.environ["COMPAT_CQ_ROOT"]) / "cadquery")],
            data_file=str(output / f"{variant}.coverage"),
        )
        cov.start()

    class Results:
        def __init__(self):
            self.collected = []
            self.deselected = []
            self.reports = []
            self.collection_errors = []

        def pytest_collection_finish(self, session):
            self.collected = [item.nodeid for item in session.items]

        def pytest_deselected(self, items):
            self.deselected.extend(item.nodeid for item in items)

        def pytest_collectreport(self, report):
            if report.failed:
                self.collection_errors.append(str(report.longrepr))

        def pytest_runtest_logreport(self, report):
            self.reports.append({
                "nodeid": report.nodeid,
                "phase": report.when,
                "outcome": report.outcome,
                "wasxfail": getattr(report, "wasxfail", None),
            })

    recorder = Results()
    args = ["-v", "--tb=short", f"--junitxml={junit_path}", *entry["pytest_args"]]
    status = 1
    provenance_error = None
    try:
        status = int(pytest.main(args, plugins=[recorder]))
        # Do not import CadQuery before tests: that would alter the lifecycle under test.
        cq = sys.modules.get("cadquery")
        if cq is None:
            provenance_error = "The native suite did not load cadquery"
        else:
            cq_path = Path(cq.__file__).resolve()
            cq_root = Path(os.environ["COMPAT_CQ_ROOT"]).resolve()
            if not cq_path.is_relative_to(cq_root):
                provenance_error = f"Unexpected CadQuery source: {cq_path}"
        if not recorder.collected and status == 0:
            status = 5
        if provenance_error and status == 0:
            status = 1
    finally:
        reports = recorder.reports
        summary = {
            "id": entry["id"],
            "original_id": entry["original_id"],
            "variant": variant,
            "observation": observation,
            "pytest_args": args,
            "cwd": str(source),
            "collected": len(recorder.collected),
            "nodeids": recorder.collected,
            "deselected": recorder.deselected,
            "collection_errors": recorder.collection_errors,
            "passed": sum(r["phase"] == "call" and r["outcome"] == "passed" and not r["wasxfail"] for r in reports),
            "failed": sum(r["phase"] == "call" and r["outcome"] == "failed" for r in reports),
            "skipped": sum(r["outcome"] == "skipped" and not r["wasxfail"] for r in reports),
            "xfailed": sum(r["outcome"] == "skipped" and bool(r["wasxfail"]) for r in reports),
            "xpassed": sum(r["phase"] == "call" and r["outcome"] == "passed" and bool(r["wasxfail"]) for r in reports),
            "setup_teardown_errors": sum(r["phase"] != "call" and r["outcome"] == "failed" for r in reports),
            "provenance_error": provenance_error,
            "returncode": status,
            "reports": reports,
        }
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps({k: v for k, v in summary.items() if k not in ("nodeids", "reports")}), flush=True)
        if cov is not None:
            cov.stop()
            cov.save()
            coverage_path = output / f"{variant}-coverage.json"
            cov.json_report(outfile=str(coverage_path))
            coverage_data = json.loads(coverage_path.read_text())
            coverage_data["files"] = {
                str(Path(name).resolve()): details
                for name, details in coverage_data["files"].items()
            }
            coverage_path.write_text(json.dumps(coverage_data))
    if provenance_error:
        print(provenance_error, file=sys.stderr, flush=True)
    return status


def main():
    child = sys.argv[1] == "--child"
    index = 2 if child else 1
    entry = specification(int(sys.argv[index]))
    source = Path(sys.argv[index + 1]).resolve()
    if child:
        os.chdir(source)
        return run_suite(entry, source)
    # Keep the native process separate from runner initialization and use the consumer's
    # working directory, as its own CI does. No source patches or test deselection.
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--child", str(entry["id"]), str(source)],
        cwd=source,
        env=os.environ.copy(),
        timeout=1140,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
