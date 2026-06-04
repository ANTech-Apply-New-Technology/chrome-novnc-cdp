from http.server import BaseHTTPRequestHandler, HTTPServer
import subprocess
import json
import os
import socket
import time
import ipaddress

# ANTech: auth shared with the CDP proxy. POST /restart-chromium is authorized
# when the request comes from loopback, presents the bearer token (header or
# ?token=), OR comes from an IP in CDP_ALLOWED_CLIENTS (resolved sibling hosts).
# GET /health is always unauthenticated.
CDP_AUTH_TOKEN = os.environ.get('CDP_AUTH_TOKEN', '').strip()
CDP_ALLOWED_CLIENTS = [
    h.strip() for h in os.environ.get('CDP_ALLOWED_CLIENTS', '').split(',') if h.strip()
]
LOOPBACK_ADDRS = ('127.0.0.1', '::1', '::ffff:127.0.0.1')
_ALLOWED_TTL_SEC = 30.0
_allowed_cache = {'ts': -1e9, 'ips': frozenset()}


def _norm_ip(addr):
    if not addr:
        return addr
    try:
        return str(ipaddress.ip_address(addr.split('%', 1)[0]))
    except ValueError:
        return addr


def _resolve_allowed_ips():
    if not CDP_ALLOWED_CLIENTS:
        return frozenset()
    now = time.monotonic()
    if now - _allowed_cache['ts'] < _ALLOWED_TTL_SEC:
        return _allowed_cache['ips']
    ips = set()
    for host in CDP_ALLOWED_CLIENTS:
        try:
            for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP):
                ips.add(_norm_ip(info[4][0]))
        except socket.gaierror:
            pass
    if ips:
        _allowed_cache['ips'] = frozenset(ips)
    _allowed_cache['ts'] = now
    return _allowed_cache['ips']


class RequestHandler(BaseHTTPRequestHandler):
    def _peer_ip(self):
        return _norm_ip(self.client_address[0]) if self.client_address else None

    def _is_loopback(self):
        return self._peer_ip() in LOOPBACK_ADDRS

    def _extract_token(self):
        auth = self.headers.get('Authorization', '')
        if auth.lower().startswith('bearer '):
            return auth[7:].strip()
        # Fall back to ?token= query param.
        if '?' in self.path:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            if qs.get('token'):
                return qs['token'][0]
        return None

    def _is_authorized(self):
        if not CDP_AUTH_TOKEN and not CDP_ALLOWED_CLIENTS:
            return True
        if self._is_loopback():
            return True
        if CDP_AUTH_TOKEN and self._extract_token() == CDP_AUTH_TOKEN:
            return True
        if CDP_ALLOWED_CLIENTS:
            ip = self._peer_ip()
            if ip and ip in _resolve_allowed_ips():
                return True
        return False

    def _send_json(self, status, payload):
        self.send_response(status)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def _path_only(self):
        return self.path.split('?', 1)[0]

    def do_POST(self):
        if self._path_only() == '/restart-chromium':
            # ANTech: bearer auth gate (loopback-exempt; no-op when token unset).
            if not self._is_authorized():
                self._send_json(401, {"message": "unauthorized"})
                return
            try:
                # Supervisorを使ってChromiumを再起動
                result = subprocess.run(['supervisorctl', '-c', '/config/supervisord.conf', 'restart', 'Chromium'], check=True, capture_output=True, text=True)
                self._send_json(200, {"message": "Chromium restart initiated.", "details": result.stdout})
            except subprocess.CalledProcessError as e:
                self._send_json(500, {"message": "Failed to restart Chromium.", "error": e.stderr})
            except FileNotFoundError:
                self._send_json(500, {"message": "supervisorctl command not found."})
        else:
            self._send_json(404, {"message": "Endpoint not found. Use POST /restart-chromium."})

    def do_GET(self):
        # ANTech: unauthenticated health endpoint for Railway healthchecks.
        if self._path_only() == '/health':
            self._send_json(200, {"ok": True})
            return
        self.send_response(405)
        self.send_header('Content-type', 'application/json')
        self.send_header('Allow', 'POST')
        self.end_headers()
        self.wfile.write(json.dumps({"message": "Method Not Allowed. Use POST."}).encode())


# ANTech: bind on IPv6 '::' (dual-stack) for Railway private networking.
# On Linux this also accepts IPv4-mapped clients.
class HTTPServerV6(HTTPServer):
    address_family = socket.AF_INET6


def run(server_class=HTTPServerV6, handler_class=RequestHandler, port=9221):
    server_address = ('::', port)
    httpd = server_class(server_address, handler_class)
    print(f'Starting httpd on [::]:{port}...')
    httpd.serve_forever()


if __name__ == '__main__':
    run()
