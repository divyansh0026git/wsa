"""
Pre-Orchestrator Visual Concept Mockup & Approval Gate.

Generates UI/dashboard/website visual concept mockups via ChatGPT (direct prompt)
or Gemini (Create image selection) before Orchestrator planning starts, pauses for
explicit user confirmation (`y` continue, `n` refine/regenerate, `m` add UI part,
`s` skip), saves approved mockups in `.wsa_concepts/`, and preserves both the
images and visual context across every stage and account/provider switch.
"""
from __future__ import annotations

import base64
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from rich.markup import escape
from rich.panel import Panel

from . import renderer as R
from .session import get_model_info, normalize_web_model

IMAGE_GEN_PROVIDERS = ("chatgpt", "gemini")

_FRONTEND_VISUAL_RE = re.compile(
    r"\b(?:website|web\s*site|web\s*app|webapp|landing\s*page|ui|ux|dashboard|admin\s*panel|"
    r"interface|frontend|front[- ]end|portfolio|storefront|e[- ]?commerce|saas|"
    r"3d\s+(?:site|website|scene|experience|viewer|solar|space|showcase)|"
    r"mobile\s*app|ios\s*app|android\s*app|macos\s*app|windows\s*app|desktop\s*app|gui)\b",
    re.IGNORECASE,
)
_DESIGN_BUILD_RE = re.compile(
    r"\b(?:build|create|make|design|redesign|revamp|overhaul|develop|style|craft|mock|mockup|wireframe|recreate)\b",
    re.IGNORECASE,
)
_PURE_INFO_RE = re.compile(
    r"^\s*(?:explain|what\s+is|what\s+are|why\s+does|how\s+does|summarize|read|check|list|run\s+pytest)\b",
    re.IGNORECASE,
)


def is_frontend_visual_prompt(
    user_input: str,
    appointment: Optional[Any] = None,
) -> bool:
    """Return True when the prompt asks to design, build, or overhaul a visual UI/frontend."""
    txt = str(user_input or "").strip()
    if not txt or _PURE_INFO_RE.match(txt):
        return False
    if appointment is not None:
        cats = set(getattr(appointment, "categories", ()) or ())
        if bool(getattr(appointment, "is_website_build", False)) or bool(
            cats & {"UI & Anti-Slop", "3D, Shaders & Motion", "Website Build"}
        ):
            return True
    has_visual = bool(_FRONTEND_VISUAL_RE.search(txt))
    has_action = bool(_DESIGN_BUILD_RE.search(txt))
    return has_visual and has_action


def select_image_gen_provider(state: Any) -> Tuple[Optional[str], str]:
    """
    Choose which connected web provider generates UI concept images.

    Only ``chatgpt`` (direct text prompt) and ``gemini`` (Create image tool)
    support web image generation. Returns ``(provider, mode)`` where ``mode`` is:
      - ``"direct"`` for active ChatGPT
      - ``"select_tool"`` for Gemini (requires Create image tool selection)
      - ``"unavailable"`` when neither ChatGPT nor Gemini is active/open
    """
    active = normalize_web_model(getattr(state, "model", "") or getattr(state, "selected_model", "")) or "gemini"
    providers_info = getattr(state, "providers_info", None) or {}
    queue = [
        p for p in (getattr(state, "multiagent_queue", None) or [])
        if normalize_web_model(p) in IMAGE_GEN_PROVIDERS
    ]

    def _is_available(pid: str) -> bool:
        if pid == active:
            return True
        info = providers_info.get(pid) if isinstance(providers_info, dict) else None
        if isinstance(info, dict) and info.get("has_tab"):
            return True
        return normalize_web_model(pid) in [normalize_web_model(q) for q in queue]

    if active == "chatgpt":
        return "chatgpt", "direct"
    if active == "gemini":
        return "gemini", "select_tool"
    if _is_available("chatgpt"):
        return "chatgpt", "direct"
    if _is_available("gemini"):
        return "gemini", "select_tool"
    return None, "unavailable"


