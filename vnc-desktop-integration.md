> **DEPRECATED:** This approach has been deprecated in favor of only displaying the image because the macOS desktop cannot be displayed in a browser tab due to proprietary software considerations. Therefore, it is not possible to replicate the look and feel of macOS inside a browser. We should not pursue this any further.

# VNC Desktop Integration Plan

Add a full desktop view alongside the existing tmux terminal, enabling the voice agent to launch and observe visual programs (GIFs, browsers, GUI apps) in real time.

---

## Overview of Approach

Replace the ttyd-only right panel with a split or tabbed view:
- **Terminal tab** — existing ttyd iframe (unchanged)
- **Desktop tab** — noVNC iframe streaming a virtual X display

The agent gets two new tools: `launch_visual` (open a GUI app on the virtual display) and `capture_screenshot` (snapshot the desktop as a PNG for vision-based reasoning).

---

## Technology Stack

| Layer | Choice | Why |
|---|---|---|
| Virtual display | **Xvfb** | Headless X server, zero hardware needed |
| VNC server | **x11vnc** | Attaches to an Xvfb display, widely supported |
| Browser client | **noVNC** | Pure HTML5/WebSocket VNC client, no plugin |
| WM (optional) | **openbox** or **fluxbox** | Lightweight window manager so apps tile properly |
| Screenshot | `scrot` or `PIL/Xlib` | Grab a frame from the Xvfb display |

---

## Step-by-Step Changes

### 1. System Dependencies

```bash
brew install xvfb x11vnc  # macOS via XQuartz; on Linux: apt install xvfb x11vnc
# noVNC is a JS library — no system install needed
```

For macOS, Xvfb is not native. Options:
- Run the desktop layer inside a **Docker container** (Linux) and proxy noVNC through FastAPI.
- Use **XQuartz** + x11vnc to share the real macOS display (simpler for dev, not headless).

The Docker path is recommended for production; XQuartz works for local demos.

### 2. Virtual Display Lifecycle (`bot.py`)

Mirror the existing ttyd spawn/teardown pattern:

```python
import subprocess, os

DISPLAY_NUM = ":99"
_xvfb_proc = None
_x11vnc_proc = None

def start_virtual_display():
    global _xvfb_proc, _x11vnc_proc
    _xvfb_proc = subprocess.Popen(
        ["Xvfb", DISPLAY_NUM, "-screen", "0", "1920x1080x24"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Optional: launch a window manager
    subprocess.Popen(
        ["openbox", "--display", DISPLAY_NUM],
        env={**os.environ, "DISPLAY": DISPLAY_NUM},
    )
    _x11vnc_proc = subprocess.Popen(
        ["x11vnc", "-display", DISPLAY_NUM, "-forever", "-nopw",
         "-listen", "localhost", "-rfbport", "5900"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

def stop_virtual_display():
    for p in (_x11vnc_proc, _xvfb_proc):
        if p:
            p.terminate()
```

Call `start_virtual_display()` at bot startup and `stop_virtual_display()` on shutdown, alongside the existing ttyd management.

### 3. noVNC WebSocket Proxy (`bot.py`)

x11vnc speaks the RFB protocol over a raw TCP socket on port 5900. noVNC needs a WebSocket bridge. Options:

**Option A — websockify sidecar** (simplest):
```bash
pip install websockify
websockify --web /path/to/novnc 6080 localhost:5900
```
Then the browser connects directly to `:6080`.

**Option B — FastAPI WebSocket proxy** (keeps everything on one port, consistent with the ttyd proxy pattern already in `bot.py`):

```python
@app.websocket("/vnc/ws")
async def vnc_ws_proxy(websocket: WebSocket):
    await websocket.accept()
    reader, writer = await asyncio.open_connection("127.0.0.1", 5900)
    # bidirectional relay: websocket <-> TCP socket
    async def ws_to_tcp():
        async for msg in websocket.iter_bytes():
            writer.write(msg)
            await writer.drain()
    async def tcp_to_ws():
        while True:
            data = await reader.read(4096)
            if not data:
                break
            await websocket.send_bytes(data)
    await asyncio.gather(ws_to_tcp(), tcp_to_ws())
```

