"""Paired fresh-process consumer imports, separate from compatibility verdicts."""

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


CHILD = r'''
import json
import os
from pathlib import Path
import sys
import time

statement = sys.argv[1]
before = time.perf_counter()
exec(statement)
elapsed = time.perf_counter() - before
cq = sys.modules.get("cadquery")
if cq is not None:
    assert Path(cq.__file__).resolve().is_relative_to(Path(os.environ["COMPAT_CQ_ROOT"]).resolve())
print("COMPAT_STARTUP=" + json.dumps({
    "import_seconds": elapsed,
    "cadquery_source": cq.__file__ if cq is not None else None,
    "ezdxf_loaded": "ezdxf" in sys.modules,
    "vtk_loaded": any(name == "vtkmodules" or name.startswith("vtkmodules.") for name in sys.modules),
    "module_count": len(sys.modules),
}))
'''


def measure(project, work, folder):
    statement = project["startup_import"]
    environments = {}
    directories = {}
    for label in ["base", "pr"]:
        env = dict(os.environ)
        env.update(project.get("startup_env", {}))
        # Match installed-package visibility, not the checkout's tests/mypy tooling.
        root = work / ("startup-import-" + label)
        root.mkdir()
        shutil.copytree(work / label / "cadquery", root / "cadquery")
        env["COMPAT_CQ_ROOT"] = str(root)
        env["CQ_COMPAT_VARIANT"] = label
        environments[label] = env
        directories[label] = root
    rows = []
    failures = []
    # Symmetric warm-up; all measured imports still start in fresh interpreters.
    for pair in range(-1, 15):
        order = ["base", "pr"] if pair % 2 == 0 else ["pr", "base"]
        for label in order:
            # An import may write files. Never let one sample mutate another's source.
            consumer = work / f"startup-consumer-{pair}-{label}"
            shutil.copytree(work / "consumer", consumer)
            env = dict(environments[label])
            paths = [str(directories[label]), str(consumer), str(consumer / "src")]
            paths.extend(str(consumer / path) for path in project.get("startup_paths", []))
            env["PYTHONPATH"] = os.pathsep.join(paths)
            env["COMPAT_CONSUMER_ROOT"] = str(consumer)
            cwd = consumer / project.get("startup_cwd", "")
            assert cwd.resolve().is_relative_to(consumer.resolve())
            assert cwd.is_dir(), str(cwd)
            started = time.perf_counter()
            try:
                completed = subprocess.run(
                    [sys.executable, "-c", CHILD, statement], env=env,
                    cwd=cwd, capture_output=True, text=True, timeout=120,
                )
                wall = time.perf_counter() - started
                (folder / f"startup-{pair}-{label}.log").write_text(
                    completed.stdout + completed.stderr, encoding="utf-8")
                if completed.returncode:
                    raise RuntimeError(f"Startup exited {completed.returncode}")
                payload = [line.removeprefix("COMPAT_STARTUP=")
                           for line in completed.stdout.splitlines()
                           if line.startswith("COMPAT_STARTUP=")]
                assert len(payload) == 1, "Missing or ambiguous startup result"
                row = json.loads(payload[0])
                row.update(pair=pair, variant=label, process_seconds=wall)
                rows.append(row)
            except (subprocess.TimeoutExpired, RuntimeError, AssertionError, ValueError) as exc:
                failures.append({"pair": pair, "variant": label, "error": str(exc)})
        if failures:
            # Keep both variants' first outcomes, without spending 15 pairs on a broken route.
            break
    result = {
        "id": project["id"], "name": project["name"], "statement": statement,
        "scope": project.get("startup_scope", "Consumer import, not full application startup"),
        "cache_scope": "Warm OS/file caches after one warm-up pair; fresh interpreter each sample",
        "pairs": 15, "warmup_pairs": 1, "order": "alternating base/pr, then pr/base",
        "samples": rows, "failures": failures,
        "interpretation": "Import timing includes the stated entrypoint only. Separate process timing includes interpreter startup and shutdown. Not a whole-project compatibility verdict or population timing estimate.",
    }
    if failures:
        result["classification"] = "startup-unavailable"
    else:
        measured = [row for row in rows if row["pair"] >= 0]
        pairs = [{label: next(row for row in measured
                             if row["pair"] == pair and row["variant"] == label)
                  for label in ["base", "pr"]} for pair in range(15)]
        randomizer = random.Random(2097)
        metrics = {}
        for key in ["import_seconds", "process_seconds"]:
            differences = [pair["base"][key] - pair["pr"][key] for pair in pairs]
            bootstraps = sorted(statistics.median(randomizer.choices(differences, k=len(differences)))
                                for _ in range(5000))
            positive = sum(value > 0 for value in differences)
            negative = sum(value < 0 for value in differences)
            nonzero = positive + negative
            smaller = min(positive, negative)
            sign_p = min(1.0, 2 * sum(math.comb(nonzero, k) for k in range(smaller + 1)) / 2 ** nonzero)
            metrics[key] = {
                "sign_test_two_sided_p": sign_p,
                "positive_pairs": positive, "negative_pairs": negative,
                "base_median": statistics.median(pair["base"][key] for pair in pairs),
                "pr_median": statistics.median(pair["pr"][key] for pair in pairs),
                "paired_saving_median": statistics.median(differences),
                "paired_saving_bootstrap_95_interval": [bootstraps[125], bootstraps[4874]],
            }
        result["metrics"] = metrics
        loaded = all(row["cadquery_source"] is not None for row in measured)
        interval = metrics["import_seconds"]["paired_saving_bootstrap_95_interval"]
        if not loaded:
            result["classification"] = "no-attributable-cq-import"
        elif interval[0] > 0:
            result["classification"] = "measured-import-beneficiary"
        elif interval[1] < 0:
            result["classification"] = "measured-import-slowdown"
        else:
            result["classification"] = "inconclusive-timing"
    (folder / "startup.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
