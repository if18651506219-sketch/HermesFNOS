#!/usr/bin/env python3
"""Unix-socket -> TCP proxy for the fnOS unified gateway, fronting the Hermes FNOS dashboard.

The fnOS gateway forwards /app/<prefix>/... WITHOUT stripping and passes the
browser's Host through. Unlike the Strata port, hermes' own FastAPI backend
natively understands `X-Forwarded-Prefix`: index.html injection, asset URL
rewrites and CSS url() rewrites all happen server-side when that header is
present. So this proxy deliberately does NOT touch response bodies — it only:

  * rewrites the request line back to the stripped path (FastAPI routes are
    registered without the prefix; X-FP only informs the SPA base path),
  * sets `Host: 127.0.0.1:<port>` (the dashboard enforces a Host-header check
    against the bound loopback host — DNS-rebinding guard GHSA-ppp5-vxwm-4cf7)
    and adds `X-Forwarded-Prefix: <prefix>`,
  * forwards WebSocket upgrade requests (chat PTY at /api/pty) verbatim as an
    unbuffered bidirectional tunnel, preserving Upgrade/Connection headers,
  * streams responses exactly (content-length or verbatim chunked) over a real
    HTTP/1.1 keep-alive loop so browser connection reuse never crosses
    responses,
  * keeps SSE (/api/...) streaming unbuffered with no read timeout.

usage: sock_proxy.py <unix-socket-path> <tcp-port> [gateway-prefix]
"""
import os
import socket
import sys
import threading

SOCK = sys.argv[1]
PORT = int(sys.argv[2])
PREFIX = (sys.argv[3] if len(sys.argv) > 3 else "/app/hermes-si").encode()
PREFIX_STR = PREFIX.decode()
BACKLOG = 128
CHUNK = 65536
IDLE_TIMEOUT = 300                # dashboard holds SSE/WS-adjacent connections quiet a long time
DEBUG = os.environ.get("HERMES_SI_PROXY_DEBUG") == "1"

def dlog(msg):
    if DEBUG:
        sys.stderr.write(str(msg) + "\n")
        sys.stderr.flush()


def recv_head(sock, timeout):
    head = b""
    sock.settimeout(timeout)
    while b"\r\n\r\n" not in head:
        try:
            d = sock.recv(4096)
        except socket.timeout:
            return None, None, None
        if not d:
            return None, None, None
        head += d
        if len(head) > 128 * 1024:
            return None, None, None
    header_block, _, rest = head.partition(b"\r\n\r\n")
    lines = header_block.split(b"\r\n")
    return lines[0], lines[1:], rest


def header_map(headers):
    out = {}
    for h in headers:
        k, _, v = h.partition(b":")
        out[k.strip().lower()] = v.strip().lower()
    return out


def rewrite_path(method_line):
    parts = method_line.split(b" ", 2)
    if len(parts) != 3:
        return method_line
    p = parts[1]
    if p == PREFIX or p.startswith(PREFIX + b"/"):
        target = p[len(PREFIX):] or b"/"
    elif p.startswith(b"/app/"):
        tail = p[len(b"/app/"):]
        seg, slash, remainder = tail.partition(b"/")
        target = (b"/" + remainder) if slash else b"/"
    else:
        target = p
    return parts[0] + b" " + target + b" " + parts[2]


def pump_exact(src, dst, initial=b"", limit=None):
    sent = 0
    try:
        if initial:
            dst.sendall(initial)
            sent += len(initial)
        while limit is None or sent < limit:
            want = CHUNK if limit is None else min(CHUNK, limit - sent)
            d = src.recv(want)
            if not d:
                break
            dst.sendall(d)
            sent += len(d)
    except OSError:
        pass
    return sent


def pump_bidir(a, b, initial_for_b=b""):
    """raw tunnel: copy a->b and b->a until both EOF; returns when either direction fully drains."""
    def copy(src, dst, initial=b""):
        try:
            if initial:
                dst.sendall(initial)
            while True:
                d = src.recv(CHUNK)
                if not d:
                    break
                dst.sendall(d)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass
    t1 = threading.Thread(target=copy, args=(a, b, initial_for_b))
    t2 = threading.Thread(target=copy, args=(b, a))
    t1.start(); t2.start(); t1.join(); t2.join()


class ChunkReader:
    """incremental chunked-body reader over one socket."""

    def __init__(self, sock, buf=b""):
        self.sock = sock
        self.buf = buf
        self.pos = 0
        self.eof = False

    def _fill(self):
        try:
            d = self.sock.recv(CHUNK)
        except OSError:
            d = b""
        if not d:
            self.eof = True
            return False
        self.buf += d
        return True

    def read_chunk(self):
        while True:
            j = self.buf.find(b"\r\n", self.pos)
            if j < 0:
                if not self._fill():
                    return None
                continue
            try:
                n = int(self.buf[self.pos:j], 16)
            except ValueError:
                return None
            if n == 0:
                self.eof = True
                return None
            need = j + 2 + n
            while len(self.buf) < need + 2:
                if not self._fill():
                    break
            data = self.buf[need - n:need]
            self.pos = need + 2
            if self.pos > 1 << 20:
                self.buf = self.buf[self.pos:]
                self.pos = 0
            return data


