"""Zero-focus headless CDP visual capture, Set-of-Mark grounding, contact-sheet compositing, and pixel diffing."""
from __future__ import annotations

import base64
import functools
import http.server
import io
import json
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
]

_VIEWPORT_PRESETS: Dict[str, Dict[str, Any]] = {
    "desktop": {"name": "desktop", "width": 1440, "height": 900, "mobile": False},
    "laptop": {"name": "desktop", "width": 1440, "height": 900, "mobile": False},
    "tablet": {"name": "tablet", "width": 768, "height": 1024, "mobile": False},
    "ipad": {"name": "tablet", "width": 768, "height": 1024, "mobile": False},
    "mobile": {"name": "mobile", "width": 390, "height": 844, "mobile": True},
    "phone": {"name": "mobile", "width": 390, "height": 844, "mobile": True},
}
_VIEWPORT_DIMS_RE = re.compile(r"^\s*(\d{3,4})\s*[xX×]\s*(\d{3,4})\s*$")


def _normalize_viewports(viewports: Any) -> List[Dict[str, Any]]:
    """Normalize dict specs, preset names ('desktop'/'tablet'/'mobile'), or 'WxH' strings into viewport dicts."""
    if not viewports:
        return [dict(v) for v in DEFAULT_VIEWPORTS]
    items = [viewports] if isinstance(viewports, (str, dict)) else list(viewports)
    out: List[Dict[str, Any]] = []
    for raw in items[:3]:
        if isinstance(raw, dict):
            out.append(dict(raw))
            continue
        if isinstance(raw, str):
            tok = raw.strip().lower()
            if tok in _VIEWPORT_PRESETS:
                out.append(dict(_VIEWPORT_PRESETS[tok]))
                continue
            m = _VIEWPORT_DIMS_RE.match(raw)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                out.append({"name": f"{w}x{h}", "width": w, "height": h, "mobile": w <= 520})
                continue
            out.append({"name": (raw.strip() or "desktop")[:24], "width": 1440, "height": 900, "mobile": False})
    return out or [dict(v) for v in DEFAULT_VIEWPORTS]

_EXPECTED_DISABLE_WEBGL_ERR_RE = re.compile(
    r"(?i)(?:webgl\s+context\s+could\s+not\s+be\s+created"
    r"|error\s+creating\s+webgl\s+context"
    r"|failed\s+to\s+create\s+webgl\s+context"
    r"|disabled\s+by\s+enterprise\s+policy\s+or\s+command\s+line)"
)


def _split_webgl_disabled_errors(console_errors: List[str], disable_webgl: bool) -> Tuple[List[str], List[str]]:
    """Separate expected WebGL context-creation logs from unexpected JS/runtime errors when WebGL is disabled."""
    if not disable_webgl or not console_errors:
        return list(console_errors), []
    real: List[str] = []
    suppressed: List[str] = []
    for err in console_errors:
        if _EXPECTED_DISABLE_WEBGL_ERR_RE.search(str(err or "")):
            suppressed.append(err)
        else:
            real.append(err)
    return real, suppressed


_DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_DESKTOP_UA_METADATA: Dict[str, Any] = {
    "brands": [
        {"brand": "Google Chrome", "version": "131"},
        {"brand": "Chromium", "version": "131"},
        {"brand": "Not_A Brand", "version": "24"},
    ],
    "fullVersionList": [
        {"brand": "Google Chrome", "version": "131.0.0.0"},
        {"brand": "Chromium", "version": "131.0.0.0"},
        {"brand": "Not_A Brand", "version": "24.0.0.0"},
    ],
    "platform": "Windows",
    "platformVersion": "15.0.0",
    "architecture": "x86",
    "model": "",
    "mobile": False,
    "bitness": "64",
    "wow64": False,
}

_MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)

