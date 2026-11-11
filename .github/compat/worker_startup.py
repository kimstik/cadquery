"""Observe original workers reaching user code, before any model geometry.

This is a separate startup experiment, not an import timer or compatibility test.
The worker and its import machinery are unmodified. Only the submitted model is
an observation probe, with no result object and no modeling/export operations.
"""

import json
import math
import os
from pathlib import Path
import random
import shutil
import statistics
import subprocess
import sys
import time


ROUTES = {
    58: {
        "entry": "src/mcp_cadquery_server/script_runner.py",
        "scope": "Original JSON worker through its natural CadQuery/CQGI imports and CQGI parse/build, ending at the first submitted-script statements. Upgrade experiment; not the HTTP server or workspace-venv setup.",
    },
    83: {
        "entry": "harness/codeverse3d/languages/wrappers/run_cq.py",
        "scope": "Original standalone run_cq.py through argument handling, report/seed setup and natural CadQuery import, ending at the first model-script statements. Not the full harness or its executor parent.",
    },
}

# All observation-only imports occur at the user-script boundary, after the
# worker's own natural import. Do not preload CadQuery or install trace hooks.
PROBE = '''import sys, time, json, os
_boundary_ns = time.perf_counter_ns()
_cq = sys.modules.get("cadquery")
print("COMPAT_WORKER_BOUNDARY=" + json.dumps({
    "boundary_ns": _boundary_ns,
    "cadquery_source": getattr(_cq, "__file__", None),
    "worker_pid": os.getpid(),
    "worker_executable": sys.executable,
    "ezdxf_loaded": "ezdxf" in sys.modules,
    "vtk_loaded": any(n == "vtkmodules" or n.startswith("vtkmodules.") for n in sys.modules),
    "module_count": len(sys.modules),
    "clock": vars(time.get_clock_info("perf_counter")),
}), flush=True)
'''


def one_sample(project, consumer, cwd, env, cq_root):
    route = ROUTES[project["id"]]
    entry = consumer / route["entry"]
    assert entry.is_file(), entry
    if project["id"] == 58:
        args = [sys.executable, str(entry)]
        stdin = json.dumps({
            "workspace_path": str(cwd), "script_content": PROBE,
            "parameters": {}, "result_id": "startup-boundary",
        })
    else:
        model = cwd / "boundary_model.py"
        model.write_text(PROBE, encoding="utf-8")
        args = [sys.executable, str(entry), "--script", str(model),
                "--out", str(cwd / "artifacts"), "--rlimit-gb", "8"]
        stdin = None
    started_ns = time.perf_counter_ns()
    completed = subprocess.run(args, input=stdin, cwd=cwd, env=env,
                               capture_output=True, text=True, timeout=120)
    finished_ns = time.perf_counter_ns()
    payloads = [line[len("COMPAT_WORKER_BOUNDARY="):]
                for line in completed.stdout.splitlines()
                if line.startswith("COMPAT_WORKER_BOUNDARY=")]
    assert len(payloads) == 1, ("Missing or ambiguous actual-worker boundary\n"
                                + completed.stdout + completed.stderr)
    row = json.loads(payloads[0])
    assert row["cadquery_source"], "Worker did not naturally load CadQuery before user code"
    assert Path(row["cadquery_source"]).resolve().is_relative_to(cq_root.resolve()), row
    assert row["worker_pid"] != os.getpid(), "Boundary must be in the actual worker"
    # CPython 3.10+ perf_counter uses the same system clock across processes;
    # require that supported runtime and retain both implementations as evidence.
    assert sys.implementation.name == "cpython" and sys.version_info >= (3, 10)
    parent_clock = vars(time.get_clock_info("perf_counter"))
    assert row["clock"]["implementation"] == parent_clock["implementation"], row
    assert row["clock"]["monotonic"] and parent_clock["monotonic"], row
    assert started_ns < row["boundary_ns"] <= finished_ns, row
    row.update(
        startup_to_user_code_seconds=(row["boundary_ns"] - started_ns) / 1e9,
        process_seconds=(finished_ns - started_ns) / 1e9,
        worker_exit_code=completed.returncode,
        parent_clock=parent_clock,
        parent_started_ns=started_ns,
        parent_finished_ns=finished_ns,
        command=args,
    )
    return row, completed.stdout + "\nSTDERR:\n" + completed.stderr


