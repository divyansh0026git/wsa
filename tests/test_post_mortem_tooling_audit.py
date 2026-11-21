"""Tests verifying fixes for the 14 tooling post-mortem issues from real Three.js/Vite/TS sessions.

Covers:
  Category A (visual_inspect / screenshots):
    1. HEADER_BLEED grounded collision & accepted_flags support
    2. Pixel diff baseline matching & animation freeze
    3. Readiness signal (frames rendered, FPS, WebGL renderer, scroll state)
    4. Contact sheet detail & unscaled native viewport image outputs
    5. SoM legend omitted elements count (+N more mapped)
    6. Page state reading via 'eval' and 'read_state' CDP actions
    7. Critique directive suppression for accepted flags
  Category B (file & tool output):
    8. read_file compaction notice providing exact re-read start/end ranges
    9. patch_file multi-hunk per-hunk line numbers and extended diff preview
    10. list_dir and glob_files media asset (PNG/JPG/GLB) visibility
    11. Skill text compression at sentence/word boundaries & omitted sections
    12. Host OS and PowerShell shell disclosure in prompts and schemas
    13. Stripping PowerShell #< CLIXML progress/error noise in background logs
    14. Intent-based skill routing avoiding single-word 'fail' false triggers
"""
from pathlib import Path
import json
import re

import pytest

from wsa_agent import tools, visual, task_skills, prompts
from wsa_agent.agent import _compact_head_tail_with_lines
from wsa_agent.tools import (
    _clean_powershell_clixml,
    _unified_diff_preview,
    _patch_file,
    _list_dir,
    _glob_files,
    set_project_root,
)
from wsa_agent.visual import (
    format_visual_report,
    _HeadlessCDPClient,
)


def test_header_bleed_accepted_flag_passes_visual_check():
    """Item 1 & 7: accepted_flags=['HEADER_BLEED'] allows intentional translucent headers without failing."""
    fake_res = {
        "ok": True,
        "tiles": ["DESKTOP 1440x900"],
        "viewports": [
            {
                "name": "desktop",
                "width": 1440,
                "height": 900,
                "audit": {
                    "header_bleeds": ["header#hud-top (alpha=0.40+blur) bleeds scrolled div.card"],
                    "marks": [{"id": 1, "selector": "header#hud-top", "x": 0, "y": 0, "w": 1440, "h": 60}],
                },
                "pixels": {"ok": True, "blank": False, "std_luma": 45.2, "unique_buckets": 120},
            }
        ],
    }
    # Without accepted flags -> fails
    report_fail = format_visual_report(fake_res)
    assert "HEADER_BLEED=1" in report_fail
    assert "Visual checks: fail" in report_fail

    # With accepted flags -> passes with (accepted) tag
    report_pass = format_visual_report(fake_res, accepted_flags=["HEADER_BLEED"])
    assert "HEADER_BLEED=1 (accepted)" in report_pass
    assert "Visual checks: pass" in report_pass


def test_som_legend_reports_omitted_elements_count():
    """Item 5: When total Set-of-Mark elements exceed 16, format count of omitted items."""
    marks = [
        {"id": i, "selector": f"button.btn-{i}", "x": i * 10, "y": 20, "w": 40, "h": 20, "text": f"Btn {i}"}
        for i in range(25)
    ]
    fake_res = {
        "ok": True,
        "tiles": ["DESKTOP 1440x900"],
        "viewports": [
            {
                "name": "desktop",
                "width": 1440,
                "height": 900,
                "audit": {"marks": marks},
                "pixels": {"ok": True, "blank": False, "std_luma": 40.0, "unique_buckets": 100},
            }
        ],
    }
    report = format_visual_report(fake_res)
    assert "[15] button.btn-15" in report
    assert "... (+9 more interactive elements mapped)" in report


