import json
import subprocess
from pathlib import Path

import pytest

from jobsearch.agent import UsageLimitError
from jobsearch.codex_agent import build_command, codex_env, run_codex

SCHEMA = {
    "type": "object",
    "properties": {"n": {"type": "integer"}},
    "required": ["n"],
    "additionalProperties": False,
}


class FakeRunner:
    """subprocess.run stand-in: writes `answer` to the --output-last-message file and records the call."""

    def __init__(self, answer='{"n": 2}', returncode=0, stdout="", stderr="", touch_workdir=False):
        self.answer, self.returncode, self.stdout, self.stderr = answer, returncode, stdout, stderr
        self.touch_workdir = touch_workdir
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        self.schema = json.loads(Path(cmd[cmd.index("--output-schema") + 1]).read_text(encoding="utf-8"))
        if self.answer is not None:
            Path(cmd[cmd.index("--output-last-message") + 1]).write_text(self.answer, encoding="utf-8")
        if self.touch_workdir:
            Path(kw["cwd"], "stolen.txt").write_text("x")
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


def run(cfg, runner, **kw):
    args = {
        "prompt": "rate these",
        "system_prompt": "SYS",
        "model": "default",
        "schema": SCHEMA,
        "timeout_s": 60,
    }
    return run_codex(cfg, runner=runner, codex_path="codex", **(args | kw))


def test_command_is_sandboxed_and_ephemeral():
    cmd = build_command("codex", "default", "s.json", "o.json", "/w", {"model_reasoning_effort": "low"})
    for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--json"):
        assert flag in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "--model" not in cmd  # "default" = the account's default model
    assert cmd[cmd.index("-c") + 1] == 'model_reasoning_effort="low"' and cmd[-1] == "-"
    assert "dangerously" not in " ".join(cmd)
    assert build_command("codex", "gpt-x", "s", "o", "/w", {})[-3:-1] == ["--model", "gpt-x"]


def test_env_drops_secrets_but_keeps_codex_home(cfg):
    env = codex_env(cfg, {"PATH": "p", "USERPROFILE": "u", "CODEX_HOME": "c", "OPENAI_API_KEY": "sk", "GEMINI_API_KEY": "g",
                          "ANTHROPIC_API_KEY": "a"})  # fmt: skip
    assert env["CODEX_HOME"] == "c" and env["USERPROFILE"] == "u"
    assert not {"OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"} & set(env)


def test_success_returns_structured_answer_and_usage(cfg):
    events = "\n".join(
        json.dumps(e)
        for e in [
            {"type": "x"},
            {"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 9}},
        ]
    )
    runner = FakeRunner(stdout=events)
    r = run(cfg, runner)
    assert r.ok and r.structured == {"n": 2} and r.usage == {"input_tokens": 50, "output_tokens": 9}
    cmd, kw = runner.calls[0]
    assert kw["input"].startswith("SYS") and "rate these" in kw["input"]
    assert runner.schema == SCHEMA
    assert not Path(kw["cwd"]).exists()  # temp workdir cleaned up


def test_usage_limit_raises(cfg):
    with pytest.raises(UsageLimitError):
        run(
            cfg, FakeRunner(answer=None, returncode=1, stderr="You've hit your usage limit. Try again later.")
        )


def test_other_failures_return_not_ok(cfg):
    r = run(cfg, FakeRunner(answer=None, returncode=1, stderr="Not logged in"))
    assert not r.ok and "Not logged in" in r.error
    assert not run(cfg, FakeRunner(answer="not json")).ok


def test_files_written_to_workdir_fail_the_call(cfg):
    r = run(cfg, FakeRunner(touch_workdir=True))
    assert not r.ok and "stolen.txt" in r.error
