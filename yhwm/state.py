from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional

from .errors import WorkflowError
from .models import (
    AltTabFocusGuard,
    ArmedAltTabSession,
    CollapseResult,
    EligibleWorkflowSpace,
    WorkflowSpaceState,
)


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

    def read_all_space_states(self) -> List[WorkflowSpaceState]:
        payload = self._load()
        spaces = payload["spaces"]
        all_space_states: List[WorkflowSpaceState] = []
        for storage_key, entry in spaces.items():
            workflow_space = self._parse_storage_key(storage_key)
            validated_entry = self._validate_space_entry(
                workflow_space=workflow_space,
                raw_entry=entry,
            )
            all_space_states.append(
                WorkflowSpaceState(
                    workflow_space=workflow_space,
                    visible_window_id=validated_entry["visible_window_id"],
                    background_window_ids=list(validated_entry["background_window_ids"]),
                    pending_split_direction=validated_entry["pending_split_direction"],
                )
            )
        return all_space_states

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

    def prepare_space_deletion_payload(
        self, workflow_space: EligibleWorkflowSpace
    ) -> Dict[str, Any]:
        payload = self._load()
        spaces = payload.setdefault("spaces", {})
        spaces.pop(workflow_space.storage_key, None)
        payload["schema_version"] = 1
        return payload

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

    def _parse_storage_key(self, storage_key: str) -> EligibleWorkflowSpace:
        parts = storage_key.split(":")
        if len(parts) != 2:
            raise WorkflowError(
                "Workflow state file has invalid schema: "
                f"space key '{storage_key}' must use '<display>:<space>' in {self._path}"
            )

        try:
            display = int(parts[0])
            space = int(parts[1])
        except ValueError as exc:
            raise WorkflowError(
                "Workflow state file has invalid schema: "
                f"space key '{storage_key}' must use integer display and space values "
                f"in {self._path}"
            ) from exc

        if display <= 0 or space <= 0:
            raise WorkflowError(
                "Workflow state file has invalid schema: "
                f"space key '{storage_key}' must use positive integer display and "
                f"space values in {self._path}"
            )

        workflow_space = EligibleWorkflowSpace(display=display, space=space)
        if workflow_space.storage_key != storage_key:
            raise WorkflowError(
                "Workflow state file has invalid schema: "
                f"space key '{storage_key}' is not in canonical '<display>:<space>' form "
                f"in {self._path}"
            )

        return workflow_space

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


