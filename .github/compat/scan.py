"""Locate source sites affected by the import changes, without importing consumers.

This is a risk inventory, not a sound whole-program reachability analysis.
Computed names, external code and alias flows can require manual investigation.
"""

import ast
import json
from pathlib import Path

REMOVED = {
    "cadquery.assembly": {"exportGLTF", "exportAssembly", "exportCAF", "exportVTKJS", "exportVRML"},
    "cadquery.occ_impl.assembly": {"vtkActor", "vtkMapper", "vtkRenderer", "vtkProp3D", "toString", "extractEdgesFaces"},
    "cadquery.occ_impl.shapes": {"vtkPolyData", "vtkTriangleFilter", "vtkPolyDataNormals"},
    "cadquery.occ_impl.importers": {"_importDXF"},
    "cadquery.sketch": {"_importDXF"},
    "cadquery.occ_impl.exporters.assembly": {"Literal"},
}
RETAINED = {
    "cadquery.occ_impl.exporters": {"exportDXF", "exportDXFProjection", "DxfDocument", "exportVTP", "dxf", "vtk", "assembly"},
}
SPECIAL = {"importlib.reload", "reload", "sys.modules", "typing.get_type_hints", "get_type_hints"}
REFLECTION = {"getattr", "hasattr", "vars", "dir", "eval", "exec", "__import__", "importlib.import_module"}
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__"}


def canonical(name):
    for short, full in [("cadquery.exporters", "cadquery.occ_impl.exporters"),
                        ("cadquery.importers", "cadquery.occ_impl.importers")]:
        if name == short or name.startswith(short + "."):
            return full + name[len(short):]
    return name


def scan(root):
    root = Path(root)
    sites, errors = [], []
    files = 0
    for path in sorted(root.rglob("*.py")):
        if SKIP.intersection(path.relative_to(root).parts):
            continue
        files += 1
        relative = str(path.relative_to(root)).replace("\\", "/")
        try:
            text = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(text)
        except (SyntaxError, UnicodeError) as error:
            errors.append({"file": relative, "error": str(error)})
            continue
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    aliases[item.asname or item.name.split(".")[0]] = item.name if item.asname else item.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                for item in node.names:
                    aliases[item.asname or item.name] = f"{node.module}.{item.name}"

        def resolve(node):
            if isinstance(node, ast.Name):
                return canonical(aliases.get(node.id, node.id))
            if isinstance(node, ast.Attribute):
                return canonical(resolve(node.value) + "." + node.attr)
            return ""

        def record(node, kind, name):
            sites.append({"file": relative, "line": node.lineno, "kind": kind,
                          "name": name, "source": text.splitlines()[node.lineno - 1].strip()})

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    # Package-relative targets need package context and alias-flow analysis.
                    continue
                module = canonical(node.module or "")
                for item in node.names:
                    target = module + "." + item.name
                    if item.name == "*" and module.startswith("cadquery"):
                        record(node, "namespace-enumeration", target)
                    if item.name in REMOVED.get(module, set()):
                        record(node, "removed-binding", target)
                    if item.name in RETAINED.get(module, set()):
                        record(node, "retained-lazy-binding", target)
            elif isinstance(node, ast.Attribute):
                name = resolve(node)
                module, _, member = name.rpartition(".")
                if member in REMOVED.get(module, set()):
                    record(node, "removed-binding", name)
                elif name == "cadquery.occ_impl.importers.dxf":
                    record(node, "deferred-package-attribute", name)
                elif member in RETAINED.get(module, set()):
                    record(node, "retained-lazy-binding", name)
                elif name == "sys.modules":
                    record(node, "module-state", name)
            elif isinstance(node, ast.Call):
                name = resolve(node.func)
                if name in SPECIAL:
                    record(node, "lifecycle-or-annotations", name)
                elif name in REFLECTION:
                    record(node, "dynamic-review-required", name)
    # Multiple syntactic references on one line remain one evidence site per kind/name.
    unique = {(s["file"], s["line"], s["kind"], s["name"]): s for s in sites}
    return {"python_files": files, "parse_errors": errors,
            "limitations": ["No whole-program alias flow or reachability proof",
                            "No native, generated, notebook or external user code analysis",
                            "Reflection sites require review; absence of matches is not compatibility proof"],
            "sites": list(unique.values())}


def observations(scan_result, coverage_path, consumer_root):
    """Map risk lines to a separate coverage run. Unexecuted does not mean unreachable."""
    coverage = json.loads(Path(coverage_path).read_text())
    files = coverage.get("files", {})
    root = Path(consumer_root).resolve()
    indexed = {}
    for name, detail in files.items():
        path = Path(name).resolve()
        if path.is_relative_to(root):
            indexed[path.relative_to(root).as_posix()] = set(detail["executed_lines"])
    return [{**site, "executed_in_observation": site["line"] in indexed.get(site["file"], set())}
            for site in scan_result["sites"]]
