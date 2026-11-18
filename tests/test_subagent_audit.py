import json
from unittest.mock import Mock, patch

import pytest

from wsa_agent import agent, subagents, tools
from wsa_agent.session import SessionState


@pytest.mark.parametrize("provider", ["claude", "chatgpt", "gemini", "deepseek", "venice"])
def test_verifier_schema_is_explicit_for_every_provider(provider):
    prompt = subagents.get_role_system_prompt("verifier", provider)
    assert '"checks":["check name: observed result"]' in prompt
    assert "not objects" in prompt
    assert "unresolved failures" in prompt


@pytest.mark.parametrize("description", [
    "error handling tests passed", "failure recovery verified", "error-handling tests passed",
])
@pytest.mark.parametrize("field", ["checks", "summary", "trailing"])
def test_verifier_accepts_successful_failure_handling_checks(description, field):
    verdict = {"verdict": "pass", "checks": ["tests passed"], "failures": [], "summary": "OK"}
    if field != "trailing":
        verdict[field] = [description] if field == "checks" else description
    raw = json.dumps(verdict)
    if field == "trailing":
        raw = '```json\n' + raw + '\n```\n' + description
    result = subagents._parse_verifier_verdict(raw, {"ok": True, "verification_evidence": [{"ok": True}]})
    assert result["verdict"] == "pass"


@pytest.mark.parametrize("description", [
    "error handling tests failed", "failure recovery is broken", "2 failures remain",
])
def test_verifier_rejects_failed_failure_handling_checks(description):
    verdict = {"verdict": "pass", "checks": [description], "failures": []}
    result = subagents._parse_verifier_verdict(json.dumps(verdict), {"ok": True, "verification_evidence": [{"ok": True}]})
    assert result["verdict"] == "fail"


@pytest.mark.parametrize("clear_roles", [True, False])
def test_provider_reset_only_invalidates_removed_urls(tmp_path, clear_roles):
    state = SessionState(project_root=tmp_path)
    target = "https://chatgpt.com/c/old"
    unrelated = "https://venice.ai/chat/agent/keep"
    role = "https://chatgpt.com/c/worker"
    state.chat_urls = {"chatgpt": target, "venice": unrelated}
    state.role_threads = {"coder": role, "reviewer": unrelated}
    client = Mock()
    agent._reset_provider_chats_for_account_change(
        state, client, "session", ["chatgpt"], clear_roles=clear_roles, refresh_health=False,
    )
    sent = client.clear_session_chats.call_args.kwargs["client_urls"]
    assert set(sent) == ({target, role} if clear_roles else {target})
    assert state.chat_urls == {"venice": unrelated}
    assert state.role_threads["reviewer"] == unrelated
    assert ("coder" in state.role_threads) == (not clear_roles)


@pytest.mark.parametrize("trailing", [
    "No files changed, but tests failed.",
    "No errors were seen earlier but the final check failed.",
    "0 failed initially, but the last test failed.",
])
def test_verifier_rejects_failure_hidden_by_unrelated_negation(trailing):
    raw = '```json\n{"verdict":"pass","checks":["test"],"failures":[]}\n```\n' + trailing
    result = subagents._parse_verifier_verdict(raw, {"ok": True, "verification_evidence": [{"ok": True}]})
    assert result["verdict"] == "fail"


@pytest.mark.parametrize("trailing", ["542 passed, 0 failed", "Failures: none", "No errors"])
def test_verifier_accepts_unambiguous_zero_failure_summary(trailing):
    raw = '```json\n{"verdict":"pass","checks":["test"],"failures":[]}\n```\n' + trailing
    result = subagents._parse_verifier_verdict(raw, {"ok": True, "verification_evidence": [{"ok": True}]})
    assert result["verdict"] == "pass"


@pytest.mark.parametrize("field", ["checks", "summary"])
def test_verifier_pass_cannot_contradict_its_own_json(field):
    verdict = {"verdict": "pass", "checks": ["test passed"], "failures": [], "summary": "OK"}
    verdict[field] = ["tests failed"] if field == "checks" else "No syntax errors, but tests failed"
    result = subagents._parse_verifier_verdict(json.dumps(verdict), {"ok": True, "verification_evidence": [{"ok": True}]})
    assert result["verdict"] == "fail"


