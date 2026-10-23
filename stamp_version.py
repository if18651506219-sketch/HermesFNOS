#!/usr/bin/env python3
"""Build-time stamp writer for Hermes FNOS fpk.

Writes app/src/install-stamp.json so hermes_cli.version_info resolves
base_version instead of "unknown" (dashboard vunknown fix).
Must be re-run on every kernel bump with the new kernel version.

usage: stamp_version.py <pkg_dir> <kernel_ver> <fnos_ver>
"""
import json
import sys
from datetime import datetime, timezone

COMMIT = "64dbae64747ab640a3fece06f359307c30d129da"  # upstream v0.22.4 tag
COMMIT_DATE = 1792599405

def main():
    pkg = sys.argv[1]
    kernel = sys.argv[2]
    fnos = sys.argv[3]
    stamp = {
        "schemaVersion": 2,
        "commit": COMMIT,
        "commitDate": COMMIT_DATE,
        "branch": None,
        "builtAt": datetime.now(timezone.utc).isoformat(),
        "dirty": False,
        "source": "ci",
        "distribution": None,
        "updateMechanism": "app-installer",
        "baseVersion": kernel,
        "displayVersion": f"{kernel} (fnOS {fnos})",
        "distance": None,
        "payload": "full",
        "tag": f"v{kernel}",
    }
    path = f"{pkg}/app/src/install-stamp.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stamp, f, indent=2)
    print("wrote", path, "| baseVersion", kernel)

if __name__ == "__main__":
    main()
