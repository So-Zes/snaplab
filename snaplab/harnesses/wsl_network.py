"""HTTP egress broker outside the job namespaces. No private destinations or DNS rebinding."""
from __future__ import annotations

import ipaddress
import select
import socket
import socketserver
from urllib.parse import urlsplit

BLOCKED_V4 = tuple(ipaddress.ip_network(value) for value in (
    '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16',
    '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24', '192.168.0.0/16',
    '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4'))


def public_addresses(host, port, blocked=()):
    host = host.lower().rstrip('.')
    if not host or '.' not in host or host.endswith(('.ts.net', '.local', '.localhost', '.internal', '.lan')):
        raise ValueError('non-public name')
    answers = socket.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
    if not answers:
        raise ValueError('no address')
    for family, kind, proto, _, address in answers:
        ip = ipaddress.ip_address(address[0])
        # Disallow IPv6 entirely: no mapped, NAT64, 6to4 or transition paths.
        if (family != socket.AF_INET or not ip.is_global or ip.is_multicast or str(ip) in blocked
                or any(ip in network for network in BLOCKED_V4)):
            raise ValueError('non-public address')
    return answers


def connect_public(host, port, blocked=(), *, reject_self=False):
    if port not in (80, 443):
        raise ValueError('only public HTTP and HTTPS are supported')
    answers = public_addresses(host, port, blocked() if callable(blocked) else blocked)
    # Connect to the vetted numeric address, never resolve a second time.
    family, kind, proto, _, address = answers[0]
    sock = socket.socket(family, kind, proto)
    sock.settimeout(20)
    try:
        sock.connect(address)
        # Windows performs this connection itself. A newly added local NIC address
        # cannot receive application bytes even if it appeared after enumeration.
        if reject_self and sock.getsockname()[0] == sock.getpeername()[0]:
            raise ValueError('local host destination')
        return sock
    except BaseException:
        sock.close()
        raise


def pump(left, right):
    """Copy bytes both ways until one side closes. No idle limit (2026-11-17): a model can think for minutes
    before its first byte, and a quiet game socket is still alive. The 60-second cut ended model calls whose answer
    took longer, and Pi gave up after seven of them."""
    for side in (left, right):
        side.settimeout(None)
    while True:
        readable, _, _ = select.select([left, right], [], [])
        for source in readable:
            data = source.recv(65536)
            if not data:
                return
            (right if source is left else left).sendall(data)


class Egress(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(30)                 # only while the request line and headers arrive
        try:
            data = bytearray()
            while b'\r\n\r\n' not in data:
                if len(data) >= 32768:
                    raise ValueError('headers too large')
                part = self.request.recv(1)
                if not part:
                    return
                data.extend(part)
            lines = bytes(data).decode('iso-8859-1').split('\r\n')
            method, target, version = lines[0].split(' ')
            if version not in ('HTTP/1.0', 'HTTP/1.1'):
                raise ValueError('bad protocol')
            if method == 'CONNECT':
                address = urlsplit('//' + target)
                if address.username or address.password or address.path or address.query or address.fragment:
                    raise ValueError('bad tunnel')
                with self.server.connect(address.hostname, address.port or 443) as upstream:
                    self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                    pump(self.request, upstream)
            else:
                address = urlsplit(target)
                if address.scheme != 'http' or address.username or address.password or address.fragment:
                    raise ValueError('bad URL')
                with self.server.connect(address.hostname, address.port or 80) as upstream:
                    path = address.path or '/'
                    if address.query:
                        path += '?' + address.query
                    headers = [f'{method} {path} {version}', f'Host: {address.netloc}', 'Connection: close']
                    for line in lines[1:]:
                        if not line:
                            continue
                        if ':' not in line or line[0].isspace():
                            raise ValueError('bad header')
                        name = line.split(':', 1)[0].lower()
                        if name not in ('host', 'connection', 'proxy-connection', 'proxy-authorization'):
                            headers.append(line)
                    upstream.sendall(('\r\n'.join(headers) + '\r\n\r\n').encode('iso-8859-1'))
                    pump(self.request, upstream)
        except (OSError, ValueError, TypeError):
            try:
                self.request.sendall(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
            except OSError:
                pass


class Broker(getattr(socketserver, 'ThreadingUnixStreamServer', socketserver.ThreadingTCPServer)):
    daemon_threads = True
    block_on_close = False

    # No cap on simultaneous connections (2026-11-17). It was 32, and a browser loading a game opens more than that;
    # the rest were closed at once and looked like network errors.
    def __init__(self, path, handler=Egress):
        self.blocked = set()
        super().__init__(str(path), handler)

    def connect(self, host, port):
        return connect_public(host, port, self.blocked)