Serve the noVNC static files (download from [github.com/novnc/noVNC](https://github.com/novnc/noVNC)) under `/vnc/`:

```python
app.mount("/vnc", StaticFiles(directory="novnc"), name="novnc")
```

### 4. `cockpit.html` — Add Desktop Panel

Add a tab strip to the right panel:

```html
<!-- Tab bar -->
<div id="view-tabs">
  <button onclick="showTab('terminal')">Terminal</button>
  <button onclick="showTab('desktop')">Desktop</button>
</div>

<!-- Terminal pane (existing) -->
<div id="pane-terminal">
  <iframe src="/terminal/" ...></iframe>
</div>

<!-- Desktop pane (new) -->
<div id="pane-desktop" style="display:none">
  <iframe id="vnc-frame"
    src="/vnc/vnc.html?host=localhost&port=443&path=vnc/ws&autoconnect=true&resize=scale"
    style="width:100%;height:100%;border:none">
  </iframe>
</div>
```

```js
function showTab(name) {
  document.getElementById('pane-terminal').style.display = name === 'terminal' ? '' : 'none';
  document.getElementById('pane-desktop').style.display  = name === 'desktop'  ? '' : 'none';
}
```

### 5. New LLM Tools in `bot.py`

**`launch_visual`** — open any GUI app on the virtual display:

```python
async def launch_visual(program: str, args: list[str] = []) -> str:
    """Launch a GUI program on the virtual display (e.g. 'eog', 'feh', 'firefox')."""
    env = {**os.environ, "DISPLAY": DISPLAY_NUM}
    subprocess.Popen([program, *args], env=env)
    return f"Launched {program}"
```

**`capture_screenshot`** — grab a frame for vision reasoning:

```python
async def capture_screenshot() -> str:
    """Take a screenshot of the virtual desktop. Returns a base64 PNG."""
    import subprocess, base64
    result = subprocess.run(
        ["scrot", "-", "--display", DISPLAY_NUM],  # outputs PNG to stdout
        capture_output=True,
    )
    return base64.b64encode(result.stdout).decode()
```

Register both tools in the `OpenAILLMService` tools list alongside the existing tmux tools.

### 6. Example: Playing a GIF

With the above in place, the voice command "show me the dancing parrot GIF" becomes:

1. Agent calls `run_command` → `wget -O /tmp/parrot.gif <url>`
2. Agent calls `launch_visual("eog", ["/tmp/parrot.gif"])` — Eye of GNOME opens on the virtual display
3. User clicks the **Desktop** tab in the cockpit UI — sees the GIF playing full-screen via noVNC

---

## Docker Path (Recommended for Portability)

Wrap the virtual display stack in a `Dockerfile`:

```dockerfile
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y \
    xvfb x11vnc openbox scrot eog feh firefox \
    python3 python3-pip websockify
# copy server/ in, install uv deps, etc.
CMD ["bash", "entrypoint.sh"]  # starts Xvfb, x11vnc, websockify, then uv run bot.py
```

The FastAPI server and all display processes run in the same container; noVNC WebSocket is proxied through FastAPI as above.

---

## Summary of File Changes

| File | Change |
|---|---|
| `bot.py` | Add `start_virtual_display`, `stop_virtual_display`, `/vnc/ws` WebSocket proxy route, `launch_visual` and `capture_screenshot` tool definitions |
| `cockpit.html` | Add Desktop tab + noVNC iframe; tab-switching JS |
| `requirements` / `pyproject.toml` | No new Python deps if using websockify as sidecar; add `websockets` if doing the FastAPI proxy |
| `novnc/` (new dir) | noVNC static assets cloned from github.com/novnc/noVNC |
| `Dockerfile` (new, optional) | Containerised Linux environment with Xvfb + x11vnc |

---

## Gemini

### Comments and Insights

1.  **Interactive Control**: While `launch_visual` allows opening apps, adding a tool like `send_input` (using `xdotool`) would enable the agent to interact with the GUI (clicking buttons, typing into forms). This transforms the agent from a viewer into an operator.
2.  **Performance Optimization**: The FastAPI WebSocket proxy is convenient but may introduce latency. For a smoother experience, consider using **KasmVNC**. It integrates the WebSocket server directly into the VNC server, supports modern codecs like H.264, and often provides better performance than the `x11vnc` + `websockify` stack.
3.  **Vision-Language Model (VLM) Integration**: When using `capture_screenshot`, ensure the agent has context about the desktop's current state. It might be useful to have a persistent "state" for the desktop (e.g., which windows are open and their positions) to help the VLM reason about where to click.
4.  **Security Hardening**: If this moves beyond a local demo, the `-nopw` flag in `x11vnc` should be replaced with a password or, better yet, the WebSocket endpoint should be protected by the same authentication mechanism as the rest of the Cockpit UI.
5.  **Resource Management**: Xvfb and GUI apps (especially browsers like Firefox) can be memory-intensive. Monitoring the health of these processes and implementing a robust "kill all" mechanism on bot teardown is crucial to prevent zombie processes from eating up server resources.
6.  **Dynamic Display Assignment**: For multi-agent scalability, instead of hardcoding `:99` and port `5900`, implement a registry to assign unique display numbers and ports to each bot instance.