@pytest.mark.parametrize("output,result", [
    ('{"verdict":"pass","checks":[],"failures":[]}', {"ok": False, "aborted_reason": "timeout"}),
    ("", {"ok": True}),
    ("Could not run the tests.", {"ok": True}),
    ('{"verdict":"pass","failures":[{"issue":"broken"}]}', {"ok": True}),
])
def test_incomplete_or_contradictory_verifier_never_passes(output, result):
    assert subagents._parse_verifier_verdict(output, result)["verdict"] == "fail"


@pytest.mark.parametrize("failed_stage", ["explorer", "architect"])
def test_failed_planning_stage_does_not_dispatch_coder(tmp_path, failed_stage):
    state = SessionState(project_root=tmp_path)
    called = []
    def role(**kwargs):
        called.append(kwargs["role"])
        if kwargs["role"] == failed_stage:
            return {"ok": False, "aborted_reason": "connection lost", "final_text": ""}
        return {"ok": True, "final_text": "[]"}
    with patch.object(subagents, "_run_role_agent", side_effect=role):
        result = subagents.run_orchestrated_turn("implement", state, Mock())
    assert not result["ok"]
    assert result["aborted_reason"] == failed_stage + "_failed"
    assert "coder" not in called


def test_duplicate_task_ids_fail_before_coder_dispatch(tmp_path):
    state = SessionState(project_root=tmp_path)
    plan = [{"id": "same", "title": "first"}, {"id": "same", "title": "second"}]
    called = []
    def role(**kwargs):
        called.append(kwargs["role"])
        return {"ok": True, "final_text": json.dumps(plan) if kwargs["role"] == "architect" else "recon"}
    with patch.object(subagents, "_run_role_agent", side_effect=role):
        result = subagents.run_orchestrated_turn("implement", state, Mock())
    assert not result["ok"]
    assert "duplicate" in result["summary"]
    assert "coder" not in called


@pytest.mark.parametrize("raw", ["", "[]", '[{"id":"t1","depends_on":"t0"}]', '[{"id":"t1","owned_files":{}}]'])
def test_invalid_architect_plan_is_rejected(raw):
    assert subagents._architect_plan_error(raw)


@pytest.mark.parametrize("tool_calls,retry_ok,attempts,completed", [
    (0, False, 2, False), (0, True, 2, True), (1, True, 1, False),
])
def test_orchestrated_rule_checker_prerequisite(tmp_path, tool_calls, retry_ok, attempts, completed):
    state = SessionState(project_root=tmp_path)
    plan = [
        {"id": "t5", "title": "Dependency-free unslop rule checker", "owned_files": ["scripts/check.mjs"]},
        {"id": "t6", "title": "Integration pass", "depends_on": ["t5"], "owned_files": ["README.md"]},
    ]
    calls = []
    timeout = "DOM relay timeout (300s) waiting for the Claude tab to finish answering"

    def role(**kwargs):
        stage = kwargs.get("label") or kwargs["role"]
        calls.append(stage)
        if stage == "architect":
            return {"ok": True, "final_text": json.dumps(plan)}
        if stage == "coder:t5":
            assert "Implement any requested deliverables" in kwargs["prompt"]
            assert kwargs["owned_files"] == ["scripts/check.mjs"]
            if calls.count(stage) == 2 and retry_ok:
                return {"ok": True}
            return {"ok": False, "aborted_reason": timeout, "tool_calls_count": tool_calls}
        if kwargs["role"] == "verifier":
            return {"ok": True, "final_text": '{"verdict":"pass","checks":["checked"],"failures":[]}',
                    "verification_evidence": [{"ok": True}]}
        return {"ok": True, "final_text": "done"}

    with patch.object(subagents, "_run_role_agent", side_effect=role), \
         patch.object(subagents, "_discover_live_relays", return_value=[]), \
         patch.object(state, "auto_checkpoint"):
        result = subagents.run_orchestrated_turn("Implement the checker and verify integration", state, Mock())

    assert calls.count("coder:t5") == attempts
    assert result["ok"] is completed
    if completed:
        assert calls[-2:] == ["coder:t6", "verifier#1"]
    else:
        assert calls[-1] == "coder:t5"
        assert [task["status"] for task in result["tasks"]] == ["failed", "blocked"]
        assert result["verdict"]["failures"] == [
            {"file": "t5", "issue": timeout}, {"file": "t6", "issue": "Blocked by t5"}
        ]
        assert result["summary"] == state.messages[-1]["content"]
        assert timeout in result["summary"]


