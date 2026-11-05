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
  * forwards the COMPLETE request body (content-length or chunked), including
    bodies split across writes or larger than one recv(),
  * forwards WebSocket upgrade requests verbatim as an unbuffered bidirectional
    tunnel, preserving Upgrade/Connection headers,
  * streams responses exactly (content-length or verbatim chunked) over a real
    HTTP/1.1 keep-alive loop so browser connection reuse never crosses
    responses,
  * keeps SSE (/api/...) streaming unbuffered with no read timeout.

fnOS-port fix 0.22.7.1: timeouts are per phase, not per socket.
`socket.create_connection(timeout=10)` leaves a 10s timeout on the socket,
so the old code truncated any idle SSE/WebSocket stream after 10s (recv raised
socket.timeout, which the pumps treated as EOF). Response HEAD waits are now
bounded, and both sockets are switched to blocking mode before any streaming.

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
IDLE_TIMEOUT = 300             # dashboard holds SSE/WS-adjacent connections quiet a long time
REQ_BODY_TIMEOUT = 120         # client must finish sending a declared request body
RESP_HEAD_TIMEOUT = 120        # upstream must produce response headers; body phase is unbounded
DEBUG = os.environ.get("HERMES_SI_PROXY_DEBUG") == "1"


def dlog(msg):
    if DEBUG:
        sys.stderr.write(str(msg) + "\n")
        sys.stderr.flush()


def recv_head(sock, timeout, initial=b""):
    """Read one header block. `initial` carries bytes already read (e.g. a
    pipelined next request left over from a previous request body)."""
    head = initial
    sock.settimeout(timeout)
    while b"\r\n\r\n" not in head:
        try:
            d = sock.recv(4096)
        except OSError:
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
            if limit is not None:
                initial = initial[:max(0, limit - sent)]
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
    for s in (a, b):
        try:
            s.settimeout(None)      # WS frames may be many minutes apart
        except OSError:
            pass

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


