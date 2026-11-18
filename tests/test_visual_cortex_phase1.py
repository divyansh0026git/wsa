"""Phase 1 verification tests: VLM image optimization and verified browser upload handshake."""
from __future__ import annotations

import base64
import io
import subprocess
from pathlib import Path

import pytest

from wsa_server import relay


def _make_png(width: int, height: int, noisy: bool = False) -> bytes:
    import random
    from PIL import Image

    if noisy:
        rng = random.Random(42)
        raw = rng.randbytes(width * height * 3)
        img = Image.frombytes("RGB", (width, height), raw)
    else:
        img = Image.new("RGBA", (width, height), (28, 32, 44, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_optimize_image_for_vlm_downscales_oversized_screenshots():
    from PIL import Image

    big_png = _make_png(2560, 1440, noisy=True)
    opt_bytes, opt_mime = relay._optimize_image_for_vlm(big_png, "image/png", max_edge=1568)
    assert opt_mime in ("image/webp", "image/jpeg", "image/png")
    with Image.open(io.BytesIO(opt_bytes)) as im:
        assert max(im.size) == 1568
        assert im.size[0] == 1568 and im.size[1] == 882
    assert len(opt_bytes) < len(big_png) // 2


def test_optimize_image_for_vlm_keeps_small_images_untouched():
    small_png = _make_png(320, 200, noisy=False)
    opt_bytes, opt_mime = relay._optimize_image_for_vlm(small_png, "image/png")
    assert opt_bytes == small_png
    assert opt_mime == "image/png"


def test_serialize_relay_images_optimizes_and_encodes():
    big_png = _make_png(2400, 1200, noisy=True)
    out = relay._serialize_relay_images([(big_png, "image/png")])
    assert len(out) == 1
    item = out[0]
    assert item["mime_type"] in ("image/webp", "image/jpeg", "image/png")
    decoded = base64.b64decode(item["data"])
    assert len(decoded) < len(big_png) // 2


def test_background_js_contains_verified_upload_handshake_for_all_providers():
    bg_path = Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js"
    src = bg_path.read_text(encoding="utf-8")
    assert src.count("[IMAGE_ATTACH_FAILED]") >= 3
    assert "hasActiveUploadSpinner" in src
    assert "hasUploadSpinner" in src
    assert "DragEvent" in src
    chk = subprocess.run(["node", "--check", str(bg_path)], capture_output=True, text=True, check=False)
    assert chk.returncode == 0, chk.stderr
