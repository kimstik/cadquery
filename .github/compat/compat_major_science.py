"""Bounded scientific consumer routes with unchanged source and natural imports.

No transport simulation, dependency preloading, source shims or alternate providers.
ParaStell is explicitly host-blocked in the accompanying manifest.
"""

import faulthandler
import json
import math
import os
from pathlib import Path
import pickle
import shutil
import subprocess
import sys
import traceback
from types import SimpleNamespace
import xml.etree.ElementTree as ET


def provenance(source, package):
    consumer = sys.modules[package]
    actual = Path(consumer.__file__).resolve()
    assert actual.is_relative_to(source), (actual, source)
    cq_actual = Path(sys.modules["cadquery"].__file__).resolve()
    expected = Path(os.environ["COMPAT_CQ_ROOT"]).resolve()
    assert cq_actual.is_relative_to(expected), (cq_actual, expected)
    record = {"consumer_source": str(actual), "cadquery_source": str(cq_actual)}
    print(json.dumps(record), flush=True)
    return record


def artifacts_folder():
    folder = Path(os.environ["COMPAT_RESULT_DIR"]) / (
        os.environ["CQ_COMPAT_VARIANT"] + "-science-artifacts"
    )
    folder.mkdir(exist_ok=True)
    return folder


def cad_to_dagmc(source):
    paths = [
        "tests/test_get_volumes.py::test_get_volumes",
        "tests/test_file_creation.py::test_h5m_with_single_volume_list[gmsh]",
        "tests/test_file_creation.py::test_h5m_with_single_volume_2[gmsh]",
        "tests/test_file_creation.py::test_h5m_with_multi_volume_not_touching[gmsh]",
        "tests/test_file_creation.py::test_gmsh_mesh_with_single_volume_list",
        "tests/test_file_creation.py::test_gmsh_mesh_with_single_volume_2",
        "tests/test_file_creation.py::test_gmsh_mesh_with_multi_volume_not_touching",
        "tests/test_file_creation.py::test_gmsh_mesh_with_multi_volume_touching",
    ]
    report = Path(os.environ["COMPAT_RESULT_DIR"]) / (
        os.environ["CQ_COMPAT_VARIANT"] + "-native-tests.xml"
    )
    bootstrap = """import json, os, sys, pytest
from pathlib import Path
code = pytest.main(sys.argv[1:])
record = {}
for name, root in [('cadquery', Path(os.environ['COMPAT_CQ_ROOT'])), ('cad_to_dagmc', Path.cwd())]:
    module = sys.modules.get(name)
    actual = Path(module.__file__).resolve() if module is not None else None
    if actual is not None:
        assert actual.is_relative_to(root.resolve()), (actual, root)
    elif code == 0:
        raise AssertionError('Native tests did not load ' + name)
    record[name] = str(actual) if actual is not None else None
folder = Path(os.environ['COMPAT_RESULT_DIR'])
(folder / (os.environ['CQ_COMPAT_VARIANT'] + '-native-provenance.json')).write_text(json.dumps(record, indent=2) + '\\n')
print(json.dumps(record), flush=True)
sys.exit(code)
"""
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", H5M_BACKEND="h5py")
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", bootstrap,
         "-ra", "--import-mode=prepend", "--junitxml=" + str(report), *paths],
        cwd=source, env=env, timeout=1500,
    )
    assert result.returncode == 0, f"Native pytest exited {result.returncode}"
    suites = ET.parse(report).getroot()
    counts = {
        key: sum(int(s.get(key, 0)) for s in suites.iter("testsuite"))
        for key in ("tests", "failures", "errors", "skipped")
    }
    assert counts == {"tests": 10, "failures": 0, "errors": 0, "skipped": 0}, counts

    # Tests already assert material/volume mapping through their own h5py reader.
    # Also keep real mesh/CAD-conversion artifacts and inspect nonempty datasets.
    import h5py

    outputs = [
        "tests/single_cube.h5m", "tests/curved_extrude.h5m",
        "tests/two_disconnected_cubes.h5m", "tests/single_cube.msh",
        "tests/curved_extrude.msh", "tests/two_disconnected_cubes.msh",
        "tests/two_connected_cubes.msh", "tests/multi_volume_cylinders.msh",
        "test2_3d.msh",
    ]
    destination = artifacts_folder()
    sizes = {}
    for name in outputs:
        artifact = source / name
        assert artifact.is_file() and artifact.stat().st_size > 100, artifact
        if artifact.suffix == ".h5m":
            with h5py.File(artifact, "r") as data:
                assert data["tstt/nodes/coordinates"].shape[0] > 0
                assert len(data["tstt/elements"]) > 0
        shutil.copy2(artifact, destination / artifact.name)
        sizes[name] = artifact.stat().st_size
    return {"native_tests": counts, "selection": paths, "artifact_bytes": sizes}


