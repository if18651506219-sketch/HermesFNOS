#!/usr/bin/env python3
"""Behaviour regression harness for sock_proxy.py (fnOS-port 0.22.7.1).

This exists because verify.py's L0 assertions are mostly *string presence*
checks. They went green while the proxy silently dropped request bodies and
inherited a 10s socket timeout that truncated idle SSE/WebSocket streams.
These tests drive the REAL proxy against a fake upstream, over a real unix
socket, and assert observable behaviour.

Scenarios:
  1. small POST, header+body in one write
  2. small POST, header and body in separate writes
  3. 10 KB POST, one write (body spans multiple recv() calls)
  4. 10 KB POST, separate writes
  5. client-forged X-Forwarded-Prefix / X-Forwarded-Host are stripped
  6. two sequential requests on one keep-alive client connection
  7. chunked request body
  8. slow chunked RESPONSE (12s idle) is not truncated

usage: python3 scripts/verify_proxy.py [--proxy PATH] [--quick]
  --quick skips scenario 8 (the 12s idle-stream test)
"""
import argparse
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

PREFIX = "/app/hermes-si"
FAILS = []
PROXY = str(Path(__file__).resolve().parent.parent / "sock_proxy.py")

# AF_UNIX paths are capped at ~104 bytes on macOS / 108 on Linux. A
# mkdtemp() prefix plus a descriptive case name exceeds that and bind()
# fails with "AF_UNIX path too long" — use short, pid-scoped paths instead.
_SEQ = [0]
SOCK_PATHS = []


def next_sock():
    _SEQ[0] += 1
    p = "/tmp/hpx%d_%d.sock" % (os.getpid(), _SEQ[0])
    SOCK_PATHS.append(p)
    return p


def check(tag, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + tag + (" | " + detail if detail else ""))
    if not cond:
        FAILS.append(tag)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def read_head(c, buf=b""):
    while b"\r\n\r\n" not in buf:
        d = c.recv(4096)
        if not d:
            break
        buf += d
    return buf.partition(b"\r\n\r\n")


def read_response(c, buf=b""):
    """Parse one response. When the proxy strips Content-Length in favour of
    `Connection: close` (client requested close), the body is everything up
    to EOF — do NOT assume length 0 and silently drop it."""
    head, _, body = read_head(c, buf)
    cl = None
    for h in head.split(b"\r\n"):
        if h.lower().startswith(b"content-length:"):
            cl = int(h.split(b":", 1)[1])
    if cl is None:
        while True:
            d = c.recv(4096)
            if not d:
                break
            body += d
        return head, body, b""
    while len(body) < cl:
        d = c.recv(4096)
        if not d:
            break
        body += d
    return head, body[:cl], body[cl:]


def content_length(head):
    for h in head.split(b"\r\n"):
        if h.lower().startswith(b"content-length:"):
            return int(h.split(b":", 1)[1])
    return 0


def start_proxy(port, sock_path):
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    p = subprocess.Popen(
        [sys.executable, PROXY, sock_path, str(port), PREFIX],
        stdout=subprocess.DEVNULL, stderr=None)
    for _ in range(60):
        if os.path.exists(sock_path):
            break
        time.sleep(0.05)
    if p.poll() is not None:
        print('  ! proxy exited early, rc=%s' % p.returncode)
    return p


def client(sock_path, timeout=8):
    c = socket.socket(socket.AF_UNIX)
    c.settimeout(timeout)
    for _ in range(40):
        try:
            c.connect(sock_path)
            break
        except OSError:
            time.sleep(0.05)
    return c


def make_server(port):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)
    srv.settimeout(20)
    return srv


def stop_proxy(proxy):
    proxy.terminate()
    try:
        proxy.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proxy.kill()


