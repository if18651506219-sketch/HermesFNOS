# Repack 0.22.7.1 fpk -> 0.22.7.2-test: byte-identical kernel, replace 4 port files.
import io, json, sys, tarfile, gzip, gzip as gz
from pathlib import Path

REPO = Path('/vol2/@apphome/hermes-si/data/hermes-home/port/hermes-repo')
BASE = REPO / 'pkg/hermes-si.fpk'
OUT = REPO / 'pkg/hermes-si-0272-test.fpk'
SCRATCH = Path('/vol2/@apphome/hermes-si/data/hermes-home/cache/scratch')

# ---- version triple guard (L0.version-consistency, pre-flight) ----
stamp_py = (REPO / 'stamp_version.py').read_text()
import re
COMMIT = re.search(r"^COMMIT\s*=\s*[\"']([0-9a-f]{40})", stamp_py, re.M).group(1)
KERNEL = '0.22.7'  # expected kernel for this repack generation
manifest = (REPO / 'manifest').read_text()
ver = re.search(r'^version="([^"]+)"', manifest, re.M).group(1)
assert ver.startswith('0.22.7.2') and ver.count('.') == 3
assert ver.rsplit('.', 1)[0] == KERNEL, f'manifest base {ver} != KERNEL {KERNEL}'
new_manifest = manifest.encode()
new_main = (REPO / 'cmd/main').read_bytes()
new_proxy = (REPO / 'sock_proxy.py').read_bytes()

def stamp_json(orig: bytes) -> bytes:
    d = json.loads(orig)
    assert d['baseVersion'] == KERNEL and d['commit'] == COMMIT, f'stamp mismatch {d["baseVersion"]} {d["commit"][:8]}'
    d['displayVersion'] = f'{KERNEL} (fnOS {ver})'
    return json.dumps(d, indent=2).encode()

# ---- rebuild inner app.tgz (streamed; only 2 members replaced) ----
inner_out = SCRATCH / 'app-0272.tgz'
with tarfile.open(BASE, 'r') as outer_r:
    inner_raw = outer_r.extractfile('app.tgz').read()
with tarfile.open(fileobj=io.BytesIO(inner_raw), mode='r|*') as tr, \
     tarfile.open(inner_out, 'w:gz') as tw:
    n_rep = 0
    for m in tr:
        if m.name == 'sock_proxy.py':
            ti = tarfile.TarInfo(m.name)
            for k in ('mode','uid','gid','mtime','type','linkname','uname','gname'):
                setattr(ti, k, getattr(m, k))
            ti.size = len(new_proxy)
            tw.addfile(ti, io.BytesIO(new_proxy)); n_rep += 1
        elif m.name == 'src/install-stamp.json':
            data = stamp_json(tr.extractfile(m).read())
            ti = tarfile.TarInfo(m.name)
            for k in ('mode','uid','gid','mtime','type','linkname','uname','gname'):
                setattr(ti, k, getattr(m, k))
            ti.size = len(data)
            tw.addfile(ti, io.BytesIO(data)); n_rep += 1
        else:
            tw.addfile(m, tr.extractfile(m))
    print('inner replaced:', n_rep)

# ---- rebuild outer fpk (manifest + cmd/main replaced, app.tgz swapped) ----
new_app = inner_out.read_bytes()
n = 0
with tarfile.open(BASE, 'r') as r, tarfile.open(OUT, 'w:gz') as w:
    for m in r:
        if m.name == 'app.tgz':
            data = new_app
        elif m.name == 'manifest':
            data = new_manifest
        elif m.name == 'cmd/main':
            data = new_main
        else:
            w.addfile(m, r.extractfile(m))
            continue
        ti = tarfile.TarInfo(m.name)
        for k in ('mode','uid','gid','mtime','type','linkname','uname','gname'):
            setattr(ti, k, getattr(m, k))
        ti.size = len(data)
        w.addfile(ti, io.BytesIO(data)); n += 1
    print('outer replaced:', n)
print('OUT', OUT, OUT.stat().st_size)
