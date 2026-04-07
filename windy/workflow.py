from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Optional

from .current_space import (
    derive_workflow_space_from_window,
    query_eligible_windows,
    query_focused_window_record,
    query_recent_window_record,
    query_window_record,
    resolve_current_space_target,
    validate_workflow_space,
)
from .eligibility import is_eligible_window
from .errors import WorkflowError
from .hammerspoon import HammerspoonClient
from .models import EligibleWorkflowSpace, LiveTile, NormalizedFrame, PendingSplit, RuntimeState, TrackedSpaceState
from .state import RuntimeStateStore
from .yabai import YabaiClient

SUPPORTED_SPLIT_DIRECTIONS = frozenset({"east", "south"})
SUPPORTED_NAVIGATION_DIRECTIONS = frozenset({"north", "east", "south", "west"})


@dataclass(frozen=True)
class _LiveSpaceSnapshot:
    workflow_space: EligibleWorkflowSpace
    tiles: list[LiveTile]
    tile_index_by_window_id: dict[int, int]

    def tile_for_window(self, window_id: int) -> Optional[LiveTile]:
        tile_index = self.tile_index_by_window_id.get(window_id)
        if tile_index is None:
            return None
        return self.tiles[tile_index]


@dataclass(frozen=True)
class _CurrentContext:
    state: RuntimeState
    workflow_space: EligibleWorkflowSpace
    focused_window_id: int
    tracked: Optional[TrackedSpaceState]
    snapshot: Optional[_LiveSpaceSnapshot]


