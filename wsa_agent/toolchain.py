"""Language-agnostic toolchain intelligence.

Picks the best language per layer for a task (any mainstream language), describes how to verify,
install packages for, and install the runtime of each language. Pure logic (argv plans only) so it
is unit-testable; execution lives in `tools.py` behind the approval gate.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ─── Language registry ────────────────────────────────────────────────────────
# markers: manifest files proving the project uses it. bins: executables required to build/run.
# verify: how an agent proves correctness. pm: package manager (None = manifest edit only).

LANGUAGES: Dict[str, Dict[str, Any]] = {
    "python": dict(label="Python", markers=("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "Pipfile"),
                   bins=("python",), verify="`run_tests` (pytest), `py_compile`, `mypy`/`ruff` if configured", pm="pip"),
    "node": dict(label="TypeScript/JavaScript", markers=("package.json",), bins=("node", "npm"),
                 verify="`node --check`, `tsc --noEmit`, vitest/jest, bundler build", pm="npm"),
    "rust": dict(label="Rust", markers=("Cargo.toml",), bins=("cargo", "rustc"),
                 verify="`cargo check`, `cargo clippy`, `cargo test`", pm="cargo"),
    "go": dict(label="Go", markers=("go.mod",), bins=("go",), verify="`go vet ./...`, `go build ./...`, `go test ./...`", pm="go"),
    "php": dict(label="PHP", markers=("composer.json",), bins=("php", "composer"),
                verify="`php -l <file>`, `composer test`/phpunit, phpstan if configured", pm="composer"),
    "ruby": dict(label="Ruby", markers=("Gemfile",), bins=("ruby", "bundle"),
                 verify="`ruby -c <file>`, `bundle exec rspec`/`rake test`", pm="bundler"),
    "java": dict(label="Java/Kotlin (JVM)", markers=("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle"),
                 bins=("java",), verify="`mvn -q test` or `gradle test`", pm=None),
    "csharp": dict(label="C#/.NET", markers=("*.csproj", "*.sln", "*.fsproj"), bins=("dotnet",),
                   verify="`dotnet build`, `dotnet test`", pm="dotnet"),
    "cpp": dict(label="C/C++", markers=("CMakeLists.txt", "Makefile", "meson.build"), bins=("cmake",),
                verify="`cmake --build`, `ctest`, compiler warnings as errors", pm=None),
    "swift": dict(label="Swift", markers=("Package.swift",), bins=("swift",), verify="`swift build`, `swift test`", pm="swift"),
    "dart": dict(label="Dart/Flutter", markers=("pubspec.yaml",), bins=("dart",), verify="`dart analyze`, `dart test`", pm="dart"),
    "elixir": dict(label="Elixir", markers=("mix.exs",), bins=("mix",), verify="`mix compile --warnings-as-errors`, `mix test`", pm="mix"),
    "zig": dict(label="Zig", markers=("build.zig",), bins=("zig",), verify="`zig build`, `zig build test`", pm=None),
}

# Explicit user wording always wins over heuristics (and over an existing project's stack).
_EXPLICIT: List[Tuple[str, str]] = [
    (r"\brust(?:lang)?\b|\bcargo\b|\btokio\b|\bactix\b|\baxum\b|\btauri\b", "rust"),
    (r"\bgolang\b|\bgo\s+lang\b|\b(?:in|using|with|use|written\s+in)\s+go\b|\bgin-gonic\b", "go"),
    (r"\bphp\b|\blaravel\b|\bsymfony\b|\bwordpress\b|\bcomposer\b", "php"),
    (r"\bruby\b|\brails\b|\bsinatra\b", "ruby"),
    (r"\bjava\b(?!\s*script)|\bspring\s*boot\b|\bkotlin\b|\bandroid\b|\bjetpack\s*compose\b|\bgradle\b|\bmaven\b", "java"),
    (r"\bc#|\bcsharp\b|\.net\b|\bdotnet\b|\basp\.net\b|\bblazor\b|\bunity\b|\bwinui\b|\bwpf\b|\bwinforms\b", "csharp"),
    (r"\bc\+\+|\bcpp\b|\bunreal\b|\bfirmware\b|\bembedded\b|\barduino\b", "cpp"),
    (r"\bswift(?:ui)?\b|\bappkit\b|\bcocoa\b|\bxcode\b|\bios\s+app(?:lication)?\b|\biphone\s+app\b|\bipad\s+app\b|\bmac(?:os)?\s+(?:desktop\s+|menu\s*bar\s+|native\s+)?app(?:lication)?\b", "swift"),
    (r"\bdart\b|\bflutter\b", "dart"),
    (r"\belixir\b|\bphoenix\b", "elixir"),
    (r"\bzig\b", "zig"),
    (r"\bpython\b|\bdjango\b|\bflask\b|\bfastapi\b|\bpandas\b|\bnumpy\b|\bpytorch\b|\btensorflow\b|\bscikit-learn\b|\bjupyter\b", "python"),
    (r"\btypescript\b|\bnode(?:\.?js)?\b|\bnext\.?js\b|\bexpress\b|\bvite\b|\breact\b|\bvue\b|\bsvelte\b|\bastro\b|\bnest(?:js)?\b", "node"),
]

# Capability-based picks when the user does not name a language: (regex, language, reason)
_CAPABILITY: List[Tuple[str, str, str]] = [
    (r"\b(?:windows\s+(?:desktop\s+|native\s+|gui\s+)?app(?:lication)?|windows\s*desktop|winui|wpf|winforms|\.exe\s+desktop)\b",
     "csharp", "Native Windows application: C# / .NET 8+ (`*.csproj`, WinUI/WPF/.NET Desktop)."),
    (r"\b(?:android\s+(?:mobile\s+|native\s+)?app(?:lication)?|jetpack\s*compose|\bapk\b)\b",
     "java", "Native Android application: Kotlin / Java with Gradle (`build.gradle.kts`)."),
    (r"\b(?:mac(?:os)?\s+(?:desktop\s+|native\s+|menu\s*bar\s+)?app(?:lication)?|ios\s+(?:mobile\s+|native\s+)?app(?:lication)?|iphone\s+app|ipad\s+app|apple\s+watch\s+app)\b",
     "swift", "Native macOS / iOS Apple application: Swift / SwiftUI (`Package.swift`)."),
    (r"\b(?:cross[\s-]?platform\s+mobile|mobile\s+app(?:\s+for\s+ios\s+and\s+android)?)\b",
     "dart", "Cross-platform mobile application: Dart / Flutter (`pubspec.yaml`)."),
    (r"\b(?:machine\s*learning|\bml\b|\bai\s+(?:model|pipeline|agent|inference|training)\b|deep\s*learning|neural\s*network|\bllm\b|\brag\b|embeddings?|computer\s*vision|\bnlp\b|data\s*(?:analysis|science|pipeline)|etl|notebook|scientific|scraper|crawler|automation|scripting)\b",
     "python", "AI / ML / Data Science / Automation: Python (`pyproject.toml` / `.venv`, PyTorch/scikit-learn/FastAPI)."),
    (r"\b(?:systems?\s+programming|high[\s-]?performance|memory[\s-]?safe|wasm|webassembly|game\s*engine|compiler|parser|crypto(?:graphy)?|low[\s-]?latency|os\s+kernel|device\s+driver)\b",
     "rust", "Performance & memory-safety critical systems engineering: Rust (`Cargo.toml`)."),
    (r"\b(?:microservices?|high\s*concurrency|network\s*service|devops|cli\s*tool|kubernetes|proxy|load\s*balancer|grpc|api\s*gateway|distributed\s*service)\b",
     "go", "High-concurrency network services & cloud/CLI tooling: Go (`go.mod`, single static binary)."),
    (r"\b(?:cms|shared\s*hosting|blog\s*engine|e-?commerce\s*store)\b", "php", "CMS / e-commerce web backend: PHP (`composer.json`)."),
    (r"\b(?:real[\s-]?time\s*chat|fault[\s-]?tolerant|websocket\s*cluster)\b", "elixir", "Soft-realtime fault-tolerant distributed backend: Elixir/BEAM (`mix.exs`)."),
    (r"\b(?:enterprise\s+(?:backend|api|system)|corporate\s+api)\b", "csharp", "Enterprise backend system: C# / ASP.NET Core (`*.csproj`)."),
]

_NATIVE_PLATFORM_APP_RE = re.compile(
    r"\b(?:windows\s+(?:desktop\s+|native\s+|gui\s+)?app(?:lication)?|windows\s*desktop|winui|wpf|winforms|"
    r"android\s+(?:mobile\s+|native\s+)?app(?:lication)?|jetpack\s*compose|"
    r"mac(?:os)?\s+(?:desktop\s+|native\s+|menu\s*bar\s+)?app(?:lication)?|ios\s+(?:mobile\s+|native\s+)?app(?:lication)?|"
    r"iphone\s+app|ipad\s+app|swiftui|appkit|flutter\s+app|cross[\s-]?platform\s+mobile)\b",
    re.IGNORECASE,
)
_EXPLICIT_WEB_TARGET_RE = re.compile(
    r"\b(?:website|web\s*site|web\s*app|landing\s*page|homepage|three\.?js|webgl|browser\s+app|react|vue|svelte|next\.?js|astro|vite)\b",
    re.IGNORECASE,
)
_FRONTEND_RE = re.compile(
    r"\b(?:website|web\s*site|web\s*app|landing\s*page|homepage|front[\s-]?end|ui|ux|dashboard|portfolio|"
    r"three\.?js|webgl|3d|shader|tailwind|css|html|animation|gsap|component|page|interface|design|redesign|premium|react|vue|svelte|next\.?js|astro)\b",
    re.IGNORECASE,
)
_BACKEND_RE = re.compile(
    r"\b(?:api|backend|server|database|sql|cli|script|pipeline|etl|bot|service|daemon|library|crate|package|"
    r"app(?:lication)?|desktop|mobile|android|ios|macos|windows|microservice|engine|compiler|parser|model)\b",
    re.IGNORECASE,
)

# binary → installable runtime key
RUNTIME_OF_BIN: Dict[str, str] = {
    "node": "node", "npm": "node", "npx": "node", "python": "python", "python3": "python", "pip": "python",
    "cargo": "rust", "rustc": "rust", "rustup": "rust", "go": "go", "php": "php", "composer": "composer",
    "ruby": "ruby", "gem": "ruby", "bundle": "ruby", "java": "java", "javac": "java", "mvn": "maven",
    "gradle": "gradle", "dotnet": "dotnet", "dart": "dart", "mix": "elixir", "elixir": "elixir", "zig": "zig",
    "cmake": "cmake", "gcc": "cpp", "g++": "cpp", "clang": "cpp", "git": "git", "deno": "deno", "bun": "bun",
}

# Allowlisted runtime installers. IDs are fixed here; user text never reaches the command line.
RUNTIME_INSTALLERS: Dict[str, Dict[str, Any]] = {
    "node":     dict(bins=("node", "npm"), winget="OpenJS.NodeJS.LTS", brew="node", apt=("nodejs", "npm"), dnf=("nodejs", "npm"), pacman=("nodejs", "npm"), url="https://nodejs.org"),
    "python":   dict(bins=("python",), winget="Python.Python.3.12", brew="python", apt=("python3", "python3-venv", "python3-pip"), dnf=("python3", "python3-pip"), pacman=("python", "python-pip"), url="https://python.org"),
    "rust":     dict(bins=("cargo", "rustc"), winget="Rustlang.Rustup", brew="rustup", apt=("cargo", "rustc"), dnf=("cargo", "rust"), pacman=("rust",), post=(("rustup", "default", "stable"),), url="https://rustup.rs"),
    "go":       dict(bins=("go",), winget="GoLang.Go", brew="go", apt=("golang-go",), dnf=("golang",), pacman=("go",), url="https://go.dev/dl"),
    "php":      dict(bins=("php",), winget="PHP.PHP.8.4", brew="php", apt=("php-cli",), dnf=("php-cli",), pacman=("php",), url="https://php.net"),
    "composer": dict(bins=("composer",), winget="Composer.Composer", brew="composer", apt=("composer",), dnf=("composer",), pacman=("composer",), url="https://getcomposer.org"),
    "ruby":     dict(bins=("ruby", "bundle"), winget="RubyInstallerTeam.RubyWithDevKit.3.3", brew="ruby", apt=("ruby-full",), dnf=("ruby",), pacman=("ruby",), url="https://ruby-lang.org"),
    "java":     dict(bins=("java",), winget="EclipseAdoptium.Temurin.21.JDK", brew="openjdk", apt=("default-jdk",), dnf=("java-21-openjdk-devel",), pacman=("jdk-openjdk",), url="https://adoptium.net"),
    "maven":    dict(bins=("mvn",), winget=None, brew="maven", apt=("maven",), dnf=("maven",), pacman=("maven",), url="https://maven.apache.org"),
    "gradle":   dict(bins=("gradle",), winget="Gradle.Gradle", brew="gradle", apt=("gradle",), dnf=("gradle",), pacman=("gradle",), url="https://gradle.org"),
    "dotnet":   dict(bins=("dotnet",), winget="Microsoft.DotNet.SDK.8", brew="dotnet", apt=("dotnet-sdk-8.0",), dnf=("dotnet-sdk-8.0",), pacman=("dotnet-sdk",), url="https://dotnet.microsoft.com"),
    "dart":     dict(bins=("dart",), winget="Google.DartSDK", brew=None, apt=None, dnf=None, pacman=None, url="https://dart.dev/get-dart"),
    "elixir":   dict(bins=("mix", "elixir"), winget=None, brew="elixir", apt=("elixir",), dnf=("elixir",), pacman=("elixir",), url="https://elixir-lang.org/install.html"),
    "zig":      dict(bins=("zig",), winget="zig.zig", brew="zig", apt=None, dnf=("zig",), pacman=("zig",), url="https://ziglang.org/download"),
    "cmake":    dict(bins=("cmake",), winget="Kitware.CMake", brew="cmake", apt=("cmake",), dnf=("cmake",), pacman=("cmake",), url="https://cmake.org"),
    "cpp":      dict(bins=("g++", "cmake"), winget="Kitware.CMake", brew="cmake", apt=("build-essential", "cmake"), dnf=("gcc-c++", "cmake"), pacman=("base-devel", "cmake"), url="https://visualstudio.microsoft.com/downloads (Build Tools) or your distro's gcc"),
    "git":      dict(bins=("git",), winget="Git.Git", brew="git", apt=("git",), dnf=("git",), pacman=("git",), url="https://git-scm.com"),
    "deno":     dict(bins=("deno",), winget="DenoLand.Deno", brew="deno", apt=None, dnf=None, pacman=("deno",), url="https://deno.com"),
    "bun":      dict(bins=("bun",), winget="Oven-sh.Bun", brew=None, apt=None, dnf=None, pacman=None, url="https://bun.sh"),
}


# ─── Probing ──────────────────────────────────────────────────────────────────

_EXTRA_PATH_DIRS = (
    "~/.cargo/bin", "~/go/bin", "~/.dotnet", "~/.dotnet/tools", "~/.local/bin", "~/.pub-cache/bin",
    "/usr/local/go/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/local/share/dotnet",
)


def refresh_path() -> None:
    """Pick up PATH entries added by installers during this process (registry on Windows, well-known dirs elsewhere)."""
    parts: List[str] = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if os.name == "nt":
        try:
            import winreg  # type: ignore
            for hive, sub in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                              (winreg.HKEY_CURRENT_USER, "Environment")):
                try:
                    with winreg.OpenKey(hive, sub) as key:
                        val, _ = winreg.QueryValueEx(key, "Path")
                        parts.extend(os.path.expandvars(str(val)).split(os.pathsep))
                except OSError:
                    continue
        except ImportError:
            pass
    for d in _EXTRA_PATH_DIRS:
        p = os.path.expanduser(d)
        if os.path.isdir(p):
            parts.append(p)
    seen: set = set()
    merged: List[str] = []
    for p in parts:
        k = os.path.normcase(os.path.normpath(p))
        if p and k not in seen:
            seen.add(k)
            merged.append(p)
    os.environ["PATH"] = os.pathsep.join(merged)


def probe_binaries(extra: Optional[List[str]] = None) -> Dict[str, Optional[str]]:
    """Return {binary: resolved path or None} for every runtime WSA may drive."""
    names = set(RUNTIME_OF_BIN) | {"npx", "pnpm", "yarn", "tsc"} | set(extra or [])
    names.discard("python3")
    found: Dict[str, Optional[str]] = {n: shutil.which(n) for n in sorted(names)}
    if not found.get("python"):
        found["python"] = shutil.which("python3") or shutil.which("py")
    return found


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def node_package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    if (root / "bun.lockb").is_file() or (root / "bun.lock").is_file():
        return "bun"
    return "npm"


def _has_marker(root: Path, marker: str) -> bool:
    try:
        return any(root.glob(marker)) if "*" in marker else (root / marker).is_file()
    except OSError:
        return False


def classify_stack(root: Path) -> Dict[str, Any]:
    """Describe the existing project stack from manifests on disk (any registered language)."""
    pkg_path = root / "package.json"
    pkg = _read_json(pkg_path) if pkg_path.is_file() else {}
    deps: Dict[str, Any] = {}
    for key in ("dependencies", "devDependencies"):
        val = pkg.get(key)
        if isinstance(val, dict):
            deps.update(val)
    detected = [lang for lang, spec in LANGUAGES.items() if any(_has_marker(root, m) for m in spec["markers"])]
    has_node = "node" in detected
    has_ts = (root / "tsconfig.json").is_file() or "typescript" in deps
    has_python = "python" in detected
    has_html = (root / "index.html").is_file()
    node_tests = [n for n in ("vitest", "jest", "mocha", "playwright") if n in deps]
    tests_dir = root / "tests"
    py_tests = (root / "pytest.ini").is_file() or (root / "conftest.py").is_file() or (tests_dir.is_dir() and any(tests_dir.glob("test_*.py")))
    bundler = next((n for n in ("vite", "webpack", "parcel", "esbuild", "next", "astro") if n in deps), "")
    return {
        "languages": detected,
        "node": has_node,
        "typescript": has_ts,
        "python": has_python,
        "static_html": has_html and not has_node,
        "package_manager": node_package_manager(root) if has_node else "",
        "bundler": bundler,
        "node_tests": node_tests,
        "python_tests": bool(py_tests),
        "scripts": sorted((pkg.get("scripts") or {}).keys()) if isinstance(pkg.get("scripts"), dict) else [],
        "empty": not (detected or has_html),
    }


# ─── Task → language selection ────────────────────────────────────────────────

def task_wants_frontend(text: str) -> bool:
    s = text or ""
    if _NATIVE_PLATFORM_APP_RE.search(s) and not _EXPLICIT_WEB_TARGET_RE.search(s):
        return False
    return bool(_FRONTEND_RE.search(s))


def task_wants_backend(text: str) -> bool:
    return bool(_BACKEND_RE.search(text or ""))


def explicit_languages(text: str) -> List[str]:
    """Languages the user named (order preserved)."""
    out: List[str] = []
    for pat, lang in _EXPLICIT:
        if re.search(pat, text or "", re.IGNORECASE) and lang not in out:
            out.append(lang)
    return out


def missing_runtimes(lang: str, bins: Dict[str, Optional[str]]) -> List[str]:
    """Runtime keys (for install_runtime) still required to work in `lang`."""
    spec = LANGUAGES.get(lang)
    if not spec:
        return []
    need: List[str] = []
    for b in spec["bins"]:
        if not bins.get(b) and not (b == "python" and (bins.get("python") or shutil.which("python3"))):
            key = RUNTIME_OF_BIN.get(b)
            if key and key not in need:
                need.append(key)
    if lang == "php" and "composer" not in need and not bins.get("composer"):
        need.append("composer")
    if lang == "java" and not (bins.get("mvn") or bins.get("gradle")):
        pass  # wrappers (mvnw/gradlew) are common; do not force an install
    return need


def recommend_stack(task_text: str, root: Path, binaries: Optional[Dict[str, Optional[str]]] = None) -> Dict[str, Any]:
    """Choose languages per layer. Order: explicit user wording > existing project stack > capability fit."""
    text = task_text or ""
    bins = binaries if binaries is not None else probe_binaries()
    stack = classify_stack(root)
    explicit = explicit_languages(text)
    frontend = task_wants_frontend(text)
    backend = task_wants_backend(text)
    has_node_rt = bool(bins.get("node") and bins.get("npm"))
    notes: List[str] = []
    ui_lang = ""
    server_key = ""
    server_label = ""
    reason = ""

    explicit_backend = [l for l in explicit if l != "node"]
    existing_backend = [l for l in stack["languages"] if l != "node"]

    # ── UI layer
    if frontend:
        if "node" in explicit or stack["node"] or stack["typescript"]:
            ui_lang = "TypeScript" if stack["typescript"] or not stack["node"] else "JavaScript (ES modules)"
            notes.append(
                "Node project: keep and extend it."
                if stack["node"]
                else (
                    "New UI/3D project on a host with Node.js: MANDATORY Node.js + TypeScript + Vite toolchain (`package.json`, `tsconfig.json`, `vite.config.ts`, `src/**/*.ts` or `.tsx`). "
                    "FORBIDDEN: do NOT fall back to lazy vanilla `js/*.js` or raw CDN importmaps. Task 1 must scaffold `package.json` + `tsconfig.json` + `vite.config.ts`, "
                    "call `install_dependencies(packages=[\"typescript\", \"vite\", ...], ecosystem=\"node\")` (add `\"three\"`, `\"@types/three\"`, `\"gsap\"` for 3D sites), "
                    "and verify with `npx tsc --noEmit` + `npx vite build --base=./` (which `visual_inspect` serves from `dist/`)."
                )
            )
            if not has_node_rt:
                notes.append("Node.js missing: call install_runtime(name=\"node\") before scaffolding.")
        elif stack["static_html"]:
            ui_lang = "JavaScript (ES modules, no-build)"
            notes.append("Existing static HTML/ES-module site; extend in place — do NOT rewrite into another framework unless asked.")
        elif explicit_backend:
            ui_lang = "HTML/CSS + ES modules (served by the chosen backend framework)"
            notes.append("Backend language was requested explicitly; keep the UI as modern HTML/CSS/ES modules or the framework's templates.")
        elif has_node_rt:
            ui_lang = "TypeScript"
            notes.append(
                "New UI/3D project on a host with Node.js: MANDATORY Node.js + TypeScript + Vite toolchain (`package.json`, `tsconfig.json`, `vite.config.ts`, `src/**/*.ts` or `.tsx`). "
                "FORBIDDEN: do NOT fall back to lazy vanilla `js/*.js` or raw CDN importmaps. Task 1 must scaffold `package.json` + `tsconfig.json` + `vite.config.ts`, "
                "call `install_dependencies(packages=[\"typescript\", \"vite\", ...], ecosystem=\"node\")` (add `\"three\"`, `\"@types/three\"`, `\"gsap\"` for 3D sites), "
                "and verify with `npx tsc --noEmit` + `npx vite build --base=./` (which `visual_inspect` serves from `dist/`)."
            )
        else:
            ui_lang = "JavaScript (ES modules, no-build)"
            notes.append("Node.js is not installed: use no-build ES modules + CSS now, or call install_runtime(name=\"node\") to unlock TypeScript/Vite.")

    # ── Backend / non-UI layer
    if backend or not frontend or explicit_backend:
        if explicit_backend:
            server_key, reason = explicit_backend[0], "Requested explicitly by the user."
        elif existing_backend and not (frontend and not backend):
            server_key, reason = existing_backend[0], "Existing project language."
        elif stack["node"] and not existing_backend:
            server_key, reason = "node", "Existing Node project."
        else:
            for pat, lang, why in _CAPABILITY:
                if re.search(pat, text, re.IGNORECASE):
                    server_key, reason = lang, why
                    break
            if not server_key:
                if "node" in explicit:
                    server_key, reason = "node", "Requested explicitly by the user."
                elif frontend and (has_node_rt or not bins.get("python")):
                    server_key, reason = "node", "Same toolchain as the UI keeps the stack small."
                else:
                    server_key, reason = "python", "General-purpose default for scripts/APIs."
        server_label = LANGUAGES[server_key]["label"] if server_key in LANGUAGES else server_key
        if server_key == "node":
            server_label = "TypeScript" if (stack["typescript"] or not stack["node"]) else "JavaScript"
        if reason:
            notes.append(f"Backend: {server_label}. {reason}")

    needed: List[str] = []
    for lang in {server_key, "node" if ui_lang.startswith(("TypeScript", "JavaScript")) and frontend else ""}:
        for rt in missing_runtimes(lang, bins):
            if rt not in needed:
                needed.append(rt)

    return {
        "ui_language": ui_lang,
        "server_language": server_label,
        "server_key": server_key,
        "has_node_runtime": has_node_rt,
        "missing_runtimes": needed,
        "stack": stack,
        "notes": notes,
    }


# ─── Skill-Domain → Authoritative Toolchain Assignment ────────────────────────

SKILL_DOMAIN_TOOLCHAINS: Dict[str, Dict[str, Any]] = {
    "ui-design": {
        "label": "UI & Anti-Slop (Normal Websites, Web Apps & Dashboards)",
        "skills": ("frontend-design", "unslop", "impeccable", "web-design-guidelines"),
        "primary_language": "TypeScript (Node.js + Vite)",
        "required_packages": ("typescript", "vite"),
        "optional_packages": ("gsap",),
        "scaffold_files": ("package.json", "tsconfig.json", "vite.config.ts", "index.html", "src/main.ts"),
        "verify": "`npx tsc --noEmit`, `npx vite build --base=./`, `visual_inspect`",
        "rule": (
            "MANDATORY for new websites/UIs when Node.js is available: use Node.js + TypeScript + Vite "
            "(`package.json`, `tsconfig.json`, `vite.config.ts`, `src/**/*.ts` or `.tsx`). "
            "Strictly FORBIDDEN: do NOT fall back to lazy vanilla `js/*.js` or Python HTML-string generation. "
            "For existing projects on disk, preserve and extend the existing workspace stack."
        ),
    },
    "3d-motion": {
        "label": "3D Websites, WebGL / Three.js, Custom GLSL Shaders & GSAP Motion",
        "skills": ("threejs-skills", "img2threejs", "gsap-skills", "dream-loop"),
        "primary_language": "TypeScript (Node.js + Vite + Three.js + GSAP + GLSL)",
        "required_packages": ("typescript", "vite", "three", "@types/three", "gsap"),
        "optional_packages": (),
        "scaffold_files": ("package.json", "tsconfig.json", "vite.config.ts", "index.html", "src/main.ts"),
        "verify": "`npx tsc --noEmit`, `npx vite build --base=./`, `visual_inspect` (desktop + mobile + `disable_webgl=True`)",
        "rule": (
            "MANDATORY for new 3D websites when Node.js is available: use Node.js + TypeScript + Vite + Three.js "
            "(`three`, `@types/three`) + GSAP (`gsap`) + typed GLSL shader modules (`src/**/*.ts`). "
            "Strictly FORBIDDEN: do NOT fall back to lazy vanilla `js/*.js` or unbundled CDN scripts. "
            "Task 1 must scaffold `package.json` + `tsconfig.json` + `vite.config.ts` and call "
            "`install_dependencies(packages=[\"typescript\", \"vite\", \"three\", \"@types/three\", \"gsap\"], ecosystem=\"node\")`."
        ),
    },
    "debugging-qa": {
        "label": "Systematic Debugging, Root-Cause Bug Fixing & Live Browser QA",
        "skills": ("systematic-debugging", "verification-before-completion", "chrome-devtools-qa"),
        "primary_language": "Target Codebase's Native Language & Compiler/Test Runner",
        "required_packages": (),
        "optional_packages": (),
        "scaffold_files": (),
        "verify": "`run_tests` (auto-matched runner), native typechecker/compiler (`tsc --noEmit`, `py_compile`, `cargo check`, `go vet`, `php -l`, `dotnet build`), `visual_inspect`/`browser_inspect`",
        "rule": (
            "Always debug, reproduce, and verify in the target codebase's own native language and compiler/test runner "
            "(never write a dummy Python test to verify a TypeScript/JS, Rust, Go, PHP, or C# fix)."
        ),
    },
    "security": {
        "label": "Adversarial Website Security Auditing, OWASP Guardrails & Skill Safety",
        "skills": ("security-audit", "semgrep-guardrails", "skill-spector"),
        "primary_language": "In-Place Native Language Hardening (TypeScript strict DOM / Parameterized Backend)",
        "required_packages": (),
        "optional_packages": (),
        "scaffold_files": (),
        "verify": "`npx tsc --noEmit` / `npx vite build` / `run_tests` + `visual_inspect` (0 console/CSP errors)",
        "rule": (
            "Harden code in its native language: enforce strict TypeScript DOM APIs (`textContent`/`createElement` over `innerHTML`), "
            "CSP & Referrer-Policy headers/meta, URL scheme allowlists, `rel=\"noopener noreferrer\"`, and parameterized backend queries."
        ),
    },
    "engineering": {
        "label": "Core Software Engineering, Systems, APIs, Data/ML & TDD",
        "skills": ("karpathy-skills", "test-driven-development", "grill-and-git-guardrails", "context7-docs"),
        "primary_language": "Best-Fit Language by Workload (TypeScript/Node for web apps, Rust for systems/CLI/perf, Go for concurrent services, Python for AI/ML/data, PHP/C#/Java/Ruby/Swift/Dart/Elixir/Zig by domain)",
        "required_packages": (),
        "optional_packages": (),
        "scaffold_files": (),
        "verify": "`run_tests` + native compiler/typechecker (`tsc --noEmit`, `cargo test`, `go test`, `pytest`, `dotnet test`, `phpunit`)",
        "rule": (
            "Select the best-fit language by workload (explicit user choice > existing project stack > workload capability fit), "
            "write failing tests first (TDD), and verify with the ecosystem's native test runner."
        ),
    },
}

SKILL_TO_DOMAIN: Dict[str, str] = {
    skill: domain
    for domain, spec in SKILL_DOMAIN_TOOLCHAINS.items()
    for skill in spec["skills"]
}

_VANILLA_JS_REQUEST_RE = re.compile(
    r"\b(?:vanilla\s*(?:js|javascript)|no[\s-]?build|plain\s*(?:html|js|javascript)|"
    r"without\s*(?:node|npm|vite|bundler|typescript))\b",
    re.IGNORECASE,
)
_THREE_D_TASK_RE = re.compile(
    r"\b(?:3d|three\.?js|webgl|shader|glsl|planet|solar\s*system|orbit|canvas\s*3d|mesh|geometry)\b",
    re.IGNORECASE,
)


def requires_typescript_ui_scaffold(
    task_text: str,
    root: Path,
    binaries: Optional[Dict[str, Optional[str]]] = None,
) -> bool:
    """True when a new frontend/3D/website build on a Node-capable host must use TypeScript + Vite (not vanilla JS)."""
    text = str(task_text or "")
    if not task_wants_frontend(text) or _VANILLA_JS_REQUEST_RE.search(text):
        return False
    explicit = explicit_languages(text)
    if any(l != "node" for l in explicit) and "node" not in explicit:
        return False
    bins = binaries if binaries is not None else probe_binaries()
    if not (bins.get("node") and bins.get("npm")):
        return False
    stack = classify_stack(root)
    return bool(stack["empty"])


def skill_toolchain_directive(
    skills: Sequence[str],
    root: Optional[Path] = None,
    task_text: str = "",
) -> str:
    """Return a binding Skill-to-Toolchain assignment line block for the appointed skills."""
    domains: List[str] = []
    for s in skills or ():
        dom = SKILL_TO_DOMAIN.get(str(s).strip().lower())
        if dom and dom not in domains:
            domains.append(dom)
    if not domains:
        return ""
    lines = ["[WSA Skill-to-Toolchain Assignment (Binding)]:"]
    has_3d = "3d-motion" in domains or bool(_THREE_D_TASK_RE.search(task_text or ""))
    for dom in domains:
        spec = SKILL_DOMAIN_TOOLCHAINS[dom]
        if dom == "ui-design" and "3d-motion" in domains:
            continue  # Covered together by the stricter 3d-motion TypeScript+Vite+Three+GSAP rule
        if dom in ("ui-design", "3d-motion") and root is not None:
            st = classify_stack(root)
            if st["static_html"] and not st["node"]:
                lines.append(
                    f"• {spec['label']}: Existing static HTML/ES-module workspace detected — extend existing files in place "
                    "and verify with `node --check` + `visual_inspect`."
                )
                continue
        pkgs = ("typescript, vite, three, @types/three, gsap" if has_3d and dom in ("ui-design", "3d-motion")
                else ", ".join(spec["required_packages"]))
        pkg_note = f" (install via `install_dependencies`: {pkgs})" if pkgs else ""
        lines.append(f"• {spec['label']} -> **{spec['primary_language']}**{pkg_note}. {spec['rule']}")
    return "\n".join(lines)


REQUIRED_LANG_EXTS: Dict[str, Tuple[str, ...]] = {
    "rust": (".rs",),
    "go": (".go",),
    "csharp": (".cs", ".fs", ".xaml", ".csproj"),
    "java": (".kt", ".kts", ".java", ".gradle"),
    "swift": (".swift",),
    "dart": (".dart",),
    "php": (".php",),
    "ruby": (".rb",),
    "elixir": (".ex", ".exs"),
    "cpp": (".cpp", ".cc", ".cxx", ".c", ".h", ".hpp"),
    "zig": (".zig",),
}


def validate_architect_toolchain_plan(
    tasks: List[Dict[str, Any]],
    task_text: str,
    root: Path,
    binaries: Optional[Dict[str, Optional[str]]] = None,
) -> str:
    """Reject lazy fallback plans (vanilla `js/*.js` for web/3D, or lazy `.py` when Rust/Go/C#/Kotlin/Swift/Dart/PHP was assigned)."""
    if not tasks:
        return ""
    owned = [
        str(p).replace("\\", "/").strip().lower()
        for t in tasks
        if isinstance(t, dict)
        for p in (t.get("owned_files") or ())
        if str(p).strip()
    ]
    if not owned:
        return ""
    if requires_typescript_ui_scaffold(task_text, root, binaries):
        has_ts = any(p.endswith((".ts", ".tsx")) for p in owned)
        has_js = any(
            p.endswith((".js", ".mjs", ".cjs"))
            and not Path(p).name.startswith(("vite.config.", "eslint.config.", "postcss.config.", "tailwind.config."))
            for p in owned
        )
        has_pkg = any(Path(p).name == "package.json" for p in owned)
        has_tsconfig = any(Path(p).name == "tsconfig.json" for p in owned)
        if has_ts and has_pkg and has_tsconfig and not has_js:
            return ""
        reasons: List[str] = []
        if has_js or not has_ts:
            reasons.append("plan uses vanilla `.js` modules instead of TypeScript (`src/**/*.ts` or `.tsx`)")
        if not has_pkg or not has_tsconfig:
            reasons.append("plan is missing `package.json` and/or `tsconfig.json` in Task 1 `owned_files`")
        pkgs = '["typescript", "vite", "three", "@types/three", "gsap"]' if _THREE_D_TASK_RE.search(task_text or "") else '["typescript", "vite"]'
        return (
            "Lazy Vanilla HTML/CSS/JS Plan Rejected (" + "; ".join(reasons) + "). "
            "Node.js and npm are installed on this host, so new websites/3D sites MUST use Node.js + TypeScript + Vite: "
            f"Task 1 must own `package.json`, `tsconfig.json`, `vite.config.ts`, and `index.html` and call "
            f"`install_dependencies(packages={pkgs}, ecosystem=\"node\")`; all application code must be in `src/**/*.ts` (or `.tsx`), "
            "never `js/*.js`; and verification must run `npx tsc --noEmit` and `npx vite build --base=./` before `visual_inspect`."
        )

    # Check non-web platform/backend/systems language assignments in empty projects
    stack = classify_stack(root)
    if not stack["empty"]:
        return ""
    rec = recommend_stack(task_text, root, binaries)
    skey = rec.get("server_key") or ""
    req_exts = REQUIRED_LANG_EXTS.get(skey)
    if not req_exts:
        return ""
    has_assigned_ext = any(p.endswith(req_exts) for p in owned)
    has_lazy_fallback = any(p.endswith((".py", ".js", ".mjs", ".cjs")) for p in owned)
    if not has_assigned_ext and has_lazy_fallback:
        spec = LANGUAGES.get(skey, {})
        label = spec.get("label", skey)
        markers = ", ".join(spec.get("markers", ()))
        verify = spec.get("verify", "`run_tests`")
        return (
            f"Lazy Fallback Plan Rejected: this workload requires **{label}** (expected `{', '.join(req_exts)}` files and manifest `{markers}`), "
            f"not Python/JS fallback files. Rewrite the architecture blueprint and task DAG in **{label}** and verify with {verify} "
            "(call `install_runtime` first if the compiler/SDK is not on PATH)."
        )
    return ""


def build_toolchain_directive(task_text: str, root: Path, role: str = "coder", skills: Sequence[str] = ()) -> str:
    """Compact, binding stack directive for stage prompts (injected before round 0 & during architecture planning)."""
    try:
        bins = probe_binaries()
        rec = recommend_stack(task_text, root, bins)
    except Exception:
        return ""
    stack = rec["stack"]
    present = sorted(k for k in RUNTIME_INSTALLERS if runtime_present(k, bins))
    absent = sorted(k for k in RUNTIME_INSTALLERS if k not in present)
    lines = ["### Toolchain Directive (binding)"]
    lines.append(
        "- Host runtimes installed: "
        + (", ".join(present) or "none")
        + (f" | Installable via `install_runtime`: {', '.join(absent)}." if absent else ".")
    )
    if rec["ui_language"]:
        lines.append(f"- UI/frontend: {rec['ui_language']}. Never build UI as generated-HTML string dumps.")
    if rec["server_language"]:
        key = rec["server_key"]
        spec = LANGUAGES.get(key)
        lines.append(f"- Backend/core language: {rec['server_language']}" + (f" — verify with {spec['verify']}." if spec else "."))
    for n in rec["notes"]:
        lines.append(f"- {n}")
    if stack["node"]:
        pm = stack["package_manager"] or "npm"
        scripts = ", ".join(stack["scripts"][:6]) or "none"
        lines.append(f"- Node project: manager={pm}, bundler={stack['bundler'] or 'none'}, scripts=[{scripts}].")
    others = [l for l in stack["languages"] if l not in ("node",) and l != rec["server_key"]]
    if others:
        lines.append("- Also present: " + ", ".join(LANGUAGES[l]["label"] for l in others) + ".")
    if rec["missing_runtimes"]:
        lines.append("- Runtime NOT installed: " + ", ".join(rec["missing_runtimes"]) +
                     ". Call `install_runtime(name=...)` first (needs user approval), then scaffold.")
    if skills:
        st_block = skill_toolchain_directive(skills, root=root, task_text=task_text)
        if st_block:
            lines.append(st_block)
    lines.append("- Architect first: pick the language by task fit, state the reasoning, define module boundaries before coding.")
    lines.append("- Missing module/package/binary? Call `install_dependencies` (libraries) or `install_runtime` (languages/tools) once, "
                 "then re-run. NEVER retry the same failing command unchanged. `detect_toolchain` shows what exists.")
    return "\n".join(lines)


def stack_note(root: Path) -> str:
    """One-line stack hint for the dynamic system prompt."""
    try:
        s = classify_stack(root)
    except Exception:
        return ""
    parts: List[str] = []
    for lang in s["languages"]:
        if lang == "node":
            parts.append(("TypeScript" if s["typescript"] else "JavaScript") + f" ({s['package_manager'] or 'npm'}"
                         + (f", {s['bundler']}" if s["bundler"] else "") + ")")
        else:
            parts.append(LANGUAGES[lang]["label"])
    if s["static_html"]:
        parts.append("static HTML/ES modules")
    if not parts:
        return ("Stack: new project — choose the best-fit language for the task (TypeScript/Vite for UI, Rust/Go for systems & services, "
                "Python for data/ML, PHP/Ruby/C#/Java/Swift/Dart/Elixir when the task or ecosystem calls for them); "
                "install missing runtimes with `install_runtime` and libraries with `install_dependencies`.")
    return (f"Stack detected: {' + '.join(parts)}. Use each ecosystem's native verification; "
            "install missing libraries with `install_dependencies` and missing languages/tools with `install_runtime` instead of retrying failed commands.")


# ─── Missing dependency detection ─────────────────────────────────────────────

_PY_IMPORT_TO_PIP = {
    "cv2": "opencv-python", "PIL": "Pillow", "yaml": "pyyaml", "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4", "dotenv": "python-dotenv", "dateutil": "python-dateutil",
    "jwt": "PyJWT", "serial": "pyserial", "OpenSSL": "pyOpenSSL", "attr": "attrs",
}
_PY_MISSING_RE = re.compile(r"No module named ['\"]?([A-Za-z0-9_.\-]+)['\"]?")
_NODE_MISSING_RE = re.compile(
    r"(?:Cannot find (?:module|package)|ERR_MODULE_NOT_FOUND[^\n]*?package|Failed to resolve import|Could not resolve)\s+['\"]([^'\"\n]+)['\"]",
    re.IGNORECASE,
)
_RUST_MISSING_RE = re.compile(r"can't find crate for `([A-Za-z0-9_\-]+)`|use of undeclared crate or module `([A-Za-z0-9_\-]+)`|unresolved import `([A-Za-z0-9_\-]+)", re.IGNORECASE)
_GO_MISSING_RE = re.compile(r"no required module provides package ([A-Za-z0-9._\-/]+)|cannot find package \"([A-Za-z0-9._\-/]+)\"", re.IGNORECASE)
_RUBY_MISSING_RE = re.compile(r"cannot load such file -- ([A-Za-z0-9_./\-]+)|Could not find gem '([A-Za-z0-9_\-]+)", re.IGNORECASE)
_PHP_MISSING_RE = re.compile(r"Failed opening required '[^']*vendor/autoload\.php'|Failed to open stream: No such file or directory in [^\n]*vendor", re.IGNORECASE)
_CMD_MISSING_RE = re.compile(
    r"(?:'([A-Za-z0-9_.+\-]+)' is not recognized as an internal or external command|"
    r"(?:bash|sh|zsh|powershell|pwsh): (?:line \d+: )?([A-Za-z0-9_.+\-]+): (?:command )?not found|"
    r"([A-Za-z0-9_.+\-]+): command not found|"
    r"The term '([A-Za-z0-9_.+\-]+)' is not recognized|"
    r"FileNotFoundError: \[Errno 2\][^\n]*?: '([A-Za-z0-9_.+\-]+)'|"
    r"error: no such command: `?([A-Za-z0-9_\-]+))",
    re.IGNORECASE,
)
_NODE_TOOL_BINS = {"tsc": "typescript", "vite": "vite", "vitest": "vitest", "jest": "jest", "eslint": "eslint",
                   "prettier": "prettier", "webpack": "webpack", "esbuild": "esbuild", "playwright": "playwright"}
_PY_TOOL_BINS = {"pytest": "pytest", "ruff": "ruff", "mypy": "mypy", "black": "black", "flake8": "flake8", "pyright": "pyright"}


def _node_pkg_name(spec: str) -> str:
    spec = spec.strip()
    if not spec or spec.startswith((".", "/", "\\", "node:", "http:", "https:", "file:")) or re.match(r"^[A-Za-z]:[\\/]", spec):
        return ""
    parts = spec.split("/")
    name = "/".join(parts[:2]) if spec.startswith("@") and len(parts) >= 2 else parts[0]
    return name if valid_package_name(name, "node") else ""


def detect_missing_dependencies(output: str) -> List[Dict[str, str]]:
    """Parse tool output → [{ecosystem, package, hint, dev}]. ecosystem=='runtime' means a language/tool must be installed."""
    text = str(output or "")
    if not text:
        return []
    found: List[Dict[str, str]] = []
    seen: set = set()

    def _add(eco: str, pkg: str, hint: str = "", dev: bool = False) -> None:
        key = (eco, pkg.lower())
        if pkg and key not in seen:
            seen.add(key)
            found.append({"ecosystem": eco, "package": pkg, "hint": hint, "dev": "1" if dev else ""})

    for m in _PY_MISSING_RE.finditer(text):
        top = m.group(1).split(".")[0]
        if top not in ("wsa_agent", "wsa_server"):
            _add("python", _PY_IMPORT_TO_PIP.get(top, top), dev=top in _PY_TOOL_BINS.values())
    for m in _NODE_MISSING_RE.finditer(text):
        pkg = _node_pkg_name(m.group(1))
        if pkg:
            _add("node", pkg)
    for m in _RUST_MISSING_RE.finditer(text):
        crate = next((g for g in m.groups() if g), "")
        if crate and crate not in ("crate", "self", "super", "std", "core", "alloc") and valid_package_name(crate.replace("_", "-"), "rust"):
            _add("rust", crate.replace("_", "-"))
    for m in _GO_MISSING_RE.finditer(text):
        mod = next((g for g in m.groups() if g), "")
        if "." in mod.split("/")[0] and valid_package_name(mod, "go"):
            _add("go", mod)
    for m in _RUBY_MISSING_RE.finditer(text):
        gem = next((g for g in m.groups() if g), "")
        gem = gem.split("/")[0]
        if gem and valid_package_name(gem, "ruby"):
            _add("ruby", gem)
    if _PHP_MISSING_RE.search(text):
        _add("php", "vendor/autoload.php", "Run install_dependencies(from_manifest=true, ecosystem=\"php\") to create vendor/.")
    for m in _CMD_MISSING_RE.finditer(text):
        cmd = next((g for g in m.groups() if g), "").lower()
        if not cmd:
            continue
        if cmd in RUNTIME_OF_BIN:
            _add("runtime", RUNTIME_OF_BIN[cmd], cmd)
        elif cmd in _NODE_TOOL_BINS:
            _add("node", _NODE_TOOL_BINS[cmd], dev=True)
        elif cmd in _PY_TOOL_BINS:
            _add("python", _PY_TOOL_BINS[cmd], dev=True)
    low = text.lower()
    if "could not determine executable to run" in low or "npm err! missing script" in low:
        for tool, pkg in _NODE_TOOL_BINS.items():
            if re.search(rf"\b{tool}\b", low):
                _add("node", pkg, dev=True)
    return found


def format_dependency_hint(output: str) -> str:
    """Actionable hint appended to failing tool output (empty when nothing is missing)."""
    missing = detect_missing_dependencies(output)
    if not missing:
        return ""
    lines = ["", "[WSA Dependency Hint] A required package/runtime is missing. Do NOT retry the same command unchanged."]
    for item in missing[:4]:
        eco, pkg = item["ecosystem"], item["package"]
        if eco == "runtime":
            if pkg in RUNTIME_INSTALLERS:
                lines.append(f"- `{item['hint'] or pkg}` is not installed. Call install_runtime(name=\"{pkg}\") (needs user approval), then re-run.")
            else:
                lines.append(f"- `{item['hint'] or pkg}` is not installed and has no automatic installer; tell the user how to install it.")
        elif eco == "php" and pkg == "vendor/autoload.php":
            lines.append("- Composer dependencies are not installed. Call install_dependencies(from_manifest=true, ecosystem=\"php\").")
        else:
            dev = ", dev=true" if item.get("dev") else ""
            lines.append(f"- Call install_dependencies(packages=[\"{pkg}\"], ecosystem=\"{eco}\"{dev}), then re-run the failed check.")
    return "\n".join(lines)


# ─── Safe install planning ────────────────────────────────────────────────────

_NODE_NAME_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*(?:@[A-Za-z0-9^~<>=.*+\-|_ ]+)?$")
_PY_NAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._\-]*(?:\[[A-Za-z0-9_,\-]+\])?(?:\s*(?:==|>=|<=|~=|!=|>|<)\s*[A-Za-z0-9.*+!_\-]+(?:\s*,\s*(?:==|>=|<=|~=|!=|>|<)\s*[A-Za-z0-9.*+!_\-]+)*)?$"
)
_NAME_RES = {
    "rust": re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*(?:@[0-9][A-Za-z0-9.+\-*^~<>=]*)?$"),
    "go": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*(?:/[A-Za-z0-9._\-]+)+(?:@[A-Za-z0-9._\-]+)?$"),
    "php": re.compile(r"^[a-z0-9][a-z0-9_.\-]*/[a-z0-9][a-z0-9_.\-]*(?::[A-Za-z0-9.*^~<>=|\-]+)?$"),
    "ruby": re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$"),
    "dotnet": re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*(?:@[0-9][A-Za-z0-9.\-+]*)?$"),
    "dart": re.compile(r"^[a-z][a-z0-9_]*(?::[A-Za-z0-9.^<>=~\-]+)?$"),
}
PACKAGE_ECOSYSTEMS = ("python", "node", "rust", "go", "php", "ruby", "dotnet", "dart")


