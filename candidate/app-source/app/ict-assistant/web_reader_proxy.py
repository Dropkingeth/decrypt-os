#!/usr/bin/env python3
"""Allow-listed HTTP(S) forward proxy for the ICT assistant's web_fetch tool.

OpenClaw's built-in web_fetch SSRF policy can only add exceptions for
otherwise-blocked private/internal targets (`allowedHostnames`) or deny
specific patterns (`blockedHostnames`); it has no "allow only these public
hosts" mode. This loopback-only proxy supplies that: the harness points
web_fetch at it with `tools.web.fetch.useTrustedEnvProxy: true` plus
HTTP_PROXY/HTTPS_PROXY set to this proxy's address (see bootstrap.py), and
every request to a host outside ALLOWED_HOSTS is refused here before any
connection to that host is made.

Standard library only, no network at import time. One thread per connection,
bounded relay buffers, short timeouts; refuses CONNECT to anything but port
443 and plain HTTP to anything but port 80. Plain HTTP carries exactly one
request per connection with a rebuilt Host header and a bounded body, so a
pipelined second request cannot reach another host. Refuses IP-literal targets
and any address a DNS answer resolves to inside a private/loopback/link-
local/reserved range (defense in depth against DNS rebinding).
"""
from __future__ import annotations

import ipaddress
import re
import socket
import socketserver
import sys
import threading

ALLOWED_HOSTS = frozenset({
    "theinnercircletrader.com",
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
})
ALLOWED_HTTPS_PORT = 443
ALLOWED_HTTP_PORT = 80
MAX_REQUEST_LINE = 8192
RELAY_CHUNK = 65536
MAX_BODY = 65536
# Hop-by-hop and proxy headers are never forwarded; Host is rebuilt from the
# checked target and Connection is forced to close.
DROPPED_HEADERS = frozenset({"host", "connection", "proxy-connection", "keep-alive", "te", "trailer",
                             "upgrade", "proxy-authorization", "proxy-authenticate", "expect"})
HEADER = re.compile(r"^([!#$%&'*+.^_`|~0-9A-Za-z-]+):[ \t]*(.*?)[ \t]*$")
CONNECT_TIMEOUT = 10
IDLE_TIMEOUT = 60
REQUEST_LINE = re.compile(rb"^([A-Za-z]+) (\S+) HTTP/1\.[01]\r\n$")
HOST_PORT = re.compile(r"^\[?([^\[\]]+)\]?:(\d+)$")


def normalize_host(host):
    """None/empty/bracketed-IPv6/trailing-dot all normalize to a plain
    lowercase name; callers compare the result against ALLOWED_HOSTS."""
    if not isinstance(host, str) or not host:
        return ""
    host = host.strip().strip("[]").rstrip(".").lower()
    return host


def is_literal_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def is_allowed_host(host):
    """Exact match only -- no subdomain or suffix matching. www.youtube.com
    is listed explicitly because it is a distinct name from youtube.com, not
    because '*.youtube.com' is accepted."""
    host = normalize_host(host)
    return bool(host) and not is_literal_ip(host) and host in ALLOWED_HOSTS


