#!/usr/bin/env python3
"""Regression for fix_web_dist.py after the 0.23.0 kernel bump.

The old script had two defects, both recorded in BUILD-NOTES-build-023.md but
worked around instead of fixed:

1. `assert fixed + already == 1` — a dist containing more than one Vite
   modulepreload helper (one already patched, one not) reported `hits=1+1`
   and aborted; so did simply running the script twice.
2. `leftover = sum(len(HELPER_OLD.search(...) or b"") ...)` — `len(re.Match)`
   raises TypeError as soon as a stale helper survives, so the failure path
   crashed instead of exiting 2 with a diagnosis.

This harness builds synthetic dists and runs the REAL script twice. No fpk,
no upstream bundle and no fnOS needed.

usage: python3 tools/verify_fix_web_dist.py
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "fix_web_dist.py"
PREFIX = "/app/hermes-si"

# Literal Vite preload-helper shapes as fix_web_dist.py's regexes see them.
OLD = b"`modulepreload`,u=function(e){return`/`+e}"
NEW = b"`modulepreload`,u=function(e){return`%s/`+e}" % PREFIX.encode()

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                         " — " + detail if detail else ""))


def make_dist(root: Path, files: dict[str, bytes]) -> Path:
    d = root / "web_dist"
    (d / "assets").mkdir(parents=True)
    for name, body in files.items():
        (d / "assets" / name).write_bytes(body)
    refs = b"".join(b'<script src="/assets/' + n.encode() + b'"></script>\n'
                    for n in files)
    (d / "index.html").write_bytes(b"<html><body>" + refs + b"</body></html>")
    return d


def run(d: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), str(d), PREFIX],
                          capture_output=True, text=True)


def case_mixed_helpers() -> None:
    """One unpatched + one already-patched helper: the old `== 1` assert died."""
    with tempfile.TemporaryDirectory() as t:
        d = make_dist(Path(t), {
            "index-abc.js": OLD + b"\nconsole.log('a');\n",
            "chunk-def.js": NEW + b"\nconsole.log('b');\n",
        })
        r1 = run(d)
        check("first run: mixed helpers accepted", r1.returncode == 0,
              (r1.stdout.strip() or r1.stderr.strip())[:190])
        if r1.returncode:
            return
        check("first run: reports fixed=1 already=1",
              "fixed=1 already-fixed=1" in r1.stdout, r1.stdout.strip()[:160])
        files1 = sorted(p.name for p in (d / "assets").glob("*.js"))
        check("first run: all assets cache-busted",
              len(files1) == 2 and all("-fN" in n for n in files1), str(files1))
        html = (d / "index.html").read_bytes()
        check("first run: index.html references renamed assets",
              all(n.encode() in html for n in files1) and b"index-abc.js" not in html)

        r2 = run(d)
        check("second run: idempotent (exit 0)", r2.returncode == 0,
              (r2.stdout.strip() or r2.stderr.strip())[:190])
        check("second run: already-fixed=2, no re-rename",
              "already-fixed=2" in r2.stdout and "already cache-busted" in r2.stdout,
              r2.stdout.strip()[:200])
        files2 = sorted(p.name for p in (d / "assets").glob("*.js"))
        check("second run: no double rename", files2 == files1,
              "%s -> %s" % (files1, files2))
        bodies = b"".join((d / "assets" / n).read_bytes() for n in files2)
        check("no stale old helper survives", OLD not in bodies)


def case_no_helper() -> None:
    """Upstream bundle shape changed -> must fail loudly, not silently pass."""
    with tempfile.TemporaryDirectory() as t:
        d = make_dist(Path(t), {"solo-aaa.js": b"console.log('x');\n"})
        r = run(d)
        check("dist with no helper is rejected", r.returncode != 0,
              "exit=%d" % r.returncode)
        check("rejection explains why",
              "no modulepreload helper found" in (r.stdout + r.stderr),
              (r.stdout + r.stderr).strip().splitlines()[-1][:160]
              if (r.stdout + r.stderr).strip() else "no output")


def case_leftover_expression() -> None:
    """Prove the old failure-path expression crashed and the new one counts."""
    helper_old = re.compile(rb"(`modulepreload`,[A-Za-z_$]{1,4}=function\(([A-Za-z_$])\)\{return)`/`(\+\2\})")
    samples = [OLD, b"clean bundle"]
    try:
        old_value = sum(len(helper_old.search(s) or b"") for s in samples)
        old_crashed = False
    except TypeError as e:
        old_value, old_crashed = str(e), True
    new_value = sum(1 for s in samples if helper_old.search(s))
    check("old leftover expression had TypeError", old_crashed, str(old_value)[:100])
    check("new leftover expression counts hits", new_value == 1,
          "hits=%d" % new_value)


def main() -> int:
    check("fix_web_dist.py present", SCRIPT.is_file(), str(SCRIPT))
    case_mixed_helpers()
    case_no_helper()
    case_leftover_expression()
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