def metrics(rows, count):
    pairs = [{label: next(row for row in rows
                           if row["pair"] == pair and row["variant"] == label)
              for label in ["base", "pr"]} for pair in range(count)]
    key = "startup_to_user_code_seconds"
    differences = [pair["base"][key] - pair["pr"][key] for pair in pairs]
    randomizer = random.Random(2097)
    bootstraps = sorted(statistics.median(randomizer.choices(differences, k=count))
                        for _ in range(5000))
    positive = sum(value > 0 for value in differences)
    negative = sum(value < 0 for value in differences)
    nonzero = positive + negative
    smaller = min(positive, negative)
    sign_p = min(1.0, 2 * sum(math.comb(nonzero, k) for k in range(smaller + 1)) / 2 ** nonzero)
    return {
        "base_median": statistics.median(pair["base"][key] for pair in pairs),
        "pr_median": statistics.median(pair["pr"][key] for pair in pairs),
        "paired_saving_median": statistics.median(differences),
        "paired_saving_bootstrap_95_interval": [bootstraps[125], bootstraps[4874]],
        "positive_pairs": positive, "negative_pairs": negative,
        "sign_test_two_sided_p": sign_p,
    }


def measure(project, work, folder):
    assert os.environ.get("GITHUB_ACTIONS") == "true", "Consumer probes run only in CI"
    assert project["id"] in ROUTES
    count = 15
    roots = {}
    for label in ["base", "pr"]:
        root = work / ("worker-startup-import-" + label)
        root.mkdir()
        shutil.copytree(work / label / "cadquery", root / "cadquery")
        roots[label] = root
    rows, failures = [], []
    for pair in range(-1, count):
        for label in (["base", "pr"] if pair % 2 == 0 else ["pr", "base"]):
            consumer = work / f"worker-startup-consumer-{pair}-{label}"
            shutil.copytree(work / "consumer", consumer)
            cwd = work / f"worker-startup-run-{pair}-{label}"
            cwd.mkdir()
            env = dict(os.environ)
            env.update(project.get("startup_env", {}))
            env["PYTHONPATH"] = os.pathsep.join([
                str(roots[label]), str(consumer), str(consumer / "src")])
            env["COMPAT_CQ_ROOT"] = str(roots[label])
            env["CQ_COMPAT_VARIANT"] = label
            log = folder / f"worker-startup-{pair}-{label}.log"
            try:
                row, output = one_sample(project, consumer, cwd, env, roots[label])
                log.write_text(output, encoding="utf-8")
                row.update(pair=pair, variant=label)
                rows.append(row)
            except (AssertionError, ValueError, OSError, subprocess.TimeoutExpired) as error:
                if isinstance(error, subprocess.TimeoutExpired):
                    def text(value):
                        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
                    log.write_text(text(error.stdout) + text(error.stderr) + "\nTIMEOUT\n", encoding="utf-8")
                else:
                    log.write_text(f"{type(error).__name__}: {error}\n", encoding="utf-8")
                failures.append({"pair": pair, "variant": label,
                                 "error": f"{type(error).__name__}: {error}"})
        if failures:
            break
    result = {
        "id": project["id"], "name": project["name"],
        "mode": "actual-worker-startup-to-user-code", **ROUTES[project["id"]],
        "timer": "Parent perf_counter_ns immediately before subprocess.run to worker timestamp at first submitted-model statements, after its natural CadQuery import. Same-host monotonic clock, includes interpreter and worker setup, excludes model geometry, export and shutdown.",
        "observer": "Submitted no-geometry model reads sys.modules; no import hooks, tracing, preload or edits to original worker source. Timestamp follows four stdlib observation imports; their cost is included.",
        "limits": "No result object is submitted. Missing-result/no-shapes report after the boundary is expected and not a compatibility verdict. Exit code and full logs retained. Does not measure pure CadQuery import time, complete application startup or model runtime.",
        "cache_scope": "One warm-up pair; warm file caches, fresh interpreter and consumer copy each sample",
        "pairs": count, "warmup_pairs": 1,
        "order": "Alternating base/pr then pr/base", "probe": PROBE,
        "samples": rows, "failures": failures,
    }
    if failures:
        result["classification"] = "worker-startup-unavailable"
    else:
        result["metrics"] = metrics(rows, count)
        interval = result["metrics"]["paired_saving_bootstrap_95_interval"]
        result["classification"] = ("measured-worker-startup-beneficiary" if interval[0] > 0 else
                                    "measured-worker-startup-slowdown" if interval[1] < 0 else
                                    "inconclusive-worker-startup-timing")
    (folder / "worker-startup.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
