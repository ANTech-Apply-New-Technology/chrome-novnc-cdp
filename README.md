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
| `CDP_AUTH_TOKEN` | private :9222 + :9221 | If set, require `Authorization: Bearer <token>` **or** `?token=<token>` on CDP and the API. Loopback is exempt. If unset, CDP/API are unauthenticated (a warning is logged). |
| `VNC_PASSWORD` | public noVNC | x11vnc password. If unset, x11vnc runs `-nopw` (warning logged). |
| `NOVNC_USER` / `NOVNC_PASSWORD` | public noVNC | If both set, websockify enforces HTTP Basic auth on the noVNC UI and WebSocket (`--web-auth`). |
| `VNC_RESOLUTION` | display | e.g. `1280x720` (default provided). |
| `VNC_SHARED` | display | `true`/`false` (default provided). |
| `TZ` | container | Defaults to `Europe/Stockholm`. |

Ports: `PORT` noVNC (public); `9221` API, `9222` CDP (private); `9223` Chromium debug (internal/loopback only).

CDP clients that can't set headers during discovery should put the token in the
URL, e.g. `cdp_url="http://<host>:9222/?token=<CDP_AUTH_TOKEN>"`.