def test_long_dependency_chain_runs_in_order(tmp_path):
    previous = tools.get_project_root()
    state = SessionState(project_root=tmp_path, dual_lane=True)
    plan = [{"id": f"t{i}", "title": f"task {i}", "owned_files": [f"f{i}.txt"],
             "depends_on": [f"t{i-1}"] if i else []} for i in range(35)]
    calls = []
    def role(**kwargs):
        name = kwargs["role"]
        if name == "architect":
            return {"ok": True, "final_text": json.dumps(plan)}
        if name == "coder":
            calls.append(kwargs["label"])
        return {"ok": True, "final_text": '{"verdict":"pass","checks":["simulated"],"failures":[]}',
                "verification_evidence": [{"tool": "run_tests", "ok": True}]}
    try:
        with patch.object(subagents, "_run_role_agent", side_effect=role), \
             patch.object(subagents, "_discover_live_relays", return_value=[{"client_id":"a"}, {"client_id":"b"}]):
            result = subagents.run_orchestrated_turn("implement", state, Mock())
    finally:
        tools.set_project_root(previous)
    assert result["ok"]
    assert calls == [f"coder:t{i}" for i in range(35)]


def test_long_agent_loop_completes_35_real_tool_rounds(tmp_path):
    previous = tools.get_project_root()
    tools.set_project_root(tmp_path)
    state = SessionState(project_root=tmp_path, team_mode="off")
    for index in range(35):
        (tmp_path / f"item{index}.txt").write_text(f"value-{index}")

    class Client:
        api_base = "http://127.0.0.1:8081/v1"
        api_key = ""
        def __init__(self):
            self.round = 0
        def stream(self, **kwargs):
            index = self.round
            self.round += 1
            if index == 35:
                return {"content": "All 35 reads completed.", "tool_calls": []}
            return {"content": None, "tool_calls": [{"id": f"read-{index}", "function": {
                "name": "read_file", "arguments": json.dumps({"path": f"item{index}.txt"})}}]}

    client = Client()
    try:
        with patch.object(state, "auto_checkpoint"):
            result = agent.execute_agent_loop("Read all files", state, client, quiet_ui=True, max_rounds=40)
    finally:
        tools.set_project_root(previous)
    assert result["ok"], result
    assert result["tool_calls_count"] == 35
    assert client.round == 36
    assert sum(m.get("role") == "tool" for m in result["messages"]) == 35


def test_architect_plan_with_verification_task_succeeds():
    """Reproduce real Claude plan with a final verification task that has owned_files: []."""
    raw_plan = json.dumps([
        {
            "id": "T1_scaffold",
            "title": "Project scaffolding",
            "depends_on": [],
            "owned_files": ["logshed/__init__.py", "pyproject.toml"],
            "acceptance": ["Scaffolding exists"],
        },
        {
            "id": "T5_cli",
            "title": "CLI entrypoints",
            "depends_on": ["T1_scaffold"],
            "owned_files": ["logshed/cli.py", "tests/test_cli.py"],
            "acceptance": [
                "tests/test_cli.py covers report",
                "python -m pytest tests/test_cli.py passes",
            ],
        },
        {
            "id": "T6_full_verify",
            "title": "Full suite + packaging verification",
            "depends_on": ["T5_cli"],
            "owned_files": [],
            "description": "No new files - run the complete test suite and packaging verification",
            "acceptance": [
                "python -m pytest -x -q passes with 0 failures",
                "pip install -e . then logshed watch sample --rules sample.yml",
            ],
        },
    ])
    raw_text = f"Here is the execution plan:\n```json\n{raw_plan}\n```\nReady to proceed."
    # 1. Validation must NOT fail with 'Each coder task must declare explicit owned_files'
    err = subagents._architect_plan_error(raw_text)
    assert err == "", f"Expected no error, got: {err}"

    # 2. Parsing must normalize acceptance list into semicolon-separated string
    tasks = subagents._parse_architect_plan(raw_text, "Build logshed")
    assert len(tasks) == 3
    t6 = tasks[2]
    assert t6["id"] == "T6_full_verify"
    assert "No new files" in t6["description"]
    assert "pytest -x -q" in t6["acceptance"]
    assert ";" in t6["acceptance"]  # List was joined cleanly

    # 3. T6 should auto-inherit files from dependencies so worker has inspection/fix scope
    assert "logshed/cli.py" in t6["owned_files"] or "tests/test_cli.py" in t6["owned_files"]


