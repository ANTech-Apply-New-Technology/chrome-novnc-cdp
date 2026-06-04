import asyncio
import logging
import json
import os
from urllib.parse import urlparse, urlunparse, parse_qs, quote
from aiohttp import web, ClientSession, WSMsgType

# 設定 (settings)
# ANTech: bind on '::' (dual-stack) so Railway IPv6 private networking works.
# On Linux a single AF_INET6 socket with '::' also accepts IPv4-mapped clients.
LISTEN_HOST = '::'
LISTEN_PORT = 9222
TARGET_HOST = 'localhost'
TARGET_PORT = 9223

TARGET_BASE_URL = f'http://{TARGET_HOST}:{TARGET_PORT}'

# ANTech: optional bearer auth. If CDP_AUTH_TOKEN is set, every non-loopback
# request to :9222 must present the token (Authorization: Bearer <t> header or
# ?token=<t> query param). If unset, all requests are allowed (back-compat).
CDP_AUTH_TOKEN = os.environ.get('CDP_AUTH_TOKEN', '').strip()

# Loopback addresses are exempt from auth so local healthchecks / supervisor work.
LOOPBACK_ADDRS = ('127.0.0.1', '::1', '::ffff:127.0.0.1')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

if not CDP_AUTH_TOKEN:
    logging.warning(
        "CDP_AUTH_TOKEN is not set: CDP proxy on :%d is UNAUTHENTICATED. "
        "Set CDP_AUTH_TOKEN to require a bearer token.", LISTEN_PORT
    )


def _is_loopback(request):
    """Return True if the request originates from a loopback peer."""
    peer = request.transport.get_extra_info('peername') if request.transport else None
    if not peer:
        return False
    host = peer[0]
    return host in LOOPBACK_ADDRS


def _extract_token(request):
    """Pull a token from the Authorization header or the ?token= query param."""
    auth = request.headers.get('Authorization', '')
    if auth.lower().startswith('bearer '):
        return auth[7:].strip()
    # Fall back to query param: CDP discovery + the ws upgrade can't always set
    # custom headers, so ?token= is the reliable path for OpenClaw / browser-use.
    qs = parse_qs(urlparse(request.path_qs).query)
    if 'token' in qs and qs['token']:
        return qs['token'][0]
    return None


def _is_authorized(request):
    """Auth gate. Allows all when no token configured; exempts loopback."""
    if not CDP_AUTH_TOKEN:
        return True
    if _is_loopback(request):
        return True
    return _extract_token(request) == CDP_AUTH_TOKEN


def _strip_token_from_raw_query(query):
    """Remove only our own `token=` param from a raw query string, preserving
    everything else verbatim.

    NOTE: some CDP endpoints carry non key/value queries (e.g.
    /json/new?<url-encoded-target-url>), so we MUST NOT round-trip through
    parse_qs/urlencode — that would corrupt the target URL. We operate on the
    raw `&`-separated segments instead and only drop the exact `token` segment.
    """
    if not query:
        return query
    kept = [
        seg for seg in query.split('&')
        if seg and seg.split('=', 1)[0] != 'token'
    ]
    return '&'.join(kept)


def _forward_path_qs(request):
    """Path+query forwarded to the target, with our ?token= param removed.

    Chromium's CDP endpoints are path-routed and the token is only meaningful to
    this proxy, so we strip it before forwarding to keep the upstream URL clean.
    """
    parsed = urlparse(request.path_qs)
    if not parsed.query:
        return request.path_qs
    new_query = _strip_token_from_raw_query(parsed.query)
    return urlunparse(parsed._replace(query=new_query))


def _with_token_query(url_parts, token):
    """Append `token=<token>` to a parsed URL's raw query (preserving the rest).

    Used to carry the headerless discovery token onto a rewritten
    webSocketDebuggerUrl so the follow-up WS connect re-presents the same token.

    The token is percent-encoded so a value with reserved chars (+, &, %, # ...)
    round-trips losslessly: parse_qs on the WS reconnect decodes it back to the
    original before comparison against CDP_AUTH_TOKEN.
    """
    encoded = quote(token, safe='')
    existing = _strip_token_from_raw_query(url_parts.query)
    new_query = f"{existing}&token={encoded}" if existing else f"token={encoded}"
    return url_parts._replace(query=new_query)