_MOBILE_UA_METADATA: Dict[str, Any] = {
    "brands": [
        {"brand": "Google Chrome", "version": "131"},
        {"brand": "Chromium", "version": "131"},
        {"brand": "Not_A Brand", "version": "24"},
    ],
    "fullVersionList": [
        {"brand": "Google Chrome", "version": "131.0.0.0"},
        {"brand": "Chromium", "version": "131.0.0.0"},
        {"brand": "Not_A Brand", "version": "24.0.0.0"},
    ],
    "platform": "Android",
    "platformVersion": "14.0.0",
    "architecture": "",
    "model": "Pixel 8",
    "mobile": True,
    "bitness": "64",
    "wow64": False,
}

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
    var cls = typeof el.className === 'string' ? el.className : '';
    var isSrOnly = /(?:^|\s)(?:sr-only|visually-hidden|screen-reader-text)(?:\s|$)/i.test(cls)
      || (el.clientWidth <= 2 && el.clientHeight <= 2)
      || (cs.clipPath && cs.clipPath.indexOf('50%') !== -1)
      || (cs.clip && cs.clip.indexOf('0px, 0px, 0px, 0px') !== -1);
    if (isSrOnly) return;
    if ((cs.overflowX === 'hidden' || cs.overflow === 'hidden') && cs.textOverflow !== 'ellipsis') {
      if (el.scrollWidth > el.clientWidth + 4 && el.clientWidth > 0) {
        clippedTexts.push(cssLabel(el) + ' ("' + String(el.innerText || '').trim().slice(0, 32) + '")');
      }
    }
  });

  function parseRgba(str) {
    if (!str || str === 'transparent') return [0, 0, 0, 0];
    var m = String(str).match(/rgba?\(\s*([0-9.]+)[\s,]+([0-9.]+)[\s,]+([0-9.]+)(?:[\s,/]+([0-9.]+))?\s*\)/i);
    if (!m) return [0, 0, 0, 0];
    return [parseFloat(m[1]) || 0, parseFloat(m[2]) || 0, parseFloat(m[3]) || 0, m[4] !== undefined ? parseFloat(m[4]) : 1];
  }
  function relLuma(rgb) {
    function ch(v) {
      var s = Math.max(0, Math.min(255, v)) / 255;
      return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
    }
    return 0.2126 * ch(rgb[0]) + 0.7152 * ch(rgb[1]) + 0.0722 * ch(rgb[2]);
  }

  var hasFixedBgCanvas = false;
  document.querySelectorAll('canvas').forEach(function(c) {
    var cr = c.getBoundingClientRect();
    if (cr.width >= vw * 0.55 && cr.height >= vh * 0.55) {
      var ccs = getComputedStyle(c);
      var pcs = c.parentElement ? getComputedStyle(c.parentElement) : null;
      if (ccs.position === 'fixed' || ccs.position === 'sticky' || (pcs && (pcs.position === 'fixed' || pcs.position === 'sticky'))) {
        hasFixedBgCanvas = true;
      }
    }
  });

  var lowContrastTexts = [];
  document.querySelectorAll('h1,h2,h3,p,dt,dd,th,td').forEach(function(el) {
    if (lowContrastTexts.length >= 6) return;
    var cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity || '1') < 0.1) return;
    var cls = typeof el.className === 'string' ? el.className : '';
    if (/(?:^|\s)(?:sr-only|visually-hidden|screen-reader-text)(?:\s|$)/i.test(cls)) return;
    var r = el.getBoundingClientRect();
    if (r.width < 16 || r.height < 10 || r.bottom <= 0 || r.top >= vh) return;
    var txt = String(el.innerText || '').replace(/\s+/g, ' ').trim();
    if (txt.length < 3) return;
    var fg = parseRgba(cs.color);
    if (fg[3] < 0.2) return;
    var fgL = relLuma(fg);
    var cur = el;
    var maxBgAlpha = 0;
    var solidBg = null;
    var hasBlur = false;
    while (cur && cur !== document.documentElement) {
      var acs = getComputedStyle(cur);
      var bg = parseRgba(acs.backgroundColor);
      if (bg[3] > maxBgAlpha) maxBgAlpha = bg[3];
      if (!solidBg && bg[3] >= 0.55) solidBg = bg;
      var bf = acs.backdropFilter || acs.webkitBackdropFilter || 'none';
      if (bf && bf !== 'none') hasBlur = true;
      cur = cur.parentElement;
    }
    if (solidBg && !hasBlur) {
      var bgL = relLuma(solidBg);
      var ratio = (Math.max(fgL, bgL) + 0.05) / (Math.min(fgL, bgL) + 0.05);
      if (ratio < 2.6) {
        lowContrastTexts.push(cssLabel(el) + ' (low contrast ' + ratio.toFixed(1) + ':1 "' + txt.slice(0, 24) + '")');
        return;
      }
    }
    if (hasFixedBgCanvas && sh > vh * 1.4 && maxBgAlpha < 0.18 && !hasBlur && (!cs.textShadow || cs.textShadow === 'none')) {
      if ((window.scrollY > 40 || (r.left < vw * 0.78 && r.right > vw * 0.35)) && fgL < 0.18) {
        lowContrastTexts.push(cssLabel(el) + ' (unbacked dark text over 3D canvas "' + txt.slice(0, 24) + '")');
      }
    }
  });

  var headerBleeds = [];
  if (sh > vh * 1.25) {
    document.querySelectorAll('header, nav, [role="banner"]').forEach(function(hdr) {
      if (headerBleeds.length >= 3) return;
      var hcs = getComputedStyle(hdr);
      if (hcs.display === 'none' || hcs.visibility === 'hidden') return;
      if (hcs.position !== 'fixed' && hcs.position !== 'sticky') return;
      var hr = hdr.getBoundingClientRect();
      if (hr.top > 24 || hr.height < 32 || hr.width < vw * 0.5) return;
      var hBg = parseRgba(hcs.backgroundColor);
      var hBf = hcs.backdropFilter || hcs.webkitBackdropFilter || 'none';
      var maxA = hBg[3];
      var hasHBlur = Boolean(hBf && hBf !== 'none');
      if (hdr.firstElementChild) {
        var ccs = getComputedStyle(hdr.firstElementChild);
        var cBg = parseRgba(ccs.backgroundColor);
        if (cBg[3] > maxA) maxA = cBg[3];
        var cBf = ccs.backdropFilter || ccs.webkitBackdropFilter || 'none';
        if (cBf && cBf !== 'none') hasHBlur = true;
      }
      if (maxA < 0.78 || (maxA < 0.90 && !hasHBlur)) {
        headerBleeds.push(cssLabel(hdr) + ' (transparent sticky header bleeds scrolled content)');
      }
    });
  }

  var textBoxes = [];
  document.querySelectorAll('h1,h2,h3,p,a,dt,dd,th,td').forEach(function(el) {
    if (textBoxes.length >= 24) return;
    var cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity || '1') < 0.1) return;
    var cls = typeof el.className === 'string' ? el.className : '';
    if (/(?:^|\s)(?:sr-only|visually-hidden|screen-reader-text)(?:\s|$)/i.test(cls)) return;
    var r = el.getBoundingClientRect();
    if (r.width < 16 || r.height < 10 || r.bottom <= 4 || r.top >= vh - 4) return;
    var txt = String(el.innerText || '').replace(/\s+/g, ' ').trim();
    if (txt.length < 3) return;
    var fg = parseRgba(cs.color);
    if (fg[3] < 0.2) return;
    textBoxes.push({
      selector: cssLabel(el),
      text: txt.slice(0, 28),
      fg: [Math.round(fg[0]), Math.round(fg[1]), Math.round(fg[2])],
      x: Math.round(r.left),
      y: Math.round(r.top),
      w: Math.round(r.width),
      h: Math.round(r.height),
      fontSize: Math.round(parseFloat(cs.fontSize) || 16)
    });
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
    low_contrast_texts: lowContrastTexts.slice(0, 6),
    header_bleeds: headerBleeds.slice(0, 3),
    text_boxes: textBoxes.slice(0, 24),
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
        try:
            from .bootstrap import ensure_wsa_dependencies
            ensure_wsa_dependencies(quiet=True)
        except Exception:
            pass
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
            "--force-device-scale-factor=1",
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
        if self._ws is None:
            return
        end = time.time() + max(0.01, wait_ms / 1000.0)
        while time.time() < end:
            rem = max(0.01, end - time.time())
            try:
                self._ws.settimeout(min(0.05, rem))
                raw = self._ws.recv()
                self._record_event(json.loads(raw))
            except Exception:
                break
        if self._ws is not None:
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
        w = int(width)
        h = int(height)
        is_mob = bool(mobile)
        orientation = (
            {"type": "portraitPrimary", "angle": 0}
            if (is_mob or h > w)
            else {"type": "landscapePrimary", "angle": 0}
        )
        self.call("Emulation.setDeviceMetricsOverride", {
            "width": w,
            "height": h,
            "deviceScaleFactor": float(scale),
            "mobile": is_mob,
            "screenWidth": w,
            "screenHeight": h,
            "positionX": 0,
            "positionY": 0,
            "screenOrientation": orientation,
        })
        try:
            self.call("Emulation.setTouchEmulationEnabled", {
                "enabled": is_mob,
                "maxTouchPoints": 5 if is_mob else 0,
            })
        except Exception:
            pass
        try:
            self.call("Emulation.setUserAgentOverride", {
                "userAgent": _MOBILE_USER_AGENT if is_mob else _DESKTOP_USER_AGENT,
                "platform": "Linux armv81" if is_mob else "Win32",
                "userAgentMetadata": _MOBILE_UA_METADATA if is_mob else _DESKTOP_UA_METADATA,
            })
        except Exception:
            pass
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
                    scroll_js = f"""
(function() {{
    var sel = {json.dumps(selector)};
    var el = null;
    try {{ el = document.querySelector(sel); }} catch (e) {{}}
    if (!el) {{
        var cands = document.querySelectorAll("button, a, [role='button'], input, label, [data-pick], [data-field]");
        var lowerSel = sel.toLowerCase().replace(/[\\[\\]'"]/g, "").trim();
        for (var i = 0; i < cands.length; i++) {{
            var c = cands[i];
            var p = (c.getAttribute("data-pick") || c.getAttribute("data-field") || c.getAttribute("aria-label") || c.innerText || "").toLowerCase().trim();
            if (p === lowerSel || lowerSel.includes(p) || p.includes(lowerSel)) {{
                el = c;
                break;
            }}
        }}
    }}
    if (el) {{
        try {{ el.scrollIntoView({{ behavior: "instant", block: "center", inline: "center" }}); }} catch (e) {{}}
        return true;
    }}
    return false;
}})()
"""
                    found = bool(self.evaluate(scroll_js))
                    if not found:
                        raise ValueError(f"Cannot perform visual action 'scroll' on {selector}")
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
            # If selector is given, resolve coordinates with polling and scroll target into view
            if selector and (cx is None or cy is None):
                resolve_js = f"""
(function() {{
    var sel = {json.dumps(selector)};
    var el = null;
    try {{ el = document.querySelector(sel); }} catch (e) {{}}
    if (!el) {{
        var cands = document.querySelectorAll("button, a, [role='button'], input, label, [data-pick], [data-field]");
        var lowerSel = sel.toLowerCase().replace(/[\\[\\]'"]/g, "").trim();
        for (var i = 0; i < cands.length; i++) {{
            var c = cands[i];
            var p = (c.getAttribute("data-pick") || c.getAttribute("data-field") || c.getAttribute("aria-label") || c.innerText || "").toLowerCase().trim();
            if (p === lowerSel || lowerSel.includes(p) || p.includes(lowerSel)) {{
                el = c;
                break;
            }}
        }}
    }}
    if (!el) return null;
    try {{ el.scrollIntoView({{ behavior: "instant", block: "center", inline: "center" }}); }} catch (e) {{}}
    var r = el.getBoundingClientRect();
    return {{
        x: r.left + r.width / 2,
        y: r.top + r.height / 2,
        width: r.width,
        height: r.height,
        tag: (el.tagName || "").toLowerCase()
    }};
}})()
"""
                deadline = time.time() + 1.2
                pt = None
                while time.time() < deadline:
                    pt = self.evaluate(resolve_js)
                    if isinstance(pt, dict) and pt.get("x") is not None:
                        break
                    if self._ws is not None:
                        self.drain_events(80)
                    else:
                        break

                if isinstance(pt, dict) and pt.get("x") is not None and float(pt.get("width") or 0) > 0 and float(pt.get("height") or 0) > 0:
                    cx, cy = float(pt["x"]), float(pt["y"])

            if kind == "click":
                clicked = False
                if cx is not None and cy is not None:
                    self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": float(cx), "y": float(cy)})
                    self.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": float(cx), "y": float(cy), "button": "left", "clickCount": 1})
                    self.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": float(cx), "y": float(cy), "button": "left", "clickCount": 1})
                    self.drain_events(250)
                    clicked = True
                if selector and not clicked:
                    dom_click_js = f"""
(function() {{
    try {{
        var el = document.querySelector({json.dumps(selector)});
        if (!el) {{
            var cands = document.querySelectorAll("button, a, [role='button'], input, label, [data-pick], [data-field]");
            var lowerSel = {json.dumps(selector)}.toLowerCase().replace(/[\\[\\]'"]/g, "").trim();
            for (var i = 0; i < cands.length; i++) {{
                var c = cands[i];
                var p = (c.getAttribute("data-pick") || c.getAttribute("data-field") || c.getAttribute("aria-label") || c.innerText || "").toLowerCase().trim();
                if (p === lowerSel || lowerSel.includes(p) || p.includes(lowerSel)) {{
                    el = c;
                    break;
                }}
            }}
        }}
        if (el) {{
            try {{ el.scrollIntoView({{ behavior: "instant", block: "center", inline: "center" }}); }} catch (e) {{}}
            if (typeof el.focus === "function") el.focus();
            if (typeof el.click === "function") el.click();
            return true;
        }}
    }} catch (e) {{}}
    return false;
}})()
"""
                    dom_hit = bool(self.evaluate(dom_click_js))
                    if dom_hit:
                        clicked = True
                    self.drain_events(150)
                if clicked:
                    log_lines.append(f"click({selector or f'{int(cx)},{int(cy)}'})")
                else:
                    raise ValueError(f"Cannot perform visual action 'click' on {selector or (cx, cy)}")
            elif kind == "hover":
                if cx is not None and cy is not None:
                    self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": float(cx), "y": float(cy)})
                    self.drain_events(200)
                    log_lines.append(f"hover({selector or f'{int(cx)},{int(cy)}'})")
                else:
                    raise ValueError(f"Cannot perform visual action 'hover' on {selector or (cx, cy)}")
            elif kind == "drag":
                if cx is not None and cy is not None:
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
                else:
                    raise ValueError(f"Cannot perform visual action 'drag' on {selector or (cx, cy)}")
            elif kind == "type":
                text = str(act.get("text") or "")
                focused = False
                if selector:
                    focus_js = f"""
(function() {{
    try {{
        var e = document.querySelector({json.dumps(selector)});
        if (e) {{
            e.scrollIntoView({{ behavior: "instant", block: "center", inline: "center" }});
            e.focus();
            return document.activeElement === e;
        }}
    }} catch (err) {{}}
    return false;
}})()
"""
                    focused = bool(self.evaluate(focus_js))
                if selector and not focused:
                    raise ValueError(f"Cannot perform visual action 'type' on {selector}")
                if text:
                    self.call("Input.insertText", {"text": text})
                    self.drain_events(150)
                    log_lines.append(f"type({selector or 'focused'})")
                else:
                    log_lines.append(f"type({selector or 'focused'}) [no text]")
            else:
                log_lines.append(f"action({kind}) [unrecognized action kind]")
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