def is_public_address(address):
    """Refuse loopback/private/link-local/reserved/multicast resolved IPs.
    Guards against an allowed hostname's DNS later answering with an
    internal address (DNS rebinding), not just the hostname text itself."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (parsed.is_private or parsed.is_loopback or parsed.is_link_local
                or parsed.is_multicast or parsed.is_reserved or parsed.is_unspecified)


def resolve_public(host, port):
    """One connected, address-validated socket, or None if nothing public
    resolved. Tries every candidate address before giving up."""
    try:
        candidates = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return None
    for family, kind, proto, _, sockaddr in candidates:
        if not is_public_address(sockaddr[0]):
            continue
        try:
            upstream = socket.socket(family, kind, proto)
            upstream.settimeout(CONNECT_TIMEOUT)
            upstream.connect(sockaddr)
            upstream.settimeout(IDLE_TIMEOUT)
            return upstream
        except OSError:
            continue
    return None


def relay(a, b):
    try:
        while True:
            chunk = a.recv(RELAY_CHUNK)
            if not chunk:
                break
            b.sendall(chunk)
    except OSError:
        pass
    finally:
        for sock in (a, b):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def pump_both_directions(client, upstream):
    forward = threading.Thread(target=relay, args=(client, upstream), daemon=True)
    forward.start()
    relay(upstream, client)
    forward.join(timeout=IDLE_TIMEOUT + 5)


def read_line(client, maximum=MAX_REQUEST_LINE):
    client.settimeout(IDLE_TIMEOUT)
    data = b""
    while not data.endswith(b"\r\n"):
        if len(data) > maximum:
            raise ValueError("line too long")
        chunk = client.recv(1)
        if not chunk:
            raise ValueError("connection closed before a line arrived")
        data += chunk
    return data


def read_request_line(client):
    return read_line(client)


def drain_header_block(client, maximum_headers=200, maximum_bytes=65536):
    """CONNECT carries no body, but real clients still send a header block
    (Host, Proxy-Connection, ...) terminated by a blank line. Those bytes must
    never leak into the raw tunnel once it starts -- discard them here first."""
    total = 0
    for _ in range(maximum_headers):
        line = read_line(client, maximum_bytes)
        total += len(line)
        if total > maximum_bytes:
            raise ValueError("header block too large")
        if line == b"\r\n":
            return
    raise ValueError("too many header lines")


def read_header_block(client, maximum_headers=100, maximum_bytes=32768):
    """[(name, value)] up to the blank line. Folded or malformed lines are refused."""
    headers, total = [], 0
    for _ in range(maximum_headers):
        line = read_line(client, maximum_bytes)
        total += len(line)
        if total > maximum_bytes:
            raise ValueError("header block too large")
        if line == b"\r\n":
            return headers
        match = HEADER.match(line[:-2].decode("latin-1"))
        if not match:
            raise ValueError("malformed header")
        headers.append((match.group(1), match.group(2)))
    raise ValueError("too many header lines")


def read_exact(client, length):
    data = b""
    while len(data) < length:
        chunk = client.recv(min(RELAY_CHUNK, length - len(data)))
        if not chunk:
            raise ValueError("body ended early")
        data += chunk
    return data


def rebuild_request(method, origin_form, host, headers, body):
    """One origin-form request: Host from the checked target, Connection: close,
    no hop-by-hop or proxy headers, Content-Length only when a body is sent."""
    lines = [method + " " + origin_form + " HTTP/1.1", "Host: " + host]
    for name, value in headers:
        if name.lower() not in DROPPED_HEADERS and name.lower() != "content-length":
            lines.append(name + ": " + value)
    if body:
        lines.append("Content-Length: " + str(len(body)))
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def send_refusal(client, status):
    client.sendall(("HTTP/1.1 " + status + "\r\nConnection: close\r\n\r\n").encode("ascii"))


class ReaderHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            line = read_request_line(self.request)
        except (OSError, ValueError):
            return
        match = REQUEST_LINE.match(line)
        if not match:
            send_refusal(self.request, "400 Bad Request")
            return
        method, target = match.group(1).decode("ascii"), match.group(2).decode("ascii")
        if method.upper() == "CONNECT":
            self.handle_connect(target)
        else:
            self.handle_plain_http(method, target, line)

    def handle_connect(self, target):
        pair = HOST_PORT.match(target)
        if not pair:
            send_refusal(self.request, "400 Bad Request")
            return
        host, port = pair.group(1), int(pair.group(2))
        if port != ALLOWED_HTTPS_PORT or not is_allowed_host(host):
            send_refusal(self.request, "403 Forbidden")
            return
        try:
            drain_header_block(self.request)
        except (OSError, ValueError):
            return
        upstream = resolve_public(host, port)
        if upstream is None:
            send_refusal(self.request, "502 Bad Gateway")
            return
        try:
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            pump_both_directions(self.request, upstream)
        finally:
            upstream.close()

    def handle_plain_http(self, method, target, request_line):
        from urllib.parse import urlsplit
        parsed = urlsplit(target)
        host = parsed.hostname or ""
        port = parsed.port or ALLOWED_HTTP_PORT
        if port != ALLOWED_HTTP_PORT or not is_allowed_host(host):
            send_refusal(self.request, "403 Forbidden")
            return
        try:
            headers = read_header_block(self.request)
            names = [name.lower() for name, _ in headers]
            if "transfer-encoding" in names or names.count("content-length") > 1:
                raise ValueError("unsupported body framing")
            length = int(dict((n.lower(), v) for n, v in headers).get("content-length", "0"))
            if not 0 <= length <= MAX_BODY:
                raise ValueError("body too large")
            body = read_exact(self.request, length)
        except (OSError, ValueError):
            send_refusal(self.request, "400 Bad Request")
            return
        upstream = resolve_public(host, port)
        if upstream is None:
            send_refusal(self.request, "502 Bad Gateway")
            return
        origin_form = parsed.path or "/"
        if parsed.query:
            origin_form += "?" + parsed.query
        try:
            upstream.sendall(rebuild_request(method, origin_form, normalize_host(host), headers, body))
            # One request per connection: only the response flows back, and
            # nothing more the client sends is forwarded.
            relay(upstream, self.request)
        finally:
            upstream.close()


class ReaderServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start(port, bind="127.0.0.1"):
    """Returns a running server; caller owns shutdown() and server_close()."""
    server = ReaderServer((bind, port), ReaderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8783)
    parser.add_argument("--bind", default="127.0.0.1")
    args = parser.parse_args(argv)
    server = start(args.port, args.bind)
    print(f"ICT web reader proxy on {args.bind}:{args.port}; allowed hosts: "
          f"{', '.join(sorted(ALLOWED_HOSTS))}", file=sys.stderr)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
