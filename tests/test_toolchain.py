"""Toolchain selection and dynamic dependency installation."""
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wsa_agent import permissions, subagents, tools, toolchain  # noqa: E402
from wsa_agent.agent import _is_error_result_text  # noqa: E402

BINS_ALL = {"python": "/usr/bin/python", "node": "/usr/bin/node", "npm": "/usr/bin/npm", "npx": "/usr/bin/npx",
            "pnpm": "/usr/bin/pnpm", "yarn": None, "bun": None, "tsc": None, "git": "/usr/bin/git"}
BINS_NO_NODE = {**BINS_ALL, "node": None, "npm": None, "npx": None, "pnpm": None}


@pytest.fixture()
def root(tmp_path):
    old = tools.get_project_root()
    tools.set_project_root(tmp_path)
    yield tmp_path
    tools.set_project_root(old)


def test_new_ui_project_recommends_typescript_not_python(tmp_path):
    rec = toolchain.recommend_stack("build a premium 3D landing page website", tmp_path, BINS_ALL)
    assert rec["ui_language"] == "TypeScript"
    assert "Vite" in " ".join(rec["notes"])
    directive = toolchain.build_toolchain_directive("build a premium landing page", tmp_path)
    assert "generated-HTML string dumps" in directive
    assert "install_dependencies" in directive


def test_no_node_runtime_falls_back_to_no_build_js(tmp_path):
    rec = toolchain.recommend_stack("redesign the website UI", tmp_path, BINS_NO_NODE)
    assert "no-build" in rec["ui_language"]
    assert any("Node.js is not installed" in n for n in rec["notes"])


def test_existing_static_site_is_extended_in_place(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    rec = toolchain.recommend_stack("improve the website", tmp_path, BINS_ALL)
    assert "no-build" in rec["ui_language"]
    assert any("do NOT rewrite" in n for n in rec["notes"])


def test_data_task_stays_python(tmp_path):
    rec = toolchain.recommend_stack("analyze data with pandas and write a script", tmp_path, BINS_ALL)
    assert rec["server_language"] == "Python"
    assert rec["ui_language"] == ""


def test_existing_node_ts_project_detected(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "devDependencies": {"vite": "^5", "typescript": "^5", "vitest": "^1"}, "scripts": {"build": "vite build"}}), encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    s = toolchain.classify_stack(tmp_path)
    assert s["node"] and s["typescript"] and s["package_manager"] == "pnpm" and s["bundler"] == "vite"
    assert toolchain.pick_test_framework(tmp_path) == "vitest"
    assert "TypeScript" in toolchain.stack_note(tmp_path)


@pytest.mark.parametrize("text,eco,pkg", [
    ("ModuleNotFoundError: No module named 'pytest'", "python", "pytest"),
    ("ModuleNotFoundError: No module named 'cv2'", "python", "opencv-python"),
    ("Error: Cannot find module 'vite'\nRequire stack", "node", "vite"),
    ("Cannot find package '@scope/pkg/sub' imported from x.js", "node", "@scope/pkg"),
    ("'tsc' is not recognized as an internal or external command", "node", "typescript"),
    ("bash: pytest: command not found", "python", "pytest"),
])
def test_detect_missing_dependencies(text, eco, pkg):
    found = toolchain.detect_missing_dependencies(text)
    assert any(f["ecosystem"] == eco and f["package"] == pkg for f in found), found


def test_relative_and_wsa_imports_are_not_installed():
    assert toolchain.detect_missing_dependencies("Cannot find module './local.js'") == []
    assert toolchain.detect_missing_dependencies("No module named 'wsa_agent.foo'") == []
    assert toolchain.detect_missing_dependencies("all good") == []


def test_missing_runtime_is_reported_not_installed():
    hint = toolchain.format_dependency_hint("'node' is not recognized as an internal or external command")
    assert 'install_runtime(name="node")' in hint
    assert "install_dependencies(packages=[\"node\"]" not in hint


@pytest.mark.parametrize("spec", ["--index-url=http://evil", "git+https://x/y.git", "pkg; rm -rf /", "../local", "a b", "$(whoami)", "pkg`id`", "file:../x", ""])
def test_unsafe_package_specs_rejected(spec):
    assert not toolchain.valid_package_name(spec, "python")
    assert not toolchain.valid_package_name(spec, "node")