def body_case(label, body_len, split_write, extra=b""):
    """Scenarios 1-5: does the upstream receive the COMPLETE declared body?"""
    port = free_port()
    sock_path = next_sock()
    obs = {}
    done = threading.Event()
    srv = make_server(port)

    def upstream():
        try:
            c, _ = srv.accept()
            c.settimeout(8)
            head, _, rest = read_head(c)
            obs["declared"] = content_length(head)
            obs["xfp"] = [h.decode(errors="replace") for h in head.split(b"\r\n")
                          if h.lower().startswith(b"x-forwarded-prefix:")]
            obs["xfh"] = [h.decode(errors="replace") for h in head.split(b"\r\n")
                          if h.lower().startswith(b"x-forwarded-host:")]
            body = rest
            while len(body) < obs["declared"]:
                d = c.recv(4096)
                if not d:
                    break
                body += d
            obs["received"] = len(body)
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
            c.close()
        except OSError as e:
            obs["upstream_error"] = repr(e)
        finally:
            srv.close()
            done.set()

    threading.Thread(target=upstream, daemon=True).start()
    proxy = start_proxy(port, sock_path)
    cli = client(sock_path)
    err = None
    try:
        req = (b"POST " + PREFIX.encode() + b"/api/test HTTP/1.1\r\n"
               b"Host: nas.local\r\nContent-Type: application/octet-stream\r\n"
               b"Content-Length: " + str(body_len).encode() + b"\r\n"
               b"Connection: close\r\n" + extra + b"\r\n")
        body = b"x" * body_len
        if split_write:
            cli.sendall(req)
            time.sleep(0.08)
            cli.sendall(body)
        else:
            cli.sendall(req + body)
        head, payload, _ = read_response(cli)
        obs["status"] = head.split(b"\r\n")[0]
        obs["reply"] = payload
    except OSError as e:
        err = repr(e)
    finally:
        cli.close()
        done.wait(10)
        stop_proxy(proxy)

    ok = (not err and obs.get("declared") == body_len
          and obs.get("received") == body_len
          and obs.get("status") == b"HTTP/1.1 200 OK"
          and obs.get("reply") == b"OK")
    if extra:
        ok = ok and obs.get("xfp") == ["X-Forwarded-Prefix: " + PREFIX] and not obs.get("xfh")
    check(label, bool(ok), (err or "") + " " + str(obs))


def keepalive_case():
    """Scenario 6: two requests on ONE client connection."""
    port = free_port()
    sock_path = next_sock()
    obs = {"seen": [], "replies": []}
    done = threading.Event()
    srv = make_server(port)

    def upstream():
        try:
            for _ in range(2):
                c, _ = srv.accept()
                c.settimeout(8)
                head, _, rest = read_head(c)
                n = content_length(head)
                body = rest
                while len(body) < n:
                    d = c.recv(4096)
                    if not d:
                        break
                    body += d
                obs["seen"].append((head.split(b"\r\n")[0], len(body)))
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
                c.close()
        except OSError as e:
            obs["upstream_error"] = repr(e)
        finally:
            srv.close()
            done.set()

    threading.Thread(target=upstream, daemon=True).start()
    proxy = start_proxy(port, sock_path)
    cli = client(sock_path)
    err = None
    extra = b""
    try:
        for _ in (1, 2):
            cli.sendall(b"POST " + PREFIX.encode() + b"/api/t HTTP/1.1\r\nHost: nas\r\n"
                        b"Content-Length: 50\r\n\r\n" + b"z" * 50)
            head, payload, extra = read_response(cli, extra)
            obs["replies"].append((head.split(b"\r\n")[0], payload))
    except OSError as e:
        err = repr(e)
    finally:
        cli.close()
        done.wait(10)
        stop_proxy(proxy)

    ok = (not err and len(obs["seen"]) == 2 and all(x[1] == 50 for x in obs["seen"])
          and len(obs["replies"]) == 2
          and all(r[0] == b"HTTP/1.1 200 OK" and r[1] == b"OK" for r in obs["replies"]))
    check("keepalive-2-requests", bool(ok), (err or "") + " " + str(obs))