def _set_keepalive(sock):
    """Enable TCP keepalive so idle websockets/long polls don't get silently
    dropped by the fnOS gateway chain (observed close code 1005)."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return
    for opt, val in (
        (getattr(socket, "TCP_KEEPIDLE", None), 30),
        (getattr(socket, "TCP_KEEPINTVL", None), 15),
        (getattr(socket, "TCP_KEEPCNT", None), 4),
    ):
        if opt is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, opt, val)
            except OSError:
                pass


def _pump_chunked_request(conn, up, initial):
    """Forward a chunked REQUEST body verbatim (size lines + data + CRLF),
    stopping after the terminating 0-chunk / trailers. Return leftover bytes."""
    buf = initial
    try:
        while True:
            while b"\r\n" not in buf:
                d = conn.recv(4096)
                if not d:
                    return b""
                buf += d
            size_line, _, rest = buf.partition(b"\r\n")
            try:
                n = int(size_line.split(b";", 1)[0], 16)
            except ValueError:
                up.sendall(size_line + b"\r\n")
                return rest
            up.sendall(size_line + b"\r\n")
            buf = rest
            if n == 0:
                # No data follows the 0-chunk: forward trailer lines verbatim
                # until the terminating empty line. Do NOT block waiting for
                # another \r\n\r\n here — the empty line is usually already in
                # buf, and waiting would deadlock the client.
                while True:
                    idx = buf.find(b"\r\n")
                    if idx >= 0:
                        up.sendall(buf[:idx + 2])
                        buf = buf[idx + 2:]
                        if idx == 0:
                            return buf
                        continue
                    d = conn.recv(4096)
                    if not d:
                        return b""
                    buf += d
            need = n + 2                       # chunk data + trailing CRLF
            while len(buf) < need:
                d = conn.recv(min(CHUNK, need - len(buf)))
                if not d:
                    return b""
                buf += d
            up.sendall(buf[:need])
            buf = buf[need:]
    except OSError:
        return b""


def forward_request_body(conn, up, rest, hmap):
    """Forward the COMPLETE request body. Bytes beyond the declared body belong
    to a pipelined next request and are returned for the next loop iteration."""
    try:
        conn.settimeout(REQ_BODY_TIMEOUT)
        if b"chunked" in hmap.get(b"transfer-encoding", b""):
            return _pump_chunked_request(conn, up, rest)
        try:
            n = int(hmap.get(b"content-length", b"0"))
        except ValueError:
            return rest
        if n <= 0:
            return rest
        take = min(n, len(rest))
        if take:
            up.sendall(rest[:take])
        remaining = n - take
        while remaining > 0:
            d = conn.recv(min(CHUNK, remaining))
            if not d:
                break
            up.sendall(d)
            remaining -= len(d)
        return rest[take:]
    except OSError:
        return b""


def handle(conn):
    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    _set_keepalive(conn)
    pending = b""
    try:
        while True:
            line, headers, rest = recv_head(conn, IDLE_TIMEOUT, pending)
            pending = b""
            if line is None:
                return
            hmap = header_map(headers)
            method = line.split(b" ", 1)[0].upper()
            client_conn = hmap.get(b"connection", b"")
            client_close = b"close" in client_conn or (b"http/1.0" in line.lower() and client_conn != b"keep-alive")
            expect_continue = b"100-continue" in hmap.get(b"expect", b"")

            # WebSocket upgrade: tunnel raw, keep Upgrade/Connection verbatim.
            is_upgrade = b"upgrade" in hmap and b"websocket" in hmap.get(b"upgrade", b"") or b"websocket" in hmap.get(b"connection", b"")
            dlog("%s%s -> %s" % (line[:100], " [WS]" if is_upgrade else "", rewrite_path(line).split(b" ")[1:2]))

            out_headers = []
            for h in headers:
                low = h.lower()
                if low.startswith(b"host:"):
                    continue
                # The proxy is the trust boundary: a client-supplied
                # X-Forwarded-* would otherwise be duplicated and, depending on
                # the upstream's first-header-wins lookup, could override the
                # SPA base path / host / client IP.
                if low.startswith((b"x-forwarded-", b"forwarded:")):
                    continue
                if expect_continue and low.startswith(b"expect:"):
                    continue
                if is_upgrade:
                    # Origin/Referer from the fnOS gateway host would fail the
                    # dashboard's loopback Host/Origin rebinding guard.
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
                if expect_continue:
                    # Answer the client's expectation ourselves so it releases
                    # the body; the upstream never sees Expect and won't send
                    # its own 100 (which would deadlock the old 1xx recursion).
                    conn.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
                pending = forward_request_body(conn, up, rest, hmap)

                if is_upgrade:
                    rline, rheaders, rrest = recv_head(up, RESP_HEAD_TIMEOUT)
                    if rline is None:
                        send_error(conn, 504, "Gateway Timeout")
                        return
                    conn.sendall(b"\r\n".join([rline] + rheaders) + b"\r\n\r\n" + rrest)
                    pump_bidir(conn, up)
                    return  # connection fully consumed by the tunnel
                keep = forward_http(conn, up, client_close=client_close)
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


def forward_http(conn, up, client_close=False, sent_any=False):
    """forward one HTTP response from up to conn; return True to keep the client connection alive."""
    rline, rheaders, rrest = recv_head(up, RESP_HEAD_TIMEOUT)
    if rline is None:
        if not sent_any:
            send_error(conn, 504, "Gateway Timeout")
        return False
    hmap = header_map(rheaders)
    status = rline.split(b" ", 2)
    is_1xx = len(status) > 1 and status[1].isdigit() and 100 <= int(status[1]) < 200
    if is_1xx:
        conn.sendall(b"\r\n".join([rline] + rheaders) + b"\r\n\r\n")
        return forward_http(conn, up, client_close, True)

    # Headers are in: streaming (SSE, chunked, long-poll, file download) must
    # never inherit a socket timeout. Clear it on BOTH ends before pumping.
    try:
        up.settimeout(None)
        conn.settimeout(None)
    except OSError:
        pass

    out = []
    close = client_close
    for h in rheaders:
        low = h.lower()
        if low.startswith((b"connection:", b"keep-alive:", b"proxy-connection:")):
            continue
        out.append(h)
    body_mode = "chunked" if b"chunked" in hmap.get(b"transfer-encoding", b"") else ("length" if b"content-length" in hmap else "close")

    if body_mode == "close":
        close = True
    if close:
        out = [h for h in out if not h.lower().startswith((b"transfer-encoding:", b"content-length:"))]
        out.append(b"Connection: close")
    conn.sendall(b"\r\n".join([rline] + out) + b"\r\n\r\n")

    try:
        if body_mode == "chunked":
            pump_chunked_forward(ChunkReader(up, rrest), conn)
        elif body_mode == "length":
            n = int(hmap[b"content-length"])
            pump_exact(up, conn, initial=rrest, limit=n)
        else:
            pump_exact(up, conn, initial=rrest, limit=None)
    except (ValueError, OSError):
        pass
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