@pytest.mark.parametrize("spec,eco", [("pytest", "python"), ("pytest>=8.0", "python"), ("requests[socks]", "python"),
                                      ("@types/node", "node"), ("vite@5", "node"), ("three", "node")])
def test_safe_package_specs_accepted(spec, eco):
    assert toolchain.valid_package_name(spec, eco)


def test_plan_install_python_is_argv_only(tmp_path):
    cmds, err = toolchain.plan_install(["pytest"], "python", tmp_path, python_exe="py", binaries=BINS_ALL)
    assert not err
    assert cmds == [["py", "-m", "pip", "install", "--disable-pip-version-check", "pytest"]]


def test_plan_install_node_inits_package_json_and_respects_lockfile(tmp_path):
    cmds, err = toolchain.plan_install(["vite"], "node", tmp_path, dev=True, binaries=BINS_ALL)
    assert not err
    assert cmds[0][1:] == ["init", "-y"]
    assert cmds[1][1:] == ["install", "--save-dev", "--no-audit", "--no-fund", "vite"]
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    cmds, err = toolchain.plan_install(["vite"], "node", tmp_path, dev=True, binaries=BINS_ALL)
    assert cmds == [["/usr/bin/pnpm", "add", "-D", "vite"]]


def test_plan_install_rejects_bad_input(tmp_path):
    assert toolchain.plan_install(["--evil"], "python", tmp_path, binaries=BINS_ALL)[0] is None
    assert "Node.js/npm not found" in toolchain.plan_install(["vite"], "node", tmp_path, binaries=BINS_NO_NODE)[1]
    assert toolchain.plan_install([], "python", tmp_path, binaries=BINS_ALL)[0] is None
    assert toolchain.plan_install(["x"], "ruby", tmp_path, binaries=BINS_ALL)[0] is None


