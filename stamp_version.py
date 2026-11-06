#!/usr/bin/env python3
"""Build-time stamp writer for Hermes FNOS fpk.

Writes app/src/install-stamp.json so hermes_cli.version_info resolves
base_version instead of "unknown" (dashboard vunknown fix).
Must be re-run on every kernel bump with the new kernel version.

fnOS-port 0.22.7.3: COMMIT/COMMIT_DATE/KERNEL are a single triple. Passing a
kernel that does not match the stamped commit used to silently produce a
package claiming "0.22.7" while carrying the 0.22.6 commit (verify.py L0 only
checked that the commit was non-empty). Refuse that combination up front.

usage: stamp_version.py <pkg_dir> <kernel_ver> <fnos_ver>
"""
import json
import os
import sys
from datetime import datetime, timezone

# Update these THREE together on every upstream kernel bump.
KERNEL = "0.23.0"
COMMIT = "2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19"  # upstream v0.23.0 tag
COMMIT_DATE = 1793908658


def main():
    if len(sys.argv) != 4:
        sys.exit("usage: stamp_version.py <pkg_dir> <kernel_ver> <fnos_ver>")
    pkg = sys.argv[1]
    kernel = sys.argv[2]
    fnos = sys.argv[3]
    if kernel != KERNEL:
        sys.exit("kernel %s != stamp KERNEL %s (COMMIT is the %s tag); "
                 "update KERNEL/COMMIT/COMMIT_DATE together" % (kernel, KERNEL, KERNEL))
    if not fnos.startswith(kernel + "."):
        sys.exit("fnos version %s must start with '%s.' (kernel + fnOS revision)" % (fnos, kernel))

    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    built = (datetime.fromtimestamp(int(epoch), timezone.utc).isoformat()
             if epoch else datetime.now(timezone.utc).isoformat())
    path = os.path.join(pkg, "app", "src", "install-stamp.json")
    if not os.path.isdir(os.path.dirname(path)):
        sys.exit("stamp target dir missing: " + os.path.dirname(path))

    stamp = {
        "schemaVersion": 2,
        "commit": COMMIT,
        "commitDate": COMMIT_DATE,
        "branch": None,
        "builtAt": built,
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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stamp, f, indent=2)
    print("wrote", path, "| baseVersion", kernel, "| commit", COMMIT[:10])


if __name__ == "__main__":
    main()
