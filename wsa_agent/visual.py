"""Zero-focus headless CDP visual capture, Set-of-Mark grounding, contact-sheet compositing, and pixel diffing."""
from __future__ import annotations

import base64
import functools
import http.server
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Keep baselines separate for each workspace, target and capture configuration.
_PREVIOUS_CAPTURES_LOCK = threading.Lock()
_PREVIOUS_CAPTURES: Dict[tuple, bytes] = {}
_MAX_PREVIOUS_CACHE = 16

DEFAULT_VIEWPORTS: List[Dict[str, Any]] = [
    {"name": "desktop", "width": 1440, "height": 900, "mobile": False},
    {"name": "mobile", "width": 390, "height": 844, "mobile": True},
]

_AUDIT_AND_SOM_JS = r"""(function(enableAnnotate, maxMarks, expectedVw) {
  try {
    var existing = document.getElementById('__wsa_som_overlay__');
    if (existing) existing.remove();
  } catch (e) {}

  var vw = window.innerWidth || document.documentElement.clientWidth || 0;
  var vh = window.innerHeight || document.documentElement.clientHeight || 0;
  var baseVw = (expectedVw && expectedVw > 0) ? Math.min(vw || expectedVw, expectedVw) : vw;
  var sw = Math.max(document.documentElement.scrollWidth || 0, (document.body && document.body.scrollWidth) || 0, vw);
  var sh = Math.max(document.documentElement.scrollHeight || 0, (document.body && document.body.scrollHeight) || 0);

  var brokenImages = [];
  document.querySelectorAll('img').forEach(function(img) {
    if (img.complete && img.naturalWidth === 0) {
      brokenImages.push(String(img.getAttribute('src') || img.alt || '<img>').slice(0, 120));
    }
  });

  var canvases = [];
  document.querySelectorAll('canvas').forEach(function(c, idx) {
    var r = c.getBoundingClientRect();
    canvases.push({
      selector: c.id ? ('canvas#' + c.id) : ('canvas:nth(' + idx + ')'),
      width: Math.round(r.width),
      height: Math.round(r.height),
      buffer_w: c.width || 0,
      buffer_h: c.height || 0,
      visible: r.width > 0 && r.height > 0
    });
  });

  function cssLabel(el) {
    var tag = (el.tagName || 'el').toLowerCase();
    if (el.id) return tag + '#' + el.id;
    var cls = typeof el.className === 'string' ? el.className.trim().split(/\s+/).filter(Boolean).slice(0, 2) : [];
    return cls.length ? (tag + '.' + cls.join('.')) : tag;
  }

  var clippedTexts = [];
  document.querySelectorAll('h1,h2,h3,p,a,button,span,label').forEach(function(el) {
    if (clippedTexts.length >= 8) return;
    var cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return;
    if ((cs.overflowX === 'hidden' || cs.overflow === 'hidden') && cs.textOverflow !== 'ellipsis') {
      if (el.scrollWidth > el.clientWidth + 4 && el.clientWidth > 0) {
        clippedTexts.push(cssLabel(el) + ' ("' + String(el.innerText || '').trim().slice(0, 32) + '")');
      }
    }
  });

  var marks = [];
  var occludedControls = [];
  var candidates = document.querySelectorAll(
    'button, a[href], input:not([type="hidden"]), select, textarea, [role="button"], canvas, header, nav, h1'
  );
  var overlay = null;
  if (enableAnnotate) {
    overlay = document.createElement('div');
    overlay.id = '__wsa_som_overlay__';
    overlay.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647;';
  }

  var markIdx = 0;
  for (var i = 0; i < candidates.length && markIdx < maxMarks; i++) {
    var el = candidates[i];
    var cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity || '1') < 0.05) continue;
    var r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) continue;
    if (r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) continue;

    var isInteractive = /^(BUTTON|A|INPUT|SELECT|TEXTAREA)$/.test(el.tagName) || el.getAttribute('role') === 'button';
    var occluded = false;
    if (isInteractive) {
      var cx = Math.min(Math.max(r.left + r.width / 2, 1), Math.max(1, vw - 1));
      var cy = Math.min(Math.max(r.top + r.height / 2, 1), Math.max(1, vh - 1));
      var topEl = document.elementFromPoint(cx, cy);
      if (topEl && topEl !== el && !el.contains(topEl) && !topEl.contains(el)) {
        occluded = true;
        occludedControls.push(cssLabel(el) + ' covered by ' + cssLabel(topEl));
      }
    }

    markIdx += 1;
    var text = String(el.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').replace(/\s+/g, ' ').trim().slice(0, 42);
    marks.push({
      id: markIdx,
      selector: cssLabel(el),
      tag: (el.tagName || '').toLowerCase(),
      x: Math.round(r.left),
      y: Math.round(r.top),
      w: Math.round(r.width),
      h: Math.round(r.height),
      text: text,
      occluded: occluded
    });

    if (overlay) {
      var box = document.createElement('div');
      box.style.cssText =
        'position:fixed;box-sizing:border-box;border:2px solid ' + (occluded ? '#ef4444' : '#ff2e93') + ';' +
        'left:' + Math.round(r.left) + 'px;top:' + Math.round(r.top) + 'px;' +
        'width:' + Math.round(r.width) + 'px;height:' + Math.round(r.height) + 'px;';
      var badge = document.createElement('span');
      badge.textContent = String(markIdx);
      badge.style.cssText =
        'position:absolute;left:-2px;top:-2px;background:' + (occluded ? '#ef4444' : '#ff2e93') + ';' +
        'color:#ffffff;font:700 11px/1 monospace;padding:2px 4px;border-radius:2px;';
      box.appendChild(badge);
      overlay.appendChild(box);
    }
  }

  if (overlay && document.documentElement) {
    document.documentElement.appendChild(overlay);
  }

  return {
    title: String(document.title || '').slice(0, 120),
    url: String(location.href || ''),
    viewport: { width: vw, height: vh },
    scroll: { width: sw, height: sh },
    overflow_x: Math.max(0, sw - baseVw),
    broken_images: brokenImages.slice(0, 10),
    canvases: canvases.slice(0, 8),
    clipped_texts: clippedTexts.slice(0, 8),
    occluded_controls: occludedControls.slice(0, 8),
    marks: marks
  };
})"""

