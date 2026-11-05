# Behavior regression for fixed sock_proxy: big POST split, Expect 100, chunked,
# X-Forwarded strip, SSE idle > header-phase timeout. Unix socket + fake upstream.
import os, socket, subprocess, sys, threading, time, tempfile
from pathlib import Path

PROXY = '/vol2/@apphome/hermes-si/data/hermes-home/port/hermes-repo/sock_proxy.py'
tmp = Path(tempfile.mkdtemp(dir='/vol2/@apphome/hermes-si/data/hermes-home/cache/scratch'))
sock = str(tmp / 'p.sock')
UP_PORT = 18991
results = []
seen = {}

def upstream():
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', UP_PORT)); srv.listen(8)
    while True:
        c, _ = srv.accept()
        data = b''
        # read request fully per Content-Length
        c.settimeout(5)
        head = b''
        while b'\r\n\r\n' not in head:
            d = c.recv(4096)
            if not d: break
            head += d
        hdr, _, rest = head.partition(b'\r\n\r\n')
        h = hdr.lower()
        cl = 0
        for line in h.split(b'\r\n'):
            if line.startswith(b'content-length:'):
                cl = int(line.split(b':')[1])
        body = rest
        while len(body) < cl:
            d = c.recv(65536)
            if not d: break
            body += d
        seen['x_forwarded'] = [l for l in hdr.split(b'\r\n') if l.lower().startswith(b'x-forwarded')]
        seen['expect_fwd'] = b'expect:' in h
        seen['host_fwd'] = [l for l in hdr.split(b'\r\n') if l.lower().startswith(b'host:')]
        seen['big_body'] = body
        if b'transfer-encoding: chunked' in h:
            drain = rest
            try:
                while not drain.endswith(b'\r\n\r\n') or b'0\r\n' not in drain:
                    d = c.recv(4096)
                    if not d: break
                    drain += d
            except OSError:
                pass
        if b'/sse' in hdr.split(b'\r\n')[0]:
            c.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n')
            time.sleep(14)   # idle longer than any header-phase timeout
            c.sendall(b'data: late\r\n\r\n')
            try: c.recv(10)
            except OSError: pass
            c.close()
            continue
        c.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')
        c.close()

threading.Thread(target=upstream, daemon=True).start()
time.sleep(0.3)
env = dict(os.environ)
proc = subprocess.Popen([sys.executable, PROXY, sock, str(UP_PORT), '/app/test'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(0.5)

def client():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(25)
    s.connect(sock)
    return s

def check(name, cond):
    results.append((name, bool(cond)))
    print(('PASS' if cond else 'FAIL'), name)

try:
    # 1) big POST 20KB in 3 writes (header alone first)
    s = client()
    payload = b'x' * 20480
    s.sendall(b'POST /save HTTP/1.1\r\nHost: fnos\r\nContent-Length: %d\r\nX-Forwarded-For: 1.2.3.4\r\n\r\n' % len(payload))
    time.sleep(0.2)
    s.sendall(payload[:9000]); time.sleep(0.2); s.sendall(payload[9000:])
    resp = s.recv(100)
    check('big-post-split', resp.startswith(b'HTTP/1.1 200') and seen['big_body'] == payload)
    check('xf-sanitized', any(l.lower().startswith(b'x-forwarded-prefix:') for l in seen['x_forwarded']) and
          not any(l.lower().startswith(b'x-forwarded-for:') for l in seen['x_forwarded']))
    s.close()
    # 2) Expect: 100-continue
    s = client()
    s.sendall(b'PUT /u HTTP/1.1\r\nContent-Length: 5\r\nExpect: 100-continue\r\n\r\n')
    first = s.recv(64)
    time.sleep(0.3)
    s.sendall(b'hello')
    resp = b''
    while b'ok' not in resp:
        d = s.recv(100)
        if not d: break
        resp += d
    check('expect-100', first.startswith(b'HTTP/1.1 100') and b'200' in resp and not seen['expect_fwd'])
    s.close()
    # 3) chunked request
    s = client()
    body = b'5\r\nhello\r\n5\r\nworld\r\n0\r\n\r\n'
    s.sendall(b'POST /c HTTP/1.1\r\nTransfer-Encoding: chunked\r\nContent-Length: \r\n\r\n'.replace(b'Content-Length: \r\n', b''))
    time.sleep(0.1); s.sendall(body)
    resp = s.recv(100)
    check('chunked-post', resp.startswith(b'HTTP/1.1 200'))
    s.close()
    # 4) SSE idle 14s not truncated (header phase 10s create_connection timeout)
    s = client()
    s.sendall(b'GET /sse HTTP/1.1\r\n\r\n')
    data = b''
    t0 = time.time()
    while b'late' not in data and time.time() - t0 < 20:
        try:
            d = s.recv(200)
        except socket.timeout:
            break
        if not d: break
        data += d
    check('sse-idle-14s', b'late' in data)
    s.close()
finally:
    proc.terminate()

print('SUMMARY', all(c for _, c in results), len(results))