class WorkflowRuntime:
    def __init__(
        self,
        *,
        yabai: YabaiClient,
        hammerspoon: HammerspoonClient,
        state_store: RuntimeStateStore,
        windy_bin: str,
    ):
        self._yabai = yabai
        self._hammerspoon = hammerspoon
        self._state_store = state_store
        self._windy_bin = windy_bin

    def navigate(self, direction: str) -> None:
        normalized_direction = direction.strip().lower()
        if normalized_direction not in SUPPORTED_NAVIGATION_DIRECTIONS:
            raise WorkflowError(f"Unsupported navigation direction: {direction}")

        context = self._current_context(
            allowed_layouts=("bsp",),
            require_tracked=True,
            raise_if_untracked=False,
        )
        if context is None:
            return
        self._yabai.focus_window_direction(normalized_direction)

    def reseed(self) -> None:
        state = self._state_store.read()
        target = resolve_current_space_target(
            self._yabai,
            allowed_layouts=("bsp", "stack", "float"),
        )
        eligible_windows = query_eligible_windows(
            self._yabai,
            workflow_space=target.workflow_space,
        )
        eligible_window_ids = [int(window["id"]) for window in eligible_windows]
        if target.focused_window_id not in eligible_window_ids:
            raise WorkflowError(
                "Focused window is not an eligible workflow window in the current "
                "eligible workflow space."
            )

        hidden_window_ids = [
            window_id
            for window_id in eligible_window_ids
            if window_id != target.focused_window_id
        ]

        self._yabai.set_space_layout(target.workflow_space.space, "bsp")
        for window_id in hidden_window_ids:
            self._yabai.stack_window(target.focused_window_id, window_id)
        self._yabai.focus_window(target.focused_window_id)

        self._state_store.write(
            _replace_space_state(
                state,
                TrackedSpaceState(
                    workflow_space=target.workflow_space,
                    pending_split=None,
                ),
            )
        )

        try:
            self._yabai.remove_signal("windy_absorb")
        except WorkflowError:
            pass
        self._yabai.add_signal(
            label="windy_absorb",
            event="window_created",
            action=f"{self._windy_bin} on-window-created --window-id $YABAI_WINDOW_ID",
        )

    def split(self, direction: str) -> None:
        normalized_direction = direction.strip().lower()
        if normalized_direction not in SUPPORTED_SPLIT_DIRECTIONS:
            raise WorkflowError(f"Unsupported split direction: {direction}")

        context = self._current_context(
            allowed_layouts=("bsp",),
            require_tracked=True,
            raise_if_untracked=True,
        )
        if context is None or context.snapshot is None or context.tracked is None:
            return

        focused_tile = context.snapshot.tile_for_window(context.focused_window_id)
        if focused_tile is None:
            return

        candidate = _choose_split_candidate(context.snapshot, focused_tile)
        if candidate is None:
            self._yabai.arm_window_split(focused_tile.visible_window_id, normalized_direction)
            self._state_store.write(
                _replace_space_state(
                    context.state,
                    replace(
                        context.tracked,
                        pending_split=PendingSplit(
                            direction=normalized_direction,
                            anchor_window_id=focused_tile.visible_window_id,
                            anchor_frame=focused_tile.frame,
                        ),
                    ),
                )
            )
            return

        candidate_tile = context.snapshot.tile_for_window(candidate)
        if candidate_tile is None:
            return

        if candidate_tile.frame == focused_tile.frame:
            self._yabai.promote_stacked_window(candidate, normalized_direction)
        else:
            self._yabai.arm_window_split(focused_tile.visible_window_id, normalized_direction)
            self._yabai.warp_window(candidate, focused_tile.visible_window_id)
        self._yabai.focus_window(focused_tile.visible_window_id)

        self._state_store.write(
            _replace_space_state(
                context.state,
                replace(context.tracked, pending_split=None),
            )
        )

    def delete_tile(self) -> None:
        context = self._current_context(
            allowed_layouts=("bsp",),
            require_tracked=True,
            raise_if_untracked=False,
        )
        if context is None or context.snapshot is None or context.tracked is None:
            return

        focused_tile = context.snapshot.tile_for_window(context.focused_window_id)
        if focused_tile is None or len(context.snapshot.tiles) <= 1:
            return

        anchor_tile = _choose_delete_anchor_tile(
            self._yabai,
            snapshot=context.snapshot,
            deleted_window_id=context.focused_window_id,
        )
        if anchor_tile is None:
            return

        for window_id in focused_tile.all_window_ids:
            self._yabai.stack_window(anchor_tile.visible_window_id, window_id)
        self._yabai.focus_window(anchor_tile.visible_window_id)

    def float_space(self) -> None:
        state = self._state_store.read()
        context = self._current_context(
            allowed_layouts=("bsp", "stack", "float"),
            require_tracked=False,
            raise_if_untracked=False,
            initial_state=state,
        )
        if context is None or context.tracked is None:
            return

        self._yabai.set_space_layout(context.workflow_space.space, "float")
        updated_state = _delete_space_state(context.state, context.workflow_space)
        self._state_store.write(updated_state)
        if not updated_state.spaces:
            try:
                self._yabai.remove_signal("windy_absorb")
            except WorkflowError:
                pass

    def alttab(self, *, origin_window_id: int, selected_window_id: int, origin_open_frame: NormalizedFrame, selected_open_frame: NormalizedFrame, selected_was_visible_at_open: bool) -> None:
        if origin_window_id == selected_window_id:
            return

        state = self._state_store.read()
        origin_window = _query_window_record_or_none(self._yabai, origin_window_id)
        if origin_window is None:
            return
        origin_workflow_space = derive_workflow_space_from_window(
            origin_window,
            description=f"origin window {origin_window_id}",
        )
        tracked = state.spaces.get(origin_workflow_space.storage_key)
        if tracked is None:
            return
        if not is_eligible_window(
            origin_window,
            target_display=origin_workflow_space.display,
            target_space=origin_workflow_space.space,
        ):
            return

        try:
            validate_workflow_space(
                self._yabai,
                workflow_space=origin_workflow_space,
                allowed_layouts=("bsp",),
            )
        except WorkflowError:
            self._state_store.write(_delete_space_state(state, origin_workflow_space))
            return

        selected_window = _query_window_record_or_none(self._yabai, selected_window_id)
        if selected_window is None:
            return
        selected_workflow_space = derive_workflow_space_from_window(
            selected_window,
            description=f"selected window {selected_window_id}",
        )
        if selected_workflow_space != origin_workflow_space:
            return
        if not is_eligible_window(
            selected_window,
            target_display=selected_workflow_space.display,
            target_space=selected_workflow_space.space,
        ):
            return

        snapshot = self._live_snapshot(origin_workflow_space)
        state, tracked = self._reconcile_tracked_space(state, tracked, snapshot)
        if tracked is None:
            return

        origin_tile = snapshot.tile_for_window(origin_window_id)
        if origin_tile is None:
            return

        if selected_open_frame == origin_open_frame:
            self._yabai.focus_window(selected_window_id)
            return

        if selected_was_visible_at_open:
            selected_tile = snapshot.tile_for_window(selected_window_id)
            if selected_tile is None:
                return
            self._yabai.swap_window(origin_tile.visible_window_id, selected_tile.visible_window_id)
            self._yabai.focus_window(selected_window_id)
            return

        self._yabai.stack_window(origin_tile.visible_window_id, selected_window_id)
        self._yabai.focus_window(selected_window_id)


    def on_window_created(self, window_id: int) -> None:
        self._yabai.rediscover_window(window_id)

        window = _query_window_record_or_none(self._yabai, window_id)
        if window is None:
            return

        workflow_space = _derive_workflow_space_or_none(
            window, description=f"new window {window_id}",
        )
        if workflow_space is None:
            return

        state = self._state_store.read()
        tracked = state.spaces.get(workflow_space.storage_key)
        if tracked is None:
            return

        try:
            validate_workflow_space(
                self._yabai,
                workflow_space=workflow_space,
                allowed_layouts=("bsp",),
            )
        except WorkflowError:
            return

        if not is_eligible_window(
            window,
            target_display=workflow_space.display,
            target_space=workflow_space.space,
        ):
            return

        snapshot = self._live_snapshot(workflow_space)

        had_pending_split = tracked.pending_split is not None
        state, tracked = self._reconcile_tracked_space(state, tracked, snapshot)
        if tracked is None:
            return
        pending_split_consumed = had_pending_split and tracked.pending_split is None

        anchor_tile = _find_anchor_tile(snapshot)
        if anchor_tile is None:
            return

        absorbed: list[int] = []
        for tile in snapshot.tiles:
            if tile.frame == anchor_tile.frame:
                continue
            if len(tile.all_window_ids) != 1:
                continue
            solo_id = tile.visible_window_id
            if pending_split_consumed and solo_id == window_id:
                continue
            self._yabai.stack_window(anchor_tile.visible_window_id, solo_id)
            absorbed.append(solo_id)

        if window_id in absorbed:
            self._yabai.focus_window(window_id)

    def _current_context(
        self,
        *,
        allowed_layouts: Iterable[str],
        require_tracked: bool,
        raise_if_untracked: bool,
        initial_state: Optional[RuntimeState] = None,
    ) -> Optional[_CurrentContext]:
        state = initial_state or self._state_store.read()
        focused_window = _query_focused_window_record_or_none(self._yabai)
        if focused_window is None:
            return None

        workflow_space = _derive_workflow_space_or_none(
            focused_window,
            description="focused window",
        )
        if workflow_space is None:
            return None

        tracked = state.spaces.get(workflow_space.storage_key)
        try:
            validate_workflow_space(
                self._yabai,
                workflow_space=workflow_space,
                allowed_layouts=allowed_layouts,
            )
        except WorkflowError:
            if tracked is not None:
                state = _delete_space_state(state, workflow_space)
                self._state_store.write(state)
            if raise_if_untracked:
                raise
            return None

        focused_window_id = int(focused_window["id"])
        if not is_eligible_window(
            focused_window,
            target_display=workflow_space.display,
            target_space=workflow_space.space,
        ):
            if raise_if_untracked:
                raise WorkflowError(
                    "Focused window is not an eligible workflow window in the current "
                    "eligible workflow space."
                )
            return None

        snapshot = None
        if tracked is not None:
            snapshot = self._live_snapshot(workflow_space)
            state, tracked = self._reconcile_tracked_space(state, tracked, snapshot)

        if require_tracked and tracked is None:
            if raise_if_untracked:
                raise WorkflowError("Current space is not tracked. Run `windy reseed` first.")
            return None

        return _CurrentContext(
            state=state,
            workflow_space=workflow_space,
            focused_window_id=focused_window_id,
            tracked=tracked,
            snapshot=snapshot,
        )

    def _reconcile_tracked_space(
        self,
        state: RuntimeState,
        tracked: TrackedSpaceState,
        snapshot: _LiveSpaceSnapshot,
    ) -> tuple[RuntimeState, Optional[TrackedSpaceState]]:
        pending_split = tracked.pending_split
        if pending_split is None:
            return state, tracked

        anchor_tile = snapshot.tile_for_window(pending_split.anchor_window_id)
        if anchor_tile is not None and anchor_tile.frame == pending_split.anchor_frame:
            return state, tracked

        updated = replace(tracked, pending_split=None)
        state = _replace_space_state(state, updated)
        self._state_store.write(state)
        return state, updated

    def _live_snapshot(self, workflow_space: EligibleWorkflowSpace) -> _LiveSpaceSnapshot:
        eligible_windows = query_eligible_windows(
            self._yabai,
            workflow_space=workflow_space,
        )
        ordered_window_ids = self._hammerspoon.ordered_window_ids()
        eligible_window_ids = {int(window["id"]) for window in eligible_windows}
        ordered_window_ids = [
            window_id
            for window_id in ordered_window_ids
            if window_id in eligible_window_ids
        ]

        windows_by_frame: dict[NormalizedFrame, list[dict[str, Any]]] = {}
        for window in eligible_windows:
            frame = _normalized_frame(window)
            windows_by_frame.setdefault(frame, []).append(dict(window))

        tiles: list[LiveTile] = []
        tile_index_by_window_id: dict[int, int] = {}
        for frame in sorted(windows_by_frame, key=_frame_sort_key):
            records = windows_by_frame[frame]
            record_ids = {int(record["id"]) for record in records}
            group_order = [window_id for window_id in ordered_window_ids if window_id in record_ids]
            for window_id in sorted(record_ids):
                if window_id not in group_order:
                    group_order.append(window_id)
            tile = LiveTile(
                frame=frame,
                visible_window_id=group_order[0],
                background_window_ids=group_order[1:],
            )
            tile_index = len(tiles)
            tiles.append(tile)
            for window_id in tile.all_window_ids:
                tile_index_by_window_id[window_id] = tile_index

        return _LiveSpaceSnapshot(
            workflow_space=workflow_space,
            tiles=tiles,
            tile_index_by_window_id=tile_index_by_window_id,
        )


