#!/usr/bin/env python3
"""Post-build fix for the dashboard SPA under an fnOS gateway prefix.

Two coupled jobs, both REQUIRED before an fpk may ship:

1. PRELOAD-HELPER FIX. Vite's modulepreload dep-map bakes root-relative
   strings ("assets/X.css") into bundles and its helper prepends a bare `/`
   -> `/assets/X.css`. Upstream X-Forwarded-Prefix rewriting covers
   index.html, runtime fetches and CSS url() but NOT this helper, so behind
   a gateway prefix lazy route chunks (ChatPage, xterm CSS, ...) fetch from
   the web ROOT and 404 (chat page = black pane, "Unable to preload CSS").
   Rewrite the helper base `/` -> `<prefix>/`, anchored on the modulepreload
   marker. Fails (exit 2) if the helper shape changed upstream.

2. CACHE-BUST RENAME. Hashed assets are served `immutable, max-age=1y`; the
   browser (Safari above all) never revalidates them, so patching a bundle
   WITHOUT renaming it is a no-op for every browser that ever loaded the
   previous build. Rename every JS asset (append -fN) and propagate the new
   names through every file that references them (bundles + index.html).
   Idempotent: already-renamed dists are detected and skipped.

usage: fix_web_dist.py <web_dist_dir> <prefix>
"""
import re
import sys
from pathlib import Path

dist = Path(sys.argv[1])
prefix = sys.argv[2]
assert re.fullmatch(r"/[A-Za-z0-9/_-]+", prefix), prefix

HELPER_OLD = re.compile(rb"(`modulepreload`,[A-Za-z_$]{1,4}=function\(([A-Za-z_$])\)\{return)`/`(\+\2\})")
HELPER_NEW = re.compile(rb"(`modulepreload`,[A-Za-z_$]{1,4}=function\(([A-Za-z_$])\)\{return)`%s/`(\+\2\})" % re.escape(prefix.encode()))
SUFFIX = b"-fN"

assets = dist / "assets"
js_files = sorted(assets.glob("*.js"))
assert js_files, "no js assets"

# --- 1. preload helper ---------------------------------------------------
fixed = already = 0
for f in js_files:
    data = f.read_bytes()
    new, n = HELPER_OLD.subn(lambda m: m.group(1) + ("`%s/`" % prefix).encode() + m.group(3), data)
    if n:
        f.write_bytes(new)
        fixed += n
    elif HELPER_NEW.search(data):
        already += 1
assert fixed + already == 1, f"preload helper hits={fixed}+{already} (expected exactly 1)"
print(f"preload-helper: fixed={fixed} already-fixed={already}")

# --- 2. cache-bust rename + reference propagation ------------------------
renamed = [f for f in js_files if SUFFIX in f.name.encode()]
assert len(renamed) in (0, len(js_files)), (
    "partially renamed dist: %d/%d renamed — refusing to mix" % (len(renamed), len(js_files)))

if renamed:
    print("rename: already cache-busted, skipping")
else:
    mapping = {f.name.encode(): (f.name.encode()[:-3] + SUFFIX + b".js") for f in js_files}
    # full-name substring collisions would corrupt neighbours
    for a in mapping:
        for b in mapping:
            if a != b and a in b:
                raise SystemExit(f"collision: {a!r} substring of {b!r}")
    targets = js_files + [dist / "index.html"]
    for f in targets:
        data = f.read_bytes()
        out = data
        for old, new in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
            out = out.replace(old, new)
        if out != data:
            f.write_bytes(out)
    for f in js_files:
        f.rename(assets / (f.name[:-3] + SUFFIX.decode() + ".js"))
    print(f"rename: {len(mapping)} js files cache-busted")

# --- 3. invariants -------------------------------------------------------
alljs = sorted(assets.glob("*.js"))
alive = {f.name.encode() for f in alljs}
refs_bad = []
for f in alljs + [dist / "index.html"]:
    data = f.read_bytes()
    for m in re.finditer(rb"[A-Za-z0-9._@-]+\.js", data):
        name = m.group(0).split(b"/")[-1]
        # dynamic/runtime names that are NOT build outputs (allowlist)
        if name in (b"hermes-config.js",):
            continue
        if (b"-" in name or b".min." in name) and name not in alive and b"node" not in m.group(0):
            refs_bad.append((f.name, name))
helper_ok = any(HELPER_NEW.search(f.read_bytes()) for f in alljs)
leftover = sum(len(HELPER_OLD.search(f.read_bytes()) or b"") for f in alljs)
print("files:", len(alljs), "helper-ok:", helper_ok, "helper-old-left:", leftover,
      "dangling-refs:", refs_bad[:6])
sys.exit(0 if (helper_ok and not leftover and not refs_bad) else 2)