_REMOVE_SOM_JS = r"""(function(){ var el = document.getElementById('__wsa_som_overlay__'); if (el) el.remove(); return true; })()"""


def find_chromium_binary() -> Optional[str]:
    """Locate an installed Chrome, Brave, Chromium, or Edge binary without disturbing any running profile."""
    override = str(os.environ.get("WSA_BROWSER_BIN") or "").strip()
    if override and Path(override).is_file():
        return override

    candidates: List[str] = []
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates.extend([
            os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(pf, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            os.path.join(pf86, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            os.path.join(local, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
        ])
    elif sys.platform == "darwin":
        candidates.extend([
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ])
    else:
        for name in ("google-chrome", "google-chrome-stable", "brave-browser", "chromium", "chromium-browser", "microsoft-edge"):
            found = shutil.which(name)
            if found:
                candidates.append(found)

    for path_str in candidates:
        if path_str and Path(path_str).is_file():
            return path_str
    return None


class _QuietStaticHandler(http.server.SimpleHTTPRequestHandler):
    """Quiet HTTP handler with modern ES module & asset MIME mappings."""

    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".css": "text/css",
        ".svg": "image/svg+xml",
        ".wasm": "application/wasm",
        ".json": "application/json",
        ".webp": "image/webp",
    }

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/favicon.ico":
            fav = Path(self.directory) / "favicon.ico"
            if not fav.is_file():
                self.send_response(204)
                self.end_headers()
                return
        super().do_GET()

    def send_head(self):
        # The entry page and its subresources must resolve within the served project.
        root = Path(self.directory).resolve()
        path = Path(self.translate_path(self.path)).resolve()
        candidates = [path]
        if path.is_dir():
            candidates.extend((path / index).resolve() for index in ("index.html", "index.htm"))
        if any(not candidate.is_relative_to(root) for candidate in candidates):
            self.send_error(403)
            return None
        return super().send_head()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


class _EphemeralStaticServer:
    """Serve a local directory on 127.0.0.1:0 only for the duration of a visual capture."""

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = Path(root_dir).resolve()
        self._httpd: Optional[http.server.HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.base_url: str = ""

    def __enter__(self) -> "_EphemeralStaticServer":
        from wsa_server.server import ThreadedServer

        handler = functools.partial(_QuietStaticHandler, directory=str(self.root_dir))
        self._httpd = ThreadedServer(("127.0.0.1", 0), handler)
        port = self._httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
        return False


class _HeadlessCDPClient:
    """Minimal raw CDP client over websocket-client targeting an isolated headless Chromium instance."""

    def __init__(
        self,
        browser_bin: str,
        *,
        disable_webgl: bool = False,
        timeout: float = 20.0,
    ) -> None:
        self.browser_bin = browser_bin
        self.disable_webgl = bool(disable_webgl)
        self.timeout = float(timeout)
        self._profile_dir: Optional[Path] = None
        self._proc: Optional[subprocess.Popen] = None
        self._ws = None
        self._msg_id = 0
        self.console_errors: List[str] = []
        self.page_exceptions: List[str] = []
        self.failed_requests: List[str] = []

    def __enter__(self) -> "_HeadlessCDPClient":
        import websocket  # type: ignore

        self._profile_dir = Path(tempfile.mkdtemp(prefix="wsa-cdp-profile-"))
        args = [
            self.browser_bin,
            "--headless=new",
            "--remote-debugging-port=0",
            f"--user-data-dir={self._profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-extensions",
            "--disable-sync",
            "--disable-popup-blocking",
            "--mute-audio",
            "--hide-scrollbars",
            "--window-size=1440,900",
        ]
        if self.disable_webgl:
            args.extend(["--disable-webgl", "--disable-webgl2"])
        else:
            args.extend(["--use-gl=angle", "--use-angle=swiftshader-webgl", "--enable-unsafe-swiftshader"])
        args.append("about:blank")

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            self._proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=creationflags,
            )

            port = self._wait_for_debug_port(timeout=min(self.timeout, 15.0))
            page_ws_url = self._resolve_page_ws_url(port, timeout=min(self.timeout, 10.0))
            self._ws = websocket.create_connection(page_ws_url, timeout=self.timeout, suppress_origin=True)
            self.call("Page.enable")
            self.call("Runtime.enable")
            self.call("Network.enable")
            self.call("Log.enable")
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def close(self) -> None:
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass
        self._ws = None
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3.0)
            except Exception:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=3.0)
                except Exception:
                    pass
            try:
                if self._proc.stderr is not None:
                    self._proc.stderr.close()
            except Exception:
                pass
            self._proc = None
        if self._profile_dir is not None:
            shutil.rmtree(self._profile_dir, ignore_errors=True)
            self._profile_dir = None

    def _wait_for_debug_port(self, timeout: float = 12.0) -> int:
        assert self._profile_dir is not None and self._proc is not None
        active_port_file = self._profile_dir / "DevToolsActivePort"
        stderr_lines: List[str] = []

        def _drain_stderr() -> None:
            assert self._proc is not None and self._proc.stderr is not None
            try:
                for ln in self._proc.stderr:
                    stderr_lines.append(ln.strip())
                    if len(stderr_lines) > 80:
                        stderr_lines.pop(0)
            except Exception:
                pass

        t = threading.Thread(target=_drain_stderr, daemon=True)
        t.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if active_port_file.is_file():
                try:
                    first = active_port_file.read_text(encoding="utf-8").splitlines()[0].strip()
                    if first.isdigit():
                        return int(first)
                except Exception:
                    pass
            for ln in list(stderr_lines):
                m = re.search(r"DevTools listening on ws://[^:]+:(\d+)/", ln)
                if m:
                    return int(m.group(1))
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"Headless browser exited early (code {self._proc.returncode}): {' | '.join(stderr_lines[-5:])}"
                )
            time.sleep(0.08)
        raise TimeoutError(f"Timed out waiting for headless browser DevTools port: {' | '.join(stderr_lines[-5:])}")

    def _resolve_page_ws_url(self, port: int, timeout: float = 8.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2.0) as resp:
                    targets = json.loads(resp.read().decode("utf-8"))
                for tgt in targets:
                    if tgt.get("type") == "page" and tgt.get("webSocketDebuggerUrl"):
                        return str(tgt["webSocketDebuggerUrl"])
            except Exception:
                pass
            time.sleep(0.1)
        raise TimeoutError("Could not find page WebSocket target on headless browser.")

    def _record_event(self, msg: Dict[str, Any]) -> None:
        method = str(msg.get("method") or "")
        params = msg.get("params") or {}
        if method == "Runtime.consoleAPICalled":
            if str(params.get("type") or "").lower() == "error":
                args = params.get("args") or []
                parts = [str(a.get("value") or a.get("description") or "") for a in args if isinstance(a, dict)]
                txt = " ".join(p for p in parts if p).strip()
                if txt and len(self.console_errors) < 20:
                    self.console_errors.append(txt[:220])
        elif method == "Runtime.exceptionThrown":
            det = params.get("exceptionDetails") or {}
            exc = det.get("exception") or {}
            txt = str(exc.get("description") or det.get("text") or "Uncaught exception").strip()
            if txt and len(self.page_exceptions) < 20:
                self.page_exceptions.append(txt[:220])
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            if str(entry.get("level") or "").lower() == "error":
                txt = str(entry.get("text") or "").strip()
                if txt and len(self.console_errors) < 20:
                    self.console_errors.append(txt[:220])
        elif method == "Network.loadingFailed":
            err_txt = str(params.get("errorText") or "loadingFailed")
            if err_txt != "net::ERR_ABORTED" and len(self.failed_requests) < 20:
                self.failed_requests.append(f"Network failed: {err_txt}")
        elif method == "Network.responseReceived":
            resp = params.get("response") or {}
            status = int(resp.get("status") or 0)
            url = str(resp.get("url") or "")
            if status >= 400 and not url.rstrip("/").endswith("/favicon.ico") and len(self.failed_requests) < 20:
                self.failed_requests.append(f"HTTP {status} {url[:140]}")

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        assert self._ws is not None
        self._msg_id += 1
        req_id = self._msg_id
        payload = {"id": req_id, "method": method, "params": params or {}}
        self._ws.settimeout(timeout or self.timeout)
        self._ws.send(json.dumps(payload))
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            raw = self._ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} error: {msg['error']}")
                return msg.get("result") or {}
            self._record_event(msg)
        raise TimeoutError(f"CDP call timed out: {method}")

    def drain_events(self, wait_ms: int = 120) -> None:
        assert self._ws is not None
        end = time.time() + max(0.01, wait_ms / 1000.0)
        while time.time() < end:
            rem = max(0.01, end - time.time())
            try:
                self._ws.settimeout(min(0.05, rem))
                raw = self._ws.recv()
                self._record_event(json.loads(raw))
            except Exception:
                break
        self._ws.settimeout(self.timeout)

    def navigate(self, url: str, wait_ms: int = 500) -> None:
        self.call("Page.navigate", {"url": url})
        deadline = time.time() + min(self.timeout, 15.0)
        while time.time() < deadline:
            try:
                res = self.evaluate("document.readyState")
                if res in ("interactive", "complete"):
                    if res == "complete":
                        break
            except Exception:
                pass
            self.drain_events(80)
        self.drain_events(max(120, int(wait_ms)))

    def set_viewport(self, width: int, height: int, *, mobile: bool = False, scale: float = 1.0) -> None:
        self.call("Emulation.setDeviceMetricsOverride", {
            "width": int(width),
            "height": int(height),
            "deviceScaleFactor": float(scale),
            "mobile": bool(mobile),
        })
        self.drain_events(120)

    def set_color_scheme(self, scheme: str) -> None:
        if scheme in ("dark", "light"):
            self.call("Emulation.setEmulatedMedia", {
                "features": [{"name": "prefers-color-scheme", "value": scheme}],
            })
            self.drain_events(100)

    def evaluate(self, expression: str) -> Any:
        res = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        if res.get("exceptionDetails"):
            raise RuntimeError(str(res["exceptionDetails"]))
        return (res.get("result") or {}).get("value")

    def screenshot_png(self, *, full_page: bool = False, clip: Optional[Dict[str, float]] = None) -> bytes:
        params: Dict[str, Any] = {"format": "png", "fromSurface": True}
        if full_page:
            params["captureBeyondViewport"] = True
        if clip:
            params["clip"] = clip
        res = self.call("Page.captureScreenshot", params)
        return base64.b64decode(res.get("data") or "")

    def run_actions(self, actions: List[Dict[str, Any]]) -> List[str]:
        log_lines: List[str] = []
        for act in actions or []:
            if not isinstance(act, dict):
                raise ValueError("Each visual action must be an object")
            kind = str(act.get("type") or act.get("action") or "").strip().lower()
            selector = str(act.get("selector") or "").strip()
            if kind == "wait":
                ms = max(0, min(int(act.get("ms") or 300), 5000))
                self.drain_events(ms)
                log_lines.append(f"wait({ms}ms)")
                continue
            if kind == "scroll":
                if selector:
                    found = self.evaluate(
                        f"(function(){{ var e=document.querySelector({json.dumps(selector)}); if(e) e.scrollIntoView({{block:'center'}}); return !!e; }})()"
                    )
                    if not found:
                        raise ValueError(f"Scroll target was not found: {selector}")
                    log_lines.append(f"scroll({selector})")
                else:
                    y = int(act.get("y") or 500)
                    x = int(act.get("x") or 0)
                    self.evaluate(f"window.scrollTo({x}, {y}); true;")
                    log_lines.append(f"scroll({x},{y})")
                self.drain_events(200)
                continue

            cx = act.get("x")
            cy = act.get("y")
            if (cx is None or cy is None) and selector:
                pt = self.evaluate(
                    f"(function(){{ var e=document.querySelector({json.dumps(selector)}); if(!e) return null; var r=e.getBoundingClientRect(); return {{x:r.left+r.width/2, y:r.top+r.height/2}}; }})()"
                )
                if isinstance(pt, dict):
                    cx, cy = float(pt["x"]), float(pt["y"])
            if kind == "click" and cx is not None and cy is not None:
                self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": float(cx), "y": float(cy)})
                self.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": float(cx), "y": float(cy), "button": "left", "clickCount": 1})
                self.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": float(cx), "y": float(cy), "button": "left", "clickCount": 1})
                self.drain_events(250)
                log_lines.append(f"click({selector or f'{int(cx)},{int(cy)}'})")
            elif kind == "hover" and cx is not None and cy is not None:
                self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": float(cx), "y": float(cy)})
                self.drain_events(200)
                log_lines.append(f"hover({selector or f'{int(cx)},{int(cy)}'})")
            elif kind == "drag" and cx is not None and cy is not None:
                dx = float(act.get("dx") or 160)
                dy = float(act.get("dy") or 0)
                steps = max(2, min(int(act.get("steps") or 8), 24))
                self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": float(cx), "y": float(cy)})
                self.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": float(cx), "y": float(cy), "button": "left", "clickCount": 1})
                for s in range(1, steps + 1):
                    nx = float(cx) + dx * (s / steps)
                    ny = float(cy) + dy * (s / steps)
                    self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": nx, "y": ny, "buttons": 1})
                    time.sleep(0.025)
                self.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": float(cx) + dx, "y": float(cy) + dy, "button": "left", "clickCount": 1})
                self.drain_events(260)
                log_lines.append(f"drag({selector or 'canvas'} dx={int(dx)},dy={int(dy)})")
            elif kind == "type":
                text = str(act.get("text") or "")
                if selector:
                    focused = self.evaluate(f"(function(){{ var e=document.querySelector({json.dumps(selector)}); if(e) e.focus(); return !!e && document.activeElement === e; }})()")
                    if not focused:
                        raise ValueError(f"Typing target could not be focused: {selector}")
                if text:
                    self.call("Input.insertText", {"text": text})
                    self.drain_events(150)
                    log_lines.append(f"type({selector or 'focused'})")
            else:
                raise ValueError(f"Cannot perform visual action {kind!r} on {selector or (cx, cy)}")
        return log_lines