async def proxy_http(request):
    """通常のHTTPリクエストをプロキシする"""
    # ANTech: bearer auth gate (loopback-exempt; no-op when token unset).
    if not _is_authorized(request):
        logging.warning("Rejected unauthenticated CDP HTTP request: %s", request.path)
        return web.json_response({"error": "unauthorized"}, status=401)

    original_host = request.headers.get('Host')
    # ANTech: strip our ?token= param before forwarding upstream.
    target_url = f"{TARGET_BASE_URL}{_forward_path_qs(request)}"

    # ターゲットへのリクエストヘッダーを準備
    forward_headers = dict(request.headers)
    forward_headers['Host'] = f'{TARGET_HOST}:{TARGET_PORT}'

    async with ClientSession() as session:
        try:
            async with session.request(
                request.method,
                target_url,
                headers=forward_headers,
                data=await request.read()
            ) as resp:
                content = await resp.read()
                # ヘッダーはミュータブルなdictにコピーする
                response_headers = dict(resp.headers)

                # /json/version, /json, /json/list の場合、レスポンスを書き換える
                if request.path in ('/json/version', '/json', '/json/list') and resp.status == 200 and original_host:
                    try:
                        data = json.loads(content)

                        # /json の場合はリスト内の各要素を処理
                        items = data if isinstance(data, list) else [data]

                        # ANTech: if the client used the headerless ?token= auth flow on
                        # discovery, carry that token onto the rewritten WS URL so the
                        # subsequent WebSocket connect passes the same auth gate.
                        client_token = _extract_token(request) if CDP_AUTH_TOKEN else None

                        for item in items:
                            if 'webSocketDebuggerUrl' in item:
                                ws_url_parts = urlparse(item['webSocketDebuggerUrl'])
                                new_ws_url_parts = ws_url_parts._replace(netloc=original_host)
                                if client_token:
                                    new_ws_url_parts = _with_token_query(new_ws_url_parts, client_token)
                                item['webSocketDebuggerUrl'] = urlunparse(new_ws_url_parts)
                                logging.info(f"Rewrote webSocketDebuggerUrl for host: {original_host}")

                        content = json.dumps(data).encode('utf-8')
                        response_headers['Content-Length'] = str(len(content))

                    except (json.JSONDecodeError, KeyError) as e:
                        logging.warning(f"Failed to modify {request.path} response: {e}")

                # aiohttpに再圧縮させないようにContent-Encodingを削除
                if 'Content-Encoding' in response_headers:
                    del response_headers['Content-Encoding']

                # Transfer-Encodingヘッダも削除
                if 'Transfer-Encoding' in response_headers:
                    del response_headers['Transfer-Encoding']

                response = web.Response(
                    body=content,
                    status=resp.status,
                    headers=response_headers
                )
                return response
        except Exception as e:
            logging.error(f"Error proxying HTTP request: {e}")
            return web.Response(status=502, text="Bad Gateway")