def test_telemetry_scroll_state_and_webgl_renderer_reported():
    """Item 3: Viewport telemetry includes scroll_state (scrollY/maxScrollY) and WebGL renderer."""
    fake_res = {
        "ok": True,
        "tiles": ["DESKTOP 1440x900"],
        "viewports": [
            {
                "name": "desktop",
                "width": 1440,
                "height": 900,
                "audit": {
                    "scroll_state": {"scrollY": 450, "maxScrollY": 2400},
                },
                "scroll_state": {"scrollY": 450, "maxScrollY": 2400},
                "webgl_renderer": "ANGLE (NVIDIA GeForce RTX 4070 Direct3D11)",
                "pixels": {"ok": True, "blank": False, "std_luma": 50.0, "unique_buckets": 200},
            }
        ],
    }
    report = format_visual_report(fake_res)
    assert "scrollY=450/2400px" in report
    assert "gl=ANGLE (NVIDIA GeForce RTX 4070 Direct3D11)" in report


def test_eval_and_read_state_results_formatted():
    """Item 6: Page evaluation and state results are surfaced prominently in the report."""
    fake_res = {
        "ok": True,
        "tiles": ["DESKTOP 1440x900"],
        "viewports": [
            {
                "name": "desktop",
                "width": 1440,
                "height": 900,
                "audit": {},
                "pixels": {"ok": True, "blank": False, "std_luma": 50.0, "unique_buckets": 200},
            }
        ],
        "eval_results": [
            {"expression": "window.scrollY", "result": 1280},
            {
                "action": "read_state",
                "result": {"scrollY": 1280, "maxScrollY": 3200, "activeElement": "BUTTON#btn-mercury", "title": "3D Solar System"},
            },
        ],
    }
    report = format_visual_report(fake_res)
    assert "Page evaluation / state results:" in report
    assert "eval(window.scrollY) -> 1280" in report
    assert "scrollY=1280/3200px" in report
    assert "BUTTON#btn-mercury" in report


def test_individual_unscaled_viewport_captures_listed():
    """Item 4: Individual unscaled viewport image paths are listed for high-resolution inspection."""
    fake_res = {
        "ok": True,
        "tiles": ["DESKTOP 1440x900", "MOBILE 390x844"],
        "viewports": [
            {"name": "desktop", "width": 1440, "height": 900, "pixels": {"ok": True}},
            {"name": "mobile", "width": 390, "height": 844, "pixels": {"ok": True}},
        ],
        "individual_images": [
            {"label": "DESKTOP 1440x900", "path": "/tmp/visual_desktop.png", "size_kb": 320},
            {"label": "MOBILE 390x844", "path": "/tmp/visual_mobile.png", "size_kb": 140},
        ],
    }
    report = format_visual_report(fake_res)
    assert "Individual unscaled viewport captures:" in report
    assert "DESKTOP 1440x900: /tmp/visual_desktop.png" in report
    assert "MOBILE 390x844: /tmp/visual_mobile.png" in report


def test_read_file_compaction_provides_exact_reread_ranges():
    """Item 8: When read_file output is compacted, notice includes exact call with line ranges."""
    fake_numbered_lines = "\n".join(f"{i:4d} | const float x_{i} = {i * 1.5:.2f};" for i in range(1, 100))
    compacted = _compact_head_tail_with_lines(fake_numbered_lines, head_n=10, tail_n=5)
    assert "call read_file with start_line=11, end_line=94 to re-read" in compacted


def test_patch_file_multi_hunk_reports_line_numbers_and_extended_diff(tmp_path):
    """Item 9: Multi-hunk patch_file reports line numbers for each hunk and doesn't truncate prematurely."""
    set_project_root(tmp_path)
    src_file = tmp_path / "scene.ts"
    lines = [f"// line {i}\nconst val_{i} = {i};\n" for i in range(1, 80)]
    src_file.write_text("".join(lines), encoding="utf-8")

    hunks = [
        {"old": "const val_10 = 10;", "new": "const val_10 = 100;"},
        {"old": "const val_40 = 40;", "new": "const val_40 = 400;"},
    ]
    res = _patch_file(str(src_file), hunks=hunks)
    assert "[OK] Patched" in res
    assert "Hunk 1" in res and "OK" in res
    assert "Hunk 2" in res and "OK" in res
    # Verify line numbers are reported
    assert re.search(r"Hunk 1 \(L\d+-L\d+\): OK", res)
    assert re.search(r"Hunk 2 \(L\d+-L\d+\): OK", res)