def analyze_screenshot_pixels(png_bytes: bytes) -> Dict[str, Any]:
    """Check whether a captured frame rendered real visual content vs a collapsed/blank canvas."""
    if not png_bytes:
        return {"ok": False, "blank": True, "unique_buckets": 0, "std_luma": 0.0}
    try:
        from PIL import Image, ImageStat  # type: ignore

        with Image.open(io.BytesIO(png_bytes)) as im:
            small = im.convert("RGB").resize((160, 100))
            gray = small.convert("L")
            std_luma = float(ImageStat.Stat(gray).stddev[0])
            # Quantize to 4-bit per channel buckets to count distinct visible tones
            palette = small.point(lambda channel: channel >> 4)
            bucket_count = len(palette.getcolors(maxcolors=16 ** 3))
            blank = (std_luma < 1.5 and bucket_count <= 2)
            return {
                "ok": True,
                "blank": blank,
                "unique_buckets": bucket_count,
                "std_luma": round(std_luma, 2),
                "width": im.size[0],
                "height": im.size[1],
            }
    except Exception:
        return {"ok": False, "blank": False, "unique_buckets": -1, "std_luma": -1.0}


def build_pixel_diff_tile(prev_png: bytes, curr_png: bytes) -> Tuple[Optional[bytes], float]:
    """Return `(diff_png_bytes, changed_pct)` highlighting pixels whose RGB delta exceeds threshold."""
    if not prev_png or not curr_png:
        return None, 0.0
    try:
        from PIL import Image, ImageChops  # type: ignore

        with Image.open(io.BytesIO(prev_png)) as im1, Image.open(io.BytesIO(curr_png)) as im2:
            a = im1.convert("RGB")
            b = im2.convert("RGB")
            if a.size != b.size:
                a = a.resize(b.size)
            diff = ImageChops.difference(a, b)
            # Downscale slightly if huge for fast per-pixel highlighting
            w, h = b.size
            if max(w, h) > 960:
                scale = 960.0 / float(max(w, h))
                nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
                b_work = b.resize((nw, nh))
                d_work = diff.resize((nw, nh))
            else:
                b_work, d_work = b, diff
            red, green, blue = d_work.split()
            max_delta = ImageChops.lighter(ImageChops.lighter(red, green), blue)
            mask = max_delta.point(lambda delta: 255 if delta > 18 else 0)
            changed = mask.histogram()[255]
            total = mask.width * mask.height
            pct = round((changed * 100.0) / total, 2)
            out_img = b_work.convert("L").point(lambda gray: gray // 2).convert("RGB")
            out_img.paste((255, 46, 147), mask=mask)
            buf = io.BytesIO()
            out_img.save(buf, format="PNG")
            return buf.getvalue(), pct
    except Exception:
        return None, 0.0


def compose_contact_sheet(
    tiles: List[Tuple[str, bytes]],
    *,
    max_width: int = 1568,
) -> Tuple[bytes, str, int, int]:
    """Stitch 1–4 labeled tiles into a single compact VLM-ready contact sheet image."""
    valid = [(lbl, data) for (lbl, data) in tiles if data][:4]
    if not valid:
        raise ValueError("No valid screenshot tiles to compose")
    if len(valid) == 1:
        single = valid[0][1]
        try:
            from PIL import Image  # type: ignore

            with Image.open(io.BytesIO(single)) as im:
                w, h = im.size
                if max(w, h) <= max_width:
                    return single, "image/png", w, h
                scale = max_width / float(max(w, h))
                nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
                resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
                resized = im.convert("RGB").resize((nw, nh), resample=resample)
                buf = io.BytesIO()
                resized.save(buf, format="WEBP", quality=86)
                return buf.getvalue(), "image/webp", nw, nh
        except Exception:
            return single, "image/png", 0, 0

    from PIL import Image, ImageDraw  # type: ignore

    cols = 2
    rows = 1 if len(valid) == 2 else 2
    pad = 10
    header_h = 26
    tile_w = (max_width - pad * (cols + 1)) // cols
    tile_h = int(round(tile_w * 0.62))
    canvas_w = pad + cols * (tile_w + pad)
    canvas_h = pad + rows * (tile_h + header_h + pad)
    sheet = Image.new("RGB", (canvas_w, canvas_h), (18, 20, 26))
    draw = ImageDraw.Draw(sheet)
    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)

    for idx, (label, png_bytes) in enumerate(valid):
        r_idx = idx // cols
        c_idx = idx % cols
        x0 = pad + c_idx * (tile_w + pad)
        y0 = pad + r_idx * (tile_h + header_h + pad)
        # Header bar
        draw.rectangle([x0, y0, x0 + tile_w, y0 + header_h], fill=(32, 36, 48))
        draw.text((x0 + 8, y0 + 6), str(label)[:64], fill=(235, 238, 245))
        # Tile background frame
        draw.rectangle([x0, y0 + header_h, x0 + tile_w, y0 + header_h + tile_h], fill=(12, 14, 18), outline=(58, 64, 80))
        with Image.open(io.BytesIO(png_bytes)) as raw_im:
            rgb = raw_im.convert("RGB")
            iw, ih = rgb.size
            scale = min(tile_w / float(max(1, iw)), tile_h / float(max(1, ih)))
            nw, nh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
            fitted = rgb.resize((nw, nh), resample=resample)
            ox = x0 + (tile_w - nw) // 2
            oy = y0 + header_h + (tile_h - nh) // 2
            sheet.paste(fitted, (ox, oy))

    buf = io.BytesIO()
    try:
        sheet.save(buf, format="WEBP", quality=86, method=4)
        return buf.getvalue(), "image/webp", canvas_w, canvas_h
    except Exception:
        buf = io.BytesIO()
        sheet.save(buf, format="JPEG", quality=86, optimize=True)
        return buf.getvalue(), "image/jpeg", canvas_w, canvas_h