def valid_package_name(spec: str, ecosystem: str) -> bool:
    """Strict allowlist: registry names/versions only — no flags, URLs, local paths, or shell metacharacters."""
    s = str(spec or "").strip()
    if not s or len(s) > 120 or s.startswith("-") or any(c in s for c in "\n\r\t;&|`$'\"\\"):
        return False
    if "://" in s or s.startswith((".", "~")) or ".." in s or s.lower().startswith(("git+", "file:", "github:")):
        return False
    if ecosystem != "go" and s.startswith("/"):
        return False
    if ecosystem == "node":
        return bool(_NODE_NAME_RE.match(s))
    if ecosystem == "python":
        return bool(_PY_NAME_RE.match(s))
    rx = _NAME_RES.get(ecosystem)
    return bool(rx and rx.match(s))


def _infer_ecosystem(pkgs: List[str], stack: Dict[str, Any], from_manifest: bool) -> str:
    langs = [l for l in stack["languages"] if l in ("python", "node", "rust", "go", "php", "ruby", "csharp", "dart")]
    alias = {"csharp": "dotnet"}
    if from_manifest and langs:
        return alias.get(langs[0], langs[0])
    cands = {e for e in PACKAGE_ECOSYSTEMS if pkgs and all(valid_package_name(p, e) for p in pkgs)}
    in_project = {alias.get(l, l) for l in langs}
    both = cands & in_project
    if len(both) == 1:
        return next(iter(both))
    if len(in_project) == 1:
        return next(iter(in_project))
    if len(cands) == 1:
        return next(iter(cands))
    return ""