def _replace_space_state(state: RuntimeState, tracked: TrackedSpaceState) -> RuntimeState:
    spaces = dict(state.spaces)
    spaces[tracked.workflow_space.storage_key] = tracked
    return replace(state, spaces=spaces)


def _delete_space_state(state: RuntimeState, workflow_space: EligibleWorkflowSpace) -> RuntimeState:
    spaces = dict(state.spaces)
    spaces.pop(workflow_space.storage_key, None)
    return replace(state, spaces=spaces)


def _choose_split_candidate(snapshot: _LiveSpaceSnapshot, focused_tile: LiveTile) -> Optional[int]:
    if focused_tile.background_window_ids:
        return focused_tile.background_window_ids[0]

    for tile in snapshot.tiles:
        if tile.frame == focused_tile.frame:
            continue
        if tile.background_window_ids:
            return tile.background_window_ids[0]
    return None


def _choose_delete_anchor_tile(
    yabai: YabaiClient,
    *,
    snapshot: _LiveSpaceSnapshot,
    deleted_window_id: int,
) -> Optional[LiveTile]:
    recent_window = _query_recent_window_or_none(yabai)
    if recent_window is not None:
        recent_tile = snapshot.tile_for_window(int(recent_window["id"]))
        deleted_tile = snapshot.tile_for_window(deleted_window_id)
        if (
            recent_tile is not None
            and deleted_tile is not None
            and recent_tile.frame != deleted_tile.frame
        ):
            return recent_tile

    deleted_tile = snapshot.tile_for_window(deleted_window_id)
    for tile in snapshot.tiles:
        if deleted_tile is None or tile.frame != deleted_tile.frame:
            return tile
    return None


