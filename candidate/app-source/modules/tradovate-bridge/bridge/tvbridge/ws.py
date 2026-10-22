"""Minimal RFC 6455 WebSocket client and test server (stdlib only).

Used for the local Chrome DevTools connection and the offline fake Tradovate
server. Text frames, ping/pong, close and fragmentation are supported.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading
import urllib.parse

GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


class Closed(Exception):
    pass


def _read_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError as e:
            raise Closed(str(e))
        if not chunk:
            raise Closed('socket closed')
        buf += chunk
    return bytes(buf)


def _read_headers(sock):
    data = bytearray()
    while b'\r\n\r\n' not in data:
        chunk = sock.recv(1)
        if not chunk:
            raise Closed('socket closed during handshake')
        data += chunk
        if len(data) > 65536:
            raise ValueError('handshake too large')
    head = data.decode('latin-1').split('\r\n')
    headers = {}
    for line in head[1:]:
        if ':' in line:
            k, v = line.split(':', 1)
            headers[k.strip().lower()] = v.strip()
    return head[0], headers


class Conn:
    """One WebSocket connection. `mask` is True for clients."""

    def __init__(self, sock, mask):
        self.sock = sock
        self.mask = mask
        self.send_lock = threading.Lock()
        self.closed = False

    def send_text(self, text):
        self._send(0x1, text.encode('utf-8'))

    def _send(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        bit = 0x80 if self.mask else 0
        if n < 126:
            header.append(bit | n)
        elif n < 65536:
            header.append(bit | 126)
            header += struct.pack('!H', n)
        else:
            header.append(bit | 127)
            header += struct.pack('!Q', n)
        if self.mask:
            key = os.urandom(4)
            header += key
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        with self.send_lock:
            if self.closed:
                raise Closed('connection closed')
            self.sock.sendall(bytes(header) + payload)

    def recv_text(self):
        """Return the next complete text message, answering pings. Raises Closed."""
        parts = []
        while True:
            b1, b2 = _read_exact(self.sock, 2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack('!H', _read_exact(self.sock, 2))[0]
            elif n == 127:
                n = struct.unpack('!Q', _read_exact(self.sock, 8))[0]
            key = _read_exact(self.sock, 4) if b2 & 0x80 else None
            payload = _read_exact(self.sock, n) if n else b''
            if key:
                payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                self.close()
                raise Closed('peer closed')
            if opcode == 0x9:
                self._send(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2, 0x0):
                parts.append(payload)
                if fin:
                    return b''.join(parts).decode('utf-8')

    def close(self):
        if self.closed:
            return
        try:
            self._send(0x8, b'')
        except Exception:
            pass
        self.closed = True
        try:
            self.sock.close()
        except Exception:
            pass


def connect(url, timeout=10):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != 'ws':
        raise ValueError('only ws:// loopback connections are supported')
    host, port = parts.hostname, parts.port or 80
    if host not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('refusing non-loopback DevTools connection')
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    path = parts.path + ('?' + parts.query if parts.query else '')
    req = (f'GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n'
           f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n')
    sock.sendall(req.encode())
    status, headers = _read_headers(sock)
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    if ' 101 ' not in status + ' ' or headers.get('sec-websocket-accept') != accept:
        sock.close()
        raise ConnectionError('websocket handshake failed: ' + status)
    sock.settimeout(None)
    return Conn(sock, mask=True)


class Server:
    """Tiny threaded WebSocket server for tests. handler(conn, path) runs per client."""

    def __init__(self, handler, host='127.0.0.1', port=0):
        self.handler = handler
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.running = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while self.running:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client):
        try:
            status, headers = _read_headers(client)
            key = headers.get('sec-websocket-key', '')
            accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            client.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                            f'Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n').encode())
            self.handler(Conn(client, mask=False), status.split(' ')[1])
        except Closed:
            pass
        finally:
            try:
                client.close()
            except Exception:
                pass

    def stop(self):
        self.running = False
        self.sock.close()
