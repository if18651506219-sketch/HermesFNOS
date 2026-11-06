#!/usr/bin/env python3
"""Pre-build gate for an upstream kernel bump.

The 0.23.0 bump shipped with `manifest version=0.22.7.3` while
`stamp_version.py KERNEL=0.23.0`: the README demanded a matching triple but
nothing actually checked it at build time, so `verify.py L0.version-consistency`
would fail on the finished fpk (and `repack_fpk.py` would refuse `--version
0.23.0.1`). This script turns that convention into an executable gate and,
when given an upstream source tree, proves every port-patch still replays.

Checks (no arguments needed):
  * manifest version is a 4-part fnOS version and its first 3 parts equal
    stamp_version.py KERNEL
  * COMMIT / COMMIT_DATE are well formed (40-hex sha, plausible epoch)
  * a simulated stamp_version.py run produces the stamp that
    verify.py L0.version-consistency expects
  * README still names the current kernel as its build target
  * port-patches/README.md documents both patch strip levels

Optional, with --kernel-src DIR:
  * replay every port-patches/*.patch with `patch --dry-run` against the
    target kernel sources, for strip levels 1 and 2. The source tree may be
    either a payload tree (src/...) or an upstream flat tree (hermes_cli/...);
    the matching file is located automatically. Nothing is modified.

Exit 0 = all green, 1 = at least one failure. No third-party dependencies.

usage:
  python3 scripts/verify_kernel_bump.py
  python3 scripts/verify_kernel_bump.py --kernel-src /tmp/upstream-src
  python3 scripts/verify_kernel_bump.py --kernel-src /tmp/payload --strip 1
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                         " — " + detail if detail else ""))


def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="ignore")


def one(pattern: str, src: str, flags: int = re.M):
    m = re.search(pattern, src, flags)
    return m.group(1) if m else ""


def stamp_triple() -> tuple[str, str, str, str]:
    m = text("manifest")
    ver = one(r'^version\s*=\s*"?([^"\s]+)"?', m)
    s = text("stamp_version.py")
    kernel = one(r'^KERNEL\s*=\s*"([^"]+)"', s)
    commit = one(r'^COMMIT\s*=\s*"([^"]+)"', s)
    epoch = one(r'^COMMIT_DATE\s*=\s*(\d+)', s)
    return ver, kernel, commit, epoch


def simulate_stamp(ver: str, kernel: str, commit: str) -> None:
    """Run stamp_version.py in a throwaway pkg dir and replay the L0 assertion."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        (p / "app" / "src").mkdir(parents=True)
        r = subprocess.run(
            [sys.executable, str(ROOT / "stamp_version.py"), str(p), kernel, ver],
            capture_output=True, text=True)
        if r.returncode:
            check("stamp_version.py accepts %s" % ver, False,
                  r.stderr.strip()[:180] or r.stdout.strip()[:180])
            return
        check("stamp_version.py accepts %s" % ver, True, r.stdout.strip())
        stamp = json.loads((p / "app" / "src" / "install-stamp.json").read_text())
        kernel_from_manifest = ".".join(ver.split(".")[:3])
        ok_consistency = (stamp.get("baseVersion") == kernel_from_manifest
                          and stamp.get("commit") == commit
                          and stamp.get("updateMechanism") == "app-installer")
        check("L0.version-consistency (simulated)", ok_consistency,
              "manifest=%s kernel=%s stamp.base=%s stamp.commit=%s" % (
                  ver, kernel_from_manifest, stamp.get("baseVersion"),
                  str(stamp.get("commit"))[:10]))
        ok_display = stamp.get("displayVersion") == "%s (fnOS %s)" % (kernel, ver)
        check("stamp displayVersion", ok_display, str(stamp.get("displayVersion")))


def replay_patches(kernel_src: Path, strips: list[int]) -> None:
    patches = sorted((ROOT / "port-patches").glob("*.patch"))
    check("port-patches present", bool(patches), "%d patches" % len(patches))
    if not patches:
        return

    def locate(rel: str) -> Path | None:
        # rel comes from `--- a/src/hermes_cli/web_server.py` minus the `a/`.
        bare = rel[len("src/"):] if rel.startswith("src/") else rel
        for cand in (kernel_src / rel, kernel_src / bare,
                     kernel_src / "src" / bare):
            if cand.is_file():
                return cand
        return None

    for strip in strips:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            missing: list[str] = []
            for p in patches:
                txt = p.read_text(encoding="utf-8", errors="ignore")
                for rel in dict.fromkeys(re.findall(r"^--- a/(\S+)", txt, re.M)):
                    src = locate(rel)
                    if src is None:
                        missing.append("%s: %s" % (p.name, rel))
                        continue
                    parts = ("a/" + rel).split("/")
                    dst = root / "/".join(parts[strip:])
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not dst.exists():
                        shutil.copy2(src, dst)
            check("strip -p%d: all patch targets found" % strip, not missing,
                  "; ".join(missing[:5]) if missing else "%d sources staged" % len(
                      list(root.rglob("*.py"))))
            if missing:
                continue
            for p in patches:
                r = subprocess.run(
                    ["patch", "-p%d" % strip, "--dry-run", "--batch"],
                    cwd=root, stdin=p.open("rb"), capture_output=True)
                out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
                check("strip -p%d: %s" % (strip, p.name), r.returncode == 0,
                      out.replace("\n", " ")[:160])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kernel-src", type=Path,
                    help="upstream/payload kernel source tree for patch replay")
    ap.add_argument("--strip", type=int, action="append", choices=(1, 2),
                    help="patch strip level(s) to test (default: 1 and 2)")
    a = ap.parse_args()

    ver, kernel, commit, epoch = stamp_triple()
    check("manifest version is a 4-part fnOS version",
          bool(re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ver)), ver or "MISSING")
    kernel_of_ver = ".".join(ver.split(".")[:3])
    check("version triple: manifest kernel == KERNEL",
          bool(kernel) and kernel_of_ver == kernel,
          "manifest=%s -> %s, stamp KERNEL=%s" % (ver, kernel_of_ver, kernel))
    check("stamp_version.py COMMIT is a 40-hex sha",
          bool(re.fullmatch(r"[0-9a-f]{40}", commit)), commit[:12] or "MISSING")
    if epoch:
        when = datetime.fromtimestamp(int(epoch), timezone.utc)
        now = datetime.now(timezone.utc)
        plausible = when <= now and (now - when).days < 3650
        check("stamp_version.py COMMIT_DATE plausible", plausible,
              when.isoformat())
    else:
        check("stamp_version.py COMMIT_DATE plausible", False, "MISSING")

    if kernel and ver.startswith(kernel + "."):
        simulate_stamp(ver, kernel, commit)
    else:
        check("stamp_version.py accepts %s" % ver, False,
              "fnos version must start with '%s.'" % kernel)

    readme = text("README.md")
    check("README names current kernel target",
          ("当前目标 v%s" % kernel) in readme,
          "expected '当前目标 v%s'" % kernel)
    ppr = text("port-patches/README.md")
    check("port-patches README documents -p1 and -p2",
          "-p1" in ppr and "-p2" in ppr)

    if a.kernel_src:
        if not a.kernel_src.is_dir():
            check("--kernel-src is a directory", False, str(a.kernel_src))
        else:
            replay_patches(a.kernel_src, a.strip or [1, 2])

    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n%d checks, %d failed" % (len(RESULTS), len(failed)))
    if failed:
        for n in failed:
            print("  FAILED:", n)
        return 1
    print("ALL-PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