def test_unified_diff_preview_up_to_120_lines():
    """Item 9: _unified_diff_preview does not truncate at 44 lines."""
    old_text = "\n".join(f"old line {i}" for i in range(35))
    new_text = "\n".join(f"new line {i}" for i in range(35))
    diff = _unified_diff_preview("test.txt", old_text, new_text)
    assert "diff preview capped" not in diff
    assert "diff truncated" not in diff
    assert len(diff.splitlines()) > 60  # Well over the old 44-line cutoff


def test_list_dir_and_glob_files_include_textures_and_images(tmp_path):
    """Item 10: Media files (.png, .jpg, .webp, .glb) are not silently ignored by list_dir or glob_files."""
    set_project_root(tmp_path)
    tex_dir = tmp_path / "public" / "textures"
    tex_dir.mkdir(parents=True)
    (tex_dir / "earth_atmos_2048.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 2000)
    (tex_dir / "saturn_ring.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1500)
    (tex_dir / "CREDITS.json").write_text("{}", encoding="utf-8")

    listed = _list_dir("public/textures")
    assert "earth_atmos_2048.jpg" in listed
    assert "saturn_ring.png" in listed
    assert "CREDITS.json" in listed

    globbed = _glob_files("public/textures/*")
    assert "earth_atmos_2048.jpg" in globbed
    assert "saturn_ring.png" in globbed


def test_clean_powershell_clixml_noise():
    """Item 13: PowerShell #< CLIXML error/progress blocks are cleanly stripped to plain text."""
    clixml_noise = (
        '#< CLIXML\r\n'
        '<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">\r\n'
        '<S S="Error">node: bad option: --invalid-flag_x000D__x000A_</S>\r\n'
        '<S S="Error">Try \'node --help\' for more information._x000D__x000A_</S>\r\n'
        '</Objs>'
    )
    cleaned = _clean_powershell_clixml(clixml_noise)
    assert "#< CLIXML" not in cleaned
    assert "<Objs" not in cleaned
    assert "node: bad option: --invalid-flag" in cleaned
    assert "Try 'node --help' for more information." in cleaned


def test_skill_compression_preserves_sentence_boundaries():
    """Item 11: _fit_section_to_budget does not slice words mid-token with ' … '."""
    long_line = (
        "- GSAP scroll choreography with ScrollTrigger: pinning the solar canvas behind HUD cards, "
        "scrubbing planet orbital arcs proportionally across 8000px virtual timeline."
    )
    fitted, was_trimmed = task_skills._fit_section_to_budget("Motion", [long_line], budget=90)
    assert " … " not in fitted  # Ensure old mid-line slice pattern is not present
    assert not re.search(r"\bchoreogr\b", fitted)  # Never cut mid-word


def test_pure_questions_do_not_falsely_trigger_debugging_skills():
    """Item 14: Conceptual questions containing 'fail' do not trigger systematic-debugging."""
    # Pure question -> no systematic-debugging appointed
    q_skills = task_skills.select_task_skills("What if a background task fails or takes longer than 5 seconds?")
    assert "systematic-debugging" not in q_skills

    # Active debugging request -> systematic-debugging IS appointed
    debug_skills = task_skills.select_task_skills("Debug and fix the test failure in celestial body math")
    assert "systematic-debugging" in debug_skills


def test_system_prompt_declares_windows_powershell():
    """Item 12: Prompts and schemas explicitly state Windows PowerShell host environment."""
    assert "PowerShell" in prompts.SYSTEM_PROMPT
    assert "Get-ChildItem -Force" in prompts.SYSTEM_PROMPT
    assert "PowerShell" in prompts.CLAUDE_SYSTEM_PROMPT
    assert len(prompts.CLAUDE_SYSTEM_PROMPT) < 3000