def plan_install(
    packages: List[str],
    ecosystem: str,
    root: Path,
    *,
    dev: bool = False,
    from_manifest: bool = False,
    python_exe: str = "",
    binaries: Optional[Dict[str, Optional[str]]] = None,
) -> Tuple[Optional[List[List[str]]], str]:
    """Return (argv commands to run in order, error). Never builds a shell string."""
    bins = binaries if binaries is not None else probe_binaries()
    eco = str(ecosystem or "auto").strip().lower()
    stack = classify_stack(root)
    pkgs = [str(p).strip() for p in (packages or []) if str(p).strip()]
    if eco == "auto":
        eco = _infer_ecosystem(pkgs, stack, from_manifest)
        if not eco:
            return None, "[Error] Cannot infer ecosystem; pass ecosystem= one of " + ", ".join(PACKAGE_ECOSYSTEMS) + "."
    if eco not in PACKAGE_ECOSYSTEMS:
        return None, (f"[Error] Unsupported ecosystem '{ecosystem}'. Use one of: {', '.join(PACKAGE_ECOSYSTEMS)}. "
                      "Languages/tools themselves are installed with install_runtime.")
    if not from_manifest and not pkgs:
        return None, "[Error] Provide `packages` or set from_manifest=true."
    if len(pkgs) > 20:
        return None, "[Error] Too many packages in one call (max 20)."
    for p in pkgs:
        if not valid_package_name(p, eco):
            return None, f"[Safety Error] Rejected package spec '{p[:60]}': only plain registry names with optional version pins are allowed."

    def need(*names: str) -> str:
        for n in names:
            if not bins.get(n):
                rt = RUNTIME_OF_BIN.get(n, n)
                return f"[Error] `{n}` not found on PATH. Call install_runtime(name=\"{rt}\")" + (
                    "" if rt in RUNTIME_INSTALLERS else f" is unavailable; install it manually") + "."
        return ""

    cmds: List[List[str]] = []
    if eco == "python":
        py = python_exe or bins.get("python") or ""
        if not py:
            return None, "[Error] Python was not found on PATH. Call install_runtime(name=\"python\")."
        if from_manifest:
            req = root / "requirements.txt"
            if req.is_file():
                cmds.append([py, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(req)])
            elif (root / "pyproject.toml").is_file():
                cmds.append([py, "-m", "pip", "install", "--disable-pip-version-check", "-e", str(root)])
            else:
                return None, "[Error] No requirements.txt or pyproject.toml to install from."
        if pkgs:
            cmds.append([py, "-m", "pip", "install", "--disable-pip-version-check", *pkgs])
        return cmds, ""

    if eco == "node":
        pm = stack["package_manager"] or "npm"
        exe = bins.get(pm) or bins.get("npm")
        if not exe or not bins.get("node"):
            return None, "[Error] Node.js/npm not found on PATH. Call install_runtime(name=\"node\")."
        if not stack["node"]:
            cmds.append([bins.get("npm") or exe, "init", "-y"])
            pm, exe = "npm", bins.get("npm") or exe
        if from_manifest:
            cmds.append([exe, "install"] + (["--no-audit", "--no-fund"] if pm == "npm" else []))
        if pkgs:
            if pm == "npm":
                cmds.append([exe, "install", *(["--save-dev"] if dev else []), "--no-audit", "--no-fund", *pkgs])
            elif pm == "bun":
                cmds.append([exe, "add", *(["--dev"] if dev else []), *pkgs])
            else:
                cmds.append([exe, "add", *(["-D"] if dev else []), *pkgs])
        return cmds, ""

    if eco == "rust":
        err = need("cargo")
        if err:
            return None, err
        if not (root / "Cargo.toml").is_file():
            cmds.append(["cargo", "init", "--vcs", "none"])
        cmds.append(["cargo", "fetch"] if from_manifest and not pkgs else ["cargo", "add", *(["--dev"] if dev else []), *pkgs])
        if from_manifest and pkgs:
            cmds.insert(len(cmds) - 1, ["cargo", "fetch"])
    elif eco == "go":
        err = need("go")
        if err:
            return None, err
        if not (root / "go.mod").is_file():
            mod = re.sub(r"[^a-z0-9._-]+", "-", root.name.lower()).strip("-") or "app"
            cmds.append(["go", "mod", "init", mod])
        if from_manifest:
            cmds.append(["go", "mod", "download"])
        for p in pkgs:
            cmds.append(["go", "get", p])
    elif eco == "php":
        err = need("php", "composer")
        if err:
            return None, err
        if from_manifest:
            cmds.append(["composer", "install", "--no-interaction"])
        if pkgs:
            cmds.append(["composer", "require", "--no-interaction", *(["--dev"] if dev else []), *pkgs])
    elif eco == "ruby":
        err = need("ruby", "bundle")
        if err:
            return None, err
        if not (root / "Gemfile").is_file():
            cmds.append(["bundle", "init"])
        if from_manifest:
            cmds.append(["bundle", "install"])
        for p in pkgs:
            cmds.append(["bundle", "add", p])
    elif eco == "dotnet":
        err = need("dotnet")
        if err:
            return None, err
        if not any(_has_marker(root, m) for m in ("*.csproj", "*.fsproj")):
            return None, "[Error] No .csproj found; scaffold first (e.g. run_command `dotnet new console`), then install packages."
        if from_manifest:
            cmds.append(["dotnet", "restore"])
        for p in pkgs:
            name, _, ver = p.partition("@")
            cmds.append(["dotnet", "add", "package", name, *(["--version", ver] if ver else [])])
    elif eco == "dart":
        err = need("dart")
        if err:
            return None, err
        if not (root / "pubspec.yaml").is_file():
            return None, "[Error] No pubspec.yaml; scaffold first (`dart create .`), then install packages."
        if from_manifest:
            cmds.append(["dart", "pub", "get"])
        if pkgs:
            cmds.append(["dart", "pub", "add", *(["--dev"] if dev else []), *pkgs])
    return cmds, ""


