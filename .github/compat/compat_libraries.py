"""Small downstream geometry/export scenarios for the pinned compatibility matrix.

Run each ID in a fresh process, with the chosen CadQuery tree on PYTHONPATH.
Consumer source is unmodified. These are bounded integration scenarios, not
certification of every feature or host application.
"""

import json
import math
import os
from pathlib import Path
import sys
import tempfile
import traceback


def solid(shape):
    import cadquery as cq

    if isinstance(shape, cq.Workplane):
        shape = shape.val()
    assert isinstance(shape, cq.Shape), type(shape)
    assert shape.isValid(), "invalid result shape"
    assert shape.Solids(), "no solids"
    volume = shape.Volume()
    assert math.isfinite(volume) and volume > 0, volume
    return shape


def file_written(path):
    assert path.is_file() and path.stat().st_size > 100, str(path)


def step_roundtrip(shape, path):
    import cadquery as cq

    shape = solid(shape)
    cq.exporters.export(shape, str(path))
    file_written(path)
    restored = solid(cq.importers.importStep(str(path)))
    assert math.isclose(restored.Volume(), shape.Volume(), rel_tol=1e-5)


def warehouse(source, output):
    import cadquery as cq
    from cq_warehouse.sprocket import Sprocket

    sprocket = solid(Sprocket(num_teeth=12))
    step_roundtrip(sprocket, output / "sprocket.step")
    assembly = cq.Assembly().add(sprocket, name="sprocket")
    assembly.save(str(output / "sprocket-assembly.step"))
    file_written(output / "sprocket-assembly.step")
    return {"scenario": "sprocket geometry, STEP roundtrip, assembly STEP"}


def cqkit(source, output):
    from cqkit import drafted_box, export_step_file, import_step_file, export_stl_file

    workplane = drafted_box(4, 5, 3, draft_angle=2)
    shape = solid(workplane)
    export_step_file(workplane, str(output / "box.step"))
    file_written(output / "box.step")
    restored = solid(import_step_file(str(output / "box.step")))
    assert math.isclose(restored.Volume(), shape.Volume(), rel_tol=1e-5)
    export_stl_file(workplane, str(output / "box.stl"))
    file_written(output / "box.stl")
    return {"scenario": "drafted box, own STEP exporter/importer and STL exporter"}


def cqmore(source, output):
    from cqmore import Workplane

    shape = Workplane().makePolygon(((-2, -2), (2, -2), (0, 2))).extrude(1)
    shape = solid(shape)
    assert math.isclose(shape.Volume(), 8, rel_tol=1e-6)
    step_roundtrip(shape, output / "triangle.step")
    first = Workplane().rect(10, 10)
    second = Workplane().center(5, 5).rect(10, 10)
    intersection = solid(first.intersect2D(second).extrude(1))
    assert math.isclose(intersection.Volume(), 25, rel_tol=1e-6)
    return {"scenario": "plugin polygon/extrusion, intersection and STEP"}


def gears(source, output):
    from cq_gears import SpurGear

    gear = SpurGear(module=1, teeth_number=12, width=2)
    shape = solid(gear.build())
    step_roundtrip(shape, output / "spur.step")
    return {"scenario": "12-tooth spur gear build and STEP roundtrip"}


def argus(source, output):
    import cadquery as cq
    from argus_diff import load_step, diff_files

    cube = cq.Workplane().box(2, 3, 4)
    cq.exporters.export(cube, str(output / "cube.step"))
    bodies = load_step(output / "cube.step")
    assert len(bodies) == 1 and math.isclose(bodies[0].volume, 24, rel_tol=1e-5)
    result = diff_files(output / "cube.step", output / "cube.step", check_interference=False)
    assert len(result.unchanged) == 1
    assert not result.added and not result.removed and not result.modified
    return {"scenario": "own STEP loader and identical-file geometric diff"}