def _audit_rendered_pixels_for_contrast(
    png_bytes: bytes,
    audit: Dict[str, Any],
    *,
    tile_tag: str = "",
) -> None:
    """Measure true WCAG contrast and canvas dark/light splits from rendered PNG pixels behind DOM text boxes."""
    if not png_bytes or not isinstance(audit, dict):
        return
    try:
        from PIL import Image  # type: ignore

        def _ch(v: float) -> float:
            s = max(0.0, min(255.0, float(v))) / 255.0
            return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

        def _rel_luma(rgb: Tuple[int, int, int]) -> float:
            return 0.2126 * _ch(rgb[0]) + 0.7152 * _ch(rgb[1]) + 0.0722 * _ch(rgb[2])

        def _wcag_contrast(a: Tuple[int, int, int], b: Tuple[int, int, int]) -> float:
            la, lb = _rel_luma(a), _rel_luma(b)
            hi, lo = (la, lb) if la >= lb else (lb, la)
            return (hi + 0.05) / (lo + 0.05)

        with Image.open(io.BytesIO(png_bytes)) as raw_im:
            im = raw_im.convert("RGB")
            img_w, img_h = im.size
            if img_w < 8 or img_h < 8:
                return
            px = im.load()

            vp = audit.get("viewport") or {}
            vw = float(vp.get("width") or img_w)
            vh = float(vp.get("height") or img_h)
            if vw <= 0 or vh <= 0:
                return
            scale = img_w / vw

            canvases = audit.get("canvases") or ()
            has_large_canvas = any(
                isinstance(c, dict)
                and float(c.get("width") or 0) >= vw * 0.55
                and float(c.get("height") or 0) >= vh * 0.55
                for c in canvases
            )

            existing_lc: List[str] = list(audit.get("low_contrast_texts") or [])
            text_boxes = audit.get("text_boxes") or ()
            for tb in text_boxes:
                if len(existing_lc) >= 6:
                    break
                if not isinstance(tb, dict):
                    continue
                fg_raw = tb.get("fg")
                if not isinstance(fg_raw, (list, tuple)) or len(fg_raw) < 3:
                    continue
                fg_rgb = (int(fg_raw[0]), int(fg_raw[1]), int(fg_raw[2]))
                x0 = max(0, int(float(tb.get("x") or 0) * scale))
                y0 = max(0, int(float(tb.get("y") or 0) * scale))
                x1 = min(img_w - 1, int((float(tb.get("x") or 0) + float(tb.get("w") or 0)) * scale))
                y1 = min(img_h - 1, int((float(tb.get("y") or 0) + float(tb.get("h") or 0)) * scale))
                if x1 - x0 < 4 or y1 - y0 < 4:
                    continue

                raw_samples: List[Tuple[int, int, int]] = []
                samples: List[Tuple[int, int, int]] = []
                for gy in range(5):
                    py = min(img_h - 1, max(0, y0 + 1 + ((y1 - y0 - 2) * gy) // 4))
                    for gx in range(8):
                        pix_x = min(img_w - 1, max(0, x0 + 1 + ((x1 - x0 - 2) * gx) // 7))
                        r, g, b = px[pix_x, py][:3]
                        raw_samples.append((r, g, b))
                        if abs(r - fg_rgb[0]) + abs(g - fg_rgb[1]) + abs(b - fg_rgb[2]) >= 45:
                            samples.append((r, g, b))
                # If nearly every pixel matches `fg`, text and background are virtually indistinguishable (~1:1).
                chosen_samples = samples if len(samples) >= max(3, len(raw_samples) // 3) else raw_samples
                if len(chosen_samples) < 3:
                    continue

                ratios = sorted(((_wcag_contrast(fg_rgb, bg), bg) for bg in chosen_samples), key=lambda item: item[0])
                median_ratio, median_bg = ratios[len(ratios) // 2]
                q20_ratio, q20_bg = ratios[max(0, min(len(ratios) - 1, int(len(ratios) * 0.20)))]
                font_sz = int(tb.get("fontSize") or 16)
                med_thresh = 2.5 if font_sz >= 28 else 3.0

                rep_ratio: Optional[float] = None
                rep_bg: Tuple[int, int, int] = median_bg
                if median_ratio < med_thresh:
                    rep_ratio, rep_bg = median_ratio, median_bg
                elif has_large_canvas and q20_ratio < 2.2:
                    rep_ratio, rep_bg = q20_ratio, q20_bg

                if rep_ratio is not None:
                    bg_hex = f"{rep_bg[0]:02x}{rep_bg[1]:02x}{rep_bg[2]:02x}"
                    txt_prev = str(tb.get("text") or "")[:24]
                    msg = f'{tile_tag}{tb.get("selector", "el")} (rendered contrast {rep_ratio:.1f}:1 on #{bg_hex} "{txt_prev}")'
                    if msg not in existing_lc:
                        existing_lc.append(msg)

            if existing_lc:
                audit["low_contrast_texts"] = existing_lc[:6]

            if has_large_canvas and text_boxes:
                def _band_median_luma(y_lo_frac: float, y_hi_frac: float) -> float:
                    vals: List[float] = []
                    y_a = max(0, int(img_h * y_lo_frac))
                    y_b = min(img_h - 1, max(y_a + 1, int(img_h * y_hi_frac)))
                    x_a = max(0, int(img_w * 0.10))
                    x_b = min(img_w - 1, max(x_a + 1, int(img_w * 0.90)))
                    for ry in range(4):
                        py = y_a + ((y_b - y_a) * ry) // 3
                        for rx in range(12):
                            pix_x = x_a + ((x_b - x_a) * rx) // 11
                            r, g, b = px[pix_x, py][:3]
                            vals.append(0.2126 * r + 0.7152 * g + 0.0722 * b)
                    vals.sort()
                    return vals[len(vals) // 2] if vals else 128.0

                upper_med = _band_median_luma(0.12, 0.24)
                lower_med = _band_median_luma(0.65, 0.82)
                dark_band = ""
                if upper_med < 28.0 and lower_med > 165.0:
                    dark_band = "upper"
                elif lower_med < 28.0 and upper_med > 165.0:
                    dark_band = "lower"

                if dark_band:
                    has_dark_text_conflict = False
                    for tb in text_boxes:
                        if not isinstance(tb, dict):
                            continue
                        fg_raw = tb.get("fg")
                        if not isinstance(fg_raw, (list, tuple)) or len(fg_raw) < 3:
                            continue
                        tb_y = float(tb.get("y") or 0)
                        tb_h = float(tb.get("h") or 0)
                        in_band = (tb_y < vh * 0.35) if dark_band == "upper" else (tb_y + tb_h > vh * 0.55)
                        if in_band and _rel_luma((int(fg_raw[0]), int(fg_raw[1]), int(fg_raw[2]))) < 0.20:
                            has_dark_text_conflict = True
                            break
                    if has_dark_text_conflict:
                        warnings = list(audit.get("canvas_palette_warnings") or [])
                        warn = (
                            f"{tile_tag}Harsh dark/light canvas split (upper luma={upper_med:.0f} vs "
                            f"lower luma={lower_med:.0f}) conflicts with dark text — match scene.background, FogExp2, and CSS --bg"
                        )
                        if warn not in warnings:
                            warnings.append(warn)
                        audit["canvas_palette_warnings"] = warnings[:3]
    except Exception:
        return


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


def _maybe_prepare_bundled_dist(base_dir: Path) -> Optional[Path]:
    """For TypeScript/Vite projects (`index.html` referencing `.ts`/`.tsx`), auto-build `dist/` when needed."""
    index_html = base_dir / "index.html"
    if not index_html.is_file():
        return None
    try:
        html_txt = index_html.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    ts_m = re.search(r"""<script\b[^>]*\bsrc=["']([^"']+\.tsx?)["']""", html_txt, re.I)
    if not ts_m and not ((base_dir / "tsconfig.json").is_file() and (base_dir / "dist" / "index.html").is_file()):
        return None
    dist_dir = base_dir / "dist"
    dist_index = dist_dir / "index.html"
    if ts_m:
        ts_rel = ts_m.group(1).strip().lstrip("/").lstrip("./")
        ts_entry = base_dir / ts_rel
        if not ts_entry.is_file() and not dist_index.is_file():
            raise FileNotFoundError(
                f"No index.html found in directory: {base_dir} (TypeScript entry '{ts_rel}' not built to dist/index.html yet)"
            )
    has_vite = (base_dir / "node_modules" / "vite").exists() or (base_dir / "node_modules" / ".bin" / "vite").exists()
    if has_vite:
        need_build = not dist_index.is_file()
        if not need_build:
            try:
                dist_mtime = dist_index.stat().st_mtime_ns
                for sub in ("src", "styles", "public"):
                    sub_dir = base_dir / sub
                    if sub_dir.is_dir():
                        for p in sub_dir.rglob("*"):
                            if p.is_file() and p.stat().st_mtime_ns > dist_mtime:
                                need_build = True
                                break
                if index_html.stat().st_mtime_ns > dist_mtime:
                    need_build = True
            except OSError:
                pass
        if need_build:
            npx_bin = shutil.which("npx") or "npx"
            try:
                subprocess.run(
                    [npx_bin, "vite", "build", "--base=./"],
                    cwd=str(base_dir),
                    capture_output=True,
                    text=True,
                    timeout=90,
                    check=False,
                )
            except Exception:
                pass
    if dist_index.is_file():
        return dist_dir.resolve()
    return None


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
        dist_root = _maybe_prepare_bundled_dist(cand)
        if dist_root is not None:
            dist_idx = dist_root / "index.html"
            return "index.html", dist_root, f"local:{dist_idx}"
        index_html = cand / "index.html"
        if not index_html.is_file():
            raise FileNotFoundError(f"No index.html found in directory: {cand}")
        return index_html.relative_to(root).as_posix(), root, f"local:{index_html}"
    if cand.is_file():
        if cand.name.lower() == "index.html":
            dist_root = _maybe_prepare_bundled_dist(cand.parent)
            if dist_root is not None:
                dist_idx = dist_root / "index.html"
                return "index.html", dist_root, f"local:{dist_idx}"
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

    vp_list = _normalize_viewports(viewports)
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
                _audit_rendered_pixels_for_contrast(clean_png, audit)
                pix = analyze_screenshot_pixels(clean_png)
                if idx == 0:
                    primary_png = clean_png
                lbl = f"{vname.upper()} {vw}x{vh}" + (" [SoM]" if want_som else "")
                tiles.append((lbl, display_png))
                sh_val = int(((audit.get("scroll") or {}).get("height") if isinstance(audit, dict) else 0) or 0)
                if idx == 0 and want_som and not actions and not full_page and sh_val >= int(vh * 2.0):
                    mid_y = max(int(vh * 0.6), int((sh_val - vh) * 0.45))
                    cdp.evaluate(f"(function(){{ window.scrollTo(0, {mid_y}); return window.scrollY; }})()")
                    cdp.drain_events(200)
                    mid_audit = cdp.evaluate(f"{_AUDIT_AND_SOM_JS}(false, 12, {int(vw)})") or {}
                    mid_png = cdp.screenshot_png(full_page=False)
                    _audit_rendered_pixels_for_contrast(mid_png, mid_audit, tile_tag="mid-scroll: ")
                    if isinstance(audit, dict) and isinstance(mid_audit, dict):
                        for a_key, cap_n in (
                            ("low_contrast_texts", 6),
                            ("header_bleeds", 3),
                            ("canvas_palette_warnings", 3),
                            ("clipped_texts", 8),
                            ("occluded_controls", 8),
                        ):
                            merged = list(dict.fromkeys([*(audit.get(a_key) or []), *(mid_audit.get(a_key) or [])]))
                            if merged:
                                audit[a_key] = merged[:cap_n]
                    tiles.append((f"{vname.upper()} MID-SCROLL {vw}x{vh}", mid_png))
                    cdp.evaluate("(function(){ window.scrollTo(0, 0); return true; })()")
                    cdp.drain_events(80)
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

            console_errors, suppressed_webgl_notices = _split_webgl_disabled_errors(
                list(cdp.console_errors),
                bool(disable_webgl),
            )
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
            "suppressed_webgl_notices": suppressed_webgl_notices,
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
        if audit.get("low_contrast_texts"):
            status_flags.append(f"LOW_CONTRAST={len(audit['low_contrast_texts'])}")
        if audit.get("header_bleeds"):
            status_flags.append(f"HEADER_BLEED={len(audit['header_bleeds'])}")
        if audit.get("canvas_palette_warnings"):
            status_flags.append("CANVAS_PALETTE_MISMATCH")
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
        if audit.get("low_contrast_texts"):
            lines.append(f"  Low contrast / canvas overlay: {'; '.join(audit['low_contrast_texts'][:4])}")
        if audit.get("header_bleeds"):
            lines.append(f"  Header bleed: {'; '.join(audit['header_bleeds'][:3])}")
        if audit.get("canvas_palette_warnings"):
            lines.append(f"  Canvas/palette mismatch: {'; '.join(audit['canvas_palette_warnings'][:2])}")
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
    suppressed = list(res.get("suppressed_webgl_notices") or [])
    if suppressed:
        lines.append(f"WebGL-Disabled Fallback Notices: {len(suppressed)} expected context-creation log(s) suppressed")
    lines.append("Visual checks: pass" if passed else "Visual checks: fail")
    return "\n".join(lines)
