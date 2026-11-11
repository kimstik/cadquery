"""Pinned engineering consumers, using their own unchanged CAD entrypoints.

Scopes are deliberately narrower than full application compatibility. Cases run
in fresh subprocesses so fixture creation cannot preload the consumer's imports.
"""

import faulthandler
import importlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import traceback
import xml.etree.ElementTree as ET


VAMS_ROOT = Path("backendPipelines/conversion/meshCadMetadataExtraction/lambdaContainer")


def provenance(source, package):
    consumer = sys.modules[package]
    actual_consumer = Path(consumer.__file__).resolve()
    assert actual_consumer.is_relative_to(source), (actual_consumer, source)
    cq = sys.modules.get("cadquery")
    assert cq is not None, "Consumer did not load CadQuery"
    actual = Path(cq.__file__).resolve()
    expected = Path(os.environ["COMPAT_CQ_ROOT"]).resolve()
    assert actual.is_relative_to(expected), (actual, expected)
    print(json.dumps({"consumer_source": str(actual_consumer), "cadquery_source": str(actual)}), flush=True)


def validate(shape):
    assert shape.isValid(), "Invalid geometry"
    assert shape.Solids(), "No solids"
    assert math.isfinite(shape.Volume()) and shape.Volume() > 0, shape.Volume()
    return shape


def aerosandbox_native(source):
    report = Path(os.environ["COMPAT_RESULT_DIR"]) / (
        os.environ["CQ_COMPAT_VARIANT"] + "-native-tests.xml"
    )
    bootstrap = """import json, os, sys, pytest
from pathlib import Path
code = pytest.main(sys.argv[1:])
record = {}
for name in ('aerosandbox', 'cadquery'):
    module = sys.modules.get(name)
    actual = str(Path(module.__file__).resolve()) if module is not None else None
    expected = Path.cwd().resolve() if name == 'aerosandbox' else Path(os.environ['COMPAT_CQ_ROOT']).resolve()
    if actual is not None:
        assert Path(actual).is_relative_to(expected), (actual, str(expected))
    elif code == 0:
        raise AssertionError('Native export test did not import ' + name)
    record[name] = actual
folder = Path(os.environ['COMPAT_RESULT_DIR'])
(folder / (os.environ['CQ_COMPAT_VARIANT'] + '-native-provenance.json')).write_text(json.dumps(record, indent=2) + '\\n')
print(json.dumps(record), flush=True)
sys.exit(code)
"""
    test = "aerosandbox/geometry/test_geometry/test_cadquery_export.py"
    assert (source / test).is_file()
    completed = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", bootstrap,
         "-ra", "--junitxml=" + str(report), test],
        cwd=source,
        env=dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", MPLBACKEND="Agg"),
        timeout=1100,
    )
    assert completed.returncode == 0, f"Native CAD export pytest exited {completed.returncode}"
    suites = ET.parse(report).getroot()
    totals = {key: sum(int(s.get(key, 0)) for s in suites.iter("testsuite"))
              for key in ("tests", "failures", "errors", "skipped")}
    assert totals["tests"] > totals["skipped"], totals
    return {"native_tests": totals, "selection": test}


def dactyl_helpers(source, work):
    sys.path.insert(0, str(source / "src"))
    helper = importlib.import_module("helpers_cadquery")
    provenance(source, "helpers_cadquery")
    # These are the backend's actual modeling helpers, not a whole keyboard build.
    for index in range(2):
        plate = helper.box(24, 18, 4)
        bore = helper.cylinder(2, 8)
        result = helper.difference(plate, [bore])
        shape = validate(result.val())
        assert math.isclose(shape.Volume(), 24 * 18 * 4 - math.pi * 2 ** 2 * 4, rel_tol=1e-5)
        stem = work / f"dactyl-{index}"
        helper.export_file(result, str(stem))
        assert stem.with_suffix(".step").stat().st_size > 100
        restored = validate(helper.import_file(str(stem)).val())
        assert math.isclose(shape.Volume(), restored.Volume(), rel_tol=1e-5)
        section = result.section()
        helper.export_dxf(section, str(stem))
        assert stem.with_suffix(".dxf").stat().st_size > 100
        import ezdxf
        document = ezdxf.readfile(str(stem.with_suffix(".dxf")))
        assert len(document.modelspace()) > 0
    return {"cycles": 2, "scope": "Own helpers: plate/bore difference, STEP export/import and section DXF export; not complete keyboard generator"}


