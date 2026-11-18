"""Human approval policy. This is a permission gate, not an OS sandbox."""
import json
import sys

from . import renderer as R


READ_ONLY_TOOLS = frozenset({
    "read_file", "get_file_info", "get_file_outline", "inspect_image",
    "glob_files", "list_dir", "search_files", "recall", "list_background",
    "list_custom_tools", "list_dynamic_rules", "browser_tabs", "spawn_subagent",
    "todo", "wait_for",
})
EXECUTION_TOOLS = frozenset({
    "run_command", "run_python", "run_tests", "start_background", "restart_service",
})


def approval_reason(name, args, state):
    from .tools import is_low_level_change

    mode = getattr(state, "permission_mode", "smart")
    # A stale auto_approve flag must never bypass a selected smart/strict policy.
    if mode == "auto":
        return ""
    if name in READ_ONLY_TOOLS:
        return ""
    if name == "rollback" and args.get("action") in ("list", "preview"):
        return ""
    if mode == "strict":
        return f"Strict mode: approve {name}"
    if name in EXECUTION_TOOLS:
        return "Execute code with your Windows account's access (files, network, and processes)"
    if name in {"delete_file", "rollback", "stop_background"}:
        return f"Destructive or process-control action: {name}"
    if name in {"http_request", "browser_inspect", "browser_interact", "web_fetch"}:
        return f"Access a network endpoint or browser session: {name}"
    if name == "visual_inspect":
        target = str(args.get("target") or "").strip().lower()
        if target.startswith(("http://", "https://")):
            import urllib.parse
            host = (urllib.parse.urlparse(target).hostname or "").lower()
            if host not in {"127.0.0.1", "localhost", "::1"}:
                return f"Access an external network endpoint in headless browser: {target[:80]}"
        return ""
    if name == "git":
        sub = str(args.get("action") or "").strip().lower()
        git_args = args.get("args") or []
        if sub in {"branch", "remote", "config"} and git_args:
            return f"Git action may change repository or user configuration: git {sub}"
        if any(str(arg).split("=", 1)[0] in {"--output", "--ext-diff", "--textconv"}
               for arg in git_args):
            return f"Git action may write files or execute a helper: git {sub}"
        if sub in {"status", "diff", "log", "show", "blame", "rev-parse", "branch", "remote", "config", "ls-files"}:
            return ""
        return f"Git repository mutation: git {sub}"
    if name in {"create_custom_tool", "remove_custom_tool", "set_dynamic_rule", "remove_dynamic_rule"}:
        return f"Change agent behavior or executable tools: {name}"
    protected, reason = is_low_level_change(name, args)
    if protected:
        return reason
    if name in {"write_file", "patch_file", "remember", "move_file", "copy_file", "todo", "apply_patch"}:
        return ""
    # Custom/unknown tools are arbitrary Python; default deny without approval.
    return f"Execute custom tool: {name}"


def request_approval(name, args, state, reason):
    """Approve exactly one action. Never let a tool response grant approval."""
    callback = getattr(state, "approval_callback", None)
    request = {"tool": name, "arguments": args, "reason": reason,
               "project_root": str(state.project_root)}
    if callback is not None:
        try:
            return callback(request) is True
        except Exception:
            return False
    if sys.stdin is None or not sys.stdin.isatty():
        return False
    R._release_console_live(R.console)
    R.sync_theme()
    from rich.markup import escape as _escape
    R.console.print(f"\n  [bold {R.Theme.WARN}]Permission required:[/] {_escape(str(reason))}")
    R.console.print(f"  [{R.Theme.DIM}]Project:[/] {_escape(str(state.project_root))}")
    R.console.print("  " + name + " " + json.dumps(args, ensure_ascii=False, indent=2), markup=False)
    R.console.print(
        f"  [bold {R.Theme.ACCENT}]\\[y][/] Allow once   "
        f"[bold {R.Theme.ACCENT}]\\[n / Enter][/] Deny and stop this step"
    )
    try:
        return R.console.input("Allow this action once? [y/N]: ", markup=False).strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def write_scope_error(name, args, owned_files):
    """Enforce exact canonical file ownership for structured mutation tools.

Shell/code execution still needs approval and cannot be confined by this check.
"""
    if owned_files is None:
        return ""
    from .tools import _resolve, _preview_rollback_paths
    if name in {"write_file", "patch_file", "delete_file"}:
        paths = [args.get("path", "")]
    elif name in {"move_file", "copy_file"}:
        paths = [args.get("path", ""), args.get("new_path", "")]
    elif name == "apply_patch":
        paths = [str(e.get("path", "")) for e in (args.get("edits") or []) if isinstance(e, dict)]
    elif name == "rollback" and str(args.get("action") or "rollback").lower() == "rollback":
        paths = _preview_rollback_paths(args.get("steps", 1), args.get("checkpoint_id"))
    else:
        return ""
    try:
        owned = {_resolve(p) for p in owned_files}
        for path in paths:
            target = _resolve(path)
            if target not in owned or target.is_dir():
                return f"'{path}' is outside this worker's assigned files. Ask the parent to revise the task scope."
    except (OSError, ValueError, TypeError):
        return "Invalid or outside-workspace file scope"
    return ""
