"""Compare pinned downstream scenarios without changing the PR branch."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import xml.etree.ElementTree as ET

from scan import scan, observations

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = ROOT / "compat-results"
WORK = ROOT / "compat-work"
BASE = "cb26a271db07a8ad21285253698452034665ec68"
HEAD = "02a0aa33de73ead142bbac02ee474b8892585082"


def inventory():
    entries = json.loads((HERE / "projects.json").read_text())
    if os.environ.get("COMPAT_MODE") == "startup":
        routes = {row["id"]: row for row in json.loads((HERE / "startup_routes.json").read_text())}
        entries = [{**project, **routes[project["id"]]}
                   for project in entries if project["id"] in routes]
    ids = [p["id"] for p in entries]
    assert len(ids) == len(set(ids)), "Duplicate inventory IDs"
    selected = os.environ.get("COMPAT_PROJECTS", "").strip()
    if selected:
        requested = {int(value) for value in selected.split(",")}
        assert requested <= set(ids), "Unknown inventory ID"
        entries = [p for p in entries if p["id"] in requested]
    return entries


def command(args, log, *, env=None, cwd=None, timeout=900):
    with log.open("w") as stream:
        try:
            result = subprocess.run(
                [str(a) for a in args], cwd=cwd, env=env,
                stdout=stream, stderr=subprocess.STDOUT, timeout=timeout,
            )
            return result.returncode
        except subprocess.TimeoutExpired:
            stream.write("\nTIMEOUT\n")
            return 124


def fetch_source(project, destination):
    destination.mkdir(parents=True, exist_ok=True)
    if "source_url" in project:
        data = urllib.request.urlopen(project["source_url"], timeout=90).read()
        assert hashlib.sha256(data).hexdigest() == project["sha256"]
        (destination / "metric_threads.py").write_bytes(data)
        return
    archive = WORK / (str(project["id"]) + ".tar.gz")
    url = project.get("archive_url") or f"https://codeload.github.com/{project['repo']}/tar.gz/{project['ref']}"
    with urllib.request.urlopen(url, timeout=90) as response:
        archive.write_bytes(response.read())
    # Inspect paths before extraction. No repository hooks or setup scripts run here.
    with tarfile.open(archive) as source:
        members = source.getmembers()
        originals = {member.name: member for member in members}
        prefix = members[0].name.split("/")[0]
        for member in members:
            relative = Path(member.name).relative_to(prefix)
            target = (destination / relative).resolve()
            assert target.is_relative_to(destination.resolve()), member.name
            if member.issym() or member.islnk():
                # Only exact, inspected internal links are accepted.
                allowed = project.get("archive_file_links", {})
                ignored = project.get("archive_ignored_links", {})
                assert member.issym(), member.name
                assert (allowed.get(relative.as_posix()) == member.linkname or
                        ignored.get(relative.as_posix()) == member.linkname), member.name
                linked = (target.parent / member.linkname).resolve()
                assert linked.is_relative_to(destination.resolve()), member.linkname
                archived = prefix + "/" + linked.relative_to(destination.resolve()).as_posix()
                linked_member = originals[archived]
                if ignored.get(relative.as_posix()) == member.linkname:
                    assert linked_member.isdir(), archived
                    continue
                assert linked_member.isfile(), archived
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(linked_member) as stream:
                    target.write_bytes(stream.read())
                continue
            if not relative.parts:
                continue
            member.name = str(relative)
            source.extract(member, destination, filter="data")


def project_run(project_id):
    project = next(p for p in inventory() if p["id"] == project_id)
    OUT.mkdir(exist_ok=True)
    WORK.mkdir(exist_ok=True)
    folder = OUT / str(project_id)
    folder.mkdir(exist_ok=True)
    result = {"id": project_id, "name": project["name"], "source": project,
              "base": BASE, "head": HEAD, "status": "blocked"}
    try:
        if project.get("disposition"):
            result.update(status=project["disposition"], reason=project["reason"])
            return
        if os.environ.get("COMPAT_MODE") == "startup" and not project.get("startup_import"):
            result.update(status="startup-unavailable", reason=project["startup_reason"])
            return
        if project.get("synthetic"):
            (WORK / "consumer").mkdir()
        else:
            fetch_source(project, WORK / "consumer")
        source_scan = scan(WORK / "consumer")
        (folder / "source-risks.json").write_text(json.dumps(source_scan, indent=2) + "\n")
        result["source_audit"] = {"python_files": source_scan["python_files"],
                                  "risk_sites": len(source_scan["sites"]),
                                  "parse_errors": len(source_scan["parse_errors"])}
        for label, revision in [("base", BASE), ("pr", HEAD)]:
            code = command(["git", "worktree", "add", "--detach", WORK / label, revision],
                           folder / (label + "-checkout.log"), cwd=ROOT)
            if code:
                result["reason"] = "CadQuery checkout failed"
                return
        # Both variants run in this one environment. Never resolve dependencies twice.
        code = command([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                        "--prefer-binary", str(WORK / "base") + "[dev]",
                        *project.get("dependencies", [])], folder / "install.log", timeout=1200)
        if code:
            result["reason"] = "Dependency installation failed"
            return
        if project.get("dependencies_no_deps"):
            code = command([sys.executable, "-m", "pip", "install", "--no-deps",
                            *project["dependencies_no_deps"]],
                           folder / "install-no-deps.log", timeout=1200)
            if code:
                result["reason"] = "Explicit no-deps dependency installation failed"
                return
        if project.get("install"):
            source = WORK / "consumer" / project.get("install_subdir", "")
            install_env = dict(os.environ)
            install_env.update(project.get("install_env", {}))
            code = command([sys.executable, "-m", "pip", "install", "--no-deps", "-e", source],
                           folder / "consumer-install.log", env=install_env)
            if code:
                result["reason"] = "Consumer installation failed"
                return
        command([sys.executable, "-m", "pip", "freeze", "--all"], folder / "dependencies.txt")
        if project.get("startup_import"):
            from startup import measure
            result["startup"] = measure(project, WORK, folder)
            result.update(status=result["startup"]["classification"],
                          platform=sys.platform, python=sys.version)
            return
        outcomes = {}
        for label in ["base", "pr"]:
            consumer = WORK / ("consumer-" + label)
            shutil.copytree(WORK / "consumer", consumer)
            run_directory = WORK / ("run-" + label)
            run_directory.mkdir()
            env = dict(os.environ)
            cq_root = WORK / label
            if project.get("package_only_path"):
                # Match an installed package: do not expose CadQuery's own tests.
                cq_root = WORK / ("import-" + label)
                cq_root.mkdir()
                shutil.copytree(WORK / label / "cadquery", cq_root / "cadquery")
            env["PYTHONPATH"] = os.pathsep.join([str(cq_root), str(consumer),
                                                  str(consumer / "src")])
            env["COMPAT_CQ_ROOT"] = str(cq_root)
            env["COMPAT_RESULT_DIR"] = str(folder)
            env["CQ_COMPAT_VARIANT"] = label
            args = [sys.executable, HERE / project["runner"]]
            if not project.get("standalone"):
                args.append(str(project_id))
            args.append(str(consumer))
            if project.get("xvfb"):
                args = ["xvfb-run", "-a", *args]
            check = command([sys.executable, "-c",
                             "import cadquery, os; from pathlib import Path; "
                             "print(cadquery.__file__); "
                             "assert Path(cadquery.__file__).resolve().is_relative_to("
                             "Path(os.environ['COMPAT_CQ_ROOT']).resolve())"],
                            folder / (label + "-import.log"), env=env, cwd=run_directory)
            if check:
                outcomes[label] = check
                continue
            outcomes[label] = command(args, folder / (label + ".log"), env=env,
                                      cwd=run_directory, timeout=project.get("timeout_seconds", 600))
            junit = folder / (label + "-native-tests.xml")
            if junit.exists():
                suites = ET.parse(junit).getroot()
                totals = {key: sum(int(s.get(key, 0)) for s in suites.iter("testsuite"))
                          for key in ["tests", "failures", "errors", "skipped"]}
                result.setdefault("native_tests", {})[label] = totals
                class_prefix = ".".join(consumer.relative_to(ROOT).parts) + "."
                test_inventory = [{"class": (case.get("classname") or "").removeprefix(class_prefix),
                                   "name": case.get("name"),
                                   "skipped": case.find("skipped") is not None}
                                  for case in suites.iter("testcase")]
                (folder / (label + "-test-inventory.json")).write_text(
                    json.dumps(test_inventory, indent=2) + "\n")
                if not test_inventory:
                    outcomes[label] = 5
            if project.get("observe"):
                # Observation is separate: it cannot turn an unchanged failure into a pass.
                observed_consumer = WORK / ("consumer-" + label + "-observed")
                shutil.copytree(WORK / "consumer", observed_consumer)
                observed_run = WORK / ("run-" + label + "-observed")
                observed_run.mkdir()
                observed_env = dict(env)
                observed_env["COMPAT_OBSERVE"] = "1"
                observed_env["CQ_COMPAT_VARIANT"] = label + "-observed"
                observed_env["PYTHONPATH"] = os.pathsep.join([str(cq_root),
                                                            str(observed_consumer),
                                                            str(observed_consumer / "src")])
                observed_args = [str(a).replace(str(consumer), str(observed_consumer)) for a in args]
                observed_code = command(observed_args, folder / (label + "-observed.log"),
                                        env=observed_env, cwd=observed_run,
                                        timeout=project.get("timeout_seconds", 600))
                result.setdefault("observation_exit_codes", {})[label] = observed_code
                coverage = folder / (label + "-observed-coverage.json")
                if coverage.exists():
                    try:
                        mapped = observations(source_scan, coverage, observed_consumer)
                        (folder / (label + "-risk-observation.json")).write_text(json.dumps(mapped, indent=2) + "\n")
                        result.setdefault("risk_observation", {})[label] = {
                            "total_sites": len(mapped),
                            "executed_sites": sum(s["executed_in_observation"] is True for s in mapped),
                            "unmeasured_sites": sum(not s["measured_file"] for s in mapped),
                            "meaning": "Executed lines only, not proof of branch reachability or contract success"}
                    except (ValueError, KeyError, OSError) as error:
                        result.setdefault("observation_errors", {})[label] = str(error)
            if project.get("standalone") and project_id == 38:
                detail = run_directory / ("paperclip-" + label)
                if detail.exists():
                    shutil.copytree(detail, folder / (label + "-worker"))
        if project_id == 11:
            controls = {}
            for mode in ["no-cleanup", "consumer-patched"]:
                controls[mode] = {}
                for label in ["base", "pr"]:
                    consumer = WORK / f"consumer-{label}-{mode}"
                    shutil.copytree(WORK / "consumer", consumer)
                    run_directory = WORK / f"run-{label}-{mode}"
                    run_directory.mkdir()
                    env = dict(os.environ)
                    env["PYTHONPATH"] = os.pathsep.join([str(WORK / label), str(consumer),
                                                       str(consumer / "src")])
                    if mode == "consumer-patched":
                        code = command([sys.executable, HERE / "patch_editor_consumer.py", consumer],
                                       folder / f"{label}-{mode}-patch.log", env=env)
                        if code:
                            controls[mode][label] = code
                            continue
                    args = [sys.executable, HERE / project["runner"], consumer]
                    if mode == "no-cleanup":
                        args.append("--no-cleanup")
                    controls[mode][label] = command(args, folder / f"{label}-{mode}.log",
                                                   env=env, cwd=run_directory, timeout=600)
            result["editor_controls"] = controls
        if project.get("synthetic"):
            result["contract_comparison"] = []
            before = folder / "contracts-base" / "summary.json"
            after = folder / "contracts-pr" / "summary.json"
            if before.exists() and after.exists():
                base_cases = {r["name"]: r for r in json.loads(before.read_text())["cases"]}
                pr_cases = {r["name"]: r for r in json.loads(after.read_text())["cases"]}
                for name in sorted(set(base_cases) | set(pr_cases)):
                    a, b = base_cases.get(name, {}), pr_cases.get(name, {})
                    result["contract_comparison"].append({
                        "name": name, "scope": a.get("scope", b.get("scope")),
                        "base": a.get("status", "missing"), "pr": b.get("status", "missing"),
                        "meaning": "Finite observable behavior, not an ecosystem or supported-API verdict"})
        inventories = [folder / (label + "-test-inventory.json") for label in ["base", "pr"]]
        if all(path.exists() for path in inventories):
            a, b = [json.loads(path.read_text()) for path in inventories]
            normalize = lambda cases: sorted(cases, key=lambda c: (c["class"] or "", c["name"] or "", c["skipped"]))
            result["same_test_cases_and_skips"] = normalize(a) == normalize(b)
        result["exit_codes"] = outcomes
        result["platform"] = sys.platform
        result["python"] = sys.version
        result["status"] = (
            "passed" if outcomes == {"base": 0, "pr": 0} else
            "regression" if outcomes["base"] == 0 else
            "baseline-failed" if outcomes["pr"] != 0 else "pr-only-pass"
        )
        if result["status"] == "passed" and result.get("same_test_cases_and_skips") is False:
            result["status"] = "test-selection-difference"
    except Exception as error:
        result["reason"] = f"{type(error).__name__}: {error}"
    finally:
        (folder / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as stream:
                stream.write(f"### {project_id}: {project['name']}\n\n{result['status']}\n")


def startup_summary(results):
    # Correct the completed CQ-import comparisons as one exploratory family.
    tested = []
    for result in results:
        startup = result.get("startup", {})
        if startup.get("classification") in {
            "measured-import-beneficiary", "measured-import-slowdown", "inconclusive-timing"
        }:
            tested.append((startup["metrics"]["import_seconds"]["sign_test_two_sided_p"], result))
    adjusted = 0.0
    for rank, (p_value, result) in enumerate(sorted(tested, key=lambda row: row[0])):
        adjusted = max(adjusted, min(1.0, p_value * (len(tested) - rank)))
        result["startup"]["holm_adjusted_sign_p"] = adjusted
        classification = result["startup"]["classification"]
        result["startup"]["group"] = (
            classification if adjusted <= 0.05 else "inconclusive-timing"
        )
    groups = {}
    for result in results:
        startup = result.get("startup", {})
        group = startup.get("group", startup.get("classification", result["status"]))
        groups.setdefault(group, []).append(result["id"])
    return {
        "groups": groups,
        "compared_cq_import_routes": len(tested),
        "method": "15 alternating fresh-process pairs after a warm-up pair; paired median differences and descriptive bootstrap intervals; two-sided exact sign tests with Holm correction across completed CQ-import routes in this batch. Exploratory CI measurements, not independent population samples.",
        "additivity": "Do not sum plugin/library savings sharing one interpreter. A sum is meaningful only for explicitly independent process startups with specified frequencies. Do not sum percentage changes. No ecosystem usage weights were measured.",
        "results": results,
    }


def report():
    lines = ["# Downstream comparison", "", f"Base: `{BASE}`", f"PR: `{HEAD}`", "",
             "Same dependency environment per project. Passing means only the listed scenarios passed.",
             "", "| ID | Project | Result |", "|---|---|---|"]
    results = []
    for project in inventory():
        path = OUT / str(project["id"]) / "result.json"
        result = json.loads(path.read_text()) if path.exists() else {
            "id": project["id"], "name": project["name"], "status": "missing-result"}
        results.append(result)
        lines.append(f"| {project['id']} | {project['name']} | {result['status']} |")
    if os.environ.get("COMPAT_MODE") == "startup":
        startup = startup_summary(results)
        (OUT / "startup-summary.json").write_text(json.dumps(startup, indent=2) + "\n")
        lines += ["", "## Import measurement groups", "", startup["method"], ""]
        lines += [f"- {group}: {ids}" for group, ids in startup["groups"].items()]
        lines += ["", startup["additivity"]]
    text = "\n".join(lines) + "\n"
    print(text)
    OUT.mkdir(exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(results, indent=2))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(text)


if __name__ == "__main__":
    if os.name == "nt":
        import ctypes
        # Inherited by children; keep shared Windows runners responsive.
        kernel = ctypes.windll.kernel32
        kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x40)
    if sys.argv[1] == "matrix":
        print("projects=" + json.dumps([p["id"] for p in inventory()]))
    elif sys.argv[1] == "project":
        project_run(int(sys.argv[2]))
    elif sys.argv[1] == "report":
        report()
