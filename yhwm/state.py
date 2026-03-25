from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, Optional

from .errors import WorkflowError
from .models import (
    AltTabSession,
    EligibleWorkflowSpace,
    FocusGuard,
    ManagedSpaceState,
    ManagedTile,
    PendingSplit,
    RuntimeState,
)

SUPPORTED_SPLIT_DIRECTIONS = frozenset({"east", "south"})


class RuntimeStateStore:
    def __init__(self, path: Path):
        self._path = path

    @staticmethod
    def default_path() -> Path:
        runtime_root = Path(__file__).resolve().parents[1]
        return runtime_root / "state" / "yhwm-state-v3.json"

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
            "schema_version": 3,
            "spaces": {
                storage_key: {
                    "display": managed.workflow_space.display,
                    "space": managed.workflow_space.space,
                    "tiles": {
                        str(tile_id): {
                            "tile_id": tile.tile_id,
                            "visible_window_id": tile.visible_window_id,
                            "hidden_window_ids": list(tile.hidden_window_ids),
                            "updated_at": _utc_now(),
                        }
                        for tile_id, tile in sorted(managed.tiles.items())
                    },
                    "last_focused_tile_id": managed.last_focused_tile_id,
                    "next_tile_id": managed.next_tile_id,
                    "updated_at": _utc_now(),
                    **(
                        {
                            "pending_split": {
                                "tile_id": managed.pending_split.tile_id,
                                "direction": managed.pending_split.direction,
                                "updated_at": _utc_now(),
                            }
                        }
                        if managed.pending_split is not None
                        else {}
                    ),
                }
                for storage_key, managed in state.spaces.items()
            },
            "alttab": {
                "session": None,
                "focus_guard": None,
            },
        }

        if state.alttab_session is not None:
            payload["alttab"]["session"] = {
                "origin_window_id": state.alttab_session.origin_window_id,
                "origin_tile_id": state.alttab_session.origin_tile_id,
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
        if schema_version != 3:
            raise WorkflowError(
                "Runtime state file has invalid schema: "
                f"expected 'schema_version' 3 in {self._path}"
            )

        raw_spaces = payload.get("spaces")
        if not isinstance(raw_spaces, dict):
            raise WorkflowError(
                f"Runtime state file has invalid schema: 'spaces' must be an object in {self._path}"
            )

        spaces: Dict[str, ManagedSpaceState] = {}
        for storage_key, raw_entry in raw_spaces.items():
            workflow_space = self._parse_storage_key(storage_key)
            raw_space = _require_object(raw_entry, self._path, storage_key)
            display = _require_positive_int(raw_space, "display", self._path, storage_key)
            space = _require_positive_int(raw_space, "space", self._path, storage_key)
            if display != workflow_space.display or space != workflow_space.space:
                raise WorkflowError(
                    "Runtime state file has invalid schema: "
                    f"space entry '{storage_key}' does not match its key in {self._path}"
                )

            raw_tiles = raw_space.get("tiles")
            if not isinstance(raw_tiles, dict) or not raw_tiles:
                raise WorkflowError(
                    "Runtime state file has invalid schema: "
                    f"'tiles' must be a non-empty object for '{storage_key}' in {self._path}"
                )
            tiles = self._parse_tiles(raw_tiles, storage_key)

            last_focused_tile_id = _require_positive_int(
                raw_space,
                "last_focused_tile_id",
                self._path,
                storage_key,
            )
            if last_focused_tile_id not in tiles:
                raise WorkflowError(
                    "Runtime state file has invalid schema: "
                    f"'last_focused_tile_id' must reference a tile in '{storage_key}' in {self._path}"
                )

            next_tile_id = _require_positive_int(raw_space, "next_tile_id", self._path, storage_key)
            if next_tile_id <= max(tiles):
                raise WorkflowError(
                    "Runtime state file has invalid schema: "
                    f"'next_tile_id' must be greater than every tile id in '{storage_key}' in {self._path}"
                )

            pending_split = self._parse_pending_split(
                raw_space.get("pending_split"),
                storage_key=storage_key,
                tiles=tiles,
            )

            spaces[storage_key] = ManagedSpaceState(
                workflow_space=workflow_space,
                tiles=tiles,
                last_focused_tile_id=last_focused_tile_id,
                next_tile_id=next_tile_id,
                pending_split=pending_split,
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

    def _parse_tiles(self, raw_tiles: Dict[str, Any], storage_key: str) -> Dict[int, ManagedTile]:
        tiles: Dict[int, ManagedTile] = {}
        seen_window_ids: set[int] = set()
        for tile_key, raw_entry in raw_tiles.items():
            if not isinstance(tile_key, str):
                raise WorkflowError(
                    "Runtime state file has invalid schema: "
                    f"tile keys must be text in '{storage_key}' in {self._path}"
                )
            try:
                tile_id = int(tile_key)
            except ValueError as exc:
                raise WorkflowError(
                    "Runtime state file has invalid schema: "
                    f"tile key '{tile_key}' must be an integer in '{storage_key}' in {self._path}"
                ) from exc
            if tile_id <= 0 or str(tile_id) != tile_key:
                raise WorkflowError(
                    "Runtime state file has invalid schema: "
                    f"tile key '{tile_key}' is not canonical in '{storage_key}' in {self._path}"
                )

            raw_tile = _require_object(raw_entry, self._path, f"{storage_key}:{tile_key}")
            stored_tile_id = _require_positive_int(
                raw_tile,
                "tile_id",
                self._path,
                f"{storage_key}:{tile_key}",
            )
            if stored_tile_id != tile_id:
                raise WorkflowError(
                    "Runtime state file has invalid schema: "
                    f"tile '{tile_key}' does not match its 'tile_id' in '{storage_key}' in {self._path}"
                )

            visible_window_id = _require_positive_int(
                raw_tile,
                "visible_window_id",
                self._path,
                f"{storage_key}:{tile_key}",
            )
            hidden_window_ids = _require_positive_int_list(
                raw_tile,
                "hidden_window_ids",
                self._path,
                f"{storage_key}:{tile_key}",
            )
            if visible_window_id in hidden_window_ids:
                raise WorkflowError(
                    "Runtime state file has invalid schema: "
                    f"tile '{tile_key}' repeats its visible window in hidden ownership in '{storage_key}' in {self._path}"
                )
            tile_window_ids = [visible_window_id, *hidden_window_ids]
            if any(window_id in seen_window_ids for window_id in tile_window_ids):
                raise WorkflowError(
                    "Runtime state file has invalid schema: "
                    f"window ownership overlaps between tiles in '{storage_key}' in {self._path}"
                )
            seen_window_ids.update(tile_window_ids)

            tiles[tile_id] = ManagedTile(
                tile_id=tile_id,
                visible_window_id=visible_window_id,
                hidden_window_ids=hidden_window_ids,
            )
        return tiles

    def _parse_pending_split(
        self,
        raw_pending_split: Any,
        *,
        storage_key: str,
        tiles: Dict[int, ManagedTile],
    ) -> Optional[PendingSplit]:
        if raw_pending_split is None:
            return None
        pending_split = _require_object(raw_pending_split, self._path, f"{storage_key}:pending_split")
        tile_id = _require_positive_int(pending_split, "tile_id", self._path, f"{storage_key}:pending_split")
        if tile_id not in tiles:
            raise WorkflowError(
                "Runtime state file has invalid schema: "
                f"'pending_split.tile_id' must reference a tile in '{storage_key}' in {self._path}"
            )
        direction = pending_split.get("direction")
        if not isinstance(direction, str):
            raise WorkflowError(
                "Runtime state file has invalid schema: "
                f"'pending_split.direction' must be text in '{storage_key}' in {self._path}"
            )
        normalized_direction = direction.strip().lower()
        if normalized_direction not in SUPPORTED_SPLIT_DIRECTIONS:
            raise WorkflowError(
                "Runtime state file has invalid schema: "
                f"'pending_split.direction' must be one of {sorted(SUPPORTED_SPLIT_DIRECTIONS)} "
                f"in '{storage_key}' in {self._path}"
            )
        return PendingSplit(tile_id=tile_id, direction=normalized_direction)

    def _parse_session(self, raw_session: Any) -> Optional[AltTabSession]:
        if raw_session is None:
            return None
        if not isinstance(raw_session, dict):
            raise WorkflowError(
                f"Runtime state file has invalid schema: 'session' must be an object in {self._path}"
            )
        origin_window_id = _require_positive_int(raw_session, "origin_window_id", self._path, "session")
        origin_tile_id = _require_positive_int(raw_session, "origin_tile_id", self._path, "session")
        origin_display = _require_positive_int(raw_session, "origin_display", self._path, "session")
        origin_space = _require_positive_int(raw_session, "origin_space", self._path, "session")
        return AltTabSession(
            origin_window_id=origin_window_id,
            origin_tile_id=origin_tile_id,
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


def _require_object(value: Any, path: Path, context: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowError(
            "Runtime state file has invalid schema: "
            f"'{context}' must be an object in {path}"
        )
    return value


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
