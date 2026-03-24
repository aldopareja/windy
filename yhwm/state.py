from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, Optional

from .errors import WorkflowError
from .models import AltTabSession, EligibleWorkflowSpace, FocusGuard, RuntimeState, TrackedSpaceState

SUPPORTED_SPLIT_DIRECTIONS = frozenset({"east", "south"})


class RuntimeStateStore:
    def __init__(self, path: Path):
        self._path = path

    @staticmethod
    def default_path() -> Path:
        runtime_root = Path(__file__).resolve().parents[1]
        return runtime_root / "state" / "yhwm-state-v2.json"

    def read(self) -> RuntimeState:
        if not self._path.exists():
            return RuntimeState.empty()

        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkflowError(f"Runtime state file is not readable: {self._path}") from exc

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"Runtime state file is not valid JSON: {self._path}") from exc

        return self._parse_payload(payload)

    def write(self, state: RuntimeState) -> None:
        payload = {
            "schema_version": 2,
            "spaces": {
                storage_key: {
                    "display": tracked.workflow_space.display,
                    "space": tracked.workflow_space.space,
                    "leader_window_id": tracked.leader_window_id,
                    "background_window_ids": list(tracked.background_window_ids),
                    "updated_at": _utc_now(),
                    **(
                        {"pending_split_direction": tracked.pending_split_direction}
                        if tracked.pending_split_direction is not None
                        else {}
                    ),
                }
                for storage_key, tracked in state.spaces.items()
            },
            "alttab": {
                "session": None,
                "focus_guard": None,
            },
        }

        if state.alttab_session is not None:
            payload["alttab"]["session"] = {
                "origin_window_id": state.alttab_session.origin_window_id,
                "origin_display": state.alttab_session.origin_workflow_space.display,
                "origin_space": state.alttab_session.origin_workflow_space.space,
                "updated_at": _utc_now(),
            }

        if state.focus_guard is not None:
            focus_guard_payload: Dict[str, Any] = {
                "display": state.focus_guard.workflow_space.display,
                "space": state.focus_guard.workflow_space.space,
                "updated_at": _utc_now(),
            }
            if state.focus_guard.target_window_id is not None:
                focus_guard_payload["target_window_id"] = state.focus_guard.target_window_id
            payload["alttab"]["focus_guard"] = focus_guard_payload

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

    def _parse_payload(self, payload: Any) -> RuntimeState:
        if not isinstance(payload, dict):
            raise WorkflowError(
                f"Runtime state file has invalid schema: expected an object in {self._path}"
            )

        schema_version = payload.get("schema_version")
        if schema_version != 2:
            raise WorkflowError(
                "Runtime state file has invalid schema: "
                f"expected 'schema_version' 2 in {self._path}"
            )

        raw_spaces = payload.get("spaces")
        if not isinstance(raw_spaces, dict):
            raise WorkflowError(
                f"Runtime state file has invalid schema: 'spaces' must be an object in {self._path}"
            )

        spaces: Dict[str, TrackedSpaceState] = {}
        for storage_key, raw_entry in raw_spaces.items():
            workflow_space = self._parse_storage_key(storage_key)
            if not isinstance(raw_entry, dict):
                raise WorkflowError(
                    "Runtime state file has invalid schema: "
                    f"space entry '{storage_key}' must be an object in {self._path}"
                )
            display = _require_positive_int(raw_entry, "display", self._path, storage_key)
            space = _require_positive_int(raw_entry, "space", self._path, storage_key)
            if display != workflow_space.display or space != workflow_space.space:
                raise WorkflowError(
                    "Runtime state file has invalid schema: "
                    f"space entry '{storage_key}' does not match its key in {self._path}"
                )
            leader_window_id = _require_positive_int(
                raw_entry,
                "leader_window_id",
                self._path,
                storage_key,
            )
            background_window_ids = _require_positive_int_list(
                raw_entry,
                "background_window_ids",
                self._path,
                storage_key,
            )
            pending_split_direction = raw_entry.get("pending_split_direction")
            if pending_split_direction is not None:
                if not isinstance(pending_split_direction, str):
                    raise WorkflowError(
                        "Runtime state file has invalid schema: "
                        f"'pending_split_direction' must be text for '{storage_key}' in {self._path}"
                    )
                pending_split_direction = pending_split_direction.strip().lower()
                if pending_split_direction not in SUPPORTED_SPLIT_DIRECTIONS:
                    raise WorkflowError(
                        "Runtime state file has invalid schema: "
                        f"'pending_split_direction' must be one of {sorted(SUPPORTED_SPLIT_DIRECTIONS)} "
                        f"for '{storage_key}' in {self._path}"
                    )
            spaces[storage_key] = TrackedSpaceState(
                workflow_space=workflow_space,
                leader_window_id=leader_window_id,
                background_window_ids=background_window_ids,
                pending_split_direction=pending_split_direction,
            )

        raw_alttab = payload.get("alttab")
        if raw_alttab is None:
            raw_alttab = {"session": None, "focus_guard": None}
        if not isinstance(raw_alttab, dict):
            raise WorkflowError(
                f"Runtime state file has invalid schema: 'alttab' must be an object in {self._path}"
            )

        return RuntimeState(
            spaces=spaces,
            alttab_session=self._parse_session(raw_alttab.get("session")),
            focus_guard=self._parse_focus_guard(raw_alttab.get("focus_guard")),
        )

    def _parse_storage_key(self, storage_key: str) -> EligibleWorkflowSpace:
        if not isinstance(storage_key, str):
            raise WorkflowError(
                f"Runtime state file has invalid schema: space keys must be text in {self._path}"
            )
        parts = storage_key.split(":")
        if len(parts) != 2:
            raise WorkflowError(
                "Runtime state file has invalid schema: "
                f"space key '{storage_key}' must use '<display>:<space>' in {self._path}"
            )
        try:
            display = int(parts[0])
            space = int(parts[1])
        except ValueError as exc:
            raise WorkflowError(
                "Runtime state file has invalid schema: "
                f"space key '{storage_key}' must use integer values in {self._path}"
            ) from exc
        if display <= 0 or space <= 0:
            raise WorkflowError(
                "Runtime state file has invalid schema: "
                f"space key '{storage_key}' must use positive integers in {self._path}"
            )
        workflow_space = EligibleWorkflowSpace(display=display, space=space)
        if workflow_space.storage_key != storage_key:
            raise WorkflowError(
                "Runtime state file has invalid schema: "
                f"space key '{storage_key}' is not canonical in {self._path}"
            )
        return workflow_space

    def _parse_session(self, raw_session: Any) -> Optional[AltTabSession]:
        if raw_session is None:
            return None
        if not isinstance(raw_session, dict):
            raise WorkflowError(
                f"Runtime state file has invalid schema: 'session' must be an object in {self._path}"
            )
        origin_window_id = _require_positive_int(raw_session, "origin_window_id", self._path, "session")
        origin_display = _require_positive_int(raw_session, "origin_display", self._path, "session")
        origin_space = _require_positive_int(raw_session, "origin_space", self._path, "session")
        return AltTabSession(
            origin_window_id=origin_window_id,
            origin_workflow_space=EligibleWorkflowSpace(
                display=origin_display,
                space=origin_space,
            ),
        )

    def _parse_focus_guard(self, raw_focus_guard: Any) -> Optional[FocusGuard]:
        if raw_focus_guard is None:
            return None
        if not isinstance(raw_focus_guard, dict):
            raise WorkflowError(
                f"Runtime state file has invalid schema: 'focus_guard' must be an object in {self._path}"
            )
        display = _require_positive_int(raw_focus_guard, "display", self._path, "focus_guard")
        space = _require_positive_int(raw_focus_guard, "space", self._path, "focus_guard")
        target_window_id = raw_focus_guard.get("target_window_id")
        if target_window_id is not None and (
            not isinstance(target_window_id, int) or target_window_id <= 0
        ):
            raise WorkflowError(
                "Runtime state file has invalid schema: "
                f"'focus_guard.target_window_id' must be a positive integer in {self._path}"
            )
        return FocusGuard(
            workflow_space=EligibleWorkflowSpace(display=display, space=space),
            target_window_id=target_window_id,
        )


def _require_positive_int(
    record: Dict[str, Any],
    key: str,
    path: Path,
    context: str,
) -> int:
    value = record.get(key)
    if not isinstance(value, int) or value <= 0:
        raise WorkflowError(
            "Runtime state file has invalid schema: "
            f"'{key}' must be a positive integer for '{context}' in {path}"
        )
    return value


def _require_positive_int_list(
    record: Dict[str, Any],
    key: str,
    path: Path,
    context: str,
) -> list[int]:
    value = record.get(key)
    if not isinstance(value, list):
        raise WorkflowError(
            "Runtime state file has invalid schema: "
            f"'{key}' must be an array for '{context}' in {path}"
        )
    normalized: list[int] = []
    for item in value:
        if not isinstance(item, int) or item <= 0:
            raise WorkflowError(
                "Runtime state file has invalid schema: "
                f"'{key}' must contain only positive integers for '{context}' in {path}"
            )
        if item not in normalized:
            normalized.append(item)
    return normalized


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )
