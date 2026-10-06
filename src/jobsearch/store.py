"""Read and write data/*.json atomically."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from jobsearch.config import ROOT

DATA = ROOT / "data"


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> bool:
    """Atomically write obj as JSON. Returns False (and leaves the file alone) if content is unchanged."""
    text = dumps(obj)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def id_sort_key(company_id: str) -> tuple[str, int, str]:
    """C2 < C10 < C100 < C1000, not string order."""
    m = re.match(r"([A-Za-z]*)(\d+)(.*)", company_id)
    return (m.group(1), int(m.group(2)), m.group(3)) if m else (company_id, 0, "")


class Store:
    def __init__(self, data_dir: Path = DATA):
        self.data_dir = data_dir
        self.companies_dir = data_dir / "companies"

    def company_path(self, company_id: str) -> Path:
        return self.companies_dir / f"{company_id}.json"

    def load_company(self, company_id: str) -> dict[str, Any] | None:
        return read_json(self.company_path(company_id))

    def save_company(self, record: dict[str, Any]) -> bool:
        return write_json(self.company_path(record["company_id"]), record)

    def company_ids(self) -> list[str]:
        if not self.companies_dir.is_dir():
            return []
        return sorted((p.stem for p in self.companies_dir.glob("*.json")), key=id_sort_key)

    def iter_companies(self):
        for cid in self.company_ids():
            yield self.load_company(cid)

    @property
    def ingest_state_path(self) -> Path:
        return self.data_dir / "ingest_state.json"
