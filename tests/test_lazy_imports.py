import ast
import os
import subprocess
import sys

import pytest

import cadquery

# packages whose members `import cadquery` evaluates; every other OCP package is
# imported on use
IMPORT_TIME_OCP_PACKAGES = frozenset(
    "Approx BOPAlgo BRepAdaptor BRepBuilderAPI BRepGProp BRepPrimAPI BRepTools "
    "GeomAbs Message TopAbs TopLoc TopoDS gp".split()
)


@pytest.fixture(scope="module")
def fresh_import():
    # a fresh interpreter: this process has ezdxf and vtk loaded by other tests
    code = (
        "import sys, cadquery;"
        "print({m.split('.')[0] for m in sys.modules} & {'ezdxf', 'vtkmodules'});"
        "print(*(m.__file__ for n, m in sys.modules.items() if n == 'cadquery'"
        " or n.startswith('cadquery.')), sep=';')"
    )
    # run next to the package under test; only stdout is checked, since the
    # interpreter may crash on exit on Windows (#1911)
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=os.path.dirname(os.path.dirname(cadquery.__file__)),
        capture_output=True,
        text=True,
    )

    assert out.stdout, out.stderr

    third_party, files = out.stdout.strip().splitlines()[-2:]

    return third_party, files.split(";")


def test_import_does_not_load_ezdxf_or_vtk(fresh_import):

    assert fresh_import[0] == "set()"


def test_import_time_ocp_packages(fresh_import):

    offenders = []

    for path in fresh_import[1]:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())

        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            else:
                continue

            for name in names:
                pkg = name.split(".")[:2]
                if pkg[0] == "OCP" and pkg[-1] not in IMPORT_TIME_OCP_PACKAGES:
                    offenders.append(f"{path}:{node.lineno} {name}")

    assert not offenders, "\n".join(offenders)
