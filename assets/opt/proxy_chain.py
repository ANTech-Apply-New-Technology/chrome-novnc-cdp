"""Loopback-only HTTP proxy that chains to an authenticated upstream proxy.

ANTech: Chromium's --proxy-server flag cannot carry credentials (user:pass in
the URL is ignored), but residential proxy providers require them. This
forwarder bridges the gap: Chromium points at http://127.0.0.1:8118 (no auth),
and this process injects Proxy-Authorization toward the upstream residential
gateway from RESIDENTIAL_PROXY_URL (http://user:pass@host:port).

SECURITY: binds 127.0.0.1 ONLY. This process must never be reachable from
outside the container — a reachable instance would be an open relay into a
paid residential proxy. The listen host is intentionally not configurable.
Credentials are read from the environment and never logged.
"""

import asyncio
import base64
import logging
import os
import sys
import time
from urllib.parse import urlsplit

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("PROXY_CHAIN_PORT", "8118"))

# StreamReader buffer ceiling for an HTTP head. asyncio defaults to 64 KiB and
# raises LimitOverrunError if the \r\n\r\n separator isn't seen within it — the
# exact bot-wall pages this lane targets carry big cf-* / Set-Cookie headers, so
# we raise it to 256 KiB. Applied to BOTH the listener and upstream connections.
STREAM_LIMIT = 256 * 1024
# Idle timeout per direction: a stuck/slow-loris tunnel must not hold a metered
# residential connection (and burn data) forever. Treated as EOF.
IDLE_TIMEOUT = float(os.environ.get("PROXY_CHAIN_IDLE_SECONDS", "120"))
# Hard daily egress budget (bytes) independent of agent behavior — defense in
# depth against a prompt-injected agent driving volume through paid residential
# egress. New tunnels are refused past the budget; in-flight ones finish.
DAILY_BUDGET_BYTES = int(os.environ.get("RESIDENTIAL_DAILY_BYTES", str(2 * 1024 ** 3)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s proxy-chain %(levelname)s %(message)s")
log = logging.getLogger("proxy-chain")

_LOOPBACK_PEERS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
_usage = {"day": None, "bytes": 0}


def _budget_add(n):
    """Accumulate egress bytes with UTC-day rollover."""
    today = time.gmtime().tm_yday
    if _usage["day"] != today:
        _usage["day"] = today
        _usage["bytes"] = 0
    _usage["bytes"] += n


def _budget_exhausted():
    today = time.gmtime().tm_yday
    if _usage["day"] != today:
        return False
    return _usage["bytes"] >= DAILY_BUDGET_BYTES


def parse_upstream(raw):
    """Parse RESIDENTIAL_PROXY_URL into (host, port, basic_auth_header|None)."""
    u = urlsplit(raw if "://" in raw else f"http://{raw}")
    if u.scheme not in ("http", ""):
        raise ValueError(f"unsupported upstream scheme: {u.scheme} (only http proxies)")
    if not u.hostname or not u.port:
        raise ValueError("upstream must be http://[user:pass@]host:port")
    auth = None
    if u.username is not None:
        cred = f"{u.username}:{u.password or ''}"
        auth = "Basic " + base64.b64encode(cred.encode("utf-8")).decode("ascii")
    return u.hostname, u.port, auth


async def read_http_head(reader):
    """Read request/response head up to the blank line. Returns raw bytes.

    LimitOverrunError (head exceeds STREAM_LIMIT without a separator) is
    re-raised as ValueError so it lands in handle()'s graceful clause instead
    of the bare except that logs a full stack trace.
    """
    try:
        return await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError:
        raise ValueError("HTTP head exceeds buffer limit")


async def pipe(reader, writer, charge=False):
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            if charge:
                _budget_add(len(chunk))
            writer.write(chunk)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    finally:
        # Half-close only: signal EOF to the peer but DON'T hard-close — the
        # sibling direction may still be draining the response (closing the
        # write side early truncates it). handle()'s finally does the hard
        # close once both directions have finished.
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except Exception:
            pass


def inject_proxy_auth(head, auth):
    """Insert/replace Proxy-Authorization in a raw request head."""
    lines = head.split(b"\r\n")
    kept = [l for l in lines if not l.lower().startswith(b"proxy-authorization:")]
    if auth:
        # After the request line, before the trailing blank pair.
        kept.insert(1, b"Proxy-Authorization: " + auth.encode("ascii"))
    return b"\r\n".join(kept)


class Chain:
    def __init__(self, host, port, auth):
        self.host = host
        self.port = port
        self.auth = auth

    async def handle(self, client_reader, client_writer):
        up_writer = None
        try:
            # Belt-and-suspenders: this forwarder is loopback-only by bind, but
            # refuse any non-loopback peer too, so a future bind-widening fails
            # closed (no authenticated relay into the paid residential proxy)
            # rather than open.
            peer = client_writer.get_extra_info("peername")
            if peer and peer[0] not in _LOOPBACK_PEERS:
                log.error("refusing non-loopback peer %s (proxy_chain is loopback-only)", peer[0])
                return

            # Hard daily egress cap, below the agent's discretion.
            if _budget_exhausted():
                log.warning("daily residential egress budget exhausted — refusing new tunnel")
                try:
                    client_writer.write(b"HTTP/1.1 503 Residential budget exhausted\r\n\r\n")
                    await client_writer.drain()
                except Exception:
                    pass
                return

            head = await read_http_head(client_reader)
            request_line = head.split(b"\r\n", 1)[0]
            method = request_line.split(b" ", 1)[0].upper()

            up_reader, up_writer = await asyncio.open_connection(
                self.host, self.port, limit=STREAM_LIMIT
            )

            if method == b"CONNECT":
                # Replay the CONNECT toward the upstream proxy with credentials,
                # relay its verdict verbatim, then go full-duplex blind pipe.
                up_writer.write(inject_proxy_auth(head, self.auth))
                await up_writer.drain()
                resp_head = await read_http_head(up_reader)
                client_writer.write(resp_head)
                await client_writer.drain()
                if b" 200" not in resp_head.split(b"\r\n", 1)[0]:
                    status = resp_head.split(b"\r\n", 1)[0].decode("latin1", "replace")
                    log.warning("upstream refused CONNECT: %s", status)
                    return
            else:
                # Absolute-form plain HTTP (rare; nearly all traffic is CONNECT).
                up_writer.write(inject_proxy_auth(head, self.auth))
                await up_writer.drain()

            # Charge only the download direction (upstream→client) toward the
            # budget — that's the bulk of residential egress and avoids
            # double-counting.
            await asyncio.gather(
                pipe(client_reader, up_writer),
                pipe(up_reader, client_writer, charge=True),
            )
        except (asyncio.IncompleteReadError, ConnectionResetError, ValueError) as err:
            log.debug("session ended: %r", err)
        except Exception:
            log.exception("unexpected proxy-chain error")
        finally:
            for w in (client_writer, up_writer):
                if w is not None:
                    try:
                        w.close()
                    except Exception:
                        pass


async def main():
    raw = os.environ.get("RESIDENTIAL_PROXY_URL", "").strip()
    if not raw:
        # Supervisor starts us unconditionally; without an upstream we just idle
        # so the program stays green and Chromium (started without
        # --proxy-server in this case) is unaffected.
        log.info("RESIDENTIAL_PROXY_URL not set — idling (no proxying)")
        await asyncio.Event().wait()
        return

    try:
        host, port, auth = parse_upstream(raw)
    except ValueError as err:
        # Misconfigured RESIDENTIAL_PROXY_URL: idle instead of crash-looping
        # (autorestart). The service stays up (noVNC reachable for debugging),
        # 8118 is NOT bound, so Chromium's --proxy-server fails fast and the
        # agent's bot-wall escalation sees the residential lane is unavailable.
        log.error("invalid RESIDENTIAL_PROXY_URL (%s) — idling, no proxying", err)
        await asyncio.Event().wait()
        return

    log.info(
        "chaining 127.0.0.1:%d -> %s:%d (auth: %s, idle=%.0fs, budget=%d bytes/day)",
        LISTEN_PORT, host, port, "yes" if auth else "no", IDLE_TIMEOUT, DAILY_BUDGET_BYTES,
    )
    chain = Chain(host, port, auth)
    server = await asyncio.start_server(
        chain.handle, LISTEN_HOST, LISTEN_PORT, limit=STREAM_LIMIT
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
