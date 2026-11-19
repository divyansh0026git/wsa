"""Zero-third-party-import startup & operational dependency bootstrapper for WSA.

Ensures `httpx`, `rich`, `prompt_toolkit`, `websocket-client` (with `create_connection`),
`Pillow` (`PIL.Image`), and `pytest` are available before WSA TUI, server, or visual
inspection operations execute. Uses argv-only subprocess invocation (`shell=False`) and
handles missing `pip` (`ensurepip`) as well as PEP 668 `--user` fallback cleanly.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

# (import_module_name, pip_requirement_spec, required_attribute_or_None)
REQUIRED_WSA_PACKAGES: Tuple[Tuple[str, str, Optional[str]], ...] = (
    ("httpx", "httpx>=0.25", None),
    ("rich", "rich>=13.0", None),
    ("prompt_toolkit", "prompt_toolkit>=3.0", None),
    ("websocket", "websocket-client>=1.6", "create_connection"),
    ("PIL.Image", "Pillow>=9.1", None),
    ("pytest", "pytest>=7.4", None),
)

_BOOTSTRAPPED_OK: bool = False


def _probe_module(mod_name: str, required_attr: Optional[str] = None) -> bool:
    """Return True if `mod_name` imports cleanly and exposes `required_attr` (if specified)."""
    try:
        mod = importlib.import_module(mod_name)
        if required_attr and not hasattr(mod, required_attr):
            return False
        return True
    except Exception:
        return False


def check_missing_wsa_packages() -> List[Tuple[str, str]]:
    """Return `[(import_name, pip_spec), ...]` for any missing or broken WSA core packages."""
    missing: List[Tuple[str, str]] = []
    for mod_name, pip_spec, req_attr in REQUIRED_WSA_PACKAGES:
        if not _probe_module(mod_name, req_attr):
            missing.append((mod_name, pip_spec))
    return missing


def _run_pip_install(python_exe: str, specs: List[str], extra_flags: Optional[List[str]] = None, timeout: int = 240) -> Tuple[int, str, str]:
    cmd = [
        python_exe,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--quiet",
        *(extra_flags or []),
        *specs,
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def ensure_wsa_dependencies(
    *,
    auto_install: bool = True,
    quiet: bool = False,
    python_exe: Optional[str] = None,
    timeout: int = 240,
) -> Dict[str, Any]:
    """Verify WSA's core operational dependencies and auto-install any missing packages."""
    global _BOOTSTRAPPED_OK
    target_py = python_exe or sys.executable
    is_current_interp = os.path.normcase(os.path.abspath(target_py)) == os.path.normcase(os.path.abspath(sys.executable))

    if is_current_interp and _BOOTSTRAPPED_OK:
        return {"ok": True, "missing": [], "installed": [], "error": ""}

    missing = check_missing_wsa_packages()
    if not missing:
        if is_current_interp:
            _BOOTSTRAPPED_OK = True
        return {"ok": True, "missing": [], "installed": [], "error": ""}

    specs = [spec for _, spec in missing]
    mod_names = [m for m, _ in missing]
    no_auto = os.environ.get("WSA_NO_AUTO_BOOTSTRAP", "").strip().lower() in ("1", "true", "yes")
    if not auto_install or no_auto:
        err = (
            f"Missing WSA runtime dependencies ({', '.join(mod_names)}). "
            f"Run: \"{target_py}\" -m pip install {' '.join(specs)}"
        )
        return {"ok": False, "missing": specs, "installed": [], "error": err}

    if not quiet:
        try:
            sys.stderr.write(
                f"[WSA Setup] Missing runtime package(s): {', '.join(specs)} — installing automatically...\n"
            )
            sys.stderr.flush()
        except Exception:
            pass

    try:
        rc, out, err_txt = _run_pip_install(target_py, specs, timeout=timeout)
        combined = f"{out}\n{err_txt}"
        if rc != 0 and "No module named pip" in combined:
            subprocess.run(
                [target_py, "-m", "ensurepip", "--upgrade"],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            rc, out, err_txt = _run_pip_install(target_py, specs, timeout=timeout)
            combined = f"{out}\n{err_txt}"
        if rc != 0 and "externally-managed-environment" in combined:
            rc, out, err_txt = _run_pip_install(target_py, specs, extra_flags=["--user"], timeout=timeout)
            combined = f"{out}\n{err_txt}"
    except Exception as exc:
        err = (
            f"Failed to auto-install WSA dependencies ({', '.join(specs)}): {exc}. "
            f"Run manually: \"{target_py}\" -m pip install -r requirements.txt"
        )
        if not quiet:
            try:
                sys.stderr.write(f"[WSA Setup Error] {err}\n")
            except Exception:
                pass
        return {"ok": False, "missing": specs, "installed": [], "error": err}

    importlib.invalidate_caches()
    # Ensure user site-packages is on sys.path if `--user` fallback was used
    try:
        import site
        user_site = site.getusersitepackages()
        if isinstance(user_site, str) and os.path.isdir(user_site) and user_site not in sys.path:
            sys.path.append(user_site)
    except Exception:
        pass

    still_missing = check_missing_wsa_packages() if is_current_interp else ([] if rc == 0 else missing)
    if not still_missing and rc == 0:
        if is_current_interp:
            _BOOTSTRAPPED_OK = True
        if not quiet:
            try:
                sys.stderr.write(f"[WSA Setup] Installed {', '.join(specs)} successfully.\n")
                sys.stderr.flush()
            except Exception:
                pass
        return {"ok": True, "missing": [], "installed": specs, "error": ""}

    rem_specs = [spec for _, spec in still_missing] or specs
    tail = "\n".join(combined.strip().splitlines()[-6:]) if combined.strip() else f"pip exited with code {rc}"
    err = (
        f"Could not install {', '.join(rem_specs)} ({tail}). "
        f"Run manually: \"{target_py}\" -m pip install -r requirements.txt"
    )
    if not quiet:
        try:
            sys.stderr.write(f"[WSA Setup Error] {err}\n")
        except Exception:
            pass
    return {"ok": False, "missing": rem_specs, "installed": [], "error": err}
