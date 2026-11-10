"""Apply the bounded CQ-editor lazy-import adaptation to a disposable checkout.

The comparison must preserve an unmodified checkout/result separately. This
patch is a tested hypothesis only after CI runs it on both CadQuery versions.
It preserves CQ's new library modules without disabling helper-script reloads.
"""

import argparse
from pathlib import Path


CQ_UTILS_IMPORT = "from importlib import reload\n"
CQ_UTILS_RELOADS = (
    "cadquery.occ_impl.importers.dxf",
    "cadquery.occ_impl.exporters.dxf",
)
CLEANUP = """        for module_name in new_modules:
            del sys.modules[module_name]
"""
PRESERVING_CLEANUP = """        preserved_namespaces = ("cadquery", "ezdxf", "vtkmodules")
        for module_name in new_modules:
            if not any(
                module_name == namespace or module_name.startswith(namespace + ".")
                for namespace in preserved_namespaces
            ):
                del sys.modules[module_name]
"""


def apply_consumer_patch(source):
    source = Path(source)
    utils_path = source / "cq_editor" / "cq_utils.py"
    debugger_path = source / "cq_editor" / "widgets" / "debugger.py"
    utils = utils_path.read_text(encoding="utf-8")
    debugger = debugger_path.read_text(encoding="utf-8")

    if utils.count(CQ_UTILS_IMPORT) != 1:
        raise ValueError(f"Unexpected importlib import in {utils_path}")
    utils = utils.replace(CQ_UTILS_IMPORT, CQ_UTILS_IMPORT + "import sys\n", 1)
    for module_name in CQ_UTILS_RELOADS:
        attribute = module_name.replace("cadquery.", "cq.", 1)
        before = f"    reload({attribute})\n"
        after = (
            f'    if (module := sys.modules.get("{module_name}")) is not None:\n'
            "        reload(module)\n"
        )
        if utils.count(before) != 1:
            raise ValueError(f"Unexpected reload site {attribute} in {utils_path}")
        utils = utils.replace(before, after, 1)
    if debugger.count(CLEANUP) != 1:
        raise ValueError(f"Unexpected module_manager cleanup in {debugger_path}")
    debugger = debugger.replace(CLEANUP, PRESERVING_CLEANUP, 1)

    # Check both inputs before writing either file.
    utils_path.write_text(utils, encoding="utf-8")
    debugger_path.write_text(debugger, encoding="utf-8")
    return [str(utils_path), str(debugger_path)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    for path in apply_consumer_patch(args.source):
        print(path)


if __name__ == "__main__":
    main()