class AltTabSessionStore:
    def __init__(self, path: Path):
        self._path = path

    @staticmethod
    def default_path() -> Path:
        runtime_root = Path(__file__).resolve().parents[1]
        return runtime_root / "state" / "alttab_session.json"

    def read_session(self) -> Optional[ArmedAltTabSession]:
        return self._parse_session(self._load()["session"])

    def read_focus_guard(self) -> Optional[AltTabFocusGuard]:
        return self._parse_focus_guard(self._load()["focus_guard"])

    def arm_session(self, session: ArmedAltTabSession) -> None:
        self._write_state(session=session, focus_guard=None)

    def disarm_session(self, *, focus_guard: AltTabFocusGuard | None = None) -> None:
        self._write_state(session=None, focus_guard=focus_guard)

    def clear_focus_guard(self) -> None:
        current_session = self.read_session()
        self._write_state(session=current_session, focus_guard=None)

    def _parse_session(self, raw_session: Any) -> Optional[ArmedAltTabSession]:
        if raw_session is None:
            return None

        if not isinstance(raw_session, dict):
            raise WorkflowError(
                "AltTab session file has invalid schema: "
                f"'session' must be an object or null in {self._path}"
            )

        origin_window_id = raw_session.get("origin_window_id")
        if not isinstance(origin_window_id, int) or origin_window_id <= 0:
            raise WorkflowError(
                "AltTab session file has invalid schema: "
                f"'origin_window_id' must be a positive integer in {self._path}"
            )

        origin_display = raw_session.get("origin_display")
        if not isinstance(origin_display, int) or origin_display <= 0:
            raise WorkflowError(
                "AltTab session file has invalid schema: "
                f"'origin_display' must be a positive integer in {self._path}"
            )

        origin_space = raw_session.get("origin_space")
        if not isinstance(origin_space, int) or origin_space <= 0:
            raise WorkflowError(
                "AltTab session file has invalid schema: "
                f"'origin_space' must be a positive integer in {self._path}"
            )

        selected_window_id = raw_session.get("selected_window_id")
        if selected_window_id is None:
            normalized_selected_window_id = origin_window_id
        else:
            if not isinstance(selected_window_id, int) or selected_window_id <= 0:
                raise WorkflowError(
                    "AltTab session file has invalid schema: "
                    f"'selected_window_id' must be a positive integer when present in {self._path}"
                )
            normalized_selected_window_id = selected_window_id

        return ArmedAltTabSession(
            origin_window_id=origin_window_id,
            origin_workflow_space=EligibleWorkflowSpace(
                display=origin_display,
                space=origin_space,
            ),
            selected_window_id=normalized_selected_window_id,
        )

    def _parse_focus_guard(self, raw_focus_guard: Any) -> Optional[AltTabFocusGuard]:
        if raw_focus_guard is None:
            return None

        if not isinstance(raw_focus_guard, dict):
            raise WorkflowError(
                "AltTab session file has invalid schema: "
                f"'focus_guard' must be an object or null in {self._path}"
            )

        display = raw_focus_guard.get("display")
        if not isinstance(display, int) or display <= 0:
            raise WorkflowError(
                "AltTab session file has invalid schema: "
                f"'focus_guard.display' must be a positive integer in {self._path}"
            )

        space = raw_focus_guard.get("space")
        if not isinstance(space, int) or space <= 0:
            raise WorkflowError(
                "AltTab session file has invalid schema: "
                f"'focus_guard.space' must be a positive integer in {self._path}"
            )

        target_window_id = raw_focus_guard.get("target_window_id")
        if target_window_id is not None:
            if not isinstance(target_window_id, int) or target_window_id <= 0:
                raise WorkflowError(
                    "AltTab session file has invalid schema: "
                    f"'focus_guard.target_window_id' must be a positive integer when present in {self._path}"
                )

        return AltTabFocusGuard(
            workflow_space=EligibleWorkflowSpace(display=display, space=space),
            target_window_id=target_window_id,
        )

    def _load(self) -> Dict[str, Any]:
        if not self._path.exists():
            return {"schema_version": 1, "session": None, "focus_guard": None}

        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkflowError(
                f"AltTab session file is not readable: {self._path}"
            ) from exc

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise WorkflowError(
                f"AltTab session file is not valid JSON: {self._path}"
            ) from exc
        return self._validate_loaded_payload(payload)

    def _validate_loaded_payload(self, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise WorkflowError(
                f"AltTab session file has invalid schema: expected a top-level object in {self._path}"
            )

        normalized_payload = dict(payload)

        schema_version = normalized_payload.get("schema_version")
        if schema_version is not None and not isinstance(schema_version, int):
            raise WorkflowError(
                "AltTab session file has invalid schema: "
                f"'schema_version' must be an integer in {self._path}"
            )

        if "session" not in normalized_payload:
            normalized_payload["session"] = None
        if "focus_guard" not in normalized_payload:
            normalized_payload["focus_guard"] = None
        return normalized_payload

    def _write_state(
        self,
        *,
        session: ArmedAltTabSession | None,
        focus_guard: AltTabFocusGuard | None,
    ) -> None:
        payload = {
            "schema_version": 1,
            "session": None,
            "focus_guard": None,
        }
        if session is not None:
            payload["session"] = {
                "origin_window_id": session.origin_window_id,
                "origin_display": session.origin_workflow_space.display,
                "origin_space": session.origin_workflow_space.space,
                "updated_at": _utc_now(),
            }
            if session.selected_window_id is not None:
                payload["session"]["selected_window_id"] = session.selected_window_id
        if focus_guard is not None:
            focus_guard_payload: Dict[str, Any] = {
                "display": focus_guard.workflow_space.display,
                "space": focus_guard.workflow_space.space,
                "updated_at": _utc_now(),
            }
            if focus_guard.target_window_id is not None:
                focus_guard_payload["target_window_id"] = focus_guard.target_window_id
            payload["focus_guard"] = focus_guard_payload

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
