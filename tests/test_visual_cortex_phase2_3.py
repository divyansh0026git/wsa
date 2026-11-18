"""Phase 2 & 3 live verification: headless CDP capture, telemetry, SoM, 2x2 contact sheet, and pixel diff."""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from wsa_agent import permissions, tools, visual
from wsa_agent.session import SessionState


def _require_chromium() -> str:
    exe = visual.find_chromium_binary()
    if not exe:
        pytest.skip("No Chrome/Brave/Edge binary available for live headless CDP test")
    return exe


def test_pixel_diff_preserves_threshold_and_highlight_in_each_channel():
    from PIL import Image

    def png(image):
        data = io.BytesIO()
        image.save(data, format="PNG")
        return data.getvalue()

    before = Image.new("RGB", (4, 1), (100, 100, 100))
    after = before.copy()
    after.putdata([(118, 118, 118), (119, 100, 100), (100, 81, 100), (100, 100, 119)])
    tile, changed_pct = visual.build_pixel_diff_tile(png(before), png(after))
    assert changed_pct == 75.0
    with Image.open(io.BytesIO(tile)) as highlighted:
        assert highlighted.size == (4, 1)
        assert highlighted.tobytes() == bytes((59, 59, 59) + (255, 46, 147) * 3)


def test_visual_inspect_permission_policy(tmp_path):
    smart = SessionState(project_root=tmp_path, permission_mode="smart")
    strict = SessionState(project_root=tmp_path, permission_mode="strict")
    auto = SessionState(project_root=tmp_path, permission_mode="auto")

    assert permissions.approval_reason("visual_inspect", {"target": "index.html"}, smart) == ""
    assert permissions.approval_reason("visual_inspect", {"target": "http://127.0.0.1:3000"}, smart) == ""
    assert permissions.approval_reason("visual_inspect", {"target": "https://example.com"}, smart) != ""
    assert permissions.approval_reason("visual_inspect", {"target": "index.html"}, strict) != ""
    assert permissions.approval_reason("visual_inspect", {"target": "https://example.com"}, auto) == ""


def test_live_headless_cdp_contact_sheet_som_and_pixel_diff(tmp_path):
    from PIL import Image

    _require_chromium()
    page = tmp_path / "index.html"
    page.write_text(
        """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>WSA Visual Demo</title>
  <style>
    body { margin: 0; font-family: system-ui, sans-serif; background: #10131a; color: #f4f4f6; padding: 24px; }
    .hero { display: flex; flex-wrap: wrap; gap: 20px; align-items: center; }
    button { background: #d77757; color: #fff; border: 0; padding: 12px 22px; border-radius: 8px; font-size: 16px; cursor: pointer; }
    canvas { border-radius: 12px; border: 1px solid #334155; max-width: 100%; }
  </style>
</head>
<body>
  <header><h1>Studio Speaker</h1></header>
  <main class="hero">
    <canvas id="scene" width="320" height="220"></canvas>
    <button id="cta-btn" onclick="document.body.style.background='#1d4ed8'">Switch Accent</button>
  </main>
  <script>
    const c = document.getElementById('scene');
    const ctx = c.getContext('2d');
    const g = ctx.createLinearGradient(0, 0, 320, 220);
    g.addColorStop(0, '#0ea5e9');
    g.addColorStop(1, '#f97316');
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, 320, 220);
    ctx.fillStyle = '#ffffff';
    ctx.beginPath();
    ctx.arc(160, 110, 56, 0, Math.PI * 2);
    ctx.fill();
  </script>
</body>
</html>""",
        encoding="utf-8",
    )

    prev_root = tools.get_project_root()
    tools.set_project_root(tmp_path)
    tools.consume_pending_images()
    try:
        out1 = tools.execute_tool("visual_inspect", {
            "target": "index.html",
            "actions": [{"type": "click", "selector": "#cta-btn"}],
            "annotate": True,
            "include_diff": True,
            "wait_ms": 300,
        })
        assert "[OK] Visual capture attached" in out1
        assert "Console/JS Errors: 0" in out1
        assert "OVERFLOW_X=" not in out1
        assert "BLANK/UNIFORM RENDER" not in out1
        assert "Set-of-Mark legend:" in out1
        assert "button#cta-btn" in out1
        assert "canvas#scene" in out1
        assert "AFTER INTERACTION" in out1

        imgs1 = tools.consume_pending_images()
        assert len(imgs1) == 1
        assert imgs1[0]["url"].startswith("data:image/")
        w1 = int(imgs1[0]["width"])
        assert 400 <= w1 <= 1568

        # Mutate visual appearance on disk and re-capture to verify Pixel-Diff tile
        page.write_text(
            page.read_text(encoding="utf-8").replace("#10131a", "#f8fafc").replace("#f4f4f6", "#0f172a"),
            encoding="utf-8",
        )
        out2 = tools.execute_tool("visual_inspect", {
            "target": "index.html",
            "annotate": True,
            "include_diff": True,
            "wait_ms": 300,
        })
        assert "[OK] Visual capture attached" in out2
        assert "PIXEL DIFF" in out2
        assert "Pixel diff vs previous capture:" in out2
        # Confirm non-zero pixel change was detected
        import re
        m = re.search(r"Pixel diff vs previous capture:\s*([0-9.]+)%", out2)
        assert m and float(m.group(1)) > 5.0

        imgs2 = tools.consume_pending_images()
        assert len(imgs2) == 1
        import base64
        raw = base64.b64decode(imgs2[0]["url"].split(",", 1)[1])
        with Image.open(io.BytesIO(raw)) as sheet:
            assert max(sheet.size) <= 1568
    finally:
        tools.consume_pending_images()
        tools.set_project_root(prev_root)


def test_live_headless_cdp_detects_overflow_console_error_occlusion_and_blank(tmp_path):
    _require_chromium()
    broken = tmp_path / "broken.html"
    broken.write_text(
        """<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body { margin: 0; background: #111; color: #fff; }
    .wide { width: 720px; height: 80px; background: #334; }
    .wrap { position: relative; width: 200px; height: 60px; }
    #covered-btn { position: absolute; inset: 0; z-index: 1; }
    .blocker { position: absolute; inset: 0; z-index: 5; background: rgba(255,0,0,0.5); }
  </style>
</head>
<body>
  <div class="wide">Too wide for mobile</div>
  <div class="wrap">
    <button id="covered-btn">Blocked CTA</button>
    <div class="blocker"></div>
  </div>
  <img src="/does-not-exist-404.png" width="40" height="40" />
  <script>console.error("BOOM_VISUAL_TEST");</script>
</body>
</html>""",
        encoding="utf-8",
    )

    blank = tmp_path / "blank.html"
    blank.write_text("<!doctype html><html><body style='margin:0;background:#ffffff;'></body></html>", encoding="utf-8")

    prev_root = tools.get_project_root()
    tools.set_project_root(tmp_path)
    try:
        rep = tools.execute_tool("visual_inspect", {"target": "broken.html", "include_diff": False, "wait_ms": 300})
        assert "OVERFLOW_X=+" in rep
        assert "OCCLUDED=" in rep
        assert "BROKEN_IMAGES=" in rep
        assert "BOOM_VISUAL_TEST" in rep

        rep_blank = tools.execute_tool("visual_inspect", {"target": "blank.html", "include_diff": False, "wait_ms": 200})
        assert "BLANK/UNIFORM RENDER" in rep_blank
    finally:
        tools.consume_pending_images()
        tools.set_project_root(prev_root)