def cad_to_openmc(source):
    # These are ordinary importable package components, not exec/AST extraction
    # around assembly.py's mandatory MOAB import. The full assembly route is not tested.
    from CAD_to_OpenMC import cq_serialize
    from CAD_to_OpenMC.assemblymesher_cq import MesherCQSTL
    from CAD_to_OpenMC.stl_utils import read_stl

    record = provenance(source, "CAD_to_OpenMC.cq_serialize")
    provenance(source, "CAD_to_OpenMC.assemblymesher_cq")
    import cadquery as cq
    import numpy as np

    cq_serialize.register()
    box = cq.Workplane("XY").box(2, 3, 4).val()
    cylinder = cq.Workplane("XY").circle(1).extrude(3).val()
    solids = [box, cylinder]
    destination = artifacts_folder()
    for index, solid in enumerate(solids):
        restored = pickle.loads(pickle.dumps(solid))
        assert restored.isValid() and len(restored.Solids()) == 1
        assert math.isclose(restored.Volume(), solid.Volume(), rel_tol=1e-9)
        assert (restored.Center() - solid.Center()).Length < 1e-8
        path = destination / f"solid-{index}.brep"
        restored.exportBrep(str(path))
        assert path.stat().st_size > 100
        reimported = cq.Shape.importBrep(str(path))
        assert reimported.isValid()
        assert math.isclose(reimported.Volume(), solid.Volume(), rel_tol=1e-9)
    compound = cq.Compound.makeCompound(solids)
    restored = pickle.loads(pickle.dumps(compound))
    assert restored.isValid() and len(restored.Solids()) == 2
    assert math.isclose(restored.Volume(), compound.Volume(), rel_tol=1e-9)
    location = cq.Location(cq.Vector(3, -4, 5), cq.Vector(0, 0, 1), 30)
    restored_location = pickle.loads(pickle.dumps(location))
    point = cq.Vertex.makeVertex(1, 2, 3)
    assert (point.moved(location).Center() - point.moved(restored_location).Center()).Length < 1e-9
    vector = cq.Vector(2, -3, 4)
    assert pickle.loads(pickle.dumps(vector)).toTuple() == vector.toTuple()

    # The mesher's input protocol is entities with a .solid attribute. These
    # fixtures supply real CQ solids, not mocked dependency modules or results.
    entities = [SimpleNamespace(solid=solid) for solid in solids]
    mesher = MesherCQSTL(
        tolerance=0.02, angular_tolerance=0.1, min_mesh_size=0.1,
        max_mesh_size=1, default=True, refine=False, threads=1, entities=entities,
    )
    filenames = mesher.generate_stls()
    assert len(filenames) == 2, filenames
    triangles = []
    for index, name in enumerate(filenames):
        path = Path(name)
        assert path.is_file() and path.stat().st_size > 100, path
        facets, count = read_stl(str(path))
        assert count > 0 and len(facets) == count
        vertices = np.asarray([face[3:12] for face in facets]).reshape(-1, 3)
        assert np.isfinite(vertices).all()
        bbox = solids[index].BoundingBox()
        expected_min = (bbox.xmin, bbox.ymin, bbox.zmin)
        expected_max = (bbox.xmax, bbox.ymax, bbox.zmax)
        assert np.allclose(vertices.min(axis=0), expected_min, atol=0.03)
        assert np.allclose(vertices.max(axis=0), expected_max, atol=0.03)
        shutil.copy2(path, destination / path.name)
        triangles.append(count)
    return {"scenario": "Own BREP/pickle geometry and location roundtrips, own CQ STL mesher",
            "mesh_triangles": triangles, "provenance": record,
            "exclusion": "No full Assembly/MOAB/DAGMC/OpenMC transport certification"}


def main():
    faulthandler.enable()
    project_id = int(sys.argv[1])
    source = Path(sys.argv[2]).resolve()
    handlers = {86: cad_to_dagmc, 88: cad_to_openmc}
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