def turbodesigner_geometry(source, work):
    # The pinned project declares cq_warehouse as a git submodule. The manifest
    # installs that exact revision, not a newer substitute or a compatibility shim.
    common = importlib.import_module("turbodesigner.cad.common")
    provenance(source, "turbodesigner.cad.common")
    import cadquery as cq
    ring = common.ExtendedWorkplane("XY").ring(10, 2, 4)
    shape = validate(ring.val())
    assert math.isclose(shape.Volume(), math.pi * (10 ** 2 - 8 ** 2) * 4, rel_tol=1e-6)
    assembly = common.colored_assembly(common.CadColors.BLADE)
    assembly.add(ring, name="Ring")
    output = work / "turbodesigner-ring.step"
    assembly.export(str(output))
    assert output.stat().st_size > 100
    restored = validate(cq.importers.importStep(str(output)).val())
    assert math.isclose(shape.Volume(), restored.Volume(), rel_tol=1e-5)
    return {"scope": "Actual CAD package initialization, ExtendedWorkplane.ring and colored_assembly STEP export; not a complete compressor"}


def vams_fixture(work):
    import cadquery as cq
    import ezdxf
    cq.exporters.export(cq.Workplane("XY").box(10, 5, 3), str(work / "fixture.step"))
    document = ezdxf.new("R2010")
    document.modelspace().add_lwpolyline([(0, 0), (10, 0), (10, 5), (0, 5)], close=True)
    document.saveas(work / "fixture.dxf")
    return {"scope": "Input fixture creation only, separate process"}


def vams_metadata(source, work, extension):
    root = source / VAMS_ROOT
    assert (root / "metadata_extractors/cad_extractor.py").is_file()
    sys.path.insert(0, str(root))
    extractor = importlib.import_module("metadata_extractors.cad_extractor")
    provenance(source, "metadata_extractors.cad_extractor")
    metadata = extractor.extract_cad_metadata(str(work / ("fixture." + extension)))
    assert isinstance(metadata, dict) and metadata, metadata
    geometry = metadata["geometric_metadata"]
    assert "extraction_error" not in geometry, geometry
    dimensions = geometry["dimensions"]
    assert math.isclose(dimensions["width"], 10, abs_tol=1e-5), dimensions
    assert math.isclose(dimensions["height"], 5, abs_tol=1e-5), dimensions
    if extension == "step":
        assert math.isclose(dimensions["depth"], 3, abs_tol=1e-5), dimensions
        assert math.isclose(geometry["volume"], 150, rel_tol=1e-5), geometry
        assert metadata["shape_statistics"]["solids"] == 1, metadata
    else:
        assert metadata["entity_statistics"]["wires"] >= 1, metadata
        assert metadata["entity_statistics"]["edges"] >= 4, metadata
    (work / (extension + "-metadata.json")).write_text(json.dumps(metadata, indent=2) + "\n")
    return {"format": extension, "metadata": metadata,
            "scope": "Real local-file Lambda CAD metadata handler and logger, no AWS requests or mocks"}


def child(case, source, work):
    faulthandler.enable()
    if case == "aerosandbox-native":
        return aerosandbox_native(source)
    if case == "dactyl-helpers":
        return dactyl_helpers(source, work)
    if case == "turbo-cad":
        return turbodesigner_geometry(source, work)
    if case == "vams-fixture":
        return vams_fixture(work)
    if case == "vams-step":
        return vams_metadata(source, work, "step")
    if case == "vams-dxf":
        return vams_metadata(source, work, "dxf")
    raise ValueError(case)


def main(project_id, source):
    source = source.resolve()
    folder = Path(os.environ["COMPAT_RESULT_DIR"]) / (
        os.environ["CQ_COMPAT_VARIANT"] + "-engineering"
    )
    folder.mkdir()
    cases = {78: ["aerosandbox-native"], 79: ["dactyl-helpers"],
             80: ["turbo-cad"], 81: ["vams-fixture", "vams-dxf", "vams-step"]}[project_id]
    results = []
    for case in cases:
        command = [sys.executable, "-X", "faulthandler", str(Path(__file__).resolve()),
                   "--case", case, str(source), str(folder)]
        with (folder / (case + ".log")).open("w") as log:
            try:
                completed = subprocess.run(command, cwd=folder, stdout=log,
                                           stderr=subprocess.STDOUT, timeout=1150,
                                           env=dict(os.environ, MPLBACKEND="Agg"))
                code = completed.returncode
            except subprocess.TimeoutExpired:
                code = 124
                log.write("\nTIMEOUT\n")
        result = {"case": case, "exit_code": code, "passed": code == 0}
        results.append(result)
        print(json.dumps(result), flush=True)
    (folder / "summary.json").write_text(json.dumps(results, indent=2) + "\n")
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    if sys.argv[1] == "--case":
        try:
            record = child(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]))
            print(json.dumps(record), flush=True)
        except Exception:
            traceback.print_exc()
            sys.exit(1)
    else:
        sys.exit(main(int(sys.argv[1]), Path(sys.argv[2])))
