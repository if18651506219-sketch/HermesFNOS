# Live regression against production 0.22.7.2 sock_proxy (temp proxy instance -> dashboard 9120)
# Uses nonexistent routes: proves body relay completes (fast 4xx) instead of hanging. No side effects.
import os, socket, subprocess, sys, threading, time, tempfile
from pathlib import Path

PROXY = '/vol2/@appcenter/hermes-si/sock_proxy.py'   # the LIVE fixed file
tmp = Path(tempfile.mkdtemp(dir='/vol2/@apphome/hermes-si/data/hermes-home/cache/scratch'))
sock = str(tmp / 'p.sock')
proc = subprocess.Popen([sys.executable, PROXY, sock, '9120', '/app/hermes-si'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(0.5)
res = []

def check(name, ok, extra=''):
    res.append(ok)
    print('PASS' if ok else 'FAIL', name, extra)

def cl():
    s = socket.socket(socket.AF_UNIX); s.settimeout(15); s.connect(sock)
    return s

try:
    # 1) 60KB POST header-first then body in 2 chunks (the old hang scenario)
    s = cl()
    body = b'y' * 61440
    s.sendall(b'POST /api/definitely-not-a-route HTTP/1.1\r\nHost: t\r\nContent-Length: %d\r\nContent-Type: application/json\r\n\r\n' % len(body))
    t0 = time.time()
    time.sleep(0.3)
    s.sendall(body[:30000]); time.sleep(0.3); s.sendall(body[30000:])
    resp = s.recv(200)
    dt = time.time() - t0
    check('post-60KB-split', resp.startswith(b'HTTP/1.1 4') or resp.startswith(b'HTTP/1.1 2'), '%.1fs %s' % (dt, resp.split(b'\r\n')[0]))
    s.close()

    # 2) Expect: 100-continue PUT 10KB
    s = cl()
    s.sendall(b'PUT /api/nope HTTP/1.1\r\nContent-Length: 10240\r\nExpect: 100-continue\r\n\r\n')
    first = s.recv(64)
    time.sleep(0.2)
    s.sendall(b'z' * 10240)
    rest = b''
    while b'HTTP/1.1' not in rest:
        d = s.recv(200)
        if not d: break
        rest += d
    check('expect-100-live', first.startswith(b'HTTP/1.1 100') and b'HTTP/1.1' in rest, '%s' % first.split(b'\r\n')[0])
    s.close()

    # 3) chunked POST 100KB
    s = cl()
    s.sendall(b'POST /api/nope HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n')
    okc = True
    try:
        for i in range(25):
            s.sendall(b'1000\r\n' + b'c' * 4096 + b'\r\n')
            time.sleep(0.02)
        s.sendall(b'0\r\n\r\n')
        resp = s.recv(200)
    except OSError:
        okc, resp = False, b''
    check('chunked-100KB-live', (resp.startswith(b'HTTP/1.1 4') or resp.startswith(b'HTTP/1.1 2')) and okc, '%s' % resp.split(b'\r\n')[:1])
    s.close()

    # 4) WS upgrade handshake through proxy (auth may reject AFTER 101 or with 4xx; must not hang)
    import base64
    key = base64.b64encode(os.urandom(16)).decode()
    s = cl()
    s.sendall(('GET /api/ws HTTP/1.1\r\nHost: t\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
               'Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n' % key).encode())
    t0 = time.time()
    resp = b''
    try:
        while b'\r\n\r\n' not in resp and time.time() - t0 < 10:
            d = s.recv(4096)
            if not d: break
            resp += d
    except OSError:
        pass
    status = resp.split(b'\r\n')[0] if resp else b'EMPTY'
    check('ws-handshake-live', resp.startswith(b'HTTP/1.1 101') or resp.startswith(b'HTTP/1.1 4'),
          '%.1fs %s' % (time.time() - t0, status))
    if resp.startswith(b'HTTP/1.1 101'):
        # idle 13s then check tunnel alive (send close frame)
        time.sleep(13)
        try:
            s.sendall(b'\x88\x00')
            s.settimeout(3)
            try:
                d = s.recv(10)
                alive = d != b''
            except socket.timeout:
                alive = True
            print('PASS' if alive else 'FAIL', 'ws-idle-13s-live')
            res.append(alive)
        except OSError:
            print('FAIL ws-idle-13s-live (conn died)')
            res.append(False)
    s.close()
finally:
    proc.terminate()

print('SUMMARY', all(res), len(res))
