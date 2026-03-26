from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict

from .errors import WorkflowError
from .models import EligibleWorkflowSpace, NormalizedFrame, PendingSplit, RuntimeState, TrackedSpaceState


class RuntimeStateStore:
    def __init__(self, path: Path):
        self._path = path

    @staticmethod
    def default_path() -> Path:
        runtime_root = Path(__file__).resolve().parents[1]
        return runtime_root / "state" / "yhwm-state-v4.json"

    def read(self) -> RuntimeState:
        if not self._path.exists():
            return RuntimeState.empty()

        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkflowError(f"Runtime state file is not readable: {self._path}") from exc

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            return RuntimeState.empty()

        return self._parse_payload(payload)

    def write(self, state: RuntimeState) -> None:
        payload = {
            "schema_version": 4,
            "spaces": {
                storage_key: {
                    "display": tracked.workflow_space.display,
                    "space": tracked.workflow_space.space,
                    "updated_at": _utc_now(),
                    **(
                        {
                            "pending_split": {
                                "direction": tracked.pending_split.direction,
                                "anchor_window_id": tracked.pending_split.anchor_window_id,
                                "anchor_frame": {
                                    "x": tracked.pending_split.anchor_frame.x,
                                    "y": tracked.pending_split.anchor_frame.y,
                                    "w": tracked.pending_split.anchor_frame.w,
                                    "h": tracked.pending_split.anchor_frame.h,
                                },
                                "updated_at": _utc_now(),
                            }
                        }
                        if tracked.pending_split is not None
                        else {}
                    ),
                }
                for storage_key, tracked in sorted(state.spaces.items())
            },
        }

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
            return RuntimeState.empty()

        if payload.get("schema_version") != 4:
            return RuntimeState.empty()

        raw_spaces = payload.get("spaces")
        if not isinstance(raw_spaces, dict):
            return RuntimeState.empty()

        spaces: Dict[str, TrackedSpaceState] = {}
        for storage_key, raw_entry in raw_spaces.items():
            if not isinstance(storage_key, str):
                return RuntimeState.empty()
            workflow_space = _parse_storage_key(storage_key, self._path)
            raw_space = _require_object(raw_entry, self._path, storage_key)
            display = _require_positive_int(raw_space, "display", self._path, storage_key)
            space = _require_positive_int(raw_space, "space", self._path, storage_key)
            if display != workflow_space.display or space != workflow_space.space:
                raise WorkflowError(
                    "Runtime state file has invalid schema: "
                    f"space entry '{storage_key}' does not match its key in {self._path}"
                )
            spaces[storage_key] = TrackedSpaceState(
                workflow_space=workflow_space,
                pending_split=_parse_pending_split(raw_space.get("pending_split"), self._path, storage_key),
            )
        return RuntimeState(spaces=spaces)


def _parse_storage_key(storage_key: str, path: Path) -> EligibleWorkflowSpace:
    parts = storage_key.split(":")
    if len(parts) != 2:
        raise WorkflowError(
            "Runtime state file has invalid schema: "
            f"space key '{storage_key}' must use '<display>:<space>' in {path}"
        )
    try:
        display = int(parts[0])
        space = int(parts[1])
    except ValueError as exc:
        raise WorkflowError(
            "Runtime state file has invalid schema: "
            f"space key '{storage_key}' must use integer values in {path}"
        ) from exc
    if display <= 0 or space <= 0:
        raise WorkflowError(
            "Runtime state file has invalid schema: "
            f"space key '{storage_key}' must use positive integers in {path}"
        )
    workflow_space = EligibleWorkflowSpace(display=display, space=space)
    if workflow_space.storage_key != storage_key:
        raise WorkflowError(
            "Runtime state file has invalid schema: "
            f"space key '{storage_key}' is not canonical in {path}"
        )
    return workflow_space


def _parse_pending_split(raw_pending_split: Any, path: Path, storage_key: str) -> PendingSplit | None:
    if raw_pending_split is None:
        return None
    pending_split = _require_object(raw_pending_split, path, f"{storage_key}:pending_split")
    direction = pending_split.get("direction")
    if not isinstance(direction, str):
        raise WorkflowError(
            "Runtime state file has invalid schema: "
            f"'pending_split.direction' must be text in '{storage_key}' in {path}"
        )
    anchor_window_id = _require_positive_int(
        pending_split,
        "anchor_window_id",
        path,
        f"{storage_key}:pending_split",
    )
    raw_frame = _require_object(
        pending_split.get("anchor_frame"),
        path,
        f"{storage_key}:pending_split.anchor_frame",
    )
    return PendingSplit(
        direction=direction,
        anchor_window_id=anchor_window_id,
        anchor_frame=NormalizedFrame(
            x=_require_int(raw_frame, "x", path, f"{storage_key}:pending_split.anchor_frame"),
            y=_require_int(raw_frame, "y", path, f"{storage_key}:pending_split.anchor_frame"),
            w=_require_positive_int(raw_frame, "w", path, f"{storage_key}:pending_split.anchor_frame"),
            h=_require_positive_int(raw_frame, "h", path, f"{storage_key}:pending_split.anchor_frame"),
        ),
    )


def _require_object(value: Any, path: Path, location: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowError(
            "Runtime state file has invalid schema: "
            f"expected an object for '{location}' in {path}"
        )
    return value


def _require_positive_int(record: Dict[str, Any], key: str, path: Path, location: str) -> int:
    value = _require_int(record, key, path, location)
    if value <= 0:
        raise WorkflowError(
            "Runtime state file has invalid schema: "
            f"'{key}' must be positive in '{location}' in {path}"
        )
    return value


def _require_int(record: Dict[str, Any], key: str, path: Path, location: str) -> int:
    value = record.get(key)
    if not isinstance(value, int):
        raise WorkflowError(
            "Runtime state file has invalid schema: "
            f"'{key}' must be an integer in '{location}' in {path}"
        )
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