# ─── Runtime installation planning ───────────────────────────────────────────

def detect_os() -> str:
    if os.name == "nt":
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def plan_runtime_install(
    name: str,
    *,
    os_name: Optional[str] = None,
    binaries: Optional[Dict[str, Optional[str]]] = None,
    is_root: Optional[bool] = None,
) -> Tuple[Optional[List[List[str]]], List[List[str]], str]:
    """Return (install argv list, post-install argv list, error). Uses winget/brew/apt/dnf/pacman only."""
    key = str(name or "").strip().lower()
    key = RUNTIME_OF_BIN.get(key, key)
    spec = RUNTIME_INSTALLERS.get(key)
    if not spec:
        return None, [], ("[Unsupported] No automatic installer for '" + str(name)[:40] + "'. Supported: "
                          + ", ".join(sorted(RUNTIME_INSTALLERS)) + ". Ask the user to install it manually.")
    bins = binaries if binaries is not None else {b: shutil.which(b) for b in ("winget", "brew", "apt-get", "dnf", "pacman", "sudo")}
    osn = os_name or detect_os()
    root_user = (hasattr(os, "geteuid") and os.geteuid() == 0) if is_root is None else is_root
    post = [list(p) for p in spec.get("post", ())] if osn in ("windows", "macos") else []
    prefix: List[str] = []

    def manual() -> str:
        return f"[Error] No supported package manager for {key} on {osn}. Install manually: {spec['url']}"

    if osn == "windows":
        if bins.get("winget") and spec.get("winget"):
            return [["winget", "install", "--id", spec["winget"], "-e", "--silent",
                     "--accept-package-agreements", "--accept-source-agreements"]], post, ""
        return None, [], manual()
    if osn == "macos":
        if bins.get("brew") and spec.get("brew"):
            return [["brew", "install", spec["brew"]]], post, ""
        return None, [], manual()
    if not root_user:
        if not bins.get("sudo"):
            return None, [], "[Error] Installing system packages needs root and `sudo` was not found. Install manually: " + spec["url"]
        prefix = ["sudo", "-n"]  # non-interactive: fail fast instead of hanging on a password prompt
    if bins.get("apt-get") and spec.get("apt"):
        return [prefix + ["apt-get", "update"], prefix + ["apt-get", "install", "-y", *spec["apt"]]], [], ""
    if bins.get("dnf") and spec.get("dnf"):
        return [prefix + ["dnf", "install", "-y", *spec["dnf"]]], [], ""
    if bins.get("pacman") and spec.get("pacman"):
        return [prefix + ["pacman", "-S", "--noconfirm", *spec["pacman"]]], [], ""
    return None, [], manual()


def runtime_present(name: str, bins: Optional[Dict[str, Optional[str]]] = None) -> bool:
    key = RUNTIME_OF_BIN.get(str(name).lower(), str(name).lower())
    spec = RUNTIME_INSTALLERS.get(key)
    if not spec:
        return False
    b = bins if bins is not None else probe_binaries()
    return all(bool(b.get(x) or (x == "python" and shutil.which("python3"))) for x in spec["bins"])


# ─── Test runner selection ───────────────────────────────────────────────────

_TEST_RUNNERS: Dict[str, Tuple[str, ...]] = {
    "cargo": ("cargo", "test"), "go": ("go", "test", "./..."), "dotnet": ("dotnet", "test"),
    "rspec": ("bundle", "exec", "rspec"), "mix": ("mix", "test"), "dart": ("dart", "test"),
    "swift": ("swift", "test"), "maven": ("mvn", "-q", "test"), "gradle": ("gradle", "test"),
    "zig": ("zig", "build", "test"), "phpunit": ("php", "vendor/bin/phpunit"), "ctest": ("ctest", "--output-on-failure"),
}
ALL_TEST_FRAMEWORKS = ("auto", "pytest", "unittest", "vitest", "jest") + tuple(_TEST_RUNNERS)


def runner_argv(framework: str, root: Path) -> Optional[List[str]]:
    argv = list(_TEST_RUNNERS.get(framework, ()))
    if not argv:
        return None
    if framework == "gradle" and (root / ("gradlew.bat" if os.name == "nt" else "gradlew")).is_file():
        argv[0] = str(root / ("gradlew.bat" if os.name == "nt" else "gradlew"))
    if framework == "maven" and (root / ("mvnw.cmd" if os.name == "nt" else "mvnw")).is_file():
        argv[0] = str(root / ("mvnw.cmd" if os.name == "nt" else "mvnw"))
    return argv


