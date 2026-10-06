import json
import os
import subprocess

import pytest

from jobsearch.agent import UsageLimitError, agent_env, build_command, parse_output, run_agent


def ok_payload(structured, cost=0.01):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(structured),
                       "structured_output": structured, "total_cost_usd": cost,
                       "usage": {"input_tokens": 100, "output_tokens": 50}})  # fmt: skip


class FakeRunner:
    def __init__(self, stdout="", stderr="", returncode=0, raise_timeout=False, write_file=False):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode
        self.raise_timeout, self.write_file = raise_timeout, write_file
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        assert os.listdir(kw["cwd"]) == []  # fresh empty workdir
        if self.write_file:
            open(os.path.join(kw["cwd"], "pwned.txt"), "w").close()
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


def call(cfg, runner, tools=(), schema=None):
    return run_agent(cfg, prompt="hello", system_prompt="sys", model="m", tools=list(tools), schema=schema,
                     timeout_s=30, runner=runner, claude_path="claude")  # fmt: skip


# --- command line --------------------------------------------------------------------------------


def flag(cmd, name):
    return cmd[cmd.index(name) + 1]


def test_no_tools_means_empty_tools_flag_and_no_allowlist():
    cmd = build_command("claude", "m", [], "sys", None)
    assert flag(cmd, "--tools") == ""
    assert "--allowedTools" not in cmd
    for required in ("-p", "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
        assert required in cmd
    assert flag(cmd, "--output-format") == "json"
    assert flag(cmd, "--setting-sources") == ""
    assert flag(cmd, "--permission-prompts") == "none"


def test_web_tools_allowlist_is_exact():
    cmd = build_command("claude", "m", ["WebSearch", "WebFetch"], "sys", {"type": "object"})
    assert flag(cmd, "--tools") == "WebSearch,WebFetch"
    assert flag(cmd, "--allowedTools") == "WebSearch,WebFetch"
    assert json.loads(flag(cmd, "--json-schema")) == {"type": "object"}
    joined = " ".join(cmd)
    for forbidden in ("Bash", "Write", "Edit", "Read", "PowerShell", "dangerously", "bypassPermissions"):
        assert forbidden not in joined


def test_prompt_goes_on_stdin_not_argv(cfg):
    r = FakeRunner(stdout=ok_payload({"a": 1}))
    call(cfg, r)
    cmd, kw = r.calls[0]
    assert kw["input"] == "hello"
    assert "hello" not in cmd


# --- environment ---------------------------------------------------------------------------------


def test_env_drops_secrets_and_unlisted_vars(cfg):
    base = {"PATH": "/bin", "USERPROFILE": "C:/Users/x", "ANTHROPIC_API_KEY": "sk-ant-secret",
            "GITLAB_TOKEN": "glpat-x", "AWS_SECRET_ACCESS_KEY": "a", "RANDOM_VAR": "x", "APPDATA": "C:/a"}  # fmt: skip
    env = agent_env(cfg, base)
    assert env["PATH"] == "/bin" and env["USERPROFILE"] == "C:/Users/x" and env["APPDATA"] == "C:/a"
    for gone in ("ANTHROPIC_API_KEY", "GITLAB_TOKEN", "AWS_SECRET_ACCESS_KEY", "RANDOM_VAR"):
        assert gone not in env


def test_env_keeps_api_key_only_in_api_key_mode(cfg):
    cfg.auth = "api-key"
    assert (
        agent_env(cfg, {"ANTHROPIC_API_KEY": "sk-ant-x", "PATH": "/bin"})["ANTHROPIC_API_KEY"] == "sk-ant-x"
    )


def test_runner_receives_scrubbed_env(cfg, monkeypatch):
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "t0ps3cret")
    r = FakeRunner(stdout=ok_payload({"a": 1}))
    call(cfg, r)
    assert "SOME_SERVICE_TOKEN" not in r.calls[0][1]["env"]
    assert "t0ps3cret" not in json.dumps(r.calls[0][1]["env"])


# --- results -------------------------------------------------------------------------------------


def test_success_returns_structured_output_and_cost(cfg):
    res = call(cfg, FakeRunner(stdout=ok_payload({"companies": []}, cost=0.0123)))
    assert res.ok and res.structured == {"companies": []} and res.cost_usd == 0.0123
    assert res.usage["output_tokens"] == 50


def test_falls_back_to_result_text_json():
    payload = json.dumps({"subtype": "success", "is_error": False, "result": '{"x": 1}'})
    assert parse_output(payload, "", 0, 1.0).structured == {"x": 1}


def test_error_result_is_not_ok():
    payload = json.dumps({"subtype": "error_max_turns", "is_error": True, "result": "too many turns"})
    res = parse_output(payload, "", 1, 1.0)
    assert not res.ok and "error_max_turns" in res.error


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        (json.dumps({"subtype": "success", "is_error": True, "api_error_status": 429, "result": "x"}), ""),
        (
            json.dumps(
                {"subtype": "error", "is_error": True, "result": "Claude usage limit reached. Resets 5pm"}
            ),
            "",
        ),
        ("", "Error: rate_limit_error"),
    ],
)
def test_usage_limits_raise(stdout, stderr):
    with pytest.raises(UsageLimitError):
        parse_output(stdout, stderr, 1, 1.0)


def test_non_json_output_is_error_not_crash():
    res = parse_output("Segmentation fault", "", 139, 1.0)
    assert not res.ok and "non-JSON" in res.error


def test_timeout_is_error(cfg):
    res = call(cfg, FakeRunner(raise_timeout=True))
    assert not res.ok and "timed out" in res.error


def test_files_written_to_workdir_fail_the_call(cfg):
    res = call(cfg, FakeRunner(stdout=ok_payload({"a": 1}), write_file=True))
    assert not res.ok and "wrote files" in res.error


def test_workdir_removed_afterwards(cfg):
    r = FakeRunner(stdout=ok_payload({"a": 1}))
    call(cfg, r)
    assert not os.path.exists(r.calls[0][1]["cwd"])


@pytest.mark.parametrize(("thinking", "expected"), [(True, None), (False, "0")])
def test_thinking_switch(cfg, thinking, expected):
    r = FakeRunner(stdout=ok_payload({"a": 1}))
    run_agent(cfg, prompt="p", system_prompt="s", model="m", tools=[], schema=None, timeout_s=5, runner=r,
              claude_path="claude", thinking=thinking)  # fmt: skip
    assert r.calls[0][1]["env"].get("MAX_THINKING_TOKENS") == expected
