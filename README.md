# Chromium with NoVNC

|English|[日本語](README.ja.md)|

![screenshot](screenshot.png "screenshot")

- Run Chromium browser inside a Docker container and remotely access the containerized Chromium browser from your browser using noVNC
- Useful for running browsers on servers for web scraping and automation tasks using LLMs


## How to Run
```
docker compose up
```

Access http://localhost:9220


## Usage Example: Using with browser-use
```python
browser_session = BrowserSession(
    headless=False,
    window_size={"width": 1280, "height": 1024},
    viewport={"width": 1248, "height": 895},
    cdp_url="http://localhost:9222",
    keep_alive=True
)
```

## ANTech fork (Railway / SPR-clawbot)

This fork is deployed **twice** in the SPR-clawbot Railway project — once as
`chrome-openclaw` and once as `chrome-browseruse` — to serve as remote Chrome
(CDP + noVNC) for OpenClaw / browser-use automation. See `railway.toml`.

Changes vs upstream:
- CDP proxy (`:9222`) and API server (`:9221`) bind IPv6 `::` for Railway private networking.
- Optional bearer auth on CDP + API; the public noVNC UI is protected with VNC password + HTTP Basic auth.
- Locale defaults to `en_US.UTF-8`, timezone `Europe/Stockholm`, Chromium launched with Swedish language flags (Swedish booking sites).
- Chromium profile persisted at `/data/chromium` (mount a Railway volume at `/data`).

### Environment variables

| Var | Scope | Effect |
|---|---|---|
| `PORT` | auto (Railway) | noVNC web UI listen port (public). |
| `CDP_AUTH_TOKEN` | private :9222 + :9221 | If set, require `Authorization: Bearer <token>` **or** `?token=<token>` on CDP and the API. Loopback is exempt. |
| `CDP_ALLOWED_CLIENTS` | private :9222 + :9221 | Comma-separated hostnames (e.g. `spr-clawbot.railway.internal`); a request whose source IP resolves to one is authorized **without** a token. This is how OpenClaw (which can't carry the token through CDP discovery) is allowed. |
| `ALLOW_UNAUTHENTICATED_CDP` | private :9222 + :9221 | **Fail-closed by default.** If neither `CDP_AUTH_TOKEN` nor `CDP_ALLOWED_CLIENTS` is set, the CDP/API plane rejects all non-loopback requests **unless** this is explicitly `true`/`1`/`yes`. Set it only for local/dev. |
| `CDP_ADVERTISE_HOST` | private :9222 | `host:port` to advertise in the rewritten `webSocketDebuggerUrl` (e.g. `chrome-openclaw.railway.internal:9222`). Needed because some CDP clients probe `/json/version` without a usable Host header. |
| `CHROME_EXTRA_ARGS` | Chromium | Extra launch flags, word-split. Used to disable Site Isolation for cross-origin-iframe booking widgets: `--disable-features=IsolateOrigins,site-per-process --disable-site-isolation-trials`. Must be space-separated simple tokens (no embedded spaces). |
| `RESIDENTIAL_PROXY_URL` | Chromium egress | If set, page traffic egresses via a residential proxy: `http://user:pass@host:port`. Chromium is launched with `--proxy-server=http://127.0.0.1:8118` toward the loopback `proxy_chain.py` forwarder, which injects the credentials (Chromium's `--proxy-server` can't carry them). Unset = direct egress. Used by the bot-wall escalation lane (`chrome-residential`). |
| `RESIDENTIAL_DAILY_BYTES` | proxy_chain | Daily egress budget in bytes (default `2147483648` = 2 GiB). Past it the forwarder refuses new tunnels — caps metered residential cost. |
| `PROXY_CHAIN_IDLE_SECONDS` | proxy_chain | Per-direction idle timeout (default `120`). |
| `PROXY_CHAIN_PORT` | proxy_chain | Loopback forwarder port (default `8118`). |
| `VNC_PASSWORD` | public noVNC | x11vnc password. If unset, x11vnc runs `-nopw` (warning logged). |
| `NOVNC_USER` / `NOVNC_PASSWORD` | public noVNC | If both set, websockify enforces HTTP Basic auth on the noVNC UI and WebSocket (`--web-auth`). |
| `VNC_RESOLUTION` | display | e.g. `1280x720` (default provided). |
| `VNC_SHARED` | display | `true`/`false` (default provided). |
| `TZ` | container | Defaults to `Europe/Stockholm`. |

Ports: `PORT` noVNC (public); `9221` API, `9222` CDP (private); `9223` Chromium debug + `8118` residential proxy-chain (internal/loopback only).

**Residential egress lane:** set `RESIDENTIAL_PROXY_URL` to route all page traffic through a paid residential proxy (defeats Cloudflare-class bot walls that block datacenter IPs). The loopback `proxy_chain.py` injects credentials and enforces a daily byte budget; Chromium also gets `--webrtc-ip-handling-policy=disable_non_proxied_udp` to prevent UDP IP leaks. Credentials live only in memory, never in argv/logs.

CDP clients that can't set headers during discovery should put the token in the
URL, e.g. `cdp_url="http://<host>:9222/?token=<CDP_AUTH_TOKEN>"`.
