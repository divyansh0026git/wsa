"""Offline skill catalog, vibe-coder prompt signal analyzer, compressed skill reader, and SkillSpector scanner."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


# ── Bundled Canonical Skill Registry (5 Unified High-Density Domain Pillars) ──

CATEGORY_LABELS: Dict[str, str] = {
    "design": "3D, UI & Cinematic Craft",
    "security": "Website Security & Guardrails",
    "engineering": "Core Engineering & Workflow",
    "debugging-qa": "Systematic Debugging & DevTools QA",
    "docs": "Documentation Grounding",
}


@dataclass(frozen=True)
class SkillSpec:
    name: str
    category: str
    tagline: str
    source: str
    excerpt_sections: Tuple[str, ...] = ()


SKILL_SPECS: Tuple[SkillSpec, ...] = (
    # 1. 3D, UI & Cinematic Craft
    SkillSpec(
        name="design-craft",
        category="design",
        tagline="Master Design Director & 3D Cinematic Craft: anti-slop typography, responsive layout, GSAP choreography, and award-winning WebGL/Three.js shaders (Igloo.inc & NASA Eyes on the Solar System standard)",
        source="anthropic/greensock/threejs-skills/igloo",
        excerpt_sections=("1. UI & Visual Identity (Anti-Slop Baseline)", "3. Igloo.inc-Class 3D Cinematic & Solar System Architecture"),
    ),
    # 2. Website Security & Pre-Install Skill Safety
    SkillSpec(
        name="security-guardrails",
        category="security",
        tagline="Master Website Security & Guardrails: OWASP Top 10 code security, 6-phase adversarial repo audit, secret hygiene, and pre-install skill safety verification",
        source="cloudflare/semgrep/nvidia-skillspector",
        excerpt_sections=("1. While-You-Code OWASP Top 10 Guardrails", "2. 6-Phase Adversarial Repository Security Audit"),
    ),
    # 3. Core Software Engineering & Workflow
    SkillSpec(
        name="engineering-workflow",
        category="engineering",
        tagline="Master Software Engineering Discipline: Karpathy simplicity, Red-Green-Refactor TDD, strict pre-completion evidence verification, and git safety guardrails",
        source="karpathy/superpowers/git-guardrails",
        excerpt_sections=("1. Karpathy 4-Pillar Clean Coding Discipline", "3. Verification Before Completion Gate"),
    ),
    # 4. Systematic Debugging & Browser QA
    SkillSpec(
        name="systematic-debugging",
        category="debugging-qa",
        tagline="Master Debugging & Browser QA: 4-phase root-cause investigation protocol, live Chrome DevTools inspection (console, network, DOM, memory), and performance profiling",
        source="superpowers/chrome-devtools-mcp",
        excerpt_sections=("1. The 4-Phase Root Cause Protocol", "2. Live Browser DevTools QA (Frontend & WebGL)"),
    ),
    # 5. Documentation Grounding
    SkillSpec(
        name="context-docs",
        category="docs",
        tagline="Live, version-accurate framework and library documentation grounding: inspect installed versions & fetch official current docs before coding",
        source="upstash/context7",
        excerpt_sections=("The Ground-Before-Code Protocol",),
    ),
)

SKILL_BY_NAME: Dict[str, SkillSpec] = {spec.name: spec for spec in SKILL_SPECS}
ALL_SKILL_NAMES: Tuple[str, ...] = tuple(spec.name for spec in SKILL_SPECS)
CANONICAL_SPECS: Tuple[SkillSpec, ...] = SKILL_SPECS
CANONICAL_SKILL_NAMES: Tuple[str, ...] = ALL_SKILL_NAMES

# Legacy-to-canonical mapping
LEGACY_TO_CANONICAL: Dict[str, str] = {
    "dream-loop": "design-craft",
    "img2threejs": "design-craft",
    "gsap-skills": "design-craft",
    "threejs-skills": "design-craft",
    "frontend-design": "design-craft",
    "unslop": "design-craft",
    "impeccable": "design-craft",
    "web-design-guidelines": "design-craft",
    "security-audit": "security-guardrails",
    "semgrep-guardrails": "security-guardrails",
    "skill-spector": "security-guardrails",
    "karpathy-skills": "engineering-workflow",
    "test-driven-development": "engineering-workflow",
    "verification-before-completion": "engineering-workflow",
    "grill-and-git-guardrails": "engineering-workflow",
    "systematic-debugging": "systematic-debugging",
    "chrome-devtools-qa": "systematic-debugging",
    "context7-docs": "context-docs",
    "context-docs": "context-docs",
    "design-craft": "design-craft",
    "security-guardrails": "security-guardrails",
    "engineering-workflow": "engineering-workflow",
}

# Alias resolution dictionary
_SKILL_ALIASES: Dict[str, str] = {
    "design-craft": "design-craft",
    "design": "design-craft",
    "ui": "design-craft",
    "3d": "design-craft",
    "cinematic": "design-craft",
    "igloo": "design-craft",
    "solar-system": "design-craft",
    "security-guardrails": "security-guardrails",
    "security": "security-guardrails",
    "guardrails": "security-guardrails",
    "engineering-workflow": "engineering-workflow",
    "engineering": "engineering-workflow",
    "workflow": "engineering-workflow",
    "systematic-debugging": "systematic-debugging",
    "debugging": "systematic-debugging",
    "context-docs": "context-docs",
    "docs": "context-docs",
    "dream-loop": "design-craft",
    "dreamloop": "design-craft",
    "dream_loop": "design-craft",
    "img2threejs": "design-craft",
    "gsap-skills": "design-craft",
    "gsap": "design-craft",
    "threejs-skills": "design-craft",
    "threejs": "design-craft",
    "three-js": "design-craft",
    "frontend-design": "design-craft",
    "unslop": "design-craft",
    "impeccable": "design-craft",
    "web-design-guidelines": "design-craft",
    "a11y": "design-craft",
    "security-audit": "security-guardrails",
    "security-audit-skill": "security-guardrails",
    "semgrep-guardrails": "security-guardrails",
    "semgrep": "security-guardrails",
    "skill-spector": "security-guardrails",
    "skillspector": "security-guardrails",
    "karpathy-skills": "engineering-workflow",
    "karpathy": "engineering-workflow",
    "andrej-karpathy-skills": "engineering-workflow",
    "test-driven-development": "engineering-workflow",
    "tdd": "engineering-workflow",
    "verification-before-completion": "engineering-workflow",
    "verify": "engineering-workflow",
    "grill-and-git-guardrails": "engineering-workflow",
    "grill-me": "engineering-workflow",
    "git-guardrails": "engineering-workflow",
    "chrome-devtools-qa": "systematic-debugging",
    "chrome-devtools": "systematic-debugging",
    "chrome-devtools-mcp": "systematic-debugging",
    "devtools": "systematic-debugging",
    "context7-docs": "context-docs",
    "context7": "context-docs",
}

# Ensure any alias lookup in SKILL_BY_NAME also returns the canonical spec
for _alias, _target in _SKILL_ALIASES.items():
    if _alias not in SKILL_BY_NAME and _target in SKILL_BY_NAME:
        SKILL_BY_NAME[_alias] = SKILL_BY_NAME[_target]


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
    r"solar\s+system|celestial|planet(?:s|ary)?|orbit(?:al)?|eyes\s+on\s+the\s+solar\s+system|"
    r"tsl|three-live|shaderpass|effectcomposer|cinematic\s+3d|"
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
    def canonical_skills(self) -> Tuple[str, ...]:
        out: List[str] = []
        for s in self.all_skills:
            canon = LEGACY_TO_CANONICAL.get(s, s)
            if canon in CANONICAL_SKILL_NAMES and canon not in out:
                out.append(canon)
        return tuple(out)

    @property
    def headline(self) -> str:
        if not self.skills and not self.requires_security_pass:
            return ""
        cats = " · ".join(self.categories) if self.categories else "Specialized Engineering"
        return f"This prompt detected {cats} — forcing web agents to read the relevant skills first..."


def normalize_skill_name(raw: str) -> str:
    """Resolve canonical bundled skill name from user or model input."""
    key = re.sub(r"[\s_]+", "-", str(raw or "").strip().lower()).strip("-")
    if key in _SKILL_ALIASES:
        return _SKILL_ALIASES[key]
    if key in SKILL_BY_NAME:
        return key
    return ""


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

    # 1. 3D, UI & Cinematic Craft
    if (
        explicit_unslop_only
        or (has_ui and (has_change or has_failure or has_vibe or has_anti_slop))
        or (has_anti_slop and (has_change or has_fix_word or any(w in text.lower() for w in ("website", "page", "ui", "site"))))
        or (has_vibe and (has_ui or has_3d or has_scroll))
        or has_3d
        or has_scroll
        or is_website_build
    ):
        categories.append(CATEGORY_LABELS["design"])
        selected.append("design-craft")

    # 2. Debugging, Optimization & Live Browser QA
    if has_failure or has_opt or has_browser_qa or (has_fix_word and (has_software or has_ui or has_3d or has_anti_slop)):
        categories.append(CATEGORY_LABELS["debugging-qa"])
        selected.append("systematic-debugging")

    # 3. Security & Skill Vetting
    if has_skill_vet or has_security:
        categories.append(CATEGORY_LABELS["security"])
        selected.append("security-guardrails")

    # 4. Core Software Engineering, Clarity, Git & Live Docs
    if has_impl or has_software or has_grill_git:
        categories.append(CATEGORY_LABELS["engineering"])
        selected.append("engineering-workflow")
    if has_live_docs and (has_change or has_failure or has_impl):
        categories.append(CATEGORY_LABELS["docs"])
        selected.append("context-docs")

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
        ("security-guardrails",) if requires_security_pass else ()
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
    target_name = normalize_skill_name(name) or name
    return files("wsa_agent").joinpath("skills", target_name, "SKILL.md").read_text(encoding="utf-8")


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
    matches = list(re.finditer(r"^(#{2})\s+(.+?)\s*$", body, re.M))
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


_PRIORITY_SECTION_RE = re.compile(
    r"(?:guardrail|anti[- ]?slop|do\s+not|iron\s+law|checklist|architecture|colorize|critical|security|"
    r"phase|pass|rule|principle|critique|core|workflow)",
    re.I,
)


def _compact_section_lines(raw_text: str) -> List[str]:
    """Keep structural lines, bullets/steps, fenced blocks, and the first prose line per section."""
    kept: List[str] = []
    in_fence = False
    prose_count = 0
    for ln in raw_text.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("```"):
            in_fence = not in_fence
            kept.append(ln.rstrip())
            continue
        if in_fence or s.startswith("#") or re.match(r"^(?:[-*+]|\d+\.|\[[ xX]\])\s+", s):
            kept.append(ln.rstrip())
        elif prose_count < 1:
            kept.append(ln.rstrip())
            prose_count += 1
    return kept


def _fit_section_to_budget(heading: str, lines: List[str], budget: int) -> Tuple[str, bool]:
    """Fit one section into `budget` chars, balancing rules proportionally so trailing items survive."""
    body_lines = list(lines)
    if heading and body_lines and heading != "Overview":
        sec_text = f"## {heading}\n" + "\n".join(body_lines)
    else:
        sec_text = "\n".join(body_lines)
    if len(sec_text) <= budget:
        return sec_text, False

    prefix = f"## {heading}\n" if (heading and heading != "Overview") else ""
    avail = max(64, budget - len(prefix))
    if not body_lines:
        return prefix.rstrip(), True

    sep_cost = max(0, len(body_lines) - 1)
    per_line_cap = max(28, int((avail - sep_cost) / len(body_lines)))
    if per_line_cap < 72:
        # Keep readable, high-value full lines instead of chopping every line to an unreadable 30-char stub
        out_lines: List[str] = []
        used = 0
        for ln in body_lines:
            clean_ln = ln if len(ln) <= 240 else (ln[:237].rstrip() + "…")
            need = len(clean_ln) + (1 if out_lines else 0)
            if used + need > avail:
                rem = avail - used - (1 if out_lines else 0)
                if rem >= 40:
                    out_lines.append(clean_ln[: rem - 1].rstrip() + "…")
                break
            out_lines.append(clean_ln)
            used += need
        return (prefix + "\n".join(out_lines)).strip(), True

    condensed: List[str] = []
    for ln in body_lines:
        if len(ln) <= per_line_cap:
            condensed.append(ln)
        else:
            head_n = int((per_line_cap - 3) * 0.62)
            tail_n = max(16, per_line_cap - 3 - head_n)
            condensed.append(ln[:head_n].rstrip() + " … " + ln[-tail_n:].lstrip())

    out_lines: List[str] = []
    used = 0
    for ln in condensed:
        need = len(ln) + (1 if out_lines else 0)
        if used + need > avail:
            rem = avail - used - (1 if out_lines else 0)
            if rem >= 32:
                out_lines.append(ln[: rem - 1].rstrip() + "…")
            elif not out_lines:
                out_lines.append(ln[: max(24, avail - 1)].rstrip() + "…")
            break
        out_lines.append(ln)
        used += need
    return (prefix + "\n".join(out_lines)).strip(), True


def _compress_markdown_losslessly(text: str, max_chars: int = 2400) -> str:
    """Compress markdown skill text with section-balanced budgets so Guardrails/Anti-Slop survive."""
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

    raw_sections = _parse_sections(compact)
    prepared: List[Tuple[str, List[str], float, int]] = []
    drop_overview = max_chars < 1400 and len(raw_sections) > 1
    drop_prose_for_budget = max_chars < 1400
    for heading, body in raw_sections:
        if drop_overview and heading == "Overview":
            continue
        lines = _compact_section_lines(body)
        if drop_prose_for_budget:
            rule_lines = [ln for ln in lines if ln.strip().startswith("#") or re.match(r"^(?:[-*+]|\d+\.|\[[ xX]\])\s+", ln.strip())]
            if rule_lines:
                lines = rule_lines
        if not lines:
            continue
        rendered = (f"## {heading}\n" if heading != "Overview" else "") + "\n".join(lines)
        if heading == "Overview":
            weight = 0.55
        elif _PRIORITY_SECTION_RE.search(heading):
            weight = 1.55
        else:
            weight = 1.0
        prepared.append((heading, lines, weight, len(rendered)))

    if not prepared:
        return compact[: max_chars - 1].rstrip() + "…"

    total_compacted = sum(p[3] for p in prepared) + max(0, len(prepared) - 1) * 2
    if total_compacted <= max_chars:
        out_secs = [
            (f"## {h}\n" if h != "Overview" else "") + "\n".join(lns)
            for h, lns, _, _ in prepared
        ]
        return "\n\n".join(out_secs).strip()

    footer = "... [More details available via read_skill(name=..., section=...)]"
    budget_left = max(240, max_chars - len(footer) - max(0, len(prepared) - 1) * 2 - 4)

    # Water-fill unused budget from short sections to longer/priority sections
    allocs: Dict[int, int] = {}
    remaining_idxs = list(range(len(prepared)))
    while remaining_idxs:
        w_sum = sum(prepared[i][2] for i in remaining_idxs) or 1.0
        settled = False
        for i in list(remaining_idxs):
            share = max(90, int(budget_left * (prepared[i][2] / w_sum)))
            if prepared[i][3] <= share:
                allocs[i] = prepared[i][3]
                budget_left = max(0, budget_left - prepared[i][3])
                remaining_idxs.remove(i)
                settled = True
        if not settled:
            w_sum = sum(prepared[i][2] for i in remaining_idxs) or 1.0
            for i in remaining_idxs:
                allocs[i] = max(90, int(budget_left * (prepared[i][2] / w_sum)))
            break

    fitted_parts: List[str] = []
    any_trimmed = False
    for i, (h, lns, _, _) in enumerate(prepared):
        sec_str, was_trimmed = _fit_section_to_budget(h, lns, allocs.get(i, 160))
        if sec_str:
            fitted_parts.append(sec_str)
        if was_trimmed:
            any_trimmed = True

    result = "\n\n".join(fitted_parts).strip()
    if any_trimmed and footer not in result:
        result = f"{result}\n{footer}"
    return result


@lru_cache(maxsize=len(SKILL_SPECS) + 4)
def _excerpt(name: str) -> str:
    """Compact offline excerpt used by `skill_guidance` (backwards-compatible)."""
    doc = _raw_skill_doc(name)
    spec = SKILL_BY_NAME[name]
    paragraphs: List[str] = []
    if name in {"unslop", "design-craft"}:
        paragraphs.append("Credit: unslop by Damir Sokolovsky (@heydamir), CC BY 4.0.")
    if spec.excerpt_sections:
        for title in spec.excerpt_sections:
            section = re.search(rf"^#{{2,3}} {re.escape(title)}\s*\n(.*?)(?=^#{{2}} |\Z)", doc, re.M | re.S)
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
    """Ultra-compact index for the web-agent workflow/skill judge (~800 bytes)."""
    lines = ["Available WSA skills (pick ONLY relevant slugs in JSON \"skills\"):"]
    for spec in CANONICAL_SPECS:
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
        sec_list = ", ".join(appointment.security_skills or ("security-guardrails",))
        lines.append(
            f"• [post-build security pass]: After website code is written, read `{sec_list}` via `read_skill` "
            "and fix any XSS, DOM injection, auth/IDOR, CSP/CORS, validation, or secret issues before concluding."
        )
    try:
        from .toolchain import SKILL_DOMAIN_TOOLCHAINS, SKILL_TO_DOMAIN
        doms = list(dict.fromkeys(SKILL_TO_DOMAIN[n] for n in all_names if n in SKILL_TO_DOMAIN))
        if doms:
            primary_dom = "3d-motion" if "3d-motion" in doms else doms[0]
            tspec = SKILL_DOMAIN_TOOLCHAINS[primary_dom]
            lines.append(
                f"• [assigned toolchain — {primary_dom}]: **{tspec['primary_language']}** "
                f"(verify: {tspec['verify']})."
            )
    except Exception:
        pass
    lines.append(
        "Need another skill? Call `read_skill(name=\"catalog\")` to browse all 18 WSA skills."
    )
    return "\n".join(lines)


def list_skills_catalog() -> str:
    """Format a compact, scannable catalog of all bundled WSA skills for `read_skill(name='catalog')`."""
    try:
        from .toolchain import SKILL_DOMAIN_TOOLCHAINS
    except Exception:
        SKILL_DOMAIN_TOOLCHAINS = {}
    lines = [f"[OK] WSA Bundled Skills Catalog ({len(SKILL_SPECS)} skills across {len(CATEGORY_LABELS)} domains):"]
    for cat_key, cat_label in CATEGORY_LABELS.items():
        tc_info = SKILL_DOMAIN_TOOLCHAINS.get(cat_key, {})
        tc_suffix = f" — Assigned Toolchain: {tc_info['primary_language']}" if tc_info.get("primary_language") else ""
        lines.append(f"\n## {cat_label}{tc_suffix}")
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
    max_chars: int = 8600,
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
    header_overhead = 260 + 165 * len(resolved)
    per_skill_budget = max(720, int((max_chars - header_overhead) / max(1, len(resolved))))

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
        if slug in {"unslop", "design-craft"} and "@heydamir" not in body:
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
