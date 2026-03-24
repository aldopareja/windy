from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict

from .errors import WorkflowError
from .models import CollapseResult


class WorkflowStateStore:
    def __init__(self, path: Path):
        self._path = path

    @staticmethod
    def default_path() -> Path:
        runtime_root = Path(__file__).resolve().parents[1]
        return runtime_root / "state" / "workflow_state.json"

    def record_collapse(self, result: CollapseResult) -> None:
        payload = self.prepare_collapse_payload(result)
        self.write_payload(payload)

    def prepare_collapse_payload(self, result: CollapseResult) -> Dict[str, Any]:
        payload = self._load()
        spaces = payload.setdefault("spaces", {})
        spaces[result.workflow_space.storage_key] = {
            **result.to_state_payload(),
            "updated_at": _utc_now(),
        }
        payload["schema_version"] = 1
        return payload

    def write_payload(self, payload: Dict[str, Any]) -> None:
        self._write(payload)

    def _load(self) -> Dict[str, Any]:
        if not self._path.exists():
            return {"schema_version": 1, "spaces": {}}

        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise WorkflowError(
                f"Workflow state file is not valid JSON: {self._path}"
            ) from exc

    def _write(self, payload: Dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self._path.parent,
            delete=False,
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(self._path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