def electronics(source, output):
    from cq_electronics.connectors.headers import PinHeader

    header = PinHeader(rows=1, columns=2, simple=True).cq_object
    shape = solid(header.toCompound())
    assert len(shape.Solids()) == 3
    header.save(str(output / "header.step"))
    file_written(output / "header.step")
    return {"scenario": "two-pin header assembly geometry and STEP export"}


def gridfinity(source, output):
    from cqgridfinity import GridfinityBox

    box = GridfinityBox(1, 1, 2, no_lip=True)
    solid(box.render())
    box.save_step_file(filename=str(output / "gridfinity.step"))
    box.save_stl_file(filename=str(output / "gridfinity.stl"))
    file_written(output / "gridfinity.step")
    file_written(output / "gridfinity.stl")
    return {"scenario": "1x1 box render and own STEP/STL exports"}


def helper(source, output):
    import cadquery as cq
    from cadqueryhelper.grid import cell_single

    # The helper swallows exceptions. Check actual nonempty geometry, not return type.
    shape = solid(cell_single(points=[(0, 0), (0, 4), (4, 4), (4, 0)], height=1,
                              taper=25, offset=-0.25))
    cq.exporters.export(shape, str(output / "cell.stl"))
    file_written(output / "cell.stl")
    return {"scenario": "documented tapered grid cell and STL export"}


def cadscript(source, output):
    import cadscript as cs

    sketch = cs.make_sketch()
    sketch.add_rect(10, 20)
    sketch.export_dxf(str(output / "sketch.dxf"))
    file_written(output / "sketch.dxf")
    restored = cs.make_sketch()
    restored.add_import_dxf(str(output / "sketch.dxf"))
    assert sketch.get_extent() == restored.get_extent()
    body = cs.make_box(2, 3, 4)
    body.export_step(str(output / "box.step"))
    body.export_stl(str(output / "box.stl"))
    file_written(output / "box.step")
    file_written(output / "box.stl")
    return {"scenario": "own DXF export/import roundtrip and body STEP/STL exports"}


def kicad(source, output):
    import cadquery as cq
    from _tools.export_tools import check_step_export_union, postprocess_step

    component = cq.Assembly()
    component.add(cq.Workplane().box(2, 2, 2), name="left")
    component.add(cq.Workplane().box(2, 2, 2).translate((1, 0, 0)), name="right")
    component.save(str(output / "component.step"), cq.exporters.ExportTypes.STEP,
                   mode=cq.exporters.assembly.ExportModes.FUSED,
                   assembly_name="component", write_pcurves=False)
    file_written(output / "component.step")
    check_step_export_union(component, str(output), "component")
    postprocess_step(component, str(output), "component")
    restored = solid(cq.importers.importStep(str(output / "component.step")))
    assert len(restored.Solids()) == 1
    assert math.isclose(restored.Volume(), 12, rel_tol=1e-5)
    return {"scenario": "generator-style fused assembly STEP, own union validator and postprocessor"}


SCENARIOS = {
    20: warehouse, 21: cqkit, 22: cqmore, 24: gears, 28: argus,
    29: electronics, 30: kicad, 31: gridfinity, 33: helper, 48: cadscript,
}


def main():
    project_id = int(sys.argv[1])
    source = Path(sys.argv[2]).resolve()
    # Keep the caller's selected CQ checkout intact. These trees contain no CQ package.
    sys.path.insert(0, str(source / "src"))
    sys.path.insert(0, str(source))
    os.chdir(source)
    try:
        with tempfile.TemporaryDirectory(prefix="cq-compat-library-") as temp:
            result = SCENARIOS[project_id](source, Path(temp))
        result.update(id=project_id, status="passed")
        print(json.dumps(result, sort_keys=True))
    except Exception as error:
        traceback.print_exc()
        print(json.dumps({"id": project_id, "status": "failed",
                          "error": f"{type(error).__name__}: {error}"}, sort_keys=True))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
