"""Offline skill catalog, vibe-coder prompt signal analyzer, compressed skill reader, and SkillSpector scanner."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


# ── Bundled Skill Registry (18 non-overlapping top-pick skills across 5 domains) ──

CATEGORY_LABELS: Dict[str, str] = {
    "3d-motion": "3D, Shaders & Motion",
    "ui-design": "UI & Anti-Slop",
    "debugging-qa": "Debugging & Browser QA",
    "security": "Website Security & Skill Safety",
    "engineering": "Core Engineering",
}


@dataclass(frozen=True)
class SkillSpec:
    name: str
    category: str
    tagline: str
    source: str
    excerpt_sections: Tuple[str, ...] = ()


SKILL_SPECS: Tuple[SkillSpec, ...] = (
    # 1. 3D, Shaders & Scroll Motion
    SkillSpec(
        name="dream-loop",
        category="3d-motion",
        tagline="Visual quality critic loop: lock target look first, critique live renders until lighting & composition match",
        source="achimala/dream-loop",
    ),
    SkillSpec(
        name="img2threejs",
        category="3d-motion",
        tagline="8-pass vision-verified procedural Three.js hero asset builder (clean THREE.Group factory, no crude primitives)",
        source="img2threejs/img2threejs",
    ),
    SkillSpec(
        name="gsap-skills",
        category="3d-motion",
        tagline="GSAP scroll-scrubbed timelines, ScrollTrigger pinned chapters, GPU-only motion, and gsap.context() cleanup",
        source="greensock/gsap-skills",
    ),
    SkillSpec(
        name="threejs-skills",
        category="3d-motion",
        tagline="Modern Three.js r150+ PBR materials, lighting, GLSL shaders, resize handling, and strict GPU disposal",
        source="CloudAI-X/threejs-skills",
    ),
    # 2. UI & UX Design (Anti-Slop)
    SkillSpec(
        name="frontend-design",
        category="ui-design",
        tagline="Distinctive aesthetic baseline: bold typography, intentional layout direction, zero templated AI defaults",
        source="anthropics/skills/frontend-design",
        excerpt_sections=("Design principles", "Restraint and self-critique"),
    ),
    SkillSpec(
        name="unslop",
        category="ui-design",
        tagline="Anti-slop polish (@heydamir, CC BY 4.0): eliminates purple glows, emoji chrome, and generic card grids",
        source="heydamir/unslop",
        excerpt_sections=("What good looks like", "Do not", "Checklist"),
    ),
    SkillSpec(
        name="impeccable",
        category="ui-design",
        tagline="Design-director passes (critique/distill/typeset/colorize/polish) + deterministic anti-slop detector",
        source="pbakaus/impeccable",
    ),
    SkillSpec(
        name="web-design-guidelines",
        category="ui-design",
        tagline="Pre-ship UI/UX & accessibility audit: WCAG contrast, keyboard focus, ARIA, forms, touch targets, CLS",
        source="vercel-labs/agent-skills",
    ),
    # 3. Debugging, Bug Fixing & Live Browser QA
    SkillSpec(
        name="systematic-debugging",
        category="debugging-qa",
        tagline="4-phase root-cause investigation protocol: no fixes without verified root cause first",
        source="obra/superpowers",
        excerpt_sections=("Quick Reference",),
    ),
    SkillSpec(
        name="verification-before-completion",
        category="debugging-qa",
        tagline="Evidence-first completion gate: run fresh verification AFTER final edit and read real output/exit code",
        source="obra/superpowers",
    ),
    SkillSpec(
        name="chrome-devtools-qa",
        category="debugging-qa",
        tagline="Live browser console, network, DOM layout, memory leak, and Core Web Vitals performance debugging",
        source="ChromeDevTools/chrome-devtools-mcp",
    ),
    # 4. Website Security & Pre-Install Skill Safety
    SkillSpec(
        name="security-audit",
        category="security",
        tagline="6-phase adversarial repo security auditor: hunt real exploit paths, disprove false positives, patch & verify",
        source="cloudflare/security-audit-skill",
    ),
    SkillSpec(
        name="semgrep-guardrails",
        category="security",
        tagline="While-you-code OWASP Top 10 & LLM Top 10 guardrails: XSS, SQLi, CSRF, SSRF, auth/IDOR, CSP, secret hygiene",
        source="semgrep/skills",
    ),
    SkillSpec(
        name="skill-spector",
        category="security",
        tagline="Pre-install scanner for third-party SKILL.md & plugins: detects prompt injection, obfuscation, exfiltration",
        source="NVIDIA/SkillSpector",
    ),
    # 5. Core Software Engineering
    SkillSpec(
        name="karpathy-skills",
        category="engineering",
        tagline="4-pillar clean coding discipline: think before coding, simplicity first, surgical changes, goal-driven checks",
        source="multica-ai/andrej-karpathy-skills",
    ),
    SkillSpec(
        name="test-driven-development",
        category="engineering",
        tagline="Red-green-refactor disciplined engineering: write failing test first, implement minimally, verify & clean up",
        source="obra/superpowers",
        excerpt_sections=("Overview", "REFACTOR - Clean Up"),
    ),
    SkillSpec(
        name="grill-and-git-guardrails",
        category="engineering",
        tagline="Pre-build clarity (grill-me requirements lock) + destructive git guardrails blocking force-push/hard-reset",
        source="mattpocock/skills",
    ),
    SkillSpec(
        name="context7-docs",
        category="engineering",
        tagline="Live version-accurate framework docs grounding: verify installed versions & fetch current docs before coding",
        source="upstash/context7",
    ),
)

SKILL_BY_NAME: Dict[str, SkillSpec] = {spec.name: spec for spec in SKILL_SPECS}
ALL_SKILL_NAMES: Tuple[str, ...] = tuple(spec.name for spec in SKILL_SPECS)

# Friendly aliases users or web agents might type
_SKILL_ALIASES: Dict[str, str] = {
    "dreamloop": "dream-loop",
    "dream_loop": "dream-loop",
    "threejs": "threejs-skills",
    "three-js": "threejs-skills",
    "gsap": "gsap-skills",
    "karpathy": "karpathy-skills",
    "andrej-karpathy-skills": "karpathy-skills",
    "chrome-devtools": "chrome-devtools-qa",
    "chrome-devtools-mcp": "chrome-devtools-qa",
    "devtools": "chrome-devtools-qa",
    "security-audit-skill": "security-audit",
    "semgrep": "semgrep-guardrails",
    "skillspector": "skill-spector",
    "grill-me": "grill-and-git-guardrails",
    "git-guardrails": "grill-and-git-guardrails",
    "context7": "context7-docs",
    "tdd": "test-driven-development",
    "debugging": "systematic-debugging",
    "verify": "verification-before-completion",
    "a11y": "web-design-guidelines",
}


# ── Intent & Vibe-Coder Signal Patterns ───────────────────────────────────────

_EXPLAIN_ONLY = re.compile(
    r"^\s*(?:what\s+(?:is|are|was|were)|explain\s+(?:what|how|why)|define|what\s+does|tell\s+me\s+about|how\s+does)\b",
    re.I,
)
_CHANGE = re.compile(
    r"\b(build|create|make|implement|develop|add|extend|wire|integrate|refactor|design|redesign|"
    r"style|restyle|polish|improve|upgrade|enhance|elevate|clean\s+up|unslop|overhaul|revamp|"
    r"rebuild|remake|fix|debug|optimi[sz]e|speed\s+up|audit|harden|secure|review|scan|vet|migrate|rewrite|transform)\b",
    re.I,
)
_IMPLEMENT = re.compile(
    r"\b(build|create|make|implement|develop|add|extend|wire|integrate|refactor|migrate|rewrite)\b",
    re.I,
)

# Structural UI & website nouns
_UI_STRUCTURAL = re.compile(
    r"\b(ui|ux|front[ -]?end|web\s?site|web\s?page|landing\s+page|home\s?page|web\s?app|webapp|"
    r"dashboard|admin\s+panel|portfolio|storefront|e-?commerce(?:\s+(?:site|store|web\s?site|app))?|"
    r"saas|checkout\s+page|layout|typography|component|responsive|interface\s+design|"
    r"user\s+interface|dark\s+mode|light\s+mode|tailwind(?:css)?|stylesheet|css|hero\s+section|"
    r"navbar|sidebar|design\s+system|bento(?:\s+grid)?)\b",
    re.I,
)
_WEBSITE_BUILD_TARGET = re.compile(
    r"\b(web\s?site|web\s?page|landing\s+page|home\s?page|web\s?app|webapp|storefront|"
    r"e-?commerce|portfolio|saas|dashboard|admin\s+panel|checkout\s+page|online\s+store|shop\s+site)\b",
    re.I,
)
_WEBSITE_BUILD_VERBS = re.compile(
    r"\b(build|create|make|develop|implement|design|redesign|restyle|overhaul|revamp|rebuild|remake|launch|ship|unslop)\b",
    re.I,
)
_STANDALONE_INTERFACE = re.compile(r"\binterface\b", re.I)
_NON_UI_INTERFACE = re.compile(
    r"\b(?:api|cli|command[- ]line|network|hardware|kernel|typescript|ts|java|go|golang|rust|c#|"
    r"driver|storage|service|programming|plugin|protocol)\s+interface\b",
    re.I,
)

# Vibe-coder aesthetic / premium / anti-slop vocabulary
_VIBE_AESTHETIC = re.compile(
    r"\b(premium|beautiful|stunning|gorgeous|sleek|modern|minimal(?:ist)?|luxur(?:y|ious)|"
    r"expensive|high[- ]end|polished|refined|editorial|awwwards|award[- ]winning|"
    r"apple[- ]?(?:like|style)|linear[- ]?style|stripe[- ]?style|vercel[- ]?style|"
    r"igloo(?:\.inc)?|cinematic|elevated|studio[- ]grade|agency[- ]grade|god[- ]tier|"
    r"crisp(?:y)?|aesthetic|eye[- ]catching|delightful|pro[- ]looking|next[- ]level|"
    r"glassmorph(?:ism|ic)|(?:neo|neu)?brutalist|micro[- ]?interactions?|buttery)\b",
    re.I,
)
_ANTI_SLOP = re.compile(
    r"\b(ai[- ]?generated|looks?\s+(?:like\s+)?ai|feels?\s+(?:like\s+)?ai|ai\s+slop|\bslop\b|"
    r"unslop|vibe[- ]?coded|cookie[- ]cutter|templated|templatey|generic\s+(?:ui|design|site|website|page|template|look)|"
    r"looks?\s+(?:cheap|ugly|bad|amateur|messy|boring|bland|generic)|cheap[- ]looking|"
    r"purple\s+gradient|neon\s+glow|default\s+tailwind|bootstrap[- ]looking|"
    r"stop\s+looking\s+(?:like\s+)?ai|make\s+it\s+look\s+(?:real|human|premium|expensive|good)|human[- ]designed)\b",
    re.I,
)

# 3D, Shaders & Scroll Motion signals
_THREE_D = re.compile(
    r"\b(3d|three\.?js|threejs|webgl|webgpu|r3f|react[- ]three[- ]fiber|shaders?|glsl|"
    r"spline|gltf|glb|pbr|raycast(?:ing)?|orbitcontrols|igloo(?:\.inc)?|particles?|"
    r"post[- ]?processing|volumetric|holographic|procedural\s+3d|dream[- ]?loop|img2threejs)\b",
    re.I,
)
_HERO_3D_ASSET = re.compile(
    r"\b(3d\s+(?:hero|model|product|asset|object|viewer|showcase|card|e-?commerce|store|web\s?site|scene)|"
    r"(?:photo|image)\s+to\s+3d|reference\s+(?:photo|image).*3d|procedural\s+3d|igloo(?:\.inc)?|img2threejs)\b",
    re.I,
)
_SCROLL_MOTION = re.compile(
    r"\b(gsap|scroll[- ]?trigger|scroll\s+storytelling|scroll[- ]?(?:driven|scrubbed|animation|reveal|chapter)|"
    r"pinned\s+(?:section|chapter|hero)|pinning|parallax|lenis|locomotive|cinematic\s+scroll|"
    r"smooth\s+scroll|buttery\s+scroll|stagger(?:ed)?\s+anim|timeline\s+animation|kinetic\s+typography|igloo(?:\.inc)?)\b",
    re.I,
)

# Debugging, Bug Fixing, Optimization & Browser QA signals
_FAILURE = re.compile(
    r"\b(debug(?:ging)?|troubleshoot(?:ing)?|bugs?|buggy|glitch(?:es|y)?|jank(?:y)?|"
    r"regressions?|crash(?:es|ing|ed)?|broken|broke|cooked|busted|fails?|failing|failures?|"
    r"traceback|exceptions?|errors?|stack\s+trace|timed?\s+out|timeout|hang(?:s|ing)?|"
    r"freez(?:e|es|ing)|unresponsive|stuck|infinite\s+loop|not\s+working|doesn['’]?t\s+work|"
    r"won['’]?t\s+load|blank\s+screen|white\s+screen|hydration\s+error|console\s+error|"
    r"500\s+error|404\s+error|cors\s+error|race\s+condition|memory\s+leak)\b",
    re.I,
)
_OPTIMIZATION = re.compile(
    r"\b(optimi[sz](?:e|ation|ing)|performance|\bperf\b|slow|sluggish|lag(?:s|gy|ging)?|"
    r"stutter(?:s|ing)?|\bfps\b|frame\s+drop|bundle\s+size|lighthouse|core\s+web\s+vitals|"
    r"\b(?:cls|lcp|inp)\b|latency|bottleneck|profil(?:e|ing)|speed\s+up|make\s+it\s+faster|too\s+slow|memory\s+leak)\b",
    re.I,
)
_BROWSER_QA = re.compile(
    r"\b(devtools|console\s+(?:error|log|warning)|network\s+(?:tab|error|fail|request)|"
    r"browser\s+(?:qa|bug|check|inspect)|inspect\s+page|viewport|responsive\s+(?:check|bug)|dom\s+bug)\b",
    re.I,
)

# Security & Pre-Install Skill Vetting signals
_SECURITY = re.compile(
    r"\b(security|secure\s+(?:the|this|my|our|it|app|site|web\s?site|api|backend|auth)|"
    r"harden(?:ing)?|vulnerabilit(?:y|ies)|vulns?|owasp|xss|csrf|ssrf|sqli|sql\s+injection|"
    r"idor|rce|cors\s+(?:policy|config|fix|issue)|csp|content[- ]security[- ]policy|"
    r"saniti[sz](?:e|ation)|pentest|security\s+(?:audit|fix(?:ing)?|review|check|scan)|"
    r"rbac|jwt|oauth2?|authentication|authorization|password\s+hash|secret\s+leak|"
    r"production[- ]?ready|prod[- ]?ready|before\s+launch|pre[- ]?ship|prompt\s+injection|llm\s+security)\b",
    re.I,
)
_SKILL_VETTING = re.compile(
    r"\b(install(?:ing)?\s+(?:a\s+|this\s+)?(?:third[- ]?party\s+)?skill|third[- ]?party\s+skill|"
    r"skill\.md|vet\s+(?:this\s+|the\s+)?skill|scan\s+(?:this\s+|the\s+)?skill|"
    r"untrusted\s+skill|malicious\s+skill|skill[- ]?spector|audit\s+(?:this\s+|the\s+)?(?:skill|mcp\s+server))\b",
    re.I,
)

# Core software engineering, pre-build clarity, git safety & live docs
_SOFTWARE = re.compile(
    r"\b(api|endpoint|service|software|app(?:lication)?|web\s?app|cli|bot|feature|support\s+for|"
    r"command|plugin|function|module|authentication|auth|login|oauth2?|pagination|search|"
    r"validation|cache|database|integration|rate\s+limit(?:er|ing)?|tests?|code|script|server|backend)\b",
    re.I,
)
_GRILL_OR_GIT = re.compile(
    r"\b(grill\s+me|interview\s+me|clarify\s+requirements|lock\s+requirements|"
    r"git\s+(?:push|reset|rebase|merge|clean|checkout|branch|commit|history)|force\s+push)\b",
    re.I,
)
_LIVE_DOCS_FRAMEWORKS = re.compile(
    r"\b(context7|latest\s+(?:docs|api|version)|up[- ]to[- ]date\s+docs|official\s+docs|"
    r"next\.?js|react\s*19|tailwind(?:css)?\s*v?4|svelte\s*5|nuxt|astro|supabase|prisma|"
    r"drizzle|fastapi|pydantic\s*v?2|stripe|clerk|auth0|playwright)\b",
    re.I,
)


@dataclass(frozen=True)
class SkillAppointment:
    skills: Tuple[str, ...]
    categories: Tuple[str, ...]
    matched_cues: Tuple[str, ...]
    requires_security_pass: bool = False
    security_skills: Tuple[str, ...] = ()
    ai_recommended: Tuple[str, ...] = ()
    is_website_build: bool = False

    @property
    def all_skills(self) -> Tuple[str, ...]:
        return tuple(dict.fromkeys((*self.skills, *self.security_skills)))

    @property
    def headline(self) -> str:
        if not self.skills and not self.requires_security_pass:
            return ""
        cats = " · ".join(self.categories) if self.categories else "Specialized Engineering"
        return f"This prompt detected {cats} — forcing web agents to read the relevant skills first..."


def normalize_skill_name(raw: str) -> str:
    """Resolve canonical bundled skill name from user or model input."""
    key = re.sub(r"[\s_]+", "-", str(raw or "").strip().lower()).strip("-")
    if key in SKILL_BY_NAME:
        return key
    return _SKILL_ALIASES.get(key, "")


def _is_ui_task(text: str) -> bool:
    if _UI_STRUCTURAL.search(text):
        return True
    if _STANDALONE_INTERFACE.search(text) and not _NON_UI_INTERFACE.search(text):
        return True
    return False


def _collect_cues(text: str, patterns: Sequence[re.Pattern[str]], limit: int = 8) -> Tuple[str, ...]:
    raw_found: List[str] = []
    seen = set()
    for pat in patterns:
        for m in pat.finditer(text):
            token = re.sub(r"\s+", " ", m.group(0).strip())
            low = token.lower()
            if token and low not in seen:
                seen.add(low)
                raw_found.append(token)
    # Remove shorter tokens that are already contained inside a longer matched cue (e.g. "ecommerce" inside "ecommerce website")
    pruned: List[str] = []
    for tok in raw_found:
        low = tok.lower()
        if any(low != other.lower() and low in other.lower().split() for other in raw_found):
            continue
        pruned.append(tok)
        if len(pruned) >= limit:
            break
    return tuple(pruned)


def analyze_prompt_skills(
    request: str,
    *,
    ai_skills: Sequence[str] = (),
    ai_security_pass: Optional[bool] = None,
    max_primary_skills: int = 8,
) -> SkillAppointment:
    """Analyze a user prompt (plus optional web-agent router picks) into a structured SkillAppointment."""
    text = str(request or "")
    explicit = [
        name for name in ALL_SKILL_NAMES
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text, re.I)
    ]

    # Pure conceptual Q&A without an explicit imperative fix/build request stays skill-free
    has_imperative_followup = bool(re.search(
        r"\b(?:and\s+(?:fix|patch|solve|resolve|harden|secure|audit|build|implement|optimi[sz]e)|please\s+(?:fix|patch|harden|audit|build))\b",
        text,
        re.I,
    ))
    if _EXPLAIN_ONLY.search(text) and not has_imperative_followup:
        if explicit:
            cats = tuple(dict.fromkeys(CATEGORY_LABELS[SKILL_BY_NAME[n].category] for n in explicit))
            return SkillAppointment(skills=tuple(explicit), categories=cats, matched_cues=tuple(explicit))
        return SkillAppointment(skills=(), categories=(), matched_cues=())

    selected: List[str] = list(explicit)
    categories: List[str] = []

    has_change = bool(_CHANGE.search(text))
    has_impl = bool(_IMPLEMENT.search(text))
    has_ui = _is_ui_task(text)
    has_vibe = bool(_VIBE_AESTHETIC.search(text))
    has_anti_slop = bool(_ANTI_SLOP.search(text))
    has_3d = bool(_THREE_D.search(text))
    has_hero_3d = bool(_HERO_3D_ASSET.search(text))
    has_scroll = bool(_SCROLL_MOTION.search(text))
    has_failure = bool(_FAILURE.search(text))
    has_fix_word = bool(re.search(r"\bfix(?:ing)?\b", text, re.I))
    has_opt = bool(_OPTIMIZATION.search(text))
    has_browser_qa = bool(_BROWSER_QA.search(text))
    has_security = bool(_SECURITY.search(text))
    has_skill_vet = bool(_SKILL_VETTING.search(text))
    has_software = bool(_SOFTWARE.search(text))
    has_grill_git = bool(_GRILL_OR_GIT.search(text))
    has_live_docs = bool(_LIVE_DOCS_FRAMEWORKS.search(text))

    # Detect explicit anti-slop only prompt ("Unslop this page") vs full UI design
    explicit_unslop_only = (
        explicit == ["unslop"]
        and not has_3d
        and not has_scroll
        and not has_failure
        and not has_security
        and not _WEBSITE_BUILD_TARGET.search(text)
        and not has_vibe
    )

    is_website_build = bool(
        _WEBSITE_BUILD_TARGET.search(text)
        and (_WEBSITE_BUILD_VERBS.search(text) or has_vibe or has_anti_slop or has_3d or has_scroll)
    )

    # 1. UI Design & Anti-Slop
    if explicit_unslop_only:
        categories.append("UI & Anti-Slop")
    elif (has_ui and (has_change or has_failure or has_vibe or has_anti_slop)) or (has_anti_slop and (has_change or has_fix_word or "website" in text.lower() or "page" in text.lower() or "ui" in text.lower() or "site" in text.lower())) or (has_vibe and (has_ui or has_3d or has_scroll)):
        categories.append("UI & Anti-Slop")
        if has_anti_slop:
            selected.extend(["unslop", "impeccable", "frontend-design"])
        else:
            selected.extend(["frontend-design", "unslop", "impeccable"])
        if is_website_build or has_vibe or has_anti_slop or re.search(r"\b(a11y|accessib|audit|ship|launch|responsive|form|checkout|dashboard)\b", text, re.I):
            selected.append("web-design-guidelines")

    # 2. 3D, Shaders & Scroll Motion
    if has_3d or has_scroll:
        categories.append("3D, Shaders & Motion")
        if has_3d:
            selected.extend(["threejs-skills", "dream-loop"])
            if has_hero_3d:
                selected.append("img2threejs")
        if has_scroll:
            selected.append("gsap-skills")
            if "dream-loop" not in selected and (has_ui or has_vibe):
                selected.append("dream-loop")

    # 3. Debugging, Optimization & Live Browser QA
    if has_failure or has_opt or (has_fix_word and (has_software or has_ui or has_3d or has_anti_slop)):
        if has_opt:
            categories.append("Optimization & Browser QA")
        else:
            categories.append("Debugging & QA")
        if has_failure or (has_fix_word and not has_anti_slop):
            selected.append("systematic-debugging")
        if has_opt or has_browser_qa or (has_failure and (has_ui or has_3d or has_scroll)):
            selected.append("chrome-devtools-qa")
        if has_failure or has_opt or has_fix_word:
            selected.append("verification-before-completion")
    elif has_browser_qa:
        categories.append("Browser QA")
        selected.append("chrome-devtools-qa")

    # 4. Security & Skill Vetting
    if has_skill_vet:
        categories.append("Skill Security Vetting")
        selected.append("skill-spector")
    if has_security:
        categories.append("Website Security")
        selected.extend(["security-audit", "semgrep-guardrails"])
        if "verification-before-completion" not in selected:
            selected.append("verification-before-completion")

    # 5. Core Software Engineering, Clarity, Git & Live Docs
    if has_grill_git:
        categories.append("Pre-Build Clarity & Git Safety")
        selected.append("grill-and-git-guardrails")
    if has_live_docs and (has_change or has_failure):
        categories.append("Live Docs Grounding")
        selected.append("context7-docs")
    if has_impl and has_software:
        categories.append("Core Engineering")
        selected.extend(["test-driven-development", "karpathy-skills"])
        if "verification-before-completion" not in selected and not has_ui:
            selected.append("verification-before-completion")

    # Merge validated web-agent AI router picks (hybrid keyword + semantic judging)
    valid_ai: List[str] = []
    for raw_name in ai_skills or ():
        norm = normalize_skill_name(raw_name)
        if norm and norm not in valid_ai:
            valid_ai.append(norm)
            if norm not in selected:
                selected.append(norm)
                cat_lbl = CATEGORY_LABELS[SKILL_BY_NAME[norm].category]
                if cat_lbl not in categories:
                    categories.append(cat_lbl)

    # Deduplicate preserving priority order, keeping budget tight
    deduped = list(dict.fromkeys(selected))
    if len(deduped) > max_primary_skills:
        # Keep explicit + highest-priority domain skills within budget cap
        deduped = deduped[:max_primary_skills]

    # Automatic Post-Build Website Security Fixing whenever designing/building websites or security requested
    requires_security_pass = bool(
        is_website_build
        or has_security
        or (ai_security_pass is True)
    )
    if is_website_build and "Website Build" not in categories:
        categories.append("Website Build")

    security_skills: Tuple[str, ...] = (
        ("security-audit", "semgrep-guardrails") if requires_security_pass else ()
    )

    cues = _collect_cues(
        text,
        (
            _ANTI_SLOP,
            _VIBE_AESTHETIC,
            _THREE_D,
            _SCROLL_MOTION,
            _WEBSITE_BUILD_TARGET,
            _UI_STRUCTURAL,
            _OPTIMIZATION,
            _FAILURE,
            _SECURITY,
            _SKILL_VETTING,
            _LIVE_DOCS_FRAMEWORKS,
            _GRILL_OR_GIT,
        ),
        limit=8,
    )
    if not cues and deduped:
        cues = tuple(deduped[:4])

    return SkillAppointment(
        skills=tuple(deduped),
        categories=tuple(dict.fromkeys(categories)),
        matched_cues=cues,
        requires_security_pass=requires_security_pass,
        security_skills=security_skills,
        ai_recommended=tuple(valid_ai),
        is_website_build=is_website_build,
    )


def select_task_skills(request: str) -> Tuple[str, ...]:
    """Return ordered tuple of canonical skill names appointed for `request`."""
    return analyze_prompt_skills(request).skills


# ── Skill Loading & Lossless High-Density Compression ─────────────────────────

@lru_cache(maxsize=len(SKILL_SPECS) + 4)
def _raw_skill_doc(name: str) -> str:
    return files("wsa_agent").joinpath("skills", name, "SKILL.md").read_text(encoding="utf-8")


def _strip_frontmatter(doc: str) -> str:
    s = doc.lstrip("\ufeff")
    if s.startswith("---"):
        m = re.match(r"^---\s*\r?\n[\s\S]*?\r?\n---\s*\r?\n?", s)
        if m:
            return s[m.end():].strip()
    return s.strip()


def _parse_sections(body: str) -> List[Tuple[str, str]]:
    """Split markdown body into ordered `(heading, content)` pairs."""
    sections: List[Tuple[str, str]] = []
    matches = list(re.finditer(r"^(#{2,3})\s+(.+?)\s*$", body, re.M))
    if not matches:
        return [("Overview", body.strip())]
    preamble = body[: matches[0].start()].strip()
    if preamble:
        sections.append(("Overview", preamble))
    for i, m in enumerate(matches):
        title = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections.append((title, body[start:end].strip()))
    return sections


def _compress_markdown_losslessly(text: str, max_chars: int = 2400) -> str:
    """Compress markdown skill text without dropping rules, headings, phases, or checklists."""
    cleaned_lines: List[str] = []
    prev_blank = False
    for raw in text.splitlines():
        ln = raw.rstrip()
        if not ln.strip():
            if not prev_blank and cleaned_lines:
                cleaned_lines.append("")
            prev_blank = True
            continue
        prev_blank = False
        cleaned_lines.append(ln)
    compact = "\n".join(cleaned_lines).strip()
    if len(compact) <= max_chars:
        return compact

    # Preserve headings, bullets, numbered steps, code-fenced Iron Laws, and first prose line per section
    kept: List[str] = []
    in_fence = False
    section_prose = 0
    for ln in cleaned_lines:
        s = ln.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            kept.append(ln)
            continue
        if in_fence:
            kept.append(ln)
            continue
        if s.startswith("#"):
            section_prose = 0
            kept.append(ln)
        elif re.match(r"^(?:[-*+]|\d+\.|\[[ xX]\])\s+", s):
            kept.append(ln)
        elif s and section_prose < 1:
            kept.append(ln)
            section_prose += 1

    result = "\n".join(kept).strip()
    if len(result) <= max_chars:
        return result
    # Graceful line-boundary trim with explicit section hint
    out_lines: List[str] = []
    total = 0
    for ln in result.splitlines():
        if total + len(ln) + 1 > max_chars - 90:
            break
        out_lines.append(ln)
        total += len(ln) + 1
    out_lines.append("... [More details available via read_skill(name=..., section=...)]")
    return "\n".join(out_lines)


@lru_cache(maxsize=len(SKILL_SPECS) + 4)
def _excerpt(name: str) -> str:
    """Compact offline excerpt used by `skill_guidance` (backwards-compatible)."""
    doc = _raw_skill_doc(name)
    spec = SKILL_BY_NAME[name]
    paragraphs: List[str] = []
    if name == "unslop":
        paragraphs.append("Credit: unslop by Damir Sokolovsky (@heydamir), CC BY 4.0.")
    if spec.excerpt_sections:
        for title in spec.excerpt_sections:
            section = re.search(rf"^#{{2,3}} {re.escape(title)}\s*\n(.*?)(?=^#{{2,3}} |\Z)", doc, re.M | re.S)
            if section is None:
                raise ValueError(f"Missing bundled skill section: {name}/{title}")
            body = section[1].strip()
            if name == "frontend-design" and title == "Design principles":
                body = "\n".join(body.splitlines()[:2])
            elif name == "unslop":
                if title == "What good looks like":
                    body = "\n".join(ln for ln in body.splitlines() if ln.lstrip().startswith("- "))
                elif title == "Checklist":
                    body = body.replace("follow [references/rtl.md](references/rtl.md)", "follow RTL rules")
            paragraphs.append(body)
        return f"[{name}]\n" + "\n".join(paragraphs)
    body = _strip_frontmatter(doc)
    return f"[{name}]\n" + _compress_markdown_losslessly(body, max_chars=1400)


def skill_guidance(names: Tuple[str, ...]) -> str:
    """Return compact inline skill guidance excerpts (kept compatible with direct callers)."""
    if not names:
        return ""
    return (
        "WSA task skills (mandatory for this task). Follow these rules within the current stage, "
        "the user's brief, and existing project conventions.\n"
        + "\n\n".join(_excerpt(n) for n in names if n in SKILL_BY_NAME)
    )


def format_router_skill_index() -> str:
    """Ultra-compact 1-line-per-skill index for the web-agent workflow/skill judge (~1.2KB)."""
    lines = ["Available WSA skills (pick ONLY relevant slugs in JSON \"skills\"):"]
    for spec in SKILL_SPECS:
        lines.append(f"- {spec.name} ({spec.category}): {spec.tagline}")
    return "\n".join(lines)


def skill_read_directive(
    appointment: SkillAppointment,
    *,
    already_loaded: Iterable[str] = (),
    include_security_note: bool = True,
) -> str:
    """Compact Read-First Appointment Directive sent in the turn prompt instead of dumping full SKILL.md files.

    Keeps the initial prompt lean (~450-900 bytes, well inside DeepSeek 18KB / ChatGPT 21.5KB budgets)
    while forcing the web agent to call `read_skill(...)` first like normal WSA tool workflow.
    """
    loaded_set = {normalize_skill_name(s) for s in (already_loaded or ()) if normalize_skill_name(s)}
    pending = [n for n in appointment.skills if n not in loaded_set]
    all_names = list(appointment.skills)
    if not all_names and not appointment.requires_security_pass:
        return ""

    cats = ", ".join(appointment.categories) if appointment.categories else "Specialized"
    cues = ", ".join(appointment.matched_cues[:6])
    cue_note = f" (cues: {cues})" if cues else ""
    lines = [
        f"[WSA Mandatory Skills — Read First] Detected: {cats}{cue_note}.",
    ]
    if pending:
        batch_arg = ",".join(pending)
        lines.append(
            f"STEP 1 (REQUIRED before planning or editing code): Call `read_skill(name=\"{batch_arg}\")` "
            "to load these appointed WSA skills in compressed form, then follow every rule in them:"
        )
    elif all_names:
        lines.append(
            "Appointed WSA skills already loaded in this session — strictly enforce their rules "
            "(call `read_skill` again if you need a specific section):"
        )

    for name in all_names:
        spec = SKILL_BY_NAME.get(name)
        if not spec:
            continue
        marker = " [loaded]" if name in loaded_set else ""
        lines.append(f"• [{name}]{marker}: {spec.tagline}")

    if include_security_note and appointment.requires_security_pass:
        sec_list = ", ".join(appointment.security_skills or ("security-audit", "semgrep-guardrails"))
        lines.append(
            f"• [post-build security pass]: After website code is written, read `{sec_list}` via `read_skill` "
            "and fix any XSS, DOM injection, auth/IDOR, CSP/CORS, validation, or secret issues before concluding."
        )
    lines.append(
        "Need another skill? Call `read_skill(name=\"catalog\")` to browse all 18 WSA skills."
    )
    return "\n".join(lines)


def list_skills_catalog() -> str:
    """Format a compact, scannable catalog of all bundled WSA skills for `read_skill(name='catalog')`."""
    lines = [f"[OK] WSA Bundled Skills Catalog ({len(SKILL_SPECS)} skills across {len(CATEGORY_LABELS)} domains):"]
    for cat_key, cat_label in CATEGORY_LABELS.items():
        lines.append(f"\n## {cat_label}")
        for spec in SKILL_SPECS:
            if spec.category != cat_key:
                continue
            lines.append(f"- {spec.name} ({spec.source}): {spec.tagline}")
    lines.append("\nUsage: read_skill(name=\"impeccable,threejs-skills\") or read_skill(name=\"security-audit\", mode=\"full\")")
    return "\n".join(lines)


def read_skill_content(
    name: str = "catalog",
    *,
    mode: str = "brief",
    section: str = "",
    max_chars: int = 8000,
) -> Tuple[str, List[str]]:
    """Read one or more bundled WSA skills in compressed (`brief`) or `full` mode.

    Returns `(formatted_tool_output, loaded_canonical_skill_names)`.
    """
    raw = str(name or "").strip()
    if not raw or raw.lower() in {"catalog", "list", "index", "all"}:
        return list_skills_catalog(), []

    requested = [tok.strip() for tok in re.split(r"[,;\s]+", raw) if tok.strip()]
    resolved: List[str] = []
    unknown: List[str] = []
    for tok in requested:
        norm = normalize_skill_name(tok)
        if norm:
            if norm not in resolved:
                resolved.append(norm)
        else:
            unknown.append(tok)

    if not resolved:
        avail = ", ".join(ALL_SKILL_NAMES)
        return (
            f"[Error] Unknown skill(s): {', '.join(unknown)}. "
            f"Available WSA skills: {avail}. Call read_skill(name=\"catalog\") for descriptions."
        ), []

    eff_mode = "full" if str(mode or "brief").strip().lower() == "full" else "brief"
    sec_query = str(section or "").strip().lower()
    per_skill_budget = max(1200, int((max_chars - 240) / max(1, len(resolved))))

    blocks: List[str] = []
    for slug in resolved:
        spec = SKILL_BY_NAME[slug]
        cat_label = CATEGORY_LABELS.get(spec.category, spec.category)
        doc = _strip_frontmatter(_raw_skill_doc(slug))
        sections = _parse_sections(doc)
        section_names = [t for t, _ in sections]

        if sec_query:
            matched = [
                (t, c) for t, c in sections
                if sec_query in t.lower()
            ]
            if not matched:
                blocks.append(
                    f"=== [{slug}] ({cat_label}) — section '{section}' not found ===\n"
                    f"Available sections: {', '.join(section_names)}"
                )
                continue
            body = "\n\n".join(f"## {t}\n{c}" for t, c in matched)
            if len(body) > per_skill_budget:
                body = _compress_markdown_losslessly(body, max_chars=per_skill_budget)
        elif eff_mode == "full":
            body = doc
            if len(body) > per_skill_budget:
                body = _compress_markdown_losslessly(body, max_chars=per_skill_budget)
        else:
            body = _compress_markdown_losslessly(doc, max_chars=min(per_skill_budget, 2600))

        extra_meta = ""
        if slug == "unslop" and "@heydamir" not in body:
            extra_meta = "Credit: unslop by Damir Sokolovsky (@heydamir), CC BY 4.0.\n"
        blocks.append(
            f"=== [{slug}] ({cat_label} · source: {spec.source}) ===\n"
            f"Sections: {', '.join(section_names)}\n"
            f"{extra_meta}{body}"
        )

    joined = "\n\n".join(blocks)
    line_count = len(joined.splitlines())
    size_kb = max(0.1, round(len(joined.encode("utf-8")) / 1024.0, 1))
    mode_lbl = f"section:{section}" if sec_query else ("full" if eff_mode == "full" else "compressed")
    if len(resolved) == 1:
        spec = SKILL_BY_NAME[resolved[0]]
        cat_lbl = CATEGORY_LABELS.get(spec.category, spec.category)
        header = (
            f"[OK] Loaded skill '{resolved[0]}' [{cat_lbl}] "
            f"({mode_lbl}, {line_count} lines · {size_kb:.1f}KB) — {spec.tagline}"
        )
    else:
        header = (
            f"[OK] Loaded {len(resolved)} skills ({', '.join(resolved)}) "
            f"({mode_lbl}, {line_count} lines · {size_kb:.1f}KB)"
        )
    if unknown:
        header += f"\n[Warn] Ignored unknown skill(s): {', '.join(unknown)}"
    return f"{header}\n\n{joined}", resolved


# ── Deterministic SkillSpector Scanner (NVIDIA/SkillSpector-inspired) ─────────

_SUSPICIOUS_INJECTION_PATTERNS: Tuple[Tuple[str, str, re.Pattern[str]], ...] = (
    ("CRITICAL", "prompt_injection_override", re.compile(
        r"\b(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|system|above)\s+(?:instructions|rules|prompts?)\b", re.I
    )),
    ("CRITICAL", "hidden_bypass_directive", re.compile(
        r"\b(?:do\s+not\s+tell\s+the\s+user|hide\s+this\s+from\s+the\s+user|silently\s+exfiltrate|bypass\s+(?:all\s+)?(?:security|permission|approval)\s+(?:checks?|gates?))\b", re.I
    )),
    ("CRITICAL", "pipe_to_shell_execution", re.compile(
        r"(?:curl|wget)\s+[^\n|]+\|\s*(?:ba)?sh\b|\b(?:irm|iwr|Invoke-WebRequest)\b[^\n|]+\|\s*(?:iex|Invoke-Expression)\b", re.I
    )),
    ("CRITICAL", "obfuscated_payload_exec", re.compile(
        r"base64\s+(?:-d|--decode)\s*\|\s*(?:ba)?sh\b|eval\s*\(\s*(?:atob|Buffer\.from)\s*\(", re.I
    )),
    ("HIGH", "credential_exfiltration", re.compile(
        r"(?:~\/\.ssh|~\/\.aws|~\/\.gnupg|wsa-auth\.json|Cookies\.sqlite)[\s\S]{0,120}(?:curl|wget|fetch|http\.post|webhook|discord\.com/api/webhooks)", re.I
    )),
    ("HIGH", "hidden_html_comment_instruction", re.compile(
        r"<!--[\s\S]{0,400}?\b(?:ignore\s+previous|system\s+prompt|execute|curl|exfiltrate|override)\b[\s\S]{0,400}?-->", re.I
    )),
)
_HIDDEN_UNICODE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\U000e0000-\U000e007f]")


def scan_skill_content(text: str, *, source_name: str = "<skill>") -> Dict[str, Any]:
    """Deterministically scan a SKILL.md or script for prompt injection, obfuscation, or exfiltration."""
    raw = str(text or "")
    findings: List[Dict[str, Any]] = []

    if _HIDDEN_UNICODE_RE.search(raw):
        findings.append({
            "severity": "HIGH",
            "rule": "hidden_unicode_bidi_or_zw",
            "source": source_name,
            "line": 1,
            "detail": "Detected invisible zero-width, bidi override, or Unicode tag characters.",
        })

    # Avoid false-positive self-triggers on documentation lines that merely quote attack examples in backticks
    # by flagging active non-quoted occurrences or checking outside explicit example lists.
    lines = raw.splitlines()
    for sev, rule_id, pat in _SUSPICIOUS_INJECTION_PATTERNS:
        for m in pat.finditer(raw):
            line_no = raw.count("\n", 0, m.start()) + 1
            line_txt = lines[line_no - 1] if 0 <= line_no - 1 < len(lines) else ""
            # If the skill is explicitly a security scanner documenting attack signatures in quotes/backticks,
            # mark as INFO/CAUTION rather than CRITICAL when inside quoted example bullets.
            is_quoted_doc = bool(
                re.search(r"^\s*[-*]\s+Flag\b", line_txt, re.I)
                or ('"' in line_txt and f'"{m.group(0)}"' in line_txt)
                or ('`' in line_txt and f'`{m.group(0)}`' in line_txt)
            )
            if is_quoted_doc:
                continue
            findings.append({
                "severity": sev,
                "rule": rule_id,
                "source": source_name,
                "line": line_no,
                "detail": line_txt.strip()[:160],
            })

    crit = sum(1 for f in findings if f["severity"] == "CRITICAL")
    high = sum(1 for f in findings if f["severity"] == "HIGH")
    if crit > 0:
        verdict = "DO NOT INSTALL"
        score = max(0, 30 - crit * 15 - high * 5)
    elif high > 0:
        verdict = "CAUTION"
        score = max(40, 80 - high * 15)
    else:
        verdict = "SAFE"
        score = 100

    return {
        "source": source_name,
        "verdict": verdict,
        "score": score,
        "findings": findings,
    }


def scan_skill_path(target: Path) -> Dict[str, Any]:
    """Scan a local skill file or skill directory with SkillSpector."""
    p = Path(target)
    if not p.exists():
        return {"source": str(p), "verdict": "ERROR", "score": 0, "findings": [{"severity": "HIGH", "rule": "not_found", "source": str(p), "line": 0, "detail": "Path does not exist"}]}
    files_to_scan = [p] if p.is_file() else [
        f for f in sorted(p.rglob("*"))
        if f.is_file() and f.suffix.lower() in {".md", ".txt", ".py", ".js", ".ts", ".sh", ".ps1", ".json", ".yaml", ".yml"}
    ]
    all_findings: List[Dict[str, Any]] = []
    worst_verdict = "SAFE"
    min_score = 100
    rank = {"SAFE": 0, "CAUTION": 1, "DO NOT INSTALL": 2}
    for f in files_to_scan[:50]:
        try:
            res = scan_skill_content(f.read_text(encoding="utf-8", errors="replace"), source_name=str(f.name))
        except OSError:
            continue
        all_findings.extend(res["findings"])
        if rank.get(res["verdict"], 0) > rank.get(worst_verdict, 0):
            worst_verdict = res["verdict"]
        min_score = min(min_score, int(res["score"]))
    return {
        "source": str(p),
        "files_scanned": len(files_to_scan),
        "verdict": worst_verdict,
        "score": min_score,
        "findings": all_findings,
    }
