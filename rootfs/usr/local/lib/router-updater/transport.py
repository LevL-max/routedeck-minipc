"""Temporary loopback SOCKS5 transport using the router's existing policy routes.

No interfaces, firewall rules, DNS settings or router modes are changed.
Both DNS and payload sockets are marked and bound to the selected interface.
"""
import ipaddress
import json
import os
import select
import socket
import socketserver
import struct
import subprocess
import threading
import time
from pathlib import Path

def current_wan(fallback='enp3s0'):
    try:
        value=json.loads(Path('/etc/router/wan-active.json').read_text())
        return value['interface']
    except (OSError,ValueError,KeyError): return fallback

MODES = {'awg': (1, 'wg0'), 'tun': (2, 'tun0'),
         'mihomo': (3, 'mihomo0')}


def recv_exact(sock, count):
    result = bytearray()
    while len(result) < count:
        part = sock.recv(count - len(result))
        if not part:
            raise OSError('Unexpected EOF')
        result.extend(part)
    return bytes(result)


def dns_addresses(packet, transaction):
    if len(packet) < 12:
        raise OSError('Short DNS response')
    ident, flags, questions, answers, _, _ = struct.unpack('!6H', packet[:12])
    if ident != transaction or not flags & 0x8000 or flags & 15 or flags & 0x200:
        raise OSError('Unsuccessful DNS response')

    def skip_name(offset):
        for _ in range(128):
            if offset >= len(packet):
                raise OSError('Invalid DNS name')
            size = packet[offset]
            if size == 0:
                return offset + 1
            if size & 0xc0 == 0xc0:
                if offset + 1 >= len(packet):
                    raise OSError('Invalid DNS pointer')
                return offset + 2
            if size > 63:
                raise OSError('Invalid DNS label')
            offset += size + 1
        raise OSError('DNS name too long')

    offset = 12
    for _ in range(questions):
        offset = skip_name(offset) + 4
    found = []
    for _ in range(answers):
        offset = skip_name(offset)
        if offset + 10 > len(packet):
            raise OSError('Truncated DNS answer')
        kind, klass, ttl, length = struct.unpack('!HHIH', packet[offset:offset + 10])
        offset += 10
        if offset + length > len(packet):
            raise OSError('Truncated DNS data')
        if kind == 1 and klass == 1 and length == 4:
            found.append(socket.inet_ntoa(packet[offset:offset + 4]))
        offset += length
    if not found:
        raise OSError('DNS returned no IPv4 address')
    return found


