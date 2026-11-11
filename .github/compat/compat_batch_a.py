"""Six pinned downstream consumers: native suites and actual geometry workflows.

No compatibility shims, dependency preloads or consumer source modifications.
The caller selects the CadQuery checkout through PYTHONPATH.
"""

import faulthandler
import json
import math
import os
from pathlib import Path
import runpy
import subprocess
import sys
import traceback
import xml.etree.ElementTree as ET


def provenance(source, package):
    consumer = sys.modules[package]
    consumer_file = Path(consumer.__file__).resolve()
    assert consumer_file.is_relative_to(source), (consumer_file, source)
    print(json.dumps({"consumer_source": str(consumer_file)}), flush=True)
    module = sys.modules.get("cadquery")
    assert module is not None, "Consumer did not load CadQuery"
    actual = Path(module.__file__).resolve()
    expected = Path(os.environ["COMPAT_CQ_ROOT"]).resolve()
    assert actual.is_relative_to(expected), (actual, expected)
    print(json.dumps({"cadquery_source": str(actual)}), flush=True)


def native(source, paths, package):
    report = Path(os.environ["COMPAT_RESULT_DIR"]) / (
        os.environ["CQ_COMPAT_VARIANT"] + "-native-tests.xml"
    )
    bootstrap = """import json, os, sys, pytest
from pathlib import Path
code = pytest.main(sys.argv[1:])
module = sys.modules.get('cadquery')
actual = str(Path(module.__file__).resolve()) if module is not None else None
expected = Path(os.environ['COMPAT_CQ_ROOT']).resolve()
if actual is not None:
    assert Path(actual).is_relative_to(expected), (actual, str(expected))
elif code == 0:
    raise AssertionError('Native tests did not load CadQuery')
consumer = sys.modules.get(os.environ['COMPAT_CONSUMER_PACKAGE'])
consumer_file = str(Path(consumer.__file__).resolve()) if consumer is not None else None
if consumer_file is not None:
    assert Path(consumer_file).is_relative_to(Path.cwd().resolve()), consumer_file
elif code == 0:
    raise AssertionError('Native tests did not load the selected consumer package')
record = {'cadquery_source': actual, 'expected_cadquery_root': str(expected), 'consumer_source': consumer_file}
folder = Path(os.environ['COMPAT_RESULT_DIR'])
(folder / (os.environ['CQ_COMPAT_VARIANT'] + '-native-provenance.json')).write_text(json.dumps(record, indent=2) + '\\n')
print(json.dumps(record), flush=True)
sys.exit(code)
"""
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", bootstrap,
         "-ra", "--junitxml=" + str(report), *paths],
        cwd=source,
        env=dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", COMPAT_CONSUMER_PACKAGE=package),
        timeout=1100,
    )
    assert result.returncode == 0, f"Native pytest exited {result.returncode}"
    root = ET.parse(report).getroot()
    counts = {
        key: sum(int(s.get(key, 0)) for s in root.iter("testsuite"))
        for key in ("tests", "failures", "errors", "skipped")
    }
    assert counts["tests"] > counts["skipped"], counts
    return {"native_tests": counts, "selection": paths}


def validate_shape(shape):
    assert shape.isValid(), "Invalid geometry"
    assert shape.Solids(), "No solids"
    assert math.isfinite(shape.Volume()) and shape.Volume() > 0, shape.Volume()
    return shape


def step_roundtrip(shape, output):
    import cadquery as cq

    validate_shape(shape)
    cq.exporters.export(shape, str(output))
    assert output.stat().st_size > 100, output
    restored = cq.importers.importStep(str(output)).val()
    validate_shape(restored)
    assert math.isclose(shape.Volume(), restored.Volume(), rel_tol=1e-5)


def paramak(source):
    return native(source, [
        "tests/test_assemblies/test_assembly.py",
        "tests/test_workplanes/test_poloidal_field_coil.py",
    ], "paramak")


def acis(source):
    # This module constructs its own analytic shapes and SAT/SAB roundtrips.
    # Unlike test_cadquery, it does not depend on an external corpus download.
    return native(source, ["tests/test_authoring.py"], "cq_acis")


def cam(source):
    return native(source, ["src/cq_cam/tests", "src/cq_cam/utils/tests"], "cq_cam")


def massembly(source):
    shown = []
    namespace = runpy.run_path(
        str(source / "examples/cq-editor/1-disk_arm.py"),
        init_globals={"show_object": lambda obj, **kwargs: shown.append(obj)},
    )
    provenance(source, "cadquery_massembly")
    assembly = namespace["disk_arm"]
    assert shown and shown[0] is assembly
    assert set(assembly.mates) == {"disk_pivot", "arm_pivot", "disk", "arm"}
    original = validate_shape(assembly.toCompound())
    assert len(original.Solids()) == 3
    assembly.assemble("arm", "arm_pivot")
    assembly.assemble("disk", "disk_pivot")
    for moving, target in (("arm", "arm_pivot"), ("disk", "disk_pivot")):
        first = assembly.mates[moving].world_mate.origin
        second = assembly.mates[target].world_mate.origin
        assert (first - second).Length < 1e-5, (moving, first, second)
    assembled = validate_shape(assembly.toCompound())
    assert math.isclose(original.Volume(), assembled.Volume(), rel_tol=1e-7)
    step_roundtrip(assembled, Path.cwd() / "mated-disk-arm.step")
    return {"scenario": "Unmodified disk-arm example, mate alignment and STEP roundtrip",
            "shown_objects": len(shown), "solids": len(assembled.Solids())}


def terrain(source):
    from cqterrain import Ladder

    provenance(source, "cqterrain")
    for index, height in enumerate((32, 48)):
        ladder = Ladder(length=20, width=4, height=height, rail_width=2)
        ladder.make()
        assert len(ladder.side_rails) == 2
        assert ladder.rungs is not None
        shape = validate_shape(ladder.build().val())
        bounds = shape.BoundingBox()
        assert math.isclose(bounds.zlen, height, rel_tol=1e-6), bounds.zlen
        step_roundtrip(shape, Path.cwd() / f"ladder-{index}.step")
    return {"scenario": "Own Ladder make/build at two heights and STEP roundtrips"}


def main():
    faulthandler.enable()
    project_id = int(sys.argv[1])
    source = Path(sys.argv[2]).resolve()
    handlers = {60: paramak, 61: acis, 62: cam, 64: massembly, 65: terrain}
    try:
        detail = handlers[project_id](source)
    except Exception:
        traceback.print_exc()
        print(json.dumps({"id": project_id, "status": "failed"}), flush=True)
        return 1
    print(json.dumps({"id": project_id, "status": "passed", **detail}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
