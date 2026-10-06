import subprocess
import sys

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from jobsearch.cli import app
from jobsearch.config import DEFAULT_CONFIG, load_config

runner = CliRunner()


def test_config_loads_with_owner_answers():
    cfg = load_config()
    assert cfg.runtime == "local"
    assert cfg.auth == "claude-code"
    assert cfg.models.triage and cfg.models.verify
    assert cfg.http.min_interval_s_per_host >= 1.0


def test_config_rejects_unknown_keys(tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_text(DEFAULT_CONFIG.read_text(encoding="utf-8") + "\nsurprise: 1\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(bad)


def test_config_rejects_impolite_rate(tmp_path):
    text = DEFAULT_CONFIG.read_text(encoding="utf-8").replace(
        "min_interval_s_per_host: 1.0", "min_interval_s_per_host: 0.1"
    )
    bad = tmp_path / "config.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(bad)


def test_status_prints_config_and_counts():
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "# config" in result.output
    assert "models:" in result.output
    assert "data/companies" in result.output


def test_unbuilt_commands_exit_nonzero():
    result = runner.invoke(app, ["nightly"])
    assert result.exit_code == 2


def test_module_entry_point():
    proc = subprocess.run(
        [sys.executable, "-m", "jobsearch", "status"], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert "# data files" in proc.stdout