def test_run_tests_missing_pytest_gives_install_instruction_not_loop(root):
    (root / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    with mock.patch.object(tools, "_run_subprocess_tree_safe", return_value=(1, "", "No module named pytest", False)) as run:
        out = tools.execute_tool("run_tests", {})
    assert out.startswith("[Missing Dependency]")
    assert 'install_dependencies(packages=["pytest"], ecosystem="python")' in out
    assert run.call_count == 1  # no silent unittest fallback
    assert _is_error_result_text(out)


def test_run_command_failure_gets_dependency_hint(root):
    fake = "Traceback...\nModuleNotFoundError: No module named 'yaml'\nExit code: 1"
    with mock.patch.object(tools, "_run_command", return_value=fake):
        out = tools.execute_tool("run_command", {"command": "python x.py"})
    assert 'install_dependencies(packages=["pyyaml"], ecosystem="python"' in out
    assert tools.execute_tool("run_command", {"command": "x"}).count("[WSA Dependency Hint]") <= 1


def test_install_dependencies_creates_project_venv_then_installs(root):
    calls = []

    def fake_run(cmd, timeout, cwd=None):
        calls.append(list(cmd))
        if cmd[1:3] == ["-m", "venv"]:
            py = root / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
            py.mkdir(parents=True)
            (py / ("python.exe" if sys.platform == "win32" else "python")).write_text("", encoding="utf-8")
        return 0, "Successfully installed pytest", "", False

    with mock.patch.object(tools, "_run_subprocess_tree_safe", side_effect=fake_run), \
         mock.patch.object(tools, "_detect_python_executable", side_effect=lambda r=None: sys.executable if not (root / ".venv").exists()
                           else str(next((root / ".venv").rglob("python*")))):
        out = tools.execute_tool("install_dependencies", {"packages": ["pytest"], "ecosystem": "python"})
    assert out.startswith("[Installed]"), out
    assert calls[0][1:3] == ["-m", "venv"]
    assert calls[1][1:5] == ["-m", "pip", "install", "--disable-pip-version-check"] and calls[1][-1] == "pytest"
    assert ".venv" in calls[1][0]


def test_install_dependencies_rejects_unsafe_without_running(root):
    with mock.patch.object(tools, "_run_subprocess_tree_safe") as run:
        out = tools.execute_tool("install_dependencies", {"packages": ["--index-url=http://x"], "ecosystem": "python"})
    assert out.startswith("[Safety Error]")
    run.assert_not_called()


def test_detect_toolchain_tool_reports(root):
    out = tools.execute_tool("detect_toolchain", {"task": "premium landing page"})
    assert "Installed runtimes:" in out and "Recommended UI language" in out


def test_tools_registered_and_scoped():
    names = {t["function"]["name"] for t in tools.TOOL_DEFINITIONS}
    assert {"install_dependencies", "detect_toolchain"} <= names
    assert {"install_dependencies", "detect_toolchain"} <= subagents.CODER_TOOLS
    assert "install_dependencies" in subagents.VERIFIER_TOOLS
    assert "detect_toolchain" in subagents.ARCHITECT_TOOLS and "install_dependencies" not in subagents.ARCHITECT_TOOLS
    assert "install_dependencies" not in subagents.SECURITY_TOOLS


def test_install_requires_approval_but_detect_is_free():
    state = SimpleNamespace(permission_mode="smart")
    assert permissions.approval_reason("install_dependencies", {"packages": ["x"]}, state)
    assert permissions.approval_reason("detect_toolchain", {}, state) == ""


def test_role_prompts_enforce_toolchain_decision():
    assert "Toolchain Decision" in subagents.ROLE_SYSTEM_PROMPTS["architect"]
    assert "Toolchain Decision" in subagents.CLAUDE_STAGE_DIRECTIVES["architect"]
    for table in (subagents.ROLE_SYSTEM_PROMPTS, subagents.CLAUDE_STAGE_DIRECTIVES):
        assert "install_dependencies" in table["coder"]


# ─── Any-language selection, installs, and runtime provisioning ───────────────

BINS_NONE = {k: None for k in BINS_ALL}


@pytest.mark.parametrize("task,lang", [
    ("build a CLI tool in Rust for log parsing", "rust"),
    ("write a golang microservice", "go"),
    ("create a Laravel store", "php"),
    ("a WordPress theme", "php"),
    ("ruby on rails blog", "ruby"),
    ("android app with kotlin", "java"),
    ("ASP.NET web api", "csharp"),
    ("firmware for an arduino", "cpp"),
    ("SwiftUI iOS app", "swift"),
    ("flutter mobile app", "dart"),
    ("phoenix liveview chat", "elixir"),
])
def test_explicit_language_wins(task, lang, tmp_path):
    assert lang in toolchain.explicit_languages(task)
    rec = toolchain.recommend_stack(task, tmp_path, BINS_ALL)
    assert rec["server_key"] == lang


def test_explicit_language_overrides_existing_stack(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    rec = toolchain.recommend_stack("rewrite the backend in Rust", tmp_path, BINS_ALL)
    assert rec["server_key"] == "rust"


@pytest.mark.parametrize("task,lang", [
    ("high-performance memory-safe parser", "rust"),
    ("a high concurrency network service with grpc", "go"),
    ("train a machine learning model", "python"),
    ("real-time chat that is fault-tolerant", "elixir"),
])
def test_capability_fit_picks_language(task, lang, tmp_path):
    assert toolchain.recommend_stack(task, tmp_path, BINS_ALL)["server_key"] == lang


def test_existing_manifest_language_is_kept(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    assert toolchain.classify_stack(tmp_path)["languages"] == ["rust"]
    assert toolchain.recommend_stack("add an api endpoint", tmp_path, BINS_ALL)["server_key"] == "rust"
    assert toolchain.pick_test_framework(tmp_path) == "cargo"
    (tmp_path / "x.csproj").write_text("<Project/>", encoding="utf-8")
    assert "csharp" in toolchain.classify_stack(tmp_path)["languages"]


def test_missing_runtime_is_flagged_in_recommendation_and_directive(tmp_path):
    rec = toolchain.recommend_stack("build it in rust", tmp_path, BINS_NONE)
    assert "rust" in rec["missing_runtimes"]
    d = toolchain.build_toolchain_directive("build it in rust", tmp_path)
    assert "Rust" in d


def test_ui_with_explicit_backend_keeps_modern_frontend(tmp_path):
    rec = toolchain.recommend_stack("premium landing website using php", tmp_path, BINS_ALL)
    assert rec["server_key"] == "php" and "HTML/CSS" in rec["ui_language"]


@pytest.mark.parametrize("out,eco,pkg", [
    ("error[E0463]: can't find crate for `serde`", "rust", "serde"),
    ("no required module provides package github.com/gin-gonic/gin; to add it:", "go", "github.com/gin-gonic/gin"),
    ("cannot load such file -- nokogiri (LoadError)", "ruby", "nokogiri"),
    ("Could not find gem 'rails' in locally installed gems", "ruby", "rails"),
])
def test_detect_missing_other_ecosystems(out, eco, pkg):
    assert any(f["ecosystem"] == eco and f["package"] == pkg for f in toolchain.detect_missing_dependencies(out))


def test_missing_language_binary_maps_to_install_runtime():
    hint = toolchain.format_dependency_hint("bash: cargo: command not found")
    assert 'install_runtime(name="rust")' in hint
    hint = toolchain.format_dependency_hint("'dotnet' is not recognized as an internal or external command")
    assert 'install_runtime(name="dotnet")' in hint
    assert 'install_dependencies(from_manifest=true, ecosystem="php")' in toolchain.format_dependency_hint(
        "PHP Fatal error: Failed opening required '/app/vendor/autoload.php'")


@pytest.mark.parametrize("eco,spec,ok", [
    ("rust", "serde", True), ("rust", "tokio@1.38", True), ("rust", "--git", False),
    ("go", "github.com/gin-gonic/gin", True), ("go", "../evil", False), ("go", "plain", False),
    ("php", "monolog/monolog", True), ("php", "monolog/monolog:^3.0", True), ("php", "x;y/z", False),
    ("ruby", "rails", True), ("ruby", "--source", False),
    ("dotnet", "Newtonsoft.Json@13.0.3", True), ("dotnet", "x y", False),
    ("dart", "http:^1.2.0", True), ("dart", "Http", False),
])
def test_package_validation_per_ecosystem(eco, spec, ok):
    assert toolchain.valid_package_name(spec, eco) is ok


def test_plan_install_other_ecosystems(tmp_path):
    bins = {**BINS_ALL, "cargo": "cargo", "go": "go", "composer": "composer", "php": "php", "bundle": "bundle", "ruby": "ruby", "dotnet": "dotnet", "dart": "dart"}
    cmds, err = toolchain.plan_install(["serde"], "rust", tmp_path, binaries=bins)
    assert not err and cmds == [["cargo", "init", "--vcs", "none"], ["cargo", "add", "serde"]]
    cmds, err = toolchain.plan_install(["github.com/gin-gonic/gin"], "go", tmp_path, binaries=bins)
    assert cmds[0][:3] == ["go", "mod", "init"] and cmds[1] == ["go", "get", "github.com/gin-gonic/gin"]
    cmds, err = toolchain.plan_install(["monolog/monolog"], "php", tmp_path, dev=True, binaries=bins)
    assert cmds == [["composer", "require", "--no-interaction", "--dev", "monolog/monolog"]]
    cmds, err = toolchain.plan_install([], "php", tmp_path, from_manifest=True, binaries=bins)
    assert cmds == [["composer", "install", "--no-interaction"]]
    assert "No .csproj" in toolchain.plan_install(["Newtonsoft.Json"], "dotnet", tmp_path, binaries=bins)[1]
    err = toolchain.plan_install(["serde"], "rust", tmp_path, binaries=BINS_NONE)[1]
    assert 'install_runtime(name="rust")' in err


def test_plan_install_infers_ecosystem_from_project(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    bins = {**BINS_ALL, "cargo": "cargo"}
    cmds, err = toolchain.plan_install(["serde"], "auto", tmp_path, binaries=bins)
    assert not err and cmds[-1] == ["cargo", "add", "serde"]


@pytest.mark.parametrize("osn,pm,expected_head", [
    ("windows", {"winget": "winget"}, ["winget", "install", "--id", "Rustlang.Rustup"]),
    ("macos", {"brew": "brew"}, ["brew", "install", "rustup"]),
])
def test_plan_runtime_install_windows_and_macos(osn, pm, expected_head):
    cmds, post, err = toolchain.plan_runtime_install("rust", os_name=osn, binaries=pm)
    assert not err and cmds[0][:len(expected_head)] == expected_head
    assert post == [["rustup", "default", "stable"]]


def test_plan_runtime_install_linux_uses_noninteractive_sudo():
    cmds, post, err = toolchain.plan_runtime_install("go", os_name="linux", binaries={"apt-get": "apt-get", "sudo": "sudo"}, is_root=False)
    assert not err
    assert cmds[0] == ["sudo", "-n", "apt-get", "update"] and cmds[1] == ["sudo", "-n", "apt-get", "install", "-y", "golang-go"]
    assert toolchain.plan_runtime_install("go", os_name="linux", binaries={"apt-get": "apt-get"}, is_root=False)[2].startswith("[Error]")
    cmds, _, _ = toolchain.plan_runtime_install("go", os_name="linux", binaries={"dnf": "dnf"}, is_root=True)
    assert cmds == [["dnf", "install", "-y", "golang"]]


def test_plan_runtime_install_rejects_unknown_and_resolves_aliases():
    cmds, post, err = toolchain.plan_runtime_install("totally-made-up; rm -rf /", os_name="linux", binaries={})
    assert cmds is None and err.startswith("[Unsupported]")
    cmds, _, err = toolchain.plan_runtime_install("cargo", os_name="windows", binaries={"winget": "winget"})
    assert not err and "Rustlang.Rustup" in cmds[0]
    assert toolchain.plan_runtime_install("rust", os_name="windows", binaries={})[2].startswith("[Error]")


def test_install_runtime_tool_already_present_short_circuits(root):
    with mock.patch.object(toolchain, "runtime_present", return_value=True), mock.patch.object(tools, "_run_subprocess_tree_safe") as run:
        out = tools.execute_tool("install_runtime", {"name": "node"})
    assert out.startswith("[Already installed]")
    run.assert_not_called()


def test_install_runtime_tool_runs_planned_argv_then_verifies(root):
    plan = ([["winget", "install", "--id", "GoLang.Go", "-e"]], [], "")
    state = {"n": 0}

    def present(name, bins=None):
        state["n"] += 1
        return state["n"] > 1  # missing first, present after install

    with mock.patch.object(toolchain, "runtime_present", side_effect=present), \
         mock.patch.object(toolchain, "plan_runtime_install", return_value=plan), \
         mock.patch.object(toolchain, "refresh_path"), \
         mock.patch.object(tools, "_run_subprocess_tree_safe", return_value=(0, "ok", "", False)) as run:
        out = tools.execute_tool("install_runtime", {"name": "go"})
    assert out.startswith("[Installed]"), out
    assert run.call_args[0][0][1:4] == ["install", "--id", "GoLang.Go"]


def test_install_runtime_unsupported_does_not_execute(root):
    with mock.patch.object(tools, "_run_subprocess_tree_safe") as run:
        out = tools.execute_tool("install_runtime", {"name": "cobol"})
    assert out.startswith("[Unsupported]")
    run.assert_not_called()


def test_run_tests_generic_runner_missing_binary_points_to_install_runtime(root):
    (root / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    with mock.patch("shutil.which", return_value=None):
        out = tools.execute_tool("run_tests", {})
    assert out.startswith("[Missing Dependency]") and 'install_runtime(name="rust")' in out


def test_run_tests_cargo_parses_summary(root):
    (root / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    cargo_out = "running 3 tests\ntest result: ok. 3 passed; 0 failed; 1 ignored; 0 measured\n"
    with mock.patch("shutil.which", return_value="cargo"), \
         mock.patch.object(tools, "_run_subprocess_tree_safe", return_value=(0, cargo_out, "", False)) as run:
        out = tools.execute_tool("run_tests", {})
    assert "Framework: cargo" in out and "3 passed" in out and "Status: PASS" in out
    assert run.call_args[0][0][1:] == ["test"]


def test_install_runtime_is_permission_gated_and_scoped():
    state = SimpleNamespace(permission_mode="smart")
    assert permissions.approval_reason("install_runtime", {"name": "rust"}, state)
    assert "install_runtime" in subagents.CODER_TOOLS and "install_runtime" in subagents.VERIFIER_TOOLS
    assert "install_runtime" not in subagents.ARCHITECT_TOOLS and "install_runtime" not in subagents.SECURITY_TOOLS
    assert "install_runtime" in {t["function"]["name"] for t in tools.TOOL_DEFINITIONS}


def test_toolchain_directive_includes_host_runtime_inventory_and_verifiers(tmp_path):
    from wsa_agent.agent import _is_verification_command
    d = toolchain.build_toolchain_directive("build a high-performance Rust service", tmp_path, role="architect")
    assert "Host runtimes installed:" in d
    assert "Backend/core language: Rust" in d
    # Inline node -e / python -c containing JS/Python identifiers like `;ps` or `;type` must count as verification
    assert _is_verification_command(
        "run_command",
        {"argv": ["node", "-e", "const assert=require('node:assert/strict');let ps={ok:1};let type='Points';assert.equal(ps.ok,1);"]},
        "PASS\nExit code: 0",
    )
    for cmd in ("cargo check", "go vet ./...", "php -l index.php", "dotnet test", "bundle exec rspec", "mix test", "dart test", "swift test", "zig build test", "pnpm test"):
        assert _is_verification_command("run_command", {"command": cmd}, "Exit code: 0"), cmd


def test_bootstrap_fast_path_and_auto_heal_missing_wsa_packages(monkeypatch):
    from wsa_agent import bootstrap

    monkeypatch.setattr(bootstrap, "_BOOTSTRAPPED_OK", False)
    res = bootstrap.ensure_wsa_dependencies(quiet=True)
    assert res["ok"] is True and res["missing"] == [] and res["installed"] == []

    # Simulate missing websocket-client and pytest, followed by successful pip install
    state = {"installed": False, "cmds": []}

    def fake_probe(mod_name, req_attr=None):
        if not state["installed"] and mod_name in ("websocket", "pytest"):
            return False
        return True

    def fake_run(cmd, **kwargs):
        state["cmds"].append(list(cmd))
        state["installed"] = True
        return SimpleNamespace(returncode=0, stdout="Successfully installed", stderr="")

    monkeypatch.setattr(bootstrap, "_BOOTSTRAPPED_OK", False)
    monkeypatch.setattr(bootstrap, "_probe_module", fake_probe)
    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    monkeypatch.delenv("WSA_NO_AUTO_BOOTSTRAP", raising=False)

    res2 = bootstrap.ensure_wsa_dependencies(quiet=True)
    assert res2["ok"] is True
    assert "websocket-client>=1.6" in res2["installed"]
    assert "pytest>=7.4" in res2["installed"]
    assert len(state["cmds"]) == 1
    assert state["cmds"][0][1:4] == ["-m", "pip", "install"]


def test_bootstrap_handles_missing_pip_and_pep668_user_fallback(monkeypatch):
    from wsa_agent import bootstrap

    calls = []
    state = {"ok": False}

    def fake_probe(mod_name, req_attr=None):
        return state["ok"] or mod_name != "PIL.Image"

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[1:3] == ["-m", "pip"] and "--user" not in cmd:
            if len(calls) == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="No module named pip")
            return SimpleNamespace(returncode=1, stdout="", stderr="error: externally-managed-environment")
        if "--user" in cmd:
            state["ok"] = True
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(bootstrap, "_BOOTSTRAPPED_OK", False)
    monkeypatch.setattr(bootstrap, "_probe_module", fake_probe)
    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    monkeypatch.delenv("WSA_NO_AUTO_BOOTSTRAP", raising=False)

    res = bootstrap.ensure_wsa_dependencies(quiet=True)
    assert res["ok"] is True
    assert any(c[1:3] == ["-m", "ensurepip"] for c in calls)
    assert any("--user" in c for c in calls)


def test_manifests_and_launchers_cover_all_operational_dependencies():
    repo_root = Path(__file__).resolve().parents[1]
    req_txt = (repo_root / "requirements.txt").read_text(encoding="utf-8")
    pyproj_txt = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    for pkg in ("httpx", "websocket-client", "rich", "prompt_toolkit", "Pillow", "pytest"):
        assert pkg in req_txt, pkg
        assert pkg in pyproj_txt, pkg
    wsa_ps1 = (repo_root / "wsa.ps1").read_text(encoding="utf-8")
    wsa_bat = (repo_root / "wsa.bat").read_text(encoding="utf-8")
    assert "ensure_wsa_dependencies" in wsa_ps1
    assert "if exist" in wsa_bat.lower()