def plan_concept_views(user_input: str) -> List[Dict[str, str]]:
    """Suggest up to 3 focused UI/dashboard/website parts worth previewing before coding."""
    low = str(user_input or "").lower()
    if any(k in low for k in ("dashboard", "admin", "analytics", "crm", "telemetry", "saas", "panel")):
        return [
            {
                "title": "Main Dashboard & Telemetry Grid",
                "focus": "Primary workspace overview with navigation sidebar, KPI/status strip, main data visualization canvas, typography, and color palette.",
            },
            {
                "title": "Detail Drawer & Controls Panel",
                "focus": "Focused inspector drawer, filter toolbar, interactive forms/toggles, and empty/active hierarchy.",
            },
            {
                "title": "Compact / Mobile Responsive View",
                "focus": "Collapsed navigation, stacked cards/sheets, and touch-friendly controls for the same design system.",
            },
        ]
    if any(k in low for k in ("3d", "solar", "webgl", "three.js", "shader", "scene", "orb", "planet", "space")):
        return [
            {
                "title": "Hero 3D Scene & Spatial HUD",
                "focus": "Full-viewport 3D hero focal composition, lighting mood, volumetric shaders, atmospheric palette, and minimal floating HUD typography.",
            },
            {
                "title": "Focused Object Inspection & Telemetry",
                "focus": "Close-up 3D camera framing paired with the clean inspection panel/readouts and interactive body selector.",
            },
            {
                "title": "Mobile 3D Framing & Controls",
                "focus": "Portrait 3D composition keeping upper hero clear above a compact bottom sheet.",
            },
        ]
    return [
        {
            "title": "Primary Hero & Navigation View",
            "focus": "Above-the-fold layout, visual hierarchy, color system, typography pairing, and primary focal section.",
        },
        {
            "title": "Interactive Core Workspace / Feature Grid",
            "focus": "Main interactive UI components, cards/controls, states, and content structure matching the same aesthetic.",
        },
        {
            "title": "Responsive Mobile Layout",
            "focus": "Mobile viewport adaptation preserving brand typography, spacing, and accessible controls.",
        },
    ]


def build_image_gen_prompt(
    provider: str,
    user_input: str,
    view_title: str,
    view_focus: str,
    refinements: Optional[List[str]] = None,
) -> str:
    """Build a provider-tailored image-generation prompt for a UI concept view."""
    goal_clean = " ".join(str(user_input or "").split())[:900]
    refine_list = [str(r).strip() for r in (refinements or []) if str(r or "").strip()]
    refine_block = (
        " Incorporating these exact user revisions: " + " | ".join(refine_list[-5:])
        if refine_list
        else ""
    )
    core = (
        f"High-craft UI/product visual concept mockup showing ONLY '{view_title}' "
        f"for the following project: {goal_clean}. "
        f"Part to depict clearly: {view_focus}.{refine_block} "
        "Render clean realistic interface composition, crisp typography hierarchy, intentional color palette, "
        "and balanced negative space — avoid gibberish clutter, neon rainbow slop, or watermarks."
    )
    if provider == "chatgpt":
        return f"Generate an image of a crisp, studio-grade {core}"
    return f"Generate a studio-grade UI concept image: {core}"


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "view").lower()).strip("-")
    return (s or "view")[:32]


