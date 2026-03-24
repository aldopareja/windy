from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional

from .errors import WorkflowError
from .models import CollapseResult, EligibleWorkflowSpace, WorkflowSpaceState


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
        return self.prepare_background_pool_payload(
            workflow_space=result.workflow_space,
            visible_window_id=result.focused_window_id,
            background_window_ids=result.background_window_ids,
            pending_split_direction=None,
        )

    def read_space_state(
        self, workflow_space: EligibleWorkflowSpace
    ) -> Optional[WorkflowSpaceState]:
        payload = self._load()
        spaces = payload["spaces"]
        entry = spaces.get(workflow_space.storage_key)
        if entry is None:
            return None

        validated_entry = self._validate_space_entry(
            workflow_space=workflow_space,
            raw_entry=entry,
        )
        return WorkflowSpaceState(
            workflow_space=workflow_space,
            visible_window_id=validated_entry["visible_window_id"],
            background_window_ids=list(validated_entry["background_window_ids"]),
            pending_split_direction=validated_entry["pending_split_direction"],
        )

    def read_background_window_ids(
        self, workflow_space: EligibleWorkflowSpace
    ) -> List[int]:
        space_state = self.read_space_state(workflow_space)
        if space_state is None:
            return []
        return list(space_state.background_window_ids)

    def prepare_background_pool_payload(
        self,
        *,
        workflow_space: EligibleWorkflowSpace,
        visible_window_id: int,
        background_window_ids: List[int],
        pending_split_direction: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = self._load()
        spaces = payload.setdefault("spaces", {})
        entry = {
            "display": workflow_space.display,
            "space": workflow_space.space,
            "visible_window_id": visible_window_id,
            "background_window_ids": list(background_window_ids),
            "updated_at": _utc_now(),
        }
        if pending_split_direction is not None:
            entry["pending_split_direction"] = pending_split_direction
        spaces[workflow_space.storage_key] = entry
        payload["schema_version"] = 1
        return payload

    def write_payload(self, payload: Dict[str, Any]) -> None:
        self._write(payload)

    def _load(self) -> Dict[str, Any]:
        if not self._path.exists():
            return {"schema_version": 1, "spaces": {}}

        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkflowError(f"Workflow state file is not readable: {self._path}") from exc

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise WorkflowError(
                f"Workflow state file is not valid JSON: {self._path}"
            ) from exc
        return self._validate_loaded_payload(payload)

    def _validate_loaded_payload(self, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise WorkflowError(
                f"Workflow state file has invalid schema: expected a top-level object in {self._path}"
            )

        normalized_payload = dict(payload)

        schema_version = normalized_payload.get("schema_version")
        if schema_version is not None and not isinstance(schema_version, int):
            raise WorkflowError(
                "Workflow state file has invalid schema: "
                f"'schema_version' must be an integer in {self._path}"
            )

        spaces = normalized_payload.get("spaces")
        if spaces is None:
            normalized_payload["spaces"] = {}
            return normalized_payload

        if not isinstance(spaces, dict):
            raise WorkflowError(
                f"Workflow state file has invalid schema: 'spaces' must be an object in {self._path}"
            )

        normalized_spaces: Dict[str, Any] = {}
        for key, value in spaces.items():
            if not isinstance(key, str):
                raise WorkflowError(
                    "Workflow state file has invalid schema: "
                    f"space keys must be strings in {self._path}"
                )
            if not isinstance(value, dict):
                raise WorkflowError(
                    "Workflow state file has invalid schema: "
                    f"space entry '{key}' must be an object in {self._path}"
                )
            normalized_spaces[key] = dict(value)

        normalized_payload["spaces"] = normalized_spaces
        return normalized_payload

    def _validate_space_entry(
        self,
        *,
        workflow_space: EligibleWorkflowSpace,
        raw_entry: Any,
    ) -> Dict[str, Any]:
        if not isinstance(raw_entry, dict):
            raise WorkflowError(
                "Workflow state file has invalid schema: "
                f"space entry '{workflow_space.storage_key}' must be an object in {self._path}"
            )

        entry = dict(raw_entry)

        display = entry.get("display")
        if not isinstance(display, int):
            raise WorkflowError(
                "Workflow state file has invalid schema: "
                f"space entry '{workflow_space.storage_key}' must contain integer 'display' "
                f"in {self._path}"
            )

        space = entry.get("space")
        if not isinstance(space, int):
            raise WorkflowError(
                "Workflow state file has invalid schema: "
                f"space entry '{workflow_space.storage_key}' must contain integer 'space' "
                f"in {self._path}"
            )

        if display != workflow_space.display or space != workflow_space.space:
            raise WorkflowError(
                "Workflow state file has invalid schema: "
                f"space entry '{workflow_space.storage_key}' does not match its stored display/space "
                f"in {self._path}"
            )

        visible_window_id = entry.get("visible_window_id")
        if not isinstance(visible_window_id, int):
            raise WorkflowError(
                "Workflow state file has invalid schema: "
                f"space entry '{workflow_space.storage_key}' must contain integer "
                f"'visible_window_id' in {self._path}"
            )

        background_window_ids = entry.get("background_window_ids")
        if not isinstance(background_window_ids, list):
            raise WorkflowError(
                "Workflow state file has invalid schema: "
                f"space entry '{workflow_space.storage_key}' must contain array "
                f"'background_window_ids' in {self._path}"
            )

        normalized_background_window_ids: List[int] = []
        for window_id in background_window_ids:
            if not isinstance(window_id, int):
                raise WorkflowError(
                    "Workflow state file has invalid schema: "
                    f"space entry '{workflow_space.storage_key}' must contain only integer "
                    f"background window ids in {self._path}"
                )
            normalized_background_window_ids.append(window_id)

        pending_split_direction = entry.get("pending_split_direction")
        normalized_pending_split_direction: Optional[str] = None
        if pending_split_direction is not None:
            if not isinstance(pending_split_direction, str):
                raise WorkflowError(
                    "Workflow state file has invalid schema: "
                    f"space entry '{workflow_space.storage_key}' must contain string "
                    f"'pending_split_direction' when present in {self._path}"
                )
            normalized_pending_split_direction = pending_split_direction.strip().lower()
            if not normalized_pending_split_direction:
                raise WorkflowError(
                    "Workflow state file has invalid schema: "
                    f"space entry '{workflow_space.storage_key}' must not contain an empty "
                    f"'pending_split_direction' in {self._path}"
                )

        return {
            "display": display,
            "space": space,
            "visible_window_id": visible_window_id,
            "background_window_ids": normalized_background_window_ids,
            "pending_split_direction": normalized_pending_split_direction,
        }

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
