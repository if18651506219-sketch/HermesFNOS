#!/usr/bin/env python3
"""Payload hygiene for HermesFNOS fpk builds (run after assembling stage/app,
before stamp_version.py + fnpack).

1. venv/bin/* shebangs: replace build-machine interpreter with the fixed
   fnOS install path /var/apps/hermes-si/target/python314/bin/python3.14.
2. Drop editable-install leftovers (__editable__*.pth/finder, direct_url.json).
3. pyvenv.cfg + activate scripts: rewrite build-machine paths.
4. Bundle feishu runtime deps into the venv if missing (P0-3: users must
   never pip install at runtime; PyPI is unreachable from most NAS hosts):
   lark-oapi pycryptodome requests-toolbelt via tuna mirror using the
   bundled python314's pip.
Fails loudly on any step so a dirty payload can never ship.
"""
import glob, os, subprocess, sys

APP = sys.argv[1] if len(sys.argv) > 1 else "stage/app"
INTERP = b"#!/var/apps/hermes-si/target/python314/bin/python3.14\n"
SP = os.path.join(APP, "venv", "lib", "python3.14", "site-packages")

def main():
    n = 0
    for p in glob.glob(os.path.join(APP, "venv", "bin", "*")):
        try:
            b = open(p, "rb").read()
        except Exception:
            continue
        first = b.split(b"\n", 1)[0]
        if b.startswith(b"#!") and b"@apphome/hermes-agent" in first:
            open(p, "wb").write(INTERP + b.split(b"\n", 1)[1])
            n += 1
    print("shebangs rewritten:", n)

    removed = 0
    for f in glob.glob(os.path.join(SP, "__editable__*")) + \
             glob.glob(os.path.join(SP, "__pycache__", "__editable__*")) + \
             glob.glob(os.path.join(SP, "*.dist-info", "direct_url.json")):
        os.remove(f); removed += 1
    print("editable leftovers removed:", removed)

    for f in [os.path.join(APP, "venv", "pyvenv.cfg")] + \
             glob.glob(os.path.join(APP, "venv", "bin", "activate*")):
        if not os.path.isfile(f):
            continue
        s = open(f, encoding="utf-8", errors="ignore").read()
        if "@apphome/hermes-agent" in s:
            s = s.replace("/vol2/@apphome/hermes-agent/data/workspace/hermes-si/testvenv314",
                          "/var/apps/hermes-si/target/python314")
            s = s.replace("home = /vol2/@apphome/hermes-agent/data/.local/share/uv/python/cpython-3.14-linux-x86_64-gnu/bin",
                          "home = /var/apps/hermes-si/target/python314")
            open(f, "w", encoding="utf-8").write(s)

    for pkg, mod in [("lark-oapi", "lark_oapi"), ("pycryptodome", "Crypto"),
                     ("requests-toolbelt", "requests_toolbelt")]:
        if not os.path.isdir(os.path.join(SP, mod)):
            r = subprocess.run(
                [os.path.join(APP, "python314", "bin", "python3.14"), "-m", "pip", "install",
                 "--target", SP, pkg, "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"],
                capture_output=True, text=True)
            if r.returncode:
                sys.exit("pip bundle failed for %s:\n%s" % (pkg, r.stderr[-400:]))
            print("bundled:", pkg)

    # final gate: no functional file in venv may reference the build machine
    for dp, _dn, fn in os.walk(os.path.join(APP, "venv")):
        for f in fn:
            p = os.path.join(dp, f)
            try:
                if b"@apphome/hermes-agent" in open(p, "rb").read(300000):
                    sys.exit("dirty path remains in payload venv: " + p)
            except Exception:
                pass
    print("payload hygiene OK")

main()