def save_generated_concept_images(
    generated_images: List[Dict[str, Any]],
    project_root: Path,
    slug: str,
) -> List[str]:
    """Decode generated base64/data_url images into `<project_root>/.wsa_concepts/` and return relative paths."""
    from .tools import detect_image_info

    if not isinstance(generated_images, list) or not generated_images:
        return []
    out_dir = Path(project_root) / ".wsa_concepts"
    out_dir.mkdir(parents=True, exist_ok=True)
    saved_rel: List[str] = []
    ts = int(time.time())
    safe_slug = _slugify(slug)
    for idx, item in enumerate(generated_images[:4], 1):
        if not isinstance(item, dict):
            continue
        durl = str(item.get("data_url") or item.get("url") or "").strip()
        if not durl.startswith("data:image/") or ";base64," not in durl:
            continue
        b64_part = durl.split(";base64,", 1)[1].strip()
        try:
            raw = base64.b64decode(b64_part, validate=False)
        except Exception:
            continue
        if len(raw) < 16:
            continue
        info = detect_image_info(raw)
        fmt = str((info or {}).get("format") or "").lower()
        if fmt not in ("png", "jpeg", "jpg", "webp", "gif"):
            continue
        ext = "jpg" if fmt == "jpeg" else fmt
        fname = f"concept_{ts}_{idx}_{safe_slug}.{ext}"
        target = out_dir / fname
        try:
            target.write_bytes(raw)
            saved_rel.append(f".wsa_concepts/{fname}")
        except OSError:
            continue
    return saved_rel


def _safe_read_choice(prompt_text: str, input_fn: Optional[Callable[[str], str]] = None) -> str:
    if input_fn is not None:
        return str(input_fn(prompt_text) or "")
    try:
        from .renderer import console as _console
        R._release_console_live(_console)
    except Exception:
        pass
    return input(prompt_text)


def prompt_visual_concept_approval(
    *,
    provider: str,
    view_title: str,
    saved_paths: List[str],
    reply_preview: str = "",
    input_fn: Optional[Callable[[str], str]] = None,
) -> Tuple[str, str]:
    """
    Display a clear approval popup asking the user to inspect the generated web image
    and choose `[y]` approve, `[n]` revise prompt, `[m]` add another UI part, or `[s]` skip.
    """
    prov_label = R.friendly_model_name(provider)
    lines = [
        f"[bold {R.Theme.PRIMARY}]Please check on the web ({escape(prov_label)}) — a UI concept image was generated from your description.[/]",
        "",
        f"  [bold]UI Part:[/] [{R.Theme.ACCENT}]{escape(view_title)}[/]",
    ]
    if saved_paths:
        lines.append(f"  [bold]Captured locally:[/] {escape(', '.join(saved_paths))}")
    else:
        lines.append("  [dim](View the generated image directly in the open browser tab)[/]")
    if provider == "gemini":
        lines.append("  [dim]Note: Gemini requires 'Create image' mode active for image generation.[/]")
    if reply_preview.strip():
        snippet = " ".join(reply_preview.split())[:220]
        lines.append(f"  [dim]Web note: {escape(snippet)}[/]")
    lines.extend([
        "",
        f"  [bold {R.Theme.OK}][y][/] Yes, continue — approve design & start Architecture Planning (locks image/context across all stages & account switches)",
        f"  [bold {R.Theme.WARN}][n][/] No, revise   — enter more prompt details so the web agent regenerates until you approve",
        f"  [bold {R.Theme.ACCENT}][m][/] More UI part — also generate a mockup for another dashboard/UI section",
        "  [dim][s][/] Skip         — proceed directly to Architecture Planning without locking a concept mockup",
    ])
    if input_fn is None:
        try:
            R._release_console_live(R.console)
            R.sync_theme()
            R.console.print()
            R.console.print(
                Panel(
                    "\n".join(lines),
                    title=f"[bold {R.Theme.PRIMARY}]Visual Concept Check (Before Architecture Planning)[/]",
                    border_style=R.Theme.ACCENT,
                    padding=(1, 2),
                )
            )
        except Exception:
            pass

    while True:
        try:
            raw = _safe_read_choice("  Approve concept? [y/n/m/s]: ", input_fn=input_fn).strip()
        except (EOFError, KeyboardInterrupt):
            return ("s", "")
        low = raw.lower()
        if low in ("y", "yes"):
            return ("y", "")
        if low in ("s", "skip"):
            return ("s", "")
        if low == "n" or low == "no" or low.startswith(("n ", "no ")):
            detail = raw.split(None, 1)[1].strip() if " " in raw else ""
            while not detail:
                try:
                    detail = _safe_read_choice(
                        "  Enter additional visual/UI details to regenerate (or 's' to skip): ",
                        input_fn=input_fn,
                    ).strip()
                except (EOFError, KeyboardInterrupt):
                    return ("s", "")
                if detail.lower() in ("s", "skip"):
                    return ("s", "")
            return ("n", detail)
        if low == "m" or low == "more" or low.startswith(("m ", "more ")):
            part = raw.split(None, 1)[1].strip() if " " in raw else ""
            if not part:
                try:
                    part = _safe_read_choice(
                        "  Which UI/dashboard part should we generate next? (e.g. 'Analytics Sidebar', 'Mobile View'): ",
                        input_fn=input_fn,
                    ).strip()
                except (EOFError, KeyboardInterrupt):
                    return ("y", "")
            return ("m", part or "Secondary Dashboard / Controls View")
        R.print_warn("Enter y (continue), n (revise prompt), m (another UI part), or s (skip).")