def test_architect_plan_with_no_files_anywhere_is_rejected():
    """A completely file-less plan with no inferrable paths must still fail validation."""
    raw = json.dumps([
        {"id": "t1", "title": "do vague work", "depends_on": [], "owned_files": []},
    ])
    err = subagents._architect_plan_error(raw)
    assert err == "Each coder task must declare explicit owned_files"


def test_orchestrated_turn_survives_verification_task_in_dag(tmp_path):
    """End-to-end orchestrated turn with a verification task completing cleanly."""
    previous = tools.get_project_root()
    tools.set_project_root(tmp_path)
    state = SessionState(project_root=tmp_path)
    plan = [
        {"id": "T1", "title": "Scaffold", "owned_files": ["app.py"], "depends_on": []},
        {"id": "T2_verify", "title": "Full suite verification", "owned_files": [], "depends_on": ["T1"],
         "description": "No new files - run tests"},
    ]
    calls = []

    def mock_role(**kwargs):
        role = kwargs["role"]
        calls.append(role)
        if role == "explorer":
            return {"ok": True, "final_text": "Workspace mapped."}
        if role == "architect":
            return {"ok": True, "final_text": f"```json\n{json.dumps(plan)}\n```"}
        if role == "coder":
            return {"ok": True, "final_text": "Task executed cleanly.", "written_files": ["app.py"]}
        if role == "verifier":
            return {
                "ok": True,
                "final_text": '```json\n{"verdict": "pass", "checks": ["pytest passes"], "failures": [], "summary": "all good"}\n```',
                "verification_evidence": [{"tool": "run_tests", "ok": True}],
            }
        return {"ok": True}

    try:
        with patch.object(subagents, "_run_role_agent", side_effect=mock_role):
            res = subagents.run_orchestrated_turn("Build project", state, Mock())
    finally:
        tools.set_project_root(previous)

    assert res["ok"], res
    assert "architect" in calls
    assert calls.count("coder") == 2
    assert "verifier" in calls


def test_invalid_verifier_format_retries_verifier_without_coder_repair(tmp_path):
    state = SessionState(project_root=tmp_path)
    calls = []
    plan = [{"id": "t1", "title": "Implement", "owned_files": ["app.py"], "depends_on": []}]
    def role(**kwargs):
        calls.append(kwargs)
        name = kwargs["role"]
        if name == "architect":
            return {"ok": True, "final_text": json.dumps(plan)}
        if name == "verifier":
            count = sum(c["role"] == "verifier" for c in calls)
            verdict = {"verdict": "pass", "checks": {"tests": "passed"} if count == 1 else ["tests passed"], "failures": []}
            return {"ok": True, "final_text": json.dumps(verdict), "verification_evidence": [{"ok": True}]}
        return {"ok": True, "final_text": "done", "written_files": ["app.py"] if name == "coder" else []}
    previous = tools.get_project_root()
    try:
        with patch.object(subagents, "_run_role_agent", side_effect=role):
            result = subagents.run_orchestrated_turn("Implement app", state, Mock())
    finally:
        tools.set_project_root(previous)
    assert result["ok"]
    assert sum(c["role"] == "coder" for c in calls) == 1
    verifier_calls = [c for c in calls if c["role"] == "verifier"]
    assert len(verifier_calls) == 2
    assert "previous verdict had an invalid format" in verifier_calls[1]["prompt"]