def _find_anchor_tile(snapshot: _LiveSpaceSnapshot) -> Optional[LiveTile]:
    if not snapshot.tiles:
        return None
    return max(snapshot.tiles, key=lambda t: len(t.all_window_ids))


def _normalized_frame(window: Mapping[str, Any]) -> NormalizedFrame:
    raw_frame = window.get("frame")
    if not isinstance(raw_frame, Mapping):
        raise WorkflowError("Expected yabai window queries to provide a frame object.")
    return NormalizedFrame(
        x=_require_frame_int(raw_frame, "x"),
        y=_require_frame_int(raw_frame, "y"),
        w=_require_positive_frame_int(raw_frame, "w"),
        h=_require_positive_frame_int(raw_frame, "h"),
    )


def _frame_sort_key(frame: NormalizedFrame) -> tuple[int, int, int, int]:
    return (frame.y, frame.x, frame.w, frame.h)


def _require_frame_int(frame: Mapping[str, Any], key: str) -> int:
    value = frame.get(key)
    if not isinstance(value, (int, float)):
        raise WorkflowError(f"Expected yabai window frames to provide numeric '{key}'.")
    return int(round(value))


def _require_positive_frame_int(frame: Mapping[str, Any], key: str) -> int:
    value = _require_frame_int(frame, key)
    if value <= 0:
        raise WorkflowError(f"Expected yabai window frames to provide positive '{key}'.")
    return value


def _derive_workflow_space_or_none(
    window: Optional[Mapping[str, Any]],
    *,
    description: str,
) -> Optional[EligibleWorkflowSpace]:
    if window is None:
        return None
    try:
        return derive_workflow_space_from_window(window, description=description)
    except WorkflowError:
        return None


def _query_window_record_or_none(yabai: YabaiClient, window_id: int) -> Optional[dict[str, Any]]:
    try:
        return query_window_record(
            yabai,
            window_id=window_id,
            description=f"window {window_id}",
        )
    except WorkflowError:
        return None


def _query_focused_window_record_or_none(yabai: YabaiClient) -> Optional[dict[str, Any]]:
    try:
        return query_focused_window_record(
            yabai,
            description="focused window",
        )
    except WorkflowError:
        return None


def _query_recent_window_or_none(yabai: YabaiClient) -> Optional[dict[str, Any]]:
    try:
        return query_recent_window_record(
            yabai,
            description="recent window",
        )
    except WorkflowError:
        return None