def format_approved_concepts_context(
    concepts: Optional[List[Dict[str, Any]]],
    *,
    max_chars: int = 2000,
) -> str:
    """Format approved visual concepts into a concise cross-stage / cross-account transfer block."""
    if not concepts:
        return ""
    lines = [
        "USER-APPROVED VISUAL UI CONCEPT (mandatory visual north star — preserve across every role, stage, model, and account change):"
    ]
    for idx, c in enumerate(concepts[-6:], 1):
        if not isinstance(c, dict):
            continue
        title = str(c.get("title") or f"View {idx}").strip()
        prov = str(c.get("provider") or "").strip()
        paths = [str(p) for p in (c.get("image_paths") or []) if p]
        prompt_note = " ".join(str(c.get("refinement_note") or c.get("focus") or "").split())[:220]
        reply_note = " ".join(str(c.get("description") or "").split())[:180]
        path_txt = f" [images: {', '.join(paths)}]" if paths else ""
        via_txt = f" (via {prov})" if prov else ""
        lines.append(f"  {idx}. {title}{via_txt}{path_txt}")
        if prompt_note:
            lines.append(f"     Spec: {prompt_note}")
        if reply_note:
            lines.append(f"     Notes: {reply_note}")
    return "\n".join(lines)[:max_chars]