async def proxy_websocket(request):
    """WebSocket接続をプロキシする"""
    # ANTech: bearer auth gate for the WS upgrade. Reject before the handshake
    # so unauthenticated clients never get an open socket. close code 4401.
    if not _is_authorized(request):
        logging.warning("Rejected unauthenticated CDP WebSocket upgrade: %s", request.path)
        ws_reject = web.WebSocketResponse()
        await ws_reject.prepare(request)
        await ws_reject.close(code=4401, message=b'unauthorized')
        return ws_reject

    # クライアントからのWebSocket接続を準備
    # heartbeat=30でkeep-aliveを設定
    # max_msg_size=200MBでbrowser-useの大きなDOMスナップショットに対応
    ws_server = web.WebSocketResponse(heartbeat=30, max_msg_size=200*1024*1024)
    await ws_server.prepare(request)

    # ターゲットへのWebSocket接続を準備
    # ANTech: strip our ?token= param before forwarding upstream.
    target_url = f"ws://{TARGET_HOST}:{TARGET_PORT}{_forward_path_qs(request)}"
    headers = dict(request.headers)
    headers['Host'] = f'{TARGET_HOST}:{TARGET_PORT}'

    async with ClientSession() as session:
        try:
            # heartbeat=30でkeep-aliveを設定
            # max_msg_size=200MBでbrowser-useの大きなDOMスナップショットに対応
            async with session.ws_connect(target_url, headers=headers, heartbeat=30, max_msg_size=200*1024*1024) as ws_client:
                logging.info("WebSocket connection established.")

                # 接続状態を追跡するためのイベント
                shutdown_event = asyncio.Event()

                async def forward_to_client():
                    """ターゲット -> プロキシ -> クライアント"""
                    msg_from_target = 0
                    try:
                        async for msg in ws_client:
                            if shutdown_event.is_set():
                                logging.debug("Forward to client: shutdown_event is set, breaking")
                                break
                            if msg.type == WSMsgType.TEXT:
                                msg_from_target += 1
                                # Log errors from Chrome
                                if '"error"' in msg.data:
                                    logging.warning(f"Forward to client [{msg_from_target}] ERROR: {msg.data[:500]}")
                                if not ws_server.closed:
                                    await ws_server.send_str(msg.data)
                            elif msg.type == WSMsgType.BINARY:
                                if not ws_server.closed:
                                    await ws_server.send_bytes(msg.data)
                            elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                                logging.info(f"Forward to client: received CLOSED/ERROR message type={msg.type}")
                                break
                            elif msg.type == WSMsgType.PING:
                                logging.debug("Forward to client: received PING")
                            elif msg.type == WSMsgType.PONG:
                                logging.debug("Forward to client: received PONG")
                    except Exception as e:
                        logging.warning(f"Forward to client exception: {type(e).__name__}: {e}")
                    finally:
                        shutdown_event.set()
                        logging.info(f"Forward to client finished. ws_client.closed={ws_client.closed}, ws_server.closed={ws_server.closed}")


                msg_count = [0, 0]  # [to_target, from_target]

                async def forward_to_target():
                    """クライアント -> プロキシ -> ターゲット"""
                    try:
                        async for msg in ws_server:
                            if shutdown_event.is_set():
                                logging.debug("Forward to target: shutdown_event is set, breaking")
                                break
                            if msg.type == WSMsgType.TEXT:
                                msg_count[0] += 1
                                if msg_count[0] <= 10:  # First 10 messages
                                    logging.debug(f"Forward to target [{msg_count[0]}]: {msg.data[:200]}...")
                                if not ws_client.closed:
                                    await ws_client.send_str(msg.data)
                            elif msg.type == WSMsgType.BINARY:
                                if not ws_client.closed:
                                    await ws_client.send_bytes(msg.data)
                            elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                                logging.info(f"Forward to target: received CLOSED/ERROR message type={msg.type}")
                                break
                            elif msg.type == WSMsgType.CLOSE:
                                logging.info(f"Forward to target: received CLOSE message, close_code={getattr(msg, 'extra', None)}")
                                break
                    except Exception as e:
                        logging.warning(f"Forward to target exception: {type(e).__name__}: {e}")
                    finally:
                        shutdown_event.set()
                        logging.info(f"Forward to target finished. ws_client.closed={ws_client.closed}, ws_server.closed={ws_server.closed}")

                # 双方向のメッセージ転送を並行して実行
                # どちらかが終了したら両方を終了させる
                tasks = [
                    asyncio.create_task(forward_to_client()),
                    asyncio.create_task(forward_to_target())
                ]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

                # 残りのタスクをキャンセル
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        except Exception as e:
            logging.error(f"Error proxying WebSocket: {e}")
        finally:
            if not ws_server.closed:
                await ws_server.close()
            logging.info("WebSocket connection closed.")

    return ws_server


async def handle_request(request):
    """HTTPとWebSocketのリクエストを振り分ける"""
    # WebSocketへのアップグレードリクエストか判定
    if 'Upgrade' in request.headers and request.headers.get('Upgrade', '').lower() == 'websocket':
        return await proxy_websocket(request)
    else:
        return await proxy_http(request)

async def main():
    app = web.Application()
    app.router.add_route('*', '/{path:.*}', handle_request)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, LISTEN_HOST, LISTEN_PORT)
    await site.start()
    logging.info(f"CDP reverse proxy started on http://{LISTEN_HOST}:{LISTEN_PORT}")
    # サーバーを永続的に実行
    await asyncio.Event().wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Proxy server shutting down.")
