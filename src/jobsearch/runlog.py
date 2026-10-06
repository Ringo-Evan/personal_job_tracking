"""Append one line per run to logs/runs.jsonl."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jobsearch.config import ROOT

RUNS_LOG = ROOT / "logs" / "runs.jsonl"


def log_run(entry: dict[str, Any], path: Path = RUNS_LOG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
