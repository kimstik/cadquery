"""Paired fresh-process import measurements, separate from compatibility runs."""

import json
import os
from pathlib import Path
import random
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
assert cq is not None, "The consumer entrypoint did not import CadQuery"
assert Path(cq.__file__).resolve().is_relative_to(Path(os.environ["COMPAT_CQ_ROOT"]).resolve())
print("COMPAT_STARTUP=" + json.dumps({
    "import_seconds": elapsed,
    "cadquery_source": cq.__file__,
    "ezdxf_loaded": "ezdxf" in sys.modules,
    "vtk_loaded": any(name == "vtkmodules" or name.startswith("vtkmodules.") for name in sys.modules),
    "module_count": len(sys.modules),
}))
'''


def measure(project, work, folder):
    statement = project["startup_import"]
    environments = {}
    for label in ["base", "pr"]:
        env = dict(os.environ)
        root = work / label
        consumer = work / "consumer"
        env["PYTHONPATH"] = os.pathsep.join([str(root), str(consumer), str(consumer / "src")])
        env["COMPAT_CQ_ROOT"] = str(root)
        environments[label] = env
    rows = []
    # Warm file caches symmetrically; every import still starts in a fresh interpreter.
    for pair in range(-1, 15):
        order = ["base", "pr"] if pair % 2 == 0 else ["pr", "base"]
        for label in order:
            started = time.perf_counter()
            completed = subprocess.run(
                [sys.executable, "-c", CHILD, statement], env=environments[label],
                cwd=work, capture_output=True, text=True, timeout=120,
            )
            wall = time.perf_counter() - started
            (folder / f"startup-{pair}-{label}.log").write_text(
                completed.stdout + completed.stderr, encoding="utf-8")
            if completed.returncode:
                raise RuntimeError(f"Startup {label} exited {completed.returncode}, pair {pair}")
            payload = [line.removeprefix("COMPAT_STARTUP=") for line in completed.stdout.splitlines()
                       if line.startswith("COMPAT_STARTUP=")]
            assert len(payload) == 1, "Missing or ambiguous startup result"
            row = json.loads(payload[0])
            row.update(pair=pair, variant=label, process_seconds=wall)
            rows.append(row)
    measured = [row for row in rows if row["pair"] >= 0]
    pairs = [{label: next(row for row in measured if row["pair"] == pair and row["variant"] == label)
              for label in ["base", "pr"]} for pair in range(15)]
    # Descriptive uncertainty for this runner only, not a population-level guarantee.
    randomizer = random.Random(2097)
    metrics = {}
    for key in ["import_seconds", "process_seconds"]:
        differences = [pair["base"][key] - pair["pr"][key] for pair in pairs]
        bootstraps = sorted(statistics.median(randomizer.choices(differences, k=len(differences)))
                            for _ in range(5000))
        metrics[key] = {
            "base_median": statistics.median(pair["base"][key] for pair in pairs),
            "pr_median": statistics.median(pair["pr"][key] for pair in pairs),
            "paired_saving_median": statistics.median(differences),
            "paired_saving_bootstrap_95_interval": [bootstraps[125], bootstraps[4874]],
        }
    result = {
        "id": project["id"], "name": project["name"], "statement": statement,
        "scope": "Consumer import in a fresh interpreter, warm file caches; not full application startup or geometry execution",
        "pairs": 15, "warmup_pairs": 1, "order": "alternating base/pr, then pr/base",
        "metrics": metrics, "samples": rows,
        "interpretation": "Positive saving means PR faster in this measured environment; inspect module loads and interval",
    }
    (folder / "startup.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