class Transport:
    connect_timeout = 12  # seconds per address (routedeck-access-v1: the panel's IP lookup uses 3)
    def __init__(self, route='current', wan='@@WAN_IF@@', mode_file='/etc/router/mode'):
        if route not in ('current', 'direct'):
            raise ValueError('Unsupported download route')
        wan = current_wan(wan)
        self.initial_wan = wan
        self.mode_file = Path(mode_file)
        self.initial_mode = self.mode_file.read_text().strip()
        self.mode = self.initial_mode if route == 'current' else 'direct'
        if self.mode == 'direct':
            self.mark, self.interface = 0, wan
        elif self.mode in MODES:
            self.mark, self.interface = MODES[self.mode]
        else:
            raise ValueError('Unknown router mode')
        self.cache = {}
        self.server = None
        self.thread = None
        self.sockets = set()
        self.guard = threading.Lock()
        self.validate_route()

    def validate_route(self):
        if current_wan(self.initial_wan) != self.initial_wan:
            raise OSError('WAN changed during download; retry on the selected WAN')
        if self.mode_file.read_text().strip() != self.initial_mode:
            raise OSError('Router mode changed during download; retry in the new mode')
        command = ['ip', '-j', '-4', 'route', 'get', '1.1.1.1']
        if self.mark:
            command += ['mark', str(self.mark)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=4, check=True)
        routes = json.loads(result.stdout)
        if not routes or routes[0].get('dev') != self.interface:
            raise OSError('Selected update route is unavailable; direct fallback is disabled')

    def connect_ip(self, address, port, timeout=12):
        self.validate_route()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.interface.encode() + b'\0')
            if self.mark:
                sock.setsockopt(socket.SOL_SOCKET, 36, self.mark)
            sock.connect((address, port))
            return sock
        except BaseException:
            sock.close()
            raise

    def resolve(self, hostname):
        try:
            address = ipaddress.ip_address(hostname)
            if address.version != 4:
                raise OSError('This router update transport currently uses IPv4')
            return [str(address)]
        except ValueError:
            pass
        cached = self.cache.get(hostname)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        name = hostname.rstrip('.').encode('idna')
        labels = name.split(b'.')
        if len(name) > 253 or any(not x or len(x) > 63 for x in labels):
            raise OSError('Invalid DNS name')
        transaction = int.from_bytes(os.urandom(2), 'big')
        question = b''.join(bytes([len(x)]) + x for x in labels) + b'\0' + struct.pack('!HH', 1, 1)
        packet = struct.pack('!6H', transaction, 0x100, 1, 0, 0, 0) + question
        errors = []
        for resolver in ('1.1.1.1', '9.9.9.9', '77.88.8.8'):
            try:
                with self.connect_ip(resolver, 53, timeout=4) as sock:
                    sock.sendall(struct.pack('!H', len(packet)) + packet)
                    length = struct.unpack('!H', recv_exact(sock, 2))[0]
                    addresses = dns_addresses(recv_exact(sock, length), transaction)
                self.cache[hostname] = (time.monotonic() + 60, addresses)
                return addresses
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                errors.append(type(exc).__name__)
        raise OSError('DNS through selected update route failed: ' + ', '.join(errors))

    def connect(self, hostname, port):
        last = None
        for address in self.resolve(hostname)[:4]:
            try:
                return self.connect_ip(address, port, timeout=self.connect_timeout)
            except OSError as exc:
                last = exc
        raise OSError('Connection through selected update route failed') from last

    def __enter__(self):
        transport = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                if not self.server.slots.acquire(blocking=False):
                    return
                outgoing = None
                try:
                    self.request.settimeout(10)
                    version, count = recv_exact(self.request, 2)
                    methods = recv_exact(self.request, count)
                    if version != 5 or 0 not in methods:
                        return
                    self.request.sendall(b'\x05\x00')
                    version, command, reserved, atyp = recv_exact(self.request, 4)
                    if version != 5 or command != 1 or reserved:
                        raise OSError('Only SOCKS CONNECT is supported')
                    if atyp == 3:
                        host = recv_exact(self.request, recv_exact(self.request, 1)[0]).decode('ascii')
                    elif atyp == 1:
                        host = socket.inet_ntoa(recv_exact(self.request, 4))
                    else:
                        raise OSError('Unsupported address family')
                    port = struct.unpack('!H', recv_exact(self.request, 2))[0]
                    if port not in (80, 443):
                        raise OSError('Only HTTP and HTTPS update destinations are allowed')
                    outgoing = transport.connect(host, port)
                    with transport.guard:
                        transport.sockets.add(outgoing)
                    self.request.sendall(b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00')
                    self.request.settimeout(30)
                    outgoing.settimeout(30)
                    while True:
                        ready, _, _ = select.select([self.request, outgoing], [], [], 30)
                        if not ready:
                            break
                        for source in ready:
                            data = source.recv(65536)
                            if not data:
                                return
                            (outgoing if source is self.request else self.request).sendall(data)
                except (OSError, ValueError, subprocess.SubprocessError):
                    try:
                        self.request.sendall(b'\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00')
                    except OSError:
                        pass
                finally:
                    if outgoing:
                        with transport.guard:
                            transport.sockets.discard(outgoing)
                        outgoing.close()
                    self.server.slots.release()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False
            request_queue_size = 16

        self.server = Server(('127.0.0.1', 0), Handler)
        self.server.slots = threading.BoundedSemaphore(16)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'socks5h://127.0.0.1:%d' % self.server.server_address[1]
        return self

    def __exit__(self, *args):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
        with self.guard:
            for sock in self.sockets:
                sock.close()
            self.sockets.clear()