def pump_chunked_forward(reader, dst):
    try:
        while True:
            data = reader.read_chunk()
            if data is None:
                dst.sendall(b"0\r\n\r\n")
                break
            dst.sendall(b"%x\r\n" % len(data) + data + b"\r\n")
    except OSError:
        pass


def send_error(conn, code, text):
    body = text.encode()
    try:
        conn.sendall(b"HTTP/1.1 %d %s\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                     % (code, text.encode(), len(body)) + body)
    except OSError:
        pass


def handle(conn):
    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    keep = True
    try:
        while keep:
            line, headers, rest = recv_head(conn, IDLE_TIMEOUT)
            if line is None:
                return
            hmap = header_map(headers)
            method = line.split(b" ", 1)[0].upper()
            client_conn = hmap.get(b"connection", b"")
            client_close = client_conn == b"close" or (b"http/1.0" in line.lower() and client_conn != b"keep-alive")

            # WebSocket upgrade: tunnel raw, keep Upgrade/Connection verbatim.
            is_upgrade = b"upgrade" in hmap and b"websocket" in hmap.get(b"upgrade", b"") or b"websocket" in hmap.get(b"connection", b"")
            dlog("%s%s -> %s" % (line[:100], " [WS]" if is_upgrade else "", rewrite_path(line).split(b" ")[1:2]))

            out_headers = []
            for h in headers:
                low = h.lower()
                if low.startswith(b"host:"):
                    continue
                if is_upgrade:
                    # Origin/Referer from the fnOS gateway host would fail the
                    # dashboard's loopback Host/Origin rebinding guard; the proxy
                    # is the trusted boundary (loopback-only socket + fnOS auth),
                    # so strip web-origin headers like the strata port does.
                    if low.startswith((b"origin:", b"referer:")):
                        continue
                    out_headers.append(h)  # Upgrade/Connection must survive for the handshake
                    continue
                if low.startswith((b"connection:", b"keep-alive:", b"proxy-connection:")):
                    continue
                if low.startswith(b"accept-encoding:"):
                    h = b"Accept-Encoding: identity"
                out_headers.append(h)
            out_headers.append(b"Host: 127.0.0.1:%d" % PORT)
            out_headers.append(b"X-Forwarded-Prefix: " + PREFIX)
            up_line = rewrite_path(line)

            up = None
            try:
                up = socket.create_connection(("127.0.0.1", PORT), timeout=10)
                up.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                head_bytes = b"\r\n".join([up_line] + out_headers) + b"\r\n\r\n"
                up.sendall(head_bytes)
                if rest:
                    up.sendall(rest)

                if is_upgrade:
                    rline, rheaders, rrest = recv_head(up, 20)
                    if rline is None:
                        send_error(conn, 502, "Bad Gateway")
                        return
                    # 101 handshake: forward verbatim, then tunnel both directions raw.
                    conn.sendall(b"\r\n".join([rline] + rheaders) + b"\r\n\r\n" + rrest)
                    pump_bidir(conn, up)
                    return  # connection fully consumed by the tunnel
                keep = forward_http(conn, up, rline_holder=None, client_close=client_close)
            except OSError as e:
                dlog("upstream error: %s" % e)
                send_error(conn, 502, "Bad Gateway")
                return
            finally:
                if up is not None:
                    try:
                        up.close()
                    except OSError:
                        pass
            if not keep:
                return
    finally:
        try:
            conn.close()
        except OSError:
            pass


def forward_http(conn, up, rline_holder, client_close):
    """forward one HTTP response from up to conn; return True to keep the client connection alive."""
    rline, rheaders, rrest = recv_head(up, None)
    if rline is None:
        return False
    hmap = header_map(rheaders)
    status = rline.split(b" ", 2)
    is_1xx = len(status) > 1 and status[1].isdigit() and 100 <= int(status[1]) < 200
    if is_1xx:
        conn.sendall(b"\r\n".join([rline] + rheaders) + b"\r\n\r\n")
        return forward_http(conn, up, None, client_close)

    out = []
    close = client_close
    for h in rheaders:
        low = h.lower()
        if low.startswith((b"connection:", b"keep-alive:", b"proxy-connection:")):
            continue
        if low.startswith(b"transfer-encoding:") and b"chunked" in low:
            out.append(h)
            continue
        out.append(h)
    body_mode = "chunked" if b"chunked" in hmap.get(b"transfer-encoding", b"") else ("length" if b"content-length" in hmap else "close")

    if body_mode == "close":
        close = True
    if close:
        out = [h for h in out if not h.lower().startswith((b"transfer-encoding:", b"content-length:"))]
        out.append(b"Connection: close")
    conn.sendall(b"\r\n".join([rline] + out) + b"\r\n\r\n")

    if body_mode == "chunked":
        pump_chunked_forward(ChunkReader(up, rrest), conn)
    elif body_mode == "length":
        n = int(hmap[b"content-length"])
        pump_exact(up, conn, initial=rrest, limit=n)
    else:
        pump_exact(up, conn, initial=rrest, limit=None)
    return not close


def main():
    try:
        if os.path.exists(SOCK):
            os.unlink(SOCK)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    try:
        os.chmod(SOCK, 0o660)
    except OSError:
        pass
    srv.listen(BACKLOG)

    def stop(*_):
        try:
            srv.close()
        except OSError:
            pass
        try:
            os.unlink(SOCK)
        except OSError:
            pass
        sys.exit(0)

    import signal
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            break
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