def chunked_request_case():
    """Scenario 7: chunked request body is forwarded verbatim."""
    port = free_port()
    sock_path = next_sock()
    obs = {}
    done = threading.Event()
    srv = make_server(port)

    def upstream():
        try:
            c, _ = srv.accept()
            c.settimeout(8)
            head, _, rest = read_head(c)
            buf = rest
            parts = []
            while True:
                while b"\r\n" not in buf:
                    d = c.recv(4096)
                    if not d:
                        break
                    buf += d
                line, _, tail = buf.partition(b"\r\n")
                n = int(line.split(b";", 1)[0], 16)
                buf = tail
                if n == 0:
                    break
                while len(buf) < n + 2:
                    d = c.recv(4096)
                    if not d:
                        break
                    buf += d
                parts.append(buf[:n])
                buf = buf[n + 2:]
            obs["body"] = b"".join(parts)
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
            c.close()
        except (OSError, ValueError) as e:
            obs["upstream_error"] = repr(e)
        finally:
            srv.close()
            done.set()

    threading.Thread(target=upstream, daemon=True).start()
    proxy = start_proxy(port, sock_path)
    cli = client(sock_path)
    err = None
    try:
        cli.sendall(b"POST " + PREFIX.encode() + b"/api/t HTTP/1.1\r\nHost: nas\r\n"
                    b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
        time.sleep(0.05)
        cli.sendall(b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n")
        head, payload, _ = read_response(cli)
        obs["status"] = head.split(b"\r\n")[0]
        obs["reply"] = payload
    except OSError as e:
        err = repr(e)
    finally:
        cli.close()
        done.wait(10)
        stop_proxy(proxy)
    check("chunked-request-body",
          not err and obs.get("body") == b"hello world" and obs.get("reply") == b"OK",
          (err or "") + " " + str(obs))


def slow_stream_case():
    """Scenario 8: >10s idle in the response body must not be treated as EOF.

    Reproduces the bug where create_connection(timeout=10) left a 10s timeout
    on the upstream socket, so an idle SSE/WS stream was truncated.
    """
    port = free_port()
    sock_path = next_sock()
    obs = {}
    done = threading.Event()
    srv = make_server(port)

    def upstream():
        try:
            c, _ = srv.accept()
            c.settimeout(30)
            read_head(c)
            c.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nfirst\r\n")
            time.sleep(12)
            c.sendall(b"6\r\nsecond\r\n0\r\n\r\n")
            c.close()
        except OSError as e:
            obs["upstream_error"] = repr(e)
        finally:
            srv.close()
            done.set()

    threading.Thread(target=upstream, daemon=True).start()
    proxy = start_proxy(port, sock_path)
    cli = client(sock_path, timeout=25)
    data = b""
    err = None
    t0 = time.time()
    try:
        cli.sendall(b"GET " + PREFIX.encode() + b"/api/stream HTTP/1.1\r\nHost: nas\r\n"
                    b"Connection: close\r\n\r\n")
        while True:
            d = cli.recv(4096)
            if not d:
                break
            data += d
            if b"0\r\n\r\n" in data:
                break
    except OSError as e:
        err = repr(e)
    finally:
        obs["elapsed"] = round(time.time() - t0, 1)
        cli.close()
        done.wait(10)
        stop_proxy(proxy)
    check("slow-stream-not-truncated",
          not err and b"first" in data and b"second" in data and obs["elapsed"] >= 12,
          (err or "") + " " + str(obs) + " " + repr(data[:100]))


def main():
    global PROXY
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=PROXY)
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    PROXY = str(Path(a.proxy).resolve())
    if not Path(PROXY).is_file():
        sys.exit("proxy script not found: " + PROXY)

    try:
        body_case("post-small-single-write", 100, False)
        body_case("post-small-split-write", 100, True)
        body_case("post-10k-single-write", 10000, False)
        body_case("post-10k-split-write", 10000, True)
        body_case("forwarded-headers-sanitized", 100, False,
                  extra=b"X-Forwarded-Prefix: /evil\r\nX-Forwarded-Host: attacker.example\r\n")
        keepalive_case()
        chunked_request_case()
        if not a.quick:
            slow_stream_case()
    finally:
        for p in SOCK_PATHS:
            try:
                os.path.exists(p) and os.unlink(p)
            except OSError:
                pass

    print("\n" + ("ALL-PASS" if not FAILS else "FAILED: " + ",".join(FAILS)))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
