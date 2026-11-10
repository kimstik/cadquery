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

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = ROOT / "compat-results"
WORK = ROOT / "compat-work"
BASE = "cb26a271db07a8ad21285253698452034665ec68"
HEAD = "02a0aa33de73ead142bbac02ee474b8892585082"


def inventory():
    entries = json.loads((HERE / "projects.json").read_text())
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
        prefix = members[0].name.split("/")[0]
        for member in members:
            relative = Path(member.name).relative_to(prefix)
            target = (destination / relative).resolve()
            assert target.is_relative_to(destination.resolve()), member.name
            assert not member.issym() and not member.islnk(), member.name
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
        fetch_source(project, WORK / "consumer")
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
        if project.get("install"):
            source = WORK / "consumer" / project.get("install_subdir", "")
            code = command([sys.executable, "-m", "pip", "install", "--no-deps", "-e", source],
                           folder / "consumer-install.log")
            if code:
                result["reason"] = "Consumer installation failed"
                return
        command([sys.executable, "-m", "pip", "freeze", "--all"], folder / "dependencies.txt")
        outcomes = {}
        for label in ["base", "pr"]:
            consumer = WORK / ("consumer-" + label)
            shutil.copytree(WORK / "consumer", consumer)
            run_directory = WORK / ("run-" + label)
            run_directory.mkdir()
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join([str(WORK / label), str(consumer),
                                                  str(consumer / "src")])
            env["COMPAT_CQ_ROOT"] = str(WORK / label)
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
                                      cwd=run_directory, timeout=600)
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
        result["exit_codes"] = outcomes
        result["status"] = (
            "passed" if outcomes == {"base": 0, "pr": 0} else
            "regression" if outcomes["base"] == 0 else
            "baseline-failed" if outcomes["pr"] != 0 else "pr-only-pass"
        )
    except Exception as error:
        result["reason"] = f"{type(error).__name__}: {error}"
    finally:
        (folder / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as stream:
                stream.write(f"### {project_id}: {project['name']}\n\n{result['status']}\n")


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
    text = "\n".join(lines) + "\n"
    print(text)
    OUT.mkdir(exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(results, indent=2))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(text)


if __name__ == "__main__":
    if sys.argv[1] == "matrix":
        print("projects=" + json.dumps([p["id"] for p in inventory()]))
    elif sys.argv[1] == "project":
        project_run(int(sys.argv[2]))
    elif sys.argv[1] == "report":
        report()
