"""Behavioral probes for pinned CadQuery ecosystem sources.

Usage: python compat_plugins.py INVENTORY_ID SOURCE_DIRECTORY
Run only on disposable CI runners. Consumers are not modified.
"""

import importlib
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback


# Reuse the consumer's own assertions where they exercise a small real model.
PYTEST_CASES = {
    1: ["tests/test_apply_to_each_face.py"],
    2: [
        "tests/test_cq_cache.py::test_cache_unique",
        "tests/test_cq_cache.py::test_cache_type_return",
        "tests/test_cq_cache.py::test_cache_type_return_with_modified_function",
    ],
    3: ["tests/test_fragment.py"],
    4: [
        "tests/test_freecad_importer.py::test_static_import",
        "tests/test_freecad_importer.py::test_get_parameters",
    ],
    6: ["tests/test_heatserts.py::TestSizes::test_heatsert_makes_a_hole"],
    8: ["tests/test_more_selectors.py"],
    9: ["tests/test_sampleplugin.py"],
    10: ["tests/test_teardrop.py"],
    23: ["tests/test_sphinx_build.py"],
    27: ["tests/test_meshes.py::test_simple_assembly"],
    34: ["tests/test_assembly.py::test_assembly_is_not_blank"],
    36: ["tests/test_import.py::test_static_import"],
    37: [
        "tests/test_assemblies.py::test_basic_assembly",
        "tests/test_assemblies.py::test_basic_multipart_assembly",
        "tests/test_assemblies.py::test_edge_handling",
        "tests/test_assemblies.py::test_vertex_handling",
    ],
    43: ["tests/test_overlays.py", "tests/test_dimensions.py"],
}


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def valid_model(result):
    import cadquery as cq

    shapes = result.vals() if isinstance(result, cq.Workplane) else [result]
    assert shapes, "The operation returned no geometry"
    for shape in shapes:
        assert isinstance(shape, cq.Shape)
        assert shape.isValid(), "The operation returned invalid geometry"
    volume = sum(shape.Volume() for shape in shapes)
    assert math.isfinite(volume) and volume > 0, "No positive finite volume"
    return volume


def custom_probe(entry, source):
    import cadquery as cq

    if entry == 5:
        # README Gear(m, z, b).build(): use a small straight gear.
        from plugins.gear_generator.gear_generator import Gear

        gear = Gear(1.0, 12, 2.0).build()
        volume = valid_model(gear)
        cq.exporters.export(gear, "gear.step")
        assert Path("gear.step").stat().st_size > 0
        return {"scenario": "small straight gear and STEP export", "volume": volume}
    if entry == 7:
        # Documented lower-case selectors follow the current workplane axes.
        import plugins.localselectors.localselectors  # noqa: F401

        box = cq.Workplane().box(10, 12, 14)
        face = box.faces(">x")
        assert face.size() == 1
        assert face.val().Center().x == 5
        wp = face.workplane()
        extended = wp.rect(2, 2).extrude(2)
        assert valid_model(extended) > valid_model(box)
        selected = extended.faces(">z")
        assert selected.size() > 0
        return {"scenario": "local face selectors and extrusion"}
    if entry == 32:
        path = source if source.is_file() else source / "metric_threads.py"
        module = load_file("metric_threads", path)
        # A single turn exercises the real helix/sweep code, not just an envelope.
        external = module.external_metric_thread(6.0, 1.0, 1.0)
        internal = module.internal_metric_thread(6.0, 1.0, 1.0, base_tube_od=10.0)
        volumes = [valid_model(external), valid_model(internal)]
        cq.exporters.export(external, "thread.step")
        assert Path("thread.step").stat().st_size > 0
        return {"scenario": "external and internal metric threads", "volumes": volumes}
    if entry == 35:
        import cqwild
        from cqwild.print import steppedHoleDownwards

        box = cqwild.WP("XY").box(20, 20, 10)
        drilled = steppedHoleDownwards(
            box.faces(">Z").workplane(), 8.0, 3.0, 4.0, 10.0
        )
        volume = valid_model(drilled)
        assert volume < valid_model(box)
        cq.exporters.export(drilled, "stepped-hole.step")
        assert Path("stepped-hole.step").stat().st_size > 0
        return {"scenario": "printable stepped hole and STEP export", "volume": volume}
    raise ValueError(f"No behavioral probe for entry {entry}")


def main():
    entry = int(sys.argv[1])
    source = Path(sys.argv[2]).resolve()
    root = source if source.is_dir() else source.parent
    paths = [str(root), str(root / "src")]
    sys.path[:0] = paths
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(paths + [env.get("PYTHONPATH", "")])
    env.setdefault("OMP_NUM_THREADS", "1")
    env.setdefault("OPENBLAS_NUM_THREADS", "1")
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    if entry in PYTEST_CASES:
        # Run tests in their original repository: fixtures use relative assets.
        command = [
            sys.executable, "-m", "pytest", "-q", "--tb=short",
            "-p", "no:cacheprovider", *PYTEST_CASES[entry],
        ]
        result = subprocess.run(command, cwd=root, env=env, timeout=240)
        print(json.dumps({"id": entry, "scenario": "consumer tests", "tests": PYTEST_CASES[entry], "returncode": result.returncode}), flush=True)
        return result.returncode
    with tempfile.TemporaryDirectory(prefix=f"cq-compat-{entry}-") as tmp:
        os.chdir(tmp)
        try:
            details = custom_probe(entry, source)
        except Exception as exc:
            traceback.print_exc()
            print(json.dumps({"id": entry, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}), flush=True)
            return 1
        print(json.dumps({"id": entry, "status": "passed", **details}), flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