def _resolve_target_url(target: str, project_root: Path) -> Tuple[str, Optional[Path], str]:
    """Resolve URL or local project file/dir into `(url_or_relpath, static_server_root, cache_key)`."""
    raw = str(target or "").strip()
    if raw.startswith(("http://", "https://")):
        return raw, None, raw
    root = Path(project_root).resolve()
    cand = (root / raw).resolve() if raw else root
    if not cand.is_relative_to(root):
        raise ValueError("Visual target must be within the project")
    if cand.is_dir():
        index_html = cand / "index.html"
        if not index_html.is_file():
            raise FileNotFoundError(f"No index.html found in directory: {cand}")
        return index_html.relative_to(root).as_posix(), root, f"local:{index_html}"
    if cand.is_file():
        return cand.relative_to(root).as_posix(), root, f"local:{cand}"
    raise FileNotFoundError(f"Visual target not found: {target}")


def capture_visual_inspection(
    target: str,
    *,
    project_root: Path,
    viewports: Optional[List[Dict[str, Any]]] = None,
    actions: Optional[List[Dict[str, Any]]] = None,
    annotate: bool = True,
    full_page: bool = False,
    include_diff: bool = True,
    color_scheme: str = "",
    disable_webgl: bool = False,
    wait_ms: int = 500,
    browser_bin: Optional[str] = None,
) -> Dict[str, Any]:
    """Capture a multi-viewport grounded visual inspection contact sheet + telemetry in total background isolation."""
    exe = browser_bin or find_chromium_binary()
    if not exe:
        return {"ok": False, "error": "No Chrome, Brave, Chromium, or Edge binary found for headless visual capture."}

    try:
        resolved, srv_root, cache_key = _resolve_target_url(target, project_root)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    vp_list = list(viewports or DEFAULT_VIEWPORTS)[:3]
    tiles: List[Tuple[str, bytes]] = []
    viewport_reports: List[Dict[str, Any]] = []
    primary_png: Optional[bytes] = None
    action_log: List[str] = []
    diff_pct: Optional[float] = None

    def _run_with_url(target_url: str) -> Dict[str, Any]:
        nonlocal primary_png, action_log, diff_pct
        with _HeadlessCDPClient(exe, disable_webgl=disable_webgl) as cdp:
            if color_scheme in ("dark", "light"):
                cdp.set_color_scheme(color_scheme)

            for idx, vp in enumerate(vp_list):
                vname = str(vp.get("name") or f"vp{idx + 1}")
                vw = max(240, min(int(vp.get("width") or 1440), 2560))
                vh = max(240, min(int(vp.get("height") or 900), 2560))
                is_mobile = bool(vp.get("mobile", vw <= 520))
                cdp.set_viewport(vw, vh, mobile=is_mobile)
                if idx == 0:
                    primary_viewport = (vw, vh, is_mobile)
                    cdp.navigate(target_url, wait_ms=wait_ms)
                else:
                    cdp.drain_events(250)

                want_som = bool(annotate and idx == 0)
                audit = cdp.evaluate(f"{_AUDIT_AND_SOM_JS}({json.dumps(want_som)}, 24, {int(vw)})") or {}
                display_png = cdp.screenshot_png(full_page=bool(full_page and idx == 0))
                if want_som:
                    cdp.evaluate(_REMOVE_SOM_JS)
                    clean_png = cdp.screenshot_png(full_page=bool(full_page))
                else:
                    clean_png = display_png
                pix = analyze_screenshot_pixels(clean_png)
                if idx == 0:
                    primary_png = clean_png
                lbl = f"{vname.upper()} {vw}x{vh}" + (" [SoM]" if want_som else "")
                tiles.append((lbl, display_png))
                viewport_reports.append({
                    "name": vname,
                    "width": vw,
                    "height": vh,
                    "audit": audit,
                    "pixels": pix,
                })

            if actions:
                width, height, mobile = primary_viewport
                cdp.set_viewport(width, height, mobile=mobile)
                action_log = cdp.run_actions(actions)
                after_png = cdp.screenshot_png(full_page=False)
                tiles.append(("AFTER INTERACTION", after_png))

            console_errors = list(cdp.console_errors)
            page_exceptions = list(cdp.page_exceptions)
            failed_requests = list(cdp.failed_requests)

        if include_diff and primary_png is not None:
            baseline = (Path(project_root).resolve(), cache_key, primary_viewport,
                        color_scheme, bool(full_page), bool(disable_webgl))
            with _PREVIOUS_CAPTURES_LOCK:
                prev_png = _PREVIOUS_CAPTURES.pop(baseline, None)
                _PREVIOUS_CAPTURES[baseline] = primary_png
                if len(_PREVIOUS_CAPTURES) > _MAX_PREVIOUS_CACHE:
                    _PREVIOUS_CAPTURES.pop(next(iter(_PREVIOUS_CAPTURES)))
            if prev_png is not None and len(tiles) < 4:
                diff_bytes, pct = build_pixel_diff_tile(prev_png, primary_png)
                if diff_bytes:
                    diff_pct = pct
                    tiles.append((f"PIXEL DIFF ({pct:.1f}% changed)", diff_bytes))

        sheet_bytes, sheet_mime, sheet_w, sheet_h = compose_contact_sheet(tiles)
        return {
            "ok": True,
            "target_url": target_url,
            "browser_bin": exe,
            "image_bytes": sheet_bytes,
            "image_mime": sheet_mime,
            "image_width": sheet_w,
            "image_height": sheet_h,
            "tiles": [lbl for (lbl, _) in tiles],
            "viewports": viewport_reports,
            "actions": action_log,
            "diff_pct": diff_pct,
            "console_errors": console_errors,
            "page_exceptions": page_exceptions,
            "failed_requests": failed_requests,
        }

    try:
        if srv_root is not None:
            with _EphemeralStaticServer(srv_root) as srv:
                url = f"{srv.base_url}/{urllib.parse.quote(resolved)}"
                return _run_with_url(url)
        return _run_with_url(resolved)
    except Exception as exc:
        return {"ok": False, "error": f"Headless visual capture failed: {type(exc).__name__}: {exc}"}


