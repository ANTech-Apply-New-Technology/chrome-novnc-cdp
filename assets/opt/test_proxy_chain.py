"""Unit tests for proxy_chain.py — parse_upstream and inject_proxy_auth.

Run standalone (no extra dependencies):
    python3 -m unittest assets/opt/test_proxy_chain.py

Or from repo root:
    python3 -m unittest discover -s assets/opt -p 'test_*.py'

These tests exercise pure logic only; no sockets, no event loop, no env vars.
"""

import base64
import sys
import os
import unittest

# Allow running from any cwd by inserting the module's directory on sys.path.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from proxy_chain import parse_upstream, inject_proxy_auth


def _b64(s):
    return base64.b64encode(s.encode()).decode("ascii")


class TestParseUpstream(unittest.TestCase):

    # --- happy paths ---

    def test_full_url_with_credentials(self):
        host, port, auth = parse_upstream("http://user:pass@gate.provider.com:8080")
        self.assertEqual(host, "gate.provider.com")
        self.assertEqual(port, 8080)
        self.assertEqual(auth, f"Basic {_b64('user:pass')}")

    def test_url_without_credentials(self):
        host, port, auth = parse_upstream("http://gate.provider.com:8080")
        self.assertEqual(host, "gate.provider.com")
        self.assertEqual(port, 8080)
        self.assertIsNone(auth)

    def test_bare_host_colon_port_no_scheme(self):
        # bare host:port is prepended with http:// by the parser
        host, port, auth = parse_upstream("gate.provider.com:8080")
        self.assertEqual(host, "gate.provider.com")
        self.assertEqual(port, 8080)
        self.assertIsNone(auth)

    def test_credentials_with_empty_password(self):
        host, port, auth = parse_upstream("http://user:@gate.provider.com:8080")
        self.assertEqual(host, "gate.provider.com")
        self.assertEqual(port, 8080)
        self.assertEqual(auth, f"Basic {_b64('user:')}")

    def test_credentials_with_special_chars_in_password(self):
        # urlsplit does NOT decode percent-encoding in the password field;
        # the raw %40 stays as-is in the credential string passed to base64.
        host, port, auth = parse_upstream("http://user:p%40ss@gate.provider.com:1234")
        self.assertEqual(host, "gate.provider.com")
        self.assertEqual(port, 1234)
        self.assertEqual(auth, f"Basic {_b64('user:p%40ss')}")

    # --- rejection cases ---

    def test_https_scheme_raises(self):
        with self.assertRaises(ValueError) as ctx:
            parse_upstream("https://user:pass@gate.provider.com:8080")
        self.assertIn("unsupported upstream scheme", str(ctx.exception))

    def test_missing_port_raises(self):
        with self.assertRaises(ValueError) as ctx:
            parse_upstream("http://gate.provider.com")
        self.assertIn("must be http://", str(ctx.exception))

    def test_bare_host_no_port_raises(self):
        with self.assertRaises(ValueError) as ctx:
            parse_upstream("gate.provider.com")
        self.assertIn("must be http://", str(ctx.exception))

    def test_empty_string_raises(self):
        with self.assertRaises(ValueError):
            parse_upstream("")

    def test_socks5_scheme_raises(self):
        with self.assertRaises(ValueError) as ctx:
            parse_upstream("socks5://user:pass@gate.provider.com:1080")
        self.assertIn("unsupported upstream scheme", str(ctx.exception))


class TestInjectProxyAuth(unittest.TestCase):

    def _make_connect_head(self, extra_headers=None):
        lines = [b"CONNECT example.com:443 HTTP/1.1", b"Host: example.com:443"]
        if extra_headers:
            lines.extend(extra_headers)
        lines += [b"", b""]
        return b"\r\n".join(lines)

    # --- inserts auth when none present ---

    def test_inserts_proxy_authorization_header(self):
        head = self._make_connect_head()
        auth = "Basic " + _b64("user:pass")
        result = inject_proxy_auth(head, auth)
        self.assertIn(b"Proxy-Authorization: Basic ", result)

    def test_inserted_auth_is_on_second_line(self):
        head = self._make_connect_head()
        auth = "Basic " + _b64("user:pass")
        result = inject_proxy_auth(head, auth)
        lines = result.split(b"\r\n")
        self.assertEqual(lines[0], b"CONNECT example.com:443 HTTP/1.1")
        self.assertTrue(lines[1].lower().startswith(b"proxy-authorization:"))

    # --- replaces existing Proxy-Authorization ---

    def test_replaces_existing_proxy_authorization(self):
        old_auth = "Basic " + _b64("old:cred")
        head = self._make_connect_head(
            extra_headers=[f"Proxy-Authorization: {old_auth}".encode()]
        )
        new_auth = "Basic " + _b64("new:cred")
        result = inject_proxy_auth(head, new_auth)
        self.assertNotIn(old_auth.encode(), result)
        self.assertIn(new_auth.encode(), result)

    def test_replaces_existing_proxy_authorization_case_insensitive(self):
        old_auth = "Basic " + _b64("old:cred")
        head = self._make_connect_head(
            extra_headers=[f"PROXY-AUTHORIZATION: {old_auth}".encode()]
        )
        new_auth = "Basic " + _b64("new:cred")
        result = inject_proxy_auth(head, new_auth)
        # Only one Proxy-Authorization line should remain
        pa_count = sum(
            1 for l in result.split(b"\r\n")
            if l.lower().startswith(b"proxy-authorization:")
        )
        self.assertEqual(pa_count, 1)

    # --- no-auth case: strips existing, adds nothing ---

    def test_strips_proxy_authorization_when_auth_is_none(self):
        existing = "Basic " + _b64("user:pass")
        head = self._make_connect_head(
            extra_headers=[f"Proxy-Authorization: {existing}".encode()]
        )
        result = inject_proxy_auth(head, None)
        self.assertNotIn(b"Proxy-Authorization", result)
        self.assertNotIn(b"proxy-authorization", result.lower())

    def test_no_auth_and_none_passed_leaves_no_header(self):
        head = self._make_connect_head()
        result = inject_proxy_auth(head, None)
        self.assertNotIn(b"proxy-authorization", result.lower())

    # --- other headers survive ---

    def test_other_headers_are_preserved(self):
        head = self._make_connect_head(extra_headers=[b"User-Agent: TestAgent/1.0"])
        auth = "Basic " + _b64("u:p")
        result = inject_proxy_auth(head, auth)
        self.assertIn(b"User-Agent: TestAgent/1.0", result)

    def test_request_line_is_preserved(self):
        head = self._make_connect_head()
        auth = "Basic " + _b64("u:p")
        result = inject_proxy_auth(head, auth)
        self.assertTrue(result.startswith(b"CONNECT example.com:443 HTTP/1.1"))


if __name__ == "__main__":
    unittest.main()