def pick_test_framework(root: Path, target: str = "") -> str:
    """Choose a test runner that matches the project stack (not Python by default)."""
    low = (target or "").lower().split("::", 1)[0]
    s = classify_stack(root)
    if low.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")):
        return "vitest" if "vitest" in s["node_tests"] or (not s["node_tests"] and s["bundler"] == "vite") else "jest"
    by_ext = {".py": "pytest", ".rs": "cargo", ".go": "go", ".cs": "dotnet", ".rb": "rspec", ".ex": "mix", ".exs": "mix",
              ".dart": "dart", ".swift": "swift", ".php": "phpunit", ".zig": "zig"}
    for ext, fw in by_ext.items():
        if low.endswith(ext):
            return fw
    langs = s["languages"]
    if s["python_tests"] and "python" in langs:
        return "pytest"
    if "node" in langs and not s["python_tests"]:
        if "vitest" in s["node_tests"]:
            return "vitest"
        if "jest" in s["node_tests"]:
            return "jest"
    lang_fw = {"rust": "cargo", "go": "go", "csharp": "dotnet", "ruby": "rspec", "elixir": "mix", "dart": "dart",
               "swift": "swift", "zig": "zig", "php": "phpunit", "cpp": "ctest"}
    for lang in langs:
        if lang in lang_fw:
            return lang_fw[lang]
    if "java" in langs:
        return "gradle" if any((root / g).is_file() for g in ("build.gradle", "build.gradle.kts")) else "maven"
    return "pytest"