def format_visual_report(res: Dict[str, Any], saved_path_summary: str = "") -> str:
    """Format compact, deterministic visual telemetry + Set-of-Mark legend for the coding agent."""
    if not res or not res.get("ok"):
        return f"[Error] {res.get('error', 'Visual inspection failed')}"

    lines: List[str] = [
        f"[OK] Visual capture attached ({', '.join(res.get('tiles') or [])}) -> {saved_path_summary or 'queued'}"
    ]
    if res.get("actions"):
        lines.append(f"Actions executed: {', '.join(res['actions'])}")
    if res.get("diff_pct") is not None:
        lines.append(f"Pixel diff vs previous capture: {res['diff_pct']:.2f}% pixels changed")

    passed = bool(res.get("viewports")) and not any(res.get(k) for k in ("console_errors", "page_exceptions", "failed_requests"))
    for vp in res.get("viewports") or []:
        audit = vp.get("audit") or {}
        pix = vp.get("pixels") or {}
        vname = vp.get("name", "vp")
        vw, vh = vp.get("width", 0), vp.get("height", 0)
        ovx = int(audit.get("overflow_x") or 0)
        status_flags = []
        if not audit or not pix.get("ok"):
            status_flags.append("INCOMPLETE_CAPTURE")
        if ovx > 0:
            status_flags.append(f"OVERFLOW_X=+{ovx}px")
        if pix.get("blank"):
            status_flags.append("WARNING: BLANK/UNIFORM RENDER")
        if audit.get("broken_images"):
            status_flags.append(f"BROKEN_IMAGES={len(audit['broken_images'])}")
        if audit.get("occluded_controls"):
            status_flags.append(f"OCCLUDED={len(audit['occluded_controls'])}")
        if audit.get("clipped_texts"):
            status_flags.append(f"CLIPPED_TEXT={len(audit['clipped_texts'])}")
        if status_flags:
            passed = False
        flag_str = " | ".join(status_flags) if status_flags else "layout clean"
        lines.append(
            f"Viewport [{vname} {vw}x{vh}]: {flag_str} "
            f"(luma_std={pix.get('std_luma')}, colors={pix.get('unique_buckets')})"
        )
        if audit.get("canvases"):
            c_desc = ", ".join(
                f"{c['selector']}({c['width']}x{c['height']})" for c in audit["canvases"][:4]
            )
            lines.append(f"  Canvases: {c_desc}")
        if audit.get("broken_images"):
            lines.append(f"  Broken images: {', '.join(audit['broken_images'][:4])}")
        if audit.get("occluded_controls"):
            lines.append(f"  Occluded controls: {'; '.join(audit['occluded_controls'][:4])}")
        if audit.get("clipped_texts"):
            lines.append(f"  Clipped text: {'; '.join(audit['clipped_texts'][:4])}")
        marks = audit.get("marks") or []
        if marks:
            lines.append("  Set-of-Mark legend:")
            for m in marks[:16]:
                occ = " [OCCLUDED]" if m.get("occluded") else ""
                txt = f' "{m["text"]}"' if m.get("text") else ""
                lines.append(f"    [{m['id']}] {m['selector']} ({m['x']},{m['y']} {m['w']}x{m['h']}){txt}{occ}")

    errs = list(res.get("console_errors") or []) + list(res.get("page_exceptions") or [])
    if errs:
        lines.append(f"Console/JS Errors ({len(errs)}):")
        for e in errs[:6]:
            lines.append(f"  - {e}")
    else:
        lines.append("Console/JS Errors: 0")

    if res.get("failed_requests"):
        lines.append(f"Failed Network Requests ({len(res['failed_requests'])}):")
        for fr in res["failed_requests"][:6]:
            lines.append(f"  - {fr}")
    lines.append("Visual checks: pass" if passed else "Visual checks: fail")
    return "\n".join(lines)