def load_approved_concept_image_blocks(
    state: Any,
    *,
    max_images: int = 4,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Load saved `.wsa_concepts/*` images from `state.approved_design_concepts`
    as OpenAI `image_url` content parts + relative paths for automatic attachment.
    """
    from .tools import detect_image_info

    concepts = getattr(state, "approved_design_concepts", None) or []
    if not concepts:
        return [], []
    root = Path(getattr(state, "project_root", Path.cwd()))
    parts: List[Dict[str, Any]] = []
    loaded_paths: List[str] = []
    seen: set[str] = set()
    for c in concepts:
        if not isinstance(c, dict):
            continue
        for rel in (c.get("image_paths") or []):
            rel_s = str(rel or "").replace("\\", "/").strip()
            if not rel_s or rel_s in seen:
                continue
            seen.add(rel_s)
            p = (root / rel_s).resolve()
            try:
                p.relative_to(root.resolve())
            except ValueError:
                continue
            if not p.is_file():
                continue
            try:
                raw = p.read_bytes()
            except OSError:
                continue
            if not raw or len(raw) > 10 * 1024 * 1024:
                continue
            info = detect_image_info(raw)
            mime = str((info or {}).get("mime") or "image/png")
            b64 = base64.b64encode(raw).decode("ascii")
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
            loaded_paths.append(rel_s)
            if len(parts) >= max_images:
                return parts, loaded_paths
    return parts, loaded_paths


def _generate_single_concept(
    *,
    provider: str,
    user_input: str,
    view_title: str,
    view_focus: str,
    refinements: List[str],
    state: Any,
    client: Any,
    concept_round: int,
) -> Dict[str, Any]:
    gen_prompt = build_image_gen_prompt(provider, user_input, view_title, view_focus, refinements)
    prov_label = R.friendly_model_name(provider)
    R.print_info(f"Requesting visual concept image '{view_title}' from {prov_label}...")
    session_key = f"{getattr(state, 'session_id', 'wsa')}:role:visual_concept:{concept_round}"
    try:
        resp = client.stream(
            messages=[{"role": "user", "content": gen_prompt}],
            model=provider,
            use_tools=False,
            session_id=session_key,
            new_chat=True,
            image_gen=True,
        ) or {}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "image_paths": [], "reply": ""}

    gen_imgs = resp.get("generated_images") or []
    saved_paths = save_generated_concept_images(
        gen_imgs,
        Path(getattr(state, "project_root", Path.cwd())),
        view_title,
    )
    reply_txt = str(resp.get("content") or "").strip()
    return {
        "ok": True,
        "provider": provider,
        "title": view_title,
        "focus": view_focus,
        "prompt": gen_prompt,
        "refinement_note": " | ".join(refinements[-4:]) if refinements else view_focus,
        "image_paths": saved_paths,
        "description": reply_txt[:800],
        "captured_count": len(saved_paths),
    }


def maybe_run_visual_concept_gate(
    user_input: str,
    state: Any,
    client: Any,
    *,
    appointment: Optional[Any] = None,
    input_fn: Optional[Callable[[str], str]] = None,
) -> Dict[str, Any]:
    """
    Pre-Orchestrator Visual Concept Mockup & Approval Gate.

    Prompts before Architecture Planning when building a UI, website, or dashboard.
    Delegates image creation cleanly to ChatGPT (direct prompt) or Gemini (Create image
    tool selection) when the active agent is Claude/DeepSeek/Venice, pauses for user
    `y/n` approval until satisfied, and saves approved concept images + metadata to
    `state.approved_design_concepts` for automatic cross-account/cross-model transfer.
    """
    no_op = {"approved": False, "skipped": True, "concepts": [], "enriched_prompt": user_input}
    mode = str(getattr(state, "visual_concept_mode", "auto") or "auto").lower()
    if mode == "off" or bool(getattr(state, "skip_visual_concept", False)):
        return no_op
    if not is_frontend_visual_prompt(user_input, appointment=appointment):
        return no_op

    is_interactive = input_fn is not None or (sys.stdin.isatty() and sys.stdout.isatty())
    if not is_interactive:
        return no_op

    provider, gen_mode = select_image_gen_provider(state)
    active_model = normalize_web_model(getattr(state, "model", "") or "gemini") or "gemini"
    planned_views = plan_concept_views(user_input)

    if provider is None:
        if input_fn is None:
            R.print_warn(
                f"{R.friendly_model_name(active_model)} does not support image generation, and no open ChatGPT/Gemini tab "
                "was detected. Open ChatGPT or Gemini in Chrome/Brave and click Sync next time for UI concept previews."
            )
        return no_op

    prov_label = R.friendly_model_name(provider)
    if mode != "on":
        note_lines = [
            f"[bold {R.Theme.PRIMARY}]Generate UI / Frontend Concept Mockup(s) Before Architecture Planning?[/]",
            "",
            f"  Previewing layout mockups first prevents wasted context by locking the exact look you want before coding starts.",
            f"  [bold]Image generator:[/] [{R.Theme.ACCENT}]{escape(prov_label)}[/]"
            + (
                f" [dim](temporarily generating mockup while keeping {escape(R.friendly_model_name(active_model))} for architecture/coding)[/]"
                if provider != active_model
                else ""
            ),
            f"  [bold]How it works:[/] "
            + (
                "ChatGPT generates the image directly from a tailored prompt."
                if provider == "chatgpt"
                else "Gemini uses Create image mode (`Tools -> Create image`)."
            ),
            "  [bold]Suggested UI part:[/] " + escape(planned_views[0]["title"]),
        ]
        if input_fn is None:
            try:
                R._release_console_live(R.console)
                R.sync_theme()
                R.console.print()
                R.console.print(
                    Panel(
                        "\n".join(note_lines),
                        title=f"[bold {R.Theme.PRIMARY}]Pre-Planning UI Visual Concept Gate[/]",
                        border_style=R.Theme.BORDER,
                        padding=(1, 2),
                    )
                )
            except Exception:
                pass
        try:
            want_raw = _safe_read_choice(
                "  Generate visual concept image first? [y/n] (default y): ",
                input_fn=input_fn,
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return no_op
        if want_raw in ("n", "no", "s", "skip"):
            return no_op

    # Ensure model note for Claude-only vs ChatGPT/Gemini clarity
    _ = get_model_info(provider, state=state)

    approved_views: List[Dict[str, Any]] = []
    refinements: List[str] = []
    view_idx = 0
    cur_title = planned_views[0]["title"]
    cur_focus = planned_views[0]["focus"]
    round_counter = 0

    while True:
        round_counter += 1
        gen_res = _generate_single_concept(
            provider=provider,
            user_input=user_input,
            view_title=cur_title,
            view_focus=cur_focus,
            refinements=refinements,
            state=state,
            client=client,
            concept_round=round_counter,
        )
        if not gen_res.get("ok"):
            R.print_warn(
                f"Visual concept image generation failed on {prov_label}: {gen_res.get('error', 'unknown error')}. "
                "Proceeding directly to Architecture Planning."
            )
            break

        choice, extra = prompt_visual_concept_approval(
            provider=provider,
            view_title=cur_title,
            saved_paths=list(gen_res.get("image_paths") or []),
            reply_preview=str(gen_res.get("description") or ""),
            input_fn=input_fn,
        )
        if choice == "n":
            refinements.append(extra)
            R.print_info(f"Regenerating '{cur_title}' with your updated details...")
            continue
        if choice == "s":
            if not approved_views:
                return no_op
            break

        approved_entry = {
            "provider": provider,
            "mode": gen_mode,
            "title": cur_title,
            "focus": cur_focus,
            "refinement_note": gen_res.get("refinement_note") or "",
            "image_paths": list(gen_res.get("image_paths") or []),
            "description": str(gen_res.get("description") or ""),
        }
        approved_views.append(approved_entry)

        if choice == "m":
            view_idx += 1
            custom_name = extra.strip()
            if custom_name:
                cur_title = custom_name
                cur_focus = f"Dedicated UI mockup for '{custom_name}' matching the approved visual design system."
            elif view_idx < len(planned_views):
                cur_title = planned_views[view_idx]["title"]
                cur_focus = planned_views[view_idx]["focus"]
            else:
                cur_title = f"Additional UI Section #{view_idx + 1}"
                cur_focus = "Additional frontend/dashboard screen matching the approved design tokens."
            refinements = []
            continue

        # choice == "y"
        break

    if not approved_views:
        return no_op

    existing = list(getattr(state, "approved_design_concepts", None) or [])
    state.approved_design_concepts = [*existing, *approved_views][-8:]
    synced = getattr(state, "_concept_synced_keys", None)
    if isinstance(synced, set):
        synced.clear()
    else:
        state._concept_synced_keys = set()

    if hasattr(state, "auto_checkpoint"):
        state.auto_checkpoint()

    summary_ctx = format_approved_concepts_context(state.approved_design_concepts)
    all_paths = [p for c in state.approved_design_concepts for p in (c.get("image_paths") or [])]
    R.print_success(
        f"Locked {len(approved_views)} approved visual UI concept(s)"
        + (f" ({len(all_paths)} image file(s) attached for all stages & account switches)" if all_paths else "")
        + " — starting Architecture Planning."
    )
    enriched = (
        f"{user_input}\n\n{summary_ctx}"
        if summary_ctx
        else user_input
    )
    return {
        "approved": True,
        "skipped": False,
        "concepts": approved_views,
        "enriched_prompt": enriched,
    }
